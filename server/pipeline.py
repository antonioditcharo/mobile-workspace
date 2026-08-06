"""Diffusers pipeline management, tuned for small-VRAM laptop GPUs.

Torch and diffusers are imported lazily inside methods rather than at module
import. That keeps the web server startable (and the UI testable) on a machine
with no GPU stack installed, and it means an unconfigured install fails with a
clear message from the API instead of an ImportError traceback at boot.

Memory strategy for a 4GB card:
  - fp16 everywhere
  - attention slicing + VAE slicing/tiling (big win, negligible speed cost)
  - SD1.5 fits fully in VRAM; SDXL does not, so it gets sequential CPU offload
  - the pipeline is cached and only rebuilt when the model actually changes
"""

from __future__ import annotations

import gc
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("localgen.pipeline")


class GenerationCancelled(Exception):
    """Raised inside the denoise callback when a client cancels a job."""


class BackendUnavailable(RuntimeError):
    """Torch/diffusers missing, or no usable device."""


# --------------------------------------------------------------------------
# Model registry
# --------------------------------------------------------------------------
# Photorealistic SD1.5 checkpoints. These are the community checkpoints that
# actually deliver the found-photo look -- the base Stable Diffusion weights
# are noticeably more "rendered" and are deliberately not offered as a default.
#
# `repo` is a HuggingFace diffusers repo id. To use a Civitai .safetensors
# download instead, drop the file into models/checkpoints/ and it appears
# automatically as a local model (see discover_local_models).

@dataclass
class ModelSpec:
    id: str
    label: str
    repo: str
    family: str  # "sd15" | "sdxl"
    blurb: str
    vae: str | None = None
    recommended: bool = False
    heavy: bool = False  # needs CPU offload on 4GB


BUILTIN_MODELS: list[ModelSpec] = [
    ModelSpec(
        id="realistic-vision-6",
        label="Realistic Vision 6.0",
        repo="SG161222/Realistic_Vision_V6.0_B1_noVAE",
        family="sd15",
        blurb="Best all-round photorealism. Start here.",
        vae="stabilityai/sd-vae-ft-mse",
        recommended=True,
    ),
    ModelSpec(
        id="epicrealism",
        label="epiCRealism",
        repo="emilianJR/epiCRealism",
        family="sd15",
        blurb="Softer, very natural skin and lighting.",
    ),
    ModelSpec(
        id="realistic-vision-51",
        label="Realistic Vision 5.1",
        repo="SG161222/Realistic_Vision_V5.1_noVAE",
        family="sd15",
        blurb="Older sibling of 6.0. Grittier output.",
        vae="stabilityai/sd-vae-ft-mse",
    ),
    ModelSpec(
        id="realvis-xl",
        label="RealVis XL 4.0 (SDXL)",
        repo="SG161222/RealVisXL_V4.0",
        family="sdxl",
        blurb="Better hands and anatomy, ~4x slower on 4GB.",
        heavy=True,
    ),
]

MODELS_BY_ID: dict[str, ModelSpec] = {m.id: m for m in BUILTIN_MODELS}
DEFAULT_MODEL_ID = "realistic-vision-6"


def discover_local_models(checkpoint_dir: Path) -> list[ModelSpec]:
    """Expose any .safetensors dropped into models/checkpoints/ as a model."""
    if not checkpoint_dir.is_dir():
        return []
    found = []
    for path in sorted(checkpoint_dir.glob("*.safetensors")):
        stem = path.stem
        family = "sdxl" if "xl" in stem.lower() else "sd15"
        found.append(
            ModelSpec(
                id=f"local:{stem}",
                label=stem,
                repo=str(path),
                family=family,
                blurb="Local checkpoint file",
                heavy=(family == "sdxl"),
            )
        )
    return found


# --------------------------------------------------------------------------
# Samplers
# --------------------------------------------------------------------------

SAMPLERS = {
    "dpmpp_2m_karras": "DPM++ 2M Karras",
    "dpmpp_sde_karras": "DPM++ SDE Karras",
    "euler_a": "Euler Ancestral",
    "unipc": "UniPC",
    "ddim": "DDIM",
}


def _build_scheduler(sampler: str, current_config):
    from diffusers import (
        DDIMScheduler,
        DPMSolverMultistepScheduler,
        DPMSolverSinglestepScheduler,
        EulerAncestralDiscreteScheduler,
        UniPCMultistepScheduler,
    )

    if sampler == "dpmpp_2m_karras":
        return DPMSolverMultistepScheduler.from_config(
            current_config, use_karras_sigmas=True, algorithm_type="dpmsolver++"
        )
    if sampler == "dpmpp_sde_karras":
        return DPMSolverSinglestepScheduler.from_config(
            current_config, use_karras_sigmas=True
        )
    if sampler == "euler_a":
        return EulerAncestralDiscreteScheduler.from_config(current_config)
    if sampler == "unipc":
        return UniPCMultistepScheduler.from_config(current_config)
    if sampler == "ddim":
        return DDIMScheduler.from_config(current_config)
    return DPMSolverMultistepScheduler.from_config(
        current_config, use_karras_sigmas=True, algorithm_type="dpmsolver++"
    )


