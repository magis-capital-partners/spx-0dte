# Daily SPXW catch-up: ThetaData download -> build -> enrich -> dashboard export -> optional Pages deploy.
#
# Runs locally (Windows Task Scheduler). Does NOT use GitHub Actions minutes.
# GitHub Pages branch deploy from docs/ also uses no Actions minutes.
#
# Scheduled for 4:15 PM ET (not 4:05) so the SPX daily-close download has time
# to reflect the finalized settlement print before reconcile_live.py values
# today's 0DTE spreads against it.
#
# Usage (from repo root):
#   $env:THETADATA_API_KEY = "..."   # or set as User env var permanently
#   .\scripts\daily_data_update.ps1
#   .\scripts\daily_data_update.ps1 -Deploy
#   .\scripts\daily_data_update.ps1 -StartDate 2026-07-07 -EndDate 2026-07-08 -Deploy
#   .\scripts\daily_data_update.ps1 -SkipDownload -SkipDeploy   # rebuild/export only
#
# Schedule: .\scripts\install_daily_update_task.ps1

param(
    [string]$StartDate = "",
    [string]$EndDate = "",
    [string]$Symbol = "SPXW",
    [double]$Equity = 13000000,
    [int]$LookbackDays = 14,
    [int]$ChunkSize = 5,
    [switch]$Deploy,
    [switch]$SkipDownload,
    [switch]$SkipDeploy,
    [switch]$FullExport,
    [string]$LogDir = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
Set-Location $Root

$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

if (-not $LogDir) { $LogDir = Join-Path $Root "data\logs" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$LogFile = Join-Path $LogDir "daily_update_$stamp.log"

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

function Invoke-Py {
    param([Parameter(Mandatory = $true)][string[]]$PyArgs)
    Write-Log ("python " + ($PyArgs -join " "))
    & $Python @PyArgs 2>&1 | ForEach-Object {
        $text = "$_"
        Write-Host $text
        Add-Content -Path $LogFile -Value $text
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): python $($PyArgs -join ' ')"
    }
}

function Get-LatestProcessedDate {
    $base = Join-Path $Root "data\processed\symbol=$Symbol"
    if (-not (Test-Path $base)) { return $null }
    $dirs = Get-ChildItem $base -Directory | Where-Object { $_.Name -like "date=*" } | Sort-Object Name
    if (-not $dirs) { return $null }
    return $dirs[-1].Name.Substring(5)
}

try {
    Write-Log "=== daily_data_update start ==="
    Write-Log "Root=$Root Log=$LogFile"

    if (-not $EndDate) {
        $EndDate = (Get-Date).ToString("yyyy-MM-dd")
    }
    if (-not $StartDate) {
        $latest = Get-LatestProcessedDate
        if ($latest) {
            $StartDate = ([datetime]::ParseExact($latest, "yyyy-MM-dd", $null)).AddDays(1).ToString("yyyy-MM-dd")
        } else {
            $StartDate = (Get-Date).AddDays(-$LookbackDays).ToString("yyyy-MM-dd")
        }
        # Also re-check a short lookback in case a mid-range day failed previously.
        $lookbackStart = (Get-Date).AddDays(-$LookbackDays).ToString("yyyy-MM-dd")
        if ($lookbackStart -lt $StartDate) {
            # Prefer catching gaps: start from lookback, backfill only downloads missing raw.
            $StartDate = $lookbackStart
        }
    }

    Write-Log "Window: $StartDate .. $EndDate"

    if (-not $SkipDownload) {
        if (-not $env:THETADATA_API_KEY) {
            throw "THETADATA_API_KEY is not set. Set a User environment variable or pass it in this shell."
        }
        Write-Log "=== ThetaData download + build ==="
        Invoke-Py -PyArgs @(
            (Join-Path $Root "simulator\backfill_history.py"),
            "--symbol", $Symbol,
            "--start-date", $StartDate,
            "--end-date", $EndDate,
            "--chunk-size", "$ChunkSize",
            "--download",
            "--build"
        )
    } else {
        Write-Log "Skipping ThetaData download (-SkipDownload)"
        Write-Log "=== Build processed from raw (if needed) ==="
        Invoke-Py -PyArgs @(
            (Join-Path $Root "simulator\backfill_history.py"),
            "--symbol", $Symbol,
            "--start-date", $StartDate,
            "--end-date", $EndDate,
            "--chunk-size", "$ChunkSize",
            "--build"
        )
    }

    # Discover which dates in the window now have processed output (for targeted enrich).
    $processedBase = Join-Path $Root "data\processed\symbol=$Symbol"
    $newDates = @()
    if (Test-Path $processedBase) {
        $newDates = Get-ChildItem $processedBase -Directory |
            Where-Object { $_.Name -like "date=*" } |
            ForEach-Object { $_.Name.Substring(5) } |
            Where-Object { $_ -ge $StartDate -and $_ -le $EndDate } |
            Sort-Object
    }
    Write-Log ("Processed dates in window: " + ($(if ($newDates) { $newDates -join ", " } else { "(none)" })))

    Write-Log "=== VIX calendar refresh ==="
    Invoke-Py -PyArgs @(
        (Join-Path $Root "scripts\download_vix_daily.py"),
        "--start-date", "2019-01-01",
        "--end-date", $EndDate
    )

    Write-Log "=== Index calendars (SPX / IXIC / RUT) ==="
    Invoke-Py -PyArgs @(
        (Join-Path $Root "scripts\download_index_daily.py"),
        "--start-date", "2019-01-01",
        "--end-date", $EndDate
    )

    if ($newDates.Count -gt 0) {
        Write-Log "=== Feature enrich (new dates) ==="
        Invoke-Py -PyArgs (@(
            (Join-Path $Root "simulator\feature_enricher.py"),
            "--symbol", $Symbol,
            "--dates"
        ) + $newDates)

        Write-Log "=== VIX enrich (new dates) ==="
        Invoke-Py -PyArgs (@(
            (Join-Path $Root "simulator\vix_signal_enricher.py"),
            "--symbol", $Symbol,
            "--dates"
        ) + $newDates)
    } else {
        Write-Log "No new processed dates in window; skipping feature/VIX enrich."
    }

    Write-Log "=== Refresh data inventory manifest ==="
    Invoke-Py -PyArgs @((Join-Path $Root "scripts\update_data_inventory.py"))

    $exportArgs = @("--incremental")
    if ($FullExport) { $exportArgs = @() }

    Write-Log "=== Dashboard run export (production presets) ==="
    # p3_poststop_compounding_f1 is published by sync_dashboard.ps1, so it must stay current here too.
    foreach ($preset in @("p3_poststop_cooldown_120", "p3_trend_bc_085", "p3_poststop_compounding_f1")) {
        Invoke-Py -PyArgs (@(
            (Join-Path $Root "simulator\export_dashboard_run.py"),
            "--preset", $preset
        ) + $exportArgs)
    }

    Write-Log "=== Live/paper reconcile (sessions with config.json) ==="
    $liveRoot = Join-Path $Root "data\live"
    $reconciled = 0
    if (Test-Path $liveRoot) {
        $liveDays = Get-ChildItem $liveRoot -Directory |
            Where-Object { Test-Path (Join-Path $_.FullName "config.json") } |
            ForEach-Object { $_.Name } |
            Where-Object { $_ -ge $StartDate -and $_ -le $EndDate } |
            Sort-Object
        foreach ($day in $liveDays) {
            Write-Log "Reconcile live session $day"
            Invoke-Py -PyArgs @(
                (Join-Path $Root "simulator\reconcile_live.py"),
                "--date", $day
            )
            $reconciled++
        }
    }
    Write-Log ("Live sessions reconciled: " + $reconciled)

    $doDeploy = $Deploy -and -not $SkipDeploy
    if ($doDeploy) {
        Write-Log "=== Rebuild + deploy dashboard (Pages branch deploy; no Actions minutes) ==="
        & (Join-Path $Root "scripts\sync_dashboard.ps1") -Deploy -Equity $Equity
        if ($LASTEXITCODE -ne 0) { throw "sync_dashboard.ps1 failed" }
    } else {
        Write-Log "=== Rebuild dashboard JSON (local only) ==="
        & (Join-Path $Root "scripts\sync_dashboard.ps1") -Equity $Equity
        if ($LASTEXITCODE -ne 0) { throw "sync_dashboard.ps1 failed" }
        Write-Log "Skipped deploy. Re-run with -Deploy to push docs/ and trigger Pages."
    }

    Write-Log "=== daily_data_update complete ==="
    exit 0
}
catch {
    Write-Log ("ERROR: " + $_.Exception.Message)
    Write-Log $_.ScriptStackTrace
    exit 1
}
