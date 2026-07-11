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


OBJECT_CATEGORIES = {
    "appliance", "decor-cluster", "furniture", "lighting", "plant",
    "soft-furnishing", "structural-sub-layer", "wall-decoration",
}
OCCUPANCY_KINDS = {"footprint", "wall", "none"}
LAYER_ROLES = {"shadow", "back", "body", "front", "light"}
ROLE_DEPTH_BIAS = {"shadow": -300, "back": -200, "body": 0, "front": 500, "light": 1000}


def _legacy_layer_specs(item: dict[str, Any]) -> list[dict[str, Any]]:
    layers: list[dict[str, Any]] = []
    if back := item.get("backLayer"):
        layers.append({"role": "back", "depthBias": -200, **back})
    if front := item.get("frontOccluder"):
        layers.append({"role": "front", **front})
    return layers


def _normalize_objects(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one runtime-facing object model while accepting the old source seam."""

    interactions_by_object: dict[str, list[str]] = {}
    for name, interaction in manifest["interactions"].items():
        interactions_by_object.setdefault(interaction["object"], []).append(name)
    normalized: list[dict[str, Any]] = []
    errors: list[str] = []
    for source in manifest["objects"]:
        object_id = source.get("id", "<unknown>")
        generic = "layers" in source or "occupancy" in source or "assetMode" in source
        if generic:
            if "provenance" not in source:
                errors.append(f"object {object_id!r} must define provenance")
            if source.get("category") not in OBJECT_CATEGORIES:
                errors.append(f"object {object_id!r} has invalid category {source.get('category')!r}")
            occupancy = source.get("occupancy", {})
            if occupancy.get("kind") not in OCCUPANCY_KINDS:
                errors.append(
                    f"object {object_id!r} has invalid occupancy kind {occupancy.get('kind')!r}"
                )
            tiles = occupancy.get("tiles", [])
            if occupancy.get("kind") == "footprint" and not tiles:
                errors.append(f"object {object_id!r} footprint occupancy must define tiles")
            if occupancy.get("kind") in {"wall", "none"} and tiles:
                errors.append(f"object {object_id!r} non-footprint occupancy cannot define tiles")
            layers = source.get("layers", [])
            if not layers:
                errors.append(f"object {object_id!r} must define at least one layer")
            for layer in layers:
                if layer.get("role") not in LAYER_ROLES:
                    errors.append(
                        f"object {object_id!r} has invalid layer role {layer.get('role')!r}"
                    )
            audits = source.get("audits", {})
            if audits.get("hidden") is not True or audits.get("solo") is not True:
                errors.append(f"object {object_id!r} must support hidden/solo audits")
            provenance = source.get("provenance", {})
            if provenance and (not provenance.get("method") or not provenance.get("reference")):
                errors.append(f"object {object_id!r} provenance must define method/reference")
            normalized.append({
                **source,
                "interactions": list(source.get("interactions", [])),
                "auditPose": source.get("auditPose", {}),
            })
            continue

        normalized.append({
            "id": object_id,
            "category": "furniture",
            "assetMode": "legacy-master-matte",
            "depthTile": source["depthTile"],
            "occupancy": {"kind": "footprint", "tiles": source["footprint"]},
            "layers": _legacy_layer_specs(source),
            "interactions": interactions_by_object.get(object_id, []),
            "audits": {"hidden": True, "solo": True, "behind": True, "front": True},
            "provenance": {
                "method": "legacy-master-matte",
                "reference": "zhizhi-room-isometric-v2.png",
            },
            "audit": source["audit"],
            "auditPose": source.get("auditPose", {}),
        })
    if errors:
        raise RoomCompileError(errors)
    return normalized


def _image_key(object_id: str, role: str, index: int) -> str:
    stem = "".join(part.capitalize() for part in object_id.split("-"))
    return f"{stem[0].lower() + stem[1:]}{role.capitalize()}{index}"


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


def _validate_layers(
    *, master: Image.Image, manifest_dir: Path, objects: list[dict[str, Any]]
) -> None:
    errors: list[str] = []
    for item in objects:
        for spec in item["layers"]:
            role = f"{spec['role']} layer"
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

        for spec in item["layers"]:
            output = spec["output"]
            if output in outputs:
                errors.append(f"duplicate layer output {output!r}")
            outputs.add(output)

        for point in item["occupancy"].get("tiles", []):
            tile = _xy(point)
            if owner := footprint_owners.get(tile):
                errors.append(
                    f"footprint tile {tile} is claimed by both {owner!r} and {object_id!r}"
                )
            else:
                footprint_owners[tile] = object_id
    if errors:
        raise RoomCompileError(errors)


def _validate_attachments(objects: list[dict[str, Any]], label: str = "") -> None:
    errors: list[str] = []
    by_id = {item["id"]: item for item in objects}
    for item in objects:
        parent = item.get("attachedTo")
        if parent and parent not in by_id:
            errors.append(f"{label}object {item['id']!r} attaches to unknown object {parent!r}")
    for item in objects:
        seen: set[str] = set()
        current = item
        while parent := current.get("attachedTo"):
            if parent in seen or parent == item["id"]:
                errors.append(f"{label}attachment cycle contains {item['id']!r}")
                break
            seen.add(parent)
            if parent not in by_id:
                break
            current = by_id[parent]
    if errors:
        raise RoomCompileError(errors)


def _xy(point: list[int | float]) -> tuple[int | float, int | float]:
    return point[0], point[1]


def _validate_sources_and_grid(
    manifest: dict[str, Any], manifest_dir: Path
) -> None:
    errors: list[str] = []
    all_objects = [*manifest["objects"], *manifest.get("_draftObjects", [])]
    for name, image in manifest["images"].items():
        if not _source_path(manifest_dir, image["source"]).is_file():
            errors.append(f"image {name!r} source does not exist")
    for item in all_objects:
        for spec in item["layers"]:
            role = f"{spec['role']} layer"
            source_value = spec.get("source") or spec.get("matte")
            if not source_value or not _source_path(manifest_dir, source_value).is_file():
                errors.append(f"object {item['id']!r} {role} source does not exist")

    object_ids = {item["id"] for item in manifest["objects"]}
    all_object_ids = {item["id"] for item in all_objects}
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
        for object_id in interaction.get("allowOccupiedBy", []):
            if object_id not in all_object_ids:
                errors.append(f"interaction {name!r} allows unknown occupancy object {object_id!r}")
        depth = interaction.get("depth")
        if isinstance(depth, dict) and depth.get("relativeTo") not in object_ids:
            errors.append(
                f"interaction {name!r} depth references unknown object {depth.get('relativeTo')!r}"
            )
    for item in all_objects:
        required_audits = {
            side for side in ("behind", "front") if item.get("audits", {}).get(side)
        }
        missing_audits = required_audits - set(item.get("audit", {}))
        if missing_audits:
            errors.append(
                f"object {item['id']!r} must define audit positions for "
                f"{', '.join(sorted(missing_audits))}"
            )
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
    for item in all_objects:
        if not in_grid(item["depthTile"]):
            errors.append(f"object {item['id']!r} depth tile is outside the room grid")
        for point in item["occupancy"].get("tiles", []):
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


def _validate_topology(manifest: dict[str, Any], objects: list[dict[str, Any]] | None = None, label: str = "") -> None:
    errors: list[str] = []
    objects = manifest["objects"] if objects is None else objects
    walkable = {_xy(point) for point in manifest["walkable"]}
    blocked_owners = {_xy(point): item["id"] for item in objects for point in item["occupancy"].get("tiles", [])}
    blocked = set(blocked_owners)
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
        owner = blocked_owners.get(approach)
        allowed = set(interaction.get("allowOccupiedBy", []))
        if owner and owner not in allowed:
            errors.append(f"{label}interaction {name!r} approach is occupied by {owner!r}")
        elif approach not in walkable:
            errors.append(f"{label}interaction {name!r} approach is not on a free walkable tile")
        elif owner:
            x, y = approach
            neighbors = ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            if approach != entry and not any(point in reachable for point in neighbors):
                errors.append(f"{label}interaction {name!r} is unreachable from entry")
        elif approach not in reachable:
            errors.append(f"{label}interaction {name!r} is unreachable from entry")

    for name, route in manifest["routes"].items():
        for point in route:
            if _xy(point) not in free:
                errors.append(f"{label}route {name!r} crosses a blocked or non-walkable tile")
                break
        for previous, current in zip(route, route[1:], strict=False):
            if abs(previous[0] - current[0]) + abs(previous[1] - current[1]) != 1:
                errors.append(f"{label}route {name!r} has a non-adjacent step")
                break

    if errors:
        raise RoomCompileError(errors)


def _load_inventory(
    manifest: dict[str, Any], manifest_dir: Path
) -> dict[str, Any]:
    inventory_path = _source_path(manifest_dir, manifest["inventory"])
    if not inventory_path.is_file():
        raise RoomCompileError(["room asset inventory does not exist"])
    inventory = json.loads(inventory_path.read_text())
    errors: list[str] = []
    if inventory.get("roomId") != manifest["id"]:
        errors.append("room asset inventory belongs to a different room")
    allowed_statuses = set(inventory.get("statusValues", ()))
    item_ids: set[str] = set()
    counts = {status: 0 for status in ("planned", "partial", "atomized", "verified")}
    for item in inventory.get("items", ()):
        item_id = item["id"]
        if item_id in item_ids:
            errors.append(f"duplicate inventory item {item_id!r}")
        item_ids.add(item_id)
        status = item.get("status")
        if status not in allowed_statuses or status not in counts:
            errors.append(f"inventory item {item_id!r} has invalid status {status!r}")
        else:
            counts[status] += 1
    object_ids = {item["id"] for item in manifest["objects"]}
    for object_id in sorted(object_ids - item_ids):
        errors.append(f"room object {object_id!r} has no inventory owner")
    for item in inventory.get("items", ()):
        if item.get("status") in {"partial", "atomized", "verified"} and item["id"] not in object_ids:
            errors.append(
                f"inventory item {item['id']!r} is {item['status']} but has no room object"
            )
    if errors:
        raise RoomCompileError(errors)
    return {
        "items": inventory["items"],
        "shellAllowed": inventory.get("shellAllowed", []),
        "summary": {"total": len(inventory["items"]), **counts},
    }


def _emit_runtime(
    *,
    manifest: dict[str, Any],
    manifest_dir: Path,
    master: Image.Image,
    build_dir: Path,
    inventory: dict[str, Any],
) -> tuple[Path, ...]:
    draft_shell_image = manifest.get("artDraft", {}).get("shell", {}).get("image")
    runtime_images = {
        name: spec["url"]
        for name, spec in manifest["images"].items()
        if name != draft_shell_image
    }
    draft_images = (
        {draft_shell_image: manifest["images"][draft_shell_image]["url"]}
        if draft_shell_image else {}
    )
    runtime_base_url = manifest["runtimeBaseUrl"].rstrip("/")
    generated_assets: list[Path] = []
    def emit_objects(
        source_objects: list[dict[str, Any]],
        subdirectory: str,
        image_map: dict[str, str],
        key_suffix: str = "",
    ) -> list[dict[str, Any]]:
        runtime_objects: list[dict[str, Any]] = []
        for source_object in source_objects:
            runtime_object: dict[str, Any] = {
                "id": source_object["id"],
                "category": source_object["category"],
                "assetMode": source_object["assetMode"],
                "tile": source_object["depthTile"],
                "occupancy": source_object["occupancy"],
                "layers": [],
                "interactions": source_object["interactions"],
                "audits": source_object["audits"],
                "provenance": source_object["provenance"],
                "audit": source_object.get("audit", {}),
                "auditPose": source_object.get("auditPose", {}),
            }
            if attached_to := source_object.get("attachedTo"):
                runtime_object["attachedTo"] = attached_to
            for index, source_layer in enumerate(source_object["layers"]):
                role = source_layer["role"]
                image_key = f"{_image_key(source_object['id'], role, index)}{key_suffix}"
                image_map[image_key] = (
                    f"{runtime_base_url}/{subdirectory}/{source_layer['output']}"
                )
                generated_assets.append(
                    _build_layer(
                        master=master,
                        manifest_dir=manifest_dir,
                        output_dir=build_dir,
                        spec=source_layer,
                        subdirectory=subdirectory,
                    )
                )
                runtime_object["layers"].append({
                    "role": role,
                    "image": image_key,
                    "origin": source_layer["origin"],
                    "depthBias": source_layer.get("depthBias", ROLE_DEPTH_BIAS[role]),
                })
            runtime_objects.append(runtime_object)
        return runtime_objects

    runtime_objects = emit_objects(manifest["objects"], "layers", runtime_images)
    draft_objects = emit_objects(
        manifest.get("_draftObjects", []), "draft-layers", draft_images, "Draft"
    )

    bundle = {
        "schemaVersion": manifest["schemaVersion"],
        "id": manifest["id"],
        "canvas": manifest["canvas"],
        "grid": manifest["grid"],
        "tile": manifest["tile"],
        "images": runtime_images,
        "sprites": manifest["sprites"],
        "movement": manifest["movement"],
        "shell": manifest["shell"],
        "inventory": inventory,
        "behavior": manifest["behavior"],
        "background": manifest["shell"]["image"],
        "walkable": manifest["walkable"],
        "anchors": manifest["anchors"],
        "objects": runtime_objects,
        "interactions": manifest["interactions"],
        "routes": manifest["routes"],
        "axisAudits": manifest["axisAudits"],
    }
    if art_draft := manifest.get("artDraft"):
        bundle["artDraft"] = {
            "background": art_draft["shell"]["image"],
            "status": art_draft["status"],
            "images": draft_images,
            "objects": draft_objects,
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

    manifest = {**manifest, "objects": _normalize_objects(manifest)}
    if art_draft := manifest.get("artDraft"):
        draft_objects = _normalize_objects(
            {**manifest, "objects": art_draft.get("objects", [])}
        )
        manifest = {**manifest, "_draftObjects": draft_objects}
    _validate_identity(manifest["objects"])
    _validate_attachments(manifest["objects"])
    if manifest.get("_draftObjects"):
        _validate_identity(manifest["_draftObjects"])
        _validate_attachments(manifest["_draftObjects"], "art draft ")
    inventory = _load_inventory(manifest, manifest_dir)
    inventory_ids = {item["id"] for item in inventory["items"]}
    missing_draft_owners = {
        item["id"] for item in manifest.get("_draftObjects", [])
    } - inventory_ids
    if missing_draft_owners:
        raise RoomCompileError([
            f"draft object {object_id!r} has no inventory owner"
            for object_id in sorted(missing_draft_owners)
        ])
    _validate_sources_and_grid(manifest, manifest_dir)
    master_spec = manifest["images"]["room"]
    master = Image.open(_source_path(manifest_dir, master_spec["source"])).convert("RGBA")
    shell_image = manifest["shell"].get("image")
    if shell_image not in manifest["images"]:
        raise RoomCompileError([f"shell references unknown image {shell_image!r}"])
    shell = Image.open(_source_path(manifest_dir, manifest["images"][shell_image]["source"]))
    if shell.size != master.size:
        raise RoomCompileError(
            [f"shell image size {shell.size} must match visual master {master.size}"]
        )
    if art_draft := manifest.get("artDraft"):
        draft_shell_image = art_draft.get("shell", {}).get("image")
        if draft_shell_image not in manifest["images"]:
            raise RoomCompileError(
                [f"art draft shell references unknown image {draft_shell_image!r}"]
            )
        draft_shell = Image.open(
            _source_path(manifest_dir, manifest["images"][draft_shell_image]["source"])
        )
        if draft_shell.size != master.size:
            raise RoomCompileError([
                f"art draft shell image size {draft_shell.size} "
                f"must match visual master {master.size}"
            ])
    _validate_layers(
        master=master,
        manifest_dir=manifest_dir,
        objects=[*manifest["objects"], *manifest.get("_draftObjects", [])],
    )
    _validate_topology(manifest)
    if manifest.get("_draftObjects"):
        _validate_topology(manifest, objects=manifest["_draftObjects"], label="art draft ")
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
            inventory=inventory,
        )
        _replace_runtime(build_dir, output_dir)
    return CompileReport(
        output_dir / "room.bundle.json",
        tuple(output_dir / relative for relative in generated),
    )
