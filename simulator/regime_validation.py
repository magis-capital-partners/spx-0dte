from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import time
from pathlib import Path
from statistics import mean
from typing import Iterable, List

from historical_baselines import compute_baselines, processed_signal_path, read_csv, transform_rows, write_csv
from mbh_simulator import (
    StrategyConfig,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    stop_diagnostics_to_rows,
    trade_margin,
    trades_to_rows,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "regime_validation"
DEFAULT_EVENT_CALENDAR = ROOT / "regime_expansion_dates_2025.csv"


def parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def discover_dates(processed_dir: Path, symbol: str) -> List[str]:
    root = processed_dir / f"symbol={symbol}"
    if not root.exists():
        return []
    dates = []
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("date=") and (path / "signals.csv").exists():
            dates.append(path.name.split("=", 1)[1])
    return sorted(dates)


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def apply_rolling_baseline(
    processed_dir: Path,
    symbol: str,
    train_dates: Iterable[str],
    test_date: str,
    output_filename: str,
) -> None:
    baselines = compute_baselines(processed_dir, symbol, train_dates)
    rows = read_csv(processed_signal_path(processed_dir, symbol, test_date))
    output_path = processed_signal_path(processed_dir, symbol, test_date, output_filename)
    write_csv(output_path, transform_rows(rows, baselines))


def classify_regime(signals) -> tuple[str, dict]:
    if not signals:
        return "missing_signals", {}

    straddle = [signal.straddle_residual_z for signal in signals]
    skew_abs = [abs(signal.skew_z) for signal in signals]
    term_abs = [abs(signal.term_ratio_z) for signal in signals]
    trend_abs = [abs(signal.trend_score) for signal in signals]
    realized_abs = [abs(signal.realized_vs_implied_z) for signal in signals]
    trend_mean = mean(signal.trend_score for signal in signals)

    metrics = {
        "mean_straddle_residual_z": mean(straddle),
        "max_abs_skew_z": max(skew_abs),
        "max_abs_term_ratio_z": max(term_abs),
        "max_abs_trend_score": max(trend_abs),
        "mean_trend_score": trend_mean,
        "max_abs_realized_vs_implied_z": max(realized_abs),
    }

    labels = []
    if metrics["mean_straddle_residual_z"] >= 0.5:
        labels.append("rich_premium")
    if metrics["mean_straddle_residual_z"] < 0.0:
        labels.append("cheap_premium")
    if metrics["max_abs_skew_z"] >= 1.5:
        labels.append("skew_dislocated")
    if metrics["max_abs_term_ratio_z"] >= 1.5:
        labels.append("term_dislocated")
    if metrics["max_abs_trend_score"] >= 2.0:
        labels.append("trend_extreme")
    if metrics["max_abs_realized_vs_implied_z"] >= 1.5:
        labels.append("realized_shock")
    if trend_mean <= -0.5:
        labels.append("downtrend_bias")
    if trend_mean >= 0.5:
        labels.append("uptrend_bias")
    if not labels:
        labels.append("quiet_mixed")
    return "|".join(labels), metrics


def write_result_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def read_event_calendar(path: Path) -> dict:
    if not path.exists():
        return {}
    events = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            event_date = row.get("date", "")
            if not event_date:
                continue
            events[event_date] = {
                "event_bucket": row.get("bucket", "unlabeled") or "unlabeled",
                "event_note": row.get("note", "") or "",
            }
    return events


def summarize(rows: List[dict], regime_name: str) -> dict:
    days = len(rows)
    trades = sum(int(row["trades"]) for row in rows)
    stopped = sum(int(row["stopped_trades"]) for row in rows)
    pnl = sum(float(row["net_pnl"]) for row in rows)
    return_sum = sum(float(row["return_on_equity"]) for row in rows)
    return {
        "regime": regime_name,
        "days": days,
        "total_net_pnl": round(pnl, 2),
        "total_return": round(return_sum, 8),
        "mean_daily_return": round(return_sum / days, 8) if days else 0.0,
        "trades": trades,
        "stopped_trades": stopped,
        "stop_rate": round(stopped / trades, 6) if trades else 0.0,
        "positive_days": sum(1 for row in rows if float(row["net_pnl"]) > 0),
        "no_trade_days": sum(1 for row in rows if int(row["trades"]) == 0),
        "halted_days": sum(1 for row in rows if str(row["halted"]) == "True"),
        "worst_day": round(min((float(row["net_pnl"]) for row in rows), default=0.0), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run rolling no-lookahead candidate-engine validation by day regime.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--dates", nargs="*", help="Optional explicit ordered dates. Defaults to processed dates.")
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--event-calendar", default=str(DEFAULT_EVENT_CALENDAR))
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--signals-filename", default="signals_regime_validation.csv")
    parser.add_argument("--account-equity", type=float, default=13_000_000)
    parser.add_argument("--baseline-contracts", type=int, default=31)
    parser.add_argument("--daily-credit-cap-pct", type=float, default=0.015)
    parser.add_argument("--daily-loss-limit-pct", type=float, default=0.0225)
    parser.add_argument("--flatten-on-daily-loss", action="store_true")
    parser.add_argument("--flatten-loss-limit-pct", type=float, default=0.0,
                        help="Deeper flatten trigger; 0 = same as daily-loss-limit-pct.")
    parser.add_argument("--stop-multiple", type=float, default=2.5)
    parser.add_argument("--target-long-abs-delta", type=float, default=0.08)
    parser.add_argument("--candidate-min-score", type=float, default=2.50)
    parser.add_argument("--candidate-max-sides", type=int, default=1)
    parser.add_argument("--two-tier-engine", action="store_true")
    parser.add_argument("--exploratory-min-score", type=float, default=2.25)
    parser.add_argument("--exploratory-max-score", type=float, default=2.40)
    parser.add_argument("--exploratory-size-fraction", type=float, default=0.15)
    parser.add_argument("--exploratory-max-sides", type=int, default=1)
    parser.add_argument("--exploratory-same-side-cluster-points", type=float, default=25.0)
    parser.add_argument("--disable-exploratory-bear-call-guard", action="store_true")
    parser.add_argument("--exploratory-bear-call-min-score", type=float, default=2.40)
    parser.add_argument("--exploratory-bear-call-min-distance-pct", type=float, default=0.0065)
    parser.add_argument("--time-of-day-controls", action="store_true")
    parser.add_argument("--early-entry-min-score", type=float, default=2.75)
    parser.add_argument("--late-core-min-score", type=float, default=2.75)
    parser.add_argument("--final-hour-min-distance-pct", type=float, default=0.006)
    parser.add_argument("--event-controls", action="store_true")
    parser.add_argument("--event-shock-buckets", default="tariff_shock,tariff_reversal")
    parser.add_argument("--scheduled-macro-buckets", default="cpi_event,fomc_event,nfp_event")
    parser.add_argument("--scheduled-macro-exploratory-min-score", type=float, default=2.35)
    parser.add_argument("--allow-exploratory-rich-term-days", action="store_true")
    parser.add_argument("--condor-sleeve", action="store_true")
    parser.add_argument("--condor-size-fraction", type=float, default=0.15)
    parser.add_argument("--condor-min-score", type=float, default=2.30)
    parser.add_argument("--condor-target-abs-delta", type=float, default=0.12)
    parser.add_argument("--condor-min-abs-delta", type=float, default=0.08)
    parser.add_argument("--condor-max-abs-delta", type=float, default=0.16)
    parser.add_argument("--condor-min-straddle-residual-z", type=float, default=0.50)
    parser.add_argument("--condor-max-abs-trend-score", type=float, default=1.00)
    parser.add_argument("--condor-max-abs-skew-z", type=float, default=1.25)
    parser.add_argument("--condor-max-abs-term-ratio-z", type=float, default=1.25)
    parser.add_argument("--condor-max-abs-realized-z", type=float, default=1.50)
    parser.add_argument("--condor-allowed-event-buckets", default="")
    parser.add_argument("--condor-block-event-buckets", default="tariff_shock,tariff_reversal,fomc_event")
    parser.add_argument("--one-dte-sleeve", action="store_true")
    parser.add_argument("--one-dte-size-fraction", type=float, default=0.10)
    parser.add_argument("--one-dte-min-score", type=float, default=2.50)
    parser.add_argument("--one-dte-target-abs-delta", type=float, default=0.12)
    parser.add_argument("--one-dte-min-abs-delta", type=float, default=0.08)
    parser.add_argument("--one-dte-max-abs-delta", type=float, default=0.16)
    parser.add_argument("--one-dte-min-straddle-residual-z", type=float, default=0.50)
    parser.add_argument("--one-dte-max-abs-trend-score", type=float, default=0.75)
    parser.add_argument("--one-dte-max-abs-skew-z", type=float, default=1.25)
    parser.add_argument("--one-dte-max-abs-term-ratio-z", type=float, default=1.25)
    parser.add_argument("--one-dte-max-abs-realized-z", type=float, default=1.50)
    parser.add_argument("--one-dte-entry-start", default="10:00")
    parser.add_argument("--one-dte-entry-end", default="14:30")
    parser.add_argument("--one-dte-allowed-event-buckets", default="")
    parser.add_argument("--one-dte-block-event-buckets", default="tariff_shock,tariff_reversal,fomc_event")
    parser.add_argument("--portfolio-allocator", action="store_true")
    parser.add_argument("--portfolio-margin-budget-pct", type=float, default=0.40)
    parser.add_argument("--core-margin-budget-pct", type=float, default=0.35)
    parser.add_argument("--exploratory-margin-budget-pct", type=float, default=0.02)
    parser.add_argument("--condor-margin-budget-pct", type=float, default=0.03)
    parser.add_argument("--one-dte-margin-budget-pct", type=float, default=0.0)
    parser.add_argument("--trend-debit-sleeve", action="store_true")
    parser.add_argument("--trend-debit-size-fraction", type=float, default=0.10)
    parser.add_argument("--trend-debit-min-abs-trend-score", type=float, default=1.75)
    parser.add_argument("--trend-debit-min-entry-time", default="10:00")
    parser.add_argument("--trend-debit-max-entry-time", default="14:30")
    parser.add_argument("--trend-debit-margin-budget-pct", type=float, default=0.03)
    parser.add_argument("--long-put-hedge-sleeve", action="store_true")
    parser.add_argument("--long-put-hedge-size-fraction", type=float, default=0.08)
    parser.add_argument("--long-put-hedge-min-downtrend-score", type=float, default=1.25)
    parser.add_argument("--long-put-hedge-min-realized-z", type=float, default=1.25)
    parser.add_argument("--long-put-hedge-margin-budget-pct", type=float, default=0.02)
    parser.add_argument("--max-open-trades-per-side", type=int, default=2)
    parser.add_argument("--max-open-trades-same-side-strike", type=int, default=1)
    parser.add_argument("--stop-cooldown-minutes", type=int, default=30)
    parser.add_argument("--same-side-stop-cooldown-minutes", type=int, default=120)
    parser.add_argument("--max-stops-per-side", type=int, default=2)
    parser.add_argument("--disable-intraday-memory-gate", action="store_true")
    parser.add_argument("--memory-term-ratio-skip-threshold", type=float, default=1.50)
    parser.add_argument("--memory-skew-skip-threshold", type=float, default=99.0)
    parser.add_argument("--memory-trend-skip-threshold", type=float, default=99.0)
    parser.add_argument("--candidate-max-chase-trend", type=float, default=1.50)
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)
    dates = sorted(args.dates) if args.dates else discover_dates(processed_dir, args.symbol)
    if args.start_date:
        dates = [date for date in dates if date >= args.start_date]
    if args.end_date:
        dates = [date for date in dates if date <= args.end_date]
    if len(dates) <= args.train_count:
        raise SystemExit("Not enough processed dates after filtering for the requested train-count.")
    event_calendar = read_event_calendar(Path(args.event_calendar))

    config = StrategyConfig(
        account_equity=args.account_equity,
        baseline_contracts=args.baseline_contracts,
        daily_credit_cap_pct=args.daily_credit_cap_pct,
        daily_loss_limit_pct=args.daily_loss_limit_pct,
        flatten_on_daily_loss=args.flatten_on_daily_loss,
        flatten_loss_limit_pct=args.flatten_loss_limit_pct,
        stop_multiple=args.stop_multiple,
        target_long_abs_delta=args.target_long_abs_delta,
        candidate_min_score=args.candidate_min_score,
        candidate_max_sides=args.candidate_max_sides,
        use_two_tier_engine=args.two_tier_engine,
        exploratory_min_score=args.exploratory_min_score,
        exploratory_max_score=args.exploratory_max_score,
        exploratory_size_fraction=args.exploratory_size_fraction,
        exploratory_max_sides=args.exploratory_max_sides,
        exploratory_same_side_cluster_points=args.exploratory_same_side_cluster_points,
        use_exploratory_bear_call_guard=not args.disable_exploratory_bear_call_guard,
        exploratory_bear_call_min_score=args.exploratory_bear_call_min_score,
        exploratory_bear_call_min_distance_pct=args.exploratory_bear_call_min_distance_pct,
        use_time_of_day_controls=args.time_of_day_controls,
        early_entry_min_score=args.early_entry_min_score,
        late_core_min_score=args.late_core_min_score,
        final_hour_min_distance_pct=args.final_hour_min_distance_pct,
        use_event_controls=args.event_controls,
        event_shock_buckets=args.event_shock_buckets,
        scheduled_macro_buckets=args.scheduled_macro_buckets,
        scheduled_macro_exploratory_min_score=args.scheduled_macro_exploratory_min_score,
        block_exploratory_rich_term_days=not args.allow_exploratory_rich_term_days,
        use_condor_sleeve=args.condor_sleeve,
        condor_size_fraction=args.condor_size_fraction,
        condor_min_score=args.condor_min_score,
        condor_target_abs_delta=args.condor_target_abs_delta,
        condor_min_abs_delta=args.condor_min_abs_delta,
        condor_max_abs_delta=args.condor_max_abs_delta,
        condor_min_straddle_residual_z=args.condor_min_straddle_residual_z,
        condor_max_abs_trend_score=args.condor_max_abs_trend_score,
        condor_max_abs_skew_z=args.condor_max_abs_skew_z,
        condor_max_abs_term_ratio_z=args.condor_max_abs_term_ratio_z,
        condor_max_abs_realized_z=args.condor_max_abs_realized_z,
        condor_allowed_event_buckets=args.condor_allowed_event_buckets,
        condor_block_event_buckets=args.condor_block_event_buckets,
        use_one_dte_sleeve=args.one_dte_sleeve,
        one_dte_size_fraction=args.one_dte_size_fraction,
        one_dte_min_score=args.one_dte_min_score,
        one_dte_target_abs_delta=args.one_dte_target_abs_delta,
        one_dte_min_abs_delta=args.one_dte_min_abs_delta,
        one_dte_max_abs_delta=args.one_dte_max_abs_delta,
        one_dte_min_straddle_residual_z=args.one_dte_min_straddle_residual_z,
        one_dte_max_abs_trend_score=args.one_dte_max_abs_trend_score,
        one_dte_max_abs_skew_z=args.one_dte_max_abs_skew_z,
        one_dte_max_abs_term_ratio_z=args.one_dte_max_abs_term_ratio_z,
        one_dte_max_abs_realized_z=args.one_dte_max_abs_realized_z,
        one_dte_entry_start=parse_time(args.one_dte_entry_start),
        one_dte_entry_end=parse_time(args.one_dte_entry_end),
        one_dte_allowed_event_buckets=args.one_dte_allowed_event_buckets,
        one_dte_block_event_buckets=args.one_dte_block_event_buckets,
        use_portfolio_allocator=args.portfolio_allocator,
        portfolio_margin_budget_pct=args.portfolio_margin_budget_pct,
        core_margin_budget_pct=args.core_margin_budget_pct,
        exploratory_margin_budget_pct=args.exploratory_margin_budget_pct,
        condor_margin_budget_pct=args.condor_margin_budget_pct,
        one_dte_margin_budget_pct=args.one_dte_margin_budget_pct,
        trend_debit_margin_budget_pct=args.trend_debit_margin_budget_pct,
        long_put_hedge_margin_budget_pct=args.long_put_hedge_margin_budget_pct,
        use_trend_debit_sleeve=args.trend_debit_sleeve,
        trend_debit_size_fraction=args.trend_debit_size_fraction,
        trend_debit_min_abs_trend_score=args.trend_debit_min_abs_trend_score,
        trend_debit_min_entry_time=parse_time(args.trend_debit_min_entry_time),
        trend_debit_max_entry_time=parse_time(args.trend_debit_max_entry_time),
        use_long_put_hedge_sleeve=args.long_put_hedge_sleeve,
        long_put_hedge_size_fraction=args.long_put_hedge_size_fraction,
        long_put_hedge_min_downtrend_score=args.long_put_hedge_min_downtrend_score,
        long_put_hedge_min_realized_z=args.long_put_hedge_min_realized_z,
        max_open_trades_per_side=args.max_open_trades_per_side,
        max_open_trades_same_side_strike=args.max_open_trades_same_side_strike,
        stop_cooldown_minutes=args.stop_cooldown_minutes,
        same_side_stop_cooldown_minutes=args.same_side_stop_cooldown_minutes,
        max_stops_per_side=args.max_stops_per_side,
        use_intraday_memory_gate=not args.disable_intraday_memory_gate,
        memory_term_ratio_skip_threshold=args.memory_term_ratio_skip_threshold,
        memory_skew_skip_threshold=args.memory_skew_skip_threshold,
        memory_trend_skip_threshold=args.memory_trend_skip_threshold,
        candidate_max_chase_trend=args.candidate_max_chase_trend,
    )

    daily_rows: List[dict] = []
    trade_rows: List[dict] = []
    stop_rows: List[dict] = []
    candidate_reason_rows: List[dict] = []

    for index in range(args.train_count, len(dates)):
        test_date = dates[index]
        train_dates = dates[index - args.train_count : index]
        apply_rolling_baseline(processed_dir, args.symbol, train_dates, test_date, args.signals_filename)

        day_dir = processed_dir / f"symbol={args.symbol}" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / args.signals_filename)
        regime, regime_metrics = classify_regime(signals)
        event_info = event_calendar.get(test_date, {"event_bucket": "unlabeled", "event_note": ""})
        day_config = replace(config, event_bucket=event_info["event_bucket"])
        result = simulate_day(quotes, signals, config=day_config)

        day_trade_rows = trades_to_rows(result.trades)
        for row in day_trade_rows:
            row["date"] = test_date
            trade_rows.append(row)
        day_stop_rows = stop_diagnostics_to_rows(result.trades)
        for row in day_stop_rows:
            row["date"] = test_date
            stop_rows.append(row)

        reason_counts = Counter((record.status, record.reason, record.sleeve) for record in result.candidate_records)
        for (status, reason, sleeve), count in reason_counts.items():
            candidate_reason_rows.append({"date": test_date, "status": status, "reason": reason, "sleeve": sleeve, "count": count})

        core_trades = [trade for trade in result.trades if trade.model == "candidate_core"]
        exploratory_trades = [trade for trade in result.trades if trade.model == "candidate_exploratory"]
        condor_trades = [trade for trade in result.trades if trade.model == "candidate_condor"]
        one_dte_trades = [trade for trade in result.trades if trade.model == "candidate_one_dte"]
        trend_debit_trades = [trade for trade in result.trades if trade.model == "candidate_trend_debit"]
        long_put_hedge_trades = [trade for trade in result.trades if trade.model == "candidate_long_put_hedge"]
        max_spread_risk = sum(trade_margin(trade, day_config) for trade in result.trades)
        core_margin = sum(trade_margin(trade, day_config) for trade in core_trades)
        exploratory_margin = sum(trade_margin(trade, day_config) for trade in exploratory_trades)
        condor_margin = sum(trade_margin(trade, day_config) for trade in condor_trades)
        one_dte_margin = sum(trade_margin(trade, day_config) for trade in one_dte_trades)
        trend_debit_margin = sum(trade_margin(trade, day_config) for trade in trend_debit_trades)
        long_put_hedge_margin = sum(trade_margin(trade, day_config) for trade in long_put_hedge_trades)

        daily_rows.append(
            {
                "date": test_date,
                "train_start": train_dates[0],
                "train_end": train_dates[-1],
                "event_bucket": event_info["event_bucket"],
                "event_note": event_info["event_note"],
                "regime": regime,
                "trades": len(result.trades),
                "core_trades": len(core_trades),
                "exploratory_trades": len(exploratory_trades),
                "condor_trades": len(condor_trades),
                "one_dte_trades": len(one_dte_trades),
                "trend_debit_trades": len(trend_debit_trades),
                "long_put_hedge_trades": len(long_put_hedge_trades),
                "stopped_trades": sum(1 for trade in result.trades if trade.stopped),
                "core_stopped_trades": sum(1 for trade in core_trades if trade.stopped),
                "exploratory_stopped_trades": sum(1 for trade in exploratory_trades if trade.stopped),
                "condor_stopped_trades": sum(1 for trade in condor_trades if trade.stopped),
                "one_dte_stopped_trades": sum(1 for trade in one_dte_trades if trade.stopped),
                "trend_debit_stopped_trades": sum(1 for trade in trend_debit_trades if trade.stopped),
                "long_put_hedge_stopped_trades": sum(1 for trade in long_put_hedge_trades if trade.stopped),
                "gross_credit_sold": round(result.gross_credit_sold, 2),
                "gross_credit_pct_equity": round(result.gross_credit_sold / day_config.account_equity, 8),
                "approx_spread_margin": round(max_spread_risk, 2),
                "approx_spread_margin_pct_equity": round(max_spread_risk / day_config.account_equity, 8),
                "core_approx_spread_margin": round(core_margin, 2),
                "exploratory_approx_spread_margin": round(exploratory_margin, 2),
                "condor_approx_spread_margin": round(condor_margin, 2),
                "one_dte_approx_spread_margin": round(one_dte_margin, 2),
                "trend_debit_approx_spread_margin": round(trend_debit_margin, 2),
                "long_put_hedge_approx_spread_margin": round(long_put_hedge_margin, 2),
                "core_credit_sold": round(sum(trade.entry_credit * trade.contracts * day_config.multiplier for trade in core_trades), 2),
                "exploratory_credit_sold": round(sum(trade.entry_credit * trade.contracts * day_config.multiplier for trade in exploratory_trades), 2),
                "condor_credit_sold": round(sum(trade.entry_credit * trade.contracts * day_config.multiplier for trade in condor_trades), 2),
                "one_dte_credit_sold": round(sum(trade.entry_credit * trade.contracts * day_config.multiplier for trade in one_dte_trades), 2),
                "trend_debit_net_debit": round(sum(abs(trade.entry_credit) * trade.contracts * day_config.multiplier for trade in trend_debit_trades), 2),
                "long_put_hedge_net_debit": round(sum(abs(trade.entry_credit) * trade.contracts * day_config.multiplier for trade in long_put_hedge_trades), 2),
                "gross_pnl": round(result.gross_pnl, 2),
                "fees": round(result.fees, 2),
                "net_pnl": round(result.net_pnl, 2),
                "core_net_pnl": round(sum(trade.net_pnl for trade in core_trades), 2),
                "exploratory_net_pnl": round(sum(trade.net_pnl for trade in exploratory_trades), 2),
                "condor_net_pnl": round(sum(trade.net_pnl for trade in condor_trades), 2),
                "one_dte_net_pnl": round(sum(trade.net_pnl for trade in one_dte_trades), 2),
                "trend_debit_net_pnl": round(sum(trade.net_pnl for trade in trend_debit_trades), 2),
                "long_put_hedge_net_pnl": round(sum(trade.net_pnl for trade in long_put_hedge_trades), 2),
                "return_on_equity": round(result.return_on_equity, 8),
                "halted": result.halted,
                "halt_time": result.halt_time.isoformat() if result.halt_time else "",
                **{key: round(value, 6) for key, value in regime_metrics.items()},
            }
        )
        print(f"{test_date} regime={regime} trades={len(result.trades)} net_pnl={round(result.net_pnl, 2)}")

    regime_groups: defaultdict[str, List[dict]] = defaultdict(list)
    event_groups: defaultdict[str, List[dict]] = defaultdict(list)
    for row in daily_rows:
        regime_groups["all"].append(row)
        event_groups["all"].append(row)
        event_groups[str(row.get("event_bucket") or "unlabeled")].append(row)
        for label in str(row["regime"]).split("|"):
            regime_groups[label].append(row)
    regime_rows = [summarize(rows, regime) for regime, rows in sorted(regime_groups.items())]
    regime_rows.sort(key=lambda row: (row["regime"] != "all", row["regime"]))
    event_rows = [summarize(rows, event_bucket) for event_bucket, rows in sorted(event_groups.items())]
    for row in event_rows:
        row["event_bucket"] = row.pop("regime")
    event_rows.sort(key=lambda row: (row["event_bucket"] != "all", row["event_bucket"]))

    results_dir = Path(args.results_dir)
    write_result_csv(results_dir / "daily_regime_validation.csv", daily_rows)
    write_result_csv(results_dir / "regime_summary.csv", regime_rows)
    write_result_csv(results_dir / "event_summary.csv", event_rows)
    write_result_csv(results_dir / "trades.csv", trade_rows)
    write_result_csv(results_dir / "stop_diagnostics.csv", stop_rows)
    write_result_csv(results_dir / "candidate_reason_summary.csv", candidate_reason_rows)
    print(f"wrote {results_dir / 'daily_regime_validation.csv'}")
    print(f"wrote {results_dir / 'regime_summary.csv'}")
    print(f"wrote {results_dir / 'event_summary.csv'}")


if __name__ == "__main__":
    main()
