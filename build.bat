@echo off
setlocal enabledelayedexpansion
title nrsc5-gui Build

echo ============================================================
echo  nrsc5-gui  ^|  Windows Build Script
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ and add to PATH.
    pause & exit /b 1
)
echo [OK] Python found

if not exist "libnrsc5.dll" (
    echo [ERROR] libnrsc5.dll not found here.
    echo         Build nrsc5 with MSYS2 and copy libnrsc5.dll next to build.bat.
    pause & exit /b 1
)
echo [OK] libnrsc5.dll found

if not exist "librtlsdr.dll" (
    echo [ERROR] librtlsdr.dll not found here.
    echo         Copy librtlsdr.dll from MSYS2 mingw64\bin next to build.bat.
    pause & exit /b 1
)
echo [OK] librtlsdr.dll found

if not exist "nrsc5.py" (
    echo [ERROR] nrsc5.py not found here.
    echo         Copy support\nrsc5.py from the nrsc5 repository here.
    pause & exit /b 1
)
echo [OK] nrsc5.py found

echo.
echo [*] Installing Python dependencies...
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt -q
python -m pip install pyinstaller -q
echo [OK] Dependencies installed

echo.
echo [*] Building exe...
pyinstaller nrsc5_gui.spec --noconfirm --clean
if errorlevel 1 (
    echo [ERROR] Build failed. See output above.
    pause & exit /b 1
)

echo.
echo ============================================================
echo  Build complete!  ^>  dist\nrsc5-gui.exe
echo.
echo  The exe is self-contained. Distribute just that one file.
echo ============================================================
echo.
pause
