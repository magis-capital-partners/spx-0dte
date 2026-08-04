# Install Windows Scheduled Tasks for SPX 0DTE session support:
#   - Morning preflight (baselines + VIX only)
#   - Watchdog, local status API, and cloud-status publishing
#
# The IB executor remains deliberately manual: no scheduled task can start it.
#
# Usage:
#   .\scripts\install_live_supervisor_tasks.ps1
#   .\scripts\install_live_supervisor_tasks.ps1 -StartNow
#   .\scripts\install_live_supervisor_tasks.ps1 -Uninstall

param(
    [switch]$StartNow,
    [switch]$Uninstall,
    [string]$DailyAt = "09:00"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$tasks = @(
    @{
        Name = "Magis SPX 0DTE Daily Preflight"
        Script = "run_live_preflight.ps1"
        ExtraArgs = @()
        Description = "Refresh SPX 0DTE live baselines and VIX calendar; never starts the IB executor."
        LimitHours = 1
    },
    @{
        Name = "Magis SPX 0DTE Watchdog"
        Script = "run_live_watchdog_supervised.ps1"
        ExtraArgs = @("-WriteKill")
        Description = "SPX 0DTE heartbeat watchdog with Magis Slack + WriteKill."
        LimitHours = 14
    },
    @{
        Name = "Magis SPX 0DTE Status API"
        Script = "run_session_status_server.ps1"
        ExtraArgs = @()
        Description = "Local dashboard status API on 127.0.0.1:8765; writes sanitized live_status.json."
        LimitHours = 14
    },
    @{
        Name = "Magis SPX 0DTE Status Publish"
        Script = "publish_live_status.ps1"
        ExtraArgs = @("-Deploy", "-MinMinutes", "2")
        Description = "Push sanitized live_status.json to GitHub Pages (~every 2 min when changed)."
        LimitHours = 1
        RepeatMinutes = 2
    }
)

$legacyTaskNames = @(
    "Magis SPX 0DTE Executor"
)

if ($Uninstall) {
    foreach ($t in $tasks) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed task: $($t.Name)"
    }
    foreach ($name in $legacyTaskNames) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed legacy task: $name"
    }
    return
}

foreach ($name in $legacyTaskNames) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed legacy automatic task: $name"
}

function Register-SpxTask($spec) {
    $scriptPath = Join-Path $PSScriptRoot $spec.Script
    $argParts = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$scriptPath`"") + $spec.ExtraArgs
    $arg = ($argParts -join " ")
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $arg `
        -WorkingDirectory $Root

    $triggers = @(
        (New-ScheduledTaskTrigger -Daily -At $DailyAt),
        (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
    )
    if ($spec.ContainsKey("RepeatMinutes") -and $spec.RepeatMinutes) {
        $rep = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $spec.RepeatMinutes) `
            -RepetitionDuration (New-TimeSpan -Days 3650)
        $triggers = @($rep) + $triggers
    }
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit (New-TimeSpan -Hours $spec.LimitHours)

    Register-ScheduledTask `
        -TaskName $spec.Name `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Description $spec.Description `
        -Force | Out-Null

    Write-Host "Registered: $($spec.Name)"
}

foreach ($t in $tasks) { Register-SpxTask $t }

if ($StartNow) {
    foreach ($name in @(
        "Magis SPX 0DTE Daily Preflight",
        "Magis SPX 0DTE Status API",
        "Magis SPX 0DTE Status Publish",
        "Magis SPX 0DTE Watchdog"
    )) {
        $st = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($st -and $st.State -ne "Running") {
            Start-ScheduledTask -TaskName $name
            Write-Host "Started: $name"
        }
    }
}

Get-ScheduledTask -TaskName ($tasks.Name) | Format-Table TaskName, State -AutoSize
Write-Host ""
Write-Host "Executor remains manual: python live/ib_executor.py"
