"""Live/paper execution settings — the single knob for running the executor.

Everything the executor needs is defined here in ``ACTIVE`` so a session is just
``python live/ib_executor.py`` with no command-line flags.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LiveConfig:
    # --- Strategy selection ------------------------------------------------- #
    # Production: put wing 150, skew 0.65, flatten −3.25%, 120min cooldown,
    # FOMC 13:30, VIX put+25, plus IC overlay (8/31 size fraction, VIX≥15, 1×/day).
    profile: str = "p3_poststop_cooldown_120"
    sizing_scheme: str = "linear_decay_downsize"

    # --- Signal baselines --------------------------------------------------- #
    baselines_path: str = "data/models/live_signal_baselines.json"
    require_baselines: bool = True
    baselines_max_age_days: int = 3

    # --- Deployment sizing -------------------------------------------------- #
    account_equity: float = 500_000.0
    contracts_per_tranche: int = 2
    contract_scale: float = 1.0
    max_contracts_per_tranche: int = 3  # round(2 × 1.25 tod × 1.25 vix) on elevated morning tranche

    # --- Execution mode ----------------------------------------------------- #
    mode: str = "paper"
    dry_with_ib: bool = True
    allow_live: bool = False

    # --- IB connection ------------------------------------------------------ #
    host: str = "127.0.0.1"
    port: int = 0
    client_id: int = 17
    # Legacy fixed sleep when use_adaptive_polling=False.
    poll_seconds: float = 15.0
    # 1=live OPRA (real-time). 3=delayed (15-min) — only if you lack index/OPRA subs.
    # Try live first; falls back to delayed if subs missing (enable delayed in TWS).
    market_data_type: int = 1
    auto_fallback_delayed: bool = True
    max_chain_lines: int = 80
    chain_points_below: float = 350.0
    chain_points_above: float = 150.0
    delayed_quote_fallback: bool = False

    # --- Phase 2: streaming market data ------------------------------------- #
    use_streaming_quotes: bool = True
    streaming_generic_ticks: str = "106"
    streaming_warmup_seconds: float = 2.0
    spot_rebalance_points: float = 50.0
    fetch_next_expiry_at_tranche: bool = True

    # --- Phase 3: adaptive polling ------------------------------------------ #
    use_adaptive_polling: bool = True
    poll_seconds_active: float = 1.5
    poll_seconds_near_stop: float = 0.75
    poll_seconds_pre_tranche: float = 0.5
    poll_seconds_max_idle: float = 30.0
    pre_tranche_wake_seconds: float = 2.0

    # --- Phase 4: stop execution ------------------------------------------ #
    stop_limit_slippage_pct: float = 0.05
    stop_limit_slippage_abs: float = 0.15
    stop_limit_timeout_seconds: float = 3.0
    stop_near_fraction: float = 0.80
    # Native STP on the short leg conflicts with scale-in (IB error 201). Synthetic
    # stops in the run loop are primary; leave this False unless you never add size
    # at the same short strike across tranches.
    use_native_stop_backstop: bool = False

    # --- Phase 5: entry execution ------------------------------------------- #
    # Limit credit = natural_credit - entry_limit_concession (- ladder steps).
    # Orders work for entry_work_seconds (non-blocking; loop keeps managing stops).
    entry_limit_concession: float = 0.05
    entry_min_credit: float = 0.20
    entry_work_seconds: float = 870.0
    entry_ladder_step: float = 0.05
    entry_ladder_interval_seconds: float = 60.0
    entry_max_ladder_steps: int = 3
    entry_poll_seconds: float = 0.5
    max_leg_quote_age_seconds: float = 5.0
    # True with OPRA (market_data_type=1); rejects crossed/stale NBBO on entry.
    entry_require_live_nbbo: bool = True
    refresh_legs_before_entry: bool = True

    # --- VIX session controls (validated in data/vix_regime_tests) ------------ #
    # Skip the entire session when same-day VIX open exceeds the threshold
    # (matches skip_gt35 backtest: better Sharpe / lower max DD).
    use_vix_session_gate: bool = True
    vix_skip_open_above: float = 35.0
    vix_calendar_path: str = "data/calendar/vix_daily.csv"
    vix_refresh_if_missing: bool = True
    # Upsize on elevated 25–35 VIX days (highest historical per-trade expectancy).
    use_vix_elevated_sizing: bool = True
    vix_elevated_min: float = 25.0
    vix_elevated_max: float = 35.0
    vix_elevated_scale: float = 1.25
    # Production profile also carries: put wing +25 when VIX>=20, FOMC entry end 13:30.
    # Those live on StrategyConfig from profiles.build_p3_poststop_cooldown_config.


# Default: prefer live OPRA (type 1) with auto-fallback to delayed (type 3).
# For strict real-time only (fail if no subs): auto_fallback_delayed=False.
# Delayed-only: market_data_type=3, entry_require_live_nbbo=False, delayed_quote_fallback=True.
ACTIVE = LiveConfig()
