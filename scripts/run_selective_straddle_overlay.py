"""Selective short-premium overlay suite: straddle + iron condor.

Production verticals simulated once per day; overlay variants applied on top.
IC skips low-vol / fee-dominated days by default (see selective_overlay_variants).

  python scripts/run_selective_straddle_overlay.py --phase B --shard 0 --shards 8 --resume
  python scripts/run_selective_straddle_overlay.py --phase A1c --resume
  python scripts/run_selective_straddle_overlay.py --phase CD --winners-json data/.../winners.json --resume
"""
from __future__ import annotations

import argparse
import json
import sys
import time as time_mod
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))
sys.path.insert(0, str(ROOT / "scripts"))

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    era_for_date,
    load_era_rules,
    resolve_start_date,
)
from long_vol_overlay import choose_atm_straddle, group_quotes, spot as overlay_spot  # noqa: E402
from mbh_simulator import (  # noqa: E402
    OptionQuote,
    normalize_option_type,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    snapshot_spot,
)
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import (  # noqa: E402
    PRODUCTION_MAX_CONTRACTS_PER_TRANCHE,
    PRODUCTION_SIZING_SCHEME,
    PRODUCTION_TRAIN_COUNT,
    SCHEMES,
    build_p3_poststop_cooldown_config,
)
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from selective_overlay_variants import (  # noqa: E402
    ACCOUNT,
    HOLDOUT_START,
    IC_MIN_CREDIT,
    MULTIPLIER,
    SELECTION_END,
    Structure,
    Variant,
    build_phase_a_ic_only,
    build_phase_b_variants,
    build_phase_c_variants,
    build_phase_d_variants,
    gate_passes,
    ic_fee_dollars,
    ic_min_credit_points,
    straddle_fee_dollars,
)
from vix_daily import DEFAULT_VIX_CSV, load_vix_daily  # noqa: E402
from vix_sizing_policies import build_production_vix_policy  # noqa: E402
from why_not_look_at_variants import load_fomc_dates  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
OUT = ROOT / "data" / "selective_straddle_overlay"
PROD_CACHE = OUT / "prod_cache"
TRAIN = PRODUCTION_TRAIN_COUNT
CHECKPOINT_VERSION = 2


def _prod_cache_path(test_date: str) -> Path:
    return PROD_CACHE / f"{test_date}.json"


def load_prod_day(test_date: str) -> Optional[dict]:
    path = _prod_cache_path(test_date)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_prod_day(test_date: str, payload: dict) -> None:
    PROD_CACHE.mkdir(parents=True, exist_ok=True)
    path = _prod_cache_path(test_date)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def out_root(phase: str) -> Path:
    return OUT / phase.lower()


def work_dir(phase: str, shard: int, shards: int) -> Path:
    root = out_root(phase)
    if shards <= 1:
        return root
    return root / f"shard_{shard}"


def shard_bounds(oos_total: int, shard: int, shards: int) -> Tuple[int, int]:
    chunk = (oos_total + shards - 1) // shards
    start = shard * chunk
    end = min(oos_total, start + chunk)
    return start, end


def save_checkpoint(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)


def load_checkpoint(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _intrinsic(option_type: str, strike: float, close_spot: float) -> float:
    if option_type == "CALL":
        return max(close_spot - strike, 0.0)
    return max(strike - close_spot, 0.0)


def _signal_at(signals, ts: datetime):
    by = {s.timestamp: s for s in signals}
    if ts in by:
        return by[ts]
    earlier = [t for t in sorted(by) if t <= ts]
    return by[earlier[-1]] if earlier else None


def _first_entry_ts(signals, by_ts) -> Optional[datetime]:
    for s in signals:
        if s.timestamp.time() >= time(10, 0) and s.timestamp in by_ts:
            return s.timestamp
    # fallback: first quote >= 10:00
    for ts in sorted(by_ts):
        if ts.time() >= time(10, 0):
            return ts
    return None


def _pick_short_leg(
    snapshot: Sequence[OptionQuote],
    option_type: str,
    target_delta: float,
    delta_lo: float,
    delta_hi: float,
) -> Optional[OptionQuote]:
    cands = []
    for q in snapshot:
        if normalize_option_type(q.option_type) != option_type or q.delta is None:
            continue
        ad = abs(q.delta)
        if not (delta_lo <= ad <= delta_hi):
            continue
        if q.bid is None or q.bid <= 0:
            continue
        cands.append((abs(ad - target_delta), -q.bid, q))
    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1]))
    return cands[0][2]


