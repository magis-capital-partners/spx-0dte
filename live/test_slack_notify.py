"""Tests for Slack notify: no-op without webhook, and never blocking the loop."""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

import slack_notify  # noqa: E402
from slack_notify import (  # noqa: E402
    flush,
    maybe_notify_safety_event,
    notify_slack,
    notify_slack_async,
)


class SlackNotifyTests(unittest.TestCase):
    def test_noop_without_url(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(notify_slack("hello", enabled=True, webhook_url=""))

    def test_disabled(self) -> None:
        self.assertFalse(notify_slack("hello", enabled=False, webhook_url="http://x"))

    def test_only_safety_events(self) -> None:
        """Non-safety events never page; safety events are queued for delivery."""
        with patch("slack_notify.notify_slack", return_value=True) as mocked:
            self.assertFalse(maybe_notify_safety_event("entry", {"event": "entry"}))
            mocked.assert_not_called()
            with patch.dict(
                "os.environ", {"SPX_SLACK_WEBHOOK_URL": "http://x"}, clear=True,
            ):
                self.assertTrue(
                    maybe_notify_safety_event(
                        "halt_entries", {"event": "halt_entries", "x": 1},
                    )
                )
            self.assertTrue(flush(timeout_sec=5.0))
            mocked.assert_called_once()

    def test_async_delivery_does_not_block_caller(self) -> None:
        """The hot path must return long before a slow webhook completes."""
        started = threading.Event()
        release = threading.Event()

        def _slow(*_a, **_k):
            started.set()
            release.wait(timeout=5.0)
            return True

        with patch("slack_notify.notify_slack", side_effect=_slow):
            with patch.dict(
                "os.environ", {"SPX_SLACK_WEBHOOK_URL": "http://x"}, clear=True,
            ):
                t0 = time.monotonic()
                self.assertTrue(notify_slack_async("hello"))
                elapsed = time.monotonic() - t0
            # Enqueue is bounded by a lock + put_nowait, not by the POST.
            self.assertLess(elapsed, 0.25)
            self.assertTrue(started.wait(timeout=5.0))
            release.set()
            self.assertTrue(flush(timeout_sec=5.0))

    def test_async_noop_without_url_does_not_queue(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(notify_slack_async("hello"))

    def test_full_queue_drops_instead_of_blocking(self) -> None:
        """A Slack outage must never grow memory or stall the executor."""
        release = threading.Event()

        def _blocked(*_a, **_k):
            release.wait(timeout=10.0)
            return True

        before = slack_notify.dropped_count()
        with patch("slack_notify.notify_slack", side_effect=_blocked):
            with patch.dict(
                "os.environ", {"SPX_SLACK_WEBHOOK_URL": "http://x"}, clear=True,
            ):
                # Overfill past _QUEUE_MAX; excess must be dropped, not block.
                for _ in range(slack_notify._QUEUE_MAX + 20):
                    notify_slack_async("spam")
                t0 = time.monotonic()
                notify_slack_async("one more")
                self.assertLess(time.monotonic() - t0, 0.25)
            self.assertGreater(slack_notify.dropped_count(), before)
            release.set()
            flush(timeout_sec=10.0)


if __name__ == "__main__":
    unittest.main()
