"""Shared intraday feature computation for backtest and live execution.

``feature_builder.py`` writes raw features to processed ``signals.csv``;
``historical_baselines.transform_rows`` z-scores them into
``signals_unconditional.csv``. Live execution uses the same raw formulas here,
then the same z-score lookup against rolling baselines.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Dict, List, Optional, Sequence, Tuple

from historical_baselines import FEATURES, minute_key, zscore
from mbh_simulator import OptionQuote, SignalSnapshot
from rv_feature import atm_iv_from_pair, realized_vs_implied_raw


@dataclass(frozen=True)
class MinuteFeatureSample:
    timestamp: datetime
    spot: float
    quotes: List[OptionQuote]
    observation_count: int


class DeterministicMinuteSampler:
    """Aggregate a fixed boundary window so alpha is not tied to poll timing."""

    def __init__(
        self,
        *,
        sample_offset_seconds: float = 1.0,
        sample_window_seconds: float = 1.0,
        min_observations: int = 2,
        max_wait_seconds: float = 1.0,
    ) -> None:
        self.sample_offset_seconds = max(float(sample_offset_seconds), 0.0)
        self.sample_window_seconds = max(float(sample_window_seconds), 0.0)
        self.min_observations = max(int(min_observations), 1)
        self.max_wait_seconds = max(float(max_wait_seconds), 0.0)
        self._minute: Optional[datetime] = None
        self._observations: List[Tuple[float, List[OptionQuote]]] = []

    @staticmethod
    def _minute_for(ts: datetime) -> datetime:
        return ts.replace(second=0, microsecond=0, tzinfo=None)

    def _roll(self, now: datetime) -> datetime:
        minute = self._minute_for(now)
        if self._minute != minute:
            self._minute = minute
            self._observations = []
        return minute

    def observe(self, now: datetime, spot: float, quotes: Sequence[OptionQuote]) -> None:
        minute = self._roll(now)
        elapsed = (now.replace(tzinfo=None) - minute).total_seconds()
        window_start = max(self.sample_offset_seconds - self.sample_window_seconds, 0.0)
        if elapsed < window_start:
            return
        if elapsed > self.sample_offset_seconds + self.max_wait_seconds:
            return
        valid = [q for q in quotes if q.bid > 0 and q.ask > q.bid]
        if math.isfinite(float(spot)) and spot > 0 and valid:
            self._observations.append((float(spot), list(valid)))

    def status(self, now: datetime) -> str:
        minute = self._roll(now)
        elapsed = (now.replace(tzinfo=None) - minute).total_seconds()
        count = len(self._observations)
        if elapsed >= self.sample_offset_seconds and count >= self.min_observations:
            return "ready"
        if elapsed >= self.sample_offset_seconds + self.max_wait_seconds:
            return "ready" if count else "unavailable"
        return "collecting"

    def aggregate(self, now: datetime) -> Optional[MinuteFeatureSample]:
        if self.status(now) != "ready" or not self._observations:
            return None
        minute = self._minute_for(now)
        by_key: Dict[Tuple[str, str, float], List[OptionQuote]] = {}
        for _, quotes in self._observations:
            for quote in quotes:
                key = (str(quote.expiry), quote.option_type, float(quote.strike))
                by_key.setdefault(key, []).append(quote)

        def optional_median(rows: Sequence[OptionQuote], field_name: str) -> Optional[float]:
            values = [float(value) for row in rows if (value := getattr(row, field_name)) is not None and math.isfinite(float(value))]
            return statistics.median(values) if values else None

        quotes_out: List[OptionQuote] = []
        spot = statistics.median(row[0] for row in self._observations)
        for (expiry, option_type, strike), rows in sorted(by_key.items()):
            quotes_out.append(OptionQuote(
                timestamp=minute,
                expiry=expiry,
                option_type=option_type,
                strike=strike,
                bid=statistics.median(row.bid for row in rows),
                ask=statistics.median(row.ask for row in rows),
                delta=optional_median(rows, "delta"),
                iv=optional_median(rows, "iv"),
                underlying_price=spot,
            ))
        return MinuteFeatureSample(minute, spot, quotes_out, len(self._observations))


@dataclass
class SessionFeatureState:
    """Intraday state carried across tranche polls within one session."""

    first_straddle: Optional[float] = None
    first_minutes: Optional[float] = None
    previous_spot: Optional[float] = None
    spot_history: List[float] = field(default_factory=list)
    last_sample_minute: Optional[str] = None
    last_raw_features: Dict[str, float] = field(default_factory=dict)
    # True once the cached minute's term_ratio_z was computed with next-expiry
    # quotes. Non-tranche polls have no next-expiry chain, so the first poll of
    # a minute can cache term_ratio_z=0.0; the tranche poll must be able to
    # upgrade it in place (see compute_raw_features_once_per_minute).
    last_minute_had_next_expiry: bool = False
    # Forensic skew-leg snapshot for the canonical minute (strikes/IVs/spot).
    # Telemetry only — deliberately not persisted by to_dict/from_dict.
    last_raw_components: Dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "first_straddle": self.first_straddle,
            "first_minutes": self.first_minutes,
            "previous_spot": self.previous_spot,
            # Cap history so mid-day restarts stay cheap to serialize.
            "spot_history": list(self.spot_history[-390:]),
            "last_sample_minute": self.last_sample_minute,
            "last_raw_features": dict(self.last_raw_features),
            "last_minute_had_next_expiry": self.last_minute_had_next_expiry,
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
            last_sample_minute=payload.get("last_sample_minute"),
            last_raw_features={
                str(k): float(v)
                for k, v in (payload.get("last_raw_features") or {}).items()
            },
            last_minute_had_next_expiry=bool(
                payload.get("last_minute_had_next_expiry", False)
            ),
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
    quotes: Sequence[OptionQuote],
    option_type: str,
    target_abs_delta: float,
    *,
    require_iv: bool = False,
) -> Optional[OptionQuote]:
    """Nearest-|delta| quote of the given right.

    ``require_iv`` restricts candidates to quotes with a usable implied vol.
    The skew feature is ``put_iv - call_iv``: without the filter, a leg whose
    IV the feed withheld selects normally and then contributes 0.0, collapsing
    skew to ±other_leg_iv (~0.20 raw ≈ +6-8 z) — under the sanity bound, so it
    would trade. It also aligns this selector's pool with the one
    ``feature_input_health`` validates (which is already IV-filtered).
    """
    right = option_type.upper()
    candidates = [
        q
        for q in quotes
        if q.option_type.upper() == right and q.delta is not None and not math.isnan(q.delta)
    ]
    if require_iv:
        candidates = [
            q
            for q in candidates
            if q.iv is not None and math.isfinite(float(q.iv)) and float(q.iv) > 0
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

    # require_iv: an IV-less leg must fail selection (skew falls back to 0.0)
    # rather than silently zeroing one side of the difference.
    put_25 = choose_delta(quotes, "PUT", 0.25, require_iv=True)
    call_25 = choose_delta(quotes, "CALL", 0.25, require_iv=True)
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

    # Forensic snapshot of the skew inputs. skew_z is IV-source-sensitive
    # (IB modelGreeks live vs ThetaData in the backtest baselines), and the
    # legs are otherwise discarded here — without this record a post-session
    # parity investigation cannot tell WHICH strike/IV produced the z-score.
    state.last_raw_components = {
        "put25_strike": put_25.strike if put_25 else None,
        "put25_iv": float(put_25.iv) if put_25 and put_25.iv is not None else None,
        "call25_strike": call_25.strike if call_25 else None,
        "call25_iv": float(call_25.iv) if call_25 and call_25.iv is not None else None,
        "atm_straddle": round(straddle, 4),
        "atm_iv": round(atm_iv, 6) if atm_iv is not None and math.isfinite(atm_iv) else None,
        "spot": round(float(spot), 3),
    }

    return {
        "straddle_residual_z": straddle_residual_z,
        "skew_z": skew_z,
        "term_ratio_z": term_ratio_z,
        "trend_score": trend_score,
        "realized_vs_implied_z": realized,
    }


def compute_raw_features_once_per_minute(
    quotes: Sequence[OptionQuote],
    spot: float,
    ts: datetime,
    state: SessionFeatureState,
    next_expiry_quotes: Optional[Sequence[OptionQuote]] = None,
) -> Dict[str, float]:
    """Advance state on the same one-minute clock used by the backtest.

    The executor polls sub-second near stops.  Those reads must not become
    hundreds of synthetic observations or redefine ``previous_spot``.  The
    first valid quote set in each minute is canonical and subsequent polls
    receive a copy of the cached raw features.
    """
    sample_ts = ts.replace(second=0, microsecond=0, tzinfo=None)
    minute = sample_ts.isoformat(timespec="minutes")
    if state.last_sample_minute == minute and state.last_raw_features:
        if next_expiry_quotes and not state.last_minute_had_next_expiry:
            # The canonical observation for this minute came from a poll with
            # no next-expiry chain, so term_ratio_z was cached as 0.0 — which
            # z-scores far from 0 (e.g. +3.86 at 09:32) and can spuriously
            # gate every candidate with term_structure_dislocation. Upgrade
            # just the term ratio in place; never re-advance session state.
            straddle, _, _ = _atm_straddle_from_quotes(quotes, spot)
            next_straddle, _, _ = _atm_straddle_from_quotes(next_expiry_quotes, spot)
            if (
                math.isfinite(straddle) and straddle > 0
                and math.isfinite(next_straddle) and next_straddle > 0
            ):
                state.last_raw_features["term_ratio_z"] = (straddle / next_straddle) - 1.0
                state.last_minute_had_next_expiry = True
        return dict(state.last_raw_features)

    before = len(state.spot_history)
    raw = compute_raw_features(
        quotes,
        spot,
        sample_ts,
        state,
        next_expiry_quotes=next_expiry_quotes,
    )
    # An empty/unmarkable chain does not consume the minute; a later warm quote
    # may still establish the canonical observation.
    if len(state.spot_history) > before:
        state.last_sample_minute = minute
        state.last_raw_features = dict(raw)
        state.last_minute_had_next_expiry = bool(next_expiry_quotes)
    return raw


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


def signal_features_are_sane(
    signal: SignalSnapshot, *, max_abs_z: float,
) -> bool:
    """Reject non-finite or implausibly extreme vendor-derived z-scores."""
    if max_abs_z <= 0:
        return True
    values = (
        signal.straddle_residual_z,
        signal.skew_z,
        signal.term_ratio_z,
        signal.trend_score,
        signal.realized_vs_implied_z,
    )
    return all(math.isfinite(float(value)) and abs(float(value)) <= max_abs_z for value in values)


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
    meta_keys = {"generated_at", "train_dates", "train_count", "train_source", "train_note"}
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
    # zscore_raw_features subscripts baselines["global"][feature] unguarded on
    # every minute; a structurally broken payload must die here at startup,
    # not as a mid-session KeyError.
    global_stats = payload.get("global")
    if not isinstance(global_stats, dict) or not global_stats:
        raise ValueError("baselines file missing global stats — run scripts/refresh_live_baselines.py")
    missing = [feature for feature in FEATURES if feature not in global_stats]
    if missing:
        raise ValueError(
            f"baselines global stats missing features {missing} — run scripts/refresh_live_baselines.py"
        )
    if not isinstance(payload.get("minutes"), dict):
        raise ValueError("baselines file missing minutes map — run scripts/refresh_live_baselines.py")
