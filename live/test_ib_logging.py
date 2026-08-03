"""Tests for bounded normal and diagnostic IB library logging."""
from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

import ib_executor  # noqa: E402
from live_config import LiveConfig  # noqa: E402


class IBLoggingTests(unittest.TestCase):
    @staticmethod
    def _close_handlers() -> None:
        logger = logging.getLogger("ib_insync")
        for handler in logger.handlers:
            handler.close()
        logger.handlers.clear()

    def test_normal_mode_filters_debug_wire_messages_and_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(ib_executor, "LIVE_DIR", Path(tmp)):
            config = replace(LiveConfig(), ib_log_max_bytes=100, ib_log_backup_count=2)
            path = ib_executor.setup_ib_logging("2026-08-03", config)
            logger = logging.getLogger("ib_insync")
            logger.debug("wire-tick-should-not-be-written")
            for index in range(20):
                logger.info("order-state-%d %s", index, "x" * 40)
            for handler in logger.handlers:
                handler.flush()

            self.assertFalse("wire-tick-should-not-be-written" in path.read_text(encoding="utf-8"))
            self.assertLessEqual(path.stat().st_size, 200)
            self.assertTrue((path.with_name("ib.log.1")).exists())
            self.assertLessEqual(len(list(path.parent.glob("ib.log*"))), 3)
            self._close_handlers()

    def test_explicit_diagnostic_mode_keeps_debug_but_stays_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(ib_executor, "LIVE_DIR", Path(tmp)):
            config = replace(
                LiveConfig(),
                ib_wire_debug_capture=True,
                ib_log_max_bytes=100,
                ib_log_backup_count=1,
            )
            path = ib_executor.setup_ib_logging("2026-08-03", config)
            logger = logging.getLogger("ib_insync")
            logger.debug("wire-diagnostic-message")
            for handler in logger.handlers:
                handler.flush()

            self.assertIn("wire-diagnostic-message", path.read_text(encoding="utf-8"))
            self.assertEqual(logger.level, logging.DEBUG)
            self._close_handlers()


if __name__ == "__main__":
    unittest.main()
