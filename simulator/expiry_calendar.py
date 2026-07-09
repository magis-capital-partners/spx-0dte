"""SPXW expiration-era calendar for historical 0DTE backtests.

Tradability requires same-day processed chain data (0DTE) plus weekday rules
that mirror when Mon/Wed/Fri and later Tue/Thu expirations existed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "data" / "calendar" / "spxw_era_rules.json"

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass(frozen=True)
class EraRule:
    name: str
    end: Optional[str]
    weekdays: Set[int]


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_era_rules(path: Path = DEFAULT_RULES) -> tuple[str, List[EraRule]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    floor = payload["start_date"]
    eras = [
        EraRule(
            name=item["name"],
            end=item.get("end"),
            weekdays=set(item["weekdays"]),
        )
        for item in payload["eras"]
    ]
    return floor, eras


def era_for_date(day: date, eras: List[EraRule]) -> str:
    for rule in eras:
        if rule.end is None or day <= parse_date(rule.end):
            return rule.name
    return eras[-1].name


def allowed_weekdays(day: date, eras: List[EraRule]) -> Set[int]:
    name = era_for_date(day, eras)
    for rule in eras:
        if rule.name == name:
            return rule.weekdays
    return eras[-1].weekdays


def resolve_start_date(
    processed_dates: Iterable[str],
    floor: str,
    *,
    require_mon_and_wed: bool = True,
) -> str:
    """First date on/after floor where era rules and optional Mon+Wed coverage allow trading."""
    floor_d = parse_date(floor)
    ordered = sorted(d for d in processed_dates if parse_date(d) >= floor_d)
    if not ordered:
        return floor

    if not require_mon_and_wed:
        return ordered[0]

    seen_mon = False
    seen_wed = False
    for day_str in ordered:
        day = parse_date(day_str)
        if day.weekday() == 0:
            seen_mon = True
        if day.weekday() == 2:
            seen_wed = True
        if seen_mon and seen_wed:
            return day_str
    return ordered[0]


def calendar_row(
    day_str: str,
    *,
    processed: bool,
    floor: str,
    end: str,
    eras: List[EraRule],
) -> dict:
    day = parse_date(day_str)
    weekday = day.weekday()
    era = era_for_date(day, eras)
    allowed = allowed_weekdays(day, eras)

    if not processed:
        eligible = False
        skip_reason = "no_processed_data"
    elif day < parse_date(floor):
        eligible = False
        skip_reason = "before_start"
    elif day > parse_date(end):
        eligible = False
        skip_reason = "after_end"
    elif weekday not in allowed:
        eligible = False
        skip_reason = "weekday_not_in_era"
    else:
        eligible = True
        skip_reason = ""

    return {
        "date": day_str,
        "weekday": WEEKDAY_NAMES[weekday],
        "era": era,
        "processed": processed,
        "eligible": eligible,
        "skip_reason": skip_reason,
    }


def build_calendar_audit(
    processed_dates: Iterable[str],
    *,
    floor: str,
    end: str,
    eras: List[EraRule],
) -> List[dict]:
    processed_set = set(processed_dates)
    lo = parse_date(floor)
    hi = parse_date(end)
    all_days = sorted(processed_set | {d for d in processed_set if lo <= parse_date(d) <= hi})
    # Include every processed date plus any in-range processed; also emit skipped weekdays
    # for processed dates only (we only know same-day expiry when processed exists).
    rows = []
    for day_str in sorted(processed_set):
        day = parse_date(day_str)
        if day < lo or day > hi:
            continue
        rows.append(
            calendar_row(
                day_str,
                processed=True,
                floor=floor,
                end=end,
                eras=eras,
            )
        )
    return rows


def discover_eligible_dates(
    processed_dates: Iterable[str],
    *,
    floor: str,
    end: str,
    eras: List[EraRule],
) -> List[str]:
    audit = build_calendar_audit(processed_dates, floor=floor, end=end, eras=eras)
    return [row["date"] for row in audit if row["eligible"]]


def is_live_tradable_day(
    day: date,
    *,
    floor: str,
    end: str,
    eras: List[EraRule],
) -> tuple[bool, str]:
    """Weekday/era eligibility for live sessions (no processed-data requirement)."""
    if day < parse_date(floor):
        return False, "before_start"
    if day > parse_date(end):
        return False, "after_end"
    if day.weekday() not in allowed_weekdays(day, eras):
        return False, "weekday_not_in_era"
    return True, ""


def summarize_eras(daily_rows: Iterable[dict], account_equity: float) -> List[dict]:
    from portfolio_metrics import portfolio_stats

    by_era: Dict[str, List[dict]] = {}
    for row in daily_rows:
        if str(row.get("eligible", "true")).lower() not in {"true", "1"}:
            continue
        by_era.setdefault(str(row.get("era", "unknown")), []).append(row)

    summaries = []
    for era_name in sorted(by_era):
        rows = by_era[era_name]
        stats = portfolio_stats(rows, account_equity, metrics_mode="eligible_only")
        stats["era"] = era_name
        summaries.append(stats)
    return summaries
