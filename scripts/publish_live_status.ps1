# Publish sanitized live_status.json to a public GitHub Gist (cloud path B).
# Does NOT commit to main or trigger GitHub Pages rebuilds.
# Rate-limited: skips upload if file unchanged or last deploy < MinMinutes ago.
#
# Usage:
#   .\scripts\publish_live_status.ps1
#   .\scripts\publish_live_status.ps1 -Deploy
#   .\scripts\publish_live_status.ps1 -Deploy -MinMinutes 5
#
# Requires SPX_LIVE_STATUS_GIST_ID in ~/.magis-spx-0dte-secrets.ps1
# (see scripts/set_spx_live_status_gist.ps1).

param(
    [switch]$Deploy,
    [double]$MinMinutes = 2
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "load_spx_live_env.ps1")

$Python = $env:SPX_PYTHON
if (-not $Python -or -not (Test-Path $Python)) {
    $Python = "python"
}
$Gh = "gh"
if (-not (Get-Command $Gh -ErrorAction SilentlyContinue)) {
    $Gh = "gh"
}

Write-Host ("publish_live_status: python={0} deploy={1} minMinutes={2}" -f $Python, [bool]$Deploy, $MinMinutes)

& $Python (Join-Path $PSScriptRoot "is_spx_trading_day.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Skipping cloud status publish: SPX is closed today."
    exit 0
}

& $Python live/session_status_server.py --write-status
if ($LASTEXITCODE -ne 0) { throw "write-status failed (exit $LASTEXITCODE)" }

$statusPath = Join-Path $Root "docs\data\live_status.json"

# Both trading PCs run this task, but only the host that actually ran today's
# executor has anything to say. Without this guard a bystander publishes
# execution_type=unknown/open_count=0 over a live session (2026-08-05: the
# dashboard showed zeros while an open spread was being managed here).
$statusDoc = $null
try {
    $statusDoc = Get-Content $statusPath -Raw -Encoding UTF8 | ConvertFrom-Json
} catch {
    throw "could not read $statusPath after --write-status: $($_.Exception.Message)"
}
$hasLocalSession = (
    ($statusDoc.execution_type -and $statusDoc.execution_type -ne "unknown") -or
    $statusDoc.heartbeat_ts -or
    $statusDoc.pid_alive
)
if (-not $hasLocalSession) {
    Write-Host (
        "Skipping publish: no local session for $($statusDoc.date) " +
        "(execution_type=$($statusDoc.execution_type), heartbeat_ts=$($statusDoc.heartbeat_ts)) " +
        "- refusing to overwrite another host's status."
    )
    exit 0
}

$stampDir = Join-Path $Root "data\live\supervisor"
$stampPath = Join-Path $stampDir "last_live_status_deploy.json"
New-Item -ItemType Directory -Force -Path $stampDir | Out-Null

if (-not $Deploy) {
    Write-Host "Wrote $statusPath (no deploy)"
    return
}

$gistId = $env:SPX_LIVE_STATUS_GIST_ID
if (-not $gistId) {
    throw 'SPX_LIVE_STATUS_GIST_ID unset. Run: .\scripts\set_spx_live_status_gist.ps1 -GistId <gist-id>'
}

$now = Get-Date
$currentHash = (Get-FileHash $statusPath -Algorithm SHA256).Hash
if (Test-Path $stampPath) {
    try {
        $prev = Get-Content $stampPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $last = [datetime]$prev.ts
        $ageMin = ($now - $last).TotalMinutes
        if ($ageMin -lt $MinMinutes) {
            Write-Host ("Skip deploy: last publish {0:N1}m ago (min {1})" -f $ageMin, $MinMinutes)
            return
        }
        if ($prev.hash -and $prev.hash -eq $currentHash) {
            Write-Host "Skip deploy: live_status.json unchanged"
            return
        }
    } catch {
        Write-Warning ("Could not read deploy stamp; continuing: {0}" -f $_.Exception.Message)
    }
}

# PATCH gists/{id}. Build JSON with Python so nested content is not mangled by
# ConvertTo-Json (PS nested hashtables previously produced invalid payloads).
$tmpBody = Join-Path $env:TEMP ("spx-gist-patch-" + [guid]::NewGuid().ToString("n") + ".json")
try {
    & $Python -c @"
import json
from pathlib import Path
status = Path(r'''$statusPath''').read_text(encoding='utf-8')
payload = {'files': {'live_status.json': {'content': status}}}
Path(r'''$tmpBody''').write_text(json.dumps(payload), encoding='utf-8')
"@
    if ($LASTEXITCODE -ne 0) { throw "failed to build gist PATCH body (exit $LASTEXITCODE)" }
    Write-Host "Uploading live_status.json to gist $gistId ..."
    & $Gh api -X PATCH "gists/$gistId" --input $tmpBody | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "gh api PATCH gists/$gistId failed (exit $LASTEXITCODE)"
    }
} finally {
    Remove-Item -Force $tmpBody -ErrorAction SilentlyContinue
}

@{ ts = $now.ToString("o"); hash = $currentHash; gist_id = $gistId } | ConvertTo-Json |
    Set-Content -Path $stampPath -Encoding UTF8

$rawUrl = $env:SPX_LIVE_STATUS_URL
if (-not $rawUrl) {
    $rawUrl = "https://gist.githubusercontent.com/GoldmanDrew/$gistId/raw/live_status.json"
}
Write-Host "Deployed live_status.json to gist: $rawUrl"
