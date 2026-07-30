"""Tests for automatic compression of completed IB session logs."""
from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from log_maintenance import compress_completed_ib_logs  # noqa: E402


class LogMaintenanceTests(unittest.TestCase):
    def test_compresses_prior_day_but_never_active_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            prior = live_dir / "2026-07-29"
            active = live_dir / "2026-07-30"
            prior.mkdir()
            active.mkdir()
            (prior / "ib.log").write_text("prior log\n" * 20, encoding="utf-8")
            (active / "ib.log").write_text("active log\n" * 20, encoding="utf-8")

            result = compress_completed_ib_logs(
                live_dir=live_dir,
                active_date="2026-07-30",
            )

            self.assertEqual(result.compressed, 1)
            self.assertFalse((prior / "ib.log").exists())
            self.assertTrue((prior / "ib.log.gz").exists())
            with gzip.open(prior / "ib.log.gz", "rt", encoding="utf-8") as handle:
                self.assertIn("prior log", handle.read())
            self.assertTrue((active / "ib.log").exists())
            self.assertFalse((active / "ib.log.gz").exists())

    def test_existing_archive_is_left_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live_dir = Path(tmp)
            prior = live_dir / "2026-07-29"
            prior.mkdir()
            (prior / "ib.log").write_text("source\n", encoding="utf-8")
            (prior / "ib.log.gz").write_bytes(b"existing")

            result = compress_completed_ib_logs(
                live_dir=live_dir,
                active_date="2026-07-30",
            )

            self.assertEqual(result.skipped, 1)
            self.assertTrue((prior / "ib.log").exists())
            self.assertEqual((prior / "ib.log.gz").read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
