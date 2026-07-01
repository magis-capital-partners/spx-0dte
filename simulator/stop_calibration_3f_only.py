"""Re-run Test 3F only using frozen winners from 3A-3D full run."""
from __future__ import annotations

from pathlib import Path

from stop_calibration_runner import (
    DEFAULT_PROCESSED,
    DEFAULT_RESULTS,
    base_config,
    discover_dates,
    pick_best,
    run_config,
)

# Frozen from full 391-day run (2026-06-30)
CORE = {
    "stop_multiple": 3.0,
    "stop_confirmation_count": 2,
    "daily_loss_limit_pct": 0.0225,
    "flatten_on_daily_loss": True,
    "flatten_loss_limit_pct": 0.035,
    "daily_credit_cap_pct": 0.015,
}

F_VARIANTS = [
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


def main() -> None:
    processed = Path(DEFAULT_PROCESSED)
    results = Path(DEFAULT_RESULTS)
    dates = discover_dates(processed, "SPXW")
    train = 40
    rows = []
    for vid, extra in F_VARIANTS:
        print(f"Running {vid}...")
        cfg_kwargs = {**CORE, **extra}
        row = run_config(
            vid, "3F", base_config(**cfg_kwargs), dates, train,
            processed, "SPXW", "signals_unconditional.csv", results,
        )
        rows.append(row)
        print(f"  trades={row['trades']} CAGR={row['cagr_pct']:.1f}% worst={row['worst_day_pct']:.1f}%")
    best = pick_best(rows, prioritize_tail=True)
    print(f"Best 3F: {best['variant_id']}")


if __name__ == "__main__":
    main()
