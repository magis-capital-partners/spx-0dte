"""Shared strategy profiles used by both backtest reruns and live execution.

Keeping the winning parameter sets in one place means the live executor and the
backtest validate the *same* numbers. Each profile returns kwargs for
``StrategyConfig`` plus the engine toggles the runners need.
"""
from __future__ import annotations

from typing import Dict


# Per-trade contract counts are expressed at $13M equity. The live executor
# scales them by (account_equity / 13_000_000) and by --contract-scale.
BASE_EQUITY = 13_000_000.0


PROFILES: Dict[str, dict] = {
    # Conservative reproduction of the prior documented best (~16% CAGR).
    "baseline": {
        "baseline_contracts": 1140,
        "daily_credit_cap_pct": 0.05,
        "daily_loss_limit_pct": 0.0225,
        "flatten_on_daily_loss": False,
        "use_two_tier_engine": True,
        "use_event_controls": True,
        "use_time_of_day_controls": True,
        "exploratory_min_score": 2.40,
        "exploratory_max_score": 2.49,
        "use_portfolio_allocator": True,
        "portfolio_margin_budget_pct": 0.40,
        "core_margin_budget_pct": 0.35,
        "exploratory_margin_budget_pct": 0.02,
    },
    # Free risk-control win: same size, flatten governor on (~19.7% CAGR,
    # worst day -2.6% vs -3.4%, Sharpe 2.16).
    "flatten": {
        "baseline_contracts": 1140,
        "daily_credit_cap_pct": 0.05,
        "daily_loss_limit_pct": 0.0225,
        "flatten_on_daily_loss": True,
        "use_two_tier_engine": True,
        "use_event_controls": True,
        "use_time_of_day_controls": True,
        "exploratory_min_score": 2.40,
        "exploratory_max_score": 2.49,
        "use_portfolio_allocator": True,
        "portfolio_margin_budget_pct": 0.40,
        "core_margin_budget_pct": 0.35,
        "exploratory_margin_budget_pct": 0.02,
    },
    # Best risk-adjusted: 2x deployment into abundant margin + flatten governor.
    # ~31.5% CAGR, Sharpe 2.0, worst day -2.5%, max DD 3.6%.
    "best": {
        "baseline_contracts": 2280,
        "daily_credit_cap_pct": 0.10,
        "daily_loss_limit_pct": 0.0225,
        "flatten_on_daily_loss": True,
        "use_two_tier_engine": True,
        "use_event_controls": True,
        "use_time_of_day_controls": True,
        "exploratory_min_score": 2.40,
        "exploratory_max_score": 2.49,
        "use_portfolio_allocator": True,
        "portfolio_margin_budget_pct": 0.80,
        "core_margin_budget_pct": 0.70,
        "exploratory_margin_budget_pct": 0.04,
    },
    # Aggressive: 2.5x deployment + deeper flatten trigger (entries halt at
    # 2.25%, positions only flattened at 3.5%) so volatile-but-recovering days
    # are not whipsawed. ~64% CAGR, Sharpe 2.8, max DD 5.0%, worst day -3.8%.
    # Higher leverage on a SMALL validated sample -- treat as upper bound and
    # validate on more data before using full size.
    "aggressive": {
        "baseline_contracts": 2850,
        "daily_credit_cap_pct": 0.125,
        "daily_loss_limit_pct": 0.0225,
        "flatten_on_daily_loss": True,
        "flatten_loss_limit_pct": 0.035,
        "use_two_tier_engine": True,
        "use_event_controls": True,
        "use_time_of_day_controls": True,
        "exploratory_min_score": 2.40,
        "exploratory_max_score": 2.49,
        "use_portfolio_allocator": True,
        "portfolio_margin_budget_pct": 1.00,
        "core_margin_budget_pct": 0.875,
        "exploratory_margin_budget_pct": 0.05,
    },
}


def scale_profile(profile: dict, account_equity: float, contract_scale: float = 1.0) -> dict:
    """Scale a profile's contract count to the live account and an optional
    fractional pilot multiplier. Budgets are equity-relative so they need no
    scaling, but contract counts are absolute and do."""
    scaled = dict(profile)
    equity_scale = account_equity / BASE_EQUITY if BASE_EQUITY else 1.0
    scaled["baseline_contracts"] = max(1, round(profile["baseline_contracts"] * equity_scale * contract_scale))
    return scaled


def _canonical_registry():
    """Lazy import of the canonical simulator registry.

    Kept lazy so merely importing ``PROFILES`` (as several simulator modules do)
    does not pull the whole backtest import chain, and so path setup only runs
    when the live executor actually resolves a config.
    """
    try:
        from profiles import PROFILE_BUILDERS, SCHEMES
    except ImportError:  # pragma: no cover - path bootstrap for live/ context
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "simulator"))
        from profiles import PROFILE_BUILDERS, SCHEMES
    return PROFILE_BUILDERS, SCHEMES


def resolve_strategy_config(live) -> tuple:
    """Turn a ``LiveConfig`` into a concrete ``(StrategyConfig, schedule)``.

    Canonical profiles (from ``simulator/profiles.py``) build the full validated
    config and are scaled by ``contract_scale``. Legacy allocator profiles fall
    back to the ``PROFILES`` kwargs path. ``schedule`` is a Test-3G time-of-day
    weighting list or ``None`` for a flat book.
    """
    from dataclasses import replace

    profile_builders, schemes = _canonical_registry()
    schedule = None
    if live.sizing_scheme and live.sizing_scheme in schemes:
        schedule = schemes[live.sizing_scheme]

    if live.profile in profile_builders:
        config = profile_builders[live.profile](account_equity=live.account_equity)
        if live.contracts_per_tranche > 0:
            scaled = live.contracts_per_tranche
        else:
            equity_scale = live.account_equity / BASE_EQUITY if BASE_EQUITY else 1.0
            scaled = max(1, round(config.baseline_contracts * equity_scale * live.contract_scale))
        return replace(config, baseline_contracts=scaled), schedule

    if live.profile in PROFILES:
        from mbh_simulator import StrategyConfig

        kwargs = scale_profile(PROFILES[live.profile], live.account_equity, live.contract_scale)
        return StrategyConfig(account_equity=live.account_equity, **kwargs), schedule

    known = sorted(set(profile_builders) | set(PROFILES))
    raise SystemExit(f"unknown profile {live.profile!r}; choose from {known}")
