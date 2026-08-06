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
    # Live deployment: 4 contracts per submitted spread (2x the prior 2-lot
    # baseline, set 2026-08-06 for the next session). Per-entry cap allows the
    # 1.25x elevated-VIX band (round(4*1.25)=5) but nothing above that.
    contracts_per_tranche: int = 4
    contract_scale: float = 1.0
    max_contracts_per_tranche: int = 5

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

    # --- Pre-open launch ---------------------------------------------------- #
    # Launching before 09:30 used to abort on "Could not obtain SPX spot from IB":
    # TWS reports "market data farm is connecting" (IB 2119) for several seconds
    # after connect, and the cash index publishes no prints pre-open, so the very
    # first snapshot legitimately comes back empty on a healthy session.
    # Idle until shortly before the open, then probe with a retry budget.
    wait_for_market_open: bool = True
    # Start the stream this far ahead of 09:30 so the chain is warm at the open.
    market_data_lead_seconds: float = 180.0
    # Total budget for the startup SPX probe before giving up (subscription
    # problems still fail loud, just not on the first empty snapshot).
    market_data_probe_timeout_seconds: float = 120.0
    market_data_probe_retry_seconds: float = 3.0

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
    # Raised 5 -> 15 on 2026-08-06 (account request): 5s was tripping on
    # ordinary momentary quote gaps mid-session (10:37:30, resumed on its own
    # after 31s via the stale_underlying auto-resume) rather than only on
    # genuine feed outages.
    stale_spot_halt_seconds: float = 15.0
    # Resume entries automatically once the SPX stream has been continuously
    # healthy for this long after a stale_underlying halt. A feed gap is a
    # transient data condition, not a risk event, so latching it for the rest of
    # the session throws the day away; requiring a sustained healthy streak
    # keeps a flapping feed from re-arming entries. 0 disables auto-resume.
    stale_underlying_resume_seconds: float = 30.0
    # A z-score this large is a feed/sentinel problem, not a tradable signal.
    signal_sanity_abs_z: float = 12.0
    # Mark-integrity guard: reject a short-leg ask above this when the leg has
    # no bid at all. A zero-bid option is worth ~nothing, so a large one-sided
    # ask is a stale/garbage print; marking against it books a phantom loss that
    # can trip the -2.25% entry halt or the -3.25% flatten on a false reading.
    # Set 0 to disable. See _short_ask_is_implausible for the 2026-08-05 case.
    mark_max_short_ask_without_bid: float = 5.0

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
    # Event-driven stop wake: the loop's idle sleep is cut short when a watched
    # short leg ticks at or above its stop price, so an immediate-mode stop
    # (severe breach / underlying cross) fires on the tick instead of waiting
    # out poll_seconds_near_stop. Detection then floors at IB's ~250ms feed
    # cadence rather than the poll interval. Slice bounds the check granularity.
    use_stop_wake: bool = True
    stop_wake_slice_seconds: float = 0.05
    # Wake slightly before the stop price so the loop is already awake and has
    # a fresh quote when the breach lands (fraction of stop_price).
    stop_wake_arm_fraction: float = 0.95

    # --- Phase 4: stop execution ------------------------------------------ #
    stop_limit_slippage_pct: float = 0.05
    stop_limit_slippage_abs: float = 0.25  # aligned with production stop_fill_slippage
    stop_limit_timeout_seconds: float = 3.0
    stop_near_fraction: float = 0.80
    # Synthetic stop must stay breached for this many seconds (matches backtest
    # stop_confirm_seconds=120). Set ≤0 to fall back to poll-count confirmation
    # via StrategyConfig.stop_confirmation_count.
    stop_confirm_seconds: float = 120.0
    # --- Dynamic stop confirmation (2026-08-04 post-mortem) ------------------ #
    # 96% of that session's $690 stop drag accrued BEFORE order submission,
    # inside the flat 120s window. Confirmation time now accrues only while the
    # short-leg quote is fresh, and severe breaches skip the wait entirely.
    # Breach time accrues only when the short-leg quote updated within this
    # window; stale/frozen quotes PAUSE (not reset) the confirmation clock.
    # 0 disables the freshness gate.
    stop_quote_max_age_seconds: float = 5.0
    # Cap on confirmation time credited per loop iteration, so a stalled loop
    # or silent outage cannot complete a 120s confirmation in one step.
    stop_confirm_max_step_seconds: float = 10.0
    # Fast tier: ask >= stop_price × ratio shortens confirmation to
    # stop_fast_confirm_seconds. 0 disables.
    stop_fast_confirm_ask_ratio: float = 1.10
    stop_fast_confirm_seconds: float = 20.0
    # Immediate tier: ask >= stop_price × ratio executes without confirmation
    # (with stop_multiple=3.0 and ratio 1.30 this is ~3.9× entry credit,
    # still inside the 4.5× native STP backstop). 0 disables.
    stop_immediate_ask_ratio: float = 1.30
    # Underlying crossing the short strike is a decisive directional breach:
    # execute immediately instead of confirming.
    stop_immediate_on_underlying_cross: bool = True
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
    # Entry cancels are awaited across loop iterations instead of blocking on
    # ib.sleep, so stop management keeps running while IB processes the cancel.
    # Upper bound before a pending entry resolves without IB confirmation.
    entry_cancel_grace_seconds: float = 1.0
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
    # Upstream connectivity breaker (IB system events 1100/1101/1102): TWS can
    # lose its connection to IB servers while the local API socket stays up.
    # Halt new entries immediately on 1100 and pause stop confirmations until
    # 1101/1102 restores; 1101 additionally forces a market-data resubscribe.
    use_upstream_health_breaker: bool = True

    # --- Next-wave safeties ------------------------------------------------- #
    # Stale-quote halt (entries only; never flatten on stale alone).
    stale_quote_confirm_polls: int = 3
    stale_quote_halt_seconds: float = 20.0
    stale_quote_near_stop_seconds: float = 10.0
    # Production-live concentration limits. Scaled with the 4-lot baseline
    # (2026-08-06: 24/18/12 at 2-lot -> 48/36/24 at 4-lot) so concentration
    # caps stay proportional and don't bind before the aggregate cap.
    max_open_contracts: int = 48
    max_open_per_side: int = 36
    # Same-strike concentration cap. Static fallback only — see
    # max_open_same_strike_multiple below, which is what actually governs
    # this in production. Left intentionally tight (2) as the floor that
    # applies if the dynamic multiplier is ever disabled (set to 0).
    max_open_same_strike: int = 2
    # 2026-08-05: made same-strike dynamic at the account's request ("12x the
    # current sell size") rather than a fixed lot count, so it scales with
    # whatever size is actually being traded (VIX-elevated sizing,
    # downsize-after-stop, etc.) instead of going stale as sizing changes.
    # Effective cap = this x the tranche's sized contracts at the moment of
    # the check, and supersedes max_open_same_strike above when > 0.
    max_open_same_strike_multiple: float = 12.0
    max_open_side_cluster: int = 24
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
