"""Unit tests for live post-stop cooldown gates (no IB required)."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))
sys.path.insert(0, str(ROOT / "simulator"))

from profiles import build_p3_poststop_cooldown_config  # noqa: E402
from risk_gates import (  # noqa: E402
    apply_side_stop_cooldowns,
    side_stop_cooldown_block_reason,
    side_stop_cooldown_remaining_seconds,
)


@dataclass
class _Stop:
    side: str


def test_poststop_profile_loads_cooldown() -> None:
    cfg = build_p3_poststop_cooldown_config()
    assert cfg.same_side_stop_cooldown_minutes == 120
    assert cfg.candidate_max_adverse_trend == 1.0
    assert cfg.candidate_max_adverse_skew == 0.65
    assert cfg.flatten_loss_limit_pct == 0.0325


def test_cooldown_blocks_same_side_only() -> None:
    cfg = build_p3_poststop_cooldown_config()
    now = datetime(2026, 7, 8, 10, 0, 0)
    clocks: dict[str, datetime] = {}
    apply_side_stop_cooldowns([_Stop("bear_call")], config=cfg, now=now, side_stop_cooldown_until=clocks)
    assert side_stop_cooldown_block_reason("bear_call", now, cfg, clocks) == "side_stop_cooldown"
    assert side_stop_cooldown_block_reason("bull_put", now, cfg, clocks) == ""
    later = now + timedelta(minutes=119)
    assert side_stop_cooldown_block_reason("bear_call", later, cfg, clocks) == "side_stop_cooldown"
    after = now + timedelta(minutes=120)
    assert side_stop_cooldown_block_reason("bear_call", after, cfg, clocks) == ""


def test_cooldown_resets_on_repeat_stop() -> None:
    cfg = build_p3_poststop_cooldown_config()
    t0 = datetime(2026, 7, 8, 10, 0, 0)
    clocks: dict[str, datetime] = {}
    apply_side_stop_cooldowns([_Stop("bull_put")], config=cfg, now=t0, side_stop_cooldown_until=clocks)
    t1 = t0 + timedelta(minutes=30)
    apply_side_stop_cooldowns([_Stop("bull_put")], config=cfg, now=t1, side_stop_cooldown_until=clocks)
    assert side_stop_cooldown_remaining_seconds("bull_put", t1 + timedelta(minutes=119), cfg, clocks) > 0
    assert side_stop_cooldown_block_reason("bull_put", t1 + timedelta(minutes=119), cfg, clocks) == "side_stop_cooldown"
    assert side_stop_cooldown_block_reason("bull_put", t1 + timedelta(minutes=120), cfg, clocks) == ""


def main() -> None:
    test_poststop_profile_loads_cooldown()
    test_cooldown_blocks_same_side_only()
    test_cooldown_resets_on_repeat_stop()
    print("risk_gates: PASS")


if __name__ == "__main__":
    main()
