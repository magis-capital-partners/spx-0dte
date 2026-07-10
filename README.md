# SPX 0DTE — Production Strategy, Backtest & Live Execution

Research and production stack for SPXW same-day vertical credit spreads: historical
backtest, performance dashboard, and Interactive Brokers paper/live executor. All
three share one canonical config in `simulator/profiles.py`.

## Production strategy (July 2026 — Wave 2 Calmar)

**Profile:** `p3_poststop_cooldown_120`  
**Sizing:** `linear_decay_downsize`  
**Account reference:** $13M (backtest/dashboard); $500k pilot (live paper default)

| Parameter | Value |
|-----------|--------|
| Put / call wings | **150 / 75** pt |
| Bear-call gates | skip if `trend_score > 1.0` or `skew_z > 0.65` |
| Stops | 3.0× short leg, 2-bar confirm |
| Daily halt | −2.25% MTM (no new entries) |
| Flatten | −3.25% MTM (close all) |
| Post-stop cooldown | **120 min**, same side only |
| VIX | skip session if open **> 35**; **1.25×** size if open **25–35** |

**Dashboard comparison run:** `p3_trend_bc_085` (trend gate 0.85 instead of 1.0) —
better risk shape in backtest; **not** used for live.

Wave 2 campaign write-up: [`overnight_calmar_wave2_results_2026-07-10.md`](overnight_calmar_wave2_results_2026-07-10.md)

Eligible-calendar OOS backtest (through 2026-07-10, SPX settle override where noted):
~**19.9% CAGR**, Calmar **~2.32**, max DD **~8.6%**.

---

## Quick start — what to run

### Live / paper trading (Interactive Brokers)

1. **Each morning** (from repo root):

   ```powershell
   python scripts/refresh_live_baselines.py
   python scripts/download_vix_daily.py
   ```

2. **Log into paper TWS or IB Gateway** (port **7497**). Requires SPX index + **OPRA**
   subscriptions for real-time quotes.

3. **Run the executor** (no CLI flags — config is in `live/live_config.py`):

   ```powershell
   python live/ib_executor.py
   ```

   Confirm startup log includes `wings=put150/call75` and
   `profile=p3_poststop_cooldown_120`.

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

Primary backtest line on the chart: **Production optimal — put wing 150 (Wave 2)**.  
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
