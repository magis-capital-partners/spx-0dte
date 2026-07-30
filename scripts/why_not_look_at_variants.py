"""Variant registry for the Why-Not-Look-At experiment suite.

Anti-overfit protocol (enforced in summarize):
  - Rolling feature train: first PRODUCTION_TRAIN_COUNT eligible days (baselines)
  - Selection (tune/rank): OOS dates <= SELECTION_END
  - Holdout (sealed test): OOS dates >= HOLDOUT_START
  - Never choose a winner using holdout metrics
"""
from __future__ import annotations

import csv
from dataclasses import replace
from datetime import time
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

from mbh_simulator import StrategyConfig
from profiles import (
    PRODUCTION_ACCOUNT_EQUITY,
    PRODUCTION_MAX_CONTRACTS_PER_TRANCHE,
    PRODUCTION_SIZING_SCHEME,
    SCHEMES,
    VIX_ELEVATED_SCALE,
    build_p3_poststop_cooldown_config,
)
from vix_sizing_policies import VixElevatedSkipPolicy, build_production_vix_policy

ROOT = Path(__file__).resolve().parents[1]
FOMC_CSV = ROOT / "data" / "calendar" / "fomc_days.csv"

# Chronological split — freeze before any holdout peek.
SELECTION_END = "2023-12-29"
HOLDOUT_START = "2024-01-02"

ACCOUNT = PRODUCTION_ACCOUNT_EQUITY
TOD = SCHEMES[PRODUCTION_SIZING_SCHEME]
CAP = PRODUCTION_MAX_CONTRACTS_PER_TRANCHE

# phase, name, config, policy_key, fed_mode, structure_mode
Variant = Tuple[str, str, StrategyConfig, str, str, str]


def load_fomc_dates(path: Path = FOMC_CSV) -> Set[str]:
    if not path.is_file():
        return set()
    dates: Set[str] = set()
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            d = (row.get("date") or "").strip()[:10]
            if d:
                dates.add(d)
    return dates


def _base(**kw) -> StrategyConfig:
    return replace(build_p3_poststop_cooldown_config(account_equity=ACCOUNT), **kw)


def _delta_band(target: float) -> dict:
    return {
        "target_abs_delta": target,
        "min_abs_delta": round(target - 0.05, 2),
        "max_abs_delta": round(target + 0.05, 2),
    }


