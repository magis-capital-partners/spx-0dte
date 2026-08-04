# Live Execution (Interactive Brokers)

Production SPX 0DTE executor with **streaming quotes**, **adaptive polling**, and
**limit-then-MKT stops**. Strategy logic matches `p3_poststop_cooldown_120` in
`simulator/profiles.py` (skew gate **0.65**, flatten **−3.25%**, put wing **150** /
call wing **75**, trend/skew gates, **120-minute same-side stop cooldown** after
any stopped spread).

**Safety overview (PDF):** [`docs/Live_Executor_Safeties.pdf`](../docs/Live_Executor_Safeties.pdf)
— regenerate with `python scripts/generate_live_safeties_pdf.py`.

## Low-latency architecture

| Phase | Implementation |
|---|---|
| **1 — Cached chain** | `reqSecDefOptParams` once at session start (`ib_market_data.py`) |
| **2 — Streaming** | `reqMktData` on SPX + strike grid; loop reads in-memory cache |
| **3 — Adaptive poll** | 0.75s near stop · 1.5s with open risk · sleep until tranche when flat |
| **4 — Stop execution** | Limit buy at ask + 5% / $0.15 buffer, MKT fallback after 3s |
| **5 — Entry execution** | Non-blocking combo limit: natural credit − $0.05 concession, work ~14.5m, ladder every 60s |

Term ratio: next-expiry ATM snapshot refreshed **at tranche boundaries only**
(four IB lines every 15 minutes).

Entry orders are **non-blocking** — the loop keeps polling fills and managing
stops while a combo works. See `live/entry_execution.py`.

### Post-stop cooldown (live)

When `same_side_stop_cooldown_minutes=120` (default profile), each stopped spread
starts a **side-specific** 120-minute entry pause:

- Stopped **bear call** → no new call spreads until cooldown expires; puts still allowed.
- Stopped **bull put** → no new put spreads; calls still allowed.

### VIX session controls

