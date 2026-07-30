"""Regression tests for recovered-position market-data coverage."""
from __future__ import annotations

import sys
import time
import unittest
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from ib_market_data import CachedQuote, IBStreamingMarketData  # noqa: E402
from live_config import LiveConfig  # noqa: E402
from profiles import build_p3_poststop_cooldown_config  # noqa: E402


def _stream() -> IBStreamingMarketData:
    stream = object.__new__(IBStreamingMarketData)
    stream.live = LiveConfig(
        max_chain_lines=80,
        chain_points_below=350.0,
        chain_points_above=150.0,
    )
    stream.config = build_p3_poststop_cooldown_config(
        account_equity=500_000.0,
        baseline_contracts=2,
    )
    stream._spxw = SimpleNamespace(
        strikes=[float(strike) for strike in range(7000, 7605, 5)]
    )
    stream._required_0dte_legs = set()
    stream._expiry_0dte = "20260730"
    stream._cache = {}
    return stream


class RecoveredMarketDataTests(unittest.TestCase):
    def test_recovered_legs_replace_grid_lines_without_exceeding_budget(self) -> None:
        """2026-07-30 replay: recovered P7205 must displace a grid line, not disappear."""
        stream = _stream()
        stream._required_0dte_legs = {
            ("P", 7350.0),
            ("P", 7205.0),
            ("P", 7355.0),
            ("P", 7305.0),
        }

        specs = stream._desired_contract_specs(7375.2)

        self.assertLessEqual(len(specs), 80)
        for leg in stream._required_0dte_legs:
            self.assertIn(leg, specs)

    def test_recovered_legs_survive_spot_rebalance(self) -> None:
        stream = _stream()
        stream._required_0dte_legs = {
            ("P", 7350.0),
            ("P", 7205.0),
            ("P", 7355.0),
            ("P", 7305.0),
        }

        initial = stream._desired_contract_specs(7375.2)
        rebalanced = stream._desired_contract_specs(7475.2)

        for leg in stream._required_0dte_legs:
            self.assertIn(leg, initial)
            self.assertIn(leg, rebalanced)

    def test_zero_bid_protective_long_is_still_markable(self) -> None:
        stream = _stream()
        stream._required_0dte_legs = {("P", 7205.0)}
        stream._cache[("2026-07-30", "PUT", 7205.0)] = CachedQuote(
            bid=0.0,
            ask=0.05,
            updated_at=time.time(),
        )

        self.assertEqual(stream.missing_required_quotes(), [])

    def test_required_leg_without_ask_is_not_ready(self) -> None:
        stream = _stream()
        stream._required_0dte_legs = {("P", 7205.0)}
        stream._cache[("2026-07-30", "PUT", 7205.0)] = CachedQuote(
            bid=0.0,
            ask=0.0,
            updated_at=time.time(),
        )

        self.assertEqual(stream.missing_required_quotes(), [("P", 7205.0)])

    def test_resubscribe_prunes_snapshot_only_quotes_outside_active_plan(self) -> None:
        """Entry snapshots must not leave stale candidates beyond the line budget."""
        stream = _stream()
        active_specs = {("P", 7350.0), ("C", 7450.0)}
        stream._cache = {
            ("2026-07-30", "PUT", 7350.0): CachedQuote(
                bid=1.0, ask=1.1, updated_at=time.time()
            ),
            ("2026-07-30", "CALL", 7450.0): CachedQuote(
                bid=1.2, ask=1.3, updated_at=time.time()
            ),
            # A one-off snapshot from an earlier entry attempt.
            ("2026-07-30", "PUT", 7210.0): CachedQuote(
                bid=0.05, ask=0.10, updated_at=time.time()
            ),
        }

        stream._prune_0dte_cache(active_specs)

        self.assertEqual(
            set(stream._cache),
            {
                ("2026-07-30", "PUT", 7350.0),
                ("2026-07-30", "CALL", 7450.0),
            },
        )


if __name__ == "__main__":
    unittest.main()
