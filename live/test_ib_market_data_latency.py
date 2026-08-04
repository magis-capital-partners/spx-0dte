"""Tranche-time market-data refreshes must never use blocking snapshots."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from ib_market_data import CachedQuote, IBStreamingMarketData  # noqa: E402
from live_config import LiveConfig  # noqa: E402


class _IB:
    def __init__(self) -> None:
        self.cancelled = []

    def qualifyContracts(self, *contracts):
        for idx, contract in enumerate(contracts, 1):
            contract.conId = idx
        return contracts

    def reqTickers(self, *_args, **_kwargs):
        raise AssertionError("blocking reqTickers is forbidden during a tranche")

    def reqMktData(self, contract, *_args, **_kwargs):
        return SimpleNamespace(
            contract=contract,
            bid=1.0,
            ask=1.1,
            modelGreeks=SimpleNamespace(delta=0.2, impliedVol=0.18),
        )

    def cancelMktData(self, contract):
        self.cancelled.append(contract)

    def sleep(self, _seconds):
        return None


def _stream() -> IBStreamingMarketData:
    stream = object.__new__(IBStreamingMarketData)
    stream.ib = _IB()
    stream.live = LiveConfig(tranche_quote_timeout_seconds=0.01)
    stream._expiry_0dte = "20260803"
    stream._expiry_next = "20260804"
    stream._spxw = SimpleNamespace(strikes=[7500.0])
    stream._spx_ticker = SimpleNamespace(last=7500.0)
    stream._delayed_fallback = False
    stream._next_expiry_quotes = []
    stream._cache = {}
    return stream


class TrancheMarketDataLatencyTests(unittest.TestCase):
    def test_invalid_ib_greeks_are_not_cached(self) -> None:
        stream = _stream()
        contract = SimpleNamespace(right="P", strike=7500.0)
        ticker = SimpleNamespace(
            bid=3.0,
            ask=3.1,
            modelGreeks=SimpleNamespace(delta=9.0, impliedVol=1e308),
        )
        stream._update_cache_from_ticker(contract, ticker)
        cached = stream._cache[("2026-08-03", "PUT", 7500.0)]
        self.assertIsNone(cached.delta)
        self.assertIsNone(cached.iv)

    def test_next_expiry_uses_bounded_streams_and_cancels_them(self) -> None:
        stream = _stream()
        stream.refresh_next_expiry_at_tranche(datetime(2026, 8, 3, 10, 2))
        self.assertEqual(len(stream._next_expiry_quotes), 2)
        self.assertEqual(len(stream.ib.cancelled), 2)

    def test_entry_leg_refresh_reads_existing_stream_cache(self) -> None:
        stream = _stream()
        stream._cache = {
            ("2026-08-03", "PUT", 7500.0): CachedQuote(3.0, 3.1, -0.2, 0.2, 1.0),
            ("2026-08-03", "PUT", 7350.0): CachedQuote(0.1, 0.2, -0.01, 0.3, 1.0),
        }
        short, long = stream.refresh_spread_legs(
            datetime(2026, 8, 3, 10, 2), "PUT", 7500.0, 7350.0,
        )
        self.assertEqual((short.bid, long.ask), (3.0, 0.2))


if __name__ == "__main__":
    unittest.main()
