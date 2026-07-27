"""Equity-proportional compounding sizing for the production path.

Under homogeneity of ``simulate_day`` in (contracts, account_equity), scaling both
by the same ``k = k_of(equity)`` leaves every gate decision unchanged while
scaling dollar P&L by ``k``. Risk governors (halt / flatten / credit cap) are
all ``pct × account_equity``, so they stay constant in percentage terms.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from profiles import (
    PRODUCTION_ACCOUNT_EQUITY,
    PRODUCTION_BASELINE_CONTRACTS,
    PRODUCTION_MAX_CONTRACTS_PER_TRANCHE,
    PRODUCTION_SIZING_SCHEME,
    SCHEMES,
    VIX_ELEVATED_SCALE,
    build_p3_poststop_cooldown_config,
)
from vix_sizing_policies import VixElevatedSkipPolicy, build_production_vix_policy

KFun = Callable[[float, int], float]  # (equity, day_index) -> k


@dataclass(frozen=True)
class CompoundingVariant:
    name: str
    label: str
    k_of: KFun
    export_dashboard: bool = False


def _clamp_k(k: float, *, floor: float = 0.0) -> float:
    if k < floor:
        return floor
    return float(k)


def k_fixed(_equity: float, _day_index: int, e0: float = PRODUCTION_ACCOUNT_EQUITY) -> float:
    return 1.0


def k_full(equity: float, _day_index: int, e0: float = PRODUCTION_ACCOUNT_EQUITY) -> float:
    return _clamp_k(equity / e0 if e0 else 1.0)


def make_fractional(f: float, e0: float = PRODUCTION_ACCOUNT_EQUITY) -> KFun:
    def k_of(equity: float, _day_index: int) -> float:
        if e0 <= 0:
            return 1.0
        ratio = max(equity / e0, 1e-12)
        return _clamp_k(ratio ** f)

    return k_of


def make_capped(cap: float, e0: float = PRODUCTION_ACCOUNT_EQUITY) -> KFun:
    def k_of(equity: float, _day_index: int) -> float:
        return _clamp_k(min(equity / e0 if e0 else 1.0, cap))

    return k_of


def make_hwm(e0: float = PRODUCTION_ACCOUNT_EQUITY) -> KFun:
    state = {"peak": e0}

    def k_of(equity: float, _day_index: int) -> float:
        state["peak"] = max(state["peak"], equity)
        return _clamp_k(state["peak"] / e0 if e0 else 1.0)

    return k_of


def make_ratchet(period_days: int = 63, e0: float = PRODUCTION_ACCOUNT_EQUITY) -> KFun:
    state = {"k": 1.0}

    def k_of(equity: float, day_index: int) -> float:
        if day_index % period_days == 0:
            state["k"] = equity / e0 if e0 else 1.0
        return _clamp_k(state["k"])

    return k_of


def make_band(band_pct: float = 0.10, e0: float = PRODUCTION_ACCOUNT_EQUITY) -> KFun:
    state = {"k": 1.0}

    def k_of(equity: float, _day_index: int) -> float:
        target = equity / e0 if e0 else 1.0
        if abs(target / state["k"] - 1.0) >= band_pct:
            state["k"] = target
        return _clamp_k(state["k"])

    return k_of


def build_variants(e0: float = PRODUCTION_ACCOUNT_EQUITY) -> Dict[str, CompoundingVariant]:
    """Canonical research grid. ``full`` is the dashboard export target."""
    return {
        "fixed": CompoundingVariant("fixed", "Fixed 31 lots (control)", make_fractional(0.0, e0)),
        "fractional_f025": CompoundingVariant(
            "fractional_f025", "Fractional f=0.25", make_fractional(0.25, e0)
        ),
        "fractional_f050": CompoundingVariant(
            "fractional_f050", "Fractional f=0.50", make_fractional(0.50, e0)
        ),
        "fractional_f075": CompoundingVariant(
            "fractional_f075", "Fractional f=0.75", make_fractional(0.75, e0)
        ),
        "full": CompoundingVariant(
            "full",
            "Full compounding f=1",
            make_fractional(1.0, e0),
            export_dashboard=True,
        ),
        "cap_2x": CompoundingVariant("cap_2x", "Full, cap 2×", make_capped(2.0, e0)),
        "cap_3x": CompoundingVariant("cap_3x", "Full, cap 3×", make_capped(3.0, e0)),
        "cap_4x": CompoundingVariant("cap_4x", "Full, cap 4×", make_capped(4.0, e0)),
        "cap_6x": CompoundingVariant("cap_6x", "Full, cap 6×", make_capped(6.0, e0)),
        "hwm": CompoundingVariant("hwm", "HWM-only sizing", make_hwm(e0)),
        "ratchet_63": CompoundingVariant(
            "ratchet_63", "63-day ratchet", make_ratchet(63, e0)
        ),
        "band_10": CompoundingVariant("band_10", "±10% resize band", make_band(0.10, e0)),
    }


def scaled_day_config(
    k: float,
    *,
    e0: float = PRODUCTION_ACCOUNT_EQUITY,
    baseline: int = PRODUCTION_BASELINE_CONTRACTS,
    max_tranche: int = PRODUCTION_MAX_CONTRACTS_PER_TRANCHE,
):
    """Build production StrategyConfig with equity + baseline contracts scaled by k."""
    k = max(0.0, float(k))
    equity = e0 * k
    contracts = max(1, round(baseline * k)) if k > 0 else 0
    return build_p3_poststop_cooldown_config(
        account_equity=equity,
        baseline_contracts=contracts,
    )


def scaled_day_policy(
    k: float,
    *,
    max_tranche: int = PRODUCTION_MAX_CONTRACTS_PER_TRANCHE,
    elevated_scale: float = VIX_ELEVATED_SCALE,
) -> VixElevatedSkipPolicy:
    """Production VIX/TOD policy with the absolute tranche cap scaled by k."""
    k = max(0.0, float(k))
    cap = max(1, round(max_tranche * k)) if k > 0 else 0
    return build_production_vix_policy(
        SCHEMES[PRODUCTION_SIZING_SCHEME],
        elevated_scale=elevated_scale,
        max_contracts=cap,
    )


def shard_variant_names(names: List[str], shard: int, shards: int) -> List[str]:
    if shards <= 1:
        return list(names)
    return [n for i, n in enumerate(names) if i % shards == shard]


def analytic_path(
    fixed_returns: List[float],
    k_of: KFun,
    *,
    e0: float = PRODUCTION_ACCOUNT_EQUITY,
) -> Tuple[List[float], List[float], List[float]]:
    """Apply a k-policy to a fixed-size daily return series.

    Returns (pnls, equities_open, ks) under the homogeneity assumption.
    """
    equity = e0
    pnls: List[float] = []
    equities: List[float] = []
    ks: List[float] = []
    for i, r in enumerate(fixed_returns):
        k = k_of(equity, i)
        pnl = k * r * e0
        equities.append(equity)
        ks.append(k)
        pnls.append(pnl)
        equity += pnl
    return pnls, equities, ks
