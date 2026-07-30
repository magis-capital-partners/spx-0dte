# Launch overnight Calmar Wave 3 suite (4 parallel shards) then merge + report.
# Archives Wave 2 artifacts first; clears shard checkpoints (version bump to 3).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$Shards = 4
$SuiteDir = Join-Path $Root "data\overnight_calmar_suite"
$LogDir = Join-Path $SuiteDir "logs"
$ArchiveDir = Join-Path $SuiteDir "wave2_archive"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $ArchiveDir | Out-Null

# Archive Wave 2 summaries if present and not already archived.
foreach ($name in @("summary.json", "summary.csv", "report.json")) {
  $src = Join-Path $SuiteDir $name
  $dst = Join-Path $ArchiveDir $name
  if ((Test-Path $src) -and -not (Test-Path $dst)) {
    Copy-Item $src $dst -Force
    Write-Host "Archived $name -> wave2_archive"
  }
}

# Clear old shard checkpoints so Wave 3 starts clean.
for ($s = 0; $s -lt $Shards; $s++) {
  $shardDir = Join-Path $SuiteDir "shard_$s"
  if (Test-Path $shardDir) {
    Remove-Item -Recurse -Force $shardDir
    Write-Host "Cleared shard_$s"
  }
}

Write-Host "Starting $Shards Wave 3 shards..."
$jobs = @()
for ($s = 0; $s -lt $Shards; $s++) {
  $log = Join-Path $LogDir "wave3_shard_$s.log"
  $errLog = Join-Path $LogDir "wave3_shard_$s.err"
  $argList = @(
    (Join-Path $Root "scripts\run_overnight_calmar_suite.py"),
    "--shard", "$s",
    "--shards", "$Shards",
    "--resume",
    "--checkpoint-every", "25"
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
& $Python (Join-Path $Root "scripts\merge_overnight_calmar_shards.py") --shards $Shards
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Summarizing (rank on selection, validate on sealed holdout)..."
& $Python (Join-Path $Root "scripts\summarize_overnight_calmar_suite.py")
exit $LASTEXITCODE
