@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Not installed. Run install.bat first.
  pause
  exit /b 1
)

start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://localhost:8765'"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8765
