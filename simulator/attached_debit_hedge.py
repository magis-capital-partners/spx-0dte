from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from mbh_simulator import OptionQuote, read_quotes_csv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"


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


def safe_int(value: object, default: int = 0) -> int:
    try:
        if value in {"", None}:
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def group_quotes(quotes: Iterable[OptionQuote]) -> dict[datetime, List[OptionQuote]]:
    grouped: dict[datetime, List[OptionQuote]] = defaultdict(list)
    for quote in quotes:
        grouped[quote.timestamp].append(quote)
    return grouped


def same_day_snapshot(snapshot: Sequence[OptionQuote]) -> List[OptionQuote]:
    target_expiry = min(quote.expiry for quote in snapshot)
    return [quote for quote in snapshot if quote.expiry == target_expiry]


def choose_by_delta(snapshot: Sequence[OptionQuote], option_type: str, target_abs_delta: float) -> Optional[OptionQuote]:
    candidates = [
        quote
        for quote in same_day_snapshot(snapshot)
        if quote.option_type == option_type and quote.delta is not None
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda quote: abs(abs(quote.delta or 0.0) - target_abs_delta))


def choose_debit_spread(
    snapshot: Sequence[OptionQuote],
    side: str,
    long_abs_delta: float,
    short_abs_delta: float,
) -> Optional[Tuple[OptionQuote, OptionQuote]]:
    option_type = "CALL" if side == "bear_call" else "PUT"
    long_leg = choose_by_delta(snapshot, option_type, long_abs_delta)
    if long_leg is None:
        return None
    if option_type == "CALL":
        short_candidates = [
            quote for quote in same_day_snapshot(snapshot)
            if quote.option_type == option_type and quote.delta is not None and quote.strike > long_leg.strike
        ]
    else:
        short_candidates = [
            quote for quote in same_day_snapshot(snapshot)
            if quote.option_type == option_type and quote.delta is not None and quote.strike < long_leg.strike
        ]
    if not short_candidates:
        return None
    short_leg = min(short_candidates, key=lambda quote: abs(abs(quote.delta or 0.0) - short_abs_delta))
    return long_leg, short_leg


def intrinsic(option_type: str, strike: float, close_spot: float) -> float:
    if option_type == "CALL":
        return max(close_spot - strike, 0.0)
    if option_type == "PUT":
        return max(strike - close_spot, 0.0)
    raise ValueError(option_type)


def snapshot_spot(snapshot: Sequence[OptionQuote]) -> float:
    for quote in snapshot:
        if quote.underlying_price is not None:
            return quote.underlying_price
    raise ValueError("missing underlying price")


def run_day(
    trade_rows: List[dict],
    quotes_path: Path,
    hedge_fraction: float,
    long_abs_delta: float,
    short_abs_delta: float,
    fee_per_contract: float,
) -> tuple[dict, List[dict]]:
    quotes = read_quotes_csv(quotes_path)
    grouped = group_quotes(quotes)
    timestamps = sorted(grouped)
    if not timestamps:
        return {}, []
    close_spot = snapshot_spot(grouped[timestamps[-1]])
    hedge_rows: List[dict] = []

    for trade in trade_rows:
        entry_time = datetime.fromisoformat(trade["entry_time"])
        snapshot = grouped.get(entry_time)
        if not snapshot:
            continue
        selected = choose_debit_spread(snapshot, trade["side"], long_abs_delta, short_abs_delta)
        if selected is None:
            continue
        long_leg, short_leg = selected
        debit = long_leg.ask - short_leg.bid
        if debit <= 0:
            continue
        short_credit = safe_float(trade.get("entry_credit")) * safe_int(trade.get("contracts")) * 100
        budget = short_credit * hedge_fraction
        contracts = math.floor(budget / (debit * 100))
        if contracts <= 0:
            continue
        settlement = intrinsic(long_leg.option_type, long_leg.strike, close_spot) - intrinsic(short_leg.option_type, short_leg.strike, close_spot)
        gross_pnl = (settlement - debit) * contracts * 100
        fees = contracts * 2 * fee_per_contract
        hedge_rows.append(
            {
                "date": trade["date"],
                "source_trade_id": trade["trade_id"],
                "source_side": trade["side"],
                "source_model": trade["model"],
                "entry_time": trade["entry_time"],
                "structure": f"{long_leg.option_type.lower()}_debit_spread",
                "contracts": contracts,
                "long_strike": long_leg.strike,
                "short_strike": short_leg.strike,
                "entry_debit": round(debit, 4),
                "budget": round(budget, 2),
                "close_spot": round(close_spot, 4),
                "gross_pnl": round(gross_pnl, 2),
                "fees": round(fees, 2),
                "net_pnl": round(gross_pnl - fees, 2),
            }
        )

    net_pnl = sum(safe_float(row["net_pnl"]) for row in hedge_rows)
    budget = sum(safe_float(row["budget"]) for row in hedge_rows)
    return {"date": trade_rows[0]["date"] if trade_rows else "", "hedge_trades": len(hedge_rows), "budget": round(budget, 2), "net_pnl": round(net_pnl, 2)}, hedge_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Attach small debit-spread hedges to short-premium trades.")
    parser.add_argument("--trades", required=True)
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--results-dir", required=True)
    parser.add_argument("--hedge-fraction", type=float, default=0.25)
    parser.add_argument("--source-models", default="")
    parser.add_argument("--source-sides", default="")
    parser.add_argument("--long-abs-delta", type=float, default=0.15)
    parser.add_argument("--short-abs-delta", type=float, default=0.05)
    parser.add_argument("--fee-per-contract", type=float, default=0.79)
    args = parser.parse_args()

    trades = read_rows(Path(args.trades))
    source_models = {item.strip() for item in args.source_models.split(",") if item.strip()}
    source_sides = {item.strip() for item in args.source_sides.split(",") if item.strip()}
    if source_models:
        trades = [row for row in trades if row.get("model", "") in source_models]
    if source_sides:
        trades = [row for row in trades if row.get("side", "") in source_sides]
    by_date: dict[str, List[dict]] = defaultdict(list)
    for row in trades:
        by_date[row["date"]].append(row)

    summaries: List[dict] = []
    hedges: List[dict] = []
    for trade_date, rows in sorted(by_date.items()):
        quotes_path = Path(args.processed_dir) / f"symbol={args.symbol}" / f"date={trade_date}" / "normalized_option_quotes.csv"
        summary, hedge_rows = run_day(rows, quotes_path, args.hedge_fraction, args.long_abs_delta, args.short_abs_delta, args.fee_per_contract)
        if summary:
            summaries.append(summary)
            hedges.extend(hedge_rows)
            print(f"{trade_date} hedge_trades={summary['hedge_trades']} net_pnl={summary['net_pnl']}")

    results_dir = Path(args.results_dir)
    write_rows(results_dir / "attached_hedge_daily_summary.csv", summaries)
    write_rows(results_dir / "attached_hedge_trades.csv", hedges)
    print(f"wrote {results_dir / 'attached_hedge_daily_summary.csv'}")
    print(f"wrote {results_dir / 'attached_hedge_trades.csv'}")


if __name__ == "__main__":
    main()
