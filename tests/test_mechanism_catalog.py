from pathlib import Path

import pytest

from companion_daemon.mechanism_catalog import CatalogValidationError, verify_mechanism_catalog


def test_repository_mechanism_catalog_is_complete_and_traceable() -> None:
    report = verify_mechanism_catalog(
        Path("configs/mechanism_closure.yaml"),
        repo_root=Path.cwd(),
    )

    assert report.schema_version == 2
    assert report.mechanism_count >= 12
    assert report.errors == ()


def test_v2_catalog_requires_runtime_scope_and_explicit_limitations(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
schema_version: 2
world_only_scopes: [src/example.py]
legacy_behavior_writers: [save_mood_state]
mechanisms:
  - id: incomplete-v2-mechanism
    status: deferred
    sources: []
    events: []
    reducers: []
    projections: []
    decision_consumers: []
    actions: []
    terminal_states: []
    adapters: []
    tests: []
""".strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("# reducer\n")

    with pytest.raises(CatalogValidationError, match="runtime_authority"):
        verify_mechanism_catalog(catalog, repo_root=tmp_path)


def test_closed_action_mechanism_requires_every_action_terminal_state(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
schema_version: 1
world_only_scopes: [src/example.py]
legacy_behavior_writers: [save_mood_state]
mechanisms:
  - id: outbound
    status: closed
    sources: [UserMessageObserved]
    events: [OutgoingActionQueued]
    reducers: [src/example.py]
    projections: [outbox]
    decision_consumers: [sender]
    actions: [reply]
    terminal_states: [delivered, failed]
    adapters: [qq]
    tests: [tests/test_example.py::test_outbound]
""".strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("# reducer\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_example.py").write_text("def test_outbound():\n    pass\n")

    with pytest.raises(CatalogValidationError, match="terminal_states"):
        verify_mechanism_catalog(catalog, repo_root=tmp_path)


def test_non_deferred_mechanism_requires_test_evidence(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
schema_version: 1
world_only_scopes: [src/example.py]
legacy_behavior_writers: [save_mood_state]
mechanisms:
  - id: projection-only
    status: structure
    sources: [Seed]
    events: [SeedLoaded]
    reducers: [src/example.py]
    projections: [read_model]
    decision_consumers: [reader]
    actions: []
    terminal_states: []
    adapters: []
    tests: []
""".strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/example.py").write_text("# reducer\n")

    with pytest.raises(CatalogValidationError, match="tests must not be empty"):
        verify_mechanism_catalog(catalog, repo_root=tmp_path)


def test_catalog_event_must_be_consumed_by_a_declared_reducer(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
schema_version: 1
world_only_scopes: [src/reducer.py::reduce_event]
legacy_behavior_writers: [save_mood_state]
mechanisms:
  - id: segmented-actions
    status: partial
    sources: [ReplyPlan]
    events: [ActionSegmentsPlanned]
    reducers: [src/reducer.py::reduce_event]
    projections: [actions]
    decision_consumers: [sender]
    actions: [reply]
    terminal_states: [delivered, failed, cancelled, expired, unknown]
    adapters: [fake]
    tests: [tests/test_example.py::test_example]
""".strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/reducer.py").write_text(
        'def inspect_only(event):\n'
        '    return event.event_type == "ActionSegmentsPlanned"\n\n'
        "def reduce_event(state, event):\n    return state\n"
    )
    (tmp_path / "src/producer.py").write_text(
        'def emit():\n    return [("ActionSegmentsPlanned", {})]\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_example.py").write_text("def test_example():\n    pass\n")

    with pytest.raises(CatalogValidationError, match="not consumed by declared reducers"):
        verify_mechanism_catalog(catalog, repo_root=tmp_path)


def test_catalog_event_must_have_a_production_emission_point(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
schema_version: 1
world_only_scopes: [src/reducer.py::reduce_event]
legacy_behavior_writers: [save_mood_state]
mechanisms:
  - id: ghost-event
    status: partial
    sources: [Command]
    events: [NeverEmitted]
    reducers: [src/reducer.py::reduce_event]
    projections: [state]
    decision_consumers: [reader]
    actions: []
    terminal_states: []
    adapters: []
    tests: [tests/test_example.py::test_example]
""".strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/reducer.py").write_text(
        'DECLARED_BUT_NOT_EMITTED = ("NeverEmitted", {})\n\n'
        'def reduce_event(state, event):\n'
        '    if event.event_type == "NeverEmitted":\n'
        '        return {**state, "seen": True}\n'
        '    return state\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_example.py").write_text("def test_example():\n    pass\n")

    with pytest.raises(CatalogValidationError, match="no production emission"):
        verify_mechanism_catalog(catalog, repo_root=tmp_path)


def test_world_only_scope_cannot_call_a_declared_legacy_behavior_writer(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(
        """
schema_version: 1
world_only_scopes: [src/world_path.py::world_turn]
legacy_behavior_writers: [CompanionStore.save_mood_state]
mechanisms:
  - id: deferred
    status: deferred
    sources: []
    events: []
    reducers: []
    projections: []
    decision_consumers: []
    actions: []
    terminal_states: []
    adapters: []
    tests: []
""".strip()
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src/world_path.py").write_text(
        "from legacy import save_mood_state as persist_old_mood\n\n"
        "def world_turn(store):\n"
        '    persist_old_mood("geoff", {})\n'
    )

    with pytest.raises(CatalogValidationError, match="save_mood_state"):
        verify_mechanism_catalog(catalog, repo_root=tmp_path)
