import json
from pathlib import Path

from PIL import Image
import pytest

from companion_daemon.room_compiler import (
    RoomCompileError,
    _normalize_independent_source,
    compile_room,
)


ROOT = Path(__file__).resolve().parents[1]
ROOM_MANIFEST = ROOT / "assets/dashboard/rooms/zhizhi-home/room.json"


def editable_manifest(tmp_path: Path) -> tuple[dict, Path]:
    manifest = json.loads(ROOM_MANIFEST.read_text())
    manifest["inventory"] = str((ROOM_MANIFEST.parent / manifest["inventory"]).resolve())
    for image in manifest["images"].values():
        image["source"] = str((ROOM_MANIFEST.parent / image["source"]).resolve())
        if patch := image.get("compositePatch"):
            patch["source"] = str((ROOM_MANIFEST.parent / patch["source"]).resolve())
            if approved := patch.get("approved"):
                patch["approved"] = str((ROOM_MANIFEST.parent / approved).resolve())
    for item in [*manifest["objects"], *manifest.get("artDraft", {}).get("objects", [])]:
        specs = item.get("layers", []) or [
            spec for spec in (item.get("backLayer"), item.get("frontOccluder")) if spec
        ]
        for spec in specs:
            source_key = "source" if "source" in spec else "matte"
            spec[source_key] = str((ROOM_MANIFEST.parent / spec[source_key]).resolve())
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
    assert bundle["images"]["deskFront0"] == (
        "/assets/dashboard/rooms/zhizhi-home/runtime/layers/desk-front.png"
    )
    assert bundle["objects"][0]["layers"][0]["image"] == "deskFront0"
    assert bundle["sprites"]["poses"]["sit"]["crop"] == [380, 620, 360, 470]
    assert bundle["interactions"]["dining"] == {
        "object": "dining", "location": "kitchen", "action": "eat",
        "approach": [0, 1, 0],
        "posePosition": [1, 1, 0], "pose": "sit", "facing": "downLeft",
        "depth": {"relativeTo": "dining", "layer": "above-front"},
    }
    assert bundle["inventory"]["summary"] == {
        "total": 65, "planned": 57, "partial": 6, "atomized": 0,
        "verified": 1, "excluded": 1,
    }
    assert {item["id"]: item["status"] for item in bundle["inventory"]["items"]}["desk"] == "partial"
    assert bundle["behavior"]["actionDefinitions"]["read_phone"]["interaction"] == "phone"
    stool = next(item for item in bundle["objects"] if item["id"] == "teal-stool")
    assert stool["occupancy"] == {"kind": "footprint", "tiles": [[7, 0]]}
    assert stool["category"] == "furniture"
    assert stool["assetMode"] == "layered"
    assert [layer["role"] for layer in stool["layers"]] == ["front"]
    assert stool["interactions"] == []
    assert stool["audits"] == {"hidden": True, "solo": True, "behind": True, "front": True}
    assert stool["provenance"]["method"] == "ai-generated-chroma"
    assert bundle["artDraft"]["background"] == "cleanShellCandidate"
    assert "cleanShellCandidate" not in bundle["images"]
    assert bundle["artDraft"]["images"]["cleanShellCandidate"].endswith(
        "/runtime/art-shell/clean-shell-windowless-composite-v1.png"
    )
    assert set(bundle["artDraft"]["images"]).isdisjoint(bundle["images"])
    assert [item["id"] for item in bundle["artDraft"]["objects"]] == [
        "sofa", "bed", "bed-bedding", "table", "desk", "office-chair",
        "sofa-cushion-green", "sofa-cushion-pink", "sofa-throw",
        "coffee-table-setting", "dining", "dining-chair-left",
        "dining-chair-right", "dining-table-setting", "divider",
        "bed-divider-content-cluster", "tall-bookcase",
        "bookcase-content-cluster", "kitchen-wall-cabinets",
        "kitchen-wall-cabinet-decor", "kitchen-sink-counter",
        "kitchen-stove-counter", "kitchen-sink-counter-decor",
        "kitchen-stove-counter-decor", "fridge", "oven", "kitchen-shelf",
        "kitchen-utensil-rail", "kitchen-bin", "desk-rug", "dining-rug",
        "bed-rug", "living-rug", "desk-floor-plant", "living-large-plant",
        "desk-lamp", "bedside-table", "bedside-lamp", "bedside-decor-cluster",
        "foreground-console", "foreground-table-lamp", "foreground-console-plants",
        "window-view", "window-frame", "wall-art-window-upper",
        "wall-art-window-lower", "wall-art-bedside",
        "window-curtains", "window-planter-left", "window-planter-right",
        "window-hanging-plant", "window-string-lights",
    ]
    assert bundle["artDraft"]["objects"][0]["layers"][0]["image"] == "sofaFront0Draft"
    bookcase = next(item for item in bundle["artDraft"]["objects"] if item["id"] == "tall-bookcase")
    assert [layer["role"] for layer in bookcase["layers"]] == ["front"]
    assert bookcase["audits"]["behind"] and bookcase["audits"]["front"]
    rugs = [
        item for item in bundle["artDraft"]["objects"]
        if item["id"] in {"desk-rug", "dining-rug", "bed-rug", "living-rug"}
    ]
    assert len(rugs) == 4
    assert all(item["category"] == "soft-furnishing" for item in rugs)
    assert all(item["occupancy"] == {"kind": "none", "tiles": []} for item in rugs)
    assert all([layer["role"] for layer in item["layers"]] == ["body"] for item in rugs)
    draft_by_id = {item["id"]: item for item in bundle["artDraft"]["objects"]}
    desk_plant = draft_by_id["desk-floor-plant"]
    living_plant = draft_by_id["living-large-plant"]
    assert desk_plant["category"] == living_plant["category"] == "plant"
    assert desk_plant["occupancy"] == {"kind": "none", "tiles": []}
    assert living_plant["occupancy"] == {"kind": "footprint", "tiles": [[7, 4]]}
    assert [layer["role"] for layer in desk_plant["layers"]] == ["front"]
    assert [layer["role"] for layer in living_plant["layers"]] == ["front"]
    assert desk_plant["audits"] == {
        "hidden": True, "solo": True, "behind": False, "front": True,
    }
    assert desk_plant["audit"] == {"front": [1.15, 4.15, 0]}
    assert all(living_plant["audits"].values())
    assert draft_by_id["bookcase-content-cluster"]["attachedTo"] == "tall-bookcase"
    assert [7, 4] not in bundle["walkable"]
    assert bundle["anchors"]["rug"] == [7, 5, 0]
    assert [6, 5, 0] in bundle["routes"]["tour"]
    assert [7, 4, 0] not in bundle["routes"]["tour"]
    lamps = [draft_by_id[item_id] for item_id in (
        "desk-lamp", "bedside-lamp", "foreground-table-lamp",
    )]
    assert all(item["category"] == "lighting" for item in lamps)
    assert all(item["occupancy"] == {"kind": "none", "tiles": []} for item in lamps)
    assert all([layer["role"] for layer in item["layers"]] == ["front", "light"] for item in lamps)
    assert all(item["layers"][1]["blendMode"] == "screen" for item in lamps)
    assert all(0 < item["layers"][1]["opacity"] < 1 for item in lamps)
    assert all(item["audits"] == {
        "hidden": True, "solo": True, "behind": False, "front": False,
    } for item in lamps)
    assert draft_by_id["desk-lamp"]["attachedTo"] == "desk"
    assert draft_by_id["bedside-lamp"]["attachedTo"] == "bedside-table"
    assert draft_by_id["bedside-decor-cluster"]["attachedTo"] == "bedside-table"
    assert draft_by_id["foreground-table-lamp"]["attachedTo"] == "foreground-console"
    assert draft_by_id["foreground-console-plants"]["attachedTo"] == "foreground-console"
    assert draft_by_id["bedside-table"]["occupancy"] == {
        "kind": "footprint", "tiles": [[7, 0]],
    }
    assert draft_by_id["bedside-table"]["audits"] == {
        "hidden": True, "solo": True, "behind": False, "front": False,
    }
    assert draft_by_id["foreground-console"]["occupancy"] == {
        "kind": "footprint", "tiles": [[3, 7], [4, 7]],
    }
    assert draft_by_id["foreground-console"]["audit"] == {
        "behind": [4.5, 7, 0],
    }
    window_objects = [draft_by_id[item_id] for item_id in (
        "window-view", "window-frame", "wall-art-window-upper",
        "wall-art-window-lower", "wall-art-bedside",
        "window-curtains", "window-planter-left", "window-planter-right",
        "window-hanging-plant", "window-string-lights",
    )]
    assert all(item["occupancy"] == {"kind": "wall", "tiles": []} for item in window_objects)
    assert all([layer["role"] for layer in item["layers"]] == ["body"] for item in window_objects)
    assert all(item["audits"] == {
        "hidden": True, "solo": True, "behind": False, "front": False,
    } for item in window_objects)
    assert draft_by_id["window-string-lights"]["category"] == "lighting"
    assert "attachedTo" not in draft_by_id["window-view"]
    assert "attachedTo" not in draft_by_id["window-frame"]
    assert draft_by_id["window-curtains"]["attachedTo"] == "window-frame"
    assert draft_by_id["window-planter-left"]["attachedTo"] == "window-frame"
    assert draft_by_id["window-planter-right"]["attachedTo"] == "window-frame"
    inventory_status = {
        item["id"]: item["status"] for item in bundle["inventory"]["items"]
    }
    assert inventory_status["window-hanging-plant"] == "planned"
    assert inventory_status["window-string-lights"] == "planned"
    assert inventory_status["kitchen-pendant-light"] == "excluded"

    master = Image.open(ROOT / "assets/dashboard/zhizhi-room-isometric-v2.png").convert("RGBA")
    matte = Image.open(ROOT / "assets/dashboard/layers/desk-front-v1.png").convert("RGBA")
    occluder = Image.open(tmp_path / "layers/desk-front.png").convert("RGBA")
    expected_rgb = master.crop((35, 445, 35 + matte.width, 445 + matte.height)).convert("RGB")

    assert occluder.convert("RGB").tobytes() == expected_rgb.tobytes()
    assert occluder.getchannel("A").tobytes() == matte.getchannel("A").tobytes()
    stool_occluder = Image.open(tmp_path / "layers/teal-stool-front.png").convert("RGBA")
    assert stool_occluder.size == (120, 120)
    assert stool_occluder.getchannel("A").getextrema() == (0, 255)
    derived_shell = Image.open(
        tmp_path / "art-shell/clean-shell-windowless-composite-v1.png"
    ).convert("RGBA")
    approved_shell = Image.open(
        ROOT / "assets/dashboard/rooms/zhizhi-home/sources/clean-shell-windowless-composite-v1.png"
    ).convert("RGBA")
    assert derived_shell.size == approved_shell.size
    assert derived_shell.tobytes() == approved_shell.tobytes()
    assert report.generated_assets == tuple(
        tmp_path / f"layers/{name}-front.png"
        for name in ("desk", "bed", "sofa", "table", "dining", "divider", "teal-stool")
    ) + tuple(
        tmp_path / f"draft-layers/{name}"
        for name in (
            "sofa-frame-draft.png", "bed-frame-draft.png",
            "bed-bedding-draft.png", "coffee-table-draft.png",
            "desk-frame-draft.png", "office-chair-draft.png",
            "sofa-cushion-green-draft.png", "sofa-cushion-pink-draft.png",
            "sofa-throw-draft.png", "coffee-table-setting-draft.png",
            "dining-table-draft.png", "dining-chair-left-draft.png",
            "dining-chair-right-draft.png", "dining-table-setting-draft.png",
            "bed-divider-draft.png", "bed-divider-content-draft.png",
            "tall-bookcase-draft.png", "bookcase-content-draft.png",
            "kitchen-wall-cabinets-draft.png",
            "kitchen-wall-cabinet-decor-draft.png",
            "kitchen-sink-counter-draft.png",
            "kitchen-stove-counter-draft.png",
            "kitchen-sink-counter-decor-draft.png",
            "kitchen-stove-counter-decor-draft.png", "fridge-draft.png",
            "oven-draft.png", "kitchen-shelf-draft.png",
            "kitchen-utensil-rail-draft.png", "kitchen-bin-draft.png",
            "desk-rug-draft.png", "dining-rug-draft.png",
            "bed-rug-draft.png", "living-rug-draft.png",
            "desk-floor-plant-draft.png", "living-large-plant-draft.png",
            "desk-lamp-front-draft.png", "desk-lamp-light-draft.png",
            "bedside-table-draft.png", "bedside-lamp-front-draft.png",
            "bedside-lamp-light-draft.png", "bedside-decor-cluster-draft.png",
            "foreground-console-draft.png",
            "foreground-table-lamp-front-draft.png",
            "foreground-table-lamp-light-draft.png",
            "foreground-console-plants-draft.png",
            "window-view-draft.png", "window-frame-draft.png",
            "wall-art-window-upper-draft.png", "wall-art-window-lower-draft.png",
            "wall-art-bedside-draft.png",
            "window-curtains-draft.png", "window-planter-left-draft.png",
            "window-planter-right-draft.png", "window-hanging-plant-draft.png",
            "window-string-lights-draft.png",
        )
    ) + (tmp_path / "art-shell/clean-shell-windowless-composite-v1.png",)


