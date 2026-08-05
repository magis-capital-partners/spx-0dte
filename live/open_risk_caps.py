"""Live open-risk concentration caps."""
from __future__ import annotations

from typing import Any, Sequence


def _active(spreads: Sequence[Any]) -> list:
    return [
        s for s in spreads
        if not getattr(s, "closed", False) and not getattr(s, "stopped", False)
    ]


def open_risk_block_reason(
    candidate: Any,
    open_spreads: Sequence[Any],
    *,
    contracts: int,
    max_open_contracts: int,
    max_open_per_side: int,
    max_open_same_strike: int,
    max_open_same_strike_multiple: float = 0.0,
    max_open_side_cluster: int = 0,
    side_cluster_points: float = 0.0,
) -> str:
    """Return a block reason if adding ``contracts`` would breach live caps.

    ``max_open_same_strike_multiple`` (>0) makes the same-strike cap dynamic:
    it scales with ``contracts`` — the size actually being traded on this
    tranche right now, after VIX-elevated sizing / downsize-after-stop have
    already been applied — instead of a lot count fixed at config time. When
    set, it supersedes the static ``max_open_same_strike``.
    """
    active = _active(open_spreads)
    open_contracts = sum(int(s.contracts) for s in active)
    if max_open_contracts > 0 and open_contracts + contracts > max_open_contracts:
        return "max_open_contracts"
    same_side = [s for s in active if s.candidate.side == candidate.side]
    side_contracts = sum(int(s.contracts) for s in same_side)
    if max_open_per_side > 0 and side_contracts + contracts > max_open_per_side:
        return "max_open_per_side"
    same_strike = [
        s for s in same_side
        if s.candidate.short_type == candidate.short_type
        and float(s.candidate.short_strike) == float(candidate.short_strike)
    ]
    strike_contracts = sum(int(s.contracts) for s in same_strike)
    same_strike_cap = (
        int(round(max_open_same_strike_multiple * contracts))
        if max_open_same_strike_multiple > 0
        else max_open_same_strike
    )
    if same_strike_cap > 0 and strike_contracts + contracts > same_strike_cap:
        return "max_open_same_strike"
    if max_open_side_cluster > 0 and side_cluster_points > 0:
        clustered = [
            s for s in same_side
            if s.candidate.short_type == candidate.short_type
            and abs(
                float(s.candidate.short_strike) - float(candidate.short_strike)
            ) <= side_cluster_points
        ]
        cluster_contracts = sum(int(s.contracts) for s in clustered)
        if cluster_contracts + contracts > max_open_side_cluster:
            return "max_open_side_cluster"
    return ""
