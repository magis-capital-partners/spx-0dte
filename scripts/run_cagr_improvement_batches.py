"""Run CAGR-improvement variant batches (single-pass per day, eligible-calendar OOS).

Supports checkpoint/resume and date sharding for crash recovery and parallel runs.

Batch 1: P3 entry gates, time-of-day schemes, post-stop rules
Batch 2: Harvest/score gates, spread-value stops, premium-gate ablation
Batch 3: Condor/trend-debit sleeves, regime downsize

Outputs per shard: data/cagr_improvement_batches/batch{N}/shard_{k}/checkpoint.json
Merged: data/cagr_improvement_batches/batch{N}/summary.json (via merge_cagr_batch_shards.py)
"""
from __future__ import annotations

import argparse
import json
import sys
import time as time_mod
from collections import deque
from dataclasses import replace
from datetime import datetime, time, timezone
from pathlib import Path
from statistics import mean
from typing import Callable, Deque, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))

from dataclasses import replace as dc_replace  # noqa: E402

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    era_for_date,
    load_era_rules,
    resolve_start_date,
)
from historical_baselines import compute_baselines, processed_signal_path, read_csv, transform_rows  # noqa: E402
from mbh_simulator import (  # noqa: E402
    SignalSnapshot,
    StrategyConfig,
    parse_timestamp,
    read_quotes_csv,
    simulate_day,
    trades_to_rows,
)
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import PRODUCTION_SIZING_SCHEME, SCHEMES, WINNERS, build_3d_flatten_config  # noqa: E402
from regime_validation import discover_dates  # noqa: E402
from stop_calibration_runner import base_config  # noqa: E402
from time_of_day_sizing_runner import TimeOfDaySizePolicy  # noqa: E402
from unconditional_baseline import FixedSizePolicy  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
OUT = ROOT / "data" / "cagr_improvement_batches"
ACCOUNT = 13_000_000.0
TRAIN = 40
DEFAULT_SIGNALS = "signals_unconditional.csv"
CHECKPOINT_VERSION = 1


def _float_or_none(value: object) -> Optional[float]:
    if value in {"", None}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def rows_to_signals(rows: List[dict]) -> List[SignalSnapshot]:
    signals: List[SignalSnapshot] = []
    for row in rows:
        signals.append(
            SignalSnapshot(
                timestamp=parse_timestamp(row["timestamp"]),
                straddle_residual_z=float(row.get("straddle_residual_z") or 0.0),
                skew_z=float(row.get("skew_z") or 0.0),
                term_ratio_z=float(row.get("term_ratio_z") or 0.0),
                trend_score=float(row.get("trend_score") or 0.0),
                realized_vs_implied_z=float(row.get("realized_vs_implied_z") or 0.0),
                vix=_float_or_none(row.get("vix")),
                minutes_to_close_norm=float(row.get("minutes_to_close_norm") or 0.0),
                overnight_gap_z=float(row.get("overnight_gap_z") or 0.0),
                prior_day_return_z=float(row.get("prior_day_return_z") or 0.0),
                abs_skew_z=float(row.get("abs_skew_z") or 0.0),
                abs_term_ratio_z=float(row.get("abs_term_ratio_z") or 0.0),
            )
        )
    return signals


def load_rolled_signals(
    processed_dir: Path,
    symbol: str,
    train_dates: List[str],
    test_date: str,
) -> List[SignalSnapshot]:
    baselines = compute_baselines(processed_dir, symbol, train_dates)
    rows = read_csv(processed_signal_path(processed_dir, symbol, test_date))
    return rows_to_signals(transform_rows(rows, baselines))


TOD = SCHEMES[PRODUCTION_SIZING_SCHEME]


def _3d(**kw) -> StrategyConfig:
    cfg = build_3d_flatten_config(account_equity=ACCOUNT, baseline_contracts=31)
    return dc_replace(cfg, **kw)


