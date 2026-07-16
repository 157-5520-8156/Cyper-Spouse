extends Node3D

@onready var camera: Camera3D = $Camera3D
@onready var daemon_poll: HTTPRequest = $DaemonPoll
@onready var status_label: Label = $Hud/Status

var room_manifest := RoomManifest.new()
var navigation := NavigationGrid.new()
var room_root := Node3D.new()
var actor := ActorAvatar.new()
var poll_timer: Timer
var last_scene_state: Dictionary = {"location": "rug", "action": "idle"}
var bridge_online := false
var state_bridge := RoomStateBridge.new()


func _ready() -> void:
	room_root.name = "Room"
	add_child(room_root)
	var manifest_path := String(ProjectSettings.get_setting("zhizhi/manifest_path"))
	var load_error := room_manifest.load_manifest(manifest_path)
	if load_error != OK:
		status_label.text = "Godot Home · manifest load failed"
		push_error("Unable to load room manifest: %s" % manifest_path)
		return
	var errors := room_manifest.validate()
	if not errors.is_empty():
		status_label.text = "Godot Home · invalid manifest"
		for manifest_error in errors:
			push_error(manifest_error)
		return
	navigation.configure(room_manifest)
	_validate_reachability()
	FurnitureFactory.build_floor(room_manifest, room_root)
	FurnitureFactory.build_walls(room_manifest, room_root)
	for object_value in room_manifest.data.get("objects", []):
		room_root.add_child(FurnitureFactory.build_object(room_manifest, object_value))
	_add_warm_lights()
	actor.name = "Zhizhi"
	var actor_error := actor.configure(room_manifest, navigation)
	if actor_error != OK:
		status_label.text = "Godot Home · actor asset load failed"
		return
	room_root.add_child(actor)
	_configure_camera()
	if bool(ProjectSettings.get_setting("zhizhi/capture_mode", false)):
		_update_status("capture preview")
		return
	daemon_poll.request_completed.connect(_on_daemon_context)
	poll_timer = Timer.new()
	poll_timer.wait_time = 4.0
	poll_timer.timeout.connect(request_daemon_context)
	add_child(poll_timer)
	poll_timer.start()
	request_daemon_context()
	_update_status("offline preview")


func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("refresh_scene"):
		request_daemon_context()


func request_daemon_context() -> void:
	if daemon_poll.get_http_client_status() != HTTPClient.STATUS_DISCONNECTED:
		return
	var daemon_url: String = ProjectSettings.get_setting("zhizhi/daemon_room_url", "")
	if daemon_url.is_empty():
		bridge_online = false
		actor.set_offline(true)
		_update_status("offline · room reader not configured")
		return
	var request_error := daemon_poll.request(daemon_url)
	if request_error != OK:
		bridge_online = false
		actor.set_offline(true)
		_update_status("offline · keeping last scene")


func _on_daemon_context(
		_result: int,
		response_code: int,
		_headers: PackedStringArray,
		body: PackedByteArray,
	) -> void:
	if response_code < 200 or response_code >= 300:
		bridge_online = false
		actor.set_offline(true)
		_update_status("offline · HTTP %d" % response_code)
		return
	var scene_state := state_bridge.scene_state_from_public_room_body(body)
	if scene_state.is_empty():
		bridge_online = false
		actor.set_offline(true)
		_update_status("offline · invalid or missing daemon scene")
		return
	last_scene_state = scene_state.duplicate(true)
	bridge_online = true
	actor.set_offline(false)
	var mapping := actor.set_scene_state(last_scene_state)
	_apply_time_of_day(String(last_scene_state.get("time_of_day", "day")))
	_update_status("online · %s" % mapping)


func _configure_camera() -> void:
	var grid := room_manifest.grid_size()
	var target := room_manifest.floor_position(Vector2(grid.x * 0.5, grid.y * 0.5))
	camera.position = target + room_manifest.camera_offset()
	camera.look_at(target, Vector3.UP)
	camera.projection = Camera3D.PROJECTION_ORTHOGONAL
	camera.size = 15.2


func _add_warm_lights() -> void:
	for logical_position in [Vector3(5.8, 3.0, 2.4), Vector3(12.6, 7.2, 1.1), Vector3(1.6, 10.6, 0.7)]:
		var light := OmniLight3D.new()
		light.light_color = Color("#ffbd72")
		light.light_energy = 1.7
		light.omni_range = 4.2
		light.position = room_manifest.logical_to_world(logical_position)
		room_root.add_child(light)


func _apply_time_of_day(time_of_day: String) -> void:
	var environment: Environment = $WorldEnvironment.environment
	var night := time_of_day in ["night", "evening", "late_night"]
	environment.ambient_light_color = Color("#473441") if night else Color("#73564d")
	environment.ambient_light_energy = 0.34 if night else 0.75
	$Sun.light_energy = 0.24 if night else 1.25


func _validate_reachability() -> void:
	var entry := room_manifest.anchor_for("entry")
	var reachable := navigation.reachable_from(entry)
	for interaction_name in room_manifest.data.get("interactions", {}):
		var interaction: Dictionary = room_manifest.data["interactions"][interaction_name]
		var approach: Array = interaction["approach"]
		var tile := Vector2i(int(approach[0]), int(approach[1]))
		if not reachable.has(tile):
			push_error("Unreachable interaction %s" % interaction_name)


func _update_status(detail: String) -> void:
	status_label.text = "Godot Home · %s" % detail
