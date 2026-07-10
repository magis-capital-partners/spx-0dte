# Launch overnight Calmar suite (4 parallel shards) then merge + report.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$Shards = 4
$LogDir = Join-Path $Root "data\overnight_calmar_suite\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host "Starting $Shards shards..."
$jobs = @()
for ($s = 0; $s -lt $Shards; $s++) {
  $log = Join-Path $LogDir "shard_$s.log"
  $errLog = Join-Path $LogDir "shard_$s.err"
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
  $ckpt = Join-Path $Root "data\overnight_calmar_suite\shard_$($j.Shard)\checkpoint.json"
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
  Write-Host "  shard $($j.Shard) done"
}

Write-Host "Merging..."
& $Python (Join-Path $Root "scripts\merge_overnight_calmar_shards.py") --shards $Shards
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Summarizing..."
& $Python (Join-Path $Root "scripts\summarize_overnight_calmar_suite.py")
exit $LASTEXITCODE