def _pick_wing(
    snapshot: Sequence[OptionQuote],
    short: OptionQuote,
    *,
    wing_width: float,
    long_direction: int,
) -> Optional[OptionQuote]:
    same = [
        q
        for q in snapshot
        if q.expiry == short.expiry
        and normalize_option_type(q.option_type) == normalize_option_type(short.option_type)
    ]
    wings = []
    for q in same:
        dist = (q.strike - short.strike) * long_direction
        if dist < wing_width * 0.5 or dist > wing_width * 1.5:
            continue
        if q.ask is None or q.ask <= 0:
            continue
        wings.append((abs(dist - wing_width), q))
    if not wings:
        return None
    wings.sort(key=lambda x: x[0])
    return wings[0][1]


def choose_iron_condor(
    snapshot: Sequence[OptionQuote],
    structure: Structure,
) -> Optional[dict]:
    spot = snapshot_spot(snapshot)
    if spot <= 0:
        return None
    band = 0.04
    put_short = _pick_short_leg(snapshot, "PUT", structure.target_delta, structure.target_delta - band, structure.target_delta + band)
    call_short = _pick_short_leg(snapshot, "CALL", structure.target_delta, structure.target_delta - band, structure.target_delta + band)
    if put_short is None or call_short is None:
        return None
    put_long = _pick_wing(snapshot, put_short, wing_width=structure.wing_width, long_direction=-1)
    call_long = _pick_wing(snapshot, call_short, wing_width=structure.wing_width, long_direction=1)
    if put_long is None or call_long is None:
        return None
    legs_px = (put_short.bid, put_long.ask, call_short.bid, call_long.ask)
    if any(p is None for p in legs_px):
        return None
    try:
        credit = (float(put_short.bid) - float(put_long.ask)) + (
            float(call_short.bid) - float(call_long.ask)
        )
    except (TypeError, ValueError):
        return None
    if credit != credit:  # NaN
        return None
    put_width = abs(put_short.strike - put_long.strike)
    call_width = abs(call_short.strike - call_long.strike)
    if credit <= 0 or put_width <= 0 or call_width <= 0:
        return None
    max_loss = max(put_width, call_width) - credit  # approx if widths differ; use wider
    # more accurate: max of each side's (width - side_credit) but combined IC max is sum of side max if both breach (rare);
    # standard IC max loss = max(put_width, call_width) - net credit when equal width.
    max_loss = (put_width + call_width) / 2.0 - credit
    if put_width == call_width:
        max_loss = put_width - credit
    return {
        "put_short": put_short,
        "put_long": put_long,
        "call_short": call_short,
        "call_long": call_long,
        "credit": credit,
        "put_width": put_width,
        "call_width": call_width,
        "max_loss": max_loss,
        "spot": spot,
    }


def _mark_ic_debit(snap: Sequence[OptionQuote], legs: dict) -> Optional[float]:
    def find(opt_type: str, strike: float, side: str) -> Optional[OptionQuote]:
        for q in snap:
            if normalize_option_type(q.option_type) == opt_type and abs(q.strike - strike) < 1e-6:
                return q
        return None

    ps = find("PUT", legs["put_short"].strike, "short")
    pl = find("PUT", legs["put_long"].strike, "long")
    cs = find("CALL", legs["call_short"].strike, "short")
    cl = find("CALL", legs["call_long"].strike, "long")
    if not all([ps, pl, cs, cl]):
        return None
    # buy back shorts at ask, sell longs at bid
    return (ps.ask - pl.bid) + (cs.ask - cl.bid)


