# PowerShell script to package client distribution package into a ready-to-deploy ZIP

$RootPath = Resolve-Path "$PSScriptRoot\.."
Set-Location $RootPath

$DistSource = "$RootPath\dist\RentalMiddleware"
if (-not (Test-Path $DistSource)) {
    Write-Host "Executable build not found. Running build_exe.ps1 first..." -ForegroundColor Yellow
    & "$PSScriptRoot\build_exe.ps1"
}

$PackageDir = "$RootPath\dist\RentAsstMiddleware_Client_Package"
if (Test-Path $PackageDir) {
    Remove-Item -Path $PackageDir -Recurse -Force -ErrorAction SilentlyContinue
}

New-Item -ItemType Directory -Path $PackageDir | Out-Null
New-Item -ItemType Directory -Path "$PackageDir\scripts" | Out-Null

Write-Host "Copying compiled binary and dependencies..." -ForegroundColor Green
Copy-Item -Path "$DistSource" -Destination "$PackageDir\RentalMiddleware" -Recurse

Write-Host "Copying installation launchers and scripts..." -ForegroundColor Green
Copy-Item -Path "$RootPath\Install.bat" -Destination "$PackageDir\Install.bat"
Copy-Item -Path "$RootPath\Uninstall.bat" -Destination "$PackageDir\Uninstall.bat"
Copy-Item -Path "$PSScriptRoot\install_service.ps1" -Destination "$PackageDir\scripts\install_service.ps1"
Copy-Item -Path "$PSScriptRoot\uninstall_service.ps1" -Destination "$PackageDir\scripts\uninstall_service.ps1"
Copy-Item -Path "$RootPath\README.md" -Destination "$PackageDir\README.md"

$ZipPath = "$RootPath\dist\RentAsstMiddleware_v1.0.0_Client_Setup.zip"
if (Test-Path $ZipPath) { Remove-Item -Path $ZipPath -Force }

Write-Host "Compressing deployment package into $ZipPath..." -ForegroundColor Green
Compress-Archive -Path "$PackageDir\*" -DestinationPath $ZipPath -Force

Write-Host "`nSUCCESS: Client Deployment Package Created!" -ForegroundColor Green
Write-Host " -> Uncompressed Directory: $PackageDir" -ForegroundColor Cyan
Write-Host " -> Zip Archive: $ZipPath" -ForegroundColor Cyan
