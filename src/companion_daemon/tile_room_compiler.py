"""Deterministic validation and bundling for grid-driven TileRoom scenes."""

from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TileRoomCompileError(ValueError):
    """Raised when a TileRoom manifest breaks the fixed spatial contract."""


@dataclass(frozen=True)
class TileRoomCompileReport:
    scene_id: str
    object_count: int
    output: Path


def _occupancy_cells(obj: dict[str, Any]) -> list[tuple[int, int]]:
    if obj.get("occupancy") == "decor":
        return []
    transform = obj["transform"]
    return [
        (x, y)
        for x in range(math.floor(transform["x"]), math.ceil(transform["x"] + transform["width"]))
        for y in range(math.floor(transform["y"]), math.ceil(transform["y"] + transform["depth"]))
    ]


def _edge_key(
    first: tuple[int, int], second: tuple[int, int]
) -> tuple[tuple[int, int], tuple[int, int]]:
    return tuple(sorted((first, second)))  # type: ignore[return-value]


def wall_edge_segments(scene: dict[str, Any]) -> set[tuple[tuple[int, int], tuple[int, int]]]:
    """Return cell-to-cell crossings blocked by interior walls, never floor tiles."""
    grid = scene["grid"]
    edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    for wall in scene.get("walls", []):
        x1, y1 = wall["from"]
        x2, y2 = wall["to"]
        if y1 == y2 and 0 < y1 < grid["depth"]:
            edges.update(
                _edge_key((x, int(y1 - 1)), (x, int(y1)))
                for x in range(math.floor(min(x1, x2)), math.ceil(max(x1, x2)))
            )
        elif x1 == x2 and 0 < x1 < grid["width"]:
            edges.update(
                _edge_key((int(x1 - 1), y), (int(x1), y))
                for y in range(math.floor(min(y1, y2)), math.ceil(max(y1, y2)))
            )
    return edges


def blocked_floor_tiles(scene: dict[str, Any]) -> set[tuple[int, int]]:
    """The floor occupancy truth: only solid furniture occupies a floor tile."""
    return {cell for obj in scene.get("objects", []) for cell in _occupancy_cells(obj)}


def _reachable_floor_tiles(
    width: int,
    depth: int,
    start: tuple[int, int],
    blocked: set[tuple[int, int]],
    wall_edges: set[tuple[tuple[int, int], tuple[int, int]]],
) -> set[tuple[int, int]]:
    if start in blocked or not (0 <= start[0] < width and 0 <= start[1] < depth):
        return set()
    reachable = {start}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            next_tile = (x + dx, y + dy)
            if (
                next_tile in reachable
                or next_tile in blocked
                or not (0 <= next_tile[0] < width and 0 <= next_tile[1] < depth)
                or _edge_key((x, y), next_tile) in wall_edges
            ):
                continue
            reachable.add(next_tile)
            queue.append(next_tile)
    return reachable


def _fail(errors: list[str]) -> None:
    if errors:
        raise TileRoomCompileError("Invalid tile room: " + "; ".join(errors))


