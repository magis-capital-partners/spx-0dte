# Dashboard (in this repo)

The interactive site is deployed from the **`docs/`** folder on **`main`** (GitHub Pages → branch deploy).

| Layer | What it does |
|-------|----------------|
| **Private repo** | Simulator, research, and live code stay private. Only the static `docs/` folder is published. |
| **Public GitHub Pages** | Clean URL: **https://magis-capital-partners.github.io/spx-0dte/** (Enterprise: repo private, Pages public). |
| **`data/investors.json` login** | App-level password gate (PBKDF2, same scheme as etf-dashboard). Required when `users` is non-empty. |

> Layer 2 is a UI gate; JSON files remain fetchable if someone bypasses login. For stronger control, use org repo ACLs or a custom domain with Cloudflare Access.

## GitHub Pages settings

**Settings → Pages → Build and deployment:**

- Source: **Deploy from a branch**
- Branch: **`main`**
- Folder: **`/docs`** (GitHub only offers `/` or `/docs` — not `/dashboard`)

Branch deploy uses **no GitHub Actions minutes**.

## Refresh + deploy

```powershell
.\scripts\sync_dashboard.ps1 -Push
```

Rebuilds `docs/data/dashboard_data.json`, commits `docs/`, and pushes `main`.

Preview locally:

```powershell
cd docs
python -m http.server 8000
```

## Add a login user

```powershell
$env:INVESTOR_PASSWORD = "choose-a-strong-password"
python docs/scripts/hash_investor_password.py --id drew --name "Drew" --merge docs/data/investors.json
git add docs/data/investors.json
git commit -m "Add dashboard login user"
git push
```

Never commit plaintext passwords — only the hashed JSON.

## Legacy

Manual fallback (no Actions): `.\docs\deploy_pages.ps1` pushes `docs/` to the `gh-pages` branch.
