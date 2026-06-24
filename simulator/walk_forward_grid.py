from __future__ import annotations

import argparse
import csv
from itertools import product
from pathlib import Path
from typing import Iterable, List

from historical_baselines import compute_baselines, processed_signal_path, read_csv, transform_rows, write_csv
from mbh_simulator import StrategyConfig, read_quotes_csv, read_signals_csv, simulate_day


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "walk_forward"


def parse_list(value: str, cast):
    return [cast(item.strip()) for item in value.split(",") if item.strip()]


def discover_dates(processed_dir: Path, symbol: str) -> List[str]:
    root = processed_dir / f"symbol={symbol}"
    dates = []
    if not root.exists():
        return dates
    for path in root.iterdir():
        if path.is_dir() and path.name.startswith("date=") and (path / "signals.csv").exists():
            dates.append(path.name.split("=", 1)[1])
    return sorted(dates)


def apply_baselines(processed_dir: Path, symbol: str, train_dates: List[str], apply_dates: List[str], output_filename: str) -> None:
    baselines = compute_baselines(processed_dir, symbol, train_dates)
    for trade_date in apply_dates:
        input_path = processed_signal_path(processed_dir, symbol, trade_date)
        output_path = processed_signal_path(processed_dir, symbol, trade_date, output_filename)
        rows = read_csv(input_path)
        write_csv(output_path, transform_rows(rows, baselines))


def run_config(processed_dir: Path, symbol: str, test_dates: List[str], signals_filename: str, config: StrategyConfig) -> dict:
    total_pnl = 0.0
    total_credit = 0.0
    total_trades = 0
    stopped_trades = 0
    halted_days = 0
    positive_days = 0
    no_trade_days = 0
    worst_day = 0.0
    daily_returns = []

    for trade_date in test_dates:
        day_dir = processed_dir / f"symbol={symbol}" / f"date={trade_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / signals_filename)
        result = simulate_day(quotes, signals, config=config)
        total_pnl += result.net_pnl
        total_credit += result.gross_credit_sold
        total_trades += len(result.trades)
        stopped_trades += sum(1 for trade in result.trades if trade.stopped)
        halted_days += 1 if result.halted else 0
        positive_days += 1 if result.net_pnl > 0 else 0
        no_trade_days += 1 if not result.trades else 0
        worst_day = min(worst_day, result.net_pnl)
        daily_returns.append(result.return_on_equity)

    return {
        "test_days": len(test_dates),
        "total_net_pnl": round(total_pnl, 2),
        "total_return": round(sum(daily_returns), 8),
        "mean_daily_return": round(sum(daily_returns) / len(daily_returns), 8) if daily_returns else 0.0,
        "total_credit_sold": round(total_credit, 2),
        "total_trades": total_trades,
        "stopped_trades": stopped_trades,
        "stop_rate": round(stopped_trades / total_trades, 6) if total_trades else 0.0,
        "halted_days": halted_days,
        "positive_days": positive_days,
        "no_trade_days": no_trade_days,
        "worst_day": round(worst_day, 2),
    }


