"""Compress completed IB debug logs without touching the active session log."""
from __future__ import annotations

import argparse
import gzip
import os
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"
SESSION_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class CompressionResult:
    compressed: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0


def _gzip_atomic(source: Path, destination: Path, *, compresslevel: int) -> None:
    temp = destination.with_name(destination.name + ".tmp")
    temp.unlink(missing_ok=True)
    try:
        with source.open("rb") as src, temp.open("wb") as raw:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                compresslevel=compresslevel,
                fileobj=raw,
                mtime=int(source.stat().st_mtime),
            ) as zipped:
                shutil.copyfileobj(src, zipped, length=1024 * 1024)
        os.replace(temp, destination)
        source.unlink()
    except Exception:
        temp.unlink(missing_ok=True)
        raise


def compress_completed_ib_logs(
    *,
    live_dir: Path = LIVE_DIR,
    active_date: Optional[str] = None,
    compresslevel: int = 6,
) -> CompressionResult:
    """Gzip ``ib.log`` for dated directories strictly before ``active_date``."""
    cutoff = active_date or date.today().isoformat()
    result = CompressionResult()
    if not live_dir.is_dir():
        return result

    for day_dir in sorted(live_dir.iterdir()):
        if (
            not day_dir.is_dir()
            or not SESSION_DIR_RE.fullmatch(day_dir.name)
            or day_dir.name >= cutoff
        ):
            continue
        source = day_dir / "ib.log"
        destination = day_dir / "ib.log.gz"
        if not source.is_file():
            continue
        if destination.exists():
            result.skipped += 1
            continue
        source_size = source.stat().st_size
        try:
            _gzip_atomic(source, destination, compresslevel=compresslevel)
        except Exception as exc:
            result.failed += 1
            print(f"[log-maintenance] failed {source}: {exc!r}", flush=True)
            continue
        result.compressed += 1
        result.bytes_before += source_size
        result.bytes_after += destination.stat().st_size
        print(
            f"[log-maintenance] compressed {day_dir.name}/ib.log "
            f"{source_size / (1024 ** 2):.1f}MB -> "
            f"{destination.stat().st_size / (1024 ** 2):.1f}MB",
            flush=True,
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-dir", type=Path, default=LIVE_DIR)
    parser.add_argument("--active-date", default=date.today().isoformat())
    parser.add_argument("--compresslevel", type=int, choices=range(1, 10), default=6)
    args = parser.parse_args()
    result = compress_completed_ib_logs(
        live_dir=args.live_dir,
        active_date=args.active_date,
        compresslevel=args.compresslevel,
    )
    saved = result.bytes_before - result.bytes_after
    print(
        f"[log-maintenance] done compressed={result.compressed} "
        f"skipped={result.skipped} failed={result.failed} "
        f"saved={saved / (1024 ** 2):.1f}MB",
        flush=True,
    )
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
