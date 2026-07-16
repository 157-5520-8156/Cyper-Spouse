class_name RoomStateBridge
extends RefCounted


const WORLD_V2_ROOM_SCHEMA := "world-v2-dashboard-room.1"
const _KNOWN_SCENES := ["zhizhi-home", "zhizhi-home-legacy"]
const _PUBLIC_AVAILABILITY := ["available", "busy", "do_not_disturb", "recovering", "active", "paused", "scheduled"]
const _ACTION_LOCATIONS := {
	"idle": "rug",
	"study": "desk",
	"eat": "kitchen",
	"relax": "sofa",
	"phone": "living",
	"read_phone": "living",
	"sleep": "bed",
	"wash": "vanity",
	"tidy": "desk",
}


func scene_state_from_public_room_body(body: PackedByteArray) -> Dictionary:
	"""Decode the narrow public World v2 DTO into local renderer state.

	This is deliberately a one-way presentation adapter.  It does not accept
	the archived dashboard context shape, and it drops unknown routes rather
	than making a visual claim from an internal or future field.
	"""
	var parser := JSON.new()
	if parser.parse(body.get_string_from_utf8()) != OK:
		return {}
	var payload: Variant = parser.data
	if not (payload is Dictionary):
		return {}
	if payload.get("schema_version") != WORLD_V2_ROOM_SCHEMA:
		return {}
	var cursor: Variant = payload.get("cursor", {})
	var route: Variant = payload.get("route", {})
	if not (cursor is Dictionary) or not (route is Dictionary):
		return {}
	if not (cursor.has("world_revision") and cursor.has("ledger_sequence")):
		return {}
	if not (route.has("scene_id") and route.has("action_id") and route.has("availability")):
		return {}
	var scene_id := String(route["scene_id"])
	var action_id := String(route["action_id"])
	var availability := String(route["availability"])
	if not _PUBLIC_AVAILABILITY.has(availability) or not _KNOWN_SCENES.has(scene_id):
		return {}
	if not _ACTION_LOCATIONS.has(action_id):
		return {}
	return {"location": _ACTION_LOCATIONS[action_id], "action": action_id}


func scene_state_from_body(body: PackedByteArray) -> Dictionary:
	"""Decode the archived dashboard shape for explicit legacy visual tests.

	New scene polling calls ``scene_state_from_public_room_body`` instead.  The
	compatibility parser is retained only for the archived 3D comparison scene
	and its fixture tests; it is never a fallback after a v2 response fails.
	"""
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
