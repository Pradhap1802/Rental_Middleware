# PowerShell script for Graceful Service Restart
$ServiceName = "RentAsstMiddlewareService"

Write-Host "Gracefully restarting $ServiceName..." -ForegroundColor Yellow

$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($Service) {
    Stop-Service -Name $ServiceName -Force
    Start-Sleep -Seconds 2
    Start-Service -Name $ServiceName
    Write-Host "$ServiceName restarted successfully." -ForegroundColor Green
} else {
    Write-Host "Service $ServiceName is not installed. Restarting via python run.py..." -ForegroundColor Red
}
