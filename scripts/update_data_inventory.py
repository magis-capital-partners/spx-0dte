"""Scan local ThetaData cache and write data/inventory/manifest.json.

Run after any backfill/build so agents know what is on disk and can avoid
re-downloading from ThetaData.
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Set

ROOT = Path(__file__).resolve().parents[1]
RAW_ROOT = ROOT / "data" / "raw" / "thetadata" / "symbol=SPXW"
PROC_ROOT = ROOT / "data" / "processed" / "symbol=SPXW"
MANIFEST = ROOT / "data" / "inventory" / "manifest.json"
VIX_CSV = ROOT / "data" / "calendar" / "vix_daily.csv"


def list_dates(root: Path) -> List[str]:
    if not root.exists():
        return []
    return sorted(
        path.name.split("=", 1)[1]
        for path in root.iterdir()
        if path.is_dir() and path.name.startswith("date=")
    )


def summarize_dates(dates: Iterable[str]) -> dict:
    ordered = list(dates)
    if not ordered:
        return {"count": 0, "first_date": "", "last_date": "", "by_year": {}}
    return {
        "count": len(ordered),
        "first_date": ordered[0],
        "last_date": ordered[-1],
        "by_year": dict(sorted(Counter(d[:4] for d in ordered).items())),
    }


def processed_complete(day_dir: Path) -> bool:
    return (day_dir / "signals.csv").exists() and (day_dir / "normalized_option_quotes.csv").exists()


def build_manifest() -> dict:
    raw_dates = list_dates(RAW_ROOT)
    proc_dates = [d for d in list_dates(PROC_ROOT) if processed_complete(PROC_ROOT / f"date={d}")]
    raw_set = set(raw_dates)
    proc_set = set(proc_dates)

    raw_unprocessed = sorted(raw_set - proc_set)
    proc_years = Counter(d[:4] for d in proc_dates)
    raw_years = Counter(d[:4] for d in raw_dates)

    missing_processed_by_year: Dict[str, int] = {}
    for year in sorted(set(raw_years) | set(proc_years)):
        raw_count = raw_years.get(year, 0)
        proc_count = proc_years.get(year, 0)
        if raw_count > proc_count:
            missing_processed_by_year[year] = raw_count - proc_count

    proc_first = proc_dates[0] if proc_dates else "2019-01-02"
    proc_last = proc_dates[-1] if proc_dates else ""

    vix_block = {"path": "data/calendar/vix_daily.csv", "count": 0, "first_date": "", "last_date": ""}
    if VIX_CSV.exists():
        import csv

        vix_dates = []
        with VIX_CSV.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("date"):
                    vix_dates.append(row["date"][:10])
        vix_dates.sort()
        if vix_dates:
            vix_block = {
                "path": "data/calendar/vix_daily.csv",
                "count": len(vix_dates),
                "first_date": vix_dates[0],
                "last_date": vix_dates[-1],
                "source": "yahoo_finance_^VIX",
            }

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "policy": (
            "SPXW history is cached locally under data/raw and data/processed. "
            "Use this cache for backtests. Do NOT call ThetaData or pass --download "
            "unless the user explicitly asks to fill manifest gaps."
        ),
        "raw": {
            "path": "data/raw/thetadata/symbol=SPXW",
            "description": "Parquet Greeks from ThetaData (source for rebuilds)",
            **summarize_dates(raw_dates),
        },
        "processed": {
            "path": "data/processed/symbol=SPXW",
            "description": "Simulator inputs: normalized_option_quotes.csv + signals.csv per day",
            **summarize_dates(proc_dates),
        },
        "vix_daily": vix_block,
        "cache_status": {
            "raw_not_built_count": len(raw_unprocessed),
            "raw_not_built_by_year": dict(sorted(Counter(d[:4] for d in raw_unprocessed).items())),
            "missing_processed_by_year": missing_processed_by_year,
        },
        "agent_commands": {
            "refresh_inventory": "python scripts/update_data_inventory.py",
            "backtest_no_download": (
                f"python simulator/historical_3d_backtest.py "
                f"--start-date {proc_first} --end-date {proc_last}"
            ),
            "build_from_raw_only": (
                f"python simulator/backfill_history.py --build "
                f"--start-date {proc_first} --end-date {proc_last or proc_first}"
            ),
            "enrich": "python simulator/feature_enricher.py --symbol SPXW",
            "download_vix": "python scripts/download_vix_daily.py --start-date 2019-01-01",
            "enrich_vix": (
                "python scripts/download_vix_daily.py --start-date 2019-01-01 && "
                "python simulator/vix_signal_enricher.py --symbol SPXW"
            ),
            "validate_vix": "python scripts/validate_vix_coverage.py",
            "vix_regime_tests": "python scripts/run_vix_regime_tests.py",
        },
        "do_not": [
            "Do not run thetadata_downloader.py or backfill_history.py --download unless the user explicitly requests new dates.",
            "Do not assume data starts in 2016; ThetaData usable same-day SPXW Greeks begin ~2019.",
            "Do not commit parquet/CSV chain files to git; they stay local under data/.",
        ],
    }


def write_manifest(path: Path = MANIFEST) -> dict:
    manifest = build_manifest()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    manifest = write_manifest()
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
