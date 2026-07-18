# Live Execution (Interactive Brokers)

Production SPX 0DTE executor with **streaming quotes**, **adaptive polling**, and
**limit-then-MKT stops**. Strategy logic matches `p3_poststop_cooldown_120` in
`simulator/profiles.py` (skew gate **0.65**, flatten **−3.25%**, put wing **150** /
call wing **75**, trend/skew gates, **120-minute same-side stop cooldown** after
any stopped spread).

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

Also cancels orphan working SPXW/BAG orders from a crashed prior run. Do **not** start a second executor mid-session; if you must restart, use one process only and let recovery reload the book.

### Operator safeties

| Control | Behavior |
|---------|----------|
| **KILL file** | Create `data/live/KILL` or `data/live/<date>/KILL` — executor flattens (with fill confirm) and exits. Present at startup → refuse to start until removed. Portable: works on whichever host runs the session. |
| **Account overlay** | PnL halt/flatten still use configured `account_equity`. Startup requires IB NetLiq ≥ equity and BuyingPower ≥ 15% of equity. Loop halts entries if NetLiq < 90% of equity. Pre-entry BP check vs estimated margin. |
| **Disconnect breaker** | On IB disconnect: halt entries, reconnect with backoff (default 120s budget), re-arm native STPs, re-verify book. Failure with open risk → confirmed flatten then exit. |
| **Mark integrity** | Missing quotes on open risk → halt entries (never treat as $0 PnL). Unavailable marks for 60s → flatten. |
| **Stale quotes** | 3 consecutive polls with short-leg age >20s (10s near stop) → **halt entries only** (never flatten on stale alone). |
| **Open-risk caps** | Max 6 open contracts / 3 per side / 2 same strike (live overlay). |
| **Live stop caps** | Max 2 stops/side and 4/day via `entry_risk_block_reason` (profile 999 overridden). |
| **Flatten confirm + audit** | MKT close waits for fill; `flatten_audit` checks IB flat afterward. |
| **Stop confirm** | After synthetic stop fill, verify IB short qty dropped; else `stop_unconfirmed` and keep managing. |
| **Live mode data** | `mode=live` forces `auto_fallback_delayed=False` (no silent delayed downgrade). |

### Slack + local watchdog (portable across machines)

Kill/watchdog stay **local to the host running the executor**. Slack reaches any phone/PC.

```powershell
$env:SPX_SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/XXX/YYY/ZZZ"
python live/ib_executor.py
# Same machine, second terminal:
.\scripts\run_live_watchdog.ps1
# Optional: write session KILL if heartbeat dies with open risk
.\scripts\run_live_watchdog.ps1 -WriteKill
```

Heartbeat: `data/live/<date>/heartbeat.json` (updated every `heartbeat_seconds`).

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
