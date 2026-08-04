"""Upstream connectivity breaker state machine (IB errors 1100/1101/1102)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from connection_health import ConnectionHealthMonitor  # noqa: E402


class ConnectionHealthTests(unittest.TestCase):
    def test_1100_marks_upstream_down_and_emits_transition(self) -> None:
        mon = ConnectionHealthMonitor()
        self.assertFalse(mon.upstream_down)
        mon.on_ib_error(1100)
        self.assertTrue(mon.upstream_down)
        transitions = mon.consume_transitions()
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].kind, "upstream_lost")
        self.assertEqual(transitions[0].code, 1100)

    def test_repeated_1100_does_not_duplicate_transitions(self) -> None:
        mon = ConnectionHealthMonitor()
        for _ in range(7):  # 2026-08-04 saw seven 1100 events
            mon.on_ib_error(1100)
        self.assertEqual(mon.lost_count, 7)
        self.assertEqual(len(mon.consume_transitions()), 1)

    def test_1102_restores_without_resubscribe(self) -> None:
        mon = ConnectionHealthMonitor()
        mon.on_ib_error(1100)
        mon.consume_transitions()
        mon.on_ib_error(1102)
        self.assertFalse(mon.upstream_down)
        self.assertFalse(mon.resubscribe_required)
        transitions = mon.consume_transitions()
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].kind, "upstream_restored")
        self.assertFalse(transitions[0].resubscribe_required)

    def test_1101_restores_and_requires_resubscribe(self) -> None:
        mon = ConnectionHealthMonitor()
        mon.on_ib_error(1100)
        mon.on_ib_error(1101)
        self.assertFalse(mon.upstream_down)
        self.assertTrue(mon.resubscribe_required)
        transitions = mon.consume_transitions()
        self.assertEqual([t.kind for t in transitions],
                         ["upstream_lost", "upstream_restored"])
        self.assertTrue(transitions[1].resubscribe_required)
        mon.mark_resubscribed()
        self.assertFalse(mon.resubscribe_required)

    def test_1101_without_prior_1100_still_requires_resubscribe(self) -> None:
        # TWS may deliver only the restore event after a very short blip.
        mon = ConnectionHealthMonitor()
        mon.on_ib_error(1101)
        self.assertTrue(mon.resubscribe_required)
        transitions = mon.consume_transitions()
        self.assertEqual(len(transitions), 1)
        self.assertEqual(transitions[0].kind, "upstream_restored")

    def test_farm_messages_do_not_trip_breaker(self) -> None:
        mon = ConnectionHealthMonitor()
        for _ in range(84):  # 2026-08-04 farm-broken volume
            mon.on_ib_error(2103)
        self.assertFalse(mon.upstream_down)
        self.assertEqual(mon.consume_transitions(), [])
        self.assertEqual(mon.farm_broken_count, 84)

    def test_snapshot_shape(self) -> None:
        mon = ConnectionHealthMonitor()
        mon.on_ib_error(1100)
        snap = mon.snapshot()
        self.assertTrue(snap["upstream_down"])
        self.assertGreaterEqual(snap["outage_seconds"], 0.0)
        self.assertEqual(snap["lost_count"], 1)


if __name__ == "__main__":
    unittest.main()
