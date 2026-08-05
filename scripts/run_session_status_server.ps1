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

# Evict whatever already holds the port. `python -m http.server <port>`
# squatters bind with SO_REUSEADDR and silently serve 404 for /status + /logs,
# which is how the dashboard loses executor stdout while the task still looks
# up. An orphaned session_status_server.py is now just as fatal: the server
# binds exclusively, and Stop-ScheduledTask kills this wrapper while leaving its
# python child on the port, so a task "restart" kept serving the 09:00 process's
# stale code all session (2026-08-05). Nothing is listening for us yet at this
# point, so any owner here is by definition a previous run.
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $listeners) {
    if ($conn.OwningProcess -eq $PID) { continue }
    $owner = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $conn.OwningProcess) -ErrorAction SilentlyContinue
    if (-not $owner -or -not $owner.CommandLine) { continue }
    if ($owner.CommandLine -match "session_status_server\.py" -or $owner.CommandLine -match "http\.server") {
        Write-Warning ("Evicting stale port-{0} listener pid={1}: {2}" -f $Port, $owner.ProcessId, $owner.CommandLine)
        Stop-Process -Id $owner.ProcessId -Force -ErrorAction SilentlyContinue
    } else {
        Write-Warning ("Port {0} owned by unexpected pid={1}: {2}" -f $Port, $owner.ProcessId, $owner.CommandLine)
    }
}

# The exclusive bind fails if the evicted socket has not been released yet.
for ($i = 0; $i -lt 40; $i++) {
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
}

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
