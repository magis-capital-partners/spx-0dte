"""Chain recorder: crash-safe minute capture that can never hurt the executor."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from chain_recorder import ChainMinuteRecorder, load_chain_minutes  # noqa: E402
from live_features import MinuteFeatureSample  # noqa: E402
from mbh_simulator import OptionQuote  # noqa: E402


def _sample(minute: datetime, spot: float = 7500.0) -> MinuteFeatureSample:
    quotes = [
        OptionQuote(minute, "2026-08-10", "CALL", 7500.0, 10.0, 10.4, 0.51, 0.14, spot),
        OptionQuote(minute, "2026-08-10", "PUT", 7500.0, 9.8, 10.2, -0.49, 0.14, spot),
        OptionQuote(minute, "2026-08-10", "PUT", 7420.0, 2.0, 2.2, -0.25, 0.16, spot),
    ]
    return MinuteFeatureSample(minute, spot, quotes, 5)


def _next_quotes(minute: datetime):
    return [
        OptionQuote(minute, "2026-08-11", "CALL", 7500.0, 24.0, 24.8, 0.51, 0.15, 7500.0),
        OptionQuote(minute, "2026-08-11", "PUT", 7500.0, 23.5, 24.3, -0.49, 0.15, 7500.0),
    ]


class ChainRecorderTests(unittest.TestCase):
    def test_records_once_per_minute_and_upgrades_with_next_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chain_minutes.jsonl"
            rec = ChainMinuteRecorder(path)
            m1 = datetime(2026, 8, 10, 9, 31)
            rec.record_sample(_sample(m1))
            rec.record_sample(_sample(m1))  # duplicate poll, same minute
            m2 = datetime(2026, 8, 10, 9, 32)
            rec.record_sample(_sample(m2, spot=7501.0))
            rec.record_next_expiry(_sample(m2, spot=7501.0), _next_quotes(m2))
            rec.record_next_expiry(_sample(m2, spot=7501.0), _next_quotes(m2))  # dedupe

            lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(lines), 3)  # m1, m2 base, m2 upgrade

            merged = load_chain_minutes(path)
            self.assertEqual(set(merged), {"2026-08-10T09:31", "2026-08-10T09:32"})
            self.assertNotIn("next_quotes", merged["2026-08-10T09:31"])
            self.assertEqual(len(merged["2026-08-10T09:32"]["next_quotes"]), 2)
            # Quote rows round-trip with expiry/type/strike and null-safe greeks.
            expiry, opt_type, strike, bid, ask, delta, iv = merged["2026-08-10T09:31"]["quotes"][0]
            self.assertEqual((expiry, opt_type, strike), ("2026-08-10", "CALL", 7500.0))
            self.assertEqual((bid, ask), (10.0, 10.4))

    def test_none_greeks_serialize_as_null(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chain_minutes.jsonl"
            rec = ChainMinuteRecorder(path)
            m = datetime(2026, 8, 10, 9, 31)
            q = OptionQuote(m, "2026-08-10", "CALL", 7600.0, 1.0, 1.2, None, None, 7500.0)
            rec.record_sample(MinuteFeatureSample(m, 7500.0, [q], 3))
            merged = load_chain_minutes(path)
            row = merged["2026-08-10T09:31"]["quotes"][0]
            self.assertIsNone(row[5])
            self.assertIsNone(row[6])

    def test_write_failure_is_swallowed(self) -> None:
        """An unwritable path must never raise into the trading loop."""
        bad = Path("Z:/definitely/not/a/real/drive/chain.jsonl")
        if bad.parent.exists():  # pragma: no cover — environment-specific
            self.skipTest("unexpected drive present")
        rec = ChainMinuteRecorder(bad)
        rec.record_sample(_sample(datetime(2026, 8, 10, 9, 31)))  # must not raise
        rec.record_next_expiry(
            _sample(datetime(2026, 8, 10, 9, 31)), _next_quotes(datetime(2026, 8, 10, 9, 31))
        )
        self.assertTrue(rec._failed_once)

    def test_restart_duplicate_minutes_resolve_to_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chain_minutes.jsonl"
            m = datetime(2026, 8, 10, 10, 0)
            first = ChainMinuteRecorder(path)
            first.record_sample(_sample(m, spot=7500.0))
            first.record_next_expiry(_sample(m, spot=7500.0), _next_quotes(m))
            # Restarted executor re-records the same minute without next-expiry:
            second = ChainMinuteRecorder(path)
            second.record_sample(_sample(m, spot=7502.0))
            merged = load_chain_minutes(path)
            # The upgraded (next_quotes-bearing) row wins over the bare re-record.
            self.assertIn("next_quotes", merged["2026-08-10T10:00"])

    def test_torn_final_line_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chain_minutes.jsonl"
            rec = ChainMinuteRecorder(path)
            rec.record_sample(_sample(datetime(2026, 8, 10, 9, 31)))
            with path.open("a", encoding="utf-8") as h:
                h.write('{"minute":"2026-08-10T09:32","spot":75')  # crash mid-write
            merged = load_chain_minutes(path)
            self.assertEqual(list(merged), ["2026-08-10T09:31"])


if __name__ == "__main__":
    unittest.main()
