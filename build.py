#!/usr/bin/env python3
"""
Windows Build Script for RentAsst Middleware
Builds standalone Windows executable package (RentalMiddleware.exe)
"""

import sys
import os
import subprocess
import zipfile

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = os.path.join(ROOT_DIR, "dist")

def main():
    print("==================================================")
    print(" Building RentAsst Middleware Standalone Windows EXE")
    print("==================================================")

    # 1. Check PyInstaller
    try:
        import PyInstaller
    except ImportError:
        print("[!] PyInstaller not found. Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # 2. Run PyInstaller
    spec_file = os.path.join(ROOT_DIR, "RentalMiddleware.spec")
    print(f"[*] Running PyInstaller with spec: {spec_file}")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", spec_file]
    result = subprocess.run(cmd, cwd=ROOT_DIR)

    if result.returncode != 0:
        print(f"[X] PyInstaller build failed with exit code {result.returncode}")
        sys.exit(1)

    print("[OK] PyInstaller build completed successfully.")

    # 3. Create ZIP distribution package
    target_dist = os.path.join(DIST_DIR, "RentalMiddleware")
    zip_path = os.path.join(DIST_DIR, "RentAsstMiddleware_Windows_v1.0.0.zip")
    
    if os.path.exists(target_dist):
        print(f"[*] Creating distribution ZIP package: {zip_path}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(target_dist):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, DIST_DIR)
                    zf.write(full_path, rel_path)

        zip_size_mb = round(os.path.getsize(zip_path) / (1024 * 1024), 2)
        print("==================================================")
        print(" SUCCESS: Windows Executable Package Created!")
        print(f" Output Zip: {zip_path} ({zip_size_mb} MB)")
        print("==================================================")
    else:
        print(f"[X] Target build directory not found: {target_dist}")

if __name__ == "__main__":
    main()