def build_all_variants() -> List[Variant]:
    """Every backtestable wave variant (diagnostics without re-sim listed separately)."""
    variants: List[Variant] = []

    def add(
        phase: str,
        name: str,
        cfg: StrategyConfig | None = None,
        policy_key: str = "vix125",
        fed_mode: str = "none",
        structure_mode: str = "vertical",
    ) -> None:
        variants.append((phase, name, cfg or _base(), policy_key, fed_mode, structure_mode))

    # Shared control (also W1-0 / W3-0 / W5-0 / W6-0)
    add("ref", "baseline", policy_key="vix125")

    # --- Wave 1: five-regime VIX with top >30 ---
    add("w1_vix5", "W1_1_skip_gt30", policy_key="skip_gt30")
    add("w1_vix5", "W1_2_skip30_elev20_30", policy_key="skip30_elev20_30")
    add("w1_vix5", "W1_3_skip30_smooth", policy_key="skip30_smooth")
    add("w1_vix5", "W1_4_half_gt30_elev20_30", policy_key="half_gt30_elev20_30")

    # --- Wave 2A: target short delta ---
    for name, target in [
        ("W2A_1_delta_16", 0.16),
        ("W2A_2_delta_18", 0.18),
        ("W2A_3_delta_22", 0.22),
        ("W2A_4_delta_25", 0.25),
    ]:
        add("w2a_delta", name, _base(**_delta_band(target)))

    # --- Wave 2B: target spread credit ---
    add(
        "w2b_credit",
        "W2B_1_credit_1.00",
        _base(target_credit=1.0, credit_selection_mode="target_credit"),
    )
    add(
        "w2b_credit",
        "W2B_2_credit_1.50",
        _base(target_credit=1.5, credit_selection_mode="target_credit"),
    )
    add(
        "w2b_credit",
        "W2B_3_ctw_0.020",
        _base(target_credit_to_width=0.020, credit_selection_mode="target_credit_to_width"),
    )
    add(
        "w2b_credit",
        "W2B_4_ctw_0.025",
        _base(target_credit_to_width=0.025, credit_selection_mode="target_credit_to_width"),
    )
    add(
        "w2b_credit",
        "W2B_5_credit_by_vix",
        _base(target_credit=1.20, credit_selection_mode="target_credit"),
        policy_key="credit_vix_tiers",
    )

    # --- Wave 2C: adjust targets ---
    add(
        "w2c_adjust",
        "W2C_1_widen_put_vix20",
        _base(vix_widen_put_wing_above=20.0, vix_widen_put_wing_extra=25.0),  # VIX >= 20
    )
    add(
        "w2c_adjust",
        "W2C_2_tighten_delta_vix25",
        _base(vix_tighten_delta_above=25.0, vix_tighten_delta_target=0.16),
    )
    add(
        "w2c_adjust",
        "W2C_3_post_stop_ctw",
        _base(post_stop_min_credit_to_width=0.020),
    )
    add(
        "w2c_adjust",
        "W2C_4_afternoon_min_credit",
        _base(afternoon_min_credit=1.0, afternoon_min_credit_start=time(13, 0)),
    )
    add(
        "w2c_adjust",
        "W2C_5_combo",
        _base(
            vix_widen_put_wing_above=20.0,
            vix_widen_put_wing_extra=25.0,
            vix_tighten_delta_above=25.0,
            vix_tighten_delta_target=0.16,
            post_stop_min_credit_to_width=0.020,
        ),
    )

    # --- Wave 3: put vs call vs both vs none ---
    add(
        "w3_sides",
        "W3_1_skew_hard_side",
        _base(candidate_max_adverse_skew=0.01),  # near-hard skew gate via existing adverse skew
        policy_key="skew_hard_side",
    )
    add("w3_sides", "W3_2_trend_calls_0.5", _base(candidate_max_adverse_trend=0.50))
    add("w3_sides", "W3_3_skip_puts_rv_m0.5", _base(skip_puts_if_realized_below=-0.5))
    add("w3_sides", "W3_3b_skip_puts_rv_m1.0", _base(skip_puts_if_realized_below=-1.0))
    add("w3_sides", "W3_4_skip_neg_vrp", _base(skip_both_if_straddle_residual_below=0.0))  # skip when residual < 0
    add(
        "w3_sides",
        "W3_5_tod_side_windows",
        _base(entry_start=time(10, 30), entry_end=time(14, 30)),
    )
    add(
        "w3_sides",
        "W3_6_both_quiet_skew",
        _base(candidate_max_sides=2, both_sides_max_abs_skew=0.25),
        policy_key="both_mid_vix",
    )
    add("w3_sides", "W3_7_none_extreme", policy_key="skip_gt30")

    # --- Wave 4: sell ATM straddles ---
    add("w4_straddle", "W4_1_straddle_eod", structure_mode="straddle_eod")
    add("w4_straddle", "W4_2_straddle_stop2x", structure_mode="straddle_stop2x")
    add("w4_straddle", "W4_3_straddle_rich", structure_mode="straddle_rich")
    add("w4_straddle", "W4_4_straddle_overlay", structure_mode="straddle_overlay")

    # --- Wave 5: early exit ---
    add("w5_exit", "W5_1_pt_50", _base(profit_take_credit_fraction=0.50))
    add("w5_exit", "W5_2_pt_70", _base(profit_take_credit_fraction=0.70))
    add(
        "w5_exit",
        "W5_3_pt_50_after_12",
        _base(profit_take_credit_fraction=0.50, profit_take_after=time(12, 0)),
    )
    add(
        "w5_exit",
        "W5_4_pt_50_spot_guard",
        _base(profit_take_credit_fraction=0.50, profit_take_max_adverse_spot_pct=0.003),
    )
    add(
        "w5_exit",
        "W5_5_time_exit_15",
        _base(time_exit_after=time(15, 0), time_exit_min_credit_fraction=0.25),
    )
    add(
        "w5_exit",
        "W5_6_pt_vix_lt15",
        _base(profit_take_credit_fraction=0.50, profit_take_vix_below=15.0),
    )

    # --- Wave 6: Fed days ---
    add("w6_fed", "W6_1_skip_fomc", fed_mode="skip")
    add("w6_fed", "W6_2_fomc_until_1300", fed_mode="until_1300")
    add("w6_fed", "W6_3_fomc_half", fed_mode="half")
    add("w6_fed", "W6_4_fomc_skip_from_1330", fed_mode="skip_from_1330")
    add("w6_fed", "W6_5_fomc_puts_only", fed_mode="puts_only")

    # --- Wave 7: liquidity / 25s ---
    add(
        "w7_liq",
        "W7_1_prefer_25s",
        _base(prefer_strike_multiple=25.0, require_strike_multiple=False),
    )
    add(
        "w7_liq",
        "W7_2_require_25s",
        _base(prefer_strike_multiple=25.0, require_strike_multiple=True),
    )
    add(
        "w7_liq",
        "W7_3_prefer_25s_min_credit",
        _base(prefer_strike_multiple=25.0, candidate_min_credit=0.35),
    )

    return variants


