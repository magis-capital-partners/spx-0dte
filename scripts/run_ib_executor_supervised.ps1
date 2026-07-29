# Supervised IB executor: detached from an interactive terminal, restart on crash.
# Loads Magis Slack secrets, respects executor.lock, backs off on repeated failures.
#
# Usage:
#   .\scripts\run_ib_executor_supervised.ps1
#   .\scripts\run_ib_executor_supervised.ps1 -MaxRestarts 20 -RestartDelaySeconds 15

param(
    [int]$MaxRestarts = 50,
    [int]$RestartDelaySeconds = 20,
    [string]$Python = "",
    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

. (Join-Path $PSScriptRoot "load_spx_live_env.ps1")

if (-not $Python) {
    $Python = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
}
if (-not $Python) {
    $Python = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
}
if (-not (Test-Path $Python)) {
    throw "Python not found: $Python"
}

if (-not $LogDir) {
    $LogDir = Join-Path $Root "data\live\supervisor"
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$logFile = Join-Path $LogDir ("executor-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

function Write-SupLog([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "o"), $msg
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

Write-SupLog "supervisor start python=$Python root=$Root slack_set=$([bool]$env:SPX_SLACK_WEBHOOK_URL)"

$restarts = 0
while ($true) {
    $today = Get-Date -Format "yyyy-MM-dd"
    $lockPath = Join-Path $Root "data\live\$today\executor.lock"
    if (Test-Path $lockPath) {
        try {
            $payload = Get-Content $lockPath -Raw | ConvertFrom-Json
            $oldPid = [int]$payload.pid
            if ($oldPid -gt 0) {
                $alive = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
                if ($alive) {
                    Write-SupLog "another executor alive pid=$oldPid - supervisor waiting 60s"
                    Start-Sleep -Seconds 60
                    continue
                }
            }
        } catch {
            Write-SupLog "lock parse warn: $_"
        }
    }

    Write-SupLog "starting ib_executor (restarts=$restarts/$MaxRestarts)"
    $p = Start-Process -FilePath $Python `
        -ArgumentList @("live/ib_executor.py") `
        -WorkingDirectory $Root `
        -PassThru `
        -NoNewWindow `
        -RedirectStandardOutput (Join-Path $LogDir "executor-stdout.log") `
        -RedirectStandardError (Join-Path $LogDir "executor-stderr.log")

    Wait-Process -Id $p.Id
    try { $p.Refresh() } catch {}
    $code = $p.ExitCode
    if ($null -eq $code) { $code = -1 }

    # After force_flat_time (~16:00) the executor ends cleanly; Start-Process can
    # leave ExitCode null on Windows. Treat "session end" in stdout as clean stop.
    $stdoutPath = Join-Path $LogDir "executor-stdout.log"
    $sawSessionEnd = $false
    if (Test-Path $stdoutPath) {
        $tail = Get-Content $stdoutPath -Tail 30 -ErrorAction SilentlyContinue
        if ($tail -match 'session end') { $sawSessionEnd = $true }
    }
    $nowT = Get-Date
    $afterFlat = ($nowT.TimeOfDay -ge [TimeSpan]::Parse("16:00:00"))

    Write-SupLog "ib_executor exited code=$code pid=$($p.Id) session_end=$sawSessionEnd after_flat=$afterFlat"

    if ($code -eq 0 -or $sawSessionEnd -or $afterFlat) {
        Write-SupLog "clean / end-of-day exit - supervisor stopping"
        break
    }

    $restarts++
    if ($restarts -gt $MaxRestarts) {
        Write-SupLog "max restarts exceeded - supervisor stopping"
        if ($env:SPX_SLACK_WEBHOOK_URL) {
            & $Python -c "import sys; sys.path.insert(0,'live'); from slack_notify import notify_slack; notify_slack('[spx-0dte] supervisor stopped after max restarts')"
        }
        break
    }

    Write-SupLog "restarting in ${RestartDelaySeconds}s"
    Start-Sleep -Seconds $RestartDelaySeconds
}
