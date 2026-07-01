"""Aggregate stop calibration results from saved daily_summary.csv files."""
from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import mean, pstdev

from historical_baselines import read_csv, write_csv
from stop_calibration_runner import build_report, pick_best, trade_stats

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "data" / "stop_calibration"
EQUITY = 13_000_000.0
TRADING_DAYS = 252


def summarize_variant(phase: str, variant_id: str) -> dict:
    daily_path = RESULTS / phase / variant_id / "daily_summary.csv"
    trades_path = RESULTS / phase / variant_id / "trades.csv"
    rows = read_csv(daily_path)
    trades = read_csv(trades_path)
    spread = [r for r in trades if r.get("model") != "net_long_overlay"]
    ts = trade_stats(spread)
    equity = EQUITY
    peak = EQUITY
    max_dd = 0.0
    worst = 0.0
    rets = []
    for row in rows:
        pnl = float(row["net_pnl"])
        worst = min(worst, pnl)
        rets.append(pnl / equity if equity else 0.0)
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    days = len(rows)
    total_ret = equity / EQUITY - 1.0
    years = days / TRADING_DAYS
    cagr = ((1 + total_ret) ** (1 / years) - 1.0) if years > 0 and total_ret > -1 else 0.0
    std = pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mean(rets) / std) * math.sqrt(TRADING_DAYS) if std > 0 else 0.0
    stops = sum(int(r.get("stopped_trades", 0)) for r in rows)
    ntrades = sum(int(r.get("trades", 0)) for r in rows)
    stopped = [r for r in spread if str(r.get("stopped")).lower() in {"true", "1"}]
    avg_stop = mean(float(r["net_pnl"]) for r in stopped) if stopped else 0.0
    return {
        "variant_id": variant_id,
        "phase": phase,
        "days": days,
        "trades": ntrades,
        "stopped_trades": stops,
        "stop_rate": round(stops / ntrades, 4) if ntrades else 0.0,
        "net_pnl": round(equity - EQUITY, 2),
        "cagr_pct": round(cagr * 100, 2),
        "sharpe": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "worst_day": round(worst, 2),
        "worst_day_pct": round(worst / EQUITY * 100, 2),
        "day_win_rate": round(sum(1 for r in rets if r > 0) / days, 4) if days else 0.0,
        "spread_win_rate": ts["win_rate"],
        "spread_expectancy": ts["expectancy_per_trade"],
        "avg_stopped_pnl": round(avg_stop, 2),
    }


def main() -> None:
    all_rows = []
    winners = {}
    for phase_dir in sorted(RESULTS.iterdir()):
        if not phase_dir.is_dir() or phase_dir.name.startswith("."):
            continue
        phase = phase_dir.name
        phase_rows = []
        for variant_dir in sorted(phase_dir.iterdir()):
            if not variant_dir.is_dir():
                continue
            if not (variant_dir / "daily_summary.csv").exists():
                continue
            row = summarize_variant(phase, variant_dir.name)
            if row["days"] < 50:
                continue
            phase_rows.append(row)
            all_rows.append(row)
        if phase_rows:
            winners[phase] = pick_best(phase_rows, prioritize_tail=(phase in {"3D", "3F"}))
    write_csv(RESULTS / "calibration_summary.csv", all_rows)
    (RESULTS / "calibration_summary.json").write_text(json.dumps(all_rows, indent=2), encoding="utf-8")
    (RESULTS / "phase_winners.json").write_text(json.dumps(winners, indent=2), encoding="utf-8")
    report = build_report(all_rows, winners)
    (ROOT / "stop_calibration_results_2026-06-30.md").write_text(report, encoding="utf-8")
    (RESULTS / "report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
