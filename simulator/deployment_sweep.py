from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
TRADING_DAYS = 252


def read_rows(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value in {"", None}:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def summarize(results_dir: Path, account_equity: float, target_margin_pct: float, baseline_contracts: int) -> dict:
    rows = read_rows(results_dir / "daily_regime_validation.csv")
    days = len(rows)
    trades = sum(safe_int(row.get("trades")) for row in rows)
    stops = sum(safe_int(row.get("stopped_trades")) for row in rows)
    pnl = sum(safe_float(row.get("net_pnl")) for row in rows)
    credit = sum(safe_float(row.get("gross_credit_sold")) for row in rows)
    max_margin = max((safe_float(row.get("approx_spread_margin")) for row in rows), default=0.0)
    avg_margin = sum(safe_float(row.get("approx_spread_margin")) for row in rows) / days if days else 0.0
    worst_day = min((safe_float(row.get("net_pnl")) for row in rows), default=0.0)
    halted_days = sum(1 for row in rows if str(row.get("halted")) == "True")
    return {
        "target_margin_pct": target_margin_pct,
        "baseline_contracts": baseline_contracts,
        "results_dir": str(results_dir),
        "days": days,
        "trades": trades,
        "stopped_trades": stops,
        "stop_rate": round(stops / trades, 6) if trades else 0.0,
        "net_pnl": round(pnl, 2),
        "annualized_return_pct": (pnl / account_equity) * (TRADING_DAYS / days) * 100.0 if days and account_equity else 0.0,
        "gross_credit_sold": round(credit, 2),
        "avg_daily_credit": round(credit / days, 2) if days else 0.0,
        "max_margin": round(max_margin, 2),
        "max_margin_pct_equity": round(max_margin / account_equity * 100.0, 4) if account_equity else 0.0,
        "avg_margin": round(avg_margin, 2),
        "avg_margin_pct_equity": round(avg_margin / account_equity * 100.0, 4) if account_equity else 0.0,
        "worst_day": round(worst_day, 2),
        "worst_day_pct_equity": round(worst_day / account_equity * 100.0, 4) if account_equity else 0.0,
        "halted_days": halted_days,
    }


def write_markdown(path: Path, rows: List[dict]) -> None:
    lines = [
        "# Deployment Sweep",
        "",
        "These are true simulator reruns with higher contract counts, not linear estimates.",
        "",
        "| Target max margin | Contracts | Trades | Stops | Stop rate | Net P&L | Ann. return | Avg credit/day | Max margin | Avg margin | Worst day | Halted days |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {target_margin_pct:.1f}% | {baseline_contracts} | {trades} | {stopped_trades} | {stop_rate:.1%} | ${net_pnl:,.2f} | {annualized_return_pct:.2f}% | ${avg_daily_credit:,.2f} | ${max_margin:,.2f} | ${avg_margin:,.2f} | ${worst_day:,.2f} | {halted_days} |".format(
                **row
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_pcts(value: str) -> List[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Rerun the current strategy at target max-margin deployment tiers.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--start-date", default="2025-04-01")
    parser.add_argument("--end-date", default="2025-09-30")
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--account-equity", type=float, default=13_000_000)
    parser.add_argument("--base-contracts", type=int, default=31)
    parser.add_argument("--base-max-margin-pct", type=float, default=1.0873846153846154)
    parser.add_argument("--daily-credit-cap-pct", type=float, default=0.015)
    parser.add_argument("--one-dte-sleeve", action="store_true")
    parser.add_argument("--portfolio-allocator", action="store_true")
    parser.add_argument("--portfolio-margin-budget-pct", type=float, default=0.40)
    parser.add_argument("--core-margin-budget-pct", type=float, default=0.35)
    parser.add_argument("--exploratory-margin-budget-pct", type=float, default=0.02)
    parser.add_argument("--condor-margin-budget-pct", type=float, default=0.03)
    parser.add_argument("--one-dte-margin-budget-pct", type=float, default=0.0)
    parser.add_argument("--trend-debit-sleeve", action="store_true")
    parser.add_argument("--trend-debit-margin-budget-pct", type=float, default=0.03)
    parser.add_argument("--trend-debit-size-fraction", type=float, default=0.10)
    parser.add_argument("--trend-debit-min-abs-trend-score", type=float, default=1.75)
    parser.add_argument("--long-put-hedge-sleeve", action="store_true")
    parser.add_argument("--long-put-hedge-margin-budget-pct", type=float, default=0.02)
    parser.add_argument("--long-put-hedge-size-fraction", type=float, default=0.08)
    parser.add_argument("--long-put-hedge-min-downtrend-score", type=float, default=1.25)
    parser.add_argument("--long-put-hedge-min-realized-z", type=float, default=1.25)
    parser.add_argument("--target-margin-pcts", default="5,10,20,40")
    parser.add_argument("--results-root", default=str(ROOT / "data" / "deployment_sweep_guard_time_exploratory240"))
    parser.add_argument("--output-csv", default=str(ROOT / "data" / "deployment_sweep_guard_time_exploratory240.csv"))
    parser.add_argument("--output-md", default=str(ROOT / "deployment_sweep_guard_time_exploratory240.md"))
    args = parser.parse_args()

    summaries = []
    for target_pct in parse_pcts(args.target_margin_pcts):
        scale = target_pct / args.base_max_margin_pct
        contracts = max(1, round(args.base_contracts * scale))
        results_dir = Path(args.results_root) / f"target_margin_{str(target_pct).replace('.', 'p')}_contracts_{contracts}"
        command = [
            sys.executable,
            str(SIM / "regime_validation.py"),
            "--symbol",
            args.symbol,
            "--start-date",
            args.start_date,
            "--end-date",
            args.end_date,
            "--results-dir",
            str(results_dir),
            "--train-count",
            str(args.train_count),
            "--account-equity",
            str(args.account_equity),
            "--baseline-contracts",
            str(contracts),
            "--daily-credit-cap-pct",
            str(args.daily_credit_cap_pct),
            "--two-tier-engine",
            "--event-controls",
            "--time-of-day-controls",
            "--exploratory-min-score",
            "2.40",
            "--exploratory-max-score",
            "2.49",
        ]
        if args.one_dte_sleeve:
            command.append("--one-dte-sleeve")
        if args.portfolio_allocator:
            command.extend(
                [
                    "--portfolio-allocator",
                    "--portfolio-margin-budget-pct",
                    str(args.portfolio_margin_budget_pct),
                    "--core-margin-budget-pct",
                    str(args.core_margin_budget_pct),
                    "--exploratory-margin-budget-pct",
                    str(args.exploratory_margin_budget_pct),
                    "--condor-margin-budget-pct",
                    str(args.condor_margin_budget_pct),
                    "--one-dte-margin-budget-pct",
                    str(args.one_dte_margin_budget_pct),
                    "--trend-debit-margin-budget-pct",
                    str(args.trend_debit_margin_budget_pct),
                    "--long-put-hedge-margin-budget-pct",
                    str(args.long_put_hedge_margin_budget_pct),
                ]
            )
        if args.trend_debit_sleeve:
            command.extend(
                [
                    "--trend-debit-sleeve",
                    "--trend-debit-size-fraction",
                    str(args.trend_debit_size_fraction),
                    "--trend-debit-min-abs-trend-score",
                    str(args.trend_debit_min_abs_trend_score),
                ]
            )
        if args.long_put_hedge_sleeve:
            command.extend(
                [
                    "--long-put-hedge-sleeve",
                    "--long-put-hedge-size-fraction",
                    str(args.long_put_hedge_size_fraction),
                    "--long-put-hedge-min-downtrend-score",
                    str(args.long_put_hedge_min_downtrend_score),
                    "--long-put-hedge-min-realized-z",
                    str(args.long_put_hedge_min_realized_z),
                ]
            )
        print("running", " ".join(command))
        subprocess.run(command, cwd=ROOT, check=True)
        summaries.append(summarize(results_dir, args.account_equity, target_pct, contracts))

    write_csv(Path(args.output_csv), summaries)
    write_markdown(Path(args.output_md), summaries)
    print(f"tiers={len(summaries)} csv={args.output_csv} md={args.output_md}")


if __name__ == "__main__":
    main()
