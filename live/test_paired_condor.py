"""Regression checks for atomic four-leg condor construction."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

import ib_executor as executor  # noqa: E402
from mbh_simulator import CandidateRecord  # noqa: E402


class _Option:
    next_id = 100

    def __init__(self, *args, **kwargs) -> None:
        self.conId = _Option.next_id
        _Option.next_id += 1


class _Contract:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)
        self.comboLegs = []


class _Leg:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class _IB:
    def qualifyContracts(self, *contracts) -> None:
        self.qualified = contracts


def _candidate(option_type: str) -> CandidateRecord:
    return CandidateRecord(
        timestamp=datetime(2026, 7, 31, 10), side="bull_put" if option_type == "PUT" else "bear_call",
        status="selected", reason="", score=2.0, expiry="2026-07-31", short_type=option_type,
        short_strike=7400 if option_type == "PUT" else 7600,
        long_strike=7250 if option_type == "PUT" else 7675,
        short_delta=-0.16, long_delta=-0.05, spot=7500, distance_pct=0.01, width=150,
        credit=2.0, credit_to_width=0.01, stop_loss_to_credit=3.0,
        straddle_residual_z=0, skew_z=0, term_ratio_z=0, trend_score=0,
        realized_vs_implied_z=0, sleeve="condor",
    )


class PairedCondorTests(unittest.TestCase):
    def test_four_leg_bag_has_both_verticals(self) -> None:
        _Option.next_id = 100
        with patch.object(executor, "Option", _Option), patch.object(executor, "Contract", _Contract), patch.object(executor, "ComboLeg", _Leg):
            bag = executor.build_paired_condor_combo(_IB(), _candidate("PUT"), _candidate("CALL"), "2026-07-31")
        self.assertEqual([leg.action for leg in bag.comboLegs], ["SELL", "BUY", "SELL", "BUY"])
        self.assertEqual([leg.conId for leg in bag.comboLegs], [100, 101, 102, 103])

    def test_rejects_unpaired_option_types(self) -> None:
        with self.assertRaises(ValueError):
            executor.build_paired_condor_combo(_IB(), _candidate("CALL"), _candidate("PUT"), "2026-07-31")


if __name__ == "__main__":
    unittest.main()
