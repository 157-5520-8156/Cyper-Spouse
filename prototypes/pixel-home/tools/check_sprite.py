#!/usr/bin/env python3
"""Measure how well a processed sprite's base matches the exact iso contract.

For each sprite listed in the manifest:
  - locate the south tip (lowest opaque pixel, ties -> centroid)
  - trace the bottom silhouette (lowest opaque pixel per column)
  - fit the left-base and right-base edge slopes on the spans adjacent to the
    south tip (expected: +0.5 / -0.5 screen slope)
  - compare the opaque footprint width against the diamond (w+d)*HX
  - report where the north anchor lands vs the manifest offset

Usage: uv run python tools/check_sprite.py [--dir assets/ai]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image

HX, HY = 16, 8


def bottom_profile(img: Image.Image) -> dict[int, int]:
    alpha = img.split()[3].load()
    w, h = img.size
    prof: dict[int, int] = {}
    for x in range(w):
        for y in range(h - 1, -1, -1):
            if alpha[x, y] >= 128:
                prof[x] = y
                break
    return prof


def fit_slope(xs: list[int], ys: list[int]) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den else float("nan")


def check(name: str, meta: dict, path: Path) -> None:
    img = Image.open(path).convert("RGBA")
    prof = bottom_profile(img)
    if not prof:
        print(f"{name}: EMPTY sprite")
        return
    w_tiles, d_tiles = meta["w"], meta["d"]
    south_y = max(prof.values())
    south_xs = [x for x, y in prof.items() if y >= south_y - 1]
    south_x = sum(south_xs) / len(south_xs)

    # sample the base edges on each side of the south tip, skipping the
    # rounded tip itself and stopping before overhangs (legs gap etc.)
    left_span = max(6, min(int(d_tiles * HX * 0.6), int(south_x) - min(prof)))
    right_span = max(6, min(int(w_tiles * HX * 0.6), max(prof) - int(south_x)))
    lx = [x for x in prof if south_x - left_span <= x <= south_x - 3]
    rx = [x for x in prof if south_x + 3 <= x <= south_x + right_span]
    slope_l = fit_slope(lx, [prof[x] for x in lx])       # expected +0.5
    slope_r = fit_slope(rx, [prof[x] for x in rx])       # expected -0.5

    ow, oh = img.size
    expected_w = (w_tiles + d_tiles) * HX
    off = meta["offset"]
    # where the south corner should be given the manifest anchor
    north = (-off[0], -off[1])
    expect_south = (north[0] + (w_tiles - d_tiles) * HX, north[1] + (w_tiles + d_tiles) * HY)

    def fmt(v):
        return "nan" if v != v else f"{v:+.3f}"

    print(f"{name}: {ow}x{oh}  footprint {w_tiles}x{d_tiles}")
    print(f"  base slopes  left {fmt(slope_l)} (want +0.500)   right {fmt(slope_r)} (want -0.500)")
    print(f"  opaque width {ow}px vs diamond {expected_w}px  ({ow - expected_w:+d}px overhang)")
    print(f"  south tip at ({south_x:.1f}, {south_y})  expected ({expect_south[0]}, {expect_south[1]})"
          f"  delta ({south_x - expect_south[0]:+.1f}, {south_y - expect_south[1]:+d})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent / "assets" / "ai"))
    args = ap.parse_args()
    root = Path(args.dir)
    manifest = json.loads((root / "manifest.json").read_text())
    for name, meta in manifest.items():
        check(name, meta, root / Path(meta["url"]).name)


if __name__ == "__main__":
    main()
