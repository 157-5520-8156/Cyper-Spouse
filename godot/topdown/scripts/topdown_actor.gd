class_name TopdownActor
extends Node2D

const ACTOR_TEXTURE := preload("res://assets/characters/zhizhi-topdown.png")

var room: TopdownRoomManifest
var navigation: TopdownNavigation
var sprite := Sprite2D.new()
var current_tile := Vector2.ZERO
var path: Array[Vector2i] = []
var target_tile := Vector2.ZERO
var facing := "down"
var action := "idle"
var expression := "neutral"
var clock := 0.0


func configure(next_room: TopdownRoomManifest, next_navigation: TopdownNavigation) -> void:
	room = next_room
	navigation = next_navigation
	current_tile = Vector2(room.anchor_for("entry")) + Vector2(0.5, 0.5)
	target_tile = current_tile
	sprite.texture = ACTOR_TEXTURE
	sprite.hframes = 7
	sprite.vframes = 4
	sprite.centered = true
	sprite.position = Vector2(0, -9)
	sprite.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
	add_child(sprite)
	_update_visual()
	_update_position()


func set_scene_state(scene_state: Dictionary) -> String:
	var interaction := room.interaction_for(scene_state)
	action = String(scene_state.get("action", "idle"))
	expression = String(scene_state.get("expression", "neutral"))
	if interaction.is_empty():
		var fallback := room.anchor_for(String(scene_state.get("location", "rug")))
		target_tile = Vector2(fallback) + Vector2(0.5, 0.5)
		_start_path(fallback)
		return "idle fallback · %s" % scene_state.get("location", "rug")
	var approach: Array = interaction["approach"]
	var target := Vector2i(int(approach[0]), int(approach[1]))
	target_tile = Vector2(target) + Vector2(0.5, 0.5)
	facing = String(interaction.get("facing", "up"))
	_start_path(target)
	return "%s · %s" % [scene_state.get("location", "room"), action]


func snap_scene_state(scene_state: Dictionary) -> String:
	var mapping := set_scene_state(scene_state)
	path.clear()
	current_tile = target_tile
	_update_visual()
	_update_position()
	return mapping


func _process(delta: float) -> void:
	clock += delta
	if not path.is_empty():
		var next_tile := Vector2(path.front()) + Vector2(0.5, 0.5)
		var old_position := current_tile
		current_tile = current_tile.move_toward(next_tile, delta * 3.2)
		var movement := current_tile - old_position
		if movement.length_squared() > 0.0001:
			facing = _facing_for(movement)
		if current_tile.distance_to(next_tile) < 0.01:
			current_tile = next_tile
			path.pop_front()
	elif current_tile.distance_to(target_tile) > 0.002:
		current_tile = current_tile.move_toward(target_tile, delta * 5.0)
	_update_visual()
	_update_position()
	queue_redraw()


func _start_path(next_target: Vector2i) -> void:
	var start := Vector2i(floori(current_tile.x), floori(current_tile.y))
	path = navigation.find_path(start, next_target)
	if start != next_target and path.is_empty():
		target_tile = current_tile
		action = "idle"


func _update_position() -> void:
	position = room.tile_to_screen(current_tile - Vector2(0.5, 0.5))
	z_index = int(position.y)


func _update_visual() -> void:
	var walking := not path.is_empty()
	var row: int = int({"down": 0, "left": 1, "right": 2, "up": 3}.get(facing, 0))
	var frame: int = int(floor(clock * 9.0)) % 4 if walking else 0
	sprite.frame = row * 7 + frame
	# The existing daemon expression becomes a restrained mood tint rather than a
	# separate, incompatible character sprite sheet.
	sprite.modulate = _expression_tint()


func _draw() -> void:
	if not path.is_empty() or action == "idle":
		return
	var label: String = String({"study": "⌨", "eat": "✦", "relax": "…", "social": "…", "phone": "▣", "read_phone": "▣", "type_phone": "▣", "notice_phone": "▣", "glance_phone": "▣", "sleep": "z", "wash": "·", "tidy": "✦"}.get(action, "·"))
	var font := ThemeDB.fallback_font
	var wobble := sin(clock * 3.0) * 2.0
	draw_string(font, Vector2(-4, -34 + wobble), label, HORIZONTAL_ALIGNMENT_LEFT, -1, 16, Color("#fff0a5"))


func _facing_for(movement: Vector2) -> String:
	if absf(movement.x) > absf(movement.y):
		return "right" if movement.x > 0 else "left"
	return "down" if movement.y > 0 else "up"


func _expression_tint() -> Color:
	match expression:
		"happy", "excited", "playful":
			return Color("#fff2cf")
		"tired", "sleepy", "sad":
			return Color("#b9c8d8")
		"focused", "serious", "thoughtful":
			return Color("#d6e9dd")
	return Color.WHITE
