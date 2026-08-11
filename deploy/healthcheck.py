#!/usr/bin/env python3
"""Container health probe.

A separate file rather than an inline one-liner: quoting a nested f-string
through Dockerfile, shell and Python is how healthchecks end up silently
broken.

Exit 0 means the API is answering. Anything else marks the container unhealthy.
"""

from __future__ import annotations

import os
import sys

try:
    import httpx
except ImportError:  # pragma: no cover - the image always has it
    sys.exit(1)


def main() -> int:
    port = os.environ.get("POKEFLIP_PORT", "8787")
    try:
        response = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=8.0)
    except Exception:
        return 1
    if response.status_code != 200:
        return 1
    try:
        return 0 if response.json().get("status") == "ok" else 1
    except ValueError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
