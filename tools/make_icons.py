"""Generate the pokeflip home-screen icons.

Pillow is not a dependency and these never change, so the PNGs are drawn once
here and committed. Kept in the repo so the mark can be regenerated rather than
reverse-engineered.
"""
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "pokeflip" / "web"
ACCENT = (0x2A, 0x78, 0xD6)
DEEP = (0x1B, 0x53, 0x99)
INK = (0xFF, 0xFF, 0xFF)
SS = 4  # supersample factor


def png(path, size, pixels):
    raw = b"".join(b"\x00" + bytes(row) for row in pixels)
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))
    head = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", head)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def coverage(size, inside, samples=SS):
    """Per-pixel coverage of a predicate, supersampled for smooth edges."""
    grid = []
    step = 1.0 / samples
    offs = [(i + 0.5) * step for i in range(samples)]
    for y in range(size):
        row = []
        for x in range(size):
            hits = sum(1 for dy in offs for dx in offs if inside(x + dx, y + dy))
            row.append(hits / (samples * samples))
        grid.append(row)
    return grid


def build(size, squircle=True, mark_scale=1.0):
    n = float(size)
    c = n / 2
    radius = n * 0.225          # rounded-square corner
    pad = 0.0 if not squircle else 0.0

    def in_bg(x, y):
        if not squircle:
            return True
        # rounded rectangle covering the whole canvas
        lo, hi = pad, n - pad
        cx = min(max(x, lo + radius), hi - radius)
        cy = min(max(y, lo + radius), hi - radius)
        return (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2 + 1e-9

    # The mark: a diamond ring with a solid diamond core - the same glyph the
    # dashboard uses, drawn rather than set in a font so it renders identically
    # on a home screen that has no idea what a font is.
    outer, inner = n * 0.31 * mark_scale, n * 0.205 * mark_scale
    core = n * 0.085 * mark_scale

    def diamond(x, y, r):
        return abs(x - c) + abs(y - c) <= r

    def in_ring(x, y):
        return diamond(x, y, outer) and not diamond(x, y, inner)

    def in_core(x, y):
        return diamond(x, y, core)

    bg = coverage(size, in_bg)
    ring = coverage(size, in_ring)
    coreg = coverage(size, in_core)

    pixels = []
    for y in range(size):
        row = []
        for x in range(size):
            a = bg[y][x]
            # vertical gradient on the tile so it does not read as a flat blob
            t = y / max(1.0, n - 1)
            base = tuple(round(ACCENT[i] + (DEEP[i] - ACCENT[i]) * t) for i in range(3))
            mark = min(1.0, ring[y][x] + coreg[y][x])
            rgb = tuple(round(base[i] + (INK[i] - base[i]) * mark) for i in range(3))
            row.extend([rgb[0], rgb[1], rgb[2], round(a * 255)])
        pixels.append(row)
    return pixels


for size, name, squircle, scale in [
    (180, "icon-180.png", True, 1.0),     # apple-touch-icon
    (192, "icon-192.png", True, 1.0),
    (512, "icon-512.png", True, 1.0),
    # Android crops a maskable icon to whatever shape it likes, so the tile
    # fills the canvas and the mark stays inside the 80% safe zone.
    (512, "icon-maskable-512.png", False, 0.72),
]:
    png(OUT / name, size, build(size, squircle, scale))
    print("wrote", name)
