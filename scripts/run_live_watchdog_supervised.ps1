# Watchdog with Magis Slack secrets loaded.
# Usage:
#   .\scripts\run_live_watchdog.ps1
#   .\scripts\run_live_watchdog_supervised.ps1 -WriteKill

param(
    [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
    [double]$PollSeconds = 10,
    [double]$MaxHeartbeatAge = 30,
    [switch]$WriteKill
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

. (Join-Path $PSScriptRoot "load_spx_live_env.ps1")

$pyArgs = @(
    "live/watchdog.py",
    "--date", $Date,
    "--poll-seconds", "$PollSeconds",
    "--max-heartbeat-age", "$MaxHeartbeatAge"
)
if ($WriteKill) { $pyArgs += "--write-kill" }

python @pyArgs
