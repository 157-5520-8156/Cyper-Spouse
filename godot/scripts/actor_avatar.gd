class_name ActorAvatar
extends Node3D

const WALK_TEXTURE := preload("res://assets/characters/zhizhi-iso-walk-v4.png")
const ACTION_MANIFEST_PATH := "res://scenes/zhizhi-actions.json"

var room: RoomManifest
var navigation: NavigationGrid
var sprite := Sprite3D.new()
var logical_position := Vector3.ZERO
var pose_target := Vector3.ZERO
var path: Array[Vector2i] = []
var facing := "down_left"
var action := "idle"
var active_pose := "idle"
var walked_distance := 0.0
var is_offline := false
var action_manifest := ActorActionManifest.new()
var effect_root := Node3D.new()
var effect_name := ""
var effect_clock := 0.0
var visual_base_y := 0.0


func configure(next_room: RoomManifest, next_navigation: NavigationGrid) -> Error:
	room = next_room
	navigation = next_navigation
	logical_position = Vector3(room.anchor_for("entry").x, room.anchor_for("entry").y, 0.0)
	pose_target = logical_position
	var action_error := action_manifest.load_manifest(ACTION_MANIFEST_PATH)
	if action_error != OK:
		push_error("Unable to load actor action manifest")
		return action_error
	if not action_manifest.validate().is_empty():
		push_error("Invalid actor action manifest")
		return ERR_INVALID_DATA
	sprite.billboard = BaseMaterial3D.BILLBOARD_ENABLED
	sprite.alpha_cut = SpriteBase3D.ALPHA_CUT_DISCARD
	sprite.alpha_scissor_threshold = 0.42
	sprite.no_depth_test = false
	sprite.texture_filter = BaseMaterial3D.TEXTURE_FILTER_NEAREST
	add_child(sprite)
	effect_root.name = "ActionEffect"
	add_child(effect_root)
	_apply_visual()
	_update_action_effects(0.0)
	_update_world_position()
	return OK


func set_scene_state(scene_state: Dictionary) -> String:
	var interaction := room.interaction_for(scene_state)
	var location := String(scene_state.get("location", "rug"))
	var requested_action := String(scene_state.get("action", "idle"))
	if interaction.is_empty():
		active_pose = "idle"
		action = requested_action if requested_action in ["idle", "walk_out", "gaze"] else "idle"
		pose_target = Vector3(room.anchor_for(location).x, room.anchor_for(location).y, 0.0)
		_start_path(Vector2i(int(pose_target.x), int(pose_target.y)))
		return "idle fallback · %s" % location
	var approach: Array = interaction["approach"]
	var target := Vector2i(int(approach[0]), int(approach[1]))
	facing = String(interaction.get("facing", facing))
	active_pose = String(interaction.get("pose", "idle"))
	action = requested_action
	var anchor: Array = interaction["pose_anchor"]
	pose_target = Vector3(float(anchor[0]), float(anchor[1]), float(anchor[2]))
	_start_path(target)
	return "%s · %s" % [location, requested_action]


func snap_scene_state(scene_state: Dictionary) -> String:
	var mapping := set_scene_state(scene_state)
	path.clear()
	logical_position = pose_target
	_apply_visual()
	_update_world_position()
	return mapping


func snap_preview(next_position: Vector3, next_facing: String) -> void:
	path.clear()
	logical_position = next_position
	pose_target = next_position
	facing = next_facing
	action = "idle"
	active_pose = "idle"
	_apply_visual()
	_update_world_position()


func set_offline(value: bool) -> void:
	is_offline = value


func _process(delta: float) -> void:
	if not path.is_empty():
		var next_tile: Vector2i = path.front()
		var target := Vector3(next_tile.x, next_tile.y, 0.0)
		var current_floor := Vector2(logical_position.x, logical_position.y)
		var target_floor := Vector2(target.x, target.y)
		var moved := current_floor.move_toward(target_floor, 2.35 * delta)
		var movement := moved - current_floor
		if movement.length_squared() > 0.0001:
			facing = _direction_for(movement)
			walked_distance += movement.length()
		logical_position.x = moved.x
		logical_position.y = moved.y
		logical_position.z = 0.0
		if moved.distance_to(target_floor) < 0.01:
			logical_position = target
			path.pop_front()
		if path.is_empty():
			logical_position = logical_position.lerp(pose_target, minf(1.0, delta * 6.0))
	else:
		logical_position = logical_position.lerp(pose_target, minf(1.0, delta * 6.0))
	_apply_visual()
	_update_action_effects(delta)
	_update_world_position()


func _start_path(target: Vector2i) -> void:
	var current := Vector2i(roundi(logical_position.x), roundi(logical_position.y))
	path = navigation.find_path(current, target)
	if current != target and path.is_empty():
		active_pose = "idle"
		action = "idle"
		pose_target = logical_position


