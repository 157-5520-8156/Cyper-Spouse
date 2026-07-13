class_name RoomManifest
extends RefCounted

const HEIGHT_UNIT := 0.816496
const SUPPORTED_KINDS := [
	"bed", "bookcase", "books", "cabinet", "chair", "console", "counter", "curtain",
	"cushion", "desk", "divider", "fridge", "lamp", "laptop", "plant", "rug", "shelf",
	"sofa", "table", "tea_set", "vanity", "wall_art", "window",
]

var data: Dictionary = {}


func load_manifest(path: String) -> Error:
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return FileAccess.get_open_error()
	var parsed: Variant = JSON.parse_string(file.get_as_text())
	if not (parsed is Dictionary):
		return ERR_PARSE_ERROR
	data = parsed
	return OK


func validate() -> PackedStringArray:
	var errors := PackedStringArray()
	var grid: Dictionary = data.get("grid", {})
	if int(grid.get("width", 0)) <= 0 or int(grid.get("depth", 0)) <= 0:
		errors.append("grid must define positive width/depth")
	if data.get("schema", "") != "godot-room-v1":
		errors.append("schema must be godot-room-v1")
	for object_value in data.get("objects", []):
		if not (object_value is Dictionary):
			errors.append("object must be a dictionary")
			continue
		var object: Dictionary = object_value
		if String(object.get("kind", "")) not in SUPPORTED_KINDS:
			errors.append("object %s has unsupported kind" % object.get("id", "<unknown>"))
		var transform: Dictionary = object.get("transform", {})
		for key in ["x", "y", "z", "width", "depth", "height"]:
			if not transform.has(key) or not (transform[key] is float or transform[key] is int):
				errors.append("object %s has invalid %s" % [object.get("id", "<unknown>"), key])
		if object.get("occupancy", "solid") == "solid":
			var declared: Dictionary = {}
			for tile_value in object.get("footprint", []):
				declared[Vector2i(int(tile_value[0]), int(tile_value[1]))] = true
			var derived: Dictionary = footprint_for(object)
			if declared != derived:
				errors.append("object %s footprint must match its ground mesh" % object.get("id", "<unknown>"))
	for interaction_name in data.get("interactions", {}):
		var interaction: Dictionary = data["interactions"][interaction_name]
		if not interaction.has("approach") or not interaction.has("pose_anchor"):
			errors.append("interaction %s needs approach and pose_anchor" % interaction_name)
	return errors


func grid_size() -> Vector2i:
	var grid: Dictionary = data.get("grid", {})
	return Vector2i(int(grid.get("width", 0)), int(grid.get("depth", 0)))


func logical_to_world(logical: Vector3) -> Vector3:
	return Vector3(logical.x, logical.z * HEIGHT_UNIT, logical.y)


func floor_position(tile: Vector2) -> Vector3:
	return logical_to_world(Vector3(tile.x, tile.y, 0.0))


func interaction_for(scene_state: Dictionary) -> Dictionary:
	var location := String(scene_state.get("location", ""))
	var action := String(scene_state.get("action", "idle"))
	for interaction_value in data.get("interactions", {}).values():
		var interaction: Dictionary = interaction_value
		if interaction.get("location", "") == location and action in interaction.get("actions", []):
			return interaction
	return {}


func anchor_for(location: String) -> Vector2i:
	var anchor: Variant = data.get("anchors", {}).get(location, data.get("anchors", {}).get("rug", [0, 0]))
	return Vector2i(int(anchor[0]), int(anchor[1]))


func footprint_for(object: Dictionary) -> Dictionary:
	var transform: Dictionary = object.get("transform", {})
	if object.get("occupancy", "solid") != "solid":
		return {}
	var cells: Dictionary = {}
	var start_x := floori(float(transform.get("x", 0.0)))
	var end_x := ceili(float(transform.get("x", 0.0)) + float(transform.get("width", 0.0)))
	var start_y := floori(float(transform.get("y", 0.0)))
	var end_y := ceili(float(transform.get("y", 0.0)) + float(transform.get("depth", 0.0)))
	for x in range(start_x, end_x):
		for y in range(start_y, end_y):
			cells[Vector2i(x, y)] = true
	return cells


func camera_offset(horizontal_radius: float = 16.970563) -> Vector3:
	var projection: Dictionary = data.get("projection", {})
	var azimuth := deg_to_rad(float(projection.get("camera_azimuth", 45.0)))
	var elevation := deg_to_rad(float(projection.get("camera_elevation", 30.0)))
	return Vector3(
		sin(azimuth) * horizontal_radius,
		tan(elevation) * horizontal_radius,
		cos(azimuth) * horizontal_radius,
	)
