import json
from pathlib import Path

from PIL import Image
import pytest

from companion_daemon.room_compiler import RoomCompileError, compile_room


ROOT = Path(__file__).resolve().parents[1]
ROOM_MANIFEST = ROOT / "assets/dashboard/rooms/zhizhi-home/room.json"


def editable_manifest(tmp_path: Path) -> tuple[dict, Path]:
    manifest = json.loads(ROOM_MANIFEST.read_text())
    for image in manifest["images"].values():
        image["source"] = str((ROOM_MANIFEST.parent / image["source"]).resolve())
    for item in manifest["objects"]:
        occluder = item["frontOccluder"]
        source_key = "source" if "source" in occluder else "matte"
        occluder[source_key] = str((ROOM_MANIFEST.parent / occluder[source_key]).resolve())
    manifest_path = tmp_path / "room.json"
    return manifest, manifest_path


def test_compile_room_builds_runtime_bundle_and_coordinate_locked_occluders(
    tmp_path: Path,
) -> None:
    report = compile_room(ROOM_MANIFEST, tmp_path)

    bundle = json.loads(report.bundle_path.read_text())
    assert bundle["id"] == "zhizhi-home"
    assert bundle["images"]["room"] == "/assets/dashboard/zhizhi-room-isometric-v2.png"
    assert [item["id"] for item in bundle["objects"]] == [
        "desk",
        "bed",
        "sofa",
        "table",
        "dining",
        "divider",
        "teal-stool",
    ]
    assert bundle["images"]["deskFront"] == (
        "/assets/dashboard/rooms/zhizhi-home/runtime/occluders/desk-front.png"
    )
    assert bundle["objects"][0]["frontOccluder"]["image"] == "deskFront"
    assert bundle["sprites"]["poses"]["sit"]["crop"] == [380, 620, 360, 470]
    assert bundle["behavior"]["actionDefinitions"]["read_phone"]["interaction"] == "phone"
    stool = next(item for item in bundle["objects"] if item["id"] == "teal-stool")
    assert stool["footprint"] == [[7, 0]]

    master = Image.open(ROOT / "assets/dashboard/zhizhi-room-isometric-v2.png").convert("RGBA")
    matte = Image.open(ROOT / "assets/dashboard/layers/desk-front-v1.png").convert("RGBA")
    occluder = Image.open(tmp_path / "occluders/desk-front.png").convert("RGBA")
    expected_rgb = master.crop((35, 445, 35 + matte.width, 445 + matte.height)).convert("RGB")

    assert occluder.convert("RGB").tobytes() == expected_rgb.tobytes()
    assert occluder.getchannel("A").tobytes() == matte.getchannel("A").tobytes()
    stool_occluder = Image.open(tmp_path / "occluders/teal-stool-front.png").convert("RGBA")
    assert stool_occluder.size == (120, 120)
    assert stool_occluder.getchannel("A").getextrema() == (0, 255)
    assert report.generated_assets == tuple(
        tmp_path / f"occluders/{name}-front.png"
        for name in ("desk", "bed", "sofa", "table", "dining", "divider", "teal-stool")
    )


