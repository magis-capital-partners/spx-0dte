# Local session status API for the dashboard (loopback).
# Also periodically writes sanitized docs/data/live_status.json for cloud sync.
#
# Usage:
#   .\scripts\run_session_status_server.ps1
#   .\scripts\run_session_status_server.ps1 -Port 8765 -WriteInterval 60

param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8765,
    [double]$WriteInterval = 60
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "load_spx_live_env.ps1")

python live/session_status_server.py `
    --host $HostAddress `
    --port $Port `
    --write-interval $WriteInterval
