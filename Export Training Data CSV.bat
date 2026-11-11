@echo off
setlocal
cd /d "%~dp0"
title Export Training Data CSV
python export_training_csv.py %*
if errorlevel 1 (
    echo.
    echo Export failed.
) else (
    echo.
    echo Done: data\exports\training_data.csv
    start "" "%~dp0data\exports"
)
echo.
pause
