"""Variant registry for overnight Calmar improvement suite (Wave 2: phases 7–13)."""
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

# No 9am entries: zero size until 10:00, then production linear decay.
SCHEDULE_NO_9AM = [
    (time(10, 0), 0.0),
    (time(10, 30), 1.25),
    (time(11, 30), 1.00),
    (time(12, 30), 0.85),
    (time(13, 30), 0.60),
    (time(14, 30), 0.45),
    (time(23, 59), 0.25),
]

# Extra time-of-day schedules retained for Wave 1 policy keys / Phase 13 combos.
SCHEDULE_ZERO_LAST_HOUR = [
    (time(14, 30), 1.0),
    (time(23, 59), 0.0),
]
SCHEDULE_LINEAR_ZERO_LAST = [
    (time(10, 30), 1.25),
    (time(11, 30), 1.00),
    (time(12, 30), 0.85),
    (time(13, 30), 0.60),
    (time(14, 30), 0.0),
    (time(23, 59), 0.0),
]


def _base(**kw) -> StrategyConfig:
    return replace(build_p3_poststop_cooldown_config(account_equity=ACCOUNT), **kw)


def _delta_band(target: float) -> dict:
    return {
        "target_abs_delta": target,
        "min_abs_delta": round(target - 0.05, 2),
        "max_abs_delta": round(target + 0.05, 2),
    }


def build_all_variants() -> List[Variant]:
    """Wave 2 singles (p7–p12) + optional Phase 13 combos.

    Wave 1 singles are not re-swept. Reference is current production substrate.
    """
    b = _base()
    variants: List[Variant] = []

    def add(phase: str, name: str, cfg: StrategyConfig | None = None, policy_key: str = "vix125"):
        variants.append((phase, name, cfg or b, policy_key))

    # --- Reference (current production) ---
    add("ref", "baseline_vix125", policy_key="vix125")

    # --- Phase 7: Circuit breakers & stop cascades ---
    add("p7_circuit", "max_stops_2", _base(max_stops_per_side=2), "vix125")
    add("p7_circuit", "max_stops_3", _base(max_stops_per_side=3), "vix125")
    add("p7_circuit", "max_stops_4", _base(max_stops_per_side=4), "vix125")
    add("p7_circuit", "halt_after_stop_2", _base(max_stops_per_day=2), "vix125")
    add("p7_circuit", "halt_after_stop_3", _base(max_stops_per_day=3), "vix125")
    add("p7_circuit", "halt_after_stop_4", _base(max_stops_per_day=4), "vix125")
    add(
        "p7_circuit",
        "late_reentry_1300",
        _base(use_late_same_side_reentry=True, same_side_stop_late_reentry_cutoff=time(13, 0)),
        "vix125",
    )
    add(
        "p7_circuit",
        "late_reentry_1400",
        _base(use_late_same_side_reentry=True, same_side_stop_late_reentry_cutoff=time(14, 0)),
        "vix125",
    )
    add("p7_circuit", "cooldown_90", _base(same_side_stop_cooldown_minutes=90), "vix125")

    # --- Phase 8: Bear-call / trend asymmetry ---
    add("p8_bc", "trend_bc_050", _base(candidate_max_adverse_trend=0.50), "vix125")
    add("p8_bc", "trend_bc_075", _base(candidate_max_adverse_trend=0.75), "vix125")
    add("p8_bc", "trend_bc_085", _base(candidate_max_adverse_trend=0.85), "vix125")
    add("p8_bc", "chase_trend_100", _base(candidate_max_chase_trend=1.0), "vix125")
    add("p8_bc", "chase_trend_125", _base(candidate_max_chase_trend=1.25), "vix125")
    add("p8_bc", "hard_trend_skip_150", _base(hard_trend_skip_threshold=1.50), "vix125")
    add("p8_bc", "hard_trend_skip_175", _base(hard_trend_skip_threshold=1.75), "vix125")
    add("p8_bc", "bc_size_050", _base(bear_call_size_scale=0.50), "vix125")
    add("p8_bc", "bc_size_075", _base(bear_call_size_scale=0.75), "vix125")
    add("p8_bc", "skew_055", _base(candidate_max_adverse_skew=0.55), "vix125")
    add("p8_bc", "skew_060", _base(candidate_max_adverse_skew=0.60), "vix125")

    # --- Phase 9: Structure & delta ---
    add("p9_struct", "delta_15", _base(**_delta_band(0.15)), "vix125")
    add("p9_struct", "delta_18", _base(**_delta_band(0.18)), "vix125")
    add("p9_struct", "delta_22", _base(**_delta_band(0.22)), "vix125")
    add("p9_struct", "put_wing_150", _base(put_wing_width=150.0), "vix125")
    add("p9_struct", "put_wing_175", _base(put_wing_width=175.0), "vix125")
    add("p9_struct", "put_wing_225", _base(put_wing_width=225.0), "vix125")
    add("p9_struct", "call_wing_50", _base(call_wing_width=50.0), "vix125")
    add("p9_struct", "call_wing_100", _base(call_wing_width=100.0), "vix125")

    # --- Phase 10: Calendar & session (gap-Q4 skip pruned — Q4 is best PnL) ---
    add("p10_cal", "entry_1000", _base(entry_start=time(10, 0)), "vix125")
    add("p10_cal", "entry_1030", _base(entry_start=time(10, 30)), "vix125")
    add("p10_cal", "no_9am_tod", policy_key="no_9am")
    add("p10_cal", "skip_tue", policy_key="skip_tue")
    add("p10_cal", "skip_mon_tue", policy_key="skip_mon_tue")
    add("p10_cal", "interval_30", _base(entry_interval_minutes=30), "vix125")

    # --- Phase 11: Dynamic / path-dependent risk ---
    add("p11_dyn", "regime_thr_22", policy_key="regime_thr_22")
    add("p11_dyn", "regime_thr_25", policy_key="regime_thr_25")
    add("p11_dyn", "regime_thr_30", policy_key="regime_thr_30")
    add("p11_dyn", "regime_scale_033", policy_key="regime_scale_033")
    add("p11_dyn", "regime_scale_066", policy_key="regime_scale_066")
    add("p11_dyn", "prior_loss_10", policy_key="prior_loss_10")
    add("p11_dyn", "prior_loss_20", policy_key="prior_loss_20")
    add("p11_dyn", "prior_loss_25", policy_key="prior_loss_25")
    add(
        "p11_dyn",
        "intraday_cut_15",
        _base(intraday_size_cut_pct=0.015, intraday_size_cut_scale=0.5),
        "vix125",
    )
    add(
        "p11_dyn",
        "intraday_cut_20",
        _base(intraday_size_cut_pct=0.020, intraday_size_cut_scale=0.5),
        "vix125",
    )
    add(
        "p11_dyn",
        "vix_flatten_tight",
        _base(
            vix_tight_flatten_above=28.0,
            vix_tight_flatten_loss_pct=0.030,
            vix_tight_daily_loss_pct=0.020,
        ),
        "vix125",
    )

    # --- Phase 12: Quality / TOD score gates ---
    add("p12_qual", "min_score_10", _base(candidate_min_score=1.0), "vix125")
    add("p12_qual", "min_score_15", _base(candidate_min_score=1.5), "vix125")
    add("p12_qual", "min_score_20", _base(candidate_min_score=2.0), "vix125")
    add("p12_qual", "sl_to_credit_35", _base(candidate_max_stop_loss_to_credit=3.5), "vix125")
    add("p12_qual", "sl_to_credit_40", _base(candidate_max_stop_loss_to_credit=4.0), "vix125")
    add("p12_qual", "realized_z_100", _base(candidate_max_abs_realized_z=1.0), "vix125")
    add("p12_qual", "realized_z_125", _base(candidate_max_abs_realized_z=1.25), "vix125")
    add("p12_qual", "tod_score_on", _base(use_time_of_day_controls=True), "vix125")
    add("p12_qual", "ctw_0150", _base(candidate_min_credit_to_width=0.0150), "vix125")
    add("p12_qual", "two_sided", _base(candidate_max_sides=2), "vix125")

    # Phase 13 combos (hypothesis-driven; refined after singles if needed).
    variants.extend(build_phase13_combos())

    return variants


