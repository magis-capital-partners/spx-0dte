# SPX 0DTE — Production Strategy, Backtest & Live Execution

Research and production stack for SPXW same-day vertical credit spreads: historical
backtest, performance dashboard, and Interactive Brokers paper/live executor. All
three share one canonical config in `simulator/profiles.py`.

## Production strategy (July 2026 — Wave 2 Calmar)

**Profile:** `p3_poststop_cooldown_120` (`simulator/profiles.py::build_p3_poststop_cooldown_config`)
**Sizing:** `linear_decay_downsize`
**Account reference:** $13M (backtest/dashboard); $500k pilot (live paper default, hard-capped
at 2 contracts/tranche regardless of the size formula below)

| Parameter | Value |
|-----------|--------|
| Put / call wings | **150 / 75** pt |
| Bear-call gates | skip if `trend_score > 1.0` or `skew_z > 0.65` |
| Stops | 3.0× short leg, **120s** sustained confirm (live + backtest) + **4.5×** native broker STP backstop — see [Stop-order mechanics](#stop-order-mechanics-exact) |
| Entry / stop fill | **$0.05** entry slip; **$0.25** stop slip; **$1.25**/contract fee |
| Daily halt | −2.25% MTM (no new entries) |
| Flatten | −3.25% MTM (close all) |
| Post-stop cooldown | **120 min**, same side only |
| VIX | skip session if open **> 35**; **1.25×** size if open **25–35** |

**Dashboard comparison run:** `p3_trend_bc_085` (trend gate 0.85 instead of 1.0) —
better risk shape in backtest; **not** used for live.

Wave 2 campaign write-up: [`overnight_calmar_wave2_results_2026-07-10.md`](overnight_calmar_wave2_results_2026-07-10.md)

Eligible-calendar OOS backtest with realism package (through 2026-07-27):
~**16.3% CAGR**, Calmar **~1.46**, max DD **~11.2%** (was ~19.9% / ~2.3 / ~8.6%
before entry-slip P&L fix, 120s stop clock, $0.25 stop slip, and $1.25 fees).

### What the strategy actually does, in full

Every trading day the executor repeatedly opens **same-day-expiry (0DTE) SPXW vertical
credit spreads** — a bull put spread and, independently, a bear call spread — plus an
optional short iron condor overlay. Nothing is held overnight; every position either
expires worthless, gets stopped, or is flattened by the close.

**Entry cadence.** New tranches are evaluated every **15 minutes** from **09:32** to
**15:30 ET** (`entry_interval_minutes=15`, `entry_start`/`entry_end` in
`simulator/mbh_simulator.py`). Each eligible tranche independently considers a put-side
vertical and a call-side vertical.

**Strike selection (per vertical, each tranche).**
1. Short strike: the option nearest **20-delta** (`target_abs_delta=0.20`) on the
   relevant side (puts for the bull-put spread, calls for the bear-call spread).
2. Long strike (the wing): a **fixed point-distance** from the short strike, not a
   delta target — **150 pts** for puts, **75 pts** for calls
   (`put_wing_width` / `call_wing_width`, `_wing_params_for_side` in
   `simulator/mbh_simulator.py`). The long wing is what bounds max loss on the spread
   regardless of what happens to the short leg — this is independent of, and much wider
   than, the short-leg stop discussed below.

**Entry gates (bear-call side only).** A call-side vertical is skipped for that tranche
if `trend_score > 1.0` or `skew_z > 0.65` — i.e., the strategy declines to sell calls
into strong upward trend or unfavorable skew conditions. The put side has no equivalent
gate in the production profile.

**Iron condor overlay.** Independently, up to **one** short iron condor per day
(`condor_max_entries_per_day=1`) between **10:00–15:00 ET**, sized at **10 contracts**
at the $13M reference book (scales with account size via `condor_size_fraction`), **50pt**
wings, target **|Δ| ≈ 0.16** (accepted range 0.12–0.20), only when **VIX open ≥ 15**, and
blocked entirely during `tariff_shock`/`tariff_reversal` event-classified sessions.

**FOMC days.** No new entries of any kind after **13:30 ET** on FOMC decision days
(`use_fomc_entry_cutoff`, `fomc_entry_end`).

**Sizing.** Each tranche's contract count is the **31-contract baseline** (at $13M),
scaled by three independent multipliers:
- **Time-of-day** (`linear_decay_downsize` schedule, `simulator/profiles.py`): 1.25× before
  10:30, 1.00× 10:30–11:30, 0.85× 11:30–12:30, 0.60× 12:30–13:30, 0.45× 13:30–14:30,
  0.25× after 14:30 — size front-loads into the morning and steps down through the day
  as 0DTE gamma risk into the close rises.
- **VIX-elevated upsize:** 1.25× when VIX opens 25–35 (highest historical per-trade
  expectancy band).
- **VIX skip:** the entire session is skipped (no entries at all) when VIX opens **> 35**.

The $500k live/paper pilot additionally hard-caps every tranche at **2 contracts**
(`contracts_per_tranche` / `max_contracts_per_tranche` in `live/live_config.py`) —
none of the scaling above can push a live tranche above that cap; it only affects the
$13M backtest/dashboard reference book.

**Risk governors (whole-book, independent of any single position's stop).**
- **Daily halt at −2.25% MTM:** no new entries for the rest of the session; existing
  open positions continue to be managed and stopped normally.
- **Daily flatten at −3.25% MTM:** every open position is force-closed at market.
- **Post-stop cooldown:** after a stop fires on a put or call spread, new entries on
  **that same side only** are blocked for **120 minutes**; the opposite side and the
  condor overlay are unaffected.

**Realism costs** (identical in backtest and live, so the reported CAGR reflects what
live execution should actually experience): **$0.05**/contract entry slippage off the
natural mid-credit, **$0.25**/contract stop-fill slippage, **$1.25**/contract all-in
IB fee.

---

## Stop-order mechanics (exact)

Every open short option leg is protected by **two independent stop layers** running at
once — a software-managed "synthetic" stop that is the primary risk control, and a
real order resting at the broker as a disaster backstop. They are deliberately set at
different trigger levels so the wider one can never race the tighter one under normal
conditions.

### Layer 1 — synthetic stop (primary; expected to fire almost every time)

This is **not** a pre-placed broker order. It is a monitoring loop
(`manage_stops` in `live/ib_executor.py`) that watches the short leg's live ask and
submits a closing order reactively, only once its trigger + confirmation logic fires.

- **Trigger:** short-leg ask ≥ **3.0×** the credit that leg was sold for
  (`stop_multiple=3.0`).
- **Confirmation (breach must persist, not just tick through, before anything is
  submitted)** — tiered by breach severity:
  - **Standard:** ask stays ≥ stop price continuously for **120 seconds**.
  - **Fast tier:** if ask ≥ **1.10×** the stop price, the window shortens to **20
    seconds**.
  - **Immediate tier — no wait at all:** if ask ≥ **1.30×** the stop price (≈3.9× the
    original entry credit) **or** the SPX underlying itself trades through the short
    strike, the order is submitted immediately.
  - The confirmation clock only advances while the short-leg quote is fresh (≤5s old)
    and the IB connection is healthy; a stale/frozen quote or an IB/TWS disconnect
    **pauses** the clock instead of letting it run against a frozen price (added after
    a 2026-08-04 session where $690 of stop drag accrued from the clock running through
    seven silent disconnects). Each loop iteration can credit at most 10 seconds toward
    the window, so a stalled loop cannot "catch up" and complete a 120s confirmation in
    one jump.
- **Execution once confirmed:** first a **limit order** to buy back the short leg at
  the current ask plus a slippage buffer (max of ask+5% or ask+$0.25), given 3 seconds
  to fill; if it doesn't fill in time it is cancelled and replaced with a **market
  order** as the guaranteed-fill fallback.
- **Net effect:** the synthetic stop is reactive, not resting. It depends on the
  executor process being alive, connected, and polling.

### Layer 2 — native broker-side STP order (the "4.5×" backstop)

**Yes — this one is placed in advance, the moment a position exists, specifically so
it doesn't depend on the local process staying alive.**

- The instant an entry fills, the executor immediately submits a real order to IB:
  `StopOrder("BUY", qty, stop_price)` — IB's plain **STP** order type, *not* a
  stop-limit (`place_or_replace_native_stop_for_short` in `live/ib_executor.py`). It
  sits working at the broker for the rest of the session (`tif="DAY"`).
- **Trigger price:** entry credit × **4.5** (`native_stop_multiple=4.5`) — deliberately
  wider than the synthetic stop's 3.0×/3.9× levels, by design, "so native cannot race
  synthetic." Its job is to catch the position **only if the synthetic layer never gets
  the chance to act** (process crash, lost connection, laptop asleep, etc.), not to be
  the everyday risk control.
- **Why a plain STP and not a stop-limit:** a plain STP converts to a **market order**
  the instant the trigger price trades, with no separate limit price that could fail to
  match. That is the specific design choice that makes fill highly likely — there's
  nothing for the order to "miss."
- **Kept alive continuously, not "fire and forget":** every ~30 seconds
  (`native_stop_verify_seconds`) the executor confirms the order is still working at IB
  and re-arms it if it's missing (IB cancellation, TWS restart, etc.). It is also
  cancelled and re-submitted as one aggregated order any time position size on that
  short strike changes (a new tranche added to the same strike, a partial fill, etc.),
  so there is always exactly one correctly-sized STP per open short strike.

### So — is there "basically no risk it won't get filled"? Mostly, but not absolutely.

Fill **probability** is high by deliberate design: a real resting order at the broker,
of the order type that has no limit price to miss, continuously verified and re-armed.
That said, three real gaps exist and are worth knowing:

1. **Brief disarm windows.** When a new tranche scales into an *existing* position on
   the same short strike, the native STP is cancelled and not yet replaced for up to
   **45 seconds** (`native_stop_disarm_max_seconds`) while the new fill is validated.
   During that window there is no broker-side stop on that leg — only the synthetic
   loop, if it's alive.
2. **IB can reject the order outright** (margin, contract restriction, etc.). The
   executor logs `native_stop_rejected` and does **not** retry indefinitely — it falls
   back to relying on the synthetic stop alone for that leg.
3. **Fill *price* is not guaranteed, only fill *existence*.** A market order fills, but
   in a genuinely thin/gapping 0DTE options tape (a flash move, an SPX trading halt, a
   liquidity air-pocket around a scheduled event) it can fill materially worse than the
   4.5× trigger. High fill certainty is not the same as bounded loss at exactly 4.5×.

In short: the 4.5× native STP is a real, pre-placed, continuously-verified broker-side
order chosen specifically to maximize execution certainty if the software dies — but it
is the *secondary* backstop, not the primary risk control (that's the 3.0× synthetic
stop above), and "very likely to fill" is a more honest description than "no risk."

---

## Quick start — what to run

### Live / paper trading (Interactive Brokers)

1. **Each morning** (from repo root):

   ```powershell
   python scripts/refresh_live_baselines.py
   python scripts/download_vix_daily.py
   ```

   These refreshes are scheduled automatically at 9:00 AM on SPX trading days.
   The watchdog and dashboard-status services also start automatically; the executor
   itself is never scheduled.

2. **Log into paper TWS or IB Gateway** (port **7497**). Requires SPX index + **OPRA**
   subscriptions for real-time quotes.

3. **Run the executor** (no CLI flags — config is in `live/live_config.py`):

   ```powershell
   python live/ib_executor.py
   ```

   Confirm startup log includes `wings=put150/call75` and
   `profile=p3_poststop_cooldown_120`.

   The executor automatically mirrors this manual command's console output to
   `data/live/<YYYY-MM-DD>/executor-console.log`, which is the stream shown in
   the local dashboard's **Executor stdout** panel.

4. **Session logs:** `data/live/<YYYY-MM-DD>/` (`fills.jsonl`, `tranches.jsonl`,
   `config.json`, `ib.log`).

5. **After the close:**

   ```powershell
   .\scripts\daily_data_update.ps1
   python simulator/reconcile_live.py --date YYYY-MM-DD
   .\scripts\sync_dashboard.ps1 -Deploy
   ```

Full IB setup, restart safety, and config table: **[`live/README.md`](live/README.md)**

**Real-money live** is still off by default (`allow_live: false`, `mode: "paper"`).
Run more paper sessions and reconcile against backtest before enabling live.

### Dashboard (backtest + paper fills)

- **Live site:** https://magis-capital-partners.github.io/spx-0dte/
- **Rebuild locally:**

  ```powershell
  .\scripts\sync_dashboard.ps1
  ```

- **Rebuild + publish to GitHub Pages:**

  ```powershell
  .\scripts\sync_dashboard.ps1 -Deploy
  ```

Details: [`docs/README.md`](docs/README.md), [`DASHBOARD.md`](DASHBOARD.md)

Primary backtest line on the chart: **Production — put 150 + FOMC 13:30 + IC10 Δ0.16 (VIX≥15)**.  
Stat cards and strategy guide use `p3_poststop_cooldown_120`. Second line:
**Trend BC 0.85** comparison.

### Historical backtest (local cache, no API unless filling gaps)

Read **`data/inventory/manifest.json`** before downloading. Do **not** re-download
ThetaData unless you are explicitly filling missing dates.

```powershell
# Export dashboard presets (incremental after first full run)
python simulator/export_dashboard_run.py --preset p3_poststop_cooldown_120 --incremental
python simulator/export_dashboard_run.py --preset p3_trend_bc_085 --incremental

# Optional: override settlement SPX for a specific day
python simulator/export_dashboard_run.py --preset p3_poststop_cooldown_120 --incremental --settlement-spot 2026-07-10=7575.39

python docs/build_dashboard_data.py --primary-run-id p3_poststop_cooldown_120 --include-live
```

Daily catch-up (download → build → export → optional deploy):

```powershell
$env:THETADATA_API_KEY = "..."
.\scripts\daily_data_update.ps1 -Deploy
```

---

## Repository layout

| Path | Purpose |
|------|---------|
| [`simulator/profiles.py`](simulator/profiles.py) | **Single source of truth** for strategy configs (backtest + live) |
| [`simulator/mbh_simulator.py`](simulator/mbh_simulator.py) | Core day simulator |
| [`simulator/export_dashboard_run.py`](simulator/export_dashboard_run.py) | Export runs to `data/dashboard_runs/` |
| [`live/live_config.py`](live/live_config.py) | Live/paper knobs (`ACTIVE` dataclass) |
| [`live/ib_executor.py`](live/ib_executor.py) | IB session loop (entries, stops, flatten, VIX gate) |
| [`live/strategy_profiles.py`](live/strategy_profiles.py) | Resolves `LiveConfig` → `StrategyConfig` |
| [`docs/build_dashboard_data.py`](docs/build_dashboard_data.py) | Builds `docs/data/dashboard_data.json` |
| [`docs/index.html`](docs/index.html) | Static dashboard UI |
| [`data/processed/`](data/processed/) | Cached SPXW quotes + signals (local only) |
| [`data/dashboard_runs/`](data/dashboard_runs/) | Exported backtest CSVs per preset |
| [`data/live/`](data/live/) | Paper/live session JSONL logs |
| [`scripts/overnight_calmar_variants.py`](scripts/overnight_calmar_variants.py) | Wave 2 variant registry |
| [`scripts/run_overnight_calmar_suite.py`](scripts/run_overnight_calmar_suite.py) | Overnight Calmar batch runner |

Data cache policy: [`data/README.md`](data/README.md)

---

## Validation

```powershell
python simulator/test_profile_regression.py
python live/test_risk_gates.py
python live/test_session_recovery.py
python live/test_entry_execution.py
python simulator/reconcile_live.py --date YYYY-MM-DD
```

---

## Research history

This repo began as an MBH strategy reconstruction exercise (see
`strategy_reconstruction_spec.md`, `implementation_status_2026-06-20.md`). The
current production path is the **3D flatten + trend/skew gates + time-of-day sizing**
line, refined through stop calibration (June 2026), improvement-plan tests, and
**Overnight Calmar Wave 1** (skew 0.65, flatten −3.25%) and **Wave 2** (put wing 150).

Older calibration scripts (`calibration_grid.py`, `walk_forward_grid.py`, etc.) remain
for research; day-to-day operations use the flow above.

---

## ThetaData

API key: `THETADATA_API_KEY` (environment variable — never commit).

```powershell
$env:THETADATA_API_KEY = "..."
python simulator/backfill_history.py --download --build --start-date 2026-07-10 --end-date 2026-07-10
python scripts/update_data_inventory.py
```

Only download when manifest shows gaps and you intentionally want to spend credits.
