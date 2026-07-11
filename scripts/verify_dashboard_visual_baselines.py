"""Compare deterministic canvas-only dashboard captures with approved pixels."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PIL import Image, ImageChops


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / "docs/visual-baselines/dashboard-room/baseline.json"


def verify_baselines(manifest_path: Path, actual_dir: Path) -> tuple[str, ...]:
    manifest = json.loads(manifest_path.read_text())
    baseline_dir = manifest_path.parent
    errors: list[str] = []
    for capture in manifest["captures"]:
        expected_path = baseline_dir / capture["file"]
        actual_path = actual_dir / capture["file"]
        expected_hash = hashlib.sha256(expected_path.read_bytes()).hexdigest()
        if expected_hash != capture["sha256"]:
            errors.append(
                f"{capture['name']}: approved baseline hash {expected_hash} "
                f"!= manifest {capture['sha256']}"
            )
            continue
        if not actual_path.is_file():
            errors.append(f"{capture['name']}: actual capture is missing")
            continue
        expected = Image.open(expected_path).convert("RGB")
        actual = Image.open(actual_path).convert("RGB")
        if actual.size != expected.size:
            errors.append(
                f"{capture['name']}: size {actual.size} != baseline {expected.size}"
            )
            continue
        difference = ImageChops.difference(expected, actual)
        if difference.getbbox() is not None:
            errors.append(
                f"{capture['name']}: pixels differ inside {difference.getbbox()}"
            )
    return tuple(errors)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("actual_dir", type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args()
    errors = verify_baselines(args.manifest, args.actual_dir)
    if errors:
        raise SystemExit("\n".join(errors))
    print(f"verified {len(json.loads(args.manifest.read_text())['captures'])} visual baselines")


if __name__ == "__main__":
    main()
