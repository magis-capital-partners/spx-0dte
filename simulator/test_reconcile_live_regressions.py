"""Regression coverage for live replay fidelity and asynchronous fills."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from reconcile_live import (  # noqa: E402
    PARITY_GATE_FLIPS_ALERT,
    _executed_by_tranche,
    _gate_flip,
    _signal_parity_summary,
    resolve_replay_config,
)


class ReconcileLiveRegressionTests(unittest.TestCase):
    def test_live_overlay_disables_unpaired_condor(self) -> None:
        snapshot = {
            "live_config": {
                "mode": "live",
                "enable_paired_condor_live": False,
                "contracts_per_tranche": 2,
            }
        }
        _live, config, _schedule = resolve_replay_config(snapshot)
        self.assertFalse(config.use_condor_sleeve)

    def test_async_fill_is_linked_to_original_tranche(self) -> None:
        events = [
            {
                "event": "entry",
                "ts": "2026-08-03T10:02:26.132635",
                "tranche_time": "2026-08-03T10:02:00",
                "contracts": 2,
            }
        ]
        counts = _executed_by_tranche(events)
        self.assertEqual(counts[datetime(2026, 8, 3, 10, 2)], 2)


class SignalParitySummaryTests(unittest.TestCase):
    @staticmethod
    def _diff(skew_live: float, skew_bt: float, trend_live: float = 0.0, trend_bt: float = 0.0) -> dict:
        return {
            "live_skew_z": skew_live,
            "backtest_skew_z": skew_bt,
            "skew_delta": round(skew_live - skew_bt, 3),
            "live_trend_z": trend_live,
            "backtest_trend_z": trend_bt,
            "trend_delta": round(trend_live - trend_bt, 3),
        }

    def test_gate_flip_detects_opposite_sides_of_cliff(self) -> None:
        # 2026-08-03 shape: live below -0.65 (bull_put blocked), backtest above.
        self.assertTrue(_gate_flip(-0.7, 0.36, 0.65))
        # Both on the same side: no flip even with a large delta.
        self.assertFalse(_gate_flip(-1.4, -0.8, 0.65))
        # Upper cliff too (bear_call side).
        self.assertTrue(_gate_flip(0.7, 0.3, 0.65))

    def test_summary_counts_and_alert(self) -> None:
        diffs = [
            self._diff(-0.70, 0.36),   # flip
            self._diff(-0.60, -0.55),  # tracks
            self._diff(0.70, 0.30),    # flip
            self._diff(0.68, 0.90),    # same side above gate: no flip
        ]
        summary = _signal_parity_summary(diffs)
        self.assertEqual(summary["n"], 4)
        self.assertEqual(summary["skew"]["gate_flips"], 2)
        self.assertAlmostEqual(summary["skew"]["max_abs_delta"], 1.06, places=6)
        # 2 flips < PARITY_GATE_FLIPS_ALERT(3); mean|delta| decides.
        mean_abs = summary["skew"]["mean_abs_delta"]
        self.assertEqual(summary["alert"], mean_abs > 0.30)

    def test_alert_fires_on_gate_flips(self) -> None:
        flip = self._diff(-0.70, 0.36)
        track = self._diff(-0.10, -0.11)
        # Enough tracking rows to hold mean|delta| under the bar; flips alone alert.
        diffs = [flip] * PARITY_GATE_FLIPS_ALERT + [track] * 100
        summary = _signal_parity_summary(diffs)
        self.assertLessEqual(summary["skew"]["mean_abs_delta"], 0.30)
        self.assertTrue(summary["alert"])

    def test_empty_diffs_do_not_alert(self) -> None:
        summary = _signal_parity_summary([])
        self.assertEqual(summary["n"], 0)
        self.assertIsNone(summary["skew"]["mean_abs_delta"])
        self.assertFalse(summary["alert"])


if __name__ == "__main__":
    unittest.main()
