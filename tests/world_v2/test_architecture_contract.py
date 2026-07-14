from __future__ import annotations

import ast
from pathlib import Path


WORLD_V2 = Path(__file__).parents[2] / "src" / "companion_daemon" / "world_v2"
FORBIDDEN_MODULES = {
    "companion_daemon.engine",
    "companion_daemon.world",
    "companion_daemon.companion_turn",
    "companion_daemon.qq_websocket",
    "companion_daemon.napcat",
}


def test_world_v2_does_not_import_legacy_or_platform_authorities() -> None:
    violations: list[str] = []
    for path in sorted(WORLD_V2.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = {node.module}
            else:
                continue
            forbidden = imported & FORBIDDEN_MODULES
            if forbidden:
                violations.append(f"{path.name}:{node.lineno}: {sorted(forbidden)}")

    assert violations == []
