#!/usr/bin/env python3
"""Convert an AI-generated character render into an engine-ready actor frame.

Unlike furniture, actor frames need no iso rectification: chroma-key removal
-> content crop -> reduction to a target pixel height -> fixed-palette
quantization -> write PNG + update the actor manifest.

Reduction modes:
  mode (default) - quantize at full res, then per output cell keep the
                   dominant palette color (crisp, hand-pixelled look)
  box            - plain BOX resample then quantize (softer)

The engine draws actor frames anchored at bottom-center (px - w/2, py - h),
so cropping to content automatically puts the feet on the anchor.

Usage:
  uv run python tools/make_actor.py --in raw/actor/front-stand.png \
      --name front-stand --height-px 28
  uv run python tools/make_actor.py --in raw/actor/front-sit.png \
      --name front-sit --match-scale raw/actor/front-stand.png --match-height 28
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from PIL import Image

from make_sprite import build_palette, content_bbox, key_background, quantize_to_palette


def keyed_content(path: str | Path) -> Image.Image:
    img = key_background(Image.open(path), "magenta")
    return img.crop(content_bbox(img))


def mode_reduce(img: Image.Image, tw: int, th: int,
                palette: list[tuple[int, int, int]], cover: float = 0.42) -> Image.Image:
    """Per output cell, keep the dominant palette color; a cell is opaque
    only if enough of it is covered by content."""
    q = quantize_to_palette(img, palette)
    src = q.load()
    w, h = q.size
    out = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    op = out.load()
    for oy in range(th):
        y0, y1 = round(oy * h / th), max(round((oy + 1) * h / th), round(oy * h / th) + 1)
        for ox in range(tw):
            x0, x1 = round(ox * w / tw), max(round((ox + 1) * w / tw), round(ox * w / tw) + 1)
            cnt: Counter = Counter()
            total = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    total += 1
                    c = src[x, y]
                    if c[3] > 128:
                        cnt[c[:3]] += 1
            if total and sum(cnt.values()) / total >= cover:
                op[ox, oy] = (*cnt.most_common(1)[0][0], 255)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--name", required=True, help="frame name, e.g. front-stand")
    ap.add_argument("--height-px", type=int, default=0, help="exact target content height")
    ap.add_argument("--match-scale", default="",
                    help="raw render whose scale this frame should share")
    ap.add_argument("--match-height", type=int, default=28,
                    help="target height assigned to the --match-scale render")
    ap.add_argument("--reduce", default="mode", choices=["mode", "box"])
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent
                                             / "assets" / "ai" / "actor"))
    args = ap.parse_args()

    content = keyed_content(args.src)

    if args.height_px:
        target_h = args.height_px
    elif args.match_scale:
        ref_h = keyed_content(args.match_scale).height
        target_h = max(8, round(content.height * args.match_height / ref_h))
    else:
        raise SystemExit("pass --height-px or --match-scale")

    target_w = max(4, round(content.width * target_h / content.height))
    palette = build_palette()
    if args.reduce == "mode":
        small = mode_reduce(content, target_w, target_h, palette)
    else:
        small = quantize_to_palette(
            content.resize((target_w, target_h), Image.Resampling.BOX), palette)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_png = out_dir / f"{args.name}.png"
    small.save(out_png)

    manifest_path = out_dir / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    manifest[args.name] = {
        "url": f"assets/ai/actor/{args.name}.png",
        "w": target_w, "h": target_h,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"{args.name}: raw {content.width}x{content.height} -> {target_w}x{target_h}")


if __name__ == "__main__":
    main()
