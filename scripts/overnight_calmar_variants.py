"""Variant registry for overnight Calmar improvement suite (Wave 3).

Wave 3 substrate: production ``p3_poststop_cooldown_120`` with put wing 150
already baked in. Focus: Calmar / CAGR vs max DD and worst day.

Anti-overfit protocol (enforced in summarize):
  - Selection period: OOS dates <= SELECTION_END (rank / pick winners here only)
  - Holdout period: OOS dates >= HOLDOUT_START (sealed; report only after ranking)
"""
from __future__ import annotations

from dataclasses import replace
from datetime import time
from typing import Deque, List, Optional, Tuple

from mbh_simulator import StrategyConfig
from overnight_policies import (
    DdqVixTodPolicy,
    PriorDayLossSkipPolicy,
    RegimeDownsizePolicy,
    VixElevatedSkipPolicyExt,
    WeekdaySkipPolicy,
)
from profiles import (
    PRODUCTION_ACCOUNT_EQUITY,
    PRODUCTION_MAX_CONTRACTS_PER_TRANCHE,
    PRODUCTION_SIZING_SCHEME,
    SCHEMES,
    VIX_ELEVATED_SCALE,
    build_p3_poststop_cooldown_config,
)
from vix_sizing_policies import VixElevatedSkipPolicy, build_production_vix_policy

Variant = Tuple[str, str, StrategyConfig, str]  # phase, name, config, policy_key

ACCOUNT = PRODUCTION_ACCOUNT_EQUITY
TOD = SCHEMES[PRODUCTION_SIZING_SCHEME]
CAP = PRODUCTION_MAX_CONTRACTS_PER_TRANCHE

# Chronological train/test split for Wave 3 ranking (do not peek at holdout).
SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"

# Milder no-9am schedule retained for a few path-risk combos.
SCHEDULE_NO_9AM = [
    (time(10, 0), 0.0),
    (time(10, 30), 1.25),
    (time(11, 30), 1.00),
    (time(12, 30), 0.85),
    (time(13, 30), 0.60),
    (time(14, 30), 0.45),
    (time(23, 59), 0.25),
]


def _base(**kw) -> StrategyConfig:
    return replace(build_p3_poststop_cooldown_config(account_equity=ACCOUNT), **kw)


def _delta_band(target: float) -> dict:
    return {
        "target_abs_delta": target,
        "min_abs_delta": round(target - 0.05, 2),
        "max_abs_delta": round(target + 0.05, 2),
    }


def _vix_flat(above: float, flatten_pct: float = 0.030, daily_pct: float = 0.020) -> dict:
    return {
        "vix_tight_flatten_above": above,
        "vix_tight_flatten_loss_pct": flatten_pct,
        "vix_tight_daily_loss_pct": daily_pct,
    }


