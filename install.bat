@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python launcher not found. Install Python 3.11 or newer first.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating Python virtual environment...
  py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv
)

echo Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt

if not exist ".env" copy ".env.example" ".env" >nul

echo.
echo Installation complete. Double-click start.bat to run.
pause

