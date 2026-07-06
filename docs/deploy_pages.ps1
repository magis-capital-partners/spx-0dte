# Redeploy dashboard to GitHub Pages via gh-pages branch (legacy fallback).
# Prefer: branch deploy from main /docs (see DASHBOARD.md)
#
# Usage (from repo root):
#   .\docs\deploy_pages.ps1

$ErrorActionPreference = "Stop"
Write-Host "Splitting docs/ into gh-pages and force-pushing..."
git branch -D gh-pages 2>$null
git subtree split --prefix docs -b gh-pages
git push -f origin gh-pages
Write-Host "Done. Site: https://magis-capital-partners.github.io/spx-0dte/"
