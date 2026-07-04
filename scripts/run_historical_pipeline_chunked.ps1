# Full chunked pipeline: backfill year-by-year, then run historical 3D backtest.
#
# Usage:
#   $env:THETADATA_API_KEY = "your-key"
#   .\scripts\run_historical_pipeline_chunked.ps1
#   .\scripts\run_historical_pipeline_chunked.ps1 -FromYear 2020 -BackfillOnly

param(
    [int]$FromYear = 2019,
    [int]$ToYear = 2025,
    [switch]$BackfillOnly,
    [switch]$BacktestOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$YearScript = Join-Path $Root "scripts\run_historical_backfill_by_year.ps1"
$BtScript = Join-Path $Root "scripts\run_historical_backtest_chunk.ps1"

if (-not $BacktestOnly) {
    for ($year = $FromYear; $year -le $ToYear; $year++) {
        Write-Host "======== BACKFILL $year ========"
        & $YearScript -FromYear $year -ToYear $year -SkipEnrich
    }
    Write-Host "======== ENRICH ALL ========"
    & $YearScript -FromYear $FromYear -ToYear $FromYear -BuildOnly -SkipEnrich 2>$null
    $Python = "python"
    & $Python (Join-Path $Root "simulator\feature_enricher.py") --symbol SPXW `
        --processed-dir (Join-Path $Root "data\processed")
}

if (-not $BackfillOnly) {
    Write-Host "======== BACKTEST $FromYear..$ToYear ========"
    & $BtScript -StartDate "$FromYear-01-02" -EndDate "2025-12-29" `
        -ResultsDir "data/historical_3d_mwf_to_daily"
}

Write-Host "Pipeline chunk complete."
