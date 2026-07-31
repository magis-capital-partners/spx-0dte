"""Fail-closed pricing rules for IB net-credit combo orders."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ComboQuote:
    """IB BAG NBBO.  Credit combos are represented as negative prices."""

    bid: Optional[float]
    ask: Optional[float]


@dataclass(frozen=True)
class ComboPriceDecision:
    requested_credit: float
    allowed_credit: Optional[float]
    collar_credit: Optional[float]
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


def protect_credit_limit(requested_credit: float, quote: ComboQuote) -> ComboPriceDecision:
    """Return a credit limit that cannot be more aggressive than the BAG ask.

    IB represents this executor's credit BAG as a BUY order with a negative
    price.  A higher (less negative) order price is more aggressive and can be
    rejected by IB's price collar.  The BAG ask is therefore the least credit
    that can safely be requested.  We improve the requested credit to that
    bound rather than conceding extra credit.
    """
    if requested_credit <= 0:
        return ComboPriceDecision(requested_credit, None, None, "invalid_requested_credit")
    ask = quote.ask
    bid = quote.bid
    if ask is None or bid is None or ask >= 0 or bid >= 0 or bid > ask:
        return ComboPriceDecision(requested_credit, None, None, "combo_nbbo_unavailable")
    collar_credit = round(-ask, 2)
    return ComboPriceDecision(
        requested_credit=round(requested_credit, 2),
        allowed_credit=round(max(requested_credit, collar_credit), 2),
        collar_credit=collar_credit,
    )
