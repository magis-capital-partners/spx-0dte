"""Every executor restart must retain its resolved configuration."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

import ib_executor  # noqa: E402
from live_config import LiveConfig  # noqa: E402
from mbh_simulator import StrategyConfig  # noqa: E402


class SessionConfigHistoryTests(unittest.TestCase):
    def test_restart_appends_history_instead_of_only_overwriting_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ib_executor, "LIVE_DIR", Path(tmp)):
                ib_executor.write_session_snapshot(
                    "2026-08-03", LiveConfig(contracts_per_tranche=1),
                    StrategyConfig(baseline_contracts=1), "linear_decay_downsize",
                )
                ib_executor.write_session_snapshot(
                    "2026-08-03", LiveConfig(contracts_per_tranche=2),
                    StrategyConfig(baseline_contracts=2), "linear_decay_downsize",
                )

            history = Path(tmp) / "2026-08-03" / "config_history.jsonl"
            rows = [json.loads(line) for line in history.read_text().splitlines()]
            self.assertEqual([r["strategy_config"]["baseline_contracts"] for r in rows], [1, 2])


if __name__ == "__main__":
    unittest.main()
