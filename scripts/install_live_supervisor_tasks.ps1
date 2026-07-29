# Install Windows Scheduled Tasks for SPX 0DTE paper auto-heal + dashboard status:
#   - Magis SPX 0DTE Executor (supervised)
#   - Magis SPX 0DTE Watchdog (WriteKill)
#   - Magis SPX 0DTE Status API (local :8765 + write live_status.json)
#   - Magis SPX 0DTE Status Publish (sanitized cloud status every 5 min while logged on)
#
# Usage:
#   .\scripts\install_live_supervisor_tasks.ps1
#   .\scripts\install_live_supervisor_tasks.ps1 -StartNow
#   .\scripts\install_live_supervisor_tasks.ps1 -Uninstall

param(
    [switch]$StartNow,
    [switch]$Uninstall,
    [string]$DailyAt = "09:15"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$tasks = @(
    @{
        Name = "Magis SPX 0DTE Executor"
        Script = "run_ib_executor_supervised.ps1"
        ExtraArgs = @()
        Description = "Supervised SPX 0DTE IB paper executor (Magis Slack, auto-restart on crash)."
        LimitHours = 14
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
        ExtraArgs = @("-Deploy", "-MinMinutes", "5")
        Description = "Push sanitized live_status.json to GitHub Pages (rate-limited)."
        LimitHours = 1
        RepeatMinutes = 5
    }
)

if ($Uninstall) {
    foreach ($t in $tasks) {
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed task: $($t.Name)"
    }
    return
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
        "Magis SPX 0DTE Status API",
        "Magis SPX 0DTE Status Publish",
        "Magis SPX 0DTE Watchdog"
    )) {
        $st = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($st -and $st.State -ne "Running") {
            Start-ScheduledTask -TaskName $name
            Write-Host "Started: $name"
        } elseif ($st) {
            Write-Host "Already running: $name"
        }
    }
    $exec = Get-ScheduledTask -TaskName "Magis SPX 0DTE Executor" -ErrorAction SilentlyContinue
    if ($exec -and $exec.State -ne "Running") {
        Start-ScheduledTask -TaskName "Magis SPX 0DTE Executor"
        Write-Host "Started: Magis SPX 0DTE Executor"
    } else {
        Write-Host "Executor task left as-is (State=$($exec.State)) - not restarted."
    }
}

Get-ScheduledTask -TaskName ($tasks.Name) | Format-Table TaskName, State -AutoSize
Write-Host ""
Write-Host "Local API:  http://127.0.0.1:8765/status"
Write-Host "Cloud file: docs/data/live_status.json (published by Status Publish task)"
Write-Host "Local UI:   .\scripts\serve_dashboard_local.ps1  -> http://127.0.0.1:5500/"
