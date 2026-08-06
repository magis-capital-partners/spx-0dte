# Pre-session state hygiene for the SPX 0DTE live executor.
#
# Reports the control files that gate a session and prunes only provably dead
# ones (KILL files from past dates). Never clears today's or the global KILL:
# those are either deliberate stop-everything switches or incidents worth a look.
#
# Usage:
#   .\scripts\run_session_hygiene.ps1                 # pre-session checks + prune
#   .\scripts\run_session_hygiene.ps1 -CheckStarted    # did the executor start?
#   .\scripts\run_session_hygiene.ps1 -DryRun
#
# Exit codes: 0 clean, 1 warnings, 2 blocked (global/today KILL present).

param(
    [switch]$CheckStarted,
    [switch]$DryRun,
    [int]$PruneDays = 0,
    [switch]$NoNotify
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

. (Join-Path $PSScriptRoot "load_spx_live_env.ps1")

$Python = $env:SPX_PYTHON
if (-not $Python -or -not (Test-Path $Python)) { $Python = "python" }

$argList = @((Join-Path $Root "live\session_hygiene.py"), "--prune-days", "$PruneDays")
if ($CheckStarted) { $argList += "--check-started" }
if ($DryRun) { $argList += "--dry-run" }
if (-not $NoNotify) { $argList += "--notify" }

& $Python @argList
$code = $LASTEXITCODE

# Only a blocker (KILL present, or executor never started) is worth a non-zero
# task result; warnings are informational and should not flag the task red.
if ($code -ge 2) {
    Write-Warning "session hygiene reported a blocker (exit $code)"
    exit $code
}
exit 0
