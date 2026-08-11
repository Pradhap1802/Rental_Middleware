# PowerShell script to compile a single-file executable installer (RentAsstMiddleware_Setup.exe)

Write-Host "Building single-file installer (RentAsstMiddleware_Setup.exe)..." -ForegroundColor Green

$RootPath = Resolve-Path "$PSScriptRoot\.."
Set-Location $RootPath

$ZipPath = "$RootPath\dist\RentAsstMiddleware_v1.0.0_Client_Setup.zip"
if (-not (Test-Path $ZipPath)) {
    Write-Host "Zip package missing. Running create_installer.ps1..." -ForegroundColor Yellow
    & "$PSScriptRoot\create_installer.ps1"
}

$OutputExe = "$RootPath\dist\RentAsstMiddleware_Setup.exe"
if (Test-Path $OutputExe) { Remove-Item -Path $OutputExe -Force }

$CSharpCode = @"
using System;
using System.IO;
using System.IO.Compression;
using System.Diagnostics;
using System.Security.Principal;

namespace RentAsstSetup
{
    class Program
    {
        static void Main(string[] args)
        {
            try {
                bool isAdmin = new WindowsPrincipal(WindowsIdentity.GetCurrent()).IsInRole(WindowsBuiltInRole.Administrator);
                if (!isAdmin) {
                    ProcessStartInfo selfElevate = new ProcessStartInfo();
                    selfElevate.FileName = Process.GetCurrentProcess().MainModule.FileName;
                    selfElevate.Verb = "runas";
                    selfElevate.UseShellExecute = true;
                    Process.Start(selfElevate);
                    return;
                }

                Console.WriteLine("=================================================");
                Console.WriteLine("  RentAsst Middleware Windows Service Installer  ");
                Console.WriteLine("=================================================");
                Console.WriteLine();
                Console.WriteLine("Extracting setup files...");

                string targetDir = @"C:\Program Files\RentAsstMiddleware";
                if (!Directory.Exists(targetDir)) {
                    Directory.CreateDirectory(targetDir);
                }

                // Extract embedded ZIP payload
                byte[] zipBytes = Convert.FromBase64String(EmbeddedData.Payload);
                string tempZip = Path.Combine(Path.GetTempPath(), "RentAsstSetup_" + Guid.NewGuid().ToString("N") + ".zip");
                File.WriteAllBytes(tempZip, zipBytes);

                Console.WriteLine("Installing to " + targetDir + "...");
                ZipFile.ExtractToDirectory(tempZip, targetDir, true);

                if (File.Exists(tempZip)) { File.Delete(tempZip); }

                string installBat = Path.Combine(targetDir, "Install.bat");
                if (File.Exists(installBat)) {
                    ProcessStartInfo psi = new ProcessStartInfo();
                    psi.FileName = "cmd.exe";
                    psi.Arguments = "/c \"" + installBat + "\"";
                    psi.WorkingDirectory = targetDir;
                    psi.UseShellExecute = false;
                    Process p = Process.Start(psi);
                    p.WaitForExit();
                } else {
                    Console.WriteLine("Install.bat not found at " + installBat);
                }

                Console.WriteLine();
                Console.WriteLine("Setup Completed Successfully!");
                Console.WriteLine("Press any key to exit...");
                Console.ReadKey();
            }
            catch (Exception ex) {
                Console.WriteLine("Installer Error: " + ex.Message);
                Console.WriteLine("Press any key to exit...");
                Console.ReadKey();
            }
        }
    }

    public static class EmbeddedData {
        public static string Payload = "PAYLOAD_PLACEHOLDER";
    }
}
"@

Write-Host "Reading ZIP bytes and generating C# payload..." -ForegroundColor Yellow
$ZipBytes = [System.IO.File]::ReadAllBytes($ZipPath)
$Base64 = [System.Convert]::ToBase64String($ZipBytes)

$CodeWithPayload = $CSharpCode.Replace("PAYLOAD_PLACEHOLDER", $Base64)

$SrcFile = "$RootPath\dist\Installer.cs"
[System.IO.File]::WriteAllText($SrcFile, $CodeWithPayload)

Write-Host "Compiling RentAsstMiddleware_Setup.exe via .NET C# compiler..." -ForegroundColor Green
Add-Type -TypeDefinition $CodeWithPayload -ReferencedAssemblies "System.IO.Compression.FileSystem", "System.IO.Compression" -OutputAssembly $OutputExe -OutputType ConsoleApplication

if (Test-Path $SrcFile) { Remove-Item -Path $SrcFile -Force }

if (Test-Path $OutputExe) {
    $SizeMB = [math]::Round((Get-Item $OutputExe).Length / 1MB, 2)
    Write-Host "`nSUCCESS: Single-file Setup Executable Created!" -ForegroundColor Green
    Write-Host " -> Output Exe: $OutputExe ($SizeMB MB)" -ForegroundColor Cyan
} else {
    Write-Host "`nERROR: Failed to compile RentAsstMiddleware_Setup.exe" -ForegroundColor Red
}