DIAGNOSTIC_IDS = [
    "W0_A_fomc_calendar",
    "W0_B_trade_enrichment",
    "W0_C_vrp_features",
    "W0_D_vix5_helper",
    "W1_5_attribution_vix5",
    "W3_D1_side_slices",
    "W3_D2_side_counterfactual",
    "W4_D_straddle_vrp",
    "W6_D_fomc_attribution",
    "W7_D_strike_roundness",
]


class HalfSizePolicy:
    def __init__(self, inner) -> None:
        self.inner = inner

    def contracts(self, signal, config: StrategyConfig) -> int:
        base = self.inner.contracts(signal, config)
        return max(0, round(base * 0.5))


class ZeroPolicy:
    def contracts(self, signal, config: StrategyConfig) -> int:
        return 0


class SmoothVix5Policy(VixElevatedSkipPolicy):
    """Skip >30; 1.25× in 25–30; 0.75× in 20–25."""

    def contracts(self, signal, config: StrategyConfig) -> int:
        base = super(VixElevatedSkipPolicy, self).contracts(signal, config)
        if base <= 0:
            return 0
        if signal is None or signal.vix is None:
            return min(base, self.max_contracts or base)
        vix = signal.vix
        if vix > self.skip_above:
            return 0
        if 25.0 <= vix <= 30.0:
            base = max(0, round(base * 1.25))
        elif 20.0 <= vix < 25.0:
            base = max(0, round(base * 0.75))
        cap = self.max_contracts
        return min(base, cap) if cap else base


class HalfAbovePolicy(VixElevatedSkipPolicy):
    """No skip; half size above skip_above; elevated scale in band."""

    def contracts(self, signal, config: StrategyConfig) -> int:
        base = super(VixElevatedSkipPolicy, self).contracts(signal, config)
        if base <= 0:
            return 0
        if signal is None or signal.vix is None:
            return min(base, self.max_contracts or base)
        vix = signal.vix
        if vix > self.skip_above:
            base = max(0, round(base * 0.5))
        elif self.elevated_min <= vix <= self.elevated_max and self.elevated_scale != 1.0:
            base = max(0, round(base * self.elevated_scale))
        cap = self.max_contracts
        return min(base, cap) if cap else base


