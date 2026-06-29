# Seven-step iterative improvement pipeline (2026-06-29).
#
# 1. Backfill 2023-2025 SPXW history (ThetaData download + build + enrich)
# 2. Tranche diagnostic (why the engine skips most tranches)
# 3. MBH daily comparison (recon shape vs MBH daily returns)
# 4. MBH green-day score refit
# 5. PM refinement study (harvest mode, gate ablations, slippage stress)
# 6. Robustness study (full-history flatten + best profiles, bootstrap CIs)
# 7. Profile reruns + dashboard refresh
#
# Requires THETADATA_API_KEY for Step 1 download (753 trading days for 2023-2025).
#
# Usage:
#   $env:THETADATA_API_KEY = "your-key"
#   .\scripts\run_seven_step_pipeline.ps1
#   .\scripts\run_seven_step_pipeline.ps1 -SkipDownload   # reuse existing processed data
#   .\scripts\run_seven_step_pipeline.ps1 -EndDate "2025-09-30"  # smaller window

param(
    [string]$StartDate = "2023-01-03",
    [string]$EndDate = "2025-12-31",
    [string]$Symbol = "SPXW",
    [double]$Equity = 13000000,
    [int]$TrainCount = 40,
    [int]$ChunkSize = 10,
    [switch]$SkipDownload,
    [switch]$SkipDashboardPush
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$Sim = Join-Path $Root "simulator"
$Data = Join-Path $Root "data"
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

function Invoke-Py {
    param([string[]]$Args)
    & $Python @Args
    if ($LASTEXITCODE -ne 0) { throw "Command failed: python $($Args -join ' ')" }
}

if (-not $SkipDownload -and -not $env:THETADATA_API_KEY) {
    throw "THETADATA_API_KEY is not set. Set it in this shell or pass -SkipDownload if processed data already exists under data/processed."
}

Write-Host "=== Step 1: Backfill $StartDate .. $EndDate ==="
$backfillArgs = @(
    (Join-Path $Sim "backfill_history.py"),
    "--symbol", $Symbol,
    "--start-date", $StartDate,
    "--end-date", $EndDate,
    "--chunk-size", $ChunkSize,
    "--enrich"
)
if (-not $SkipDownload) {
    $backfillArgs += "--all"
}
Invoke-Py $backfillArgs

Write-Host "=== Step 2: Validation + tranche export + tranche diagnostic ==="
$phase0 = Join-Path $Data "phase0_tranche_full"
Invoke-Py @(
    (Join-Path $Sim "regime_validation.py"),
    "--start-date", $StartDate, "--end-date", $EndDate,
    "--train-count", $TrainCount,
    "--account-equity", $Equity,
    "--results-dir", $phase0,
    "--flatten-on-daily-loss", "--two-tier-engine", "--event-controls",
    "--time-of-day-controls", "--portfolio-allocator",
    "--baseline-contracts", "1140", "--daily-credit-cap-pct", "0.05",
    "--portfolio-margin-budget-pct", "0.40", "--core-margin-budget-pct", "0.35",
    "--exploratory-margin-budget-pct", "0.02",
    "--exploratory-min-score", "2.40", "--exploratory-max-score", "2.49"
)

Invoke-Py @(
    (Join-Path $Sim "tranche_diagnostic.py"),
    "--tranche-csv", (Join-Path $phase0 "tranche_snapshots.csv"),
    "--output-dir", (Join-Path $Data "signal_diagnostics_full")
)

Write-Host "=== Step 3: MBH daily comparison (2025 overlap) ==="
$mbhOut = Join-Path $Data "mbh_vs_recon_2025"
Invoke-Py @(
    (Join-Path $Sim "mbh_daily_comparison.py"),
    "--recon-daily", (Join-Path $phase0 "daily_regime_validation.csv"),
    "--year", "2025",
    "--out-prefix", $mbhOut
)

Write-Host "=== Step 4: MBH green-day score refit ==="
Invoke-Py @(
    (Join-Path $Sim "mbh_green_day_refit.py"),
    "--results-dir", (Join-Path $Data "mbh_green_day_refit"),
    "--tranche-path", (Join-Path $phase0 "tranche_snapshots.csv"),
    "--source", "phase0_tranches"
)

Write-Host "=== Step 5: PM refinement study ==="
Invoke-Py @(
    (Join-Path $Sim "pm_refinement_study.py"),
    "--output-dir", (Join-Path $Data "pm_refinement_study"),
    "--grid-start", $StartDate,
    "--grid-end", $EndDate,
    "--gate-start", $StartDate,
    "--gate-end", $EndDate
)

Write-Host "=== Step 6: Robustness study (flatten + best profiles) ==="
Invoke-Py @(
    (Join-Path $Sim "robustness_study.py"),
    "--results-dir", (Join-Path $Data "phase2_robustness_full"),
    "--train-count", $TrainCount,
    "--account-equity", $Equity
)

Write-Host "=== Step 7: Profile reruns + dashboard rebuild ==="
& (Join-Path $Root "run_profiles.ps1") -Start $StartDate -End $EndDate -Equity $Equity -TrainCount $TrainCount

if (-not $SkipDashboardPush) {
    $syncScript = Join-Path $ScriptDir "sync_dashboard.ps1"
    if (Test-Path $syncScript) {
        Write-Host "=== Dashboard sync (optional push) ==="
        & $syncScript -Push -Equity $Equity
    }
}

Write-Host "=== Seven-step pipeline complete ==="
Write-Host "Key outputs:"
Write-Host "  data/phase0_tranche_full/daily_regime_validation.csv"
Write-Host "  data/signal_diagnostics_full/"
Write-Host "  data/mbh_vs_recon_2025.md"
Write-Host "  data/mbh_green_day_refit/"
Write-Host "  data/pm_refinement_study/pm_refinement_report.md"
Write-Host "  data/phase2_robustness_full/robustness_report.md"
Write-Host "  data/profile_best/ (and baseline/flatten/aggressive)"
Write-Host "  dashboard/data/dashboard_data.json"
