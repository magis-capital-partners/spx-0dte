"""Run Test 3A–3F stop calibration suite on 391 OOS days (wide wings substrate)."""
from __future__ import annotations

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
    stop_diagnostics_to_rows,
    trades_to_rows,
)
from regime_validation import apply_rolling_baseline, discover_dates
from unconditional_baseline import FixedSizePolicy, build_unconditional_config, trade_stats

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROCESSED = ROOT / "data" / "processed"
DEFAULT_RESULTS = ROOT / "data" / "stop_calibration"
TRADING_DAYS = 252


def wide_wings() -> dict:
    return {
        "put_wing_width": 200.0,
        "call_wing_width": 75.0,
        "wing_selection_mode": "fixed_width",
        "max_wing_width": 400.0,
        "use_net_long_overlay": False,
        "use_short_leg_stops": True,
        "record_tranche_summaries": False,
    }


def base_config(stop_multiple: float = 3.0, **overrides) -> StrategyConfig:
    cfg = build_unconditional_config(stop_multiple=stop_multiple)
    return replace(cfg, **wide_wings(), **overrides)


def portfolio_stats(daily_rows: Sequence[dict], account_equity: float) -> dict:
    days = len(daily_rows)
    if days == 0:
        return {"days": 0}
    equity = account_equity
    peak = account_equity
    max_dd = 0.0
    worst = 0.0
    rets: List[float] = []
    for row in daily_rows:
        pnl = float(row["net_pnl"])
        worst = min(worst, pnl)
        rets.append(pnl / equity if equity else 0.0)
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    total_ret = equity / account_equity - 1.0
    years = days / TRADING_DAYS
    cagr = ((1 + total_ret) ** (1 / years) - 1.0) if years > 0 and total_ret > -1 else 0.0
    std = pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mean(rets) / std) * math.sqrt(TRADING_DAYS) if std > 0 else 0.0
    stops = sum(int(r["stopped_trades"]) for r in daily_rows)
    trades = sum(int(r["trades"]) for r in daily_rows)
    return {
        "days": days,
        "trades": trades,
        "stopped_trades": stops,
        "stop_rate": round(stops / trades, 4) if trades else 0.0,
        "net_pnl": round(equity - account_equity, 2),
        "cagr_pct": round(cagr * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "worst_day": round(worst, 2),
        "worst_day_pct": round(worst / account_equity * 100, 2),
        "day_win_rate": round(sum(1 for r in rets if r > 0) / days, 4),
    }


def run_config(
    variant_id: str,
    phase: str,
    config: StrategyConfig,
    dates: List[str],
    train_count: int,
    processed_dir: Path,
    symbol: str,
    signals_filename: str,
    results_dir: Path,
) -> dict:
    policy = FixedSizePolicy()
    daily_rows: List[dict] = []
    all_trades: List[dict] = []
    stop_rows: List[dict] = []

    for index in range(train_count, len(dates)):
        test_date = dates[index]
        train_dates = dates[index - train_count : index]
        apply_rolling_baseline(processed_dir, symbol, train_dates, test_date, signals_filename)
        day_dir = processed_dir / f"symbol={symbol}" / f"date={test_date}"
        result = simulate_day(
            read_quotes_csv(day_dir / "normalized_option_quotes.csv"),
            read_signals_csv(day_dir / signals_filename),
            config=config,
            policy=policy,
        )
        day_trades = trades_to_rows(result.trades)
        day_stops = stop_diagnostics_to_rows(result.trades)
        for row in day_trades:
            row["date"] = test_date
            all_trades.append(row)
        for row in day_stops:
            row["date"] = test_date
            stop_rows.append(row)
        daily_rows.append(
            {
                "date": test_date,
                "trades": len(result.trades),
                "stopped_trades": sum(1 for t in result.trades if t.stopped),
                "net_pnl": round(result.net_pnl, 2),
                "halted": result.halted,
            }
        )

    spread_trades = [r for r in all_trades if r.get("model") != "net_long_overlay"]
    ts = trade_stats(spread_trades)
    port = portfolio_stats(daily_rows, config.account_equity)

    out_dir = results_dir / phase / variant_id
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "daily_summary.csv", daily_rows)
    write_csv(out_dir / "trades.csv", all_trades)
    write_csv(out_dir / "stop_diagnostics.csv", stop_rows)

    stopped = [r for r in spread_trades if str(r.get("stopped")).lower() in {"true", "1"}]
    stopped_pnl = mean(float(r["net_pnl"]) for r in stopped) if stopped else 0.0

    return {
        "variant_id": variant_id,
        "phase": phase,
        **port,
        "spread_win_rate": ts["win_rate"],
        "spread_expectancy": ts["expectancy_per_trade"],
        "avg_stopped_pnl": round(stopped_pnl, 2),
        "stop_multiple": config.stop_multiple,
        "stop_mode": config.stop_mode,
        "stop_confirmation_count": config.stop_confirmation_count,
        "spread_stop_loss_multiple": config.spread_stop_loss_multiple,
        "flatten_on_daily_loss": config.flatten_on_daily_loss,
        "flatten_loss_limit_pct": config.flatten_loss_limit_pct,
        "daily_loss_limit_pct": config.daily_loss_limit_pct,
        "candidate_min_score": config.candidate_min_score,
    }


