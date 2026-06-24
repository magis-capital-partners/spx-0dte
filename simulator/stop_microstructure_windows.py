from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import List


ROOT = Path(__file__).resolve().parents[1]


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def compact_right(short_symbol: str) -> str:
    value = short_symbol.strip().upper()
    if value.startswith("P"):
        return "PUT"
    if value.startswith("C"):
        return "CALL"
    return ""


def strike_from_short(short_symbol: str) -> str:
    return short_symbol.strip()[1:]


def read_rows(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def build_windows(rows: List[dict], pre_minutes: int, post_minutes: int) -> List[dict]:
    windows: List[dict] = []
    for row in rows:
        entry_time = parse_timestamp(row["entry_time"])
        stop_time = parse_timestamp(row["stop_time"])
        start_time = entry_time - timedelta(minutes=pre_minutes)
        end_time = stop_time + timedelta(minutes=post_minutes)
        windows.append(
            {
                "date": row.get("date", entry_time.date().isoformat()),
                "trade_id": row.get("trade_id", ""),
                "side": row.get("side", ""),
                "model": row.get("model", ""),
                "expiry": row.get("entry_time", "")[:10],
                "short_type": compact_right(row.get("short", "")),
                "short_strike": strike_from_short(row.get("short", "")),
                "long": row.get("long", ""),
                "entry_time": entry_time.isoformat(),
                "stop_time": stop_time.isoformat(),
                "window_start_time": start_time.time().isoformat(timespec="seconds"),
                "window_end_time": end_time.time().isoformat(timespec="seconds"),
                "minutes_to_stop": row.get("minutes_to_stop", ""),
                "entry_credit": row.get("entry_credit", ""),
                "stop_price": row.get("stop_price", ""),
                "stop_fill": row.get("stop_fill", ""),
                "entry_spot": row.get("entry_spot", ""),
                "stop_spot": row.get("stop_spot", ""),
                "candidate_score": row.get("candidate_score", ""),
                "net_pnl": row.get("net_pnl", ""),
                "suggested_download_interval": "10s",
            }
        )
    windows.sort(key=lambda item: (item["date"], item["entry_time"], item["trade_id"]))
    return windows


def main() -> None:
    parser = argparse.ArgumentParser(description="Create targeted microstructure download windows for stopped trades.")
    parser.add_argument("--stops", default=str(ROOT / "data" / "validation_13m_event_two_tier" / "stop_diagnostics.csv"))
    parser.add_argument("--output", default=str(ROOT / "data" / "validation_13m_event_two_tier" / "microstructure_windows.csv"))
    parser.add_argument("--pre-minutes", type=int, default=5)
    parser.add_argument("--post-minutes", type=int, default=5)
    args = parser.parse_args()

    rows = read_rows(Path(args.stops))
    windows = build_windows(rows, args.pre_minutes, args.post_minutes)
    write_rows(Path(args.output), windows)
    print(f"stopped_trades={len(rows)} windows={len(windows)} output={args.output}")


if __name__ == "__main__":
    main()
