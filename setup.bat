@echo off
REM Filmroom setup for Windows + NVIDIA GPU.
REM Run this once. It creates a virtual environment and installs everything.

setlocal

REM cu121 wheels have the widest driver compatibility. If you have a recent
REM driver and want newer CUDA, change this to cu124.
set TORCH_INDEX=https://download.pytorch.org/whl/cu121

echo.
echo  Filmroom setup
echo  ----------------------------------------------------
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo  ERROR: Python not found on PATH.
  echo  Install Python 3.10 or 3.11 from python.org and TICK
  echo  "Add Python to PATH" during installation.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo  Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo  ERROR: could not create venv.
    pause
    exit /b 1
  )
)

call .venv\Scripts\activate.bat

echo  Upgrading pip...
python -m pip install --upgrade pip --quiet

echo.
echo  Installing PyTorch with CUDA support ^(this is the big one, ~2.5GB^)...
pip install torch torchvision --index-url %TORCH_INDEX%
if errorlevel 1 (
  echo.
  echo  ERROR: PyTorch install failed. Check your internet connection.
  pause
  exit /b 1
)

echo.
echo  Installing the rest...
pip install -r requirements.txt
if errorlevel 1 (
  echo  ERROR: dependency install failed.
  pause
  exit /b 1
)

echo.
echo  Verifying GPU...
python -c "import torch; print('  CUDA available:', torch.cuda.is_available()); print('  GPU:', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE - will run on CPU (very slow)')"

echo.
echo  ----------------------------------------------------
echo  Setup done. Now run:  start.bat
echo  ----------------------------------------------------
echo.
pause
