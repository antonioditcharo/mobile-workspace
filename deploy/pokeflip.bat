@echo off
REM Double-click launcher for Windows.
REM
REM Put a shortcut to this on the desktop or in
REM   shell:startup
REM to have pokeflip come up with the machine.
REM
REM Uses the per-user config at %APPDATA%\pokeflip\config.json unless a
REM config.json sits next to this file.

setlocal

REM Prefer a virtualenv beside this script, then whatever is on PATH.
if exist "%~dp0..\.venv\Scripts\pokeflip.exe" (
    set "POKEFLIP=%~dp0..\.venv\Scripts\pokeflip.exe"
) else (
    set "POKEFLIP=pokeflip"
)

REM No console window hanging around behind the app window.
start "" /B "%POKEFLIP%" app %*

endlocal
