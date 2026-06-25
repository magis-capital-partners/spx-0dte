# Sync backtest JSON to the private dashboard repo and optionally push.
#
# Usage (from Strategy Recreation root):
#   .\scripts\sync_dashboard.ps1
#   .\scripts\sync_dashboard.ps1 -Push
#   .\scripts\sync_dashboard.ps1 -DashboardRoot "C:\path\to\spx-0dte-dashboard"

param(
  [string]$DashboardRoot = "",
  [switch]$Push,
  [double]$Equity = 13000000
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here

if (-not $DashboardRoot) {
  $Investing = (Resolve-Path (Join-Path $Root "..\..\..")).Path
  $DashboardRoot = Join-Path $Investing "spx-0dte-dashboard"
}
if (-not (Test-Path $DashboardRoot)) {
  throw "Dashboard repo not found at $DashboardRoot. Clone magis-capital-partners/spx-0dte-dashboard alongside Investing."
}

Write-Host "Building dashboard data..."
python "$Root\dashboard\build_dashboard_data.py" `
  --run "baseline=data/baseline_repro:Baseline (prior best, no governor)" `
  --run "flatten=data/exp1_flatten:1x + flatten governor" `
  --run "best=data/exp2_scale2x:2x deploy + flatten" `
  --run "aggressive=data/exp6_2p5x_deepflat:2.5x + deep flatten" `
  --account-equity $Equity `
  --out "$Root\dashboard\data\dashboard_data.json"

$dest = Join-Path $DashboardRoot "data\dashboard_data.json"
Copy-Item "$Root\dashboard\data\dashboard_data.json" $dest -Force
Write-Host "Copied -> $dest"

if ($Push) {
  Push-Location $DashboardRoot
  git add data/dashboard_data.json
  if (-not (git diff --cached --quiet)) {
    git commit -m "dashboard: refresh backtest data"
    git push
    Write-Host "Pushed dashboard repo (Actions will redeploy Pages)."
  } else {
    Write-Host "No dashboard data changes to commit."
  }
  Pop-Location
}
