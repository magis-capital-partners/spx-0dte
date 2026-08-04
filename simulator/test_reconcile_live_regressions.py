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
    _executed_by_tranche,
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


if __name__ == "__main__":
    unittest.main()
