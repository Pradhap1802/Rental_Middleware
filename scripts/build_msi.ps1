# PowerShell script to prepare MSI distribution package
Write-Host "Preparing RentAsst Middleware MSI Build Workspace..." -ForegroundColor Green

$DistDir = "$PSScriptRoot\..\dist\RentAsstMiddleware"
if (Test-Path $DistDir) { Remove-Item -Path $DistDir -Recurse -Force }
New-Item -ItemType Directory -Path $DistDir | Out-Null

Copy-Item -Path "$PSScriptRoot\..\app" -Destination "$DistDir\app" -Recurse
Copy-Item -Path "$PSScriptRoot\..\run.py" -Destination "$DistDir\run.py"
Copy-Item -Path "$PSScriptRoot\..\service.py" -Destination "$DistDir\service.py"
Copy-Item -Path "$PSScriptRoot\..\requirements.txt" -Destination "$DistDir\requirements.txt"
Copy-Item -Path "$PSScriptRoot\..\README.md" -Destination "$DistDir\README.md"

Write-Host "MSI Package files staged successfully in $DistDir" -ForegroundColor Green
