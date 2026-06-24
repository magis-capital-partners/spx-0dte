# Runs the key validated strategy profiles over the validation window and
# rebuilds the dashboard data blob. Each run is a true simulator rerun.
#
# Usage:
#   .\run_profiles.ps1                       # full window 2025-04-01..2025-09-30
#   .\run_profiles.ps1 -Start 2025-04-01 -End 2025-09-30 -Equity 13000000

param(
  [string]$Start = "2025-04-01",
  [string]$End = "2025-09-30",
  [double]$Equity = 13000000,
  [int]$TrainCount = 40
)

$ErrorActionPreference = "Stop"
$common = @(
  "--start-date", $Start, "--end-date", $End, "--train-count", $TrainCount,
  "--account-equity", $Equity,
  "--two-tier-engine", "--event-controls", "--time-of-day-controls",
  "--exploratory-min-score", "2.40", "--exploratory-max-score", "2.49",
  "--portfolio-allocator"
)

Write-Host "== baseline (prior best, no governor) =="
python simulator\regime_validation.py @common --baseline-contracts 1140 --daily-credit-cap-pct 0.05 `
  --portfolio-margin-budget-pct 0.40 --core-margin-budget-pct 0.35 --exploratory-margin-budget-pct 0.02 `
  --results-dir "data\profile_baseline"

Write-Host "== flatten (1x + flatten governor) =="
python simulator\regime_validation.py @common --baseline-contracts 1140 --daily-credit-cap-pct 0.05 `
  --portfolio-margin-budget-pct 0.40 --core-margin-budget-pct 0.35 --exploratory-margin-budget-pct 0.02 `
  --flatten-on-daily-loss --results-dir "data\profile_flatten"

Write-Host "== best (2x deploy + flatten) =="
python simulator\regime_validation.py @common --baseline-contracts 2280 --daily-credit-cap-pct 0.10 `
  --portfolio-margin-budget-pct 0.80 --core-margin-budget-pct 0.70 --exploratory-margin-budget-pct 0.04 `
  --flatten-on-daily-loss --results-dir "data\profile_best"

Write-Host "== aggressive (2.5x + deep flatten) =="
python simulator\regime_validation.py @common --baseline-contracts 2850 --daily-credit-cap-pct 0.125 `
  --portfolio-margin-budget-pct 1.00 --core-margin-budget-pct 0.875 --exploratory-margin-budget-pct 0.05 `
  --flatten-on-daily-loss --flatten-loss-limit-pct 0.035 --results-dir "data\profile_aggressive"

Write-Host "== summaries =="
foreach ($d in "profile_baseline","profile_flatten","profile_best","profile_aggressive") {
  python simulator\summarize_run.py "data\$d" --account-equity $Equity
}

Write-Host "== rebuild dashboard data =="
python dashboard\build_dashboard_data.py `
  --run "baseline=data/profile_baseline:Baseline (prior best, no governor)" `
  --run "flatten=data/profile_flatten:1x + flatten governor" `
  --run "best=data/profile_best:2x deploy + flatten" `
  --run "aggressive=data/profile_aggressive:2.5x + deep flatten (aggressive)" `
  --account-equity $Equity

Write-Host "Done. Serve the dashboard with: cd dashboard; python -m http.server 8000"
