@echo off
REM ==========================================================================
REM  Launch the Swing Trade Tracker headless (live monitor + localhost UI).
REM  Used by the "SwingTracker-AutoTrade" scheduled task, and handy to double-
REM  click for a manual start. Edit PY below if your Python lives elsewhere.
REM ==========================================================================
set "PROJECT=C:\Ravi\fable\swing\ravilabs-project"
set "PY=C:\Users\ravib\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"

cd /d "%PROJECT%"
start "" "%PY%" serve.py
