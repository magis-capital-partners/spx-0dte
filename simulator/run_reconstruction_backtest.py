from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import List

from mbh_simulator import (
    StrategyConfig,
    candidate_records_to_rows,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
    stop_diagnostics_to_rows,
    trades_to_rows,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "results"


def run_day(
    symbol: str,
    trade_date: str,
    processed_dir: Path,
    config: StrategyConfig,
    signals_filename: str,
) -> tuple[dict, List[dict], List[dict], List[dict]]:
    day_dir = processed_dir / f"symbol={symbol}" / f"date={trade_date}"
    quotes_path = day_dir / "normalized_option_quotes.csv"
    signals_path = day_dir / signals_filename
    quotes = read_quotes_csv(quotes_path)
    signals = read_signals_csv(signals_path)
    result = simulate_day(quotes, signals, config=config)
    candidate_rows = candidate_records_to_rows(result.candidate_records)
    stop_rows = stop_diagnostics_to_rows(result.trades)
    summary = {
        "date": trade_date,
        "trades": len(result.trades),
        "candidate_rows": len(candidate_rows),
        "selected_candidates": sum(1 for row in candidate_rows if row["status"] == "selected"),
        "stopped_trades": len(stop_rows),
        "gross_credit_sold": round(result.gross_credit_sold, 2),
        "gross_pnl": round(result.gross_pnl, 2),
        "fees": round(result.fees, 2),
        "net_pnl": round(result.net_pnl, 2),
        "return_on_equity": round(result.return_on_equity, 8),
        "halted": result.halted,
        "halt_time": result.halt_time.isoformat() if result.halt_time else "",
    }
    trade_rows = trades_to_rows(result.trades)
    for row in trade_rows:
        row["date"] = trade_date
    for row in candidate_rows:
        row["date"] = trade_date
    for row in stop_rows:
        row["date"] = trade_date
    return summary, trade_rows, candidate_rows, stop_rows


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
    parser = argparse.ArgumentParser(description="Run MBH reconstruction simulator on processed ThetaData files.")
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--dates", nargs="+", required=True)
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--signals-filename", default="signals.csv")
    parser.add_argument("--account-equity", type=float, default=1_000_000.0)
    parser.add_argument("--baseline-contracts", type=int, default=16)
    parser.add_argument("--daily-credit-cap-pct", type=float, default=0.015)
    parser.add_argument("--daily-loss-limit-pct", type=float, default=0.0225)
    parser.add_argument("--stop-multiple", type=float, default=2.0)
    parser.add_argument("--target-long-abs-delta", type=float, default=0.05)
    parser.add_argument("--wing-selection-mode", default="target_delta", choices=["target_delta", "fixed_width"])
    parser.add_argument("--wing-width", type=float, default=25.0)
    parser.add_argument("--straddle-cheap-threshold", type=float, default=-1.0)
    parser.add_argument("--skew-extreme-threshold", type=float, default=1.0)
    parser.add_argument("--term-extreme-threshold", type=float, default=1.0)
    parser.add_argument("--danger-skip-threshold", type=float, default=2.5)
    parser.add_argument("--danger-quarter-size-threshold", type=float, default=1.5)
    parser.add_argument("--danger-half-size-threshold", type=float, default=0.75)
    parser.add_argument("--delta-neutral-trend-threshold", type=float, default=0.25)
    parser.add_argument("--delta-neutral-min-straddle-residual", type=float, default=-0.5)
    parser.add_argument("--trend-direction-threshold", type=float, default=0.25)
    parser.add_argument("--disable-four-model-ensemble", action="store_true")
    parser.add_argument("--model-sleeve-fraction", type=float, default=0.50)
    parser.add_argument("--confluence-sleeve-fraction", type=float, default=0.50)
    parser.add_argument("--atm-surface-min-residual", type=float, default=0.25)
    parser.add_argument("--skew-model-extreme-threshold", type=float, default=1.25)
    parser.add_argument("--duration-model-term-threshold", type=float, default=1.25)
    parser.add_argument("--allow-cheap-premium-entries", action="store_true")
    parser.add_argument("--hard-term-ratio-skip-threshold", type=float, default=1.50)
    parser.add_argument("--hard-realized-skip-threshold", type=float, default=1.75)
    parser.add_argument("--hard-trend-skip-threshold", type=float, default=2.00)
    parser.add_argument("--disable-candidate-engine", action="store_true")
    parser.add_argument("--candidate-min-score", type=float, default=2.50)
    parser.add_argument("--candidate-half-score", type=float, default=2.25)
    parser.add_argument("--candidate-full-score", type=float, default=2.50)
    parser.add_argument("--candidate-max-sides", type=int, default=1)
    parser.add_argument("--candidate-min-credit", type=float, default=0.20)
    parser.add_argument("--candidate-min-credit-to-width", type=float, default=0.0125)
    parser.add_argument("--candidate-max-stop-loss-to-credit", type=float, default=4.50)
    parser.add_argument("--candidate-max-adverse-trend", type=float, default=0.65)
    parser.add_argument("--candidate-max-chase-trend", type=float, default=1.50)
    parser.add_argument("--candidate-max-adverse-skew", type=float, default=0.75)
    parser.add_argument("--candidate-max-abs-term-ratio-z", type=float, default=1.25)
    parser.add_argument("--candidate-max-abs-realized-z", type=float, default=1.50)
    parser.add_argument("--candidate-distance-weight", type=float, default=12.0)
    parser.add_argument("--max-open-trades-per-side", type=int, default=2)
    parser.add_argument("--max-open-trades-same-side-strike", type=int, default=1)
    parser.add_argument("--stop-cooldown-minutes", type=int, default=30)
    parser.add_argument("--same-side-stop-cooldown-minutes", type=int, default=120)
    parser.add_argument("--max-stops-per-side", type=int, default=2)
    parser.add_argument("--disable-intraday-memory-gate", action="store_true")
    parser.add_argument("--memory-term-ratio-skip-threshold", type=float, default=1.50)
    parser.add_argument("--memory-skew-skip-threshold", type=float, default=99.0)
    parser.add_argument("--memory-trend-skip-threshold", type=float, default=99.0)
    args = parser.parse_args()

    config = StrategyConfig(
        account_equity=args.account_equity,
        baseline_contracts=args.baseline_contracts,
        daily_credit_cap_pct=args.daily_credit_cap_pct,
        daily_loss_limit_pct=args.daily_loss_limit_pct,
        stop_multiple=args.stop_multiple,
        target_long_abs_delta=args.target_long_abs_delta,
        wing_selection_mode=args.wing_selection_mode,
        wing_width=args.wing_width,
        straddle_cheap_threshold=args.straddle_cheap_threshold,
        skew_extreme_threshold=args.skew_extreme_threshold,
        term_extreme_threshold=args.term_extreme_threshold,
        danger_skip_threshold=args.danger_skip_threshold,
        danger_quarter_size_threshold=args.danger_quarter_size_threshold,
        danger_half_size_threshold=args.danger_half_size_threshold,
        delta_neutral_trend_threshold=args.delta_neutral_trend_threshold,
        delta_neutral_min_straddle_residual=args.delta_neutral_min_straddle_residual,
        trend_direction_threshold=args.trend_direction_threshold,
        use_four_model_ensemble=not args.disable_four_model_ensemble,
        model_sleeve_fraction=args.model_sleeve_fraction,
        confluence_sleeve_fraction=args.confluence_sleeve_fraction,
        atm_surface_min_residual=args.atm_surface_min_residual,
        skew_model_extreme_threshold=args.skew_model_extreme_threshold,
        duration_model_term_threshold=args.duration_model_term_threshold,
        require_positive_premium_richness=not args.allow_cheap_premium_entries,
        hard_term_ratio_skip_threshold=args.hard_term_ratio_skip_threshold,
        hard_realized_skip_threshold=args.hard_realized_skip_threshold,
        hard_trend_skip_threshold=args.hard_trend_skip_threshold,
        use_candidate_engine=not args.disable_candidate_engine,
        candidate_min_score=args.candidate_min_score,
        candidate_half_score=args.candidate_half_score,
        candidate_full_score=args.candidate_full_score,
        candidate_max_sides=args.candidate_max_sides,
        candidate_min_credit=args.candidate_min_credit,
        candidate_min_credit_to_width=args.candidate_min_credit_to_width,
        candidate_max_stop_loss_to_credit=args.candidate_max_stop_loss_to_credit,
        candidate_max_adverse_trend=args.candidate_max_adverse_trend,
        candidate_max_chase_trend=args.candidate_max_chase_trend,
        candidate_max_adverse_skew=args.candidate_max_adverse_skew,
        candidate_max_abs_term_ratio_z=args.candidate_max_abs_term_ratio_z,
        candidate_max_abs_realized_z=args.candidate_max_abs_realized_z,
        candidate_distance_weight=args.candidate_distance_weight,
        max_open_trades_per_side=args.max_open_trades_per_side,
        max_open_trades_same_side_strike=args.max_open_trades_same_side_strike,
        stop_cooldown_minutes=args.stop_cooldown_minutes,
        same_side_stop_cooldown_minutes=args.same_side_stop_cooldown_minutes,
        max_stops_per_side=args.max_stops_per_side,
        use_intraday_memory_gate=not args.disable_intraday_memory_gate,
        memory_term_ratio_skip_threshold=args.memory_term_ratio_skip_threshold,
        memory_skew_skip_threshold=args.memory_skew_skip_threshold,
        memory_trend_skip_threshold=args.memory_trend_skip_threshold,
    )

    summaries = []
    trades = []
    candidates = []
    stops = []
    for trade_date in args.dates:
        summary, day_trades, day_candidates, day_stops = run_day(
            args.symbol,
            trade_date,
            Path(args.processed_dir),
            config,
            args.signals_filename,
        )
        summaries.append(summary)
        trades.extend(day_trades)
        candidates.extend(day_candidates)
        stops.extend(day_stops)
        print(f"{trade_date} trades={summary['trades']} net_pnl={summary['net_pnl']} return={summary['return_on_equity']}")

    results_dir = Path(args.results_dir)
    write_csv(results_dir / "daily_summary.csv", summaries)
    write_csv(results_dir / "trades.csv", trades)
    write_csv(results_dir / "candidate_diagnostics.csv", candidates)
    write_csv(results_dir / "stop_diagnostics.csv", stops)
    print(f"wrote {results_dir / 'daily_summary.csv'}")
    print(f"wrote {results_dir / 'trades.csv'}")
    print(f"wrote {results_dir / 'candidate_diagnostics.csv'}")
    print(f"wrote {results_dir / 'stop_diagnostics.csv'}")


if __name__ == "__main__":
    main()
