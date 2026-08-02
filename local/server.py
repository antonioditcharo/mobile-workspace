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

# PyTorch lags new Python releases by many months. Catch that here rather than
# letting pip fail with "No matching distribution found", which reads like a
# network problem and sends people looking in the wrong place.
SUPPORTED_PYTHON = (3, 10, 3, 13)


def check_python_version():
    lo_major, lo_minor, hi_major, hi_minor = SUPPORTED_PYTHON
    major, minor = sys.version_info[:2]
    if (major, minor) < (lo_major, lo_minor) or (major, minor) > (hi_major, hi_minor):
        print()
        print(f"  Python {major}.{minor} is not supported by PyTorch.")
        print(f"  Supported: {lo_major}.{lo_minor} through {hi_major}.{hi_minor}.")
        print()
        print("  Install Python 3.12, then delete the .venv folder here and")
        print("  run start-local-gpu.bat again.")
        print("  https://www.python.org/downloads/release/python-3128/")
        print()
        return False
    return True

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
_pipeline_i2v = None
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


# Frame size drives memory use more sharply than anything else, so the default
# is chosen from the card rather than fixed. Dimensions stay multiples of 16 to
# satisfy the VAE's spatial compression and patch size.
VRAM_TIERS = [
    (5.0, (480, 320), 33),
    (7.0, (640, 384), 49),
    (11.0, (832, 480), 49),
    (float("inf"), (832, 480), 81),
]


def defaults_for_vram(vram):
    for ceiling, (width, height), frames in VRAM_TIERS:
        if vram < ceiling:
            return width, height, frames
    return 832, 480, 81


_auto_defaults = None


def auto_defaults():
    """Resolution and frame ceiling suited to the installed card."""
    global _auto_defaults
    if _auto_defaults is not None:
        return _auto_defaults
    try:
        import torch
        _auto_defaults = defaults_for_vram(total_vram_gb(torch))
    except Exception:
        _auto_defaults = (640, 384, 49)
    return _auto_defaults


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
    if vram and vram < 5:
        w, h, f = defaults_for_vram(vram)
        log(f"Low VRAM — defaulting to {w}x{h} at up to {f} frames.")
        log("Anything larger will run out of memory. Expect slow generation.")
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


def decode_image(value):
    """Accept a data URL or bare base64 and return a PIL image."""
    import base64
    from io import BytesIO

    from PIL import Image

    if not isinstance(value, str):
        return None
    payload = value.split(",", 1)[1] if value.startswith("data:") else value
    try:
        return Image.open(BytesIO(base64.b64decode(payload))).convert("RGB")
    except Exception as exc:
        raise ValueError(f"Could not read the start frame: {exc}")


def load_i2v_pipeline():
    """
    Image-to-video pipeline, used for start frames and for continuing a clip
    when RealFrame chains segments into something longer.
    """
    global _pipeline_i2v
    if _pipeline_i2v is not None:
        return _pipeline_i2v

    spec = resolve_model()
    if spec["kind"] != "ltx":
        raise RuntimeError(
            "This model is text-to-video only, so it cannot continue from a frame. "
            "Long clips and start frames need an image-to-video model — restart with "
            "LOCAL_MODEL=ltx."
        )

    import torch
    from diffusers import LTXImageToVideoPipeline

    log("Loading the image-to-video pipeline (shares weights with the loaded model)")
    base = load_pipeline()
    dtype = pick_dtype(torch)

    # Reuse the already-resident components rather than paying twice in VRAM.
    try:
        _pipeline_i2v = LTXImageToVideoPipeline(
            scheduler=base.scheduler,
            vae=base.vae,
            text_encoder=base.text_encoder,
            tokenizer=base.tokenizer,
            transformer=base.transformer,
        )
    except Exception:
        _pipeline_i2v = LTXImageToVideoPipeline.from_pretrained(
            spec["repo"], torch_dtype=dtype
        )
        _pipeline_i2v.enable_model_cpu_offload()

    return _pipeline_i2v


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

    auto_w, auto_h, auto_frames = auto_defaults()
    width = int(param("width", os.environ.get("LOCAL_WIDTH", auto_w)))
    height = int(param("height", os.environ.get("LOCAL_HEIGHT", auto_h)))
    num_frames = int(param("num_frames", auto_frames))

    # A frame count sized for a datacenter GPU will not fit a small card, and
    # failing 20 minutes in is a poor way to find out.
    cap = int(os.environ.get("LOCAL_MAX_FRAMES", auto_frames))
    if num_frames > cap:
        log(f"Capping {num_frames} frames to {cap} for the available VRAM "
            f"(raise with LOCAL_MAX_FRAMES).")
        num_frames = cap
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

    # A start frame means either an image-to-video request or a continuation
    # segment of a longer clip.
    raw_image = payload.get("image") or payload.get("image_data_url") or params.get("image")
    start_image = decode_image(raw_image) if raw_image else None

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

    if start_image is not None:
        pipe = load_i2v_pipeline()
        kwargs["image"] = start_image.resize((width, height))
        log(f"Continuing from a start frame: {num_frames}f {width}x{height} steps={steps}")
    else:
        log(f"Generating: {num_frames}f {width}x{height} steps={steps} cfg={guidance}")

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
    if not check_python_version():
        sys.exit(1)

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
