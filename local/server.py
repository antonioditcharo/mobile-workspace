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

    LOCAL_MODEL     auto from system RAM | animatediff | svd | wan-1.3b | ltx
    LOCAL_PORT      8000
    LOCAL_OFFLOAD   auto (default) | sequential | model | none
    LOCAL_WIDTH     auto from VRAM
    LOCAL_HEIGHT    auto from VRAM
    LOCAL_MAX_FRAMES auto from VRAM
    LOCAL_DTYPE     auto (default) | float16 | bfloat16 | float32
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

# `ram_gb` is the rough system-memory footprint while loading, which on Windows
# has to be backed by the page file. It is the number that decides whether a
# machine can run a model at all — more often the blocker than VRAM.
MODELS = {
    "animatediff": {
        "repo": "emilianJR/epiCRealism",
        "adapter": "guoyww/animatediff-motion-adapter-v1-5-2",
        "kind": "animatediff",
        "vram_gb": 4,
        "ram_gb": 6,
        "download_gb": 4,
        "note": "Lightest text-to-video. Runs where the others cannot.",
    },
    "svd": {
        "repo": "stabilityai/stable-video-diffusion-img2vid-xt",
        "kind": "svd",
        "vram_gb": 4,
        "ram_gb": 8,
        "download_gb": 5,
        "note": "Image-to-video only. No text encoder, so a small download.",
    },
    "wan-1.3b": {
        "repo": "Wan-AI/Wan2.1-T2V-1.3B-Diffusers",
        "kind": "wan",
        "vram_gb": 6,
        "ram_gb": 32,
        "download_gb": 28,
        "note": "Best small-model realism. Needs a large page file.",
    },
    "ltx": {
        "repo": "Lightricks/LTX-Video",
        "kind": "ltx",
        "vram_gb": 6,
        "ram_gb": 24,
        "download_gb": 19,
        "note": "Faster than Wan, softer detail. Also needs a large page file.",
    },
}

PAGEFILE_HELP = """
  Windows ran out of virtual memory while loading the model (os error 1455).
  This is the page file, not your graphics card - raising any timeout will
  not help.

  Two ways forward:

  1. Use a lighter model. Restart with:
         set LOCAL_MODEL=animatediff
         start-local-gpu.bat
     It needs about 6 GB instead of 32, and downloads 4 GB instead of 28.

  2. Or enlarge the Windows page file, then retry this model:
       - Press Windows key, type "Advanced system settings", open it
       - Performance -> Settings -> Advanced -> Virtual memory -> Change
       - Untick "Automatically manage paging file size"
       - Select your C: drive, choose "Custom size"
       - Initial size: 8192      Maximum size: 65536
       - Set -> OK -> restart the computer
"""

_pipeline = None
_pipeline_i2v = None
_pipeline_error = None
_offload_mode = "unknown"


def log(msg):
    print(f"  {msg}", flush=True)


