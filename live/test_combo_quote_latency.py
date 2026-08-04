"""SMART BAG quote collection must be bounded and cancellable."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from ib_executor import fetch_combo_execution_quote  # noqa: E402


class _IB:
    def __init__(self) -> None:
        self.cancelled = False
        self.stream_requests = 0

    def reqTickers(self, *_args, **_kwargs):
        raise AssertionError("blocking reqTickers must not be used for BAG quotes")

    def reqMktData(self, *_args, **_kwargs):
        self.stream_requests += 1
        return SimpleNamespace(bid=-6.95, ask=-6.30)

    def cancelMktData(self, _contract):
        self.cancelled = True

    def sleep(self, _seconds):
        return None


class ComboQuoteLatencyTests(unittest.TestCase):
    def test_uses_bounded_stream_and_always_cancels(self) -> None:
        ib = _IB()
        bag = object()
        quote = fetch_combo_execution_quote(ib, bag, timeout_seconds=0.01)
        self.assertEqual((quote.bid, quote.ask), (-6.95, -6.30))
        self.assertEqual(ib.stream_requests, 1)
        self.assertTrue(ib.cancelled)


if __name__ == "__main__":
    unittest.main()