At startup the executor reads **same-day VIX open** from `data/calendar/vix_daily.csv`
(Yahoo `^VIX`, auto-refreshed if today's row is missing). Validated in
`data/vix_regime_tests/` (July 2026).

| Rule | Default | Behavior |
|------|---------|----------|
| **Skip session** | VIX open **> 35** | `SystemExit` before IB connect — no trading |
| **Elevated sizing** | VIX **25–35** | Contract count × **1.25** (cap **3** at 2-contract baseline) |

Configure in `live/live_config.py` (`use_vix_session_gate`, `vix_skip_open_above`,
`use_vix_elevated_sizing`, `vix_elevated_scale`, `max_contracts_per_tranche`, etc.).
Refresh calendar: `python scripts/download_vix_daily.py`.
- Open positions are not closed by the cooldown — only new entries are blocked.
- Implemented in `live/risk_gates.py`, wired from `ib_executor.py` after `manage_stops()`.

Events: `side_stop_cooldown_start` and `entry_blocked` with `reason=side_stop_cooldown`
in `data/live/<date>/fills.jsonl`.

## Before each session

```powershell
python scripts/refresh_live_baselines.py
python scripts/download_vix_daily.py   # optional; auto-refreshes if today's row missing
```

## Paper trading with real-time data

**Prerequisites (IB account + TWS/Gateway):**

1. Log into **paper** TWS or IB Gateway (API port **7497**).
2. **Market data subscriptions** on your IB account (paper inherits live subs):
   - **US Securities Snapshot and Futures Value Bundle** (or CBOE **US Index** for SPX)
   - **OPRA** (US Options) — required for SPXW option quotes
3. TWS → **Settings → API → Settings**: enable *ActiveX and Socket Clients*, note port **7497**.
4. TWS → **Settings → Market Data**: you can disable *Allow delayed market data* if you only want live feeds.

**Config** (`live/live_config.py` — already set for real-time):

| Field | Value | Purpose |
|-------|-------|---------|
| `mode` | `"paper"` | Routes orders to paper account (port 7497) |
| `market_data_type` | `1` | Live/real-time quotes (not 15-min delayed) |
| `auto_fallback_delayed` | `True` | Falls back to delayed if live subs missing (set `False` for strict OPRA) |
| `delayed_quote_fallback` | `False` | Do not synthesize bid/ask from last/mid |
| `entry_require_live_nbbo` | `True` | Block entries on crossed or missing NBBO |

**Run:**

```powershell
python live/ib_executor.py
```

On connect you should see `market_data_type=1 (live)` in the chain banner. If SPX spot fails with error **10168**, your account lacks index/OPRA subs — add them in IB Account Management or temporarily revert to delayed settings (see comment at bottom of `live_config.py`).

Session output: `data/live/<YYYY-MM-DD>/` (`config.json`, `fills.jsonl`, `tranches.jsonl`, `ib.log`).

### Day-to-day paper vs backtest

1. **Pre-session:** `python scripts/refresh_live_baselines.py` (+ VIX calendar if needed).
2. **Session:** `python live/ib_executor.py` → writes `data/live/<date>/` (includes `session_end.marked_pnl`).
3. **Post-close:** `.\scripts\daily_data_update.ps1` builds ThetaData for the day, then runs
   `python simulator/reconcile_live.py --date YYYY-MM-DD` when a live session exists.
4. **Dashboard:** `.\scripts\sync_dashboard.ps1` embeds live fills (`--include-live`). Daily drill-down
   shows paper fills next to backtest tranches plus reconcile DIFF chips.

### Restart safety (required before live)

The executor now:

1. **Single-instance lock** — `data/live/<date>/executor.lock` (PID). A second process exits unless the prior PID is dead.
2. **Book recovery** — rebuilds `open_spreads` from today's `fills.jsonl` (entries minus stops/flatten) and resumes stop/flatten management.
3. **Governor recovery** — restores `entries_halted`, `flattened`, and same-side cooldowns from `fills.jsonl` so a restart cannot resume selling after a halt/flatten.
4. **Fail loud** — if IB shows SPXW option risk that does not match the recovered book, startup exits with the residual legs printed. Flatten/reconcile in TWS, then restart.
5. **Recovered-leg quote reservation** — every short and long leg in the recovered
   book is pinned inside the streaming line budget before the first mark. These
   subscriptions survive spot-grid rebalances and reconnects. Startup waits up
   to 10 seconds for fresh markable quotes; a prior mark-only restart halt is
   cleared only after all recovered legs validate. P&L, account, stop-count,
   stale-data, flatten, and operator halts are never auto-cleared.

Also cancels orphan working SPXW/BAG orders from a crashed prior run. Do **not** start a second executor mid-session; if you must restart, use one process only and let recovery reload the book.

### Operator safeties

| Control | Behavior |
|---------|----------|
| **KILL file** | Create `data/live/KILL` or `data/live/<date>/KILL` — executor flattens (with fill confirm) and exits. Present at startup → refuse to start until removed. Portable: works on whichever host runs the session. |
| **CLEAR_STALE_HALT** | Create `data/live/<date>/CLEAR_STALE_HALT`, then restart the executor. One-shot: lifts a sticky `stale_quotes` entry halt only (never flatten/PnL/account). File is consumed on startup. |
| **Account overlay** | PnL halt/flatten still use configured `account_equity`. Startup requires IB NetLiq ≥ equity and BuyingPower ≥ 15% of equity. Loop halts entries if NetLiq < 90% of equity. Pre-entry BP check vs estimated margin. |
| **Disconnect breaker** | On IB disconnect: halt entries, reconnect with backoff (default 120s budget), re-arm native STPs, re-verify book. Failure with open risk → confirmed flatten then exit. |
| **Upstream breaker (1100/1101/1102)** | TWS can lose its IB-server link while the API socket stays "connected" (7× on 2026-08-04). Error 1100 → immediate entry halt + stop-confirmation pause; 1102 → resume; 1101 → resume after full market-data resubscribe and open-leg quote warmup. Other halts raised during the outage are never auto-cleared. |
| **Dynamic stop confirm** | Confirmation time accrues only on fresh short-leg quotes (≤5s age) with a healthy connection; stale marks pause (not reset) the clock, and one loop step credits ≤10s. Standard 120s for minor noise; ask ≥ stop×1.10 → 20s fast tier; ask ≥ stop×1.30 or the underlying crossing the short strike → immediate execution. |
| **Strike qualification** | Subscription grid is built from each expiry's true listed strikes (one `reqContractDetails` per expiry) with a negative cache for anything IB refuses — no repeated error-200 churn (56× on 2026-08-04). Required open-position legs always bypass the negative cache. |
| **Sizing floor** | Time-of-day × VIX multipliers are combined and rounded once; any positive product floors at 1 contract so a 1-lot baseline can't be rounded to zero after 14:30 (`round(1×0.45)=0` artifact). An explicit 0.0 schedule segment still halts entries. |
| **Run identity** | Every fills/tranches row, heartbeat, and config snapshot carries `run_id`, `git_commit`, `config_hash`, `signal_version`, and `pid`; entry and stop events carry decision→submission and submission→fill latencies. Restarts are auditable from the logs alone. |
| **Signal cold-start** | No feature-state advancement before 09:30 (pre-open spot history poisoned realized-vol z-scores: −1.29M on 2026-08-04); z-scores beyond the sanity bound are discarded and logged with their values. |
| **Mark integrity** | Missing quotes on open risk → halt entries (never treat as $0 PnL). Unavailable marks for 60s → flatten. |
| **Stale quotes** | 3 consecutive polls with short-leg age >20s (10s near stop) → **halt entries only** (never flatten on stale alone). |
| **Open-risk caps** | Paper-fidelity backstop: max 40 open contracts / 40 per side / 25 at one short strike. Calibrated just above reconstructed pilot-scale historical maxima; per-entry size remains capped at 3. |
| **Live stop caps** | Max 2 stops/side and 4/day via `entry_risk_block_reason` (profile 999 overridden). |
| **Flatten confirm + audit** | MKT close waits for fill; `flatten_audit` checks IB flat afterward. |
| **Stop confirm** | After synthetic stop fill, verify IB short qty dropped; else `stop_unconfirmed` and keep managing. |
| **Live mode data** | `mode=live` forces `auto_fallback_delayed=False` (no silent delayed downgrade). |

### Slack + supervised auto-heal (Magis workspace)

Slack must use the **Magis Capital Partners** workspace (`drew@magiscapitalpartners.com`), not a personal Slack.

```powershell
# One-time: wire Magis Incoming Webhook (copies Magis org webhook, or paste a dedicated #spx URL)
.\scripts\set_spx_slack_webhook.ps1 -UseMagisWorkspaceWebhook -SendTest
# Or: .\scripts\set_spx_slack_webhook.ps1 -SlackWebhookUrl "https://hooks.slack.com/services/..." -SendTest

# Install Task Scheduler jobs (daily 09:15 + at logon): executor, watchdog, status API, cloud publish
.\scripts\install_live_supervisor_tasks.ps1 -StartNow
```

Secrets live in `%USERPROFILE%\.magis-spx-0dte-secrets.ps1` (not git). Logs: `data/live/supervisor/`.

### Dashboard Session now (local + cloud)

| Path | What | Where |
|------|------|--------|
| **A local** | Heartbeat + stdout console | `http://127.0.0.1:8765` polled by dashboard |
| **B cloud** | Sanitized alive/halted/open/PnL | `docs/data/live_status.json` on GitHub Pages |

```powershell
# Status API (also writes live_status.json every 60s)
.\scripts\run_session_status_server.ps1

# Optional: publish sanitized status to Pages (rate-limited)
.\scripts\publish_live_status.ps1 -Deploy

# Best local UX (HTTP↔HTTP, full console): 
.\scripts\serve_dashboard_local.ps1   # http://127.0.0.1:5500/
```

On the public Pages site you get cloud status (B). Full command-prompt stream needs the trading PC + Status API (A); Chrome often allows `127.0.0.1` from HTTPS Pages, otherwise use the local serve script.

Manual / second terminal (loads the same Magis secrets):

```powershell
.\scripts\run_ib_executor_supervised.ps1
.\scripts\run_live_watchdog_supervised.ps1 -WriteKill
```

Heartbeat: `data/live/<date>/heartbeat.json` (updated every `heartbeat_seconds`).

The watchdog resolves the current session date on every poll, so a long-running
process rolls forward automatically at midnight. Passing `--date YYYY-MM-DD`
still pins a date for an intentional soak drill.

The local status API exits with a rollover signal when the date changes; its
PowerShell wrapper relaunches it on the new trading day. The scheduled task also
starts a fresh instance each morning.

Completed `data/live/<date>/ib.log` files are gzip-compressed automatically by
the scheduled morning preflight. The current day's active log is never touched;
archives remain beside the session as `ib.log.gz`.

**KILL one-liners**

```powershell
# Windows
echo. > data\live\KILL
# or session-scoped:
echo. > data\live\YYYY-MM-DD\KILL
```

```bash
# macOS / Linux
touch data/live/KILL
```

### IB Precautionary Settings (TWS / Gateway)

Configure in the brokerage UI on every machine you trade from:

1. **API** — enable socket clients; trusted IP `127.0.0.1`; read-only API **off** for the trading user.
2. **Precautionary** — max order size ≥ pilot (e.g. 5); max daily loss aligned with flatten (~3.25% of configured equity); outside RTH **off** for SPXW.
3. **Market data** — OPRA + US index for live; paper may use delayed only if you accept weaker entry guards.
4. Confirm paper vs live port (7497 / 7496) matches `LiveConfig.mode`.

### Running on another computer

The executor is portable, but each host needs its own local stack. Code alone is not enough:

1. **Repo** — `git pull` on that machine.
2. **Python deps** — same env (`ib_insync`, etc.).
3. **IB Gateway / TWS** — installed, logged into paper or live, API enabled, correct port (7497 paper / 7496 live).
4. **Market data** — OPRA + US index on that IB account (live mode will **not** fall back to delayed).
5. **Signal baselines** — `python scripts/refresh_live_baselines.py` (and VIX calendar if used).
6. **Slack (optional)** — set `SPX_SLACK_WEBHOOK_URL` in that machine’s shell/user environment.
7. **Watchdog** — start `.\scripts\run_live_watchdog.ps1` on the **same** machine as the executor.
8. **TWS precautionary settings** — redo max order size / daily loss / outside-RTH on that install (see above).
9. **One host only** — do not run two executors against the same account at once.

Kill files, heartbeat, and the executor lock live under that machine’s `data/live/` and do **not** sync between PCs (by design). Slack is what notifies you wherever you are.

### Paper soak

See [`scripts/paper_soak_checklist.md`](../scripts/paper_soak_checklist.md). Verify:

```powershell
python scripts/verify_soak_events.py --date YYYY-MM-DD --expect kill,flatten,governor
```

Manual reconcile (dual-scale: paper equity + normalized $13M):

```powershell
python simulator/reconcile_live.py --date YYYY-MM-DD
# → data/live/<date>/reconcile.json
```

## Run (quick reference)

```
python live/ib_executor.py
```

### Key `LiveConfig` defaults (`live/live_config.py`)

| Field | Default | Notes |
|---|---|---|
| `profile` | `p3_poststop_cooldown_120` | Skew 0.65 + flatten 3.25% + put wing 150 + 120min same-side cooldown |
| `use_streaming_quotes` | `True` | Set `False` to revert to per-poll snapshots |
| `use_adaptive_polling` | `True` | Set `False` to use fixed `poll_seconds` |
| `poll_seconds_active` | `1.5` | Open positions |
| `poll_seconds_near_stop` | `0.75` | Short ask ≥ 80% of stop |
| `market_data_type` | `1` | Live OPRA; use `3` on paper without subs |
| `stop_limit_slippage_pct` | `0.05` | Limit stop buffer above ask |
| `entry_limit_concession` | `0.05` | Haircut from natural credit on combo limit |
| `entry_work_seconds` | `870` | How long to work an entry before `entry_unfilled` |
| `entry_ladder_step` | `0.05` | Extra concession per ladder step (every 60s) |
| `entry_require_live_nbbo` | `True` | Reject crossed/missing NBBO on entry |

**Delayed fallback (paper only):** `market_data_type=3`, `auto_fallback_delayed=True`,
`delayed_quote_fallback=True`, `entry_require_live_nbbo=False`.
**Live mode never auto-falls back to delayed.**

Enable portfolio allocator when sizing up: `use_portfolio_allocator_live=True`.

Backtest parity: production profile includes `entry_fill_slippage=0.05` matching
`entry_limit_concession`.

## Validation

```
python live/test_risk_gates.py
python live/test_loop_timing.py
python live/test_entry_execution.py
python live/test_ib_order_hygiene.py
python live/test_session_recovery.py
python live/test_kill_switch.py
python live/test_account_guards.py
python live/test_ib_connection.py
python live/test_mark_book.py
python live/test_flatten_confirm.py
python live/test_stale_quotes.py
python live/test_open_risk_caps.py
python live/test_slack_notify.py
python live/test_heartbeat_watchdog.py
python live/test_live_entry_risk.py
python simulator/test_profile_regression.py
python simulator/test_live_signal_parity.py
python simulator/reconcile_live.py --date YYYY-MM-DD
```
