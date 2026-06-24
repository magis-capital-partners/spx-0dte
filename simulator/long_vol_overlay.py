from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from mbh_simulator import OptionQuote, read_quotes_csv, read_signals_csv


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "results"


def group_quotes(quotes: Iterable[OptionQuote]) -> Dict[datetime, List[OptionQuote]]:
    grouped: Dict[datetime, List[OptionQuote]] = defaultdict(list)
    for quote in quotes:
        grouped[quote.timestamp].append(quote)
    return grouped


def spot(snapshot: Sequence[OptionQuote]) -> float:
    for quote in snapshot:
        if quote.underlying_price is not None:
            return quote.underlying_price
    raise ValueError("snapshot missing underlying price")


def same_day_snapshot(snapshot: Sequence[OptionQuote]) -> List[OptionQuote]:
    target_expiry = min(quote.expiry for quote in snapshot)
    return [quote for quote in snapshot if quote.expiry == target_expiry]


def choose_atm_straddle(snapshot: Sequence[OptionQuote]) -> Optional[Tuple[OptionQuote, OptionQuote]]:
    snapshot = same_day_snapshot(snapshot)
    level = spot(snapshot)
    calls = [quote for quote in snapshot if quote.option_type == "CALL"]
    puts = [quote for quote in snapshot if quote.option_type == "PUT"]
    if not calls or not puts:
        return None
    strikes = sorted({quote.strike for quote in snapshot})
    atm = min(strikes, key=lambda strike: abs(strike - level))
    call = min(calls, key=lambda quote: abs(quote.strike - atm))
    put = min(puts, key=lambda quote: abs(quote.strike - atm))
    return call, put


def choose_put_by_delta(snapshot: Sequence[OptionQuote], target_abs_delta: float) -> Optional[OptionQuote]:
    snapshot = same_day_snapshot(snapshot)
    puts = [quote for quote in snapshot if quote.option_type == "PUT" and quote.delta is not None]
    if not puts:
        return None
    return min(puts, key=lambda quote: abs(abs(quote.delta or 0.0) - target_abs_delta))


def choose_put_spread(
    snapshot: Sequence[OptionQuote],
    long_target_abs_delta: float,
    short_target_abs_delta: float,
) -> Optional[Tuple[OptionQuote, OptionQuote]]:
    long_put = choose_put_by_delta(snapshot, long_target_abs_delta)
    if long_put is None:
        return None
    short_candidates = [
        quote
        for quote in snapshot
        if quote.option_type == "PUT"
        and quote.delta is not None
        and quote.strike < long_put.strike
    ]
    if not short_candidates:
        return None
    short_put = min(short_candidates, key=lambda quote: abs(abs(quote.delta or 0.0) - short_target_abs_delta))
    return long_put, short_put


def intrinsic(option_type: str, strike: float, close_spot: float) -> float:
    if option_type == "CALL":
        return max(close_spot - strike, 0.0)
    if option_type == "PUT":
        return max(strike - close_spot, 0.0)
    raise ValueError(option_type)


def should_enter(
    signal,
    straddle_threshold: float,
    term_threshold: float,
    trend_threshold: float,
    skew_threshold: float,
    trigger_mode: str,
) -> bool:
    rich_premium = signal.straddle_residual_z >= straddle_threshold
    term_dislocated = abs(signal.term_ratio_z) >= term_threshold
    trend_extreme = abs(signal.trend_score) >= trend_threshold
    downside_trend = signal.trend_score <= -trend_threshold
    skew_dislocated = abs(signal.skew_z) >= skew_threshold

    if trigger_mode == "any":
        return rich_premium or term_dislocated or trend_extreme or skew_dislocated
    if trigger_mode == "confluence":
        return sum([rich_premium, term_dislocated, trend_extreme, skew_dislocated]) >= 2
    if trigger_mode == "crash_hedge":
        return downside_trend and term_dislocated and skew_dislocated
    raise ValueError(f"Unsupported trigger mode: {trigger_mode}")


