# Set SPX_SLACK_WEBHOOK_URL for Magis Capital Partners Slack
# (drew@magiscapitalpartners.com workspace - NOT a personal Slack).
#
# Usage:
#   .\scripts\set_spx_slack_webhook.ps1 -UseMagisWorkspaceWebhook
#   .\scripts\set_spx_slack_webhook.ps1 -SlackWebhookUrl "https://hooks.slack.com/services/..."
#
# Prefer a dedicated Incoming Webhook into an #spx-0dte (or trading) Magis channel.
# -UseMagisWorkspaceWebhook copies the Magis org webhook from ~/.magis-ci-autofix-secrets.ps1.

param(
    [string]$SlackWebhookUrl = "",
    [switch]$UseMagisWorkspaceWebhook,
    [switch]$SendTest
)

$ErrorActionPreference = "Stop"

if ($UseMagisWorkspaceWebhook) {
    $ciSecrets = Join-Path $env:USERPROFILE ".magis-ci-autofix-secrets.ps1"
    if (-not (Test-Path $ciSecrets)) {
        throw "Missing $ciSecrets - cannot copy Magis workspace webhook."
    }
    . $ciSecrets
    if (-not $env:SLACK_WEBHOOK_URL) {
        throw "SLACK_WEBHOOK_URL not set in Magis CI secrets file."
    }
    $SlackWebhookUrl = $env:SLACK_WEBHOOK_URL.Trim()
}

if (-not $SlackWebhookUrl) {
    throw "Pass -SlackWebhookUrl or -UseMagisWorkspaceWebhook"
}

if ($SlackWebhookUrl -notmatch '^https://hooks\.slack\.com/services/') {
    throw "Expected an Incoming Webhook URL starting with https://hooks.slack.com/services/"
}

$secretsFile = Join-Path $env:USERPROFILE ".magis-spx-0dte-secrets.ps1"
$lines = @()
if (Test-Path $secretsFile) {
    $lines = Get-Content $secretsFile
}
$out = New-Object System.Collections.Generic.List[string]
$added = $false
foreach ($line in $lines) {
    if ($line -match '^\$env:SPX_SLACK_WEBHOOK_URL\s*=') {
        $out.Add("`$env:SPX_SLACK_WEBHOOK_URL = '$SlackWebhookUrl'")
        $added = $true
        continue
    }
    $out.Add($line)
}
if (-not $added) {
    $out.Add("# Magis Capital Partners Slack (drew@magiscapitalpartners.com workspace)")
    $out.Add("`$env:SPX_SLACK_WEBHOOK_URL = '$SlackWebhookUrl'")
}
$out | Set-Content -Path $secretsFile -Encoding UTF8

# Also set User-level env so new shells / Task Scheduler children can see it.
[Environment]::SetEnvironmentVariable("SPX_SLACK_WEBHOOK_URL", $SlackWebhookUrl, "User")
$env:SPX_SLACK_WEBHOOK_URL = $SlackWebhookUrl

Write-Host "Saved SPX_SLACK_WEBHOOK_URL -> $secretsFile (Magis workspace)"
Write-Host "Also set User environment variable SPX_SLACK_WEBHOOK_URL"

if ($SendTest) {
    $Root = Split-Path -Parent $PSScriptRoot
    Set-Location $Root
    $env:PYTHONIOENCODING = "utf-8"
    python -c @"
import sys
sys.path.insert(0, 'live')
from slack_notify import notify_slack
ok = notify_slack('[spx-0dte] Slack wiring test - Magis workspace (drew@magiscapitalpartners.com)')
print('test_sent' if ok else 'test_failed')
raise SystemExit(0 if ok else 1)
"@
}
