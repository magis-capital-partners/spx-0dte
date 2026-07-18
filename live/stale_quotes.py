"""Consecutive-poll stale-quote halt (entries only; never flatten on stale)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence


@dataclass
class StaleQuoteTracker:
    """Tracks consecutive polls where open short-leg quotes are too old."""

    consecutive: int = 0
    last_stale_legs: List[str] = field(default_factory=list)

    def reset(self) -> None:
        self.consecutive = 0
        self.last_stale_legs = []


@dataclass(frozen=True)
class StaleQuoteResult:
    confirmed: bool
    consecutive: int
    stale_legs: List[str]
    threshold_used: float


def _age_for_spread(
    spread: Any,
    quotes: Sequence[Any],
    *,
    quote_age_fn,
) -> Optional[float]:
    """Return short-leg quote age seconds, or None if unknown/missing."""
    if quote_age_fn is not None:
        age = quote_age_fn(spread.candidate.short_type, float(spread.candidate.short_strike))
        if age is not None:
            return float(age)
    # Fallback: treat missing short ask in the poll snapshot as infinitely stale.
    lookup = {(q.option_type, float(q.strike)): q for q in quotes}
    sq = lookup.get((spread.candidate.short_type, float(spread.candidate.short_strike)))
    if sq is None or float(getattr(sq, "ask", 0) or 0) <= 0:
        return None
    return 0.0  # present in snapshot → fresh for this poll


def evaluate_stale_quotes(
    tracker: StaleQuoteTracker,
    open_spreads: Sequence[Any],
    quotes: Sequence[Any],
    *,
    live: Any,
    quote_age_fn=None,
) -> StaleQuoteResult:
    """Update tracker; return whether halt should fire (confirmed consecutive stale)."""
    active = [s for s in open_spreads if not getattr(s, "closed", False) and not getattr(s, "stopped", False)]
    if not active:
        tracker.reset()
        return StaleQuoteResult(confirmed=False, consecutive=0, stale_legs=[], threshold_used=0.0)

    near_frac = float(getattr(live, "stop_near_fraction", 0.80))
    far_thresh = float(getattr(live, "stale_quote_halt_seconds", 20.0))
    near_thresh = float(getattr(live, "stale_quote_near_stop_seconds", 10.0))
    need = int(getattr(live, "stale_quote_confirm_polls", 3))

    lookup = {(q.option_type, float(q.strike)): q for q in quotes}
    stale_legs: List[str] = []
    worst_thresh = far_thresh

    for spread in active:
        cand = spread.candidate
        age = _age_for_spread(spread, quotes, quote_age_fn=quote_age_fn)
        sq = lookup.get((cand.short_type, float(cand.short_strike)))
        near_stop = False
        if sq is not None and spread.stop_price > 0 and float(sq.ask or 0) > 0:
            near_stop = float(sq.ask) >= near_frac * float(spread.stop_price)
        thresh = near_thresh if near_stop else far_thresh
        if near_stop:
            worst_thresh = min(worst_thresh, near_thresh)
        # Missing age or age above threshold → stale for this leg.
        if age is None or age > thresh:
            age_txt = f"{age:.1f}" if age is not None else "missing"
            stale_legs.append(
                f"{cand.short_type}:{cand.short_strike:g}"
                f"(age={age_txt},thresh={thresh:g})"
            )

    if not stale_legs:
        tracker.reset()
        return StaleQuoteResult(
            confirmed=False, consecutive=0, stale_legs=[], threshold_used=worst_thresh
        )

    tracker.consecutive += 1
    tracker.last_stale_legs = stale_legs
    confirmed = tracker.consecutive >= need
    return StaleQuoteResult(
        confirmed=confirmed,
        consecutive=tracker.consecutive,
        stale_legs=stale_legs,
        threshold_used=worst_thresh,
    )
