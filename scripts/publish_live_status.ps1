# Publish sanitized live_status.json to GitHub Pages (cloud path B).
# Rate-limited: skips push if file unchanged or last deploy < MinMinutes ago.
#
# Usage:
#   .\scripts\publish_live_status.ps1
#   .\scripts\publish_live_status.ps1 -Deploy -MinMinutes 5

param(
    [switch]$Deploy,
    [double]$MinMinutes = 5
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
. (Join-Path $PSScriptRoot "load_spx_live_env.ps1")

$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
$Git = "C:\Program Files\Git\bin\git.exe"
if (-not (Test-Path $Git)) { $Git = "git" }

& $Python (Join-Path $PSScriptRoot "is_spx_trading_day.py")
if ($LASTEXITCODE -ne 0) {
    Write-Host "Skipping cloud status publish: SPX is closed today."
    exit 0
}

& $Python live/session_status_server.py --write-status
if ($LASTEXITCODE -ne 0) { throw "write-status failed" }

$statusPath = Join-Path $Root "docs\data\live_status.json"
$stampPath = Join-Path $Root "data\live\supervisor\last_live_status_deploy.json"

if (-not $Deploy) {
    Write-Host "Wrote $statusPath (no deploy)"
    return
}

$now = Get-Date
if (Test-Path $stampPath) {
    try {
        $prev = Get-Content $stampPath -Raw | ConvertFrom-Json
        $last = [datetime]$prev.ts
        $ageMin = ($now - $last).TotalMinutes
        if ($ageMin -lt $MinMinutes) {
            Write-Host ("Skip deploy: last publish {0:N1}m ago (min {1})" -f $ageMin, $MinMinutes)
            return
        }
        if ($prev.hash -and (Get-FileHash $statusPath -Algorithm SHA256).Hash -eq $prev.hash) {
            Write-Host "Skip deploy: live_status.json unchanged"
            return
        }
    } catch {}
}

& $Git -C $Root add -- "docs/data/live_status.json"
$status = & $Git -C $Root status --porcelain -- "docs/data/live_status.json"
if (-not $status) {
    Write-Host "Nothing to commit"
    return
}

& $Git -C $Root commit -m "chore: refresh sanitized live session status"
& $Git -C $Root push origin HEAD
$hash = (Get-FileHash $statusPath -Algorithm SHA256).Hash
@{ ts = $now.ToString("o"); hash = $hash } | ConvertTo-Json |
    Set-Content -Path $stampPath -Encoding UTF8
Write-Host "Deployed live_status.json to origin"
