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
    # keeps the validated flat book. "linear_decay_downsize" sells more early,
    # less late — must be a key in simulator/profiles.SCHEMES.
    sizing_scheme: str = "linear_decay_downsize"

    # --- Deployment sizing -------------------------------------------------- #
    account_equity: float = 500_000.0
    # Peak contracts per tranche (morning). Time-of-day scheme scales this down
    # through the session — with linear_decay_downsize and base=2 you get:
    #   09:32-12:30 → 2 contracts   12:30-15:30 → 1 contract
    contracts_per_tranche: int = 2
    # Used only when contracts_per_tranche is 0. Multiplier on the profile's
    # validated 31-contract baseline after equity scaling.
    contract_scale: float = 1.0
    # Hard per-tranche safety cap, applied after time-of-day scaling.
    max_contracts_per_tranche: int = 2

    # --- Execution mode ----------------------------------------------------- #
    # dry   -- compute + log intended trades, place nothing (safe default).
    # paper -- route orders to IB paper (port 7497).
    # live  -- route orders to IB live (port 7496). Requires allow_live=True.
    mode: str = "paper"
    # In dry mode, still connect to IB to read the live chain (places nothing).
    dry_with_ib: bool = True
    # Safety interlock: live mode refuses to run unless this is explicitly True.
    allow_live: bool = False

    # --- IB connection ------------------------------------------------------ #
    host: str = "127.0.0.1"
    port: int = 0  # 0 = auto (7497 paper / 7496 live)
    client_id: int = 17
    poll_seconds: float = 15.0
    # IB market data type: 1=live, 2=frozen, 3=delayed (~15 min), 4=delayed frozen.
    # Use 3 on paper until OPRA + index subscriptions are active (fixes error 10168).
    market_data_type: int = 3
    # Max simultaneous option quote lines (puts + calls). IB default cap is 100
    # including the SPX index line — keep this ≤ 90.
    max_chain_lines: int = 80
    # Strike window around spot (points). Covers ~20Δ shorts plus 200/75 wings.
    chain_points_below: float = 350.0
    chain_points_above: float = 150.0
    # Path to rolling signal baselines (JSON) for live z-score assembly. Empty
    # keeps neutral signals (fine with gates off; see README known-gaps).
    baselines_path: str = ""
    # When using delayed data (market_data_type=3), synthesize missing bid/ask from
    # last/mid so credit filters can pass. Ignored when market_data_type=1 (live).
    delayed_quote_fallback: bool = True


# The one object the executor reads. Edit this to change the session.
#
# Current paper setup ($500k): peak 2 contracts/tranche, taper to 1 by midday
# via linear_decay_downsize. To go flatter, set contracts_per_tranche = 1
# (morning 1, afternoon 0–1).
ACTIVE = LiveConfig()
