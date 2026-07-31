"""Auditable live defined-risk and marked-return-on-margin ledger."""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def _round(value: float) -> float:
    return round(float(value), 2)


def build_risk_snapshot(
    open_spreads: Sequence[Any],
    quotes: Sequence[Any],
    *,
    multiplier: float,
) -> dict:
    """Return current risk/margin/mark values using the executor's live book.

    ``max_loss_no_stop`` is the defined expiry loss of an open vertical.  The
    stop value is an estimate at the configured short-leg trigger, assuming the
    retained long wing ultimately expires worthless; a fast market can exceed
    this estimate, so it is never labelled as a hard maximum.
    """
    lookup: Mapping[tuple, Any] = {
        (q.option_type, float(q.strike)): q for q in quotes
    }
    positions = []
    totals = {
        "max_loss_no_stop": 0.0,
        "planned_stop_loss": 0.0,
        "defined_risk_margin": 0.0,
        "marked_pnl": 0.0,
    }
    marked_count = 0
    active_count = 0
    for spread in open_spreads:
        if getattr(spread, "closed", False):
            continue
        candidate = spread.candidate
        qty = int(spread.contracts)
        width = abs(float(candidate.long_strike) - float(candidate.short_strike))
        credit = float(getattr(spread, "fill_credit", 0.0) or getattr(spread, "entry_credit", 0.0))
        stopped = bool(getattr(spread, "stopped", False))
        # A stopped short leaves a long option; it has no further margin call
        # or expiry loss beyond its already-realized short-cover cost.
        margin = 0.0 if stopped else width * multiplier * qty
        max_loss = 0.0 if stopped else max(width - credit, 0.0) * multiplier * qty
        stop_loss = 0.0 if stopped else max(float(spread.stop_price) - credit, 0.0) * multiplier * qty
        option_type = candidate.short_type
        long_q = lookup.get((option_type, float(candidate.long_strike)))
        short_q = lookup.get((option_type, float(candidate.short_strike)))
        mark = None
        if long_q is not None and getattr(long_q, "bid", 0) is not None:
            if stopped:
                short_cover = float(getattr(spread, "stop_fill_price", None) or spread.stop_price)
                per_contract = credit - short_cover + float(long_q.bid)
            elif short_q is not None and getattr(short_q, "ask", 0) not in (None, 0):
                per_contract = credit - float(short_q.ask) + float(long_q.bid)
            else:
                per_contract = None
            if per_contract is not None:
                mark = per_contract * multiplier * qty
                totals["marked_pnl"] += mark
                marked_count += 1
        totals["max_loss_no_stop"] += max_loss
        totals["planned_stop_loss"] += stop_loss
        totals["defined_risk_margin"] += margin
        active_count += 1
        positions.append({
            "side": candidate.side,
            "short_strike": float(candidate.short_strike),
            "long_strike": float(candidate.long_strike),
            "contracts": qty,
            "entry_credit": _round(credit),
            "max_loss_no_stop": _round(max_loss),
            "planned_stop_loss": _round(stop_loss),
            "defined_risk_margin": _round(margin),
            "marked_pnl": _round(mark) if mark is not None else None,
            "stopped": stopped,
            "condor_id": getattr(spread, "condor_id", None),
        })
    for key in totals:
        totals[key] = _round(totals[key])
    margin = totals["defined_risk_margin"]
    rom = totals["marked_pnl"] / margin if margin else 0.0
    totals["marked_return_on_margin"] = round(rom, 6)
    totals["marked_return_on_margin_pct"] = _round(rom * 100.0)
    totals["open_count"] = active_count
    totals["marked_count"] = marked_count
    totals["mark_quality"] = "ok" if marked_count == active_count else (
        "unavailable" if marked_count == 0 and active_count else "partial"
    )
    totals["positions"] = positions
    return totals
