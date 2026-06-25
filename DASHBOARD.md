# Dashboard (separate private repo)

The interactive site lives in **[magis-capital-partners/spx-0dte-dashboard](https://github.com/magis-capital-partners/spx-0dte-dashboard)** — not in this repo.

| Repo | Contents | Visibility |
|------|----------|------------|
| **spx-0dte** (this repo) | Simulator, live IB executor, research | Private |
| **spx-0dte-dashboard** | Static React dashboard + backtest JSON | Private + password login |

## Refresh dashboard data after backtests

```powershell
.\scripts\sync_dashboard.ps1 -Push
```

## Enterprise access

1. **GitHub Enterprise SSO** — grant read access to `spx-0dte-dashboard`; private Pages requires org login.
2. **Dashboard password** — add users via `hash_investor_password.py` in the dashboard repo (see its README).
3. **Custom domain** (optional) — Settings → Pages on the dashboard repo for a clean URL while staying private.

Local dashboard sources under `dashboard/` are kept for building only; deploy from **spx-0dte-dashboard**.
