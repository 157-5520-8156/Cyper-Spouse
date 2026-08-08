from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.character_interior.inbound_tool_contract import (
    InboundToolContracts,
)
from companion_daemon.world_v2.character_interior.inbound_wire import _provider_invocation_identity
from companion_daemon.world_v2.character_interior.inbound_appraisal_wire import (
    AppraisalDraftWire,
    RelationshipSignalWire,
    RelationshipSuggestedDeltasWire,
)
from companion_daemon.world_v2.expression_draft import (
    ExpressionDraft,
    QQ_NAPCAT_EXPRESSION_CAPABILITIES,
)


def test_initial_contract_keeps_recall_as_a_role_owned_legal_outcome() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
    )

    raw = json.dumps(
        {
            "result_kind": "recall",
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "我想先确认那段记忆。",
                "attended_source_refs": [],
            },
            "recall_request": {"query_text": "上次说的那家店"},
        },
        ensure_ascii=False,
    )

    assert json.loads(contract.unwrap(raw)) == {
        "private_turn_state": {
            "contract": "private-turn-state.1",
            "inner_state_summary": "我想先确认那段记忆。",
            "attended_source_refs": [],
        },
        "recall_request": {"query_text": "上次说的那家店"},
    }
    assert contract.provider_tools[0]["function"]["name"] == "character_inbound_initial_v1"
    assert contract.identity.schema_sha256.startswith("sha256:")


def test_after_recall_contract_cannot_reopen_recall() -> None:
    contract = InboundToolContracts().contract_for(
        phase="after_recall",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=False,
    )

    with pytest.raises(ValueError, match="recall"):
        contract.unwrap(json.dumps({"result_kind": "recall", "recall_request": {}}))


@pytest.mark.parametrize(
    "mixed",
    [
        {
            "result_kind": "decision",
            "appraisal_draft": {},
            "expression_draft": {},
            "recall_request": {"query_text": "记忆"},
        },
        {
            "result_kind": "recall",
            "recall_request": {"query_text": "记忆"},
            "appraisal_draft": {},
            "expression_draft": {},
        },
        {"appraisal_draft": {}, "expression_draft": {}},
    ],
)
def test_unwrap_rejects_ambiguous_or_missing_forced_transport_kind(
    mixed: dict[str, object],
) -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial", capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES, recall_allowed=True
    )

    with pytest.raises(ValueError, match="transport"):
        contract.unwrap(json.dumps(mixed))


def _appraisal() -> dict[str, object]:
    return {
        "appraise": True,
        "affect": "open",
        "brief_rationale": "这句话让我认真在意。",
        "behavior_tendency": "按自己的感受回应",
        "stance": "坦诚",
        "display_strategy": "自然表达",
        "confidence": 7000,
        "meanings": [{"meaning": "被认真对待", "confidence": 0.8}],
        "attribution": "user",
        "severity": 4200,
        "components": [{"dimension": "warmth", "target_intensity_bp": 4200}],
        "relationship_signal": {
            "signal_code": "closer_after_open_talk",
            "confidence_bp": 6000,
            "persistence": "session",
            "rationale_code": "felt_heard",
            "suggested_deltas": {
                "trust_bp": 20,
                "closeness_bp": 40,
                "respect_bp": 10,
                "reliability_bp": 0,
                "mutuality_bp": 30,
                "repair_confidence_bp": 0,
            },
        },
    }


def _private_state() -> dict[str, object]:
    return {
        "contract": "private-turn-state.1",
        "inner_state_summary": "我想把这份暖意表达出来。",
        "attended_source_refs": [],
    }


def _object_schema(value: object) -> dict[str, object]:
    if isinstance(value, dict) and isinstance(value.get("properties"), dict):
        return value
    if isinstance(value, dict) and isinstance(value.get("anyOf"), list):
        for variant in value["anyOf"]:
            if isinstance(variant, dict) and isinstance(variant.get("properties"), dict):
                return variant
    raise AssertionError("expected object schema")


