"""Live/paper execution settings — the single knob for running the executor.

Everything the executor needs is defined here in ``ACTIVE`` so a session is just
``python live/ib_executor.py`` with no command-line flags. Edit ``ACTIVE`` to
change what runs, then dry-run, paper, and (eventually) go live off the same
object. The *strategy* itself comes from a named profile in
``simulator/profiles.py``; this file only controls how that profile is deployed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LiveConfig:
    # --- Strategy selection ------------------------------------------------- #
    profile: str = "3d_flatten_3_5"
    # Optional time-of-day contract weighting (Test 3G). "" or "control_flat"
    # keeps the validated flat book; e.g. "linear_decay_downsize" sells more
    # early / less late. Must be a key in simulator/profiles.SCHEMES.
    sizing_scheme: str = ""

    # --- Deployment sizing -------------------------------------------------- #
    account_equity: float = 1_000_000.0
    # Fractional multiplier on the profile's validated ($13M-referenced) size.
    # 0.10 on a $1M paper account trades a small, safe clip per tranche.
    contract_scale: float = 0.10
    # Hard per-tranche safety cap on contracts, applied after all sizing.
    max_contracts_per_tranche: int = 5

    # --- Execution mode ----------------------------------------------------- #
    # dry   -- compute + log intended trades, place nothing (safe default).
    # paper -- route orders to IB paper (port 7497).
    # live  -- route orders to IB live (port 7496). Requires allow_live=True.
    mode: str = "dry"
    # In dry mode, still connect to IB to read the live chain (places nothing).
    dry_with_ib: bool = False
    # Safety interlock: live mode refuses to run unless this is explicitly True.
    allow_live: bool = False

    # --- IB connection ------------------------------------------------------ #
    host: str = "127.0.0.1"
    port: int = 0  # 0 = auto (7497 paper / 7496 live)
    client_id: int = 17
    poll_seconds: float = 15.0
    # Path to rolling signal baselines (JSON) for live z-score assembly. Empty
    # keeps neutral signals (fine with gates off; see README known-gaps).
    baselines_path: str = ""


# The one object the executor reads. Edit this to change the session.
ACTIVE = LiveConfig()
