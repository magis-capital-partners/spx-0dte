# Launch Why-Not-Look-At suite (8 parallel date shards) then merge + report.
# Checkpoints every 10 OOS days so progress survives crashes.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$Shards = 8
$SuiteDir = Join-Path $Root "data\why_not_look_at"
$LogDir = Join-Path $SuiteDir "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $SuiteDir | Out-Null

Write-Host "Starting $Shards Why-Not-Look-At shards (resume enabled)..."
$jobs = @()
for ($s = 0; $s -lt $Shards; $s++) {
  $log = Join-Path $LogDir "wnla_shard_$s.log"
  $errLog = Join-Path $LogDir "wnla_shard_$s.err"
  $argList = @(
    (Join-Path $Root "scripts\run_why_not_look_at_suite.py"),
    "--shard", "$s",
    "--shards", "$Shards",
    "--resume",
    "--checkpoint-every", "10"
  )
  $proc = Start-Process -FilePath $Python -ArgumentList $argList -WorkingDirectory $Root -RedirectStandardOutput $log -RedirectStandardError $errLog -PassThru -NoNewWindow
  $jobs += [PSCustomObject]@{ Shard = $s; Process = $proc; Log = $log }
  Write-Host "  shard $s pid $($proc.Id) -> $log"
}

Write-Host "Waiting for shards..."
foreach ($j in $jobs) {
  $j.Process.WaitForExit()
  $ckpt = Join-Path $SuiteDir "shard_$($j.Shard)\checkpoint.json"
  $complete = $false
  if (Test-Path $ckpt) {
    try {
      $state = Get-Content $ckpt -Raw | ConvertFrom-Json
      $complete = [bool]$state.complete
    } catch {}
  }
  if (-not $complete -and $j.Process.ExitCode -ne 0) {
    Write-Warning "Shard $($j.Shard) may have failed (exit $($j.Process.ExitCode)). See $($j.Log)"
  }
  Write-Host "  shard $($j.Shard) done (exit $($j.Process.ExitCode))"
}

Write-Host "Merging (full + selection + holdout)..."
& $Python (Join-Path $Root "scripts\merge_why_not_look_at_shards.py") --shards $Shards
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Summarizing (rank on selection, validate on sealed holdout)..."
& $Python (Join-Path $Root "scripts\summarize_why_not_look_at_suite.py")
exit $LASTEXITCODE
