#!/usr/bin/env python3
"""Build the deterministic TileRoom runtime bundle."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from companion_daemon.tile_room_compiler import compile_tile_room  # noqa: E402


if __name__ == "__main__":
    source = ROOT / "assets/dashboard/tile-rooms/zhizhi-home/room.json"
    output = ROOT / "assets/dashboard/tile-rooms/zhizhi-home/runtime/room.bundle.json"
    report = compile_tile_room(source, output)
    print(f"built {report.scene_id}: {report.object_count} objects -> {report.output.relative_to(ROOT)}")
