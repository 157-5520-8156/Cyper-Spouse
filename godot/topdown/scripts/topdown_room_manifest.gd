class_name TopdownRoomManifest
extends RefCounted

var data: Dictionary = {}


func load_manifest(path: String) -> Error:
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return FileAccess.get_open_error()
	var parser := JSON.new()
	if parser.parse(file.get_as_text()) != OK or not (parser.data is Dictionary):
		return ERR_PARSE_ERROR
	data = parser.data
	return OK


func validate() -> PackedStringArray:
	var errors := PackedStringArray()
	if data.get("schema") != "topdown-room-v1":
		errors.append("topdown manifest schema must be topdown-room-v1")
	var grid: Dictionary = data.get("grid", {})
	if int(grid.get("width", 0)) <= 0 or int(grid.get("height", 0)) <= 0:
		errors.append("topdown grid must have positive dimensions")
	var objects_by_id := {}
	for object_value in data.get("objects", []):
		if not (object_value is Dictionary):
			errors.append("topdown objects must be dictionaries")
			continue
		var object: Dictionary = object_value
		var id := String(object.get("id", ""))
		if id.is_empty() or objects_by_id.has(id):
			errors.append("topdown objects need unique ids")
		objects_by_id[id] = true
		if int(object.get("width", 0)) <= 0 or int(object.get("height", 0)) <= 0:
			errors.append("%s needs a positive footprint" % id)
		for tile in tiles_for(object):
			if not in_bounds(tile):
				errors.append("%s has an out-of-bounds footprint" % id)
	for interaction_name in data.get("interactions", {}):
		var interaction: Dictionary = data["interactions"][interaction_name]
		if not objects_by_id.has(String(interaction.get("object", ""))):
			errors.append("%s references an unknown object" % interaction_name)
		var approach: Array = interaction.get("approach", [])
		if approach.size() != 2 or not in_bounds(Vector2i(int(approach[0]), int(approach[1]))):
			errors.append("%s needs an in-bounds approach tile" % interaction_name)
	return errors


func grid_size() -> Vector2i:
	var grid: Dictionary = data.get("grid", {})
	return Vector2i(int(grid.get("width", 0)), int(grid.get("height", 0)))


func tile_size() -> int:
	return int(data.get("grid", {}).get("tile_size", 32))


func origin() -> Vector2:
	var values: Array = data.get("grid", {}).get("origin", [0, 0])
	return Vector2(float(values[0]), float(values[1]))


func tile_to_screen(tile: Vector2) -> Vector2:
	return origin() + (tile + Vector2(0.5, 0.5)) * tile_size()


func anchor_for(name: String) -> Vector2i:
	var values: Array = data.get("anchors", {}).get(name, data.get("anchors", {}).get("entry", [0, 0]))
	return Vector2i(int(values[0]), int(values[1]))


func interaction_for(scene_state: Dictionary) -> Dictionary:
	var location := String(scene_state.get("location", ""))
	var action := String(scene_state.get("action", ""))
	# An explicit daemon action is more specific than a broad location. For example,
	# `phone` in the living room must select the phone pose, not generic relaxation.
	for interaction_name in data.get("interactions", {}):
		var interaction: Dictionary = data["interactions"][interaction_name]
		if action in interaction.get("actions", []):
			return interaction.duplicate(true)
	for interaction_name in data.get("interactions", {}):
		var interaction: Dictionary = data["interactions"][interaction_name]
		if location in interaction.get("locations", []):
			return interaction.duplicate(true)
	return {}


func tiles_for(object: Dictionary) -> Array[Vector2i]:
	var tiles: Array[Vector2i] = []
	for y in range(int(object.get("y", 0)), int(object.get("y", 0)) + int(object.get("height", 1))):
		for x in range(int(object.get("x", 0)), int(object.get("x", 0)) + int(object.get("width", 1))):
			tiles.append(Vector2i(x, y))
	return tiles


func in_bounds(tile: Vector2i) -> bool:
	var size := grid_size()
	return tile.x >= 0 and tile.y >= 0 and tile.x < size.x and tile.y < size.y
