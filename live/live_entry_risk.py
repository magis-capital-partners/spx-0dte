"""Adapters to reuse backtest ``entry_risk_block_reason`` in the live loop."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Sequence

from mbh_simulator import StrategyConfig, entry_risk_block_reason


def apply_live_risk_overlays(config: StrategyConfig, live: Any) -> StrategyConfig:
    """Apply live-only stop caps and optional portfolio allocator."""
    # max_open_same_strike_multiple is meant to supersede the static
    # max_open_same_strike floor when set (live_config.py's own docstring says
    # so) — but this static value feeds entry_risk_block_reason, which runs
    # *before* open_risk_caps.open_risk_block_reason's multiplier-aware check
    # in the tranche loop. Left nonzero here, the static floor always blocks
    # first, so the dynamic cap could never actually apply once a session held
    # more than max_open_same_strike lots at one strike (observed 2026-08-06:
    # blocked at 4 contracts against an intended 12x2=24 cap). Zero it here so
    # this gate defers to the dynamic one, matching the documented intent.
    same_strike_static = int(getattr(live, "max_open_same_strike", 0))
    if float(getattr(live, "max_open_same_strike_multiple", 0.0)) > 0:
        same_strike_static = 0
    cfg = replace(
        config,
        max_stops_per_side=int(getattr(live, "live_max_stops_per_side", config.max_stops_per_side)),
        max_stops_per_day=int(
            getattr(live, "live_max_stops_per_day", getattr(config, "max_stops_per_day", 999))
        ),
        max_open_contracts=int(getattr(live, "max_open_contracts", 0)),
        max_open_contracts_per_side=int(getattr(live, "max_open_per_side", 0)),
        max_open_contracts_same_strike=same_strike_static,
        max_open_contracts_side_cluster=int(
            getattr(live, "max_open_side_cluster", 0)
        ),
        open_contract_side_cluster_points=float(
            getattr(live, "side_cluster_points", 0.0)
        ),
    )
    if bool(getattr(live, "use_portfolio_allocator_live", False)):
        cfg = replace(cfg, use_portfolio_allocator=True)
    # The simulator's condor sleeve yields two vertical candidates.  In live
    # execution that representation is unsafe unless they are submitted as one
    # four-leg order, so fail closed while the paired path is disabled.
    if not bool(getattr(live, "enable_paired_condor_live", False)):
        cfg = replace(cfg, use_condor_sleeve=False)
    return cfg


def open_spreads_as_trades(open_spreads: Sequence[Any]) -> list:
    """Minimal Trade-like objects for concentration checks."""
    trades = []
    for spread in open_spreads:
        if getattr(spread, "closed", False):
            continue
        cand = spread.candidate
        stopped = bool(getattr(spread, "stopped", False))
        trades.append(
            SimpleNamespace(
                side=cand.side,
                short_type=cand.short_type,
                short_strike=float(cand.short_strike),
                long_strike=float(cand.long_strike),
                contracts=int(getattr(spread, "contracts", 0) or 0),
                stopped=stopped,
                exit_reason="stop" if stopped else "open",
                model=getattr(cand, "sleeve", "") or "core",
            )
        )
    return trades


def live_entry_risk_block(
    candidate: Any,
    open_spreads: Sequence[Any],
    *,
    now: datetime,
    config: StrategyConfig,
    side_stop_cooldown_until: Mapping[str, datetime],
    side_stop_counts: Mapping[str, int],
) -> str:
    trades = open_spreads_as_trades(open_spreads)
    return entry_risk_block_reason(
        candidate,
        trades,
        now,
        config,
        global_stop_cooldown_until=None,
        side_stop_cooldown_until=dict(side_stop_cooldown_until),
        side_stop_counts=dict(side_stop_counts),
        intraday_memory_reasons=set(),
    )


def recover_side_stop_counts(events: Sequence[dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for event in events:
        if event.get("event") != "stop":
            continue
        side = str(event.get("side") or "")
        if side:
            counts[side] = counts.get(side, 0) + 1
    return counts
