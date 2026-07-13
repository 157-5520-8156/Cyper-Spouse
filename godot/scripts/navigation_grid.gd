class_name NavigationGrid
extends RefCounted

var grid_size := Vector2i.ZERO
var blocked: Dictionary = {}
var wall_edges: Dictionary = {}


func configure(room: RoomManifest) -> void:
	grid_size = room.grid_size()
	blocked.clear()
	wall_edges.clear()
	for object_value in room.data.get("objects", []):
		var object: Dictionary = object_value
		if object.get("occupancy", "solid") != "solid":
			continue
		for footprint_value in object.get("footprint", []):
			blocked[Vector2i(int(footprint_value[0]), int(footprint_value[1]))] = true
	for wall_value in room.data.get("walls", []):
		var wall: Dictionary = wall_value
		_add_wall_edges(wall.get("from", []), wall.get("to", []))


func find_path(start: Vector2i, target: Vector2i) -> Array[Vector2i]:
	if not _walkable(start) or not _walkable(target):
		return []
	var open: Array[Vector2i] = [start]
	var came_from: Dictionary = {start: start}
	var cost: Dictionary = {start: 0.0}
	while not open.is_empty():
		var current_index := 0
		var current_score := INF
		for index in range(open.size()):
			var candidate: Vector2i = open[index]
			var candidate_score := float(cost[candidate]) + _heuristic(candidate, target)
			if candidate_score < current_score:
				current_score = candidate_score
				current_index = index
		var current: Vector2i = open.pop_at(current_index)
		if current == target:
			return _reconstruct(came_from, start, target)
		for next_tile in _neighbors(current):
			if not _walkable(next_tile) or wall_edges.has(_edge_key(current, next_tile)):
				continue
			var next_cost := float(cost[current]) + 1.0
			if next_cost < float(cost.get(next_tile, INF)):
				came_from[next_tile] = current
				cost[next_tile] = next_cost
				if not open.has(next_tile):
					open.append(next_tile)
	return []


func reachable_from(start: Vector2i) -> Dictionary:
	var reachable: Dictionary = {}
	if not _walkable(start):
		return reachable
	var queue: Array[Vector2i] = [start]
	reachable[start] = true
	while not queue.is_empty():
		var current: Vector2i = queue.pop_front()
		for next_tile in _neighbors(current):
			if _walkable(next_tile) and not wall_edges.has(_edge_key(current, next_tile)) and not reachable.has(next_tile):
				reachable[next_tile] = true
				queue.append(next_tile)
	return reachable


func _add_wall_edges(from: Array, to: Array) -> void:
	if from.size() != 2 or to.size() != 2:
		return
	var x1 := int(from[0])
	var y1 := int(from[1])
	var x2 := int(to[0])
	var y2 := int(to[1])
	if y1 == y2 and y1 > 0 and y1 < grid_size.y:
		for x in range(mini(x1, x2), maxi(x1, x2)):
			wall_edges[_edge_key(Vector2i(x, y1 - 1), Vector2i(x, y1))] = true
	if x1 == x2 and x1 > 0 and x1 < grid_size.x:
		for y in range(mini(y1, y2), maxi(y1, y2)):
			wall_edges[_edge_key(Vector2i(x1 - 1, y), Vector2i(x1, y))] = true


func _walkable(tile: Vector2i) -> bool:
	return tile.x >= 0 and tile.y >= 0 and tile.x < grid_size.x and tile.y < grid_size.y and not blocked.has(tile)


func _neighbors(tile: Vector2i) -> Array[Vector2i]:
	return [tile + Vector2i.RIGHT, tile + Vector2i.LEFT, tile + Vector2i.UP, tile + Vector2i.DOWN]


func _heuristic(first: Vector2i, second: Vector2i) -> float:
	return absf(first.x - second.x) + absf(first.y - second.y)


func _edge_key(first: Vector2i, second: Vector2i) -> String:
	if first.x < second.x or (first.x == second.x and first.y <= second.y):
		return "%d,%d|%d,%d" % [first.x, first.y, second.x, second.y]
	return "%d,%d|%d,%d" % [second.x, second.y, first.x, first.y]


func _reconstruct(came_from: Dictionary, start: Vector2i, target: Vector2i) -> Array[Vector2i]:
	var path: Array[Vector2i] = []
	var current := target
	while current != start:
		path.push_front(current)
		current = came_from[current]
	return path
