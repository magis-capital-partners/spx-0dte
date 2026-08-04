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
    # FOMC 13:30, IC overlay (10/31 size fraction, Δ0.16, VIX≥15, 1×/day). No VIX put-widen.
    profile: str = "p3_poststop_cooldown_120"
    sizing_scheme: str = "linear_decay_downsize"

    # --- Signal baselines --------------------------------------------------- #
    baselines_path: str = "data/models/live_signal_baselines.json"
    require_baselines: bool = True
    baselines_max_age_days: int = 3

    # --- Deployment sizing -------------------------------------------------- #
    account_equity: float = 500_000.0
    # Live deployment: two contracts per submitted spread. The per-entry cap
    # prevents time-of-day or elevated-VIX sizing from increasing this.
    contracts_per_tranche: int = 2
    contract_scale: float = 1.0
    max_contracts_per_tranche: int = 2

    # --- Execution mode ----------------------------------------------------- #
    mode: str = "live"
    dry_with_ib: bool = False
    allow_live: bool = True

    # --- IB connection ------------------------------------------------------ #
    host: str = "127.0.0.1"
    # Explicitly use the user-configured TWS live API endpoint. Do not rely on
    # the paper/live default-port convention for this production session.
    port: int = 7496
    # Explicit account binding. This Gateway exposes multiple accounts; every
    # account guard and submitted order must use this account only.
    ib_account: str = "U805366"
    client_id: int = 17
    # Keep normal live logs compact. Structured fills/errors/tranches already
    # provide the execution audit trail; ib_insync DEBUG additionally contains
    # every streamed wire message and can grow to gigabytes in one session.
    # Set ib_wire_debug_capture=True only for a short diagnostic session.
    ib_log_level: str = "INFO"
    ib_wire_debug_capture: bool = False
    ib_log_max_bytes: int = 10 * 1024 * 1024
    ib_log_backup_count: int = 3
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
    # Restart/reconnect: wait for every recovered position leg to have a fresh
    # markable quote before the mark-integrity governor begins.
    recovery_quote_warmup_seconds: float = 10.0
    spot_rebalance_points: float = 10.0
    fetch_next_expiry_at_tranche: bool = True
    # Bound temporary next-expiry quote collection. Never use IB's ~11-second
    # blocking snapshot path inside an entry tranche.
    tranche_quote_timeout_seconds: float = 0.75
    # Fail closed for new entries if the SPX index stream has not delivered a
    # valid live-last update within this window. Open-position option marks and
    # stops continue to be managed; stale underlying alone never flattens.
    stale_spot_halt_seconds: float = 5.0
    # A z-score this large is a feed/sentinel problem, not a tradable signal.
    signal_sanity_abs_z: float = 12.0

    # --- Phase 3: adaptive polling ------------------------------------------ #
    use_adaptive_polling: bool = True
    poll_seconds_active: float = 1.5
    poll_seconds_near_stop: float = 0.75
    poll_seconds_pre_tranche: float = 0.5
    poll_seconds_max_idle: float = 30.0
    pre_tranche_wake_seconds: float = 2.0
    # Alpha is sampled on a fixed minute-boundary window, independent of loop
    # cadence. Risk and order management continue on their faster clocks.
    signal_sample_offset_seconds: float = 1.0
    signal_sample_window_seconds: float = 1.0
    signal_sample_min_observations: int = 2
    signal_sample_max_wait_seconds: float = 1.0
    signal_sample_poll_seconds: float = 0.25
    signal_max_feature_quote_age_seconds: float = 5.0
    signal_max_feature_timestamp_dispersion_seconds: float = 1.5

    # --- Phase 4: stop execution ------------------------------------------ #
    stop_limit_slippage_pct: float = 0.05
    stop_limit_slippage_abs: float = 0.25  # aligned with production stop_fill_slippage
    stop_limit_timeout_seconds: float = 3.0
    stop_near_fraction: float = 0.80
    # Synthetic stop must stay breached for this many seconds (matches backtest
    # stop_confirm_seconds=120). Set ≤0 to fall back to poll-count confirmation
    # via StrategyConfig.stop_confirmation_count.
    stop_confirm_seconds: float = 120.0
    # Cancel → add → replace native BUY STP on the short leg.
    # Synthetic loop stops remain primary (at strategy 3× after confirm_seconds);
    # native STP is a WIDER disaster backstop if the process dies. Default
    # native_stop_multiple=4.5 so it cannot race the synthetic 3× path.
    # Before a same-strike combo add, existing STPs are cancelled (IB error 201),
    # then replaced for the aggregated short qty after fill — or re-armed on reject.
    use_native_stop_replace: bool = True
    # Wider than StrategyConfig.stop_multiple (3.0) so native cannot race synthetic.
    native_stop_multiple: float | None = 4.5
    # Cap how long same-strike scale-ins may leave STPs disarmed (also caps
    # pending work_until when adding onto an existing short).
    native_stop_disarm_max_seconds: float = 45.0
    # Re-check working STP still exists in IB; replace if cancelled/missing.
    native_stop_verify_seconds: float = 30.0
    # Legacy disaster-only STP at stop_price×1.5. Prefer use_native_stop_replace
    # with a wider native_stop_multiple.
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
    entry_max_signal_age_seconds: float = 75.0
    entry_max_spot_drift_points: float = 8.0
    entry_max_spot_drift_pct: float = 0.0015
    entry_min_credit_ratio: float = 0.80
    entry_max_credit_drop: float = 0.50
    entry_max_short_delta_drift: float = 0.05
    entry_max_leg_timestamp_dispersion_seconds: float = 1.0
    entry_max_short_bid_ask_width: float = 0.50
    entry_max_long_bid_ask_width: float = 0.30
    # True with OPRA (market_data_type=1); rejects crossed/stale NBBO on entry.
    entry_require_live_nbbo: bool = True
    refresh_legs_before_entry: bool = True
    # Record the SMART-combo NBBO alongside the two leg quotes.  The guard
    # remains off until paper validation confirms IB's BAG quote convention.
    combo_quote_guard_enabled: bool = False
    combo_quote_timeout_seconds: float = 0.75
    # A condor must be routed as one four-leg BAG, never as two independent
    # vertical orders.  Leave this off until the paired-order path has passed
    # replay and paper-soak validation; the live overlay then removes the
    # backtest's condor sleeve before candidates are generated.
    enable_paired_condor_live: bool = False

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
    # Production profile also carries: FOMC 13:30 + IC overlay Δ0.16 / 10-lot (no VIX put-widen).
    # Those live on StrategyConfig from profiles.build_p3_poststop_cooldown_config.

    # --- Safety overlays (Phases A–F) ---------------------------------------- #
    # External KILL file: data/live/KILL or data/live/<date>/KILL
    kill_switch_enabled: bool = True
    # Account overlay: PnL governor still uses account_equity; these only gate
    # that IB NetLiq / BuyingPower are sufficient to run the configured book.
    use_account_guards: bool = True
    netliq_min_ratio: float = 1.0          # startup: NetLiq >= equity × ratio
    buying_power_min_ratio: float = 0.15   # startup: BP >= equity × ratio
    netliq_halt_ratio: float = 0.90        # loop: halt entries if NetLiq < equity × ratio
    netliq_flatten_ratio: float = 0.0      # loop flatten floor (0 = disabled)
    flatten_on_netliq_breach: bool = False
    account_guard_poll_seconds: float = 45.0
    # Mark integrity: halt/flatten when open risk cannot be marked.
    mark_degraded_halt: bool = True
    mark_unavailable_halt_seconds: float = 15.0
    # Quote outages halt new entries immediately.  Keep the bounded-risk spread
    # open long enough for a transient OPRA/streaming gap to recover before a
    # market flatten is allowed.
    mark_unavailable_flatten_seconds: float = 300.0
    # Flatten confirmation
    flatten_fill_timeout_seconds: float = 12.0
    flatten_retry_mkt: bool = True
    # Disconnect / reconnect circuit breaker
    use_disconnect_breaker: bool = True
    reconnect_max_seconds: float = 120.0
    reconnect_initial_backoff: float = 2.0
    reconnect_max_backoff: float = 30.0

    # --- Next-wave safeties ------------------------------------------------- #
    # Stale-quote halt (entries only; never flatten on stale alone).
    stale_quote_confirm_polls: int = 3
    stale_quote_halt_seconds: float = 20.0
    stale_quote_near_stop_seconds: float = 10.0
    # Production-live concentration limits. At the two-lot pilot these permit
    # at most three same-side structures, only one at an exact strike, and two
    # structures inside a 25-point directional cluster.
    max_open_contracts: int = 8
    max_open_per_side: int = 6
    max_open_same_strike: int = 2
    max_open_side_cluster: int = 4
    side_cluster_points: float = 25.0
    # Live stop-count caps (production profile uses 999; tighten here).
    live_max_stops_per_side: int = 2
    live_max_stops_per_day: int = 4
    # Slack alerts via SPX_SLACK_WEBHOOK_URL (no-op if unset).
    slack_notify_enabled: bool = True
    # Heartbeat for local watchdog (same machine as executor).
    heartbeat_seconds: float = 5.0
    # Persist a compact risk/return-on-margin point at this cadence.
    risk_snapshot_seconds: float = 60.0
    # Enable portfolio margin allocator when sizing up past pilot.
    use_portfolio_allocator_live: bool = False
    # Confirm synthetic stop against IB short-leg qty after fill.
    confirm_stop_against_ib: bool = True
    # Pre-entry buying-power check vs estimated margin.
    use_pre_entry_buying_power: bool = True


# Default: prefer live OPRA (type 1) with auto-fallback to delayed (type 3).
# Live mode forces auto_fallback_delayed=False at runtime.
# Delayed-only paper: market_data_type=3, entry_require_live_nbbo=False, delayed_quote_fallback=True.
ACTIVE = LiveConfig()
