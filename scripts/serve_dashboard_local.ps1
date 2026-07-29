# Serve docs/ over HTTP so the dashboard can poll http://127.0.0.1:8765 without mixed-content issues.
# Usage: .\scripts\serve_dashboard_local.ps1
# Then open http://127.0.0.1:5500/

param([int]$Port = 5500)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Docs = Join-Path $Root "docs"
Set-Location $Docs
Write-Host "Serving dashboard at http://127.0.0.1:$Port/  (Ctrl+C to stop)"
Write-Host "Local console API should be on http://127.0.0.1:8765/status"
python -m http.server $Port --bind 127.0.0.1
