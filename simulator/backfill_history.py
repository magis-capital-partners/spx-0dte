"""Download and process missing SPXW history for contiguous walk-forward coverage."""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Set


ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = Path(__file__).resolve().parent
DEFAULT_RAW = ROOT / "data" / "raw" / "thetadata"
DEFAULT_PROCESSED = ROOT / "data" / "processed"

US_HOLIDAYS = {
    "2016-01-01", "2016-01-18", "2016-02-15", "2016-03-25", "2016-05-30",
    "2016-07-04", "2016-09-05", "2016-11-24", "2016-12-26",
    "2017-01-02", "2017-01-16", "2017-02-20", "2017-04-14", "2017-05-29",
    "2017-07-04", "2017-09-04", "2017-11-23", "2017-12-25",
    "2018-01-01", "2018-01-15", "2018-02-19", "2018-03-30", "2018-05-28",
    "2018-07-04", "2018-09-03", "2018-11-22", "2018-12-25",
    "2019-01-01", "2019-01-21", "2019-02-18", "2019-04-19", "2019-05-27",
    "2019-07-04", "2019-09-02", "2019-11-28", "2019-12-25",
    "2020-01-01", "2020-01-20", "2020-02-17", "2020-04-10", "2020-05-25",
    "2020-07-03", "2020-09-07", "2020-11-26", "2020-12-25",
    "2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02", "2021-05-31",
    "2021-07-05", "2021-09-06", "2021-11-25", "2021-12-24",
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30",
    "2022-06-20", "2022-07-04", "2022-09-05", "2022-11-24", "2022-12-26",
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29",
    "2023-06-19", "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}


def iso(value: date) -> str:
    return value.isoformat()


def trading_days(start: date, end: date) -> List[str]:
    days: List[str] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and iso(current) not in US_HOLIDAYS:
            days.append(iso(current))
        current += timedelta(days=1)
    return days


def existing_dates(root: Path, symbol: str) -> Set[str]:
    base = root / f"symbol={symbol}"
    if not base.exists():
        return set()
    return {
        path.name.split("=", 1)[1]
        for path in base.iterdir()
        if path.is_dir() and path.name.startswith("date=")
    }


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def run_command(command: List[str], retries: int = 3, pause_seconds: float = 5.0) -> bool:
    print(">", " ".join(command))
    for attempt in range(1, retries + 1):
        result = subprocess.run(command)
        if result.returncode == 0:
            return True
        print(f"command failed (attempt {attempt}/{retries}), exit={result.returncode}")
        if attempt < retries:
            time.sleep(pause_seconds * attempt)
    return False


def run_chunks(
    chunks: List[List[str]],
    build_command,
    label: str,
    out_dir: Path,
) -> None:
    failed: List[str] = []
    for index, chunk in enumerate(chunks, start=1):
        print(f"{label} chunk {index}/{len(chunks)} ({len(chunk)} dates)")
        if not run_command(build_command(chunk)):
            failed.extend(chunk)
    if failed:
        fail_path = out_dir / f"{label}_failed_dates.txt"
        fail_path.write_text("\n".join(failed) + "\n", encoding="utf-8")
        print(f"{label}: {len(failed)} date(s) failed; see {fail_path}")
    else:
        print(f"{label}: all chunks completed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill missing SPXW raw/processed history.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--start-date", default="2023-01-03")
    parser.add_argument("--end-date", default="2024-04-08")
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--processed-dir", type=Path, default=DEFAULT_PROCESSED)
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--strike-range", type=int, default=80)
    parser.add_argument("--download", action="store_true", help="Download missing raw dates via ThetaData.")
    parser.add_argument("--build", action="store_true", help="Build processed features for raw dates missing processed output.")
    parser.add_argument("--enrich", action="store_true", help="Run feature_enricher across all processed dates.")
    parser.add_argument("--chunk-size", type=int, default=10)
    parser.add_argument("--download-retries", type=int, default=4)

    parser.add_argument("--all", action="store_true", help="Download + build + enrich when possible.")
    args = parser.parse_args()

    if args.all:
        args.download = True
        args.build = True
        args.enrich = True

    start = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    end = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    target_days = trading_days(start, end)

    raw_existing = existing_dates(args.raw_dir, args.symbol)
    processed_existing = existing_dates(args.processed_dir, args.symbol)
    missing_raw = [day for day in target_days if day not in raw_existing]
    missing_processed = [day for day in target_days if day in raw_existing and day not in processed_existing]

    inventory_rows = [
        {
            "date": day,
            "in_target_range": day in target_days,
            "raw_exists": day in raw_existing,
            "processed_exists": day in processed_existing,
        }
        for day in sorted(set(target_days) | raw_existing | processed_existing)
        if day <= args.end_date or day in raw_existing
    ]
    out_dir = ROOT / "data" / "backfill"
    write_csv(out_dir / "inventory.csv", inventory_rows)
    summary = {
        "target_range": [args.start_date, args.end_date],
        "target_trading_days": len(target_days),
        "raw_existing_total": len(raw_existing),
        "processed_existing_total": len(processed_existing),
        "missing_raw_in_range": len(missing_raw),
        "missing_processed_in_range": len(missing_processed),
        "earliest_processed": min(processed_existing) if processed_existing else "",
        "latest_processed": max(processed_existing) if processed_existing else "",
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))

    if args.download:
        if not os.environ.get("THETADATA_API_KEY"):
            print("THETADATA_API_KEY is not set — skipping download. Set the key and rerun with --download.")
        elif not missing_raw:
            print("No missing raw dates in target range.")
        else:
            chunks = [
                missing_raw[index : index + args.chunk_size]
                for index in range(0, len(missing_raw), args.chunk_size)
            ]

            def download_command(chunk: List[str]) -> List[str]:
                return [
                    sys.executable,
                    str(SIMULATOR / "thetadata_downloader.py"),
                    "--symbol",
                    args.symbol,
                    "--dates",
                    *chunk,
                    "--interval",
                    args.interval,
                    "--strike-range",
                    str(args.strike_range),
                    "--output-dir",
                    str(args.raw_dir),
                ]

            run_chunks(chunks, download_command, "download", out_dir)

    if args.build:
        raw_existing = existing_dates(args.raw_dir, args.symbol)
        processed_existing = existing_dates(args.processed_dir, args.symbol)
        to_build = sorted(day for day in raw_existing if day not in processed_existing and day in target_days)
        if not to_build:
            print("No raw dates need feature building in target range.")
        else:
            chunks = [
                to_build[index : index + args.chunk_size]
                for index in range(0, len(to_build), args.chunk_size)
            ]

            def build_command(chunk: List[str]) -> List[str]:
                return [
                    sys.executable,
                    str(SIMULATOR / "feature_builder.py"),
                    "--symbol",
                    args.symbol,
                    "--dates",
                    *chunk,
                    "--raw-dir",
                    str(args.raw_dir),
                    "--processed-dir",
                    str(args.processed_dir),
                ]

            run_chunks(chunks, build_command, "build", out_dir)

    if args.enrich:
        run_command(
            [
                sys.executable,
                str(SIMULATOR / "feature_enricher.py"),
                "--symbol",
                args.symbol,
                "--processed-dir",
                str(args.processed_dir),
            ]
        )

    print(f"wrote {out_dir / 'inventory.csv'}")
    print(f"wrote {out_dir / 'summary.json'}")

    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        from update_data_inventory import write_manifest

        manifest = write_manifest()
        print(
            f"updated data/inventory/manifest.json "
            f"(processed {manifest['processed']['count']} days, "
            f"{manifest['processed']['first_date']} .. {manifest['processed']['last_date']})"
        )
    except Exception as exc:
        print(f"warning: could not update data/inventory/manifest.json: {exc}")


if __name__ == "__main__":
    main()
