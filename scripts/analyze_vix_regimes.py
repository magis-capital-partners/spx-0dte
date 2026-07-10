"""Bucket backtest P&L by daily VIX regime using an existing dashboard run.

Joins trades.csv / daily_summary.csv with data/calendar/vix_daily.csv and prints
regime attribution tables. Read-only on simulation outputs.

Usage:
    python scripts/analyze_vix_regimes.py
    python scripts/analyze_vix_regimes.py --run-dir data/dashboard_runs/p3_poststop_cooldown_120
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from vix_daily import DEFAULT_VIX_CSV, load_vix_daily, regime_bucket  # noqa: E402


def analyze_run(run_dir: Path, vix_csv: Path) -> dict:
    import pandas as pd

    daily_path = run_dir / "daily_summary.csv"
    trades_path = run_dir / "trades.csv"
    if not daily_path.exists():
        raise FileNotFoundError(f"Missing {daily_path}")

    vix_by_date = load_vix_daily(vix_csv)
    daily = pd.read_csv(daily_path, parse_dates=["date"])
    daily["date_str"] = daily["date"].dt.strftime("%Y-%m-%d")
    daily["vix_open"] = daily["date_str"].map(lambda d: vix_by_date[d].open if d in vix_by_date else None)
    daily["vix_bucket"] = daily["vix_open"].map(lambda v: regime_bucket(v) if pd.notna(v) else "missing")

    bucket_daily = (
        daily.groupby("vix_bucket", dropna=False)
        .agg(
            days=("date", "count"),
            net_pnl=("net_pnl", "sum"),
            mean_day=("net_pnl", "mean"),
            win_rate=("net_pnl", lambda s: float((s > 0).mean())),
            stop_rate=("stopped_trades", "sum"),
            trades=("trades", "sum"),
            halted=("halted", "sum"),
        )
        .reset_index()
    )
    bucket_daily["stop_rate"] = (bucket_daily["stop_rate"] / bucket_daily["trades"].clip(lower=1)).round(3)
    bucket_daily["net_pnl"] = bucket_daily["net_pnl"].round(0)
    bucket_daily["mean_day"] = bucket_daily["mean_day"].round(0)
    bucket_daily["win_rate"] = bucket_daily["win_rate"].round(3)

    trade_bucket = None
    if trades_path.exists():
        trades = pd.read_csv(trades_path, parse_dates=["date", "entry_time"])
        trades["date_str"] = trades["date"].dt.strftime("%Y-%m-%d")
        trades["vix_open"] = trades["date_str"].map(lambda d: vix_by_date[d].open if d in vix_by_date else None)
        trades["vix_bucket"] = trades["vix_open"].map(lambda v: regime_bucket(v) if pd.notna(v) else "missing")
        trade_bucket = (
            trades.groupby("vix_bucket", dropna=False)
            .agg(
                trades=("net_pnl", "size"),
                net_pnl=("net_pnl", "sum"),
                expectancy=("net_pnl", "mean"),
                stop_rate=("stopped", "mean"),
                avg_credit=("entry_credit", "mean"),
            )
            .reset_index()
        )
        trade_bucket["net_pnl"] = trade_bucket["net_pnl"].round(0)
        trade_bucket["expectancy"] = trade_bucket["expectancy"].round(2)
        trade_bucket["stop_rate"] = trade_bucket["stop_rate"].round(3)
        trade_bucket["avg_credit"] = trade_bucket["avg_credit"].round(2)

    missing_days = int((daily["vix_bucket"] == "missing").sum())
    return {
        "run_dir": str(run_dir),
        "days": int(len(daily)),
        "missing_vix_days": missing_days,
        "daily_by_bucket": bucket_daily.to_dict(orient="records"),
        "trades_by_bucket": trade_bucket.to_dict(orient="records") if trade_bucket is not None else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze dashboard run P&L by VIX regime.")
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="Dashboard or vix_regime_tests run dir (default: poststop, else baseline_tod).",
    )
    parser.add_argument("--vix-csv", type=Path, default=DEFAULT_VIX_CSV)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "vix_regime_analysis" / "summary.json")
    args = parser.parse_args()

    run_dir = args.run_dir
    if run_dir is None:
        poststop = ROOT / "data" / "dashboard_runs" / "p3_poststop_cooldown_120"
        baseline = ROOT / "data" / "vix_regime_tests" / "baseline_tod"
        run_dir = poststop if poststop.exists() else baseline

    report = analyze_run(run_dir, args.vix_csv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"=== VIX regime analysis: {run_dir.name} ===")
    print(f"Days: {report['days']}  missing VIX: {report['missing_vix_days']}")
    print("\n--- Daily P&L by VIX bucket ---")
    for row in report["daily_by_bucket"]:
        print(
            f"  {row['vix_bucket']:<18} days={row['days']:>4}  "
            f"pnl=${row['net_pnl']:>12,.0f}  mean=${row['mean_day']:>8,.0f}  "
            f"win={row['win_rate']:.1%}  stop={row['stop_rate']:.1%}"
        )
    if report["trades_by_bucket"]:
        print("\n--- Trade-level by VIX bucket ---")
        for row in report["trades_by_bucket"]:
            print(
                f"  {row['vix_bucket']:<18} n={row['trades']:>5}  "
                f"pnl=${row['net_pnl']:>12,.0f}  exp=${row['expectancy']:>7.2f}  "
                f"credit=${row['avg_credit']:.2f}  stop={row['stop_rate']:.1%}"
            )
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
