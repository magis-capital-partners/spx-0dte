"""Persist / reload SessionFeatureState across mid-session restarts."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))

from live_features import SessionFeatureState  # noqa: E402

LIVE_DIR = ROOT / "data" / "live"


def feature_state_path(today: str, live_dir: Path = LIVE_DIR) -> Path:
    return live_dir / today / "feature_state.json"


def save_feature_state(
    today: str,
    state: SessionFeatureState,
    *,
    live_dir: Path = LIVE_DIR,
) -> Path:
    path = feature_state_path(today, live_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.to_dict()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_feature_state(
    today: str,
    *,
    live_dir: Path = LIVE_DIR,
) -> Optional[SessionFeatureState]:
    path = feature_state_path(today, live_dir)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return SessionFeatureState.from_dict(payload)
