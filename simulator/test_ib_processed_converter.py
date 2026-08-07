"""IB recording -> processed day: schema parity and live-code-path fidelity.

The converter's whole guarantee is that signals.csv equals what the LIVE
feature path would compute from the recorded minutes — these tests replay the
same quotes directly through compute_raw_features_once_per_minute and demand
exact equality against the CSV.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from chain_recorder import ChainMinuteRecorder  # noqa: E402
from historical_baselines import compute_baselines  # noqa: E402
from live_features import (  # noqa: E402
    MinuteFeatureSample,
    SessionFeatureState,
    compute_raw_features_once_per_minute,
)
from mbh_simulator import OptionQuote, read_quotes_csv, read_signals_csv  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "build_processed_from_ib", ROOT / "scripts" / "build_processed_from_ib.py",
)
converter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(converter)

DAY = "2026-08-06"  # real trading day so the VIX calendar has an entry
EXPIRY = DAY
NEXT_EXPIRY = "2026-08-07"


def _chain(ts: datetime, spot: float, bump: float = 0.0):
    s = round(spot / 5) * 5
    return [
        OptionQuote(ts, EXPIRY, "CALL", s, 17.0 + bump, 17.6 + bump, 0.51, 0.140, spot),
        OptionQuote(ts, EXPIRY, "PUT", s, 16.8 + bump, 17.4 + bump, -0.49, 0.141, spot),
        OptionQuote(ts, EXPIRY, "CALL", s + 80, 2.1, 2.3, 0.25, 0.128, spot),
        OptionQuote(ts, EXPIRY, "PUT", s - 90, 2.4, 2.6, -0.25, 0.152, spot),
        OptionQuote(ts, EXPIRY, "CALL", s + 150, 0.5, 0.7, 0.10, 0.135, spot),
        OptionQuote(ts, EXPIRY, "PUT", s - 200, 0.6, 0.8, -0.10, 0.170, spot),
    ]


def _next_chain(ts: datetime, spot: float):
    s = round(spot / 5) * 5
    return [
        OptionQuote(ts, NEXT_EXPIRY, "CALL", s, 30.0, 30.8, 0.51, 0.145, spot),
        OptionQuote(ts, NEXT_EXPIRY, "PUT", s, 29.5, 30.3, -0.49, 0.146, spot),
    ]


def _record_synthetic_day(live_dir: Path) -> list[tuple[datetime, float, bool]]:
    """Write a 5-minute recording; returns (ts, spot, has_next) per minute."""
    rec = ChainMinuteRecorder(live_dir / DAY / "chain_minutes.jsonl")
    plan = [
        (datetime(2026, 8, 6, 9, 31), 7724.9, False),
        (datetime(2026, 8, 6, 9, 32), 7727.4, True),   # tranche minute
        (datetime(2026, 8, 6, 9, 33), 7722.1, False),
        (datetime(2026, 8, 6, 9, 34), 7730.6, False),
        (datetime(2026, 8, 6, 9, 35), 7735.2, True),
    ]
    for i, (ts, spot, has_next) in enumerate(plan):
        sample = MinuteFeatureSample(ts, spot, _chain(ts, spot, bump=0.1 * i), 5)
        rec.record_sample(sample)
        if has_next:
            rec.record_next_expiry(sample, _next_chain(ts, spot))
    return plan


def _expected_signals(plan) -> list[dict]:
    state = SessionFeatureState()
    rows = []
    for i, (ts, spot, has_next) in enumerate(plan):
        quotes = _chain(ts, spot, bump=0.1 * i)
        nq = _next_chain(ts, spot) if has_next else None
        rows.append(dict(compute_raw_features_once_per_minute(
            quotes, spot, ts, state, next_expiry_quotes=nq,
        )))
    return rows


class ConverterTests(unittest.TestCase):
    def _convert(self, tmp: Path) -> Path:
        live_dir = tmp / "live"
        processed = tmp / "processed"
        plan = _record_synthetic_day(live_dir)
        self.plan = plan
        converter.convert_day(
            DAY, live_dir=live_dir, processed_dir=processed, symbol="SPXW",
        )
        return processed / "symbol=SPXW" / f"date={DAY}"

    def test_signals_match_live_feature_path_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day_dir = self._convert(Path(tmp))
            with (day_dir / "signals.csv").open(encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            expected = _expected_signals(self.plan)
            self.assertEqual(len(rows), len(expected))
            for got, want in zip(rows, expected):
                for feature, value in want.items():
                    self.assertAlmostEqual(
                        float(got[feature]), float(value), places=12,
                        msg=f"{feature} @ {got['timestamp']}",
                    )
            # term_ratio only at the recorded tranche minutes.
            terms = [float(r["term_ratio_z"]) for r in rows]
            self.assertNotEqual(terms[1], 0.0)
            self.assertEqual(terms[0], 0.0)

    def test_vendor_schema_and_readers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            day_dir = self._convert(Path(tmp))
            with (day_dir / "signals.csv").open(encoding="utf-8-sig") as f:
                header = next(csv.reader(f))
            for col in (
                "timestamp", "straddle", "linear_decay_baseline",
                "straddle_residual_z", "skew_z", "term_ratio_z", "trend_score",
                "realized_vs_implied_z", "vix", "underlying_price",
                "minutes_to_close_norm", "abs_skew_z", "abs_term_ratio_z",
                "overnight_gap_z", "prior_day_return_z",
                "vix_open", "vix_close", "vix_prior_close",
            ):
                self.assertIn(col, header)

            signals = read_signals_csv(day_dir / "signals.csv")
            self.assertEqual(len(signals), 5)
            self.assertGreater(signals[0].vix or 0.0, 0.0)  # real VIX for 2026-08-06

            quotes = read_quotes_csv(day_dir / "normalized_option_quotes.csv")
            expiries = {q.expiry for q in quotes}
            self.assertEqual(expiries, {EXPIRY, NEXT_EXPIRY})

            source = json.loads((day_dir / "source.json").read_text(encoding="utf-8"))
            self.assertEqual(source["source"], "ib_live")

            # compute_baselines consumes the day without complaint.
            baselines = compute_baselines(day_dir.parents[1], "SPXW", [DAY])
            self.assertIn("skew_z", baselines["global"])

    def test_refuses_to_overwrite_vendor_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            live_dir = tmp / "live"
            processed = tmp / "processed"
            _record_synthetic_day(live_dir)
            vendor_day = processed / "symbol=SPXW" / f"date={DAY}"
            vendor_day.mkdir(parents=True)
            (vendor_day / "signals.csv").write_text("timestamp\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                converter.convert_day(
                    DAY, live_dir=live_dir, processed_dir=processed, symbol="SPXW",
                )
            converter.convert_day(  # explicit --force path works
                DAY, live_dir=live_dir, processed_dir=processed, symbol="SPXW", force=True,
            )
            self.assertTrue((vendor_day / "source.json").exists())

    def test_auto_discovery_targets_only_missing_days(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            live_dir = tmp / "live"
            processed = tmp / "processed"
            _record_synthetic_day(live_dir)
            self.assertEqual(
                converter.discover_auto_days(live_dir, processed, "SPXW"), [DAY],
            )
            converter.convert_day(
                DAY, live_dir=live_dir, processed_dir=processed, symbol="SPXW",
            )
            self.assertEqual(converter.discover_auto_days(live_dir, processed, "SPXW"), [])


if __name__ == "__main__":
    unittest.main()
