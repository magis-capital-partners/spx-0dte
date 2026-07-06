# Download + build SPXW history with parallel workers (one calendar year each).
#
# Usage:
#   $env:THETADATA_API_KEY = "your-key"
#   .\scripts\run_parallel_year_backfill.ps1
#   .\scripts\run_parallel_year_backfill.ps1 -FromYear 2020 -ToYear 2022 -MaxParallel 2
#   .\scripts\run_parallel_year_backfill.ps1 -Years 2021,2022,2020 -MaxParallel 3
#   .\scripts\run_parallel_year_backfill.ps1 -BuildOnly -Years 2019

param(
    [int]$FromYear = 2019,
    [int]$ToYear = 2026,
    [object]$Years,
    [int]$MaxParallel = 3,
    [switch]$BuildOnly,
    [switch]$SkipPostSteps
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

if ($Years) {
    if ($Years -is [string]) {
        $yearList = @($Years -split '[,\s]+' | Where-Object { $_ -match '^\d{4}$' } | ForEach-Object { [int]$_ })
    } elseif ($Years -is [System.Array]) {
        $yearList = @($Years | ForEach-Object { [int]$_ })
    } else {
        $yearList = @([int]$Years)
    }
} else {
    $yearList = @($FromYear..$ToYear)
}
if ($yearList.Count -eq 0) {
    throw "No years to backfill. Pass -Years or -FromYear/-ToYear."
}

function Invoke-YearBackfill {
    param([int]$Year)

    $start = "$Year-01-01"
    $end = "$Year-12-31"
    $log = Join-Path $LogDir "backfill_$Year.log"
    Write-Host "  start $Year ($start .. $end) -> $log"

    $pyArgs = @(
        $Backfill,
        "--start-date", $start,
        "--end-date", $end,
        "--chunk-size", "5"
    )
    if ($BuildOnly) {
        $pyArgs += "--build"
    } else {
        $pyArgs += "--download", "--build"
    }

    $job = Start-Job -Name "backfill_$Year" -ScriptBlock {
        param($PythonPath, $Arguments, $LogPath, $ApiKey)
        if ($ApiKey) { $env:THETADATA_API_KEY = $ApiKey }
        try {
            & $PythonPath @Arguments *>&1 | Out-File -FilePath $LogPath -Encoding utf8
            return [pscustomobject]@{
                ExitCode = $LASTEXITCODE
                Year = ($Arguments | Where-Object { $_ -match '^\d{4}-\d{2}-\d{2}$' } | Select-Object -First 1).Substring(0, 4)
            }
        } catch {
            $_ | Out-File -FilePath $LogPath -Append -Encoding utf8
            return [pscustomobject]@{ ExitCode = 1; Year = "unknown" }
        }
    } -ArgumentList $Python, $pyArgs, $log, $env:THETADATA_API_KEY

    return [pscustomobject]@{
        Year = $Year
        Job = $job
        Log = $log
    }
}

function Wait-BackfillBatch {
    param([array]$Workers)

    $failed = @()
    foreach ($w in $Workers) {
        $result = Receive-Job -Job $w.Job -Wait -AutoRemoveJob
        $exitCode = if ($null -eq $result) { 1 } else { [int]$result.ExitCode }
        if ($exitCode -ne 0) {
            $failed += $w.Year
            Write-Host "  FAILED $($w.Year) (exit $exitCode) - see $($w.Log)" -ForegroundColor Red
        } else {
            Write-Host "  OK $($w.Year)" -ForegroundColor Green
        }
    }
    return $failed
}

Write-Host "=== Parallel backfill: years $($yearList -join ', ') (MaxParallel=$MaxParallel) ==="
$allFailed = @()

for ($i = 0; $i -lt $yearList.Count; $i += $MaxParallel) {
    $batchEnd = [Math]::Min($i + $MaxParallel - 1, $yearList.Count - 1)
    $batch = $yearList[$i..$batchEnd]
    Write-Host "--- Batch: $($batch -join ', ') ---"
    $workers = @()
    foreach ($year in $batch) {
        $workers += Invoke-YearBackfill -Year $year
    }
    $batchFailed = Wait-BackfillBatch -Workers $workers
    if ($batchFailed.Count -gt 0) {
        $allFailed += $batchFailed
    }
}

if ($allFailed.Count -gt 0) {
    throw "Backfill failed for year(s): $($allFailed -join ', '). See data/backfill/year_logs/"
}

if ($SkipPostSteps) {
    Write-Host "=== Skipping enrich / inventory / audit (SkipPostSteps) ==="
    exit 0
}

Write-Host "=== Enrich all processed dates ==="
$enrichLog = Join-Path $LogDir "enrich.log"
& $Python (Join-Path $Root "simulator\feature_enricher.py") --symbol SPXW `
    --processed-dir (Join-Path $Root "data\processed") 2>&1 |
    Tee-Object -FilePath $enrichLog
if ($LASTEXITCODE -ne 0) { throw "Enrich failed (exit $LASTEXITCODE)" }

Write-Host "=== Update data inventory ==="
& $Python (Join-Path $Root "scripts\update_data_inventory.py") 2>&1 |
    Tee-Object -FilePath (Join-Path $LogDir "inventory.log")
if ($LASTEXITCODE -ne 0) { throw "Inventory update failed (exit $LASTEXITCODE)" }

Write-Host "=== Audit eligible coverage ==="
& $Python (Join-Path $Root "scripts\audit_eligible_coverage.py") 2>&1 |
    Tee-Object -FilePath (Join-Path $LogDir "audit.log")
if ($LASTEXITCODE -ne 0) { throw "Audit failed (exit $LASTEXITCODE)" }

Write-Host "Done. Years: $($yearList -join ', ')"
