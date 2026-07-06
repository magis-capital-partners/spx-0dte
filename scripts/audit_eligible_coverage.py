"""Audit eligible SPXW calendar days vs local raw/processed cache."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from backfill_history import trading_days  # noqa: E402
from expiry_calendar import DEFAULT_RULES, build_calendar_audit, load_era_rules, parse_date  # noqa: E402

PROC = ROOT / "data/processed/symbol=SPXW"
RAW = ROOT / "data/raw/thetadata/symbol=SPXW"
OUT = ROOT / "data/inventory/eligible_coverage_audit.json"

FLOOR = "2019-01-02"
END = "2026-07-02"


def list_dates(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {
        path.name.split("=", 1)[1]
        for path in root.iterdir()
        if path.is_dir() and path.name.startswith("date=")
    }


def processed_complete(day: str) -> bool:
    day_dir = PROC / f"date={day}"
    return (day_dir / "signals.csv").exists() and (day_dir / "normalized_option_quotes.csv").exists()


def main() -> None:
    _, eras = load_era_rules(DEFAULT_RULES)
    all_trading = trading_days(parse_date(FLOOR), parse_date(END))
    full_audit = build_calendar_audit(all_trading, floor=FLOOR, end=END, eras=eras)
    expected = [row for row in full_audit if row["eligible"]]

    raw = list_dates(RAW)
    processed = {day for day in list_dates(PROC) if processed_complete(day)}

    gaps: list[dict] = []
    for row in expected:
        day = row["date"]
        if day in processed:
            continue
        if day in raw:
            action = "build"
        else:
            action = "download_then_build"
        gaps.append({"date": day, "era": row["era"], "weekday": row["weekday"], "action": action})

    by_year_expected = Counter(r["date"][:4] for r in expected)
    by_year_have = Counter(r["date"][:4] for r in expected if r["date"] in processed)
    by_year_gap = Counter(g["date"][:4] for g in gaps)

    report = {
        "floor": FLOOR,
        "end": END,
        "expected_eligible_days": len(expected),
        "processed_eligible_days": len(expected) - len(gaps),
        "missing_eligible_days": len(gaps),
        "need_download": sum(1 for g in gaps if g["action"] == "download_then_build"),
        "need_build_only": sum(1 for g in gaps if g["action"] == "build"),
        "by_year": {
            year: {
                "expected": by_year_expected[year],
                "have": by_year_have[year],
                "gap": by_year_gap[year],
            }
            for year in sorted(by_year_expected)
        },
        "gaps": gaps,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"Expected eligible days: {report['expected_eligible_days']}")
    print(f"Have processed:         {report['processed_eligible_days']}")
    print(f"Missing:                {report['missing_eligible_days']}")
    print(f"  download+build:       {report['need_download']}")
    print(f"  build only:           {report['need_build_only']}")
    print("\nBy year (expected / have / gap):")
    for year, row in report["by_year"].items():
        print(f"  {year}: {row['expected']} / {row['have']} / {row['gap']}")
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
