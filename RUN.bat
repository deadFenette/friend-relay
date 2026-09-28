@echo off
setlocal
cd /d "%~dp0"
title Friend Relay - Chat and Files (PySide6)

if not exist "main_qt.py" (
    echo.
    echo ERROR: main_qt.py not found!
    echo Make sure you're running this from the project directory.
    echo.
    pause
    exit /b 1
)

if not exist "qt_app\" (
    echo.
    echo ERROR: qt_app folder not found!
    echo.
    pause
    exit /b 1
)

echo Starting Friend Relay (PySide6)...

py -3 main_qt.py
if %errorlevel% equ 0 goto end

where python >nul 2>nul
if %errorlevel% equ 0 (
    python main_qt.py
    goto end
)

echo.
echo ERROR: Python not found!
echo Install Python 3.10+ and PySide6.
echo.
pause

:end
if errorlevel 1 (
    echo.
    echo Program closed with error!
    echo.
    pause
)