class SkewHardSidePolicy:
    """Puts if skew_z>0 else calls — enforced by zeroing wrong-side via allowed_sides mutation."""

    def __init__(self, inner) -> None:
        self.inner = inner

    def contracts(self, signal, config: StrategyConfig) -> int:
        return self.inner.contracts(signal, config)

    def apply_day_config(self, config: StrategyConfig, signal) -> StrategyConfig:
        if signal is None:
            return config
        if signal.skew_z > 0:
            return replace(config, allowed_sides="bull_put")
        return replace(config, allowed_sides="bear_call")


class BothMidVixPolicy(VixElevatedSkipPolicy):
    def contracts(self, signal, config: StrategyConfig) -> int:
        return super().contracts(signal, config)


def make_policy(policy_key: str, *, trailing_stop: Deque[float] | None = None) -> object:
    if policy_key == "vix125":
        return build_production_vix_policy(TOD, elevated_scale=VIX_ELEVATED_SCALE, max_contracts=CAP)
    if policy_key == "skip_gt30":
        return VixElevatedSkipPolicy(
            TOD, elevated_min=25.0, elevated_max=30.0, elevated_scale=1.0, skip_above=30.0, max_contracts=CAP
        )
    if policy_key == "skip30_elev20_30":
        return VixElevatedSkipPolicy(
            TOD, elevated_min=20.0, elevated_max=30.0, elevated_scale=1.25, skip_above=30.0, max_contracts=CAP
        )
    if policy_key == "skip30_smooth":
        return SmoothVix5Policy(
            TOD, elevated_min=25.0, elevated_max=30.0, elevated_scale=1.25, skip_above=30.0, max_contracts=CAP
        )
    if policy_key == "half_gt30_elev20_30":
        return HalfAbovePolicy(
            TOD, elevated_min=20.0, elevated_max=30.0, elevated_scale=1.25, skip_above=30.0, max_contracts=CAP
        )
    if policy_key == "credit_vix_tiers":
        return build_production_vix_policy(TOD, elevated_scale=VIX_ELEVATED_SCALE, max_contracts=CAP)
    if policy_key == "skew_hard_side":
        return SkewHardSidePolicy(build_production_vix_policy(TOD, max_contracts=CAP))
    if policy_key == "both_mid_vix":
        return BothMidVixPolicy(
            TOD, elevated_min=15.0, elevated_max=20.0, elevated_scale=1.0, skip_above=30.0, max_contracts=CAP
        )
    return build_production_vix_policy(TOD, elevated_scale=VIX_ELEVATED_SCALE, max_contracts=CAP)


def apply_fed_day(
    config: StrategyConfig,
    policy,
    *,
    fed_mode: str,
    is_fomc: bool,
) -> Tuple[StrategyConfig, object]:
    if not is_fomc or fed_mode == "none":
        return config, policy
    if fed_mode == "skip":
        return config, ZeroPolicy()
    if fed_mode == "until_1300":
        return replace(config, entry_end=time(13, 0)), policy
    if fed_mode == "half":
        return config, HalfSizePolicy(policy)
    if fed_mode == "skip_from_1330":
        return replace(config, entry_end=time(13, 30)), policy
    if fed_mode == "puts_only":
        return replace(config, allowed_sides="bull_put"), policy
    return config, policy


def day_config_overrides(
    config: StrategyConfig,
    policy,
    *,
    signal,
    policy_key: str,
) -> StrategyConfig:
    cfg = config
    if policy_key == "skew_hard_side" and hasattr(policy, "apply_day_config"):
        # Use first available signal later in runner; here keep config and mutate per tranche via allowed_sides
        # Runner applies per-day using first signal skew.
        pass
    if policy_key == "credit_vix_tiers" and signal is not None and signal.vix is not None:
        vix = signal.vix
        if vix < 20:
            target = 0.80
        elif vix <= 30:
            target = 1.20
        else:
            target = 1.60
        cfg = replace(cfg, target_credit=target, credit_selection_mode="target_credit")
    return cfg
