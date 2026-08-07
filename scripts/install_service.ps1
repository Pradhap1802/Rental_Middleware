# PowerShell script to install RentAsst Middleware as a Windows Service
$ServiceName = "RentAsstMiddlewareService"
$DisplayName = "RentAsst Standalone Middleware Service"
$ScriptPath = "$PSScriptRoot\..\service.py"
$PythonPath = "$PSScriptRoot\..\venv\Scripts\python.exe"

Write-Host "Installing $DisplayName..." -ForegroundColor Green
$BinaryPath = "`"$PythonPath`" `"$ScriptPath`""

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
Write-Host "Service $ServiceName started." -ForegroundColor Green
