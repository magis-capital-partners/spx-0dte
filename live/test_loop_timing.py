"""Unit tests for adaptive loop timing (no IB required)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, time as dt_time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from live_config import LiveConfig  # noqa: E402
from loop_timing import (  # noqa: E402
    adaptive_sleep_seconds,
    interruptible_sleep,
    next_entry_datetime,
    seconds_until_market_open,
    seconds_until_next_tranche,
    stop_wake_thresholds,
)
from profiles import build_p3_trend_skew_config  # noqa: E402

SESSION_OPEN = dt_time(9, 30)


def test_next_tranche_after_now() -> None:
    config = build_p3_trend_skew_config()
    now = datetime(2026, 7, 7, 9, 40, 0)
    nxt = next_entry_datetime(now, config)
    assert nxt is not None
    assert nxt.hour == 9 and nxt.minute == 47


def test_adaptive_idle_sleeps_toward_tranche() -> None:
    live = LiveConfig(use_adaptive_polling=True, poll_seconds_max_idle=30.0, pre_tranche_wake_seconds=2.0)
    config = build_p3_trend_skew_config()
    # 20s past the minute: outside the deterministic signal-sampling window,
    # which legitimately caps sleep at signal_sample_poll_seconds (0.25s).
    now = datetime(2026, 7, 7, 9, 40, 20)
    sleep_for = adaptive_sleep_seconds(live=live, now=now, open_spreads=[], quotes=[], config=config)
    secs = seconds_until_next_tranche(now, config)
    assert secs is not None
    assert sleep_for <= 30.0
    assert sleep_for >= live.poll_seconds_pre_tranche


class SignalSamplingTimingTests(unittest.TestCase):
    def test_idle_loop_wakes_for_each_minute_sample_window(self) -> None:
        live = LiveConfig(
            use_adaptive_polling=True,
            poll_seconds_max_idle=30.0,
            signal_sample_offset_seconds=1.0,
            signal_sample_window_seconds=1.0,
            signal_sample_poll_seconds=0.25,
        )
        config = build_p3_trend_skew_config()
        before_window = datetime(2026, 7, 7, 9, 40, 50)
        self.assertLessEqual(
            adaptive_sleep_seconds(
                live=live, now=before_window, open_spreads=[], quotes=[], config=config,
            ),
            10.0,
        )
        inside_window = datetime(2026, 7, 7, 9, 41, 0, 500_000)
        self.assertLessEqual(
            adaptive_sleep_seconds(
                live=live, now=inside_window, open_spreads=[], quotes=[], config=config,
            ),
            0.25,
        )


class PreOpenWaitTests(unittest.TestCase):
    def test_early_launch_waits_until_the_lead_window(self) -> None:
        now = datetime(2026, 8, 5, 9, 11, 49)
        self.assertAlmostEqual(
            seconds_until_market_open(now, session_open=SESSION_OPEN, lead_seconds=180.0),
            (9 * 3600 + 27 * 60) - (9 * 3600 + 11 * 60 + 49),
            places=3,
        )

    def test_inside_lead_window_does_not_wait(self) -> None:
        now = datetime(2026, 8, 5, 9, 28, 0)
        self.assertEqual(
            seconds_until_market_open(now, session_open=SESSION_OPEN, lead_seconds=180.0),
            0.0,
        )

    def test_mid_session_restart_does_not_wait(self) -> None:
        now = datetime(2026, 8, 5, 13, 5, 0)
        self.assertEqual(
            seconds_until_market_open(now, session_open=SESSION_OPEN, lead_seconds=180.0),
            0.0,
        )

    def test_after_close_never_parks_until_tomorrow(self) -> None:
        now = datetime(2026, 8, 5, 17, 45, 0)
        self.assertEqual(
            seconds_until_market_open(now, session_open=SESSION_OPEN, lead_seconds=0.0),
            0.0,
        )


class StopWakeTests(unittest.TestCase):
    """Event-driven stop wake: a breaching tick must cut the idle sleep short."""

    @staticmethod
    def _spread(short_type: str, strike: float, stop_price: float, **kw):
        return SimpleNamespace(
            candidate=SimpleNamespace(short_type=short_type, short_strike=strike),
            stop_price=stop_price,
            stopped=kw.get("stopped", False),
            closed=kw.get("closed", False),
        )

    def test_thresholds_arm_live_short_legs_just_below_stop(self) -> None:
        live = LiveConfig(stop_wake_arm_fraction=0.95)
        spreads = [
            self._spread("CALL", 7545.0, 9.60),
            self._spread("PUT", 7400.0, 6.00),
        ]
        armed = stop_wake_thresholds(spreads, live)
        self.assertEqual(len(armed), 2)
        self.assertEqual(armed[0][0], "CALL")
        self.assertAlmostEqual(armed[0][2], 9.60 * 0.95, places=6)
        self.assertAlmostEqual(armed[1][2], 6.00 * 0.95, places=6)

    def test_thresholds_skip_closed_stopped_and_unpriced(self) -> None:
        live = LiveConfig(stop_wake_arm_fraction=0.95)
        spreads = [
            self._spread("CALL", 7545.0, 9.60, closed=True),
            self._spread("PUT", 7400.0, 6.00, stopped=True),
            self._spread("PUT", 7300.0, 0.0),
        ]
        self.assertEqual(stop_wake_thresholds(spreads, live), [])

    def test_interruptible_sleep_returns_early_on_wake(self) -> None:
        clock = {"t": 0.0}
        slept = []

        def fake_sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["t"] += seconds

        # Wake trips after three slices.
        def should_wake() -> bool:
            return len(slept) >= 3

        elapsed = interruptible_sleep(
            1.5,
            sleep_fn=fake_sleep,
            should_wake=should_wake,
            slice_seconds=0.05,
            monotonic=lambda: clock["t"],
        )
        self.assertEqual(len(slept), 3)
        self.assertAlmostEqual(elapsed, 0.15, places=6)

    def test_interruptible_sleep_runs_full_duration_without_wake(self) -> None:
        clock = {"t": 0.0}

        def fake_sleep(seconds: float) -> None:
            clock["t"] += seconds

        elapsed = interruptible_sleep(
            0.5,
            sleep_fn=fake_sleep,
            should_wake=lambda: False,
            slice_seconds=0.05,
            monotonic=lambda: clock["t"],
        )
        self.assertAlmostEqual(elapsed, 0.5, places=6)

    def test_interruptible_sleep_skips_sleep_when_already_woken(self) -> None:
        """A tick during the loop body must not cost a full slice of latency."""
        slept = []
        elapsed = interruptible_sleep(
            1.0,
            sleep_fn=lambda s: slept.append(s),
            should_wake=lambda: True,
            slice_seconds=0.05,
            monotonic=lambda: 0.0,
        )
        self.assertEqual(slept, [])
        self.assertEqual(elapsed, 0.0)

    def test_interruptible_sleep_without_wake_fn_matches_plain_sleep(self) -> None:
        clock = {"t": 0.0}

        def fake_sleep(seconds: float) -> None:
            clock["t"] += seconds

        elapsed = interruptible_sleep(
            0.2, sleep_fn=fake_sleep, slice_seconds=0.05,
            monotonic=lambda: clock["t"],
        )
        self.assertAlmostEqual(elapsed, 0.2, places=6)

    def test_interruptible_sleep_ignores_nonpositive_duration(self) -> None:
        slept = []
        self.assertEqual(
            interruptible_sleep(0.0, sleep_fn=lambda s: slept.append(s)), 0.0,
        )
        self.assertEqual(slept, [])


def main() -> None:
    test_next_tranche_after_now()
    test_adaptive_idle_sleeps_toward_tranche()
    print("loop_timing: PASS")


if __name__ == "__main__":
    main()
