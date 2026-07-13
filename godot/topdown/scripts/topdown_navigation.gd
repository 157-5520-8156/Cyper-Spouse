class_name TopdownNavigation
extends RefCounted

var grid := AStarGrid2D.new()
var blocked: Dictionary = {}


func configure(room: TopdownRoomManifest) -> void:
	blocked.clear()
	grid.region = Rect2i(Vector2i.ZERO, room.grid_size())
	grid.cell_size = Vector2(room.tile_size(), room.tile_size())
	grid.diagonal_mode = AStarGrid2D.DIAGONAL_MODE_NEVER
	grid.update()
	for object_value in room.data.get("objects", []):
		var object: Dictionary = object_value
		if not bool(object.get("solid", true)):
			continue
		for tile in room.tiles_for(object):
			blocked[tile] = true
			grid.set_point_solid(tile, true)


func find_path(start: Vector2i, target: Vector2i) -> Array[Vector2i]:
	if blocked.has(start) or blocked.has(target):
		return []
	var raw_path := grid.get_id_path(start, target)
	var path: Array[Vector2i] = []
	for point in raw_path:
		path.append(Vector2i(point))
	if not path.is_empty() and path.front() == start:
		path.pop_front()
	return path


func reachable_from(start: Vector2i) -> Dictionary:
	var reachable := {}
	if blocked.has(start):
		return reachable
	var queue: Array[Vector2i] = [start]
	reachable[start] = true
	while not queue.is_empty():
		var current: Vector2i = queue.pop_front()
		for offset: Vector2i in [Vector2i.LEFT, Vector2i.RIGHT, Vector2i.UP, Vector2i.DOWN]:
			var next_tile: Vector2i = current + offset
			if grid.region.has_point(next_tile) and not blocked.has(next_tile) and not reachable.has(next_tile):
				reachable[next_tile] = true
				queue.append(next_tile)
	return reachable
