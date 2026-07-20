import json
from pathlib import Path

import pytest

from companion_daemon.tile_room_compiler import (
    TileRoomCompileError,
    blocked_floor_tiles,
    compile_tile_room,
    wall_edge_segments,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "assets/dashboard/tile-rooms/zhizhi-home/room.json"


def test_compile_tile_room_builds_grid_driven_runtime_bundle(tmp_path: Path) -> None:
    report = compile_tile_room(SOURCE, tmp_path / "room.bundle.json")
    bundle = json.loads(report.output.read_text())

    assert report.scene_id == "zhizhi-home-tile"
    assert report.object_count == 13
    assert bundle["projection"] == {"tile": [128, 64], "height": 64, "origin": [760, 126]}
    assert bundle["objects"][0]["transform"] == {
        "x": 1,
        "y": 2,
        "z": 0,
        "width": 2,
        "depth": 1,
        "height": 1.25,
    }
    assert bundle["sprites"]["walk"]["url"].endswith("zhizhi-iso-walk-v4.png")
    assert all("collider" in item for item in bundle["objects"])


def test_compile_tile_room_rejects_blocked_interaction_and_invalid_route(tmp_path: Path) -> None:
    scene = json.loads(SOURCE.read_text())
    scene["interactions"]["study"]["approach"] = [1, 2]
    scene["routes"]["tour"][1] = [9, 9]
    source = tmp_path / "bad-room.json"
    source.write_text(json.dumps(scene))

    with pytest.raises(TileRoomCompileError, match="approach is blocked"):
        compile_tile_room(source, tmp_path / "room.bundle.json")


def test_solid_furniture_collider_is_derived_from_its_floor_footprint(tmp_path: Path) -> None:
    scene = json.loads(SOURCE.read_text())
    plant = next(item for item in scene["objects"] if item["id"] == "plant")
    plant["collider"] = []
    source = tmp_path / "drifted-collider.json"
    source.write_text(json.dumps(scene))

    with pytest.raises(
        TileRoomCompileError, match="plant collider must match its occupied floor tiles"
    ):
        compile_tile_room(source, tmp_path / "room.bundle.json")


def test_walls_block_crossings_not_their_adjacent_floor_tiles() -> None:
    scene = json.loads(SOURCE.read_text())
    scene["walls"].append(
        {"id": "inner", "from": [6, 0], "to": [6, 10], "height": 2, "material": "cream"}
    )

    blocked = blocked_floor_tiles(scene)
    edges = wall_edge_segments(scene)

    assert (6, 0) not in blocked
    assert ((5, 6), (6, 6)) in edges
    assert (6, 7) not in blocked


def test_compile_tile_room_rejects_interaction_separated_by_a_wall(tmp_path: Path) -> None:
    scene = json.loads(SOURCE.read_text())
    scene["walls"].append(
        {"id": "inner", "from": [6, 0], "to": [6, 10], "height": 2, "material": "cream"}
    )
    source = tmp_path / "unreachable-room.json"
    source.write_text(json.dumps(scene))

    with pytest.raises(TileRoomCompileError, match="study approach is unreachable"):
        compile_tile_room(source, tmp_path / "room.bundle.json")
