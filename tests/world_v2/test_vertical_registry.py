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
        row for row in VERTICAL_REGISTRY if row.lane_id != "afterthought_replay"
    )
    monkeypatch.setattr(vertical_registry, "VERTICAL_REGISTRY", trimmed)
    with pytest.raises(VerticalRegistryError) as caught:
        assert_bounded_vertical_coverage()
    message = str(caught.value)
    assert "schemas.py" in message
    assert "afterthought_author" in message


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
    """Framework evolution must never require a hand-written well to change."""

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
    assert violations == []


def test_retired_quick_reaction_author_has_no_registry_or_module_surface() -> None:
    assert all(item.lane_id != "quick_reaction" for item in VERTICAL_REGISTRY)
    assert not (WORLD_V2 / "quick_reaction.py").exists()
    assert not (WORLD_V2 / "quick_reaction_vertical.py").exists()


def test_retired_afterthought_process_kind_is_replay_only() -> None:
    row = next(item for item in VERTICAL_REGISTRY if item.lane_id == "afterthought_replay")

    assert row.process_kinds == ("afterthought_author",)
    assert row.shape == "infrastructure"
    assert row.runtime_drain_markers == ()
    assert row.composition_markers == ()
    assert not (WORLD_V2 / "afterthought_author_vertical.py").exists()


def test_retired_external_result_author_is_replay_only() -> None:
    row = next(item for item in VERTICAL_REGISTRY if item.lane_id == "external_result")

    assert row.process_kinds == ("external_result_deliberation",)
    assert row.shape == "infrastructure"
    assert row.module == "reducers.py"
    assert row.runtime_drain_markers == ()
    assert row.composition_markers == ()
    assert "retired" in row.drain_site
    assert not (WORLD_V2 / "external_result_trigger_runtime.py").exists()


def test_live_affect_and_historical_trigger_are_registered_separately() -> None:
    live = next(item for item in VERTICAL_REGISTRY if item.lane_id == "affect")
    replay = next(
        item
        for item in VERTICAL_REGISTRY
        if item.lane_id == "affect_deliberation_replay"
    )

    assert live.shape == "inline_once"
    assert live.module == "immediate_emotion_proposal_worker.py"
    assert live.grammar_lanes == ("affect",)
    assert live.process_kinds == ()
    assert replay.shape == "infrastructure"
    assert replay.module == "reducers.py"
    assert replay.process_kinds == ("affect_deliberation",)
    assert replay.runtime_drain_markers == ()
    assert replay.composition_markers == ()
    assert "retired" in replay.drain_site
