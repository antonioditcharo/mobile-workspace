"""
Local GPU backend for RealFrame.

Runs an open video model on your own graphics card and speaks the same HTTP
shape RealFrame's custom provider sends, so the app and the realism engine work
unchanged. No credits, no rate limits, no per-generation cost.

    python server.py

Then in RealFrame's .env:

    PROVIDER=custom
    CUSTOM_ENDPOINT=http://localhost:8000/generate

Environment variables:

    LOCAL_MODEL     wan-1.3b (default) | ltx | a diffusers repo id
    LOCAL_PORT      8000
    LOCAL_OFFLOAD   auto (default) | sequential | model | none
    LOCAL_WIDTH     832
    LOCAL_HEIGHT    480
    LOCAL_DTYPE     auto (default) | float16 | bfloat16
"""

import io
import json
import os
import sys
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("LOCAL_PORT", "8000"))

MODELS = {
    "wan-1.3b": {
        "repo": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
        "kind": "wan",
        "vram_gb": 6,
        "note": "Best small-model realism. ~5GB download.",
    },
    "ltx": {
        "repo": "Lightricks/LTX-Video",
        "kind": "ltx",
        "vram_gb": 6,
        "note": "Several times faster, softer detail. ~9GB download.",
    },
}

_pipeline = None
_pipeline_error = None


def log(msg):
    print(f"  {msg}", flush=True)


def resolve_model():
    name = os.environ.get("LOCAL_MODEL", "wan-1.3b").strip()
    if name in MODELS:
        return MODELS[name]
    # Anything else is treated as a raw diffusers repo id.
    kind = "ltx" if "ltx" in name.lower() else "wan"
    return {"repo": name, "kind": kind, "vram_gb": 0, "note": "custom repo"}


def pick_dtype(torch):
    """
    Turing cards (GTX 16xx, RTX 20xx) do not support bfloat16 in hardware, and
    most model cards assume you are on Ampere or newer. Pick per-device rather
    than trusting the default.
    """
    override = os.environ.get("LOCAL_DTYPE", "auto").lower()
    if override == "float16":
        return torch.float16
    if override == "bfloat16":
        return torch.bfloat16

    if not torch.cuda.is_available():
        return torch.float32
    try:
        if torch.cuda.is_bf16_supported():
            return torch.bfloat16
    except Exception:
        pass
    return torch.float16


def total_vram_gb(torch):
    if not torch.cuda.is_available():
        return 0
    try:
        return torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    except Exception:
        return 0


def load_pipeline():
    """Load the model once, on first request, so startup failures are visible."""
    global _pipeline, _pipeline_error
    if _pipeline is not None or _pipeline_error is not None:
        return _pipeline

    try:
        import torch
    except ImportError:
        _pipeline_error = (
            "PyTorch is not installed. Run start-local-gpu.bat, or "
            "pip install -r requirements.txt"
        )
        return None

    spec = resolve_model()
    dtype = pick_dtype(torch)
    vram = total_vram_gb(torch)

    if not torch.cuda.is_available():
        _pipeline_error = (
            "No CUDA GPU detected. Either the NVIDIA driver is missing, or PyTorch "
            "was installed without CUDA support. Reinstall with:\n"
            "  pip install torch --index-url https://download.pytorch.org/whl/cu121"
        )
        return None

    log(f"GPU: {torch.cuda.get_device_name(0)} ({vram:.1f} GB)")
    log(f"Model: {spec['repo']}")
    log(f"Precision: {str(dtype).replace('torch.', '')}")
    log("Loading — the first run downloads several GB and can take a while.")

    try:
        if spec["kind"] == "wan":
            from diffusers import AutoencoderKLWan, WanPipeline

            # Wan's VAE is unstable in fp16; keep it in fp32 while the
            # transformer runs at the lower precision.
            vae = AutoencoderKLWan.from_pretrained(
                spec["repo"], subfolder="vae", torch_dtype=torch.float32
            )
            pipe = WanPipeline.from_pretrained(spec["repo"], vae=vae, torch_dtype=dtype)
        else:
            from diffusers import LTXPipeline

            pipe = LTXPipeline.from_pretrained(spec["repo"], torch_dtype=dtype)
    except Exception as exc:
        _pipeline_error = f"Could not load {spec['repo']}: {exc}"
        traceback.print_exc()
        return None

    # Offloading trades speed for VRAM. Sequential is the aggressive setting
    # that makes 6 GB cards viable at all.
    offload = os.environ.get("LOCAL_OFFLOAD", "auto").lower()
    if offload == "auto":
        offload = "sequential" if vram and vram < 10 else "model"

    try:
        if offload == "sequential":
            pipe.enable_sequential_cpu_offload()
            log("Offload: sequential (low VRAM, slower)")
        elif offload == "model":
            pipe.enable_model_cpu_offload()
            log("Offload: model (balanced)")
        else:
            pipe.to("cuda")
            log("Offload: none (fastest, needs the most VRAM)")
    except Exception as exc:
        log(f"Offload setup failed ({exc}); falling back to model offload.")
        pipe.enable_model_cpu_offload()

    # Tiling keeps the VAE decode step from spiking memory at the end of a run,
    # which is where low-VRAM cards usually fail.
    for enable in ("enable_tiling", "enable_slicing"):
        try:
            getattr(pipe.vae, enable)()
        except Exception:
            pass

    _pipeline = pipe
    log("Ready.")
    return _pipeline


