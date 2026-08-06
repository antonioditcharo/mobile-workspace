"""FastAPI app: REST + SSE, and it serves the mobile PWA itself.

Everything is local. The server binds your LAN so the phone can reach it, and
nothing it does touches an external service except the one-time model download
from HuggingFace during setup.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, presets
from .jobs import Job, JobManager, JobRequest, encode_preview, save_image
from .detect import detector_status
from .pipeline import (
    DEFAULT_MODEL_ID,
    SAMPLERS,
    BackendUnavailable,
    GenerationCancelled,
    PipelineManager,
    probe_hardware,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s"
)
log = logging.getLogger("localgen")

config.ensure_dirs()

app = FastAPI(title="Local Image Generator", docs_url="/api/docs")

# The PWA is served from this same origin, but a phone browser may hit the
# server by IP while the manifest was cached under a hostname. Permissive CORS
# is safe here: the server is LAN-only and holds no credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

pipeline = PipelineManager(
    cache_dir=config.MODEL_CACHE_DIR,
    checkpoint_dir=config.CHECKPOINT_DIR,
    lora_dir=config.LORA_DIR,
    detector_dir=config.DETECTOR_DIR,
)


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------

class LoraSelection(BaseModel):
    id: str
    weight: float = Field(default=0.8, ge=-2.0, le=2.0)


class GenerateRequest(BaseModel):
    prompt: str = ""
    negative: str = ""
    preset: str = presets.DEFAULT_PRESET_ID
    model: str = DEFAULT_MODEL_ID
    aspect: str = presets.DEFAULT_ASPECT
    steps: int | None = None
    guidance: float | None = None
    sampler: str | None = None
    seed: int = -1
    batch: int = Field(default=1, ge=1, le=config.MAX_BATCH)
    hires: bool = False
    loras: list[LoraSelection] = Field(default_factory=list)
    refine_face: bool = False
    refine_hands: bool = False
    refine_strength: float = Field(default=0.4, ge=0.1, le=0.8)


class LoadRequest(BaseModel):
    model: str


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------

def _stub_image(job: Job, index: int):
    """Placeholder generator used when LOCALGEN_STUB=1.

    Exists so the phone UI, queue, progress stream, gallery and download path
    can all be exercised before any model weights exist on disk.
    """
    from PIL import Image, ImageDraw

    req = job.request
    rnd = random.Random(req.seed + index)
    width, height = req.width, req.height
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)

    base = (rnd.randint(30, 90), rnd.randint(30, 90), rnd.randint(40, 110))
    for y in range(height):
        ratio = y / max(height - 1, 1)
        draw.line(
            [(0, y), (width, y)],
            fill=(
                int(base[0] + ratio * 90),
                int(base[1] + ratio * 70),
                int(base[2] + ratio * 50),
            ),
        )

    for _ in range(int(width * height * 0.02)):
        x, y = rnd.randrange(width), rnd.randrange(height)
        shade = rnd.randint(0, 255)
        draw.point((x, y), fill=(shade, shade, shade))

    label = [
        "STUB MODE",
        f"preset: {req.preset_id}",
        f"seed: {req.seed + index}",
        f"{width}x{height}",
        (req.prompt or "(no prompt)")[:38],
    ]
    for i, line in enumerate(label):
        draw.text((14, 14 + i * 16), line, fill=(255, 255, 255))

    total = req.steps
    for step in range(total):
        if job.cancelled:
            raise GenerationCancelled()
        time.sleep(0.05)
        _publish_step(job, index, step + 1, total)

    # Mirror the real pipeline's extra phases so their UI states are reachable
    # without a GPU.
    if req.loras:
        _publish_note(job, f"Applying {len(req.loras)} LoRA(s)")
        time.sleep(0.3)
    for kind, on in (("face", req.refine_face), ("hand", req.refine_hands)):
        if not on:
            continue
        _publish_note(job, f"Refining {kind} 1/1")
        time.sleep(0.4)
        draw.rectangle(
            [width // 4, height // 4, width * 3 // 4, height * 3 // 4],
            outline=(255, 138, 76),
            width=3,
        )
        draw.text((width // 4 + 6, height // 4 + 6), f"{kind} refined",
                  fill=(255, 138, 76))

    return image


def _publish_step(job: Job, image_index: int, step: int, total: int) -> None:
    batch = max(job.request.batch, 1)
    overall = (image_index + step / max(total, 1)) / batch
    job.progress = round(min(overall, 0.999), 4)
    job.message = f"Image {image_index + 1}/{batch} - step {step}/{total}"
    job.emit(
        {
            "type": "progress",
            "id": job.id,
            "progress": job.progress,
            "message": job.message,
            "step": step,
            "total_steps": total,
            "image_index": image_index,
        }
    )


def _publish_note(job: Job, text: str) -> None:
    """Status text for phases that have no step count (detection, refining)."""
    job.message = text
    job.emit(
        {
            "type": "progress",
            "id": job.id,
            "progress": job.progress,
            "message": text,
        }
    )


def run_job(job: Job) -> None:
    req = job.request
    job.status = "running"
    job.message = "Starting"
    job.emit({"type": "status", **job.public()})

    preset = presets.PRESETS_BY_ID.get(req.preset_id) or presets.PRESETS_BY_ID[
        presets.DEFAULT_PRESET_ID
    ]

    if not config.STUB_MODE:
        hw = probe_hardware()
        if not hw["ready"]:
            raise BackendUnavailable(hw["detail"])

        if pipeline.loaded_model_id != req.model_id:
            job.status = "loading"
            job.message = "Loading model (first run downloads several GB)"
            job.emit({"type": "status", **job.public()})

            def _note(text: str) -> None:
                job.message = text
                job.emit({"type": "status", **job.public()})

            pipeline.load(req.model_id, progress=_note)
            job.status = "running"

    for index in range(req.batch):
        if job.cancelled:
            break

        seed = req.seed + index

        try:
            if config.STUB_MODE:
                image = _stub_image(job, index)
            else:
                image = pipeline.generate(
                    model_id=req.model_id,
                    prompt=req.full_prompt,
                    negative=req.full_negative,
                    width=req.width,
                    height=req.height,
                    steps=req.steps,
                    guidance=req.guidance,
                    sampler=req.sampler,
                    seed=seed,
                    hires=req.hires,
                    loras=req.loras,
                    refine_face=req.refine_face,
                    refine_hands=req.refine_hands,
                    refine_strength=req.refine_strength,
                    on_step=lambda done, total, i=index: _publish_step(
                        job, i, done, total
                    ),
                    should_cancel=lambda: job.cancelled,
                    on_note=lambda text: _publish_note(job, text),
                )
        except GenerationCancelled:
            break

        meta: dict[str, Any] = {
            "prompt": req.prompt,
            "negative": req.negative,
            "full_prompt": req.full_prompt,
            "full_negative": req.full_negative,
            "preset": req.preset_id,
            "preset_label": preset.label,
            "model": req.model_id,
            "width": req.width,
            "height": req.height,
            "steps": req.steps,
            "guidance": req.guidance,
            "sampler": req.sampler,
            "seed": seed,
            "hires": req.hires,
            "loras": req.loras,
            "refine_face": req.refine_face,
            "refine_hands": req.refine_hands,
            "stub": config.STUB_MODE,
            "created": time.time(),
        }

        record = save_image(image, config.OUTPUT_DIR, meta)
        record["preview"] = encode_preview(image)
        job.images.append(record)
        job.emit({"type": "image", "id": job.id, "image": record})

    if job.cancelled and not job.images:
        job.status = "cancelled"
        job.message = "Cancelled"
    else:
        job.status = "done"
        job.progress = 1.0
        job.message = f"Done - {len(job.images)} image(s)"

    job.emit({"type": "status", **job.public()})
    job.emit({"type": "end", "id": job.id})


manager = JobManager(run_job)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/api/health")
def health() -> dict[str, Any]:
    hw = probe_hardware()
    return {
        "ok": True,
        "stub": config.STUB_MODE,
        "hardware": hw,
        "pipeline": pipeline.status(),
        "detector": detector_status(config.DETECTOR_DIR),
        "output_dir": str(config.OUTPUT_DIR),
        "lora_dir": str(config.LORA_DIR),
        "lan_url": f"http://{config.lan_ip()}:{config.PORT}",
    }


@app.get("/api/options")
def options() -> dict[str, Any]:
    return {
        "presets": presets.presets_payload(),
        "aspects": presets.aspects_payload(),
        "samplers": [{"id": k, "label": v} for k, v in SAMPLERS.items()],
        "models": pipeline.available_models(),
        "loras": pipeline.discover_loras(),
        "detector": detector_status(config.DETECTOR_DIR),
        "defaults": {
            "preset": presets.DEFAULT_PRESET_ID,
            "aspect": presets.DEFAULT_ASPECT,
            "model": DEFAULT_MODEL_ID,
            "max_batch": config.MAX_BATCH,
            "max_steps": config.MAX_STEPS,
        },
    }


@app.get("/api/models")
def models() -> dict[str, Any]:
    return {"models": pipeline.available_models()}


@app.post("/api/models/load")
def load_model(body: LoadRequest) -> dict[str, Any]:
    try:
        pipeline.load(body.model)
    except Exception as exc:  # noqa: BLE001 - reported to the client
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "status": pipeline.status()}


@app.post("/api/models/unload")
def unload_model() -> dict[str, Any]:
    pipeline.unload()
    return {"ok": True, "status": pipeline.status()}


def _with_lora_triggers(subject: str, loras: list[dict[str, Any]]) -> str:
    """Prepend each selected LoRA's trigger words to the subject.

    Style LoRAs frequently do nothing without their trigger token, and that
    failure is silent -- the image just comes out unstyled. Injecting from the
    .txt sidecar means nobody has to type "analogfilm_v2_style" on a phone.
    """
    if not loras:
        return subject

    available = {entry["id"]: entry for entry in pipeline.discover_loras()}
    triggers = []
    for selection in loras:
        entry = available.get(selection.get("id"))
        if entry and entry.get("trigger"):
            triggers.append(entry["trigger"])

    if not triggers:
        return subject

    joined = ", ".join(triggers)
    return f"{joined}, {subject}".strip().strip(",") if subject else joined


@app.get("/api/loras")
def loras() -> dict[str, Any]:
    return {"loras": pipeline.discover_loras(), "dir": str(config.LORA_DIR)}


@app.post("/api/preview-prompt")
def preview_prompt(body: GenerateRequest) -> dict[str, Any]:
    """Show exactly what gets sent to the model. No secret sauce hidden."""
    preset = presets.PRESETS_BY_ID.get(body.preset) or presets.PRESETS_BY_ID[
        presets.DEFAULT_PRESET_ID
    ]
    return {
        "prompt": presets.build_prompt(body.prompt, preset),
        "negative": presets.build_negative(preset, body.negative),
    }


@app.post("/api/generate")
def generate(body: GenerateRequest) -> dict[str, Any]:
    preset = presets.PRESETS_BY_ID.get(body.preset)
    if preset is None:
        raise HTTPException(status_code=400, detail=f"Unknown preset: {body.preset}")

    aspect = body.aspect if body.aspect in presets.ASPECTS else preset.aspect
    width, height = presets.resolve_dimensions(aspect)

    steps = body.steps if body.steps is not None else preset.steps
    steps = max(1, min(int(steps), config.MAX_STEPS))

    guidance = body.guidance if body.guidance is not None else preset.guidance
    guidance = max(1.0, min(float(guidance), 20.0))

    sampler = body.sampler or preset.sampler
    if sampler not in SAMPLERS:
        sampler = preset.sampler

    seed = body.seed
    if seed is None or seed < 0:
        seed = random.randint(0, 2**31 - 1)

    loras = [{"id": l.id, "weight": l.weight} for l in body.loras]
    subject = _with_lora_triggers(body.prompt, loras)

    request = JobRequest(
        prompt=body.prompt.strip(),
        negative=body.negative.strip(),
        preset_id=preset.id,
        model_id=body.model,
        width=width,
        height=height,
        steps=steps,
        guidance=guidance,
        sampler=sampler,
        seed=int(seed),
        batch=int(body.batch),
        hires=bool(body.hires),
        loras=loras,
        refine_face=bool(body.refine_face),
        refine_hands=bool(body.refine_hands),
        refine_strength=float(body.refine_strength),
        full_prompt=presets.build_prompt(subject, preset),
        full_negative=presets.build_negative(preset, body.negative),
    )

    job = manager.submit(request)
    return {"job": job.public()}


@app.get("/api/jobs")
def list_jobs(limit: int = 20) -> dict[str, Any]:
    return {"jobs": manager.recent(limit)}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    return {"job": job.public()}


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")
    job.cancel()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/events")
def job_events(job_id: str) -> StreamingResponse:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job")

    def stream():
        sub = job.subscribe()
        try:
            while True:
                try:
                    event = sub.get(timeout=15)
                except Exception:
                    # Keepalive: phones and proxies drop idle connections.
                    yield ": keepalive\n\n"
                    if job.status in ("done", "error", "cancelled"):
                        break
                    continue

                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "end":
                    break
        finally:
            job.unsubscribe(sub)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --------------------------------------------------------------------------
# Gallery
# --------------------------------------------------------------------------

def _safe_output(name: str) -> Path:
    """Reject anything that escapes the output directory."""
    candidate = (config.OUTPUT_DIR / name).resolve()
    if not str(candidate).startswith(str(config.OUTPUT_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Bad filename")
    return candidate


@app.get("/api/gallery")
def gallery(limit: int = 100) -> dict[str, Any]:
    files = sorted(
        config.OUTPUT_DIR.glob("*.png"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:limit]

    items = []
    for path in files:
        meta = {}
        sidecar = path.with_suffix(".json")
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text("utf-8"))
            except Exception:
                meta = {}
        items.append(
            {
                "filename": path.name,
                "url": f"/api/images/{path.name}",
                "thumb": f"/api/images/{path.name}?thumb=1",
                "created": path.stat().st_mtime,
                "meta": meta,
            }
        )
    return {"images": items}


@app.get("/api/images/{name}")
def image(name: str, thumb: int = 0, download: int = 0):
    path = _safe_output(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found")

    if thumb:
        from io import BytesIO

        from fastapi.responses import Response
        from PIL import Image

        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((400, 400), Image.LANCZOS)
            buffer = BytesIO()
            img.save(buffer, format="JPEG", quality=80)
        return Response(
            content=buffer.getvalue(),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    headers = {}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return FileResponse(path, media_type="image/png", headers=headers)


@app.delete("/api/images/{name}")
def delete_image(name: str) -> dict[str, Any]:
    path = _safe_output(name)
    if path.exists():
        path.unlink()
    sidecar = path.with_suffix(".json")
    if sidecar.exists():
        sidecar.unlink()
    return {"ok": True}


# --------------------------------------------------------------------------
# Static PWA
# --------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html", media_type="text/html")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    # Must be served from the root scope to control the whole app.
    return FileResponse(
        config.WEB_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(
        config.WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json"
    )


if config.WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


@app.exception_handler(BackendUnavailable)
def backend_unavailable(_request, exc: BackendUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})