def _3d_wide(**kw) -> StrategyConfig:
    return base_config(account_equity=ACCOUNT, baseline_contracts=31, **WINNERS, **kw)


Variant = Tuple[str, str, StrategyConfig, Optional[Callable]]


class RegimeDownsizePolicy(TimeOfDaySizePolicy):
    def __init__(self, schedule, trailing: Deque[float]) -> None:
        super().__init__(schedule)
        self.trailing = trailing

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        base_c = super().contracts(signal, config)
        if len(self.trailing) >= 5 and mean(self.trailing) > 0.28:
            return max(0, round(base_c * 0.5))
        return base_c


def batch1_variants() -> List[Variant]:
    v: List[Variant] = []
    ref = _3d()
    v.append(("p3_gates", "ref_3d_tod_downsize", ref, lambda: TimeOfDaySizePolicy(TOD)))

    for name, kw in [
        ("p3_trend_1.0", {"candidate_max_adverse_trend": 1.0}),
        ("p3_skew_0.75", {"candidate_max_adverse_skew": 0.75}),
        ("p3_entry_10_00", {"entry_start": time(10, 0)}),
        ("p3_trend_0.65", {"candidate_max_adverse_trend": 0.65}),
        (
            "p3_combo",
            {
                "candidate_max_adverse_trend": 1.0,
                "candidate_max_adverse_skew": 0.75,
                "entry_start": time(10, 0),
            },
        ),
    ]:
        v.append(("p3_gates", name, dc_replace(ref, **kw), lambda: TimeOfDaySizePolicy(TOD)))

    for scheme in [
        "control_flat",
        "step_3block_mild",
        "linear_decay_neutral",
        "linear_decay_downsize",
        "morning_heavy_afternoon_off",
    ]:
        v.append(("tod_schemes", f"tod_{scheme}", ref, lambda s=scheme: TimeOfDaySizePolicy(SCHEMES[s])))

    post_base = _3d_wide()
    post_variants = [
        ("poststop_baseline", {"same_side_stop_cooldown_minutes": 0, "max_stops_per_side": 999}),
        ("poststop_cooldown_120", {"same_side_stop_cooldown_minutes": 120, "max_stops_per_side": 999}),
        ("poststop_max2_side", {"max_stops_per_side": 2}),
        ("poststop_no_same_strike", {"block_same_strike_after_stop": True}),
        (
            "poststop_combo",
            {
                "same_side_stop_cooldown_minutes": 120,
                "max_stops_per_side": 2,
                "block_same_strike_after_stop": True,
            },
        ),
    ]
    for name, kw in post_variants:
        v.append(("post_stop", name, dc_replace(post_base, **kw), lambda: FixedSizePolicy()))

    return v


def batch2_variants() -> List[Variant]:
    v: List[Variant] = []
    ref = _3d_wide()

    v.append(("harvest_score", "harvest_ref_3d", ref, lambda: FixedSizePolicy()))
    harvest_variants = [
        (
            "harvest_mode",
            {
                "use_harvest_mode": True,
                "harvest_min_score": 1.75,
                "harvest_base_size_fraction": 0.08,
            },
        ),
        (
            "harvest_no_premium",
            {
                "require_positive_premium_richness": False,
                "use_harvest_mode": True,
                "harvest_min_score": 1.75,
                "harvest_base_size_fraction": 0.08,
            },
        ),
        ("score_gate_1.5", {"candidate_min_score": 1.5, "use_harvest_mode": False}),
        ("score_gate_2.0", {"candidate_min_score": 2.0, "use_harvest_mode": False}),
        ("no_premium_gate", {"require_positive_premium_richness": False}),
    ]
    for name, kw in harvest_variants:
        v.append(("harvest_score", name, dc_replace(ref, **kw), lambda: FixedSizePolicy()))

    stop_variants = [
        ("stop_short_3x_2bar", {}),
        ("stop_spread_1.5x", {"stop_mode": "spread_value", "spread_stop_loss_multiple": 1.5}),
        ("stop_spread_2.0x", {"stop_mode": "spread_value", "spread_stop_loss_multiple": 2.0}),
        ("stop_short_3.5x", {"stop_multiple": 3.5}),
    ]
    for name, kw in stop_variants:
        v.append(("stop_mode", name, dc_replace(ref, **kw), lambda: FixedSizePolicy()))

    v.append(
        ("premium_gate", "premium_on", dc_replace(ref, require_positive_premium_richness=True), lambda: FixedSizePolicy())
    )
    v.append(
        ("premium_gate", "premium_off", dc_replace(ref, require_positive_premium_richness=False), lambda: FixedSizePolicy())
    )
    return v


