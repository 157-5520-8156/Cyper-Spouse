#!/usr/bin/env python3
"""Overlay a labeled grid (and optional crosshair marks) on a raw render so
footprint corners can be located by eye.

Usage:
  uv run python tools/annotate.py --in raw/bed.png [--grid 64] [--marks "x,y x,y ..."] [--out preview.png]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw

GRID_COLOR = (0, 255, 128, 160)
LABEL_COLOR = (255, 255, 255, 255)
MARK_COLOR = (255, 40, 40, 255)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--grid", type=int, default=64)
    ap.add_argument("--marks", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    src = Path(args.src)
    img = Image.open(src).convert("RGBA")
    draw = ImageDraw.Draw(img)
    step = args.grid
    for x in range(0, img.width, step):
        draw.line([(x, 0), (x, img.height)], fill=GRID_COLOR, width=1)
        draw.text((x + 2, 2), str(x), fill=LABEL_COLOR)
    for y in range(0, img.height, step):
        draw.line([(0, y), (img.width, y)], fill=GRID_COLOR, width=1)
        draw.text((2, y + 2), str(y), fill=LABEL_COLOR)

    for token in args.marks.split():
        x, y = (int(v) for v in token.split(","))
        draw.line([(x - 18, y), (x + 18, y)], fill=MARK_COLOR, width=3)
        draw.line([(x, y - 18), (x, y + 18)], fill=MARK_COLOR, width=3)

    out = Path(args.out) if args.out else src.with_name(src.stem + "-annotated.png")
    img.save(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
