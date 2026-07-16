extends SceneTree

var failures := PackedStringArray()


func _init() -> void:
	var room := RoomManifest.new()
	_expect(room.load_manifest("res://scenes/zhizhi-home.json") == OK, "manifest loads")
	_expect(room.validate().is_empty(), "manifest validates")
	_expect(room.grid_size() == Vector2i(14, 12), "grid dimensions are stable")
	_expect(is_equal_approx(room.logical_to_world(Vector3(0, 0, 1)).y, RoomManifest.HEIGHT_UNIT), "logical height uses the orthographic conversion")
	_expect(ProjectSettings.get_setting("display/window/size/viewport_width") == 696, "pixel viewport width is stable")
	_expect(ProjectSettings.get_setting("display/window/size/viewport_height") == 543, "pixel viewport height is stable")
	_expect(ProjectSettings.get_setting("display/window/stretch/mode") == "viewport", "pixel viewport scales as a single image")
	_expect(room.data["projection"]["camera_azimuth"] == 45, "camera azimuth is fixed at 45 degrees")
	_expect(room.data["projection"]["camera_elevation"] == 30, "camera elevation is fixed at 30 degrees")
	var camera_offset := room.camera_offset()
	_expect(is_equal_approx(camera_offset.x, camera_offset.z), "camera azimuth produces equal X/Z offset")
	_expect(is_equal_approx(rad_to_deg(atan2(camera_offset.y, Vector2(camera_offset.x, camera_offset.z).length())), 30.0), "camera offset implements 30 degree elevation")
	_expect(room.data["render"]["filter"] == "nearest", "render manifest requires nearest-neighbor upscale")
	_expect(room.data["objects"].size() >= 45, "reference layout keeps a dense furniture and decor set")
	for required_interaction in ["study", "eat", "relax", "phone", "sleep", "wash", "tidy"]:
		_expect(room.data["interactions"].has(required_interaction), "%s interaction exists" % required_interaction)

	var actions := ActorActionManifest.new()
	_expect(actions.load_manifest("res://scenes/zhizhi-actions.json") == OK, "actor action manifest loads")
	_expect(actions.validate().is_empty(), "actor action manifest validates")
	for required_action in ["idle.down_left", "walk.down_left", "study.up_right", "eat.up_right", "relax.up_right", "phone.up_right", "sleep.up_left", "wash.up_left", "tidy.up_right"]:
		_expect(not actions.definition_for(required_action).is_empty(), "%s action definition exists" % required_action)
	var idle_definition := actions.definition_for("idle.down_left")
	_expect(is_equal_approx(float(idle_definition.get("canonical_scale", 0.0)), 1.0), "actor canonical scale is stable")
	_expect(actions.pivot_kind_for("study.up_right") == "seat", "study uses a seat pivot")
	_expect(actions.pivot_kind_for("sleep.up_left") == "surface", "sleep uses a bed surface pivot")
	_expect(actions.definition_for("unknown.down_left") == idle_definition, "unknown actions fall back to directional idle")
	_expect(is_equal_approx(actions.pixel_size_for("idle.down_left") * 128.0, 1.2656), "standing art uses logical-height world conversion")
	var interaction_effects := {}
	for action_key in ["study.up_right", "eat.up_right", "relax.up_right", "phone.up_right", "sleep.up_left", "wash.up_left", "tidy.up_right"]:
		interaction_effects[actions.effect_for(action_key)] = true
	_expect(interaction_effects.size() == 7, "seven interactions expose distinct local animations")

	var bridge := RoomStateBridge.new()
	var valid_body := JSON.stringify({"dashboard": {"scene": {"location": "desk", "action": "study"}}}).to_utf8_buffer()
	_expect(bridge.scene_state_from_body(valid_body) == {"location": "desk", "action": "study"}, "bridge extracts daemon scene state")
	_expect(bridge.scene_state_from_body("not-json".to_utf8_buffer()).is_empty(), "bridge rejects invalid JSON")
	_expect(bridge.scene_state_from_body(JSON.stringify({"dashboard": {}}).to_utf8_buffer()).is_empty(), "bridge rejects missing scene")
	var world_v2_body := JSON.stringify({"schema_version": "world-v2-dashboard-room.1", "cursor": {"world_revision": 7, "ledger_sequence": 12}, "projection_hash": "a".repeat(64), "route": {"scene_id": "zhizhi-home", "action_id": "study", "availability": "busy"}}).to_utf8_buffer()
	_expect(bridge.scene_state_from_public_room_body(world_v2_body) == {"location": "desk", "action": "study"}, "bridge maps a public World v2 route")
	var unavailable_body := JSON.stringify({"schema_version": "world-v2-dashboard-room.1", "cursor": {"world_revision": 7, "ledger_sequence": 12}, "projection_hash": "a".repeat(64), "route": {"scene_id": "unavailable", "action_id": "idle", "availability": "unavailable"}}).to_utf8_buffer()
	_expect(bridge.scene_state_from_public_room_body(unavailable_body).is_empty(), "bridge keeps prior scene for unavailable World v2 routes")
	_expect(bridge.scene_state_from_public_room_body(valid_body).is_empty(), "World v2 parser never falls back to archived dashboard data")

	var navigation := NavigationGrid.new()
	navigation.configure(room)
	var entry := room.anchor_for("entry")
	var reachable := navigation.reachable_from(entry)
	for interaction_name in room.data["interactions"]:
		var interaction: Dictionary = room.data["interactions"][interaction_name]
		var approach: Array = interaction["approach"]
		_expect(reachable.has(Vector2i(int(approach[0]), int(approach[1]))), "%s is reachable from entry" % interaction_name)
	for object_value in room.data["objects"]:
		var object: Dictionary = object_value
		if object.get("occupancy", "solid") != "solid":
			continue
		var declared: Dictionary = {}
		for tile_value in object.get("footprint", []):
			declared[Vector2i(int(tile_value[0]), int(tile_value[1]))] = true
		_expect(declared == room.footprint_for(object), "%s footprint matches its visible ground mesh" % object["id"])

	var isolated := RoomManifest.new()
	isolation_fixture(room, isolated)
	var isolated_navigation := NavigationGrid.new()
	isolation_fixture_navigation(isolated, isolated_navigation)
	_expect(isolated_navigation.find_path(Vector2i(5, 6), Vector2i(6, 6)).is_empty(), "interior wall blocks only its crossing")
	_expect(not isolated_navigation.blocked.has(Vector2i(6, 1)), "wall does not consume adjacent floor tiles")

	if failures.is_empty():
		print("Godot room tests passed")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


func isolation_fixture(source: RoomManifest, target: RoomManifest) -> void:
	target.data = source.data.duplicate(true)
	target.data["walls"].append({"id": "test-inner", "from": [6, 0], "to": [6, 12], "height": 2.0, "material": "cream"})


func isolation_fixture_navigation(room: RoomManifest, navigation: NavigationGrid) -> void:
	navigation.configure(room)


func _expect(condition: bool, message: String) -> void:
	if not condition:
		failures.append("FAIL: %s" % message)
