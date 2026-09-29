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

# Check if service already exists
$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($Service) {
    Write-Host "Service $ServiceName already exists. Stopping and removing..." -ForegroundColor Yellow
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName
}

if (Test-Path $ExePath) {
    # The compiled exe correctly responds to the Service Control Manager's dispatch
    # protocol (service.py's entry point hands off to servicemanager when launched
    # with no arguments, exactly how SCM launches a registered binary), so New-Service
    # can register it directly.
    Write-Host "Installing $DisplayName using compiled standalone executable: $ExePath" -ForegroundColor Green
    New-Service -Name $ServiceName -BinaryPathName "`"$ExePath`"" -DisplayName $DisplayName -StartupType Automatic -Description "High-performance integration gateway for RentAsst, Tally Prime, and external ERPs."
} else {
    # No compiled exe: fall back to running from source. A plain python.exe process is
    # NOT a valid SCM service binary on its own — New-Service would register it but SCM
    # could never correctly start/stop it. pywin32's own `install` command is what
    # correctly registers a Python-based service (via win32serviceutil), so shell out
    # to that instead of using New-Service here.
    $PythonPath = "$PSScriptRoot\..\venv\Scripts\python.exe"
    $ScriptPath = "$PSScriptRoot\..\service.py"
    if (-not (Test-Path $PythonPath)) {
        Write-Host "No compiled executable and no venv found at $PythonPath. Run 'python build.py' first, or create the venv and install requirements.txt." -ForegroundColor Red
        exit 1
    }
    Write-Host "Installing $DisplayName from source via pywin32 (Python script: $ScriptPath)" -ForegroundColor Yellow
    & $PythonPath $ScriptPath --startup auto install
}

sc.exe failure $ServiceName reset= 86400 actions= restart/10000/restart/10000/restart/10000

Write-Host "Service $ServiceName installed successfully." -ForegroundColor Green
Start-Service -Name $ServiceName
Write-Host "Service $ServiceName started in background." -ForegroundColor Green
