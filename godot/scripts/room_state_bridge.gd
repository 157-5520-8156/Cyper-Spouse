class_name RoomStateBridge
extends RefCounted


func scene_state_from_body(body: PackedByteArray) -> Dictionary:
	var parser := JSON.new()
	if parser.parse(body.get_string_from_utf8()) != OK:
		return {}
	var payload: Variant = parser.data
	if not (payload is Dictionary):
		return {}
	var dashboard: Variant = payload.get("dashboard", {})
	if not (dashboard is Dictionary):
		return {}
	var scene_state: Variant = dashboard.get("scene", {})
	if not (scene_state is Dictionary):
		return {}
	return scene_state.duplicate(true)
