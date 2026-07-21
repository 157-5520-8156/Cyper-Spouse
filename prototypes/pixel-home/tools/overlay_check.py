#!/usr/bin/env python3
"""Overlay the ideal iso wireframe (from manifest w/d/h + offset) on each sprite.

Any global rotation / perspective skew shows up immediately as the art
diverging from the red wireframe. Writes tools/raw/overlay-sheet.png.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import make_sprite

HX, HY, HZ = make_sprite.HX, make_sprite.HY, make_sprite.HZ
ZOOM = 3

ROOT = Path(__file__).resolve().parent.parent
AI = ROOT / "assets" / "ai"


def wireframe(draw: ImageDraw.ImageDraw, meta: dict, zoom: int) -> None:
    w, d, h = meta["w"], meta["d"], meta.get("h", 1.0)
    off = meta["offset"]
    n = (-off[0], -off[1])
    e = (n[0] + w * HX, n[1] + w * HY)
    s = (n[0] + (w - d) * HX, n[1] + (w + d) * HY)
    wpt = (n[0] - d * HX, n[1] + d * HY)
    base = [n, e, s, wpt]
    top = [(x, y - h * HZ) for x, y in base]

    def L(a, b, color):
        draw.line([a[0] * zoom, a[1] * zoom, b[0] * zoom, b[1] * zoom], fill=color, width=2)

    red = (255, 60, 60, 230)
    cyan = (60, 220, 255, 230)
    for i in range(4):
        L(base[i], base[(i + 1) % 4], red)
        L(top[i], top[(i + 1) % 4], cyan)
    for i in range(4):
        L(base[i], top[i], (255, 220, 80, 200))


def main() -> None:
    manifest = json.loads((AI / "manifest.json").read_text())
    cells = []
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 22)
    except OSError:
        font = ImageFont.load_default()
    for name, meta in manifest.items():
        img = Image.open(AI / Path(meta["url"]).name).convert("RGBA")
        # pad so the wireframe (which can exceed the crop) stays visible
        pad = 24
        cw, ch = img.width + pad * 2, img.height + pad * 2
        cell = Image.new("RGBA", (cw * ZOOM, ch * ZOOM + 34), (44, 40, 62, 255))
        up = img.resize((img.width * ZOOM, img.height * ZOOM), Image.NEAREST)
        cell.alpha_composite(up, (pad * ZOOM, pad * ZOOM))
        draw = ImageDraw.Draw(cell)
        meta_shift = dict(meta)
        meta_shift["offset"] = [meta["offset"][0] - pad, meta["offset"][1] - pad]
        wireframe(draw, meta_shift, ZOOM)
        draw.text((8, ch * ZOOM + 4), name, fill=(240, 236, 255, 255), font=font)
        cells.append(cell)

    cols = 4
    rows = math.ceil(len(cells) / cols)
    W = max(sum(c.width for c in cells[r * cols:(r + 1) * cols]) for r in range(rows)) + 20
    H = sum(max(c.height for c in cells[r * cols:(r + 1) * cols]) for r in range(rows)) + 20
    sheet = Image.new("RGBA", (W, H), (30, 27, 45, 255))
    y = 10
    for r in range(rows):
        row = cells[r * cols:(r + 1) * cols]
        x = 10
        for c in row:
            sheet.alpha_composite(c, (x, y))
            x += c.width
        y += max(c.height for c in row)
    out = ROOT / "tools" / "raw" / "overlay-sheet.png"
    sheet.save(out)
    print("wrote", out, sheet.size)


if __name__ == "__main__":
    main()
