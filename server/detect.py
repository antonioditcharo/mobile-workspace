"""Face and hand detection for the detail pass.

Three backends, tried best-first, because the good one needs a download and
the app has to keep working when that download hasn't happened:

  1. ultralytics YOLO  - best quality, faces AND hands, needs weight files
  2. MediaPipe         - good, and the practical default (see below)
  3. OpenCV Haar       - frontal faces only, last resort

Two traps worth knowing about, both found the hard way:

  - MediaPipe 0.10.x bundles its models in the wheel; 1.x dropped that AND the
    `mp.solutions` API entirely, exposing only the Tasks API with no weights.
    Both generations are supported here, and the 1.x path fetches its (small)
    model files on first use.
  - OpenCV 5 removed the bundled Haar cascades, so the "always available"
    fallback is not actually always available. It is checked for, not assumed.

Everything is optional. With none of them installed the detail pass reports
that it found nothing and generation is otherwise unaffected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

log = logging.getLogger("localgen.detect")

Kind = Literal["face", "hand"]

# YOLO detector weights, if the user has fetched them. Same filenames the
# A1111 ADetailer extension uses, so existing downloads drop straight in.
YOLO_FILES: dict[Kind, list[str]] = {
    "face": ["face_yolov8m.pt", "face_yolov8n.pt", "face_yolov8s.pt"],
    "hand": ["hand_yolov8n.pt", "hand_yolov8s.pt"],
}


@dataclass
class Region:
    kind: Kind
    box: tuple[int, int, int, int]  # x1, y1, x2, y2
    score: float = 1.0

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.box
        return max(0, x2 - x1) * max(0, y2 - y1)


def _clamp_box(box, width: int, height: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = box
    x1 = max(0, min(int(x1), width))
    y1 = max(0, min(int(y1), height))
    x2 = max(0, min(int(x2), width))
    y2 = max(0, min(int(y2), height))
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    return x1, y1, x2, y2


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------

def _detect_yolo(image, kinds: list[Kind], weights_dir: Path, conf: float):
    try:
        from ultralytics import YOLO
    except ImportError:
        return None

    found: list[Region] = []
    used = False

    for kind in kinds:
        weight_path = None
        for name in YOLO_FILES.get(kind, []):
            candidate = weights_dir / name
            if candidate.exists():
                weight_path = candidate
                break
        if weight_path is None:
            continue

        try:
            model = _yolo_cache.get(str(weight_path))
            if model is None:
                model = YOLO(str(weight_path))
                _yolo_cache[str(weight_path)] = model
            results = model.predict(image, conf=conf, verbose=False)
        except Exception as exc:
            log.warning("YOLO %s detection failed: %s", kind, exc)
            continue

        used = True
        for result in results:
            for box in result.boxes:
                coords = box.xyxy[0].tolist()
                found.append(
                    Region(
                        kind=kind,
                        box=_clamp_box(coords, image.width, image.height),
                        score=float(box.conf[0]),
                    )
                )

    return found if used else None


_yolo_cache: dict[str, object] = {}


# MediaPipe split its API in two. 0.10.x has `mp.solutions` with model weights
# baked into the wheel; 1.x dropped both, exposing only the Tasks API and
# shipping no models at all. Supporting one or the other would silently break
# for half of users, so both are handled.
MP_TASK_MODELS: dict[Kind, tuple[str, str]] = {
    "face": (
        "blaze_face_short_range.tflite",
        "https://storage.googleapis.com/mediapipe-models/face_detector/"
        "blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
    ),
    "hand": (
        "hand_landmarker.task",
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
        "hand_landmarker/float16/1/hand_landmarker.task",
    ),
}

_mp_cache: dict[str, object] = {}


def mp_model_path(kind: Kind, weights_dir: Path, download: bool = True) -> Path | None:
    """Locate (and on first use fetch) a MediaPipe Tasks model file.

    These are small -- a few hundred KB for the face detector -- and land in
    the detectors folder alongside any YOLO weights.
    """
    entry = MP_TASK_MODELS.get(kind)
    if entry is None:
        return None
    filename, url = entry

    path = weights_dir / filename
    if path.exists():
        return path
    if not download:
        return None

    try:
        import urllib.request

        weights_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        log.info("Downloading MediaPipe %s model...", kind)
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(path)
        return path
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not download MediaPipe %s model: %s", kind, exc)
        return None


def _mp_has_legacy() -> bool:
    try:
        import mediapipe as mp

        return hasattr(mp, "solutions")
    except ImportError:
        return False


def _detect_mediapipe_legacy(image, kinds: list[Kind], conf: float):
    """MediaPipe 0.10.x: models ship inside the wheel, nothing to download."""
    import mediapipe as mp
    import numpy as np

    array = np.array(image.convert("RGB"))
    width, height = image.width, image.height
    found: list[Region] = []
    used = False

    if "face" in kinds:
        try:
            with mp.solutions.face_detection.FaceDetection(
                model_selection=1, min_detection_confidence=conf
            ) as detector:
                result = detector.process(array)
            used = True
            for detection in result.detections or []:
                rel = detection.location_data.relative_bounding_box
                found.append(
                    Region(
                        kind="face",
                        box=_clamp_box(
                            (
                                rel.xmin * width,
                                rel.ymin * height,
                                (rel.xmin + rel.width) * width,
                                (rel.ymin + rel.height) * height,
                            ),
                            width,
                            height,
                        ),
                        score=float(detection.score[0]) if detection.score else 1.0,
                    )
                )
        except Exception as exc:
            log.warning("MediaPipe (legacy) face detection failed: %s", exc)

    if "hand" in kinds:
        try:
            with mp.solutions.hands.Hands(
                static_image_mode=True,
                max_num_hands=4,
                min_detection_confidence=conf,
            ) as detector:
                result = detector.process(array)
            used = True
            for landmarks in result.multi_hand_landmarks or []:
                xs = [lm.x * width for lm in landmarks.landmark]
                ys = [lm.y * height for lm in landmarks.landmark]
                found.append(
                    Region(
                        kind="hand",
                        box=_clamp_box(
                            (min(xs), min(ys), max(xs), max(ys)), width, height
                        ),
                    )
                )
        except Exception as exc:
            log.warning("MediaPipe (legacy) hand detection failed: %s", exc)

    return found if used else None


def _detect_mediapipe_tasks(image, kinds: list[Kind], weights_dir: Path, conf: float):
    """MediaPipe 1.x Tasks API. Model files are fetched once into weights_dir."""
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks.python import BaseOptions
    from mediapipe.tasks.python import vision

    array = np.ascontiguousarray(np.array(image.convert("RGB")))
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=array)
    width, height = image.width, image.height
    found: list[Region] = []
    used = False

    if "face" in kinds:
        model = mp_model_path("face", weights_dir)
        if model is not None:
            try:
                detector = _mp_cache.get("face")
                if detector is None:
                    detector = vision.FaceDetector.create_from_options(
                        vision.FaceDetectorOptions(
                            base_options=BaseOptions(model_asset_path=str(model)),
                            min_detection_confidence=conf,
                        )
                    )
                    _mp_cache["face"] = detector
                result = detector.detect(mp_image)
                used = True
                for detection in result.detections or []:
                    box = detection.bounding_box
                    score = 1.0
                    if detection.categories:
                        score = float(detection.categories[0].score or 1.0)
                    found.append(
                        Region(
                            kind="face",
                            box=_clamp_box(
                                (
                                    box.origin_x,
                                    box.origin_y,
                                    box.origin_x + box.width,
                                    box.origin_y + box.height,
                                ),
                                width,
                                height,
                            ),
                            score=score,
                        )
                    )
            except Exception as exc:
                log.warning("MediaPipe (tasks) face detection failed: %s", exc)

    if "hand" in kinds:
        model = mp_model_path("hand", weights_dir)
        if model is not None:
            try:
                landmarker = _mp_cache.get("hand")
                if landmarker is None:
                    landmarker = vision.HandLandmarker.create_from_options(
                        vision.HandLandmarkerOptions(
                            base_options=BaseOptions(model_asset_path=str(model)),
                            num_hands=4,
                            min_hand_detection_confidence=conf,
                        )
                    )
                    _mp_cache["hand"] = landmarker
                result = landmarker.detect(mp_image)
                used = True
                # Landmarks, not boxes -- wrap the points.
                for landmarks in result.hand_landmarks or []:
                    xs = [lm.x * width for lm in landmarks]
                    ys = [lm.y * height for lm in landmarks]
                    found.append(
                        Region(
                            kind="hand",
                            box=_clamp_box(
                                (min(xs), min(ys), max(xs), max(ys)), width, height
                            ),
                        )
                    )
            except Exception as exc:
                log.warning("MediaPipe (tasks) hand detection failed: %s", exc)

    return found if used else None


def _detect_mediapipe(image, kinds: list[Kind], weights_dir: Path, conf: float):
    try:
        import mediapipe  # noqa: F401
    except ImportError:
        return None

    if _mp_has_legacy():
        return _detect_mediapipe_legacy(image, kinds, conf)
    return _detect_mediapipe_tasks(image, kinds, weights_dir, conf)


def _detect_haar(image, kinds: list[Kind]):
    """Last resort. Frontal faces only, but opencv always has the cascade."""
    if "face" not in kinds:
        return None
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
    if not cascade_path.exists():
        return None

    cascade = cv2.CascadeClassifier(str(cascade_path))
    gray = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)
    faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                     minSize=(48, 48))

    return [
        Region(
            kind="face",
            box=_clamp_box((x, y, x + w, y + h), image.width, image.height),
            score=0.5,
        )
        for (x, y, w, h) in faces
    ]


# --------------------------------------------------------------------------
# Public
# --------------------------------------------------------------------------

def detector_status(weights_dir: Path) -> dict:
    """What the UI shows in Settings, so a missing backend is visible."""
    status = {"backend": "none", "faces": False, "hands": False, "detail": ""}

    yolo_face = any((weights_dir / n).exists() for n in YOLO_FILES["face"])
    yolo_hand = any((weights_dir / n).exists() for n in YOLO_FILES["hand"])

    try:
        import ultralytics  # noqa: F401

        if yolo_face or yolo_hand:
            status.update(
                backend="yolo",
                faces=yolo_face,
                hands=yolo_hand,
                detail="YOLO detectors loaded",
            )
            return status
    except ImportError:
        pass

    try:
        import mediapipe  # noqa: F401

        if _mp_has_legacy():
            status.update(
                backend="mediapipe",
                faces=True,
                hands=True,
                detail="MediaPipe 0.10 - models bundled, nothing to download",
            )
        else:
            # 1.x ships no weights; report what is actually on disk rather than
            # promising a capability that depends on a download succeeding.
            face_ready = (weights_dir / MP_TASK_MODELS["face"][0]).exists()
            hand_ready = (weights_dir / MP_TASK_MODELS["hand"][0]).exists()
            status.update(
                backend="mediapipe-tasks",
                faces=True,
                hands=True,
                detail=(
                    "MediaPipe 1.x - model files "
                    + (
                        "ready"
                        if (face_ready and hand_ready)
                        else "download automatically on first use"
                    )
                ),
            )
        return status
    except ImportError:
        pass

    try:
        import cv2

        # OpenCV 5 removed the bundled cascades, so this can be absent even
        # with cv2 installed.
        if (Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml").exists():
            status.update(
                backend="haar",
                faces=True,
                hands=False,
                detail="OpenCV Haar - frontal faces only. Install mediapipe for hands.",
            )
            return status
    except ImportError:
        pass

    status["detail"] = "No detector installed - detail pass disabled"
    return status


def detect(
    image,
    kinds: list[Kind],
    weights_dir: Path,
    conf: float = 0.35,
    min_area_ratio: float = 0.0006,
) -> list[Region]:
    """Find faces/hands, largest first.

    Regions below min_area_ratio of the frame are dropped: re-rendering a face
    that is twelve pixels across produces a smear, not a fix.
    """
    if not kinds:
        return []

    regions = None
    for backend in (
        lambda: _detect_yolo(image, kinds, weights_dir, conf),
        lambda: _detect_mediapipe(image, kinds, weights_dir, conf),
        lambda: _detect_haar(image, kinds),
    ):
        try:
            regions = backend()
        except Exception as exc:  # noqa: BLE001
            log.warning("detector error: %s", exc)
            regions = None
        if regions is not None:
            break

    if not regions:
        return []

    frame_area = image.width * image.height
    kept = [r for r in regions if r.area >= frame_area * min_area_ratio]
    kept.sort(key=lambda r: r.area, reverse=True)
    return kept
