"""Late-day decay must scale a positive baseline, never round it to zero.

2026-08-04: baseline 1-lot × linear_decay_downsize 0.45 (post-14:30) rounded
to 0 and silently suppressed four afternoon tranches (``zero_base_contracts``).
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import StrategyConfig  # noqa: E402
from profiles import SCHEMES  # noqa: E402
from ib_executor import _tranche_base_contracts  # noqa: E402


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 8, 4, hour, minute, 0)


class TrancheSizingFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schedule = SCHEMES["linear_decay_downsize"]

    def _base(self, contracts: int, when: datetime, *, vix: float = 1.0,
              schedule=None) -> int:
        config = StrategyConfig(baseline_contracts=contracts)
        return _tranche_base_contracts(
            config, self.schedule if schedule is None else schedule, when,
            vix_sizing_multiplier=vix,
        )

    def test_one_lot_survives_late_day_decay(self) -> None:
        # The 2026-08-04 artifact: round(1 × 0.45) == 0 after 14:30.
        self.assertEqual(self._base(1, _at(14, 45)), 1)
        self.assertEqual(self._base(1, _at(15, 30)), 1)  # 0.25 tail segment

    def test_two_lot_decays_to_one(self) -> None:
        self.assertEqual(self._base(2, _at(14, 45)), 1)   # round(0.9)
        self.assertEqual(self._base(2, _at(13, 45)), 1)   # round(1.2) with 0.60

    def test_morning_upsize_still_applies(self) -> None:
        self.assertEqual(self._base(4, _at(10, 0)), 5)    # round(4 × 1.25)

    def test_explicit_zero_multiplier_still_halts(self) -> None:
        off_schedule = SCHEMES["morning_heavy_afternoon_off"]
        self.assertEqual(self._base(1, _at(15, 0), schedule=off_schedule), 0)

    def test_zero_baseline_stays_zero(self) -> None:
        self.assertEqual(self._base(0, _at(10, 0)), 0)

    def test_vix_multiplier_combined_before_single_rounding(self) -> None:
        # 1 × 0.45 × 1.25 = 0.5625 → floored at one lot, not double-rounded to 0.
        self.assertEqual(self._base(1, _at(14, 45), vix=1.25), 1)

    def test_no_schedule_flat_baseline(self) -> None:
        config = StrategyConfig(baseline_contracts=3)
        self.assertEqual(
            _tranche_base_contracts(config, None, _at(14, 45)), 3,
        )


if __name__ == "__main__":
    unittest.main()
