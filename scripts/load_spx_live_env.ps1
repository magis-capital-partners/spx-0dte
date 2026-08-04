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
$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"

# Prefer the repo venv, then a local Python 3.12 install, then PATH.
$RootForPython = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $RootForPython ".venv\Scripts\python.exe"
$Py312 = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
if (Test-Path $VenvPython) {
    $env:SPX_PYTHON = $VenvPython
} elseif (Test-Path $Py312) {
    $env:SPX_PYTHON = $Py312
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $env:SPX_PYTHON = $cmd.Source }
}
if ($env:SPX_PYTHON) {
    $env:Path = "$(Split-Path -Parent $env:SPX_PYTHON);$env:Path"
}

if (-not $env:SPX_SLACK_WEBHOOK_URL) {
    Write-Warning "SPX_SLACK_WEBHOOK_URL unset. Run: .\scripts\set_spx_slack_webhook.ps1 -UseMagisWorkspaceWebhook"
}