def build_phase13_combos() -> List[Variant]:
    """Hypothesis-driven combos from Phase 0 what-ifs."""
    variants: List[Variant] = []

    def add(name: str, cfg: StrategyConfig | None = None, policy_key: str = "vix125"):
        variants.append(("p13_combo", name, cfg or _base(), policy_key))

    add(
        "combo_trend075_halt3",
        _base(candidate_max_adverse_trend=0.75, max_stops_per_day=3),
    )
    add(
        "combo_trend075_maxstops3",
        _base(candidate_max_adverse_trend=0.75, max_stops_per_side=3),
    )
    add(
        "combo_bc075_entry1000",
        _base(bear_call_size_scale=0.75, entry_start=time(10, 0)),
    )
    add(
        "combo_trend075_entry1000_halt3",
        _base(
            candidate_max_adverse_trend=0.75,
            entry_start=time(10, 0),
            max_stops_per_day=3,
        ),
    )
    add("combo_no9am_trend075", _base(candidate_max_adverse_trend=0.75), "no_9am")
    add(
        "combo_dd_hunter",
        _base(
            intraday_size_cut_pct=0.015,
            intraday_size_cut_scale=0.5,
            max_stops_per_day=3,
        ),
        "no_9am",
    )
    add(
        "combo_bc075_regime25",
        _base(bear_call_size_scale=0.75),
        "regime_thr_25",
    )
    add(
        "combo_skip_tue_trend075",
        _base(candidate_max_adverse_trend=0.75),
        "skip_tue",
    )
    add(
        "combo_delta18_halt3",
        _base(**_delta_band(0.18), max_stops_per_day=3),
    )
    add(
        "combo_skew060_halt3_entry1000",
        _base(
            candidate_max_adverse_skew=0.60,
            max_stops_per_day=3,
            entry_start=time(10, 0),
        ),
    )
    add(
        "combo_late1400_trend075",
        _base(
            candidate_max_adverse_trend=0.75,
            use_late_same_side_reentry=True,
            same_side_stop_late_reentry_cutoff=time(14, 0),
        ),
    )
    add(
        "combo_cagr_hunter",
        _base(**_delta_band(0.18), candidate_min_credit_to_width=0.0150),
    )

    return variants


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
    if policy_key == "elev_25_30":
        return VixElevatedSkipPolicy(
            TOD, elevated_min=25.0, elevated_max=30.0, elevated_scale=1.25, max_contracts=CAP
        )
    if policy_key == "downsize_17_25":
        return VixElevatedSkipPolicyExt(
            TOD,
            low_vix_downsize_min=17.0,
            low_vix_downsize_max=25.0,
            low_vix_scale=0.85,
            max_contracts=CAP,
        )
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
    if policy_key == "tod_front_load":
        return build_production_vix_policy(SCHEMES["front_load_morning"], max_contracts=CAP)
    if policy_key == "tod_half_noon":
        return build_production_vix_policy(SCHEMES["half_after_noon"], max_contracts=CAP)
    if policy_key == "tod_morning_heavy_off":
        return build_production_vix_policy(SCHEMES["morning_heavy_afternoon_off"], max_contracts=CAP)
    if policy_key == "tod_zero_last":
        return build_production_vix_policy(SCHEDULE_LINEAR_ZERO_LAST, max_contracts=CAP)
    if policy_key == "elev_morning":
        return VixElevatedSkipPolicyExt(
            TOD,
            elevated_scale=VIX_ELEVATED_SCALE,
            elevated_morning_end=time(12, 30),
            max_contracts=CAP,
        )
    if policy_key == "cap_40":
        return build_production_vix_policy(TOD, max_contracts=40)
    if policy_key == "cap_none":
        return build_production_vix_policy(TOD, max_contracts=None)
    if policy_key == "regime_half":
        return RegimeDownsizePolicy(TOD, trailing_stop, max_contracts=CAP)
    if policy_key == "prior_day_loss":
        inner = build_production_vix_policy(TOD, max_contracts=CAP)
        return PriorDayLossSkipPolicy(
            inner, prior_day_pnl=prior_day_pnl, account_equity=ACCOUNT, loss_pct=0.015
        )
    if policy_key == "tod_zero_elev125":
        return VixElevatedSkipPolicy(
            SCHEDULE_LINEAR_ZERO_LAST, elevated_scale=VIX_ELEVATED_SCALE, max_contracts=CAP
        )

    # --- Wave 2 policy keys ---
    if policy_key == "no_9am":
        return build_production_vix_policy(SCHEDULE_NO_9AM, max_contracts=CAP)
    if policy_key == "skip_tue":
        inner = build_production_vix_policy(TOD, max_contracts=CAP)
        return WeekdaySkipPolicy(inner, skip_weekdays=(1,))  # Tuesday
    if policy_key == "skip_mon_tue":
        inner = build_production_vix_policy(TOD, max_contracts=CAP)
        return WeekdaySkipPolicy(inner, skip_weekdays=(0, 1))
    if policy_key == "regime_thr_22":
        return RegimeDownsizePolicy(TOD, trailing_stop, threshold=0.22, max_contracts=CAP)
    if policy_key == "regime_thr_25":
        return RegimeDownsizePolicy(TOD, trailing_stop, threshold=0.25, max_contracts=CAP)
    if policy_key == "regime_thr_30":
        return RegimeDownsizePolicy(TOD, trailing_stop, threshold=0.30, max_contracts=CAP)
    if policy_key == "regime_scale_033":
        return RegimeDownsizePolicy(
            TOD, trailing_stop, threshold=0.25, scale=1.0 / 3.0, max_contracts=CAP
        )
    if policy_key == "regime_scale_066":
        return RegimeDownsizePolicy(
            TOD, trailing_stop, threshold=0.25, scale=2.0 / 3.0, max_contracts=CAP
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
    if policy_key == "prior_loss_25":
        inner = build_production_vix_policy(TOD, max_contracts=CAP)
        return PriorDayLossSkipPolicy(
            inner, prior_day_pnl=prior_day_pnl, account_equity=ACCOUNT, loss_pct=0.025
        )

    return build_production_vix_policy(TOD, max_contracts=CAP)
