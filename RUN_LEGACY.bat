@echo off
setlocal
cd /d "%~dp0"
title Friend Relay - Legacy (Tkinter ultralight)

if not exist "legacy\tkinter\app.py" (
    echo.
    echo ERROR: legacy\tkinter\app.py not found!
    echo Make sure you're running this from the project directory.
    echo.
    pause
    exit /b 1
)

echo Starting Friend Relay (Legacy Tkinter)...

py -3 legacy\tkinter\relay_app.py
if %errorlevel% equ 0 goto end

where python >nul 2>nul
if %errorlevel% equ 0 (
    python legacy\tkinter\relay_app.py
    goto end
)

echo.
echo ERROR: Python not found!
echo Install Python 3.10+ (tkinter is built in).
echo.
pause

:end
if errorlevel 1 (
    echo.
    echo Program closed with error!
    echo.
    pause
)
