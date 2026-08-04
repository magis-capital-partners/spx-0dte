"""Fixed production wings must never silently widen to another risk profile."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from mbh_simulator import OptionQuote, StrategyConfig, select_long_wing  # noqa: E402


class ExactWingSelectionTests(unittest.TestCase):
    def test_missing_150_point_put_wing_fails_closed(self) -> None:
        ts = datetime(2026, 8, 3, 10, 2)
        short = OptionQuote(ts, "2026-08-03", "PUT", 7530.0, 3.8, 3.9, -0.20, 0.20)
        wrong = OptionQuote(ts, "2026-08-03", "PUT", 7340.0, 0.1, 0.2, -0.01, 0.30)
        cfg = StrategyConfig(put_wing_width=150.0, fixed_wing_tolerance=0.0)
        self.assertIsNone(select_long_wing([short, wrong], short, -1, cfg, side="bull_put"))

    def test_exact_150_point_put_wing_is_selected(self) -> None:
        ts = datetime(2026, 8, 3, 10, 2)
        short = OptionQuote(ts, "2026-08-03", "PUT", 7530.0, 3.8, 3.9, -0.20, 0.20)
        exact = OptionQuote(ts, "2026-08-03", "PUT", 7380.0, 0.1, 0.2, -0.02, 0.28)
        cfg = StrategyConfig(put_wing_width=150.0, fixed_wing_tolerance=0.0)
        self.assertIs(select_long_wing([short, exact], short, -1, cfg, side="bull_put"), exact)


if __name__ == "__main__":
    unittest.main()