def build_all_variants() -> List[Variant]:
    """Wave 3: trend/structure fine grid + tail hunters + pre-specified promos."""
    b = _base()
    variants: List[Variant] = []

    def add(phase: str, name: str, cfg: StrategyConfig | None = None, policy_key: str = "vix125"):
        variants.append((phase, name, cfg or b, policy_key))

    # --- Reference (current production: put wing 150) ---
    add("ref", "baseline_vix125", policy_key="vix125")

    # --- Phase 14: Wave 2b winner combos / fine grid around trend_bc_085 ---
    add("p14_combo", "trend_bc_080", _base(candidate_max_adverse_trend=0.80))
    add("p14_combo", "trend_bc_085", _base(candidate_max_adverse_trend=0.85))
    add("p14_combo", "trend_bc_090", _base(candidate_max_adverse_trend=0.90))
    add("p14_combo", "trend_bc_095", _base(candidate_max_adverse_trend=0.95))
    add("p14_combo", "put_wing_140", _base(put_wing_width=140.0))
    add("p14_combo", "put_wing_160", _base(put_wing_width=160.0))
    add("p14_combo", "put_wing_175", _base(put_wing_width=175.0))
    add(
        "p14_combo",
        "pw175_trend085",
        _base(put_wing_width=175.0, candidate_max_adverse_trend=0.85),
    )
    add(
        "p14_combo",
        "trend085_vixflat28",
        _base(candidate_max_adverse_trend=0.85, **_vix_flat(28.0)),
    )
    add(
        "p14_combo",
        "trend085_icut20",
        _base(
            candidate_max_adverse_trend=0.85,
            intraday_size_cut_pct=0.020,
            intraday_size_cut_scale=0.5,
        ),
    )
    add(
        "p14_combo",
        "trend085_halt3",
        _base(candidate_max_adverse_trend=0.85, max_stops_per_day=3),
    )
    add(
        "p14_combo",
        "trend085_maxstops3",
        _base(candidate_max_adverse_trend=0.85, max_stops_per_side=3),
    )
    add(
        "p14_combo",
        "trend085_vixflat_halt3",
        _base(candidate_max_adverse_trend=0.85, max_stops_per_day=3, **_vix_flat(28.0)),
    )
    add(
        "p14_combo",
        "trend085_icut_vixflat",
        _base(
            candidate_max_adverse_trend=0.85,
            intraday_size_cut_pct=0.020,
            intraday_size_cut_scale=0.5,
            **_vix_flat(28.0),
        ),
    )

    # --- Phase 15: Tail / DD / worst-day hunters ---
    add(
        "p15_tail",
        "flatten_300_200",
        _base(flatten_loss_limit_pct=0.030, daily_loss_limit_pct=0.020),
    )
    add(
        "p15_tail",
        "flatten_275_175",
        _base(flatten_loss_limit_pct=0.0275, daily_loss_limit_pct=0.0175),
    )
    add(
        "p15_tail",
        "flatten_350_250",
        _base(flatten_loss_limit_pct=0.035, daily_loss_limit_pct=0.025),
    )
    add("p15_tail", "stop_mult_175", _base(stop_multiple=1.75))
    add("p15_tail", "stop_mult_225", _base(stop_multiple=2.25))
    add("p15_tail", "max_open_1", _base(max_open_trades_per_side=1))
    add("p15_tail", "block_same_strike", _base(block_same_strike_after_stop=True))
    add("p15_tail", "cooldown_60", _base(same_side_stop_cooldown_minutes=60))
    add("p15_tail", "cooldown_90", _base(same_side_stop_cooldown_minutes=90))
    add("p15_tail", "cooldown_150", _base(same_side_stop_cooldown_minutes=150))
    add("p15_tail", "bc_size_090", _base(bear_call_size_scale=0.90))
    add("p15_tail", "bc_size_085", _base(bear_call_size_scale=0.85))
    add(
        "p15_tail",
        "trend085_flatten300",
        _base(
            candidate_max_adverse_trend=0.85,
            flatten_loss_limit_pct=0.030,
            daily_loss_limit_pct=0.020,
        ),
    )
    add(
        "p15_tail",
        "trend085_bc090",
        _base(candidate_max_adverse_trend=0.85, bear_call_size_scale=0.90),
    )
    add(
        "p15_tail",
        "trend085_maxopen1",
        _base(candidate_max_adverse_trend=0.85, max_open_trades_per_side=1),
    )

    # --- Phase 16: CAGR hunters (must still clear DD / worst-day floors) ---
    add("p16_cagr", "elev_130", policy_key="elev_130")
    add("p16_cagr", "elev_135", policy_key="elev_135")
    add("p16_cagr", "credit_cap_175", _base(daily_credit_cap_pct=0.0175))
    add("p16_cagr", "credit_cap_200", _base(daily_credit_cap_pct=0.0200))
    add("p16_cagr", "contracts_34", _base(baseline_contracts=34))
    add("p16_cagr", "contracts_36", _base(baseline_contracts=36))
    add("p16_cagr", "delta_21", _base(**_delta_band(0.21)))
    add("p16_cagr", "call_wing_60", _base(call_wing_width=60.0))
    add("p16_cagr", "call_wing_90", _base(call_wing_width=90.0))
    add("p16_cagr", "ctw_0135", _base(candidate_min_credit_to_width=0.0135))
    add(
        "p16_cagr",
        "trend085_elev130",
        _base(candidate_max_adverse_trend=0.85),
        "elev_130",
    )
    add(
        "p16_cagr",
        "trend085_credit175",
        _base(candidate_max_adverse_trend=0.85, daily_credit_cap_pct=0.0175),
    )

    # --- Phase 17: Path-dependent / VIX risk shaping ---
    add("p17_path", "vix_flat_25", _base(**_vix_flat(25.0)))
    add("p17_path", "vix_flat_28", _base(**_vix_flat(28.0)))
    add("p17_path", "vix_flat_30", _base(**_vix_flat(30.0)))
    add(
        "p17_path",
        "intraday_cut_10",
        _base(intraday_size_cut_pct=0.010, intraday_size_cut_scale=0.5),
    )
    add(
        "p17_path",
        "intraday_cut_25",
        _base(intraday_size_cut_pct=0.025, intraday_size_cut_scale=0.5),
    )
    add("p17_path", "late_off", policy_key="late_off")
    add("p17_path", "ddq", policy_key="ddq")
    add(
        "p17_path",
        "trend085_late_off",
        _base(candidate_max_adverse_trend=0.85),
        "late_off",
    )
    add(
        "p17_path",
        "trend085_prior15",
        _base(candidate_max_adverse_trend=0.85),
        "prior_loss_15",
    )
    add(
        "p17_path",
        "trend085_regime25",
        _base(candidate_max_adverse_trend=0.85),
        "regime_thr_25",
    )
    add(
        "p17_path",
        "trend085_no9am",
        _base(candidate_max_adverse_trend=0.85),
        "no_9am",
    )

    # --- Phase 18: unique pre-specified promo not already covered in p14/p15 ---
    add(
        "p18_promo",
        "promo_dd_hunter",
        _base(
            candidate_max_adverse_trend=0.85,
            bear_call_size_scale=0.90,
            flatten_loss_limit_pct=0.030,
            daily_loss_limit_pct=0.020,
        ),
    )

    return variants


