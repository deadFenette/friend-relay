@echo off
setlocal
cd /d "%~dp0"
title Friend Relay - Server GUI

if not exist "server_gui.py" (
    echo.
    echo ERROR: server_gui.py not found!
    echo Put server_gui.py next to main_qt.py in the project directory.
    echo.
    pause
    exit /b 1
)

rem 1) ЛОКАЛЬНОЕ ОКРУЖЕНИЕ ПРОЕКТА (.venv рядом со скриптом) — приоритет:
rem    именно в нём обычно установлен PySide6 (а в системном python — нет).
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" server_gui.py
    exit /b 0
)

rem 2) pythonw без консольного окна.
rem Если GUI не стартует — server_gui.py сам покажет окно-сообщение с
rem РЕАЛЬНОЙ причиной и запишет отчёт в server_gui_error.log.
where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw server_gui.py
    exit /b 0
)

rem 3) Фолбэк: py -3 / python с окном консоли (ошибки видны).
py -3 -X utf8 server_gui.py
if %errorlevel% equ 0 goto end

where python >nul 2>nul
if %errorlevel% equ 0 (
    python -X utf8 server_gui.py
    goto end
)

echo.
echo ERROR: Python not found!
echo Install Python 3.10+ and run:  pip install PySide6
echo.
pause
goto :eof

:end
if errorlevel 1 (
    echo.
    echo Server GUI exited with error!
    echo Причина и что делать: окно-сообщение и файл server_gui_error.log
    echo рядом с server_gui.py. Диагностика:  python server_gui.py --diag
    echo ^(сам сервер работает без GUI:  python server_main.py^)
    echo.
    pause
)
