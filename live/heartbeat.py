"""Session heartbeat file for the local watchdog."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
LIVE_DIR = ROOT / "data" / "live"


def heartbeat_path(today: str, *, live_dir: Path = LIVE_DIR) -> Path:
    return live_dir / today / "heartbeat.json"


def write_heartbeat(
    today: str,
    *,
    open_count: int,
    marked_pnl: float,
    entries_halted: bool = False,
    flattened: bool = False,
    live_dir: Path = LIVE_DIR,
    pid: Optional[int] = None,
    extra: Optional[dict] = None,
) -> Path:
    path = heartbeat_path(today, live_dir=live_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "ts": datetime.now().isoformat(),
        "pid": pid if pid is not None else os.getpid(),
        "open_count": int(open_count),
        "marked_pnl": round(float(marked_pnl), 2),
        "entries_halted": bool(entries_halted),
        "flattened": bool(flattened),
    }
    if extra:
        payload.update(extra)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def append_risk_snapshot(
    today: str,
    snapshot: dict,
    *,
    live_dir: Path = LIVE_DIR,
) -> Path:
    """Append an intraday risk observation for return-on-margin history."""
    path = live_dir / today / "risk_snapshots.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": datetime.now().isoformat(), **snapshot}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    return path


def read_heartbeat(today: str, *, live_dir: Path = LIVE_DIR) -> Optional[dict]:
    path = heartbeat_path(today, live_dir=live_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def heartbeat_age_seconds(payload: dict, *, now: Optional[datetime] = None) -> Optional[float]:
    raw = payload.get("ts")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return None
    clock = now or datetime.now()
    return max(0.0, (clock - ts).total_seconds())