def export_video(frames, fps):
    """Write frames to a temporary mp4 and return the bytes."""
    from diffusers.utils import export_to_video

    path = os.path.join(tempfile.gettempdir(), f"realframe-{os.getpid()}.mp4")
    export_to_video(frames, path, fps=fps)
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        os.remove(path)
    except OSError:
        pass
    return data


def generate(payload):
    pipe = load_pipeline()
    if pipe is None:
        raise RuntimeError(_pipeline_error or "Pipeline unavailable.")

    import torch

    params = payload.get("parameters") or {}

    def param(name, default):
        for source in (payload, params):
            if isinstance(source, dict) and source.get(name) is not None:
                return source[name]
        return default

    prompt = payload.get("prompt") or payload.get("inputs") or ""
    if not prompt:
        raise ValueError("No prompt supplied.")

    negative = payload.get("negative_prompt") or params.get("negative_prompt") or None

    width = int(param("width", os.environ.get("LOCAL_WIDTH", 832)))
    height = int(param("height", os.environ.get("LOCAL_HEIGHT", 480)))
    num_frames = int(param("num_frames", 49))
    fps = int(param("fps", 16))
    steps = int(param("num_inference_steps", 30))
    guidance = float(param("guidance_scale", 5.0))
    seed = param("seed", None)

    # Most video VAEs need a frame count of 4n+1.
    if num_frames % 4 != 1:
        num_frames = (num_frames // 4) * 4 + 1

    generator = None
    if seed not in (None, ""):
        generator = torch.Generator(device="cpu").manual_seed(int(seed))

    log(f"Generating: {num_frames}f {width}x{height} steps={steps} cfg={guidance}")

    kwargs = dict(
        prompt=prompt,
        height=height,
        width=width,
        num_frames=num_frames,
        num_inference_steps=steps,
        guidance_scale=guidance,
    )
    if negative:
        kwargs["negative_prompt"] = negative
    if generator is not None:
        kwargs["generator"] = generator

    try:
        result = pipe(**kwargs)
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        raise RuntimeError(
            f"Out of VRAM at {width}x{height} with {num_frames} frames. "
            "Lower the frame count in RealFrame, or set LOCAL_WIDTH=640 and "
            "LOCAL_HEIGHT=384 before starting this server."
        )

    frames = result.frames[0]
    return export_video(frames, fps)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # the generate path logs what matters

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            spec = resolve_model()
            self._send_json(200, {
                "status": "error" if _pipeline_error else ("ready" if _pipeline else "idle"),
                "model": spec["repo"],
                "error": _pipeline_error,
            })
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.path.startswith("/generate"):
            self._send_json(404, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:
            self._send_json(400, {"error": f"Bad request body: {exc}"})
            return

        try:
            video = generate(payload)
        except Exception as exc:
            traceback.print_exc()
            self._send_json(500, {"error": str(exc)})
            return

        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(video)))
        self.end_headers()
        self.wfile.write(video)
        log(f"Sent {len(video) / 1024:.0f} KB")


def main():
    spec = resolve_model()
    print()
    print("  RealFrame local GPU server")
    print(f"  http://localhost:{PORT}/generate")
    print(f"  model: {spec['repo']}")
    print()
    print("  In RealFrame's .env set:")
    print("    PROVIDER=custom")
    print(f"    CUSTOM_ENDPOINT=http://localhost:{PORT}/generate")
    print()
    print("  The model loads on the first generation, not now.")
    print("  Press Ctrl+C to stop.")
    print()

    if os.environ.get("LOCAL_PRELOAD", "").lower() in ("1", "true", "yes"):
        load_pipeline()

    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
