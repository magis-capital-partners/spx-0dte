"""Retry ThetaData downloads for raw date dirs missing 0DTE parquet."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "thetadata" / "symbol=SPXW"
DOWNLOADER = ROOT / "simulator" / "thetadata_downloader.py"


def missing_0dte_dates() -> list[str]:
    out: list[str] = []
    if not RAW.exists():
        return out
    for day_dir in sorted(RAW.iterdir()):
        if not day_dir.is_dir():
            continue
        if not list(day_dir.glob("greeks_first_order_exp=*_0dte_*.parquet")):
            out.append(day_dir.name.split("=", 1)[1])
    return out


def main() -> None:
    missing = missing_0dte_dates()
    print(f"Retrying {len(missing)} dates one-at-a-time...", flush=True)
    ok = fail = 0
    for i, day in enumerate(missing):
        print(f"[{i + 1}/{len(missing)}] {day}", flush=True)
        subprocess.run(
            [
                sys.executable,
                str(DOWNLOADER),
                "--symbol",
                "SPXW",
                "--dates",
                day,
                "--interval",
                "1m",
                "--strike-range",
                "80",
            ],
            check=False,
        )
        day_dir = RAW / f"date={day}"
        has = list(day_dir.glob("greeks_first_order_exp=*_0dte_*.parquet")) if day_dir.exists() else []
        if has:
            ok += 1
        else:
            fail += 1
        time.sleep(3)
    print(f"DOWNLOAD_RETRY_DONE ok={ok} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
