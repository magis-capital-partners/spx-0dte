"""Live feature state must advance once per canonical minute, not per poll."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from live_features import (  # noqa: E402
    SessionFeatureState,
    compute_raw_features_once_per_minute,
    signal_features_are_sane,
)
from mbh_simulator import OptionQuote, SignalSnapshot  # noqa: E402


def _quotes(ts: datetime, spot: float):
    strike = round(spot / 5.0) * 5.0
    expiry = ts.date().isoformat()
    return [
        OptionQuote(ts, expiry, "CALL", strike, 4.9, 5.1, 0.50, 0.20, spot),
        OptionQuote(ts, expiry, "PUT", strike, 4.9, 5.1, -0.50, 0.20, spot),
        OptionQuote(ts, expiry, "CALL", strike + 10, 1.9, 2.1, 0.25, 0.19, spot),
        OptionQuote(ts, expiry, "PUT", strike - 10, 1.9, 2.1, -0.25, 0.21, spot),
    ]


class LiveFeatureCadenceTests(unittest.TestCase):
    def test_absurd_vendor_zscore_fails_sanity_gate(self) -> None:
        signal = SignalSnapshot(
            timestamp=datetime(2026, 8, 3, 9, 32),
            realized_vs_implied_z=-8_616_164.0,
        )
        self.assertFalse(signal_features_are_sane(signal, max_abs_z=12.0))

    def test_repeated_polls_in_one_minute_do_not_mutate_state(self) -> None:
        state = SessionFeatureState()
        first = datetime(2026, 8, 3, 10, 2, 1)
        compute_raw_features_once_per_minute(_quotes(first, 100.0), 100.0, first, state)
        cached = compute_raw_features_once_per_minute(
            _quotes(first.replace(second=45), 101.0),
            101.0,
            first.replace(second=45),
            state,
        )

        self.assertEqual(state.spot_history, [100.0])
        self.assertEqual(state.previous_spot, 100.0)
        self.assertEqual(cached["trend_score"], 0.0)

        next_minute = datetime(2026, 8, 3, 10, 3, 2)
        raw = compute_raw_features_once_per_minute(
            _quotes(next_minute, 102.0), 102.0, next_minute, state
        )
        self.assertEqual(state.spot_history, [100.0, 102.0])
        self.assertAlmostEqual(raw["trend_score"], 0.2, places=6)

    def test_tranche_poll_upgrades_cached_term_ratio(self) -> None:
        """A non-tranche poll caching term_ratio_z=0.0 must not poison the
        tranche poll landing later in the same minute (raw 0.0 z-scores to
        ~+3.9 and would gate every candidate term_structure_dislocation)."""
        state = SessionFeatureState()
        first = datetime(2026, 8, 3, 10, 2, 1)
        # Poll without next-expiry quotes establishes the canonical minute.
        raw_first = compute_raw_features_once_per_minute(
            _quotes(first, 100.0), 100.0, first, state
        )
        self.assertEqual(raw_first["term_ratio_z"], 0.0)
        self.assertFalse(state.last_minute_had_next_expiry)

        # Tranche poll in the same minute supplies the next-expiry chain:
        # 0DTE straddle mid = 10.0, next-expiry straddle mid = 16.0.
        next_expiry = [
            OptionQuote(first, "2026-08-05", "CALL", 100.0, 7.9, 8.1, 0.50, 0.22, 100.0),
            OptionQuote(first, "2026-08-05", "PUT", 100.0, 7.9, 8.1, -0.50, 0.22, 100.0),
        ]
        raw_tranche = compute_raw_features_once_per_minute(
            _quotes(first.replace(second=30), 100.0),
            100.0,
            first.replace(second=30),
            state,
            next_expiry_quotes=next_expiry,
        )
        self.assertAlmostEqual(raw_tranche["term_ratio_z"], 10.0 / 16.0 - 1.0, places=6)
        self.assertTrue(state.last_minute_had_next_expiry)
        # State advanced exactly once for the minute.
        self.assertEqual(state.spot_history, [100.0])

        # A later poll without next-expiry data keeps the upgraded value.
        raw_again = compute_raw_features_once_per_minute(
            _quotes(first.replace(second=50), 100.0),
            100.0,
            first.replace(second=50),
            state,
        )
        self.assertAlmostEqual(raw_again["term_ratio_z"], 10.0 / 16.0 - 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
