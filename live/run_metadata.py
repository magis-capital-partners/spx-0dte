"""Immutable per-process run identity for live telemetry.

Intraday restarts on 2026-08-04 (five ``session_start`` events) could not be
tied to a code version or resolved config, which made the post-mortem audit
slow. Every fills/tranches row and heartbeat now carries:

  run_id       -- unique per executor process (restart boundary marker)
  git_commit   -- short hash of HEAD (+ ``-dirty`` when the tree has edits)
  config_hash  -- sha256 over the resolved LiveConfig + StrategyConfig
  signal_version -- feature-set identity used for z-scored signals
  pid          -- OS process id
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]

# Bump when the live feature formulas change in a way that affects signals
# (see simulator/live_features.py and simulator/rv_feature.py).
SIGNAL_ALGO_VERSION = 1


@lru_cache(maxsize=1)
def git_commit_hash() -> str:
    """Short HEAD hash with a ``-dirty`` suffix; ``unknown`` off-repo."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        if head.returncode != 0:
            return "unknown"
        commit = head.stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        dirty = status.returncode == 0 and bool(status.stdout.strip())
        return f"{commit}-dirty" if dirty else commit
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def config_hash(*payloads: Dict[str, Any]) -> str:
    """Deterministic short hash over resolved config dicts."""
    canonical = json.dumps(payloads, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def signal_version() -> str:
    try:
        from historical_baselines import FEATURES
        feature_id = hashlib.sha256(
            ",".join(FEATURES).encode("utf-8")
        ).hexdigest()[:8]
    except ImportError:
        feature_id = "unknown"
    return f"v{SIGNAL_ALGO_VERSION}-{feature_id}"


def build_run_metadata(live_config_dict: Dict[str, Any],
                       strategy_config_dict: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": uuid.uuid4().hex[:12],
        "git_commit": git_commit_hash(),
        "config_hash": config_hash(live_config_dict, strategy_config_dict),
        "signal_version": signal_version(),
        "pid": os.getpid(),
    }
