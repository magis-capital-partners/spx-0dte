"""Entry order pricing, quote guards, and non-blocking combo fill polling."""
from __future__ import annotations

import time as _time
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

from live_config import LiveConfig
from mbh_simulator import CandidateRecord, OptionQuote


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


def _hard_rejection_reason(trade) -> str:
    for entry in reversed(trade.log):
        msg = entry.message or ""
        code = getattr(entry, "errorCode", 0) or 0
        if code in {201, 203, 110} or "reject" in msg.lower() or "not allowed" in msg.lower():
            return msg or f"error {code}"
        if msg and "permission" in msg.lower():
            return msg
    status = _order_status(trade)
    if status in {"inactive", "apicancelled"} or "reject" in status:
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
        fill_credit = abs(float(trade.orderStatus.avgFillPrice or pending.limit_credit))
        slippage = round(pending.natural_credit - fill_credit, 4)
        legs = _entry_leg_fields(pending)
        return None, {
            "event": "entry",
            "side": pending.candidate.side,
            "sleeve": pending.sleeve,
            "short_strike": pending.candidate.short_strike,
            "long_strike": pending.candidate.long_strike,
            "contracts": pending.contracts,
            "natural_credit": round(pending.natural_credit, 2),
            "limit_credit": round(pending.limit_credit, 2),
            "credit": round(fill_credit, 2),
            "fill_slippage": slippage,
            "score": round(pending.score, 3),
            "ladder_steps": pending.ladder_step,
            **legs,
        }

    hard = _hard_rejection_reason(trade)
    if hard:
        return None, {
            "event": "order_rejected",
            "side": pending.candidate.side,
            "short_strike": pending.candidate.short_strike,
            "long_strike": pending.candidate.long_strike,
            "contracts": pending.contracts,
            "natural_credit": round(pending.natural_credit, 2),
            "limit_credit": round(pending.limit_credit, 2),
            "credit": round(pending.limit_credit, 2),
            "status": trade.orderStatus.status,
            "reason": hard,
            **_entry_leg_fields(pending),
        }

    if now >= pending.work_until:
        filled = _filled_qty(trade)
        if 0 < filled < pending.contracts:
            # Book the partial before cancelling the remainder — never orphan IB legs.
            ib.cancelOrder(trade.order)
            ib.sleep(0.25)
            fill_credit = abs(float(trade.orderStatus.avgFillPrice or pending.limit_credit))
            slippage = round(pending.natural_credit - fill_credit, 4)
            return None, {
                "event": "entry",
                "side": pending.candidate.side,
                "sleeve": pending.sleeve,
                "short_strike": pending.candidate.short_strike,
                "long_strike": pending.candidate.long_strike,
                "contracts": filled,
                "requested_contracts": pending.contracts,
                "partial": True,
                "natural_credit": round(pending.natural_credit, 2),
                "limit_credit": round(pending.limit_credit, 2),
                "credit": round(fill_credit, 2),
                "fill_slippage": slippage,
                "score": round(pending.score, 3),
                "ladder_steps": pending.ladder_step,
                **_entry_leg_fields(pending),
            }
        ib.cancelOrder(trade.order)
        ib.sleep(0.25)
        return None, {
            "event": "order_rejected",
            "side": pending.candidate.side,
            "short_strike": pending.candidate.short_strike,
            "long_strike": pending.candidate.long_strike,
            "contracts": pending.contracts,
            "natural_credit": round(pending.natural_credit, 2),
            "limit_credit": round(pending.limit_credit, 2),
            "credit": round(pending.limit_credit, 2),
            "status": "Cancelled",
            "reason": "entry_unfilled",
            **_entry_leg_fields(pending),
        }

    if (
        live.entry_ladder_step > 0
        and now >= pending.next_ladder_at
        and pending.ladder_step < live.entry_max_ladder_steps
    ):
        pending.ladder_step += 1
        new_limit = entry_limit_credit(pending.natural_credit, live, ladder_step=pending.ladder_step)
        if new_limit < pending.limit_credit:
            pending.limit_credit = new_limit
            trade.order.lmtPrice = -new_limit
            ib.placeOrder(trade.contract, trade.order)
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
        pending.next_ladder_at = now + timedelta(seconds=live.entry_ladder_interval_seconds)

    return pending, None


def work_deadline(submitted_at: datetime, live: LiveConfig, entry_interval_minutes: int) -> datetime:
    if live.entry_work_seconds > 0:
        return submitted_at + timedelta(seconds=live.entry_work_seconds)
    # Default: work until ~30s before the next tranche boundary.
    interval = max(entry_interval_minutes, 1) * 60
    seconds = max(interval - 30, live.entry_ladder_interval_seconds)
    return submitted_at + timedelta(seconds=seconds)
