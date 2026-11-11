@echo off
setlocal
cd /d "%~dp0"
title Export Market Data CSV
python export_market_csv.py %*
if errorlevel 1 (
    echo.
    echo Export failed.
) else (
    echo.
    echo Done: data\exports\market_data.csv
    start "" "%~dp0data\exports"
)
echo.
pause
