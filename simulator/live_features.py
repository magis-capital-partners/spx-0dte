"""Shared intraday feature computation for backtest and live execution.

``feature_builder.py`` writes raw features to processed ``signals.csv``;
``historical_baselines.transform_rows`` z-scores them into
``signals_unconditional.csv``. Live execution uses the same raw formulas here,
then the same z-score lookup against rolling baselines.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, List, Optional, Sequence, Tuple

from historical_baselines import FEATURES, minute_key, zscore
from mbh_simulator import OptionQuote, SignalSnapshot
from rv_feature import atm_iv_from_pair, realized_vs_implied_raw


@dataclass
class SessionFeatureState:
    """Intraday state carried across tranche polls within one session."""

    first_straddle: Optional[float] = None
    first_minutes: Optional[float] = None
    previous_spot: Optional[float] = None
    spot_history: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "first_straddle": self.first_straddle,
            "first_minutes": self.first_minutes,
            "previous_spot": self.previous_spot,
            # Cap history so mid-day restarts stay cheap to serialize.
            "spot_history": list(self.spot_history[-390:]),
        }

    @classmethod
    def from_dict(cls, payload: Optional[dict]) -> "SessionFeatureState":
        if not payload:
            return cls()
        hist = payload.get("spot_history") or []
        return cls(
            first_straddle=payload.get("first_straddle"),
            first_minutes=payload.get("first_minutes"),
            previous_spot=payload.get("previous_spot"),
            spot_history=[float(x) for x in hist if x is not None],
        )


def minutes_to_close(ts: datetime, close: time = time(16, 0)) -> float:
    close_dt = ts.replace(hour=close.hour, minute=close.minute, second=0, microsecond=0)
    return max((close_dt - ts.replace(tzinfo=None)).total_seconds() / 60.0, 0.0)


def quote_mid(q: OptionQuote) -> float:
    if q.bid > 0 and q.ask > 0:
        return (q.bid + q.ask) / 2.0
    return math.nan


def choose_atm_pair(
    quotes: Sequence[OptionQuote], spot: float
) -> Tuple[Optional[OptionQuote], Optional[OptionQuote]]:
    calls = [q for q in quotes if q.option_type.upper() == "CALL"]
    puts = [q for q in quotes if q.option_type.upper() == "PUT"]
    if not calls or not puts or spot <= 0 or math.isnan(spot):
        return None, None
    strikes = sorted({q.strike for q in quotes})
    atm = min(strikes, key=lambda strike: abs(strike - spot))

    def pick(candidates: Sequence[OptionQuote]) -> Optional[OptionQuote]:
        near = [q for q in candidates if q.strike == atm]
        if near:
            return near[0]
        return min(
            candidates,
            key=lambda row: (
                abs(row.strike - atm),
                abs(abs(float(row.delta or 0.0)) - 0.5),
            ),
        )

    return pick(calls), pick(puts)


def choose_delta(
    quotes: Sequence[OptionQuote], option_type: str, target_abs_delta: float
) -> Optional[OptionQuote]:
    right = option_type.upper()
    candidates = [
        q
        for q in quotes
        if q.option_type.upper() == right and q.delta is not None and not math.isnan(q.delta)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(abs(float(row.delta)) - target_abs_delta))


def _atm_straddle_from_quotes(quotes: Sequence[OptionQuote], spot: float) -> Tuple[float, Optional[OptionQuote], Optional[OptionQuote]]:
    call, put = choose_atm_pair(quotes, spot)
    if not call or not put:
        return math.nan, call, put
    straddle = quote_mid(call) + quote_mid(put)
    if not math.isfinite(straddle) or straddle <= 0:
        return math.nan, call, put
    return straddle, call, put


def compute_raw_features(
    quotes: Sequence[OptionQuote],
    spot: float,
    ts: datetime,
    state: SessionFeatureState,
    next_expiry_quotes: Optional[Sequence[OptionQuote]] = None,
) -> Dict[str, float]:
    """Compute pre-zscore features matching ``feature_builder.build_for_day``."""
    straddle, atm_call, atm_put = _atm_straddle_from_quotes(quotes, spot)
    if not math.isfinite(straddle) or straddle <= 0:
        return {feature: 0.0 for feature in FEATURES}

    state.spot_history.append(float(spot))
    mins = minutes_to_close(ts)
    if state.first_straddle is None:
        state.first_straddle = straddle
        state.first_minutes = mins
    baseline = state.first_straddle
    if state.first_minutes and state.first_minutes > 0:
        baseline = state.first_straddle * (mins / state.first_minutes)
    straddle_residual_z = (straddle - baseline) / max(state.first_straddle, 1e-9)

    put_25 = choose_delta(quotes, "PUT", 0.25)
    call_25 = choose_delta(quotes, "CALL", 0.25)
    skew_z = 0.0
    if put_25 and call_25:
        put_iv = float(put_25.iv or 0.0)
        call_iv = float(call_25.iv or 0.0)
        skew_z = put_iv - call_iv

    term_ratio_z = 0.0
    if next_expiry_quotes:
        next_straddle, _, _ = _atm_straddle_from_quotes(next_expiry_quotes, spot)
        if math.isfinite(next_straddle) and next_straddle > 0:
            term_ratio_z = (straddle / next_straddle) - 1.0

    trend_score = 0.0
    if state.previous_spot is not None and straddle > 0:
        trend_score = (spot - state.previous_spot) / straddle
    state.previous_spot = spot

    atm_iv = atm_iv_from_pair(
        float(atm_call.iv) if atm_call and atm_call.iv is not None else None,
        float(atm_put.iv) if atm_put and atm_put.iv is not None else None,
    )
    realized = realized_vs_implied_raw(
        state.spot_history, spot=spot, straddle=straddle, atm_iv=atm_iv
    )

    return {
        "straddle_residual_z": straddle_residual_z,
        "skew_z": skew_z,
        "term_ratio_z": term_ratio_z,
        "trend_score": trend_score,
        "realized_vs_implied_z": realized,
    }


def zscore_raw_features(raw: Dict[str, float], baselines: dict, ts: datetime) -> Dict[str, float]:
    """Apply minute-of-day (fallback global) z-scores — same as ``transform_rows``."""
    key = minute_key(ts.isoformat())
    minute_stats = baselines.get("minutes", {}).get(key, {})
    out: Dict[str, float] = {}
    for feature in FEATURES:
        raw_value = float(raw.get(feature, 0.0))
        stats = minute_stats.get(feature, baselines["global"][feature])
        out[feature] = zscore(raw_value, stats)
    return out


def raw_to_signal_snapshot(
    raw: Dict[str, float], baselines: dict, ts: datetime
) -> SignalSnapshot:
    zscored = zscore_raw_features(raw, baselines, ts)
    return SignalSnapshot(
        timestamp=ts,
        straddle_residual_z=zscored["straddle_residual_z"],
        skew_z=zscored["skew_z"],
        term_ratio_z=zscored["term_ratio_z"],
        trend_score=zscored["trend_score"],
        realized_vs_implied_z=zscored["realized_vs_implied_z"],
    )


def split_session_quotes(
    quotes: Sequence[OptionQuote], session_date: str
) -> Tuple[List[OptionQuote], Optional[List[OptionQuote]]]:
    """Split a quote snapshot into same-day (0DTE) and next-expiry chains."""
    session = session_date[:10]
    same = [q for q in quotes if str(q.expiry)[:10] == session]
    expiries = sorted({str(q.expiry)[:10] for q in quotes})
    future = [e for e in expiries if e > session]
    if not future:
        return same, None
    nxt = future[0]
    nxt_quotes = [q for q in quotes if str(q.expiry)[:10] == nxt]
    return same, nxt_quotes if nxt_quotes else None


def baselines_payload_for_live(baselines: dict, train_dates: Sequence[str]) -> dict:
    """Wrap computed baselines with metadata for live staleness checks."""
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "train_dates": list(train_dates),
        "train_count": len(train_dates),
        **baselines,
    }


def extract_baselines_core(payload: dict) -> dict:
    """Strip metadata keys; return the object ``zscore_raw_features`` expects."""
    meta_keys = {"generated_at", "train_dates", "train_count"}
    return {k: v for k, v in payload.items() if k not in meta_keys}


def validate_baselines_freshness(payload: dict, max_age_days: int) -> None:
    generated = payload.get("generated_at")
    if not generated:
        raise ValueError("baselines file missing generated_at metadata — run scripts/refresh_live_baselines.py")
    try:
        created = datetime.fromisoformat(str(generated))
    except ValueError as exc:
        raise ValueError(f"invalid generated_at in baselines: {generated!r}") from exc
    age_days = (datetime.now() - created).total_seconds() / 86400.0
    if age_days > max_age_days:
        raise ValueError(
            f"baselines are {age_days:.1f} days old (max {max_age_days}) — "
            "run scripts/refresh_live_baselines.py"
        )
    if not payload.get("train_dates"):
        raise ValueError("baselines file missing train_dates — run scripts/refresh_live_baselines.py")
