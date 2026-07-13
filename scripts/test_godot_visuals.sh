#!/usr/bin/env sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
"$PYTHON_BIN" scripts/capture_godot_room.py "$@"
