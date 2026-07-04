# Rebuild dashboard data and deploy to GitHub Pages (single repo: spx-0dte).
#
# Usage (from repo root):
#   .\scripts\sync_dashboard.ps1              # rebuild dashboard/data/dashboard_data.json only
#   .\scripts\sync_dashboard.ps1 -Push        # commit dashboard/ + push main (Actions deploys Pages)
#   .\scripts\sync_dashboard.ps1 -DeployOnly  # push existing dashboard/ without rebuilding

param(
  [switch]$Push,
  [switch]$DeployOnly,
  [double]$Equity = 13000000
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
$Git = "C:\Program Files\Git\bin\git.exe"
if (-not (Test-Path $Git)) { $Git = "git" }

if (-not $DeployOnly) {
  Write-Host "Building dashboard data..."
  & $Python "$Root\dashboard\build_dashboard_data.py" `
    --run "linear_decay_downsize=data/dashboard_runs/linear_decay_downsize:3D + linear decay · full history (2019–2026)" `
    --account-equity $Equity `
    --out "$Root\dashboard\data\dashboard_data.json"
}

if (-not $Push) {
  Write-Host "Done. Preview: cd dashboard; python -m http.server 8000"
  Write-Host "Deploy: .\scripts\sync_dashboard.ps1 -Push"
  exit 0
}

Push-Location $Root
& $Git add dashboard/
$staged = & $Git diff --cached --name-only
if (-not $staged) {
  Write-Host "No dashboard changes to commit."
  Pop-Location
  exit 0
}
& $Git -c user.name="Drew Goldman" -c user.email="dag5wd@virginia.edu" commit -m "dashboard: refresh backtest data and deploy"
& $Git push origin main
Pop-Location
Write-Host "Pushed main. GitHub Actions will deploy Pages from dashboard/."
Write-Host "Site: https://magis-capital-partners.github.io/spx-0dte/"