def test_chroma_despill_uses_the_declared_key_channels() -> None:
    source = Image.new("RGBA", (3, 1))
    source.putdata([
        (255, 0, 255, 255),
        (255, 80, 255, 255),
        (120, 80, 40, 255),
    ])

    result = _normalize_independent_source(source, {
        "chromaKey": "#ff00ff",
        "transparentThreshold": 12,
        "opaqueThreshold": 220,
        "despill": True,
    })

    assert result.getpixel((0, 0))[3] == 0
    red, green, blue, alpha = result.getpixel((1, 0))
    assert 0 < alpha < 252
    assert red <= green and blue <= green
    assert result.getpixel((2, 0))[3] == 255


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
    assert "duplicate layer output 'desk-front.png'" in raised.value.errors
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

    assert [layer["role"] for layer in compiled["layers"]] == ["back", "front"]
    assert compiled["layers"][0]["image"] == "deskBack0"
    assert compiled["layers"][1]["image"] == "deskFront1"
    assert Image.open(tmp_path / "runtime/layers/desk-back-independent.png").tobytes() == Image.open(back_source).tobytes()
    assert Image.open(tmp_path / "runtime/layers/desk-front-independent.png").tobytes() == Image.open(front_source).tobytes()


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


def test_compile_room_rejects_a_shell_with_canvas_geometry_drift(tmp_path: Path) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    wrong_shell = tmp_path / "wrong-shell.png"
    Image.new("RGBA", (100, 80), (0, 0, 0, 255)).save(wrong_shell)
    manifest["images"]["wrong-shell"] = {
        "source": str(wrong_shell), "url": "/wrong-shell.png"
    }
    manifest["shell"]["image"] = "wrong-shell"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RoomCompileError, match="shell image size .* must match visual master"):
        compile_room(manifest_path, tmp_path / "runtime")


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
    stale = output_dir / "layers/stale-chair.png"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    report = compile_room(ROOM_MANIFEST, output_dir)
    bundle = json.loads(report.bundle_path.read_text())
    expected_layer_count = sum(
        len(item["layers"])
        for item in [*bundle["objects"], *bundle["artDraft"]["objects"]]
    )

    assert report.bundle_path == output_dir / "room.bundle.json"
    assert not stale.exists()
    assert len(report.generated_assets) == expected_layer_count + 1
    assert output_dir / "art-shell/clean-shell-windowless-composite-v1.png" in report.generated_assets


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda item: item.pop("provenance"), "must define provenance"),
        (lambda item: item.update(category="mystery"), "invalid category"),
        (lambda item: item["occupancy"].update(kind="fog"), "invalid occupancy kind"),
        (lambda item: item["layers"][0].update(role="middle"), "invalid layer role"),
        (lambda item: item["audits"].update(hidden=False), "must support hidden/solo audits"),
    ],
)
def test_compile_room_rejects_invalid_generic_object_contract(
    tmp_path: Path, mutate, message: str
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    stool = next(item for item in manifest["objects"] if item["id"] == "teal-stool")
    mutate(stool)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RoomCompileError, match=message):
        compile_room(manifest_path, tmp_path / "runtime")