def simulate_overlay(
    quotes: Sequence[OptionQuote],
    signals,
    variant: Variant,
    *,
    vix: Optional[float],
    is_fomc: bool,
    vertical_halted: bool,
    vertical_stopped: bool,
    vertical_trades: int,
) -> dict:
    """Returns overlay day result dict."""
    empty = {
        "overlay_pnl": 0.0,
        "overlay_trades": 0,
        "overlay_stopped": 0,
        "overlay_skipped": "off" if variant.structure.kind == "none" else "no_entry",
        "credit": None,
        "max_loss": None,
        "hit_max_loss": 0,
        "fee_blocked": 0,
        "low_vol_blocked": 0,
    }
    if variant.structure.kind == "none":
        empty["overlay_skipped"] = "none"
        return empty

    if variant.skip_if_vertical_halted and vertical_halted:
        empty["overlay_skipped"] = "vertical_halted"
        return empty
    if variant.skip_if_vertical_stopped and vertical_stopped:
        empty["overlay_skipped"] = "vertical_stopped"
        return empty
    if variant.skip_if_vertical_entered and vertical_trades > 0:
        empty["overlay_skipped"] = "vertical_entered"
        return empty

    by_ts = group_quotes(quotes)
    if not by_ts:
        empty["overlay_skipped"] = "no_quotes"
        return empty
    entry_ts = _first_entry_ts(signals, by_ts)
    if entry_ts is None:
        empty["overlay_skipped"] = "no_entry_ts"
        return empty
    sig = _signal_at(signals, entry_ts)
    if sig is None:
        empty["overlay_skipped"] = "no_signal"
        return empty

    if not gate_passes(
        variant.gate,
        residual=sig.straddle_residual_z,
        trend=sig.trend_score,
        rv=sig.realized_vs_implied_z,
        term=sig.term_ratio_z,
        vix=vix,
        is_fomc=is_fomc,
    ):
        empty["overlay_skipped"] = "gate"
        return empty

    structure = variant.structure
    contracts = variant.contracts
    timestamps = sorted(by_ts)

    if structure.kind == "straddle":
        selected = choose_atm_straddle(by_ts[entry_ts])
        if selected is None:
            empty["overlay_skipped"] = "no_straddle"
            return empty
        call_q, put_q = selected
        credit = call_q.bid + put_q.bid
        if credit <= 0:
            empty["overlay_skipped"] = "bad_credit"
            return empty
        fees = straddle_fee_dollars(contracts)
        stop_level = credit * variant.stop_multiple if variant.stop_multiple else None
        tp_level = credit * (1.0 - variant.take_profit_frac) if variant.take_profit_frac else None
        time_exit = None
        if variant.time_exit:
            hh, mm = map(int, variant.time_exit.split(":"))
            time_exit = time(hh, mm)

        exit_debit = None
        stopped = False
        stop_breach = 0
        confirm_n = max(1, int(variant.stop_confirmation_count or 1))
        for ts in timestamps:
            if ts < entry_ts:
                continue
            if time_exit and ts.time() >= time_exit and exit_debit is None:
                snap = by_ts[ts]
                calls = [
                    q
                    for q in snap
                    if normalize_option_type(q.option_type) == "CALL" and q.strike == call_q.strike
                ]
                puts = [
                    q
                    for q in snap
                    if normalize_option_type(q.option_type) == "PUT" and q.strike == put_q.strike
                ]
                if calls and puts:
                    exit_debit = calls[0].ask + puts[0].ask
                    break
            snap = by_ts[ts]
            calls = [
                q
                for q in snap
                if normalize_option_type(q.option_type) == "CALL" and q.strike == call_q.strike
            ]
            puts = [
                q
                for q in snap
                if normalize_option_type(q.option_type) == "PUT" and q.strike == put_q.strike
            ]
            if not calls or not puts:
                continue
            debit = calls[0].ask + puts[0].ask
            if stop_level is not None and debit >= stop_level:
                stop_breach += 1
                if stop_breach >= confirm_n:
                    exit_debit = debit
                    stopped = True
                    break
            else:
                stop_breach = 0
            if tp_level is not None and debit <= tp_level:
                exit_debit = debit
                break
        if exit_debit is None:
            close_spot = overlay_spot(by_ts[timestamps[-1]])
            exit_debit = _intrinsic("CALL", call_q.strike, close_spot) + _intrinsic(
                "PUT", put_q.strike, close_spot
            )
        per = credit - exit_debit
        net = per * contracts * MULTIPLIER - fees
        return {
            "overlay_pnl": round(net, 2),
            "overlay_trades": 1,
            "overlay_stopped": 1 if stopped else 0,
            "overlay_skipped": "",
            "credit": round(credit, 4),
            "max_loss": None,
            "hit_max_loss": 0,
            "fee_blocked": 0,
            "low_vol_blocked": 0,
        }

    # Iron condor
    if structure.enforce_low_vol_skip:
        if vix is None or vix < structure.min_vix:
            empty["overlay_skipped"] = "low_vol"
            empty["low_vol_blocked"] = 1
            return empty

    legs = choose_iron_condor(by_ts[entry_ts], structure)
    if legs is None:
        empty["overlay_skipped"] = "no_ic"
        return empty
    credit = float(legs["credit"])
    max_loss = float(legs["max_loss"])
    min_cred = max(
        IC_MIN_CREDIT,
        ic_min_credit_points(contracts, structure.min_fee_multiple) if structure.enforce_low_vol_skip else 0.0,
    )
    if structure.enforce_low_vol_skip and credit < min_cred:
        empty["overlay_skipped"] = "fee_credit"
        empty["fee_blocked"] = 1
        empty["credit"] = round(credit, 4)
        return empty

    fees = ic_fee_dollars(contracts)
    stop_level = credit * variant.stop_multiple if variant.stop_multiple else None
    tp_level = credit * (1.0 - variant.take_profit_frac) if variant.take_profit_frac else None
    time_exit = None
    if variant.time_exit:
        hh, mm = map(int, variant.time_exit.split(":"))
        time_exit = time(hh, mm)

    exit_debit = None
    stopped = False
    hit_max = False
    stop_breach = 0
    confirm_n = max(1, int(variant.stop_confirmation_count or 1))
    for ts in timestamps:
        if ts < entry_ts:
            continue
        debit = _mark_ic_debit(by_ts[ts], legs)
        if debit is None:
            continue
        if time_exit and ts.time() >= time_exit:
            exit_debit = debit
            break
        if stop_level is not None and debit >= stop_level:
            stop_breach += 1
            if stop_breach >= confirm_n:
                exit_debit = debit
                stopped = True
                break
        else:
            stop_breach = 0
        if tp_level is not None and debit <= tp_level:
            exit_debit = debit
            break
        if variant.directional_stop:
            spot = overlay_spot(by_ts[ts])
            put_breach = spot <= legs["put_short"].strike - 0.5 * legs["put_width"]
            call_breach = spot >= legs["call_short"].strike + 0.5 * legs["call_width"]
            if put_breach or call_breach:
                exit_debit = debit
                stopped = True
                break

    if exit_debit is None:
        close_spot = overlay_spot(by_ts[timestamps[-1]])
        # settlement: shorts intrinsic - longs intrinsic (debit to close at intrinsic)
        put_short_i = _intrinsic("PUT", legs["put_short"].strike, close_spot)
        put_long_i = _intrinsic("PUT", legs["put_long"].strike, close_spot)
        call_short_i = _intrinsic("CALL", legs["call_short"].strike, close_spot)
        call_long_i = _intrinsic("CALL", legs["call_long"].strike, close_spot)
        exit_debit = (put_short_i - put_long_i) + (call_short_i - call_long_i)
        # bound: debit cannot exceed max_loss + tiny float
        if exit_debit > max_loss + 1e-6:
            exit_debit = max_loss
            hit_max = True

    # path stop can also realize near max loss
    if exit_debit is not None and exit_debit >= max_loss * 0.98:
        hit_max = True

    per = credit - exit_debit
    net = per * contracts * MULTIPLIER - fees
    return {
        "overlay_pnl": round(net, 2),
        "overlay_trades": 1,
        "overlay_stopped": 1 if stopped else 0,
        "overlay_skipped": "",
        "credit": round(credit, 4),
        "max_loss": round(max_loss, 4),
        "hit_max_loss": 1 if hit_max else 0,
        "fee_blocked": 0,
        "low_vol_blocked": 0,
    }


