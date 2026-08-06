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

function Push-WithRebase {
  # Multi-machine dashboard deploys can still race; rebase+retry with autostash
  # so unrelated dirty files don't block the pull.
  param([int]$Attempts = 3)
  for ($i = 1; $i -le $Attempts; $i++) {
    & $Git push origin main
    if ($LASTEXITCODE -eq 0) {
      Write-Host "Pushed main (docs/)."
      return
    }
    if ($i -eq $Attempts) { break }
    Write-Warning "Push rejected (attempt $i/$Attempts); rebasing onto origin/main and retrying."
    & $Git pull --rebase --autostash origin main
    if ($LASTEXITCODE -ne 0) {
      & $Git rebase --abort 2>&1 | Out-Null
      throw "Could not rebase onto origin/main; dashboard data was committed locally but not pushed."
    }
  }
  throw "git push origin main failed after $Attempts attempt(s); dashboard data was committed locally but not pushed."
}

function Invoke-PagesBuildRequest {
  # Pages allows exactly one in-flight deployment. A concurrent push (or a
  # previous build still finishing) can return 400/409 "due to in progress
  # deployment". Back off and retry rather than failing the job.
  param(
    [string]$Repository,
    [int]$Attempts = 4,
    [int]$BackoffSeconds = 20
  )
  for ($i = 1; $i -le $Attempts; $i++) {
    Write-Host "Requesting GitHub Pages build for $Repository (attempt $i/$Attempts) ..."
    $resp = & $Gh api -X POST "repos/$Repository/pages/builds" 2>&1
    if ($LASTEXITCODE -eq 0) {
      if ($resp) { Write-Host $resp }
      return
    }
    $text = "$resp"
    if ($text -notmatch "in progress deployment|already in progress|HTTP 40") {
      throw "Pages build request failed: $resp"
    }
    if ($i -eq $Attempts) {
      Write-Warning "Pages build still busy after $Attempts attempts; the branch tip will deploy on the next build."
      return
    }
    Write-Host "  another deployment is in flight; retrying in ${BackoffSeconds}s"
    Start-Sleep -Seconds $BackoffSeconds
  }
}

function Wait-PagesBuild {
  param(
    [string]$Repository,
    [int]$TimeoutSeconds = 120,
    # When set, only a build whose .commit matches this SHA counts as done.
    # Without this, polling immediately after a push can see the *previous*
    # build still reporting "built" before GitHub has even enqueued a run for
    # what we just pushed, and return early having verified nothing.
    [string]$ExpectCommit = ""
  )
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  Write-Host "Waiting for Pages build (up to ${TimeoutSeconds}s) ..."
  while ((Get-Date) -lt $deadline) {
    $build = & $Gh api "repos/$Repository/pages/builds/latest" 2>&1
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "Could not poll Pages build status: $build"
      return
    }
    $parsed = $build | ConvertFrom-Json
    $status = $parsed.status
    $commit = $parsed.commit
    Write-Host "  Pages build status: $status commit=$commit (updated $($parsed.updated_at))"
    if ($ExpectCommit -and $commit -ne $ExpectCommit) {
      Write-Host "  (still the prior build; waiting for $ExpectCommit to start building)"
    } elseif ($status -eq "built") {
      $site = (& $Gh api "repos/$Repository/pages" --jq ".html_url")
      Write-Host "Pages deployment complete: $site"
      return
    } elseif ($status -eq "errored") {
      throw "GitHub Pages build failed for commit ${commit}. Check repo Settings -> Pages and recent builds."
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
    --run "p3_poststop_cooldown_120=data/dashboard_runs/p3_poststop_cooldown_120:Production - OPEX 2x + month-end 0.5x + FOMC 13:30 + IC10 d0.16 (VIX>=15)" `
    --run "p3_poststop_compounding_f1=data/dashboard_runs/p3_poststop_compounding_f1:Compounding f=1 - size tracks equity" `
    --primary-run-id "p3_poststop_cooldown_120" `
    --account-equity $Equity `
    --include-live `
    --out "$Root\docs\data\dashboard_data.json"
  if ($LASTEXITCODE -ne 0) { throw "build_dashboard_data.py failed (exit $LASTEXITCODE); refusing to deploy stale data." }
}

$justPushed = $false
if ($doDeploy -or $DeployOnly) {
  Push-Location $Root
  try {
    # Only stage dashboard artifacts - avoid sweeping unrelated docs/ files (PDFs, guides, etc.).
    foreach ($artifact in @(
      "docs/data/dashboard_data.json",
      "docs/data/build_stamp.txt",
      "docs/data/live_status_url.json",
      "docs/index.html",
      "docs/build_dashboard_data.py",
      "docs/data/investors.json",
      "scripts/sync_dashboard.ps1"
    )) {
      if (Test-Path (Join-Path $Root $artifact)) {
        & $Git add -- $artifact
      }
    }
    # Stage other tracked docs/data/ changes, but never live_status.json
    # (gitignored; cloud status publishes to a gist, not Pages).
    & $Git add -- "docs/data/"
    $staged = & $Git diff --cached --name-only
    if ($staged) {
      & $Git -c user.name="Drew Goldman" -c user.email="dag5wd@virginia.edu" commit -m "dashboard: refresh backtest data and deploy"
      if ($LASTEXITCODE -ne 0) { throw "git commit failed (exit $LASTEXITCODE)" }
      Push-WithRebase
      $justPushed = $true
    } else {
      Write-Host "No dashboard changes to commit; continuing with Pages build trigger."
    }
  } finally {
    Pop-Location
  }
}

if ($doTriggerPages) {
  $expectCommit = (& $Git -C $Root rev-parse HEAD).Trim()
  if ($justPushed) {
    # This is legacy "Deploy from a branch" Pages: the push above already
    # auto-triggers a pages-build-deployment run for this commit. Requesting a
    # second build here raced that automatic one and failed with "in progress
    # deployment" (2026-08-06 run 31108752748). Only ask for an explicit build
    # when nothing was just pushed, i.e. -TriggerPagesBuild alone or a -Deploy
    # run with no dashboard changes to commit.
    Write-Host "Skipping explicit Pages build request: the push above already triggers one."
  } else {
    Invoke-PagesBuildRequest -Repository $Repo
  }
  Wait-PagesBuild -Repository $Repo -TimeoutSeconds $PagesPollSeconds -ExpectCommit $expectCommit
}

if (-not $doDeploy -and -not $DeployOnly -and -not $TriggerPagesBuild) {
  Write-Host "Done. Preview: cd docs; python -m http.server 8000"
  Write-Host "Deploy: .\scripts\sync_dashboard.ps1 -Deploy"
}
