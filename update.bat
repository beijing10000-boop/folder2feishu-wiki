@echo off
setlocal
cd /d "%~dp0"

git pull --ff-only
if errorlevel 1 (
  echo [ERROR] Update failed. No local files were overwritten.
  pause
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

echo Update complete.
pause

