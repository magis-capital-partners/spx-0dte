"""Tests for live open-risk caps."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from open_risk_caps import open_risk_block_reason  # noqa: E402


def _spread(side="bear_call", strike=7500.0, contracts=2):
    cand = SimpleNamespace(side=side, short_type="CALL", short_strike=strike)
    return SimpleNamespace(candidate=cand, contracts=contracts, closed=False, stopped=False)


class OpenRiskCapTests(unittest.TestCase):
    def test_max_contracts(self) -> None:
        reason = open_risk_block_reason(
            _spread().candidate,
            [_spread(contracts=5)],
            contracts=2,
            max_open_contracts=6,
            max_open_per_side=10,
            max_open_same_strike=10,
        )
        self.assertEqual(reason, "max_open_contracts")

    def test_max_per_side(self) -> None:
        reason = open_risk_block_reason(
            _spread().candidate,
            [_spread(contracts=2)],
            contracts=2,
            max_open_contracts=20,
            max_open_per_side=3,
            max_open_same_strike=10,
        )
        self.assertEqual(reason, "max_open_per_side")

    def test_ok(self) -> None:
        reason = open_risk_block_reason(
            _spread().candidate,
            [_spread(contracts=1)],
            contracts=1,
            max_open_contracts=6,
            max_open_per_side=3,
            max_open_same_strike=2,
        )
        self.assertEqual(reason, "")

    def test_same_strike_static_cap_blocks(self) -> None:
        reason = open_risk_block_reason(
            _spread().candidate,
            [_spread(contracts=2)],
            contracts=2,
            max_open_contracts=20,
            max_open_per_side=20,
            max_open_same_strike=2,
        )
        self.assertEqual(reason, "max_open_same_strike")

    def test_same_strike_multiple_scales_with_current_tranche_size(self) -> None:
        """Dynamic cap = multiple x contracts being traded right now, not a
        fixed lot count — so it moves with VIX-elevated sizing or a
        downsize-after-stop, instead of going stale."""
        # Static cap (2) would block this, but the multiplier (12x current
        # size=2 -> 24) supersedes it and allows the add.
        reason = open_risk_block_reason(
            _spread().candidate,
            [_spread(contracts=2)],
            contracts=2,
            max_open_contracts=20,
            max_open_per_side=20,
            max_open_same_strike=2,
            max_open_same_strike_multiple=12.0,
        )
        self.assertEqual(reason, "")

    def test_same_strike_multiple_still_blocks_past_its_own_cap(self) -> None:
        # 12x a downsized 1-contract tranche = 12; 12 existing + 1 new = 13 > 12.
        reason = open_risk_block_reason(
            _spread().candidate,
            [_spread(contracts=12)],
            contracts=1,
            max_open_contracts=999,
            max_open_per_side=999,
            max_open_same_strike=2,
            max_open_same_strike_multiple=12.0,
        )
        self.assertEqual(reason, "max_open_same_strike")

    def test_same_strike_multiple_zero_falls_back_to_static(self) -> None:
        reason = open_risk_block_reason(
            _spread().candidate,
            [_spread(contracts=2)],
            contracts=2,
            max_open_contracts=20,
            max_open_per_side=20,
            max_open_same_strike=2,
            max_open_same_strike_multiple=0.0,
        )
        self.assertEqual(reason, "max_open_same_strike")

    def test_nearby_same_side_strikes_share_cluster_cap(self) -> None:
        reason = open_risk_block_reason(
            _spread(strike=7590.0).candidate,
            [_spread(strike=7580.0), _spread(strike=7585.0)],
            contracts=2,
            max_open_contracts=20,
            max_open_per_side=20,
            max_open_same_strike=20,
            max_open_side_cluster=4,
            side_cluster_points=25.0,
        )
        self.assertEqual(reason, "max_open_side_cluster")


if __name__ == "__main__":
    unittest.main()
