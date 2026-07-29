@echo off
REM ==========================================================================
REM  Restart the Swing Trade Tracker: stop the running (headless) instance,
REM  then start it fresh with serve.py. Double-click to run.
REM ==========================================================================
set "PROJECT=C:\Ravi\fable\swing\ravilabs-project"
set "PY=C:\Users\ravib\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"

echo Stopping any running instance on port 5000...
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
ping -n 3 127.0.0.1 >nul

echo Starting tracker...
cd /d "%PROJECT%"
start "" "%PY%" serve.py
ping -n 6 127.0.0.1 >nul
echo.
echo Done. Open http://127.0.0.1:5000
ping -n 4 127.0.0.1 >nul
