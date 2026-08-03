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

REM ===================================================================
REM  1. Find a Python that PyTorch actually ships builds for.
REM
REM  PyTorch lags new Python releases by many months, so the newest
REM  version is usually the wrong one. The py launcher lets us pick a
REM  supported install without touching whatever else is on the system.
REM ===================================================================

set "PYCMD="
call :try_version 3.12
if not defined PYCMD call :try_version 3.11
if not defined PYCMD call :try_version 3.13
if not defined PYCMD call :try_version 3.10

if defined PYCMD goto :have_python

REM Nothing from the launcher - check whether plain "python" is usable.
REM These stay outside a parenthesized block on purpose: batch expands
REM variables when it parses a block, so a value set inside one is not
REM readable until the block ends.
where python >nul 2>nul
if errorlevel 1 goto :no_python

set "PYVER="
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
if not defined PYVER goto :bad_version

set "PYMAJOR="
set "PYMINOR="
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do set "PYMAJOR=%%a"& set "PYMINOR=%%b"

if not "%PYMAJOR%"=="3" goto :bad_version
if not defined PYMINOR goto :bad_version
if %PYMINOR% GEQ 14 goto :bad_version
if %PYMINOR% LSS 10 goto :bad_version

set "PYCMD=python"

:have_python
for /f "delims=" %%v in ('%PYCMD% --version 2^>^&1') do echo   [OK] Using %%v

REM ===================================================================
REM  2. Virtual environment
REM ===================================================================

REM The environment is kept outside the project folder by default. Downloading
REM a fresh copy of RealFrame replaces this directory, and a .venv inside it
REM would be thrown away with it - forcing a full reinstall every update.
set "SHARED_VENV=%LOCALAPPDATA%\realframe-venv"

REM An environment already inside the folder still wins, so existing setups
REM keep working untouched.
if exist ".venv\Scripts\python.exe" goto :venv_local
if exist "%SHARED_VENV%\Scripts\python.exe" goto :venv_shared

echo   [..] Creating a private Python environment ^(one time^)
echo        Location: %SHARED_VENV%
%PYCMD% -m venv "%SHARED_VENV%"
if errorlevel 1 goto :venv_fallback
goto :venv_shared

:venv_fallback
echo   [!] Could not create it there; using this folder instead.
%PYCMD% -m venv .venv
if errorlevel 1 (
  echo   [X] Could not create the environment.
  pause
  exit /b 1
)

:venv_local
set "PY=.venv\Scripts\python.exe"
echo   [OK] Environment ready ^(this folder^)
goto :venv_done

:venv_shared
set "PY=%SHARED_VENV%\Scripts\python.exe"
echo   [OK] Environment ready ^(%SHARED_VENV%^)

:venv_done

REM ===================================================================
REM  3. PyTorch
REM ===================================================================

%PY% -c "import torch" >nul 2>nul
if not errorlevel 1 goto :torch_ok

echo.
echo   [..] Installing PyTorch with CUDA support.
echo        About 2.5 GB. Expect 5-15 minutes. This happens once.
echo        Leave the window open.
echo.

%PY% -m pip install --upgrade pip --quiet

echo   [..] Trying CUDA 12.4 build
%PY% -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
%PY% -c "import torch" >nul 2>nul
if not errorlevel 1 goto :torch_ok

echo   [..] Trying CUDA 12.1 build
%PY% -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
%PY% -c "import torch" >nul 2>nul
if not errorlevel 1 goto :torch_ok

goto :torch_failed

:torch_ok
echo   [OK] PyTorch installed

REM ===================================================================
REM  4. Everything else
REM ===================================================================

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

REM ===================================================================
REM  5. GPU check
REM ===================================================================

%PY% -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [!] PyTorch cannot see your graphics card.
  echo       Usually the NVIDIA driver needs updating:
  echo       https://www.nvidia.com/Download/index.aspx
  echo.
  echo       Starting anyway so you can see the full error.
  echo.
) else (
  for /f "delims=" %%g in ('%PY% -c "import torch;p=torch.cuda.get_device_properties(0);print(p.name+chr(32)+chr(45)+chr(32)+str(round(p.total_memory/1073741824,1))+chr(32)+chr(71)+chr(66))"') do echo   [OK] GPU: %%g
)

echo.
echo   Starting the server. Leave this window open while you generate.
echo   Press Ctrl+C to stop.
echo.

%PY% server.py

pause
exit /b 0

REM ===================================================================
REM  Helpers and failure paths
REM ===================================================================

:try_version
py -%1 --version >nul 2>nul
if not errorlevel 1 set "PYCMD=py -%1"
exit /b 0

:no_python
echo   [X] Python is not installed.
echo.
echo   Install Python 3.12 from:
echo     https://www.python.org/downloads/release/python-3128/
echo.
echo   Scroll to "Windows installer (64-bit)".
echo   IMPORTANT: tick "Add python.exe to PATH" on the first screen.
echo.
pause
exit /b 1

:bad_version
echo   [X] Python %PYVER% is installed, but PyTorch has no builds for it.
echo.
echo   PyTorch supports Python 3.10 to 3.13. Version 3.14 is too new -
echo   this is why the download failed, not your internet connection.
echo.
echo   Install Python 3.12 alongside what you have:
echo     https://www.python.org/downloads/release/python-3128/
echo.
echo   Scroll to "Windows installer (64-bit)".
echo   Tick "Add python.exe to PATH" on the first screen.
echo.
echo   Your existing Python %PYVER% is left alone - this script will
echo   find and use 3.12 automatically next time.
echo.
pause
exit /b 1

:torch_failed
echo.
echo   [X] PyTorch would not install.
echo.
echo   If the errors above say "from versions: none", no build exists
echo   for this Python version - install Python 3.12 and run this again.
echo.
echo   Anything else usually means the download was interrupted. Try
echo   running this file again; pip resumes rather than restarting.
echo.
pause
exit /b 1