# --------------------------------------------------------------------------
# Hardware probe
# --------------------------------------------------------------------------

def probe_hardware() -> dict[str, Any]:
    """Describe the machine. Never raises -- the UI shows whatever it finds."""
    info: dict[str, Any] = {
        "torch": False,
        "diffusers": False,
        "device": "none",
        "gpu_name": None,
        "vram_gb": None,
        "compel": False,
        "ready": False,
        "detail": "",
    }

    try:
        import torch
    except ImportError:
        info["detail"] = "PyTorch is not installed. Run the setup script."
        return info

    info["torch"] = getattr(torch, "__version__", True)

    try:
        import diffusers  # noqa: F401

        info["diffusers"] = getattr(diffusers, "__version__", True)
    except ImportError:
        info["detail"] = "diffusers is not installed. Run the setup script."
        return info

    try:
        import compel  # noqa: F401

        info["compel"] = True
    except ImportError:
        info["compel"] = False

    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        info["device"] = "cuda"
        info["gpu_name"] = props.name
        info["vram_gb"] = round(props.total_memory / (1024**3), 1)
        info["ready"] = True
        info["detail"] = f"CUDA ready on {props.name}"
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        info["device"] = "mps"
        info["gpu_name"] = "Apple Silicon (MPS)"
        info["ready"] = True
        info["detail"] = "Apple Silicon GPU ready"
    else:
        info["device"] = "cpu"
        info["ready"] = True
        info["detail"] = "No GPU found - CPU mode will be very slow (minutes per image)"

    return info


# --------------------------------------------------------------------------
# Pipeline manager
# --------------------------------------------------------------------------

