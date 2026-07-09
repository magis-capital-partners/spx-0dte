"""Unit tests for adaptive loop timing (no IB required)."""
from __future__ import annotations

import sys
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
    now = datetime(2026, 7, 7, 9, 40, 0)
    sleep_for = adaptive_sleep_seconds(live=live, now=now, open_spreads=[], quotes=[], config=config)
    secs = seconds_until_next_tranche(now, config)
    assert secs is not None
    assert sleep_for <= 30.0
    assert sleep_for >= live.poll_seconds_pre_tranche


def main() -> None:
    test_next_tranche_after_now()
    test_adaptive_idle_sleeps_toward_tranche()
    print("loop_timing: PASS")


if __name__ == "__main__":
    main()
