"""FOMC decision-day calendar for session entry cutoffs."""
from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Set

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOMC_CSV = ROOT / "data" / "calendar" / "fomc_days.csv"


@lru_cache(maxsize=4)
def load_fomc_dates(path: str = "") -> frozenset[str]:
    csv_path = Path(path) if path else DEFAULT_FOMC_CSV
    if not csv_path.is_file():
        return frozenset()
    dates: Set[str] = set()
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            d = (row.get("date") or "").strip()[:10]
            if d:
                dates.add(d)
    return frozenset(dates)


def is_fomc_day(trade_date: str, path: str = "") -> bool:
    return trade_date[:10] in load_fomc_dates(path)