def adopt_hf_token():
    """
    Reuse the token from RealFrame's .env if one is set.

    Anonymous Hub downloads are rate limited and noticeably slower, and the
    weights here run to tens of gigabytes. The token is only used to fetch
    files — generation stays entirely local.
    """
    if os.environ.get("HF_TOKEN"):
        return True

    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    try:
        with open(env_path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() in ("HF_TOKEN", "HUGGINGFACE_API_KEY"):
                    value = value.strip().strip('"').strip("'")
                    if value:
                        os.environ["HF_TOKEN"] = value
                        return True
    except OSError:
        pass
    return False


def total_ram_gb():
    """System memory, or 0 if it cannot be determined."""
    try:
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except Exception:
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
    except (ValueError, OSError, AttributeError):
        return 0


def default_model_name():
    """
    Pick a model the machine can actually load.

    Wan and LTX carry an 11 GB text encoder, and loading it on Windows needs
    that much virtual memory. On a machine without it the load dies with a
    paging-file error after a 28 GB download — an expensive way to find out.
    """
    ram = total_ram_gb()
    if ram and ram < 16:
        return "animatediff"
    return "wan-1.3b"


def resolve_model():
    name = os.environ.get("LOCAL_MODEL", "").strip() or default_model_name()
    if name in MODELS:
        return MODELS[name]
    # Anything else is treated as a raw diffusers repo id.
    kind = "ltx" if "ltx" in name.lower() else "wan"
    return {"repo": name, "kind": kind, "vram_gb": 0, "note": "custom repo"}


def pick_dtype(torch, kind=None):
    """
    Choose a precision the model architecture actually tolerates.

    Two separate constraints. Turing cards (GTX 16xx, RTX 20xx) have no
    hardware bfloat16 at all. And Stable Diffusion 1.5 lineage models —
    AnimateDiff and SVD here — are trained and validated in float16; running
    their UNet and VAE in bfloat16 loses enough mantissa to collapse the
    denoising, which decodes to flat grey mush rather than an image. The
    newer transformer video models (Wan, LTX) do prefer bfloat16.
    """
    override = os.environ.get("LOCAL_DTYPE", "auto").lower()
    if override == "float16":
        return torch.float16
    if override == "bfloat16":
        return torch.bfloat16
    if override == "float32":
        return torch.float32

    if not torch.cuda.is_available():
        return torch.float32

    if kind in ("animatediff", "svd"):
        return torch.float16

    try:
        if torch.cuda.is_bf16_supported():
            return torch.bfloat16
    except Exception:
        pass
    return torch.float16


# Approximate size of the components that must be resident during a denoising
# step — the transformer/UNet plus the VAE. The text encoder is excluded: it
# runs once at the start and can sit on the CPU without costing throughput.
WORKING_SET_GB = {
    "animatediff": 2.2,
    "svd": 3.0,
    "wan": 3.4,
    "ltx": 4.4,
}


def estimate_working_set(spec, dtype):
    base = WORKING_SET_GB.get(spec["kind"], 3.5)
    # float32 doubles the resident weights.
    return base * 2 if "float32" in str(dtype) else base


def free_vram_gb(torch):
    try:
        free, _total = torch.cuda.mem_get_info()
        return free / (1024 ** 3)
    except Exception:
        return 0


def release_vram():
    """Hand memory back between runs so the next one starts from a clean slate."""
    try:
        import gc
        import torch
        gc.collect()
        torch.cuda.empty_cache()
    except Exception:
        pass


def looks_degenerate(frames):
    """
    Detect output that decoded to flat mush instead of an image.

    A failed denoise produces near-uniform frames — the tell is very low
    variation across the whole frame. Real content, even a dim night shot,
    carries far more.
    """
    try:
        import numpy as np
    except ImportError:
        return False, 0.0
    try:
        sample = np.asarray(frames[len(frames) // 2].convert("RGB")).astype("float32")
    except Exception:
        return False, 0.0
    std = float(sample.std())
    return std < 18.0, std


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


def frame_budget(vram):
    """
    Total pixels-times-frames the card can hold in one pass.

    Memory during denoising scales with the product, not with either alone, so
    a fixed frame cap is the wrong shape: it blocks a longer clip at a smaller
    frame size that would fit comfortably. Derived from the tier defaults, which
    are the known-good combinations.
    """
    width, height, frames = defaults_for_vram(vram)
    return width * height * frames


def max_frames_for(width, height, vram=None):
    """How many frames fit at this frame size."""
    if vram is None:
        try:
            import torch
            vram = total_vram_gb(torch)
        except Exception:
            vram = 6.0
    per_frame = max(1, width * height)
    return max(9, int(frame_budget(vram) // per_frame))


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
    dtype = pick_dtype(torch, spec["kind"])
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
    ram = total_ram_gb()
    log(f"Model: {spec['repo']}")
    log(f"Precision: {str(dtype).replace('torch.', '')}")
    log(f"Authenticated Hub downloads: {'yes' if os.environ.get('HF_TOKEN') else 'no (slower, rate limited)'}")

    needed = spec.get("ram_gb", 0)
    if ram and needed and ram < needed:
        log(f"System RAM {ram:.0f} GB, this model wants about {needed} GB while")
        log("loading. Windows can cover the gap from the page file, but the")
        log("default page file is usually too small. If this fails with error")
        log("1455, use LOCAL_MODEL=animatediff or enlarge the page file.")

    size = spec.get("download_gb")
    if size:
        log(f"Loading. First run downloads about {size} GB. Progress appears below.")
        log("Nothing is stuck.")

    try:
        if spec["kind"] == "animatediff":
            from diffusers import AnimateDiffPipeline, DDIMScheduler, MotionAdapter

            adapter = MotionAdapter.from_pretrained(spec["adapter"], torch_dtype=dtype)
            pipe = AnimateDiffPipeline.from_pretrained(
                spec["repo"],
                motion_adapter=adapter,
                torch_dtype=dtype,
                low_cpu_mem_usage=True,
            )
            pipe.scheduler = DDIMScheduler.from_pretrained(
                spec["repo"],
                subfolder="scheduler",
                clip_sample=False,
                timestep_spacing="linspace",
                beta_schedule="linear",
                steps_offset=1,
            )
        elif spec["kind"] == "svd":
            from diffusers import StableVideoDiffusionPipeline

            pipe = StableVideoDiffusionPipeline.from_pretrained(
                spec["repo"], torch_dtype=dtype, variant="fp16", low_cpu_mem_usage=True
            )
        elif spec["kind"] == "wan":
            from diffusers import AutoencoderKLWan, WanPipeline

            # Wan's VAE is unstable in fp16; keep it in fp32 while the
            # transformer runs at the lower precision.
            vae = AutoencoderKLWan.from_pretrained(
                spec["repo"], subfolder="vae", torch_dtype=torch.float32
            )
            pipe = WanPipeline.from_pretrained(
                spec["repo"], vae=vae, torch_dtype=dtype, low_cpu_mem_usage=True
            )
        else:
            from diffusers import LTXPipeline

            pipe = LTXPipeline.from_pretrained(
                spec["repo"], torch_dtype=dtype, low_cpu_mem_usage=True
            )
    except Exception as exc:
        # Windows error 1455 is virtual memory exhaustion, which reads as an
        # inscrutable OSError but has a specific fix.
        if "1455" in str(exc) or "paging file" in str(exc).lower():
            _pipeline_error = (
                f"Not enough Windows virtual memory to load {spec['repo']} "
                f"(needs roughly {spec.get('ram_gb', 32)} GB). "
                "Use LOCAL_MODEL=animatediff, or enlarge the page file — the "
                "server window prints the steps."
            )
            print(PAGEFILE_HELP, flush=True)
        else:
            _pipeline_error = f"Could not load {spec['repo']}: {exc}"
            traceback.print_exc()
        return None

    # Offloading trades speed for VRAM, and the gap is large: sequential moves
    # weights across the PCIe bus for every single layer of every step, and
    # commonly runs 2-4x slower than model offload, which moves whole
    # components once per step. Pick the fastest mode the card can hold rather
    # than defaulting to the safest.
    offload = os.environ.get("LOCAL_OFFLOAD", "auto").lower()
    if offload == "auto":
        working = estimate_working_set(spec, dtype)
        if vram and vram >= working * 2.2:
            offload = "none"
        elif vram and vram >= working * 1.25:
            offload = "model"
        else:
            offload = "sequential"
        log(f"Offload chosen from {vram:.1f} GB VRAM vs ~{working:.1f} GB working set.")

    global _offload_mode
    try:
        if offload == "sequential":
            pipe.enable_sequential_cpu_offload()
            log("Offload: sequential (least VRAM, slowest)")
        elif offload == "model":
            pipe.enable_model_cpu_offload()
            log("Offload: model (balanced - usually 2-4x faster than sequential)")
        else:
            pipe.to("cuda")
            log("Offload: none (fastest, whole model resident)")
        _offload_mode = offload
    except Exception as exc:
        log(f"Offload setup failed ({exc}); falling back to model offload.")
        pipe.enable_model_cpu_offload()
        _offload_mode = "model"

    # Slicing keeps the VAE decode from spiking memory at the end of a run,
    # which is where low-VRAM cards usually fail. It is free of artifacts.
    try:
        pipe.vae.enable_slicing()
    except Exception:
        pass

    # Tiling decodes the frame in overlapping patches, which saves more memory
    # but leaves a visible seam where the tiles meet. At the frame sizes small
    # cards use it is unnecessary — the whole frame fits — so only enable it
    # when the output is actually large.
    width, height, _ = auto_defaults()
    if max(width, height) >= 704:
        try:
            pipe.vae.enable_tiling()
            log("VAE tiling on (large frame).")
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
    if spec["kind"] == "svd":
        return load_pipeline()  # already image-to-video
    if spec["kind"] != "ltx":
        raise RuntimeError(
            "This model cannot continue from a frame. Start frames and long "
            "chained clips need an image-to-video model — restart with "
            "LOCAL_MODEL=ltx, or LOCAL_MODEL=svd on a memory-constrained machine."
        )

    import torch
    from diffusers import LTXImageToVideoPipeline

    log("Loading the image-to-video pipeline (shares weights with the loaded model)")
    base = load_pipeline()
    dtype = pick_dtype(torch, spec["kind"])

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


def encode_long_prompt(pipe, prompt, negative_prompt):
    """
    Encode prompts longer than CLIP's 77-token window.

    Stable Diffusion 1.5 tokenizers cap at 77 tokens, and the realism engine
    emits far more than that — the whole subject-detail layer (skin pores,
    asymmetry, fabric drape) falls off the end and is silently discarded.
    Encoding in 75-token chunks and concatenating the embeddings keeps all of
    it, which is the difference between the engine working and not.

    Returns (prompt_embeds, negative_embeds), or None to fall back to the
    pipeline's own truncating path.
    """
    import torch

    tokenizer = getattr(pipe, "tokenizer", None)
    text_encoder = getattr(pipe, "text_encoder", None)
    if tokenizer is None or text_encoder is None:
        return None

    max_len = getattr(tokenizer, "model_max_length", 77)
    body = max_len - 2
    bos = tokenizer.bos_token_id
    eos = tokenizer.eos_token_id
    if bos is None or eos is None:
        return None

    device = getattr(pipe, "_execution_device", None) or torch.device("cpu")

    def encode(text, min_chunks):
        ids = tokenizer(text or "", truncation=False, add_special_tokens=False).input_ids
        chunks = [ids[i:i + body] for i in range(0, len(ids), body)] or [[]]
        while len(chunks) < min_chunks:
            chunks.append([])
        pieces = []
        for chunk in chunks:
            padded = [bos] + chunk + [eos]
            padded += [eos] * (max_len - len(padded))
            tensor = torch.tensor([padded[:max_len]], device=device)
            with torch.no_grad():
                pieces.append(text_encoder(tensor)[0])
        return torch.cat(pieces, dim=1), len(chunks)

    try:
        positive, n_pos = encode(prompt, 1)
        negative, n_neg = encode(negative_prompt, 1)

        # Classifier-free guidance requires both sides to be the same length.
        if n_pos != n_neg:
            target = max(n_pos, n_neg)
            positive, _ = encode(prompt, target)
            negative, _ = encode(negative_parts_safe(negative_prompt), target)

        return positive, negative
    except Exception as exc:
        log(f"Long-prompt encoding unavailable ({exc}); falling back to truncation.")
        return None


def negative_parts_safe(text):
    return text or ""


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
    # failing 20 minutes in is a poor way to find out. The ceiling depends on
    # the frame size, not just the card: memory scales with pixels x frames, so
    # a smaller frame buys proportionally more of them.
    override = os.environ.get("LOCAL_MAX_FRAMES")
    cap = int(override) if override else max_frames_for(width, height)
    if num_frames > cap:
        seconds = cap / max(1, fps)
        smaller = max_frames_for(320, 192)
        log(f"Capping {num_frames} frames to {cap} — that is {seconds:.1f}s at "
            f"{fps}fps, the most that fits at {width}x{height}.")
        log(f"  For longer: set LOCAL_WIDTH=320 and LOCAL_HEIGHT=192 for up to "
            f"{smaller} frames ({smaller / max(1, fps):.1f}s), or raise the")
        log("  ceiling directly with LOCAL_MAX_FRAMES and accept the risk of "
            "running out of memory.")
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

    spec = resolve_model()

    if spec["kind"] == "svd":
        # SVD has no text encoder at all — it animates a still and takes a
        # different argument set.
        if start_image is None:
            raise ValueError(
                "Stable Video Diffusion is image-to-video only. Upload a start "
                "frame under Model & parameters, or switch to "
                "LOCAL_MODEL=animatediff for text-to-video."
            )
        kwargs = dict(
            image=start_image.resize((width, height)),
            num_frames=min(num_frames, 25),
            num_inference_steps=steps,
            decode_chunk_size=2,
        )
        if generator is not None:
            kwargs["generator"] = generator
        log(f"Animating a still: {kwargs['num_frames']}f {width}x{height}")

    elif spec["kind"] == "animatediff":
        # The v1.5 motion adapter was trained on 16 frames at 512x512, and it
        # degrades sharply outside that. Past ~16 frames the temporal layers
        # wash the image out progressively — the clip starts with structure and
        # decays into flat colour by the end. Off-square, off-512 frame sizes
        # hurt for the same reason. Hold it to what it was trained for.
        requested = num_frames
        kwargs["num_frames"] = min(num_frames, 16)
        kwargs["height"] = 512 if height >= 384 else 384
        kwargs["width"] = 512 if width >= 384 else 384

        if requested > kwargs["num_frames"]:
            log(f"AnimateDiff trained on 16 frames; capping {requested} to 16 "
                "(more makes the picture dissolve, not longer).")
        if (width, height) != (kwargs["width"], kwargs["height"]):
            log(f"Using {kwargs['width']}x{kwargs['height']} — its native size.")

        # Stable Diffusion 1.5 expects roughly 7-8 guidance. The value arriving
        # here is the RealFrame default for whichever model is named in the UI,
        # which is usually a Wan-family number far too low for this one.
        if guidance < 6.0:
            log(f"Raising guidance {guidance} to 7.5 for Stable Diffusion 1.5.")
            kwargs["guidance_scale"] = 7.5

        # 16 frames at 16fps is a one-second clip. AnimateDiff's motion is
        # paced for 8fps, which also gives a watchable two seconds.
        if fps > 10:
            fps = 8
        if start_image is not None:
            log("AnimateDiff ignores start frames; generating from the prompt.")

        # CLIP truncates at 77 tokens, which would cut the realism modifiers off
        # the end of the prompt. Encode the whole thing instead.
        embeds = encode_long_prompt(pipe, prompt, negative)
        if embeds is not None:
            kwargs.pop("prompt", None)
            kwargs.pop("negative_prompt", None)
            kwargs["prompt_embeds"], kwargs["negative_prompt_embeds"] = embeds
            tokens = embeds[0].shape[1]
            log(f"Encoded the full prompt in {tokens // 77} chunks (no truncation).")

        log(f"Generating: {kwargs['num_frames']}f {kwargs['width']}x{kwargs['height']} steps={steps}")

    elif spec["kind"] == "wan":
        # Wan wants dimensions on a multiple of 16 and a 4n+1 frame count; it
        # errors rather than rounding. Guidance around 5 is its trained range,
        # which is also what RealFrame sends by default.
        kwargs["width"] = (width // 16) * 16
        kwargs["height"] = (height // 16) * 16
        if (kwargs["width"], kwargs["height"]) != (width, height):
            log(f"Rounded to {kwargs['width']}x{kwargs['height']} (Wan needs multiples of 16).")

        if start_image is not None:
            pipe = load_i2v_pipeline()
            kwargs["image"] = start_image.resize((kwargs["width"], kwargs["height"]))
        log(f"Generating: {num_frames}f {kwargs['width']}x{kwargs['height']} "
            f"steps={steps} cfg={guidance}")

    elif start_image is not None:
        pipe = load_i2v_pipeline()
        kwargs["image"] = start_image.resize((width, height))
        log(f"Continuing from a start frame: {num_frames}f {width}x{height} steps={steps}")
    else:
        log(f"Generating: {num_frames}f {width}x{height} steps={steps} cfg={guidance}")

    import time
    started = time.time()

    try:
        result = pipe(**kwargs)
    except torch.cuda.OutOfMemoryError:
        release_vram()

        # An aggressive offload mode is worth retrying more conservatively
        # rather than failing outright — the user has already waited.
        global _offload_mode
        if _offload_mode in ("none", "model"):
            fallback = "model" if _offload_mode == "none" else "sequential"
            log(f"Out of VRAM with '{_offload_mode}' offload. Retrying with "
                f"'{fallback}', which is slower but fits.")
            try:
                if fallback == "model":
                    pipe.enable_model_cpu_offload()
                else:
                    pipe.enable_sequential_cpu_offload()
                _offload_mode = fallback
                result = pipe(**kwargs)
            except torch.cuda.OutOfMemoryError:
                release_vram()
                raise RuntimeError(
                    f"Out of VRAM at {kwargs.get('width', width)}x"
                    f"{kwargs.get('height', height)} with "
                    f"{kwargs.get('num_frames', num_frames)} frames, even with "
                    "full offloading. Lower the frame count in RealFrame, or set "
                    "LOCAL_WIDTH=384 and LOCAL_HEIGHT=256 before starting this server."
                )
        else:
            raise RuntimeError(
                f"Out of VRAM at {kwargs.get('width', width)}x"
                f"{kwargs.get('height', height)} with "
                f"{kwargs.get('num_frames', num_frames)} frames. "
                "Lower the frame count in RealFrame, or set LOCAL_WIDTH=384 and "
                "LOCAL_HEIGHT=256 before starting this server."
            )

    elapsed = time.time() - started
    per_step = elapsed / max(1, kwargs.get("num_inference_steps", steps))
    log(f"Done in {elapsed / 60:.1f} min ({per_step:.1f}s per step, "
        f"offload={_offload_mode}).")

    frames = result.frames[0]

    # Shipping flat grey as if it were a video wastes the next five minutes of
    # the user's life on the wrong diagnosis. Say what happened.
    bad, std = looks_degenerate(frames)
    if bad:
        import torch
        current = str(pick_dtype(torch, spec["kind"])).replace("torch.", "")
        log("")
        log(f"WARNING: the output looks flat (variation {std:.1f}, expected 30+).")
        log("Denoising did not converge. This is almost always precision.")
        if current != "float16":
            log("  Try:  set LOCAL_DTYPE=float16   then restart this server.")
        elif spec["kind"] == "animatediff":
            log("  Already at float16. For this model the usual causes are")
            log("  asking for more than 16 frames, a frame size far from 512,")
            log("  or guidance below 6 — all three are now clamped, so if you")
            log("  still see this, try LOCAL_DTYPE=float32 (slower but exact).")
        else:
            log("  Already at float16. Try LOCAL_DTYPE=float32 (slower), or")
            log("  raise Steps in RealFrame, or lower Guidance to 3-4.")
        log("")

    video = export_video(frames, fps)
    release_vram()
    return video


MODEL_LABELS = {
    "animatediff": "AnimateDiff (SD 1.5)",
    "svd": "Stable Video Diffusion",
    "wan": "Wan 2.1 T2V 1.3B",
    "ltx": "LTX-Video",
}

# Parameters each architecture actually wants. RealFrame sends defaults for
# whichever model is named in its dropdown, which is unrelated to what is
# loaded here, so the app asks for these over /health and applies them.
SUGGESTED = {
    "animatediff": {"num_inference_steps": 32, "guidance_scale": 7.5, "fps": 8, "num_frames": 16},
    "svd": {"num_inference_steps": 25, "guidance_scale": 3.0, "fps": 7, "num_frames": 25},
    "wan": {"num_inference_steps": 30, "guidance_scale": 5.0, "fps": 16},
    "ltx": {"num_inference_steps": 30, "guidance_scale": 3.0, "fps": 24},
}


def model_label(spec):
    return MODEL_LABELS.get(spec["kind"], spec["repo"])


def suggested_params(spec):
    return dict(SUGGESTED.get(spec["kind"], {}))


def gpu_summary():
    try:
        import torch
        if not torch.cuda.is_available():
            return {"name": "none", "vram_gb": 0}
        props = torch.cuda.get_device_properties(0)
        return {
            "name": props.name,
            "vram_gb": round(props.total_memory / (1024 ** 3), 1),
            "free_gb": round(free_vram_gb(torch), 1),
        }
    except Exception:
        return {"name": "unknown", "vram_gb": 0}


def run_benchmark():
    """
    Time a deliberately tiny generation and report throughput.

    Useful because the levers that matter — offload mode, precision, frame
    size — are invisible from the outside, and a full clip takes minutes to
    tell you whether a change helped.
    """
    import time

    pipe = load_pipeline()
    if pipe is None:
        raise RuntimeError(_pipeline_error or "Pipeline unavailable.")

    spec = resolve_model()
    width, height, _frames = auto_defaults()
    frames = 16 if spec["kind"] == "animatediff" else 9
    if spec["kind"] == "animatediff":
        width = height = 512

    kwargs = dict(
        prompt="a street at night",
        num_frames=frames,
        num_inference_steps=6,
        height=height,
        width=width,
    )
    if spec["kind"] == "svd":
        raise RuntimeError("Benchmarking needs a text prompt; not supported for SVD.")

    release_vram()
    started = time.time()
    pipe(**kwargs)
    elapsed = time.time() - started
    release_vram()

    per_step = elapsed / 6
    return {
        "seconds_total": round(elapsed, 1),
        "seconds_per_step": round(per_step, 2),
        "offload": _offload_mode,
        "frame": f"{width}x{height}",
        "frames": frames,
        "estimate_32_steps_min": round(per_step * 32 / 60, 1),
        "gpu": gpu_summary(),
        "advice": benchmark_advice(per_step),
    }


def benchmark_advice(per_step):
    tips = []
    if _offload_mode == "sequential":
        tips.append(
            "Running with sequential offload, the slowest mode. If generation "
            "succeeds without out-of-memory errors, try LOCAL_OFFLOAD=model for "
            "roughly 2-4x throughput."
        )
    if per_step > 6:
        tips.append(
            "Over 6s per step. Lower LOCAL_WIDTH/LOCAL_HEIGHT, or reduce the "
            "frame count — both scale cost roughly linearly."
        )
    if per_step < 2:
        tips.append("Headroom available; try a larger frame or more steps.")
    tips.append("Steps trade time for detail almost linearly; 25-30 is usually enough.")
    return tips


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
            width, height, frames = auto_defaults()
            override = os.environ.get("LOCAL_MAX_FRAMES")
            ceiling = int(override) if override else max_frames_for(width, height)
            self._send_json(200, {
                "status": "error" if _pipeline_error else ("ready" if _pipeline else "idle"),
                "model": spec["repo"],
                "kind": spec["kind"],
                "label": model_label(spec),
                "offload": _offload_mode,
                "defaults": {
                    "width": width,
                    "height": height,
                    "num_frames": min(frames, ceiling),
                    **suggested_params(spec),
                },
                "max_frames": ceiling,
                "gpu": gpu_summary(),
                "error": _pipeline_error,
            })
            return

        if self.path.startswith("/benchmark"):
            try:
                self._send_json(200, run_benchmark())
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
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

        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(video)))
            self.end_headers()
            self.wfile.write(video)
            log(f"Sent {len(video) / 1024:.0f} KB")
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            # The video is finished; the client just stopped waiting. Say so
            # plainly rather than dumping a traceback that looks like a crash.
            log(f"Generated {len(video) / 1024:.0f} KB but RealFrame had already "
                "disconnected — its job timeout fired. Set JOB_TIMEOUT_MS=0 in "
                ".env to remove the limit.")


def main():
    if not check_python_version():
        sys.exit(1)

    adopt_hf_token()
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