def pick_best(rows: Sequence[dict], prioritize_tail: bool = False) -> dict:
    def score(row: dict) -> float:
        cagr = row.get("cagr_pct", 0.0)
        worst = row.get("worst_day_pct", -100.0)
        if prioritize_tail:
            bonus = 15.0 if worst >= -5.0 else 0.0
            penalty = 0.0 if worst >= -7.0 else abs(worst + 7.0) * 0.3
            return cagr + bonus - penalty
        penalty = 0.0 if worst >= -7.0 else abs(worst + 7.0) * 0.2
        return cagr - penalty

    return max(rows, key=score)


def build_report(all_rows: Sequence[dict], winners: dict) -> str:
    lines = [
        "# Stop Calibration Results — Test 3A–3F",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "Substrate: wide wings (put 200pt / call 75pt), 391 OOS days, gates off except 3F.",
        "",
        "## Phase winners",
        "",
    ]
    for phase, row in winners.items():
        lines.append(
            f"- **{phase}** → `{row['variant_id']}`: "
            f"CAGR {row['cagr_pct']:.1f}%, Sharpe {row['sharpe']:.2f}, "
            f"worst {row['worst_day_pct']:.1f}%, win {row['spread_win_rate']*100:.1f}%, "
            f"stop {row['stop_rate']*100:.1f}%"
        )

    lines.extend(["", "## All variants", ""])
    lines.append(
        "| Phase | Variant | CAGR | Sharpe | Worst% | Win% | Stop% | E[trade] | Avg stop P&L |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in sorted(all_rows, key=lambda r: (r["phase"], -r["cagr_pct"])):
        lines.append(
            f"| {row['phase']} | {row['variant_id']} | {row['cagr_pct']:.1f}% | {row['sharpe']:.2f} | "
            f"{row['worst_day_pct']:.1f}% | {row['spread_win_rate']*100:.1f}% | "
            f"{row['stop_rate']*100:.1f}% | ${row['spread_expectancy']:,.0f} | ${row['avg_stopped_pnl']:,.0f} |"
        )

    final = winners.get("3F") or winners.get("3D") or winners.get("3C")
    lines.extend(
        [
            "",
            "## Final recommendation",
            "",
            f"Best overall calibrated config: **`{final['variant_id']}`** "
            f"({final['cagr_pct']:.1f}% CAGR, {final['worst_day_pct']:.1f}% worst day).",
            "",
            "MBH targets: ~30–40% CAGR, ~65% win, ~4–5% worst day, ~2.25% all-stop portfolio cap.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--processed-dir", default=str(DEFAULT_PROCESSED))
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS))
    parser.add_argument("--train-count", type=int, default=40)
    parser.add_argument("--symbol", default="SPXW")
    parser.add_argument("--signals-filename", default="signals_unconditional.csv")
    parser.add_argument("--max-days", type=int, default=0, help="Limit OOS days (0=all)")
    parser.add_argument("--from-phase", default="", help="Run only this phase (3A,3B,3C,3D,3F)")
    parser.add_argument("--resume-winners", default="", help="JSON path to phase_winners for resume")
    args = parser.parse_args()

    processed_dir = Path(args.processed_dir)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    dates = discover_dates(processed_dir, args.symbol)
    if args.max_days > 0:
        dates = dates[: args.train_count + args.max_days]
    if len(dates) <= args.train_count:
        raise SystemExit("Not enough dates")

    all_rows: List[dict] = []
    winners: Dict[str, dict] = {}

    # --- 3A: stop multiples ---
    phase = "3A"
    phase_rows = []
    for mult in (2.0, 2.5, 3.0, 3.5):
        vid = f"3A_stop_{mult:.1f}x"
        print(f"Running {vid}...")
        row = run_config(
            vid, phase, base_config(stop_multiple=mult), dates, args.train_count,
            processed_dir, args.symbol, args.signals_filename, results_dir,
        )
        phase_rows.append(row)
        all_rows.append(row)
        print(f"  CAGR {row['cagr_pct']:.1f}% worst {row['worst_day_pct']:.1f}% stop {row['stop_rate']*100:.1f}%")
    best_3a = pick_best(phase_rows)
    winners[phase] = best_3a
    best_mult = best_3a["stop_multiple"]

    # --- 3B: trigger/fill on best multiple ---
    phase = "3B"
    phase_rows = []
    b_variants = [
        ("3B_ask_baseline", {}),
        ("3B_ask_slip_0.05", {"stop_fill_slippage": 0.05}),
        ("3B_short_mid", {"stop_mode": "short_mid"}),
        ("3B_spread_1.5x", {"stop_mode": "spread_value", "spread_stop_loss_multiple": 1.5}),
        ("3B_spread_2.0x", {"stop_mode": "spread_value", "spread_stop_loss_multiple": 2.0}),
        ("3B_confirm_2bar", {"stop_confirmation_count": 2}),
    ]
    for vid, extra in b_variants:
        print(f"Running {vid}...")
        row = run_config(
            vid, phase, base_config(stop_multiple=best_mult, **extra), dates, args.train_count,
            processed_dir, args.symbol, args.signals_filename, results_dir,
        )
        phase_rows.append(row)
        all_rows.append(row)
        print(f"  CAGR {row['cagr_pct']:.1f}% worst {row['worst_day_pct']:.1f}%")
    best_3b = pick_best(phase_rows)
    winners[phase] = best_3b

    best_3b_cfg_kwargs = {"stop_multiple": best_3b["stop_multiple"]}
    for vid, extra in b_variants:
        if vid == best_3b["variant_id"]:
            best_3b_cfg_kwargs.update(extra)
            break

    # --- 3C: post-stop rules ---
    phase = "3C"
    phase_rows = []
    c_variants = [
        ("3C_baseline", {"same_side_stop_cooldown_minutes": 0, "max_stops_per_side": 999}),
        ("3C_cooldown_120", {"same_side_stop_cooldown_minutes": 120, "max_stops_per_side": 999}),
        ("3C_max2_stops_side", {"max_stops_per_side": 2}),
        ("3C_no_same_strike", {"block_same_strike_after_stop": True}),
        (
            "3C_cooldown_nostrike",
            {
                "same_side_stop_cooldown_minutes": 120,
                "max_stops_per_side": 2,
                "block_same_strike_after_stop": True,
            },
        ),
    ]
    for vid, extra in c_variants:
        print(f"Running {vid}...")
        row = run_config(
            vid, phase,
            base_config(**best_3b_cfg_kwargs, **extra),
            dates, args.train_count, processed_dir, args.symbol, args.signals_filename, results_dir,
        )
        phase_rows.append(row)
        all_rows.append(row)
        print(f"  CAGR {row['cagr_pct']:.1f}% worst {row['worst_day_pct']:.1f}%")
    best_3c = pick_best(phase_rows)
    winners[phase] = best_3c

    best_3c_extra = {}
    for vid, extra in c_variants:
        if vid == best_3c["variant_id"]:
            best_3c_extra = extra
            break

    # --- 3D: portfolio governor ---
    phase = "3D"
    phase_rows = []
    d_variants = [
        ("3D_halt_2.25", {"daily_loss_limit_pct": 0.0225, "flatten_on_daily_loss": False}),
        (
            "3D_flatten_2.25",
            {
                "daily_loss_limit_pct": 0.0225,
                "flatten_on_daily_loss": True,
                "flatten_loss_limit_pct": 0.0225,
            },
        ),
        (
            "3D_flatten_3.5",
            {
                "daily_loss_limit_pct": 0.0225,
                "flatten_on_daily_loss": True,
                "flatten_loss_limit_pct": 0.035,
            },
        ),
    ]
    for vid, extra in d_variants:
        print(f"Running {vid}...")
        row = run_config(
            vid, phase,
            base_config(**best_3b_cfg_kwargs, **best_3c_extra, **extra),
            dates, args.train_count, processed_dir, args.symbol, args.signals_filename, results_dir,
        )
        phase_rows.append(row)
        all_rows.append(row)
        print(f"  CAGR {row['cagr_pct']:.1f}% worst {row['worst_day_pct']:.1f}%")
    best_3d = pick_best(phase_rows, prioritize_tail=True)
    winners[phase] = best_3d

    best_3d_extra = {}
    for vid, extra in d_variants:
        if vid == best_3d["variant_id"]:
            best_3d_extra = extra
            break

    # --- 3F: selective entry on top of 3D stop stack ---
    phase = "3F"
    phase_rows = []
    core = {**best_3b_cfg_kwargs, **best_3c_extra, **best_3d_extra, "daily_credit_cap_pct": 0.015}
    f_variants = [
        (
            "3F_gated_2.50",
            {
                "candidate_min_score": 2.50,
                "candidate_half_score": 2.25,
                "candidate_full_score": 2.50,
                "require_positive_premium_richness": True,
                "atm_surface_min_residual": 0.25,
            },
        ),
        (
            "3F_ablate_cheap_2.40",
            {
                "candidate_min_score": 2.40,
                "candidate_half_score": 2.25,
                "candidate_full_score": 2.40,
                "require_positive_premium_richness": False,
                "hard_term_ratio_skip_threshold": 99.0,
                "hard_trend_skip_threshold": 99.0,
            },
        ),
        (
            "3F_harvest_2.50",
            {
                "use_harvest_mode": True,
                "harvest_min_score": 2.25,
                "harvest_base_size_fraction": 0.25,
                "require_positive_premium_richness": False,
            },
        ),
        (
            "3F_event_time_2.50",
            {
                "candidate_min_score": 2.50,
                "candidate_half_score": 2.25,
                "candidate_full_score": 2.50,
                "require_positive_premium_richness": True,
                "atm_surface_min_residual": 0.25,
                "use_time_of_day_controls": True,
                "use_event_controls": True,
                "stop_cooldown_minutes": 30,
                "same_side_stop_cooldown_minutes": 120,
                "max_stops_per_side": 2,
                "max_open_trades_per_side": 2,
                "max_open_trades_same_side_strike": 1,
            },
        ),
    ]
    for vid, extra in f_variants:
        print(f"Running {vid}...")
        cfg_kwargs = {**core, **extra}
        row = run_config(
            vid, phase, base_config(**cfg_kwargs), dates, args.train_count,
            processed_dir, args.symbol, args.signals_filename, results_dir,
        )
        phase_rows.append(row)
        all_rows.append(row)
        print(f"  CAGR {row['cagr_pct']:.1f}% worst {row['worst_day_pct']:.1f}% trades {row['trades']}")
    best_3f = pick_best(phase_rows, prioritize_tail=True)
    winners[phase] = best_3f

    write_csv(results_dir / "calibration_summary.csv", all_rows)
    (results_dir / "calibration_summary.json").write_text(json.dumps(all_rows, indent=2), encoding="utf-8")
    (results_dir / "phase_winners.json").write_text(json.dumps(winners, indent=2, default=str), encoding="utf-8")

    report = build_report(all_rows, winners)
    report_path = ROOT / f"stop_calibration_results_{datetime.now().strftime('%Y-%m-%d')}.md"
    report_path.write_text(report, encoding="utf-8")
    (results_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"\nWrote {results_dir / 'calibration_summary.csv'}")
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    main()
