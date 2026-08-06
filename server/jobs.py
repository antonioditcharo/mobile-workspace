"""Generation job queue.

One worker thread. A GPU serializes work anyway, and a queue gives the phone
UI something honest to display ("2nd in line") instead of silently stalling.

Each job publishes events to any number of SSE subscribers. Subscribers that
join late get the full backlog replayed, so a phone that locks its screen
mid-generation and reconnects doesn't lose the result.
"""

from __future__ import annotations

import base64
import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable


@dataclass
class JobRequest:
    prompt: str
    negative: str
    preset_id: str
    model_id: str
    width: int
    height: int
    steps: int
    guidance: float
    sampler: str
    seed: int
    batch: int
    hires: bool
    loras: list[dict] = field(default_factory=list)
    refine_face: bool = False
    refine_hands: bool = False
    refine_strength: float = 0.4
    full_prompt: str = ""
    full_negative: str = ""


@dataclass
class Job:
    id: str
    request: JobRequest
    status: str = "queued"  # queued|loading|running|done|error|cancelled
    created: float = field(default_factory=time.time)
    progress: float = 0.0
    message: str = "Queued"
    images: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    queue_position: int = 0

    _events: list[dict] = field(default_factory=list, repr=False)
    _subscribers: list[queue.Queue] = field(default_factory=list, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, event: dict) -> None:
        with self._lock:
            self._events.append(event)
            subscribers = list(self._subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(event)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        sub: queue.Queue = queue.Queue(maxsize=512)
        with self._lock:
            backlog = list(self._events)
            self._subscribers.append(sub)
        for event in backlog:
            try:
                sub.put_nowait(event)
            except queue.Full:
                break
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "images": self.images,
            "error": self.error,
            "queue_position": self.queue_position,
            "params": {
                "prompt": self.request.prompt,
                "negative": self.request.negative,
                "preset": self.request.preset_id,
                "model": self.request.model_id,
                "width": self.request.width,
                "height": self.request.height,
                "steps": self.request.steps,
                "guidance": self.request.guidance,
                "sampler": self.request.sampler,
                "seed": self.request.seed,
                "batch": self.request.batch,
                "hires": self.request.hires,
                "loras": self.request.loras,
                "refine_face": self.request.refine_face,
                "refine_hands": self.request.refine_hands,
            },
        }


class JobManager:
    """Single-worker job runner with replayable progress events."""

    def __init__(self, runner: Callable[[Job], None], max_history: int = 60):
        self._runner = runner
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._max_history = max_history
        self._current: str | None = None
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def submit(self, request: JobRequest) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], request=request)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._trim_locked()
            pending = self._queue.qsize()
        job.queue_position = pending + (1 if self._current else 0)
        if job.queue_position > 0:
            job.message = f"Queued - {job.queue_position} ahead"
        job.emit({"type": "status", **job.public()})
        self._queue.put(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order))[:limit]
        return [self._jobs[i].public() for i in ids if i in self._jobs]

    def _trim_locked(self) -> None:
        while len(self._order) > self._max_history:
            stale = self._order.pop(0)
            self._jobs.pop(stale, None)

    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self._jobs.get(job_id)
            if job is None:
                continue
            if job.cancelled:
                job.status = "cancelled"
                job.message = "Cancelled"
                job.emit({"type": "status", **job.public()})
                continue

            self._current = job_id
            try:
                self._runner(job)
            except Exception as exc:  # noqa: BLE001 - surfaced to the client
                job.status = "error"
                job.error = str(exc)
                job.message = f"Failed: {exc}"
                job.emit({"type": "status", **job.public()})
            finally:
                self._current = None
                self._renumber()

    def _renumber(self) -> None:
        """Refresh queue positions so waiting clients see the line move."""
        with self._lock:
            waiting = [
                self._jobs[i]
                for i in self._order
                if i in self._jobs and self._jobs[i].status == "queued"
            ]
        for index, job in enumerate(waiting):
            job.queue_position = index
            job.message = "Next up" if index == 0 else f"Queued - {index} ahead"
            job.emit({"type": "status", **job.public()})


def encode_preview(image, max_side: int = 512) -> str:
    """Small base64 JPEG for instant display on the phone.

    The full-resolution PNG stays on disk and is fetched only when the user
    actually opens or downloads it -- important on a phone over WiFi.
    """
    from PIL import Image

    preview = image.copy()
    preview.thumbnail((max_side, max_side), Image.LANCZOS)
    buffer = BytesIO()
    preview.convert("RGB").save(buffer, format="JPEG", quality=82)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def save_image(image, output_dir: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Write PNG plus embedded metadata and a JSON sidecar."""
    from PIL import PngImagePlugin

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{meta.get('seed', 0)}-{uuid.uuid4().hex[:6]}.png"
    path = output_dir / name

    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", json.dumps(meta, ensure_ascii=False))
    info.add_text("prompt", str(meta.get("full_prompt", "")))
    image.save(path, format="PNG", pnginfo=info)

    sidecar = path.with_suffix(".json")
    sidecar.write_text(json.dumps(meta, indent=2, ensure_ascii=False), "utf-8")

    return {
        "filename": name,
        "url": f"/api/images/{name}",
        "width": image.width,
        "height": image.height,
        "seed": meta.get("seed"),
        "created": time.time(),
        "meta": meta,
    }
