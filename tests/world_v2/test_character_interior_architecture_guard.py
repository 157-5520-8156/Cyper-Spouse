from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from companion_daemon.world_v2.character_interior import inbound_appraisal_wire
from companion_daemon.world_v2.character_interior_architecture_guard import (
    CharacterInteriorArchitectureError,
    CharacterInteriorArchitectureViolation,
    assert_character_interior_architecture,
    classify_character_interior_violations,
    render_character_interior_architecture_report,
    scan_character_interior_architecture,
    scan_character_interior_source,
)
from companion_daemon.world_v2.character_interior.inbound_turn import InboundTurnFaculty
from companion_daemon.world_v2.character_interior.inbound_author import (
    _InboundCharacterAuthor,
)
from companion_daemon.world_v2.runtime import WorldRuntime

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_inbound_faculty_has_one_character_author_seam() -> None:
    parameters = inspect.signature(InboundTurnFaculty).parameters

    assert "author" in parameters
    assert "cognition" not in parameters
    assert not {"expression", "appraisal", "recovery"} & set(parameters)


def test_inbound_author_has_no_backup_character_author_surface() -> None:
    parameters = inspect.signature(_InboundCharacterAuthor).parameters

    assert not {
        "recovery_model",
        "discover_recovery_model",
    } & set(parameters)
    assert not hasattr(_InboundCharacterAuthor, "_retry_with_recovery_provider")


def test_appraisal_wire_is_not_an_executable_character_author() -> None:
    assert not hasattr(inbound_appraisal_wire, "_AppraisalDraftWire")


def test_world_runtime_exposes_only_unified_inbound_state_settlement() -> None:
    parameters = inspect.signature(WorldRuntime).parameters

    assert "inbound_state_owner" in parameters
    assert "inbound_relationship_worker" in parameters
    assert not {
        "interaction_appraisal_turn",
        "interaction_bid_turn",
        "interaction_bid_worker",
        "interaction_bid_owner",
        "affect_deliberation_owner",
        "affect_worker",
        "read_only_tool_owner",
        "read_only_tool_trigger_runtime",
        "relationship_deliberation_owner",
        "relationship_worker",
    } & set(parameters)


