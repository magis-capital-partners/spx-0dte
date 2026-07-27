# Compounding sizing suite — parallel shards by variant, then merge + summarize.
param(
  [int]$Shards = 8,
  [int]$CheckpointEvery = 10,
  [int]$MaxOosDays = 0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$SuiteDir = Join-Path $Root "data\compounding_sizing"
$LogDir = Join-Path $SuiteDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $SuiteDir | Out-Null

Write-Host "Starting $Shards compounding-sizing shards (by variant)..."
$jobs = @()
for ($s = 0; $s -lt $Shards; $s++) {
  $log = Join-Path $LogDir ("compound_shard_{0}.log" -f $s)
  $errLog = Join-Path $LogDir ("compound_shard_{0}.err" -f $s)
  $argList = @(
    (Join-Path $Root "scripts\run_compounding_sizing_suite.py"),
    "--shard", "$s",
    "--shards", "$Shards",
    "--resume",
    "--checkpoint-every", "$CheckpointEvery"
  )
  if ($MaxOosDays -gt 0) { $argList += @("--max-oos-days", "$MaxOosDays") }
  $proc = Start-Process -FilePath $Python -ArgumentList $argList -WorkingDirectory $Root `
    -RedirectStandardOutput $log -RedirectStandardError $errLog -PassThru -NoNewWindow
  $jobs += [PSCustomObject]@{ Shard = $s; Process = $proc; Log = $log }
  Write-Host "  shard $s pid $($proc.Id) -> $log"
}

Write-Host "Waiting for shards..."
$failed = $false
foreach ($j in $jobs) {
  $j.Process.WaitForExit()
  $code = $j.Process.ExitCode
  if ($null -eq $code) { $code = 0 }
  $ckpt = Join-Path $SuiteDir ("shard_{0}\checkpoint.json" -f $j.Shard)
  $complete = $false
  if (Test-Path $ckpt) {
    try { $complete = [bool]((Get-Content $ckpt -Raw | ConvertFrom-Json).complete) } catch {}
  }
  Write-Host "  shard $($j.Shard) done (exit $code, complete=$complete)"
  if (($code -ne 0) -or (-not $complete)) {
    Write-Warning "Shard $($j.Shard) failed. See $($j.Log) / .err"
    $failed = $true
  }
}
if ($failed) { exit 1 }

Write-Host "Merging..."
& $Python (Join-Path $Root "scripts\merge_compounding_sizing_shards.py") --shards "$Shards"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Summarizing..."
& $Python (Join-Path $Root "scripts\summarize_compounding_sizing.py")
exit $LASTEXITCODE
