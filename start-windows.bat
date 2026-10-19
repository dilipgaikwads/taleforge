@echo off
REM VideoFactory - start script for Windows (Task Scheduler runs this automatically).
cd /d "%~dp0"
if not exist "data\logs" mkdir "data\logs"
set PYTHONUNBUFFERED=1
echo === VideoFactory starting: %date% %time% === >> "data\logs\app.log"
"%~dp0venv\Scripts\waitress-serve.exe" --listen=0.0.0.0:8000 app.main:app >> "data\logs\app.log" 2>&1
