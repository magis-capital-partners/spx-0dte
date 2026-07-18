"""External operator kill switch for the live executor.

Presence of either file aborts trading:
  - ``data/live/KILL`` (global)
  - ``data/live/<date>/KILL`` (session-day)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"


@dataclass(frozen=True)
class KillSwitchHit:
    path: Path
    scope: str  # "global" | "session"


def kill_paths(today: str, *, live_dir: Path = LIVE_DIR) -> tuple[Path, Path]:
    return live_dir / "KILL", live_dir / today / "KILL"


def check_kill_switch(
    today: str,
    *,
    enabled: bool = True,
    live_dir: Path = LIVE_DIR,
) -> Optional[KillSwitchHit]:
    """Return a hit if a KILL file exists; otherwise None."""
    if not enabled:
        return None
    global_path, session_path = kill_paths(today, live_dir=live_dir)
    if global_path.is_file():
        return KillSwitchHit(path=global_path, scope="global")
    if session_path.is_file():
        return KillSwitchHit(path=session_path, scope="session")
    return None