def test_compile_room_rejects_invalid_geometry_before_writing_outputs(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    manifest["objects"][0]["frontOccluder"]["origin"] = [2000, 2000]
    manifest_path.write_text(json.dumps(manifest))
    output_dir = tmp_path / "runtime"

    with pytest.raises(RoomCompileError, match="exceeds the visual master bounds"):
        compile_room(manifest_path, output_dir)

    assert not output_dir.exists()


def test_compile_room_rejects_broken_routes_and_unreachable_interactions(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    manifest["routes"]["tour"] = [[0, 1, 0], [7, 7, 0]]
    manifest["interactions"]["sofa"]["approach"] = [4, 7, 0]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RoomCompileError) as raised:
        compile_room(manifest_path, tmp_path / "runtime")

    assert "route 'tour' has a non-adjacent step" in raised.value.errors
    assert "interaction 'sofa' approach is not on a free walkable tile" in raised.value.errors


def test_compile_room_rejects_matte_without_a_transparency_contour(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    opaque_matte = tmp_path / "opaque.png"
    Image.new("RGBA", (20, 20), (255, 255, 255, 255)).save(opaque_matte)
    occluder = manifest["objects"][0]["frontOccluder"]
    occluder.update({"matte": str(opaque_matte), "origin": [20, 20]})
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RoomCompileError, match="must contain transparent and opaque pixels"):
        compile_room(manifest_path, tmp_path / "runtime")


def test_compile_room_rejects_interaction_disconnected_from_entry(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    manifest["walkable"].remove([0, 2])
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RoomCompileError) as raised:
        compile_room(manifest_path, tmp_path / "runtime")

    assert "interaction 'dining' is unreachable from entry" in raised.value.errors


def test_compile_room_rejects_duplicate_object_identity_and_footprint(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    manifest["objects"][1]["id"] = "desk"
    manifest["objects"][1]["footprint"] = [[1, 5]]
    manifest["objects"][1]["frontOccluder"]["output"] = "desk-front.png"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RoomCompileError) as raised:
        compile_room(manifest_path, tmp_path / "runtime")

    assert "duplicate object id 'desk'" in raised.value.errors
    assert "duplicate occluder output 'desk-front.png'" in raised.value.errors
    assert "footprint tile (1, 5) is claimed by both 'desk' and 'desk'" in raised.value.errors


def test_compile_room_supports_independent_back_and_front_layers(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    back_source = tmp_path / "stool-back.png"
    front_source = tmp_path / "stool-front.png"
    Image.new("RGBA", (12, 10), (80, 60, 40, 0)).save(back_source)
    Image.new("RGBA", (12, 10), (100, 70, 45, 0)).save(front_source)
    for source in (back_source, front_source):
        image = Image.open(source)
        image.putpixel((5, 5), (120, 80, 50, 255))
        image.save(source)
    desk = manifest["objects"][0]
    desk["backLayer"] = {
        "source": str(back_source), "output": "desk-back-independent.png", "origin": [35, 445]
    }
    desk["frontOccluder"] = {
        "source": str(front_source), "output": "desk-front-independent.png",
        "origin": [35, 445], "depthBias": 500
    }
    manifest_path.write_text(json.dumps(manifest))

    report = compile_room(manifest_path, tmp_path / "runtime")
    bundle = json.loads(report.bundle_path.read_text())
    compiled = bundle["objects"][0]

    assert compiled["backLayer"]["image"] == "deskBack"
    assert compiled["frontOccluder"]["image"] == "deskFront"
    assert Image.open(tmp_path / "runtime/layers/desk-back-independent.png").tobytes() == Image.open(back_source).tobytes()
    assert Image.open(tmp_path / "runtime/occluders/desk-front-independent.png").tobytes() == Image.open(front_source).tobytes()


def test_compile_room_rejects_missing_sources_and_out_of_grid_coordinates(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    manifest["images"]["sprite"]["source"] = str(tmp_path / "missing-sprite.png")
    manifest["anchors"]["rug"] = [8, 4, 0]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RoomCompileError) as raised:
        compile_room(manifest_path, tmp_path / "runtime")

    assert "image 'sprite' source does not exist" in raised.value.errors
    assert "anchor 'rug' is outside the room grid" in raised.value.errors


def test_compile_room_rejects_unknown_pose_and_object_references(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    manifest["interactions"]["desk"]["pose"] = "levitate"
    manifest["interactions"]["desk"]["object"] = "missing-desk"
    manifest["interactions"]["desk"]["facing"] = "sideways"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RoomCompileError) as raised:
        compile_room(manifest_path, tmp_path / "runtime")

    assert "interaction 'desk' references unknown pose 'levitate'" in raised.value.errors
    assert "interaction 'desk' references unknown object 'missing-desk'" in raised.value.errors
    assert "interaction 'desk' references unknown facing 'sideways'" in raised.value.errors


def test_compile_room_replaces_stale_runtime_as_one_complete_output(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "runtime"
    stale = output_dir / "occluders/stale-chair.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    report = compile_room(ROOM_MANIFEST, output_dir)

    assert report.bundle_path == output_dir / "room.bundle.json"
    assert not stale.exists()
    assert len(report.generated_assets) == 7
