"""Regression tests for the 2026-08-03 stale-SPX incident."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from ib_market_data import IBStreamingMarketData, _spot_from_ticker  # noqa: E402


class _ContaminatedTicker:
    """IB index ticker with a live last and stale snapshot bid/ask."""

    last = 7567.09
    close = 7500.0
    bid = 7532.22
    ask = 7545.08
    bidSize = 1
    askSize = 1

    def marketPrice(self):
        return (self.bid + self.ask) / 2.0


class _SnapshotIB:
    def __init__(self) -> None:
        self.requested = None

    def reqTickers(self, contract):
        self.requested = contract
        return [_ContaminatedTicker()]

    def sleep(self, _seconds):
        return None


class _EmptyTicker:
    """What IB returns while a market data farm is still connecting (IB 2119)."""

    last = 0.0
    close = 0.0
    bid = 0.0
    ask = 0.0
    bidSize = 0
    askSize = 0

    def marketPrice(self):
        return 0.0


class _WarmingIB:
    def __init__(self, empty_probes: int) -> None:
        self._empty_probes = empty_probes
        self.probes = 0
        self.slept = 0.0

    def reqTickers(self, contract):
        self.probes += 1
        if self.probes <= self._empty_probes:
            return [_EmptyTicker()]
        return [_ContaminatedTicker()]

    def sleep(self, seconds):
        self.slept += seconds or 0.0


class ProbeRetryTests(unittest.TestCase):
    """2026-08-05: a 09:11 launch aborted on the first empty snapshot."""

    @staticmethod
    def _stream(ib) -> IBStreamingMarketData:
        stream = object.__new__(IBStreamingMarketData)
        stream.ib = ib
        stream._spx = SimpleNamespace(symbol="SPX", conId=416904)
        stream.live = SimpleNamespace(
            market_data_probe_retry_seconds=0.0,
            market_data_probe_timeout_seconds=120.0,
        )
        return stream

    def test_probe_retries_while_the_data_farm_connects(self) -> None:
        ib = _WarmingIB(empty_probes=3)
        stream = self._stream(ib)
        self.assertGreater(
            stream._probe_spx_spot(timeout_seconds=120.0, wait_sec=0), 0
        )
        self.assertEqual(ib.probes, 4)

    def test_probe_still_gives_up_once_the_budget_expires(self) -> None:
        ib = _WarmingIB(empty_probes=10_000)
        stream = self._stream(ib)
        self.assertEqual(
            stream._probe_spx_spot(timeout_seconds=0.0, wait_sec=0), 0.0
        )
        self.assertEqual(ib.probes, 1)


class SpotRegressionTests(unittest.TestCase):
    def test_live_index_last_wins_over_stale_snapshot_midpoint(self) -> None:
        self.assertEqual(_spot_from_ticker(_ContaminatedTicker()), 7567.09)

    def test_snapshot_probe_does_not_reuse_stream_contract_object(self) -> None:
        stream = object.__new__(IBStreamingMarketData)
        stream.ib = _SnapshotIB()
        stream._spx = SimpleNamespace(symbol="SPX", conId=416904)

        self.assertGreater(stream._probe_spx_snapshot(wait_sec=0), 0)
        self.assertIsNot(stream.ib.requested, stream._spx)

    def test_stale_stream_is_detected_even_when_old_price_is_positive(self) -> None:
        stream = object.__new__(IBStreamingMarketData)
        stream._last_spx_update_at = time.monotonic() - 10.0
        self.assertTrue(stream.spot_is_stale(5.0))
        self.assertFalse(stream.spot_is_stale(15.0))


if __name__ == "__main__":
    unittest.main()
