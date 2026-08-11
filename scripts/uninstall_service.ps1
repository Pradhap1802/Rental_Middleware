# PowerShell script to stop and uninstall RentAsst Middleware Windows Service

# Require Administrator Elevation
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    Write-Host "Elevating privileges to Administrator..." -ForegroundColor Yellow
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

$ServiceName = "RentAsstMiddlewareService"

$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($Service) {
    Write-Host "Stopping service $ServiceName..." -ForegroundColor Yellow
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    
    Write-Host "Deleting service $ServiceName..." -ForegroundColor Yellow
    sc.exe delete $ServiceName
    Write-Host "Service $ServiceName removed successfully." -ForegroundColor Green
} else {
    Write-Host "Service $ServiceName is not installed." -ForegroundColor Cyan
}