def batch3_variants() -> List[Variant]:
    v: List[Variant] = []
    ref = _3d()
    v.append(("sleeves", "sleeve_ref_3d_tod", ref, lambda: TimeOfDaySizePolicy(TOD)))

    sleeve_kw = [
        ("sleeve_condor", {"use_condor_sleeve": True}),
        ("sleeve_trend_debit", {"use_trend_debit_sleeve": True}),
        ("sleeve_both", {"use_condor_sleeve": True, "use_trend_debit_sleeve": True}),
    ]
    for name, kw in sleeve_kw:
        v.append(("sleeves", name, dc_replace(ref, **kw), lambda: TimeOfDaySizePolicy(TOD)))

    v.append(("regime_downsize", "regime_ref_tod", ref, lambda: TimeOfDaySizePolicy(TOD)))
    v.append(("regime_downsize", "regime_downsize_half", ref, None))
    return v


BATCH_BUILDERS = {1: batch1_variants, 2: batch2_variants, 3: batch3_variants}

# Variant groups per batch (suite mode).
BATCH_SUITES: Dict[int, List[str]] = {
    1: ["p3_gates", "tod_schemes", "post_stop"],
    2: ["harvest_score", "stop_mode", "premium_gate"],
    3: ["sleeves", "regime_downsize"],
}

# regime_downsize shards must run in order (trailing_stop carry).
SEQUENTIAL_SUITES = {"regime_downsize"}


def filter_variants(batch_id: int, suite: Optional[str]) -> List[Variant]:
    all_v = BATCH_BUILDERS[batch_id]()
    if not suite:
        return all_v
    known = BATCH_SUITES.get(batch_id, [])
    if suite not in known:
        raise SystemExit(f"Unknown suite {suite!r} for batch {batch_id}; choose from {known}")
    return [v for v in all_v if v[0] == suite]


def work_dir(batch_id: int, suite: Optional[str], shard: int, shards: int) -> Path:
    base = OUT / f"batch{batch_id}"
    if suite:
        base = base / f"suite_{suite}"
    if shards <= 1:
        return base
    return base / f"shard_{shard}"


def shard_dir(batch_id: int, shard: int, shards: int) -> Path:
    """Legacy path without suite (avoid in new runs)."""
    return work_dir(batch_id, None, shard, shards)


def checkpoint_path(work_dir: Path) -> Path:
    return work_dir / "checkpoint.json"


def empty_trade_agg() -> dict:
    return {"trades": 0, "wins": 0, "stopped": 0, "total_pnl": 0.0}


def update_trade_agg(agg: dict, trade_rows: List[dict]) -> None:
    for t in trade_rows:
        if t.get("model") == "net_long_overlay":
            continue
        agg["trades"] += 1
        pnl = float(t.get("net_pnl") or 0.0)
        agg["total_pnl"] += pnl
        if pnl > 0:
            agg["wins"] += 1
        if t.get("stopped"):
            agg["stopped"] += 1


def agg_win_rate(agg: dict) -> float:
    n = agg.get("trades", 0)
    return round(agg["wins"] / n, 4) if n else 0.0


def agg_expectancy(agg: dict) -> float:
    n = agg.get("trades", 0)
    return round(agg["total_pnl"] / n, 2) if n else 0.0


