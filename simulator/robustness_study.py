"""Phase 2: statistical robustness study.

1. Full-history rolling walk-forward (all processed dates, 40-day baseline warmup)
2. Block bootstrap + trade bootstrap confidence intervals on daily returns
3. Period splits (2024 / 2025 halves / original validation window)
4. Sensitivity: candidate score gate +/- 0.10 and entry-grid timing offset
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from dataclasses import dataclass, replace
from datetime import time
from pathlib import Path
from statistics import mean, pstdev
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from mbh_simulator import StrategyConfig, read_quotes_csv, read_signals_csv, simulate_day, trade_margin, trades_to_rows
from regime_validation import apply_rolling_baseline, classify_regime, discover_dates, read_event_calendar
from summarize_run import TRADING_DAYS, safe_float, safe_int, summarize


ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from live.strategy_profiles import PROFILES  # noqa: E402

DEFAULT_RESULTS = ROOT / "data" / "phase2_robustness"
DEFAULT_EVENT_CALENDAR = ROOT / "regime_expansion_dates_2025.csv"
SIGNALS_FILENAME = "signals_regime_validation.csv"

PERIOD_SPLITS = [
    ("Full sample", "", ""),
    ("2024 H2", "2024-07-01", "2024-12-31"),
    ("2025 H1", "2025-01-01", "2025-06-30"),
    ("2025 H2", "2025-07-01", "2025-12-31"),
    ("2026 YTD", "2026-01-01", "2026-12-31"),
    ("Apr-Sep 2025 (original window)", "2025-04-01", "2025-09-30"),
]

SENSITIVITY_VARIANTS = [
    {"label": "gate_2.40", "candidate_min_score": 2.40},
    {"label": "gate_2.50_baseline", "candidate_min_score": 2.50},
    {"label": "gate_2.60", "candidate_min_score": 2.60},
    {"label": "entry_09_37", "candidate_min_score": 2.50, "entry_start": time(9, 37)},
    {"label": "entry_09_47", "candidate_min_score": 2.50, "entry_start": time(9, 47)},
]


def read_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def profile_config(profile_name: str, account_equity: float) -> StrategyConfig:
    profile = PROFILES[profile_name]
    return StrategyConfig(
        account_equity=account_equity,
        baseline_contracts=profile["baseline_contracts"],
        daily_credit_cap_pct=profile["daily_credit_cap_pct"],
        daily_loss_limit_pct=profile["daily_loss_limit_pct"],
        flatten_on_daily_loss=profile.get("flatten_on_daily_loss", False),
        flatten_loss_limit_pct=profile.get("flatten_loss_limit_pct", 0.0),
        use_two_tier_engine=profile.get("use_two_tier_engine", True),
        use_event_controls=profile.get("use_event_controls", True),
        use_time_of_day_controls=profile.get("use_time_of_day_controls", True),
        exploratory_min_score=profile.get("exploratory_min_score", 2.40),
        exploratory_max_score=profile.get("exploratory_max_score", 2.49),
        use_portfolio_allocator=profile.get("use_portfolio_allocator", True),
        portfolio_margin_budget_pct=profile.get("portfolio_margin_budget_pct", 0.40),
        core_margin_budget_pct=profile.get("core_margin_budget_pct", 0.35),
        exploratory_margin_budget_pct=profile.get("exploratory_margin_budget_pct", 0.02),
        candidate_min_score=2.50,
        record_tranche_summaries=False,
    )


def apply_overrides(config: StrategyConfig, overrides: dict) -> StrategyConfig:
    return replace(config, **overrides)


def filter_period(rows: Sequence[dict], start: str, end: str) -> List[dict]:
    if not start and not end:
        return list(rows)
    filtered = []
    for row in rows:
        date = row["date"]
        if start and date < start:
            continue
        if end and date > end:
            continue
        filtered.append(row)
    return filtered


def run_walkforward(
    processed_dir: Path,
    symbol: str,
    dates: Sequence[str],
    config: StrategyConfig,
    train_count: int,
    event_calendar: dict,
) -> Tuple[List[dict], List[dict]]:
    daily_rows: List[dict] = []
    trade_rows: List[dict] = []

    for index in range(train_count, len(dates)):
        test_date = dates[index]
        train_dates = dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, SIGNALS_FILENAME)

        day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / SIGNALS_FILENAME)
        regime, _ = classify_regime(signals)
        event_info = event_calendar.get(test_date, {"event_bucket": "unlabeled", "event_note": ""})
        day_config = replace(config, event_bucket=event_info["event_bucket"])
        result = simulate_day(quotes, signals, config=day_config)

        stopped = sum(1 for trade in result.trades if trade.stopped)
        core_trades = [trade for trade in result.trades if trade.model == "candidate_core"]
        exploratory_trades = [trade for trade in result.trades if trade.model == "candidate_exploratory"]
        max_spread_risk = sum(trade_margin(trade, day_config) for trade in result.trades)

        daily_rows.append(
            {
                "date": test_date,
                "regime": regime,
                "event_bucket": event_info["event_bucket"],
                "trades": len(result.trades),
                "core_trades": len(core_trades),
                "exploratory_trades": len(exploratory_trades),
                "stopped_trades": stopped,
                "net_pnl": round(result.net_pnl, 2),
                "gross_credit_sold": round(result.gross_credit_sold, 2),
                "return_on_equity": round(result.return_on_equity, 8),
                "halted": result.halted,
                "approx_spread_margin": round(max_spread_risk, 2),
            }
        )

        for row in trades_to_rows(result.trades):
            row["date"] = test_date
            trade_rows.append(row)

    return daily_rows, trade_rows


def daily_returns_from_rows(rows: Sequence[dict], account_equity: float, compound: bool = True) -> List[float]:
    equity = account_equity
    returns: List[float] = []
    for row in rows:
        day_pnl = safe_float(row.get("net_pnl"))
        base = equity if compound else account_equity
        ret = day_pnl / base if base else 0.0
        returns.append(ret)
        if compound:
            equity += day_pnl
    return returns


def compounded_cagr(daily_returns: Sequence[float], account_equity: float, compound: bool = True) -> float:
    equity = account_equity
    for index, ret in enumerate(daily_returns):
        if compound:
            equity *= 1.0 + ret
        else:
            equity = account_equity + sum(account_equity * r for r in daily_returns[: index + 1])
    total_return = equity / account_equity - 1.0
    years = len(daily_returns) / TRADING_DAYS
    if years <= 0 or total_return <= -1.0:
        return 0.0
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def max_drawdown_from_returns(daily_returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in daily_returns:
        equity *= 1.0 + ret
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd


def sharpe_from_returns(daily_returns: Sequence[float]) -> float:
    if len(daily_returns) < 2:
        return 0.0
    mean_daily = mean(daily_returns)
    std_daily = pstdev(daily_returns)
    return (mean_daily / std_daily) * math.sqrt(TRADING_DAYS) if std_daily > 0 else 0.0


def percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


@dataclass
class BootstrapResult:
    metric: str
    p5: float
    p25: float
    p50: float
    p75: float
    p95: float

    def to_row(self) -> dict:
        return {
            "metric": self.metric,
            "p5": round(self.p5, 4),
            "p25": round(self.p25, 4),
            "p50": round(self.p50, 4),
            "p75": round(self.p75, 4),
            "p95": round(self.p95, 4),
        }


def block_bootstrap(
    daily_returns: Sequence[float],
    account_equity: float,
    n_simulations: int,
    block_size: int,
    seed: int,
) -> List[BootstrapResult]:
    if not daily_returns:
        return []
    rng = random.Random(seed)
    n = len(daily_returns)
    cagrs: List[float] = []
    sharpes: List[float] = []
    max_dds: List[float] = []
    total_rets: List[float] = []

    for _ in range(n_simulations):
        sampled: List[float] = []
        while len(sampled) < n:
            start = rng.randrange(n)
            sampled.extend(daily_returns[start : min(start + block_size, n)])
        sampled = sampled[:n]
        cagrs.append(compounded_cagr(sampled, account_equity) * 100.0)
        sharpes.append(sharpe_from_returns(sampled))
        max_dds.append(max_drawdown_from_returns(sampled) * 100.0)
        equity = account_equity
        for ret in sampled:
            equity *= 1.0 + ret
        total_rets.append((equity / account_equity - 1.0) * 100.0)

    return [
        BootstrapResult("cagr_pct", *[percentile(cagrs, p) for p in (5, 25, 50, 75, 95)]),
        BootstrapResult("sharpe", *[percentile(sharpes, p) for p in (5, 25, 50, 75, 95)]),
        BootstrapResult("max_drawdown_pct", *[percentile(max_dds, p) for p in (5, 25, 50, 75, 95)]),
        BootstrapResult("total_return_pct", *[percentile(total_rets, p) for p in (5, 25, 50, 75, 95)]),
    ]


def trade_bootstrap(
    trade_rows: Sequence[dict],
    test_days: int,
    account_equity: float,
    n_simulations: int,
    seed: int,
) -> List[BootstrapResult]:
    pnls = [safe_float(row.get("net_pnl")) for row in trade_rows]
    if not pnls:
        return []
    rng = random.Random(seed + 1)
    cagrs: List[float] = []
    total_pnls: List[float] = []
    win_rates: List[float] = []

    for _ in range(n_simulations):
        sample = [pnls[rng.randrange(len(pnls))] for _ in range(len(pnls))]
        total_pnl = sum(sample)
        total_pnls.append(total_pnl)
        wins = sum(1 for value in sample if value > 0)
        win_rates.append(wins / len(sample))
        years = test_days / TRADING_DAYS
        total_return = total_pnl / account_equity
        cagr = ((1.0 + total_return) ** (1.0 / years) - 1.0) * 100.0 if years > 0 and total_return > -1.0 else 0.0
        cagrs.append(cagr)

    return [
        BootstrapResult("trade_resample_cagr_pct", *[percentile(cagrs, p) for p in (5, 25, 50, 75, 95)]),
        BootstrapResult("trade_resample_total_pnl", *[percentile(total_pnls, p) for p in (5, 25, 50, 75, 95)]),
        BootstrapResult("trade_resample_win_rate", *[percentile(win_rates, p) for p in (5, 25, 50, 75, 95)]),
    ]


def summarize_rows(rows: Sequence[dict], account_equity: float) -> dict:
    if not rows:
        return {"days": 0, "trades": 0, "cagr_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0}
    tmp_dir = Path("_tmp_robustness_summary")
    tmp_dir.mkdir(exist_ok=True)
    daily_path = tmp_dir / "daily_regime_validation.csv"
    write_csv(daily_path, list(rows))
    summary = summarize(tmp_dir, account_equity, compound=True)
    return summary


def run_profile_study(
    label: str,
    profile_name: str,
    processed_dir: Path,
    symbol: str,
    dates: Sequence[str],
    train_count: int,
    event_calendar: dict,
    results_dir: Path,
    account_equity: float,
) -> dict:
    config = profile_config(profile_name, account_equity)
    daily_rows, trade_rows = run_walkforward(processed_dir, symbol, dates, config, train_count, event_calendar)
    out_dir = results_dir / label
    write_csv(out_dir / "daily_regime_validation.csv", daily_rows)
    write_csv(out_dir / "trades.csv", trade_rows)
    summary = summarize(out_dir, account_equity, compound=True)
    summary["profile"] = profile_name
    summary["label"] = label
    return summary


def run_sensitivity_grid(
    base_config: StrategyConfig,
    processed_dir: Path,
    symbol: str,
    dates: Sequence[str],
    train_count: int,
    event_calendar: dict,
    account_equity: float,
) -> List[dict]:
    rows: List[dict] = []
    for variant in SENSITIVITY_VARIANTS:
        overrides = {key: value for key, value in variant.items() if key != "label"}
        config = apply_overrides(base_config, overrides)
        daily_rows, trade_rows = run_walkforward(processed_dir, symbol, dates, config, train_count, event_calendar)
        summary = summarize_rows(daily_rows, account_equity)
        rows.append(
            {
                "variant": variant["label"],
                "candidate_min_score": overrides.get("candidate_min_score", base_config.candidate_min_score),
                "entry_start": str(overrides.get("entry_start", base_config.entry_start)),
                "days": summary.get("days", 0),
                "trades": summary.get("trades", 0),
                "cagr_pct": summary.get("cagr_pct", 0.0),
                "sharpe": summary.get("sharpe", 0.0),
                "max_drawdown_pct": summary.get("max_drawdown_pct", 0.0),
                "net_pnl": summary.get("net_pnl", 0.0),
                "stop_rate": summary.get("stop_rate", 0.0),
            }
        )
        print(
            f"  sensitivity {variant['label']}: trades={rows[-1]['trades']} "
            f"cagr={rows[-1]['cagr_pct']:.1f}% sharpe={rows[-1]['sharpe']:.2f}"
        )
    return rows


def build_report(
    flatten_summary: dict,
    best_summary: dict,
    period_rows: Sequence[dict],
    bootstrap_rows: Sequence[dict],
    trade_bootstrap_rows: Sequence[dict],
    sensitivity_rows: Sequence[dict],
    test_days: int,
    total_trades: int,
) -> str:
    lines = [
        "# Phase 2 Robustness Study",
        "",
        f"- Test days (after {40}-day rolling warmup): **{test_days}**",
        f"- Date span: processed SPXW history",
        f"- Total executed trades (flatten 1x): **{total_trades}**",
        "",
        "## Full-history point estimates",
        "",
        "| Profile | Days | Trades | CAGR | Sharpe | Max DD | Net P&L |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| flatten (1x) | {flatten_summary.get('days', 0)} | {flatten_summary.get('trades', 0)} | "
        f"{flatten_summary.get('cagr_pct', 0):.1f}% | {flatten_summary.get('sharpe', 0):.2f} | "
        f"{flatten_summary.get('max_drawdown_pct', 0):.1f}% | ${flatten_summary.get('net_pnl', 0):,.0f} |",
        f"| best (2x) | {best_summary.get('days', 0)} | {best_summary.get('trades', 0)} | "
        f"{best_summary.get('cagr_pct', 0):.1f}% | {best_summary.get('sharpe', 0):.2f} | "
        f"{best_summary.get('max_drawdown_pct', 0):.1f}% | ${best_summary.get('net_pnl', 0):,.0f} |",
        "",
        "## Period splits (flatten 1x)",
        "",
        "| Period | Days | Trades | CAGR | Sharpe | Max DD | Net P&L |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in period_rows:
        lines.append(
            f"| {row['period']} | {row['days']} | {row['trades']} | {row['cagr_pct']:.1f}% | "
            f"{row['sharpe']:.2f} | {row['max_drawdown_pct']:.1f}% | ${row['net_pnl']:,.0f} |"
        )

    lines.extend(["", "## Block bootstrap (daily returns, 5-day blocks, 5,000 sims)", ""])
    lines.append("| Metric | p5 | p25 | p50 | p75 | p95 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
    for row in bootstrap_rows:
        lines.append(
            f"| {row['metric']} | {row['p5']} | {row['p25']} | {row['p50']} | {row['p75']} | {row['p95']} |"
        )

    if trade_bootstrap_rows:
        lines.extend(["", "## Trade resample bootstrap (trade-level, 5,000 sims)", ""])
        lines.append("| Metric | p5 | p25 | p50 | p75 | p95 |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: |")
        for row in trade_bootstrap_rows:
            lines.append(
                f"| {row['metric']} | {row['p5']} | {row['p25']} | {row['p50']} | {row['p75']} | {row['p95']} |"
            )

    lines.extend(["", "## Sensitivity grid (flatten 1x, full history)", ""])
    lines.append("| Variant | Gate | Entry start | Trades | CAGR | Sharpe | Max DD |")
    lines.append("| --- | ---: | --- | ---: | ---: | ---: | ---: |")
    baseline_cagr = next((row["cagr_pct"] for row in sensitivity_rows if row["variant"] == "gate_2.50_baseline"), 0.0)
    for row in sensitivity_rows:
        delta = row["cagr_pct"] - baseline_cagr
        lines.append(
            f"| {row['variant']} | {row['candidate_min_score']:.2f} | {row['entry_start']} | {row['trades']} | "
            f"{row['cagr_pct']:.1f}% ({delta:+.1f}) | {row['sharpe']:.2f} | {row['max_drawdown_pct']:.1f}% |"
        )

    cagr_p5 = next((row["p5"] for row in bootstrap_rows if row["metric"] == "cagr_pct"), 0.0)
    cagr_p95 = next((row["p95"] for row in bootstrap_rows if row["metric"] == "cagr_pct"), 0.0)
    gate_spread = max(row["cagr_pct"] for row in sensitivity_rows) - min(row["cagr_pct"] for row in sensitivity_rows)

    lines.extend(["", "## Sizing go / no-go", ""])
    if cagr_p5 > 0 and flatten_summary.get("cagr_pct", 0) > 0:
        lines.append(
            f"- **Edge likely positive:** flatten CAGR {flatten_summary.get('cagr_pct', 0):.1f}% with bootstrap 5th pct "
            f"{cagr_p5:.1f}% — lower bound still above zero on this sample."
        )
    elif cagr_p5 <= 0 <= cagr_p95:
        lines.append(
            f"- **Edge inconclusive:** bootstrap CAGR 5th–95th pct spans {cagr_p5:.1f}% to {cagr_p95:.1f}%. "
            "Sample is too thin to confirm edge at high confidence."
        )
    else:
        lines.append(
            f"- **Edge weak / negative:** bootstrap median and lower tail do not support aggressive sizing."
        )

    if gate_spread >= 10:
        lines.append(
            f"- **Parameter fragility:** score gate sensitivity swings CAGR by {gate_spread:.1f} pts — "
            "the 2.50 threshold may be overfit; treat headline CAGR as upper-bound."
        )
    else:
        lines.append(
            f"- **Parameter stability:** gate +/- 0.10 moves CAGR by {gate_spread:.1f} pts — "
            "core gate is not hypersensitive on full history."
        )

    if best_summary.get("cagr_pct", 0) > 0 and flatten_summary.get("cagr_pct", 0) > 0:
        lines.append(
            f"- **2x scaling:** best profile CAGR {best_summary.get('cagr_pct', 0):.1f}% vs flatten "
            f"{flatten_summary.get('cagr_pct', 0):.1f}%. Linear scaling holds if margin stays non-binding; "
            "validate live before deploying 2x."
        )

    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 2 statistical robustness study.")
    parser.add_argument("--processed-dir", type=Path, default=ROOT / "data" / "processed")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--account-equity", type=float, default=13_000_000.0)
    parser.add_argument("--bootstrap-sims", type=int, default=5000)
    parser.add_argument("--bootstrap-block", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--skip-sensitivity", action="store_true")
    parser.add_argument("--skip-walkforward", action="store_true", help="Reuse existing full_history_* outputs")
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    dates = discover_dates(processed_dir, args.symbol)
    if len(dates) <= args.train_count:
        raise SystemExit("Not enough processed dates for walk-forward testing.")
    event_calendar = read_event_calendar(DEFAULT_EVENT_CALENDAR)
    test_days = len(dates) - args.train_count

    print(f"Running full-history flatten profile ({test_days} test days)...")
    if args.skip_walkforward and (results_dir / "full_history_flatten" / "daily_regime_validation.csv").exists():
        flatten_summary = summarize(results_dir / "full_history_flatten", args.account_equity, compound=True)
        flatten_summary["profile"] = "flatten"
        flatten_summary["label"] = "full_history_flatten"
        print(f"  flatten (cached): trades={flatten_summary['trades']} cagr={flatten_summary['cagr_pct']:.1f}%")
    else:
        flatten_summary = run_profile_study(
            "full_history_flatten",
            "flatten",
            processed_dir,
            args.symbol,
            dates,
            args.train_count,
            event_calendar,
            results_dir,
            args.account_equity,
        )
        print(
            f"  flatten: trades={flatten_summary['trades']} cagr={flatten_summary['cagr_pct']:.1f}% "
            f"sharpe={flatten_summary['sharpe']:.2f}"
        )

    print(f"Running full-history best (2x) profile ({test_days} test days)...")
    if args.skip_walkforward and (results_dir / "full_history_best" / "daily_regime_validation.csv").exists():
        best_summary = summarize(results_dir / "full_history_best", args.account_equity, compound=True)
        best_summary["profile"] = "best"
        best_summary["label"] = "full_history_best"
        print(f"  best (cached): trades={best_summary['trades']} cagr={best_summary['cagr_pct']:.1f}%")
    else:
        best_summary = run_profile_study(
            "full_history_best",
            "best",
            processed_dir,
            args.symbol,
            dates,
            args.train_count,
            event_calendar,
            results_dir,
            args.account_equity,
        )
        print(
            f"  best: trades={best_summary['trades']} cagr={best_summary['cagr_pct']:.1f}% "
            f"sharpe={best_summary['sharpe']:.2f}"
        )

    flatten_daily = read_csv(results_dir / "full_history_flatten" / "daily_regime_validation.csv")
    flatten_trades = read_csv(results_dir / "full_history_flatten" / "trades.csv")

    period_rows: List[dict] = []
    for label, start, end in PERIOD_SPLITS:
        subset = filter_period(flatten_daily, start, end)
        summary = summarize_rows(subset, args.account_equity)
        period_rows.append(
            {
                "period": label,
                "days": summary.get("days", 0),
                "trades": summary.get("trades", 0),
                "cagr_pct": summary.get("cagr_pct", 0.0),
                "sharpe": summary.get("sharpe", 0.0),
                "max_drawdown_pct": summary.get("max_drawdown_pct", 0.0),
                "net_pnl": summary.get("net_pnl", 0.0),
            }
        )

    write_csv(results_dir / "period_splits.csv", period_rows)

    daily_returns = daily_returns_from_rows(flatten_daily, args.account_equity, compound=True)
    bootstrap = block_bootstrap(daily_returns, args.account_equity, args.bootstrap_sims, args.bootstrap_block, args.seed)
    trade_boot = trade_bootstrap(flatten_trades, test_days, args.account_equity, args.bootstrap_sims, args.seed)
    bootstrap_rows = [row.to_row() for row in bootstrap + trade_boot]
    write_csv(results_dir / "bootstrap_distribution.csv", bootstrap_rows)

    sensitivity_rows: List[dict] = []
    if not args.skip_sensitivity:
        print("Running sensitivity grid (gate +/- 0.10, entry timing)...")
        base_config = profile_config("flatten", args.account_equity)
        sensitivity_rows = run_sensitivity_grid(
            base_config,
            processed_dir,
            args.symbol,
            dates,
            args.train_count,
            event_calendar,
            args.account_equity,
        )
        write_csv(results_dir / "sensitivity_grid.csv", sensitivity_rows)

    report = build_report(
        flatten_summary,
        best_summary,
        period_rows,
        [row.to_row() for row in bootstrap],
        [row.to_row() for row in trade_boot],
        sensitivity_rows,
        test_days,
        flatten_summary.get("trades", 0),
    )
    (results_dir / "robustness_report.md").write_text(report, encoding="utf-8")
    (results_dir / "summary.json").write_text(
        json.dumps(
            {
                "flatten": flatten_summary,
                "best": best_summary,
                "period_splits": period_rows,
                "bootstrap": bootstrap_rows,
                "sensitivity": sensitivity_rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\nwrote {results_dir / 'robustness_report.md'}")
    print(f"wrote {results_dir / 'bootstrap_distribution.csv'}")
    if sensitivity_rows:
        print(f"wrote {results_dir / 'sensitivity_grid.csv'}")
    print()
    print(report)


if __name__ == "__main__":
    main()
