"""Static reverse-dependency guard for World v2 platform ingress.

The migration intentionally keeps an archive implementation in the repository,
but a provider event that has selected a World v2 lane must never acquire a
legacy conversation authority on the way in.  This module is deliberately
small and dependency-free so it can run in unit tests and as a repository
verification script without booting either runtime.

It guards two shapes that ordinary import checks miss:

* every concrete World v2 platform adapter is free of legacy runtime imports
  and direct authority references; and
* the V2-only branches inside shared HTTP/OneBot entry modules return before
  any legacy authority is reached.

This is a negative contract.  Passing it does not claim that every production
adapter has migrated; it only prevents a selected V2 path from silently
falling back to the archived runtime.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_LEGACY_MODULE_PREFIXES = (
    "companion_daemon.engine",
    "companion_daemon.world",
    "companion_daemon.companion_turn",
    "companion_daemon.runtime",
    "companion_daemon.qq_websocket",
    "companion_daemon.world_clock",
)
_FORBIDDEN_AUTHORITY_SYMBOLS = frozenset(
    {
        "CompanionEngine",
        "Engine",
        "WorldKernel",
        "CompanionTurn",
        "QQMessageCoalescer",
        "WorldClockDriver",
        "_handle_world_message",
        "build_companion_engine",
    }
)


@dataclass(frozen=True, slots=True)
class PlatformArchitectureViolation:
    """One source-level reason a selected V2 lane is unsafe."""

    path: Path
    lineno: int
    rule: str
    detail: str

    def render(self, *, repository_root: Path) -> str:
        try:
            rendered_path = self.path.relative_to(repository_root)
        except ValueError:
            rendered_path = self.path
        return f"{rendered_path}:{self.lineno}: {self.rule}: {self.detail}"


class PlatformArchitectureError(RuntimeError):
    """Raised when a V2 platform path points back to legacy authority."""


def _is_legacy_module(module: str) -> bool:
    return any(
        module == prefix or module.startswith(f"{prefix}.") for prefix in _LEGACY_MODULE_PREFIXES
    )


def _imported_modules(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if not node.module:
        return ()
    modules = [node.module]
    # ``from companion_daemon import engine`` is semantically the same
    # forbidden dependency as ``import companion_daemon.engine``.
    if node.module == "companion_daemon":
        modules.extend(f"companion_daemon.{alias.name}" for alias in node.names)
    return tuple(modules)


def _walk(nodes: Iterable[ast.AST]) -> Iterable[ast.AST]:
    for node in nodes:
        yield from ast.walk(node)


def scan_v2_platform_source(
    path: Path, nodes: Iterable[ast.AST] | None = None
) -> tuple[PlatformArchitectureViolation, ...]:
    """Scan all or part of one adapter source file.

    ``nodes`` is intentionally public for shared entry modules such as
    ``app.py`` and ``napcat_cli.py``: their archive routes may retain legacy
    imports, while a selected V2 route may not.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = tuple(nodes) if nodes is not None else (tree,)
    violations: list[PlatformArchitectureViolation] = []
    for node in _walk(selected):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for module in _imported_modules(node):
                if _is_legacy_module(module):
                    violations.append(
                        PlatformArchitectureViolation(path, node.lineno, "legacy_import", module)
                    )
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_AUTHORITY_SYMBOLS:
                        violations.append(
                            PlatformArchitectureViolation(
                                path,
                                node.lineno,
                                "legacy_symbol_import",
                                alias.name,
                            )
                        )
        elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_AUTHORITY_SYMBOLS:
            violations.append(
                PlatformArchitectureViolation(path, node.lineno, "legacy_symbol_reference", node.id)
            )
        elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_AUTHORITY_SYMBOLS:
            violations.append(
                PlatformArchitectureViolation(
                    path, node.lineno, "legacy_symbol_reference", node.attr
                )
            )
    return tuple(violations)


