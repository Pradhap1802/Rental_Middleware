# PowerShell script to install RentAsst Middleware Executable as a Windows Service

# Require Administrator Elevation
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    Write-Host "Elevating privileges to Administrator..." -ForegroundColor Yellow
    # -Wait: without it, this (non-elevated) process falls straight through to
    # Install.bat's "pause" — showing "press any key" before the elevated window
    # has actually installed anything or opened the browser.
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs -Wait
    exit
}

$ServiceName = "RentAsstMiddlewareService"
$DisplayName = "RentAsst Standalone Middleware Service"
# Two layouts call this script: the dev repo (scripts/ next to dist/RentalMiddleware/)
# and create_installer.ps1's client package (scripts/ next to RentalMiddleware/, no
# "dist" segment) — check both so the client package's Install.bat actually finds the exe.
$ExePathCandidates = @(
    "$PSScriptRoot\..\dist\RentalMiddleware\RentalMiddleware.exe",
    "$PSScriptRoot\..\RentalMiddleware\RentalMiddleware.exe"
)
$ExePath = $ExePathCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

# Check if service already exists
$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($Service) {
    Write-Host "Service $ServiceName already exists. Stopping and removing..." -ForegroundColor Yellow
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    sc.exe delete $ServiceName
}

if ($ExePath -and (Test-Path $ExePath)) {
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

# Start-Service returns as soon as SCM marks the service RUNNING, which happens
# before uvicorn's own async startup finishes binding the port (see SvcDoRun in
# service.py) — poll the liveness probe instead of opening the browser immediately
# against a port that isn't listening yet.
Write-Host "Waiting for the middleware to come online..." -ForegroundColor Yellow
$DashboardUrl = "http://127.0.0.1:8088/login"
$Ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8088/health/live" -UseBasicParsing -TimeoutSec 2
        if ($resp.StatusCode -eq 200) { $Ready = $true; break }
    } catch {}
    Start-Sleep -Seconds 1
}

if ($Ready) {
    Write-Host "Opening the middleware dashboard in your browser..." -ForegroundColor Green
    Start-Process $DashboardUrl
} else {
    Write-Host "The service didn't respond within 30 seconds. Open $DashboardUrl manually once it's ready." -ForegroundColor Yellow
}
