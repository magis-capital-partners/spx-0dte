from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, List, Optional

import polars as pl
from thetadata import ThetaClient


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "raw" / "thetadata"


@dataclass
class DownloadRecord:
    trade_date: str
    symbol: str
    expiration: str
    dte_label: str
    interval: str
    strike_range: int
    rows: int
    path: str
    status: str
    error: Optional[str] = None


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def dates_from_args(args: argparse.Namespace) -> List[date]:
    dates: List[date] = []
    if args.dates:
        dates.extend(parse_date(value) for value in args.dates)
    if args.start_date and args.end_date:
        start = parse_date(args.start_date)
        end = parse_date(args.end_date)
        current = start
        while current <= end:
            dates.append(current)
            current = current.fromordinal(current.toordinal() + 1)
    return sorted(set(dates))


def dataframe_dates(df: pl.DataFrame, column: str) -> List[date]:
    values = df[column].to_list()
    parsed = []
    for value in values:
        if isinstance(value, date):
            parsed.append(value)
        else:
            parsed.append(parse_date(str(value)[:10]))
    return parsed


def next_expiration(expirations: Iterable[date], trade_date: date) -> Optional[date]:
    future = sorted(exp for exp in expirations if exp > trade_date)
    return future[0] if future else None


def write_frame(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(path)


def download_one(
    client: ThetaClient,
    symbol: str,
    trade_date: date,
    expiration: date,
    dte_label: str,
    output_dir: Path,
    interval: str,
    strike_range: int,
    start_time: str,
    end_time: str,
) -> DownloadRecord:
    rel = Path(f"symbol={symbol}") / f"date={trade_date.isoformat()}" / f"greeks_first_order_exp={expiration.isoformat()}_{dte_label}_{interval}_sr{strike_range}.parquet"
    path = output_dir / rel
    if path.exists() and path.stat().st_size > 0:
        try:
            rows = pl.scan_parquet(path).select(pl.len()).collect().item()
        except Exception:
            rows = -1
        return DownloadRecord(trade_date.isoformat(), symbol, expiration.isoformat(), dte_label, interval, strike_range, rows, str(path), "exists")

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
        write_frame(df, path)
        return DownloadRecord(trade_date.isoformat(), symbol, expiration.isoformat(), dte_label, interval, strike_range, df.height, str(path), "ok")
    except Exception as exc:
        return DownloadRecord(trade_date.isoformat(), symbol, expiration.isoformat(), dte_label, interval, strike_range, 0, str(path), "error", f"{type(exc).__name__}: {str(exc)[:1000]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Download SPX/SPXW option Greeks from ThetaData.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--dates", nargs="*", help="Trading dates in YYYY-MM-DD format.")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--interval", default="1m")
    parser.add_argument("--strike-range", type=int, default=80)
    parser.add_argument("--start-time", default="09:30:00")
    parser.add_argument("--end-time", default="16:00:00")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--include-next-expiration", action="store_true", default=True)
    parser.add_argument("--include-non-expiration-dates", action="store_true", help="Do not skip dates where the symbol has no same-day expiration.")
    args = parser.parse_args()

    if not os.environ.get("THETADATA_API_KEY"):
        raise SystemExit("THETADATA_API_KEY is not set. Set it in the shell before running this downloader.")

    trade_dates = dates_from_args(args)
    if not trade_dates:
        raise SystemExit("Provide --dates or --start-date/--end-date.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    client = ThetaClient()
    expirations_df = client.option_list_expirations(symbol=args.symbol)
    expirations = dataframe_dates(expirations_df, "expiration")
    records: List[DownloadRecord] = []
    expiration_set = set(expirations)
    if not args.include_non_expiration_dates:
        skipped = [trade_date for trade_date in trade_dates if trade_date not in expiration_set]
        trade_dates = [trade_date for trade_date in trade_dates if trade_date in expiration_set]
        for trade_date in skipped:
            records.append(
                DownloadRecord(
                    trade_date=trade_date.isoformat(),
                    symbol=args.symbol,
                    expiration=trade_date.isoformat(),
                    dte_label="0dte",
                    interval=args.interval,
                    strike_range=args.strike_range,
                    rows=0,
                    path="",
                    status="skipped_no_same_day_expiration",
                )
            )

    for trade_date in trade_dates:
        expirations_to_fetch = [(trade_date, "0dte")]
        if args.include_next_expiration:
            next_exp = next_expiration(expirations, trade_date)
            if next_exp:
                expirations_to_fetch.append((next_exp, "next"))
        for expiration, dte_label in expirations_to_fetch:
            records.append(
                download_one(
                    client=client,
                    symbol=args.symbol,
                    trade_date=trade_date,
                    expiration=expiration,
                    dte_label=dte_label,
                    output_dir=output_dir,
                    interval=args.interval,
                    strike_range=args.strike_range,
                    start_time=args.start_time,
                    end_time=args.end_time,
                )
            )

    manifest_path = output_dir / "download_manifest.json"
    existing = []
    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
    existing.extend(asdict(record) for record in records)
    manifest_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    ok = sum(1 for record in records if record.status in {"ok", "exists"})
    print(f"download_records={len(records)} ok_or_exists={ok} manifest={manifest_path}")
    for record in records:
        print(f"{record.trade_date} {record.expiration} {record.dte_label} {record.status} rows={record.rows}")


if __name__ == "__main__":
    main()
