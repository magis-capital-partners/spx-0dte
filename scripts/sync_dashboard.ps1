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

$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

Write-Host "Building dashboard data..."
& $Python "$Root\dashboard\build_dashboard_data.py" `
  --run "linear_decay_downsize=data/dashboard_runs/linear_decay_downsize:3D + linear decay downsize (sell early, less late)" `
  --account-equity $Equity `
  --out "$Root\dashboard\data\dashboard_data.json"

$destData = Join-Path $DashboardRoot "data\dashboard_data.json"
Copy-Item "$Root\dashboard\data\dashboard_data.json" $destData -Force
Write-Host "Copied -> $destData"

$destIndex = Join-Path $DashboardRoot "index.html"
Copy-Item "$Root\dashboard\index.html" $destIndex -Force
Write-Host "Copied -> $destIndex"

if ($Push) {
  $git = "C:\Program Files\Git\bin\git.exe"
  Push-Location $DashboardRoot
  & $git pull origin main
  Copy-Item "$Root\dashboard\data\dashboard_data.json" $destData -Force
  Copy-Item "$Root\dashboard\index.html" $destIndex -Force
  & $git add data/dashboard_data.json index.html
  if (-not (& $git diff --cached --quiet)) {
    & $git -c user.name="Drew Goldman" -c user.email="dag5wd@virginia.edu" commit -m "dashboard: refresh backtest data and UI"
    & $git push origin main
    Write-Host "Pushed dashboard repo (Actions will redeploy Pages)."
  } else {
    Write-Host "No dashboard changes to commit."
  }
  Pop-Location
}