def write_result_csv(path: Path, rows: List[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run walk-forward parameter grids on processed SPXW data.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--dates", nargs="*", help="Optional explicit ordered dates. Defaults to all processed dates.")
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--train-count", type=int, default=3)
    parser.add_argument("--test-count", type=int, default=2)
    parser.add_argument("--signals-filename", default="signals_historical.csv")
    parser.add_argument("--account-equity", type=float, default=28_000_000)
    parser.add_argument("--baseline-contracts", default="16,33,66,85,110")
    parser.add_argument("--daily-credit-cap-pcts", default="0.015,0.02")
    parser.add_argument("--stop-multiples", default="2.0,2.5,3.0")
    parser.add_argument("--target-long-deltas", default="0.03,0.05,0.08")
    parser.add_argument("--skew-extreme-thresholds", default="1.0,1.5")
    parser.add_argument("--term-extreme-thresholds", default="1.0,1.5")
    parser.add_argument("--atm-surface-min-residuals", default="0.25,0.5")
    parser.add_argument("--hard-term-ratio-skip-thresholds", default="1.25,1.5")
    parser.add_argument("--hard-realized-skip-thresholds", default="1.5,1.75")
    parser.add_argument("--hard-trend-skip-thresholds", default="1.5,2.0")
    parser.add_argument("--candidate-min-scores", default="2.50")
    parser.add_argument("--candidate-half-scores", default="2.25")
    parser.add_argument("--candidate-full-scores", default="2.50")
    parser.add_argument("--candidate-max-sides", default="1")
    parser.add_argument("--candidate-min-credits", default="0.20")
    parser.add_argument("--candidate-min-credit-to-widths", default="0.0125")
    parser.add_argument("--candidate-max-stop-loss-to-credits", default="4.50")
    parser.add_argument("--candidate-max-adverse-trends", default="0.65")
    parser.add_argument("--candidate-max-chase-trends", default="1.50")
    parser.add_argument("--candidate-max-adverse-skews", default="0.75")
    parser.add_argument("--candidate-max-abs-term-ratio-zs", default="1.25")
    parser.add_argument("--candidate-max-abs-realized-zs", default="1.50")
    parser.add_argument("--max-open-trades-per-sides", default="2")
    parser.add_argument("--max-open-trades-same-side-strikes", default="1")
    parser.add_argument("--stop-cooldown-minutes-list", default="30")
    parser.add_argument("--same-side-stop-cooldown-minutes-list", default="120")
    parser.add_argument("--max-stops-per-sides", default="2")
    parser.add_argument("--memory-term-ratio-skip-thresholds", default="1.50")
    parser.add_argument("--memory-skew-skip-thresholds", default="99.0")
    parser.add_argument("--memory-trend-skip-thresholds", default="99.0")
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)
    dates = sorted(args.dates) if args.dates else discover_dates(processed_dir, args.symbol)
    if len(dates) < args.train_count + 1:
        raise SystemExit("Not enough processed dates for walk-forward testing.")

    train_dates = dates[: args.train_count]
    test_dates = dates[args.train_count : args.train_count + args.test_count]
    apply_baselines(processed_dir, args.symbol, train_dates, test_dates, args.signals_filename)

    option_grid = {
        "baseline_contracts": parse_list(args.baseline_contracts, int),
        "daily_credit_cap_pct": parse_list(args.daily_credit_cap_pcts, float),
        "stop_multiple": parse_list(args.stop_multiples, float),
        "target_long_abs_delta": parse_list(args.target_long_deltas, float),
        "skew_extreme_threshold": parse_list(args.skew_extreme_thresholds, float),
        "term_extreme_threshold": parse_list(args.term_extreme_thresholds, float),
        "atm_surface_min_residual": parse_list(args.atm_surface_min_residuals, float),
        "hard_term_ratio_skip_threshold": parse_list(args.hard_term_ratio_skip_thresholds, float),
        "hard_realized_skip_threshold": parse_list(args.hard_realized_skip_thresholds, float),
        "hard_trend_skip_threshold": parse_list(args.hard_trend_skip_thresholds, float),
        "candidate_min_score": parse_list(args.candidate_min_scores, float),
        "candidate_half_score": parse_list(args.candidate_half_scores, float),
        "candidate_full_score": parse_list(args.candidate_full_scores, float),
        "candidate_max_sides": parse_list(args.candidate_max_sides, int),
        "candidate_min_credit": parse_list(args.candidate_min_credits, float),
        "candidate_min_credit_to_width": parse_list(args.candidate_min_credit_to_widths, float),
        "candidate_max_stop_loss_to_credit": parse_list(args.candidate_max_stop_loss_to_credits, float),
        "candidate_max_adverse_trend": parse_list(args.candidate_max_adverse_trends, float),
        "candidate_max_chase_trend": parse_list(args.candidate_max_chase_trends, float),
        "candidate_max_adverse_skew": parse_list(args.candidate_max_adverse_skews, float),
        "candidate_max_abs_term_ratio_z": parse_list(args.candidate_max_abs_term_ratio_zs, float),
        "candidate_max_abs_realized_z": parse_list(args.candidate_max_abs_realized_zs, float),
        "max_open_trades_per_side": parse_list(args.max_open_trades_per_sides, int),
        "max_open_trades_same_side_strike": parse_list(args.max_open_trades_same_side_strikes, int),
        "stop_cooldown_minutes": parse_list(args.stop_cooldown_minutes_list, int),
        "same_side_stop_cooldown_minutes": parse_list(args.same_side_stop_cooldown_minutes_list, int),
        "max_stops_per_side": parse_list(args.max_stops_per_sides, int),
        "memory_term_ratio_skip_threshold": parse_list(args.memory_term_ratio_skip_thresholds, float),
        "memory_skew_skip_threshold": parse_list(args.memory_skew_skip_thresholds, float),
        "memory_trend_skip_threshold": parse_list(args.memory_trend_skip_thresholds, float),
    }

    rows: List[dict] = []
    option_names = list(option_grid)
    for values in product(*(option_grid[name] for name in option_names)):
        params = dict(zip(option_names, values))
        config = StrategyConfig(
            account_equity=args.account_equity,
            **params,
        )
        summary = run_config(processed_dir, args.symbol, test_dates, args.signals_filename, config)
        rows.append(
            {
                "train_dates": ",".join(train_dates),
                "test_dates": ",".join(test_dates),
                **params,
                **summary,
            }
        )

    rows.sort(key=lambda row: (row["mean_daily_return"], -row["stop_rate"]), reverse=True)
    output = Path(args.results_dir) / "walk_forward_grid.csv"
    write_result_csv(output, rows)
    print(f"train_dates={train_dates}")
    print(f"test_dates={test_dates}")
    print(f"wrote {output}")
    for row in rows[:5]:
        print(row)


if __name__ == "__main__":
    main()
