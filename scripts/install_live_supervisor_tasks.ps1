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
    # Re-registering a Running task stops it, and these two only re-trigger at
    # $DailyAt or logon — so applying them mid-session would leave the watchdog
    # and status API down for the rest of the day. Use this to retime the rest
    # during a live session and pick the services up after the close.
    [switch]$SkipServices,
    [string]$DailyAt = "09:25",
    [string]$PublishStart = "09:30",
    [int]$PublishRepeatMinutes = 5,
    # 09:30 -> 16:05, so the window covers the session and the first post-close
    # publish. The 16:15 daily data update owns the final reconcile + deploy.
    [int]$PublishWindowMinutes = 395,
    [string]$SessionCheckAt = "09:35"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

$Weekdays = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")

$tasks = @(
    @{
        Name = "Magis SPX 0DTE Daily Preflight"
        Script = "run_live_preflight.ps1"
        ExtraArgs = @()
        Description = "Refresh SPX 0DTE live baselines and VIX calendar; never starts the IB executor."
        LimitHours = 1
    },
    @{
        Name = "Magis SPX 0DTE Session Hygiene"
        Script = "run_session_hygiene.ps1"
        ExtraArgs = @()
        Description = "Report KILL/CLEAR control files before the open and prune past-date KILL files."
        LimitHours = 1
    },
    @{
        Name = "Magis SPX 0DTE Watchdog"
        Script = "run_live_watchdog_supervised.ps1"
        ExtraArgs = @("-WriteKill")
        Description = "SPX 0DTE heartbeat watchdog with Magis Slack + WriteKill."
        LimitHours = 14
        IsService = $true
    },
    @{
        Name = "Magis SPX 0DTE Status API"
        Script = "run_session_status_server.ps1"
        ExtraArgs = @()
        Description = "Local dashboard status API on 127.0.0.1:8765; writes sanitized live_status.json."
        LimitHours = 14
        IsService = $true
    },
    @{
        # The executor is manual by design, so a forgotten start is a silent
        # no-trade day. Nothing else notices; this does.
        Name = "Magis SPX 0DTE Session Start Check"
        Script = "run_session_hygiene.ps1"
        ExtraArgs = @("-CheckStarted")
        Description = "Alert if the manual IB executor recorded no session_start shortly after the open."
        LimitHours = 1
        At = $SessionCheckAt
        WeekdaysOnly = $true
        NoLogonTrigger = $true
    },
    @{
        Name = "Magis SPX 0DTE Status Publish"
        Script = "publish_live_status.ps1"
        ExtraArgs = @("-Deploy", "-MinMinutes", "5")
        Description = "Push sanitized live_status.json to GitHub Pages every 5 min during market hours only."
        LimitHours = 1
        At = $PublishStart
        WeekdaysOnly = $true
        NoLogonTrigger = $true
        RepeatMinutes = $PublishRepeatMinutes
        WindowMinutes = $PublishWindowMinutes
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

    $at = if ($spec.ContainsKey("At") -and $spec.At) { $spec.At } else { $DailyAt }

    if ($spec.ContainsKey("WeekdaysOnly") -and $spec.WeekdaysOnly) {
        $primary = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Weekdays -At $at
    } else {
        $primary = New-ScheduledTaskTrigger -Daily -At $at
    }

    # A bounded repetition window keeps a polling task inside market hours. The
    # previous 3650-day duration meant the publisher fired every few minutes
    # around the clock, all weekend, churning live_status.json commits on main.
    if ($spec.ContainsKey("RepeatMinutes") -and $spec.RepeatMinutes) {
        $windowMinutes = if ($spec.ContainsKey("WindowMinutes") -and $spec.WindowMinutes) {
            $spec.WindowMinutes
        } else {
            390
        }
        $template = New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes $spec.RepeatMinutes) `
            -RepetitionDuration (New-TimeSpan -Minutes $windowMinutes)
        $primary.Repetition = $template.Repetition
    }

    $triggers = @($primary)
    if (-not ($spec.ContainsKey("NoLogonTrigger") -and $spec.NoLogonTrigger)) {
        # Long-running services and preflight should come back after a reboot.
        $triggers += (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME)
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

foreach ($t in $tasks) {
    if ($SkipServices -and $t.ContainsKey("IsService") -and $t.IsService) {
        Write-Host "Skipped (service, -SkipServices): $($t.Name)"
        continue
    }
    Register-SpxTask $t
}

if ($StartNow) {
    $startNames = @(
        "Magis SPX 0DTE Daily Preflight",
        "Magis SPX 0DTE Session Hygiene",
        "Magis SPX 0DTE Status Publish"
    )
    if (-not $SkipServices) {
        $startNames += @("Magis SPX 0DTE Status API", "Magis SPX 0DTE Watchdog")
    }
    foreach ($name in $startNames) {
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