def agg_stop_rate(agg: dict) -> float:
    n = agg.get("trades", 0)
    return round(agg["stopped"] / n, 4) if n else 0.0


def save_checkpoint(
    path: Path,
    *,
    batch_id: int,
    suite: Optional[str],
    shard: int,
    shards: int,
    variant_meta: List[dict],
    daily_by: Dict[str, List[dict]],
    trade_agg: Dict[str, dict],
    trailing_stop: Deque[float],
    oos_done: int,
    oos_total: int,
    last_date: str,
    complete: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CHECKPOINT_VERSION,
        "batch_id": batch_id,
        "suite": suite or "",
        "shard": shard,
        "shards": shards,
        "oos_done": oos_done,
        "oos_total": oos_total,
        "last_date": last_date,
        "complete": complete,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "variant_meta": variant_meta,
        "daily_by": daily_by,
        "trade_agg": trade_agg,
        "trailing_stop": list(trailing_stop),
    }
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


def resolve_eligible() -> Tuple[List[str], object]:
    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(PROCESSED, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    eligible = discover_eligible_dates(
        processed_dates, floor=resolved_start, end=processed_dates[-1], eras=eras
    )
    return eligible, eras


def shard_bounds(oos_total: int, shard: int, shards: int) -> Tuple[int, int]:
    """Return [oos_start, oos_end) indices within the OOS window."""
    chunk = (oos_total + shards - 1) // shards
    start = shard * chunk
    end = min(oos_total, start + chunk)
    return start, end


def run_batch(
    batch_id: int,
    *,
    suite: Optional[str] = None,
    shard: int = 0,
    shards: int = 1,
    max_oos: int = 0,
    resume: bool = False,
    checkpoint_every: int = 25,
    sleep_ms: int = 0,
    carry_trailing: Optional[List[float]] = None,
) -> Tuple[List[dict], dict, bool]:
    eligible, eras = resolve_eligible()
    variants = filter_variants(batch_id, suite)
    if not variants:
        raise SystemExit(f"No variants for batch {batch_id} suite {suite!r}")
    names = [x[1] for x in variants]
    variant_meta = [{"group": g, "variant": n} for g, n, _, _ in variants]

    out_dir = work_dir(batch_id, suite, shard, shards)
    out_dir.mkdir(parents=True, exist_ok=True)
    ckpt_path = checkpoint_path(out_dir)

    oos_total = len(eligible) - TRAIN
    if max_oos > 0:
        oos_total = min(oos_total, max_oos)
    oos_start, oos_end = shard_bounds(oos_total, shard, shards)
    shard_days = oos_end - oos_start

    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trade_agg: Dict[str, dict] = {n: empty_trade_agg() for n in names}
    trailing_stop: Deque[float] = deque(maxlen=5)
    if carry_trailing:
        for v in carry_trailing[-5:]:
            trailing_stop.append(float(v))

    start_oos_offset = oos_start
    if resume:
        ckpt = load_checkpoint(ckpt_path)
        if ckpt and ckpt.get("version") == CHECKPOINT_VERSION:
            ckpt_total = int(ckpt.get("oos_total", 0))
            if ckpt.get("complete") and ckpt_total == shard_days:
                print(f"Shard {shard}/{shards} already complete — skipping sim loop.", flush=True)
                sums, ref = build_summaries(batch_id, variants, ckpt["daily_by"], ckpt["trade_agg"])
                return sums, ref, True
            if ckpt.get("complete") and ckpt_total != shard_days:
                print(
                    f"Shard {shard}/{shards} has short checkpoint ({ckpt_total} days, need {shard_days}) — rerunning.",
                    flush=True,
                )
            elif int(ckpt.get("oos_done", 0)) > 0:
                daily_by = ckpt["daily_by"]
                trade_agg = ckpt["trade_agg"]
                trailing_stop = deque(ckpt.get("trailing_stop", []), maxlen=5)
                start_oos_offset = oos_start + int(ckpt.get("oos_done", 0))
                print(
                    f"Resuming shard {shard}/{shards} from OOS offset {start_oos_offset - oos_start}/{shard_days} "
                    f"({ckpt.get('last_date', '?')})",
                    flush=True,
                )

    suite_label = f" suite={suite}" if suite else ""
    print(
        f"Batch {batch_id}{suite_label} shard {shard}/{shards}: {len(variants)} variants × {shard_days} OOS days "
        f"(global {oos_start}-{oos_end} of {oos_total})",
        flush=True,
    )

    for oos_i in range(start_oos_offset, oos_end):
        index = TRAIN + oos_i
        test_date = eligible[index]
        train_dates = eligible[index - TRAIN : index]
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = load_rolled_signals(PROCESSED, "SPXW", train_dates, test_date)
        era = era_for_date(datetime.strptime(test_date, "%Y-%m-%d").date(), eras)

        regime_policy = RegimeDownsizePolicy(TOD, trailing_stop)

        for group, name, cfg, policy_factory in variants:
            if name == "regime_downsize_half":
                policy = regime_policy
            elif policy_factory is None:
                policy = FixedSizePolicy()
            else:
                policy = policy_factory()

            result = simulate_day(quotes, signals, config=cfg, policy=policy)
            rows = trades_to_rows(result.trades)
            update_trade_agg(trade_agg[name], rows)
            daily_by[name].append(
                {
                    "date": test_date,
                    "eligible": True,
                    "era": era,
                    "trades": len(result.trades),
                    "stopped_trades": sum(1 for t in result.trades if t.stopped),
                    "net_pnl": round(result.net_pnl, 2),
                    "halted": result.halted,
                }
            )

        ref_name = names[0]
        base_day = daily_by[ref_name][-1]
        tr = int(base_day["trades"])
        sr = int(base_day["stopped_trades"]) / tr if tr else 0.0
        trailing_stop.append(sr)

        done_in_shard = oos_i - oos_start + 1
        if done_in_shard % 100 == 0 or oos_i == oos_end - 1:
            print(f"  shard {shard}: {done_in_shard}/{shard_days} ({test_date})", flush=True)

        if checkpoint_every > 0 and (done_in_shard % checkpoint_every == 0 or oos_i == oos_end - 1):
            save_checkpoint(
                ckpt_path,
                batch_id=batch_id,
                suite=suite,
                shard=shard,
                shards=shards,
                variant_meta=variant_meta,
                daily_by=daily_by,
                trade_agg=trade_agg,
                trailing_stop=trailing_stop,
                oos_done=done_in_shard,
                oos_total=shard_days,
                last_date=test_date,
                complete=(oos_i == oos_end - 1),
            )

        if sleep_ms > 0:
            time_mod.sleep(sleep_ms / 1000.0)

    summaries, ref_stats = build_summaries(batch_id, variants, daily_by, trade_agg)
    return summaries, ref_stats, True


def build_summaries(
    batch_id: int,
    variants: List[Variant],
    daily_by: Dict[str, List[dict]],
    trade_agg: Dict[str, dict],
) -> Tuple[List[dict], dict]:
    names = [x[1] for x in variants]
    ref_stats = portfolio_stats(daily_by[names[0]], ACCOUNT, metrics_mode="eligible_only")
    summaries: List[dict] = []
    for group, name, _, _ in variants:
        daily = daily_by[name]
        port = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
        agg = trade_agg.get(name, empty_trade_agg())
        summaries.append(
            {
                "batch": batch_id,
                "group": group,
                "variant": name,
                **port,
                "win_rate": agg_win_rate(agg),
                "expectancy": agg_expectancy(agg),
                "halted_days": sum(1 for d in daily if d.get("halted")),
                "cagr_delta_vs_ref": round(port.get("cagr_pct", 0) - ref_stats.get("cagr_pct", 0), 2),
                "worst_day_delta_vs_ref": round(
                    port.get("worst_day_pct", 0) - ref_stats.get("worst_day_pct", 0), 2
                ),
                "max_dd_delta_vs_ref": round(
                    port.get("max_drawdown_pct", 0) - ref_stats.get("max_drawdown_pct", 0), 2
                ),
            }
        )
    return summaries, ref_stats


def write_summary(out_dir: Path, summaries: List[dict], ref: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {"reference": ref, "variants": summaries}
    (out_dir / "summary.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    cols = [
        "group", "variant", "cagr_pct", "sharpe", "max_drawdown_pct", "worst_day_pct",
        "stop_rate", "win_rate", "expectancy", "halted_days",
        "cagr_delta_vs_ref", "worst_day_delta_vs_ref", "max_dd_delta_vs_ref",
    ]
    lines = [",".join(cols)]
    for s in summaries:
        lines.append(",".join(str(s.get(c, "")) for c in cols))
    (out_dir / "summary.csv").write_text("\n".join(lines), encoding="utf-8")


def print_results(batch_id: int, summaries: List[dict], ref: dict) -> None:
    print(f"\n=== BATCH {batch_id} RESULTS (ref: {summaries[0]['variant']}) ===")
    print(f"Ref CAGR {ref['cagr_pct']}%  worst {ref['worst_day_pct']}%  maxDD {ref['max_drawdown_pct']}%")
    for s in sorted(summaries, key=lambda x: x["cagr_pct"], reverse=True):
        flag = ""
        if (
            s["cagr_delta_vs_ref"] > 0
            and s["worst_day_delta_vs_ref"] >= 0
            and s["max_dd_delta_vs_ref"] >= 0
        ):
            flag = " *** PASSES CONSTRAINT ***"
        print(
            f"  {s['group']:16s} {s['variant']:28s}  CAGR {s['cagr_pct']:5.1f}% "
            f"(d{s['cagr_delta_vs_ref']:+.1f})  worst {s['worst_day_pct']:5.1f}% "
            f"(d{s['worst_day_delta_vs_ref']:+.1f})  DD {s['max_drawdown_pct']:5.1f}% "
            f"Sharpe {s['sharpe']:.2f}{flag}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="CAGR improvement batches with checkpoint/resume.")
    parser.add_argument("--batch", type=int, required=True, choices=[1, 2, 3])
    parser.add_argument("--suite", default="", help="Variant suite (p3_gates, tod_schemes, ...)")
    parser.add_argument("--shard", type=int, default=0, help="Shard index (0-based)")
    parser.add_argument("--shards", type=int, default=1, help="Total date shards")
    parser.add_argument("--max-oos-days", type=int, default=0)
    parser.add_argument("--resume", action="store_true", help="Continue from checkpoint.json")
    parser.add_argument("--checkpoint-every", type=int, default=25, help="Save every N OOS days (0=end only)")
    parser.add_argument("--sleep-ms", type=int, default=0, help="Throttle CPU between days")
    parser.add_argument("--carry-trailing", default="", help="Comma-separated trailing stop rates from prior shard")
    args = parser.parse_args()

    carry = [float(x) for x in args.carry_trailing.split(",") if x.strip()] if args.carry_trailing else None
    suite = args.suite.strip() or None

    summaries, ref, _ = run_batch(
        args.batch,
        suite=suite,
        shard=args.shard,
        shards=args.shards,
        max_oos=args.max_oos_days,
        resume=args.resume,
        checkpoint_every=args.checkpoint_every,
        sleep_ms=args.sleep_ms,
        carry_trailing=carry,
    )

    # Per-shard summary (partial metrics — use merge for full batch)
    out_dir = work_dir(args.batch, suite, args.shard, args.shards)
    write_summary(out_dir, summaries, ref)
    if args.shards > 1 or suite:
        print(f"\nWrote shard summary {out_dir / 'summary.json'} (merge for full batch metrics)")
    else:
        print_results(args.batch, summaries, ref)
        print(f"\nWrote {out_dir / 'summary.json'}")


if __name__ == "__main__":
    main()
