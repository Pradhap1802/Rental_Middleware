# PowerShell script to build Standalone Executable using PyInstaller

Write-Host "Starting RentAsst Middleware Standalone EXE Build..." -ForegroundColor Green

$RootPath = Resolve-Path "$PSScriptRoot\.."
Set-Location $RootPath

# Check PyInstaller availability
$PythonPath = "$RootPath\venv\Scripts\python.exe"
if (-not (Test-Path $PythonPath)) {
    $PythonPath = "python"
}

Write-Host "Ensuring build tools (pyinstaller, pywin32)..." -ForegroundColor Yellow
& $PythonPath -m pip install --quiet pyinstaller pywin32 tenacity

$DistDir = "$RootPath\dist\RentalMiddleware"
if (Test-Path $DistDir) {
    Write-Host "Cleaning existing dist directory: $DistDir..." -ForegroundColor Yellow
    Remove-Item -Path $DistDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "Running PyInstaller with RentalMiddleware.spec..." -ForegroundColor Green
& $PythonPath -m PyInstaller --clean -y RentalMiddleware.spec


$ExePath = "$RootPath\dist\RentalMiddleware\RentalMiddleware.exe"
$UiPath = "$RootPath\dist\RentalMiddleware\_internal\app\ui\index.html"

if (Test-Path $ExePath) {
    Write-Host "`nSUCCESS: Standalone executable created at:" -ForegroundColor Green
    Write-Host " -> $ExePath" -ForegroundColor Cyan
    
    if (Test-Path $UiPath) {
        Write-Host " -> UI Assets included: $UiPath" -ForegroundColor Green
    }
} else {
    Write-Host "`nERROR: Build failed! RentalMiddleware.exe was not found." -ForegroundColor Red
}
