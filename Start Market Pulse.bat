@echo off
setlocal
cd /d "%~dp0"
title Pre-Market Pulse

rem ---- locate Python -------------------------------------------------------
set "PY=python"
where python >nul 2>nul || set "PY=py -3"

rem ---- already running? just open the dashboard ---------------------------
powershell -NoProfile -Command "try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 'http://127.0.0.1:8080/api/health').StatusCode | Out-Null; exit 0 } catch { exit 1 }"
if not errorlevel 1 (
    if /i not "%~1"=="--no-open" start "" "http://127.0.0.1:8080"
    exit /b 0
)

rem ---- start the server (minimised) ---------------------------------------
if /i "%~1"=="--no-open" (
    start "Pre-Market Pulse" /min %PY% "market_mobile.py" --port 8080
) else (
    start "Pre-Market Pulse" /min %PY% "market_mobile.py" --port 8080 --open
)
exit /b 0
