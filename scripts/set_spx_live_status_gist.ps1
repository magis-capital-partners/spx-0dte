# Configure the public gist used for cloud live status (path B).
# Does NOT push to GitHub Pages - status publishes via gh gist edit.
#
# Usage:
#   .\scripts\set_spx_live_status_gist.ps1 -GistId eb3c0aba82982a05b8bb430b380c808a
#   .\scripts\set_spx_live_status_gist.ps1 -Create   # create a new public gist from local JSON
#   .\scripts\set_spx_live_status_gist.ps1 -GistId eb3c0aba82982a05b8bb430b380c808a -SkipUrlFile

param(
    [string]$GistId = "",
    [switch]$Create,
    [string]$Owner = "GoldmanDrew",
    # Skip rewriting docs/data/live_status_url.json (secrets-only on a second PC).
    [switch]$SkipUrlFile
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Gh = "gh"
if (-not (Get-Command $Gh -ErrorAction SilentlyContinue)) {
    throw "gh CLI not found; install GitHub CLI and authenticate (gist scope)."
}

$statusPath = Join-Path $Root "docs\data\live_status.json"
$urlPath = Join-Path $Root "docs\data\live_status_url.json"

if ($Create) {
    if (-not (Test-Path $statusPath)) {
        throw "Missing $statusPath - run session_status_server.py --write-status first."
    }
    $tmp = Join-Path $env:TEMP ("spx-live-status-gist-" + [guid]::NewGuid().ToString("n"))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    try {
        Copy-Item $statusPath (Join-Path $tmp "live_status.json") -Force
        Push-Location $tmp
        $gistUrl = & $Gh gist create live_status.json --public --desc "SPX 0DTE sanitized live session status (cloud path B; not Pages)"
        Pop-Location
    } finally {
        Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    }
    if ($LASTEXITCODE -ne 0 -or -not $gistUrl) {
        throw "gh gist create failed"
    }
    if ($gistUrl -notmatch 'gist\.github\.com/[^/]+/([a-f0-9]+)') {
        throw "Could not parse gist id from: $gistUrl"
    }
    $GistId = $Matches[1]
    Write-Host "Created gist: $gistUrl"
}

if (-not $GistId) {
    throw 'Pass -GistId <gist-id> or -Create'
}
if ($GistId -notmatch '^[a-f0-9]+$') {
    throw "GistId must be a hex gist id, got: $GistId"
}

$rawUrl = "https://gist.githubusercontent.com/$Owner/$GistId/raw/live_status.json"

$secretsFile = Join-Path $env:USERPROFILE ".magis-spx-0dte-secrets.ps1"
$lines = @()
if (Test-Path $secretsFile) {
    $lines = Get-Content $secretsFile
}
$out = New-Object System.Collections.Generic.List[string]
$haveId = $false
$haveUrl = $false
foreach ($line in $lines) {
    if ($line -match '^\$env:SPX_LIVE_STATUS_GIST_ID\s*=') {
        $out.Add("`$env:SPX_LIVE_STATUS_GIST_ID = '$GistId'")
        $haveId = $true
        continue
    }
    if ($line -match '^\$env:SPX_LIVE_STATUS_URL\s*=') {
        $out.Add("`$env:SPX_LIVE_STATUS_URL = '$rawUrl'")
        $haveUrl = $true
        continue
    }
    $out.Add($line)
}
if (-not $haveId) {
    $out.Add("# Public gist for sanitized live_status.json (cloud path B; not Pages)")
    $out.Add("`$env:SPX_LIVE_STATUS_GIST_ID = '$GistId'")
}
if (-not $haveUrl) {
    $out.Add("`$env:SPX_LIVE_STATUS_URL = '$rawUrl'")
}
$out | Set-Content -Path $secretsFile -Encoding UTF8

[Environment]::SetEnvironmentVariable("SPX_LIVE_STATUS_GIST_ID", $GistId, "User")
[Environment]::SetEnvironmentVariable("SPX_LIVE_STATUS_URL", $rawUrl, "User")
$env:SPX_LIVE_STATUS_GIST_ID = $GistId
$env:SPX_LIVE_STATUS_URL = $rawUrl

Write-Host "Saved SPX_LIVE_STATUS_GIST_ID / SPX_LIVE_STATUS_URL -> $secretsFile"
Write-Host "Raw URL: $rawUrl"

if (-not $SkipUrlFile) {
    @{
        schema = 1
        gist_id = $GistId
        owner = $Owner
        url = $rawUrl
        notes = "Cloud Session-now status; published by publish_live_status.ps1 via gh gist edit (not GitHub Pages)."
    } | ConvertTo-Json | Set-Content -Path $urlPath -Encoding UTF8
    Write-Host "Wrote $urlPath"
}

Write-Host "Verify: gh gist view $GistId"