class PipelineManager:
    """Owns the loaded pipeline. One model resident at a time."""

    def __init__(self, cache_dir: Path, checkpoint_dir: Path):
        self.cache_dir = cache_dir
        self.checkpoint_dir = checkpoint_dir
        self._lock = threading.Lock()
        self._pipe = None
        self._img2img = None
        self._compel = None
        self._loaded_id: str | None = None
        self._loaded_family: str | None = None
        self._load_status = "idle"
        self._load_detail = ""

    # -- introspection ----------------------------------------------------

    @property
    def loaded_model_id(self) -> str | None:
        return self._loaded_id

    def status(self) -> dict[str, Any]:
        return {
            "loaded": self._loaded_id,
            "state": self._load_status,
            "detail": self._load_detail,
        }

    def available_models(self) -> list[dict[str, Any]]:
        specs = list(BUILTIN_MODELS) + discover_local_models(self.checkpoint_dir)
        return [
            {
                "id": s.id,
                "label": s.label,
                "family": s.family,
                "blurb": s.blurb,
                "recommended": s.recommended,
                "heavy": s.heavy,
                "cached": self._is_cached(s),
                "loaded": s.id == self._loaded_id,
            }
            for s in specs
        ]

    def _resolve_spec(self, model_id: str) -> ModelSpec:
        if model_id in MODELS_BY_ID:
            return MODELS_BY_ID[model_id]
        for spec in discover_local_models(self.checkpoint_dir):
            if spec.id == model_id:
                return spec
        raise ValueError(f"Unknown model: {model_id}")

    def _is_cached(self, spec: ModelSpec) -> bool:
        """Best-effort check for whether a model is already on disk."""
        if spec.repo.endswith(".safetensors"):
            return Path(spec.repo).exists()
        folder = "models--" + spec.repo.replace("/", "--")
        return (self.cache_dir / folder).is_dir()

    # -- loading ----------------------------------------------------------

    def load(self, model_id: str, progress: Callable[[str], None] | None = None):
        """Load a model, reusing the resident pipeline when it already matches."""
        with self._lock:
            if self._loaded_id == model_id and self._pipe is not None:
                return self._pipe

            spec = self._resolve_spec(model_id)
            self._load_status = "loading"
            self._load_detail = f"Loading {spec.label}"
            if progress:
                progress(f"Loading {spec.label}...")

            try:
                self._unload_locked()
                self._pipe = self._build_pipeline(spec, progress)
                self._loaded_id = spec.id
                self._loaded_family = spec.family
                self._load_status = "ready"
                self._load_detail = f"{spec.label} ready"
            except Exception as exc:
                self._load_status = "error"
                self._load_detail = str(exc)
                self._pipe = None
                self._loaded_id = None
                raise

            return self._pipe

    def _build_pipeline(self, spec: ModelSpec, progress):
        try:
            import torch
            from diffusers import (
                AutoencoderKL,
                StableDiffusionPipeline,
                StableDiffusionXLPipeline,
            )
        except ImportError as exc:
            raise BackendUnavailable(
                "PyTorch/diffusers are not installed. Run setup.bat (Windows) "
                "or ./setup.sh (Linux/Mac) on the machine running this server."
            ) from exc

        hw = probe_hardware()
        device = hw["device"]
        if device == "none":
            raise BackendUnavailable(hw["detail"])

        dtype = torch.float16 if device == "cuda" else torch.float32
        is_xl = spec.family == "sdxl"
        cls = StableDiffusionXLPipeline if is_xl else StableDiffusionPipeline

        common = {
            "torch_dtype": dtype,
            "use_safetensors": True,
        }
        if not is_xl:
            # Local generation, no server-side content filtering -- same posture
            # as every desktop Stable Diffusion tool.
            common["safety_checker"] = None
            common["requires_safety_checker"] = False

        if spec.repo.endswith(".safetensors"):
            pipe = cls.from_single_file(spec.repo, **common)
        else:
            pipe = cls.from_pretrained(
                spec.repo, cache_dir=str(self.cache_dir), **common
            )

        if spec.vae and not spec.repo.endswith(".safetensors"):
            if progress:
                progress("Loading VAE...")
            pipe.vae = AutoencoderKL.from_pretrained(
                spec.vae, cache_dir=str(self.cache_dir), torch_dtype=dtype
            )

        # Memory configuration. On a 4GB card SD1.5 fits resident; SDXL only
        # runs at all with sequential offload, which streams weights per
        # submodule and is slow but functional.
        vram = hw.get("vram_gb") or 0
        if device == "cuda":
            if is_xl and vram < 8:
                if progress:
                    progress("Enabling CPU offload for SDXL (slow but fits)...")
                pipe.enable_sequential_cpu_offload()
            elif vram < 6:
                pipe.to(device)
                pipe.enable_attention_slicing()
            else:
                pipe.to(device)
        else:
            pipe.to(device)
            pipe.enable_attention_slicing()

        # Cheap and always worth it at these resolutions.
        try:
            pipe.enable_vae_slicing()
            pipe.enable_vae_tiling()
        except Exception:
            pass

        try:
            pipe.enable_xformers_memory_efficient_attention()
        except Exception:
            pass  # xformers is optional; torch SDPA is fine

        pipe.set_progress_bar_config(disable=True)

        self._compel = self._build_compel(pipe, is_xl)
        return pipe

    def _build_compel(self, pipe, is_xl: bool):
        """Compel gives us >77 token prompts and (word:1.2) weighting.

        Without it, diffusers silently truncates at 77 tokens, which would cut
        off most of the style scaffolding and quietly ruin the whole point.
        """
        try:
            from compel import Compel, ReturnedEmbeddingsType
        except ImportError:
            log.warning("compel not installed - long prompts will be truncated")
            return None

        try:
            if is_xl:
                return Compel(
                    tokenizer=[pipe.tokenizer, pipe.tokenizer_2],
                    text_encoder=[pipe.text_encoder, pipe.text_encoder_2],
                    returned_embeddings_type=(
                        ReturnedEmbeddingsType.PENULTIMATE_HIDDEN_STATES_NON_NORMALIZED
                    ),
                    requires_pooled=[False, True],
                    truncate_long_prompts=False,
                )
            return Compel(
                tokenizer=pipe.tokenizer,
                text_encoder=pipe.text_encoder,
                truncate_long_prompts=False,
            )
        except Exception as exc:
            log.warning("compel init failed (%s) - falling back to plain prompts", exc)
            return None

    def _unload_locked(self):
        self._pipe = None
        self._img2img = None
        self._compel = None
        self._loaded_id = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def unload(self):
        with self._lock:
            self._unload_locked()
            self._load_status = "idle"
            self._load_detail = "Model unloaded"

    # -- generation -------------------------------------------------------

    def _encode(self, prompt: str, negative: str, is_xl: bool):
        """Returns kwargs for the pipeline call: embeddings if compel is
        available, plain strings otherwise."""
        if self._compel is None:
            return {"prompt": prompt, "negative_prompt": negative}

        if is_xl:
            cond, pooled = self._compel([prompt, negative])
            return {
                "prompt_embeds": cond[0:1],
                "pooled_prompt_embeds": pooled[0:1],
                "negative_prompt_embeds": cond[1:2],
                "negative_pooled_prompt_embeds": pooled[1:2],
            }

        cond = self._compel([prompt, negative])
        # Compel pads the pair to equal length so the two embeddings line up.
        return {
            "prompt_embeds": cond[0:1],
            "negative_prompt_embeds": cond[1:2],
        }

    def generate(
        self,
        *,
        model_id: str,
        prompt: str,
        negative: str,
        width: int,
        height: int,
        steps: int,
        guidance: float,
        sampler: str,
        seed: int,
        hires: bool = False,
        hires_strength: float = 0.35,
        on_step: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ):
        """Run one generation. Returns a PIL image."""
        import torch

        pipe = self.load(model_id)
        is_xl = self._loaded_family == "sdxl"

        pipe.scheduler = _build_scheduler(sampler, pipe.scheduler.config)

        device = "cuda" if torch.cuda.is_available() else "cpu"
        generator = torch.Generator(device=device).manual_seed(int(seed))

        total_steps = steps + (int(steps * hires_strength) if hires else 0)
        state = {"done": 0}

        def _callback(pipe_ref, step_index, timestep, kwargs):
            if should_cancel and should_cancel():
                raise GenerationCancelled()
            state["done"] += 1
            if on_step:
                on_step(state["done"], total_steps)
            return kwargs

        encoded = self._encode(prompt, negative, is_xl)

        call_kwargs: dict[str, Any] = {
            **encoded,
            "width": width,
            "height": height,
            "num_inference_steps": steps,
            "guidance_scale": guidance,
            "generator": generator,
            "callback_on_step_end": _callback,
        }

        try:
            result = pipe(**call_kwargs)
        except TypeError:
            # Older diffusers: legacy callback signature.
            call_kwargs.pop("callback_on_step_end", None)

            def _legacy(step, timestep, latents):
                if should_cancel and should_cancel():
                    raise GenerationCancelled()
                state["done"] += 1
                if on_step:
                    on_step(state["done"], total_steps)

            call_kwargs["callback"] = _legacy
            call_kwargs["callback_steps"] = 1
            result = pipe(**call_kwargs)

        image = result.images[0]

        if hires:
            try:
                image = self._hires_pass(
                    image=image,
                    encoded=encoded,
                    guidance=guidance,
                    steps=steps,
                    strength=hires_strength,
                    generator=generator,
                    is_xl=is_xl,
                    on_step=on_step,
                    should_cancel=should_cancel,
                    state=state,
                    total_steps=total_steps,
                )
            except GenerationCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                # The detail pass is an enhancement, not the deliverable. It can
                # legitimately fail -- OOM at the larger resolution, or broken
                # offload hooks when rebuilding from an SDXL pipeline running
                # under sequential CPU offload. Keep the base image either way.
                log.warning("Detail pass failed (%s) - keeping base image", exc)

        return image

    def _hires_pass(
        self,
        *,
        image,
        encoded,
        guidance,
        steps,
        strength,
        generator,
        is_xl,
        on_step,
        should_cancel,
        state,
        total_steps,
    ):
        """Upscale 1.5x then lightly re-denoise.

        This is the single biggest quality win for faces at SD1.5 resolutions:
        the first pass gets composition right, the second pass repaints detail
        at a resolution where eyes and teeth have enough pixels to resolve.
        """
        from diffusers import (
            StableDiffusionImg2ImgPipeline,
            StableDiffusionXLImg2ImgPipeline,
        )

        if self._img2img is None:
            cls = (
                StableDiffusionXLImg2ImgPipeline
                if is_xl
                else StableDiffusionImg2ImgPipeline
            )
            # Reuses the loaded weights -- no extra VRAM for a second model.
            self._img2img = cls(**self._pipe.components)
            self._img2img.set_progress_bar_config(disable=True)

        target = (int(image.width * 1.5) // 8 * 8, int(image.height * 1.5) // 8 * 8)
        from PIL import Image

        upscaled = image.resize(target, Image.LANCZOS)

        def _callback(pipe_ref, step_index, timestep, kwargs):
            if should_cancel and should_cancel():
                raise GenerationCancelled()
            state["done"] += 1
            if on_step:
                on_step(min(state["done"], total_steps), total_steps)
            return kwargs

        kwargs: dict[str, Any] = {
            **encoded,
            "image": upscaled,
            "strength": strength,
            "guidance_scale": guidance,
            "num_inference_steps": steps,
            "generator": generator,
            "callback_on_step_end": _callback,
        }

        try:
            return self._img2img(**kwargs).images[0]
        except TypeError:
            kwargs.pop("callback_on_step_end", None)
            return self._img2img(**kwargs).images[0]
