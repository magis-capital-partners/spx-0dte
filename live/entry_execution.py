"""Entry order pricing, quote guards, and non-blocking combo fill polling."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

from live_config import LiveConfig
from mbh_simulator import CandidateRecord, OptionQuote

# Match ib_insync.OrderStatus DoneStates / ActiveStates (lowercase).
# PendingCancel is terminal for ladder purposes — never amend while IB is cancelling.
_DONE_STATUSES = frozenset({"filled", "cancelled", "apicancelled", "pendingcancel"})
_ACTIVE_STATUSES = frozenset({"apipending", "presubmitted", "submitted", "pendingsubmit"})


def round_spx_premium(price: float) -> float:
    if price <= 0:
        return price
    tick = 0.05 if price < 3.0 else 0.10
    return round(round(price / tick) * tick, 2)


def natural_credit(candidate: CandidateRecord) -> float:
    if candidate.short_quote is None or candidate.long_quote is None:
        return 0.0
    return max(candidate.short_quote.bid - candidate.long_quote.ask, 0.0)


def entry_limit_credit(natural: float, live: LiveConfig, *, ladder_step: int = 0) -> float:
    """Limit credit for combo open (positive = net credit received).

    Rounds down on the SPX tick grid so the concession is never lost to rounding.
    """
    haircut = live.entry_limit_concession + live.entry_ladder_step * ladder_step
    limit = natural - haircut
    tick = 0.05 if limit < 3.0 else 0.10
    limit = math.floor(limit / tick) * tick
    return round(max(limit, live.entry_min_credit), 2)


def entry_quote_block_reason(
    candidate: CandidateRecord,
    live: LiveConfig,
    *,
    leg_ages: Optional[List[Optional[float]]] = None,
) -> str:
    """Return a non-empty block reason when quotes are not safe to trade."""
    sq = candidate.short_quote
    lq = candidate.long_quote
    if sq is None or lq is None:
        return "missing_quotes"
    if sq.bid <= 0 or sq.ask <= 0 or lq.bid <= 0 or lq.ask <= 0:
        return "incomplete_nbbo"
    if live.entry_require_live_nbbo and (sq.bid >= sq.ask or lq.bid >= lq.ask):
        return "crossed_nbbo"
    nat = natural_credit(candidate)
    if nat < live.entry_min_credit:
        return "insufficient_credit"
    if live.max_leg_quote_age_seconds > 0 and leg_ages is not None:
        for age in leg_ages:
            if age is None or age > live.max_leg_quote_age_seconds:
                return "stale_quote"
    return ""


@dataclass
class PendingEntry:
    spread: Any
    trade: Any
    candidate: CandidateRecord
    contracts: int
    natural_credit: float
    limit_credit: float
    submitted_at: datetime
    work_until: datetime
    next_ladder_at: datetime
    ladder_step: int = 0
    tranche_time: Optional[datetime] = None
    sleeve: str = "core"
    score: float = 0.0


def _order_status(trade) -> str:
    return (trade.orderStatus.status or "").lower()


def _is_active_status(trade) -> bool:
    return _order_status(trade) in _ACTIVE_STATUSES


def pending_trade_is_active(pending: PendingEntry) -> bool:
    """True when the working entry order is still amendable/cancellable."""
    return _is_active_status(pending.trade)


def _hard_rejection_reason(trade) -> str:
    for entry in reversed(trade.log):
        msg = entry.message or ""
        code = getattr(entry, "errorCode", 0) or 0
        # 202 = IB price-collar / "Order Canceled" (align with ib_executor._trade_rejection_reason).
        if code in {201, 202, 203, 110} or "reject" in msg.lower() or "not allowed" in msg.lower():
            return msg or f"error {code}"
        if msg and "permission" in msg.lower():
            return msg
    status = _order_status(trade)
    if status in {"inactive", "apicancelled", "cancelled", "pendingcancel"} or "reject" in status:
        return trade.orderStatus.status or "rejected"
    return ""


def _is_filled(trade, quantity: int) -> bool:
    status = _order_status(trade)
    filled = float(trade.orderStatus.filled or 0)
    return status == "filled" or filled >= float(quantity)


def _filled_qty(trade) -> int:
    try:
        return int(round(float(trade.orderStatus.filled or 0)))
    except (TypeError, ValueError):
        return 0


def _entry_leg_fields(pending: PendingEntry) -> dict:
    """Persist short/long premiums so restart rebuilds stop = 3× short premium."""
    spread = pending.spread
    short_sell = float(getattr(spread, "short_entry_sell", 0.0) or 0.0)
    long_buy = float(getattr(spread, "long_entry_buy", 0.0) or 0.0)
    return {
        "short_entry_sell": round(short_sell, 4),
        "long_entry_buy": round(long_buy, 4),
    }


def _entry_fill_event(pending: PendingEntry, *, contracts: int, partial: bool = False) -> dict:
    trade = pending.trade
    fill_credit = abs(float(trade.orderStatus.avgFillPrice or pending.limit_credit))
    slippage = round(pending.natural_credit - fill_credit, 4)
    event = {
        "event": "entry",
        "side": pending.candidate.side,
        "sleeve": pending.sleeve,
        "short_strike": pending.candidate.short_strike,
        "long_strike": pending.candidate.long_strike,
        "contracts": contracts,
        "natural_credit": round(pending.natural_credit, 2),
        "limit_credit": round(pending.limit_credit, 2),
        "credit": round(fill_credit, 2),
        "fill_slippage": slippage,
        "score": round(pending.score, 3),
        "ladder_steps": pending.ladder_step,
        **_entry_leg_fields(pending),
    }
    if partial:
        event["partial"] = True
        event["requested_contracts"] = pending.contracts
    return event


def _entry_reject_event(pending: PendingEntry, *, reason: str, status: Optional[str] = None) -> dict:
    trade = pending.trade
    return {
        "event": "order_rejected",
        "side": pending.candidate.side,
        "short_strike": pending.candidate.short_strike,
        "long_strike": pending.candidate.long_strike,
        "contracts": pending.contracts,
        "natural_credit": round(pending.natural_credit, 2),
        "limit_credit": round(pending.limit_credit, 2),
        "credit": round(pending.limit_credit, 2),
        "status": status if status is not None else (trade.orderStatus.status or "unknown"),
        "reason": reason,
        **_entry_leg_fields(pending),
    }


def _resolve_terminal_or_reject(
    pending: PendingEntry,
    *,
    reason: str,
) -> Tuple[Optional[PendingEntry], dict]:
    """Book a partial fill if any, otherwise reject. Never leaves a Done trade pending."""
    filled = _filled_qty(pending.trade)
    if 0 < filled < pending.contracts:
        return None, _entry_fill_event(pending, contracts=filled, partial=True)
    if filled >= pending.contracts:
        return None, _entry_fill_event(pending, contracts=pending.contracts)
    return None, _entry_reject_event(pending, reason=reason)


def poll_pending_entry(
    ib,
    pending: PendingEntry,
    live: LiveConfig,
    today: str,
    now: datetime,
    *,
    log_event,
) -> Tuple[Optional[PendingEntry], Optional[dict]]:
    """Advance a working entry; return (remaining pending, resolution event)."""
    trade = pending.trade
    if _is_filled(trade, pending.contracts):
        return None, _entry_fill_event(pending, contracts=pending.contracts)

    hard = _hard_rejection_reason(trade)
    if hard:
        return _resolve_terminal_or_reject(pending, reason=hard)

    status = _order_status(trade)
    if status in _DONE_STATUSES:
        # Cancelled / ApiCancelled (Filled already handled above).
        return _resolve_terminal_or_reject(
            pending, reason=f"entry_terminal_{trade.orderStatus.status or status}",
        )

    if now >= pending.work_until:
        filled = _filled_qty(trade)
        if 0 < filled < pending.contracts:
            # Book the partial before cancelling the remainder — never orphan IB legs.
            ib.cancelOrder(trade.order)
            ib.sleep(0.25)
            return None, _entry_fill_event(pending, contracts=filled, partial=True)
        ib.cancelOrder(trade.order)
        ib.sleep(0.25)
        return None, _entry_reject_event(
            pending, reason="entry_unfilled", status="Cancelled",
        )

    if (
        live.entry_ladder_step > 0
        and now >= pending.next_ladder_at
        and pending.ladder_step < live.entry_max_ladder_steps
    ):
        if not _is_active_status(trade):
            # Not amendable and not filled — clear pending without placeOrder.
            raw = trade.orderStatus.status or status or "unknown"
            return _resolve_terminal_or_reject(
                pending, reason=f"entry_terminal_{raw}",
            )

        next_step = pending.ladder_step + 1
        new_limit = entry_limit_credit(
            pending.natural_credit, live, ladder_step=next_step,
        )
        if new_limit < pending.limit_credit:
            # Re-check immediately before any IB call — race: Cancelled between
            # the active check above and placeOrder. ib_insync wires the modify
            # then asserts DoneStates → bare AssertionError().
            if not _is_active_status(trade):
                raw = trade.orderStatus.status or status or "unknown"
                return _resolve_terminal_or_reject(
                    pending, reason=f"entry_terminal_{raw}",
                )
            old_price = trade.order.lmtPrice
            trade.order.lmtPrice = -new_limit
            try:
                # Never amend a Done/PendingCancel trade. If status flipped under
                # us, placeOrder raises; catch and clear without crashing the day.
                if not _is_active_status(trade):
                    trade.order.lmtPrice = old_price
                    raw = trade.orderStatus.status or status or "unknown"
                    return _resolve_terminal_or_reject(
                        pending, reason=f"entry_terminal_{raw}",
                    )
                ib.placeOrder(trade.contract, trade.order)
            except Exception:
                trade.order.lmtPrice = old_price
                try:
                    if _is_active_status(trade):
                        ib.cancelOrder(trade.order)
                        ib.sleep(0.25)
                except Exception:
                    pass
                return None, _entry_reject_event(
                    pending,
                    reason="entry_ladder_failed",
                    status=trade.orderStatus.status or "unknown",
                )
            pending.ladder_step = next_step
            pending.limit_credit = new_limit
            log_event(today, {
                "event": "entry_ladder",
                "side": pending.candidate.side,
                "short_strike": pending.candidate.short_strike,
                "long_strike": pending.candidate.long_strike,
                "ladder_step": pending.ladder_step,
                "limit_credit": round(new_limit, 2),
            })
            print(
                f"[{now.isoformat()}] ENTRY ladder step {pending.ladder_step} "
                f"{pending.candidate.side} limit={new_limit:.2f}"
            )
        else:
            pending.ladder_step = next_step
        pending.next_ladder_at = now + timedelta(seconds=live.entry_ladder_interval_seconds)

    return pending, None


def work_deadline(submitted_at: datetime, live: LiveConfig, entry_interval_minutes: int) -> datetime:
    if live.entry_work_seconds > 0:
        return submitted_at + timedelta(seconds=live.entry_work_seconds)
    # Default: work until ~30s before the next tranche boundary.
    interval = max(entry_interval_minutes, 1) * 60
    seconds = max(interval - 30, live.entry_ladder_interval_seconds)
    return submitted_at + timedelta(seconds=seconds)
