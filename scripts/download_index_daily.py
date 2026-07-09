"""Download daily index OHLC (^GSPC, ^IXIC, ^RUT) into data/calendar/."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from index_daily import CALENDAR_DIR, DEFAULT_SYMBOLS, download_all  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download free daily index OHLC from Yahoo Finance (^GSPC, ^IXIC, ^RUT)."
    )
    parser.add_argument("--start-date", default="2019-01-01", help="Inclusive start (YYYY-MM-DD).")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="Inclusive end (YYYY-MM-DD).")
    parser.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated Yahoo symbols (default: ^GSPC,^IXIC,^RUT).",
    )
    parser.add_argument("--calendar-dir", type=Path, default=CALENDAR_DIR)
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    results = download_all(
        symbols,
        start_date=args.start_date,
        end_date=args.end_date,
        calendar_dir=args.calendar_dir,
    )
    print(json.dumps({"source": "yahoo_finance", "indices": results}, indent=2))


if __name__ == "__main__":
    main()
