"""Execute improvement-plan test matrix (P0 inventory + P1–P4 re-sims).

Single-pass over eligible OOS days; each day runs every variant on the same quotes.
Outputs: data/improvement_plan_tests/summary.json + summary.csv
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from dataclasses import replace
from datetime import time
from pathlib import Path
from statistics import mean
from typing import Callable, Deque, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = ROOT / "simulator"
sys.path.insert(0, str(SIMULATOR))

from dataclasses import replace as dc_replace  # noqa: E402

from expiry_calendar import (  # noqa: E402
    DEFAULT_RULES,
    discover_eligible_dates,
    load_era_rules,
    resolve_start_date,
)
from mbh_simulator import (  # noqa: E402
    SignalSnapshot,
    StrategyConfig,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    trades_to_rows,
)
from portfolio_metrics import portfolio_stats  # noqa: E402
from profiles import PRODUCTION_PROFILE, SCHEMES, WINNERS, build_p3_trend_skew_config  # noqa: E402
from regime_validation import apply_rolling_baseline, discover_dates  # noqa: E402
from stop_calibration_runner import base_config  # noqa: E402
from time_of_day_sizing_runner import TimeOfDaySizePolicy  # noqa: E402
from unconditional_baseline import trade_stats  # noqa: E402

PROCESSED = ROOT / "data" / "processed"
RESULTS = ROOT / "data" / "improvement_plan_tests"
ACCOUNT = 13_000_000.0
TRAIN = 40
SCHEDULE = SCHEMES["linear_decay_downsize"]


def build_variants() -> Dict[str, Tuple[StrategyConfig, Optional[Callable]]]:
    """Return variant_id -> (config, optional extra policy factory)."""
    base = base_config(account_equity=ACCOUNT, baseline_contracts=31, **WINNERS)
    v: Dict[str, Tuple[StrategyConfig, Optional[Callable]]] = {}

    def add(name: str, **kw):
        v[name] = (dc_replace(base, **kw), None)

    # Baseline
    v["baseline"] = (base, None)

    # P1 — tail-day / stop control
    add("p1_flatten_1.5pct", flatten_loss_limit_pct=0.015, daily_loss_limit_pct=0.010)
    add("p1_flatten_2.0pct", flatten_loss_limit_pct=0.020, daily_loss_limit_pct=0.015)
    add("p1_flatten_2.5pct", flatten_loss_limit_pct=0.025, daily_loss_limit_pct=0.020)
    add("p1_halt_only_2.0", flatten_on_daily_loss=False, daily_loss_limit_pct=0.020)
    add("p1_stop_max_3", max_stops_per_side=3)
    add("p1_stop_max_5", max_stops_per_side=5)

    # P2 — two-sided book
    add("p2_max_sides_2", candidate_max_sides=2)
    add("p2_use_condor", use_condor_sleeve=True, candidate_max_sides=2)
    add("p2_portfolio_allocator", use_portfolio_allocator=True, candidate_max_sides=2)

    # P3 — entry filters
    add("p3_trend_gate_1.0", candidate_max_adverse_trend=1.0)
    add("p3_trend_gate_0.65", candidate_max_adverse_trend=0.65)
    add("p3_skew_gate_0.75", candidate_max_adverse_skew=0.75)
    add("p3_entry_start_10_00", entry_start=time(10, 0))
    v[PRODUCTION_PROFILE] = (build_p3_trend_skew_config(account_equity=ACCOUNT), None)

    # P3 combo
    add(
        "p3_combo",
        candidate_max_adverse_trend=1.0,
        candidate_max_adverse_skew=0.75,
        entry_start=time(10, 0),
        max_stops_per_side=3,
    )

    # P4 — regime-aware sizing (same config; policy applied in run loop)
    v["p4_regime_downsize"] = (base, None)

    return v


class RegimeDownsizePolicy(TimeOfDaySizePolicy):
    """P4: halve size when trailing 5-day stop rate > 28%."""

    def __init__(self, schedule, trailing: Deque[float]) -> None:
        super().__init__(schedule)
        self.trailing = trailing

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        base_c = super().contracts(signal, config)
        if len(self.trailing) >= 5 and mean(self.trailing) > 0.28:
            return max(0, round(base_c * 0.5))
        return base_c


def side_mix(trades: List[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"bear_call_pct": 0, "bull_put_pct": 0}
    bc = sum(1 for t in trades if t.get("side") == "bear_call")
    return {"bear_call_pct": round(bc / n * 100, 1), "bull_put_pct": round((n - bc) / n * 100, 1)}


def drift_corr(daily: List[dict], trades: List[dict]) -> Optional[float]:
    if not daily or not trades:
        return None
    import pandas as pd

    t = pd.DataFrame(trades)
    if t.empty:
        return None
    t["entry_time"] = pd.to_datetime(t["entry_time"])
    g = t.sort_values("entry_time").groupby("date")["entry_spot"]
    drift = ((g.last() - g.first()) / g.first()).rename("drift")
    d = pd.DataFrame(daily).merge(drift.reset_index(), on="date", how="left")
    if d["drift"].notna().sum() < 10:
        return None
    return round(float(d["net_pnl"].corr(d["drift"])), 3)


def yearly_pnl(daily: List[dict]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in daily:
        y = row["date"][:4]
        out[y] = out.get(y, 0.0) + float(row["net_pnl"])
    return {k: round(v, 0) for k, v in sorted(out.items())}


def p0_inventory() -> dict:
    import pandas as pd

    manifest = json.loads((ROOT / "data" / "inventory" / "manifest.json").read_text())
    proc = manifest["processed"]["count"]
    raw_not_built = manifest["cache_status"]["raw_not_built_count"]
    # vix check on recent day
    sig = ROOT / "data" / "processed" / "symbol=SPXW" / "date=2026-07-02" / "signals.csv"
    vix_frac = 0.0
    if sig.exists():
        s = pd.read_csv(sig, nrows=500)
        vix_frac = float(s["vix"].notna().mean()) if "vix" in s.columns else 0.0
    return {
        "processed_days": proc,
        "raw_unbuilt_days": raw_not_built,
        "missing_years": manifest["cache_status"]["missing_processed_by_year"],
        "vix_populated_fraction_sample": vix_frac,
        "note": "Build with build_missing_processed.py (no ThetaData credits).",
    }


def run_matrix(max_oos_days: int = 0) -> List[dict]:
    floor, eras = load_era_rules(DEFAULT_RULES)
    processed_dates = discover_dates(PROCESSED, "SPXW")
    resolved_start = resolve_start_date(processed_dates, floor, require_mon_and_wed=True)
    end_date = processed_dates[-1]
    eligible = discover_eligible_dates(processed_dates, floor=resolved_start, end=end_date, eras=eras)

    variants = build_variants()
    names = list(variants.keys())
    daily_by: Dict[str, List[dict]] = {n: [] for n in names}
    trades_by: Dict[str, List[dict]] = {n: [] for n in names}
    trailing_stop: Deque[float] = deque(maxlen=5)

    oos = len(eligible) - TRAIN
    if max_oos_days > 0:
        eligible = eligible[: TRAIN + max_oos_days]
        oos = max_oos_days
    print(f"Running {len(names)} variants × {oos} OOS days...", flush=True)

    for index in range(TRAIN, len(eligible)):
        test_date = eligible[index]
        train_dates = eligible[index - TRAIN : index]
        apply_rolling_baseline(PROCESSED, "SPXW", train_dates, test_date, "signals_unconditional.csv")
        day_dir = PROCESSED / "symbol=SPXW" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / "signals_unconditional.csv")

        regime_policy = RegimeDownsizePolicy(SCHEDULE, trailing_stop)

        for name, (cfg, _) in variants.items():
            if name == "p4_regime_downsize":
                policy = regime_policy
            else:
                policy = TimeOfDaySizePolicy(SCHEDULE)
            result = simulate_day(quotes, signals, config=cfg, policy=policy)
            rows = trades_to_rows(result.trades)
            for r in rows:
                r["date"] = test_date
                trades_by[name].append(r)
            daily_by[name].append(
                {
                    "date": test_date,
                    "eligible": True,
                    "trades": len(result.trades),
                    "stopped_trades": sum(1 for t in result.trades if t.stopped),
                    "net_pnl": round(result.net_pnl, 2),
                    "halted": result.halted,
                }
            )

        # Update trailing stop rate from baseline for regime policy
        base_day = daily_by["baseline"][-1]
        tr = int(base_day["trades"])
        sr = int(base_day["stopped_trades"]) / tr if tr else 0.0
        trailing_stop.append(sr)

        done = index - TRAIN + 1
        if done % 50 == 0 or done == oos:
            print(f"  {done}/{oos} ({test_date})", flush=True)

    # Add P4 variant after loop — need to re-run? Actually I forgot to add p4 to build_variants.
    # Fix: add p4_regime_downsize as duplicate baseline config but use regime policy — handled above.

    summaries: List[dict] = []
    base_yearly = yearly_pnl(daily_by["baseline"])

    for name in names:
        daily = daily_by[name]
        trades = [t for t in trades_by[name] if t.get("model") != "net_long_overlay"]
        port = portfolio_stats(daily, ACCOUNT, metrics_mode="eligible_only")
        ts = trade_stats(trades)
        yr = yearly_pnl(daily)
        years_beat = sum(1 for y, p in yr.items() if p > base_yearly.get(y, 0))
        sm = side_mix(trades)
        summaries.append(
            {
                "variant": name,
                **port,
                "win_rate": ts["win_rate"],
                "expectancy": ts["expectancy_per_trade"],
                "halted_days": sum(1 for d in daily if d.get("halted")),
                "bear_call_pct": sm["bear_call_pct"],
                "drift_corr": drift_corr(daily, trades),
                "yearly_pnl": yr,
                "years_beat_baseline": years_beat,
            }
        )

    return summaries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-oos-days", type=int, default=0, help="Limit OOS days (0=all)")
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    p0 = p0_inventory()
    (RESULTS / "p0_inventory.json").write_text(json.dumps(p0, indent=2), encoding="utf-8")
    print("P0 inventory:", json.dumps(p0, indent=2), flush=True)

    summaries = run_matrix(max_oos_days=args.max_oos_days)
    (RESULTS / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")

    # CSV
    cols = [
        "variant", "net_pnl", "cagr_pct", "sharpe", "max_drawdown_pct", "worst_day",
        "stop_rate", "halted_days", "bear_call_pct", "drift_corr", "years_beat_baseline",
    ]
    lines = [",".join(cols)]
    for s in summaries:
        lines.append(",".join(str(s.get(c, "")) for c in cols))
    (RESULTS / "summary.csv").write_text("\n".join(lines), encoding="utf-8")

    print("\n=== RESULTS (sorted by net_pnl) ===")
    for s in sorted(summaries, key=lambda x: x["net_pnl"], reverse=True):
        print(
            f"{s['variant']:28s}  pnl ${s['net_pnl']:>11,.0f}  "
            f"CAGR {s['cagr_pct']:5.1f}%  Sharpe {s['sharpe']:4.2f}  "
            f"DD {s['max_drawdown_pct']:5.1f}%  halt {s['halted_days']:2d}  "
            f"BC {s['bear_call_pct']:5.1f}%  drift_r {s['drift_corr']}"
        )


if __name__ == "__main__":
    main()
