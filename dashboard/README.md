# SPX 0DTE Dashboard

Static React dashboard for backtest runs, MBH benchmark comparison, and (when wired) live IB fills.

## Live URL

| Repo visibility | URL |
|---|---|
| **Public** (same as [etf-dashboard](https://magis-capital-partners.github.io/etf-dashboard/)) | `https://magis-capital-partners.github.io/spx-0dte/` |
| **Private** (current) | Org-only random subdomain, e.g. `https://….pages.github.io/` — see **Settings → Pages** in the repo |
| **Custom domain** (optional) | e.g. `spx0dte.yourdomain.com` — works with private or public; add DNS + **Settings → Pages → Custom domain** |

The repo is **private**, so GitHub does not assign the short `org.github.io/repo-name/` path. To get the same pattern as etf-dashboard, either make this repo **public** or attach a **custom domain**.

## Deployment: Actions (recommended) vs gh-pages branch

| Method | When to use |
|---|---|
| **GitHub Actions** (recommended) | Same model as etf-dashboard. Deploys automatically when `dashboard/**` changes on `main`. Requires Pages source = **GitHub Actions** and the workflow in `.github/workflows/deploy-dashboard.yml`. |
| **gh-pages branch** (legacy fallback) | Manual only: `.\dashboard\deploy_pages.ps1` after rebuilding data. Use if the GitHub token lacks the `workflow` scope and Actions cannot be pushed yet. |

**Recommendation:** use **Actions**, not “deploy from main branch.” The app lives in `dashboard/`; `main` holds the simulator and live code. Actions publish only `dashboard/` as the site artifact (etf-dashboard builds `_site` the same way).

One-time setup for Actions:

```powershell
gh auth refresh -h github.com -s workflow
git add .github/workflows/deploy-dashboard.yml
git commit -m "Enable Actions-based Pages deploy"
git push
```

Then in the repo: **Settings → Pages → Build and deployment → Source: GitHub Actions**.

## Local dev

```powershell
python dashboard/build_dashboard_data.py `
  --run "best=data/profile_best:2x deploy + flatten" `
  --account-equity 13000000
cd dashboard
python -m http.server 8000
# open http://localhost:8000
```

## Refresh data after backtests

```powershell
.\run_profiles.ps1   # reruns profiles + rebuilds dashboard/data/dashboard_data.json
git add dashboard/data/dashboard_data.json
git commit -m "dashboard: refresh backtest data"
git push             # Actions deploys if workflow is enabled
```
