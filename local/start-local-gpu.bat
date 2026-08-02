@echo off
REM ===================================================================
REM  RealFrame - local GPU server (Windows)
REM
REM  Double-click this file. It sets up everything the first time and
REM  starts the server every time after that.
REM ===================================================================

setlocal
cd /d "%~dp0"

echo.
echo   RealFrame local GPU setup
echo   =========================
echo.

REM ---- 1. Python present? ----
where python >nul 2>nul
if errorlevel 1 (
  echo   [X] Python is not installed.
  echo.
  echo   Install it from https://www.python.org/downloads/
  echo   IMPORTANT: on the first installer screen, tick
  echo   "Add python.exe to PATH" before clicking Install.
  echo.
  echo   Then double-click this file again.
  echo.
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo   [OK] Python %PYVER%

REM ---- 2. Virtual environment ----
if not exist ".venv\Scripts\python.exe" (
  echo   [..] Creating a private Python environment ^(one time^)
  python -m venv .venv
  if errorlevel 1 (
    echo   [X] Could not create the environment.
    pause
    exit /b 1
  )
)
echo   [OK] Environment ready

set PY=.venv\Scripts\python.exe

REM ---- 3. Dependencies ----
%PY% -c "import torch" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [..] Installing PyTorch with CUDA support.
  echo        This downloads about 2.5 GB and takes 5-15 minutes.
  echo        This happens once. Leave the window open.
  echo.
  %PY% -m pip install --upgrade pip --quiet
  %PY% -m pip install torch --index-url https://download.pytorch.org/whl/cu121
  if errorlevel 1 (
    echo   [X] PyTorch install failed. Check your internet connection.
    pause
    exit /b 1
  )
)
echo   [OK] PyTorch installed

%PY% -c "import diffusers" >nul 2>nul
if errorlevel 1 (
  echo   [..] Installing the video model libraries
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo   [X] Install failed.
    pause
    exit /b 1
  )
)
echo   [OK] Libraries installed

REM ---- 4. GPU check ----
%PY% -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [!] PyTorch cannot see your graphics card.
  echo       Usually this means the NVIDIA driver needs updating:
  echo       https://www.nvidia.com/Download/index.aspx
  echo.
  echo       Starting anyway so you can see the full error.
  echo.
) else (
  for /f "delims=" %%g in ('%PY% -c "import torch;p=torch.cuda.get_device_properties(0);print(p.name+' - '+str(round(p.total_memory/1073741824,1))+' GB')"') do echo   [OK] GPU: %%g
)

REM ---- 5. Low-VRAM defaults ----
REM 6 GB cards need a smaller frame than the model's default.
if not defined LOCAL_WIDTH set LOCAL_WIDTH=832
if not defined LOCAL_HEIGHT set LOCAL_HEIGHT=480

echo.
echo   Starting the server. Leave this window open while you generate.
echo   Press Ctrl+C to stop.
echo.

%PY% server.py

pause
