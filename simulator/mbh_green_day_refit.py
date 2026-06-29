"""Refit candidate score weights using MBH daily returns as tranche labels.

Instead of training on sparse clone trade win/loss (~24 trades), label every
core-spread candidate on MBH green days as positive and red days as negative.
This aligns the score model with MBH's deployment environment rather than our
under-deployed clone outcomes.
"""
from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "simulator"
sys.path.insert(0, str(SIM))
sys.path.insert(0, str(ROOT))

from gate_attribution import read_mbh_daily  # noqa: E402
from live.strategy_profiles import PROFILES  # noqa: E402
from mbh_daily_comparison import series_stats  # noqa: E402
from mbh_simulator import (  # noqa: E402
    StrategyConfig,
    read_quotes_csv,
    read_signals_csv,
    simulate_day,
)
from pm_refinement_study import MBH_TARGET, compare_to_mbh  # noqa: E402
from regime_validation import (  # noqa: E402
    apply_rolling_baseline,
    discover_dates,
    read_event_calendar,
)
from robustness_study import profile_config, run_walkforward, write_csv  # noqa: E402
from score_model import (  # noqa: E402
    DEFAULT_SCORE_WEIGHTS,
    CandidateScoreWeights,
    fit_logistic_regression,
    save_score_weights,
    weights_from_logistic,
)
from signal_score_refit import safe_float, trade_feature_row  # noqa: E402
from summarize_run import summarize  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "mbh_green_day_refit"
DEFAULT_MODEL = ROOT / "data" / "models" / "mbh_green_day_weights.json"
PHASE0_TRANCHES = ROOT / "data" / "phase0_tranche_full" / "tranche_snapshots.csv"
MBH_SHEETS = [ROOT / "data" / "mbh_returns" / "2024.csv", ROOT / "data" / "mbh_returns" / "2025.csv"]
SIGNALS_FILENAME = "signals_regime_validation.csv"
OVERLAP_START = "2025-02-27"
OVERLAP_END = "2025-12-31"
CORE_SIDES = {"bull_put", "bear_call"}
CORE_SLEEVES = {"", "core", "exploratory"}


def read_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def load_mbh_by_date() -> Dict[str, float]:
    out: Dict[str, float] = {}
    for path in MBH_SHEETS:
        out.update(read_mbh_daily(path))
    return out


def candidate_feature_row(row: dict) -> List[float]:
    features, _ = trade_feature_row(row)
    return features


def tranche_side_row(
    row: dict,
    side: str,
    mbh_return: float,
    label: int,
) -> dict:
    return {
        "date": row["date"],
        "timestamp": row["timestamp"],
        "side": side,
        "status": "tranche_proxy",
        "reason": row.get("skip_reason", ""),
        "sleeve": "core",
        "score": safe_float(row.get("best_bull_put_score" if side == "bull_put" else "best_bear_call_score")),
        "short_delta": 0.18 if side == "bull_put" else -0.18,
        "credit_to_width": 0.03,
        "distance_pct": 0.015,
        "entry_straddle_residual_z": safe_float(row.get("straddle_residual_z")),
        "entry_skew_z": safe_float(row.get("skew_z")),
        "entry_term_ratio_z": safe_float(row.get("term_ratio_z")),
        "entry_trend_score": safe_float(row.get("trend_score")),
        "entry_realized_vs_implied_z": safe_float(row.get("realized_vs_implied_z")),
        "mbh_return": mbh_return,
        "label": label,
    }


def collect_from_phase0_tranches(
    tranche_path: Path,
    mbh_by_date: Dict[str, float],
    overlap_start: str,
    overlap_end: str,
    label_mode: str,
) -> List[dict]:
    rows: List[dict] = []
    for row in read_csv(tranche_path):
        day = row.get("date", "")
        if not (overlap_start <= day <= overlap_end):
            continue
        if day not in mbh_by_date:
            continue
        label = label_for_mbh_return(mbh_by_date[day], label_mode)
        if label is None:
            continue
        rows.append(tranche_side_row(row, "bull_put", mbh_by_date[day], label))
        rows.append(tranche_side_row(row, "bear_call", mbh_by_date[day], label))
    return rows


def label_for_mbh_return(mbh_return: float | None, mode: str) -> int | None:
    if mbh_return is None:
        return None
    if mode == "green":
        return 1 if mbh_return > 0 else 0
    if mode == "strong_green":
        return 1 if mbh_return >= 0.01 else 0
    if mode == "non_red":
        return 1 if mbh_return >= 0 else 0
    raise ValueError(f"Unknown label mode: {mode}")


