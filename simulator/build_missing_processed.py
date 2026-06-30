"""Build processed features for raw dates with 0DTE but no signals.csv."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "thetadata" / "symbol=SPXW"
PROC = ROOT / "data" / "processed" / "symbol=SPXW"


def need_build() -> list[str]:
    dates: list[str] = []
    for day_dir in sorted(RAW.iterdir()):
        if not day_dir.is_dir():
            continue
        day = day_dir.name.split("=", 1)[1]
        if not list(day_dir.glob("greeks_first_order_exp=*_0dte_*.parquet")):
            continue
        if not (PROC / f"date={day}" / "signals.csv").exists():
            dates.append(day)
    return dates


def main() -> None:
    dates = need_build()
    print(f"Building {len(dates)} dates...", flush=True)
    failed: list[str] = []
    for i, day in enumerate(dates):
        print(f"[{i + 1}/{len(dates)}] {day}", flush=True)
        r = subprocess.run(
            [sys.executable, str(ROOT / "simulator" / "feature_builder.py"), "--symbol", "SPXW", "--dates", day]
        )
        if r.returncode != 0:
            failed.append(day)
    print("Enriching...", flush=True)
    subprocess.run([sys.executable, str(ROOT / "simulator" / "feature_enricher.py"), "--symbol", "SPXW"], check=False)
    print(f"BUILD_DONE built={len(dates) - len(failed)} failed={len(failed)}", flush=True)


if __name__ == "__main__":
    main()
