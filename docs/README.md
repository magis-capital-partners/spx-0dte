# SPX 0DTE Dashboard

Static React dashboard for backtest runs, MBH benchmark comparison, and (when wired) live IB fills.

## Live URL

**https://magis-capital-partners.github.io/spx-0dte/**

- **Repo:** private (`magis-capital-partners/spx-0dte`)
- **Pages:** public (Enterprise allows public Pages on a private repo)
- **Login:** optional password gate via `data/investors.json` (see [DASHBOARD.md](../DASHBOARD.md))

## Deployment

**GitHub Pages** deploys from the **`/docs`** folder on **`main`**. In Settings → Pages, choose branch **`main`** and folder **`/docs`** (GitHub only offers `/` or `/docs`, not `/dashboard`).

```powershell
# Rebuild only
.\scripts\sync_dashboard.ps1

# Rebuild + deploy to GitHub Pages
.\scripts\sync_dashboard.ps1 -Deploy
```

Legacy fallback (no Actions): `.\docs\deploy_pages.ps1` force-pushes the `gh-pages` branch.

## Local dev

```powershell
python docs/build_dashboard_data.py
cd docs
python -m http.server 8000
# open http://localhost:8000
```

## Refresh data after backtests

Full historical run (local cache, no ThetaData download):

```powershell
python simulator/export_dashboard_run.py --preset p3_poststop_cooldown_120
python simulator/export_dashboard_run.py --preset p3_trend_bc_085
python docs/build_dashboard_data.py --primary-run-id p3_poststop_cooldown_120
# or: .\scripts\sync_dashboard.ps1
```

Deploy to Pages:

```powershell
.\scripts\sync_dashboard.ps1 -Deploy
```
