#!/usr/bin/env bash
# Starts Filmroom and prints the address to open on your phone.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "  No virtual environment found. Run ./setup.sh first."
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Pass --stub to test the phone connection without downloading any model.
exec python run.py "$@"
