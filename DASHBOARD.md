# Dashboard (in this repo)

The interactive site is deployed from the **`dashboard/`** folder on **`main`** (GitHub Pages → branch deploy).

| Layer | What it does |
|-------|----------------|
| **Private repo** | Simulator, research, and live code stay private. Only the static `dashboard/` folder is published. |
| **Public GitHub Pages** | Clean URL: **https://magis-capital-partners.github.io/spx-0dte/** (Enterprise: repo private, Pages public). |
| **`data/investors.json` login** | App-level password gate (PBKDF2, same scheme as etf-dashboard). Required when `users` is non-empty. |

> Layer 2 is a UI gate; JSON files remain fetchable if someone bypasses login. For stronger control, use org repo ACLs or a custom domain with Cloudflare Access.

## Refresh + deploy

```powershell
.\scripts\sync_dashboard.ps1 -Push
```

Rebuilds `dashboard/data/dashboard_data.json`, commits `dashboard/`, and pushes `main` (Pages serves `/dashboard` on `main`).

Preview locally:

```powershell
cd dashboard
python -m http.server 8000
```

## Add a login user

```powershell
$env:INVESTOR_PASSWORD = "choose-a-strong-password"
python dashboard/scripts/hash_investor_password.py --id drew --name "Drew" --merge dashboard/data/investors.json
git add dashboard/data/investors.json
git commit -m "Add dashboard login user"
git push
```

Never commit plaintext passwords — only the hashed JSON.

## Legacy

The separate **`spx-0dte-dashboard`** repo is deprecated; use this repo only. Archive that repo when convenient.

Manual fallback (no Actions): `.\dashboard\deploy_pages.ps1` pushes `dashboard/` to the `gh-pages` branch.
