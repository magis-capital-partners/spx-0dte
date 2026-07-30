"""Exit successfully only when US equity and SPX options markets are open today."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from backfill_history import US_HOLIDAYS  # noqa: E402


def main() -> int:
    today = date.today()
    is_open = today.weekday() < 5 and today.isoformat() not in US_HOLIDAYS
    print(f"{today.isoformat()}: {'SPX market open' if is_open else 'SPX market closed'}")
    return 0 if is_open else 1


if __name__ == "__main__":
    raise SystemExit(main())
