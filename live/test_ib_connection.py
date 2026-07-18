"""Tests for IB reconnect backoff helper."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from ib_connection import ib_is_connected, reconnect_ib  # noqa: E402


class FakeIB:
    def __init__(self, succeed_on: int = 2) -> None:
        self._connected = False
        self._attempts = 0
        self.succeed_on = succeed_on
        self.connect_calls = 0

    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def connect(self, host, port, clientId=0) -> None:
        self.connect_calls += 1
        self._attempts += 1
        if self._attempts >= self.succeed_on:
            self._connected = True


class IbConnectionTests(unittest.TestCase):
    def test_already_connected(self) -> None:
        ib = FakeIB()
        ib._connected = True
        out = reconnect_ib(ib, host="127.0.0.1", port=7497, client_id=1, sleep_fn=lambda _s: None)
        self.assertTrue(out.connected)
        self.assertEqual(out.attempts, 0)

    def test_reconnect_succeeds(self) -> None:
        ib = FakeIB(succeed_on=2)
        sleeps: list[float] = []
        out = reconnect_ib(
            ib,
            host="127.0.0.1",
            port=7497,
            client_id=1,
            max_seconds=30,
            initial_backoff=0.01,
            max_backoff=0.01,
            sleep_fn=lambda s: sleeps.append(s),
        )
        self.assertTrue(out.connected)
        self.assertEqual(out.attempts, 2)
        self.assertGreaterEqual(ib.connect_calls, 2)

    def test_reconnect_budget(self) -> None:
        ib = FakeIB(succeed_on=10_000_000)  # never connects within budget
        # Advance a fake clock so max_seconds trips without spinning forever.
        clock = {"t": 0.0}

        def fake_sleep(seconds: float) -> None:
            clock["t"] += float(seconds)

        import ib_connection as mod

        real_time = mod._time.time
        try:
            mod._time.time = lambda: clock["t"]  # type: ignore[method-assign]
            out = reconnect_ib(
                ib,
                host="127.0.0.1",
                port=7497,
                client_id=1,
                max_seconds=0.05,
                initial_backoff=0.02,
                max_backoff=0.02,
                sleep_fn=fake_sleep,
            )
        finally:
            mod._time.time = real_time  # type: ignore[method-assign]
        self.assertFalse(out.connected)
        self.assertIn(
            out.reason,
            {"reconnect_budget_exhausted", "connect_returned_disconnected"},
        )

    def test_ib_is_connected_none(self) -> None:
        self.assertFalse(ib_is_connected(None))
        self.assertFalse(ib_is_connected(SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
