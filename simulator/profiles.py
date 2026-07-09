"""Canonical strategy-config registry — the single source of truth.

Every consumer (backtest runners, dashboard export, and the live IB executor)
builds its ``StrategyConfig`` from the same functions here so a change in one
place moves the backtest, the dashboard, and live execution together. This is
what makes iterating on the config safe: add or edit a profile once, and it is
immediately testable in backtest and runnable live with no hand-syncing.

Frozen winners come from the full 391-day stop calibration (2026-06-30, Test
3A-3D) and the time-of-day sizing sweep (2026-07-01, Test 3G).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import time
from typing import Callable, Dict, List, Tuple

from mbh_simulator import StrategyConfig
from stop_calibration_runner import wide_wings
from unconditional_baseline import build_unconditional_config

# Production deployment target (dashboard #1, improvement-plan winner).
PRODUCTION_PROFILE = "p3_trend1_skew075"
PRODUCTION_SIZING_SCHEME = "linear_decay_downsize"
PRODUCTION_BASELINE_CONTRACTS = 31
PRODUCTION_ACCOUNT_EQUITY = 13_000_000.0
PRODUCTION_TRAIN_COUNT = 40
VIX_SKIP_OPEN_ABOVE = 35.0
VIX_ELEVATED_MIN = 25.0
VIX_ELEVATED_MAX = 35.0
VIX_ELEVATED_SCALE = 1.25
# Overnight Calmar winner (combo_skew065_flat325, July 2026): tighter skew gate + flatten.
PRODUCTION_SKEW_GATE = 0.65
PRODUCTION_FLATTEN_LOSS_LIMIT_PCT = 0.0325
PRODUCTION_DAILY_LOSS_LIMIT_PCT = 0.0225
# Peak morning elevated tranche: round(baseline × max_tod_mult × vix_elevated_scale).
PRODUCTION_MAX_CONTRACTS_PER_TRANCHE = 48  # round(31 × 1.25 × 1.25)
LIVE_PILOT_MAX_CONTRACTS_PER_TRANCHE = 3  # round(2 × 1.25 × 1.25)

# --------------------------------------------------------------------------- #
# Frozen 3D winner (wide wings + 3x stop + 2-bar confirm + flatten governor).
# --------------------------------------------------------------------------- #
WINNERS: Dict[str, object] = {
    "stop_multiple": 3.0,
    "stop_confirmation_count": 2,
    "same_side_stop_cooldown_minutes": 0,
    "max_stops_per_side": 999,
    "daily_loss_limit_pct": 0.0225,   # halt NEW entries here
    "flatten_on_daily_loss": True,
    "flatten_loss_limit_pct": 0.035,  # force-flatten OPEN positions here
    "entry_fill_slippage": 0.05,      # matches live entry_limit_concession
}


def build_3d_flatten_config(
    account_equity: float = 13_000_000.0,
    baseline_contracts: int = 31,
) -> StrategyConfig:
    """`3D_flatten_3.5`: the validated production candidate.

    Unconditional cadence (gates off), wide asymmetric wings (put 200 / call
    75), 3.0x short-leg stop with 2-bar confirmation, entries halt at -2.25%
    daily loss, open positions flattened at -3.5%. 26.3% CAGR / 73.2% win /
    -4.6% worst day on 391 OOS days.
    """
    cfg = build_unconditional_config(
        account_equity=account_equity,
        baseline_contracts=baseline_contracts,
        stop_multiple=float(WINNERS["stop_multiple"]),
    )
    winners_wo_stop = {k: v for k, v in WINNERS.items() if k != "stop_multiple"}
    return replace(cfg, **wide_wings(), **winners_wo_stop)


def build_p3_trend_skew_config(
    account_equity: float = PRODUCTION_ACCOUNT_EQUITY,
    baseline_contracts: int = PRODUCTION_BASELINE_CONTRACTS,
) -> StrategyConfig:
    """Production profile: 3D flatten + trend/skew entry gates.

    Skips bear calls when trend_score > 1.0 or skew_z > 0.75 (z-scored features).
    ~23% CAGR on eligible-calendar OOS path (805 days as of 2026-07).
    """
    return replace(
        build_3d_flatten_config(account_equity, baseline_contracts),
        candidate_max_adverse_trend=1.0,
        candidate_max_adverse_skew=0.75,
    )


def build_p3_poststop_cooldown_config(
    account_equity: float = PRODUCTION_ACCOUNT_EQUITY,
    baseline_contracts: int = PRODUCTION_BASELINE_CONTRACTS,
    cooldown_minutes: int = 120,
) -> StrategyConfig:
    """Production profile + same-side post-stop re-entry cooldown.

    After a stopped trade on puts or calls, block new entries on that side only
    for ``cooldown_minutes`` (default 120). Opposite side and risk governors unchanged.

    Skew gate 0.65 and flatten at −3.25% (entries halt −2.25%) are the overnight
    Calmar suite winner ``combo_skew065_flat325`` (July 2026).
    """
    return replace(
        build_p3_trend_skew_config(account_equity, baseline_contracts),
        same_side_stop_cooldown_minutes=cooldown_minutes,
        candidate_max_adverse_skew=PRODUCTION_SKEW_GATE,
        daily_loss_limit_pct=PRODUCTION_DAILY_LOSS_LIMIT_PCT,
        flatten_loss_limit_pct=PRODUCTION_FLATTEN_LOSS_LIMIT_PCT,
    )


# Named profiles: name -> builder(account_equity, baseline_contracts) -> config.
PROFILE_BUILDERS: Dict[str, Callable[..., StrategyConfig]] = {
    "3d_flatten_3_5": build_3d_flatten_config,
    PRODUCTION_PROFILE: build_p3_trend_skew_config,
    "p3_poststop_cooldown_120": build_p3_poststop_cooldown_config,
}


# --------------------------------------------------------------------------- #
# Time-of-day contract weighting schemes (Test 3G).
# Each schedule is an ordered list of (upper_bound_time, multiplier). For an
# entry at time t we use the multiplier of the first segment with t < bound.
# Applied on top of a profile's flat baseline_contracts; 0.0 halts entries in
# that window. "" / control_flat == pure production 3D.
# --------------------------------------------------------------------------- #
Schedule = List[Tuple[time, float]]

SCHEMES: Dict[str, Schedule] = {
    "control_flat": [
        (time(23, 59), 1.0),
    ],
    "linear_decay_neutral": [
        (time(10, 30), 1.50),
        (time(11, 30), 1.25),
        (time(12, 30), 1.00),
        (time(13, 30), 0.75),
        (time(14, 30), 0.60),
        (time(23, 59), 0.50),
    ],
    "linear_decay_downsize": [
        (time(10, 30), 1.25),
        (time(11, 30), 1.00),
        (time(12, 30), 0.85),
        (time(13, 30), 0.60),
        (time(14, 30), 0.45),
        (time(23, 59), 0.25),
    ],
    "step_3block_mild": [
        (time(11, 30), 1.25),
        (time(13, 30), 1.00),
        (time(23, 59), 0.50),
    ],
    "step_3block_aggressive": [
        (time(11, 0), 1.50),
        (time(13, 0), 0.75),
        (time(23, 59), 0.33),
    ],
    "front_load_morning": [
        (time(12, 0), 1.25),
        (time(14, 0), 0.50),
        (time(23, 59), 0.25),
    ],
    "morning_heavy_afternoon_off": [
        (time(12, 0), 1.00),
        (time(14, 0), 0.50),
        (time(23, 59), 0.00),
    ],
    "half_after_noon": [
        (time(12, 0), 1.00),
        (time(23, 59), 0.50),
    ],
    "taper_4step": [
        (time(10, 30), 1.50),
        (time(12, 0), 1.00),
        (time(13, 30), 0.60),
        (time(23, 59), 0.30),
    ],
}


def schedule_multiplier(t: time, schedule: Schedule) -> float:
    for bound, mult in schedule:
        if t < bound:
            return mult
    return schedule[-1][1]
