@echo off
rem (v3.5.1) Запуск СЕРВЕРА без GUI — командой/батником, окно консоли.
rem Файл сохранён в UTF-8: если русские буквы ниже выглядят кракозябрами,
rem НЕ пересохраняй его в ANSI/Windows-1251 — верни UTF-8 без BOM.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Friend Relay - Server (console)

rem ================== НАСТРОЙКИ (поменяй под себя) ==================
rem Имя хоста-админа — личность, которую защищает ключ админа:
set NAME=Василиса

rem Ключ админа: на другом устройстве введи в веб-клиенте ИМЯ ХОСТА +
rem ЭТОТ КЛЮЧ в поле «Ключ админа» — станешь админом и из браузера.
rem Ключ сохраняется в relay_data\admin_key.json и переживает рестарты.
rem Оставь пустым (set ADMIN_KEY=) — сервер сам сгенерирует ключ
rem и покажет его в шпаргалке при старте.
set ADMIN_KEY=ВАСИЛИСА_2014_ХХХ_КАРА_НЕБЕСНАЯ

rem Порт: 8420 = HTTP+HTTPS, 8421 = голос, 8422 = voxel:
set PORT=8420
rem ===================================================================

if not exist "server_main.py" (
    echo.
    echo ERROR: server_main.py not found! Put this .bat next to it.
    echo.
    pause
    exit /b 1
)

set ARGS=--name "%NAME%" --port %PORT%
if not "%ADMIN_KEY%"=="" set ARGS=%ARGS% --admin-key "%ADMIN_KEY%"

echo Starting Friend Relay server...
echo   %ARGS%
echo   Stop: Ctrl+C.  Журнал: relay_data\logs\server.log
echo.

py -3 -X utf8 server_main.py %ARGS%
if %errorlevel% equ 0 goto end

where python >nul 2>nul
if %errorlevel% equ 0 (
    python -X utf8 server_main.py %ARGS%
    goto end
)

echo.
echo ERROR: Python not found! Install Python 3.10+ and PySide6 deps.
echo.
pause
exit /b 1

:end
if errorlevel 1 (
    echo.
    echo Server exited with error! Причина: relay_data\logs\server.log
    echo и logs\crash_*.log / hang_*.log, если дело в краше/зависании.
    echo.
    pause
)
endlocal