@pytest.mark.parametrize(
    "source",
    (
        'payload = {"character_model_role": "character_model"}\n',
        'payload = {"character_deliberation": audit}\n',
        "def _run_character_choice_phase():\n    pass\n",
    ),
)
def test_guard_rejects_retired_life_development_character_writer(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "life_development_runtime.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == "retired_life_development_character_writer" for item in violations)


def test_character_interior_guard_rejects_a_legacy_character_adapter_import_and_construction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "composition.py"
    path.write_text(
        "from companion_daemon.world_v2.single_call_inbound_cognition import "
        "SingleCallInboundCognition as LegacyTurn\n\n"
        "turn = LegacyTurn()\n",
        encoding="utf-8",
    )

    violations = scan_character_interior_source(path)

    assert {
        "legacy_character_interior_import",
        "legacy_character_interior_construction",
    } <= {item.rule for item in violations}


@pytest.mark.parametrize(
    ("module", "symbol"),
    (
        ("single_call_inbound_cognition", "SingleCallInboundCognition"),
        ("proactive_action", "ProactiveDraftAdapter"),
        ("appraisal_chat_model_adapter", "AppraisalDraftDeliberationAdapter"),
        ("appraisal_chat_model_adapter", "FastAppraisalDraftDeliberationAdapter"),
        (
            "character_interior.inbound_appraisal_wire",
            "_FastAppraisalDraftWire",
        ),
        ("affect_chat_model_adapter", "AffectDraftDeliberationAdapter"),
        ("affect_deliberation_worker", "AffectDeliberationWorker"),
        ("affect_deliberation_worker", "AffectDeliberationWorkResult"),
        ("affect_trigger_runtime", "AffectTriggerRuntime"),
        ("affect_trigger_runtime", "AffectTriggerRunResult"),
        ("affect_trigger", "affect_deliberation_trigger_events"),
        ("affect_trigger", "affect_deliberation_trigger_id"),
        (
            "interaction_appraisal_trigger_runtime",
            "InteractionAppraisalTriggerRuntime",
        ),
        ("interaction_appraisal_trigger_runtime", "AppraisalTriggerRunResult"),
        (
            "relationship_draft_deliberation_adapter",
            "RelationshipDraftDeliberationAdapter",
        ),
        ("private_impression_producer", "PrivateImpressionDraftAdapter"),
        (
            "expression_reconsideration_model_adapter",
            "ExpressionReconsiderationChatModelAdapter",
        ),
        (
            "expression_reconsideration_model_adapter",
            "AuditedReplacementReconsiderationReviewer",
        ),
        ("chat_model_deliberation_adapter", "ChatModelDeliberationAdapter"),
        ("chat_model_deliberation_adapter", "RoutedChatModelDeliberationAdapter"),
        ("single_call_inbound_cognition", "SingleCallAppraisalAdapter"),
        ("single_call_inbound_cognition", "SingleCallExpressionAdapter"),
        ("outcome_draft_deliberation_adapter", "OutcomeDraftDeliberationAdapter"),
        ("outcome_selection_draft", "OutcomeSelectionDraftAdapter"),
        ("aspiration_runtime", "AspirationRuntime"),
        ("aspiration_draft_adapter", "AspirationDraftAdapter"),
        ("aspiration_chat_model_adapter", "AspirationChatModelAdapter"),
        ("contextual_life_inspiration", "ContextualLifeInspirationRuntime"),
        ("future_life_author", "FutureLifeAuthorRuntime"),
        ("life_author_runtime", "LifeAuthorRuntime"),
        ("life_author_runtime", "LifeAuthorResult"),
        ("npc_initiative", "NpcInitiativeRuntime"),
        (
            "perception_result_trigger_runtime",
            "NoopPerceptionResultDeliberator",
        ),
        ("shared_private_invitation", "SharedPrivateInvitationRuntime"),
    ),
)
def test_character_interior_guard_rejects_every_independent_character_author_adapter(
    tmp_path: Path,
    module: str,
    symbol: str,
) -> None:
    path = tmp_path / "composition.py"
    path.write_text(
        f"from .{module} import {symbol} as LegacyAdapter\n\nadapter = LegacyAdapter()\n",
        encoding="utf-8",
    )

    violations = scan_character_interior_source(path)

    assert {
        "legacy_character_interior_import",
        "legacy_character_interior_construction",
    } <= {item.rule for item in violations}


def test_character_interior_guard_resolves_module_alias_construction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "composition.py"
    path.write_text(
        "import companion_daemon.world_v2.single_call_inbound_cognition as legacy_turn\n\n"
        "turn = legacy_turn.SingleCallInboundCognition()\n",
        encoding="utf-8",
    )

    violations = scan_character_interior_source(path)

    assert {item.rule for item in violations} == {
        "legacy_character_interior_import",
        "legacy_character_interior_construction",
    }


@pytest.mark.parametrize(
    "source",
    (
        "class AffectDeliberationWorker:\n    pass\n",
        "class AffectTriggerRuntime:\n    pass\n",
        "class InteractionAppraisalTriggerRuntime:\n    pass\n",
        "def affect_deliberation_trigger_events():\n    pass\n",
    ),
)
def test_guard_rejects_redefining_retired_affect_and_appraisal_runtimes(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "new_runtime.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == "retired_protagonist_author_definition" for item in violations)


def test_character_interior_guard_allows_the_single_new_composition_seam(
    tmp_path: Path,
) -> None:
    path = tmp_path / "composition.py"
    path.write_text(
        "from .character_interior import CharacterInterior\n\n"
        "def compose(*, character_interior: CharacterInterior):\n"
        "    return build(character_interior=character_interior)\n",
        encoding="utf-8",
    )

    assert scan_character_interior_source(path) == ()


@pytest.mark.parametrize(
    "source",
    (
        "def compose(*, actor_model: object):\n    return actor_model\n",
        "def compose(*, npc_actor_model: object):\n    return npc_actor_model\n",
        "lane = RogueSemanticLane(actor_model=model)\n",
        "lane = RogueSemanticLane(npc_actor_model=model)\n",
        "self._actor_model = model\n",
    ),
)
def test_guard_rejects_unregistered_actor_semantic_domains(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "orphan_actor_lane.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == "unregistered_actor_semantic_domain" for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        "runtime = WorldRuntime(quick_reaction_worker=worker)\n",
        "class Config:\n    quick_reaction_enabled: bool = False\n",
        "self._quick_reaction_worker = worker\n",
        "from .quick_reaction import QuickReactionWorker\n",
        "def _bdv_pilot_disabled():\n    return False\n",
    ),
)
def test_guard_rejects_every_retired_quick_reaction_production_surface(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "composition.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == "retired_quick_reaction_surface" for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        "turn = InteractionBidDeliberationTurn(ledger=ledger)\n",
        "runtime = InteractionBidTriggerRuntime(ledger=ledger)\n",
        "worker = InteractionBidProposalWorker(ledger=ledger)\n",
        "deliberation = compose_injected_read_only_tool_deliberation(router=router, model=model)\n",
        "runtime = ReadOnlyToolTriggerRuntime(ledger=ledger)\n",
    ),
)
def test_guard_rejects_recomposing_retired_dormant_character_lanes(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "production_turn_application.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == "retired_dormant_character_lane" for item in violations)


@pytest.mark.parametrize(
    ("source", "expected_rule"),
    (
        (
            "def compose(*, appraisal_model: object):\n    return None\n",
            "legacy_character_interior_signature",
        ),
        (
            "def compose(*, character_interior: CharacterInterior, "
            "appraisal_model: object):\n    return None\n",
            "mixed_character_interior_signature",
        ),
        (
            "def compose(*, character_interior: CharacterInterior, "
            "outcome_draft_model: object):\n    return None\n",
            "mixed_character_interior_signature",
        ),
        (
            "result = build_sqlite_world_v2_turn_application("
            "character_interior=interior, outcome_model=model)\n",
            "mixed_character_interior_call",
        ),
        (
            "def compose(*, compiler: 'ProactiveDraftAdapter | None'):\n    return None\n",
            "legacy_character_interior_signature",
        ),
        (
            "result = build_sqlite_world_v2_turn_application(appraisal_model=model)\n",
            "legacy_character_interior_call",
        ),
        (
            "result = build_sqlite_world_v2_turn_application("
            "character_interior=interior, proactive_model=model)\n",
            "mixed_character_interior_call",
        ),
        (
            "class Composition:\n"
            "    character_interior: CharacterInterior\n"
            "    appraisal_model: object\n",
            "mixed_character_interior_signature",
        ),
        (
            "class Config:\n    aspiration_enabled: bool = True\n",
            "legacy_character_interior_signature",
        ),
        (
            "def compose(*, life_author_followup: object):\n    return None\n",
            "legacy_character_interior_signature",
        ),
        (
            "def compose(*, character_interior: CharacterInterior, "
            "aspiration_model: object):\n    return None\n",
            "mixed_character_interior_signature",
        ),
        (
            "def compose(*, aspiration_adapter: object):\n    return None\n",
            "legacy_character_interior_signature",
        ),
        (
            "def compose(*, character_interior: CharacterInterior, "
            "interaction_bid_model: object):\n    return None\n",
            "mixed_character_interior_signature",
        ),
        (
            "result = build_sqlite_world_v2_turn_application("
            "character_interior=interior, read_only_tool_model=model)\n",
            "mixed_character_interior_call",
        ),
        (
            "def compose(*, read_only_tool_transport: object):\n    return None\n",
            "legacy_character_interior_signature",
        ),
        (
            "def compose(*, character_interior: CharacterInterior, "
            "legacy_event_media_planner: object):\n    return None\n",
            "mixed_character_interior_signature",
        ),
        (
            "result = build_sqlite_world_v2_turn_application("
            "character_interior=interior, event_media_result_store=store)\n",
            "mixed_character_interior_call",
        ),
    ),
)
def test_character_interior_guard_rejects_legacy_or_mixed_composition_surfaces(
    tmp_path: Path,
    source: str,
    expected_rule: str,
) -> None:
    path = tmp_path / "composition.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == expected_rule for item in violations)


def test_character_interior_guard_reports_all_regressions_in_one_operator_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import companion_daemon.world_v2.character_interior_architecture_guard as guard

    violation = CharacterInteriorArchitectureViolation(
        tmp_path / "composition.py",
        4,
        "legacy_character_interior_import",
        "InboundCharacterAuthor",
    )
    monkeypatch.setattr(
        guard,
        "scan_character_interior_architecture",
        lambda _root: (violation,),
    )

    with pytest.raises(
        CharacterInteriorArchitectureError,
        match="CharacterInterior architecture guard failed",
    ):
        assert_character_interior_architecture(REPOSITORY_ROOT)


def _write_production(root: Path, relative: str, source: str) -> Path:
    path = root / "src" / "companion_daemon" / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_repository_scan_covers_nested_production_python_not_only_known_roots(
    tmp_path: Path,
) -> None:
    path = _write_production(
        tmp_path,
        "world_v2/nested/orphan_lane.py",
        "from ..proactive_action import ProactiveDraftAdapter\nadapter = ProactiveDraftAdapter()\n",
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert any(item.path == path for item in violations)
    assert "legacy_character_interior_construction" in {item.rule for item in violations}


def test_repository_scan_excludes_interior_implementation_and_historical_replay_codecs(
    tmp_path: Path,
) -> None:
    _write_production(
        tmp_path,
        "world_v2/character_interior/private_composition.py",
        "from ..proactive_action import ProactiveDraftAdapter\nadapter = ProactiveDraftAdapter()\n",
    )
    _write_production(
        tmp_path,
        "world_v2/upcasting.py",
        "from .proactive_action import ProactiveDraftAdapter\n"
        "historical_type = ProactiveDraftAdapter\n",
    )

    assert scan_character_interior_architecture(tmp_path) == ()


def test_repository_scan_rejects_retired_provider_wire_outside_historical_replay(
    tmp_path: Path,
) -> None:
    live_wire = _write_production(
        tmp_path,
        "world_v2/character_interior/inbound_wire.py",
        'protocol = "expression-units.1"\n',
    )
    replay_codec = _write_production(
        tmp_path,
        "world_v2/upcasting.py",
        'historical_protocol = "expression-units.1"\n',
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert tuple((item.path, item.rule, item.detail) for item in violations) == (
        (
            live_wire,
            "retired_live_provider_contract",
            "expression-units.1",
        ),
    )
    assert all(item.path != replay_codec for item in violations)


@pytest.mark.parametrize(
    "declaration",
    (
        "class CharacterInteriorApplicationPorts:\n    pass\n",
        "class CharacterInteriorRuntimeEffects:\n    pass\n",
        "def character_interior_application_ports(interior):\n    return interior\n",
        "def compose_character_interior_runtime_effects(interior):\n    return interior\n",
        "def runtime_faculty(interior, name):\n    return interior\n",
        "def optional_runtime_faculty(interior, name):\n    return interior\n",
    ),
)
def test_repository_scan_rejects_compatibility_facades_inside_the_interior_module(
    tmp_path: Path,
    declaration: str,
) -> None:
    facade = _write_production(
        tmp_path,
        "world_v2/character_interior/compatibility.py",
        declaration,
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert any(
        item.path == facade and item.rule == "character_interior_compatibility_facade"
        for item in violations
    )


def test_repository_scan_does_not_hide_a_legacy_implementation_file(
    tmp_path: Path,
) -> None:
    _write_production(
        tmp_path,
        "world_v2/single_call_inbound_cognition.py",
        "from .proactive_action import ProactiveDraftAdapter\nadapter = ProactiveDraftAdapter()\n",
    )
    host = _write_production(
        tmp_path,
        "world_v2/host.py",
        "from .single_call_inbound_cognition import SingleCallInboundCognition\n"
        "adapter = SingleCallInboundCognition()\n",
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert violations
    assert {item.path for item in violations} == {
        tmp_path / "src/companion_daemon/world_v2/single_call_inbound_cognition.py",
        host,
    }
    assert "legacy_character_interior_import" in {item.rule for item in violations}


def test_repository_scan_rejects_extracting_the_private_inbound_author(
    tmp_path: Path,
) -> None:
    host = _write_production(
        tmp_path,
        "world_v2/host.py",
        "from .character_interior.inbound_author import _InboundCharacterAuthor\n"
        "author = _InboundCharacterAuthor()\n",
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert any(
        item.path == host and item.rule == "inbound_private_implementation_import"
        for item in violations
    )
    assert any(
        item.path == host
        and item.rule == "inbound_character_author_construction_outside_composition"
        for item in violations
    )


def test_repository_scan_rejects_extracting_an_inbound_wire(
    tmp_path: Path,
) -> None:
    host = _write_production(
        tmp_path,
        "world_v2/host.py",
        "from .character_interior.inbound_wire import _ExpressionDraftWire\n"
        "wire = _ExpressionDraftWire()\n",
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert any(
        item.path == host and item.rule == "inbound_private_implementation_import"
        for item in violations
    )
    assert any(
        item.path == host and item.rule == "inbound_wire_construction_outside_author"
        for item in violations
    )


@pytest.mark.parametrize(
    "source",
    (
        "def _discover_recovery_model(*, flash_model):\n    return flash_model\n",
        "def compose(*, recovery_model=None):\n    return recovery_model\n",
        "def compose(*, discover_recovery_model=True):\n    return discover_recovery_model\n",
        "class _AppraisalDraftWire:\n"
        "    async def propose(self, request):\n"
        "        return request\n",
    ),
)
def test_repository_scan_rejects_backup_or_split_inbound_author_surfaces(
    tmp_path: Path,
    source: str,
) -> None:
    path = _write_production(
        tmp_path,
        "world_v2/character_interior/inbound_author.py",
        source,
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert any(
        item.path == path and item.rule == "retired_inbound_author_surface" for item in violations
    )


@pytest.mark.parametrize(
    "source",
    (
        "port = runtime_faculty(interior, 'inbound_expression')\n",
        "port = optional_runtime_faculty(interior, 'affect')\n",
        "port = interior._runtime_faculty('proactive_author')\n",
    ),
)
def test_guard_rejects_runtime_faculty_extraction_outside_interior(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "runtime.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == "runtime_faculty_extraction_outside_interior" for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        "def _build_inner_life_snapshot(context):\n    return {}\n",
        "def compile_inner_life_snapshot(context):\n    return {}\n",
    ),
)
def test_guard_rejects_a_second_inner_life_snapshot_producer(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "context.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == "second_inner_life_snapshot_producer" for item in violations)


def test_guard_rejects_calling_the_private_snapshot_compiler_outside_interior(
    tmp_path: Path,
) -> None:
    path = tmp_path / "consumer.py"
    path.write_text(
        "from .character_interior.snapshot_compiler import "
        "compile_inner_life_snapshot\n\n"
        "snapshot = compile_inner_life_snapshot(context)\n",
        encoding="utf-8",
    )

    violations = scan_character_interior_source(path)

    assert "snapshot_compiler_bypass" in {item.rule for item in violations}


@pytest.mark.parametrize(
    ("symbol", "arguments", "lane"),
    (
        ("MediaSelectionDraftAdapter", "model=model", "media_selection"),
        ("QQPerceptionDecisionModel", "model=model", "qq_perception"),
        ("ChatCompletionLiveAttentionModel", "model=model, model_id='x'", "external_attention"),
        ("ChatCompletionShadowAttentionModel", "model=model, model_id='x'", "external_attention"),
        (
            "RoleBoundLifeDevelopmentModelAdapter",
            "model=model, role='character_model'",
            "life_character",
        ),
        ("PrivateImpressionDraftAdapter", "model=model", "private_memory"),
        ("FactMemoryDraftAdapter", "model=model", "private_memory"),
        ("ExperienceMemoryDraftAdapter", "model=model", "private_memory"),
        ("MemoryWithdrawalReviewAdapter", "model=model", "private_memory"),
    ),
)
def test_guard_classifies_protagonist_role_bypass_constructors(
    tmp_path: Path,
    symbol: str,
    arguments: str,
    lane: str,
) -> None:
    path = tmp_path / "composition.py"
    path.write_text(f"adapter = {symbol}({arguments})\n", encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(
        item.rule == "protagonist_role_bypass" and item.detail.startswith(f"{lane}:")
        for item in violations
    )


@pytest.mark.parametrize(
    ("module", "symbol", "lane"),
    (
        ("media_selection_draft", "MediaSelectionDraftAdapter", "media_selection"),
        ("perception_decision_adapter", "QQPerceptionDecisionModel", "qq_perception"),
        (
            "external_world_perception.production_attention",
            "ChatCompletionLiveAttentionModel",
            "external_attention",
        ),
        ("fact_memory_draft", "FactMemoryDraftAdapter", "private_memory"),
    ),
)
def test_guard_rejects_importing_a_concrete_protagonist_role_model(
    tmp_path: Path,
    module: str,
    symbol: str,
    lane: str,
) -> None:
    path = tmp_path / "worker.py"
    path.write_text(f"from .{module} import {symbol}\n", encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(
        item.rule == "protagonist_role_bypass" and item.detail == f"{lane}: import {symbol}"
        for item in violations
    )


@pytest.mark.parametrize(
    ("symbol", "lane"),
    (
        ("AffectDraftDeliberationAdapter", "affect"),
        (
            "AuditedReplacementReconsiderationReviewer",
            "expression_reconsideration",
        ),
        ("ChatCompletionLiveAttentionModel", "external_attention"),
        ("ChatCompletionShadowAttentionModel", "external_attention"),
        ("ExperienceMemoryDraftAdapter", "private_memory"),
        (
            "ExpressionReconsiderationChatModelAdapter",
            "expression_reconsideration",
        ),
        ("_FastAppraisalDraftWire", "inbound_turn"),
        ("FactMemoryDraftAdapter", "private_memory"),
        ("LifeAuthorResult", "life_character"),
        ("LifeAuthorRuntime", "life_character"),
        ("MediaSelectionDraftAdapter", "media_selection"),
        ("MemoryWithdrawalReviewAdapter", "private_memory"),
        ("PrivateImpressionDraftAdapter", "private_memory"),
        ("PlanDisruptionAppraisalTriggerRuntime", "world_stimulus"),
        ("PlanDisruptionAppraisalTurn", "world_stimulus"),
        ("ProactiveDraftAdapter", "proactive_contact"),
        ("NpcWorldAppraisalTriggerRuntime", "world_stimulus"),
        ("SettledWorldAppraisalTurn", "world_stimulus"),
        ("SilenceAppraisalTriggerRuntime", "world_stimulus"),
        ("SilenceAppraisalTurn", "world_stimulus"),
        ("RelationshipDraftDeliberationAdapter", "relationship"),
        ("SingleCallAppraisalAdapter", "inbound_turn"),
        ("SingleCallExpressionAdapter", "inbound_turn"),
        ("SingleCallInboundCognition", "inbound_turn"),
    ),
)
def test_guard_rejects_redefining_a_retired_protagonist_author(
    tmp_path: Path,
    symbol: str,
    lane: str,
) -> None:
    path = tmp_path / "production_attention.py"
    path.write_text(f"class {symbol}:\n    pass\n", encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(
        item.rule == "retired_protagonist_author_definition" and item.detail == f"{lane}: {symbol}"
        for item in violations
    )


@pytest.mark.parametrize(
    ("relative", "symbol", "lane"),
    (
        (
            "world_v2/proactive_action.py",
            "ProactiveDraftAdapter",
            "proactive_contact",
        ),
        (
            "world_v2/private_impression_producer.py",
            "PrivateImpressionDraftAdapter",
            "private_memory",
        ),
    ),
)
def test_repository_scan_rejects_retired_authors_inside_private_implementation_allowlist(
    tmp_path: Path,
    relative: str,
    symbol: str,
    lane: str,
) -> None:
    path = _write_production(
        tmp_path,
        relative,
        f"class {symbol}:\n    pass\n",
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert any(
        item.path == path
        and item.rule == "retired_protagonist_author_definition"
        and item.detail == f"{lane}: {symbol}"
        for item in violations
    )


def test_repository_scan_rejects_split_inbound_faculty_composition_inside_module(
    tmp_path: Path,
) -> None:
    path = _write_production(
        tmp_path,
        "world_v2/character_interior/production.py",
        "faculty = InboundTurnFaculty(\n"
        "    expression=cognition._expression_materializer,\n"
        "    appraisal=cognition._appraisal_materializer,\n"
        "    recovery=cognition._expression_materializer,\n"
        ")\n",
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert any(
        item.path == path
        and item.rule == "split_inbound_cognition_composition"
        and item.detail == "appraisal, expression, recovery"
        for item in violations
    )


def test_guard_allows_the_world_author_role_but_rejects_a_dynamic_external_role(
    tmp_path: Path,
) -> None:
    world_author = tmp_path / "world_author.py"
    world_author.write_text(
        "adapter = RoleBoundLifeDevelopmentModelAdapter(model=model, role='world_author')\n",
        encoding="utf-8",
    )
    dynamic = tmp_path / "host.py"
    dynamic.write_text(
        "adapter = RoleBoundLifeDevelopmentModelAdapter(model=model, role=role)\n",
        encoding="utf-8",
    )

    assert not any(
        item.rule == "protagonist_role_bypass"
        for item in scan_character_interior_source(world_author)
    )
    assert any(
        item.rule == "protagonist_role_bypass" for item in scan_character_interior_source(dynamic)
    )


@pytest.mark.parametrize(
    ("parameter", "lane"),
    (
        ("media_selection_model", "media_selection"),
        ("perception_model", "qq_perception"),
        ("life_character_model", "life_character"),
        ("private_impression_model", "private_memory"),
        ("memory_model", "private_memory"),
    ),
)
def test_guard_classifies_protagonist_role_bypass_call_parameters(
    tmp_path: Path,
    parameter: str,
    lane: str,
) -> None:
    path = tmp_path / "host.py"
    path.write_text(f"application = build({parameter}=model)\n", encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(
        item.rule == "protagonist_role_bypass" and item.detail.startswith(f"{lane}:")
        for item in violations
    )


@pytest.mark.parametrize(
    "source",
    (
        "from .testing_application import build_sqlite_world_v2_test_application\n",
        "import companion_daemon.world_v2.testing_application as fixtures\n",
        "from .production_turn_application import build_sqlite_world_v2_test_application\n",
        "from tests.support.world_v2_application import build_sqlite_world_v2_test_application\n",
        "from world_v2_application import build_sqlite_world_v2_test_application\n",
    ),
)
def test_guard_rejects_production_import_or_reexport_of_testing_application(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "production.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == "production_testing_application_dependency" for item in violations)


def test_guard_rejects_a_testing_builder_definition_in_any_production_module(
    tmp_path: Path,
) -> None:
    path = tmp_path / "production.py"
    path.write_text(
        "def build_sqlite_world_v2_test_application(**kwargs):\n    return kwargs\n",
        encoding="utf-8",
    )

    violations = scan_character_interior_source(path)

    assert any(item.rule == "testing_helper_in_production_tree" for item in violations)


@pytest.mark.parametrize(
    "source",
    (
        "from companion_daemon.llm import FakeCompanionModel\n",
        "from companion_daemon.llm import FakeCompanionModel as FixtureModel\n"
        "model = FixtureModel()\n",
        "model = FakeCompanionModel()\n",
    ),
)
def test_repository_scan_rejects_fixture_character_model_in_online_composition_roots(
    tmp_path: Path,
    source: str,
) -> None:
    path = _write_production(
        tmp_path,
        "world_v2/qq_c2c_host.py",
        source,
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert any(
        item.path == path and item.rule == "production_fixture_character_model"
        for item in violations
    )


@pytest.mark.parametrize(
    "relative",
    (
        "cli.py",
        "world_v2/scenario_runner.py",
        "world_v2/qq_c2c_onebot_app.py",
    ),
)
def test_repository_scan_allows_explicit_fixture_model_in_offline_hosts(
    tmp_path: Path,
    relative: str,
) -> None:
    _write_production(
        tmp_path,
        relative,
        "from companion_daemon.llm import FakeCompanionModel\nfixture = FakeCompanionModel()\n",
    )

    assert scan_character_interior_architecture(tmp_path) == ()


@pytest.mark.parametrize(
    "source",
    (
        "from .character_interior.production import compose_adapter_fixture_character_interior\n",
        "interior = compose_adapter_fixture_character_interior("
        "inbound_expression=main, quick_recovery=quick, optional_faculties={})\n",
    ),
)
def test_guard_rejects_test_only_interior_fixture_from_production(
    tmp_path: Path,
    source: str,
) -> None:
    path = tmp_path / "host.py"
    path.write_text(source, encoding="utf-8")

    violations = scan_character_interior_source(path)

    assert any(item.rule == "production_test_fixture_dependency" for item in violations)


def test_repository_scan_flags_a_testing_helper_left_in_production_tree(
    tmp_path: Path,
) -> None:
    helper = _write_production(
        tmp_path,
        "world_v2/testing_application.py",
        "def build_fixture():\n    return None\n",
    )

    violations = scan_character_interior_architecture(tmp_path)

    assert any(
        item.path == helper and item.rule == "testing_helper_in_production_tree"
        for item in violations
    )


def test_architecture_report_groups_every_violation_by_actionable_category(
    tmp_path: Path,
) -> None:
    violations = (
        CharacterInteriorArchitectureViolation(
            tmp_path / "a.py", 3, "protagonist_role_bypass", "media_selection: Adapter"
        ),
        CharacterInteriorArchitectureViolation(
            tmp_path / "b.py", 7, "second_inner_life_snapshot_producer", "compile_snapshot"
        ),
    )

    classified = classify_character_interior_violations(violations)
    report = render_character_interior_architecture_report(
        violations,
        repository_root=tmp_path,
    )

    assert tuple(classified) == (
        "protagonist_role_bypass",
        "second_inner_life_snapshot_producer",
    )
    assert sum(map(len, classified.values())) == len(violations)
    assert "protagonist_role_bypass (1)" in report
    assert "second_inner_life_snapshot_producer (1)" in report


def test_production_composition_uses_only_character_interior() -> None:
    assert_character_interior_architecture(REPOSITORY_ROOT)
