"""Test 3G — time-of-day contract weighting on top of the frozen 3D_flatten_3.5 config.

Motivation
----------
The production 3D config sells a flat 31 contracts at every 15-minute tranche
(09:32 -> 15:17). Late-day tranches add fresh 0DTE max-loss exposure right when
there is the least time for a position to recover before the daily-loss governor
or settlement. This suite keeps the 3D stop/flatten substrate identical and only
reshapes *how many contracts we sell as a function of time of day* — selling more
early and less late — to shrink peak concentration and tail (worst-day / max-DD)
risk.

Every scheme is a piecewise multiplier applied to the 31-contract baseline. Some
schemes are ~size-neutral (pure reshaping) and some net-downsize the book so we
can read the risk/return trade-off directly against the flat control.

Implementation is non-invasive: the core simulator is untouched. Sizing is driven
by a custom `TimeOfDaySizePolicy` that scales `baseline_contracts` using the
entry-tranche timestamp carried on each SignalSnapshot. All schemes are evaluated
in a single walk-forward pass (rolling baseline + quotes read once per day, reused
across schemes) for speed.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from statistics import mean
from typing import Dict, List, Optional, Sequence, Tuple

from historical_baselines import write_csv
from mbh_simulator import (
    DefaultSignalPolicy,
    SignalSnapshot,
    StrategyConfig,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    stop_diagnostics_to_rows,
    trades_to_rows,
)
from regime_validation import apply_rolling_baseline, discover_dates
from stop_calibration_runner import base_config, portfolio_stats
from unconditional_baseline import trade_stats

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "time_of_day_sizing"

# Frozen 3D winner (matches simulator/export_dashboard_run.py WINNERS + dashboard).
WINNERS = {
    "stop_multiple": 3.0,
    "stop_confirmation_count": 2,
    "same_side_stop_cooldown_minutes": 0,
    "max_stops_per_side": 999,
    "daily_loss_limit_pct": 0.0225,
    "flatten_on_daily_loss": True,
    "flatten_loss_limit_pct": 0.035,
}

# --- Time-of-day weighting schemes ------------------------------------------
# Each schedule is an ordered list of (upper_bound_time, multiplier). For an
# entry at time t we use the multiplier of the first segment with t < bound.
# The final segment (time(23, 59)) is the afternoon catch-all. Multipliers are
# applied to the 31-contract baseline and rounded; 0.0 halts new entries in that
# window. Segments are chosen so the tranche cadence (09:32..15:17, every 15m)
# maps cleanly onto the buckets.
Schedule = List[Tuple[time, float]]

SCHEMES: Dict[str, Schedule] = {
    # Control: flat 1.0x == production 3D_flatten_3.5. Sanity check vs dashboard.
    "control_flat": [
        (time(23, 59), 1.0),
    ],
    # Pure reshaping, ~size-neutral: heavier morning, lighter afternoon.
    "linear_decay_neutral": [
        (time(10, 30), 1.50),
        (time(11, 30), 1.25),
        (time(12, 30), 1.00),
        (time(13, 30), 0.75),
        (time(14, 30), 0.60),
        (time(23, 59), 0.50),
    ],
    # Same shape but net-downsized (avg ~0.75x): reshapes AND shrinks the book.
    "linear_decay_downsize": [
        (time(10, 30), 1.25),
        (time(11, 30), 1.00),
        (time(12, 30), 0.85),
        (time(13, 30), 0.60),
        (time(14, 30), 0.45),
        (time(23, 59), 0.25),
    ],
    # Three mild blocks: morning heavier, afternoon lighter.
    "step_3block_mild": [
        (time(11, 30), 1.25),
        (time(13, 30), 1.00),
        (time(23, 59), 0.50),
    ],
    # Three aggressive blocks.
    "step_3block_aggressive": [
        (time(11, 0), 1.50),
        (time(13, 0), 0.75),
        (time(23, 59), 0.33),
    ],
    # Front-load the morning, taper hard into the afternoon.
    "front_load_morning": [
        (time(12, 0), 1.25),
        (time(14, 0), 0.50),
        (time(23, 59), 0.25),
    ],
    # Full morning size, half midday, halt new entries after 14:00.
    "morning_heavy_afternoon_off": [
        (time(12, 0), 1.00),
        (time(14, 0), 0.50),
        (time(23, 59), 0.00),
    ],
    # Simple downsize: half size after noon.
    "half_after_noon": [
        (time(12, 0), 1.00),
        (time(23, 59), 0.50),
    ],
    # Four-step smooth taper.
    "taper_4step": [
        (time(10, 30), 1.50),
        (time(12, 0), 1.00),
        (time(13, 30), 0.60),
        (time(23, 59), 0.30),
    ],
}


def schedule_multiplier(t: time, schedule: Schedule) -> float:
    for bound, mult in schedule:
        if t < bound:
            return mult
    return schedule[-1][1]


class TimeOfDaySizePolicy(DefaultSignalPolicy):
    """Scale baseline_contracts by a time-of-day multiplier (no VIX/danger scaling)."""

    def __init__(self, schedule: Schedule) -> None:
        self.schedule = schedule

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        if signal is None:
            return config.baseline_contracts
        mult = schedule_multiplier(signal.timestamp.time(), self.schedule)
        return max(0, round(config.baseline_contracts * mult))


def scheme_avg_multiplier(schedule: Schedule, config: StrategyConfig) -> float:
    """Mean multiplier across the actual entry-tranche grid (09:32..entry_end)."""
    start_m = config.entry_start.hour * 60 + config.entry_start.minute
    end_m = config.entry_end.hour * 60 + config.entry_end.minute
    mults = []
    m = start_m
    while m <= end_m:
        mults.append(schedule_multiplier(time(m // 60, m % 60), schedule))
        m += config.entry_interval_minutes
    return mean(mults) if mults else 0.0


def run() -> None:
    parser = argparse.ArgumentParser(description="Test 3G time-of-day contract weighting sweep.")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--signals-filename", default="signals_unconditional.csv")
    parser.add_argument("--baseline-contracts", type=int, default=31)
    parser.add_argument("--account-equity", type=float, default=13_000_000.0)
    parser.add_argument("--max-days", type=int, default=0, help="Limit OOS days (0=all)")
    parser.add_argument("--schemes", default="", help="Comma list to restrict schemes (default all)")
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    config = base_config(
        account_equity=args.account_equity,
        baseline_contracts=args.baseline_contracts,
        **WINNERS,
    )

    schemes = dict(SCHEMES)
    if args.schemes:
        wanted = {s.strip() for s in args.schemes.split(",") if s.strip()}
        schemes = {k: v for k, v in SCHEMES.items() if k in wanted}
        if not schemes:
            raise SystemExit(f"No matching schemes in {sorted(SCHEMES)}")

    dates = discover_dates(processed_dir, args.symbol)
    if len(dates) <= args.train_count:
        raise SystemExit(f"Need more than {args.train_count} dates; have {len(dates)}.")
    oos_end = len(dates) if args.max_days <= 0 else min(len(dates), args.train_count + args.max_days)

    policies = {name: TimeOfDaySizePolicy(sched) for name, sched in schemes.items()}
    daily_by_scheme: Dict[str, List[dict]] = {name: [] for name in schemes}
    trades_by_scheme: Dict[str, List[dict]] = {name: [] for name in schemes}
    stops_by_scheme: Dict[str, List[dict]] = {name: [] for name in schemes}

    oos_days = oos_end - args.train_count
    print(f"Running {len(schemes)} schemes over {oos_days} OOS days (single-pass)...")

    for index in range(args.train_count, oos_end):
        test_date = dates[index]
        train_dates = dates[index - args.train_count : index]
        apply_rolling_baseline(processed_dir, args.symbol, train_dates, test_date, args.signals_filename)
        day_dir = processed_dir / f"symbol={args.symbol}" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / args.signals_filename)

        for name, policy in policies.items():
            result = simulate_day(quotes, signals, config=config, policy=policy)
            day_trades = trades_to_rows(result.trades)
            day_stops = stop_diagnostics_to_rows(result.trades)
            for row in day_trades:
                row["date"] = test_date
                trades_by_scheme[name].append(row)
            for row in day_stops:
                row["date"] = test_date
                stops_by_scheme[name].append(row)
            contracts_sold = sum(int(t.contracts) for t in result.trades)
            daily_by_scheme[name].append(
                {
                    "date": test_date,
                    "trades": len(result.trades),
                    "contracts_sold": contracts_sold,
                    "stopped_trades": sum(1 for t in result.trades if t.stopped),
                    "net_pnl": round(result.net_pnl, 2),
                    "halted": result.halted,
                }
            )
        done = index - args.train_count + 1
        if done % 50 == 0:
            print(f"  {done}/{oos_days} OOS days done ({test_date})")

    summary_rows: List[dict] = []
    control_contracts = None
    for name, sched in schemes.items():
        daily_rows = daily_by_scheme[name]
        spread_trades = [r for r in trades_by_scheme[name] if r.get("model") != "net_long_overlay"]
        ts = trade_stats(spread_trades)
        port = portfolio_stats(daily_rows, config.account_equity)
        total_contracts = sum(int(r["contracts_sold"]) for r in daily_rows)
        if name == "control_flat":
            control_contracts = total_contracts

        out_dir = results_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)
        write_csv(out_dir / "daily_summary.csv", daily_rows)
        write_csv(out_dir / "trades.csv", trades_by_scheme[name])
        write_csv(out_dir / "stop_diagnostics.csv", stops_by_scheme[name])

        summary_rows.append(
            {
                "scheme": name,
                "avg_multiplier": round(scheme_avg_multiplier(sched, config), 3),
                **port,
                "total_contracts": total_contracts,
                "spread_win_rate": ts["win_rate"],
                "spread_expectancy": ts["expectancy_per_trade"],
            }
        )

    # Fill relative-contract column now that control is known.
    for row in summary_rows:
        if control_contracts:
            row["contracts_vs_control_pct"] = round(row["total_contracts"] / control_contracts * 100.0, 1)
        else:
            row["contracts_vs_control_pct"] = 100.0

    write_csv(results_dir / "sizing_summary.csv", summary_rows)
    (results_dir / "sizing_summary.json").write_text(json.dumps(summary_rows, indent=2), encoding="utf-8")

    report = build_report(summary_rows, schemes, config, oos_days)
    report_path = ROOT / f"time_of_day_sizing_results_{datetime.now().strftime('%Y-%m-%d')}.md"
    report_path.write_text(report, encoding="utf-8")
    (results_dir / "report.md").write_text(report, encoding="utf-8")

    print("\n=== Test 3G: Time-of-day contract weighting ===")
    hdr = f"  {'scheme':<28} {'avgX':>5} {'CAGR%':>7} {'Shrp':>5} {'worst%':>7} {'maxDD%':>7} {'size%':>6}"
    print(hdr)
    for row in summary_rows:
        print(
            f"  {row['scheme']:<28} {row['avg_multiplier']:>5.2f} {row['cagr_pct']:>7.1f} "
            f"{row['sharpe']:>5.2f} {row['worst_day_pct']:>7.1f} {row['max_drawdown_pct']:>7.1f} "
            f"{row['contracts_vs_control_pct']:>6.1f}"
        )
    print(f"\nWrote {results_dir / 'sizing_summary.csv'}")
    print(f"Wrote {report_path}")


def build_report(
    summary_rows: Sequence[dict],
    schemes: Dict[str, Schedule],
    config: StrategyConfig,
    oos_days: int,
) -> str:
    lines = [
        "# Test 3G — Time-of-Day Contract Weighting (on 3D_flatten_3.5)",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Substrate: frozen `3D_flatten_3.5` (wide wings put 200 / call 75, 3x short-leg "
        "stop w/ 2-bar confirm, halt -2.25%, flatten -3.5%). Only the per-tranche "
        f"contract size is reshaped by time of day. Baseline {config.baseline_contracts} "
        f"contracts, {oos_days} OOS days, ${config.account_equity:,.0f} equity.",
        "",
        "Goal: sell more early / less late to cut peak concentration and tail (worst-day "
        "and max-drawdown) risk, and read the return give-up against the flat control.",
        "",
        "## Results",
        "",
        "| Scheme | Avg size | CAGR | Sharpe | Worst day | Max DD | Day win | Contracts vs control |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary_rows:
        lines.append(
            f"| {row['scheme']} | {row['avg_multiplier']:.2f}x | {row['cagr_pct']:.1f}% | "
            f"{row['sharpe']:.2f} | {row['worst_day_pct']:.2f}% | {row['max_drawdown_pct']:.2f}% | "
            f"{row.get('day_win_rate', 0)*100:.1f}% | {row['contracts_vs_control_pct']:.1f}% |"
        )

    lines.extend(["", "## Scheme definitions (multiplier of baseline by entry time)", ""])
    for name, sched in schemes.items():
        parts = []
        prev = config.entry_start.strftime("%H:%M")
        for bound, mult in sched:
            label = "close" if bound >= time(23, 0) else bound.strftime("%H:%M")
            parts.append(f"{prev}-{label}: {mult:g}x")
            prev = label
        lines.append(f"- **{name}**: " + ", ".join(parts))

    lines.extend(
        [
            "",
            "## How to read this",
            "",
            "- `control_flat` reproduces production 3D (flat 31 contracts).",
            "- Schemes with avg size ~1.0x are pure reshaping (same capital, timed differently).",
            "- Schemes with avg size < 1.0x also net-downsize the book.",
            "- Prefer schemes that materially improve worst-day / max-DD for a small CAGR give-up.",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    run()