def test_schema_parity_and_deletion_guard_cover_authoritative_wires() -> None:
    capabilities = QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required", "recorded_cadence_mode": "on"}
    )
    contract = InboundToolContracts().contract_for(
        phase="initial",
        capabilities=capabilities,
        recall_allowed=True,
        require_turn_posture=True,
    )
    root = contract.provider_tools[0]["function"]["parameters"]
    root_properties = _object_schema(root)["properties"]
    expression = _object_schema(root_properties["expression_draft"])
    appraisal = _object_schema(root_properties["appraisal_draft"])

    assert set(ExpressionDraft.model_fields) <= set(expression["properties"])
    assert set(AppraisalDraftWire.model_fields) <= set(appraisal["properties"])
    assert "affect" in appraisal["required"]
    lifecycle_required = {
        branch["properties"]["affect"]["enum"][0]: set(branch["required"])
        for branch in appraisal["anyOf"]
        if branch["properties"]["appraise"]["enum"] == [True]
    }
    assert {"components"} <= lifecycle_required["open"]
    assert {"episode_id", "components"} <= lifecycle_required["update"]
    assert {"episode_id", "resolution_summary"} <= lifecycle_required["resolve"]
    assert {"episode_id", "components"} <= lifecycle_required["supersede"]
    relationship = _object_schema(appraisal["properties"]["relationship_signal"])
    deltas = _object_schema(relationship["properties"]["suggested_deltas"])
    assert set(RelationshipSignalWire.model_fields) == set(relationship["properties"])
    assert set(RelationshipSuggestedDeltasWire.model_fields) == set(deltas["properties"])
    beats = _object_schema(expression["properties"]["beats"]["items"])
    assert beats["properties"]["modality"]["enum"] == list(capabilities.modalities)
    assert beats["properties"]["reaction_id"]["anyOf"][0]["enum"] == [
        item.option_id for item in capabilities.reaction_options
    ]
    assert beats["properties"]["sticker_id"]["anyOf"][0]["enum"] == [
        item.option_id for item in capabilities.sticker_options
    ]
    assert expression["properties"]["beats"]["maxItems"] == capabilities.max_beats
    assert {"timing_choice", "beats", "confidence", "stance", "brief_rationale", "cadence", "turn_posture", "private_turn_state"} <= set(expression["required"])

    # Deleting a canonical provider field makes the parity assertion fail;
    # this protects against a future hand-written/shrunk tool projection.
    deleted = dict(expression["properties"])
    deleted.pop("response_expectation")
    assert not set(ExpressionDraft.model_fields) <= set(deleted)


def test_contract_identity_changes_with_executable_capability_or_route() -> None:
    contracts = InboundToolContracts()
    base = contracts.contract_for(
        phase="initial", capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES, recall_allowed=True
    )
    text_only = contracts.contract_for(
        phase="initial",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"modalities": ("text",), "reaction_options": (), "sticker_options": ()}
        ),
        recall_allowed=True,
    )
    profile_revision = contracts.contract_for(
        phase="initial",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"profile_id": "expression:qq-napcat.2"}
        ),
        recall_allowed=True,
    )
    final = contracts.contract_for(
        phase="after_recall", capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES, recall_allowed=False
    )

    assert base.identity.schema_sha256 != text_only.identity.schema_sha256
    assert base.identity.schema_sha256 == profile_revision.identity.schema_sha256
    assert base.identity.capabilities_sha256 != profile_revision.identity.capabilities_sha256
    assert base.identity.contract_sha256 != profile_revision.identity.contract_sha256
    assert base.identity.schema_sha256 != final.identity.schema_sha256
    assert base.identity.tool_name != final.identity.tool_name


def test_local_contract_identity_is_bound_into_provider_request_hash() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial", capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES, recall_allowed=True
    )
    common = {
        "parent_call_id": "call:contract-identity",
        "purpose": "paired_cognition_initial",
        "messages": [{"role": "user", "content": "x"}],
        "temperature": 0.8,
        "tools": list(contract.provider_tools),
        "tool_choice": contract.provider_tool_choice,
    }

    unbound = _provider_invocation_identity(**common)
    bound = _provider_invocation_identity(
        **common,
        tool_contract_identity=contract.identity.request_identity_material(),
    )

    assert bound.request_hash != unbound.request_hash


def test_later_branch_carries_the_stricter_capability_beat_limit() -> None:
    capabilities = QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
        update={"max_beats": 6, "max_later_beats": 2}
    )
    contract = InboundToolContracts().contract_for(
        phase="initial", capabilities=capabilities, recall_allowed=False
    )
    expression = _object_schema(
        _object_schema(contract.provider_tools[0]["function"]["parameters"])["properties"][
            "expression_draft"
        ]
    )

    later_branches = [
        branch
        for branch in expression.get("anyOf", [])
        if branch.get("properties", {}).get("timing_choice", {}).get("enum") == ["later"]
    ]
    assert later_branches[0]["properties"]["beats"]["maxItems"] == 2
    assert later_branches[0]["properties"]["beats"]["items"]["properties"]["modality"][
        "enum"
    ] == ["text"]


