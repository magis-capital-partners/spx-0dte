"""Test 2: stop + structure matrix on unconditional cadence (391 OOS days).

Variants:
  - Stop multiples: none, 2.0x, 2.5x, 3.0x
  - Wide asymmetric wings: put 200pt / call 75pt (MBH snapshot shape)
  - Net-long overlay: put 1.5x / call 1.8x long-to-short ratio
  - Combined MBH-like: wide wings + net-long + no stop
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, List, Sequence

from historical_baselines import write_csv
from mbh_simulator import (
    StrategyConfig,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    trades_to_rows,
)
from regime_validation import apply_rolling_baseline, discover_dates
from unconditional_baseline import FixedSizePolicy, build_unconditional_config, trade_stats

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "stop_structure_matrix"
TRADING_DAYS = 252

VARIANTS: Dict[str, dict] = {
    "stop_2.0_baseline": {
        "description": "2.0x short-leg stop, default wings (Test 1 repeat)",
        "stop_multiple": 2.0,
        "use_short_leg_stops": True,
    },
    "stop_2.5": {
        "description": "2.5x short-leg stop, default wings",
        "stop_multiple": 2.5,
        "use_short_leg_stops": True,
    },
    "stop_3.0": {
        "description": "3.0x short-leg stop, default wings",
        "stop_multiple": 3.0,
        "use_short_leg_stops": True,
    },
    "no_stop": {
        "description": "No per-trade stop — hold defined-risk to settlement",
        "use_short_leg_stops": False,
    },
    "wide_wings_stop_2.0": {
        "description": "Put wing 200pt / call wing 75pt, 2.0x stop",
        "stop_multiple": 2.0,
        "use_short_leg_stops": True,
        "put_wing_width": 200.0,
        "call_wing_width": 75.0,
        "wing_selection_mode": "fixed_width",
        "max_wing_width": 400.0,
    },
    "wide_wings_no_stop": {
        "description": "Put wing 200pt / call wing 75pt, no stop",
        "use_short_leg_stops": False,
        "put_wing_width": 200.0,
        "call_wing_width": 75.0,
        "wing_selection_mode": "fixed_width",
        "max_wing_width": 400.0,
    },
    "net_long_no_stop": {
        "description": "Net-long overlay (1.5x put / 1.8x call), no stop",
        "use_short_leg_stops": False,
        "use_net_long_overlay": True,
        "put_long_overlay_ratio": 1.5,
        "call_long_overlay_ratio": 1.8,
        "overlay_wing_extra_width": 50.0,
    },
    "net_long_stop_2.0": {
        "description": "Net-long overlay (1.5x put / 1.8x call), 2.0x stop",
        "stop_multiple": 2.0,
        "use_short_leg_stops": True,
        "use_net_long_overlay": True,
        "put_long_overlay_ratio": 1.5,
        "call_long_overlay_ratio": 1.8,
        "overlay_wing_extra_width": 50.0,
    },
    "mbh_like": {
        "description": "Wide wings + net-long overlay + no stop (full MBH structure hypothesis)",
        "use_short_leg_stops": False,
        "put_wing_width": 200.0,
        "call_wing_width": 75.0,
        "wing_selection_mode": "fixed_width",
        "max_wing_width": 400.0,
        "use_net_long_overlay": True,
        "put_long_overlay_ratio": 1.5,
        "call_long_overlay_ratio": 1.8,
        "overlay_wing_extra_width": 50.0,
    },
}


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def portfolio_stats(daily_rows: Sequence[dict], account_equity: float) -> dict:
    days = len(daily_rows)
    if days == 0:
        return {"days": 0}

    trades = sum(int(row["trades"]) for row in daily_rows)
    spread_trades = sum(int(row.get("spread_trades", row["trades"])) for row in daily_rows)
    overlay_trades = sum(int(row.get("overlay_trades", 0)) for row in daily_rows)
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

    return {
        "days": days,
        "trades": trades,
        "spread_trades": spread_trades,
        "overlay_trades": overlay_trades,
        "net_pnl": round(equity - account_equity, 2),
        "cagr_pct": round(cagr * 100.0, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_drawdown * 100.0, 2),
        "worst_day": round(worst_day, 2),
        "worst_day_pct": round(worst_day / account_equity * 100.0, 2) if account_equity else 0.0,
        "day_win_rate": round(sum(1 for r in daily_returns if r > 0) / days, 4) if days else 0.0,
    }


def build_variant_config(base: StrategyConfig, overrides: dict) -> StrategyConfig:
    return replace(base, **overrides)


def run_variant(
    variant_name: str,
    overrides: dict,
    dates: List[str],
    train_count: int,
    processed_dir: Path,
    symbol: str,
    signals_filename: str,
    account_equity: float,
    baseline_contracts: int,
) -> tuple[dict, List[dict], List[dict]]:
    base = build_unconditional_config(
        account_equity=account_equity,
        baseline_contracts=baseline_contracts,
        stop_multiple=overrides.get("stop_multiple", 2.0),
    )
    config = build_variant_config(base, overrides)
    policy = FixedSizePolicy()

    daily_rows: List[dict] = []
    trade_rows: List[dict] = []

    for index in range(train_count, len(dates)):
        test_date = dates[index]
        train_dates = dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, signals_filename)

        day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / signals_filename)
        result = simulate_day(quotes, signals, config=config, policy=policy)

        day_trades = trades_to_rows(result.trades)
        spread_count = sum(1 for t in result.trades if t.model != "net_long_overlay")
        overlay_count = sum(1 for t in result.trades if t.model == "net_long_overlay")
        for row in day_trades:
            row["date"] = test_date
            row["variant"] = variant_name
            trade_rows.append(row)

        daily_rows.append(
            {
                "date": test_date,
                "variant": variant_name,
                "trades": len(result.trades),
                "spread_trades": spread_count,
                "overlay_trades": overlay_count,
                "stopped_trades": sum(1 for t in result.trades if t.stopped),
                "net_pnl": round(result.net_pnl, 2),
                "return_on_equity": round(result.return_on_equity, 8),
            }
        )

    spread_only = [row for row in trade_rows if row.get("model") != "net_long_overlay"]
    overlay_only = [row for row in trade_rows if row.get("model") == "net_long_overlay"]
    overall = trade_stats(spread_only)
    overlay_stats = trade_stats(overlay_only) if overlay_only else {"count": 0, "expectancy_per_trade": 0.0, "win_rate": 0.0, "stop_rate": 0.0}
    portfolio = portfolio_stats(daily_rows, account_equity)

    summary = {
        "variant": variant_name,
        "description": VARIANTS[variant_name]["description"],
        **portfolio,
        "spread_trades_count": overall["count"],
        "spread_win_rate": overall["win_rate"],
        "spread_stop_rate": overall["stop_rate"],
        "spread_expectancy": overall["expectancy_per_trade"],
        "spread_avg_win": overall["avg_win"],
        "spread_avg_loss": overall["avg_loss"],
        "overlay_trades_count": overlay_stats["count"],
        "overlay_expectancy": overlay_stats["expectancy_per_trade"],
        "overlay_win_rate": overlay_stats.get("win_rate", 0.0),
    }
    return summary, daily_rows, trade_rows


def build_report(summaries: Sequence[dict]) -> str:
    lines = [
        "# Stop + Structure Matrix — Test 2",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Unconditional cadence (391 OOS days, gates off, 31 contracts/tranche).",
        "",
        "## Results",
        "",
        "| Variant | CAGR | Sharpe | Worst day | Spread win% | Spread stop% | Spread E[trade] | Overlay trades |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    ranked = sorted(summaries, key=lambda row: row.get("cagr_pct", 0), reverse=True)
    for row in ranked:
        lines.append(
            f"| {row['variant']} | {row['cagr_pct']:.1f}% | {row['sharpe']:.2f} | "
            f"{row['worst_day_pct']:.1f}% | {row['spread_win_rate']*100:.1f}% | "
            f"{row['spread_stop_rate']*100:.1f}% | ${row['spread_expectancy']:,.0f} | "
            f"{row['overlay_trades_count']:,} |"
        )

    lines.extend(["", "## Variant descriptions", ""])
    for name, spec in VARIANTS.items():
        lines.append(f"- **{name}**: {spec['description']}")

    best = ranked[0] if ranked else {}
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            f"Best CAGR: **{best.get('variant', 'n/a')}** at {best.get('cagr_pct', 0):.1f}% "
            f"(Sharpe {best.get('sharpe', 0):.2f}, worst day {best.get('worst_day_pct', 0):.1f}%).",
            "",
            "MBH target: ~30–40% CAGR, Sharpe ~2.5, worst day ~4–5%, win rate ~65%.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Test 2 stop + structure matrix.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--signals-filename", default="signals_unconditional.csv")
    parser.add_argument("--account-equity", type=float, default=13_000_000.0)
    parser.add_argument("--baseline-contracts", type=int, default=31)
    parser.add_argument("--variants", nargs="*", default=list(VARIANTS.keys()))
    parser.add_argument("--max-days", type=int, default=0)
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    dates = discover_dates(processed_dir, args.symbol)
    if len(dates) <= args.train_count:
        raise SystemExit(f"Need more than {args.train_count} dates; have {len(dates)}.")
    if args.max_days > 0:
        dates = dates[: args.train_count + args.max_days]

    all_summaries: List[dict] = []
    all_daily: List[dict] = []

    for idx, variant_name in enumerate(args.variants, start=1):
        if variant_name not in VARIANTS:
            raise SystemExit(f"Unknown variant: {variant_name}")
        spec = VARIANTS[variant_name]
        overrides = {k: v for k, v in spec.items() if k != "description"}
        print(f"\n[{idx}/{len(args.variants)}] Running {variant_name}...")
        summary, daily_rows, trade_rows = run_variant(
            variant_name,
            overrides,
            dates,
            args.train_count,
            processed_dir,
            args.symbol,
            args.signals_filename,
            args.account_equity,
            args.baseline_contracts,
        )
        all_summaries.append(summary)
        all_daily.extend(daily_rows)

        variant_dir = results_dir / variant_name
        variant_dir.mkdir(parents=True, exist_ok=True)
        write_csv(variant_dir / "daily_summary.csv", daily_rows)
        write_csv(variant_dir / "trades.csv", trade_rows)
        print(
            f"  CAGR {summary['cagr_pct']:.1f}% | Sharpe {summary['sharpe']:.2f} | "
            f"spread win {summary['spread_win_rate']*100:.1f}% | stop {summary['spread_stop_rate']*100:.1f}% | "
            f"worst day {summary['worst_day_pct']:.1f}%"
        )

    write_csv(results_dir / "matrix_summary.csv", all_summaries)
    (results_dir / "matrix_summary.json").write_text(json.dumps(all_summaries, indent=2), encoding="utf-8")

    report = build_report(all_summaries)
    report_path = ROOT / f"stop_structure_matrix_results_{datetime.now().strftime('%Y-%m-%d')}.md"
    report_path.write_text(report, encoding="utf-8")
    (results_dir / "report.md").write_text(report, encoding="utf-8")

    print(f"\nWrote {results_dir / 'matrix_summary.csv'}")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