def test_compile_room_normalizes_legacy_objects_to_the_generic_runtime_contract(
    tmp_path: Path,
) -> None:
    report = compile_room(ROOM_MANIFEST, tmp_path)
    desk = json.loads(report.bundle_path.read_text())["objects"][0]

    assert set(desk) >= {
        "id", "category", "assetMode", "occupancy", "tile", "layers",
        "interactions", "audits", "provenance", "audit", "auditPose",
    }
    assert "frontOccluder" not in desk
    assert "backLayer" not in desk
    assert "footprint" not in desk
    assert desk["occupancy"] == {"kind": "footprint", "tiles": [[1, 5]]}
    assert desk["layers"][0]["role"] == "front"


def test_compile_room_allows_non_occluding_objects_without_actor_audit_positions(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    stool = next(item for item in manifest["objects"] if item["id"] == "teal-stool")
    stool["audits"] = {"hidden": True, "solo": True, "behind": False, "front": False}
    stool.pop("audit")
    manifest_path.write_text(json.dumps(manifest))

    report = compile_room(manifest_path, tmp_path / "runtime")

    compiled = next(
        item for item in json.loads(report.bundle_path.read_text())["objects"]
        if item["id"] == "teal-stool"
    )
    assert compiled["audit"] == {}


def test_compile_room_rejects_an_unowned_art_draft_object(tmp_path: Path) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    office_chair = next(
        item for item in manifest["artDraft"]["objects"]
        if item["id"] == "office-chair"
    )
    office_chair["id"] = "untracked-office-chair-draft"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(
        RoomCompileError,
        match="draft object 'untracked-office-chair-draft' has no inventory owner",
    ):
        compile_room(manifest_path, tmp_path / "runtime")


def test_compile_room_rejects_inventory_category_drift(tmp_path: Path) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    shelf = next(
        item for item in manifest["artDraft"]["objects"]
        if item["id"] == "kitchen-shelf"
    )
    shelf["category"] = "furniture"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(
        RoomCompileError,
        match="object 'kitchen-shelf' category 'furniture' does not match inventory category 'wall-decoration'",
    ):
        compile_room(manifest_path, tmp_path / "runtime")


def test_compile_room_rejects_an_inventory_excluded_object(tmp_path: Path) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    desk_lamp = next(
        item for item in manifest["artDraft"]["objects"]
        if item["id"] == "desk-lamp"
    )
    desk_lamp["id"] = "kitchen-pendant-light"
    desk_lamp.pop("attachedTo", None)
    for index, layer in enumerate(desk_lamp["layers"]):
        layer["output"] = f"excluded-pendant-{index}.png"
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(
        RoomCompileError,
        match="object 'kitchen-pendant-light' is excluded by the room inventory",
    ):
        compile_room(manifest_path, tmp_path / "runtime")


def test_compile_room_rejects_art_draft_shell_geometry_drift(tmp_path: Path) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    wrong_shell = tmp_path / "wrong-draft-shell.png"
    Image.new("RGBA", (100, 80), (0, 0, 0, 255)).save(wrong_shell)
    manifest["images"]["cleanShellCandidate"]["source"] = str(wrong_shell)
    manifest["images"]["cleanShellCandidate"].pop("compositePatch")
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(
        RoomCompileError,
        match="art draft shell image size .* must match visual master",
    ):
        compile_room(manifest_path, tmp_path / "runtime")


def test_compile_room_rejects_drifted_composite_patch(tmp_path: Path) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    patch = manifest["images"]["cleanShellCandidate"]["compositePatch"]
    patch["mask"] = [980, 125, 1225, 460]
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(
        RoomCompileError,
        match="compositePatch output does not match its approved image",
    ):
        compile_room(manifest_path, tmp_path / "runtime")


def test_compile_room_emits_and_validates_attachment_dag(tmp_path: Path) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    bedding = next(item for item in manifest["artDraft"]["objects"] if item["id"] == "bed-bedding")
    bedding["attachedTo"] = "bed"
    manifest_path.write_text(json.dumps(manifest))
    report = compile_room(manifest_path, tmp_path / "runtime")
    compiled = next(item for item in json.loads(report.bundle_path.read_text())["artDraft"]["objects"] if item["id"] == "bed-bedding")
    assert compiled["attachedTo"] == "bed"
    next(item for item in manifest["artDraft"]["objects"] if item["id"] == "bed")["attachedTo"] = "bed-bedding"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RoomCompileError, match="art draft attachment cycle"):
        compile_room(manifest_path, tmp_path / "cycle-runtime")


def test_compile_room_validates_draft_topology_against_draft_occupancy(tmp_path: Path) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    sofa = next(item for item in manifest["artDraft"]["objects"] if item["id"] == "sofa")
    sofa["occupancy"] = {"kind": "footprint", "tiles": [[0, 1]]}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RoomCompileError, match="art draft interaction 'dining' approach is occupied by 'sofa'"):
        compile_room(manifest_path, tmp_path / "runtime")


def test_compile_room_rejects_invalid_inventory_and_unowned_room_object(
    tmp_path: Path,
) -> None:
    manifest, manifest_path = editable_manifest(tmp_path)
    inventory = json.loads(Path(manifest["inventory"]).read_text())
    inventory["items"][0]["status"] = "done-ish"
    inventory["items"] = [
        item for item in inventory["items"] if item["id"] != "desk"
    ]
    inventory_path = tmp_path / "asset-inventory.json"
    inventory_path.write_text(json.dumps(inventory))
    manifest["inventory"] = str(inventory_path)
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(RoomCompileError) as raised:
        compile_room(manifest_path, tmp_path / "runtime")

    assert "inventory item 'window-view' has invalid status 'done-ish'" in raised.value.errors
    assert "room object 'desk' has no inventory owner" in raised.value.errors
