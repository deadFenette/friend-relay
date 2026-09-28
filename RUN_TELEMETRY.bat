@echo off
setlocal
cd /d "%~dp0"
title Friend Relay - Telemetry Viewer (live)

if not exist "telemetry_viewer.py" (
    echo.
    echo ERROR: telemetry_viewer.py not found!
    echo Make sure you're running this from the project directory.
    echo.
    pause
    exit /b 1
)

echo Starting Telemetry Viewer...
echo.
echo   - Connects to http://localhost:8420 by default
echo   - Make sure Friend Relay host is already running
echo   - You can change host/key in the viewer window
echo.

py -3 telemetry_viewer.py
if %errorlevel% equ 0 goto end

where python >nul 2>nul
if %errorlevel% equ 0 (
    python telemetry_viewer.py
    goto end
)

echo.
echo ERROR: Python not found!
echo Install Python 3.10+ and PySide6 (pip install PySide6).
echo.
pause

:end
if errorlevel 1 (
    echo.
    echo Program closed with error!
    echo.
    pause
)
