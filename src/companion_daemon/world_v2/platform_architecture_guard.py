"""Static reverse-dependency guard for World v2 platform ingress.

The migration intentionally keeps an archive implementation in the repository,
but a provider event that has selected a World v2 lane must never acquire a
legacy conversation authority on the way in.  This module is deliberately
small and dependency-free so it can run in unit tests and as a repository
verification script without booting either runtime.

It guards three shapes that ordinary import checks miss:

* every concrete World v2 platform adapter is free of legacy runtime imports
  and direct authority references;
* selected platform boundaries can depend on the application/projection seams,
  but cannot import ledger, reducer, Acceptance, or domain Runtime writers; and
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
_WORLD_V2_PACKAGE = "companion_daemon.world_v2"
_BOUNDARY_AUTHORITY_MODULES = frozenset(
    {
        f"{_WORLD_V2_PACKAGE}.accepted_ledger_batch",
        f"{_WORLD_V2_PACKAGE}.ledger",
        f"{_WORLD_V2_PACKAGE}.reducers",
        f"{_WORLD_V2_PACKAGE}.runtime",
        f"{_WORLD_V2_PACKAGE}.sqlite_ledger",
    }
)
# The provider executor needs this one pure, projection-only enforcement check
# immediately before dispatch.  Importing its Runtime class or any other symbol
# from the module remains forbidden at the platform boundary.
_BOUNDARY_ALLOWED_IMPORTS = {
    f"{_WORLD_V2_PACKAGE}.media_delivery_runtime": frozenset(
        {"require_current_media_delivery_approval"}
    )
}
_LEGACY_HTTP_WRITER_PREFIXES = (
    "/proactive",
    "/debug",
    "/world",
    "/world-runtime",
    "/qq/webhook",
    "/world-console",
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


def _qualified_import_from_module(node: ast.ImportFrom) -> str:
    """Resolve World-v2 relative imports for the boundary-only check."""

    module = node.module or ""
    if node.level:
        package_parts = _WORLD_V2_PACKAGE.split(".")
        retained = package_parts[: max(0, len(package_parts) - node.level + 1)]
        parts = (*retained, *(module.split(".") if module else ()))
        return ".".join(parts)
    return module


def _is_boundary_authority_module(module: str) -> bool:
    if any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in _BOUNDARY_AUTHORITY_MODULES
    ):
        return True
    if not module.startswith(f"{_WORLD_V2_PACKAGE}."):
        return False
    leaf = module.removeprefix(f"{_WORLD_V2_PACKAGE}.").split(".", 1)[0]
    return (
        leaf.startswith("ledger_")
        or leaf.endswith("_reducers")
        or leaf.endswith("_runtime")
        or "acceptance" in leaf
    )


def _boundary_authority_imports(
    node: ast.Import | ast.ImportFrom,
) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(
            alias.name for alias in node.names if _is_boundary_authority_module(alias.name)
        )

    module = _qualified_import_from_module(node)
    allowed_names = _BOUNDARY_ALLOWED_IMPORTS.get(module)
    if _is_boundary_authority_module(module):
        imported_names = frozenset(alias.name for alias in node.names)
        if allowed_names is not None and imported_names <= allowed_names:
            return ()
        return (module,)
    if module == _WORLD_V2_PACKAGE:
        return tuple(
            f"{module}.{alias.name}"
            for alias in node.names
            if _is_boundary_authority_module(f"{module}.{alias.name}")
        )
    return ()


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


def scan_v2_platform_boundary_source(
    path: Path, nodes: Iterable[ast.AST] | None = None,
) -> tuple[PlatformArchitectureViolation, ...]:
    """Scan a selected platform boundary without constraining domain internals.

    World-v2 Runtime modules legitimately import their ledger and reducers.  A
    platform host/transport/viewer may instead depend only on the application,
    projection, schema, and executor interfaces.  Keeping this as a separate
    scanner prevents the Phase-7 rule from becoming a package-wide false
    positive while still covering shared registered-route call closures.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = tuple(nodes) if nodes is not None else (tree,)
    violations = list(scan_v2_platform_source(path, selected))
    for node in _walk(selected):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for module in _boundary_authority_imports(node):
            violations.append(
                PlatformArchitectureViolation(
                    path, node.lineno, "world_v2_authority_import", module
                )
            )
    return tuple(dict.fromkeys(violations))


