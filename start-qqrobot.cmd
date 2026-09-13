@echo off
setlocal
chcp 65001 >nul
title QQ Robot - Codex Remote Bridge
set "BRIDGE_DEBUG=1"

set "ROOT=%~dp0codex-remote-bridge"
if not exist "%ROOT%\client\qq_gateway_client.py" set "ROOT=%~dp0"

if not exist "%ROOT%\client\qq_gateway_client.py" (
  echo [ERROR] Cannot find client\qq_gateway_client.py
  echo Checked:
  echo   %~dp0codex-remote-bridge
  echo   %~dp0
  pause
  exit /b 1
)

cd /d "%ROOT%"

if not exist "client\.env" (
  echo [ERROR] Missing client\.env
  echo Run setup-venv.ps1 first, then configure QQ_APP_ID and QQ_APP_SECRET.
  pause
  exit /b 1
)

set "PYTHON_EXE=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" (
  echo [INFO] Project virtual environment is missing.
  echo Running setup-venv.ps1...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\setup-venv.ps1"
  if errorlevel 1 (
    echo [ERROR] Failed to prepare the project virtual environment.
    pause
    exit /b 1
  )
  if not exist "%PYTHON_EXE%" (
    echo [ERROR] Project virtual environment was not created: %PYTHON_EXE%
    pause
    exit /b 1
  )
)

for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'qq_gateway_client\.py' -and $_.ProcessId -ne $PID } | Select-Object -First 1 -ExpandProperty ProcessId"`) do set "EXISTING_PID=%%P"

if defined EXISTING_PID (
  echo [WARN] QQ robot bridge is already running. PID: %EXISTING_PID%
  echo Close the existing window or stop that process before starting another one.
  pause
  exit /b 0
)

echo Starting QQ robot bridge...
echo Project: %ROOT%
echo Debug: BRIDGE_DEBUG=%BRIDGE_DEBUG%
echo Python:
"%PYTHON_EXE%" --version
echo Codex configuration: read from client\.env by the bridge
echo.
if "%BRIDGE_DIAGNOSTICS_ONLY%"=="1" (
  echo Diagnostics complete. Bridge was not started.
  pause
  exit /b 0
)
echo Press Ctrl+C to stop.
echo.

"%PYTHON_EXE%" client\qq_gateway_client.py

echo.
echo Bridge exited.
pause
