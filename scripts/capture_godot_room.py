"""Capture deterministic Godot room states and compose reference comparisons."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from PIL import Image, ImageOps


ROOT = Path(__file__).resolve().parents[1]
GODOT = Path(os.environ.get("GODOT_BIN", "/Applications/Godot.app/Contents/MacOS/Godot"))
CAPTURE_MANIFEST = ROOT / "godot/tests/visual-captures.json"
DEFAULT_OUTPUT = ROOT / ".artifacts/godot-visual-baselines"


def capture_state(state: str, output: Path) -> None:
    command = [
        str(GODOT),
        "--path",
        str(ROOT / "godot"),
        "--script",
        "res://tests/capture_runner.gd",
        "--",
        "--output",
        str(output),
        "--state",
        state,
    ]
    subprocess.run(command, check=True, cwd=ROOT)


def validate_capture(path: Path, expected_size: tuple[int, int]) -> Image.Image:
    image = Image.open(path).convert("RGB")
    if image.size != expected_size:
        raise ValueError(f"{path.name}: expected {expected_size}, got {image.size}")
    if image.getbbox() is None:
        raise ValueError(f"{path.name}: capture is empty")
    return image


def compose_comparison(actual: Image.Image, reference: Image.Image, output: Path) -> None:
    target_size = (1392, 1086)
    actual = actual.resize(target_size, Image.Resampling.NEAREST)
    reference = ImageOps.pad(reference.convert("RGB"), target_size, method=Image.Resampling.NEAREST, color="black")
    canvas = Image.new("RGB", (target_size[0] * 2, target_size[1]), "black")
    canvas.paste(reference, (0, 0))
    canvas.paste(actual, (target_size[0], 0))
    canvas.save(output)


def image_metrics(image: Image.Image) -> dict[str, object]:
    mask = ImageOps.grayscale(image).point(lambda value: 255 if value > 12 else 0)
    bbox = mask.getbbox()
    quantized = image.resize((174, 136), Image.Resampling.NEAREST).quantize(colors=8)
    palette = quantized.getpalette() or []
    color_counts = sorted(quantized.getcolors() or [], reverse=True)[:8]
    dominant_colors = []
    for count, index in color_counts:
        offset = index * 3
        dominant_colors.append({"count": count, "rgb": palette[offset : offset + 3]})
    return {"content_bbox": bbox, "dominant_colors": dominant_colors}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--state", action="append")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(CAPTURE_MANIFEST.read_text())
    states = args.state or manifest["captures"]
    expected_size = tuple(manifest["internal_size"])
    reference = Image.open(ROOT / manifest["reference"])
    room_manifest = json.loads((ROOT / "godot/scenes/zhizhi-home.json").read_text())
    args.output.mkdir(parents=True, exist_ok=True)
    report = {
        "internal_size": expected_size,
        "object_count": len(room_manifest["objects"]),
        "reference_metrics": image_metrics(reference.convert("RGB")),
        "captures": [],
    }
    for state in states:
        capture_path = args.output / f"{state}.png"
        if not args.validate_only:
            capture_state(state, capture_path)
        actual = validate_capture(capture_path, expected_size)
        comparison_path = args.output / f"{state}-comparison.png"
        compose_comparison(actual, reference, comparison_path)
        report["captures"].append({
            "state": state,
            "capture": str(capture_path),
            "comparison": str(comparison_path),
            "metrics": image_metrics(actual),
        })
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"captured {len(states)} Godot visual baselines in {args.output}")


if __name__ == "__main__":
    main()
