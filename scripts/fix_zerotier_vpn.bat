@echo off
:: ============================================================
::  fix_zerotier_vpn.bat - Friend Relay v3.5.10
::  ASCII-ONLY wrapper: launches the real fixer
::  scripts\fix_vpn_zt.py (diagnose + fix, UTF-8 output).
::
::  WHY NOT RUSSIAN TEXT HERE: cmd.exe reads .bat files in the
::  OEM codepage (CP866). UTF-8 Cyrillic in a .bat prints as
::  mojibake ("abracadabra"). The Python script prints proper
::  Russian via the Windows Unicode console API - no mojibake.
::
::  Usage:  right-click -> "Run as administrator"
::          (or the GUI button "Fix VPN+ZT" launches it)
::  All changes live until reboot (route add without -p).
:: ============================================================
setlocal
cd /d "%~dp0.."
title Friend Relay - ZeroTier/VPN fix

rem ---- locate a Python interpreter ------------------------------
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
  echo [ERROR] Python not found. Install Python 3.10+ first.
  echo Friend Relay needs Python anyway - see README.md
  pause
  exit /b 1
)

if not exist "scripts\fix_vpn_zt.py" (
  echo [ERROR] scripts\fix_vpn_zt.py not found - broken installation?
  pause
  exit /b 1
)

rem The Python script itself checks admin rights and prints
rem a clear message if they are missing.
%PY% "scripts\fix_vpn_zt.py" fix %*
set EC=%errorlevel%
if %EC% neq 0 echo.
if %EC% equ 3 echo [HINT] Right-click this .bat -^> "Run as administrator".
pause
endlocal
exit /b %EC%
