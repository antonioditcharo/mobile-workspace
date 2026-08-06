@echo off
REM Starts Filmroom and prints the address to open on your phone.

setlocal

if not exist ".venv" (
  echo  No virtual environment found. Run setup.bat first.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat

REM Pass --stub to test the phone connection without downloading any model:
REM   start.bat --stub
python run.py %*

pause
