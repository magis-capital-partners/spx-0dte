from __future__ import annotations

import argparse
import csv
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List

from thetadata import ThetaClient


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "microstructure" / "thetadata"


@dataclass
class MicrostructureDownloadRecord:
    date: str
    trade_id: str
    symbol: str
    expiration: str
    interval: str
    strike_range: int
    start_time: str
    end_time: str
    rows: int
    path: str
    status: str
    error: str = ""


def read_rows(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value in {"", None}:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


def window_times(row: dict, around_stop_minutes: int, after_stop_minutes: int) -> tuple[str, str]:
    if around_stop_minutes <= 0:
        return row["window_start_time"], row["window_end_time"]
    stop_time = parse_timestamp(row["stop_time"])
    start_time = stop_time - timedelta(minutes=around_stop_minutes)
    end_time = stop_time + timedelta(minutes=after_stop_minutes)
    return start_time.time().isoformat(timespec="seconds"), end_time.time().isoformat(timespec="seconds")


def output_path(output_dir: Path, row: dict, symbol: str, interval: str, strike_range: int) -> Path:
    trade_date = row["date"]
    trade_id = row.get("trade_id", "unknown")
    short_type = row.get("short_type", "")
    short_strike = row.get("short_strike", "")
    return (
        output_dir
        / f"symbol={symbol}"
        / f"date={trade_date}"
        / f"trade={trade_id}_{short_type}_{short_strike}_{interval}_sr{strike_range}.parquet"
    )


def download_window(
    client: ThetaClient,
    row: dict,
    symbol: str,
    output_dir: Path,
    interval: str,
    strike_range: int,
    around_stop_minutes: int,
    after_stop_minutes: int,
) -> MicrostructureDownloadRecord:
    trade_date = parse_date(row["date"])
    expiration = parse_date(row.get("expiry") or row["date"])
    start_time, end_time = window_times(row, around_stop_minutes, after_stop_minutes)
    path = output_path(output_dir, row, symbol, interval, strike_range)
    if path.exists() and path.stat().st_size > 0:
        return MicrostructureDownloadRecord(
            date=row["date"],
            trade_id=row.get("trade_id", ""),
            symbol=symbol,
            expiration=expiration.isoformat(),
            interval=interval,
            strike_range=strike_range,
            start_time=start_time,
            end_time=end_time,
            rows=-1,
            path=str(path),
            status="exists",
        )

    try:
        df = client.option_history_greeks_first_order(
            symbol=symbol,
            expiration=expiration,
            date=trade_date,
            interval=interval,
            strike_range=strike_range,
            start_time=start_time,
            end_time=end_time,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        df.write_parquet(path)
        return MicrostructureDownloadRecord(
            date=row["date"],
            trade_id=row.get("trade_id", ""),
            symbol=symbol,
            expiration=expiration.isoformat(),
            interval=interval,
            strike_range=strike_range,
            start_time=start_time,
            end_time=end_time,
            rows=df.height,
            path=str(path),
            status="ok",
        )
    except Exception as exc:
        return MicrostructureDownloadRecord(
            date=row["date"],
            trade_id=row.get("trade_id", ""),
            symbol=symbol,
            expiration=expiration.isoformat(),
            interval=interval,
            strike_range=strike_range,
            start_time=start_time,
            end_time=end_time,
            rows=0,
            path=str(path),
            status="error",
            error=f"{type(exc).__name__}: {str(exc)[:1000]}",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Download ThetaData microstructure windows for stopped trades.")
    parser.add_argument("--windows", required=True)
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--interval", default="10s")
    parser.add_argument("--strike-range", type=int, default=30)
    parser.add_argument("--around-stop-minutes", type=int, default=0, help="If positive, download only this many minutes before the stop.")
    parser.add_argument("--after-stop-minutes", type=int, default=5, help="Minutes after the stop when --around-stop-minutes is used.")
    parser.add_argument("--skip", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not os.environ.get("THETADATA_API_KEY"):
        raise SystemExit("THETADATA_API_KEY is not set. Set it in the shell before running this downloader.")

    rows = read_rows(Path(args.windows))
    if args.skip:
        rows = rows[args.skip :]
    if args.limit:
        rows = rows[: args.limit]
    output_dir = Path(args.output_dir)
    client = ThetaClient()
    records = [
        download_window(
            client,
            row,
            args.symbol,
            output_dir,
            args.interval,
            args.strike_range,
            args.around_stop_minutes,
            args.after_stop_minutes,
        )
        for row in rows
    ]

    manifest_path = output_dir / "microstructure_download_manifest.json"
    existing = []
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
    existing.extend(asdict(record) for record in records)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    ok = sum(1 for record in records if record.status in {"ok", "exists"})
    print(f"download_records={len(records)} ok_or_exists={ok} manifest={manifest_path}")
    for record in records:
        print(f"{record.date} trade={record.trade_id} {record.status} rows={record.rows}")


if __name__ == "__main__":
    main()
