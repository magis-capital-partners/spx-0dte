# Load SPX 0DTE live secrets into the current process.
# Secrets file (not in git): %USERPROFILE%\.magis-spx-0dte-secrets.ps1
# Must be created while logged into Slack as drew@magiscapitalpartners.com (Magis workspace).

$ErrorActionPreference = "Stop"

$SecretsFile = Join-Path $env:USERPROFILE ".magis-spx-0dte-secrets.ps1"
if (Test-Path $SecretsFile) {
    . $SecretsFile
}

# UTF-8 so ib_executor lock arrow / Unicode logs don't crash under Task Scheduler.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"

if (-not $env:SPX_SLACK_WEBHOOK_URL) {
    Write-Warning "SPX_SLACK_WEBHOOK_URL unset. Run: .\scripts\set_spx_slack_webhook.ps1 -UseMagisWorkspaceWebhook"
}