def _module_function(
    tree: ast.Module, *, name: str, path: Path
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise PlatformArchitectureError(f"{path}: expected V2 entry function {name!r}")


def _decorated_route_paths(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[str, ...]:
    paths: list[str] = []
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        target = decorator.func
        if not (
            isinstance(target, ast.Attribute)
            and target.attr in {"get", "post", "put", "patch", "delete", "api_route", "route"}
        ):
            continue
        candidate: ast.expr | None = decorator.args[0] if decorator.args else None
        if candidate is None:
            candidate = next(
                (item.value for item in decorator.keywords if item.arg == "path"), None
            )
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            paths.append(candidate.value)
    return tuple(paths)


def _is_selected_v2_http_path(route: str) -> bool:
    return (
        route in {"/dashboard", "/messages"}
        or route == "/world-v2"
        or route.startswith("/world-v2/")
        or route == "/internal/world-v2"
        or route.startswith("/internal/world-v2/")
    )


def scan_registered_v2_http_source(
    path: Path,
) -> tuple[PlatformArchitectureViolation, ...]:
    """Scan every registered V2 HTTP route and its module-local call closure.

    Route discovery is decorator-driven so adding a new ``/world-v2`` or
    ``/internal/world-v2`` handler automatically expands the guard.  The
    public ``/messages`` route and the default ``/dashboard`` browser surface
    are explicit selected aliases during migration.
    Module-local helpers are followed transitively; moving an Engine call one
    function away must not bypass the architecture contract.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    selected_names = {
        name
        for name, function in functions.items()
        if any(_is_selected_v2_http_path(route) for route in _decorated_route_paths(function))
    }
    if not selected_names:
        raise PlatformArchitectureError(f"{path}: no registered World v2 HTTP routes found")
    pending = list(sorted(selected_names))
    while pending:
        name = pending.pop()
        function = functions[name]
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            called = node.func.id
            if called in functions and called not in selected_names:
                selected_names.add(called)
                pending.append(called)
    selected = tuple(functions[name] for name in sorted(selected_names))
    violations = list(scan_v2_platform_boundary_source(path, selected))
    for node in _walk(selected):
        if isinstance(node, ast.Name) and node.id == "engine":
            violations.append(
                PlatformArchitectureViolation(
                    path, node.lineno, "legacy_http_authority_reference", "engine"
                )
            )
    return tuple(dict.fromkeys(violations))


def scan_default_http_route_paths(
    paths: Iterable[str], *, source_path: Path,
) -> tuple[PlatformArchitectureViolation, ...]:
    """Reject archive authority routes in the deployed default ASGI graph.

    This accepts route paths rather than FastAPI objects so the guard remains
    dependency-free and can be used both by black-box composition tests and
    repository verification scripts.  Exact ``/world-v2`` namespaces are
    excluded from the broader historical ``/world`` prefix deliberately.
    """

    violations: list[PlatformArchitectureViolation] = []
    for route in sorted(set(paths)):
        is_v2_namespace = route == "/world-v2" or route.startswith("/world-v2/")
        is_internal_v2 = route == "/internal/world-v2" or route.startswith(
            "/internal/world-v2/"
        )
        if is_v2_namespace or is_internal_v2:
            continue
        if any(
            route == prefix or route.startswith(f"{prefix}/")
            for prefix in _LEGACY_HTTP_WRITER_PREFIXES
        ):
            violations.append(
                PlatformArchitectureViolation(
                    source_path,
                    1,
                    "default_http_legacy_route",
                    route,
                )
            )
    return tuple(violations)


def _literal_string_collection(
    tree: ast.Module, *, assignment_name: str, path: Path,
) -> tuple[str, ...]:
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == assignment_name
            for target in statement.targets
        ):
            continue
        value = statement.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            if value.func.id == "frozenset" and value.args:
                value = value.args[0]
        if isinstance(value, (ast.Set, ast.Tuple, ast.List)):
            items = tuple(
                item.value
                for item in value.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
            if len(items) == len(value.elts):
                return items
    raise PlatformArchitectureError(
        f"{path}: expected literal route collection {assignment_name!r}"
    )


def _scan_default_http_registration(
    path: Path, tree: ast.Module,
) -> tuple[PlatformArchitectureViolation, ...]:
    """Statically evaluate the default composition's explicit route policy."""

    exact = _literal_string_collection(
        tree, assignment_name="_DEFAULT_V2_EXACT_PATHS", path=path
    )
    prefixes = _literal_string_collection(
        tree, assignment_name="_DEFAULT_V2_PREFIXES", path=path
    )
    declared_paths = tuple(
        route
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for route in _decorated_route_paths(node)
    )
    selected = tuple(
        route
        for route in declared_paths
        if route in exact or any(route.startswith(prefix) for prefix in prefixes)
    )
    violations = list(scan_default_http_route_paths(selected, source_path=path))
    composer = _module_function(tree, name="_compose_asgi_app", path=path)
    selector_is_used = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_is_default_v2_route_path"
        for node in ast.walk(composer)
    )
    if not selector_is_used:
        violations.append(
            PlatformArchitectureViolation(
                path,
                composer.lineno,
                "default_http_route_filter_missing",
                "_compose_asgi_app must filter every route through the V2 allowlist",
            )
        )
    default_composed = False
    for statement in tree.body:
        if not (
            isinstance(statement, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "app" for target in statement.targets)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "_compose_asgi_app"
        ):
            continue
        include = next(
            (
                keyword.value
                for keyword in statement.value.keywords
                if keyword.arg == "include_default_v2_routes"
            ),
            None,
        )
        default_composed = isinstance(include, ast.Constant) and include.value is True
    if not default_composed:
        violations.append(
            PlatformArchitectureViolation(
                path,
                1,
                "default_http_composition_missing",
                "app must be composed with include_default_v2_routes=True",
            )
        )
    return tuple(violations)


def _scan_http_entry(repository_root: Path) -> tuple[PlatformArchitectureViolation, ...]:
    path = repository_root / "src/companion_daemon/app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = list(scan_registered_v2_http_source(path))
    violations.extend(_scan_default_http_registration(path, tree))
    # Imports execute before any route.  Shared archive imports remain outside
    # the selected closure during migration, but directly importing a v2 writer
    # at module scope would still hand the HTTP boundary mutable authority.
    module_imports = tuple(
        statement for statement in tree.body if isinstance(statement, (ast.Import, ast.ImportFrom))
    )
    violations.extend(
        item
        for item in scan_v2_platform_boundary_source(path, module_imports)
        if item.rule == "world_v2_authority_import"
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
    return tuple(dict.fromkeys(violations))


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
            prefix_violations = scan_v2_platform_boundary_source(path, prefix)
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
    return scan_v2_platform_boundary_source(path, _v2_branch(create_app, path=path).body)


def _is_platform_boundary_path(path: Path) -> bool:
    """Identify concrete platform seams, not domain/application internals."""

    name = path.name
    return (
        name == "dashboard_projection_adapter.py"
        or name.startswith("platform_")
        or name.endswith("_host.py")
        or name.endswith("_transport.py")
        or name.endswith("_onebot_app.py")
    )


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
        if _is_platform_boundary_path(path):
            violations.extend(scan_v2_platform_boundary_source(path))
    violations.extend(_scan_http_entry(repository_root))
    violations.extend(_scan_qq_entry(repository_root))
    return tuple(dict.fromkeys(violations))


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
    "scan_registered_v2_http_source",
    "scan_default_http_route_paths",
    "scan_v2_platform_architecture",
    "scan_v2_platform_boundary_source",
    "scan_v2_platform_source",
]
