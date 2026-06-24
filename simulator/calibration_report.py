from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "data" / "results"


def read_csv(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def option_side(label: str) -> str:
    return "CALL" if label.startswith("C") else "PUT"


def summarize_trades(trades: List[dict]) -> Dict[str, dict]:
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for row in trades:
        grouped[row["date"]].append(row)

    summaries: Dict[str, dict] = {}
    for trade_date, rows in grouped.items():
        stopped = [row for row in rows if row["stopped"].lower() == "true"]
        retained_long_wings = sum(int(row["contracts"]) for row in stopped)
        call_spreads = sum(1 for row in rows if option_side(row["short"]) == "CALL")
        put_spreads = sum(1 for row in rows if option_side(row["short"]) == "PUT")
        summaries[trade_date] = {
            "trades": len(rows),
            "put_spreads": put_spreads,
            "call_spreads": call_spreads,
            "stopped_trades": len(stopped),
            "stop_rate": len(stopped) / len(rows) if rows else 0.0,
            "opened_longs": sum(int(row["contracts"]) for row in rows),
            "opened_shorts": sum(int(row["contracts"]) for row in rows),
            "retained_long_wings_from_stops": retained_long_wings,
        }
    return summaries


def build_report(results_dir: Path) -> str:
    daily = read_csv(results_dir / "daily_summary.csv")
    trades = read_csv(results_dir / "trades.csv")
    trade_summaries = summarize_trades(trades)

    lines = [
        "# Reconstruction Calibration Report",
        "",
        "This report summarizes the current placeholder-policy backtest. It is a calibration report, not evidence that the edge has been recreated.",
        "",
        "## Daily Results",
        "",
        "| Date | Trades | Gross Credit | Net PnL | Return | Halted |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in daily:
        lines.append(
            f"| {row['date']} | {row['trades']} | {float(row['gross_credit_sold']):,.0f} | "
            f"{float(row['net_pnl']):,.0f} | {float(row['return_on_equity']):.4%} | {row['halted']} |"
        )

    lines.extend(
        [
            "",
            "## Trade Shape",
            "",
            "| Date | Put Spreads | Call Spreads | Stopped | Stop Rate | Retained Long Wings From Stops |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for trade_date, summary in sorted(trade_summaries.items()):
        lines.append(
            f"| {trade_date} | {summary['put_spreads']} | {summary['call_spreads']} | {summary['stopped_trades']} | "
            f"{summary['stop_rate']:.1%} | {summary['retained_long_wings_from_stops']} |"
        )

    lines.extend(
        [
            "",
            "## Calibration Notes",
            "",
            "- The current policy is deliberately simple. It validates plumbing and mechanics, not the proprietary edge.",
            "- The strategy now needs parameter search and walk-forward testing over a larger date set.",
            "- The March 2, 2026 DDQ snapshot had 1,410 short contracts and 2,199 long contracts around 3 PM. The current simulation should be compared against intraday simulated holdings, not just end-of-day trades.",
            "- Next implementation step: add an intraday holdings recorder and a snapshot-matching scorer.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a calibration report from reconstruction backtest outputs.")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    path = results_dir / "calibration_report.md"
    path.write_text(build_report(results_dir), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
