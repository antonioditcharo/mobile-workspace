"""Paths and runtime settings."""

from __future__ import annotations

import os
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = Path(os.environ.get("LOCALGEN_DATA", ROOT / "data"))
OUTPUT_DIR = DATA_DIR / "outputs"
MODEL_CACHE_DIR = Path(
    os.environ.get("LOCALGEN_MODELS", DATA_DIR / "models" / "hub")
)
CHECKPOINT_DIR = Path(
    os.environ.get("LOCALGEN_CHECKPOINTS", DATA_DIR / "models" / "checkpoints")
)
WEB_DIR = ROOT / "web"

HOST = os.environ.get("LOCALGEN_HOST", "0.0.0.0")
PORT = int(os.environ.get("LOCALGEN_PORT", "7867"))

# Produces placeholder images without torch, so the phone <-> laptop
# connection and the whole UI can be verified before downloading any models.
STUB_MODE = os.environ.get("LOCALGEN_STUB", "").lower() in ("1", "true", "yes")

# Generations are queued; the GPU can only do one at a time anyway.
MAX_BATCH = 8
MAX_STEPS = 60
MAX_QUEUE = 32


def ensure_dirs() -> None:
    for directory in (OUTPUT_DIR, MODEL_CACHE_DIR, CHECKPOINT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def lan_ip() -> str:
    """Best-effort LAN address, for the 'open this on your phone' URL.

    No packets are actually sent -- connecting a UDP socket just asks the
    routing table which local interface would be used to reach the internet.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
