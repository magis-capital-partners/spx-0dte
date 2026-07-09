# Local SPXW Data Cache

**Agents: read `data/inventory/manifest.json` before any ThetaData download.**

This project keeps a **local copy** of SPXW option-chain history so backtests can be
re-run without paying for ThetaData again. The heavy files live here on disk; they
are **not** committed to git (see `.gitignore`).

## Layout

| Path | Contents | Backtest uses? |
|------|----------|----------------|
| `data/raw/thetadata/symbol=SPXW/date=YYYY-MM-DD/` | Parquet Greeks (0DTE + next expiry) | Rebuild only |
| `data/processed/symbol=SPXW/date=YYYY-MM-DD/` | `normalized_option_quotes.csv`, `signals.csv` | **Yes** |
| `data/processed/.../signals_unconditional.csv` | Rolling-baseline z-scores | Written during backtest |
| `data/inventory/manifest.json` | Date ranges, counts, gaps (tracked in git) | Agent checklist |
| `data/calendar/spxw_era_rules.json` | Mon/Wed/Fri vs 5-day expiration eras | Historical backtest |
| `data/calendar/vix_daily.csv` | Daily ^VIX OHLC from Yahoo Finance (free) | VIX regime tests |
| `data/calendar/spx_daily.csv` | Daily ^GSPC OHLC (Yahoo) | Market-factor / beta analysis |
| `data/calendar/ixic_daily.csv` | Daily ^IXIC OHLC (Yahoo) | Market-factor / beta analysis |
| `data/calendar/rut_daily.csv` | Daily ^RUT OHLC (Yahoo) | Market-factor / beta analysis |

## VIX daily (free external source)

VIX is **not** available on your ThetaData Index (Free) tier. We use Yahoo Finance
`^VIX` daily bars instead — same-day **open** is written to `signals.csv` as `vix`
for regime bucketing and sizing experiments.

```powershell
# Download / refresh calendar (no API key)
python scripts/download_vix_daily.py --start-date 2019-01-01

# Stamp vix into every processed signals.csv
python simulator/vix_signal_enricher.py --symbol SPXW

# Verify coverage
python scripts/validate_vix_coverage.py

# Run VIX sizing variants (production path)
python scripts/run_vix_regime_tests.py
```

`vix` is constant within a trading day (daily open proxy). Intraday VIX moves are
not modeled; use IB live stream for execution-time parity.

## Index calendars + market-factor analysis

```powershell
# Refresh SPX / NASDAQ Composite / RUT daily bars (no API key)
python scripts/download_index_daily.py --start-date 2019-01-01

# Covariance, betas, rolling, VIX regimes, PCA, hedge ratios, etc.
python scripts/analyze_market_covariance.py --preset p3_poststop_cooldown_120 --equity 13000000
# → data/analysis/market_covariance_<preset>_<end>.json
```

`daily_data_update.ps1` refreshes these calendars alongside VIX.

## Default agent behavior

1. **Read** `data/inventory/manifest.json`.
2. **Run backtests** from `data/processed/` — no API key required.
3. **Do not download** unless the user explicitly asks to fill gaps listed in the manifest.

```powershell
# Re-run historical 3D backtest (no ThetaData)
python simulator/historical_3d_backtest.py --start-date 2019-01-02 --end-date 2025-12-29

# Refresh manifest after local changes
python scripts/update_data_inventory.py
```

## Rebuild processed from raw (no download)

If you change `feature_builder.py` but raw parquet is already on disk:

```powershell
python simulator/backfill_history.py --build --start-date 2019-01-01 --end-date 2025-12-29
python simulator/feature_enricher.py --symbol SPXW
python scripts/update_data_inventory.py
```

## Download (user-requested only)

Only when manifest shows missing dates **and** the user wants to pay for ThetaData:

```powershell
$env:THETADATA_API_KEY = "..."   # never commit
python simulator/backfill_history.py --download --build --start-date YYYY-MM-DD --end-date YYYY-MM-DD
python scripts/update_data_inventory.py
```

Year-sized chunks: `scripts/run_historical_backfill_by_year.ps1`

## Daily automation (local Task Scheduler — no GitHub Actions)

GitHub Actions minutes are not required. Run catch-up on this PC after the close, then
push `docs/` for GitHub Pages (branch deploy uses **no** Actions minutes).

```powershell
# One-time: save API key as a User env var + install weekday 6:30 PM task (with Pages deploy)
.\scripts\install_daily_update_task.ps1 -ApiKey "td1_..." -Deploy

# Manual catch-up (e.g. fill 7/7–7/8)
$env:THETADATA_API_KEY = "..."   # if not already in User env
.\scripts\daily_data_update.ps1 -StartDate 2026-07-07 -EndDate 2026-07-08 -Deploy

# What the daily job does:
#   1. ThetaData download + build for missing dates in a short lookback window
#   2. VIX + SPX/IXIC/RUT calendar refresh + feature/VIX enrich for new days
#   3. Incremental dashboard run export (append new OOS days only)
#   4. Reconcile any data/live/<date> paper sessions in the window
#   5. Rebuild docs/data/dashboard_data.json with --include-live (+ optional Pages deploy)
```

Logs: `data/logs/daily_update_*.log`. Uninstall: `.\scripts\install_daily_update_task.ps1 -Uninstall`.

After strategy/config changes, force a full re-export once:
`.\scripts\daily_data_update.ps1 -SkipDownload -FullExport -Deploy`

## Coverage notes

- ThetaData same-day SPXW Greeks are **not** available before ~**2019** (2016–2018 are mostly empty).
- Historical backtests use the expiration-era calendar in `data/calendar/spxw_era_rules.json`
  (Mon/Wed/Fri until Apr/May 2022, then all weekdays).
- Backtest outputs (trades, daily P&L) go under `data/historical_3d_*` and `data/dashboard_runs/`.

## Backup

Because `data/*` is gitignored, back up this folder separately if you move machines
(external drive, cloud sync, etc.). Losing `data/raw` + `data/processed` means
re-downloading from ThetaData.
