# Run historical 3D backtest over a date window (resumable chunks).
#
# Usage:
#   .\scripts\run_historical_backtest_chunk.ps1 -StartDate 2019-01-02 -EndDate 2019-12-31
#   .\scripts\run_historical_backtest_chunk.ps1 -StartDate 2023-01-03 -EndDate 2025-12-29 -ResultsDir data/historical_3d_regression

param(
    [string]$StartDate = "2019-01-02",
    [string]$EndDate = "2025-12-29",
    [string]$ResultsDir = "data/historical_3d_mwf_to_daily",
    [string]$SizingScheme = "linear_decay_downsize",
    [int]$TrainCount = 40
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$out = Join-Path $Root $ResultsDir
& $Python (Join-Path $Root "simulator\historical_3d_backtest.py") `
    --start-date $StartDate `
    --end-date $EndDate `
    --sizing-scheme $SizingScheme `
    --train-count $TrainCount `
    --results-dir $out
if ($LASTEXITCODE -ne 0) { throw "Backtest failed (exit $LASTEXITCODE)" }
