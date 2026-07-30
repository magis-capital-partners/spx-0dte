# Watchdog with Magis Slack secrets loaded.
# Usage:
#   .\scripts\run_live_watchdog.ps1
#   .\scripts\run_live_watchdog_supervised.ps1 -WriteKill

param(
    [string]$Date = "",
    [double]$PollSeconds = 10,
    [double]$MaxHeartbeatAge = 30,
    [switch]$WriteKill
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

. (Join-Path $PSScriptRoot "load_spx_live_env.ps1")

& python (Join-Path $PSScriptRoot "is_spx_trading_day.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Skipping watchdog: SPX is closed today."
    exit 0
}

$pyArgs = @(
    "live/watchdog.py",
    "--poll-seconds", "$PollSeconds",
    "--max-heartbeat-age", "$MaxHeartbeatAge"
)
if ($Date) { $pyArgs += @("--date", $Date) }
if ($WriteKill) { $pyArgs += "--write-kill" }

python @pyArgs
