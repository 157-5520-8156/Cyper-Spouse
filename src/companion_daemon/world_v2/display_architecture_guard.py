"""Static contract for selected World v2 display consumers.

The archived dashboard remains in the repository during staged migration, so
this guard deliberately does not inspect that user-interface implementation.
Instead it protects the consumer seam that has actually selected World v2:
the public DTO adapter and the two Godot polling scenes.  Those consumers may
only read a public projection route; they must not grow a ledger/reducer or
archive-dashboard dependency in order to render a room.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


_ADAPTER_RELATIVE_PATH = Path("src/companion_daemon/world_v2/dashboard_projection_adapter.py")
_GODOT_PROJECT_RELATIVE_PATH = Path("godot/project.godot")
_GODOT_CONSUMER_RELATIVE_PATHS = (
    Path("godot/scripts/main.gd"),
    Path("godot/topdown/scripts/topdown_home.gd"),
)
_FORBIDDEN_ADAPTER_TOKENS = (
    "companion_daemon.engine",
    "companion_daemon.dashboard_ui",
    "companion_daemon.world",
    "WorldRuntime",
    "WorldLedger",
    "SQLiteWorldLedger",
    "life_reducers",
    "_ledger",
)
_FORBIDDEN_GODOT_TOKENS = (
    "daemon_context_url",
    "scene_state_from_body(",
    "/debug/",
    "ledger",
    "reducer",
    "CompanionEngine",
)


@dataclass(frozen=True, slots=True)
class DisplayArchitectureViolation:
    """One display consumer that can no longer be treated as a v2 read seam."""

    path: Path
    rule: str
    detail: str

    def render(self, *, repository_root: Path) -> str:
        try:
            rendered_path = self.path.relative_to(repository_root)
        except ValueError:
            rendered_path = self.path
        return f"{rendered_path}: {self.rule}: {self.detail}"


class DisplayArchitectureError(RuntimeError):
    """Raised when a selected v2 display consumer reaches an archived authority."""


def scan_v2_display_architecture(
    repository_root: Path,
) -> tuple[DisplayArchitectureViolation, ...]:
    """Return source-level violations for World v2's selected display seam."""

    repository_root = repository_root.resolve()
    violations: list[DisplayArchitectureViolation] = []
    adapter_path = repository_root / _ADAPTER_RELATIVE_PATH
    adapter_source = adapter_path.read_text(encoding="utf-8")
    adapter_tree = ast.parse(adapter_source, filename=str(adapter_path))
    imported_modules = {
        node.module
        for node in ast.walk(adapter_tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    allowed_imports = {"__future__", "dataclasses", "schemas", "typing"}
    if imported_modules != allowed_imports:
        violations.append(
            DisplayArchitectureViolation(
                adapter_path,
                "adapter_import_surface",
                f"expected {sorted(allowed_imports)!r}, got {sorted(imported_modules)!r}",
            )
        )
    for token in _FORBIDDEN_ADAPTER_TOKENS:
        if token in adapter_source:
            violations.append(
                DisplayArchitectureViolation(adapter_path, "forbidden_adapter_dependency", token)
            )

    project_path = repository_root / _GODOT_PROJECT_RELATIVE_PATH
    project_source = project_path.read_text(encoding="utf-8")
    if 'daemon_room_url="http://127.0.0.1:8767/world-v2/room"' not in project_source:
        violations.append(
            DisplayArchitectureViolation(
                project_path,
                "missing_v2_room_endpoint",
                "daemon_room_url must select /world-v2/room",
            )
        )
    if "daemon_context_url" in project_source:
        violations.append(
            DisplayArchitectureViolation(
                project_path,
                "legacy_room_endpoint",
                "daemon_context_url is not a selected v2 display setting",
            )
        )

    for relative_path in _GODOT_CONSUMER_RELATIVE_PATHS:
        path = repository_root / relative_path
        source = path.read_text(encoding="utf-8")
        for required in ("daemon_room_url", "scene_state_from_public_room_body("):
            if required not in source:
                violations.append(
                    DisplayArchitectureViolation(path, "missing_v2_read_seam", required)
                )
        for token in _FORBIDDEN_GODOT_TOKENS:
            if token in source:
                violations.append(
                    DisplayArchitectureViolation(path, "forbidden_display_dependency", token)
                )
    return tuple(violations)


def assert_v2_display_architecture(repository_root: Path) -> None:
    """Raise one actionable error when selected readers regress to an archive path."""

    violations = scan_v2_display_architecture(repository_root)
    if violations:
        details = "\n".join(
            violation.render(repository_root=repository_root) for violation in violations
        )
        raise DisplayArchitectureError(
            "World v2 display architecture guard failed:\n" + details
        )


__all__ = [
    "DisplayArchitectureError",
    "DisplayArchitectureViolation",
    "assert_v2_display_architecture",
    "scan_v2_display_architecture",
]
