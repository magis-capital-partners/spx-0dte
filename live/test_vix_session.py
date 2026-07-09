"""Unit tests for VIX session gate and elevated sizing (no IB required)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "live"))

from live_config import LiveConfig  # noqa: E402
from vix_session import (  # noqa: E402
    check_vix_session_allowed,
    format_vix_session_banner,
    vix_elevated_sizing_multiplier,
)


def test_skip_when_vix_above_threshold() -> None:
    live = LiveConfig(use_vix_session_gate=True, vix_skip_open_above=35.0)
    blocked, reason = check_vix_session_allowed(35.1, live)
    assert blocked is True
    assert reason == "vix_above_skip_threshold"
    blocked, reason = check_vix_session_allowed(35.0, live)
    assert blocked is False
    assert reason == ""


def test_gate_off_allows_high_vix() -> None:
    live = LiveConfig(use_vix_session_gate=False)
    blocked, reason = check_vix_session_allowed(42.0, live)
    assert blocked is False
    assert reason == ""


def test_unavailable_vix_blocks_when_gate_on() -> None:
    live = LiveConfig(use_vix_session_gate=True)
    blocked, reason = check_vix_session_allowed(None, live)
    assert blocked is True
    assert reason == "vix_unavailable"


def test_elevated_sizing_band() -> None:
    live = LiveConfig(
        use_vix_elevated_sizing=True,
        vix_elevated_min=25.0,
        vix_elevated_max=35.0,
        vix_elevated_scale=1.25,
    )
    assert vix_elevated_sizing_multiplier(24.9, live) == 1.0
    assert vix_elevated_sizing_multiplier(25.0, live) == 1.25
    assert vix_elevated_sizing_multiplier(30.0, live) == 1.25
    assert vix_elevated_sizing_multiplier(35.0, live) == 1.25
    assert vix_elevated_sizing_multiplier(35.1, live) == 1.0


def test_elevated_sizing_off() -> None:
    live = LiveConfig(use_vix_elevated_sizing=False, vix_elevated_scale=1.25)
    assert vix_elevated_sizing_multiplier(30.0, live) == 1.0


def test_session_banner() -> None:
    live = LiveConfig()
    text = format_vix_session_banner(
        28.5,
        vix_source="calendar",
        skip_reason="",
        sizing_multiplier=1.25,
        live=live,
    )
    assert "vix_open=28.50" in text
    assert "sizing=1.25x" in text


if __name__ == "__main__":
    test_skip_when_vix_above_threshold()
    test_gate_off_allows_high_vix()
    test_unavailable_vix_blocks_when_gate_on()
    test_elevated_sizing_band()
    test_elevated_sizing_off()
    test_session_banner()
    print("ok")
