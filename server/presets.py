"""Style presets and prompt scaffolding.

This module is the realism engine. Model choice matters, but the difference
between "obviously AI" and "found in a shoebox" is mostly prompt construction:
naming a real film stock and camera, describing the *flaws* of that medium, and
aggressively negating the glossy render aesthetic that base models drift toward.

Three rules encoded here:

1. Name the medium, not the mood. "Kodak Gold 200, direct flash" beats
   "nostalgic warm photo" every time -- the model has seen millions of images
   captioned with film stock names and almost none captioned "nostalgic".
2. Ask for imperfection explicitly. Real snapshots have blown highlights, red
   eye, tilted horizons, and skin with pores. Models default to correcting all
   of that, so it has to be requested.
3. Negate the render look harder than you affirm the photo look. Most of the
   "AI sheen" lives in the negative prompt's absence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# --------------------------------------------------------------------------
# Shared prompt scaffolding
# --------------------------------------------------------------------------

# Prepended to every prompt. Kept short so it never crowds out the user's
# subject -- the presets carry the heavy styling.
BASE_POSITIVE = (
    "amateur snapshot, candid unposed moment, authentic photograph, "
    "natural skin texture with visible pores and skin imperfections, "
    "unretouched, imperfect casual framing"
)

# Anatomy failures. Always negated -- no preset wants six fingers.
NEG_ANATOMY = (
    "bad anatomy, bad proportions, deformed, disfigured, mutated, mutation, "
    "extra limbs, missing limbs, extra arms, extra legs, malformed limbs, "
    "bad hands, poorly drawn hands, mutated hands, extra fingers, "
    "fused fingers, too many fingers, missing fingers, long neck, "
    "cloned face, poorly drawn face, deformed iris, deformed pupils, "
    "crossed eyes, misaligned eyes, bad teeth, deformed teeth"
)

# The "AI sheen". This is the block doing the heavy lifting for your ask --
# every term here is a specific failure mode that makes an image read as
# generated rather than captured.
NEG_AI_LOOK = (
    "cgi, 3d render, octane render, unreal engine, digital art, artstation, "
    "illustration, painting, drawing, sketch, anime, cartoon, concept art, "
    "airbrushed, smooth skin, plastic skin, waxy skin, porcelain skin, "
    "flawless skin, beauty retouching, skin smoothing, instagram filter, "
    "glamour shot, magazine cover, professional studio lighting, "
    "dramatic cinematic lighting, rim lighting, volumetric lighting, "
    "perfectly symmetrical face, idealized face, model looks, doll, mannequin, "
    "uncanny valley, oversaturated, hdr, overprocessed, heavy vignette, "
    "shallow depth of field bokeh, tack sharp, hyperdetailed, 8k, ultra hd"
)

# Technical junk. Droppable: lo-fi presets (VHS, CCTV, webcam) genuinely want
# noise, softness and compression artifacts, so they remove these terms.
NEG_QUALITY = (
    "worst quality, low quality, lowres, jpeg artifacts, compression artifacts, "
    "blurry, out of focus, grainy, noise, pixelated, oversharpened"
)

# Never wanted, ever.
NEG_JUNK = (
    "watermark, signature, username, artist name, logo, text overlay, "
    "cropped, out of frame, duplicate, error, frame border"
)


@dataclass
class Preset:
    """A camera/film/era look.

    positive is appended after the user's subject: subject first, style second.
    That ordering matters -- CLIP weights early tokens more heavily, so the
    subject should own the front of the prompt.
    """

    id: str
    label: str
    emoji: str
    blurb: str
    era: str
    positive: str
    negative_add: str = ""
    # Terms removed from NEG_QUALITY, for looks that want technical flaws.
    negative_drop: list[str] = field(default_factory=list)
    steps: int = 30
    guidance: float = 6.0
    sampler: str = "dpmpp_2m_karras"
    aspect: str = "portrait"
    # Multiplied into the hi-res fix denoise strength. Lo-fi looks get less,
    # so the upscale pass doesn't sharpen away the character.
    detail_scale: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PRESETS: list[Preset] = [
    Preset(
        id="p90s",
        label="90s Point & Shoot",
        emoji="📸",
        blurb="Kodak Gold, direct flash, date stamp",
        era="1990s",
        positive=(
            "1990s amateur snapshot, 35mm Kodak Gold 200 film, "
            "point and shoot compact camera, direct on-camera flash, "
            "harsh flash falloff with dark background, slight red eye, "
            "warm color cast, visible film grain, soft focus, "
            "orange date stamp in corner, slightly tilted horizon, "
            "faded scanned photo print"
        ),
        negative_add="modern, smartphone, digital, 4k",
        negative_drop=["grainy", "blurry"],
        steps=30,
        guidance=5.5,
        aspect="landscape",
        detail_scale=0.8,
    ),
    Preset(
        id="p2000s",
        label="2000s Digicam",
        emoji="💾",
        blurb="Early digital compact, harsh flash",
        era="2000s",
        positive=(
            "early 2000s digital compact camera photo, low megapixel, "
            "harsh built-in flash, blown out highlights on skin, "
            "crushed noisy shadows, poor white balance, greenish tint, "
            "limited dynamic range, slight chromatic aberration, "
            "yellow timestamp in corner, casual party snapshot"
        ),
        negative_add="film grain, analog, modern smartphone",
        negative_drop=["lowres", "jpeg artifacts", "noise", "pixelated"],
        steps=28,
        guidance=5.5,
        aspect="digicam",
        detail_scale=0.7,
    ),
    Preset(
        id="polaroid",
        label="Polaroid SX-70",
        emoji="🖼️",
        blurb="Instant film, milky and soft",
        era="1970s–80s",
        positive=(
            "Polaroid SX-70 instant film photograph, square format, "
            "soft low contrast milky image, washed out pastel colors, "
            "warm yellow color shift, soft focus, gentle vignette, "
            "slight light leak, thick white instant film border, "
            "chemical development streaks"
        ),
        negative_add="sharp, high contrast, digital, modern",
        negative_drop=["blurry", "low quality"],
        steps=28,
        guidance=5.0,
        aspect="square",
        detail_scale=0.7,
    ),
    Preset(
        id="portra",
        label="Kodak Portra 400",
        emoji="🎞️",
        blurb="Pro film, natural skin tones",
        era="Timeless",
        positive=(
            "shot on Kodak Portra 400 35mm film, natural muted color palette, "
            "true to life skin tones, soft rolling highlights, "
            "fine organic film grain, natural available window light, "
            "medium format feel, gentle contrast, analog photograph"
        ),
        negative_add="oversaturated, digital clarity",
        negative_drop=["grainy"],
        steps=34,
        guidance=6.0,
        aspect="portrait",
        detail_scale=1.0,
    ),
    Preset(
        id="disposable",
        label="Disposable Camera",
        emoji="🎉",
        blurb="Fujifilm QuickSnap party flash",
        era="1990s–2000s",
        positive=(
            "disposable camera photograph, Fujifilm QuickSnap, "
            "brutal direct flash, heavily blown out foreground, "
            "pitch black background, heavy grain, motion blur, "
            "fingerprint smudge on lens, crooked framing, "
            "high contrast, candid party snapshot, plastic lens softness"
        ),
        negative_add="professional, tripod, studio",
        negative_drop=["blurry", "grainy", "noise", "low quality"],
        steps=26,
        guidance=5.0,
        aspect="landscape",
        detail_scale=0.65,
    ),
    Preset(
        id="vhs",
        label="Camcorder / VHS",
        emoji="📼",
        blurb="Interlaced tape still, scanlines",
        era="1980s–90s",
        positive=(
            "still frame from VHS home video camcorder, interlaced scanlines, "
            "low resolution analog video, color bleeding, chroma smear, "
            "tape tracking distortion, soft blurry image, "
            "blue timestamp overlay in corner, harsh camcorder light, "
            "4:3 aspect, magnetic tape noise"
        ),
        negative_add="film, photograph, sharp, high resolution",
        negative_drop=[
            "lowres",
            "blurry",
            "low quality",
            "worst quality",
            "noise",
            "pixelated",
            "compression artifacts",
        ],
        steps=26,
        guidance=5.0,
        aspect="digicam",
        detail_scale=0.5,
    ),
    Preset(
        id="earlyphone",
        label="Early Smartphone",
        emoji="📱",
        blurb="2010s phone, oversharpened HDR",
        era="2010s",
        positive=(
            "early smartphone photo, small sensor, aggressive oversharpening, "
            "HDR halo artifacts around edges, heavy noise reduction smearing, "
            "luminance noise in shadows, mediocre dynamic range, "
            "casual everyday snapshot, handheld, mixed indoor lighting"
        ),
        negative_add="film, analog, professional camera, DSLR",
        negative_drop=["oversharpened", "noise", "jpeg artifacts"],
        steps=28,
        guidance=5.5,
        aspect="portrait",
        detail_scale=0.8,
    ),
    Preset(
        id="cctv",
        label="Security Camera",
        emoji="🎥",
        blurb="Ceiling CCTV, timestamp burn-in",
        era="Surveillance",
        positive=(
            "security camera CCTV still frame, high ceiling mounted angle "
            "looking down, wide angle barrel distortion, desaturated washed "
            "colors, heavy low light sensor noise, motion blur, "
            "white timestamp burned into corner, fluorescent lighting, "
            "grainy surveillance footage"
        ),
        negative_add="professional photo, portrait, posed, sharp",
        negative_drop=[
            "lowres",
            "blurry",
            "grainy",
            "noise",
            "low quality",
            "worst quality",
        ],
        steps=26,
        guidance=5.0,
        aspect="landscape",
        detail_scale=0.5,
    ),
    Preset(
        id="kodachrome",
        label="Kodachrome 64",
        emoji="🌅",
        blurb="70s slide film, rich and warm",
        era="1960s–70s",
        positive=(
            "shot on Kodachrome 64 slide film, 1970s photograph, "
            "rich saturated reds and warm earth tones, deep contrast, "
            "slight color fade with age, fine grain, "
            "natural midday sunlight, vintage clothing and styling, "
            "scanned slide with subtle dust specks"
        ),
        negative_add="modern, digital, contemporary clothing",
        negative_drop=["grainy"],
        steps=32,
        guidance=6.0,
        aspect="landscape",
        detail_scale=0.9,
    ),
    Preset(
        id="trix",
        label="B&W Tri-X 400",
        emoji="⚫",
        blurb="Grainy monochrome documentary",
        era="Timeless",
        positive=(
            "black and white photograph, shot on Kodak Tri-X 400 film, "
            "monochrome, coarse visible grain, deep blacks and bright "
            "highlights, documentary photojournalism style, "
            "natural available light, 35mm rangefinder, candid street moment"
        ),
        negative_add="color, colorized, saturated",
        negative_drop=["grainy"],
        steps=32,
        guidance=6.0,
        aspect="portrait",
        detail_scale=0.9,
    ),
    Preset(
        id="webcam",
        label="2000s Webcam",
        emoji="💻",
        blurb="Low-res, terrible white balance",
        era="2000s",
        positive=(
            "early 2000s webcam capture, very low resolution, "
            "poor white balance with orange or blue cast, "
            "heavy compression blocking, smeared detail, "
            "dim indoor lighting from a monitor, grainy dark image, "
            "static tripod-less angle"
        ),
        negative_add="professional, sharp, high resolution, DSLR",
        negative_drop=[
            "lowres",
            "blurry",
            "low quality",
            "worst quality",
            "compression artifacts",
            "jpeg artifacts",
            "pixelated",
            "noise",
        ],
        steps=24,
        guidance=5.0,
        aspect="digicam",
        detail_scale=0.5,
    ),
    Preset(
        id="goldenhour",
        label="Golden Hour",
        emoji="🌤️",
        blurb="Natural late light, no flash",
        era="Timeless",
        positive=(
            "natural golden hour sunlight, late afternoon warm light, "
            "soft directional sun, gentle lens flare, "
            "shot on 35mm film, natural skin tones, "
            "relaxed candid moment outdoors, handheld snapshot"
        ),
        negative_add="flash, studio lighting, artificial light",
        steps=32,
        guidance=6.0,
        aspect="portrait",
        detail_scale=1.0,
    ),
    Preset(
        id="flashnight",
        label="Night Flash",
        emoji="🌙",
        blurb="After-dark on-camera flash",
        era="Timeless",
        positive=(
            "night time flash photograph, on-camera flash in darkness, "
            "harsh bright subject against pure black background, "
            "strong falloff, deep shadows, slight motion blur trail, "
            "35mm film grain, candid nightlife snapshot"
        ),
        negative_add="daylight, bright background, studio",
        negative_drop=["grainy"],
        steps=30,
        guidance=5.5,
        aspect="portrait",
        detail_scale=0.85,
    ),
    Preset(
        id="none",
        label="No Style",
        emoji="⚪",
        blurb="Raw prompt, realism scaffolding only",
        era="—",
        positive="realistic photograph, natural lighting",
        steps=30,
        guidance=6.0,
        aspect="portrait",
        detail_scale=1.0,
    ),
]

PRESETS_BY_ID: dict[str, Preset] = {p.id: p for p in PRESETS}
DEFAULT_PRESET_ID = "p90s"


# --------------------------------------------------------------------------
# Aspect ratios
# --------------------------------------------------------------------------
# SD1.5 is trained at 512x512. Push much past ~768 on either axis and it
# starts duplicating heads and torsos, so these stay in the safe envelope.
# All dimensions are multiples of 8 (VAE requirement).

ASPECTS: dict[str, dict[str, Any]] = {
    "portrait": {"label": "Portrait 2:3", "width": 512, "height": 768},
    "landscape": {"label": "Landscape 3:2", "width": 768, "height": 512},
    "square": {"label": "Square 1:1", "width": 512, "height": 512},
    "digicam": {"label": "Classic 4:3", "width": 640, "height": 480},
    "tall": {"label": "Tall 4:5", "width": 512, "height": 640},
    "phone": {"label": "Phone 9:16", "width": 448, "height": 768},
}
DEFAULT_ASPECT = "portrait"


def resolve_dimensions(aspect: str) -> tuple[int, int]:
    spec = ASPECTS.get(aspect) or ASPECTS[DEFAULT_ASPECT]
    return int(spec["width"]), int(spec["height"])


def build_prompt(subject: str, preset: Preset) -> str:
    """Subject first, then style. Empty subjects still produce a valid prompt."""
    subject = (subject or "").strip().rstrip(",")
    parts = [p for p in (subject, BASE_POSITIVE, preset.positive) if p]
    return ", ".join(parts)


def build_negative(preset: Preset, user_negative: str = "") -> str:
    """Assemble the negative prompt, honoring the preset's dropped terms."""
    quality_terms = [t.strip() for t in NEG_QUALITY.split(",")]
    dropped = {d.strip().lower() for d in preset.negative_drop}
    kept = [t for t in quality_terms if t.lower() not in dropped]

    blocks = [NEG_ANATOMY, NEG_AI_LOOK, ", ".join(kept), NEG_JUNK]
    if preset.negative_add:
        blocks.append(preset.negative_add)
    if user_negative and user_negative.strip():
        blocks.append(user_negative.strip())

    return ", ".join(b for b in blocks if b)


def presets_payload() -> list[dict[str, Any]]:
    return [p.to_dict() for p in PRESETS]


def aspects_payload() -> list[dict[str, Any]]:
    return [{"id": key, **value} for key, value in ASPECTS.items()]
