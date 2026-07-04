# Download + build SPXW history one calendar year at a time (resumable).
#
# Usage:
#   $env:THETADATA_API_KEY = "your-key"
#   .\scripts\run_historical_backfill_by_year.ps1
#   .\scripts\run_historical_backfill_by_year.ps1 -FromYear 2020 -ToYear 2022
#   .\scripts\run_historical_backfill_by_year.ps1 -BuildOnly -FromYear 2019 -ToYear 2019

param(
    [int]$FromYear = 2019,
    [int]$ToYear = 2025,
    [int]$ChunkSize = 10,
    [switch]$BuildOnly,
    [switch]$SkipEnrich
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
$Backfill = Join-Path $Root "simulator\backfill_history.py"
$LogDir = Join-Path $Root "data\backfill\year_logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (-not $BuildOnly -and -not $env:THETADATA_API_KEY) {
    throw "THETADATA_API_KEY is not set."
}

for ($year = $FromYear; $year -le $ToYear; $year++) {
    $start = "$year-01-01"
    $end = "$year-12-31"
    $log = Join-Path $LogDir "backfill_$year.log"
    Write-Host "=== $year ($start .. $end) -> $log ==="

    $args = @(
        $Backfill,
        "--start-date", $start,
        "--end-date", $end,
        "--chunk-size", $ChunkSize
    )
    if ($BuildOnly) {
        $args += "--build"
    } else {
        $args += "--download", "--build"
    }

    & $Python @args *>&1 | Tee-Object -FilePath $log
    if ($LASTEXITCODE -ne 0) { throw "Backfill failed for $year (exit $LASTEXITCODE). See $log" }
}

if (-not $SkipEnrich) {
    Write-Host "=== Enrich all processed dates ==="
    & $Python (Join-Path $Root "simulator\feature_enricher.py") --symbol SPXW `
        --processed-dir (Join-Path $Root "data\processed") 2>&1 |
        Tee-Object -FilePath (Join-Path $LogDir "enrich.log")
    if ($LASTEXITCODE -ne 0) { throw "Enrich failed (exit $LASTEXITCODE)" }
}

Write-Host "Done $FromYear..$ToYear"