def empty_trade_agg() -> dict:
    return {
        "trades": 0,
        "wins": 0,
        "stopped": 0,
        "total_pnl": 0.0,
        "overlay_trades": 0,
        "overlay_pnl": 0.0,
        "fee_blocked": 0,
        "low_vol_blocked": 0,
        "hit_max_loss": 0,
    }


def update_overlay_agg(agg: dict, ov: dict) -> None:
    agg["overlay_trades"] += int(ov.get("overlay_trades") or 0)
    agg["overlay_pnl"] += float(ov.get("overlay_pnl") or 0.0)
    agg["fee_blocked"] += int(ov.get("fee_blocked") or 0)
    agg["low_vol_blocked"] += int(ov.get("low_vol_blocked") or 0)
    agg["hit_max_loss"] += int(ov.get("hit_max_loss") or 0)
    if ov.get("overlay_trades"):
        agg["trades"] += 1
        pnl = float(ov["overlay_pnl"])
        agg["total_pnl"] += pnl
        if pnl > 0:
            agg["wins"] += 1
        if ov.get("overlay_stopped"):
            agg["stopped"] += 1


def filter_daily(daily: List[dict], *, end: Optional[str] = None, start: Optional[str] = None) -> List[dict]:
    out = []
    for row in daily:
        d = str(row.get("date") or "")
        if end is not None and d > end:
            continue
        if start is not None and d < start:
            continue
        out.append(row)
    return out


