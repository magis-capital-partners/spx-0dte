"""Test 1: unconditional baseline — sell ~20Δ verticals every 15 min with gates off.

Measures raw per-trade expectancy, win rate, and deployment cadence without
signal scoring or regime filters. Structural rejects (min credit, width) remain.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Dict, List, Optional, Sequence

from historical_baselines import compute_baselines, processed_signal_path, read_csv, transform_rows, write_csv
from mbh_simulator import (
    DefaultSignalPolicy,
    SignalSnapshot,
    StrategyConfig,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    trade_margin,
    trades_to_rows,
    tranche_summaries_to_rows,
)
from regime_validation import apply_rolling_baseline, discover_dates

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "unconditional_baseline"
TRADING_DAYS = 252


class FixedSizePolicy(DefaultSignalPolicy):
    """Always deploy baseline_contracts — no VIX or danger scaling."""

    def contracts(self, signal: Optional[SignalSnapshot], config: StrategyConfig) -> int:
        return config.baseline_contracts


def build_unconditional_config(
    account_equity: float = 13_000_000.0,
    baseline_contracts: int = 31,
    stop_multiple: float = 2.0,
) -> StrategyConfig:
    """Config with all signal/score gates disabled; fixed size every tranche."""
    return StrategyConfig(
        account_equity=account_equity,
        baseline_contracts=baseline_contracts,
        stop_multiple=stop_multiple,
        target_long_abs_delta=0.08,
        daily_credit_cap_pct=0.50,
        daily_loss_limit_pct=0.50,
        flatten_on_daily_loss=False,
        require_positive_premium_richness=False,
        atm_surface_min_residual=-99.0,
        straddle_cheap_threshold=-99.0,
        skew_extreme_threshold=99.0,
        term_extreme_threshold=99.0,
        realized_extreme_threshold=99.0,
        danger_skip_threshold=99.0,
        danger_quarter_size_threshold=99.0,
        danger_half_size_threshold=99.0,
        hard_term_ratio_skip_threshold=99.0,
        hard_realized_skip_threshold=99.0,
        hard_trend_skip_threshold=99.0,
        use_candidate_engine=True,
        candidate_min_score=-999.0,
        candidate_half_score=-999.0,
        candidate_full_score=-999.0,
        candidate_max_sides=1,
        candidate_max_abs_term_ratio_z=99.0,
        candidate_max_abs_realized_z=99.0,
        candidate_max_adverse_trend=99.0,
        candidate_max_chase_trend=99.0,
        candidate_max_adverse_skew=99.0,
        use_two_tier_engine=False,
        use_time_of_day_controls=False,
        use_event_controls=False,
        use_condor_sleeve=False,
        use_one_dte_sleeve=False,
        use_trend_debit_sleeve=False,
        use_long_put_hedge_sleeve=False,
        use_portfolio_allocator=False,
        use_intraday_memory_gate=False,
        stop_cooldown_minutes=0,
        same_side_stop_cooldown_minutes=0,
        max_stops_per_side=999,
        max_open_trades_per_side=999,
        max_open_trades_same_side_strike=999,
        record_tranche_summaries=True,
    )


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


def trade_stats(trades: Sequence[dict]) -> dict:
    if not trades:
        return {
            "count": 0,
            "win_rate": 0.0,
            "stop_rate": 0.0,
            "expectancy_per_trade": 0.0,
            "median_pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_win_loss_ratio": 0.0,
            "total_net_pnl": 0.0,
        }

    pnls = [safe_float(row.get("net_pnl")) for row in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    stopped = sum(1 for row in trades if str(row.get("stopped")).lower() in {"true", "1"})

    avg_win = mean(wins) if wins else 0.0
    avg_loss = mean(losses) if losses else 0.0
    win_rate = len(wins) / len(pnls)
    wl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0.0

    return {
        "count": len(pnls),
        "win_rate": round(win_rate, 4),
        "stop_rate": round(stopped / len(pnls), 4) if pnls else 0.0,
        "expectancy_per_trade": round(mean(pnls), 2),
        "median_pnl": round(median(pnls), 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_win_loss_ratio": round(wl_ratio, 4),
        "total_net_pnl": round(sum(pnls), 2),
    }


def portfolio_stats(daily_rows: Sequence[dict], account_equity: float) -> dict:
    days = len(daily_rows)
    if days == 0:
        return {"days": 0}

    trades = sum(safe_int(row.get("trades")) for row in daily_rows)
    active_days = sum(1 for row in daily_rows if safe_int(row.get("trades")) > 0)
    daily_returns: List[float] = []
    equity = account_equity
    peak = account_equity
    max_drawdown = 0.0
    worst_day = 0.0

    for row in daily_rows:
        day_pnl = safe_float(row.get("net_pnl"))
        worst_day = min(worst_day, day_pnl)
        ret = day_pnl / equity if equity else 0.0
        daily_returns.append(ret)
        equity += day_pnl
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    total_return = (equity / account_equity) - 1.0
    years = days / TRADING_DAYS
    cagr = ((1.0 + total_return) ** (1.0 / years) - 1.0) if years > 0 and total_return > -1.0 else 0.0
    mean_daily = mean(daily_returns) if daily_returns else 0.0
    std_daily = pstdev(daily_returns) if len(daily_returns) > 1 else 0.0
    sharpe = (mean_daily / std_daily) * math.sqrt(TRADING_DAYS) if std_daily > 0 else 0.0

    tranches = sum(safe_int(row.get("tranches")) for row in daily_rows)
    executed_tranches = sum(safe_int(row.get("executed_tranches")) for row in daily_rows)

    return {
        "days": days,
        "trades": trades,
        "active_days": active_days,
        "active_day_pct": round(active_days / days, 4) if days else 0.0,
        "tranches": tranches,
        "executed_tranches": executed_tranches,
        "tranche_fill_rate": round(executed_tranches / tranches, 4) if tranches else 0.0,
        "trades_per_active_day": round(trades / active_days, 2) if active_days else 0.0,
        "net_pnl": round(equity - account_equity, 2),
        "cagr_pct": round(cagr * 100.0, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_drawdown * 100.0, 2),
        "worst_day": round(worst_day, 2),
        "day_win_rate": round(sum(1 for r in daily_returns if r > 0) / days, 4) if days else 0.0,
    }


def build_report(
    portfolio: dict,
    overall_trade: dict,
    by_side: Dict[str, dict],
    by_exit: Dict[str, dict],
    gate_counts: Counter,
    no_trade_reasons: Counter,
    config: StrategyConfig,
    oos_days: int,
) -> str:
    lines = [
        "# Unconditional Baseline — Test 1",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Configuration",
        "",
        f"- Account equity: ${config.account_equity:,.0f}",
        f"- Baseline contracts: {config.baseline_contracts}",
        f"- Stop multiple: {config.stop_multiple}x (short leg)",
        f"- Target short delta: {config.target_abs_delta} ({config.min_abs_delta}–{config.max_abs_delta})",
        f"- All signal/score gates: **disabled**",
        f"- Risk cooldowns / concentration limits: **disabled**",
        f"- OOS days (40-day warmup): {oos_days}",
        "",
        "## Portfolio (compounded)",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| OOS days | {portfolio.get('days', 0)} |",
        f"| Active days | {portfolio.get('active_days', 0)} ({portfolio.get('active_day_pct', 0)*100:.1f}%) |",
        f"| Total trades | {portfolio.get('trades', 0)} |",
        f"| Tranches / executed | {portfolio.get('tranches', 0)} / {portfolio.get('executed_tranches', 0)} ({portfolio.get('tranche_fill_rate', 0)*100:.1f}% fill) |",
        f"| Trades per active day | {portfolio.get('trades_per_active_day', 0)} |",
        f"| Net P&L | ${portfolio.get('net_pnl', 0):,.0f} |",
        f"| CAGR | {portfolio.get('cagr_pct', 0):.2f}% |",
        f"| Sharpe | {portfolio.get('sharpe', 0):.2f} |",
        f"| Max drawdown | {portfolio.get('max_drawdown_pct', 0):.2f}% |",
        f"| Worst day | ${portfolio.get('worst_day', 0):,.0f} |",
        f"| Day win rate | {portfolio.get('day_win_rate', 0)*100:.1f}% |",
        "",
        "## Per-Trade Stats (overall)",
        "",
        "| Metric | Value | MBH target |",
        "|---|---:|---:|",
        f"| Trades | {overall_trade.get('count', 0)} | — |",
        f"| **Win rate** | **{overall_trade.get('win_rate', 0)*100:.1f}%** | ~65% |",
        f"| Stop rate | {overall_trade.get('stop_rate', 0)*100:.1f}% | — |",
        f"| **Expectancy / trade** | **${overall_trade.get('expectancy_per_trade', 0):,.0f}** | > $0 |",
        f"| Median P&L | ${overall_trade.get('median_pnl', 0):,.0f} | — |",
        f"| Avg win | ${overall_trade.get('avg_win', 0):,.0f} | — |",
        f"| Avg loss | ${overall_trade.get('avg_loss', 0):,.0f} | — |",
        f"| Win/loss ratio | {overall_trade.get('avg_win_loss_ratio', 0):.2f}x | — |",
        "",
        "## By Side",
        "",
        "| Side | Trades | Win rate | Expectancy | Stop rate |",
        "|---|---:|---:|---:|---:|",
    ]

    for side in sorted(by_side):
        s = by_side[side]
        lines.append(
            f"| {side} | {s['count']} | {s['win_rate']*100:.1f}% | "
            f"${s['expectancy_per_trade']:,.0f} | {s['stop_rate']*100:.1f}% |"
        )

    lines.extend(
        [
            "",
            "## By Exit Reason",
            "",
            "| Exit reason | Trades | Win rate | Expectancy |",
            "|---|---:|---:|---:|",
        ]
    )
    for reason in sorted(by_exit, key=lambda k: by_exit[k]["count"], reverse=True):
        s = by_exit[reason]
        lines.append(
            f"| {reason} | {s['count']} | {s['win_rate']*100:.1f}% | ${s['expectancy_per_trade']:,.0f} |"
        )

    lines.extend(["", "## Why Tranches Did Not Execute (top gate/reject reasons)", ""])
    if gate_counts:
        lines.append("| Reason | Count |")
        lines.append("|---|---:|")
        for reason, count in gate_counts.most_common(15):
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("_No candidate diagnostics recorded._")

    lines.extend(["", "## No-Trade Tranche Skip Reasons", ""])
    if no_trade_reasons:
        lines.append("| Skip reason | Tranches |")
        lines.append("|---|---:|")
        for reason, count in no_trade_reasons.most_common(10):
            lines.append(f"| {reason} | {count} |")

    lines.extend(
        [
            "",
            "## Interpretation Guide",
            "",
            "- Win rate ≈ 65% but negative expectancy → **stop/exit problem** (losses too large per stop)",
            "- Win rate < 55% → **structure/strike problem** (delta selection or side choice)",
            "- Win rate ≈ 65% and positive expectancy → **deployment/sizing problem** (scale up)",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Test 1 unconditional baseline backtest.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--signals-filename", default="signals_unconditional.csv")
    parser.add_argument("--account-equity", type=float, default=13_000_000.0)
    parser.add_argument("--baseline-contracts", type=int, default=31)
    parser.add_argument("--stop-multiple", type=float, default=2.0)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--max-days", type=int, default=0, help="Limit OOS days (0 = all).")
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    dates = discover_dates(processed_dir, args.symbol)
    if args.start_date:
        dates = [d for d in dates if d >= args.start_date]
    if args.end_date:
        dates = [d for d in dates if d <= args.end_date]
    if len(dates) <= args.train_count:
        raise SystemExit(f"Need more than {args.train_count} dates; have {len(dates)}.")

    config = build_unconditional_config(
        account_equity=args.account_equity,
        baseline_contracts=args.baseline_contracts,
        stop_multiple=args.stop_multiple,
    )
    policy = FixedSizePolicy()

    daily_rows: List[dict] = []
    trade_rows: List[dict] = []
    tranche_rows: List[dict] = []
    gate_counts: Counter = Counter()
    no_trade_reasons: Counter = Counter()

    oos_end = len(dates) if args.max_days <= 0 else min(len(dates), args.train_count + args.max_days)
    for index in range(args.train_count, oos_end):
        test_date = dates[index]
        train_dates = dates[index - args.train_count : index]
        apply_rolling_baseline(processed_dir, args.symbol, train_dates, test_date, args.signals_filename)

        day_dir = processed_dir / f"symbol={args.symbol}" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / args.signals_filename)
        result = simulate_day(quotes, signals, config=config, policy=policy)

        day_trades = trades_to_rows(result.trades)
        for row in day_trades:
            row["date"] = test_date
            trade_rows.append(row)

        day_tranches = tranche_summaries_to_rows(result.tranche_summaries)
        executed_tranches = 0
        for row in day_tranches:
            row["date"] = test_date
            tranche_rows.append(row)
            if safe_int(row.get("candidates_executed")) > 0:
                executed_tranches += 1
            elif row.get("skip_reason"):
                no_trade_reasons[row["skip_reason"]] += 1
            elif safe_int(row.get("candidates_executed")) == 0:
                no_trade_reasons["no_trade"] += 1

        for record in result.candidate_records:
            if record.status in {"gated", "rejected", "risk_blocked", "blocked"}:
                gate_counts[f"{record.status}:{record.reason}"] += 1

        max_margin = sum(trade_margin(trade, config) for trade in result.trades)
        daily_rows.append(
            {
                "date": test_date,
                "trades": len(result.trades),
                "stopped_trades": sum(1 for t in result.trades if t.stopped),
                "tranches": len(day_tranches),
                "executed_tranches": executed_tranches,
                "gross_credit_sold": round(result.gross_credit_sold, 2),
                "net_pnl": round(result.net_pnl, 2),
                "return_on_equity": round(result.return_on_equity, 8),
                "halted": result.halted,
                "approx_spread_margin": round(max_margin, 2),
            }
        )
        if (index - args.train_count + 1) % 50 == 0:
            print(f"  {index - args.train_count + 1} OOS days done ({test_date})")

    oos_days = len(daily_rows)
    print(f"Completed {oos_days} OOS days, {len(trade_rows)} trades")

    write_csv(results_dir / "daily_summary.csv", daily_rows)
    write_csv(results_dir / "trades.csv", trade_rows)
    write_csv(results_dir / "tranches.csv", tranche_rows)

    overall = trade_stats(trade_rows)
    by_side = {
        side: trade_stats([row for row in trade_rows if row.get("side") == side])
        for side in sorted({row.get("side", "") for row in trade_rows})
    }
    by_exit = {
        reason: trade_stats([row for row in trade_rows if row.get("exit_reason") == reason])
        for reason in sorted({row.get("exit_reason", "") for row in trade_rows})
    }
    portfolio = portfolio_stats(daily_rows, config.account_equity)

    summary = {
        "config": {
            "account_equity": config.account_equity,
            "baseline_contracts": config.baseline_contracts,
            "stop_multiple": config.stop_multiple,
            "oos_days": oos_days,
        },
        "portfolio": portfolio,
        "trade_stats": overall,
        "by_side": by_side,
        "by_exit": by_exit,
        "top_gate_reasons": gate_counts.most_common(20),
        "top_skip_reasons": no_trade_reasons.most_common(10),
    }
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    report = build_report(
        portfolio, overall, by_side, by_exit, gate_counts, no_trade_reasons, config, oos_days
    )
    report_path = ROOT / f"unconditional_baseline_results_{datetime.now().strftime('%Y-%m-%d')}.md"
    report_path.write_text(report, encoding="utf-8")
    (results_dir / "report.md").write_text(report, encoding="utf-8")

    print()
    print("=== Test 1: Unconditional Baseline ===")
    print(f"  OOS days: {oos_days}  |  Trades: {overall['count']}  |  Active days: {portfolio.get('active_day_pct', 0)*100:.1f}%")
    print(f"  Win rate: {overall['win_rate']*100:.1f}%  |  Stop rate: {overall['stop_rate']*100:.1f}%")
    print(f"  Expectancy/trade: ${overall['expectancy_per_trade']:,.0f}  |  Avg win: ${overall['avg_win']:,.0f}  |  Avg loss: ${overall['avg_loss']:,.0f}")
    print(f"  CAGR: {portfolio.get('cagr_pct', 0):.2f}%  |  Sharpe: {portfolio.get('sharpe', 0):.2f}")
    print(f"  Wrote {results_dir}")
    print(f"  Wrote {report_path}")


if __name__ == "__main__":
    main()
