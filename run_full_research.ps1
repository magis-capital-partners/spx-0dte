param(
    [string]$StartDate = "2025-01-02",
    [string]$EndDate = "2025-03-31",
    [string]$Symbol = "SPXW",
    [string]$Interval = "1m",
    [int]$StrikeRange = 80,
    [double]$AccountEquity = 28000000,
    [int]$TrainCount = 40,
    [int]$TestCount = 20
)

$ErrorActionPreference = "Stop"

if (-not $env:THETADATA_API_KEY) {
    throw "THETADATA_API_KEY is not set. Set it in this shell before running."
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Sim = Join-Path $Root "simulator"
$Python = "python"

Write-Host "Downloading ThetaData chains..."
& $Python (Join-Path $Sim "thetadata_downloader.py") `
    --symbol $Symbol `
    --start-date $StartDate `
    --end-date $EndDate `
    --interval $Interval `
    --strike-range $StrikeRange

$processedRoot = Join-Path $Root "data\processed\symbol=$Symbol"
$rawRoot = Join-Path $Root "data\raw\thetadata\symbol=$Symbol"
$dates = Get-ChildItem -LiteralPath $rawRoot -Directory |
    Where-Object { $_.Name -like "date=*" } |
    ForEach-Object { $_.Name.Substring(5) } |
    Sort-Object

Write-Host "Building processed features..."
& $Python (Join-Path $Sim "feature_builder.py") --symbol $Symbol --dates $dates

$dates = Get-ChildItem -LiteralPath $processedRoot -Directory |
    Where-Object { $_.Name -like "date=*" -and (Test-Path (Join-Path $_.FullName "signals.csv")) } |
    ForEach-Object { $_.Name.Substring(5) } |
    Sort-Object

if ($dates.Count -lt ($TrainCount + $TestCount)) {
    throw "Not enough processed dates. Have $($dates.Count), need $($TrainCount + $TestCount)."
}

$dateArgs = $dates | Select-Object -First ($TrainCount + $TestCount)

Write-Host "Running walk-forward grid..."
& $Python (Join-Path $Sim "walk_forward_grid.py") `
    --symbol $Symbol `
    --dates $dateArgs `
    --train-count $TrainCount `
    --test-count $TestCount `
    --account-equity $AccountEquity `
    --results-dir (Join-Path $Root "data\walk_forward_full")

$testDates = $dateArgs | Select-Object -Skip $TrainCount -First $TestCount

Write-Host "Running long-vol overlay on test dates..."
& $Python (Join-Path $Sim "long_vol_overlay.py") `
    --symbol $Symbol `
    --dates $testDates `
    --signals-filename "signals_historical.csv" `
    --account-equity $AccountEquity `
    --results-dir (Join-Path $Root "data\long_vol_full")

Write-Host "Done."
Write-Host "Walk-forward results: $(Join-Path $Root 'data\walk_forward_full\walk_forward_grid.csv')"
Write-Host "Long-vol results: $(Join-Path $Root 'data\long_vol_full\long_vol_daily_summary.csv')"
