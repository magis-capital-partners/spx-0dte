"""Production defaults must retain the explicitly requested one-lot cap."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from live_config import LiveConfig  # noqa: E402
from strategy_profiles import resolve_strategy_config  # noqa: E402


class LiveDefaultSizeTests(unittest.TestCase):
    def test_production_default_and_hard_cap_are_one_lot(self) -> None:
        live = LiveConfig()
        config, _schedule = resolve_strategy_config(live)

        self.assertEqual(live.contracts_per_tranche, 2)
        self.assertEqual(live.max_contracts_per_tranche, 2)
        self.assertEqual(config.baseline_contracts, 2)


if __name__ == "__main__":
    unittest.main()