# Frozen before any holdout peek. Rank on selection; promote only if holdout passes.
PROMO_CANDIDATE_NAMES = frozenset(
    {
        "trend_bc_085",
        "trend085_vixflat28",
        "pw175_trend085",
        "trend085_icut_vixflat",
        "promo_dd_hunter",
        "trend085_flatten300",
        "trend085_halt3",
    }
)


def _elev_scale_from_key(policy_key: str) -> Optional[float]:
    if not policy_key.startswith("elev_"):
        return None
    body = policy_key.replace("elev_", "").replace("x", "")
    if body in ("25_30", "morning", "120"):
        return None
    if body.isdigit() and len(body) == 3:
        return float(f"{body[0]}.{body[1:]}")
    return None


def make_policy(
    policy_key: str,
    *,
    trailing_stop: Deque[float],
    prior_day_pnl: float = 0.0,
) -> object:
    """Instantiate sizing policy for a variant."""
    if policy_key == "vix125":
        return build_production_vix_policy(TOD, elevated_scale=VIX_ELEVATED_SCALE, max_contracts=CAP)

    elev_scale = _elev_scale_from_key(policy_key)
    if elev_scale is not None:
        return VixElevatedSkipPolicy(TOD, elevated_scale=elev_scale, max_contracts=CAP)

    if policy_key == "elev_120":
        return VixElevatedSkipPolicy(TOD, elevated_scale=1.20, max_contracts=CAP)
    if policy_key == "late_off":
        return VixElevatedSkipPolicyExt(
            TOD,
            elevated_scale=VIX_ELEVATED_SCALE,
            late_vix_off_threshold=28.0,
            late_vix_off_after=time(14, 0),
            max_contracts=CAP,
        )
    if policy_key == "ddq":
        return DdqVixTodPolicy(TOD, max_contracts=CAP)
    if policy_key == "no_9am":
        return build_production_vix_policy(SCHEDULE_NO_9AM, max_contracts=CAP)
    if policy_key == "skip_tue":
        inner = build_production_vix_policy(TOD, max_contracts=CAP)
        return WeekdaySkipPolicy(inner, skip_weekdays=(1,))
    if policy_key == "regime_thr_25":
        return RegimeDownsizePolicy(TOD, trailing_stop, threshold=0.25, max_contracts=CAP)
    if policy_key == "prior_loss_15":
        inner = build_production_vix_policy(TOD, max_contracts=CAP)
        return PriorDayLossSkipPolicy(
            inner, prior_day_pnl=prior_day_pnl, account_equity=ACCOUNT, loss_pct=0.015
        )
    if policy_key == "prior_loss_10":
        inner = build_production_vix_policy(TOD, max_contracts=CAP)
        return PriorDayLossSkipPolicy(
            inner, prior_day_pnl=prior_day_pnl, account_equity=ACCOUNT, loss_pct=0.010
        )
    if policy_key == "prior_loss_20":
        inner = build_production_vix_policy(TOD, max_contracts=CAP)
        return PriorDayLossSkipPolicy(
            inner, prior_day_pnl=prior_day_pnl, account_equity=ACCOUNT, loss_pct=0.020
        )

    return build_production_vix_policy(TOD, elevated_scale=VIX_ELEVATED_SCALE, max_contracts=CAP)