def collect_labeled_candidates(
    processed_dir: Path,
    symbol: str,
    all_dates: Sequence[str],
    overlap_dates: Sequence[str],
    train_count: int,
    config: StrategyConfig,
    mbh_by_date: Dict[str, float],
    label_mode: str,
) -> List[dict]:
    event_calendar = read_event_calendar(ROOT / "regime_expansion_dates_2025.csv")
    rows: List[dict] = []
    day_config_base = replace(config, record_tranche_summaries=True)
    total = len(overlap_dates)
    for idx, test_date in enumerate(overlap_dates, start=1):
        if test_date not in mbh_by_date:
            continue
        try:
            index = all_dates.index(test_date)
        except ValueError:
            continue
        if index < train_count:
            continue

        train_dates = all_dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, SIGNALS_FILENAME)
        day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
        quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
        signals = read_signals_csv(day_dir / SIGNALS_FILENAME)
        event_info = event_calendar.get(test_date, {"event_bucket": "unlabeled"})
        day_config = replace(day_config_base, event_bucket=event_info["event_bucket"])
        result = simulate_day(quotes, signals, config=day_config)

        mbh_return = mbh_by_date[test_date]
        label = label_for_mbh_return(mbh_return, label_mode)
        if label is None:
            continue

        for record in result.candidate_records:
            if record.side not in CORE_SIDES:
                continue
            if record.sleeve not in CORE_SLEEVES:
                continue
            rows.append(
                {
                    "date": test_date,
                    "timestamp": record.timestamp.isoformat(),
                    "side": record.side,
                    "status": record.status,
                    "reason": record.reason,
                    "sleeve": record.sleeve,
                    "score": record.score,
                    "short_delta": record.short_delta,
                    "credit_to_width": record.credit_to_width,
                    "distance_pct": record.distance_pct,
                    "entry_straddle_residual_z": record.straddle_residual_z,
                    "entry_skew_z": record.skew_z,
                    "entry_term_ratio_z": record.term_ratio_z,
                    "entry_trend_score": record.trend_score,
                    "entry_realized_vs_implied_z": record.realized_vs_implied_z,
                    "mbh_return": mbh_return,
                    "label": label,
                }
            )
        if idx % 10 == 0 or idx == total:
            print(f"  collected {idx}/{total} days ({len(rows):,} rows)", flush=True)
    return rows


