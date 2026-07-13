class_name FurnitureFactory
extends RefCounted

const OAK_TEXTURE := preload("res://assets/materials/oak-seamless-v1.png")
const CITY_TEXTURE := preload("res://assets/overlays/window-city-evening-v1.png")


static func build_floor(room: RoomManifest, parent: Node3D) -> void:
	var grid := room.grid_size()
	var color := _material_color(room, String(room.data.get("floor", {}).get("material", "oak")))
	for x in range(grid.x):
		for y in range(grid.y):
			_add_box(parent, room, x, y, -0.08, 1.0, 1.0, 0.08, color, "Floor_%d_%d" % [x, y])


static func build_walls(room: RoomManifest, parent: Node3D) -> void:
	for wall_value in room.data.get("walls", []):
		var wall: Dictionary = wall_value
		var from: Array = wall.get("from", [])
		var to: Array = wall.get("to", [])
		if from.size() != 2 or to.size() != 2:
			continue
		var height := float(wall.get("height", 3.0))
		var color := _material_color(room, String(wall.get("material", "cream")))
		if int(from[1]) == int(to[1]):
			_add_box(parent, room, minf(float(from[0]), float(to[0])), float(from[1]) - 0.08, 0.0, absf(float(to[0]) - float(from[0])), 0.16, height, color, "Wall_%s" % wall.get("id", "horizontal"))
		elif int(from[0]) == int(to[0]):
			_add_box(parent, room, float(from[0]) - 0.08, minf(float(from[1]), float(to[1])), 0.0, 0.16, absf(float(to[1]) - float(from[1])), height, color, "Wall_%s" % wall.get("id", "vertical"))


static func build_object(room: RoomManifest, object: Dictionary) -> Node3D:
	var root := Node3D.new()
	root.name = String(object.get("id", "Furniture"))
	var transform: Dictionary = object.get("transform", {})
	var x := float(transform.get("x", 0.0))
	var y := float(transform.get("y", 0.0))
	var z := float(transform.get("z", 0.0))
	var width := float(transform.get("width", 1.0))
	var depth := float(transform.get("depth", 1.0))
	var height := float(transform.get("height", 1.0))
	var material := _material_color(room, String(object.get("material", "oak")))
	match String(object.get("kind", "cabinet")):
		"desk", "table", "console":
			_build_table(root, room, x, y, z, width, depth, height, material)
		"counter":
			_build_counter(root, room, x, y, z, width, depth, height, material)
		"cabinet", "wardrobe":
			_build_cabinet(root, room, x, y, z, width, depth, height, material)
		"fridge":
			_build_fridge(root, room, x, y, z, width, depth, height, material)
		"vanity":
			_build_vanity(root, room, x, y, z, width, depth, height, material)
		"bookcase", "divider":
			_build_bookcase(root, room, x, y, z, width, depth, height, material)
		"sofa":
			_build_sofa(root, room, x, y, z, width, depth, height, material)
		"bed":
			_build_bed(root, room, x, y, z, width, depth, height, material)
		"plant":
			_build_plant(root, room, x, y, z, width, depth, height, material)
		"window":
			_build_window(root, room, x, y, z, width, depth, height, material)
		"chair":
			_build_chair(root, room, x, y, z, width, depth, height, material)
		"rug":
			_add_box(root, room, x, y, z, width, depth, minf(height, 0.035), material, "Textile")
		"laptop":
			_build_laptop(root, room, x, y, z, width, depth, height, material)
		"books":
			_build_books(root, room, x, y, z, width, depth, height, material)
		"lamp":
			_build_lamp(root, room, x, y, z, width, depth, height, material)
		"cushion":
			_add_box(root, room, x, y, z, width, depth, height, material, "Cushion")
		"tea_set":
			_build_tea_set(root, room, x, y, z, width, depth, height, material)
		"curtain":
			_build_curtain(root, room, x, y, z, width, depth, height, material)
		"wall_art":
			_build_wall_art(root, room, x, y, z, width, depth, height, material)
		_:
			_add_box(root, room, x, y, z, width, depth, height, material, "Body")
	return root


