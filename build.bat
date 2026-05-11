@echo off
setlocal

echo ==============================================
echo  BlueOrbit Tracker - Build
echo ==============================================
echo.

REM ── Kill any running instance so the exe is not locked ───────────
echo [0/3] Stopping any running tracker...
taskkill /F /IM "echobl.exe" >nul 2>&1
ping 127.0.0.1 -n 2 >nul

REM ── Install / upgrade build tools ─────────────────────────────────
echo [1/3] Installing dependencies...
pip install -r requirements.txt --quiet
pip install pyinstaller --quiet
if %errorlevel% neq 0 (
    echo [ERROR] pip install failed. Make sure Python is on PATH.
    pause
    exit /b 1
)

REM ── Build folder bundle (onedir avoids AV false-positives) ─────────
echo [2/3] Building with PyInstaller...
pyinstaller ^
    --onefile ^
    --noconsole ^
    --name "echobl" ^
    --add-data "config.json;." ^
    --hidden-import winreg ^
    --hidden-import tkinter ^
    --hidden-import tkinter.ttk ^
    --hidden-import requests ^
    --hidden-import urllib3 ^
    --hidden-import certifi ^
    --hidden-import charset_normalizer ^
    --noupx ^
    -y ^
    tracker.py

if %errorlevel% neq 0 (
    echo [ERROR] PyInstaller build failed. See output above.
    pause
    exit /b 1
)

echo.
echo ==============================================
echo  Done!
echo  EXE: dist\echobl.exe
echo.
echo  Distribute only the single EXE file.
echo  config.json is bundled inside the exe.
echo  Users are prompted for their name on first run.
echo  Hotkey: Ctrl+Shift+9 to toggle timeline window.
echo ==============================================
echo.
pause
endlocal
