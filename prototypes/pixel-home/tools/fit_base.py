#!/usr/bin/env python3
"""Fit the dominant base edges of a raw AI render and emit a --pts string.

The eye judges a sprite's perspective by its longest visible base structure
(bed frame rail, sofa base box) -- not by the leg tips.  This script keys the
background, takes the bottom silhouette, robustly fits the two base lines
(iteratively discarding legs / drapes hanging below them), intersects them for
the south corner, projects the profile extremes for the west/east corners and
estimates the structure height z from how far the legs drop below the lines.

Usage:
  uv run python tools/fit_base.py --in tools/raw/ai-bed-raw.png --w 2 --d 3
Prints a ready-to-paste --pts argument for make_sprite.py.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

import make_sprite

HX, HZ = 16, 16


def robust_fit(points: list[tuple[int, int]], iters: int = 4, tol: float = 4.0):
    """Least-squares line fit, iteratively discarding points hanging below
    the line (legs, fringes) and far above it (insets)."""
    pts = points
    slope, inter = 0.0, 0.0
    for _ in range(iters):
        n = len(pts)
        if n < 8:
            break
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        num = sum((x - mx) * (y - my) for x, y in pts)
        den = sum((x - mx) ** 2 for x, _ in pts)
        if not den:
            break
        slope, inter = num / den, my - (num / den) * mx
        kept = [(x, y) for x, y in pts
                if -3 * tol < y - (slope * x + inter) < tol]
        if len(kept) == len(pts):
            break
        pts = kept
    return slope, inter, pts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--w", type=int, required=True)
    ap.add_argument("--d", type=int, required=True)
    ap.add_argument("--key", default="magenta")
    ap.add_argument("--trim", type=int, default=10,
                    help="ignore this many columns at each silhouette end")
    args = ap.parse_args()

    img = make_sprite.key_background(Image.open(args.src), args.key)
    alpha = img.split()[3].load()
    w, h = img.size
    prof: dict[int, int] = {}
    for x in range(w):
        for y in range(h - 1, -1, -1):
            if alpha[x, y] >= 128:
                prof[x] = y
                break
    xs = sorted(prof)
    south_y = max(prof.values())
    tip_x = sum(x for x in xs if prof[x] >= south_y - 2) / max(
        1, len([x for x in xs if prof[x] >= south_y - 2]))

    left = [(x, prof[x]) for x in xs if xs[0] + args.trim <= x <= tip_x - 4]
    right = [(x, prof[x]) for x in xs if tip_x + 4 <= x <= xs[-1] - args.trim]
    sl, il, kept_l = robust_fit(left)
    sr, ir, kept_r = robust_fit(right)

    # south corner = intersection of the two base lines
    sx = (ir - il) / (sl - sr)
    sy = sl * sx + il
    # west/east corners: ends of the inlier spans (drapes/legs were discarded,
    # so these sit on the actual base structure, not on overhanging cloth)
    wx = min(x for x, _ in kept_l) if kept_l else xs[0]
    wy = sl * wx + il
    ex = max(x for x, _ in kept_r) if kept_r else xs[-1]
    ey = sr * ex + ir
    # leg drop: deepest profile point below its line -> structure height
    drop = 0.0
    for x, y in left + right:
        line_y = (sl * x + il) if x <= tip_x else (sr * x + ir)
        drop = max(drop, y - line_y)
    diamond_raw = ex - wx
    scale = (args.w + args.d) * HX / diamond_raw
    z = round(drop * scale / HZ, 2)

    print(f"left slope {sl:+.3f} ({len(kept_l)}/{len(left)} pts), "
          f"right slope {sr:+.3f} ({len(kept_r)}/{len(right)} pts)")
    print(f"corners W({wx:.0f},{wy:.0f}) S({sx:.0f},{sy:.0f}) E({ex:.0f},{ey:.0f})")
    print(f"leg drop {drop:.0f}px raw -> z={z} (scale {scale:.3f})")
    print(f'--pts "0,{args.d},{z}:{wx:.0f},{wy:.0f} '
          f'{args.w},{args.d},{z}:{sx:.0f},{sy:.0f} '
          f'{args.w},0,{z}:{ex:.0f},{ey:.0f}"')


if __name__ == "__main__":
    main()
