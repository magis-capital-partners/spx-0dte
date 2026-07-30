# Run 32 jobs (4 shards × 3 suites × batches 1-3) with 3 parallel workers.
#
#   .\scripts\run_cagr_parallel_plan.ps1              # start queue (resume-aware)
#   .\scripts\run_cagr_parallel_plan.ps1 -StatusOnly  # check progress
#   .\scripts\run_cagr_parallel_plan.ps1 -Resume        # skip completed shards
#
# Monitor:
#   python scripts/manage_cagr_suite_queue.py --status --shards 4
#   Get-Content data/cagr_improvement_batches/queue_state.json
#   Get-Content data/cagr_improvement_batches/logs/b1_p3_gates_s0.log -Tail 5

param(
    [int]$Shards = 4,
    [int]$Parallel = 3,
    [switch]$Resume,
    [switch]$StatusOnly,
    [switch]$MergeOnly,
    [int]$CheckpointEvery = 25
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$py = "python"
$mgr = "scripts/manage_cagr_suite_queue.py"

if ($StatusOnly) {
    & $py $mgr --status --shards $Shards
    exit 0
}

if ($MergeOnly) {
    foreach ($b in 1, 2, 3) {
        & $py scripts/merge_cagr_batch_shards.py --batch $b --shards $Shards --suite-mode
    }
    exit 0
}

$args = @($mgr, "--shards", $Shards, "--parallel", $Parallel, "--checkpoint-every", $CheckpointEvery)
if ($Resume) { $args += "--resume" }

Write-Host "Starting suite queue: $Parallel workers, $Shards shards, 32 jobs total"
Write-Host "Logs: data/cagr_improvement_batches/logs/"
& $py @args
