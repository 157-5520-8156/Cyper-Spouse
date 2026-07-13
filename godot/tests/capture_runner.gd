extends SceneTree

const MAIN_SCENE := preload("res://scenes/main.tscn")


func _init() -> void:
	call_deferred("_capture")


func _capture() -> void:
	var options := _options()
	var output_path := String(options.get("output", "user://godot-room.png"))
	var state_name := String(options.get("state", "full-room"))
	ProjectSettings.set_setting("zhizhi/capture_mode", true)
	var scene := MAIN_SCENE.instantiate()
	root.add_child(scene)
	await process_frame
	await process_frame
	var hud := scene.get_node_or_null("Hud")
	if hud != null:
		hud.visible = false
	var scene_state := _scene_state_for(state_name)
	if not scene_state.is_empty():
		scene.actor.snap_scene_state(scene_state)
		scene._apply_time_of_day(String(scene_state.get("time_of_day", "evening")))
	var preview := _preview_for(state_name)
	if not preview.is_empty():
		var logical: Array = preview["logical"]
		scene.actor.snap_preview(Vector3(float(logical[0]), float(logical[1]), float(logical[2])), String(preview["facing"]))
	var camera_view := _camera_view_for(state_name)
	if not camera_view.is_empty():
		var center: Array = camera_view["center"]
		var target: Vector3 = scene.room_manifest.floor_position(Vector2(float(center[0]), float(center[1])))
		scene.camera.position = target + scene.room_manifest.camera_offset()
		scene.camera.look_at(target, Vector3.UP)
		scene.camera.size = float(camera_view["size"])
	for _frame in range(8):
		await process_frame
	await RenderingServer.frame_post_draw
	var image := root.get_texture().get_image()
	var directory := output_path.get_base_dir()
	if not directory.is_empty():
		DirAccess.make_dir_recursive_absolute(directory)
	var save_error := image.save_png(output_path)
	if save_error != OK:
		push_error("Unable to save capture: %s" % output_path)
		quit(1)
		return
	print("capture:%s:%dx%d" % [output_path, image.get_width(), image.get_height()])
	scene.queue_free()
	await process_frame
	quit(0)


func _options() -> Dictionary:
	var result := {}
	var arguments := OS.get_cmdline_user_args()
	var index := 0
	while index < arguments.size():
		var key := String(arguments[index]).trim_prefix("--")
		if index + 1 < arguments.size():
			result[key] = arguments[index + 1]
		index += 2
	return result


func _scene_state_for(state_name: String) -> Dictionary:
	return {
		"full-room": {"location": "rug", "action": "idle", "time_of_day": "evening"},
		"study": {"location": "desk", "action": "study", "time_of_day": "evening"},
		"eat": {"location": "kitchen", "action": "eat", "time_of_day": "evening"},
		"relax": {"location": "sofa", "action": "relax", "time_of_day": "evening"},
		"phone": {"location": "sofa", "action": "read_phone", "time_of_day": "evening"},
		"sleep": {"location": "bed", "action": "sleep", "time_of_day": "night"},
		"wash": {"location": "vanity", "action": "wash", "time_of_day": "evening"},
		"tidy": {"location": "desk", "action": "tidy", "time_of_day": "evening"},
	}.get(state_name, {})


func _preview_for(state_name: String) -> Dictionary:
	return {
		"desk-front": {"logical": [3.9, 8.0, 0.0], "facing": "up_right"},
		"desk-back": {"logical": [2.0, 6.0, 0.0], "facing": "down_left"},
		"sofa-front": {"logical": [5.5, 10.1, 0.0], "facing": "up_right"},
		"sofa-back": {"logical": [5.5, 8.3, 0.0], "facing": "down_left"},
		"bed-front": {"logical": [10.5, 8.0, 0.0], "facing": "up_right"},
		"bed-back": {"logical": [10.5, 4.7, 0.0], "facing": "down_left"},
		"shelf-front": {"logical": [1.2, 3.3, 0.0], "facing": "up_right"},
		"shelf-back": {"logical": [0.2, 1.8, 0.0], "facing": "down_left"},
	}.get(state_name, {})


func _camera_view_for(state_name: String) -> Dictionary:
	return {
		"zone-work": {"center": [2.2, 7.1], "size": 7.5},
		"zone-kitchen": {"center": [4.8, 1.8], "size": 7.5},
		"zone-living": {"center": [5.4, 9.5], "size": 7.5},
		"zone-bedroom": {"center": [10.7, 6.5], "size": 7.5},
		"zone-window": {"center": [10.7, 1.4], "size": 6.5},
	}.get(state_name, {})
