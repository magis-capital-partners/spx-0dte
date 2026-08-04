"""Synthetic stop confirmation: freshness-gated accrual + dynamic windows."""
from __future__ import annotations

import shutil
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "live"))

from mbh_simulator import CandidateRecord, OptionQuote, StrategyConfig  # noqa: E402
from live_config import LiveConfig  # noqa: E402
from ib_executor import OpenSpread, effective_stop_confirm_seconds, manage_stops  # noqa: E402

# Synthetic session date: dry-run manage_stops appends events under
# data/live/<TODAY>/ and must never touch a real session folder.
TODAY = "1999-01-01"


def tearDownModule() -> None:  # noqa: N802 (unittest hook)
    shutil.rmtree(ROOT / "data" / "live" / TODAY, ignore_errors=True)


def _spread(short: float = 7550.0, short_entry: float = 1.0) -> OpenSpread:
    now = datetime(2026, 7, 9, 11, 0, 0)
    cand = CandidateRecord(
        timestamp=now,
        side="bear_call",
        status="pass",
        reason="",
        score=1.0,
        expiry="20260709",
        short_type="CALL",
        short_strike=short,
        long_strike=short + 75,
        short_delta=-0.2,
        long_delta=-0.05,
        spot=7500.0,
        distance_pct=0.01,
        width=75.0,
        credit=0.5,
        credit_to_width=0.5 / 75.0,
        stop_loss_to_credit=3.0,
        straddle_residual_z=0.0,
        skew_z=0.0,
        term_ratio_z=0.0,
        trend_score=0.0,
        realized_vs_implied_z=0.0,
    )
    return OpenSpread(
        candidate=cand,
        contracts=1,
        short_entry_sell=short_entry,
        long_entry_buy=0.2,
        stop_price=short_entry * 3.0,
    )


def _quote(ts: datetime, ask: float, strike: float = 7550.0) -> OptionQuote:
    return OptionQuote(ts, "20260709", "CALL", strike, ask - 0.05, ask)


def _run_polls(spread, ask, config, live, *, start, seconds, step_seconds=5.0,
               quote_age_fn=None, spot=0.0, connection_ok=True):
    """Simulate the executor's poll cadence over a breach window."""
    stopped = []
    t = start
    end = start + timedelta(seconds=seconds)
    while t <= end:
        stopped = manage_stops(
            None, [spread], [_quote(t, ask)], config, TODAY, True, live,
            now=t, quote_age_fn=quote_age_fn, spot=spot,
            connection_ok=connection_ok,
        )
        if stopped:
            return stopped, t
        t += timedelta(seconds=step_seconds)
    return stopped, t


