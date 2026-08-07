"""Tranche forensics: raw features + skew legs + baseline stats ride each row.

Entry rejections and side-selection questions are triaged from tranches.jsonl.
Before this telemetry the rows carried only z-scores, so a live-vs-backtest
parity investigation could not recover which strikes/IVs produced a z — the
Aug 2026 skew_z calibration investigation had to reconstruct everything from
vendor data alone.
"""
from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from ib_executor import IBSignalProvider  # noqa: E402
from live_features import FEATURES, SessionFeatureState  # noqa: E402
from mbh_simulator import TrancheSummary  # noqa: E402


def _provider(state: SessionFeatureState, baselines: dict | None) -> IBSignalProvider:
    provider = object.__new__(IBSignalProvider)
    provider._feature_state = state
    provider.baselines = baselines
    return provider


def _state() -> SessionFeatureState:
    state = SessionFeatureState()
    state.last_sample_minute = "2026-08-07T09:47"
    state.last_raw_features = {
        "straddle_residual_z": -0.115,
        "skew_z": 0.017,
        "term_ratio_z": -0.42,
        "trend_score": 0.049,
        "realized_vs_implied_z": -0.0026,
    }
    state.last_raw_components = {
        "put25_strike": 7380.0,
        "put25_iv": 0.212,
        "call25_strike": 7600.0,
        "call25_iv": 0.189,
        "atm_straddle": 34.5,
        "atm_iv": 0.2005,
        "spot": 7501.25,
    }
    return state


def _baselines() -> dict:
    return {
        "minutes": {
            "09:47": {f: {"mean": 0.01, "std": 0.02} for f in FEATURES},
        },
        "global": {f: {"mean": 0.0, "std": 1.0} for f in FEATURES},
        "features": FEATURES,
    }


class SignalForensicsTests(unittest.TestCase):
    def test_forensics_shape(self) -> None:
        out = _provider(_state(), _baselines()).signal_forensics()
        self.assertEqual(out["sample_minute"], "2026-08-07T09:47")
        self.assertEqual(set(out["raw_features"]), set(FEATURES))
        self.assertAlmostEqual(out["raw_features"]["skew_z"], 0.017)
        self.assertEqual(out["raw_components"]["put25_strike"], 7380.0)
        self.assertEqual(out["raw_components"]["call25_iv"], 0.189)
        # Baseline stats resolve via the minute key of the canonical sample.
        self.assertEqual(set(out["baseline_stats"]), set(FEATURES))
        self.assertAlmostEqual(out["baseline_stats"]["skew_z"]["std"], 0.02)

    def test_minute_fallback_to_global_stats(self) -> None:
        state = _state()
        state.last_sample_minute = "2026-08-07T11:03"  # not in minutes map
        out = _provider(state, _baselines()).signal_forensics()
        self.assertAlmostEqual(out["baseline_stats"]["skew_z"]["std"], 1.0)

    def test_empty_state_yields_no_keys(self) -> None:
        self.assertEqual(_provider(SessionFeatureState(), _baselines()).signal_forensics(), {})

    def test_no_baselines_omits_stats_but_keeps_raw(self) -> None:
        out = _provider(_state(), None).signal_forensics()
        self.assertIn("raw_features", out)
        self.assertNotIn("baseline_stats", out)

    def test_forensics_keys_do_not_collide_with_tranche_summary(self) -> None:
        """tranche_row.update(forensics) must never overwrite a summary field."""
        summary_fields = {f.name for f in dataclasses.fields(TrancheSummary)}
        forensics_keys = set(_provider(_state(), _baselines()).signal_forensics())
        overlap = summary_fields & forensics_keys
        self.assertEqual(overlap, set(), f"forensics keys shadow TrancheSummary: {overlap}")


if __name__ == "__main__":
    unittest.main()
