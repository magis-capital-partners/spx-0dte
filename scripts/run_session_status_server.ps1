# Local session status API for the dashboard (loopback).
# Also periodically writes sanitized docs/data/live_status.json for cloud sync.
#
# Usage:
#   .\scripts\run_session_status_server.ps1
#   .\scripts\run_session_status_server.ps1 -Port 8765 -WriteInterval 60

param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [double]$WriteInterval = 30,
    # Keep serving through the close so local console + cloud writers stay available.
    [string]$StopAt = "16:30"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "load_spx_live_env.ps1")

$Python = $env:SPX_PYTHON
if (-not $Python -or -not (Test-Path $Python)) { $Python = "python" }

& $Python (Join-Path $PSScriptRoot "is_spx_trading_day.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Skipping session status API: SPX is closed today."
    exit 0
}

Write-Host ("session_status_server: python={0} writeInterval={1}s stopAt={2}" -f $Python, $WriteInterval, $StopAt)

while ($true) {
    & $Python live/session_status_server.py `
        --host $HostAddress `
        --port $Port `
        --write-interval $WriteInterval `
        --stop-at $StopAt
    $code = $LASTEXITCODE
    if ($code -ne 75) { exit $code }

    Write-Host "Status service rolled to a new date; checking the trading calendar."
    & $Python (Join-Path $PSScriptRoot "is_spx_trading_day.py")
    if ($LASTEXITCODE -ne 0) {
        Write-Host "New date is not an SPX trading day; status service will resume at the next scheduled start."
        exit 0
    }
}
