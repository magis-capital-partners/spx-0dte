# Portable local watchdog for the live executor (run on the same machine).
# Usage:
#   .\scripts\run_live_watchdog.ps1
#   .\scripts\run_live_watchdog.ps1 -WriteKill
# Requires SPX_SLACK_WEBHOOK_URL for phone/Slack alerts (optional).

param(
    [string]$Date = (Get-Date -Format "yyyy-MM-dd"),
    [double]$PollSeconds = 10,
    [double]$MaxHeartbeatAge = 30,
    [switch]$WriteKill
)

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$args = @(
    "live/watchdog.py",
    "--date", $Date,
    "--poll-seconds", "$PollSeconds",
    "--max-heartbeat-age", "$MaxHeartbeatAge"
)
if ($WriteKill) { $args += "--write-kill" }

python @args
