# Install (or update) a Windows Scheduled Task that runs daily_data_update.ps1 after the close.
#
# Why local Task Scheduler instead of GitHub Actions:
# - You are out of Actions minutes until August.
# - GitHub Pages "Deploy from a branch" (/docs on main) uses NO Actions minutes.
# - ThetaData downloads need your machine + API key anyway (local cache under data/).
#
# Prerequisites:
#   1. Set THETADATA_API_KEY as a User environment variable (this script can do it).
#   2. PC should be on / awake around the scheduled time (default 4:05 PM ET weekdays).
#   3. gh CLI authenticated if you want -Deploy to push + trigger Pages.
#
# Usage:
#   .\scripts\install_daily_update_task.ps1
#   .\scripts\install_daily_update_task.ps1 -ApiKey "td1_..." -Deploy
#   .\scripts\install_daily_update_task.ps1 -Time "16:05" -Deploy
#   .\scripts\install_daily_update_task.ps1 -Uninstall

param(
    [string]$TaskName = "SPX-0DTE Daily Data Update",
    [string]$Time = "16:05",
    [string]$ApiKey = "",
    [switch]$Deploy,
    [switch]$Uninstall,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$UpdateScript = Join-Path $ScriptDir "daily_data_update.ps1"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task: $TaskName"
    exit 0
}

if (-not (Test-Path $UpdateScript)) {
    throw "Missing $UpdateScript"
}

# Persist API key to User environment (never commit; not written into the task XML as plaintext arg).
if ($ApiKey) {
    [Environment]::SetEnvironmentVariable("THETADATA_API_KEY", $ApiKey, "User")
    $env:THETADATA_API_KEY = $ApiKey
    Write-Host "Saved THETADATA_API_KEY to User environment variables."
} elseif (-not [Environment]::GetEnvironmentVariable("THETADATA_API_KEY", "User") -and -not $env:THETADATA_API_KEY) {
    Write-Warning "THETADATA_API_KEY is not set. Pass -ApiKey or set it before the first scheduled run."
}

$argList = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$UpdateScript`""
)
if ($Deploy) {
    $argList += "-Deploy"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument ($argList -join " ") `
    -WorkingDirectory $Root

# Weekdays at $Time (local machine time). The daily workflow begins shortly
# after the cash close so the refreshed backtest and dashboard can publish
# during the post-close window.
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $Time

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host ""
Write-Host "Installed scheduled task: $TaskName"
Write-Host "  When: weekdays at $Time (local time)"
Write-Host "  Script: $UpdateScript"
Write-Host "  Deploy: $Deploy"
Write-Host "  WorkingDirectory: $Root"
Write-Host ""
Write-Host "Manage: Task Scheduler -> Task Scheduler Library -> '$TaskName'"
Write-Host "Logs:   data\logs\daily_update_*.log"
Write-Host "Manual: .\scripts\daily_data_update.ps1 -Deploy"
Write-Host ""

if ($RunNow) {
    Write-Host "Starting task now..."
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started. Watch data\logs\ for progress."
}
