"""Tests for consecutive stale-quote halt (entries only)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from stale_quotes import StaleQuoteTracker, evaluate_stale_quotes  # noqa: E402
from live_config import LiveConfig  # noqa: E402


def _spread(ask: float = 1.0, stop: float = 5.0):
    cand = SimpleNamespace(short_type="CALL", short_strike=7500.0, side="bear_call")
    return SimpleNamespace(
        candidate=cand,
        closed=False,
        stopped=False,
        stop_price=stop,
        contracts=1,
    )


def _quote(ask: float = 1.0):
    return SimpleNamespace(option_type="CALL", strike=7500.0, ask=ask, bid=0.9)


class StaleQuoteTests(unittest.TestCase):
    def test_needs_consecutive_polls(self) -> None:
        live = LiveConfig(stale_quote_confirm_polls=3, stale_quote_halt_seconds=20)
        tracker = StaleQuoteTracker()
        ages = {"CALL:7500": 25.0}

        def age_fn(opt, strike):
            return ages.get(f"{opt}:{strike:g}")

        for i in range(2):
            res = evaluate_stale_quotes(
                tracker, [_spread()], [_quote()], live=live, quote_age_fn=age_fn
            )
            self.assertFalse(res.confirmed, i)
        res = evaluate_stale_quotes(
            tracker, [_spread()], [_quote()], live=live, quote_age_fn=age_fn
        )
        self.assertTrue(res.confirmed)
        self.assertEqual(res.consecutive, 3)

    def test_fresh_resets_counter(self) -> None:
        live = LiveConfig(stale_quote_confirm_polls=3, stale_quote_halt_seconds=20)
        tracker = StaleQuoteTracker()

        def stale_fn(opt, strike):
            return 30.0

        def fresh_fn(opt, strike):
            return 1.0

        evaluate_stale_quotes(tracker, [_spread()], [_quote()], live=live, quote_age_fn=stale_fn)
        evaluate_stale_quotes(tracker, [_spread()], [_quote()], live=live, quote_age_fn=stale_fn)
        self.assertEqual(tracker.consecutive, 2)
        res = evaluate_stale_quotes(
            tracker, [_spread()], [_quote()], live=live, quote_age_fn=fresh_fn
        )
        self.assertFalse(res.confirmed)
        self.assertEqual(tracker.consecutive, 0)

    def test_near_stop_tighter_threshold(self) -> None:
        live = LiveConfig(
            stale_quote_confirm_polls=1,
            stale_quote_halt_seconds=20,
            stale_quote_near_stop_seconds=10,
            stop_near_fraction=0.80,
        )
        tracker = StaleQuoteTracker()
        # ask 4.5 >= 0.8*5 → near stop; age 12 > 10 → stale
        res = evaluate_stale_quotes(
            tracker,
            [_spread(ask=4.5, stop=5.0)],
            [_quote(ask=4.5)],
            live=live,
            quote_age_fn=lambda *_: 12.0,
        )
        self.assertTrue(res.confirmed)

    def test_flat_book_no_halt(self) -> None:
        live = LiveConfig(stale_quote_confirm_polls=1)
        tracker = StaleQuoteTracker()
        closed = _spread()
        closed.closed = True
        res = evaluate_stale_quotes(
            tracker, [closed], [], live=live, quote_age_fn=lambda *_: 999.0
        )
        self.assertFalse(res.confirmed)


if __name__ == "__main__":
    unittest.main()
