@echo off
rem start the deadlock broadcast hub and open the control panel.
rem the hub reads the game through mem-bridge, so build that once first
rem   cd mem-bridge ^&^& cargo build --release
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo   Python 3 was not found.
  echo   Install it from https://www.python.org/downloads/windows/
  echo   and tick "Add python.exe to PATH" during setup.
  echo.
  pause
  exit /b 1
)

start "" http://127.0.0.1:8787/
if not exist "%~dp0mem-bridge\target\release\dl-live-bridge.exe" (
  echo.
  echo   the bridge is not built yet. build it once with:
  echo     cd mem-bridge ^&^& cargo build --release
  echo.
  pause
  exit /b 1
)

%PY% "%~dp0hub.py" %*