def _module_function(
    tree: ast.Module, *, name: str, path: Path
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise PlatformArchitectureError(f"{path}: expected V2 entry function {name!r}")


def _scan_http_entry(repository_root: Path) -> tuple[PlatformArchitectureViolation, ...]:
    path = repository_root / "src/companion_daemon/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    v2_function_names = (
        "_http_v2_capture",
        "_http_v2_ingress_evidence",
        "post_message",
        "world_v2_tick",
        "world_v2_drain",
        "world_v2_public_room",
    )
    violations = list(
        scan_v2_platform_source(
            path,
            (_module_function(tree, name=name, path=path) for name in v2_function_names),
        )
    )
    # ``app.py`` is a shared module until archive routes can be removed.  Its
    # module initializer is therefore part of the selected HTTP V2 path even
    # though archive handler bodies are not.  Do not permit it to construct a
    # legacy authority before the V2 route has a chance to run.
    for statement in tree.body:
        if isinstance(
            statement,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom),
        ):
            continue
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and node.id in _FORBIDDEN_AUTHORITY_SYMBOLS:
                violations.append(
                    PlatformArchitectureViolation(
                        path, node.lineno, "legacy_eager_bootstrap", node.id
                    )
                )
            elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_AUTHORITY_SYMBOLS:
                violations.append(
                    PlatformArchitectureViolation(
                        path, node.lineno, "legacy_eager_bootstrap", node.attr
                    )
                )
    return tuple(violations)


def _v2_branch(create_app: ast.FunctionDef | ast.AsyncFunctionDef, *, path: Path) -> ast.If:
    for index, statement in enumerate(create_app.body):
        if (
            isinstance(statement, ast.If)
            and isinstance(statement.test, ast.Name)
            and statement.test.id == "world_v2_c2c"
        ):
            if not statement.body or not isinstance(statement.body[-1], ast.Return):
                raise PlatformArchitectureError(
                    f"{path}:{statement.lineno}: World v2 OneBot branch must return before archive setup"
                )
            # A legacy authority before this branch would have run even when
            # the flag selected V2.  Check that prefix too.
            prefix = create_app.body[:index]
            prefix_violations = scan_v2_platform_source(path, prefix)
            if prefix_violations:
                rendered = "\n".join(
                    violation.render(repository_root=path.parents[2])
                    for violation in prefix_violations
                )
                raise PlatformArchitectureError(
                    "World v2 OneBot setup reaches legacy authority before its branch:\n" + rendered
                )
            return statement
    raise PlatformArchitectureError(f"{path}: expected explicit `if world_v2_c2c` branch")


def _scan_qq_entry(repository_root: Path) -> tuple[PlatformArchitectureViolation, ...]:
    path = repository_root / "src/companion_daemon/napcat_cli.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    create_app = _module_function(tree, name="create_app", path=path)
    return scan_v2_platform_source(path, _v2_branch(create_app, path=path).body)


def scan_v2_platform_architecture(
    repository_root: Path,
) -> tuple[PlatformArchitectureViolation, ...]:
    """Return every reverse-dependency violation for selected V2 platform paths."""

    repository_root = repository_root.resolve()
    adapter_dir = repository_root / "src/companion_daemon/world_v2"
    violations: list[PlatformArchitectureViolation] = []
    # World v2 has no legitimate dependency on the archived conversation
    # authority.  Scanning the complete package, rather than a hand-maintained
    # adapter list, makes a new host/transport/viewer module join this guard
    # automatically and closes the registration loophole.
    for path in sorted(adapter_dir.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        violations.extend(scan_v2_platform_source(path))
    violations.extend(_scan_http_entry(repository_root))
    violations.extend(_scan_qq_entry(repository_root))
    return tuple(violations)


def assert_v2_platform_architecture(repository_root: Path) -> None:
    """Raise a readable error if a selected V2 platform lane regresses."""

    violations = scan_v2_platform_architecture(repository_root)
    if violations:
        details = "\n".join(
            violation.render(repository_root=repository_root) for violation in violations
        )
        raise PlatformArchitectureError(
            "World v2 platform reverse-dependency guard failed:\n" + details
        )


__all__ = [
    "PlatformArchitectureError",
    "PlatformArchitectureViolation",
    "assert_v2_platform_architecture",
    "scan_v2_platform_architecture",
    "scan_v2_platform_source",
]
