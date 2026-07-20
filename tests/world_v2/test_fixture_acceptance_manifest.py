from __future__ import annotations

import ast
import json
from pathlib import Path
import re

from companion_daemon.world_v2.fixture_acceptance_manifest import (
    FIXTURE_ACCEPTANCE_MANIFEST,
    FIXTURE_ACCEPTANCE_MANIFEST_VERSION,
    export_fixture_acceptance_manifest,
)


ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs/world-v2-refactor-plan.md"
FIXTURE_ID = re.compile(r"`(W2-[A-Z]+-[0-9]{3})`")


def _frozen_plan_ids() -> tuple[str, ...]:
    text = PLAN.read_text(encoding="utf-8")
    table = text.split("### 11.2 首批权威 Fixtures", 1)[1].split(
        "#### 11.2.1 Authority 攻击套件", 1
    )[0]
    return tuple(FIXTURE_ID.findall(table))


def _test_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _production_reachable_paths() -> set[str]:
    source_root = ROOT / "src/companion_daemon/world_v2"
    module_paths = {
        f"companion_daemon.world_v2.{path.stem}": path for path in source_root.glob("*.py")
    }
    graph: dict[Path, set[Path]] = {path: set() for path in module_paths.values()}
    for module_path in module_paths.values():
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level == 1 and node.module:
                dependency = module_paths.get(f"companion_daemon.world_v2.{node.module}")
                if dependency is not None:
                    graph[module_path].add(dependency)
            elif node.level == 0 and node.module:
                dependency = module_paths.get(node.module)
                if dependency is not None:
                    graph[module_path].add(dependency)

    roots = {
        source_root / name
        for name in (
            "production_turn_application.py",
            "http_capture_host.py",
            "qq_c2c_host.py",
            "platform_host.py",
        )
    }
    reachable = set(roots)
    pending = list(roots)
    while pending:
        current = pending.pop()
        for dependency in graph[current]:
            if dependency not in reachable:
                reachable.add(dependency)
                pending.append(dependency)
    return {str(path.relative_to(ROOT)) for path in reachable}


def test_manifest_covers_exactly_the_70_frozen_plan_ids_once() -> None:
    plan_ids = _frozen_plan_ids()
    manifest_ids = tuple(item.fixture_id for item in FIXTURE_ACCEPTANCE_MANIFEST)

    assert len(plan_ids) == len(set(plan_ids)) == 70
    assert len(manifest_ids) == len(set(manifest_ids)) == 70
    assert set(manifest_ids) == set(plan_ids)


def test_every_fixture_has_requirement_mechanism_authority_and_real_test_nodes() -> None:
    for fixture in FIXTURE_ACCEPTANCE_MANIFEST:
        assert fixture.requirement.strip(), fixture.fixture_id
        assert len(fixture.mechanisms) >= 2, fixture.fixture_id
        assert fixture.production_anchors, fixture.fixture_id
        assert fixture.authority_events or fixture.authority_projections, fixture.fixture_id
        assert fixture.test_nodes, fixture.fixture_id
        for node_id in fixture.test_nodes:
            relative_path, separator, function_name = node_id.partition("::")
            assert separator and function_name.startswith("test_"), (fixture.fixture_id, node_id)
            path = ROOT / relative_path
            assert path.is_file(), (fixture.fixture_id, node_id)
            assert function_name in _test_functions(path), (fixture.fixture_id, node_id)


def test_each_production_reachability_claim_has_static_source_evidence() -> None:
    for fixture in FIXTURE_ACCEPTANCE_MANIFEST:
        for anchor in fixture.production_anchors:
            path = ROOT / anchor.path
            assert path.is_file(), (fixture.fixture_id, anchor.path)
            source = path.read_text(encoding="utf-8")
            assert anchor.path.startswith("src/companion_daemon/world_v2/"), (
                fixture.fixture_id,
                anchor.path,
            )
            assert anchor.markers, (fixture.fixture_id, anchor.path)
            for marker in anchor.markers:
                assert marker in source, (fixture.fixture_id, anchor.path, marker)


