from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List


@dataclass(frozen=True)
class PositionRow:
    source: str
    timestamp: str
    expiry: str
    strike: float
    call_contracts: int
    put_contracts: int


def read_position_snapshot_csv(path: str | Path) -> List[PositionRow]:
    rows: List[PositionRow] = []
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            rows.append(
                PositionRow(
                    source=row["source"],
                    timestamp=row["timestamp"],
                    expiry=row["expiry"],
                    strike=float(row["strike"]),
                    call_contracts=int(row["call_contracts"]),
                    put_contracts=int(row["put_contracts"]),
                )
            )
    return rows


def summarize_positions(rows: Iterable[PositionRow]) -> Dict[str, Dict[str, int | float | None]]:
    grouped: Dict[str, List[PositionRow]] = defaultdict(list)
    for row in rows:
        grouped[row.source].append(row)

    summaries: Dict[str, Dict[str, int | float | None]] = {}
    for source, source_rows in grouped.items():
        long_calls = sum(row.call_contracts for row in source_rows if row.call_contracts > 0)
        short_calls = -sum(row.call_contracts for row in source_rows if row.call_contracts < 0)
        long_puts = sum(row.put_contracts for row in source_rows if row.put_contracts > 0)
        short_puts = -sum(row.put_contracts for row in source_rows if row.put_contracts < 0)
        call_strikes = [row.strike for row in source_rows if row.call_contracts != 0]
        put_strikes = [row.strike for row in source_rows if row.put_contracts != 0]
        summaries[source] = {
            "rows": len(source_rows),
            "long_calls": long_calls,
            "short_calls": short_calls,
            "net_calls": long_calls - short_calls,
            "long_puts": long_puts,
            "short_puts": short_puts,
            "net_puts": long_puts - short_puts,
            "total_longs": long_calls + long_puts,
            "total_shorts": short_calls + short_puts,
            "net_contracts": long_calls + long_puts - short_calls - short_puts,
            "min_call_strike": min(call_strikes) if call_strikes else None,
            "max_call_strike": max(call_strikes) if call_strikes else None,
            "min_put_strike": min(put_strikes) if put_strikes else None,
            "max_put_strike": max(put_strikes) if put_strikes else None,
        }
    return summaries


def main() -> None:
    default_path = Path(__file__).resolve().parents[1] / "position_snapshots.csv"
    summaries = summarize_positions(read_position_snapshot_csv(default_path))
    for source, summary in summaries.items():
        print(source)
        for key, value in summary.items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
