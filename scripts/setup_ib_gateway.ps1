# IB Gateway setup helper for SPX 0DTE paper trading.
# Installs (if needed), launches Gateway, and prints one-time API checklist.
#
# Usage (from repo root):
#   .\scripts\setup_ib_gateway.ps1              # check install + show checklist
#   .\scripts\setup_ib_gateway.ps1 -Install     # download + silent install to C:\IBGateway
#   .\scripts\setup_ib_gateway.ps1 -Launch      # start IB Gateway (paper login still manual)

param(
    [switch]$Install,
    [switch]$Launch
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$InstallerDir = Join-Path $Root "installers"
$Installer = Join-Path $InstallerDir "ibgateway-stable-standalone-windows-x64.exe"
$GatewayExe = "C:\IBGateway\ibgateway.exe"
$GatewayDir = "C:\IBGateway"
$Url = "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-windows-x64.exe"

function Write-Step([string]$n, [string]$msg) {
    Write-Host ""
    Write-Host "[$n] $msg" -ForegroundColor Cyan
}

if ($Install) {
    Write-Step "1" "Downloading IB Gateway installer..."
    New-Item -ItemType Directory -Force -Path $InstallerDir | Out-Null
    if (-not (Test-Path $Installer)) {
        Invoke-WebRequest -Uri $Url -OutFile $Installer -UseBasicParsing
    }
    Write-Host "  Installer: $Installer ($([math]::Round((Get-Item $Installer).Length/1MB)) MB)"

    if (Test-Path $GatewayExe) {
        Write-Host "  IB Gateway already installed at $GatewayExe"
    } else {
        Write-Step "2" "Installing IB Gateway to $GatewayDir ..."
        if (Test-Path "C:\Jts\tws.exe") {
            Write-Host "  Note: TWS is installed at C:\Jts - Gateway goes to separate folder C:\IBGateway"
        }
        Start-Process -FilePath $Installer -ArgumentList "-q", "-dir", $GatewayDir -Wait -NoNewWindow
        if (-not (Test-Path $GatewayExe)) {
            throw "Install failed - $GatewayExe not found"
        }
        Write-Host "  Installed: $GatewayExe" -ForegroundColor Green
    }
}

if ($Launch) {
    if (-not (Test-Path $GatewayExe)) {
        throw "IB Gateway not found at $GatewayExe. Run: .\scripts\setup_ib_gateway.ps1 -Install"
    }
    Write-Host "Launching IB Gateway..."
    Write-Host "  Log in with PAPER credentials (red/simulated banner)."
    Start-Process -FilePath $GatewayExe
}

Write-Step "CHECKLIST" "One-time configuration (you must do this in Gateway GUI)"
Write-Host @"
  BEFORE login -> Configure -> Settings -> API -> Settings:
    [x] Enable ActiveX and Socket Clients
    [ ] Read-Only API  (must be OFF)
    [ ] Socket port: 7497  (IMPORTANT - Gateway default is 4002; our bot uses 7497)

  Configure -> Settings -> API -> Precautions:
    [x] Allow connections from localhost only (if Python runs on same PC)
    Trusted IPs: 127.0.0.1

  Configure -> Settings -> Market Data:
    [x] Allow delayed market data (fallback)

  Client Portal (live login) - one time:
    [x] Paper Trading -> Share real-time market data with paper = YES
    [x] Market Data API Acknowledgement signed

  Login:
    - Use PAPER username (not live)
    - Close live TWS/Gateway before paper session (shared data rule)
"@

Write-Step "RUN" "Daily paper session"
Write-Host @"
  1. Start IB Gateway (paper login, port 7497)
  2. cd $Root
  3. python scripts\refresh_live_baselines.py
  4. python live\ib_executor.py

  Or: .\scripts\start_ib_gateway_paper.ps1  then run executor
"@

if (-not $Install -and -not $Launch) {
    if (Test-Path $GatewayExe) {
        Write-Host ""
        Write-Host "Status: IB Gateway installed at $GatewayExe" -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "Status: IB Gateway NOT installed. Run: .\scripts\setup_ib_gateway.ps1 -Install" -ForegroundColor Yellow
    }
    if (Test-Path "C:\Jts\tws.exe") {
        Write-Host "Status: TWS also installed at C:\Jts\tws.exe (use one at a time for shared data)" -ForegroundColor Yellow
    }
}
