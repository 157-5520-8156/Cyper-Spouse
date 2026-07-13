class_name TopdownFurniture
extends Node2D

var definition: Dictionary
var tile_size := 32
var active := false
var pulse := 0.0


func configure(next_definition: Dictionary, next_tile_size: int) -> void:
	definition = next_definition.duplicate(true)
	tile_size = next_tile_size
	position = Vector2(
		(float(definition.get("x", 0)) + float(definition.get("width", 1)) * 0.5) * tile_size,
		(float(definition.get("y", 0)) + float(definition.get("height", 1))) * tile_size
	)
	z_index = int(position.y)
	queue_redraw()


func set_active(value: bool) -> void:
	active = value
	queue_redraw()


func _process(delta: float) -> void:
	pulse += delta
	if active:
		queue_redraw()


func _draw() -> void:
	var width := float(definition.get("width", 1)) * tile_size
	var height := float(definition.get("height", 1)) * tile_size
	var rect := Rect2(-width * 0.5, -height, width, height)
	var color := Color(String(definition.get("resolved_color", "#ffffff")))
	var kind := String(definition.get("kind", "table"))
	if kind == "rug":
		draw_rect(rect.grow(-3), color.darkened(0.12), true)
		draw_rect(rect.grow(-5), color.lightened(0.12), false, 2.0)
		return
	match kind:
		"bed":
			draw_rect(rect, color.darkened(0.28), true)
			draw_rect(Rect2(rect.position + Vector2(3, 3), Vector2(rect.size.x - 6, rect.size.y * 0.54)), color.lightened(0.14), true)
			draw_rect(Rect2(rect.position + Vector2(4, rect.size.y * 0.58), Vector2(rect.size.x - 8, rect.size.y * 0.3)), Color("#f3e6ca"), true)
			draw_line(rect.position + Vector2(0, rect.size.y * 0.55), rect.position + Vector2(rect.size.x, rect.size.y * 0.55), color.darkened(0.4), 2.0)
		"sofa":
			draw_rect(rect, color.darkened(0.3), true)
			draw_rect(Rect2(rect.position + Vector2(3, 4), Vector2(rect.size.x - 6, rect.size.y * 0.6)), color, true)
			for seat in range(int(definition.get("width", 1))):
				draw_line(Vector2(rect.position.x + (seat + 1) * tile_size, rect.position.y + 5), Vector2(rect.position.x + (seat + 1) * tile_size, rect.end.y - 4), color.darkened(0.25), 1.0)
		"desk", "counter", "bookcase", "fridge", "sink":
			draw_rect(rect, color.darkened(0.38), true)
			draw_rect(Rect2(rect.position + Vector2(2, 2), rect.size - Vector2(4, 6)), color, true)
			if kind == "desk":
				draw_rect(Rect2(rect.get_center() - Vector2(10, 10), Vector2(20, 10)), Color("#263f50"), true)
			if kind == "bookcase":
				for shelf in range(1, int(definition.get("height", 1)) * 2):
					var shelf_y := rect.position.y + shelf * rect.size.y / (int(definition.get("height", 1)) * 2.0)
					draw_line(Vector2(rect.position.x + 3, shelf_y), Vector2(rect.end.x - 3, shelf_y), Color("#f2cf77"), 2.0)
			if kind == "fridge":
				draw_line(Vector2(rect.position.x + 3, rect.get_center().y), Vector2(rect.end.x - 3, rect.get_center().y), Color("#dce8d2"), 2.0)
			if kind == "sink":
				draw_circle(rect.get_center(), minf(rect.size.x, rect.size.y) * 0.24, Color("#8bb5bd"))
		"plant":
			draw_rect(Rect2(-width * 0.24, -height * 0.35, width * 0.48, height * 0.3), Color("#b46f50"), true)
			for angle in [-1.0, -0.45, 0.0, 0.45, 1.0]:
				draw_line(Vector2(0, -height * 0.35), Vector2(sin(angle) * width * 0.36, -height * 0.92 + absf(angle) * 8), color, 5.0)
		"chair":
			draw_rect(rect.grow(-4), color.darkened(0.25), true)
			draw_rect(Rect2(rect.position + Vector2(5, 5), rect.size - Vector2(10, 11)), color, true)
		_:
			draw_rect(rect, color.darkened(0.35), true)
			draw_rect(rect.grow(-4), color, true)
	if active:
		var glow := 0.6 + sin(pulse * 5.0) * 0.25
		draw_rect(rect.grow(4), Color(1.0, 0.86, 0.46, glow), false, 2.0)
