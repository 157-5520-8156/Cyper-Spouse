"""P0 acceptance for the closed vertical registry and its hard gate.

Covered: the coverage assertion holds against the real tree with zero drift,
drift is detected with a message naming the file to fix, the composition root
refuses to build without the assertion, and the P3 discipline holds early —
the frozen hand-written wells never import the framework.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

import pytest

from companion_daemon.world_v2 import vertical_registry
from companion_daemon.world_v2.schemas import TriggerProcess
from companion_daemon.world_v2.vertical_registry import (
    VERTICAL_REGISTRY,
    VerticalRegistryError,
    assert_bounded_vertical_coverage,
)


WORLD_V2 = Path(__file__).parents[2] / "src" / "companion_daemon" / "world_v2"
FRAMEWORK_MODULES = {
    "companion_daemon.world_v2.bounded_decision_vertical",
    "companion_daemon.world_v2.vertical_registry",
}


def test_registry_covers_the_tree_with_zero_drift() -> None:
    assert_bounded_vertical_coverage()


def test_every_process_kind_has_exactly_one_owner_row() -> None:
    literal = set(get_args(TriggerProcess.model_fields["process_kind"].annotation))
    owned: dict[str, str] = {}
    for row in VERTICAL_REGISTRY:
        for kind in row.process_kinds:
            assert kind not in owned, f"{kind} owned by {owned[kind]} and {row.lane_id}"
            owned[kind] = row.lane_id
    assert set(owned) == literal


def test_drift_detection_names_the_file_that_must_change(monkeypatch) -> None:
    trimmed = tuple(
        row for row in VERTICAL_REGISTRY if row.lane_id != "afterthought"
    )
    monkeypatch.setattr(vertical_registry, "VERTICAL_REGISTRY", trimmed)
    with pytest.raises(VerticalRegistryError) as caught:
        assert_bounded_vertical_coverage()
    message = str(caught.value)
    assert "schemas.py" in message
    assert "afterthought_author" in message


def test_framework_hard_gate_requires_a_resolvable_spec(monkeypatch) -> None:
    """A non-hand-rolled row must resolve to a real VerticalSpec surface."""

    import dataclasses

    broken = tuple(
        dataclasses.replace(item, spec_builder="does_not_exist")
        if item.lane_id == "quick_reaction"
        else item
        for item in VERTICAL_REGISTRY
    )
    monkeypatch.setattr(vertical_registry, "VERTICAL_REGISTRY", broken)
    with pytest.raises(VerticalRegistryError) as caught:
        assert_bounded_vertical_coverage()
    assert "does_not_exist" in str(caught.value)


def test_composition_root_asserts_coverage_before_building() -> None:
    source = (WORLD_V2 / "production_turn_application.py").read_text(encoding="utf-8")
    build_start = source.index("def build_sqlite_world_v2_turn_application")
    ledger_construction = source.index("ledger = SQLiteWorldLedger(", build_start)
    assertion = source.find("assert_bounded_vertical_coverage()", build_start)
    assert assertion != -1, "the composition root no longer asserts registry coverage"
    assert assertion < ledger_construction, (
        "registry coverage must be asserted before any resource is built"
    )


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.level:
                imported.add(f"companion_daemon.world_v2.{node.module}")
            else:
                imported.add(node.module)
    return imported


def test_hand_rolled_wells_never_import_the_framework() -> None:
    """P3 discipline, raised early: framework evolution must never require a
    hand-written well to change; the frozen pilot twins especially must stay
    byte-stable for the hot-rollback window."""

    violations: list[str] = []
    for row in VERTICAL_REGISTRY:
        if not row.hand_rolled or row.shape == "infrastructure":
            # Infrastructure plumbing (runtime/settlement/clock) legitimately
            # references the framework for the composition switch; the
            # discipline binds decision wells only.
            continue
        path = WORLD_V2 / row.module
        if not path.exists():
            violations.append(f"{row.lane_id}: module {row.module} is missing")
            continue
        forbidden = _imported_modules(path) & FRAMEWORK_MODULES
        if forbidden:
            violations.append(f"{row.module}: imports {sorted(forbidden)}")
    # The frozen pilot twins are hand-written implementations that no longer
    # own a registry row of their own; guard them explicitly.
    for frozen_twin in ("quick_reaction.py", "afterthought_author.py"):
        forbidden = _imported_modules(WORLD_V2 / frozen_twin) & FRAMEWORK_MODULES
        if forbidden:
            violations.append(f"{frozen_twin}: imports {sorted(forbidden)}")
    assert violations == []


def test_framework_editions_do_not_import_the_frozen_twins() -> None:
    """The coexistence window must not create a hidden coupling: deleting the
    hand-written files later must not break the framework editions."""

    frozen = {
        "companion_daemon.world_v2.quick_reaction",
        "companion_daemon.world_v2.afterthought_author",
    }
    for module in ("quick_reaction_vertical.py", "afterthought_author_vertical.py"):
        overlap = _imported_modules(WORLD_V2 / module) & frozen
        assert not overlap, f"{module} imports the frozen twin(s): {sorted(overlap)}"


def test_pilot_rollback_switch_reads_the_environment(monkeypatch) -> None:
    from companion_daemon.world_v2.production_turn_application import _bdv_pilot_disabled

    monkeypatch.delenv("WORLD_V2_BDV_PILOT_DISABLED", raising=False)
    assert _bdv_pilot_disabled() is False
    for value in ("1", "true", "YES", " on "):
        monkeypatch.setenv("WORLD_V2_BDV_PILOT_DISABLED", value)
        assert _bdv_pilot_disabled() is True, value
    for value in ("", "0", "false", "off"):
        monkeypatch.setenv("WORLD_V2_BDV_PILOT_DISABLED", value)
        assert _bdv_pilot_disabled() is False, value


def test_frozen_twin_identity_constants_stay_equal() -> None:
    """Both editions must keep byte-identical proposal namespaces while the
    rollback window is open."""

    from companion_daemon.world_v2.afterthought_author import (
        AFTERTHOUGHT_PROPOSAL_PREFIX as hand_afterthought,
    )
    from companion_daemon.world_v2.afterthought_author_vertical import (
        AFTERTHOUGHT_PROPOSAL_PREFIX as framework_afterthought,
    )
    from companion_daemon.world_v2.quick_reaction import (
        QUICK_REACTION_PROPOSAL_PREFIX as hand_quick,
    )
    from companion_daemon.world_v2.quick_reaction_vertical import (
        QUICK_REACTION_PROPOSAL_PREFIX as framework_quick,
    )

    assert hand_quick == framework_quick
    assert hand_afterthought == framework_afterthought
