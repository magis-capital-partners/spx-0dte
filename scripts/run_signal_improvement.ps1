param(
    [string]$StartDate = "2023-01-03",
    [string]$EndDate = "2024-04-08",
    [string]$Symbol = "SPXW",
    [switch]$Download,
    [switch]$Build,
    [switch]$Enrich,
    [switch]$All
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Sim = Join-Path $Root "simulator"
$Python = "python"

$argsList = @(
    (Join-Path $Sim "backfill_history.py"),
    "--symbol", $Symbol,
    "--start-date", $StartDate,
    "--end-date", $EndDate
)

if ($All) { $argsList += "--all" }
if ($Download) { $argsList += "--download" }
if ($Build) { $argsList += "--build" }
if ($Enrich) { $argsList += "--enrich" }
if (-not ($All -or $Download -or $Build -or $Enrich)) {
    $argsList += "--enrich"
}

& $Python @argsList

Write-Host "Running tranche diagnostic..."
& $Python (Join-Path $Sim "tranche_diagnostic.py")

Write-Host "Running signal score refit..."
& $Python (Join-Path $Sim "signal_score_refit.py")

Write-Host "Done."
