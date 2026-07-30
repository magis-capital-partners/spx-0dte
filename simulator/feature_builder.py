from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import polars as pl

from rv_feature import atm_iv_from_pair, realized_vs_implied_raw


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW = ROOT / "data" / "raw" / "thetadata"
DEFAULT_PROCESSED = ROOT / "data" / "processed"


def parse_date_from_day_dir(day_dir: Path) -> str:
    name = day_dir.name
    if name.startswith("date="):
        return name.split("=", 1)[1]
    return name


def mid(row: dict) -> float:
    bid = row.get("bid")
    ask = row.get("ask")
    if bid is None or ask is None:
        return math.nan
    return (float(bid) + float(ask)) / 2.0


def row_timestamp(row: dict) -> str:
    value = row["timestamp"]
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def clean_time_key(value: object) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat(timespec="seconds")
    return str(value).replace(" EST", "").replace(" EDT", "")


def minutes_to_close(ts: datetime, close: time = time(16, 0)) -> float:
    close_dt = ts.replace(hour=close.hour, minute=close.minute, second=0, microsecond=0)
    return max((close_dt - ts.replace(tzinfo=None)).total_seconds() / 60.0, 0.0)


def choose_atm_pair(rows: List[dict], spot: float) -> Tuple[Optional[dict], Optional[dict]]:
    calls = [row for row in rows if str(row["right"]).upper() == "CALL"]
    puts = [row for row in rows if str(row["right"]).upper() == "PUT"]
    if not calls or not puts or math.isnan(spot):
        return None, None
    strikes = sorted(set(float(row["strike"]) for row in rows))
    atm = min(strikes, key=lambda strike: abs(strike - spot))
    call = min(calls, key=lambda row: (abs(float(row["strike"]) - atm), abs(abs(float(row.get("delta") or 0.0)) - 0.5)))
    put = min(puts, key=lambda row: (abs(float(row["strike"]) - atm), abs(abs(float(row.get("delta") or 0.0)) - 0.5)))
    return call, put


def choose_delta(rows: List[dict], right: str, target_abs_delta: float) -> Optional[dict]:
    candidates = [row for row in rows if str(row["right"]).upper() == right and row.get("delta") is not None]
    if not candidates:
        return None
    return min(candidates, key=lambda row: abs(abs(float(row["delta"])) - target_abs_delta))


def load_parquet(path: Path) -> List[dict]:
    df = pl.read_parquet(path)
    return df.to_dicts()


def group_by_timestamp(rows: Iterable[dict]) -> Dict[str, List[dict]]:
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        grouped[clean_time_key(row["timestamp"])].append(row)
    return grouped


def find_day_files(raw_dir: Path, symbol: str, trade_date: str) -> Tuple[Path, Optional[Path]]:
    day_dir = raw_dir / f"symbol={symbol}" / f"date={trade_date}"
    if not day_dir.exists():
        raise FileNotFoundError(day_dir)
    zero = sorted(day_dir.glob(f"greeks_first_order_exp={trade_date}_0dte_*.parquet"))
    next_files = sorted(day_dir.glob("greeks_first_order_exp=*_next_*.parquet"))
    if not zero:
        raise FileNotFoundError(f"No 0DTE file in {day_dir}")
    return zero[-1], next_files[-1] if next_files else None