def test_text_only_contract_never_advertises_later_reaction_or_sticker() -> None:
    capabilities = QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
        update={"modalities": ("text",), "reaction_options": (), "sticker_options": ()}
    )
    contract = InboundToolContracts().contract_for(
        phase="initial", capabilities=capabilities, recall_allowed=False
    )
    expression = _object_schema(
        _object_schema(contract.provider_tools[0]["function"]["parameters"])["properties"][
            "expression_draft"
        ]
    )
    later = next(
        branch
        for branch in expression["anyOf"]
        if branch.get("properties", {}).get("timing_choice", {}).get("enum") == ["later"]
    )

    assert later["properties"]["beats"]["items"]["properties"]["modality"]["enum"] == [
        "text"
    ]


def test_canonical_appraisal_wire_rejects_an_attribution_legacy_materializer_rejects() -> None:
    invalid = _appraisal() | {"attribution": "uncertain"}

    with pytest.raises(ValueError, match="attribution"):
        AppraisalDraftWire.model_validate_json(json.dumps(invalid), strict=True)


def test_capability_contract_keeps_all_live_expression_coordinates() -> None:
    capabilities = QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required", "recorded_cadence_mode": "on"}
    )
    contract = InboundToolContracts().contract_for(
        phase="initial",
        capabilities=capabilities,
        recall_allowed=True,
        require_turn_posture=True,
    )
    expression = {
        "private_turn_state": _private_state(),
        "timing_choice": "now",
        "turn_posture": "continue",
        "cadence": "conversational",
        "beats": [
            {"modality": "typing"},
            {"modality": "text", "text": "我也有点开心。"},
            {"modality": "reaction", "reaction_id": "heart"},
            {"modality": "sticker", "sticker_id": "qq-face:14"},
        ],
        "stance": "warm",
        "brief_rationale": "把真实反应告诉你。",
        "confidence": 7200,
        "variation_profile": {
            "deviation_kind": "warm",
            "deviation_intensity": 2000,
            "change_phase": "turn",
            "sampling_mode": "recorded",
            "recovery_posture": "steady",
        },
        "response_expectation": {
            "hoped_response": "也说说你的感受",
            "pressure_bp": 1000,
            "importance_bp": 3000,
            "wait_seconds": 60,
            "expires_after_seconds": 600,
        },
        "response_expectation_assessment": {"status": "uncertain", "reason": "刚刚提出"},
        "world_claims": [
            {
                "claim_text": "我想慢慢讲。",
                "scope": "subjective_or_hypothetical",
                "source_refs": [],
            }
        ],
    }

    unwrapped = contract.unwrap(
        json.dumps(
            {"result_kind": "decision", "appraisal_draft": _appraisal(), "expression_draft": expression},
            ensure_ascii=False,
        )
    )

    assert json.loads(unwrapped)["expression_draft"] == expression
    provider_schema = contract.provider_tools[0]["function"]["parameters"]
    assert provider_schema["anyOf"][0]["required"] == [
        "result_kind",
        "appraisal_draft",
        "expression_draft",
    ]


@pytest.mark.parametrize(
    ("timing_choice", "beats", "due"),
    [
        ("later", [{"modality": "text", "text": "晚点再认真回你。"}], {"delay_seconds": 20, "expires_after_seconds": 120}),
        ("silent", [], {}),
    ],
)
def test_contract_preserves_later_and_silent_role_choices(
    timing_choice: str, beats: list[dict[str, str]], due: dict[str, int]
) -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=False,
    )
    expression: dict[str, object] = {
        "timing_choice": timing_choice,
        "beats": beats,
        "stance": "own_choice",
        "brief_rationale": "这是我现在想要的节奏。",
        "confidence": 6000,
        **due,
    }

    assert json.loads(
        contract.unwrap(
            json.dumps(
                {"result_kind": "decision", "appraisal_draft": _appraisal(), "expression_draft": expression},
                ensure_ascii=False,
            )
        )
    )["expression_draft"]["timing_choice"] == timing_choice
