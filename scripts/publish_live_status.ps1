# Publish sanitized live_status.json to GitHub Pages (cloud path B).
# Rate-limited: skips push if file unchanged or last deploy < MinMinutes ago.
#
# Usage:
#   .\scripts\publish_live_status.ps1
#   .\scripts\publish_live_status.ps1 -Deploy
#   .\scripts\publish_live_status.ps1 -Deploy -MinMinutes 2

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
$Git = "C:\Program Files\Git\bin\git.exe"
if (-not (Test-Path $Git)) { $Git = "git" }

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
    $statusDoc = Get-Content $statusPath -Raw | ConvertFrom-Json
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

$now = Get-Date
$currentHash = (Get-FileHash $statusPath -Algorithm SHA256).Hash
if (Test-Path $stampPath) {
    try {
        $prev = Get-Content $stampPath -Raw | ConvertFrom-Json
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

& $Git -C $Root add -- "docs/data/live_status.json"
$status = & $Git -C $Root status --porcelain -- "docs/data/live_status.json"
if (-not $status) {
    Write-Host "Nothing to commit"
    @{ ts = $now.ToString("o"); hash = $currentHash } | ConvertTo-Json |
        Set-Content -Path $stampPath -Encoding UTF8
    return
}

& $Git -C $Root commit -m "chore: refresh sanitized live session status"
if ($LASTEXITCODE -ne 0) { throw "git commit failed (exit $LASTEXITCODE)" }

# Status Publish often races other machines' status commits; rebase then push.
# --autostash is required, not cosmetic: any unrelated dirty file (an edited
# script, or data/calendar/vix_daily.csv refreshed by the 09:00 preflight) makes
# plain `pull --rebase` refuse with "You have unstaged changes", which silently
# stopped every status push for an hour on 2026-08-05.
& $Git -C $Root pull --rebase --autostash origin HEAD
if ($LASTEXITCODE -ne 0) {
    # Live status JSON conflicts are expected; take our freshly written file.
    $conflicted = & $Git -C $Root diff --name-only --diff-filter=U
    if ($conflicted -match "docs/data/live_status.json") {
        & $Python live/session_status_server.py --write-status
        if ($LASTEXITCODE -ne 0) { throw "rewrite-status after conflict failed" }
        & $Git -C $Root add -- "docs/data/live_status.json"
        $env:GIT_EDITOR = "true"
        & $Git -C $Root -c core.editor=true rebase --continue
        if ($LASTEXITCODE -ne 0) {
            & $Git -C $Root rebase --abort
            throw "git pull --rebase failed after live_status conflict"
        }
    } else {
        & $Git -C $Root rebase --abort
        throw "git pull --rebase failed (exit $LASTEXITCODE); conflicts=$conflicted"
    }
}

& $Git -C $Root push origin HEAD
if ($LASTEXITCODE -ne 0) { throw "git push failed (exit $LASTEXITCODE)" }

@{ ts = $now.ToString("o"); hash = $currentHash } | ConvertTo-Json |
    Set-Content -Path $stampPath -Encoding UTF8
Write-Host "Deployed live_status.json to origin"
