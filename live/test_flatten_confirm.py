"""Tests for confirmed flatten (fill wait + residual IB check)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import CandidateRecord  # noqa: E402
from ib_executor import OpenSpread, flatten_all  # noqa: E402
from live_config import LiveConfig  # noqa: E402


def _spread() -> OpenSpread:
    cand = CandidateRecord(
        timestamp=datetime(2026, 7, 18, 10, 0),
        side="bear_call",
        status="open",
        reason="test",
        score=1.0,
        expiry="20260718",
        short_type="CALL",
        short_strike=7500.0,
        long_strike=7550.0,
        short_delta=None,
        long_delta=None,
        spot=7480.0,
        distance_pct=0.0,
        width=50.0,
        credit=1.5,
        credit_to_width=0.03,
        stop_loss_to_credit=3.0,
        straddle_residual_z=0.0,
        skew_z=0.0,
        term_ratio_z=0.0,
        trend_score=0.0,
        realized_vs_implied_z=0.0,
        contracts=1,
        sleeve="core",
    )
    return OpenSpread(
        candidate=cand,
        contracts=1,
        short_entry_sell=1.8,
        long_entry_buy=0.3,
        stop_price=5.4,
    )


class FlattenConfirmTests(unittest.TestCase):
    def test_dry_marks_closed(self) -> None:
        spread = _spread()
        live = LiveConfig()
        result = flatten_all(None, [spread], "2026-07-18", dry=True, live=live)
        self.assertTrue(spread.closed)
        self.assertEqual(result.closed, 1)
        self.assertTrue(result.complete)

    def test_filled_path(self) -> None:
        spread = _spread()
        live = LiveConfig(flatten_retry_mkt=False)

        class FakeStatus:
            status = "Filled"
            filled = 1
            avgFillPrice = 1.25

        class FakeOrder:
            orderId = 1
            totalQuantity = 1

        class FakeTrade:
            order = FakeOrder()
            orderStatus = FakeStatus()
            log = []

        class FakeIB:
            def placeOrder(self, *_a, **_k):
                return FakeTrade()

            def sleep(self, _s):
                return None

        with patch("ib_executor.HAS_IB", True), patch(
            "ib_executor.clear_short_leg_backstops", return_value=None
        ), patch(
            "ib_executor.build_combo", return_value=(SimpleNamespace(), SimpleNamespace())
        ), patch(
            "ib_executor._wait_for_order", return_value=("filled", "")
        ), patch(
            "ib_executor.fetch_ib_spxw_positions", return_value={}
        ), patch(
            "ib_executor.log_event", return_value=None
        ):
            result = flatten_all(FakeIB(), [spread], "2026-07-18", dry=False, live=live)

        self.assertTrue(spread.closed)
        self.assertEqual(result.closed, 1)
        self.assertTrue(result.complete)

    def test_unfilled_leaves_open(self) -> None:
        spread = _spread()
        live = LiveConfig(flatten_retry_mkt=True)

        class FakeStatus:
            status = "Submitted"
            filled = 0
            avgFillPrice = 0

        class FakeOrder:
            orderId = 1
            totalQuantity = 1

        class FakeTrade:
            order = FakeOrder()
            orderStatus = FakeStatus()
            log = []

        class FakeIB:
            def placeOrder(self, *_a, **_k):
                return FakeTrade()

            def cancelOrder(self, *_a, **_k):
                return None

            def sleep(self, _s):
                return None

        with patch("ib_executor.HAS_IB", True), patch(
            "ib_executor.clear_short_leg_backstops", return_value=None
        ), patch(
            "ib_executor.build_combo", return_value=(SimpleNamespace(), SimpleNamespace())
        ), patch(
            "ib_executor._wait_for_order", return_value=("pending", "")
        ), patch(
            "ib_executor.fetch_ib_spxw_positions", return_value={}
        ), patch(
            "ib_executor.log_event", return_value=None
        ):
            result = flatten_all(FakeIB(), [spread], "2026-07-18", dry=False, live=live)

        self.assertFalse(spread.closed)
        self.assertEqual(result.failed, 1)
        self.assertFalse(result.complete)


if __name__ == "__main__":
    unittest.main()
