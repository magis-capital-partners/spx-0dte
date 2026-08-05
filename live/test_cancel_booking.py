"""A fill that beats a teardown cancel must be booked, not silently dropped.

Teardown paths (new_tranche, flatten, disconnect, faults) discard the pending
without running the poll resolver. Before this was fixed, an entry that filled
in that window became a short leg the loop did not know about: no synthetic
stop, no mark, not in the flatten set.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from entry_execution import PendingEntry  # noqa: E402
from ib_executor import OpenSpread, cancel_pending_entry  # noqa: E402
from live_config import LiveConfig  # noqa: E402
from mbh_simulator import CandidateRecord, OptionQuote  # noqa: E402
from profiles import build_p3_trend_skew_config  # noqa: E402


def _cand() -> CandidateRecord:
    return CandidateRecord(
        timestamp=datetime(2026, 8, 5, 10, 0),
        side="bear_call",
        status="pass",
        reason="",
        score=2.0,
        expiry="20260805",
        short_type="CALL",
        short_strike=7550.0,
        long_strike=7620.0,
        short_delta=-0.15,
        long_delta=-0.05,
        spot=7500.0,
        distance_pct=0.01,
        width=70.0,
        credit=1.5,
        credit_to_width=0.021,
        stop_loss_to_credit=3.0,
        straddle_residual_z=0.0,
        skew_z=0.0,
        term_ratio_z=0.0,
        trend_score=1.0,
        realized_vs_implied_z=0.0,
        short_quote=OptionQuote(
            datetime(2026, 8, 5, 10, 0), "20260805", "CALL", 7550.0, 1.75, 1.80,
        ),
        long_quote=OptionQuote(
            datetime(2026, 8, 5, 10, 0), "20260805", "CALL", 7620.0, 0.25, 0.30,
        ),
        contracts=2,
        sleeve="core",
    )


def _pending(*, filled: float, status: str = "Cancelled", contracts: int = 2) -> PendingEntry:
    cand = _cand()
    spread = OpenSpread(
        candidate=cand,
        contracts=contracts,
        short_entry_sell=1.75,
        long_entry_buy=0.30,
        stop_price=0.0,  # set by booking from short_entry_sell x stop_multiple
    )
    trade = SimpleNamespace(
        order=SimpleNamespace(orderId=7, totalQuantity=contracts),
        orderStatus=SimpleNamespace(
            status=status, filled=filled, avgFillPrice=-1.45,
        ),
        log=[],
        contract=object(),
    )
    submitted = datetime(2026, 8, 5, 10, 0)
    return PendingEntry(
        spread=spread,
        trade=trade,
        candidate=cand,
        contracts=contracts,
        natural_credit=1.50,
        limit_credit=1.45,
        submitted_at=submitted,
        work_until=submitted + timedelta(seconds=870),
        next_ladder_at=submitted + timedelta(seconds=60),
        tranche_time=submitted,
        sleeve="core",
        score=2.0,
    )


class _IB:
    def __init__(self) -> None:
        self.cancels = 0

    def cancelOrder(self, _order):
        self.cancels += 1

    def sleep(self, _s):
        return None


class CancelBookingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = build_p3_trend_skew_config()
        self.events: list = []
        # log_event writes to the live/ tree; capture instead.
        self._patches = [
            patch("ib_executor.HAS_IB", True),
            patch(
                "ib_executor.log_event",
                side_effect=lambda _today, event, **_k: self.events.append(event),
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def _event_names(self) -> list:
        return [e.get("event") for e in self.events]

    def test_full_fill_beating_cancel_is_booked_and_managed(self) -> None:
        pending = _pending(filled=2.0)
        book: list = []
        sleeve_margin: dict = {"core": 0.0}

        booking = cancel_pending_entry(
            _IB(),
            pending,
            "2026-08-05",
            reason="new_tranche",
            dry=False,
            open_spreads=book,
            config=self.config,
            sleeve_margin_used=sleeve_margin,
        )

        self.assertEqual(booking.contracts, 2)
        # In the book => manage_stops / _mark_book / flatten_all all see it.
        self.assertEqual(len(book), 1)
        self.assertIs(book[0], pending.spread)
        # Synthetic stop derived from the short premium, not left at 0.
        self.assertGreater(book[0].stop_price, 0.0)
        self.assertAlmostEqual(
            book[0].stop_price,
            round(1.75 * self.config.stop_multiple, 1),
            places=1,
        )
        # Credit and margin accounted.
        self.assertGreater(booking.credit_added, 0.0)
        self.assertGreater(booking.margin, 0.0)
        self.assertGreater(sleeve_margin["core"], 0.0)
        # Booked as an entry, and NOT double-counted as a rejection.
        self.assertIn("entry", self._event_names())
        self.assertNotIn("order_rejected", self._event_names())

    def test_partial_fill_books_only_filled_qty(self) -> None:
        pending = _pending(filled=1.0, contracts=2)
        book: list = []

        booking = cancel_pending_entry(
            _IB(),
            pending,
            "2026-08-05",
            reason="flatten",
            dry=False,
            open_spreads=book,
            config=self.config,
            sleeve_margin_used={},
        )

        self.assertEqual(booking.contracts, 1)
        self.assertEqual(book[0].contracts, 1)
        entry = next(e for e in self.events if e.get("event") == "entry")
        self.assertTrue(entry["partial"])
        self.assertEqual(entry["requested_contracts"], 2)
        self.assertEqual(entry["booked_at_cancel"], "flatten")

    def test_unfilled_cancel_books_nothing_and_still_rejects(self) -> None:
        pending = _pending(filled=0.0)
        book: list = []

        booking = cancel_pending_entry(
            _IB(),
            pending,
            "2026-08-05",
            reason="new_tranche",
            dry=False,
            open_spreads=book,
            config=self.config,
            sleeve_margin_used={},
        )

        self.assertEqual(booking.contracts, 0)
        self.assertEqual(book, [])
        # Unchanged prior behaviour for the ordinary unfilled case.
        self.assertIn("order_rejected", self._event_names())
        self.assertNotIn("entry", self._event_names())

    def test_fill_without_a_book_warns_but_does_not_reject(self) -> None:
        """No book supplied: log the fill, never claim it was rejected."""
        pending = _pending(filled=2.0)

        booking = cancel_pending_entry(
            _IB(), pending, "2026-08-05", reason="disconnect", dry=False,
        )

        self.assertEqual(booking.contracts, 0)
        self.assertIn("entry", self._event_names())
        self.assertNotIn("order_rejected", self._event_names())
        entry = next(e for e in self.events if e.get("event") == "entry")
        self.assertIsNone(entry["booked_at_cancel"])

    def test_dry_run_never_books(self) -> None:
        pending = _pending(filled=2.0)
        book: list = []
        booking = cancel_pending_entry(
            _IB(),
            pending,
            "2026-08-05",
            reason="new_tranche",
            dry=True,
            open_spreads=book,
            config=self.config,
        )
        self.assertEqual(booking.contracts, 0)
        self.assertEqual(book, [])
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
