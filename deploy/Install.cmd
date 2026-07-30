@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Folder2Feishu.ps1"
if errorlevel 1 (
  echo.
  echo Installation failed. Keep the error shown above.
  pause
)
