#!/usr/bin/env python3
"""
Generate the PWA icon PNGs.

Written as a direct rasteriser rather than a headless-browser screenshot: the
browser route kept clipping the viewport, and this has no external dependency
(stdlib zlib only), always produces exact dimensions, and is reproducible.

Usage:  python3 build/make-icons.py
Writes: icons/icon-192.png, icons/icon-512.png, icons/icon-maskable-512.png
"""

import os
import struct
import zlib

BG = (20, 20, 22, 255)  # --bg   #141416
AMBER = (232, 163, 61, 255)  # --accent #e8a33d
INK = (242, 242, 244, 255)  # --ink  #f2f2f4

SAMPLES = 3  # per axis; 9 samples per pixel


def rounded_rect(px, py, x0, y0, x1, y1, r):
    """Inside test for an axis-aligned rounded rectangle."""
    if px < x0 or px > x1 or py < y0 or py > y1:
        return False
    qx = max(x0 + r - px, px - (x1 - r), 0.0)
    qy = max(y0 + r - py, py - (y1 - r), 0.0)
    return qx * qx + qy * qy <= r * r


def circle(px, py, cx, cy, r):
    dx, dy = px - cx, py - cy
    return dx * dx + dy * dy <= r * r


def annulus(px, py, cx, cy, r_inner, r_outer):
    dx, dy = px - cx, py - cy
    d2 = dx * dx + dy * dy
    return r_inner * r_inner <= d2 <= r_outer * r_outer


def glyph_layers():
    """
    Shapes in the 120x96 glyph coordinate space, painted in order.
    Mirrors the SVG used elsewhere: film frame, sprocket holes, camera lens.
    """
    layers = []

    # Film frame: rect(6,6,108x84,rx9) with a 6-unit centred stroke.
    def frame(px, py):
        outer = rounded_rect(px, py, 3, 3, 117, 93, 12)
        inner = rounded_rect(px, py, 9, 9, 111, 87, 6)
        return outer and not inner

    layers.append((frame, AMBER))

    # Sprocket holes down both edges.
    holes = []
    for y in (14, 31, 48, 65):
        holes.append((13, y, 21, y + 9))
        holes.append((99, y, 107, y + 9))

    def sprockets(px, py):
        return any(rounded_rect(px, py, x0, y0, x1, y1, 2) for x0, y0, x1, y1 in holes)

    layers.append((sprockets, AMBER))

    # Lens: white ring, amber aperture, small highlight.
    layers.append((lambda px, py: annulus(px, py, 60, 48, 20, 26), INK))
    layers.append((lambda px, py: circle(px, py, 60, 48, 10), AMBER))
    layers.append((lambda px, py: circle(px, py, 51, 39, 3.5), INK))

    return layers


def blend(dst, src, coverage):
    """Source-over compositing of a solid colour at fractional coverage."""
    sa = (src[3] / 255.0) * coverage
    if sa <= 0:
        return dst
    da = dst[3] / 255.0
    out_a = sa + da * (1 - sa)
    if out_a <= 0:
        return (0, 0, 0, 0)
    out = []
    for i in range(3):
        c = (src[i] * sa + dst[i] * da * (1 - sa)) / out_a
        out.append(int(round(c)))
    return (out[0], out[1], out[2], int(round(out_a * 255)))


def render(size, maskable):
    plate_radius = 0.0 if maskable else size * 0.1875
    glyph_frac = 0.54 if maskable else 0.70

    # Map the 120x96 glyph box into a centred region of the plate.
    gw = size * glyph_frac
    gh = gw * 96.0 / 120.0
    gx = (size - gw) / 2.0
    gy = (size - gh) / 2.0
    scale = 120.0 / gw

    layers = glyph_layers()
    step = 1.0 / SAMPLES
    total = SAMPLES * SAMPLES

    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            pixel = (0, 0, 0, 0)

            # Plate coverage.
            hits = 0
            for sy in range(SAMPLES):
                py = y + (sy + 0.5) * step
                for sx in range(SAMPLES):
                    px = x + (sx + 0.5) * step
                    if maskable or rounded_rect(
                        px, py, 0, 0, size, size, plate_radius
                    ):
                        hits += 1
            if hits:
                pixel = blend(pixel, BG, hits / total)

            # Glyph layers, only for pixels inside the glyph box.
            if gx - 1 <= x <= gx + gw + 1 and gy - 1 <= y <= gy + gh + 1:
                for shape, colour in layers:
                    hits = 0
                    for sy in range(SAMPLES):
                        py = (y + (sy + 0.5) * step - gy) * scale
                        for sx in range(SAMPLES):
                            px = (x + (sx + 0.5) * step - gx) * scale
                            if shape(px, py):
                                hits += 1
                    if hits:
                        pixel = blend(pixel, colour, hits / total)

            row += bytes(pixel)
        rows.append(bytes(row))
    return rows


def write_png(path, size, rows):
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    with open(path, "wb") as handle:
        handle.write(png)


def main():
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    out = os.path.join(root, "icons")
    os.makedirs(out, exist_ok=True)

    targets = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
    ]
    for name, size, maskable in targets:
        write_png(os.path.join(out, name), size, render(size, maskable))
        print(f"wrote icons/{name} ({size}x{size})")


if __name__ == "__main__":
    main()
