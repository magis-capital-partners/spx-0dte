from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS = ROOT / "data" / "results"


def read_csv(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def parse_leg(label: str) -> Tuple[str, float]:
    option_type = "CALL" if label.startswith("C") else "PUT"
    return option_type, float(label[1:])


def is_short_active(trade: dict, snapshot_time: datetime) -> bool:
    stopped_value = trade["stopped"]
    stopped = stopped_value if isinstance(stopped_value, bool) else str(stopped_value).lower() == "true"
    if not stopped:
        return True
    stop_time = trade.get("stop_time") or ""
    if not stop_time:
        return True
    return parse_dt(stop_time) > snapshot_time


def holdings_at(trades: Iterable[dict], trade_date: str, snapshot_time: datetime) -> Dict[Tuple[float, str], int]:
    holdings: Dict[Tuple[float, str], int] = defaultdict(int)
    for trade in trades:
        if trade["date"] != trade_date:
            continue
        if parse_dt(trade["entry_time"]) > snapshot_time:
            continue

        contracts = int(trade["contracts"])
        long_type, long_strike = parse_leg(trade["long"])
        holdings[(long_strike, long_type)] += contracts

        if is_short_active(trade, snapshot_time):
            short_type, short_strike = parse_leg(trade["short"])
            holdings[(short_strike, short_type)] -= contracts
    return holdings


def rows_for_snapshot(trades: List[dict], trade_date: str, time_of_day: str) -> List[dict]:
    snapshot_time = parse_dt(f"{trade_date}T{time_of_day}")
    holdings = holdings_at(trades, trade_date, snapshot_time)
    strikes = sorted(set(strike for strike, _ in holdings))
    rows = []
    for strike in strikes:
        rows.append(
            {
                "date": trade_date,
                "timestamp": snapshot_time.isoformat(),
                "strike": strike,
                "call_contracts": holdings.get((strike, "CALL"), 0),
                "put_contracts": holdings.get((strike, "PUT"), 0),
            }
        )
    return rows


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: List[dict]) -> List[dict]:
    grouped: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["date"], row["timestamp"])].append(row)
    summaries = []
    for (trade_date, timestamp), snapshot_rows in grouped.items():
        long_calls = sum(int(row["call_contracts"]) for row in snapshot_rows if int(row["call_contracts"]) > 0)
        short_calls = -sum(int(row["call_contracts"]) for row in snapshot_rows if int(row["call_contracts"]) < 0)
        long_puts = sum(int(row["put_contracts"]) for row in snapshot_rows if int(row["put_contracts"]) > 0)
        short_puts = -sum(int(row["put_contracts"]) for row in snapshot_rows if int(row["put_contracts"]) < 0)
        summaries.append(
            {
                "date": trade_date,
                "timestamp": timestamp,
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
        )
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconstruct strike-level holdings from simulated trades at specific times.")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--times", nargs="+", default=["11:00:00", "15:00:00"])
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    trades = read_csv(results_dir / "trades.csv")
    rows: List[dict] = []
    for trade_date in args.dates:
        for time_of_day in args.times:
            rows.extend(rows_for_snapshot(trades, trade_date, time_of_day))

    holdings_path = results_dir / "simulated_holdings_snapshots.csv"
    summary_path = results_dir / "simulated_holdings_summary.csv"
    write_csv(holdings_path, rows)
    write_csv(summary_path, summarize(rows))
    print(f"wrote {holdings_path}")
    print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
