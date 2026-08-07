"""Unit tests for entry limit pricing, quote guards, and pending-entry polling."""
from __future__ import annotations

import sys
import unittest
from dataclasses import replace
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
    EntryQualityResult,
    PendingEntry,
    evaluate_entry_quality,
    entry_limit_credit,
    entry_quote_block_reason,
    natural_credit,
    poll_pending_entry,
    refresh_pending_entry_quality,
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
        # The entry poll path runs while the book is short 0DTE gamma; blocking
        # here blinds stop management. Cancels are awaited across polls instead.
        raise AssertionError("blocking ib.sleep is forbidden on the entry poll path")


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
        tranche_time=now.replace(second=0, microsecond=0),
        ladder_step=ladder_step,
        sleeve="core",
        score=1.5,
    )


class EntryExecutionTests(unittest.TestCase):
    def test_fast_entry_quality_accepts_unchanged_market(self) -> None:
        now = datetime(2026, 8, 3, 10, 2, 2)
        cand = _cand(7545, 7615, bid=3.25, ask_long=0.10)
        cand.timestamp = now.replace(second=0)
        result = evaluate_entry_quality(
            cand,
            LiveConfig(),
            now=now,
            current_spot=7501.0,
            spot_age_seconds=0.2,
            reference_spot=7500.0,
            reference_credit=natural_credit(cand),
            reference_short_delta=0.15,
            leg_ages=[0.2, 0.3],
            leg_update_times=[100.0, 99.8],
            short_delta_min=0.10,
            short_delta_max=0.30,
        )
        self.assertTrue(result.ok)

    def test_fast_entry_quality_blocks_spot_credit_delta_and_desync(self) -> None:
        now = datetime(2026, 8, 3, 10, 2, 2)
        cand = _cand(7545, 7615, bid=3.25, ask_long=0.10)
        cand.timestamp = now.replace(second=0)
        base = dict(
            candidate=cand,
            live=LiveConfig(
                entry_max_spot_drift_points=8.0,
                entry_min_credit_ratio=0.80,
                entry_max_short_delta_drift=0.05,
                entry_max_leg_timestamp_dispersion_seconds=1.0,
            ),
            now=now,
            current_spot=7500.0,
            spot_age_seconds=0.2,
            reference_spot=7500.0,
            reference_credit=natural_credit(cand),
            reference_short_delta=0.15,
            leg_ages=[0.2, 0.3],
            leg_update_times=[100.0, 99.8],
            short_delta_min=0.10,
            short_delta_max=0.30,
        )

        self.assertEqual(evaluate_entry_quality(**{**base, "current_spot": 7510.0}).reason, "spot_drift")

        cand.short_quote = replace(cand.short_quote, bid=1.5)
        self.assertEqual(evaluate_entry_quality(**base).reason, "credit_deterioration")
        cand.short_quote = replace(cand.short_quote, bid=3.20)

        cand.short_quote = replace(cand.short_quote, delta=0.24)
        self.assertEqual(evaluate_entry_quality(**base).reason, "short_delta_drift")
        cand.short_quote = replace(cand.short_quote, delta=0.15)

        self.assertEqual(
            evaluate_entry_quality(**{**base, "leg_update_times": [100.0, 97.0]}).reason,
            "quote_desync",
        )

    def test_fast_entry_quality_blocks_expired_signal(self) -> None:
        now = datetime(2026, 8, 3, 10, 3, 20)
        cand = _cand(7545, 7615, bid=3.25, ask_long=0.10)
        cand.timestamp = datetime(2026, 8, 3, 10, 2)
        result = evaluate_entry_quality(
            cand,
            LiveConfig(entry_max_signal_age_seconds=75.0),
            now=now,
            current_spot=7500.0,
            spot_age_seconds=0.2,
            reference_spot=7500.0,
            reference_credit=natural_credit(cand),
            reference_short_delta=0.15,
            leg_ages=[0.2, 0.3],
            leg_update_times=[100.0, 99.8],
            short_delta_min=0.10,
            short_delta_max=0.30,
        )
        self.assertEqual(result.reason, "signal_expired")

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
    def test_quality_deterioration_cancels_working_order_before_ladder(self) -> None:
        """Cancel is requested at once, then resolved on a later poll (no ib.sleep)."""
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB()
        trade = _FakeTrade("Submitted")
        pending = _pending(trade, now=now, next_ladder_in=-1.0)
        live = LiveConfig(entry_ladder_step=0.05)

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now,
            log_event=lambda *_a, **_k: None,
            quality_block_reason="entry_quality_spot_drift",
        )
        # Phase 1: cancel sent, pending retained, nothing booked yet.
        self.assertIsNotNone(remaining)
        self.assertIsNone(resolution)
        self.assertEqual(ib.cancel_calls, 1)
        self.assertEqual(ib.place_calls, 0)
        assert remaining is not None
        self.assertEqual(remaining.cancel_reason, "entry_quality_spot_drift")

        # Phase 2: IB confirms; the original block reason is what gets booked.
        trade.orderStatus.status = "Cancelled"
        remaining2, resolution2 = poll_pending_entry(
            ib, remaining, live, "2026-07-29", now + timedelta(seconds=0.5),
            log_event=lambda *_a, **_k: None,
            quality_block_reason="entry_quality_spot_drift",
        )
        self.assertIsNone(remaining2)
        assert resolution2 is not None
        self.assertEqual(resolution2["reason"], "entry_quality_spot_drift")
        # No second cancel and never a placeOrder while cancelling.
        self.assertEqual(ib.cancel_calls, 1)
        self.assertEqual(ib.place_calls, 0)

    def test_awaited_cancel_resolves_on_grace_timeout(self) -> None:
        """IB never confirms: resolve at the grace bound rather than hanging."""
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB()
        pending = _pending(_FakeTrade("Submitted"), now=now, next_ladder_in=-1.0)
        live = LiveConfig(entry_ladder_step=0.05, entry_cancel_grace_seconds=1.0)

        remaining, _ = poll_pending_entry(
            ib, pending, live, "2026-07-29", now,
            log_event=lambda *_a, **_k: None,
            quality_block_reason="entry_quality_spot_drift",
        )
        assert remaining is not None

        # Still inside the grace window and still unconfirmed: keep waiting.
        mid, res_mid = poll_pending_entry(
            ib, remaining, live, "2026-07-29", now + timedelta(seconds=0.5),
            log_event=lambda *_a, **_k: None,
            quality_block_reason="entry_quality_spot_drift",
        )
        self.assertIsNotNone(mid)
        self.assertIsNone(res_mid)

        # Past the grace bound: book the reject.
        assert mid is not None
        final, res_final = poll_pending_entry(
            ib, mid, live, "2026-07-29", now + timedelta(seconds=1.5),
            log_event=lambda *_a, **_k: None,
            quality_block_reason="entry_quality_spot_drift",
        )
        self.assertIsNone(final)
        assert res_final is not None
        self.assertEqual(res_final["event"], "order_rejected")
        self.assertEqual(res_final["reason"], "entry_quality_spot_drift")
        self.assertEqual(ib.cancel_calls, 1)

    def test_fill_during_awaited_cancel_is_booked(self) -> None:
        """A fill that lands while the cancel is in flight must still be booked."""
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB()
        trade = _FakeTrade("Submitted")
        pending = _pending(trade, now=now, next_ladder_in=-1.0)
        live = LiveConfig(entry_ladder_step=0.05)

        remaining, _ = poll_pending_entry(
            ib, pending, live, "2026-07-29", now,
            log_event=lambda *_a, **_k: None,
            quality_block_reason="entry_quality_spot_drift",
        )
        assert remaining is not None

        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = 2.0
        trade.orderStatus.avgFillPrice = -3.05
        final, resolution = poll_pending_entry(
            ib, remaining, live, "2026-07-29", now + timedelta(seconds=0.2),
            log_event=lambda *_a, **_k: None,
            quality_block_reason="entry_quality_spot_drift",
        )
        self.assertIsNone(final)
        assert resolution is not None
        self.assertEqual(resolution["event"], "entry")
        self.assertEqual(resolution["contracts"], 2)

    def test_partial_fill_during_awaited_cancel_is_booked(self) -> None:
        """Deadline cancel with a partial fill books the partial, never orphans legs."""
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB()
        trade = _FakeTrade("Submitted")
        # work_until already passed -> deadline cancel path.
        pending = _pending(trade, now=now, work_seconds=-1.0, next_ladder_in=600.0)
        live = LiveConfig(entry_ladder_step=0.05)

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now, log_event=lambda *_a, **_k: None,
        )
        self.assertIsNotNone(remaining)
        self.assertIsNone(resolution)
        self.assertEqual(ib.cancel_calls, 1)

        assert remaining is not None
        trade.orderStatus.status = "Cancelled"
        trade.orderStatus.filled = 1.0
        trade.orderStatus.avgFillPrice = -3.05
        final, res = poll_pending_entry(
            ib, remaining, live, "2026-07-29", now + timedelta(seconds=0.3),
            log_event=lambda *_a, **_k: None,
        )
        self.assertIsNone(final)
        assert res is not None
        self.assertEqual(res["event"], "entry")
        self.assertTrue(res["partial"])
        self.assertEqual(res["contracts"], 1)
        self.assertEqual(res["requested_contracts"], 2)

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

    def test_tape_replay_cancelled_ladder_no_place_no_assert(self) -> None:
        """2026-07-29 tape: Cancelled at 09:48:49, ladder due at 09:49 with AssertionError if place attempted."""
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB(place_raises=AssertionError())
        pending = _pending(
            _FakeTrade("Cancelled"),
            now=now,
            next_ladder_in=-1.0,
            ladder_step=1,
            limit_credit=9.20,
            natural=9.35,
        )
        live = LiveConfig(
            entry_ladder_step=0.05,
            entry_limit_concession=0.05,
            entry_max_ladder_steps=3,
        )

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now, log_event=lambda *_a, **_k: None,
        )

        self.assertIsNone(remaining)
        assert resolution is not None
        self.assertEqual(resolution["event"], "order_rejected")
        self.assertEqual(ib.place_calls, 0)
        self.assertEqual(pending.ladder_step, 1)
        self.assertAlmostEqual(pending.limit_credit, 9.20, places=2)

    def test_pending_cancel_terminal_no_place(self) -> None:
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB(place_raises=AssertionError())
        pending = _pending(
            _FakeTrade("PendingCancel"),
            now=now,
            next_ladder_in=-1.0,
            ladder_step=1,
            limit_credit=9.20,
            natural=9.35,
        )
        live = LiveConfig(entry_ladder_step=0.05, entry_limit_concession=0.05)

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now, log_event=lambda *_a, **_k: None,
        )

        self.assertIsNone(remaining)
        assert resolution is not None
        self.assertEqual(resolution["event"], "order_rejected")
        self.assertEqual(ib.place_calls, 0)

    def test_hard_reject_on_ib_error_202(self) -> None:
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB(place_raises=AssertionError())
        trade = _FakeTrade("Submitted")
        trade.log.append(
            SimpleNamespace(
                message="Order Canceled - limit too aggressive",
                errorCode=202,
            )
        )
        pending = _pending(
            trade,
            now=now,
            next_ladder_in=-1.0,
            ladder_step=1,
            limit_credit=9.20,
            natural=9.35,
        )
        live = LiveConfig(entry_ladder_step=0.05, entry_limit_concession=0.05)

        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now, log_event=lambda *_a, **_k: None,
        )

        self.assertIsNone(remaining)
        assert resolution is not None
        self.assertEqual(resolution["event"], "order_rejected")
        self.assertIn("aggressive", resolution["reason"].lower())
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

        # Failed amend requests a cancel and defers; the step must not advance
        # and the limit must be restored to its pre-amend value.
        self.assertIsNotNone(remaining)
        self.assertIsNone(resolution)
        self.assertEqual(pending.ladder_step, 0)
        self.assertAlmostEqual(pending.limit_credit, 3.20, places=2)
        self.assertAlmostEqual(trade.order.lmtPrice, -3.0, places=2)
        self.assertEqual(ib.place_calls, 1)
        self.assertEqual(ib.cancel_calls, 1)

        assert remaining is not None
        trade.orderStatus.status = "Cancelled"
        final, res = poll_pending_entry(
            ib, remaining, live, "2026-07-29", now + timedelta(seconds=0.3),
            log_event=lambda *_a, **_k: None,
        )
        self.assertIsNone(final)
        assert res is not None
        self.assertEqual(res["event"], "order_rejected")
        self.assertEqual(res["reason"], "entry_ladder_failed")
        self.assertEqual(pending.ladder_step, 0)
        # No retry of the amend, no second cancel.
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
        self.assertEqual(resolution["tranche_time"], "2026-07-29T09:49:00")
        self.assertEqual(ib.place_calls, 0)


