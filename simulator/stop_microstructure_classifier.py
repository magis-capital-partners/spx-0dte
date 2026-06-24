from __future__ import annotations

import argparse
import csv
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import polars as pl


ROOT = Path(__file__).resolve().parents[1]


def read_rows(path: Path) -> List[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_rows(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def micro_path(root: Path, row: dict, symbol: str, interval: str, strike_range: int) -> Path:
    return (
        root
        / f"symbol={symbol}"
        / f"date={row['date']}"
        / f"trade={row.get('trade_id', 'unknown')}_{row.get('short_type', '')}_{row.get('short_strike', '')}_{interval}_sr{strike_range}.parquet"
    )


def resolve_micro_path(root: Path, row: dict, symbol: str, interval: str, strike_range: int) -> Optional[Path]:
    exact = micro_path(root, row, symbol, interval, strike_range)
    if exact.exists() and exact.stat().st_size > 0:
        return exact
    date_dir = root / f"symbol={symbol}" / f"date={row['date']}"
    pattern = f"trade={row.get('trade_id', 'unknown')}_{row.get('short_type', '')}_{row.get('short_strike', '')}_{interval}_sr*.parquet"
    matches = [path for path in date_dir.glob(pattern) if path.stat().st_size > 0]
    if not matches:
        return None
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]


def normalize_quotes(df: pl.DataFrame) -> pl.DataFrame:
    rename = {}
    for source, target in {
        "ms_of_day": "ms_of_day",
        "date": "date",
        "right": "option_type",
        "option_type": "option_type",
        "strike": "strike",
        "bid": "bid",
        "ask": "ask",
        "underlying_price": "underlying_price",
        "delta": "delta",
    }.items():
        if source in df.columns and source != target:
            rename[source] = target
    if rename:
        df = df.rename(rename)
    if "timestamp" not in df.columns and "ms_of_day" in df.columns and "date" in df.columns:
        df = df.with_columns(
            (
                pl.col("date").cast(pl.Utf8)
                + "T"
                + (pl.col("ms_of_day") // 3_600_000).cast(pl.Int64).cast(pl.Utf8).str.zfill(2)
                + ":"
                + ((pl.col("ms_of_day") % 3_600_000) // 60_000).cast(pl.Int64).cast(pl.Utf8).str.zfill(2)
                + ":"
                + ((pl.col("ms_of_day") % 60_000) // 1000).cast(pl.Int64).cast(pl.Utf8).str.zfill(2)
            ).alias("timestamp")
        )
    return df


def load_micro_quotes(path: Path, row: dict) -> Optional[pl.DataFrame]:
    if not path.exists() or path.stat().st_size == 0:
        return None
    df = normalize_quotes(pl.read_parquet(path))
    required = {"timestamp", "option_type", "strike", "ask", "bid", "underlying_price"}
    if not required.issubset(set(df.columns)):
        return None
    option_type = row.get("short_type", "").upper()
    strike = safe_float(row.get("short_strike"))
    return (
        df.filter((pl.col("option_type").cast(pl.Utf8).str.to_uppercase() == option_type) & (pl.col("strike").cast(pl.Float64) == strike))
        .sort("timestamp")
    )


def classify(row: dict, quotes: Optional[pl.DataFrame]) -> dict:
    entry_credit = safe_float(row.get("entry_credit"))
    stop_price = safe_float(row.get("stop_price"))
    stop_fill = safe_float(row.get("stop_fill"))
    entry_spot = safe_float(row.get("entry_spot"))
    stop_spot = safe_float(row.get("stop_spot"))
    side = row.get("side", "")
    minutes_to_stop = safe_float(row.get("minutes_to_stop"))
    adverse_move = stop_spot - entry_spot if side == "bear_call" else entry_spot - stop_spot

    result = {
        **row,
        "micro_path": "",
        "micro_rows": 0,
        "max_short_ask": "",
        "max_bid_ask_width": "",
        "ask_above_stop_ticks": 0,
        "first_ask_above_stop_time": "",
        "classification": "missing_microstructure",
        "classification_reason": "no readable microstructure file",
    }
    if quotes is None or quotes.is_empty():
        return result

    quotes = quotes.with_columns((pl.col("ask").cast(pl.Float64) - pl.col("bid").cast(pl.Float64)).alias("bid_ask_width"))
    above = quotes.filter(pl.col("ask").cast(pl.Float64) >= stop_price)
    max_short_ask = quotes.select(pl.max("ask")).item()
    max_width = quotes.select(pl.max("bid_ask_width")).item()
    first_above = above.select(pl.first("timestamp")).item() if not above.is_empty() else ""
    width_ratio = max_width / entry_credit if entry_credit else 0.0
    stop_slippage = stop_fill - stop_price

    if adverse_move >= abs(safe_float(row.get("short_strike")) - entry_spot) * 0.75:
        label = "real_directional_failure"
        reason = "underlying moved most of the entry distance to the short strike"
    elif above.height <= 2 and width_ratio >= 0.75:
        label = "quote_spike_or_width_artifact"
        reason = "stop was touched only briefly while bid/ask width was large relative to entry credit"
    elif minutes_to_stop <= 30:
        label = "fast_intraday_whipsaw"
        reason = "stop occurred within 30 minutes of entry"
    elif parse_ts(row["stop_time"]).time().hour >= 14:
        label = "late_day_reversal"
        reason = "stop occurred in the late-session risk window"
    elif stop_slippage > max(0.25, entry_credit * 0.10):
        label = "slippage_sensitive_stop"
        reason = "stop fill exceeded stop trigger by more than 10% of entry credit"
    else:
        label = "ordinary_stop_path"
        reason = "stop was confirmed by the microstructure path without an obvious special label"

    result.update(
        {
            "micro_rows": quotes.height,
            "max_short_ask": round(float(max_short_ask), 4),
            "max_bid_ask_width": round(float(max_width), 4),
            "ask_above_stop_ticks": above.height,
            "first_ask_above_stop_time": first_above,
            "classification": label,
            "classification_reason": reason,
        }
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify stopped trades using downloaded microstructure windows.")
    parser.add_argument("--windows", required=True)
    parser.add_argument("--micro-root", default=str(ROOT / "data" / "microstructure" / "thetadata"))
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--interval", default="10s")
    parser.add_argument("--strike-range", type=int, default=30)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    rows = read_rows(Path(args.windows))
    classified = []
    for row in rows:
        path = resolve_micro_path(Path(args.micro_root), row, args.symbol, args.interval, args.strike_range)
        result = classify(row, load_micro_quotes(path, row) if path else None)
        if path:
            result["micro_path"] = str(path)
        classified.append(result)
    write_rows(Path(args.output), classified)
    counts = {}
    for row in classified:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1
    print(f"classified={len(classified)} output={args.output}")
    for key, value in sorted(counts.items()):
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
