# Rebuild dashboard data and deploy to GitHub Pages (single repo: spx-0dte).
#
# Usage (from repo root):
#   .\scripts\sync_dashboard.ps1                    # rebuild docs/data/dashboard_data.json only
#   .\scripts\sync_dashboard.ps1 -Deploy            # rebuild + commit + push + trigger Pages build
#   .\scripts\sync_dashboard.ps1 -Push              # alias for -Deploy
#   .\scripts\sync_dashboard.ps1 -DeployOnly        # commit/push existing docs/ + trigger Pages build
#   .\scripts\sync_dashboard.ps1 -TriggerPagesBuild # request Pages rebuild via API (no git)
#   .\scripts\sync_dashboard.ps1 -SkipBuild -Deploy # push docs/ as-is + trigger Pages build

param(
  [switch]$Deploy,
  [switch]$Push,
  [switch]$DeployOnly,
  [switch]$SkipBuild,
  [switch]$TriggerPagesBuild,
  [string]$Repo = "magis-capital-partners/spx-0dte",
  [int]$PagesPollSeconds = 120,
  [double]$Equity = 13000000
)

$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $Here
$Python = "C:\Users\drewg\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
$Git = "C:\Program Files\Git\bin\git.exe"
if (-not (Test-Path $Git)) { $Git = "git" }
$Gh = "gh"
if (-not (Get-Command $Gh -ErrorAction SilentlyContinue)) { $Gh = "gh" }

$doDeploy = $Deploy -or $Push
$doTriggerPages = $TriggerPagesBuild -or $doDeploy -or $DeployOnly
$doBuild = -not $SkipBuild -and -not $DeployOnly

function Invoke-PagesBuildRequest {
  param([string]$Repository)
  Write-Host "Requesting GitHub Pages build for $Repository ..."
  $resp = & $Gh api -X POST "repos/$Repository/pages/builds" 2>&1
  if ($LASTEXITCODE -ne 0) {
    throw "Pages build request failed: $resp"
  }
  if ($resp) { Write-Host $resp }
}

function Wait-PagesBuild {
  param(
    [string]$Repository,
    [int]$TimeoutSeconds = 120
  )
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  Write-Host "Waiting for Pages build (up to ${TimeoutSeconds}s) ..."
  while ((Get-Date) -lt $deadline) {
    $build = & $Gh api "repos/$Repository/pages/builds" --jq ".[0]" 2>&1
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "Could not poll Pages build status: $build"
      return
    }
    $status = (& $Gh api "repos/$Repository/pages/builds" --jq ".[0].status")
    $updated = (& $Gh api "repos/$Repository/pages/builds" --jq ".[0].updated_at")
    Write-Host "  Pages build status: $status (updated $updated)"
    if ($status -eq "built") {
      $site = (& $Gh api "repos/$Repository/pages" --jq ".html_url")
      Write-Host "Pages deployment complete: $site"
      return
    }
    if ($status -eq "errored") {
      throw "GitHub Pages build failed. Check repo Settings -> Pages and recent builds."
    }
    Start-Sleep -Seconds 5
  }
  Write-Warning "Timed out waiting for Pages build; it may still be running on GitHub."
  $site = (& $Gh api "repos/$Repository/pages" --jq ".html_url")
  Write-Host "Site URL: $site"
}

if ($doBuild) {
  Write-Host "Building dashboard data (including live/paper sessions)..."
  & $Python "$Root\docs\build_dashboard_data.py" `
    --run "p3_poststop_cooldown_120=data/dashboard_runs/p3_poststop_cooldown_120:Production — put wing 150 + IC8 overlay (VIX≥15)" `
    --run "p3_trend_bc_085=data/dashboard_runs/p3_trend_bc_085:Trend BC 0.85 gate (Wave 2 risk-shape)" `
    --primary-run-id "p3_poststop_cooldown_120" `
    --account-equity $Equity `
    --include-live `
    --out "$Root\docs\data\dashboard_data.json"
}

if ($doDeploy -or $DeployOnly) {
  Push-Location $Root
  # Only stage dashboard artifacts - avoid sweeping unrelated docs/ files (PDFs, guides, etc.).
  foreach ($artifact in @(
    "docs/data/dashboard_data.json",
    "docs/data/build_stamp.txt",
    "docs/index.html",
    "docs/build_dashboard_data.py",
    "docs/data/investors.json"
  )) {
    if (Test-Path (Join-Path $Root $artifact)) {
      & $Git add -- $artifact
    }
  }
  # Also stage any other tracked files already under docs/data/ that changed.
  & $Git add -- "docs/data/"
  $staged = & $Git diff --cached --name-only
  if ($staged) {
    & $Git -c user.name="Drew Goldman" -c user.email="dag5wd@virginia.edu" commit -m "dashboard: refresh backtest data and deploy"
    & $Git push origin main
    Write-Host "Pushed main (docs/)."
  } else {
    Write-Host "No dashboard changes to commit; continuing with Pages build trigger."
  }
  Pop-Location
}

if ($doTriggerPages) {
  Invoke-PagesBuildRequest -Repository $Repo
  Wait-PagesBuild -Repository $Repo -TimeoutSeconds $PagesPollSeconds
}

if (-not $doDeploy -and -not $DeployOnly -and -not $TriggerPagesBuild) {
  Write-Host "Done. Preview: cd docs; python -m http.server 8000"
  Write-Host "Deploy: .\scripts\sync_dashboard.ps1 -Deploy"
}
