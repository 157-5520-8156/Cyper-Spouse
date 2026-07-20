#!/usr/bin/env python3
"""Convert an AI-generated furniture render into an engine-ready pixel sprite.

Pipeline: chroma-key removal -> content crop -> nearest-neighbour downscale to
the footprint's native pixel box -> fixed-palette quantization -> anchor
computation -> write PNG + update manifest.json.

The engine's sprite frame convention: the offset anchors the *north corner*
of the w×d footprint.  We assume the render fills its footprint: after
scaling to the diamond width (w+d)*HX, the bottom tip of the content is the
south corner at z=0, so  offset = [-(d*HX), -(H - (w+d)*HY)].

Usage:
  uv run python tools/make_sprite.py --in raw/bed.png --type bed --w 2 --d 3 \
      [--pad-bottom N] [--key auto|magenta] [--out-dir assets/ai]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from PIL import Image

HX, HY, HZ = 16, 8, 16

# Mirror of the engine palette (pixel.js PAL) plus the shade() ramp used by
# the procedural bakes, so AI sprites land on the same colors.
PAL = {
    "outline": "#2a1c2e",
    "floorA": "#b07d4e", "floorB": "#a5714a", "floorSeam": "#7e5233", "floorLite": "#c99a64",
    "kitchenA": "#c9a87c", "kitchenB": "#bd9a70",
    "wall": "#e3d3b4", "wallLow": "#cdb894", "wallTrim": "#9c7350",
    "wood": "#96603a", "woodDark": "#6d4128", "woodLite": "#b97f4e", "woodDeep": "#54301f",
    "cream": "#efe3c8", "linen": "#f0e6cf",
    "teal": "#4e8d80", "tealDark": "#38685f",
    "sage": "#7ea183", "sageDark": "#5c7a62",
    "rose": "#c47a6d", "roseDark": "#9c5751", "blush": "#e0a793",
    "gold": "#e3b263", "navy": "#4a5f82",
    "leaf": "#4c8552", "leafLite": "#6fae67", "leafDark": "#33633f", "potClay": "#a9603f",
    "skin": "#f6d3ae", "skinDark": "#dda57e",
    "hair": "#8a5638", "hairLite": "#ab7449", "hairDark": "#663d26",
    "white": "#f7efdb", "paper": "#f6e9a8", "metal": "#8f9ba2", "metalLite": "#b9c4ca",
    "screen": "#8fd0c6", "dark": "#3c4552",
}
SHADE_FACTORS = (0.56, 0.66, 0.78, 0.88, 1.0, 1.1, 1.2)


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def shade(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    if factor <= 1:
        return tuple(min(255, max(0, round(c * factor))) for c in rgb)
    t = factor - 1
    return tuple(min(255, max(0, round(c + (255 - c) * t))) for c in rgb)


def build_palette() -> list[tuple[int, int, int]]:
    colors: set[tuple[int, int, int]] = set()
    for value in PAL.values():
        base = hex_to_rgb(value)
        for factor in SHADE_FACTORS:
            colors.add(shade(base, factor))
    return sorted(colors)


import colorsys


def key_background(img: Image.Image, mode: str) -> Image.Image:
    """Remove the chroma background, anti-aliased fringe and key-tinted
    ground shadows (any strongly magenta-hued pixel goes transparent)."""
    img = img.convert("RGBA")
    px = img.load()
    w, h = img.size
    if mode == "auto":
        corners = [px[0, 0], px[w - 1, 0], px[0, h - 1], px[w - 1, h - 1]]
        key = max(set(c[:3] for c in corners), key=lambda c: sum(1 for cc in corners if cc[:3] == c))
    else:
        key = (255, 0, 255)
    kr, kg, kb = key
    key_hue = colorsys.rgb_to_hsv(kr / 255, kg / 255, kb / 255)[0]

    def dist2(c):
        return (c[0] - kr) ** 2 + (c[1] - kg) ** 2 + (c[2] - kb) ** 2

    for y in range(h):
        for x in range(w):
            c = px[x, y]
            if dist2(c) < 5200:
                px[x, y] = (0, 0, 0, 0)
                continue
            hue, sat, _val = colorsys.rgb_to_hsv(c[0] / 255, c[1] / 255, c[2] / 255)
            hue_d = min(abs(hue - key_hue), 1 - abs(hue - key_hue))
            # key-hued at any brightness = background artifact (fringe/shadow)
            if hue_d < 0.07 and sat > 0.22:
                px[x, y] = (0, 0, 0, 0)
    return img


def content_bbox(img: Image.Image) -> tuple[int, int, int, int]:
    alpha = img.split()[3]
    bbox = alpha.getbbox()
    if not bbox:
        raise SystemExit("no opaque content found after keying")
    return bbox


def quantize_to_palette(img: Image.Image, palette: list[tuple[int, int, int]]) -> Image.Image:
    pal_img = Image.new("P", (1, 1))
    flat: list[int] = []
    for color in palette[:256]:
        flat.extend(color)
    flat.extend([0] * (768 - len(flat)))
    pal_img.putpalette(flat)
    rgb = img.convert("RGB").quantize(palette=pal_img, dither=Image.Dither.NONE).convert("RGBA")
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(rgb, (0, 0))
    # restore hard alpha
    alpha = img.split()[3].point(lambda a: 255 if a >= 128 else 0)
    out.putalpha(alpha)
    return out


OVERSAMPLE = 4          # rectify at 4x final scale, then nearest-downscale
MARGIN = 256            # canonical canvas padding for overhanging parts


def parse_pts(spec: str) -> list[tuple[float, float, float, float, float]]:
    """'gx,gy[,z]:px,py ...' -> [(gx, gy, z, px, py), ...]

    z (height above ground, in units of HZ) lets you annotate corners of the
    dominant visible base structure (e.g. a bed frame's bottom rail) instead
    of guessing occluded ground-contact points -- the visible edges then map
    to exactly 2:1 and the legs land on the ground automatically."""
    out = []
    for token in spec.split():
        grid, pix = token.split(":")
        parts = [float(v) for v in grid.split(",")]
        gx, gy = parts[0], parts[1]
        gz = parts[2] if len(parts) > 2 else 0.0
        px, py = (float(v) for v in pix.split(","))
        out.append((gx, gy, gz, px, py))
    if len(out) != 3:
        raise SystemExit("--pts needs exactly three gx,gy[,z]:px,py corners")
    return out


def solve3(m: list[list[float]], rhs: list[float]) -> list[float]:
    """Cramer's rule for a 3x3 system."""
    def det(a):
        return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    base = det(m)
    if abs(base) < 1e-9:
        raise SystemExit("--pts corners are collinear; pick three spread-out corners")
    out = []
    for col in range(3):
        mm = [row[:] for row in m]
        for r in range(3):
            mm[r][col] = rhs[r]
        out.append(det(mm) / base)
    return out


def rectify(img: Image.Image, pts, w: int, d: int) -> tuple[Image.Image, tuple[int, int]]:
    """Affine-warp the render so its ground plane matches the exact iso basis
    at OVERSAMPLE x the final scale.  Returns (canonical image, north corner)."""
    hx, hy = HX * OVERSAMPLE, HY * OVERSAMPLE
    hz = HZ * OVERSAMPLE
    canvas_w = round((w + d) * hx) + MARGIN * 2
    canvas_h = round((w + d) * hy) + MARGIN * 2 + 6 * HZ * OVERSAMPLE
    north = (MARGIN + round(d * hx), canvas_h - MARGIN - round((w + d) * hy))

    def target(gx, gy, gz=0.0):
        return (north[0] + (gx - gy) * hx, north[1] + (gx + gy) * hy - gz * hz)

    src_mat = [[px, py, 1.0] for _, _, _, px, py in pts]
    dst = [target(gx, gy, gz) for gx, gy, gz, _, _ in pts]
    ax = solve3(src_mat, [p[0] for p in dst])       # forward affine, x row
    ay = solve3(src_mat, [p[1] for p in dst])       # forward affine, y row
    # invert [[a,b,c],[d,e,f],[0,0,1]] for PIL (output->input mapping)
    a, b, c = ax
    dd, e, f = ay
    det = a * e - b * dd
    if abs(det) < 1e-12:
        raise SystemExit("degenerate affine from --pts")
    ia, ib = e / det, -b / det
    idd, ie = -dd / det, a / det
    ic = -(ia * c + ib * f)
    if_ = -(idd * c + ie * f)
    coeffs = (ia, ib, ic, idd, ie, if_)
    warped = img.transform((canvas_w, canvas_h), Image.Transform.AFFINE, coeffs,
                           resample=Image.Resampling.BICUBIC, fillcolor=(0, 0, 0, 0))
    return warped, north


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--type", dest="ftype", required=True)
    ap.add_argument("--w", type=float, required=True,
                    help="footprint width in tiles (fractional ok: the engine keeps "
                         "its own integer collision box; this only shapes the sprite)")
    ap.add_argument("--d", type=float, required=True, help="footprint depth in tiles")
    ap.add_argument("--h", type=float, default=1.0, help="visual height in units (manifest only)")
    ap.add_argument("--key", default="auto", choices=["auto", "magenta"])
    ap.add_argument("--pts", default="",
                    help="three reference corners 'gx,gy[,z]:px,py' x3 in raw pixels "
                         "(z = height in HZ units); enables exact iso rectification "
                         "+ anchoring")
    ap.add_argument("--pad-bottom", type=int, default=0,
                    help="(legacy path) extra px below the footprint south corner")
    ap.add_argument("--width-scale", type=float, default=1.0,
                    help="(legacy path) content width relative to the diamond width")
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "assets" / "ai"))
    args = ap.parse_args()

    src = Path(args.src)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    img = key_background(Image.open(src), args.key)

    if args.pts:
        pts = parse_pts(args.pts)
        warped, north = rectify(img, pts, args.w, args.d)
        bbox = content_bbox(warped)
        # snap the crop so the north corner stays on the downscale lattice
        cx = bbox[0] - ((north[0] - bbox[0]) % OVERSAMPLE)
        cy = bbox[1] - ((north[1] - bbox[1]) % OVERSAMPLE)
        cropped = warped.crop((cx, cy, bbox[2], bbox[3]))
        target_w = max(8, cropped.width // OVERSAMPLE)
        target_h = max(8, cropped.height // OVERSAMPLE)
        small = cropped.resize((target_w, target_h), Image.Resampling.NEAREST)
        off_x = -((north[0] - cx) // OVERSAMPLE)
        off_y = -((north[1] - cy) // OVERSAMPLE)
    else:
        img = img.crop(content_bbox(img))
        target_w = max(8, round((args.w + args.d) * HX * args.width_scale))
        scale = target_w / img.width
        target_h = max(8, round(img.height * scale))
        small = img.resize((target_w, target_h), Image.Resampling.NEAREST)
        off_x = -(args.d * HX)
        off_y = -(target_h - (args.w + args.d) * HY - args.pad_bottom)

    small = quantize_to_palette(small, build_palette())

    out_png = out_dir / f"{args.ftype}.png"
    small.save(out_png)

    manifest_path = out_dir / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    manifest[args.ftype] = {
        "url": f"assets/ai/{args.ftype}.png",
        "offset": [off_x, off_y],
        "w": args.w, "d": args.d, "h": args.h,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    mode = "rectified" if args.pts else "width-scaled"
    print(f"{args.ftype} ({mode}): -> {target_w}x{target_h}, "
          f"offset [{off_x}, {off_y}], wrote {out_png.name} + manifest")


if __name__ == "__main__":
    main()
