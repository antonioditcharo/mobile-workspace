"""Region detail pass: re-render faces and hands at full resolution.

The core problem this solves: in a 512x768 frame a face might occupy 90x110
pixels, which in latent space is barely 11x14. There simply aren't enough
latents there to resolve an eye, so faces come out smeared and hands come out
as claws -- the single most reliable tell that an image was generated.

The fix is to crop the region, scale it up so it fills a full 512px canvas,
re-render *just that crop* with img2img at low denoise, then composite it back
through a feathered mask. The face now gets the model's full attention at
proper resolution while the rest of the frame is untouched.

Geometry lives here, separate from any model code, so it can be tested without
a GPU -- coordinate and paste-alignment bugs are the likely failure mode and
they're invisible until you look at a picture.
"""

from __future__ import annotations

import logging
from typing import Callable

from .detect import Region

log = logging.getLogger("localgen.refine")

# Prompt fragments prepended when re-rendering a region. Kept deliberately
# plain: the crop should inherit the era styling from the original prompt, and
# adding "beautiful detailed face" here would drag it straight back toward the
# glossy render look the presets work to avoid.
REGION_PROMPTS: dict[str, str] = {
    "face": "a face in sharp focus, natural skin texture, visible pores",
    "hand": "a hand, five fingers, correct anatomy, natural pose",
}


def expand_box(
    box: tuple[int, int, int, int],
    image_size: tuple[int, int],
    padding: float,
) -> tuple[int, int, int, int]:
    """Grow a detection box by `padding` (fraction of its size), clamped.

    Context matters: handing the model a box cropped tight to the jawline gives
    it nothing to anchor the head against, and the re-render drifts. Roughly a
    third again on each side works well.
    """
    x1, y1, x2, y2 = box
    width, height = image_size
    box_w, box_h = x2 - x1, y2 - y1

    dx = box_w * padding
    dy = box_h * padding

    nx1 = max(0, int(round(x1 - dx)))
    ny1 = max(0, int(round(y1 - dy)))
    nx2 = min(width, int(round(x2 + dx)))
    ny2 = min(height, int(round(y2 + dy)))

    return nx1, ny1, nx2, ny2


def work_size(
    box_size: tuple[int, int], max_side: int = 512, min_side: int = 256
) -> tuple[int, int]:
    """Resolution to re-render the crop at: longest side to max_side, /8 aligned.

    Aspect ratio is preserved -- squashing a tall crop into a square makes the
    model render a squashed face, which then gets stretched back on paste.
    """
    box_w, box_h = box_size
    if box_w <= 0 or box_h <= 0:
        return min_side, min_side

    scale = max_side / max(box_w, box_h)
    target_w = max(min_side, int(round(box_w * scale)))
    target_h = max(min_side, int(round(box_h * scale)))

    # VAE requires multiples of 8.
    return (target_w // 8) * 8 or min_side, (target_h // 8) * 8 or min_side


def feather_mask(
    size: tuple[int, int],
    feather: float = 0.25,
    flush: tuple[bool, bool, bool, bool] = (False, False, False, False),
):
    """Soft-edged paste mask, so the re-rendered patch has no visible seam.

    `flush` marks sides (left, top, right, bottom) that sit against the image
    border. Those are NOT feathered: there is no surrounding content to blend
    into there, and fading them out would leave a face at the frame edge with
    its outer edge un-refined. The gradient is pushed off-canvas instead so the
    mask stays fully opaque right up to the boundary.
    """
    from PIL import Image, ImageDraw, ImageFilter

    width, height = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)

    inset = int(min(width, height) * feather * 0.5)
    inset = max(1, min(inset, min(width, height) // 2 - 1))
    blur = max(1.0, min(width, height) * feather * 0.25)

    # Far enough outside that the blur gradient never reaches back into frame.
    outside = int(blur * 3) + inset
    left, top, right, bottom = flush

    draw.rectangle(
        [
            -outside if left else inset,
            -outside if top else inset,
            width + outside if right else width - inset - 1,
            height + outside if bottom else height - inset - 1,
        ],
        fill=255,
    )

    return mask.filter(ImageFilter.GaussianBlur(blur))


def refine_regions(
    image,
    regions: list[Region],
    render: Callable,
    *,
    padding: float = 0.35,
    feather: float = 0.25,
    max_side: int = 512,
    max_regions: int = 4,
    on_region: Callable[[int, int, str], None] | None = None,
):
    """Re-render each region and composite it back.

    `render(crop, kind, size)` returns a re-rendered image for that crop. A
    failure on one region is logged and skipped rather than losing the whole
    image -- a slightly soft hand beats no picture.
    """
    from PIL import Image

    if not regions:
        return image, 0

    result = image.copy()
    targets = regions[:max_regions]
    done = 0

    for index, region in enumerate(targets):
        box = expand_box(region.box, (result.width, result.height), padding)
        x1, y1, x2, y2 = box
        box_w, box_h = x2 - x1, y2 - y1
        if box_w < 24 or box_h < 24:
            continue

        if on_region:
            on_region(index, len(targets), region.kind)

        target_size = work_size((box_w, box_h), max_side=max_side)

        try:
            crop = result.crop(box).resize(target_size, Image.LANCZOS)
            rendered = render(crop, region.kind, target_size)
            if rendered is None:
                continue

            patch = rendered.resize((box_w, box_h), Image.LANCZOS)
            mask = feather_mask(
                (box_w, box_h),
                feather,
                flush=(x1 <= 0, y1 <= 0, x2 >= result.width, y2 >= result.height),
            )
            result.paste(patch, (x1, y1), mask)
            done += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("region refine failed (%s): %s", region.kind, exc)
            continue

    return result, done