def test_reachability_is_explicit_and_does_not_promote_tested_modules_to_production() -> None:
    module_only = {
        item.fixture_id
        for item in FIXTURE_ACCEPTANCE_MANIFEST
        if item.production_reachability == "module_only"
    }
    assert module_only == set()
    assert all(
        item.reachability_note
        for item in FIXTURE_ACCEPTANCE_MANIFEST
        if item.production_reachability != "production"
    )
    assert all(
        item.reachability_note is None
        for item in FIXTURE_ACCEPTANCE_MANIFEST
        if item.production_reachability == "production"
    )
    reachable_paths = _production_reachable_paths()
    for fixture in FIXTURE_ACCEPTANCE_MANIFEST:
        if fixture.production_reachability == "production":
            assert any(anchor.path in reachable_paths for anchor in fixture.production_anchors), (
                fixture.fixture_id
            )
    assert {
        item.fixture_id
        for item in FIXTURE_ACCEPTANCE_MANIFEST
        if item.production_reachability == "ci_gate"
    } == {"W2-ARCH-001"}


def test_human_state_fixtures_are_mechanism_chains_not_parameter_only_variants() -> None:
    by_id = {item.fixture_id: item for item in FIXTURE_ACCEPTANCE_MANIFEST}
    chain_ids = {
        "W2-AFF-001",
        "W2-AFF-002",
        "W2-IMP-001",
        "W2-LIFE-001",
        "W2-LIFE-003",
        "W2-MEM-002",
        "W2-RHY-001",
        "W2-RHY-002",
        "W2-INT-001",
        "W2-INT-002",
        "W2-PRO-001",
        "W2-PULSE-001",
    }
    for fixture_id in chain_ids:
        fixture = by_id[fixture_id]
        assert len(fixture.mechanisms) >= 3, fixture_id
        assert len(fixture.authority_events) + len(fixture.authority_projections) >= 2, fixture_id
        # At least one executable node must exercise a runtime/production chain,
        # not merely a schema, matrix value, or evaluator score.
        assert any(
            token in node
            for node in fixture.test_nodes
            for token in (
                "runtime",
                "production",
                "scenario_runner",
                "social_action_vertical",
                "reconsideration",
                "life_projection",
                "appraisal_authority",
                "memory_candidate_authority",
                "thread_authority",
            )
        ), fixture_id


def test_cross_cutting_human_mechanisms_have_explicit_fixture_evidence() -> None:
    expected = {
        "emotion",
        "relationship",
        "memory",
        "npc",
        "controlled_random",
        "resistance",
        "repair",
        "migration",
    }
    tagged: dict[str, list[str]] = {}
    for fixture in FIXTURE_ACCEPTANCE_MANIFEST:
        for tag in fixture.coverage_tags:
            tagged.setdefault(tag, []).append(fixture.fixture_id)

    assert set(tagged) == expected
    assert len(tagged["emotion"]) >= 2
    assert tagged["relationship"] == ["W2-AFF-001"]
    assert tagged["memory"] == ["W2-MEM-002"]
    assert tagged["npc"] == ["W2-LIFE-001"]
    assert tagged["controlled_random"] == ["W2-REP-001"]
    assert set(tagged["resistance"]) == {"W2-AFF-002", "W2-INT-001"}
    assert tagged["repair"] == ["W2-AFF-001"]
    assert tagged["migration"] == ["W2-EXP-004"]


def test_only_real_transport_cost_and_latency_fixtures_remain_hybrid() -> None:
    hybrid = {
        item.fixture_id: item.external_gate
        for item in FIXTURE_ACCEPTANCE_MANIFEST
        if item.acceptance_kind == "hybrid"
    }
    assert hybrid == {
        "W2-RHY-003": "real provider warm/cold P95 requires a separately captured complete transport trace",
        "W2-COST-001": "provider invoice and production-token reconciliation require real provider artifacts",
        "W2-PERF-001": "P95 <= 5s needs 20 complete real-transport warm samples in the target deployment",
    }
    assert all(
        item.external_gate is None
        for item in FIXTURE_ACCEPTANCE_MANIFEST
        if item.acceptance_kind == "internal"
    )


def test_export_is_fully_expanded_json_and_not_test_name_inference() -> None:
    exported = export_fixture_acceptance_manifest()

    assert exported["version"] == FIXTURE_ACCEPTANCE_MANIFEST_VERSION
    assert len(exported["fixtures"]) == 70  # type: ignore[arg-type]
    encoded = json.dumps(exported, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert "W2-AFF-001" in encoded
    assert "production_anchors" in encoded
    assert "authority_events" in encoded
