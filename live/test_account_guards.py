"""Tests for NetLiq / BuyingPower overlay guards."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from account_guards import (  # noqa: E402
    AccountSnapshot,
    check_loop_account_guard,
    check_startup_account_guard,
    snapshot_from_account_values,
)


class AccountGuardTests(unittest.TestCase):
    def test_snapshot_prefers_buying_power(self) -> None:
        snap = snapshot_from_account_values(
            {"NetLiquidation": "520000", "BuyingPower": "100000", "AvailableFunds": "50"}
        )
        self.assertEqual(snap.net_liquidation, 520000.0)
        self.assertEqual(snap.buying_power, 100000.0)

    def test_startup_ok(self) -> None:
        snap = AccountSnapshot(net_liquidation=500_000, buying_power=100_000)
        res = check_startup_account_guard(
            snap,
            account_equity=500_000,
            netliq_min_ratio=1.0,
            buying_power_min_ratio=0.15,
        )
        self.assertTrue(res.ok)

    def test_startup_netliq_fail(self) -> None:
        snap = AccountSnapshot(net_liquidation=400_000, buying_power=100_000)
        res = check_startup_account_guard(
            snap,
            account_equity=500_000,
            netliq_min_ratio=1.0,
            buying_power_min_ratio=0.15,
        )
        self.assertFalse(res.ok)
        self.assertIn("netliq_below_min", res.reason)

    def test_startup_bp_fail(self) -> None:
        snap = AccountSnapshot(net_liquidation=500_000, buying_power=10_000)
        res = check_startup_account_guard(
            snap,
            account_equity=500_000,
            netliq_min_ratio=1.0,
            buying_power_min_ratio=0.15,
        )
        self.assertFalse(res.ok)
        self.assertIn("buying_power_below_min", res.reason)

    def test_startup_missing_required(self) -> None:
        snap = AccountSnapshot(net_liquidation=None, buying_power=None)
        res = check_startup_account_guard(
            snap,
            account_equity=500_000,
            netliq_min_ratio=1.0,
            buying_power_min_ratio=0.15,
        )
        self.assertFalse(res.ok)

    def test_loop_halt(self) -> None:
        snap = AccountSnapshot(net_liquidation=440_000, buying_power=80_000)
        res = check_loop_account_guard(
            snap,
            account_equity=500_000,
            netliq_halt_ratio=0.90,
        )
        self.assertTrue(res.halt_entries)
        self.assertFalse(res.flatten)

    def test_loop_flatten_optional(self) -> None:
        snap = AccountSnapshot(net_liquidation=300_000, buying_power=10_000)
        res = check_loop_account_guard(
            snap,
            account_equity=500_000,
            netliq_halt_ratio=0.90,
            netliq_flatten_ratio=0.70,
            flatten_on_netliq_breach=True,
        )
        self.assertTrue(res.halt_entries)
        self.assertTrue(res.flatten)

    def test_fetch_uses_account_values_without_req_summary(self) -> None:
        from account_guards import fetch_account_snapshot
        from types import SimpleNamespace

        class FakeIB:
            def __init__(self):
                self.req_summary_calls = 0
                self.summary_calls = 0
                self._values = [
                    SimpleNamespace(tag="NetLiquidation", value="500000"),
                    SimpleNamespace(tag="BuyingPower", value="100000"),
                ]

            def reqAccountSummary(self):
                self.req_summary_calls += 1

            def accountSummary(self):
                self.summary_calls += 1
                return []

            def accountValues(self):
                return list(self._values)

        ib = FakeIB()
        snap = fetch_account_snapshot(ib)
        self.assertEqual(snap.net_liquidation, 500_000.0)
        self.assertEqual(snap.buying_power, 100_000.0)
        self.assertEqual(ib.req_summary_calls, 0)
        self.assertEqual(ib.summary_calls, 0)

    def test_fetch_falls_back_to_cached_account_summary(self) -> None:
        from account_guards import fetch_account_snapshot
        from types import SimpleNamespace

        class FakeIB:
            def __init__(self):
                self.req_summary_calls = 0
                self._summary = [
                    SimpleNamespace(tag="NetLiquidation", value="520000"),
                    SimpleNamespace(tag="AvailableFunds", value="90000"),
                ]

            def reqAccountSummary(self):
                self.req_summary_calls += 1

            def accountSummary(self):
                return list(self._summary)

            def accountValues(self):
                return []

        ib = FakeIB()
        snap = fetch_account_snapshot(ib)
        self.assertEqual(snap.net_liquidation, 520_000.0)
        self.assertEqual(snap.buying_power, 90_000.0)
        self.assertEqual(ib.req_summary_calls, 0)

if __name__ == "__main__":
    unittest.main()
