#!/bin/bash
# Double-click launcher for macOS.
#
# Finder will not run a .command file until it is marked executable:
#   chmod +x deploy/pokeflip.command
#
# To start it at login: System Settings -> General -> Login Items -> add this
# file.

set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer a virtualenv beside the project, then whatever is on PATH.
if [[ -x "$here/../.venv/bin/pokeflip" ]]; then
    exec "$here/../.venv/bin/pokeflip" app "$@"
fi

if ! command -v pokeflip >/dev/null 2>&1; then
    echo "pokeflip is not on your PATH."
    echo "Install it first:  pip install -e '$here/..'"
    read -r -p "Press return to close."
    exit 1
fi

exec pokeflip app "$@"