class _FakeQualityProvider:
    """Returns a scripted sequence of quality results, one per evaluate call."""

    def __init__(self, results: List[EntryQualityResult]):
        self._results = results
        self.refresh_calls = 0
        self.evaluate_calls = 0

    def refresh_candidate_legs(self, _candidate, _now):
        self.refresh_calls += 1

    def evaluate_candidate_quality(
        self, _candidate, _now, *, reference_spot, reference_credit, reference_short_delta,
    ):
        result = self._results[min(self.evaluate_calls, len(self._results) - 1)]
        self.evaluate_calls += 1
        return result


class RefreshPendingEntryQualityTests(unittest.TestCase):
    def test_diagnostics_freeze_once_cancel_is_requested(self) -> None:
        """Regression: a rejection logged for quote_desync must carry the
        diagnostics that actually triggered the cancel, not values from a
        later poll that would have passed."""
        now = datetime(2026, 7, 29, 9, 49, 0)
        ib = _FakeIB()
        trade = _FakeTrade("Submitted")
        pending = _pending(trade, now=now, next_ladder_in=-1.0)
        pending.reference_spot = 7500.0
        pending.reference_natural_credit = 3.15
        pending.reference_short_delta = 0.15
        live = LiveConfig(entry_ladder_step=0.05)

        desync_result = EntryQualityResult(
            False,
            "quote_desync",
            {
                "entry_quality_reason": "quote_desync",
                "entry_quality_quote_dispersion_seconds": 2.4,
            },
        )
        ok_result = EntryQualityResult(
            True,
            diagnostics={
                "entry_quality_reason": "ok",
                "entry_quality_quote_dispersion_seconds": 0.26,
            },
        )
        provider = _FakeQualityProvider([desync_result, ok_result, ok_result])

        # Poll 1: the quote-desync check fails and a cancel is requested.
        quality_block = refresh_pending_entry_quality(provider, pending, now)
        self.assertEqual(quality_block, "entry_quality_quote_desync")
        remaining, resolution = poll_pending_entry(
            ib, pending, live, "2026-07-29", now,
            log_event=lambda *_a, **_k: None,
            quality_block_reason=quality_block,
        )
        self.assertIsNone(resolution)
        assert remaining is not None
        self.assertIsNotNone(remaining.cancel_requested_at)
        self.assertEqual(remaining.entry_diagnostics["entry_quality_reason"], "quote_desync")

        # Poll 2: cancel is in flight. A fresh quote check would now pass, but
        # it must not run — the diagnostics that justified the cancel stay frozen.
        later = now + timedelta(seconds=0.3)
        quality_block2 = refresh_pending_entry_quality(provider, remaining, later)
        self.assertEqual(quality_block2, "")
        self.assertEqual(provider.evaluate_calls, 1)
        self.assertEqual(provider.refresh_calls, 1)
        self.assertEqual(remaining.entry_diagnostics["entry_quality_reason"], "quote_desync")
        self.assertEqual(
            remaining.entry_diagnostics["entry_quality_quote_dispersion_seconds"], 2.4,
        )

        # IB confirms the cancel: the logged rejection must be self-consistent.
        trade.orderStatus.status = "Cancelled"
        final, res_final = poll_pending_entry(
            ib, remaining, live, "2026-07-29", later + timedelta(seconds=0.5),
            log_event=lambda *_a, **_k: None,
            quality_block_reason=quality_block2,
        )
        self.assertIsNone(final)
        assert res_final is not None
        self.assertEqual(res_final["event"], "order_rejected")
        self.assertEqual(res_final["reason"], "entry_quality_quote_desync")
        self.assertEqual(res_final["entry_quality_reason"], "quote_desync")
        self.assertEqual(res_final["entry_quality_quote_dispersion_seconds"], 2.4)

    def test_no_active_pending_still_refreshes_when_not_cancelling(self) -> None:
        now = datetime(2026, 7, 29, 9, 49, 0)
        trade = _FakeTrade("Submitted")
        pending = _pending(trade, now=now, next_ladder_in=-1.0)
        pending.reference_spot = 7500.0
        pending.reference_natural_credit = 3.15
        pending.reference_short_delta = 0.15
        ok_result = EntryQualityResult(True, diagnostics={"entry_quality_reason": "ok"})
        provider = _FakeQualityProvider([ok_result])

        quality_block = refresh_pending_entry_quality(provider, pending, now)

        self.assertEqual(quality_block, "")
        self.assertEqual(provider.refresh_calls, 1)
        self.assertEqual(provider.evaluate_calls, 1)
        self.assertEqual(pending.entry_diagnostics["entry_quality_reason"], "ok")

    def test_missing_reference_data_skips_refresh(self) -> None:
        now = datetime(2026, 7, 29, 9, 49, 0)
        trade = _FakeTrade("Submitted")
        pending = _pending(trade, now=now, next_ladder_in=-1.0)
        provider = _FakeQualityProvider([EntryQualityResult(True)])

        quality_block = refresh_pending_entry_quality(provider, pending, now)

        self.assertEqual(quality_block, "")
        self.assertEqual(provider.refresh_calls, 0)
        self.assertEqual(provider.evaluate_calls, 0)


if __name__ == "__main__":
    unittest.main()
