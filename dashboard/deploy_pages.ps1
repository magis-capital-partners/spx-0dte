# Redeploy dashboard to GitHub Pages via gh-pages branch (legacy fallback).
# Prefer: .\scripts\sync_dashboard.ps1 -Push  (GitHub Actions deploy from main)
#
# Usage (from repo root):
#   .\dashboard\deploy_pages.ps1

$ErrorActionPreference = "Stop"
Write-Host "Splitting dashboard/ into gh-pages and force-pushing..."
git branch -D gh-pages 2>$null
git subtree split --prefix dashboard -b gh-pages
git push -f origin gh-pages
Write-Host "Done. Site: https://magis-capital-partners.github.io/spx-0dte/"
