"""Unit tests for entry limit pricing, quote guards, and pending-entry polling."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from live_config import LiveConfig  # noqa: E402
from mbh_simulator import CandidateRecord, OptionQuote  # noqa: E402
from entry_execution import (  # noqa: E402
    PendingEntry,
    entry_limit_credit,
    entry_quote_block_reason,
    natural_credit,
    poll_pending_entry,
    work_deadline,
)


def _cand(short: float, long: float, *, bid: float = 3.0, ask_long: float = 0.2) -> CandidateRecord:
    return CandidateRecord(
        timestamp=datetime.now(),
        side="bear_call",
        status="pass",
        reason="",
        score=2.0,
        expiry="20260706",
        short_type="CALL",
        short_strike=short,
        long_strike=long,
        short_delta=-0.15,
        long_delta=-0.05,
        spot=7500.0,
        distance_pct=0.01,
        width=abs(long - short),
        credit=bid - ask_long,
        credit_to_width=(bid - ask_long) / abs(long - short),
        stop_loss_to_credit=2.0,
        straddle_residual_z=0.0,
        skew_z=0.0,
        term_ratio_z=0.0,
        trend_score=1.0,
        realized_vs_implied_z=0.0,
        short_quote=OptionQuote(datetime.now(), "20260706", "CALL", short, bid - 0.05, bid),
        long_quote=OptionQuote(datetime.now(), "20260706", "CALL", long, ask_long - 0.05, ask_long),
    )


class _FakeOrderStatus:
    def __init__(self, status: str, *, filled: float = 0.0, avg_fill: float = 0.0):
        self.status = status
        self.filled = filled
        self.avgFillPrice = avg_fill


class _FakeOrder:
    def __init__(self, lmt_price: float = -3.0):
        self.lmtPrice = lmt_price
        self.orderId = 42


class _FakeTrade:
    def __init__(self, status: str, *, filled: float = 0.0, avg_fill: float = 0.0):
        self.orderStatus = _FakeOrderStatus(status, filled=filled, avg_fill=avg_fill)
        self.order = _FakeOrder()
        self.contract = object()
        self.log: List[Any] = []


class _FakeIB:
    def __init__(self, *, place_raises: Optional[BaseException] = None):
        self.place_calls = 0
        self.cancel_calls = 0
        self._place_raises = place_raises

    def placeOrder(self, _contract, _order):
        self.place_calls += 1
        if self._place_raises is not None:
            raise self._place_raises

    def cancelOrder(self, _order):
        self.cancel_calls += 1

    def sleep(self, _s):
        return None


def _pending(
    trade: _FakeTrade,
    *,
    now: datetime,
    ladder_step: int = 0,
    limit_credit: float = 3.0,
    natural: float = 3.15,
    work_seconds: float = 600.0,
    next_ladder_in: float = -1.0,
) -> PendingEntry:
    cand = _cand(7545, 7615, bid=3.25, ask_long=0.10)
    spread = SimpleNamespace(short_entry_sell=3.25, long_entry_buy=0.10)
    return PendingEntry(
        spread=spread,
        trade=trade,
        candidate=cand,
        contracts=2,
        natural_credit=natural,
        limit_credit=limit_credit,
        submitted_at=now - timedelta(seconds=90),
        work_until=now + timedelta(seconds=work_seconds),
        next_ladder_at=now + timedelta(seconds=next_ladder_in),
        ladder_step=ladder_step,
        sleeve="core",
        score=1.5,
    )


class EntryExecutionTests(unittest.TestCase):
    def test_natural_credit(self) -> None:
        cand = _cand(7545, 7615, bid=3.25, ask_long=0.15)
        self.assertAlmostEqual(natural_credit(cand), 3.05, places=2)

    def test_limit_applies_concession(self) -> None:
        live = LiveConfig(entry_limit_concession=0.05)
        self.assertAlmostEqual(entry_limit_credit(3.20, live), 3.10, places=2)

    def test_ladder_walks_price(self) -> None:
        live = LiveConfig(entry_limit_concession=0.05, entry_ladder_step=0.05)
        self.assertAlmostEqual(entry_limit_credit(3.20, live, ladder_step=2), 3.00, places=2)

    def test_blocks_incomplete_nbbo(self) -> None:
        cand = _cand(7545, 7615)
        cand = CandidateRecord(
            **{**cand.__dict__, "long_quote": OptionQuote(
                datetime.now(), "20260706", "CALL", 7615.0, 0.0, 0.0,
            )}
        )
        self.assertEqual(entry_quote_block_reason(cand, LiveConfig()), "incomplete_nbbo")

    def test_blocks_stale_quote(self) -> None:
        cand = _cand(7545, 7615)
        live = LiveConfig(max_leg_quote_age_seconds=5.0)
        self.assertEqual(
            entry_quote_block_reason(cand, live, leg_ages=[10.0, 1.0]),
            "stale_quote",
        )

    def test_work_deadline_uses_config_seconds(self) -> None:
        live = LiveConfig(entry_work_seconds=600.0)
        start = datetime(2026, 7, 6, 13, 32, 0)
        end = work_deadline(start, live, 15)
        self.assertEqual((end - start).total_seconds(), 600.0)


class PollPendingEntryTests(unittest.TestCase):
    def test_cancelled_rejects_without_place_order(self) -> None:
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB()
        pending = _pending(_FakeTrade("Cancelled"), now=now, next_ladder_in=-1.0)
        live = LiveConfig(entry_ladder_step=0.05, entry_limit_concession=0.05)
        events: List[dict] = []

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now, log_event=lambda *_a, **_k: events.append(_a[1]),
        )

        self.assertIsNone(remaining)
        self.assertIsNotNone(resolution)
        assert resolution is not None
        self.assertEqual(resolution["event"], "order_rejected")
        self.assertIn("Cancelled", resolution["reason"])
        self.assertEqual(ib.place_calls, 0)

    def test_done_at_ladder_time_rejects_no_assert(self) -> None:
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB(place_raises=AssertionError())
        pending = _pending(
            _FakeTrade("Cancelled"),
            now=now,
            next_ladder_in=-1.0,
            ladder_step=0,
        )
        live = LiveConfig(entry_ladder_step=0.05)

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now, log_event=lambda *_a, **_k: None,
        )

        self.assertIsNone(remaining)
        assert resolution is not None
        self.assertEqual(resolution["event"], "order_rejected")
        self.assertEqual(ib.place_calls, 0)
        self.assertEqual(pending.ladder_step, 0)

    def test_active_ladder_places_once_and_bumps_step(self) -> None:
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB()
        # step0 limit=3.20; step1=3.10 so amend fires.
        pending = _pending(
            _FakeTrade("Submitted"),
            now=now,
            next_ladder_in=-1.0,
            ladder_step=0,
            limit_credit=3.20,
            natural=3.25,
        )
        live = LiveConfig(
            entry_ladder_step=0.05,
            entry_limit_concession=0.05,
            entry_max_ladder_steps=3,
        )
        events: List[dict] = []

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now,
            log_event=lambda today, event: events.append(event),
        )

        self.assertIs(remaining, pending)
        self.assertIsNone(resolution)
        self.assertEqual(ib.place_calls, 1)
        self.assertEqual(pending.ladder_step, 1)
        self.assertAlmostEqual(pending.limit_credit, 3.10, places=2)
        self.assertEqual(events[0]["event"], "entry_ladder")

    def test_ladder_place_failure_rejects_without_step_bump(self) -> None:
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB(place_raises=AssertionError())
        trade = _FakeTrade("Submitted")
        pending = _pending(
            trade,
            now=now,
            next_ladder_in=-1.0,
            ladder_step=0,
            limit_credit=3.20,
            natural=3.25,
        )
        live = LiveConfig(entry_ladder_step=0.05, entry_limit_concession=0.05)

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now, log_event=lambda *_a, **_k: None,
        )

        self.assertIsNone(remaining)
        assert resolution is not None
        self.assertEqual(resolution["event"], "order_rejected")
        self.assertEqual(resolution["reason"], "entry_ladder_failed")
        self.assertEqual(pending.ladder_step, 0)
        self.assertAlmostEqual(pending.limit_credit, 3.20, places=2)
        self.assertEqual(ib.place_calls, 1)
        self.assertEqual(ib.cancel_calls, 1)

    def test_cancelled_partial_books_entry(self) -> None:
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB()
        trade = _FakeTrade("Cancelled", filled=1.0, avg_fill=-3.05)
        pending = _pending(trade, now=now, next_ladder_in=-1.0)
        live = LiveConfig(entry_ladder_step=0.05)

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now, log_event=lambda *_a, **_k: None,
        )

        self.assertIsNone(remaining)
        assert resolution is not None
        self.assertEqual(resolution["event"], "entry")
        self.assertTrue(resolution.get("partial"))
        self.assertEqual(resolution["contracts"], 1)
        self.assertEqual(ib.place_calls, 0)


if __name__ == "__main__":
    unittest.main()