static func _build_table(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	var slab := minf(0.16, height * 0.22)
	_add_box(parent, room, x, y, z + height - slab, width, depth, slab, material, "Top")
	var leg := minf(0.16, minf(width, depth) * 0.2)
	for leg_x in [x + 0.1, x + width - leg - 0.1]:
		for leg_y in [y + 0.1, y + depth - leg - 0.1]:
			_add_box(parent, room, leg_x, leg_y, z, leg, leg, height - slab, material, "Leg")


static func _build_cabinet(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	_add_box(parent, room, x, y, z, width, depth, height, material, "Case")
	var accent := _duplicate_material(material, 1.18)
	var door_height := maxf(0.25, height * 0.42)
	for door_x in [x + width * 0.08, x + width * 0.53]:
		_add_box(parent, room, door_x, y + depth - 0.025, z + height * 0.12, width * 0.38, 0.035, door_height, accent, "Door")


static func _build_counter(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	var body_height := maxf(0.04, height - 0.1)
	_build_cabinet(parent, room, x, y, z, width, depth, body_height, material)
	_add_box(parent, room, x - 0.03, y - 0.03, z + body_height, width + 0.06, depth + 0.06, 0.1, _material(Color("#d8c6a8")), "Worktop")


static func _build_fridge(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	_add_box(parent, room, x, y, z, width, depth, height, material, "Body")
	var face := _duplicate_material(material, 1.1)
	_add_box(parent, room, x + width * 0.05, y + depth - 0.025, z + height * 0.46, width * 0.9, 0.04, height * 0.48, face, "UpperDoor")
	_add_box(parent, room, x + width * 0.05, y + depth - 0.025, z + height * 0.06, width * 0.9, 0.04, height * 0.34, face, "LowerDoor")
	var metal := _material(Color("#8c8b80"))
	_add_box(parent, room, x + width * 0.82, y + depth + 0.01, z + height * 0.52, width * 0.045, 0.035, height * 0.28, metal, "Handle")


static func _build_vanity(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	_build_counter(parent, room, x, y, z, width, depth, height, _material(Color("#d6c7aa")))
	var basin := _material(Color("#55797a"))
	_add_box(parent, room, x + width * 0.2, y + depth * 0.22, z + height, width * 0.6, depth * 0.5, 0.06, basin, "Basin")
	var mirror := _duplicate_material(material, 1.2)
	mirror.metallic = 0.45
	mirror.roughness = 0.18
	_add_box(parent, room, x + width * 0.16, y + 0.02, z + height + 0.2, width * 0.68, 0.045, 0.86, mirror, "Mirror")


static func _build_bookcase(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	var edge := minf(0.13, minf(width, depth) * 0.3)
	_add_box(parent, room, x, y, z, edge, depth, height, material, "SideLeft")
	_add_box(parent, room, x + width - edge, y, z, edge, depth, height, material, "SideRight")
	_add_box(parent, room, x, y, z, width, depth, edge, material, "Base")
	_add_box(parent, room, x, y, z + height - edge, width, depth, edge, material, "Top")
	for shelf_index in range(1, 4):
		var shelf_z := z + height * shelf_index / 4.0
		_add_box(parent, room, x, y, shelf_z, width, depth, edge, material, "Shelf_%d" % shelf_index)
		var book_color := _material([Color("#355c62"), Color("#b66f5f"), Color("#d4b06b")][shelf_index - 1])
		_add_box(parent, room, x + edge * 1.4, y + depth * 0.18, shelf_z + edge, width * 0.46, depth * 0.62, height * 0.16, book_color, "ShelfBooks_%d" % shelf_index)


static func _build_sofa(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	var base_height := height * 0.42
	_add_box(parent, room, x, y, z, width, depth, base_height, material, "Base")
	var cushion := _duplicate_material(material, 1.08)
	_add_box(parent, room, x + width * 0.08, y + depth * 0.14, z + base_height, width * 0.84, depth * 0.55, height * 0.22, cushion, "Seat")
	_add_box(parent, room, x, y, z + base_height, width, depth * 0.18, height - base_height, material, "Back")
	_add_box(parent, room, x, y + depth * 0.12, z + base_height, width * 0.12, depth * 0.74, height * 0.38, material, "ArmLeft")
	_add_box(parent, room, x + width * 0.88, y + depth * 0.12, z + base_height, width * 0.12, depth * 0.74, height * 0.38, material, "ArmRight")


static func _build_bed(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	var frame_height := height * 0.26
	_add_box(parent, room, x, y, z, width, depth, frame_height, _duplicate_material(material, 0.72), "Frame")
	_add_box(parent, room, x + 0.08, y + 0.08, z + frame_height, width - 0.16, depth - 0.16, height * 0.42, _duplicate_material(material, 1.18), "Mattress")
	_add_box(parent, room, x + 0.16, y + depth * 0.38, z + frame_height + height * 0.4, width - 0.32, depth * 0.54, height * 0.16, material, "Blanket")
	_add_box(parent, room, x, y, z + frame_height, width, depth * 0.14, height * 0.74, _duplicate_material(material, 0.68), "Headboard")


static func _build_plant(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	_add_box(parent, room, x + width * 0.25, y + depth * 0.25, z, width * 0.5, depth * 0.5, height * 0.28, _duplicate_material(material, 0.65), "Pot")
	var leaves := SphereMesh.new()
	leaves.radius = minf(width, depth) * 0.36
	leaves.height = height * 0.72
	var leaf_mesh := MeshInstance3D.new()
	leaf_mesh.name = "Leaves"
	leaf_mesh.mesh = leaves
	leaf_mesh.material_override = _material(Color("#5f8052"))
	leaf_mesh.position = room.logical_to_world(Vector3(x + width * 0.5, y + depth * 0.5, z + height * 0.62))
	parent.add_child(leaf_mesh)


static func _build_window(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	var glass := _duplicate_material(material, 1.12)
	glass.albedo_texture = CITY_TEXTURE
	glass.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	glass.emission_enabled = false
	_add_box(parent, room, x, y, z, width, depth, height, glass, "Glass")
	var frame := _material(Color("#493a35"))
	_add_box(parent, room, x - 0.08, y - 0.02, z - 0.08, 0.09, depth + 0.04, height + 0.16, frame, "FrameLeft")
	_add_box(parent, room, x + width - 0.01, y - 0.02, z - 0.08, 0.09, depth + 0.04, height + 0.16, frame, "FrameRight")
	_add_box(parent, room, x, y - 0.02, z - 0.08, width, depth + 0.04, 0.09, frame, "FrameBottom")
	_add_box(parent, room, x, y - 0.02, z + height, width, depth + 0.04, 0.09, frame, "FrameTop")
	_add_box(parent, room, x + width * 0.48, y - 0.02, z, 0.07, depth + 0.04, height, frame, "Mullion")


static func _build_chair(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	_add_box(parent, room, x + width * 0.12, y + depth * 0.14, z + height * 0.36, width * 0.76, depth * 0.68, height * 0.18, material, "Seat")
	_add_box(parent, room, x + width * 0.12, y, z + height * 0.48, width * 0.76, depth * 0.14, height * 0.52, material, "Back")
	_add_box(parent, room, x + width * 0.44, y + depth * 0.44, z, width * 0.12, depth * 0.12, height * 0.4, material, "Stem")


static func _build_laptop(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	var dark := _material(Color("#26323a"))
	_add_box(parent, room, x, y, z, width, depth, 0.06, dark, "Keyboard")
	_add_box(parent, room, x, y, z + 0.05, width, 0.06, height - 0.05, dark, "Screen")
	var glow := _material(Color("#85b7c6"))
	glow.emission_enabled = true
	glow.emission = Color("#557f99")
	glow.emission_energy_multiplier = 0.6
	_add_box(parent, room, x + 0.06, y - 0.012, z + 0.12, width - 0.12, 0.025, height - 0.22, glow, "Display")


static func _build_books(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	var colors := [material, _material(Color("#355c62")), _material(Color("#b66f5f")), _material(Color("#d4b06b"))]
	var layers := maxi(1, int(height / 0.12))
	for index in range(layers):
		_add_box(parent, room, x + 0.02 * (index % 2), y, z + index * height / layers, width - 0.04, depth, height / layers * 0.82, colors[index % colors.size()], "Book_%d" % index)


static func _build_lamp(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	_add_cylinder(parent, room, x + width * 0.5, y + depth * 0.5, z, minf(width, depth) * 0.32, height * 0.08, _material(Color("#5b463c")), "Base")
	_add_cylinder(parent, room, x + width * 0.5, y + depth * 0.5, z + height * 0.08, minf(width, depth) * 0.07, height * 0.58, _material(Color("#8a6846")), "Stem")
	var shade := _duplicate_material(material, 1.15)
	shade.emission_enabled = true
	shade.emission = shade.albedo_color
	shade.emission_energy_multiplier = 0.45
	_add_box(parent, room, x + width * 0.12, y + depth * 0.12, z + height * 0.62, width * 0.76, depth * 0.76, height * 0.38, shade, "Shade")


static func _build_tea_set(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	_add_cylinder(parent, room, x + width * 0.5, y + depth * 0.5, z, minf(width, depth) * 0.22, height, material, "Teapot")
	_add_cylinder(parent, room, x + width * 0.18, y + depth * 0.58, z, minf(width, depth) * 0.1, height * 0.55, material, "CupLeft")
	_add_cylinder(parent, room, x + width * 0.82, y + depth * 0.58, z, minf(width, depth) * 0.1, height * 0.55, material, "CupRight")


static func _build_curtain(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	for fold in range(4):
		var fold_width := width / 4.0
		_add_box(parent, room, x + fold * fold_width, y + (fold % 2) * 0.025, z, fold_width * 0.86, depth, height, _duplicate_material(material, 0.9 + fold * 0.05), "Fold_%d" % fold)


static func _build_wall_art(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D) -> void:
	_add_box(parent, room, x, y, z, width, depth, height, _material(Color("#5b463c")), "Frame")
	_add_box(parent, room, x + 0.07, y - 0.015, z + 0.07, width - 0.14, depth + 0.025, height - 0.14, material, "Print")


static func _add_cylinder(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, radius: float, height: float, material: StandardMaterial3D, node_name: String) -> void:
	var mesh := CylinderMesh.new()
	mesh.top_radius = radius
	mesh.bottom_radius = radius
	mesh.height = height * RoomManifest.HEIGHT_UNIT
	mesh.radial_segments = 8
	var instance := MeshInstance3D.new()
	instance.name = node_name
	instance.mesh = mesh
	instance.material_override = material
	instance.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_ON
	instance.position = room.logical_to_world(Vector3(x, y, z + height * 0.5))
	parent.add_child(instance)


static func _add_box(parent: Node3D, room: RoomManifest, x: float, y: float, z: float, width: float, depth: float, height: float, material: StandardMaterial3D, node_name: String) -> void:
	if width <= 0.0 or depth <= 0.0 or height <= 0.0:
		return
	var mesh := BoxMesh.new()
	mesh.size = Vector3(width, height * RoomManifest.HEIGHT_UNIT, depth)
	var instance := MeshInstance3D.new()
	instance.name = node_name
	instance.mesh = mesh
	instance.material_override = material
	instance.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_ON
	instance.position = room.logical_to_world(Vector3(x + width * 0.5, y + depth * 0.5, z + height * 0.5))
	parent.add_child(instance)


static func _material_color(room: RoomManifest, name: String) -> StandardMaterial3D:
	var color := Color(String(room.data.get("materials", {}).get(name, "#8a6550")))
	var material := _material(color)
	if name == "oak":
		material.albedo_texture = OAK_TEXTURE
		material.uv1_scale = Vector3(1.0, 1.0, 1.0)
	return material


static func _material(color: Color) -> StandardMaterial3D:
	var material := StandardMaterial3D.new()
	material.albedo_color = color
	material.roughness = 0.86
	material.texture_filter = BaseMaterial3D.TEXTURE_FILTER_NEAREST
	return material


static func _duplicate_material(source: StandardMaterial3D, brightness: float) -> StandardMaterial3D:
	var material := source.duplicate() as StandardMaterial3D
	material.albedo_color = Color(source.albedo_color.r * brightness, source.albedo_color.g * brightness, source.albedo_color.b * brightness, source.albedo_color.a)
	return material
