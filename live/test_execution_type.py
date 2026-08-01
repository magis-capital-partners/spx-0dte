"""Regression coverage for paper/live execution labels and dashboard history."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "docs"))

from execution_type import execution_type  # noqa: E402
from build_dashboard_data import build_live  # noqa: E402


class ExecutionTypeTests(unittest.TestCase):
    def test_mode_mapping_and_recorded_value(self) -> None:
        self.assertEqual(execution_type("paper"), "paper")
        self.assertEqual(execution_type("dry"), "dry_run")
        self.assertEqual(execution_type("live"), "production_live")
        self.assertEqual(execution_type("paper", "production_live"), "production_live")

    def test_dashboard_backfills_legacy_modes_and_separates_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = {
                "2026-07-30": [
                    {"event": "session_start", "mode": "paper"},
                    {"event": "entry", "contracts": 1, "credit": 2.0},
                    {"event": "session_end", "marked_pnl": 125.0},
                ],
                "2026-07-31": [
                    {"event": "session_start", "mode": "live", "execution_type": "production_live"},
                    {"event": "entry", "contracts": 1, "credit": 2.5},
                    {"event": "session_end", "marked_pnl": -1200.0},
                ],
            }
            for day, events in sessions.items():
                folder = root / day
                folder.mkdir()
                (folder / "fills.jsonl").write_text(
                    "\n".join(json.dumps(event) for event in events), encoding="utf-8",
                )
            result = build_live(root, 500_000)
            self.assertEqual(result["days"]["2026-07-30"]["execution_type"], "paper")
            self.assertEqual(result["days"]["2026-07-30"]["execution_type_source"], "backfilled_from_mode")
            self.assertEqual(result["days"]["2026-07-31"]["execution_type"], "production_live")
            self.assertEqual(result["totals"]["paper"]["marked_pnl"], 125.0)
            self.assertEqual(result["totals"]["production_live"]["marked_pnl"], -1200.0)


if __name__ == "__main__":
    unittest.main()
