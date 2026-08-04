"""Unit tests for adaptive loop timing (no IB required)."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from live_config import LiveConfig  # noqa: E402
from loop_timing import adaptive_sleep_seconds, next_entry_datetime, seconds_until_next_tranche  # noqa: E402
from profiles import build_p3_trend_skew_config  # noqa: E402


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


def main() -> None:
    test_next_tranche_after_now()
    test_adaptive_idle_sleeps_toward_tranche()
    print("loop_timing: PASS")


if __name__ == "__main__":
    main()
