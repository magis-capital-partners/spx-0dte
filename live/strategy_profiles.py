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
