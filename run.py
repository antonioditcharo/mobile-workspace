#!/usr/bin/env python3
"""Launcher: prints the URL to open on your phone, then serves the app.

Run this instead of uvicorn directly -- the QR code and LAN URL are the whole
point of a phone-first tool, and the hardware summary catches a broken CUDA
install before you sit there wondering why the first image takes ten minutes.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def print_qr(url: str) -> None:
    """ASCII QR so you can point the phone camera at the terminal."""
    try:
        import qrcode
    except ImportError:
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Filmroom local image generator")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--stub",
        action="store_true",
        help="Run without any AI model - produces placeholder images. Use this "
        "to verify your phone can reach the laptop before downloading models.",
    )
    parser.add_argument("--reload", action="store_true", help="Dev auto-reload")
    args = parser.parse_args()

    # These must land in the environment *before* server.config is imported --
    # it snapshots them at import time, and with --reload uvicorn re-imports
    # everything in a fresh subprocess where only the environment survives.
    if args.stub:
        os.environ["LOCALGEN_STUB"] = "1"
    if args.port is not None:
        os.environ["LOCALGEN_PORT"] = str(args.port)
    if args.host is not None:
        os.environ["LOCALGEN_HOST"] = args.host

    from server import config

    host, port = config.HOST, config.PORT
    config.ensure_dirs()

    from server.pipeline import probe_hardware

    hw = probe_hardware()
    url = f"http://{config.lan_ip()}:{port}"

    print()
    print("  Filmroom")
    print("  " + "-" * 52)
    if config.STUB_MODE:
        print("  Mode        : STUB - placeholder images, no AI model")
    print(f"  Device      : {hw['device']}  {hw['gpu_name'] or ''}")
    if hw.get("vram_gb"):
        print(f"  VRAM        : {hw['vram_gb']} GB")
    print(f"  Backend     : {hw['detail']}")
    if not hw.get("compel") and not config.STUB_MODE:
        print("  WARNING     : compel missing -> long prompts will be cut at 77")
        print("                tokens and the style presets will barely work.")
        print("                Fix with: pip install compel")
    print(f"  Images      : {config.OUTPUT_DIR}")
    print("  " + "-" * 52)
    print(f"  On this machine : http://127.0.0.1:{port}")
    print(f"  ON YOUR PHONE   : {url}")
    print("  " + "-" * 52)
    print("  Phone and laptop must be on the same WiFi.")
    print("  Then: Chrome menu -> Add to Home screen.")
    print()
    print_qr(url)

    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
