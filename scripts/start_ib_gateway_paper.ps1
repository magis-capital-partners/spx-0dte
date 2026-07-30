# Start IB Gateway for paper trading (login is still manual).
$GatewayExe = "C:\IBGateway\ibgateway.exe"
if (-not (Test-Path $GatewayExe)) {
    Write-Error "IB Gateway not found. Run: .\scripts\setup_ib_gateway.ps1 -Install"
}
Write-Host "Starting IB Gateway..."
Write-Host "  1. Configure -> API -> Socket port = 7497 (if not already set)"
Write-Host "  2. Log in with PAPER credentials"
Start-Process -FilePath $GatewayExe
