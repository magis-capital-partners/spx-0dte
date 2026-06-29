"""Add cross-day and intraday context features to processed signals.csv."""
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"

EXTRA_FEATURES = [
    "minutes_to_close_norm",
    "overnight_gap_z",
    "prior_day_return_z",
    "abs_skew_z",
    "abs_term_ratio_z",
]


def read_csv(path: Path) -> List[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: List[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def discover_dates(processed_dir: Path, symbol: str) -> List[str]:
    root = processed_dir / f"symbol={symbol}"
    if not root.exists():
        return []
    dates = []
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("date=") and (path / "signals.csv").exists():
            dates.append(path.name.split("=", 1)[1])
    return sorted(dates)


def day_close_spot(rows: List[dict]) -> Optional[float]:
    if not rows:
        return None
    return safe_float(rows[-1].get("underlying_price"), default=float("nan"))


def day_open_spot(rows: List[dict]) -> Optional[float]:
    if not rows:
        return None
    return safe_float(rows[0].get("underlying_price"), default=float("nan"))


def day_range_pct(rows: List[dict]) -> float:
    spots = [safe_float(row.get("underlying_price")) for row in rows if row.get("underlying_price") not in {"", None}]
    if len(spots) < 2:
        return 0.0
    low = min(spots)
    high = max(spots)
    mid = (high + low) / 2.0
    return (high - low) / mid if mid else 0.0


def minutes_to_close_norm(timestamp: str) -> float:
    clock = timestamp.split("T", 1)[1][:8]
    hour, minute, second = (int(part) for part in clock.split(":"))
    total_minutes = hour * 60 + minute + second / 60.0
    open_minutes = 9 * 60 + 30
    close_minutes = 16 * 60
    span = max(close_minutes - open_minutes, 1.0)
    remaining = max(close_minutes - total_minutes, 0.0)
    return remaining / span


def enrich_day(rows: List[dict], prior_close: Optional[float], prior_range_pct: float) -> List[dict]:
    open_spot = day_open_spot(rows)
    enriched: List[dict] = []
    for row in rows:
        out = dict(row)
        skew = safe_float(row.get("skew_z"))
        term = safe_float(row.get("term_ratio_z"))
        out["minutes_to_close_norm"] = round(minutes_to_close_norm(str(row["timestamp"])), 6)
        out["abs_skew_z"] = round(abs(skew), 6)
        out["abs_term_ratio_z"] = round(abs(term), 6)
        if prior_close and open_spot and prior_close > 0:
            gap = (open_spot - prior_close) / prior_close
            out["overnight_gap_z"] = round(gap / max(prior_range_pct, 0.002), 6)
        else:
            out["overnight_gap_z"] = 0.0
        if prior_close and open_spot and prior_close > 0:
            out["prior_day_return_z"] = round((open_spot - prior_close) / prior_close, 6)
        else:
            out["prior_day_return_z"] = 0.0
        enriched.append(out)
    return enriched


def enrich_symbol(processed_dir: Path, symbol: str, dates: Optional[List[str]] = None) -> int:
    dates = dates or discover_dates(processed_dir, symbol)
    prior_close: Optional[float] = None
    prior_range_pct = 0.01
    updated = 0
    for trade_date in dates:
        day_dir = processed_dir / f"symbol={symbol}" / f"date={trade_date}"
        signals_path = day_dir / "signals.csv"
        rows = read_csv(signals_path)
        if not rows:
            continue
        enriched = enrich_day(rows, prior_close, prior_range_pct)
        write_csv(signals_path, enriched)
        prior_close = day_close_spot(rows)
        prior_range_pct = max(day_range_pct(rows), 0.002)
        updated += 1
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich processed signals with cross-day context features.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED)
    parser.add_argument("--dates", nargs="*", help="Optional explicit dates; default all processed dates.")
    args = parser.parse_args()
    dates = args.dates if args.dates else None
    count = enrich_symbol(Path(args.processed_dir), args.symbol, dates)
    print(f"enriched {count} day(s)")


if __name__ == "__main__":
    main()
