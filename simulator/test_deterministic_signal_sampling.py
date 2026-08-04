"""Deterministic live minute sampling must be independent of poll frequency."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from live_features import DeterministicMinuteSampler  # noqa: E402
from mbh_simulator import OptionQuote  # noqa: E402


def _quotes(ts: datetime, spot: float, premium: float):
    return [
        OptionQuote(ts, "2026-08-03", "CALL", 7500.0, premium, premium + 0.1, 0.50, 0.20, spot),
        OptionQuote(ts, "2026-08-03", "PUT", 7500.0, premium + 0.2, premium + 0.3, -0.50, 0.21, spot),
    ]


class DeterministicMinuteSamplerTests(unittest.TestCase):
    def test_aggregates_window_with_medians_at_fixed_offset(self) -> None:
        sampler = DeterministicMinuteSampler(
            sample_offset_seconds=1.0,
            sample_window_seconds=1.0,
            min_observations=2,
            max_wait_seconds=1.0,
        )
        minute = datetime(2026, 8, 3, 10, 2)
        sampler.observe(minute.replace(microsecond=200_000), 7500.0, _quotes(minute, 7500.0, 3.0))
        sampler.observe(minute.replace(microsecond=800_000), 7502.0, _quotes(minute, 7502.0, 3.2))

        self.assertEqual(sampler.status(minute.replace(microsecond=900_000)), "collecting")
        self.assertEqual(sampler.status(minute.replace(second=1)), "ready")
        sample = sampler.aggregate(minute.replace(second=1))
        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(sample.observation_count, 2)
        self.assertEqual(sample.spot, 7501.0)
        call = next(q for q in sample.quotes if q.option_type == "CALL")
        self.assertAlmostEqual(call.bid, 3.1)

    def test_single_observation_waits_then_falls_back_at_deadline(self) -> None:
        sampler = DeterministicMinuteSampler(
            sample_offset_seconds=1.0,
            sample_window_seconds=1.0,
            min_observations=2,
            max_wait_seconds=1.0,
        )
        minute = datetime(2026, 8, 3, 10, 2)
        sampler.observe(minute.replace(microsecond=500_000), 7500.0, _quotes(minute, 7500.0, 3.0))
        self.assertEqual(sampler.status(minute.replace(second=1, microsecond=500_000)), "collecting")
        self.assertEqual(sampler.status(minute.replace(second=2)), "ready")

    def test_no_observations_fails_closed_after_deadline(self) -> None:
        sampler = DeterministicMinuteSampler(
            sample_offset_seconds=1.0,
            sample_window_seconds=1.0,
            min_observations=2,
            max_wait_seconds=1.0,
        )
        minute = datetime(2026, 8, 3, 10, 2)
        self.assertEqual(sampler.status(minute.replace(second=2)), "unavailable")
        self.assertIsNone(sampler.aggregate(minute.replace(second=2)))


if __name__ == "__main__":
    unittest.main()
