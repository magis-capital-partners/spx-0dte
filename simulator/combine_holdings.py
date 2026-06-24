from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]


def read_csv(path: Path) -> List[dict]:
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


def key(date: str, timestamp: str, strike: float) -> Tuple[str, str, float]:
    return date, timestamp, float(strike)


def combine(base_rows: List[dict], overlay_rows: List[dict]) -> List[dict]:
    combined: Dict[Tuple[str, str, float], dict] = {}
    snapshots = sorted({(row["date"], row["timestamp"]) for row in base_rows})

    for row in base_rows:
        k = key(row["date"], row["timestamp"], float(row["strike"]))
        combined[k] = {
            "date": row["date"],
            "timestamp": row["timestamp"],
            "strike": float(row["strike"]),
            "call_contracts": int(float(row["call_contracts"])),
            "put_contracts": int(float(row["put_contracts"])),
        }

    for overlay in overlay_rows:
        entry = datetime.fromisoformat(overlay["entry_time"])
        trade_date = overlay["date"]
        for date, timestamp in snapshots:
            if date != trade_date:
                continue
            snapshot_dt = datetime.fromisoformat(timestamp)
            if entry > snapshot_dt:
                continue
            call_contracts = int(float(overlay.get("call_contracts") or 0))
            put_contracts = int(float(overlay.get("put_contracts") or 0))
            if call_contracts:
                strike = float(overlay["call_strike"])
                k = key(date, timestamp, strike)
                combined.setdefault(k, {"date": date, "timestamp": timestamp, "strike": strike, "call_contracts": 0, "put_contracts": 0})
                combined[k]["call_contracts"] += call_contracts
            if put_contracts:
                strike = float(overlay["put_strike"])
                k = key(date, timestamp, strike)
                combined.setdefault(k, {"date": date, "timestamp": timestamp, "strike": strike, "call_contracts": 0, "put_contracts": 0})
                combined[k]["put_contracts"] += put_contracts

    return sorted(combined.values(), key=lambda row: (row["date"], row["timestamp"], row["strike"]))


def summarize(rows: List[dict]) -> List[dict]:
    grouped: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["date"], row["timestamp"])].append(row)
    summaries = []
    for (date, timestamp), items in grouped.items():
        long_calls = sum(int(row["call_contracts"]) for row in items if int(row["call_contracts"]) > 0)
        short_calls = -sum(int(row["call_contracts"]) for row in items if int(row["call_contracts"]) < 0)
        long_puts = sum(int(row["put_contracts"]) for row in items if int(row["put_contracts"]) > 0)
        short_puts = -sum(int(row["put_contracts"]) for row in items if int(row["put_contracts"]) < 0)
        summaries.append(
            {
                "date": date,
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
    parser = argparse.ArgumentParser(description="Combine short-premium holdings with long-vol overlay holdings.")
    parser.add_argument("--base-holdings", required=True)
    parser.add_argument("--overlay-trades", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary-output", required=True)
    args = parser.parse_args()

    rows = combine(read_csv(Path(args.base_holdings)), read_csv(Path(args.overlay_trades)))
    write_csv(Path(args.output), rows)
    write_csv(Path(args.summary_output), summarize(rows))
    print(f"wrote {args.output}")
    print(f"wrote {args.summary_output}")


if __name__ == "__main__":
    main()
