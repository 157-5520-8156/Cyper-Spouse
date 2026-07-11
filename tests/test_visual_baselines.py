from pathlib import Path
import importlib.util
import shutil

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/visual-baselines/dashboard-room/baseline.json"
MODULE_PATH = ROOT / "scripts/verify_dashboard_visual_baselines.py"
SPEC = importlib.util.spec_from_file_location("verify_dashboard_visual_baselines", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
verify_baselines = MODULE.verify_baselines


def test_visual_baseline_verifier_detects_pixel_drift(tmp_path: Path) -> None:
    baseline_dir = MANIFEST.parent
    for path in baseline_dir.glob("*.jpg"):
        shutil.copy2(path, tmp_path / path.name)

    assert verify_baselines(MANIFEST, tmp_path) == ()

    changed = tmp_path / "desk-behind.jpg"
    image = Image.open(changed).convert("RGB")
    image.putpixel((0, 0), (255, 0, 255))
    image.save(changed, quality=95)

    errors = verify_baselines(MANIFEST, tmp_path)
    assert len(errors) == 1
    assert errors[0].startswith("desk-behind: pixels differ inside ")
