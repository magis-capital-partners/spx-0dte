from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from holdings_from_trades import holdings_at
from mbh_simulator import StrategyConfig, read_quotes_csv, read_signals_csv, simulate_day, trades_to_rows


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "results_grid"


DDQ_2026_03_02_1500 = {
    "total_longs": 2199,
    "total_shorts": 1410,
    "net_contracts": 789,
    "long_calls": 379,
    "short_calls": 206,
    "long_puts": 1820,
    "short_puts": 1204,
}


def parse_list(value: str, cast):
    return [cast(item.strip()) for item in value.split(",") if item.strip()]


def summarize_holdings(holdings: Dict[tuple, int]) -> Dict[str, int]:
    long_calls = sum(value for (strike, side), value in holdings.items() if side == "CALL" and value > 0)
    short_calls = -sum(value for (strike, side), value in holdings.items() if side == "CALL" and value < 0)
    long_puts = sum(value for (strike, side), value in holdings.items() if side == "PUT" and value > 0)
    short_puts = -sum(value for (strike, side), value in holdings.items() if side == "PUT" and value < 0)
    return {
        "long_calls": long_calls,
        "short_calls": short_calls,
        "net_calls": long_calls - short_calls,
        "long_puts": long_puts,
        "short_puts": short_puts,
        "net_puts": long_puts - short_puts,
        "total_longs": long_calls + long_puts,
        "total_shorts": short_calls + short_puts,
        "net_contracts": long_calls + long_puts - short_calls - short_puts,
    }


def score(summary: Dict[str, int]) -> float:
    keys = ["total_longs", "total_shorts", "net_contracts", "long_calls", "short_calls", "long_puts", "short_puts"]
    total = 0.0
    for key in keys:
        target = DDQ_2026_03_02_1500[key]
        total += abs(summary[key] - target) / max(target, 1)
    return total / len(keys)


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score reconstruction parameter grids against the March 2 DDQ 3 PM snapshot.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--date", default="2026-03-02")
    parser.add_argument("--snapshot-time", default="15:00:00")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--signals-filename", default="signals.csv")
    parser.add_argument("--account-equity", type=float, default=28_000_000)
    parser.add_argument("--baseline-contracts", default="16,33,66,85,110")
    parser.add_argument("--daily-credit-cap-pcts", default="0.015,0.02")
    parser.add_argument("--stop-multiples", default="2.0,2.5,3.0")
    parser.add_argument("--target-long-deltas", default="0.03,0.05,0.08")
    parser.add_argument("--skew-extreme-thresholds", default="1.0")
    parser.add_argument("--term-extreme-thresholds", default="1.0")
    args = parser.parse_args()

    day_dir = Path(args.processed_dir) / f"symbol={args.symbol}" / f"date={args.date}"
    quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
    signals = read_signals_csv(day_dir / args.signals_filename)
    snapshot_dt = datetime.fromisoformat(f"{args.date}T{args.snapshot_time}")

    rows: List[dict] = []
    for baseline in parse_list(args.baseline_contracts, int):
        for cap in parse_list(args.daily_credit_cap_pcts, float):
            for stop_multiple in parse_list(args.stop_multiples, float):
                for long_delta in parse_list(args.target_long_deltas, float):
                    for skew_threshold in parse_list(args.skew_extreme_thresholds, float):
                        for term_threshold in parse_list(args.term_extreme_thresholds, float):
                            config = StrategyConfig(
                                account_equity=args.account_equity,
                                baseline_contracts=baseline,
                                daily_credit_cap_pct=cap,
                                stop_multiple=stop_multiple,
                                target_long_abs_delta=long_delta,
                                skew_extreme_threshold=skew_threshold,
                                term_extreme_threshold=term_threshold,
                            )
                            result = simulate_day(quotes, signals, config=config)
                            trade_rows = trades_to_rows(result.trades)
                            for row in trade_rows:
                                row["date"] = args.date
                            holdings = holdings_at(trade_rows, args.date, snapshot_dt)
                            holding_summary = summarize_holdings(holdings)
                            rows.append(
                                {
                                    "baseline_contracts": baseline,
                                    "daily_credit_cap_pct": cap,
                                    "stop_multiple": stop_multiple,
                                    "target_long_abs_delta": long_delta,
                                    "skew_extreme_threshold": skew_threshold,
                                    "term_extreme_threshold": term_threshold,
                                    "trades": len(result.trades),
                                    "net_pnl": round(result.net_pnl, 2),
                                    "return_on_equity": round(result.return_on_equity, 8),
                                    "snapshot_score": round(score(holding_summary), 6),
                                    **holding_summary,
                                }
                            )

    rows.sort(key=lambda row: row["snapshot_score"])
    out = Path(args.results_dir) / f"grid_{args.date}_{args.snapshot_time.replace(':', '')}.csv"
    write_csv(out, rows)
    print(f"wrote {out}")
    print("top rows:")
    for row in rows[:5]:
        print(row)


if __name__ == "__main__":
    main()