def run_overlay_day(
    quotes_path: Path,
    signals_path: Path,
    trade_date: str,
    account_equity: float,
    daily_budget_pct: float,
    max_trades: int,
    min_minutes_between: int,
    straddle_threshold: float,
    term_threshold: float,
    trend_threshold: float,
    skew_threshold: float,
    trigger_mode: str,
    fee_per_contract: float,
    structure: str,
    put_target_delta: float,
    put_spread_long_delta: float,
    put_spread_short_delta: float,
) -> tuple[dict, List[dict]]:
    quotes = read_quotes_csv(quotes_path)
    signals = read_signals_csv(signals_path)
    by_ts = group_quotes(quotes)
    timestamps = sorted(by_ts)
    if not timestamps:
        raise ValueError(f"no quotes for {trade_date}")
    close_spot = spot(by_ts[timestamps[-1]])
    budget_remaining = account_equity * daily_budget_pct
    budget_per_trade = budget_remaining / max(max_trades, 1)
    last_entry: Optional[datetime] = None
    rows: List[dict] = []

    for signal in signals:
        timestamp = signal.timestamp
        if timestamp not in by_ts:
            continue
        if len(rows) >= max_trades:
            break
        if last_entry is not None and (timestamp - last_entry).total_seconds() / 60.0 < min_minutes_between:
            continue
        if not should_enter(signal, straddle_threshold, term_threshold, trend_threshold, skew_threshold, trigger_mode):
            continue
        if structure == "atm_straddle":
            selected = choose_atm_straddle(by_ts[timestamp])
            if selected is None:
                continue
            call, put = selected
            legs = [(call, 1), (put, 1)]
        elif structure == "put_only":
            selected_put = choose_put_by_delta(by_ts[timestamp], put_target_delta)
            if selected_put is None:
                continue
            call = None
            put = selected_put
            legs = [(selected_put, 1)]
        elif structure == "put_spread":
            selected_spread = choose_put_spread(by_ts[timestamp], put_spread_long_delta, put_spread_short_delta)
            if selected_spread is None:
                continue
            long_put, short_put = selected_spread
            call = None
            put = long_put
            legs = [(long_put, 1), (short_put, -1)]
        else:
            raise ValueError(f"Unsupported long-vol structure: {structure}")

        debit = sum(leg.ask * qty if qty > 0 else -leg.bid * abs(qty) for leg, qty in legs)
        if debit <= 0:
            continue
        contracts = math.floor(min(budget_per_trade, budget_remaining) / (debit * 100))
        if contracts <= 0:
            continue
        budget_remaining -= contracts * debit * 100
        settlement = sum(intrinsic(leg.option_type, leg.strike, close_spot) * qty for leg, qty in legs)
        gross_pnl = (settlement - debit) * contracts * 100
        fees = sum(abs(qty) for _, qty in legs) * contracts * fee_per_contract
        call_contracts = sum(qty for leg, qty in legs if leg.option_type == "CALL") * contracts
        put_contracts = sum(qty for leg, qty in legs if leg.option_type == "PUT") * contracts
        short_put_strike = ""
        for leg, qty in legs:
            if leg.option_type == "PUT" and qty < 0:
                short_put_strike = leg.strike
        rows.append(
            {
                "date": trade_date,
                "entry_time": timestamp.isoformat(),
                "trigger_mode": trigger_mode,
                "structure": structure,
                "contracts": contracts,
                "call_strike": call.strike if call is not None else "",
                "put_strike": put.strike if put is not None else "",
                "short_put_strike": short_put_strike,
                "call_contracts": call_contracts,
                "put_contracts": put_contracts,
                "entry_debit": round(debit, 4),
                "close_spot": round(close_spot, 4),
                "gross_pnl": round(gross_pnl, 2),
                "fees": round(fees, 2),
                "net_pnl": round(gross_pnl - fees, 2),
                "trigger_straddle_residual_z": round(signal.straddle_residual_z, 6),
                "trigger_skew_z": round(signal.skew_z, 6),
                "trigger_term_ratio_z": round(signal.term_ratio_z, 6),
                "trigger_trend_score": round(signal.trend_score, 6),
            }
        )
        last_entry = timestamp

    net_pnl = sum(float(row["net_pnl"]) for row in rows)
    summary = {
        "date": trade_date,
        "long_vol_trades": len(rows),
        "daily_budget_pct": daily_budget_pct,
        "net_pnl": round(net_pnl, 2),
        "return_on_equity": round(net_pnl / account_equity, 8) if account_equity else 0.0,
        "budget_used": round(account_equity * daily_budget_pct - budget_remaining, 2),
    }
    return summary, rows


def write_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a configurable long-volatility overlay on processed ThetaData days.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--signals-filename", default="signals.csv")
    parser.add_argument("--account-equity", type=float, default=28_000_000)
    parser.add_argument("--daily-budget-pct", type=float, default=0.0005)
    parser.add_argument("--max-trades", type=int, default=2)
    parser.add_argument("--min-minutes-between", type=int, default=60)
    parser.add_argument("--straddle-threshold", type=float, default=1.25)
    parser.add_argument("--term-threshold", type=float, default=1.25)
    parser.add_argument("--trend-threshold", type=float, default=1.5)
    parser.add_argument("--skew-threshold", type=float, default=1.25)
    parser.add_argument("--trigger-mode", default="crash_hedge", choices=["any", "confluence", "crash_hedge"])
    parser.add_argument("--fee-per-contract", type=float, default=0.79)
    parser.add_argument("--structure", default="put_spread", choices=["atm_straddle", "put_only", "put_spread"])
    parser.add_argument("--put-target-delta", type=float, default=0.10)
    parser.add_argument("--put-spread-long-delta", type=float, default=0.15)
    parser.add_argument("--put-spread-short-delta", type=float, default=0.05)
    args = parser.parse_args()

    summaries: List[dict] = []
    trades: List[dict] = []
    for trade_date in args.dates:
        day_dir = Path(args.processed_dir) / f"symbol={args.symbol}" / f"date={trade_date}"
        summary, rows = run_overlay_day(
            quotes_path=day_dir / "normalized_option_quotes.csv",
            signals_path=day_dir / args.signals_filename,
            trade_date=trade_date,
            account_equity=args.account_equity,
            daily_budget_pct=args.daily_budget_pct,
            max_trades=args.max_trades,
            min_minutes_between=args.min_minutes_between,
            straddle_threshold=args.straddle_threshold,
            term_threshold=args.term_threshold,
            trend_threshold=args.trend_threshold,
            skew_threshold=args.skew_threshold,
            trigger_mode=args.trigger_mode,
            fee_per_contract=args.fee_per_contract,
            structure=args.structure,
            put_target_delta=args.put_target_delta,
            put_spread_long_delta=args.put_spread_long_delta,
            put_spread_short_delta=args.put_spread_short_delta,
        )
        summaries.append(summary)
        trades.extend(rows)
        print(f"{trade_date} long_vol_trades={summary['long_vol_trades']} net_pnl={summary['net_pnl']}")

    results_dir = Path(args.results_dir)
    write_csv(results_dir / "long_vol_daily_summary.csv", summaries)
    write_csv(results_dir / "long_vol_trades.csv", trades)
    print(f"wrote {results_dir / 'long_vol_daily_summary.csv'}")
    print(f"wrote {results_dir / 'long_vol_trades.csv'}")


if __name__ == "__main__":
    main()
