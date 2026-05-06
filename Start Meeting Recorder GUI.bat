@echo off
setlocal

set "APP_DIR=%~dp0"
set "PYTHON=%APP_DIR%.venv\Scripts\pythonw.exe"
set "SCRIPT=%APP_DIR%meeting_recorder_gui.py"

if not exist "%PYTHON%" (
  echo Python virtual environment launcher not found:
  echo %PYTHON%
  echo.
  echo Create it first with:
  echo python -m venv .venv
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%PYTHON%' -ArgumentList '\"%SCRIPT%\"' -WorkingDirectory '%APP_DIR%' -Verb RunAs"
