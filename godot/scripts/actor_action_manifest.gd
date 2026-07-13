class_name ActorActionManifest
extends RefCounted

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
	if data.get("schema", "") != "actor-actions-v1":
		errors.append("schema must be actor-actions-v1")
	for action_name in data.get("actions", {}):
		var definition: Dictionary = data["actions"][action_name]
		var region: Array = definition.get("region", [])
		if region.size() != 4 or float(region[2]) <= 0.0 or float(region[3]) <= 0.0:
			errors.append("action %s needs a positive region" % action_name)
		if float(definition.get("canonical_scale", 0.0)) <= 0.0:
			errors.append("action %s needs canonical_scale" % action_name)
		var texture_path := String(definition.get("texture", ""))
		if texture_path.is_empty() or not FileAccess.file_exists(texture_path):
			errors.append("action %s references a missing texture" % action_name)
		if not definition.has("foot_pivot") and not definition.has("seat_pivot") and not definition.has("surface_pivot"):
			errors.append("action %s needs a pivot" % action_name)
		else:
			var pivot_value: Variant = definition.get("seat_pivot", definition.get("surface_pivot", definition.get("foot_pivot", [])))
			if not (pivot_value is Array) or pivot_value.size() != 2:
				errors.append("action %s pivot must contain two numbers" % action_name)
				continue
			var pivot := Vector2(float(pivot_value[0]), float(pivot_value[1]))
			if pivot.x < 0.0 or pivot.x > 1.0 or pivot.y < 0.0 or pivot.y > 1.0:
				errors.append("action %s pivot must be normalized" % action_name)
		if float(definition.get("world_extent", 0.0)) <= 0.0:
			errors.append("action %s needs world_extent" % action_name)
		if float(definition.get("body_reference_height", 0.0)) <= 0.0:
			errors.append("action %s needs body_reference_height" % action_name)
	return errors


func definition_for(action_name: String) -> Dictionary:
	var actions: Dictionary = data.get("actions", {})
	if actions.has(action_name):
		return actions[action_name]
	var direction := action_name.get_slice(".", 1)
	return actions.get("idle.%s" % direction, actions.get("idle.down_left", {}))


func pivot_kind_for(action_name: String) -> String:
	var definition := definition_for(action_name)
	if definition.has("seat_pivot"):
		return "seat"
	if definition.has("surface_pivot"):
		return "surface"
	return "foot"


func pixel_size_for(action_name: String) -> float:
	var definition := definition_for(action_name)
	if not definition.has("surface_pivot"):
		return float(data.get("canonical_body_world_height", 1.2656)) * float(definition.get("canonical_scale", 1.0)) / float(definition.get("body_reference_height", 128.0))
	var region: Array = definition.get("region", [0, 0, 1, 1])
	var axis := String(definition.get("extent_axis", "height"))
	var source_extent := float(region[2] if axis == "width" else region[3])
	return float(definition.get("world_extent", 1.0)) * float(definition.get("canonical_scale", 1.0)) / source_extent


func pivot_for(action_name: String) -> Vector2:
	var definition := definition_for(action_name)
	for key in ["seat_pivot", "surface_pivot", "foot_pivot"]:
		if definition.has(key):
			var pivot: Array = definition[key]
			return Vector2(float(pivot[0]), float(pivot[1]))
	return Vector2(0.5, 1.0)


func effect_for(action_name: String) -> String:
	return String(definition_for(action_name).get("effect", "none"))