def build_summaries(
    variants: List[Variant],
    daily_by: Dict[str, List[dict]],
    trade_agg: Dict[str, dict],
    *,
    period: str = "full",
    end: Optional[str] = None,
    start: Optional[str] = None,
) -> List[dict]:
    ref_name = variants[0].name
    ref_daily = filter_daily(daily_by[ref_name], end=end, start=start)
    ref_stats = portfolio_stats(ref_daily, ACCOUNT, metrics_mode="eligible_only")
    ref_calmar = float(ref_stats.get("cagr_pct") or 0) / max(float(ref_stats.get("max_drawdown_pct") or 1), 0.01)
    rows = []
    for v in variants:
        daily = filter_daily(daily_by[v.name], end=end, start=start)
        port = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
        agg = trade_agg.get(v.name, empty_trade_agg())
        max_dd = float(port.get("max_drawdown_pct") or 0)
        cagr = float(port.get("cagr_pct") or 0)
        calmar = round(cagr / max_dd, 4) if max_dd > 0 else 0.0
        ov_trades = sum(int(r.get("overlay_trades") or 0) for r in daily)
        ov_pnl = sum(float(r.get("overlay_pnl") or 0) for r in daily)
        rows.append(
            {
                "period": period,
                "selection_end": SELECTION_END,
                "holdout_start": HOLDOUT_START,
                "phase": v.phase,
                "variant": v.name,
                "structure": v.structure.name,
                "gate": v.gate.name,
                "n_days": len(daily),
                **port,
                "calmar": calmar,
                "overlay_trades": ov_trades,
                "overlay_pnl": round(ov_pnl, 2),
                "overlay_win_rate": round(agg["wins"] / agg["trades"], 4) if agg["trades"] else 0.0,
                "overlay_stop_rate": round(agg["stopped"] / agg["trades"], 4) if agg["trades"] else 0.0,
                "fee_blocked_days": agg.get("fee_blocked", 0),
                "low_vol_blocked_days": agg.get("low_vol_blocked", 0),
                "hit_max_loss_days": agg.get("hit_max_loss", 0),
                "cagr_delta_vs_ref": round(cagr - float(ref_stats.get("cagr_pct") or 0), 2),
                "worst_day_delta_vs_ref": round(
                    float(port.get("worst_day_pct") or 0) - float(ref_stats.get("worst_day_pct") or 0), 2
                ),
                "max_dd_delta_vs_ref": round(max_dd - float(ref_stats.get("max_drawdown_pct") or 0), 2),
                "calmar_delta_vs_ref": round(calmar - ref_calmar, 4),
            }
        )
    return rows


def load_variants(phase: str, winners_json: Optional[Path]) -> List[Variant]:
    phase_u = phase.upper()
    if phase_u == "A1C":
        return build_phase_a_ic_only()
    if phase_u == "B":
        return build_phase_b_variants()
    if phase_u in ("C", "D", "CD"):
        if not winners_json or not winners_json.is_file():
            raise SystemExit("--winners-json required for phase C/D/CD")
        payload = json.loads(winners_json.read_text(encoding="utf-8"))
        variants: List[Variant] = []
        # always include production-only ref
        from selective_overlay_variants import NONE, Gate

        variants.append(Variant("B0", "B0_prod_only", Gate("off"), NONE))
        for w in payload.get("winners", []):
            # rebuild from B registry by name prefix match on gate+structure
            all_b = {v.name: v for v in build_phase_b_variants()}
            base_name = w["variant"]
            if base_name not in all_b:
                raise SystemExit(f"Unknown winner variant {base_name}")
            base = all_b[base_name]
            prefix = base_name
            if phase_u in ("C", "CD"):
                variants.extend(build_phase_c_variants(base.gate, base.structure, prefix))
            if phase_u in ("D", "CD"):
                variants.extend(build_phase_d_variants(base.gate, base.structure, prefix))
        # de-dupe names
        seen = set()
        uniq = []
        for v in variants:
            if v.name in seen:
                continue
            seen.add(v.name)
            uniq.append(v)
        return uniq
    raise SystemExit(f"Unknown phase {phase}")


