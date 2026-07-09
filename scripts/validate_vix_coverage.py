"""Validate VIX calendar coverage against processed SPXW sessions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from regime_validation import discover_dates  # noqa: E402
from vix_daily import DEFAULT_VIX_CSV, load_vix_daily, summarize_coverage  # noqa: E402


DEFAULT_PROCESSED = ROOT / "data" / "processed"


def signal_vix_fraction(trade_date: str, processed_dir: Path, symbol: str) -> float:
    import csv

    path = processed_dir / f"symbol={symbol}" / f"date={trade_date}" / "signals.csv"
    if not path.exists():
        return 0.0
    total = 0
    populated = 0
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            total += 1
            value = (row.get("vix") or "").strip()
            if value:
                populated += 1
    return populated / total if total else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate VIX calendar + signals.csv coverage.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED)
    parser.add_argument("--vix-csv", type=Path, default=DEFAULT_VIX_CSV)
    parser.add_argument("--sample-dates", type=int, default=5)
    args = parser.parse_args()

    processed_dates = discover_dates(args.processed_dir, args.symbol)
    vix_by_date = load_vix_daily(args.vix_csv)
    calendar_cov = summarize_coverage(vix_by_date, processed_dates)

    sample = processed_dates[-args.sample_dates :] if processed_dates else []
    signal_checks = {
        trade_date: round(signal_vix_fraction(trade_date, args.processed_dir, args.symbol), 4)
        for trade_date in sample
    }

    report = {
        "vix_calendar": {
            "path": str(args.vix_csv),
            "rows": len(vix_by_date),
            "first_date": min(vix_by_date) if vix_by_date else "",
            "last_date": max(vix_by_date) if vix_by_date else "",
        },
        "processed_coverage": calendar_cov,
        "signals_sample": signal_checks,
        "ok": calendar_cov.get("missing_count", 1) == 0 and all(v == 1.0 for v in signal_checks.values()),
    }
    print(json.dumps(report, indent=2))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
