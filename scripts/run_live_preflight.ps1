# Refresh the inputs required before a manually started SPX 0DTE paper/live session.
# This script never starts the IB executor or submits orders.
#
# Usage:
#   .\scripts\run_live_preflight.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Python = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
if (-not $Python) {
    $Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
}
if (-not (Test-Path $Python)) {
    throw "Python not found: $Python"
}

& $Python "scripts/is_spx_trading_day.py"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Skipping live preflight: SPX is closed today."
    exit 0
}

Write-Host "Refreshing live signal baselines..."
& $Python "scripts/refresh_live_baselines.py"
if ($LASTEXITCODE -ne 0) { throw "Live baseline refresh failed (exit $LASTEXITCODE)" }

Write-Host "Compressing completed IB session logs..."
& $Python "live/log_maintenance.py"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "IB log compression reported a failure; continuing preflight."
}

Write-Host "Refreshing VIX daily calendar..."
& $Python "scripts/download_vix_daily.py"
if ($LASTEXITCODE -ne 0) { throw "VIX calendar refresh failed (exit $LASTEXITCODE)" }

Write-Host "Live preflight complete. Start the executor manually when ready: python live/ib_executor.py"
