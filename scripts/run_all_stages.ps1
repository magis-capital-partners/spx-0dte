param(
    [string]$StartDate = "2023-01-03",
    [string]$EndDate = "2024-04-08",
    [string]$Symbol = "SPXW"
)

$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Sim = Join-Path $Root "simulator"
$Data = Join-Path $Root "data"
$Python = "python"

if (-not $env:THETADATA_API_KEY) {
    Write-Host "WARNING: THETADATA_API_KEY is not set. Stage 1 download will be skipped."
}

Write-Host "=== Stage 1: Backfill download + build + enrich ==="
& $Python (Join-Path $Sim "backfill_history.py") --all --start-date $StartDate --end-date $EndDate --chunk-size 10

Write-Host "=== Stage 2: Full-history tranche export + diagnostic ==="
& $Python (Join-Path $Sim "regime_validation.py") `
    --results-dir (Join-Path $Data "phase0_tranche_full") `
    --flatten-on-daily-loss --two-tier-engine --event-controls `
    --time-of-day-controls --portfolio-allocator `
    --baseline-contracts 1140 --daily-credit-cap-pct 0.05 `
    --portfolio-margin-budget-pct 0.40 --core-margin-budget-pct 0.35 `
    --exploratory-margin-budget-pct 0.02 `
    --exploratory-min-score 2.40 --exploratory-max-score 2.49

& $Python (Join-Path $Sim "tranche_diagnostic.py") `
    --tranche-csv (Join-Path $Data "phase0_tranche_full\tranche_snapshots.csv") `
    --output-dir (Join-Path $Data "signal_diagnostics_full")

Write-Host "=== Stage 3: Signal score refit ==="
& $Python (Join-Path $Sim "signal_score_refit.py") `
    --results-dir (Join-Path $Data "signal_refit_full")

Write-Host "=== Stage 4: Phase 2 robustness study ==="
& $Python (Join-Path $Sim "robustness_study.py") `
    --results-dir (Join-Path $Data "phase2_robustness_full")

Write-Host "=== All stages complete ==="
