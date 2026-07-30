# Launch overlay Calmar structure shards, then merge + summarize.
param(
  [Parameter(Mandatory = $true)][string]$Phase,
  [int]$Shards = 8,
  [string]$WinnersJson = "",
  [int]$CheckpointEvery = 10,
  [int]$MaxOosDays = 0
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$SuiteDir = Join-Path $Root "data\overlay_calmar_structure\$($Phase.ToLower())"
$LogDir = Join-Path $SuiteDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $SuiteDir | Out-Null

Write-Host "Starting $Shards overlay-calmar shards phase=$Phase (resume enabled)..."
$jobs = @()
for ($s = 0; $s -lt $Shards; $s++) {
  $log = Join-Path $LogDir ("calmar_{0}_shard_{1}.log" -f $Phase.ToLower(), $s)
  $errLog = Join-Path $LogDir ("calmar_{0}_shard_{1}.err" -f $Phase.ToLower(), $s)
  $argList = @(
    (Join-Path $Root "scripts\run_overlay_calmar_structure.py"),
    "--phase", "$Phase",
    "--shard", "$s",
    "--shards", "$Shards",
    "--resume",
    "--checkpoint-every", "$CheckpointEvery"
  )
  if ($MaxOosDays -gt 0) { $argList += @("--max-oos-days", "$MaxOosDays") }
  if ($WinnersJson -ne "") { $argList += @("--winners-json", $WinnersJson) }
  $proc = Start-Process -FilePath $Python -ArgumentList $argList -WorkingDirectory $Root -RedirectStandardOutput $log -RedirectStandardError $errLog -PassThru -NoNewWindow
  $jobs += [PSCustomObject]@{ Shard = $s; Process = $proc; Log = $log }
  Write-Host "  shard $s pid $($proc.Id) -> $log"
}

Write-Host "Waiting for shards..."
$failed = $false
foreach ($j in $jobs) {
  $j.Process.WaitForExit()
  # Start-Process sometimes leaves ExitCode $null even on success; treat null as 0.
  $code = $j.Process.ExitCode
  if ($null -eq $code) { $code = 0 }
  Write-Host "  shard $($j.Shard) done (exit $code)"
  if ($code -ne 0) {
    Write-Warning "Shard $($j.Shard) failed. See $($j.Log) / .err"
    $failed = $true
  }
}
if ($failed) { exit 1 }

$mergeArgs = @(
  (Join-Path $Root "scripts\merge_overlay_calmar_shards.py"),
  "--phase", $Phase,
  "--shards", "$Shards"
)
if ($WinnersJson -ne "") { $mergeArgs += @("--winners-json", $WinnersJson) }
Write-Host "Merging..."
& $Python @mergeArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$sumArgs = @(
  (Join-Path $Root "scripts\summarize_overlay_calmar_structure.py"),
  "--phase", $Phase
)
Write-Host "Summarizing..."
& $Python @sumArgs
exit $LASTEXITCODE
