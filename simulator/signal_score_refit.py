"""Walk-forward refit of candidate score weights from realized trades."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from mbh_simulator import StrategyConfig, simulate_day, read_quotes_csv, read_signals_csv
from regime_validation import apply_rolling_baseline, discover_dates, read_event_calendar
from score_model import (
    CandidateScoreWeights,
    DEFAULT_SCORE_WEIGHTS,
    fit_logistic_regression,
    save_score_weights,
    weights_from_logistic,
)
from summarize_run import summarize


ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from live.strategy_profiles import PROFILES  # noqa: E402

DEFAULT_RESULTS = ROOT / "data" / "signal_refit"
DEFAULT_MODEL = ROOT / "data" / "models" / "candidate_score_weights.json"
SIGNALS_FILENAME = "signals_regime_validation.csv"


def read_csv(path: Path) -> List[dict]:
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


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in {"", None}:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def profile_config(profile_name: str, account_equity: float, weights: CandidateScoreWeights | None = None) -> StrategyConfig:
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
        candidate_score_weights=weights,
        record_tranche_summaries=False,
    )


def trade_feature_row(trade: dict) -> Tuple[List[float], int]:
    side = trade.get("side", "")
    trend = safe_float(trade.get("entry_trend_score"))
    skew = safe_float(trade.get("entry_skew_z"))
    trend_alignment = trend if side == "bull_put" else -trend
    skew_alignment = skew if side == "bull_put" else -skew
    features = [
        max(min((safe_float(trade.get("entry_straddle_residual_z")) - 0.25) / 1.5, 1.5), -1.0),
        max(min(1.0 - abs(safe_float(trade.get("entry_term_ratio_z"))) / 1.5, 1.0), -1.0),
        max(min(1.0 - abs(safe_float(trade.get("entry_realized_vs_implied_z"))) / 1.75, 1.0), -1.0),
        max(min(trend_alignment / 1.0, 1.5), -1.5),
        max(min(skew_alignment / 1.25, 1.5), -1.5),
        max(min(1.0 - abs(abs(safe_float(trade.get("short_delta", 0.20))) - 0.20) / 0.10, 1.0), -1.0)
        if trade.get("short_delta") not in {"", None}
        else 0.0,
        max(min(safe_float(trade.get("credit_to_width")) / 0.04, 1.5), 0.0),
        max(min(safe_float(trade.get("distance_pct")) * 12.0, 1.5), 0.0),
        0.0,
    ]
    contracts = max(int(float(trade.get("contracts") or 1)), 1)
    label = 1 if safe_float(trade.get("net_pnl")) / contracts > 0 else 0
    return features, label


def collect_trades(processed_dir: Path, symbol: str, dates: Sequence[str], train_count: int, config: StrategyConfig) -> List[dict]:
    event_calendar = read_event_calendar(ROOT / "regime_expansion_dates_2025.csv")
    rows: List[dict] = []
    for index in range(train_count, len(dates)):
        test_date = dates[index]
        train_dates = dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, SIGNALS_FILENAME)
        day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / SIGNALS_FILENAME)
        event_info = event_calendar.get(test_date, {"event_bucket": "unlabeled"})
        day_config = replace(config, event_bucket=event_info["event_bucket"])
        result = simulate_day(quotes, signals, config=day_config)
        for trade in result.trades:
            rows.append(
                {
                    "date": test_date,
                    "side": trade.side,
                    "model": trade.model,
                    "contracts": trade.contracts,
                    "candidate_score": trade.candidate_score,
                    "net_pnl": trade.net_pnl,
                    "stopped": trade.stopped,
                    "entry_straddle_residual_z": trade.entry_straddle_residual_z,
                    "entry_skew_z": trade.entry_skew_z,
                    "entry_term_ratio_z": trade.entry_term_ratio_z,
                    "entry_trend_score": trade.entry_trend_score,
                    "entry_realized_vs_implied_z": trade.entry_realized_vs_implied_z,
                    "credit_to_width": trade.credit_to_width,
                    "distance_pct": trade.distance_pct,
                    "short_delta": trade.short_delta,
                }
            )
    return rows


def walkforward_eval(trades: Sequence[dict], min_train: int = 8) -> List[dict]:
    rows: List[dict] = []
    ordered = sorted(trades, key=lambda row: row["date"])
    for index in range(min_train, len(ordered)):
        train = ordered[:index]
        test = ordered[index]
        x_train, y_train = zip(*(trade_feature_row(row) for row in train))
        weights, bias = fit_logistic_regression(x_train, y_train)
        x_test, y_test = trade_feature_row(test)
        logit = bias + sum(w * x for w, x in zip(weights, x_test))
        pred = 1 if logit > 0 else 0
        rows.append(
            {
                "date": test["date"],
                "actual_win": y_test,
                "predicted_win": pred,
                "candidate_score": test.get("candidate_score"),
                "net_pnl": test.get("net_pnl"),
            }
        )
    return rows


def calibrate_weight_scale(trades: Sequence[dict], logistic_weights: Sequence[float], bias: float) -> float:
    raw_scores: List[float] = []
    for trade in trades:
        features, _ = trade_feature_row(trade)
        raw_scores.append(bias + sum(weight * value for weight, value in zip(logistic_weights, features)))
    if not raw_scores:
        return 1.0
    target = 2.50
    median = sorted(raw_scores)[len(raw_scores) // 2]
    if abs(median) < 1e-6:
        return 8.0
    return target / median


def run_backtest(
    processed_dir: Path,
    symbol: str,
    dates: Sequence[str],
    train_count: int,
    config: StrategyConfig,
    results_dir: Path,
) -> dict:
    event_calendar = read_event_calendar(ROOT / "regime_expansion_dates_2025.csv")
    daily_rows: List[dict] = []
    for index in range(train_count, len(dates)):
        test_date = dates[index]
        train_dates = dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, SIGNALS_FILENAME)
        day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / SIGNALS_FILENAME)
        event_info = event_calendar.get(test_date, {"event_bucket": "unlabeled"})
        day_config = replace(config, event_bucket=event_info["event_bucket"])
        result = simulate_day(quotes, signals, config=day_config)
        daily_rows.append(
            {
                "date": test_date,
                "trades": len(result.trades),
                "stopped_trades": sum(1 for trade in result.trades if trade.stopped),
                "net_pnl": round(result.net_pnl, 2),
                "gross_credit_sold": round(result.gross_credit_sold, 2),
                "halted": result.halted,
                "approx_spread_margin": 0.0,
            }
        )
    write_csv(results_dir / "daily_regime_validation.csv", daily_rows)
    return summarize(results_dir, config.account_equity, compound=True)


def build_report(
    trades: Sequence[dict],
    wf_rows: Sequence[dict],
    baseline_summary: dict,
    refit_summary: dict,
    refit_weights: CandidateScoreWeights,
    scale: float,
) -> str:
    wins = sum(1 for row in trades if safe_float(row["net_pnl"]) > 0)
    wf_acc = (
        sum(1 for row in wf_rows if int(row["actual_win"]) == int(row["predicted_win"])) / len(wf_rows)
        if wf_rows
        else 0.0
    )
    lines = [
        "# Signal Score Refit Report",
        "",
        f"- Training trades: **{len(trades)}**",
        f"- Historical win rate: **{wins / len(trades):.1%}**" if trades else "- Historical win rate: n/a",
        f"- Walk-forward accuracy (pilot): **{wf_acc:.1%}** on {len(wf_rows)} holdout trades",
        "",
        "## Backtest comparison (flatten 1x, full processed history)",
        "",
        "| Model | Trades | CAGR | Sharpe | Max DD | Net P&L |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        f"| Hand-tuned weights | {baseline_summary.get('trades', 0)} | {baseline_summary.get('cagr_pct', 0):.1f}% | "
        f"{baseline_summary.get('sharpe', 0):.2f} | {baseline_summary.get('max_drawdown_pct', 0):.1f}% | "
        f"${baseline_summary.get('net_pnl', 0):,.0f} |",
        f"| Refit weights | {refit_summary.get('trades', 0)} | {refit_summary.get('cagr_pct', 0):.1f}% | "
        f"{refit_summary.get('sharpe', 0):.2f} | {refit_summary.get('max_drawdown_pct', 0):.1f}% | "
        f"${refit_summary.get('net_pnl', 0):,.0f} |",
        "",
        "## Refit weights",
        "",
        f"- premium={refit_weights.premium:.3f}, term={refit_weights.term:.3f}, realized={refit_weights.realized:.3f}",
        f"- trend={refit_weights.trend:.3f}, skew={refit_weights.skew:.3f}, delta={refit_weights.delta:.3f}",
        f"- credit={refit_weights.credit:.3f}, distance={refit_weights.distance:.3f}, stop={refit_weights.stop:.3f}",
        f"- intercept={refit_weights.intercept:.3f} (calibrated scale={scale:.2f})",
        "",
        "## Recommendation",
        "",
    ]
    if len(trades) < 40:
        lines.append(
            "- Sample is still too small for production refit weights. Treat this as a **pilot** until 2023-2024 backfill adds trades."
        )
    elif refit_summary.get("cagr_pct", 0) > baseline_summary.get("cagr_pct", 0):
        lines.append(
            "- Refit weights improve full-history CAGR on this sample. Deploy behind a feature flag and re-validate after backfill."
        )
    else:
        lines.append(
            "- Refit weights do **not** beat hand-tuned weights on full history. Keep default weights; focus on signal features and more data."
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward refit of candidate score weights.")
    parser.add_argument("--processed-dir", type=Path, default=ROOT / "data" / "processed")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--account-equity", type=float, default=13_000_000.0)
    parser.add_argument("--profile", default="flatten")
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--skip-backtests", action="store_true")
    args = parser.parse_args()

    dates = discover_dates(args.processed_dir, args.symbol)
    if len(dates) <= args.train_count:
        raise SystemExit("Not enough processed dates.")

    baseline_config = profile_config(args.profile, args.account_equity, DEFAULT_SCORE_WEIGHTS)
    trades_path = args.results_dir / "training_trades.csv"
    if args.skip_collect and trades_path.exists():
        trades = read_csv(trades_path)
    else:
        trades = collect_trades(args.processed_dir, args.symbol, dates, args.train_count, baseline_config)
    if len(trades) < 8:
        raise SystemExit(f"Need at least 8 trades to refit; found {len(trades)}.")

    features, labels = zip(*(trade_feature_row(row) for row in trades))
    logistic_weights, bias = fit_logistic_regression(features, labels)
    scale = calibrate_weight_scale(trades, logistic_weights, bias)
    refit_weights = weights_from_logistic(logistic_weights, bias, scale=scale)

    wf_rows = walkforward_eval(trades)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    write_csv(args.results_dir / "training_trades.csv", trades)
    write_csv(args.results_dir / "walkforward_predictions.csv", wf_rows)

    if args.skip_backtests:
        baseline_summary = summarize(args.results_dir / "baseline", args.account_equity) if (args.results_dir / "baseline" / "daily_regime_validation.csv").exists() else {"trades": 0, "cagr_pct": 0.0, "sharpe": 0.0, "max_drawdown_pct": 0.0, "net_pnl": 0.0}
        refit_summary = run_backtest(
            args.processed_dir,
            args.symbol,
            dates,
            args.train_count,
            profile_config(args.profile, args.account_equity, refit_weights),
            args.results_dir / "refit",
        )
    else:
        baseline_summary = run_backtest(
            args.processed_dir,
            args.symbol,
            dates,
            args.train_count,
            baseline_config,
            args.results_dir / "baseline",
        )
        refit_summary = run_backtest(
            args.processed_dir,
            args.symbol,
            dates,
            args.train_count,
            profile_config(args.profile, args.account_equity, refit_weights),
            args.results_dir / "refit",
        )

    save_score_weights(
        args.model_out,
        refit_weights,
        metadata={"training_trades": len(trades), "profile": args.profile},
    )
    report = build_report(trades, wf_rows, baseline_summary, refit_summary, refit_weights, scale)
    (args.results_dir / "signal_refit_report.md").write_text(report, encoding="utf-8")
    print(f"wrote {args.model_out}")
    print(f"wrote {args.results_dir / 'signal_refit_report.md'}")
    print(report)


if __name__ == "__main__":
    main()
