"""Realized session P&L and contract counts recovered from the fills log.

``build_risk_snapshot`` marks only the *currently open* book. A stopped spread
stays in that book, so its realized short-cover cost survives inside
``marked_pnl``; a spread that is fully closed sets ``spread.closed`` — which
happens solely on the flatten path — drops out of the book entirely and takes
its P&L with it.

So a flattened session publishes ``marked_pnl = 0.0`` no matter how the day
actually went (2026-08-05: four spreads closed for roughly +$1,180 of realized
cash, dashboard showed zero). This module recovers that missing piece from
fills.jsonl so status can report ``total_pnl = closed_pnl + marked_pnl`` with no
double counting: every spread is in exactly one of the two terms.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# SPX/SPXW options are $100 per point.
DEFAULT_MULTIPLIER = 100.0


def _spread_key(event: dict) -> Tuple[str, float, float]:
    return (
        str(event.get("side") or ""),
        float(event.get("short_strike") or 0.0),
        float(event.get("long_strike") or 0.0),
    )


@dataclass
class _Lot:
    """One entry fill, decremented as contracts are closed out."""
    key: Tuple[str, float, float]
    credit: float
    remaining: int
    stop_fill: Optional[float] = None


@dataclass(frozen=True)
class SessionPnl:
    closed_pnl: float
    credit_received: float
    entry_count: int
    contracts_traded: int
    open_contracts: int
    stopped_contracts: int

    def as_dict(self) -> Dict[str, Any]:
        return {
            "closed_pnl": round(self.closed_pnl, 2),
            "credit_received": round(self.credit_received, 2),
            "entry_count": self.entry_count,
            "contracts_traded": self.contracts_traded,
            "open_contracts": self.open_contracts,
            "stopped_contracts": self.stopped_contracts,
        }


def _match_lots(lots: List[_Lot], key: Tuple[str, float, float], qty: int) -> List[Tuple[_Lot, int]]:
    """FIFO-allocate ``qty`` contracts against open lots for ``key``."""
    taken: List[Tuple[_Lot, int]] = []
    outstanding = qty
    for lot in lots:
        if outstanding <= 0:
            break
        if lot.key != key or lot.remaining <= 0:
            continue
        use = min(lot.remaining, outstanding)
        taken.append((lot, use))
        outstanding -= use
    return taken


def session_pnl_summary(
    events: Sequence[dict],
    *,
    multiplier: float = DEFAULT_MULTIPLIER,
) -> SessionPnl:
    """Realized P&L of fully-closed spreads, plus contract counts, for one day.

    ``closed_pnl`` covers only spreads removed from the book by a flatten.
    Open and stopped-but-retained spreads are deliberately excluded because the
    executor's ``marked_pnl`` already carries them.
    """
    lots: List[_Lot] = []
    closed_pnl = 0.0
    credit_received = 0.0
    entry_count = 0
    contracts_traded = 0

    for event in events:
        name = event.get("event")

        if name == "entry":
            try:
                qty = int(event.get("contracts") or 0)
                credit = float(event.get("credit") or 0.0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            entry_count += 1
            contracts_traded += qty
            credit_received += credit * qty * multiplier
            lots.append(_Lot(key=_spread_key(event), credit=credit, remaining=qty))
            continue

        if name == "stop":
            raw = event.get("stop_fill")
            if raw is None:
                raw = event.get("stop_price")
            try:
                stop_fill = float(raw)
                qty = int(event.get("contracts") or 0)
            except (TypeError, ValueError):
                continue
            # A stop closes the short leg but keeps the long wing, so the lot
            # stays open and marked; only record what the cover cost.
            for lot, _use in _match_lots(lots, _spread_key(event), max(qty, 0)):
                if lot.stop_fill is None:
                    lot.stop_fill = stop_fill
            continue

        if name == "flatten_fill":
            try:
                fill_price = float(event.get("fill_price") or 0.0)
                qty = int(event.get("contracts") or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            for lot, use in _match_lots(lots, _spread_key(event), qty):
                # fill_price is signed as cash: negative = debit paid to close.
                per_contract = lot.credit + fill_price
                if lot.stop_fill is not None:
                    # Short was already bought back; the flatten only sold the
                    # retained long wing.
                    per_contract -= lot.stop_fill
                closed_pnl += per_contract * use * multiplier
                lot.remaining -= use
            continue

    open_contracts = sum(lot.remaining for lot in lots)
    stopped_contracts = sum(
        lot.remaining for lot in lots if lot.stop_fill is not None
    )
    return SessionPnl(
        closed_pnl=closed_pnl,
        credit_received=credit_received,
        entry_count=entry_count,
        contracts_traded=contracts_traded,
        open_contracts=open_contracts,
        stopped_contracts=stopped_contracts,
    )
