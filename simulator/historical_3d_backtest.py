"""Historical 3D backtest with SPXW expiration-era calendar filtering.

Runs the frozen 3D_flatten_3.5 substrate only on eligible weekdays (Mon/Wed/Fri
before Tue/Thu listings, then all weekdays). Portfolio metrics use the
eligible-day equity path — skipped Tue/Thu pre-2022 are excluded from CAGR,
vol, and drawdown.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from expiry_calendar import (
    DEFAULT_RULES,
    build_calendar_audit,
    discover_eligible_dates,
    era_for_date,
    load_era_rules,
    parse_date,
    resolve_start_date,
    summarize_eras,
)
from historical_baselines import write_csv
from mbh_simulator import read_quotes_csv, read_signals_csv, simulate_day, stop_diagnostics_to_rows, trades_to_rows
from portfolio_metrics import portfolio_stats
from profiles import SCHEMES, WINNERS
from regime_validation import apply_rolling_baseline, discover_dates
from stop_calibration_runner import base_config
from time_of_day_sizing_runner import TimeOfDaySizePolicy
from unconditional_baseline import FixedSizePolicy, trade_stats

ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = Path(__file__).resolve().parent
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "historical_3d_mwf_to_daily"
BASELINE_RUN = ROOT / "data" / "dashboard_runs" / "linear_decay_downsize"


def read_csv_rows(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def build_policy(scheme: str):
    if scheme in ("", "control_flat"):
        return FixedSizePolicy()
    if scheme not in SCHEMES:
        raise SystemExit(f"Unknown sizing scheme {scheme!r}; choose from {sorted(SCHEMES)}")
    return TimeOfDaySizePolicy(SCHEMES[scheme])


def run_backfill(start: str, end: str, chunk_size: int) -> None:
    if not os.environ.get("THETADATA_API_KEY"):
        print("THETADATA_API_KEY not set — skipping download/build backfill.")
        return
    cmd = [
        sys.executable,
        str(SIMULATOR / "backfill_history.py"),
        "--all",
        "--start-date",
        start,
        "--end-date",
        end,
        "--chunk-size",
        str(chunk_size),
    ]
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True)


def run_simulation(
    *,
    processed_dir: Path,
    symbol: str,
    eligible_dates: List[str],
    eras,
    train_count: int,
    signals_filename: str,
    config,
    policy,
) -> tuple[List[dict], List[dict], List[dict]]:
    if len(eligible_dates) <= train_count:
        raise SystemExit(
            f"Need more than {train_count} eligible dates; have {len(eligible_dates)}."
        )

    daily_rows: List[dict] = []
    all_trades: List[dict] = []
    stop_rows: List[dict] = []

    oos_days = len(eligible_dates) - train_count
    print(f"Simulating {oos_days} OOS eligible days (train={train_count})...")

    for index in range(train_count, len(eligible_dates)):
        test_date = eligible_dates[index]
        train_dates = eligible_dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, signals_filename)
        day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
        result = simulate_day(
            read_quotes_csv(day_dir / "normalized_option_quotes.csv"),
            read_signals_csv(day_dir / signals_filename),
            config=config,
            policy=policy,
        )
        day = parse_date(test_date)
        era = era_for_date(day, eras)

        day_trades = trades_to_rows(result.trades)
        day_stops = stop_diagnostics_to_rows(result.trades)
        for row in day_trades:
            row["date"] = test_date
            all_trades.append(row)
        for row in day_stops:
            row["date"] = test_date
            stop_rows.append(row)

        daily_rows.append(
            {
                "date": test_date,
                "weekday": day.strftime("%a"),
                "era": era,
                "eligible": True,
                "traded": len(result.trades) > 0,
                "trades": len(result.trades),
                "contracts_sold": sum(int(t.contracts) for t in result.trades),
                "stopped_trades": sum(1 for t in result.trades if t.stopped),
                "net_pnl": round(result.net_pnl, 2),
                "halted": result.halted,
            }
        )

        done = index - train_count + 1
        if done % 50 == 0 or done == oos_days:
            print(f"  {done}/{oos_days} OOS days done ({test_date})")

    return daily_rows, all_trades, stop_rows


def compare_regression(
    baseline_dir: Path,
    new_daily: List[dict],
    new_trades: List[dict],
) -> dict:
    base_daily = read_csv_rows(baseline_dir / "daily_summary.csv")
    base_trades = read_csv_rows(baseline_dir / "trades.csv")

    base_pnl = sum(float(r.get("net_pnl", 0)) for r in base_daily)
    new_pnl = sum(float(r.get("net_pnl", 0)) for r in new_daily)

    return {
        "baseline_days": len(base_daily),
        "new_days": len(new_daily),
        "dates_match": [r["date"] for r in base_daily] == [r["date"] for r in new_daily],
        "baseline_trades": len(base_trades),
        "new_trades": len(new_trades),
        "trades_match": len(base_trades) == len(new_trades),
        "baseline_net_pnl": round(base_pnl, 2),
        "new_net_pnl": round(new_pnl, 2),
        "pnl_match": abs(base_pnl - new_pnl) < 0.02,
        "passed": (
            [r["date"] for r in base_daily] == [r["date"] for r in new_daily]
            and len(base_trades) == len(new_trades)
            and abs(base_pnl - new_pnl) < 0.02
        ),
    }


def sanity_checks(trades: List[dict]) -> List[str]:
    errors: List[str] = []
    for row in trades:
        day = row.get("date") or (row.get("entry_time") or "")[:10]
        if not day:
            continue
        d = parse_date(day)
        if d.weekday() == 1 and day < "2022-04-18":
            errors.append(f"Tue trade before 2022-04-18: {day}")
        if d.weekday() == 3 and day < "2022-05-11":
            errors.append(f"Thu trade before 2022-05-11: {day}")
    return errors


def write_summary_md(path: Path, summary: dict) -> None:
    lines = [
        "# Historical 3D Backtest Summary",
        "",
        f"Generated: {summary['generated_at']}",
        "",
        "## Configuration",
        f"- Start (resolved): `{summary['start_date']}`",
        f"- End: `{summary['end_date']}`",
        f"- Sizing: `{summary['sizing_scheme']}`",
        f"- Metrics mode: `{summary['metrics_mode']}`",
        f"- Train count: {summary['train_count']}",
        "",
        "## Headline (eligible-day path)",
    ]
    head = summary["headline"]
    lines.extend(
        [
            f"- OOS eligible days: {head.get('days', 0)}",
            f"- Trades: {head.get('trades', 0)}",
            f"- Net P&L: ${head.get('net_pnl', 0):,.2f}",
            f"- CAGR: {head.get('cagr_pct', 0):.2f}%",
            f"- Ann vol: {head.get('ann_vol_pct', 0):.2f}%",
            f"- Max DD: {head.get('max_drawdown_pct', 0):.2f}%",
            f"- Sharpe: {head.get('sharpe', 0):.2f}",
            "",
        ]
    )
    if summary.get("regression"):
        reg = summary["regression"]
        lines.extend(
            [
                "## Regression vs dashboard baseline",
                f"- Passed: **{reg['passed']}**",
                f"- Days: {reg['baseline_days']} vs {reg['new_days']} (dates_match={reg['dates_match']})",
                f"- Trades: {reg['baseline_trades']} vs {reg['new_trades']}",
                f"- Net P&L: ${reg['baseline_net_pnl']:,.2f} vs ${reg['new_net_pnl']:,.2f}",
                "",
            ]
        )
    if summary.get("era_summaries"):
        lines.append("## By era")
        lines.append("| Era | Days | Trades | CAGR | Ann vol | Max DD | Sharpe |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for row in summary["era_summaries"]:
            lines.append(
                f"| {row['era']} | {row['days']} | {row['trades']} | "
                f"{row['cagr_pct']:.1f}% | {row['ann_vol_pct']:.1f}% | "
                f"{row['max_drawdown_pct']:.1f}% | {row['sharpe']:.2f} |"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Historical 3D backtest with expiration-era calendar.")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--signals-filename", default="signals_unconditional.csv")
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--start-date", default="", help="Override floor (default from era rules JSON).")
    parser.add_argument("--end-date", default="", help="Default: latest processed date.")
    parser.add_argument("--sizing-scheme", default="linear_decay_downsize")
    parser.add_argument("--account-equity", type=float, default=13_000_000.0)
    parser.add_argument("--baseline-contracts", type=int, default=31)
    parser.add_argument(
        "--metrics-mode",
        default="eligible_only",
        choices=["eligible_only", "traded_only", "all_rows"],
    )
    parser.add_argument("--rules-file", default=str(DEFAULT_RULES))
    parser.add_argument(
        "--download",
        action="store_true",
        help="Backfill missing raw/processed via ThetaData (costs API credits; see data/README.md).",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Required with --download to confirm paid ThetaData fetch.",
    )
    parser.add_argument("--chunk-size", type=int, default=10)
    parser.add_argument(
        "--regression",
        action="store_true",
        help="Run 2023-2025 window and compare to data/dashboard_runs/linear_decay_downsize.",
    )
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    floor, eras = load_era_rules(Path(args.rules_file))
    processed_dates = discover_dates(processed_dir, args.symbol)
    if not processed_dates:
        raise SystemExit(f"No processed dates under {processed_dir}")

    end_date = args.end_date or processed_dates[-1]
    start_floor = args.start_date or floor
    resolved_start = resolve_start_date(processed_dates, start_floor, require_mon_and_wed=True)

    if args.regression:
        resolved_start = "2023-01-03"
        end_date = "2025-12-29"

    if args.download:
        if not args.force_download:
            raise SystemExit(
                "Refusing --download without --force-download. "
                "Local cache is under data/processed/ — see data/inventory/manifest.json. "
                "Re-run with --force-download only if the user explicitly wants to pay for ThetaData."
            )
        run_backfill(resolved_start, end_date, args.chunk_size)
        processed_dates = discover_dates(processed_dir, args.symbol)
        if not args.end_date:
            end_date = processed_dates[-1]

    eligible_dates = discover_eligible_dates(
        processed_dates,
        floor=resolved_start,
        end=end_date,
        eras=eras,
    )
    calendar_audit = build_calendar_audit(
        processed_dates,
        floor=resolved_start,
        end=end_date,
        eras=eras,
    )

    config = base_config(
        account_equity=args.account_equity,
        baseline_contracts=args.baseline_contracts,
        **WINNERS,
    )
    policy = build_policy(args.sizing_scheme)

    daily_rows, all_trades, stop_rows = run_simulation(
        processed_dir=processed_dir,
        symbol=args.symbol,
        eligible_dates=eligible_dates,
        eras=eras,
        train_count=args.train_count,
        signals_filename=args.signals_filename,
        config=config,
        policy=policy,
    )

    spread_trades = [r for r in all_trades if r.get("model") != "net_long_overlay"]
    ts = trade_stats(spread_trades)
    headline = portfolio_stats(daily_rows, config.account_equity, metrics_mode=args.metrics_mode)
    era_summaries = summarize_eras(daily_rows, config.account_equity)
    checks = sanity_checks(all_trades)

    regression: Optional[dict] = None
    if args.regression:
        regression = compare_regression(BASELINE_RUN, daily_rows, all_trades)
        print(json.dumps(regression, indent=2))

    summary = {
        "generated_at": datetime.now().isoformat(),
        "start_date": resolved_start,
        "end_date": end_date,
        "sizing_scheme": args.sizing_scheme,
        "metrics_mode": args.metrics_mode,
        "train_count": args.train_count,
        "processed_dates": len(processed_dates),
        "eligible_dates": len(eligible_dates),
        "oos_eligible_days": len(daily_rows),
        "first_oos_date": daily_rows[0]["date"] if daily_rows else "",
        "last_oos_date": daily_rows[-1]["date"] if daily_rows else "",
        "headline": headline,
        "trade_stats": ts,
        "era_summaries": era_summaries,
        "sanity_errors": checks,
        "regression": regression,
    }

    write_csv(results_dir / "calendar_audit.csv", calendar_audit)
    write_csv(results_dir / "daily_summary.csv", daily_rows)
    write_csv(results_dir / "trades.csv", all_trades)
    write_csv(results_dir / "stop_diagnostics.csv", stop_rows)
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_summary_md(results_dir / "summary.md", summary)

    print(
        f"Done: {len(daily_rows)} OOS days, {headline.get('trades', 0)} trades, "
        f"CAGR {headline.get('cagr_pct', 0):.1f}%, max DD {headline.get('max_drawdown_pct', 0):.1f}%"
    )
    if checks:
        print(f"WARNING: {len(checks)} calendar sanity errors")
    if regression and not regression["passed"]:
        raise SystemExit("Regression check failed — see summary.json")


if __name__ == "__main__":
    main()
