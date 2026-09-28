@echo off
setlocal
REM ============================================================================
REM   Canonical Windows build for Friend Relay (PySide6).
REM   Result: dist\FriendRelay\FriendRelay.exe
REM
REM   Перед сборкой автоматически ставит все зависимости из requirements.txt
REM   (PySide6, cryptography, sounddevice, numpy) и pyinstaller.
REM
REM   Запуск: дважды кликни build_exe.bat ИЛИ запусти scripts\build_exe.bat
REM ============================================================================

cd /d "%~dp0.."

echo ==========================================
echo   Building Friend Relay (PySide6)
echo ==========================================
echo.

REM ── Шаг 1: ставим pyinstaller если нет ──────────────────────────────
py -3 -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    py -3 -m pip install pyinstaller
    if errorlevel 1 goto :error
)

REM ── Шаг 2: ставим все зависимости из requirements.txt ──────────────
echo Checking dependencies from requirements.txt...
py -3 -m pip install -r requirements.txt
if errorlevel 1 goto :error

REM ── Шаг 3: чистим старый билд ───────────────────────────────────────
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "FriendRelay.spec" del /f /q "FriendRelay.spec"

REM ── Шаг 4: собираем через PyInstaller ───────────────────────────────
REM
REM   --collect-all PySide6       — все Qt-плагины + переводы
REM   --collect-all sounddevice   — PortAudio-библиотеки (libportaudio.dll)
REM   --collect-submodules cryptography — собирает rust-биндинги
REM   --collect-submodules websockets   — WSS-мост голоса для браузера
REM   --hidden-import _cffi_backend  — нужен cryptography на некоторых сборках
REM
echo Running PyInstaller...
py -3 -m PyInstaller --noconfirm --windowed --name FriendRelay ^
  --collect-all PySide6 ^
  --collect-all sounddevice ^
  --collect-submodules cryptography ^
  --collect-submodules websockets ^
  --hidden-import _cffi_backend ^
  --add-data "qt_app;qt_app" ^
  --add-data "lib;lib" ^
  --add-data "voice;voice" ^
  --add-data "web_client;web_client" ^
  main_qt.py

if errorlevel 1 goto :error

REM ── Шаг 5: успех ────────────────────────────────────────────────────
echo.
echo ==========================================
echo   BUILD SUCCESS
echo ==========================================
echo Executable: dist\FriendRelay\FriendRelay.exe
echo.
echo Чтобы раздать друзьям:
echo   1. Зайди в dist\FriendRelay\
echo   2. Запакуй всю папку в ZIP (правый клик ^> Send to ^> Compressed folder)
echo   3. Кинь ZIP друзьям — они распакуют и запустят FriendRelay.exe
echo.
pause
exit /b 0

:error
echo.
echo ==========================================
echo   BUILD FAILED
echo ==========================================
echo Смотри ошибки выше. Если что-то не ставится — попробуй:
echo   py -3 -m pip install --upgrade pip
echo   py -3 -m pip install -r requirements.txt --force-reinstall
echo.
pause
exit /b 1