def validate_tile_room(scene: dict[str, Any]) -> None:
    errors: list[str] = []
    grid = scene.get("grid", {})
    width, depth = grid.get("width"), grid.get("depth")
    if scene.get("renderer") != "tile-v1":
        errors.append("renderer must be tile-v1")
    if not isinstance(width, int) or not isinstance(depth, int) or width <= 0 or depth <= 0:
        errors.append("grid must define positive integer width/depth")
    projection = scene.get("projection", {})
    if projection.get("tile") != [128, 64] or projection.get("height") != 64:
        errors.append("projection must use fixed tile [128, 64] and height 64")

    materials = scene.get("materials", {})
    if scene.get("floor", {}).get("material") not in materials:
        errors.append("floor references unknown material")
    for wall in scene.get("walls", []):
        if wall.get("material") not in materials:
            errors.append(f"wall {wall.get('id', '<unknown>')} references unknown material")
        if not isinstance(wall.get("height"), (int, float)) or wall["height"] <= 0:
            errors.append(f"wall {wall.get('id', '<unknown>')} has invalid height")
        for point in (wall.get("from"), wall.get("to")):
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not all(isinstance(value, (int, float)) for value in point)
            ):
                errors.append(f"wall {wall.get('id', '<unknown>')} has invalid endpoint")
    occupied: set[tuple[int, int]] = set()
    objects: dict[str, dict[str, Any]] = {}
    for obj in scene.get("objects", []):
        ident = obj.get("id")
        if not isinstance(ident, str) or not ident or ident in objects:
            errors.append(f"duplicate or missing object id {ident!r}")
            continue
        objects[ident] = obj
        transform = obj.get("transform", {})
        transform_is_numeric = True
        for key in ("x", "y", "z", "width", "depth", "height"):
            if not isinstance(transform.get(key), (int, float)):
                errors.append(f"object {ident} has invalid {key}")
                transform_is_numeric = False
        if any(transform.get(key, 0) <= 0 for key in ("width", "depth", "height")):
            errors.append(f"object {ident} must have positive dimensions")
        if obj.get("material") not in materials:
            errors.append(f"object {ident} references unknown material")
        declared_collider: set[tuple[int, int]] = set()
        for point in obj.get("collider", []):
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not all(isinstance(value, int) for value in point)
            ):
                errors.append(f"object {ident} collider must contain integer grid points")
                continue
            tile = (point[0], point[1])
            declared_collider.add(tile)
        expected_collider = set(_occupancy_cells(obj)) if transform_is_numeric else set()
        if declared_collider != expected_collider:
            errors.append(f"object {ident} collider must match its occupied floor tiles")
        for tile in expected_collider:
            if not (0 <= tile[0] < width and 0 <= tile[1] < depth):
                errors.append(f"object {ident} collider is outside grid")
            if tile in occupied:
                errors.append(f"collider overlap at {tile[0]},{tile[1]}")
            occupied.add(tile)

    navigation_blocked = occupied
    wall_edges = wall_edge_segments(scene)
    for name, interaction in scene.get("interactions", {}).items():
        if interaction.get("object") not in objects:
            errors.append(f"interaction {name} references unknown object")
        approach = interaction.get("approach", [])
        if (
            not isinstance(approach, list)
            or len(approach) != 2
            or not all(isinstance(value, int) for value in approach)
        ):
            errors.append(f"interaction {name} requires an integer approach")
            continue
        tile = (approach[0], approach[1])
        if not (0 <= tile[0] < width and 0 <= tile[1] < depth):
            errors.append(f"interaction {name} approach is outside grid")
        if tile in navigation_blocked:
            errors.append(f"interaction {name} approach is blocked")

    for name, action in scene.get("actions", {}).items():
        if action.get("interaction") not in scene.get("interactions", {}):
            errors.append(f"action {name} references unknown interaction")

    entry = scene.get("anchors", {}).get("entry")
    if (
        not isinstance(entry, list)
        or len(entry) != 2
        or not all(isinstance(value, int) for value in entry)
    ):
        errors.append("anchors.entry must be an integer grid point")
    else:
        reachable = _reachable_floor_tiles(
            width, depth, (entry[0], entry[1]), navigation_blocked, wall_edges
        )
        for name, interaction in scene.get("interactions", {}).items():
            approach = interaction.get("approach")
            if (
                isinstance(approach, list)
                and len(approach) == 2
                and tuple(approach) not in reachable
            ):
                errors.append(f"interaction {name} approach is unreachable from entry")

    sprites = scene.get("sprites", {})
    if not isinstance(sprites.get("walk", {}).get("url"), str) or not sprites["walk"][
        "url"
    ].startswith("/assets/"):
        errors.append("walk sprite must reference a local /assets/ URL")
    for name, pose in sprites.get("poses", {}).items():
        crop = pose.get("crop")
        display, anchor = pose.get("display"), pose.get("anchor")
        if not isinstance(pose.get("url"), str) or not pose["url"].startswith("/assets/"):
            errors.append(f"pose {name} must reference a local /assets/ URL")
        if (
            not isinstance(crop, list)
            or len(crop) != 4
            or not all(isinstance(value, int) and value >= 0 for value in crop)
            or (len(crop) == 4 and (crop[2] <= 0 or crop[3] <= 0))
        ):
            errors.append(f"pose {name} has invalid crop")
        if (
            not isinstance(display, list)
            or len(display) != 2
            or not all(isinstance(value, (int, float)) and value > 0 for value in display)
        ):
            errors.append(f"pose {name} has invalid display size")
        if (
            not isinstance(anchor, list)
            or len(anchor) != 2
            or not all(isinstance(value, (int, float)) and 0 <= value <= 1 for value in anchor)
        ):
            errors.append(f"pose {name} has invalid anchor")

    for name, route in scene.get("routes", {}).items():
        if not isinstance(route, list) or len(route) < 2:
            errors.append(f"route {name} must contain at least two points")
            continue
        previous: tuple[int, int] | None = None
        for index, point in enumerate(route):
            if (
                not isinstance(point, list)
                or len(point) != 2
                or not all(isinstance(value, int) for value in point)
            ):
                errors.append(f"route {name} point {index} must be an integer grid point")
                continue
            tile = (point[0], point[1])
            if not (0 <= tile[0] < width and 0 <= tile[1] < depth) or tile in navigation_blocked:
                errors.append(f"route {name} point {index} is invalid or blocked")
            if previous and abs(tile[0] - previous[0]) + abs(tile[1] - previous[1]) != 1:
                errors.append(f"route {name} step {index} is not adjacent")
            if previous and _edge_key(previous, tile) in wall_edges:
                errors.append(f"route {name} step {index} crosses a wall")
            previous = tile
    _fail(errors)


def compile_tile_room(source: Path, output: Path) -> TileRoomCompileReport:
    scene = json.loads(source.read_text(encoding="utf-8"))
    validate_tile_room(scene)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(scene, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return TileRoomCompileReport(
        scene_id=scene["id"], object_count=len(scene["objects"]), output=output
    )