class ManageStopsConfirmTests(unittest.TestCase):
    def _config(self) -> StrategyConfig:
        return StrategyConfig(
            stop_multiple=3.0, stop_confirmation_count=2, use_short_leg_stops=True,
        )

    def test_brief_spike_does_not_stop(self) -> None:
        live = LiveConfig(stop_confirm_seconds=120.0)
        config = self._config()
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price + 0.10
        q = [_quote(t0, ask)]
        stopped = manage_stops(None, [spread], q, config, TODAY, True, live, now=t0)
        self.assertEqual(stopped, [])
        self.assertFalse(spread.stopped)
        # 30s later still below confirm window
        t1 = t0 + timedelta(seconds=30)
        stopped = manage_stops(None, [spread], q, config, TODAY, True, live, now=t1)
        self.assertEqual(stopped, [])
        self.assertFalse(spread.stopped)

    def test_sustained_breach_stops(self) -> None:
        live = LiveConfig(stop_confirm_seconds=120.0)
        config = self._config()
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price + 0.10
        stopped, fired_at = _run_polls(
            spread, ask, config, live, start=t0, seconds=180,
        )
        self.assertEqual(len(stopped), 1)
        self.assertTrue(spread.stopped)
        elapsed = (fired_at - t0).total_seconds()
        self.assertGreaterEqual(elapsed, 120.0)
        self.assertLess(elapsed, 140.0)

    def test_reset_on_ask_recede(self) -> None:
        live = LiveConfig(stop_confirm_seconds=120.0)
        config = StrategyConfig(stop_multiple=3.0, use_short_leg_stops=True)
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        hot = spread.stop_price + 0.10
        cool = spread.stop_price - 0.50
        manage_stops(
            None, [spread], [_quote(t0, hot)],
            config, TODAY, True, live, now=t0,
        )
        t1 = t0 + timedelta(seconds=60)
        manage_stops(
            None, [spread], [_quote(t1, cool)],
            config, TODAY, True, live, now=t1,
        )
        self.assertIsNone(spread.stop_breach_since)
        self.assertEqual(spread.stop_confirmed_seconds, 0.0)
        t2 = t1 + timedelta(seconds=120)
        stopped = manage_stops(
            None, [spread], [_quote(t2, hot)],
            config, TODAY, True, live, now=t2,
        )
        # Fresh breach at t2 — not yet confirmed
        self.assertEqual(stopped, [])
        self.assertFalse(spread.stopped)

    def test_stalled_loop_cannot_confirm_in_one_step(self) -> None:
        """Two evaluations 120s apart credit at most the per-step cap."""
        live = LiveConfig(stop_confirm_seconds=120.0, stop_confirm_max_step_seconds=10.0)
        config = self._config()
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price + 0.10
        manage_stops(None, [spread], [_quote(t0, ask)], config, TODAY, True, live, now=t0)
        t1 = t0 + timedelta(seconds=120)
        stopped = manage_stops(
            None, [spread], [_quote(t1, ask)], config, TODAY, True, live, now=t1,
        )
        self.assertEqual(stopped, [])
        self.assertFalse(spread.stopped)
        self.assertLessEqual(spread.stop_confirmed_seconds, 10.0)

    def test_stale_quotes_pause_confirmation(self) -> None:
        """Frozen marks (silent 1100 outage) must not advance the clock."""
        live = LiveConfig(stop_confirm_seconds=120.0, stop_quote_max_age_seconds=5.0)
        config = self._config()
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price + 0.10
        stale_age_fn = lambda opt_type, strike: 45.0  # noqa: E731
        stopped, _ = _run_polls(
            spread, ask, config, live, start=t0, seconds=300,
            quote_age_fn=stale_age_fn,
        )
        self.assertEqual(stopped, [])
        self.assertFalse(spread.stopped)
        self.assertTrue(spread.stop_confirm_paused)
        self.assertEqual(spread.stop_confirmed_seconds, 0.0)
        # Quotes come back fresh: accrual resumes and completes normally.
        fresh_age_fn = lambda opt_type, strike: 0.5  # noqa: E731
        t_resume = t0 + timedelta(seconds=305)
        stopped, fired_at = _run_polls(
            spread, ask, config, live, start=t_resume, seconds=180,
            quote_age_fn=fresh_age_fn,
        )
        self.assertEqual(len(stopped), 1)
        self.assertGreaterEqual((fired_at - t_resume).total_seconds(), 120.0)

    def test_connection_loss_pauses_confirmation(self) -> None:
        live = LiveConfig(stop_confirm_seconds=120.0)
        config = self._config()
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price + 0.10
        stopped, _ = _run_polls(
            spread, ask, config, live, start=t0, seconds=300,
            connection_ok=False,
        )
        self.assertEqual(stopped, [])
        self.assertFalse(spread.stopped)
        self.assertEqual(spread.stop_confirmed_seconds, 0.0)

    def test_severe_breach_fires_immediately(self) -> None:
        live = LiveConfig(stop_confirm_seconds=120.0, stop_immediate_ask_ratio=1.30)
        config = self._config()
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price * 1.35
        stopped = manage_stops(
            None, [spread], [_quote(t0, ask)], config, TODAY, True, live, now=t0,
        )
        self.assertEqual(len(stopped), 1)
        self.assertTrue(spread.stopped)

    def test_underlying_cross_fires_immediately(self) -> None:
        live = LiveConfig(
            stop_confirm_seconds=120.0, stop_immediate_on_underlying_cross=True,
        )
        config = self._config()
        spread = _spread(short=7550.0)
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price + 0.10  # minor premium breach on its own
        stopped = manage_stops(
            None, [spread], [_quote(t0, ask)], config, TODAY, True, live,
            now=t0, spot=7551.0,  # SPX through the short call strike
        )
        self.assertEqual(len(stopped), 1)
        self.assertTrue(spread.stopped)

    def test_fast_tier_shortens_confirmation(self) -> None:
        live = LiveConfig(
            stop_confirm_seconds=120.0,
            stop_fast_confirm_ask_ratio=1.10,
            stop_fast_confirm_seconds=20.0,
        )
        config = self._config()
        spread = _spread()
        t0 = datetime(2026, 7, 9, 11, 0, 0)
        ask = spread.stop_price * 1.15
        stopped, fired_at = _run_polls(
            spread, ask, config, live, start=t0, seconds=60, step_seconds=5.0,
        )
        self.assertEqual(len(stopped), 1)
        elapsed = (fired_at - t0).total_seconds()
        self.assertGreaterEqual(elapsed, 20.0)
        self.assertLess(elapsed, 40.0)

    def test_effective_confirm_modes(self) -> None:
        live = LiveConfig(
            stop_confirm_seconds=120.0,
            stop_fast_confirm_ask_ratio=1.10,
            stop_fast_confirm_seconds=20.0,
            stop_immediate_ask_ratio=1.30,
            stop_immediate_on_underlying_cross=True,
        )
        base_kwargs = dict(
            stop_price=3.0, spot=7500.0, short_type="CALL",
            short_strike=7550.0, live=live,
        )
        self.assertEqual(
            effective_stop_confirm_seconds(ask=3.1, **base_kwargs),
            (120.0, "standard"),
        )
        self.assertEqual(
            effective_stop_confirm_seconds(ask=3.4, **base_kwargs),
            (20.0, "fast_breach"),
        )
        self.assertEqual(
            effective_stop_confirm_seconds(ask=4.0, **base_kwargs),
            (0.0, "severe_breach"),
        )
        crossed = dict(base_kwargs, spot=7550.5)
        self.assertEqual(
            effective_stop_confirm_seconds(ask=3.1, **crossed),
            (0.0, "underlying_cross"),
        )
        # Put side crosses downward.
        put_kwargs = dict(
            stop_price=3.0, spot=7449.0, short_type="PUT",
            short_strike=7450.0, live=live,
        )
        self.assertEqual(
            effective_stop_confirm_seconds(ask=3.1, **put_kwargs),
            (0.0, "underlying_cross"),
        )


if __name__ == "__main__":
    unittest.main()
