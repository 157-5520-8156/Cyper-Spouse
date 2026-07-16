"""Validation for the machine-readable world mechanism closure catalog."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ACTION_TERMINAL_STATES = frozenset(
    {"delivered", "failed", "cancelled", "expired", "unknown"}
)
MECHANISM_STATUSES = frozenset({"closed", "partial", "structure", "deferred"})
RUNTIME_ACTIVATIONS = frozenset(
    {"default", "limited-production", "harness", "not-wired"}
)
_REQUIRED_FIELDS = (
    "sources",
    "events",
    "reducers",
    "projections",
    "decision_consumers",
    "actions",
    "terminal_states",
    "adapters",
    "tests",
)
_V2_REQUIRED_FIELDS = ("phase", "runtime_activation", "limitations")


class CatalogValidationError(ValueError):
    """Raised when catalog evidence is malformed or cannot be located."""


@dataclass(frozen=True)
class CatalogVerificationReport:
    schema_version: int
    mechanism_count: int
    errors: tuple[str, ...]


def verify_mechanism_catalog(
    catalog_path: Path,
    *,
    repo_root: Path,
) -> CatalogVerificationReport:
    """Verify catalog shape and that file/test evidence exists in this checkout."""

    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise CatalogValidationError("catalog root must be a mapping")
    schema_version = raw.get("schema_version")
    if schema_version not in {1, 2}:
        raise CatalogValidationError(f"unsupported schema_version: {schema_version!r}")
    mechanisms = raw.get("mechanisms")
    if not isinstance(mechanisms, list) or not mechanisms:
        raise CatalogValidationError("mechanisms must be a non-empty list")

    errors: list[str] = []
    if schema_version == 2:
        _verify_v2_runtime_authority(raw, errors)
    world_only_scopes = _validated_string_list(
        raw, "world_only_scopes", label="catalog", errors=errors
    )
    legacy_behavior_writers = _validated_string_list(
        raw, "legacy_behavior_writers", label="catalog", errors=errors
    )
    if not world_only_scopes:
        errors.append("catalog.world_only_scopes must not be empty")
    if not legacy_behavior_writers:
        errors.append("catalog.legacy_behavior_writers must not be empty")
    if len(world_only_scopes) != len(set(world_only_scopes)):
        errors.append("catalog.world_only_scopes must not contain duplicates")
    if len(legacy_behavior_writers) != len(set(legacy_behavior_writers)):
        errors.append("catalog.legacy_behavior_writers must not contain duplicates")
    production_emissions = _production_event_emissions(repo_root, errors)
    v2_event_literals = _v2_event_literals(repo_root, errors) if schema_version == 2 else set()
    seen_ids: set[str] = set()
    for index, mechanism in enumerate(mechanisms):
        label = f"mechanisms[{index}]"
        if not isinstance(mechanism, dict):
            errors.append(f"{label} must be a mapping")
            continue
        mechanism_id = mechanism.get("id")
        if not isinstance(mechanism_id, str) or not mechanism_id.strip():
            errors.append(f"{label}.id must be a non-empty string")
            mechanism_id = label
        elif mechanism_id in seen_ids:
            errors.append(f"duplicate mechanism id: {mechanism_id}")
        seen_ids.add(str(mechanism_id))
        label = str(mechanism_id)

        status = mechanism.get("status")
        if status not in MECHANISM_STATUSES:
            errors.append(f"{label}.status must be one of {sorted(MECHANISM_STATUSES)}")
        for field in _REQUIRED_FIELDS:
            value = mechanism.get(field)
            if not isinstance(value, list):
                errors.append(f"{label}.{field} must be a list")
            elif any(not isinstance(item, str) or not item.strip() for item in value):
                errors.append(f"{label}.{field} entries must be non-empty strings")
        if schema_version == 2:
            _verify_v2_mechanism_fields(mechanism, label=label, errors=errors)

        actions = _string_list(mechanism.get("actions"))
        terminal_states = set(_string_list(mechanism.get("terminal_states")))
        if status == "closed" and actions and terminal_states != ACTION_TERMINAL_STATES:
            errors.append(
                f"{label}.terminal_states must contain exactly "
                f"{sorted(ACTION_TERMINAL_STATES)} for a closed action mechanism"
            )

        if status != "deferred":
            for field in (
                "sources",
                "projections",
                "decision_consumers",
                "tests",
            ):
                if not _string_list(mechanism.get(field)):
                    errors.append(f"{label}.{field} must not be empty when status={status}")

        events = _string_list(mechanism.get("events"))
        reducers = _string_list(mechanism.get("reducers"))
        if status in {"closed", "partial"} and not events:
            errors.append(f"{label}.events must not be empty when status={status}")
        if events and not reducers:
            errors.append(f"{label}.reducers must not be empty when events are declared")

        reducer_consumption: set[str] = set()
        for reducer in reducers:
            reducer_path, separator, reducer_scope = reducer.partition("::")
            absolute_reducer_path = repo_root / reducer_path
            if not absolute_reducer_path.is_file():
                errors.append(f"{label}.reducers missing file: {reducer_path}")
                continue
            tree = _parse_python(absolute_reducer_path, errors, label=f"{label}.reducers")
            if tree is not None:
                reducer_node: ast.AST = tree
                if separator:
                    resolved = _resolve_qualified_scope(tree, reducer_scope)
                    if resolved is None:
                        errors.append(f"{label}.reducers missing scope: {reducer}")
                        continue
                    reducer_node = resolved
                reducer_consumption.update(_consumed_event_names(reducer_node))
        for event_name in events:
            if schema_version == 2:
                # V2 reducers are registered through a catalog and several
                # event families are expanded from typed payload registries.
                # The v1 AST heuristic (a direct `event_type ==` comparison
                # plus a tuple literal emission) would incorrectly call those
                # live paths absent.  Require the event to be present in the
                # installed v2 source artifact instead; individual evidence
                # tests remain mandatory per mechanism.
                if event_name not in v2_event_literals:
                    errors.append(f"{label}.events {event_name} is not present in world_v2 source")
            else:
                if event_name not in reducer_consumption:
                    errors.append(
                        f"{label}.events {event_name} is not consumed by declared reducers"
                    )
                if event_name not in production_emissions:
                    errors.append(f"{label}.events {event_name} has no production emission")
        for node_id in _string_list(mechanism.get("tests")):
            _verify_test_node(label, node_id, repo_root, errors)

    _verify_world_only_scopes(
        world_only_scopes,
        legacy_behavior_writers=legacy_behavior_writers,
        repo_root=repo_root,
        errors=errors,
    )

    if errors:
        raise CatalogValidationError("; ".join(errors))
    return CatalogVerificationReport(
        schema_version=schema_version,
        mechanism_count=len(mechanisms),
        errors=(),
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _verify_v2_runtime_authority(raw: dict[str, Any], errors: list[str]) -> None:
    """Require the deployment caveats that make a v2 closure catalog honest.

    Version 1 catalogs only proved that a list of old-world mechanisms named
    files and tests.  A v2 catalog must additionally state which runtime owns
    routed writes and whether its evidence is production, harness-only, or
    still external.  This is deliberately metadata validation: it prevents a
    mechanically green catalog from silently becoming a product-completion
    claim.
    """

    authority = raw.get("runtime_authority")
    if not isinstance(authority, dict):
        errors.append("catalog.runtime_authority must be a mapping for schema_version=2")
        return
    canonical = authority.get("canonical_v2_write_model")
    if not isinstance(canonical, str) or "world_v2" not in canonical:
        errors.append("catalog.runtime_authority.canonical_v2_write_model must name world_v2")
    hosts = authority.get("v2_ingress_hosts")
    if not _string_list(hosts):
        errors.append("catalog.runtime_authority.v2_ingress_hosts must not be empty")
    activation = authority.get("deployment_activation")
    if activation not in RUNTIME_ACTIVATIONS:
        errors.append(
            "catalog.runtime_authority.deployment_activation must be one of "
            f"{sorted(RUNTIME_ACTIVATIONS)}"
        )
    for field in ("legacy_status", "external_evidence"):
        if not isinstance(authority.get(field), str) or not authority[field].strip():
            errors.append(f"catalog.runtime_authority.{field} must be a non-empty string")


def _verify_v2_mechanism_fields(
    mechanism: dict[str, Any], *, label: str, errors: list[str]
) -> None:
    for field in _V2_REQUIRED_FIELDS:
        if field not in mechanism:
            errors.append(f"{label}.{field} is required for schema_version=2")
    phase = mechanism.get("phase")
    if not isinstance(phase, int) or isinstance(phase, bool) or not 0 <= phase <= 8:
        errors.append(f"{label}.phase must be an integer from 0 through 8")
    activation = mechanism.get("runtime_activation")
    if activation not in RUNTIME_ACTIVATIONS:
        errors.append(
            f"{label}.runtime_activation must be one of {sorted(RUNTIME_ACTIVATIONS)}"
        )
    limitations = mechanism.get("limitations")
    if not isinstance(limitations, list) or not limitations:
        errors.append(f"{label}.limitations must be a non-empty list")
    elif any(not isinstance(item, str) or not item.strip() for item in limitations):
        errors.append(f"{label}.limitations entries must be non-empty strings")


def _validated_string_list(
    raw: dict[str, Any],
    field: str,
    *,
    label: str,
    errors: list[str],
) -> list[str]:
    value = raw.get(field)
    if not isinstance(value, list):
        errors.append(f"{label}.{field} must be a list")
        return []
    if any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{label}.{field} entries must be non-empty strings")
    return _string_list(value)


def _parse_python(path: Path, errors: list[str], *, label: str) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        errors.append(f"{label} cannot parse {path}: {exc}")
        return None


def _consumed_event_names(tree: ast.AST) -> set[str]:
    consumed: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            expressions = [node.left, *node.comparators]
            if any(_is_event_type_expression(item) for item in expressions):
                for expression in expressions:
                    consumed.update(_string_literals(expression))
        elif isinstance(node, ast.Match) and _is_event_type_expression(node.subject):
            for case in node.cases:
                consumed.update(_string_literals(case.pattern))
    return consumed


def _is_event_type_expression(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "event_type" or (
        isinstance(node, ast.Name) and node.id == "event_type"
    )


def _string_literals(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


def _production_event_emissions(repo_root: Path, errors: list[str]) -> set[str]:
    source_root = repo_root / "src"
    emitted: set[str] = set()
    if not source_root.is_dir():
        errors.append("catalog production source root is missing: src")
        return emitted
    for path in sorted(source_root.rglob("*.py")):
        tree = _parse_python(path, errors, label="production source")
        if tree is None:
            continue
        visitor = _EventEmissionVisitor()
        visitor.visit(tree)
        emitted.update(visitor.emitted)
    return emitted


def _v2_event_literals(repo_root: Path, errors: list[str]) -> set[str]:
    """Return string literals in the installed v2 artifact.

    This deliberately stays static: the verifier must not instantiate a world
    or import a live adapter merely to check documentation.  It is used only by
    schema v2, whose per-mechanism tests and reducer paths are separately
    checked above.
    """

    source_root = repo_root / "src" / "companion_daemon" / "world_v2"
    if not source_root.is_dir():
        errors.append("catalog world_v2 source root is missing")
        return set()
    literals: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        tree = _parse_python(path, errors, label="world_v2 source")
        if tree is None:
            continue
        literals.update(_string_literals(tree))
    return literals


class _EventEmissionVisitor(ast.NodeVisitor):
    """Find event pairs only where code returns or appends them for projection."""

    def __init__(self) -> None:
        self.emitted: set[str] = set()

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
        if node.value is not None:
            self.emitted.update(_event_pairs_in(node.value))
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        if any(_is_event_collection_target(target) for target in node.targets):
            self.emitted.update(_event_pairs_in(node.value))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is not None and _is_event_collection_target(node.target):
            self.emitted.update(_event_pairs_in(node.value))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        call_name = _call_leaf_name(node.func)
        if call_name == "emit" and node.args:
            if event_name := _constant_string(node.args[0]):
                self.emitted.add(event_name)
        elif call_name in {"append", "extend", "_append_and_project"}:
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                self.emitted.update(_event_pairs_in(argument))
        self.generic_visit(node)


def _event_pairs_in(node: ast.AST) -> set[str]:
    return {
        event_name
        for child in ast.walk(node)
        if isinstance(child, ast.Tuple)
        and len(child.elts) == 2
        and (event_name := _constant_string(child.elts[0])) is not None
    }


def _is_event_collection_target(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and (
        "event" in node.id.lower() or node.id.lower() in {"specs", "specifications"}
    )


def _constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_leaf_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _verify_world_only_scopes(
    scopes: list[str],
    *,
    legacy_behavior_writers: list[str],
    repo_root: Path,
    errors: list[str],
) -> None:
    writer_leaf_names = {writer.rsplit(".", 1)[-1] for writer in legacy_behavior_writers}
    for scope in scopes:
        path_text, separator, qualified_name = scope.partition("::")
        path = repo_root / path_text
        if not path.is_file():
            errors.append(f"world_only_scopes missing file: {path_text}")
            continue
        tree = _parse_python(path, errors, label="world_only_scopes")
        if tree is None:
            continue
        import_aliases = _import_aliases(tree)
        scope_node: ast.AST = tree
        if separator:
            resolved = _resolve_qualified_scope(tree, qualified_name)
            if resolved is None:
                errors.append(f"world_only_scopes missing scope: {scope}")
                continue
            scope_node = resolved
        for node in ast.walk(scope_node):
            if not isinstance(node, ast.Call):
                continue
            call_name = _call_leaf_name(node.func)
            resolved_call_name = import_aliases.get(str(call_name), call_name)
            if resolved_call_name in writer_leaf_names:
                errors.append(
                    f"world_only_scopes {scope}:{node.lineno} calls legacy behavior writer "
                    f"{resolved_call_name}"
                )


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for imported in node.names:
            local_name = imported.asname or imported.name
            aliases[local_name] = imported.name.rsplit(".", 1)[-1]
    return aliases


def _resolve_qualified_scope(tree: ast.Module, qualified_name: str) -> ast.AST | None:
    current_body: list[ast.stmt] = tree.body
    current: ast.AST | None = None
    for part in qualified_name.split("."):
        current = next(
            (
                node
                for node in current_body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == part
            ),
            None,
        )
        if current is None:
            return None
        current_body = current.body  # type: ignore[union-attr]
    return current


def _verify_test_node(label: str, node_id: str, repo_root: Path, errors: list[str]) -> None:
    test_path, separator, test_name = node_id.partition("::")
    path = repo_root / test_path
    if not path.is_file():
        errors.append(f"{label}.tests missing file: {test_path}")
        return
    if separator and test_name:
        source = path.read_text(encoding="utf-8")
        leaf_name = test_name.rsplit("::", 1)[-1].split("[", 1)[0]
        if f"def {leaf_name}(" not in source and f"class {leaf_name}:" not in source:
            errors.append(f"{label}.tests missing node: {node_id}")
