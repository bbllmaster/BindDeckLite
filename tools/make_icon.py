#!/usr/bin/env python3
"""Generate host/assets/app.ico (multi-size, 32-bit) with the stdlib only.

The icon is a teal tile with three dark key caps — the same motif the tray
icon falls back to, so the exe, the taskbar and the tray all match.

    python tools/make_icon.py [output.ico]
"""

from __future__ import annotations

import os
import struct
import sys

SIZES = (16, 20, 24, 32, 48, 64, 128, 256)
SCALE = 4  # supersampling factor

BG = (53, 224, 194)
FG = (9, 24, 32)


def _inside_round_rect(x: float, y: float, x0: float, y0: float,
                       x1: float, y1: float, r: float) -> bool:
    if x < x0 or x > x1 or y < y0 or y > y1:
        return False
    if r <= 0:
        return True
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    dx = x - cx
    dy = y - cy
    return dx * dx + dy * dy <= r * r


def render(size: int):
    """Return RGBA rows (top-down) for one icon size."""
    big = size * SCALE
    pixels = [[(0, 0, 0, 0)] * big for _ in range(big)]
    b = float(big)
    tile = (0.0, 0.0, b, b, b * 0.20)
    keys = (
        (b * 0.20, b * 0.20, b * 0.80, b * 0.40, b * 0.055),
        (b * 0.20, b * 0.46, b * 0.48, b * 0.80, b * 0.055),
        (b * 0.52, b * 0.46, b * 0.80, b * 0.80, b * 0.055),
    )
    for y in range(big):
        py = y + 0.5
        row = pixels[y]
        for x in range(big):
            px = x + 0.5
            if not _inside_round_rect(px, py, *tile):
                continue
            color = BG
            for key in keys:
                if _inside_round_rect(px, py, *key):
                    color = FG
                    break
            row[x] = (color[0], color[1], color[2], 255)

    # Box-filter down to the target size.
    out = []
    for y in range(size):
        line = []
        for x in range(size):
            r = g = bb = a = 0
            for sy in range(SCALE):
                for sx in range(SCALE):
                    p = pixels[y * SCALE + sy][x * SCALE + sx]
                    r += p[0] * p[3]
                    g += p[1] * p[3]
                    bb += p[2] * p[3]
                    a += p[3]
            n = SCALE * SCALE
            if a:
                line.append((r // a, g // a, bb // a, a // n))
            else:
                line.append((0, 0, 0, 0))
        out.append(line)
    return out


def _dib(rows, size: int) -> bytes:
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    body = bytearray()
    for y in range(size - 1, -1, -1):  # bottom-up
        for (r, g, b, a) in rows[y]:
            body += bytes((b, g, r, a))
    mask_row = ((size + 31) // 32) * 4
    body += b"\x00" * (mask_row * size)
    return header + bytes(body)


def main(argv: list[str]) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = argv[1] if len(argv) > 1 else os.path.join(here, "host", "assets", "app.ico")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    images = []
    for size in SIZES:
        data = _dib(render(size), size)
        images.append((size, data))

    offset = 6 + 16 * len(images)
    directory = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    payload = b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,
            0 if size >= 256 else size,
            0, 0, 1, 32, len(data), offset + len(payload),
        )
        payload += data

    with open(out, "wb") as f:
        f.write(directory + entries + payload)
    print(f"wrote {out} ({os.path.getsize(out)} bytes, {len(images)} sizes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
