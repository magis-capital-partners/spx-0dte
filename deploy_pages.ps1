# Redeploy the dashboard to GitHub Pages WITHOUT needing the Actions `workflow`
# token scope. Pages is configured to serve the repo's `gh-pages` branch root,
# whose contents are the `dashboard/` folder. This script rebuilds that branch
# from the current dashboard/ and force-pushes it.
#
# Run after rebuilding data: python dashboard/build_dashboard_data.py ...
#
# Usage (from repo root "MBH Capital/Strategy Recreation"):
#   .\dashboard\deploy_pages.ps1

$ErrorActionPreference = "Stop"
Write-Host "Committing any dashboard changes on main..."
git add dashboard
if (-not (git diff --cached --quiet; $?)) { git commit -m "dashboard: refresh data/blob" }

Write-Host "Splitting dashboard/ into gh-pages and force-pushing..."
git branch -D gh-pages 2>$null
git subtree split --prefix dashboard -b gh-pages
git push -f origin gh-pages

Write-Host "Done. If Pages source is GitHub Actions, the workflow deploys on push to main."
Write-Host "Check URL: gh api repos/magis-capital-partners/spx-0dte/pages --jq .html_url"

# Alternative (preferred long-term): grant the workflow scope once with
#   gh auth refresh -h github.com -s workflow
# then `git add .github && git commit && git push`, and the
# .github/workflows/deploy-dashboard.yml Action will deploy on every push.