def build_for_day(raw_dir: Path, processed_dir: Path, symbol: str, trade_date: str) -> Tuple[Path, Path]:
    zero_file, next_file = find_day_files(raw_dir, symbol, trade_date)
    zero_rows = load_parquet(zero_file)
    next_rows = load_parquet(next_file) if next_file else []
    zero_by_ts = group_by_timestamp(zero_rows)
    next_by_ts = group_by_timestamp(next_rows)

    quote_rows = []
    feature_rows = []
    first_straddle: Optional[float] = None
    first_minutes: Optional[float] = None
    previous_spot: Optional[float] = None
    spot_history: List[float] = []

    for ts_key in sorted(zero_by_ts):
        rows = zero_by_ts[ts_key]
        spot_values = [float(row["underlying_price"]) for row in rows if row.get("underlying_price") is not None and not math.isnan(float(row["underlying_price"]))]
        if not spot_values:
            continue
        spot = spot_values[0]
        spot_history.append(spot)
        ts = datetime.fromisoformat(ts_key)

        for row in rows:
            if row.get("bid") is None or row.get("ask") is None or row.get("delta") is None:
                continue
            quote_rows.append(
                {
                    "timestamp": ts_key,
                    "expiry": str(row["expiration"])[:10],
                    "option_type": str(row["right"]).upper(),
                    "strike": float(row["strike"]),
                    "bid": float(row["bid"]),
                    "ask": float(row["ask"]),
                    "delta": float(row["delta"]),
                    "iv": float(row.get("implied_vol") or 0.0),
                    "underlying_price": spot,
                }
            )
        if ts_key in next_by_ts:
            next_spot_values = [
                float(row["underlying_price"])
                for row in next_by_ts[ts_key]
                if row.get("underlying_price") is not None and not math.isnan(float(row["underlying_price"]))
            ]
            next_spot = next_spot_values[0] if next_spot_values else spot
            for row in next_by_ts[ts_key]:
                if row.get("bid") is None or row.get("ask") is None or row.get("delta") is None:
                    continue
                quote_rows.append(
                    {
                        "timestamp": ts_key,
                        "expiry": str(row["expiration"])[:10],
                        "option_type": str(row["right"]).upper(),
                        "strike": float(row["strike"]),
                        "bid": float(row["bid"]),
                        "ask": float(row["ask"]),
                        "delta": float(row["delta"]),
                        "iv": float(row.get("implied_vol") or 0.0),
                        "underlying_price": next_spot,
                    }
                )

        call, put = choose_atm_pair(rows, spot)
        if not call or not put:
            continue
        straddle = mid(call) + mid(put)
        if not math.isfinite(straddle) or straddle <= 0:
            continue
        if first_straddle is None:
            first_straddle = straddle
            first_minutes = minutes_to_close(ts)
        baseline = first_straddle
        if first_minutes and first_minutes > 0:
            baseline = first_straddle * (minutes_to_close(ts) / first_minutes)
        straddle_residual_z = (straddle - baseline) / max(first_straddle, 1e-9)

        put_25 = choose_delta(rows, "PUT", 0.25)
        call_25 = choose_delta(rows, "CALL", 0.25)
        skew_z = 0.0
        if put_25 and call_25:
            skew_z = float(put_25.get("implied_vol") or 0.0) - float(call_25.get("implied_vol") or 0.0)

        next_straddle = math.nan
        if ts_key in next_by_ts:
            next_spot_values = [float(row["underlying_price"]) for row in next_by_ts[ts_key] if row.get("underlying_price") is not None and not math.isnan(float(row["underlying_price"]))]
            next_spot = next_spot_values[0] if next_spot_values else spot
            n_call, n_put = choose_atm_pair(next_by_ts[ts_key], next_spot)
            if n_call and n_put:
                next_straddle = mid(n_call) + mid(n_put)
        term_ratio_z = 0.0
        if math.isfinite(next_straddle) and next_straddle > 0:
            term_ratio_z = (straddle / next_straddle) - 1.0

        trend_score = 0.0
        if previous_spot is not None and straddle > 0:
            trend_score = (spot - previous_spot) / straddle
        previous_spot = spot

        atm_iv = atm_iv_from_pair(
            float(call.get("implied_vol") or 0.0) or None,
            float(put.get("implied_vol") or 0.0) or None,
        )
        realized = realized_vs_implied_raw(
            spot_history, spot=spot, straddle=straddle, atm_iv=atm_iv
        )

        feature_rows.append(
            {
                "timestamp": ts_key,
                "straddle": straddle,
                "linear_decay_baseline": baseline,
                "straddle_residual_z": straddle_residual_z,
                "skew_z": skew_z,
                "term_ratio_z": term_ratio_z,
                "trend_score": trend_score,
                "realized_vs_implied_z": realized,
                "vix": "",
                "underlying_price": spot,
                "atm_call_strike": float(call["strike"]),
                "atm_put_strike": float(put["strike"]),
            }
        )

    out_dir = processed_dir / f"symbol={symbol}" / f"date={trade_date}"
    out_dir.mkdir(parents=True, exist_ok=True)
    quotes_path = out_dir / "normalized_option_quotes.csv"
    signals_path = out_dir / "signals.csv"
    pl.DataFrame(quote_rows).write_csv(quotes_path)
    pl.DataFrame(feature_rows).write_csv(signals_path)
    return quotes_path, signals_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build MBH reconstruction features from ThetaData raw files.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW))
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    args = parser.parse_args()

    for trade_date in args.dates:
        quotes_path, signals_path = build_for_day(Path(args.raw_dir), Path(args.processed_dir), args.symbol, trade_date)
        print(f"{trade_date} quotes={quotes_path} signals={signals_path}")


if __name__ == "__main__":
    main()
