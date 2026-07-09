"""Populate the ``vix`` column in processed signals.csv from daily VIX calendar."""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Dict, List, Optional

from vix_daily import DEFAULT_VIX_CSV, VixDay, load_vix_daily


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"


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


def discover_dates(processed_dir: Path, symbol: str) -> List[str]:
    root = processed_dir / f"symbol={symbol}"
    if not root.exists():
        return []
    dates = []
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("date=") and (path / "signals.csv").exists():
            dates.append(path.name.split("=", 1)[1])
    return sorted(dates)


def enrich_signals_for_day(rows: List[dict], vix_day: Optional[VixDay]) -> List[dict]:
    if vix_day is None:
        return rows
    decision = f"{vix_day.decision_vix:.4f}"
    prior = "" if vix_day.prior_close is None else f"{vix_day.prior_close:.4f}"
    close = f"{vix_day.close:.4f}"
    enriched: List[dict] = []
    for row in rows:
        out = dict(row)
        out["vix"] = decision
        out["vix_open"] = decision
        out["vix_close"] = close
        out["vix_prior_close"] = prior
        enriched.append(out)
    return enriched


def enrich_symbol(
    processed_dir: Path,
    symbol: str,
    vix_by_date: Dict[str, VixDay],
    dates: Optional[List[str]] = None,
) -> dict:
    dates = dates or discover_dates(processed_dir, symbol)
    updated = 0
    missing_dates: List[str] = []
    for trade_date in dates:
        vix_day = vix_by_date.get(trade_date)
        if vix_day is None:
            missing_dates.append(trade_date)
            continue
        signals_path = processed_dir / f"symbol={symbol}" / f"date={trade_date}" / "signals.csv"
        rows = read_csv(signals_path)
        if not rows:
            continue
        write_csv(signals_path, enrich_signals_for_day(rows, vix_day))
        updated += 1
    return {
        "symbol": symbol,
        "updated_days": updated,
        "missing_vix_days": len(missing_dates),
        "missing_vix_sample": missing_dates[:20],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich processed signals.csv with daily VIX open.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED)
    parser.add_argument("--vix-csv", type=Path, default=DEFAULT_VIX_CSV)
    parser.add_argument("--dates", nargs="*", help="Optional explicit dates; default all processed dates.")
    args = parser.parse_args()

    vix_by_date = load_vix_daily(args.vix_csv)
    if not vix_by_date:
        raise SystemExit(f"No VIX calendar at {args.vix_csv}; run scripts/download_vix_daily.py first.")

    dates = args.dates if args.dates else None
    summary = enrich_symbol(Path(args.processed_dir), args.symbol, vix_by_date, dates)
    print(summary)


if __name__ == "__main__":
    main()
