extends Node2D

const MANIFEST_PATH := "res://topdown/scenes/zhizhi-home-topdown.json"
const TINY_TOWN_TILEMAP := preload("res://assets/third_party/kenney/tiny-town/Tilemap/tilemap.png")

@onready var daemon_poll: HTTPRequest = $DaemonPoll
@onready var status_label: Label = $Hud/Status

var room := TopdownRoomManifest.new()
var navigation := TopdownNavigation.new()
var actor := TopdownActor.new()
var furniture_root := Node2D.new()
var furniture_by_id := {}
var poll_timer := Timer.new()
var state_bridge := RoomStateBridge.new()
var active_object_id := ""


func _ready() -> void:
	if room.load_manifest(String(ProjectSettings.get_setting("zhizhi/topdown_manifest_path", MANIFEST_PATH))) != OK:
		status_label.text = "Zhizhi Home · manifest load failed"
		return
	var errors := room.validate()
	if not errors.is_empty():
		for manifest_error in errors:
			push_error(manifest_error)
		status_label.text = "Zhizhi Home · invalid manifest"
		return
	navigation.configure(room)
	_validate_reachability()
	queue_redraw()
	furniture_root.y_sort_enabled = true
	add_child(furniture_root)
	for object_value in room.data.get("objects", []):
		var object: Dictionary = object_value.duplicate(true)
		object["resolved_color"] = _color_for(String(object.get("color", "paper"))).to_html()
		var furniture := TopdownFurniture.new()
		furniture.name = String(object["id"])
		furniture.configure(object, room.tile_size(), room.origin())
		furniture_root.add_child(furniture)
		furniture_by_id[object["id"]] = furniture
	actor.name = "Zhizhi"
	actor.configure(room, navigation)
	add_child(actor)
	_add_kenney_decor()
	daemon_poll.request_completed.connect(_on_daemon_context)
	poll_timer.wait_time = 3.0
	poll_timer.timeout.connect(request_daemon_context)
	add_child(poll_timer)
	poll_timer.start()
	request_daemon_context()
	status_label.text = "Zhizhi Home · top-down preview"


func request_daemon_context() -> void:
	if daemon_poll.get_http_client_status() != HTTPClient.STATUS_DISCONNECTED:
		return
	var room_url: String = ProjectSettings.get_setting("zhizhi/daemon_room_url", "")
	if room_url.is_empty():
		status_label.text = "Zhizhi Home · room reader not configured"
		return
	var error := daemon_poll.request(room_url)
	if error != OK:
		status_label.text = "Zhizhi Home · offline preview"


func _on_daemon_context(_result: int, response_code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	if response_code < 200 or response_code >= 300:
		status_label.text = "Zhizhi Home · offline · keeping activity"
		return
	var scene_state := state_bridge.scene_state_from_public_room_body(body)
	if scene_state.is_empty():
		status_label.text = "Zhizhi Home · offline · invalid scene"
		return
	var mapping := actor.set_scene_state(scene_state)
	_set_active_object(room.interaction_for(scene_state))
	status_label.text = "Zhizhi Home · %s" % mapping


func _draw() -> void:
	var palette: Dictionary = room.data.get("palette", {})
	var size := get_viewport_rect().size
	draw_rect(Rect2(Vector2.ZERO, size), Color(String(palette.get("grass", "#85b86c"))), true)
	var grid := room.grid_size()
	var tile := room.tile_size()
	var origin := room.origin()
	for y in range(grid.y):
		for x in range(grid.x):
			var rect := Rect2(origin + Vector2(x, y) * tile, Vector2(tile, tile))
			draw_rect(rect, Color(String(palette.get("grass", "#85b86c"))).darkened(0.1) if (x + y) % 2 == 0 else Color(String(palette.get("grass", "#85b86c"))), true)
	for room_value in room.data.get("rooms", []):
		var room_definition: Dictionary = room_value
		var values: Array = room_definition["rect"]
		var rect := Rect2(origin + Vector2(float(values[0]), float(values[1])) * tile, Vector2(float(values[2]), float(values[3])) * tile)
		var floor_color := Color(String(room_definition.get("floor", "#c98c5c")))
		draw_rect(rect, floor_color, true)
		for y in range(int(values[3])):
			for x in range(int(values[2])):
				if (x + y) % 2 == 0:
					draw_rect(Rect2(rect.position + Vector2(x, y) * tile, Vector2(tile, tile)), floor_color.lightened(0.045), true)
		draw_rect(rect, Color(String(palette.get("wall_shadow", "#b77a5d"))), false, 5.0)
		draw_line(rect.position + Vector2(0, 3), Vector2(rect.end.x, rect.position.y + 3), Color(String(palette.get("wall", "#f0d4a6"))), 6.0)
	var house_rect := Rect2(origin + Vector2(1, 1) * tile, Vector2(16, 11) * tile)
	draw_rect(house_rect, Color("#ffffff"), false, 2.0)
	draw_string(ThemeDB.fallback_font, origin + Vector2(2, -18), "Z H I Z H I  ·  H O M E", HORIZONTAL_ALIGNMENT_LEFT, -1, 14, Color("#fff1c7"))


func _add_kenney_decor() -> void:
	# These are local regions from Kenney Tiny Town's CC0 tilemap, not a flattened
	# background image. The room remains entirely data-driven.
	for decoration in [
		{"region": Rect2(112, 0, 32, 48), "position": Vector2(30, 112)},
		{"region": Rect2(144, 0, 32, 48), "position": Vector2(656, 126)},
		{"region": Rect2(80, 0, 16, 32), "position": Vector2(34, 410)}
	]:
		var atlas := AtlasTexture.new()
		atlas.atlas = TINY_TOWN_TILEMAP
		atlas.region = decoration["region"]
		var sprite := Sprite2D.new()
		sprite.texture = atlas
		sprite.position = decoration["position"]
		sprite.scale = Vector2(1.5, 1.5)
		sprite.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
		sprite.z_index = -5
		add_child(sprite)


func _color_for(key: String) -> Color:
	return Color(String(room.data.get("palette", {}).get(key, "#f2e7cc")))


func _set_active_object(interaction: Dictionary) -> void:
	var next_id := String(interaction.get("object", ""))
	if next_id == active_object_id:
		return
	if furniture_by_id.has(active_object_id):
		furniture_by_id[active_object_id].set_active(false)
	active_object_id = next_id
	if furniture_by_id.has(active_object_id):
		furniture_by_id[active_object_id].set_active(true)


func _validate_reachability() -> void:
	var reachable := navigation.reachable_from(room.anchor_for("entry"))
	for interaction_name in room.data.get("interactions", {}):
		var interaction: Dictionary = room.data["interactions"][interaction_name]
		var approach: Array = interaction["approach"]
		if not reachable.has(Vector2i(int(approach[0]), int(approach[1]))):
			push_error("Unreachable top-down interaction %s" % interaction_name)
