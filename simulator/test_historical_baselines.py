"""Baseline statistics must never produce degenerate (1e-9-std) minutes.

A per-minute std of exactly zero arises structurally: the first session bar
has trend_score/straddle_residual_z identically 0.0 on every training day,
and realized_vs_implied_z is 0.0 until six spot observations exist. Before
the STD_FLOOR_FRAC floor, those minutes carried std=1e-9 and any nonzero
live raw value z-scored to ~1e6 (2026-08-04: realized_vs_implied_z=-1.29M),
killing the tranche via the sanity gate.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from historical_baselines import (  # noqa: E402
    FEATURES,
    STD_FLOOR_FRAC,
    compute_baselines,
    transform_rows,
    write_csv,
    zscore,
)
from live_features import validate_baselines_freshness  # noqa: E402


def _write_signals(processed: Path, symbol: str, date: str, rows: list[dict]) -> None:
    day_dir = processed / f"symbol={symbol}" / f"date={date}"
    day_dir.mkdir(parents=True, exist_ok=True)
    write_csv(day_dir / "signals.csv", rows)


def _row(date: str, hhmm: str, **features: float) -> dict:
    row = {"timestamp": f"{date}T{hhmm}:00"}
    for feature in FEATURES:
        row[feature] = features.get(feature, 0.0)
    return row


class ComputeBaselinesTests(unittest.TestCase):
    def _baselines_from(self, per_date_rows: dict[str, list[dict]]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            processed = Path(tmp)
            for date, rows in per_date_rows.items():
                _write_signals(processed, "SPXW", date, rows)
            return compute_baselines(processed, "SPXW", sorted(per_date_rows))

    def test_constant_minute_gets_floored_std_not_1e9(self) -> None:
        """A minute that is identically zero on every train day must not
        produce a divide-by-~zero z-scale."""
        dates = ["2026-08-03", "2026-08-04", "2026-08-05"]
        rows = {
            d: [
                # 09:31 constant across days; 09:32 has real variance.
                _row(d, "09:31", trend_score=0.0),
                _row(d, "09:32", trend_score=0.1 * (i + 1)),
            ]
            for i, d in enumerate(dates)
        }
        baselines = self._baselines_from(rows)

        global_std = baselines["global"]["trend_score"]["std"]
        floored = baselines["minutes"]["09:31"]["trend_score"]["std"]
        self.assertAlmostEqual(floored, STD_FLOOR_FRAC * global_std, places=12)
        self.assertGreater(floored, 1e-6)

        # A modest live raw value now z-scores to something finite and sane.
        z = zscore(0.05, baselines["minutes"]["09:31"]["trend_score"])
        self.assertLess(abs(z), 100.0)

    def test_healthy_minute_std_unchanged_by_floor(self) -> None:
        """The floor must not bind when the minute has real variance."""
        dates = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"]
        rows = {
            d: [_row(d, "10:00", skew_z=0.02 + 0.01 * i)]
            for i, d in enumerate(dates)
        }
        baselines = self._baselines_from(rows)
        minute = baselines["minutes"]["10:00"]["skew_z"]
        global_stats = baselines["global"]["skew_z"]
        # Minute pool == global pool here, so stds match exactly and the
        # 10% floor cannot have replaced the real pstdev.
        self.assertAlmostEqual(minute["std"], global_stats["std"], places=12)
        self.assertGreater(minute["std"], STD_FLOOR_FRAC * global_stats["std"])

    def test_single_observation_minute_falls_back_to_global_std(self) -> None:
        rows = {
            "2026-08-03": [
                _row("2026-08-03", "09:31", skew_z=0.02),
                _row("2026-08-03", "09:32", skew_z=0.03),
            ]
        }
        baselines = self._baselines_from(rows)
        self.assertEqual(
            baselines["minutes"]["09:31"]["skew_z"]["std"],
            baselines["global"]["skew_z"]["std"],
        )

    def test_payload_shape(self) -> None:
        baselines = self._baselines_from(
            {"2026-08-03": [_row("2026-08-03", "09:31", skew_z=0.02)]}
        )
        self.assertEqual(set(baselines), {"minutes", "global", "features"})
        self.assertEqual(baselines["features"], FEATURES)
        for feature in FEATURES:
            self.assertIn(feature, baselines["global"])


class TransformRowsTests(unittest.TestCase):
    def test_unknown_minute_falls_back_to_global(self) -> None:
        baselines = {
            "minutes": {},
            "global": {f: {"mean": 0.0, "std": 2.0, "count": 10} for f in FEATURES},
            "features": FEATURES,
        }
        rows = [{"timestamp": "2026-08-03T11:00:00", "skew_z": 1.0}]
        out = transform_rows(rows, baselines)[0]
        self.assertAlmostEqual(out["skew_z"], 0.5)
        self.assertAlmostEqual(out["raw_skew_z"], 1.0)

    def test_zscore_guards_zero_std(self) -> None:
        z = zscore(1.0, {"mean": 0.0, "std": 0.0})
        self.assertTrue(abs(z) < 1e12 or z == 1e9)  # guarded, not inf/nan
        self.assertEqual(z, 1.0 / 1e-9)


class ValidateBaselinesFreshnessTests(unittest.TestCase):
    """A malformed payload must die at startup, not as a mid-session KeyError."""

    @staticmethod
    def _payload(**overrides) -> dict:
        from datetime import datetime

        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "train_dates": ["2026-08-06"],
            "train_count": 1,
            "minutes": {},
            "global": {
                f: {"mean": 0.0, "std": 1.0, "count": 10} for f in FEATURES
            },
            "features": FEATURES,
        }
        payload.update(overrides)
        return payload

    def test_valid_payload_passes(self) -> None:
        validate_baselines_freshness(self._payload(), max_age_days=3)

    def test_missing_generated_at_raises(self) -> None:
        payload = self._payload()
        del payload["generated_at"]
        with self.assertRaisesRegex(ValueError, "generated_at"):
            validate_baselines_freshness(payload, max_age_days=3)

    def test_stale_payload_raises(self) -> None:
        payload = self._payload(generated_at="2026-01-01T09:00:00")
        with self.assertRaisesRegex(ValueError, "days old"):
            validate_baselines_freshness(payload, max_age_days=3)

    def test_missing_global_raises(self) -> None:
        payload = self._payload()
        del payload["global"]
        with self.assertRaisesRegex(ValueError, "global"):
            validate_baselines_freshness(payload, max_age_days=3)

    def test_partial_global_raises(self) -> None:
        payload = self._payload()
        del payload["global"]["skew_z"]
        with self.assertRaisesRegex(ValueError, "skew_z"):
            validate_baselines_freshness(payload, max_age_days=3)

    def test_missing_minutes_raises(self) -> None:
        payload = self._payload()
        del payload["minutes"]
        with self.assertRaisesRegex(ValueError, "minutes"):
            validate_baselines_freshness(payload, max_age_days=3)


if __name__ == "__main__":
    unittest.main()
