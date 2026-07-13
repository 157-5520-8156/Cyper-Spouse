#!/usr/bin/env sh
set -eu

GODOT_BIN="${GODOT_BIN:-/Applications/Godot.app/Contents/MacOS/Godot}"
"$GODOT_BIN" --headless --path godot --editor --quit
"$GODOT_BIN" --headless --path godot --script res://tests/test_runner.gd
"$GODOT_BIN" --headless --path godot --quit-after 5
