extends SceneTree

var failures := PackedStringArray()


func _init() -> void:
	var room := TopdownRoomManifest.new()
	_expect(room.load_manifest("res://topdown/scenes/zhizhi-home-topdown.json") == OK, "top-down manifest loads")
	_expect(room.validate().is_empty(), "top-down manifest validates")
	_expect(room.grid_size() == Vector2i(18, 13), "top-down grid dimensions are stable")
	var navigation := TopdownNavigation.new()
	navigation.configure(room)
	_expect(navigation.blocked.has(Vector2i(2, 2)), "desk footprint is blocked")
	_expect(not navigation.blocked.has(Vector2i(1, 3)), "rugs do not block navigation")
	var reachable := navigation.reachable_from(room.anchor_for("entry"))
	for interaction_name in room.data["interactions"]:
		var interaction: Dictionary = room.data["interactions"][interaction_name]
		var approach: Array = interaction["approach"]
		_expect(reachable.has(Vector2i(int(approach[0]), int(approach[1]))), "%s approach is reachable" % interaction_name)
	var path := navigation.find_path(room.anchor_for("entry"), Vector2i(4, 3))
	_expect(not path.is_empty(), "actor can route from entry to desk approach")
	_expect(not path.has(Vector2i(2, 2)), "path never crosses a furniture footprint")
	var bridge := RoomStateBridge.new()
	var state := bridge.scene_state_from_body(JSON.stringify({"dashboard": {"scene": {"location": "desk", "action": "study"}}}).to_utf8_buffer())
	_expect(room.interaction_for(state).get("object") == "desk", "daemon study state maps to desk interaction")
	var world_v2_state := bridge.scene_state_from_public_room_body(JSON.stringify({"schema_version": "world-v2-dashboard-room.1", "cursor": {"world_revision": 7, "ledger_sequence": 12}, "projection_hash": "a".repeat(64), "route": {"scene_id": "zhizhi-home", "action_id": "study", "availability": "busy"}}).to_utf8_buffer())
	_expect(room.interaction_for(world_v2_state).get("object") == "desk", "public World v2 study route maps to desk interaction")
	_expect(room.interaction_for({"location": "living", "action": "phone"}).get("object") == "sofa", "explicit phone action wins over living fallback")
	if failures.is_empty():
		print("Top-down room tests passed")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


func _expect(condition: bool, message: String) -> void:
	if not condition:
		failures.append("FAIL: %s" % message)
