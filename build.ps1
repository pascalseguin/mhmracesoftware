# build.ps1 - Build MHM Race Management into a distributable Windows .exe
#
# Usage (from the mhm\ project root):
#   powershell -ExecutionPolicy Bypass -File build.ps1
#   powershell -ExecutionPolicy Bypass -File build.ps1 -Clean
#
# Output:
#   dist\MHM-Race.exe     -- standalone bundled executable
#
# After building, run Inno Setup on installer\setup.iss to produce the
# full Windows installer: installer\output\MHM-Race-Setup.exe

param(
    [switch]$Clean   # Wipe dist\ and build\ before building
)

# Use Continue so PyInstaller's stderr progress output doesn't get treated as a
# fatal error by PowerShell 5.1. Exit codes are checked manually after each step.
$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  MHM Race Management -- Build Script" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""


# 1. Check Python
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: Python not found." -ForegroundColor Red
    Write-Host "Install Python 3.11+ from https://python.org (tick Add to PATH)." -ForegroundColor Yellow
    exit 1
}
$pyver = python --version 2>&1
Write-Host "Python: $pyver" -ForegroundColor Green


# 2. Install / upgrade pip dependencies
Write-Host ""
Write-Host "Installing dependencies..." -ForegroundColor Yellow

python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Failed to install requirements.txt" -ForegroundColor Red; exit 1 }

python -m pip install pyinstaller pillow pystray --quiet
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: Failed to install pyinstaller/pillow/pystray" -ForegroundColor Red; exit 1 }

Write-Host "Dependencies OK." -ForegroundColor Green


# 3. Optional clean
if ($Clean) {
    Write-Host ""
    Write-Host "Cleaning previous build..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force "dist"  -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force "build" -ErrorAction SilentlyContinue
    Write-Host "Clean done." -ForegroundColor Green
}


# 4. Run PyInstaller
Write-Host ""
Write-Host "Building exe (this takes 1-3 minutes)..." -ForegroundColor Yellow
Write-Host ""

python -m PyInstaller mhm.spec --noconfirm
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: PyInstaller failed. See output above." -ForegroundColor Red
    exit 1
}


# 5. Verify output
$exe = "dist\MHM-Race.exe"
if (-not (Test-Path $exe)) {
    Write-Host "ERROR: Expected output not found: $exe" -ForegroundColor Red
    exit 1
}

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Build complete!" -ForegroundColor Green
Write-Host "  Output : $exe ($size MB)" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next step: create the Windows installer" -ForegroundColor Cyan
Write-Host "  1. Install Inno Setup from https://jrsoftware.org/isdl.php" -ForegroundColor Cyan
Write-Host "  2. Open installer\setup.iss in Inno Setup and click Build" -ForegroundColor Cyan
Write-Host "     OR run: & 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe' installer\setup.iss" -ForegroundColor Cyan
Write-Host ""
