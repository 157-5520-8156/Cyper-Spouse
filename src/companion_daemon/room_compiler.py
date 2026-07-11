"""Compile an editable dashboard room manifest into runtime assets.

The compiler is the seam between source artwork and the browser renderer.  AI
or hand-edited PNG files may provide alpha mattes or independent layers. Raw
chroma-key candidates can also be normalized by manifest-declared transforms.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

from PIL import Image


class RoomCompileError(ValueError):
    """Raised when source room data cannot produce a trustworthy bundle."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(errors))


@dataclass(frozen=True)
class CompileReport:
    """Observable result of a successful room compilation."""

    bundle_path: Path
    generated_assets: tuple[Path, ...]
    warnings: tuple[str, ...] = ()


def _source_path(manifest_dir: Path, value: str) -> Path:
    return (manifest_dir / value).resolve()


@dataclass(frozen=True)
class LayerSource:
    mode: str
    path: Path
    image: Image.Image


def _parse_hex_color(value: str) -> tuple[int, int, int]:
    normalized = value.removeprefix("#")
    if len(normalized) != 6:
        raise RoomCompileError(["sourceTransform chromaKey must be a six-digit RGB hex value"])
    try:
        return tuple(int(normalized[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError as exc:
        raise RoomCompileError(["sourceTransform chromaKey must be a six-digit RGB hex value"]) from exc


def _normalize_independent_source(
    image: Image.Image, transform: dict[str, Any] | None
) -> Image.Image:
    if not transform:
        return image
    if crop := transform.get("crop"):
        image = image.crop(tuple(crop))
    if key_value := transform.get("chromaKey"):
        key = _parse_hex_color(key_value)
        transparent = float(transform.get("transparentThreshold", 12))
        opaque = float(transform.get("opaqueThreshold", 220))
        if not 0 <= transparent < opaque <= 255:
            raise RoomCompileError(
                ["sourceTransform thresholds must satisfy 0 <= transparent < opaque <= 255"]
            )
        pixels = image.load()
        for y in range(image.height):
            for x in range(image.width):
                red, green, blue, source_alpha = pixels[x, y]
                distance = max(abs(red - key[0]), abs(green - key[1]), abs(blue - key[2]))
                ratio = max(0.0, min(1.0, (distance - transparent) / (opaque - transparent)))
                smooth = ratio * ratio * (3.0 - 2.0 * ratio)
                alpha = round(255 * smooth * (source_alpha / 255))
                if alpha <= 8:
                    pixels[x, y] = (0, 0, 0, 0)
                    continue
                if transform.get("despill") and alpha < 252:
                    green = min(green, max(red, blue) - 1)
                pixels[x, y] = (red, max(0, green), blue, alpha)
    if resize := transform.get("resize"):
        image = image.resize(tuple(resize), Image.Resampling.LANCZOS)
    return image


def _resolve_layer_source(
    manifest_dir: Path, spec: dict[str, Any]
) -> LayerSource:
    if "source" in spec:
        path = _source_path(manifest_dir, spec["source"])
        image = Image.open(path).convert("RGBA")
        image = _normalize_independent_source(image, spec.get("sourceTransform"))
        return LayerSource("independent", path, image)
    path = _source_path(manifest_dir, spec["matte"])
    image = Image.open(path).convert("RGBA")
    if crop := spec.get("matteCrop"):
        image = image.crop(tuple(crop))
    return LayerSource("master-matte", path, image)


def _source_layer(
    *, master: Image.Image, manifest_dir: Path, spec: dict[str, Any]
) -> Image.Image:
    resolved = _resolve_layer_source(manifest_dir, spec)
    if resolved.mode == "independent":
        return resolved.image
    left, top = spec["origin"]
    foreground = master.crop(
        (left, top, left + resolved.image.width, top + resolved.image.height)
    ).convert("RGBA")
    foreground.putalpha(resolved.image.getchannel("A"))
    return foreground


def _build_layer(
    *,
    master: Image.Image,
    manifest_dir: Path,
    output_dir: Path,
    spec: dict[str, Any],
    subdirectory: str,
) -> Path:
    layer = _source_layer(master=master, manifest_dir=manifest_dir, spec=spec)
    target = output_dir / subdirectory / spec["output"]
    target.parent.mkdir(parents=True, exist_ok=True)
    layer.save(target)
    return target


def _validate_occluders(
    *, master: Image.Image, manifest_dir: Path, objects: list[dict[str, Any]]
) -> None:
    errors: list[str] = []
    for item in objects:
        for role, spec in (("occluder", item["frontOccluder"]), ("back layer", item.get("backLayer"))):
            if spec is None:
                continue
            layer = _resolve_layer_source(manifest_dir, spec).image
            width, height = layer.size
            alpha_min, alpha_max = layer.getchannel("A").getextrema()
            if alpha_min != 0 or alpha_max != 255:
                errors.append(
                    f"{role} {spec['output']!r} must contain transparent and opaque pixels"
                )
            left, top = spec["origin"]
            right, bottom = left + width, top + height
            if left < 0 or top < 0 or right > master.width or bottom > master.height:
                errors.append(
                    f"{role} {spec['output']!r} exceeds the visual master bounds"
                )
    if errors:
        raise RoomCompileError(errors)


def _validate_identity(objects: list[dict[str, Any]]) -> None:
    errors: list[str] = []
    ids: set[str] = set()
    outputs: set[str] = set()
    footprint_owners: dict[tuple[int | float, int | float], str] = {}
    for item in objects:
        object_id = item["id"]
        if object_id in ids:
            errors.append(f"duplicate object id {object_id!r}")
        ids.add(object_id)

        for spec in (item.get("backLayer"), item["frontOccluder"]):
            if spec is None:
                continue
            output = spec["output"]
            if output in outputs:
                errors.append(f"duplicate occluder output {output!r}")
            outputs.add(output)

        for point in item["footprint"]:
            tile = _xy(point)
            if owner := footprint_owners.get(tile):
                errors.append(
                    f"footprint tile {tile} is claimed by both {owner!r} and {object_id!r}"
                )
            else:
                footprint_owners[tile] = object_id
    if errors:
        raise RoomCompileError(errors)


def _xy(point: list[int | float]) -> tuple[int | float, int | float]:
    return point[0], point[1]


def _validate_sources_and_grid(
    manifest: dict[str, Any], manifest_dir: Path
) -> None:
    errors: list[str] = []
    for name, image in manifest["images"].items():
        if not _source_path(manifest_dir, image["source"]).is_file():
            errors.append(f"image {name!r} source does not exist")
    for item in manifest["objects"]:
        for role, spec in (
            ("back layer", item.get("backLayer")),
            ("front occluder", item["frontOccluder"]),
        ):
            if spec is None:
                continue
            source_value = spec.get("source") or spec.get("matte")
            if not source_value or not _source_path(manifest_dir, source_value).is_file():
                errors.append(f"object {item['id']!r} {role} source does not exist")

    object_ids = {item["id"] for item in manifest["objects"]}
    pose_names = set(manifest["sprites"]["poses"])
    facing_names = set(manifest["sprites"]["walk"]["columns"])
    interaction_names = set(manifest["interactions"])
    for name, interaction in manifest["interactions"].items():
        if interaction["object"] not in object_ids:
            errors.append(
                f"interaction {name!r} references unknown object {interaction['object']!r}"
            )
        if interaction["pose"] not in pose_names:
            errors.append(
                f"interaction {name!r} references unknown pose {interaction['pose']!r}"
            )
        if interaction["facing"] not in facing_names:
            errors.append(
                f"interaction {name!r} references unknown facing {interaction['facing']!r}"
            )
        depth = interaction.get("depth")
        if isinstance(depth, dict) and depth.get("relativeTo") not in object_ids:
            errors.append(
                f"interaction {name!r} depth references unknown object {depth.get('relativeTo')!r}"
            )
    for item in manifest["objects"]:
        if set(item.get("audit", {})) != {"behind", "front"}:
            errors.append(f"object {item['id']!r} must define behind/front audit positions")
        for side, pose in item.get("auditPose", {}).items():
            if side not in item.get("audit", {}):
                errors.append(f"object {item['id']!r} has an audit pose for unknown side {side!r}")
            if pose not in pose_names:
                errors.append(f"object {item['id']!r} audit references unknown pose {pose!r}")

    behavior = manifest["behavior"]
    for action, definition in behavior["actionDefinitions"].items():
        interaction = definition.get("interaction")
        if interaction and interaction not in interaction_names:
            errors.append(
                f"action {action!r} references unknown interaction {interaction!r}"
            )
    for location, facing in behavior["locationFacing"].items():
        if facing not in facing_names:
            errors.append(
                f"location {location!r} references unknown facing {facing!r}"
            )

    image_specs = manifest["images"]
    sprite_specs = manifest["sprites"]
    if sprite_specs["walk"]["image"] not in image_specs:
        errors.append("walk sprite references an unknown image")
    for name, pose in sprite_specs["poses"].items():
        image_spec = image_specs.get(pose["image"])
        if image_spec is None:
            errors.append(f"pose {name!r} references an unknown image")
            continue
        image_path = _source_path(manifest_dir, image_spec["source"])
        if not image_path.is_file():
            continue
        width, height = Image.open(image_path).size
        x, y, crop_width, crop_height = pose["crop"]
        if x < 0 or y < 0 or x + crop_width > width or y + crop_height > height:
            errors.append(f"pose {name!r} crop exceeds its source image")

    grid = manifest["grid"]

    def in_grid(point: list[int | float]) -> bool:
        x, y = _xy(point)
        return grid["minX"] <= x <= grid["maxX"] and grid["minY"] <= y <= grid["maxY"]

    for name, point in manifest["anchors"].items():
        if not in_grid(point):
            errors.append(f"anchor {name!r} is outside the room grid")
    for item in manifest["objects"]:
        if not in_grid(item["depthTile"]):
            errors.append(f"object {item['id']!r} depth tile is outside the room grid")
        for point in item["footprint"]:
            if not in_grid(point):
                errors.append(f"object {item['id']!r} footprint is outside the room grid")
    for name, interaction in manifest["interactions"].items():
        if not in_grid(interaction["approach"]):
            errors.append(f"interaction {name!r} approach is outside the room grid")
        if interaction.get("posePosition") and not in_grid(interaction["posePosition"]):
            errors.append(f"interaction {name!r} pose is outside the room grid")
    for name, route in manifest["routes"].items():
        if any(not in_grid(point) for point in route):
            errors.append(f"route {name!r} is outside the room grid")
    if errors:
        raise RoomCompileError(errors)


def _validate_topology(manifest: dict[str, Any]) -> None:
    errors: list[str] = []
    walkable = {_xy(point) for point in manifest["walkable"]}
    blocked = {
        _xy(point)
        for item in manifest["objects"]
        for point in item["footprint"]
    }
    free = walkable - blocked
    entry = _xy(manifest["anchors"]["entry"])
    reachable: set[tuple[int | float, int | float]] = set()
    pending = [entry] if entry in free else []
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        x, y = current
        pending.extend(
            candidate
            for candidate in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            if candidate in free and candidate not in reachable
        )

    for name, interaction in manifest["interactions"].items():
        approach = _xy(interaction["approach"])
        if approach not in free:
            errors.append(
                f"interaction {name!r} approach is not on a free walkable tile"
            )
        elif approach not in reachable:
            errors.append(f"interaction {name!r} is unreachable from entry")

    for name, route in manifest["routes"].items():
        for point in route:
            if _xy(point) not in free:
                errors.append(f"route {name!r} crosses a blocked or non-walkable tile")
                break
        for previous, current in zip(route, route[1:], strict=False):
            if abs(previous[0] - current[0]) + abs(previous[1] - current[1]) != 1:
                errors.append(f"route {name!r} has a non-adjacent step")
                break

    if errors:
        raise RoomCompileError(errors)


def _emit_runtime(
    *,
    manifest: dict[str, Any],
    manifest_dir: Path,
    master: Image.Image,
    build_dir: Path,
) -> tuple[Path, ...]:
    runtime_images = {name: spec["url"] for name, spec in manifest["images"].items()}
    runtime_base_url = manifest["runtimeBaseUrl"].rstrip("/")
    runtime_objects: list[dict[str, Any]] = []
    generated_assets: list[Path] = []
    for source_object in manifest["objects"]:
        source_occluder = source_object["frontOccluder"]
        image_key = f"{source_object['id']}Front"
        runtime_images[image_key] = (
            f"{runtime_base_url}/occluders/{source_occluder['output']}"
        )
        runtime_object: dict[str, Any] = {
            "id": source_object["id"],
            "tile": source_object["depthTile"],
            "footprint": source_object["footprint"],
            "frontOccluder": {
                "image": image_key,
                "origin": source_occluder["origin"],
                "depthBias": source_occluder["depthBias"],
            },
            "audit": source_object["audit"],
            "auditPose": source_object.get("auditPose", {}),
        }
        if source_back := source_object.get("backLayer"):
            back_key = f"{source_object['id']}Back"
            runtime_images[back_key] = (
                f"{runtime_base_url}/layers/{source_back['output']}"
            )
            generated_assets.append(
                _build_layer(
                    master=master,
                    manifest_dir=manifest_dir,
                    output_dir=build_dir,
                    spec=source_back,
                    subdirectory="layers",
                )
            )
            runtime_object["backLayer"] = {
                "image": back_key,
                "origin": source_back["origin"],
            }
        generated_assets.append(
            _build_layer(
                master=master,
                manifest_dir=manifest_dir,
                output_dir=build_dir,
                spec=source_occluder,
                subdirectory="occluders",
            )
        )
        runtime_objects.append(runtime_object)

    bundle = {
        "schemaVersion": manifest["schemaVersion"],
        "id": manifest["id"],
        "canvas": manifest["canvas"],
        "grid": manifest["grid"],
        "tile": manifest["tile"],
        "images": runtime_images,
        "sprites": manifest["sprites"],
        "movement": manifest["movement"],
        "behavior": manifest["behavior"],
        "background": "room",
        "walkable": manifest["walkable"],
        "anchors": manifest["anchors"],
        "objects": runtime_objects,
        "interactions": manifest["interactions"],
        "routes": manifest["routes"],
        "axisAudits": manifest["axisAudits"],
    }
    (build_dir / "room.bundle.json").write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2) + "\n"
    )
    return tuple(path.relative_to(build_dir) for path in generated_assets)


def _replace_runtime(build_dir: Path, output_dir: Path) -> None:
    backup_dir = output_dir.with_name(f".{output_dir.name}.previous")
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    if output_dir.exists():
        output_dir.replace(backup_dir)
    try:
        build_dir.replace(output_dir)
    except Exception:
        if backup_dir.exists():
            backup_dir.replace(output_dir)
        raise
    if backup_dir.exists():
        shutil.rmtree(backup_dir)


def compile_room(manifest_path: Path | str, output_dir: Path | str) -> CompileReport:
    """Compile one room manifest and return its complete output report."""

    manifest_path = Path(manifest_path).resolve()
    output_dir = Path(output_dir).resolve()
    manifest = json.loads(manifest_path.read_text())
    manifest_dir = manifest_path.parent

    _validate_identity(manifest["objects"])
    _validate_sources_and_grid(manifest, manifest_dir)
    master_spec = manifest["images"]["room"]
    master = Image.open(_source_path(manifest_dir, master_spec["source"])).convert("RGBA")
    _validate_occluders(
        master=master,
        manifest_dir=manifest_dir,
        objects=manifest["objects"],
    )
    _validate_topology(manifest)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{output_dir.name}.build-", dir=output_dir.parent
    ) as temporary:
        build_dir = Path(temporary)
        generated = _emit_runtime(
            manifest=manifest,
            manifest_dir=manifest_dir,
            master=master,
            build_dir=build_dir,
        )
        _replace_runtime(build_dir, output_dir)
    return CompileReport(
        output_dir / "room.bundle.json",
        tuple(output_dir / relative for relative in generated),
    )
