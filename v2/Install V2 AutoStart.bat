@echo off
setlocal
cd /d "%~dp0.."
title Install V2 AutoStart
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1"
echo.
pause
