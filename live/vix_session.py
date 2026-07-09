"""VIX open-of-day session gate and elevated-regime sizing for live execution."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from live_config import LiveConfig

ROOT = Path(__file__).resolve().parents[1]


def _vix_calendar_path(live: LiveConfig) -> Path:
    return ROOT / live.vix_calendar_path


def resolve_session_vix_open(today: str, live: LiveConfig) -> Tuple[Optional[float], str]:
    """Return (same-day VIX open, source tag) for ``today`` (YYYY-MM-DD)."""
    import sys

    sys.path.insert(0, str(ROOT / "simulator"))
    from vix_daily import download_and_save, load_vix_daily, vix_for_date  # noqa: E402

    path = _vix_calendar_path(live)
    vix_by_date = load_vix_daily(path)
    day = vix_for_date(vix_by_date, today)
    if day is not None:
        return day.decision_vix, "calendar"

    if not live.vix_refresh_if_missing:
        return None, "missing"

    try:
        download_and_save(start_date=today, end_date=today, path=path)
    except Exception as exc:
        return None, f"fetch_failed:{exc!r}"

    vix_by_date = load_vix_daily(path)
    day = vix_for_date(vix_by_date, today)
    if day is not None:
        return day.decision_vix, "yahoo"
    return None, "missing"


def check_vix_session_allowed(vix_open: Optional[float], live: LiveConfig) -> Tuple[bool, str]:
    """Return (blocked, reason). Blocked sessions should not start the executor."""
    if not live.use_vix_session_gate:
        return False, ""
    if vix_open is None:
        return True, "vix_unavailable"
    if vix_open > live.vix_skip_open_above:
        return True, "vix_above_skip_threshold"
    return False, ""


def vix_elevated_sizing_multiplier(vix_open: Optional[float], live: LiveConfig) -> float:
    """Scale contracts when VIX open is in the elevated 25–35 band (backtest sweet spot)."""
    if not live.use_vix_elevated_sizing or vix_open is None:
        return 1.0
    if live.vix_elevated_min <= vix_open <= live.vix_elevated_max:
        return live.vix_elevated_scale
    return 1.0


def format_vix_session_banner(
    vix_open: Optional[float],
    *,
    vix_source: str,
    skip_reason: str,
    sizing_multiplier: float,
    live: LiveConfig,
) -> str:
    if skip_reason:
        vix_txt = f"{vix_open:.2f}" if vix_open is not None else "n/a"
        return f"vix_open={vix_txt} ({vix_source}) SKIP>{live.vix_skip_open_above:.0f}"
    vix_txt = f"{vix_open:.2f}" if vix_open is not None else "n/a"
    sizing_txt = f"{sizing_multiplier:.2f}x" if sizing_multiplier != 1.0 else "1.00x"
    gate = "on" if live.use_vix_session_gate else "off"
    return f"vix_open={vix_txt} ({vix_source}) gate={gate} sizing={sizing_txt}"
