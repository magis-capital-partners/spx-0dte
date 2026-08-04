"""Expiration-aware strike qualification + negative caching (IB error 200).

2026-08-04: 56 strike-qualification requests were rejected because the
subscription grid was built from the reqSecDefOptParams strike union (all
expirations) and failures were re-requested on every spot rebalance.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from live_config import LiveConfig  # noqa: E402
from mbh_simulator import StrategyConfig  # noqa: E402
from ib_market_data import IBStreamingMarketData  # noqa: E402


class _Event:
    def __iadd__(self, _handler):
        return self


class _Ticker(SimpleNamespace):
    def __init__(self, contract):
        super().__init__(contract=contract, updateEvent=_Event(),
                         bid=1.0, ask=1.2, last=1.1, close=1.0)


class _FakeIB:
    """Qualifies only strikes in ``listed_today``; counts every request."""

    def __init__(self, listed_today, details_strikes=None, details_raises=False):
        self.listed_today = set(listed_today)
        self.details_strikes = details_strikes
        self.details_raises = details_raises
        self.qualify_requests: list = []
        self.details_calls = 0

    def reqContractDetails(self, contract):
        self.details_calls += 1
        if self.details_raises:
            raise RuntimeError("details unavailable")
        strikes = (
            self.details_strikes
            if self.details_strikes is not None
            else sorted(self.listed_today)
        )
        return [
            SimpleNamespace(contract=SimpleNamespace(strike=s)) for s in strikes
        ]

    def qualifyContracts(self, *contracts):
        for contract in contracts:
            self.qualify_requests.append(
                (str(contract.right), float(contract.strike))
            )
            contract.conId = (
                int(contract.strike) if contract.strike in self.listed_today else 0
            )
        return [c for c in contracts if c.conId]

    def reqMktData(self, contract, *_args):
        return _Ticker(contract)

    def cancelMktData(self, _contract):
        return None

    def sleep(self, _seconds):
        return None


def _stream(fake_ib, listed_union) -> IBStreamingMarketData:
    stream = object.__new__(IBStreamingMarketData)
    stream.ib = fake_ib
    stream.live = LiveConfig(use_streaming_quotes=True)
    stream.config = StrategyConfig()
    stream._spxw = SimpleNamespace(strikes=sorted(listed_union))
    stream._expiry_0dte = "20260804"
    stream._expiry_next = "20260805"
    stream._cache = {}
    stream._tickers = {}
    stream._contracts = {}
    stream._required_0dte_legs = set()
    stream._expiry_strikes = {}
    stream._unqualified_specs = set()
    stream._anchor_spot = 0.0
    stream._listed_strikes = []
    return stream


class StrikeQualificationTests(unittest.TestCase):
    def test_strikes_for_expiry_uses_contract_details_and_caches(self) -> None:
        fake = _FakeIB(listed_today={7500.0, 7550.0}, details_strikes=[7500.0, 7550.0])
        stream = _stream(fake, listed_union=[7400.0, 7500.0, 7550.0, 7835.0])
        strikes = stream._strikes_for_expiry("20260804")
        self.assertEqual(strikes, [7500.0, 7550.0])
        stream._strikes_for_expiry("20260804")
        self.assertEqual(fake.details_calls, 1)  # cached after first call

    def test_strikes_for_expiry_falls_back_to_union_on_failure(self) -> None:
        fake = _FakeIB(listed_today={7500.0}, details_raises=True)
        union = [7400.0, 7500.0, 7550.0]
        stream = _stream(fake, listed_union=union)
        self.assertEqual(stream._strikes_for_expiry("20260804"), union)

    def test_select_strikes_only_offers_expiry_listed_strikes(self) -> None:
        # 7835 exists in the union (other expiries) but not on today's expiry.
        fake = _FakeIB(
            listed_today={7700.0, 7750.0, 7800.0},
            details_strikes=[7700.0, 7750.0, 7800.0],
        )
        stream = _stream(fake, listed_union=[7700.0, 7750.0, 7800.0, 7835.0])
        selected = stream._select_strikes(spot=7750.0)
        self.assertNotIn(7835.0, selected)
        self.assertTrue(selected)

    def test_negative_cache_prevents_requalification(self) -> None:
        # Details fall back to the union so an unlisted strike (7835) reaches
        # qualifyContracts once — and only once.
        fake = _FakeIB(listed_today={7700.0, 7750.0, 7800.0}, details_raises=True)
        stream = _stream(fake, listed_union=[7700.0, 7750.0, 7800.0, 7835.0])
        stream._subscribe_strikes(spot=7750.0)
        first_requests = [s for _, s in fake.qualify_requests]
        self.assertIn(7835.0, first_requests)
        self.assertIn(("20260804", "P", 7835.0), stream._unqualified_specs)

        fake.qualify_requests.clear()
        stream._subscribe_strikes(spot=7752.0)  # rebalance
        second_requests = [s for _, s in fake.qualify_requests]
        self.assertNotIn(7835.0, second_requests)

    def test_required_legs_bypass_negative_cache(self) -> None:
        fake = _FakeIB(listed_today={7700.0, 7750.0, 7800.0}, details_raises=True)
        stream = _stream(fake, listed_union=[7700.0, 7750.0, 7800.0])
        # Poison the cache with a required leg, then confirm it is still
        # requested and never re-added to the cache.
        stream._required_0dte_legs = {("C", 7800.0)}
        stream._unqualified_specs.add(("20260804", "C", 7800.0))
        stream._subscribe_strikes(spot=7750.0)
        self.assertIn(("C", 7800.0), fake.qualify_requests)


if __name__ == "__main__":
    unittest.main()
