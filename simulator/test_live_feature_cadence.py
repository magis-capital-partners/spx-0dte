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


if __name__ == "__main__":
    unittest.main()