def calibrate_weight_scale(rows: Sequence[dict], logistic_weights: Sequence[float], bias: float) -> float:
    positive_scores: List[float] = []
    for row in rows:
        if int(row["label"]) != 1:
            continue
        features = candidate_feature_row(row)
        positive_scores.append(bias + sum(weight * value for weight, value in zip(logistic_weights, features)))
    if not positive_scores:
        return 8.0
    target = 2.50
    median = sorted(positive_scores)[len(positive_scores) // 2]
    if abs(median) < 1e-6:
        return 8.0
    return target / median


def fit_logistic_pilot(features: Sequence[Sequence[float]], labels: Sequence[int]) -> tuple[List[float], float]:
    return fit_logistic_regression(features, labels, epochs=400, learning_rate=0.08)


def aggregate_day_rows(rows: Sequence[dict]) -> List[dict]:
    buckets: Dict[str, List[dict]] = {}
    for row in rows:
        buckets.setdefault(row["date"], []).append(row)
    out: List[dict] = []
    for day, day_rows in sorted(buckets.items()):
        numeric_keys = [
            "entry_straddle_residual_z",
            "entry_skew_z",
            "entry_term_ratio_z",
            "entry_trend_score",
            "entry_realized_vs_implied_z",
            "credit_to_width",
            "distance_pct",
        ]
        merged = dict(day_rows[0])
        merged["side"] = "bull_put"
        merged["short_delta"] = 0.18
        for key in numeric_keys:
            merged[key] = sum(safe_float(r.get(key)) for r in day_rows) / len(day_rows)
        out.append(merged)
    return out


def walkforward_day_accuracy(rows: Sequence[dict], min_train_days: int = 20) -> List[dict]:
    day_rows = aggregate_day_rows(rows)
    ordered = sorted(day_rows, key=lambda row: row["date"])
    out: List[dict] = []
    for index in range(min_train_days, len(ordered)):
        train = ordered[:index]
        test = ordered[index]
        x_train, y_train = zip(*((candidate_feature_row(row), int(row["label"])) for row in train))
        weights, bias = fit_logistic_pilot(x_train, y_train)
        features = candidate_feature_row(test)
        logit = bias + sum(w * x for w, x in zip(weights, features))
        predicted_green = 1 if logit > 0 else 0
        actual_green = int(test["label"])
        out.append(
            {
                "date": test["date"],
                "actual_green": actual_green,
                "predicted_green": predicted_green,
                "mbh_return": test["mbh_return"],
                "candidate_count": 1,
            }
        )
    return out


def build_report(
    rows: Sequence[dict],
    wf_rows: Sequence[dict],
    baseline_cmp: dict,
    refit_cmp: dict,
    baseline_summary: dict,
    refit_summary: dict,
    refit_weights: CandidateScoreWeights,
    scale: float,
    label_mode: str,
) -> str:
    labels = [int(row["label"]) for row in rows]
    pos_rate = sum(labels) / len(labels) if labels else 0.0
    wf_acc = (
        sum(1 for row in wf_rows if int(row["actual_green"]) == int(row["predicted_green"])) / len(wf_rows)
        if wf_rows
        else 0.0
    )
    lines = [
        "# MBH Green-Day Score Refit",
        "",
        f"- Label mode: **{label_mode}**",
        f"- Labeled candidate rows: **{len(rows):,}** ({len({row['date'] for row in rows})} days)",
        f"- Positive label rate: **{pos_rate:.1%}**",
        f"- Walk-forward day accuracy (pilot): **{wf_acc:.1%}** on {len(wf_rows)} holdout days",
        "",
        "## MBH shape comparison (overlap window)",
        "",
        "| Model | Trades | Active% | Ann return | Sharpe | Credit/day | Shape dist |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        f"| Hand-tuned weights | {baseline_cmp.get('trades', 0)} | "
        f"{baseline_cmp.get('active_pct', 0):.1%} | {baseline_cmp.get('ann_return', 0):.1%} | "
        f"{baseline_cmp.get('sharpe', 0):.2f} | {baseline_cmp.get('avg_daily_credit_pct', 0):.2%} | "
        f"{baseline_cmp.get('shape_distance', 0):.3f} |",
        f"| MBH green-day refit | {refit_cmp.get('trades', 0)} | "
        f"{refit_cmp.get('active_pct', 0):.1%} | {refit_cmp.get('ann_return', 0):.1%} | "
        f"{refit_cmp.get('sharpe', 0):.2f} | {refit_cmp.get('avg_daily_credit_pct', 0):.2%} | "
        f"{refit_cmp.get('shape_distance', 0):.3f} |",
        f"| MBH actual target | - | {MBH_TARGET['active_pct']:.1%} | {MBH_TARGET['ann_return']:.1%} | - | - | 0.000 |",
        "",
        "## Full-history backtest (same overlap window)",
        "",
        "| Model | CAGR | Max DD | Stop rate | Net P&L |",
        "| --- | ---: | ---: | ---: | ---: |",
        f"| Hand-tuned | {baseline_summary.get('cagr_pct', 0):.1f}% | "
        f"{baseline_summary.get('max_drawdown_pct', 0):.1f}% | "
        f"{baseline_summary.get('stop_rate', 0):.1%} | ${baseline_summary.get('net_pnl', 0):,.0f} |",
        f"| Refit | {refit_summary.get('cagr_pct', 0):.1f}% | "
        f"{refit_summary.get('max_drawdown_pct', 0):.1f}% | "
        f"{refit_summary.get('stop_rate', 0):.1%} | ${refit_summary.get('net_pnl', 0):,.0f} |",
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
    pnl_ok = refit_cmp.get("ann_return", 0) >= 0.10
    shape_ok = refit_cmp.get("shape_distance", 999) < baseline_cmp.get("shape_distance", 999)
    active_ok = refit_cmp.get("active_pct", 0) > baseline_cmp.get("active_pct", 0) + 0.05
    if shape_ok and pnl_ok and active_ok:
        lines.append(
            "- Refit improves MBH shape **and** keeps a positive P&L floor. Next: combine with relaxed "
            "cheap_premium gate and re-validate on period splits."
        )
    elif active_ok and not pnl_ok:
        lines.append(
            "- Refit opens deployment cadence but **P&L is still below floor**. Do not deploy harvest-mode "
            "gates without further feature work; tighten label mode or add tranche-level pseudo-labels."
        )
    else:
        lines.append(
            "- Refit does not yet beat hand-tuned weights on shape + P&L jointly. Keep as research weights; "
            "iterate on label design (strong-green only, score-weighted samples)."
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Refit score weights from MBH green-day tranche labels.")
    parser.add_argument("--processed-dir", type=Path, default=ROOT / "data" / "processed")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--model-out", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--account-equity", type=float, default=13_000_000.0)
    parser.add_argument("--profile", default="baseline")
    parser.add_argument("--overlap-start", default=OVERLAP_START)
    parser.add_argument("--overlap-end", default=OVERLAP_END)
    parser.add_argument("--label-mode", choices=["green", "strong_green", "non_red"], default="green")
    parser.add_argument(
        "--source",
        choices=["phase0_tranches", "simulate"],
        default="phase0_tranches",
        help="phase0_tranches uses cached tranche snapshots (fast); simulate re-runs each day (slow).",
    )
    parser.add_argument("--tranche-path", type=Path, default=PHASE0_TRANCHES)
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--skip-backtests", action="store_true")
    args = parser.parse_args()

    all_dates = discover_dates(args.processed_dir, args.symbol)
    overlap_dates = [d for d in all_dates if args.overlap_start <= d <= args.overlap_end]
    if len(overlap_dates) <= args.train_count:
        raise SystemExit("Not enough overlap dates for rolling baseline.")

    mbh_by_date = load_mbh_by_date()
    baseline_config = profile_config(args.profile, args.account_equity)
    labeled_path = args.results_dir / "labeled_candidates.csv"

    if args.skip_collect and labeled_path.exists():
        rows = read_csv(labeled_path)
    elif args.source == "phase0_tranches":
        print(f"Building labels from {args.tranche_path}...", flush=True)
        rows = collect_from_phase0_tranches(
            args.tranche_path,
            mbh_by_date,
            args.overlap_start,
            args.overlap_end,
            args.label_mode,
        )
    else:
        print(f"Collecting labeled candidates on {len(overlap_dates)} overlap days...", flush=True)
        rows = collect_labeled_candidates(
            args.processed_dir,
            args.symbol,
            all_dates,
            overlap_dates,
            args.train_count,
            baseline_config,
            mbh_by_date,
            args.label_mode,
        )
    if len(rows) < 100:
        raise SystemExit(f"Need at least 100 labeled candidate rows; found {len(rows)}.")

    features, labels = zip(*((candidate_feature_row(row), int(row["label"])) for row in rows))
    logistic_weights, bias = fit_logistic_regression(features, labels, epochs=800, learning_rate=0.06)
    scale = calibrate_weight_scale(rows, logistic_weights, bias)
    refit_weights = weights_from_logistic(logistic_weights, bias, scale=scale)
    wf_rows = walkforward_day_accuracy(rows)

    args.results_dir.mkdir(parents=True, exist_ok=True)
    write_csv(labeled_path, rows)
    write_csv(args.results_dir / "walkforward_day_predictions.csv", wf_rows)

    if args.skip_backtests:
        baseline_cmp = {}
        refit_cmp = {}
        baseline_summary = {"cagr_pct": 0.0, "max_drawdown_pct": 0.0, "stop_rate": 0.0, "net_pnl": 0.0}
        refit_summary = baseline_summary
    else:
        event_calendar = read_event_calendar(ROOT / "regime_expansion_dates_2025.csv")
        grid_dates = overlap_dates
        baseline_config_bt = replace(baseline_config, candidate_score_weights=DEFAULT_SCORE_WEIGHTS)
        refit_config_bt = replace(baseline_config, candidate_score_weights=refit_weights)

        print("Running baseline backtest...")
        baseline_daily, _ = run_walkforward(
            args.processed_dir, args.symbol, grid_dates, baseline_config_bt, args.train_count, event_calendar
        )
        print("Running refit backtest...")
        refit_daily, _ = run_walkforward(
            args.processed_dir, args.symbol, grid_dates, refit_config_bt, args.train_count, event_calendar
        )

        baseline_dir = args.results_dir / "baseline"
        refit_dir = args.results_dir / "refit"
        write_csv(baseline_dir / "daily_regime_validation.csv", baseline_daily)
        write_csv(refit_dir / "daily_regime_validation.csv", refit_daily)
        baseline_summary = summarize(baseline_dir, args.account_equity, compound=True)
        refit_summary = summarize(refit_dir, args.account_equity, compound=True)

        eval_start = grid_dates[args.train_count]
        baseline_cmp = compare_to_mbh(
            baseline_daily, args.account_equity, mbh_by_date, eval_start, args.overlap_end
        )
        refit_cmp = compare_to_mbh(
            refit_daily, args.account_equity, mbh_by_date, eval_start, args.overlap_end
        )
        write_csv(args.results_dir / "comparison_summary.csv", [
            {"model": "baseline", **baseline_cmp},
            {"model": "mbh_green_refit", **refit_cmp},
        ])

    save_score_weights(
        args.model_out,
        refit_weights,
        metadata={
            "label_mode": args.label_mode,
            "labeled_rows": len(rows),
            "overlap_start": args.overlap_start,
            "overlap_end": args.overlap_end,
            "profile": args.profile,
        },
    )
    report = build_report(
        rows,
        wf_rows,
        baseline_cmp,
        refit_cmp,
        baseline_summary,
        refit_summary,
        refit_weights,
        scale,
        args.label_mode,
    )
    (args.results_dir / "mbh_green_day_refit_report.md").write_text(report, encoding="utf-8")
    print(f"wrote {args.model_out}")
    print(f"wrote {args.results_dir / 'mbh_green_day_refit_report.md'}")
    print(report)


if __name__ == "__main__":
    main()
