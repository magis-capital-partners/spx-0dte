"""Baseline windows must never mix vendor and IB-recorded days."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from data_sources import (  # noqa: E402
    IB_SOURCE,
    VENDOR_SOURCE,
    day_source,
    resolve_homogeneous_train_dates,
)


def _mk_days(processed: Path, dates: list[str], source: str | None) -> None:
    for d in dates:
        day_dir = processed / "symbol=SPXW" / f"date={d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "signals.csv").write_text("timestamp\n", encoding="utf-8")
        if source is not None:
            (day_dir / "source.json").write_text(
                json.dumps({"source": source}), encoding="utf-8"
            )


def _dates(prefix: str, n: int, start: int = 1) -> list[str]:
    return [f"{prefix}-{i:02d}" for i in range(start, start + n)]


class DataSourcesTests(unittest.TestCase):
    def test_untagged_day_is_vendor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            processed = Path(tmp)
            _mk_days(processed, ["2026-08-01"], None)
            _mk_days(processed, ["2026-08-02"], IB_SOURCE)
            self.assertEqual(day_source(processed, "SPXW", "2026-08-01"), VENDOR_SOURCE)
            self.assertEqual(day_source(processed, "SPXW", "2026-08-02"), IB_SOURCE)

    def test_vendor_only_returns_normal_rolling_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            processed = Path(tmp)
            days = _dates("2026-06", 45)
            _mk_days(processed, days, None)
            train, source, note = resolve_homogeneous_train_dates(processed, "SPXW", days, 40)
            self.assertEqual(train, days[-40:])
            self.assertEqual(source, VENDOR_SOURCE)
            self.assertEqual(note, "")

    def test_transition_uses_frozen_vendor_window_with_countdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            processed = Path(tmp)
            vendor = _dates("2026-06", 45)
            ib = _dates("2026-08", 5)
            _mk_days(processed, vendor, None)
            _mk_days(processed, ib, IB_SOURCE)
            train, source, note = resolve_homogeneous_train_dates(
                processed, "SPXW", vendor + ib, 40,
            )
            self.assertEqual(train, vendor[-40:])
            self.assertEqual(source, VENDOR_SOURCE)
            self.assertIn("CUTOVER PENDING", note)
            self.assertIn("5/40", note)
            self.assertIn("35 more", note)
            # Never a mixed window.
            self.assertTrue(all(d in vendor for d in train))

    def test_auto_cutover_once_ib_window_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            processed = Path(tmp)
            vendor = _dates("2026-06", 45)
            ib = _dates("2026-08", 41)
            _mk_days(processed, vendor, None)
            _mk_days(processed, ib, IB_SOURCE)
            train, source, note = resolve_homogeneous_train_dates(
                processed, "SPXW", vendor + ib, 40,
            )
            self.assertEqual(train, ib[-40:])
            self.assertEqual(source, IB_SOURCE)
            self.assertEqual(note, "")

    def test_no_full_window_anywhere_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            processed = Path(tmp)
            vendor = _dates("2026-06", 10)
            ib = _dates("2026-08", 5)
            _mk_days(processed, vendor, None)
            _mk_days(processed, ib, IB_SOURCE)
            with self.assertRaises(SystemExit):
                resolve_homogeneous_train_dates(processed, "SPXW", vendor + ib, 40)

    def test_too_few_dates_total_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                resolve_homogeneous_train_dates(Path(tmp), "SPXW", ["2026-08-01"], 40)


if __name__ == "__main__":
    unittest.main()
