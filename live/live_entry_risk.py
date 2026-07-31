"""Adapters to reuse backtest ``entry_risk_block_reason`` in the live loop."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Mapping, Sequence

from mbh_simulator import StrategyConfig, entry_risk_block_reason


def apply_live_risk_overlays(config: StrategyConfig, live: Any) -> StrategyConfig:
    """Apply live-only stop caps and optional portfolio allocator."""
    cfg = replace(
        config,
        max_stops_per_side=int(getattr(live, "live_max_stops_per_side", config.max_stops_per_side)),
        max_stops_per_day=int(
            getattr(live, "live_max_stops_per_day", getattr(config, "max_stops_per_day", 999))
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