def run_suite(
    *,
    phase: str,
    shard: int = 0,
    shards: int = 1,
    max_oos: int = 0,
    resume: bool = False,
    checkpoint_every: int = 10,
    winners_json: Optional[Path] = None,
    skip_production: bool = False,
) -> bool:
    variants = load_variants(phase, winners_json)
    names = [v.name for v in variants]
    need_production = any(v.structure.kind != "none" or v.name == "B0_prod_only" for v in variants)
    # A1c diagnostics: no production book needed
    a1c = phase.upper() == "A1C"
    if a1c:
        need_production = False

    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(PROCESSED, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(
        processed_dates, floor=resolved_start, end=processed_dates[-1], eras=eras
    )
    oos_total = len(eligible) - TRAIN
    if max_oos > 0:
        oos_total = min(oos_total, max_oos)
    oos_start, oos_end = shard_bounds(oos_total, shard, shards)
    shard_days = oos_end - oos_start

    out_dir = work_dir(phase, shard, shards)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = out_dir / "checkpoint.json"

    fomc_dates = load_fomc_dates()
    vix_by = load_vix_daily(DEFAULT_VIX_CSV)
    from profiles import VIX_ELEVATED_SCALE  # noqa: E402

    base_cfg = build_p3_poststop_cooldown_config(account_equity=ACCOUNT)
    tod = SCHEMES[PRODUCTION_SIZING_SCHEME]
    policy = build_production_vix_policy(
        tod, elevated_scale=VIX_ELEVATED_SCALE, max_contracts=PRODUCTION_MAX_CONTRACTS_PER_TRANCHE
    )

    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trade_agg: Dict[str, dict] = {n: empty_trade_agg() for n in names}
    # A1c row dump
    a1c_rows: List[dict] = []
    start_oos_offset = oos_start

    if resume:
        ckpt = load_checkpoint(ckpt_path)
        if ckpt and ckpt.get("version") == CHECKPOINT_VERSION and ckpt.get("phase") == phase.upper():
            if ckpt.get("complete") and int(ckpt.get("oos_total", 0)) == shard_days:
                print(f"Shard {shard}/{shards} phase {phase} already complete.", flush=True)
                return True
            if int(ckpt.get("oos_done", 0)) > 0:
                daily_by = ckpt["daily_by"]
                trade_agg = ckpt["trade_agg"]
                a1c_rows = ckpt.get("a1c_rows", [])
                start_oos_offset = oos_start + int(ckpt.get("oos_done", 0))
                print(
                    f"Resume shard {shard}/{shards} phase {phase} at "
                    f"{start_oos_offset - oos_start}/{shard_days}",
                    flush=True,
                )

    print(
        f"Selective overlay phase={phase} shard={shard}/{shards}: "
        f"{len(variants)} variants × {shard_days} OOS days "
        f"(sel<={SELECTION_END}, hold>={HOLDOUT_START})",
        flush=True,
    )

    for oos_i in range(start_oos_offset, oos_end):
        index = TRAIN + oos_i
        test_date = eligible[index]
        train_dates = eligible[index - TRAIN : index]
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        apply_rolling_baseline(PROCESSED, "SPXW", train_dates, test_date, "signals_unconditional.csv")
        signals = read_signals_csv(day_dir / "signals_unconditional.csv")
        era = era_for_date(datetime.strptime(test_date, "%Y-%m-%d").date(), eras)
        is_fomc = test_date in fomc_dates
        vix_day = vix_by.get(test_date)
        vix_open = float(vix_day.open) if vix_day else None

        prod_pnl = 0.0
        prod_trades = 0
        prod_stopped = 0
        prod_halted = False
        if need_production and not skip_production:
            cached = load_prod_day(test_date)
            if cached is not None:
                prod_pnl = float(cached["prod_pnl"])
                prod_trades = int(cached["prod_trades"])
                prod_stopped = int(cached["prod_stopped"])
                prod_halted = bool(cached["prod_halted"])
            else:
                result = simulate_day(quotes, signals, config=base_cfg, policy=policy)
                prod_pnl = float(result.net_pnl)
                prod_trades = len(result.trades)
                prod_stopped = sum(1 for t in result.trades if t.stopped)
                prod_halted = bool(result.halted)
                save_prod_day(
                    test_date,
                    {
                        "prod_pnl": prod_pnl,
                        "prod_trades": prod_trades,
                        "prod_stopped": prod_stopped,
                        "prod_halted": prod_halted,
                    },
                )

        for v in variants:
            ov = simulate_overlay(
                quotes,
                signals,
                v,
                vix=vix_open,
                is_fomc=is_fomc,
                vertical_halted=prod_halted,
                vertical_stopped=prod_stopped > 0,
                vertical_trades=prod_trades,
            )
            if a1c:
                sig = None
                by_ts = group_quotes(quotes)
                entry_ts = _first_entry_ts(signals, by_ts) if by_ts else None
                if entry_ts:
                    sig = _signal_at(signals, entry_ts)
                if sig is not None:
                    a1c_rows.append(
                        {
                            "date": test_date,
                            "variant": v.name,
                            "straddle_residual_z": round(sig.straddle_residual_z, 6),
                            "term_ratio_z": round(sig.term_ratio_z, 6),
                            "trend_score": round(sig.trend_score, 6),
                            "realized_vs_implied_z": round(sig.realized_vs_implied_z, 6),
                            "vix_open": vix_open,
                            "pnl": ov["overlay_pnl"],
                            "credit": ov.get("credit"),
                            "max_loss": ov.get("max_loss"),
                            "stopped": ov.get("overlay_stopped"),
                            "hit_max_loss": ov.get("hit_max_loss"),
                            "low_vol_blocked": ov.get("low_vol_blocked"),
                            "fee_blocked": ov.get("fee_blocked"),
                            "skipped": ov.get("overlay_skipped"),
                        }
                    )
                net = float(ov["overlay_pnl"])
            else:
                net = round(prod_pnl + float(ov["overlay_pnl"]), 2)

            update_overlay_agg(trade_agg[v.name], ov)
            daily_by[v.name].append(
                {
                    "date": test_date,
                    "eligible": True,
                    "era": era,
                    "trades": prod_trades + int(ov["overlay_trades"]),
                    "stopped_trades": prod_stopped + int(ov["overlay_stopped"]),
                    "net_pnl": net if not a1c else float(ov["overlay_pnl"]),
                    "prod_pnl": round(prod_pnl, 2),
                    "overlay_pnl": ov["overlay_pnl"],
                    "overlay_trades": ov["overlay_trades"],
                    "halted": prod_halted,
                    "is_fomc": is_fomc,
                    "vix_open": vix_open,
                    "overlay_skipped": ov.get("overlay_skipped"),
                }
            )

        done_in_shard = oos_i - oos_start + 1
        if done_in_shard % 25 == 0 or oos_i == oos_end - 1:
            print(f"  shard {shard}: {done_in_shard}/{shard_days} ({test_date})", flush=True)
        if checkpoint_every > 0 and (done_in_shard % checkpoint_every == 0 or oos_i == oos_end - 1):
            save_checkpoint(
                ckpt_path,
                {
                    "version": CHECKPOINT_VERSION,
                    "phase": phase.upper(),
                    "shard": shard,
                    "shards": shards,
                    "oos_done": done_in_shard,
                    "oos_total": shard_days,
                    "last_date": test_date,
                    "complete": oos_i == oos_end - 1,
                    "variant_names": names,
                    "daily_by": daily_by,
                    "trade_agg": trade_agg,
                    "a1c_rows": a1c_rows,
                    "selection_end": SELECTION_END,
                    "holdout_start": HOLDOUT_START,
                },
            )

    summaries = build_summaries(variants, daily_by, trade_agg)
    (out_dir / "shard_summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    if a1c and shards <= 1:
        _write_a1c_report(a1c_rows, out_dir)
    return True


def _write_a1c_report(rows: List[dict], out_dir: Path) -> None:
    import csv
    from collections import defaultdict

    out_dir.mkdir(parents=True, exist_ok=True)
    # prefer fee-aware IC rows
    primary = [r for r in rows if r["variant"] == "A1c_ic_d12" and r.get("skipped") == ""]
    if not primary:
        primary = [r for r in rows if r.get("pnl") is not None and r.get("skipped") == ""]
    path = out_dir / "ic_rows.csv"
    if rows:
        with path.open("w", newline="", encoding="utf-8") as handle:
            w = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    features = ["straddle_residual_z", "term_ratio_z", "trend_score", "realized_vs_implied_z"]
    sel = [r for r in primary if r["date"] <= SELECTION_END]
    hold = [r for r in primary if r["date"] >= HOLDOUT_START]

    def quintile_edges(values: List[float]) -> List[float]:
        xs = sorted(values)
        n = len(xs)
        if n == 0:
            return [0, 0, 0, 0]

        def q(p: float) -> float:
            return xs[min(n - 1, max(0, int(p * (n - 1))))]

        return [q(0.2), q(0.4), q(0.6), q(0.8)]

    def assign(v: float, edges: List[float]) -> int:
        for i, e in enumerate(edges):
            if v <= e:
                return i + 1
        return 5

    slices = []
    for period, grp in (("selection", sel), ("holdout", hold)):
        for feat in features:
            vals = [float(r[feat]) for r in grp]
            edges = quintile_edges(vals)
            buckets: Dict[int, List] = defaultdict(list)
            for r in grp:
                buckets[assign(float(r[feat]), edges)].append(r)
            for q in range(1, 6):
                g = buckets.get(q, [])
                if not g:
                    continue
                n = len(g)
                slices.append(
                    {
                        "period": period,
                        "feature": feat,
                        "quintile": q,
                        "n": n,
                        "mean_feature": round(sum(float(r[feat]) for r in g) / n, 4),
                        "mean_pnl": round(sum(float(r["pnl"]) for r in g) / n, 2),
                        "win_rate": round(sum(1 for r in g if float(r["pnl"]) > 0) / n, 4),
                        "stop_rate": round(sum(int(r.get("stopped") or 0) for r in g) / n, 4),
                        "hit_max_loss_rate": round(sum(int(r.get("hit_max_loss") or 0) for r in g) / n, 4),
                    }
                )

    (out_dir / "quintile_summary.json").write_text(json.dumps(slices, indent=2), encoding="utf-8")
    # skip stats
    all_primary_attempts = [r for r in rows if r["variant"] == "A1c_ic_d12"]
    low_vol = sum(int(r.get("low_vol_blocked") or 0) for r in all_primary_attempts)
    fee_blk = sum(int(r.get("fee_blocked") or 0) for r in all_primary_attempts)
    entered = sum(1 for r in all_primary_attempts if r.get("skipped") == "")
    lines = [
        "# Phase A1c — Iron condor counterfactual (fee / low-vol aware)",
        "",
        f"Selection <= `{SELECTION_END}` | Holdout >= `{HOLDOUT_START}`",
        f"Entered (IC_d12 with VIX/fee filters): {entered}",
        f"Blocked low-vol (VIX < min): {low_vol}",
        f"Blocked fee/credit: {fee_blk}",
        f"Selection entered rows: {len(sel)} | Holdout: {len(hold)}",
        "",
        "Positive mean_pnl = short IC made money after fees (4 contracts default).",
        "",
    ]
    for period in ("selection", "holdout"):
        lines.append(f"## {period}")
        lines.append("")
        lines.append("| Feature | Q | N | Mean feat | Mean PnL | Win% | Stop% | MaxLoss% |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for row in slices:
            if row["period"] != period:
                continue
            lines.append(
                f"| {row['feature']} | {row['quintile']} | {row['n']} | {row['mean_feature']} | "
                f"{row['mean_pnl']} | {row['win_rate']} | {row['stop_rate']} | {row['hit_max_loss_rate']} |"
            )
        lines.append("")
    (out_dir / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Selective straddle/IC overlay suite")
    parser.add_argument("--phase", required=True, help="A1c | B | C | D | CD")
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--max-oos-days", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--winners-json", type=Path, default=None)
    parser.add_argument("--skip-production", action="store_true")
    args = parser.parse_args()
    t0 = time_mod.time()
    run_suite(
        phase=args.phase,
        shard=args.shard,
        shards=args.shards,
        max_oos=args.max_oos_days,
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
        winners_json=args.winners_json,
        skip_production=args.skip_production,
    )
    print(f"Done phase={args.phase} shard={args.shard} in {(time_mod.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