func _apply_visual() -> void:
	var is_walking := not path.is_empty()
	var action_key := _visual_action_key(is_walking)
	var definition := action_manifest.definition_for(action_key)
	if definition.is_empty():
		return
	var pivot := action_manifest.pivot_for(action_key)
	if is_walking:
		sprite.texture = WALK_TEXTURE
		sprite.hframes = 4
		sprite.vframes = 4
		sprite.frame = (int(floor(walked_distance * 8.0)) % 4) * 4 + _direction_column(facing)
		sprite.pixel_size = action_manifest.pixel_size_for(action_key)
		visual_base_y = (pivot.y - 0.5) * 128.0 * sprite.pixel_size
		sprite.position.y = visual_base_y
		return
	var texture: Texture2D = load(String(definition["texture"]))
	var atlas := AtlasTexture.new()
	atlas.atlas = texture
	var region: Array = definition["region"]
	atlas.region = Rect2(float(region[0]), float(region[1]), float(region[2]), float(region[3]))
	sprite.pixel_size = action_manifest.pixel_size_for(action_key)
	visual_base_y = (pivot.y - 0.5) * float(region[3]) * sprite.pixel_size
	sprite.position.y = visual_base_y
	sprite.texture = atlas
	sprite.hframes = 1
	sprite.vframes = 1
	sprite.frame = 0


func _visual_action_key(is_walking: bool) -> String:
	if is_walking:
		return "walk.%s" % facing
	var requested := action
	if requested in ["read_phone", "type_phone", "notice_phone", "glance_phone"]:
		requested = "phone"
	if requested == "social":
		requested = "relax"
	var requested_key := "%s.%s" % [requested, facing]
	if not action_manifest.definition_for(requested_key).is_empty() and action_manifest.data.get("actions", {}).has(requested_key):
		return requested_key
	return "idle.%s" % facing


func _update_action_effects(delta: float) -> void:
	effect_clock += delta
	var next_effect := action_manifest.effect_for(_visual_action_key(not path.is_empty()))
	if next_effect != effect_name:
		effect_name = next_effect
		_rebuild_action_effect()
	var pulse := sin(effect_clock * 4.0)
	match effect_name:
		"typing", "tidy_sparkle":
			effect_root.position.y = 0.02 + pulse * 0.025
		"phone_glow":
			effect_root.scale = Vector3.ONE * (1.0 + pulse * 0.06)
		"breathing", "sleep_breathing":
			sprite.position.y = visual_base_y + pulse * 0.015
		"water":
			effect_root.position.y = fposmod(effect_clock * 0.35, 0.22)
		_:
			effect_root.position = Vector3.ZERO
			effect_root.scale = Vector3.ONE


func _rebuild_action_effect() -> void:
	for child in effect_root.get_children():
		child.free()
	effect_root.position = Vector3.ZERO
	effect_root.scale = Vector3.ONE
	match effect_name:
		"typing":
			_add_effect_box(Vector3(-0.16, 0.48, 0.08), Vector3(0.1, 0.04, 0.08), Color("#f3d078"))
			_add_effect_box(Vector3(0.02, 0.48, 0.08), Vector3(0.1, 0.04, 0.08), Color("#f3d078"))
		"meal":
			_add_effect_cylinder(Vector3(0.18, 0.42, 0.1), 0.12, 0.08, Color("#ece0bd"))
		"phone_glow":
			_add_effect_box(Vector3(0.18, 0.55, 0.12), Vector3(0.16, 0.24, 0.035), Color("#5eb4ca"), true)
		"water":
			for offset in [-0.08, 0.0, 0.08]:
				_add_effect_sphere(Vector3(offset, 0.38 + absf(offset), 0.1), 0.035, Color("#72c7da"), true)
		"tidy_sparkle":
			_add_effect_sphere(Vector3(-0.2, 0.62, 0.08), 0.05, Color("#ffd478"), true)
			_add_effect_sphere(Vector3(0.2, 0.78, 0.08), 0.04, Color("#fff0a5"), true)


func _effect_material(color: Color, emissive: bool = false) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = color
	material.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	if emissive:
		material.emission_enabled = true
		material.emission = color
		material.emission_energy_multiplier = 0.7
	return material


func _add_effect_box(local_position: Vector3, size: Vector3, color: Color, emissive: bool = false) -> void:
	var mesh := BoxMesh.new()
	mesh.size = size
	var instance := MeshInstance3D.new()
	instance.mesh = mesh
	instance.material_override = _effect_material(color, emissive)
	instance.position = local_position
	effect_root.add_child(instance)


func _add_effect_sphere(local_position: Vector3, radius: float, color: Color, emissive: bool = false) -> void:
	var mesh := SphereMesh.new()
	mesh.radius = radius
	mesh.height = radius * 2.0
	mesh.radial_segments = 8
	mesh.rings = 4
	var instance := MeshInstance3D.new()
	instance.mesh = mesh
	instance.material_override = _effect_material(color, emissive)
	instance.position = local_position
	effect_root.add_child(instance)


func _add_effect_cylinder(local_position: Vector3, radius: float, height: float, color: Color) -> void:
	var mesh := CylinderMesh.new()
	mesh.top_radius = radius
	mesh.bottom_radius = radius
	mesh.height = height
	mesh.radial_segments = 8
	var instance := MeshInstance3D.new()
	instance.mesh = mesh
	instance.material_override = _effect_material(color)
	instance.position = local_position
	effect_root.add_child(instance)


func _update_world_position() -> void:
	position = room.logical_to_world(logical_position)


func _direction_for(movement: Vector2) -> String:
	if absf(movement.x) > absf(movement.y):
		return "down_right" if movement.x > 0.0 else "up_left"
	return "down_left" if movement.y > 0.0 else "up_right"


func _direction_column(direction: String) -> int:
	return {"down_right": 0, "down_left": 1, "up_left": 2, "up_right": 3}.get(direction, 1)
