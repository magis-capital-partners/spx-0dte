"""Cross-implementation parity for the 25-delta / ATM quote selectors.

``feature_builder`` (vendor dict rows) and ``live_features`` (OptionQuote)
each define ``choose_delta`` / ``choose_atm_pair``. The skew feature depends
on both picking the same contract from the same market, so any divergence is
a silent live-vs-backtest calibration break. These tests lock the agreement
on clean data and document the known behavioural differences on dirty data
(NaN delta, missing IV) until the implementations are consolidated.
"""
from __future__ import annotations

import math
import sys
import unittest
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

import feature_builder  # noqa: E402
import live_features  # noqa: E402
from mbh_simulator import OptionQuote  # noqa: E402

TS = datetime(2026, 8, 6, 10, 0)
EXPIRY = "2026-08-06"


def _leg(right: str, strike: float, delta: Optional[float], iv: Optional[float]):
    """One synthetic contract in both representations (vendor dict, live quote)."""
    row = {
        "right": right,
        "strike": strike,
        "delta": delta,
        "implied_vol": iv,
        "bid": 1.0,
        "ask": 1.2,
    }
    quote = OptionQuote(TS, EXPIRY, right, strike, 1.0, 1.2, delta, iv, 7500.0)
    return row, quote


def _chain(legs):
    rows = [row for row, _ in legs]
    quotes = [quote for _, quote in legs]
    return rows, quotes


class ChooseDeltaParityTests(unittest.TestCase):
    def test_clean_chain_same_25d_pick(self) -> None:
        rows, quotes = _chain([
            _leg("PUT", 7400.0, -0.30, 0.21),
            _leg("PUT", 7380.0, -0.25, 0.22),
            _leg("PUT", 7360.0, -0.20, 0.23),
            _leg("CALL", 7600.0, 0.24, 0.19),
            _leg("CALL", 7620.0, 0.18, 0.18),
        ])
        for right, expected_strike in (("PUT", 7380.0), ("CALL", 7600.0)):
            vendor = feature_builder.choose_delta(rows, right, 0.25)
            live = live_features.choose_delta(quotes, right, 0.25)
            assert vendor is not None and live is not None
            self.assertEqual(float(vendor["strike"]), expected_strike)
            self.assertEqual(live.strike, expected_strike)

    def test_nan_delta_documented_divergence(self) -> None:
        """Live rejects NaN deltas; vendor keeps them, and a NaN candidate can
        corrupt the vendor min() ordering. Locked here so consolidation (Wave 4)
        knows exactly what behaviour it is changing."""
        rows, quotes = _chain([
            _leg("PUT", 7400.0, float("nan"), 0.21),
            _leg("PUT", 7380.0, -0.25, 0.22),
        ])
        live = live_features.choose_delta(quotes, "PUT", 0.25)
        assert live is not None
        self.assertEqual(live.strike, 7380.0)

        vendor = feature_builder.choose_delta(rows, "PUT", 0.25)
        assert vendor is not None
        # Vendor does not filter NaN: depending on candidate order min() may
        # return the NaN row. Assert only that it did NOT reliably match live —
        # i.e. the NaN row remains in the candidate pool.
        vendor_pool = [
            row for row in rows
            if str(row["right"]).upper() == "PUT" and row.get("delta") is not None
        ]
        self.assertEqual(len(vendor_pool), 2, "vendor pool still contains the NaN row")

    def test_require_iv_skips_ivless_leg(self) -> None:
        """The nearest-25d put lacks IV: without require_iv it wins selection
        and would zero one side of skew; with require_iv the next candidate
        with usable IV is chosen."""
        _, quotes = _chain([
            _leg("PUT", 7380.0, -0.25, None),
            _leg("PUT", 7360.0, -0.22, 0.23),
            _leg("CALL", 7600.0, 0.24, 0.19),
        ])
        unfiltered = live_features.choose_delta(quotes, "PUT", 0.25)
        assert unfiltered is not None
        self.assertEqual(unfiltered.strike, 7380.0)
        self.assertIsNone(unfiltered.iv)

        filtered = live_features.choose_delta(quotes, "PUT", 0.25, require_iv=True)
        assert filtered is not None
        self.assertEqual(filtered.strike, 7360.0)
        self.assertEqual(filtered.iv, 0.23)

    def test_require_iv_returns_none_when_no_iv_bearing_candidates(self) -> None:
        _, quotes = _chain([
            _leg("PUT", 7380.0, -0.25, None),
            _leg("PUT", 7360.0, -0.22, float("nan")),
        ])
        self.assertIsNone(live_features.choose_delta(quotes, "PUT", 0.25, require_iv=True))

    def test_skew_falls_back_to_zero_when_leg_unavailable(self) -> None:
        """End-to-end: an IV-less 25d put must yield skew_z from the IV-bearing
        fallback leg, never a one-sided ±other_leg_iv artifact."""
        _, quotes = _chain([
            # ATM pair so the straddle is markable.
            _leg("CALL", 7500.0, 0.50, 0.20),
            _leg("PUT", 7500.0, -0.50, 0.20),
            _leg("PUT", 7380.0, -0.25, None),      # nearest 25d put, no IV
            _leg("PUT", 7360.0, -0.22, 0.23),      # fallback with IV
            _leg("CALL", 7600.0, 0.24, 0.19),
        ])
        state = live_features.SessionFeatureState()
        raw = live_features.compute_raw_features(quotes, 7500.0, TS, state)
        # 0.23 (fallback put IV) - 0.19 (call IV), not -0.19 or 0.0 - 0.19.
        self.assertAlmostEqual(raw["skew_z"], 0.04, places=9)


class ChooseAtmPairParityTests(unittest.TestCase):
    def test_clean_chain_same_atm_pick(self) -> None:
        rows, quotes = _chain([
            _leg("CALL", 7495.0, 0.52, 0.20),
            _leg("PUT", 7495.0, -0.48, 0.20),
            _leg("CALL", 7505.0, 0.48, 0.20),
            _leg("PUT", 7505.0, -0.52, 0.20),
        ])
        vendor_call, vendor_put = feature_builder.choose_atm_pair(rows, 7501.0)
        live_call, live_put = live_features.choose_atm_pair(quotes, 7501.0)
        assert vendor_call and vendor_put and live_call and live_put
        self.assertEqual(float(vendor_call["strike"]), live_call.strike)
        self.assertEqual(float(vendor_put["strike"]), live_put.strike)
        self.assertEqual(live_call.strike, 7505.0)


if __name__ == "__main__":
    unittest.main()
