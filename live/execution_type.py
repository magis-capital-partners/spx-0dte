"""Immutable labels for how an execution session was routed."""
from __future__ import annotations

from typing import Optional


_MODE_TO_TYPE = {
    "paper": "paper",
    "dry": "dry_run",
    "live": "production_live",
}


def execution_type(mode: object, recorded: Optional[object] = None) -> str:
    """Return a stable public label, honoring a recorded valid value first."""
    if recorded in set(_MODE_TO_TYPE.values()):
        return str(recorded)
    return _MODE_TO_TYPE.get(str(mode or "").lower(), "unknown")


def execution_type_label(value: object) -> str:
    return {
        "paper": "Paper trading",
        "dry_run": "Dry run",
        "production_live": "Production live",
    }.get(str(value or ""), "Unknown")
