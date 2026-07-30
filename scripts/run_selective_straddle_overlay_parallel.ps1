# Launch selective straddle/IC overlay suite shards, then merge + summarize.
param(
  [Parameter(Mandatory = $true)][string]$Phase,
  [int]$Shards = 8,
  [string]$WinnersJson = "",
  [switch]$Promote,
  [int]$CheckpointEvery = 10
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$SuiteDir = Join-Path $Root "data\selective_straddle_overlay\$($Phase.ToLower())"
$LogDir = Join-Path $SuiteDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $SuiteDir | Out-Null

Write-Host "Starting $Shards selective-overlay shards phase=$Phase (resume enabled)..."
$jobs = @()
for ($s = 0; $s -lt $Shards; $s++) {
  $log = Join-Path $LogDir ("overlay_{0}_shard_{1}.log" -f $Phase.ToLower(), $s)
  $errLog = Join-Path $LogDir ("overlay_{0}_shard_{1}.err" -f $Phase.ToLower(), $s)
  $argList = @(
    (Join-Path $Root "scripts\run_selective_straddle_overlay.py"),
    "--phase", "$Phase",
    "--shard", "$s",
    "--shards", "$Shards",
    "--resume",
    "--checkpoint-every", "$CheckpointEvery"
  )
  if ($WinnersJson -ne "") {
    $argList += @("--winners-json", $WinnersJson)
  }
  $proc = Start-Process -FilePath $Python -ArgumentList $argList -WorkingDirectory $Root -RedirectStandardOutput $log -RedirectStandardError $errLog -PassThru -NoNewWindow
  $jobs += [PSCustomObject]@{ Shard = $s; Process = $proc; Log = $log }
  Write-Host "  shard $s pid $($proc.Id) -> $log"
}

Write-Host "Waiting for shards..."
foreach ($j in $jobs) {
  $j.Process.WaitForExit()
  Write-Host "  shard $($j.Shard) done (exit $($j.Process.ExitCode))"
  if ($j.Process.ExitCode -ne 0) {
    Write-Warning "Shard $($j.Shard) failed. See $($j.Log) / .err"
  }
}

$mergeArgs = @(
  (Join-Path $Root "scripts\merge_selective_straddle_shards.py"),
  "--phase", $Phase,
  "--shards", "$Shards"
)
if ($WinnersJson -ne "") {
  $mergeArgs += @("--winners-json", $WinnersJson)
}
Write-Host "Merging..."
& $Python @mergeArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$sumArgs = @(
  (Join-Path $Root "scripts\summarize_selective_straddle_overlay.py"),
  "--phase", $Phase
)
if ($Promote) { $sumArgs += "--promote" }
Write-Host "Summarizing..."
& $Python @sumArgs
exit $LASTEXITCODE
