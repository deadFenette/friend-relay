@echo off
setlocal
REM ============================================================================
REM   Сборка ЛЁГКОГО legacy-клиента (tkinter) в ОДИН exe-файл.
REM   Результат: dist\FriendRelay-Legacy.exe  (~12-16 МБ)
REM
REM   Это тот самый "ультралайт клиент для старого компа": без PySide6,
REM   без sounddevice, без numpy — только tkinter + cryptography.
REM   Пишет сообщения, принимает/отдаёт файлы, показывает скорость скачивания.
REM
REM   Запуск: дважды кликни build_legacy_exe.bat (нужен Python 3.10+ с галкой
REM   tcl/tk — стандартный установщик с python.org подходит)
REM ============================================================================

cd /d "%~dp0.."

echo ==========================================
echo   Building FriendRelay-Legacy (tkinter)
echo ==========================================
echo.

REM -- Шаг 1: pyinstaller, если нет -------------------------------------------
py -3 -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
    echo Installing PyInstaller...
    py -3 -m pip install pyinstaller
    if errorlevel 1 goto :error
)

REM -- Шаг 2: чистим старый билд -----------------------------------------------
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "FriendRelay-Legacy.spec" del /f /q "FriendRelay-Legacy.spec"

REM -- Шаг 3: собираем ----------------------------------------------------------
REM   --onefile  -- один exe, кинул другу и всё
REM   --windowed -- без чёрной консоли
REM   cryptography нужен lib.crypto (AES-256-GCM); тянем целиком, как в основном билде
echo Running PyInstaller...
py -3 -m PyInstaller --noconfirm --onefile --windowed --name FriendRelay-Legacy ^
  --paths . ^
  --collect-submodules cryptography ^
  --hidden-import _cffi_backend ^
  legacy\tkinter\relay_app.py

if errorlevel 1 goto :error

REM -- Шаг 4: успех --------------------------------------------------------------
echo.
echo ==========================================
echo   BUILD SUCCESS
echo ==========================================
echo Готово: dist\FriendRelay-Legacy.exe
echo.
echo Как пользоваться:
echo   1. Скинь этот ОДИН файл другу (ZeroTier/Tailscale/через что угодно).
echo   2. Друг запускает его - ставить Python НЕ надо.
echo   3. Друг вводит своё имя, адрес твоего хоста (http://100.x.x.x:9000)
echo      и жмёт "Подключиться". Файлы - вкладка "Файлы", скорость скачивания
echo      видна строкой под шапкой.
echo.
pause
exit /b 0

:error
echo.
echo ==========================================
echo   BUILD FAILED
echo ==========================================
echo Смотри ошибки выше. Обычно помогает:
echo   py -3 -m pip install --upgrade pip pyinstaller
echo.
pause
exit /b 1
