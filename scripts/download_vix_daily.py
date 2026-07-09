"""Download daily ^VIX from Yahoo Finance into data/calendar/vix_daily.csv."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from vix_daily import DEFAULT_VIX_CSV, download_and_save  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Download free daily VIX (^VIX) from Yahoo Finance.")
    parser.add_argument("--start-date", default="2019-01-01", help="Inclusive start (YYYY-MM-DD).")
    parser.add_argument("--end-date", default=date.today().isoformat(), help="Inclusive end (YYYY-MM-DD).")
    parser.add_argument("--output", type=Path, default=DEFAULT_VIX_CSV)
    args = parser.parse_args()

    count, first, last = download_and_save(
        start_date=args.start_date,
        end_date=args.end_date,
        path=args.output,
    )
    print(
        json.dumps(
            {
                "path": str(args.output),
                "rows": count,
                "first_date": first,
                "last_date": last,
                "source": "yahoo_finance_^VIX",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
