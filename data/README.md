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

## Coverage notes

- ThetaData same-day SPXW Greeks are **not** available before ~**2019** (2016–2018 are mostly empty).
- Historical backtests use the expiration-era calendar in `data/calendar/spxw_era_rules.json`
  (Mon/Wed/Fri until Apr/May 2022, then all weekdays).
- Backtest outputs (trades, daily P&L) go under `data/historical_3d_*` and `data/dashboard_runs/`.

## Backup

Because `data/*` is gitignored, back up this folder separately if you move machines
(external drive, cloud sync, etc.). Losing `data/raw` + `data/processed` means
re-downloading from ThetaData.
