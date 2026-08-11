# PowerShell script to install RentAsst Middleware Executable as a Windows Service

# Require Administrator Elevation
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    Write-Host "Elevating privileges to Administrator..." -ForegroundColor Yellow
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

$ServiceName = "RentAsstMiddlewareService"
$DisplayName = "RentAsst Standalone Middleware Service"
$ExePath = "$PSScriptRoot\..\dist\RentalMiddleware\RentalMiddleware.exe"

if (Test-Path $ExePath) {
    $BinaryPath = "`"$ExePath`""
    Write-Host "Installing $DisplayName using compiled standalone executable: $ExePath" -ForegroundColor Green
} else {
    $PythonPath = "$PSScriptRoot\..\venv\Scripts\python.exe"
    $ScriptPath = "$PSScriptRoot\..\service.py"
    $BinaryPath = "`"$PythonPath`" `"$ScriptPath`""
    Write-Host "Installing $DisplayName using Python script: $ScriptPath" -ForegroundColor Yellow
}


# Check if service already exists
$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($Service) {
    Write-Host "Service $ServiceName already exists. Stopping and updating..." -ForegroundColor Yellow
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName
}

# Create New Windows Service with Auto-Start and Recovery
New-Service -Name $ServiceName -BinaryPathName $BinaryPath -DisplayName $DisplayName -StartupType Automatic -Description "High-performance integration gateway for RentAsst, Tally Prime, and external ERPs."
sc.exe failure $ServiceName reset= 86400 actions= restart/10000/restart/10000/restart/10000

Write-Host "Service $ServiceName installed successfully." -ForegroundColor Green
Start-Service -Name $ServiceName
Write-Host "Service $ServiceName started in background." -ForegroundColor Green
