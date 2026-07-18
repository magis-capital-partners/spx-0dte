"""Tests for Slack notify (no-op without webhook)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from slack_notify import maybe_notify_safety_event, notify_slack  # noqa: E402


class SlackNotifyTests(unittest.TestCase):
    def test_noop_without_url(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(notify_slack("hello", enabled=True, webhook_url=""))

    def test_disabled(self) -> None:
        self.assertFalse(notify_slack("hello", enabled=False, webhook_url="http://x"))

    def test_only_safety_events(self) -> None:
        with patch("slack_notify.notify_slack", return_value=True) as mocked:
            self.assertFalse(maybe_notify_safety_event("entry", {"event": "entry"}))
            mocked.assert_not_called()
            self.assertTrue(
                maybe_notify_safety_event("halt_entries", {"event": "halt_entries", "x": 1})
            )
            mocked.assert_called_once()


if __name__ == "__main__":
    unittest.main()
