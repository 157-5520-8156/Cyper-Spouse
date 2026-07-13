extends SceneTree

const TOPDOWN_SCENE := preload("res://topdown/scenes/topdown_home.tscn")


func _init() -> void:
	call_deferred("_run")


func _run() -> void:
	var scene := TOPDOWN_SCENE.instantiate()
	root.add_child(scene)
	await process_frame
	await process_frame
	if scene.actor == null or scene.furniture_by_id.size() < 10:
		push_error("Top-down scene did not create its actor and furniture")
		quit(1)
		return
	var mapping: String = scene.actor.snap_scene_state({"location": "desk", "action": "study"})
	if mapping != "desk · study" or scene.actor.path.size() != 0:
		push_error("Top-down actor could not snap to the desk interaction")
		quit(1)
		return
	scene._set_active_object(scene.room.interaction_for({"location": "desk", "action": "study"}))
	if not scene.furniture_by_id["desk"].active:
		push_error("Top-down interaction did not highlight the active furniture")
		quit(1)
		return
	print("Top-down runtime test passed")
	scene.queue_free()
	quit(0)
