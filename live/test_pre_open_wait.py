"""2026-08-05: a 09:11 launch aborted instead of waiting for the 09:30 open."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

import ib_executor  # noqa: E402
from live_config import LiveConfig  # noqa: E402


class _RecordingIB:
    """Stand-in for ib_insync's event-loop-pumping sleep."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class PreOpenWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(ib_executor, "log_event")
        self.log_event = patcher.start()
        self.addCleanup(patcher.stop)

    def test_waits_through_ib_sleep_so_the_socket_stays_serviced(self) -> None:
        ib = _RecordingIB()
        soon = (datetime.now() + timedelta(seconds=1)).time()
        with mock.patch.object(ib_executor, "SESSION_OPEN", soon):
            ib_executor.wait_for_market_open(
                LiveConfig(wait_for_market_open=True, market_data_lead_seconds=0.0),
                "2026-08-05",
                ib=ib,
            )
        self.assertTrue(ib.sleeps, "expected the wait to idle via ib.sleep")
        self.assertLessEqual(max(ib.sleeps), 30.0, "wait must stay chunked")
        self.assertEqual(self.log_event.call_count, 1)
        self.assertEqual(
            self.log_event.call_args.args[1]["event"], "pre_open_wait"
        )

    def test_lead_seconds_shorten_the_wait(self) -> None:
        ib = _RecordingIB()
        soon = (datetime.now() + timedelta(seconds=30)).time()
        with mock.patch.object(ib_executor, "SESSION_OPEN", soon):
            ib_executor.wait_for_market_open(
                LiveConfig(wait_for_market_open=True, market_data_lead_seconds=3600.0),
                "2026-08-05",
                ib=ib,
            )
        self.assertEqual(ib.sleeps, [])
        self.assertEqual(self.log_event.call_count, 0)

    def test_disabled_switch_starts_immediately(self) -> None:
        ib = _RecordingIB()
        soon = (datetime.now() + timedelta(seconds=30)).time()
        with mock.patch.object(ib_executor, "SESSION_OPEN", soon):
            ib_executor.wait_for_market_open(
                LiveConfig(wait_for_market_open=False, market_data_lead_seconds=0.0),
                "2026-08-05",
                ib=ib,
            )
        self.assertEqual(ib.sleeps, [])
        self.assertEqual(self.log_event.call_count, 0)

    def test_mid_session_restart_is_not_delayed(self) -> None:
        ib = _RecordingIB()
        past = (datetime.now() - timedelta(minutes=5)).time()
        with mock.patch.object(ib_executor, "SESSION_OPEN", past):
            ib_executor.wait_for_market_open(
                LiveConfig(wait_for_market_open=True, market_data_lead_seconds=0.0),
                "2026-08-05",
                ib=ib,
            )
        self.assertEqual(ib.sleeps, [])
        self.assertEqual(self.log_event.call_count, 0)


if __name__ == "__main__":
    unittest.main()
