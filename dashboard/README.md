# SPX 0DTE Dashboard

Static React dashboard for backtest runs, MBH benchmark comparison, and (when wired) live IB fills.

## Live URL

**https://magis-capital-partners.github.io/spx-0dte/**

- **Repo:** private (`magis-capital-partners/spx-0dte`)
- **Pages:** public (Enterprise allows public Pages on a private repo)
- **Login:** optional password gate via `data/investors.json` (see [DASHBOARD.md](../DASHBOARD.md))

## Deployment

**GitHub Actions** (recommended). Pushes to `main` that touch `dashboard/**` trigger `.github/workflows/deploy-dashboard.yml`.

One-time: **Settings → Pages → Build and deployment → Source: GitHub Actions**.

```powershell
.\scripts\sync_dashboard.ps1 -Push
```

Legacy fallback (no Actions): `.\dashboard\deploy_pages.ps1` force-pushes the `gh-pages` branch.

## Local dev

```powershell
python dashboard/build_dashboard_data.py
cd dashboard
python -m http.server 8000
# open http://localhost:8000
```

## Refresh data after backtests

Full historical run (local cache, no ThetaData download):

```powershell
python simulator/export_dashboard_run.py --preset p3_trend1_skew075
python dashboard/build_dashboard_data.py
# or: .\scripts\sync_dashboard.ps1
```

Deploy to Pages:

```powershell
.\scripts\sync_dashboard.ps1 -Push
```
