#!/usr/bin/env bash
# Filmroom setup for Linux / macOS.
set -euo pipefail

cd "$(dirname "$0")"

# cu121 has the widest driver compatibility. Change to cu124 for newer drivers,
# or to "cpu" if you have no NVIDIA GPU (expect minutes per image).
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu121}"

echo
echo "  Filmroom setup"
echo "  ----------------------------------------------------"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "  ERROR: python3 not found."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "  Creating virtual environment..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip --quiet

if [ "$(uname)" = "Darwin" ]; then
  echo "  Installing PyTorch (Apple Silicon / MPS)..."
  pip install torch torchvision
else
  echo "  Installing PyTorch with CUDA support (~2.5GB)..."
  pip install torch torchvision --index-url "$TORCH_INDEX"
fi

echo
echo "  Installing the rest..."
pip install -r requirements.txt

echo
echo "  Verifying GPU..."
python - <<'PY'
import torch
if torch.cuda.is_available():
    print("  CUDA available:", torch.cuda.get_device_name(0))
elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
    print("  Apple Silicon GPU (MPS) available")
else:
    print("  NO GPU DETECTED - will run on CPU (very slow)")
PY

echo
echo "  ----------------------------------------------------"
echo "  Setup done. Now run:  ./start.sh"
echo "  ----------------------------------------------------"
echo
