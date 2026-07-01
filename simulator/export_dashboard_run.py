"""Export a stop-calibration variant to data/dashboard_runs/ for the dashboard."""
from __future__ import annotations

import argparse
from pathlib import Path

from stop_calibration_runner import (
    DEFAULT_PROCESSED,
    base_config,
    discover_dates,
    run_config,
)

# Frozen winners from full 391-day calibration (2026-06-30)
WINNERS = {
    "stop_multiple": 3.0,
    "stop_confirmation_count": 2,
    "same_side_stop_cooldown_minutes": 0,
    "max_stops_per_side": 999,
    "daily_loss_limit_pct": 0.0225,
    "flatten_on_daily_loss": True,
    "flatten_loss_limit_pct": 0.035,
}

PRESETS = {
    "3d_flatten_3_5": ("3D_flatten_3.5", WINNERS, "3D flatten @ -3.5% (wide wings + 3x stop)"),
    "test1_unconditional": None,  # uses unconditional_baseline output dir instead
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset", default="3d_flatten_3_5")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--train-count", type=int, default=40)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    preset = args.preset
    if preset != "3d_flatten_3_5":
        raise SystemExit(f"Only 3d_flatten_3_5 export is implemented; got {preset}")

    vid, kwargs, _ = PRESETS[preset]
    out = Path(args.out_dir) if args.out_dir else root / "data" / "dashboard_runs" / preset
    processed = Path(DEFAULT_PROCESSED)
    dates = discover_dates(processed, "SPXW")
    cfg = base_config(**kwargs)
    print(f"Running {vid} -> {out} ({len(dates) - args.train_count} OOS days)...")
    row = run_config(
        vid, "3D", cfg, dates, args.train_count,
        processed, "SPXW", "signals_unconditional.csv", out.parent,
    )
    # run_config writes to out.parent/phase/variant_id; move to flat out dir
    src = out.parent / "3D" / vid
    out.mkdir(parents=True, exist_ok=True)
    for name in ("daily_summary.csv", "trades.csv", "stop_diagnostics.csv"):
        (src / name).replace(out / name)
    print(f"  CAGR {row['cagr_pct']:.1f}%  Sharpe {row['sharpe']:.2f}  worst {row['worst_day_pct']:.1f}%  trades {row['trades']}")


if __name__ == "__main__":
    main()
