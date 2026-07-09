# Live Execution (Interactive Brokers)

Production SPX 0DTE executor with **streaming quotes**, **adaptive polling**, and
**limit-then-MKT stops**. Strategy logic matches `p3_poststop_cooldown_120` in
`simulator/profiles.py` (skew gate **0.65**, flatten **−3.25%**, trend/skew gates,
**120-minute same-side stop cooldown** after any stopped spread).

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

```
python scripts/refresh_live_baselines.py
```

## Run

```
python live/ib_executor.py
```

### Key `LiveConfig` defaults (`live/live_config.py`)

| Field | Default | Notes |
|---|---|---|
| `profile` | `p3_poststop_cooldown_120` | Skew 0.65 + flatten 3.25% + 120min same-side cooldown |
| `use_streaming_quotes` | `True` | Set `False` to revert to per-poll snapshots |
| `use_adaptive_polling` | `True` | Set `False` to use fixed `poll_seconds` |
| `poll_seconds_active` | `1.5` | Open positions |
| `poll_seconds_near_stop` | `0.75` | Short ask ≥ 80% of stop |
| `market_data_type` | `1` | Live OPRA; use `3` on paper without subs |
| `stop_limit_slippage_pct` | `0.05` | Limit stop buffer above ask |
| `entry_limit_concession` | `0.05` | Haircut from natural credit on combo limit |
| `entry_work_seconds` | `870` | How long to work an entry before `entry_unfilled` |
| `entry_ladder_step` | `0.05` | Extra concession per ladder step (every 60s) |
| `entry_require_live_nbbo` | `False` | Set `True` with OPRA subs |

Paper without OPRA (current): `market_data_type=3`, `delayed_quote_fallback=True`,
`entry_require_live_nbbo=False`.

**Paper with OPRA** (switch when ready):

```python
market_data_type = 1
auto_fallback_delayed = False
delayed_quote_fallback = False
entry_require_live_nbbo = True
```

Backtest parity: production profile includes `entry_fill_slippage=0.05` matching
`entry_limit_concession`.

## Validation

```
python live/test_risk_gates.py
python live/test_loop_timing.py
python live/test_entry_execution.py
python live/test_ib_order_hygiene.py
python simulator/test_profile_regression.py
python simulator/test_live_signal_parity.py
python simulator/reconcile_live.py --date YYYY-MM-DD
```
