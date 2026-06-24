from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Set


ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
DEFAULT_PROCESSED = ROOT / "data" / "processed"


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def observed_fixed(month: int, day: int, year: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def easter(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def market_holidays(year: int) -> Set[date]:
    return {
        observed_fixed(1, 1, year),
        nth_weekday(year, 1, 0, 3),
        nth_weekday(year, 2, 0, 3),
        easter(year) - timedelta(days=2),
        last_weekday(year, 5, 0),
        observed_fixed(6, 19, year),
        observed_fixed(7, 4, year),
        nth_weekday(year, 9, 0, 1),
        nth_weekday(year, 11, 3, 4),
        observed_fixed(12, 25, year),
    }


def market_dates(start: date, end: date) -> List[str]:
    holidays: Set[date] = set()
    for year in range(start.year, end.year + 1):
        holidays.update(market_holidays(year))
    dates: List[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def existing_processed_dates(symbol: str, dates: Iterable[str]) -> List[str]:
    processed_root = DEFAULT_PROCESSED / f"symbol={symbol}"
    existing = []
    for trade_date in dates:
        day_dir = processed_root / f"date={trade_date}"
        if (day_dir / "signals.csv").exists() and (day_dir / "normalized_option_quotes.csv").exists():
            existing.append(trade_date)
    return existing


def run_command(args: List[str]) -> None:
    print("running", " ".join(args))
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download, process, and validate continuous SPXW research windows.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--start-date", default="2025-04-01")
    parser.add_argument("--end-date", default="2025-09-30")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--strike-range", type=int, default=80)
    parser.add_argument("--download", action="store_true", help="Download missing raw ThetaData files before processing.")
    parser.add_argument("--build", action="store_true", help="Build processed features for the requested date range.")
    parser.add_argument("--validate", action="store_true", help="Run the current $13M event-aware two-tier validation.")
    parser.add_argument("--results-dir", default=str(ROOT / "data" / "validation_13m_continuous_q2_q3"))
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--disable-time-of-day-controls", action="store_true")
    parser.add_argument("--exploratory-min-score", type=float, default=2.40)
    parser.add_argument("--exploratory-max-score", type=float, default=2.49)
    args = parser.parse_args()

    dates = market_dates(parse_date(args.start_date), parse_date(args.end_date))
    print(f"market_dates={len(dates)} first={dates[:3]} last={dates[-3:]}")

    if args.download:
        if not os.environ.get("THETADATA_API_KEY"):
            raise SystemExit("THETADATA_API_KEY is not set; cannot download continuous ThetaData.")
        run_command(
            [
                sys.executable,
                str(SIM / "thetadata_downloader.py"),
                "--symbol",
                args.symbol,
                "--interval",
                args.interval,
                "--strike-range",
                str(args.strike_range),
                "--dates",
                *dates,
            ]
        )

    if args.build:
        run_command([sys.executable, str(SIM / "feature_builder.py"), "--symbol", args.symbol, "--dates", *dates])

    available_dates = existing_processed_dates(args.symbol, dates)
    print(f"available_processed_dates_in_window={len(available_dates)}")

    if args.validate:
        if len(available_dates) <= args.train_count:
            raise SystemExit(
                f"Need more than train-count processed dates for validation. "
                f"Have {len(available_dates)}, train-count={args.train_count}."
            )
        command = [
            sys.executable,
            str(SIM / "regime_validation.py"),
            "--symbol",
            args.symbol,
            "--dates",
            *available_dates,
            "--results-dir",
            args.results_dir,
            "--train-count",
            str(args.train_count),
            "--two-tier-engine",
            "--event-controls",
            "--exploratory-min-score",
            str(args.exploratory_min_score),
            "--exploratory-max-score",
            str(args.exploratory_max_score),
        ]
        if not args.disable_time_of_day_controls:
            command.append("--time-of-day-controls")
        run_command(command)


if __name__ == "__main__":
    main()
