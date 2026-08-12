from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from jsonschema import Draft202012Validator

from companion_daemon.llm import DeepSeekChatModel
from companion_daemon.world_v2.character_interior.inbound_tool_contract import (
    InboundToolContracts,
)
from companion_daemon.world_v2.character_interior.inbound_wire import (
    _incremental_first_expression,
    _provider_invocation_identity,
    _stream_first_expression,
    _stream_tail_expression,
    complete_bounded_validation_reselection,
)
from companion_daemon.world_v2.character_interior.inbound_appraisal_wire import (
    AppraisalDraftWire,
    RelationshipSignalWire,
    RelationshipSuggestedDeltasWire,
)
from companion_daemon.world_v2.expression_draft import (
    ExpressionDraft,
    QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    qq_expression_capabilities,
)


def _reply_only_stream_arguments() -> dict[str, object]:
    return {
        "result_kind": "reply_only",
        "protocol": "character-interior-events.1",
        "appraisal_draft": {
            "appraise": False,
            "affect": "no_change",
            "brief_rationale": "这句不需要形成新的持久评价。",
            "behavior_tendency": "自由接话",
            "stance": "自然回应",
            "display_strategy": "直接说",
            "confidence": 7000,
            "meanings": None,
            "attribution": None,
            "severity": None,
            "components": None,
            "episode_id": None,
            "resolution_summary": None,
        },
        "events": [
            {
                "type": "head",
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我想先自然接住这句话。",
                    "attended_source_refs": ["s0"],
                },
                "timing_choice": "now",
                "turn_posture": "continue",
                "cadence": "conversational",
                "beat": {
                    "modality": "text",
                    "text": "嗯，我在听。",
                },
                "stance": "自然接话",
                "brief_rationale": "这一句已经完整表达了我此刻想说的。",
                "confidence": 7600,
                "response_expectation": None,
                "response_expectation_assessment": None,
                "world_claims": [],
                "media_request": "none",
                "media_source_refs": [],
            },
            {"type": "end"},
        ],
    }


def _reply_only_appraisal_effect_arguments() -> dict[str, object]:
    candidate = _reply_only_stream_arguments()
    candidate["appraisal_draft"] = {
        "appraise": True,
        "affect": "open",
        "brief_rationale": "我把这句话理解成对方希望被认真听见。",
        "behavior_tendency": "认真倾听",
        "stance": "在场且关切",
        "display_strategy": "自然接住",
        "confidence": 8200,
        "meanings": [
            {
                "meaning": "对方希望我认真听见此刻的感受",
                "confidence": 8400,
            }
        ],
        "attribution": "user",
        "severity": 3200,
        "components": [
            {
                "dimension": "warmth",
                "target_intensity_bp": 3600,
            }
        ],
        "episode_id": None,
        "resolution_summary": None,
    }
    return candidate


def _compact_gate_carrier(result: dict[str, object]) -> dict[str, object]:
    kind = result.get("result_kind")
    if kind == "full_turn":
        payload_json = result.get("full_turn_json")
        assert isinstance(payload_json, str)
    else:
        payload_json = json.dumps(
            {key: value for key, value in result.items() if key != "result_kind"},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return {"result_kind": kind, "payload_json": payload_json}


def test_compact_gate_strict_contract_is_small_and_keeps_role_owned_branches() -> None:
    contract = InboundToolContracts().compact_gate_for(
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
        schema_dialect="deepseek-strict",
    )
    function = contract.provider_tools[0]["function"]
    parameters = function["parameters"]

    Draft202012Validator.check_schema(parameters)
    assert function["name"] == "character_inbound_compact_gate_v2"
    assert len(
        json.dumps(parameters, ensure_ascii=False, separators=(",", ":")).encode()
    ) <= 12 * 1024
    assert parameters == {
        "type": "object",
        "properties": {
            "result_kind": {
                "type": "string",
                "enum": ["reply_only", "full_turn", "recall"],
            },
            "payload_json": {"type": "string"},
        },
        "required": ["result_kind", "payload_json"],
        "additionalProperties": False,
    }
    assert parameters["properties"]["result_kind"]["enum"] == [
        "reply_only",
        "full_turn",
        "recall",
    ]

    description = function["description"]
    assert isinstance(description, str)
    assert "complete external effect is one immediate text message" in description
    assert "minimum sufficient branch" in description
    assert "losslessly represents the external effect you choose" in description
    assert "multiple sentences or paragraphs" in description
    assert "not required to be terse or emotionally flat" in description
    assert "only when the external effect you choose actually requires" in description
    assert "does not classify by topic, length, complexity, or keywords" in description
    assert "does not choose the branch" in description
    assert "Choose reply_only only when" not in description
    assert "canonical appraisal and affect lifecycle" in description
    assert "brief_rationale, behavior_tendency, stance, display_strategy, and confidence" in (
        description
    )
    assert "appraise and affect are your choices" in description
    assert "no appraisal, affect" not in description
    assert "complete chosen branch object as a JSON string in payload_json" in description
    assert "full_turn_json" not in description

    multi_paragraph = _reply_only_stream_arguments()
    events = multi_paragraph["events"]
    assert isinstance(events, list)
    head = events[0]
    assert isinstance(head, dict)
    head["beat"] = {
        "modality": "text",
        "text": "第一句完整回应。\n\n第二段仍属于同一条消息。",
    }
    compact_carrier = {
        "result_kind": "reply_only",
        "payload_json": json.dumps(
            {key: value for key, value in multi_paragraph.items() if key != "result_kind"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    assert list(Draft202012Validator(parameters).iter_errors(compact_carrier)) == []
    assert contract.decode(json.dumps(compact_carrier, ensure_ascii=False))[
        "events"
    ] == events


@pytest.mark.parametrize(
    ("result", "expected_keys"),
    (
        (
            {
                "result_kind": "reply_only",
                **_reply_only_stream_arguments(),
            },
            {"result_kind", "protocol", "appraisal_draft", "events"},
        ),
        (
            _reply_only_appraisal_effect_arguments(),
            {"result_kind", "protocol", "appraisal_draft", "events"},
        ),
        (
            {
                "result_kind": "full_turn",
                "full_turn_json": json.dumps(
                    {
                        key: value
                        for key, value in _reply_only_stream_arguments().items()
                        if key != "result_kind"
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
            {"result_kind", "full_turn_json"},
        ),
        (
            {
                "result_kind": "recall",
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我想先确认此前相关记忆。",
                    "attended_source_refs": [],
                },
                "recall_request": {
                    "query_text": "此前相关记忆",
                    "lexical_text": None,
                    "occurred_from": None,
                    "occurred_to": None,
                    "link_refs": [],
                    "memory_kinds": ["episodic"],
                    "include_historical": False,
                    "limit": 4,
                },
            },
            {"result_kind", "private_turn_state", "recall_request"},
        ),
    ),
)
def test_compact_gate_decoder_removes_only_strict_null_siblings(
    result: dict[str, object],
    expected_keys: set[str],
) -> None:
    contract = InboundToolContracts().compact_gate_for(
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
        schema_dialect="deepseek-strict",
    )
    strict_result = _compact_gate_carrier(result)

    Draft202012Validator(
        contract.provider_tools[0]["function"]["parameters"]
    ).validate(strict_result)
    decoded = contract.decode(json.dumps(strict_result, ensure_ascii=False))

    assert set(decoded) == expected_keys


def test_compact_gate_decoder_rejects_non_null_cross_branch_semantics() -> None:
    contract = InboundToolContracts().compact_gate_for(
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
        schema_dialect="deepseek-strict",
    )
    result = {
        "result_kind": "full_turn",
        "payload_json": json.dumps(
            {
                "protocol": "character-interior-events.1",
                "appraisal_draft": {"appraise": False},
                "events": [],
                "recall_request": {},
            },
            separators=(",", ":"),
        ),
    }

    with pytest.raises(ValueError, match="exact event envelope"):
        contract.decode(json.dumps(result, ensure_ascii=False))


def test_compact_gate_carrier_rejects_duplicate_or_inner_transport_authority() -> None:
    contract = InboundToolContracts().compact_gate_for(
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
        schema_dialect="deepseek-strict",
    )
    duplicate_inner = {
        "result_kind": "reply_only",
        "payload_json": '{"protocol":"character-interior-events.1",'
        '"protocol":"character-interior-events.1","appraisal_draft":{},"events":[]}',
    }
    with pytest.raises(ValueError, match="duplicate field"):
        contract.decode(json.dumps(duplicate_inner, separators=(",", ":")))

    inner_authority = _reply_only_stream_arguments()
    inner_authority["result_kind"] = "full_turn"
    with pytest.raises(ValueError, match="cannot own transport authority"):
        contract.decode(
            json.dumps(
                {
                    "result_kind": "reply_only",
                    "payload_json": json.dumps(inner_authority, ensure_ascii=False),
                },
                ensure_ascii=False,
            )
        )

    payload_json = _compact_gate_carrier(_reply_only_stream_arguments())["payload_json"]
    assert isinstance(payload_json, str)
    duplicate_outer = (
        '{"result_kind":"reply_only","result_kind":"full_turn","payload_json":'
        + json.dumps(payload_json, ensure_ascii=False)
        + "}"
    )
    with pytest.raises(ValueError, match="duplicate field"):
        contract.decode(duplicate_outer)
    with pytest.raises(ValueError, match="conflicting duplicated field"):
        _stream_first_expression(duplicate_outer)


def test_stream_contract_contains_one_constrained_role_owned_reply_branch() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
    )
    function = contract.provider_tools[0]["function"]
    parameters = function["parameters"]
    Draft202012Validator.check_schema(parameters)
    validator = Draft202012Validator(parameters)

    reply_only = _reply_only_stream_arguments()
    assert list(validator.iter_errors(reply_only)) == []
    assert json.loads(contract.unwrap(json.dumps(reply_only, ensure_ascii=False))) == {
        key: value for key, value in reply_only.items() if key != "result_kind"
    }
    reply_branch = next(
        branch
        for branch in parameters["anyOf"]
        if branch["properties"]["result_kind"]["enum"] == ["reply_only"]
    )
    appraisal = reply_branch["properties"]["appraisal_draft"]
    head = next(
        branch
        for branch in reply_branch["properties"]["events"]["items"]["anyOf"]
        if branch["properties"]["type"]["enum"] == ["head"]
    )
    assert appraisal["properties"]["appraise"]["type"] == "boolean"
    assert appraisal["properties"]["affect"]["enum"] == [
        "no_change",
        "open",
        "update",
        "resolve",
        "supersede",
    ]
    assert {
        "meanings",
        "components",
        "attribution",
        "severity",
        "episode_id",
        "resolution_summary",
    } <= set(appraisal["properties"])
    assert not {
        "relationship_signal",
        "relationship_commitment",
        "interaction_act",
    } & set(appraisal["properties"])
    assert head["properties"]["timing_choice"]["enum"] == ["now"]
    assert head["properties"]["beat"]["properties"]["modality"]["enum"] == [
        "text"
    ]
    assert head["properties"]["media_request"]["enum"] == ["none"]
    assert reply_branch["properties"]["events"]["maxItems"] == 2


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("timing_choice", "later"),
        ("beat", {"modality": "reaction", "reaction_id": "heart"}),
        ("media_request", "consider_available_candidate"),
    ),
)
def test_stream_reply_only_rejects_expression_capability_escalation(
    field: str,
    value: object,
) -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=False,
    )
    candidate = _reply_only_stream_arguments()
    events = list(candidate["events"])
    events[0] = {**events[0], field: value}
    candidate["events"] = events

    assert list(
        Draft202012Validator(
            contract.provider_tools[0]["function"]["parameters"]
        ).iter_errors(candidate)
    )


def test_stream_reply_only_accepts_canonical_appraisal_affect_but_rejects_cross_turn_effects() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=False,
    )
    validator = Draft202012Validator(
        contract.provider_tools[0]["function"]["parameters"]
    )
    appraisal_effect = _reply_only_appraisal_effect_arguments()
    assert list(validator.iter_errors(appraisal_effect)) == []
    assert "嗯，我在听。" in _stream_first_expression(
        json.dumps(appraisal_effect, ensure_ascii=False)
    )

    for patch in (
        {
            "relationship_commitment": {
                "target_stage": "friend",
                "commitment_code": "mutual_friendship",
                "persistence": "durable",
                "visible_text_span": "嗯，我在听。",
            }
        },
        {
            "interaction_act": {
                "operation": "declare",
                "status_code": "heard",
            }
        },
    ):
        candidate = _reply_only_stream_arguments()
        candidate["appraisal_draft"] = {
            **candidate["appraisal_draft"],
            **patch,
        }
        assert list(validator.iter_errors(candidate))


@pytest.mark.parametrize("missing", ("meanings", "components"))
def test_stream_reply_only_appraisal_lifecycle_missing_required_fields_fails_closed(
    missing: str,
) -> None:
    candidate = _reply_only_appraisal_effect_arguments()
    del candidate["appraisal_draft"][missing]

    with pytest.raises(ValueError, match="reply-only appraisal is invalid"):
        _stream_first_expression(json.dumps(candidate, ensure_ascii=False))


def test_stream_reply_only_requires_pending_expectation_assessment_when_pinned() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=False,
        response_expectation_assessment_required=True,
    )
    validator = Draft202012Validator(
        contract.provider_tools[0]["function"]["parameters"]
    )
    missing_assessment = _reply_only_stream_arguments()
    assessed = _reply_only_stream_arguments()
    events = list(assessed["events"])
    events[0] = {
        **events[0],
        "response_expectation_assessment": {
            "status": "still_pending",
            "reason": "我仍然希望等到对方回应。",
        },
    }
    assessed["events"] = events

    assert list(validator.iter_errors(missing_assessment))
    assert list(validator.iter_errors(assessed)) == []


def test_stream_reply_only_and_full_decision_share_one_strict_tool_request() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
        schema_dialect="deepseek-strict",
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    Draft202012Validator.check_schema(parameters)
    assert parameters["properties"]["result_kind"]["enum"] == [
        "decision",
        "reply_only",
        "recall",
    ]
    assert contract.provider_tools[0]["function"]["name"] == (
        "character_inbound_initial_stream_v1"
    )
    strict_reply = _reply_only_stream_arguments()
    for field in parameters["properties"]:
        strict_reply.setdefault(field, None)
    Draft202012Validator(parameters).validate(strict_reply)
    assert json.loads(
        contract.unwrap(json.dumps(strict_reply, ensure_ascii=False))
    ) == {
        key: value
        for key, value in _reply_only_stream_arguments().items()
        if key != "result_kind"
    }


def test_reply_only_stream_parser_closes_strict_schema_capability_gaps() -> None:
    good = _reply_only_stream_arguments()
    assert "嗯，我在听。" in _stream_first_expression(
        json.dumps(good, ensure_ascii=False)
    )

    strict_empty_padding = json.loads(json.dumps(good, ensure_ascii=False))
    strict_empty_padding.update(
        {
            "full_turn_json": "",
            "private_turn_state": {},
            "recall_request": None,
        }
    )
    assert "嗯，我在听。" in _stream_first_expression(
        json.dumps(strict_empty_padding, ensure_ascii=False)
    )

    compact_carrier = _compact_gate_carrier(good)
    assert "嗯，我在听。" in _stream_first_expression(
        json.dumps(compact_carrier, ensure_ascii=False)
    )

    appraisal_effect = _reply_only_appraisal_effect_arguments()
    assert "嗯，我在听。" in _stream_first_expression(
        json.dumps(appraisal_effect, ensure_ascii=False)
    )

    relationship_effect = json.loads(json.dumps(good, ensure_ascii=False))
    relationship_effect["appraisal_draft"]["relationship_signal"] = {
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
    }
    with pytest.raises(ValueError, match="reply-only appraisal"):
        _stream_first_expression(json.dumps(relationship_effect, ensure_ascii=False))

    extra_beat = json.loads(json.dumps(good, ensure_ascii=False))
    extra_beat["events"].insert(
        1,
        {
            "type": "beat",
            "beat": {"modality": "text", "text": "第二条不能越权出现。"},
            "world_claims": [],
        },
    )
    with pytest.raises(ValueError, match="exactly one head"):
        _stream_tail_expression(json.dumps(extra_beat, ensure_ascii=False))


def test_reply_only_incremental_release_requires_exact_head_and_end() -> None:
    good = _reply_only_stream_arguments()
    raw = json.dumps(good, ensure_ascii=False)
    end_frame = json.dumps({"type": "end"}, ensure_ascii=False)

    # A complete first head is not enough authority: the end frame proves that
    # this constrained branch contains no second visible beat.
    assert _incremental_first_expression(
        raw[: raw.index(end_frame)],
        forced_tool=True,
    ) is None

    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
        schema_dialect="deepseek-strict",
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    duplicate_head = json.loads(json.dumps(good, ensure_ascii=False))
    duplicate_head["events"] = [
        duplicate_head["events"][0],
        duplicate_head["events"][0],
    ]
    for field in parameters["properties"]:
        duplicate_head.setdefault(field, None)
    # DeepSeek's strict projection deliberately drops min/maxItems. The host
    # parser remains the final capability boundary for this malformed result.
    Draft202012Validator(parameters).validate(duplicate_head)
    with pytest.raises(ValueError, match="terminate after its single text head"):
        _incremental_first_expression(
            json.dumps(duplicate_head, ensure_ascii=False),
            forced_tool=True,
        )

    assert "嗯，我在听。" in _incremental_first_expression(
        raw,
        forced_tool=True,
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


def test_media_enabled_inbound_contract_requires_an_explicit_role_owned_choice() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        capabilities=qq_expression_capabilities(
            "napcat", media_request_available=True
        ),
        recall_allowed=False,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    decision = next(
        branch
        for branch in parameters["anyOf"]
        if branch["properties"]["result_kind"]["enum"] == ["decision"]
    )
    expression = decision["properties"]["expression_draft"]

    assert {"media_request", "media_source_refs"} <= set(expression["required"])
    assert expression["properties"]["media_request"]["enum"] == [
        "none",
        "consider_available_candidate",
    ]


def test_deepseek_media_contract_never_transports_source_refs_as_null() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        capabilities=qq_expression_capabilities(
            "napcat", media_request_available=True
        ),
        recall_allowed=False,
        schema_dialect="deepseek-strict",
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    decision = next(
        branch
        for branch in parameters["anyOf"]
        if branch["properties"]["result_kind"]["enum"] == ["decision"]
    )
    expression = decision["properties"]["expression_draft"]

    assert {"media_source_refs", "world_claims"} <= set(expression["required"])
    for timing_branch in expression["anyOf"]:
        for field_name in ("media_source_refs", "world_claims"):
            field = timing_branch["properties"][field_name]
            assert field["type"] == "array"
            assert "anyOf" not in field


def test_deepseek_stream_head_never_transports_authored_arrays_as_null() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=qq_expression_capabilities(
            "napcat", media_request_available=True
        ),
        recall_allowed=False,
        schema_dialect="deepseek-strict",
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    decision = next(
        branch
        for branch in parameters["anyOf"]
        if branch["properties"]["result_kind"]["enum"] == ["decision"]
    )
    event_variants = decision["properties"]["events"]["items"]["anyOf"]
    head = next(
        branch
        for branch in event_variants
        if branch["properties"]["type"]["enum"] == ["head"]
    )

    assert {"media_source_refs", "world_claims"} <= set(head["required"])
    for timing_branch in head["anyOf"]:
        for field_name in ("media_source_refs", "world_claims"):
            field = timing_branch["properties"][field_name]
            assert field["type"] == "array"
            assert "anyOf" not in field


def test_stream_contract_preserves_append_only_expression_event_transport() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    decision = next(
        branch
        for branch in parameters["anyOf"]
        if branch["properties"]["result_kind"]["enum"] == ["decision"]
    )

    assert contract.identity.transport == "stream"
    assert contract.identity.tool_name == "character_inbound_initial_stream_v1"
    assert decision["required"] == [
        "result_kind",
        "protocol",
        "appraisal_draft",
        "events",
    ]
    assert decision["properties"]["protocol"]["enum"] == [
        "character-interior-events.1"
    ]
    assert decision["properties"]["events"]["minItems"] == 2


def test_stream_function_parameters_have_an_object_root_for_deepseek() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
    )

    parameters = contract.provider_tools[0]["function"]["parameters"]

    assert parameters["type"] == "object"
    assert parameters["required"] == ["result_kind"]
    assert parameters["properties"]["result_kind"]["enum"] == [
        "decision",
        "reply_only",
        "recall",
    ]
    assert set(parameters["properties"]) >= {
        "result_kind",
        "protocol",
        "appraisal_draft",
        "events",
        "recall_request",
        "private_turn_state",
    }

    raw = json.dumps(
        {
            "result_kind": "decision",
            "protocol": "character-interior-events.1",
            "appraisal_draft": _appraisal(),
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "第一条先到。"},
                    "stance": "自然接话",
                    "brief_rationale": "我想分两句说。",
                    "confidence": 7000,
                    "world_claims": [],
                },
                {
                    "type": "beat",
                    "beat": {"modality": "text", "text": "第二条随后到。"},
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
    )

    assert json.loads(contract.unwrap(raw))["protocol"] == "character-interior-events.1"


def test_stream_provider_root_accepts_both_decision_and_recall_branches() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
    )
    schema = contract.provider_tools[0]["function"]["parameters"]
    validator = Draft202012Validator(schema)

    decision = {
        "result_kind": "decision",
        "protocol": "character-interior-events.1",
        "appraisal_draft": _appraisal(),
        "events": [{"type": "end"}, {"type": "end"}],
    }
    recall = {
        "result_kind": "recall",
        "recall_request": {"query_text": "上次说的那家店"},
    }

    assert list(validator.iter_errors(decision)) == []
    assert list(validator.iter_errors(recall)) == []


def test_deepseek_strict_dialect_projects_optional_fields_to_required_nulls() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
        schema_dialect="deepseek-strict",
    )
    function = contract.provider_tools[0]["function"]
    assert function["strict"] is True
    assert contract.identity.schema_dialect == "deepseek-strict"
    parameters = function["parameters"]
    Draft202012Validator.check_schema(parameters)

    def assert_strict_objects(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                assert_strict_objects(item)
            return
        if not isinstance(value, dict):
            return
        properties = value.get("properties")
        if isinstance(properties, dict):
            assert value.get("type") == "object"
            assert value.get("additionalProperties") is False
            assert set(value.get("required", ())) == set(properties)
        for item in value.values():
            assert_strict_objects(item)

    assert_strict_objects(parameters)
    assert "maxLength" not in json.dumps(parameters)
    assert "minLength" not in json.dumps(parameters)
    assert '"date-time"' not in json.dumps(parameters)


def test_deepseek_strict_unwrap_removes_only_outer_null_branch_siblings() -> None:
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="atomic",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
        schema_dialect="deepseek-strict",
    )
    value = {
        "result_kind": "decision",
        "appraisal_draft": _appraisal(),
        "expression_draft": {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "现在回你。"}],
            "stance": "自然",
            "brief_rationale": "保持当前对话。",
            "confidence": 7000,
            "world_claims": [],
        },
    }
    parameters = contract.provider_tools[0]["function"]["parameters"]
    for key in parameters["properties"]:
        value.setdefault(key, None)
    unwrapped = json.loads(contract.unwrap(json.dumps(value, ensure_ascii=False)))
    assert unwrapped == {
        "appraisal_draft": _appraisal(),
        "expression_draft": value["expression_draft"],
    }


def test_final_contract_is_a_decision_only_atomic_contract() -> None:
    contract = InboundToolContracts().contract_for(
        phase="final",
        transport="atomic",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=True,
    )

    assert contract.recall_allowed is False
    assert contract.identity.phase == "final"
    assert contract.identity.tool_name == "character_inbound_final_atomic_v1"
    assert len(contract.provider_tools[0]["function"]["parameters"]["anyOf"]) == 1


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


@pytest.mark.asyncio
async def test_nonmetered_correction_keeps_local_contract_identity_off_provider_wire() -> None:
    contract = InboundToolContracts().contract_for(
        phase="final",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=False,
    )
    expression = {
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "我重新选好了。"}],
        "stance": "自然回应",
        "brief_rationale": "结构修正不改变我的选择权。",
        "confidence": 7000,
        "world_claims": [],
    }

    class StrictProvider:
        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float,
            tools: list[dict[str, object]],
            tool_choice: object,
        ) -> str:
            del messages, temperature, tools, tool_choice
            return json.dumps(
                {
                    "result_kind": "decision",
                    "appraisal_draft": _appraisal(),
                    "expression_draft": expression,
                },
                ensure_ascii=False,
            )

    result = await asyncio.wait_for(
        complete_bounded_validation_reselection(
            model=StrictProvider(),  # type: ignore[arg-type]
            messages=[{"role": "user", "content": "choose"}],
            raw="{}",
            instruction="choose again",
            temperature=0.8,
            timeout_seconds=1.0,
            parent_call_id="call:local-contract-only",
            tools=list(contract.provider_tools),
            tool_choice=contract.provider_tool_choice,
            tool_contract_identity=contract.identity.request_identity_material(),
            unwrap_tool_result=contract.unwrap,
        ),
        timeout=1.5,
    )

    assert json.loads(result.raw)["expression_draft"] == expression


@pytest.mark.asyncio
async def test_metered_correction_emits_the_exact_durable_request_identity() -> None:
    contract = InboundToolContracts().contract_for(
        phase="final",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recall_allowed=False,
    )
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": contract.identity.tool_name,
                                        "arguments": json.dumps(
                                            {
                                                "result_kind": "decision",
                                                "appraisal_draft": _appraisal(),
                                                "expression_draft": {
                                                    "timing_choice": "now",
                                                    "beats": [
                                                        {
                                                            "modality": "text",
                                                            "text": "我重新选好了。",
                                                        }
                                                    ],
                                                    "stance": "自然回应",
                                                    "brief_rationale": "结构修正不改变我的选择权。",
                                                    "confidence": 7000,
                                                    "world_claims": [],
                                                },
                                            },
                                            ensure_ascii=False,
                                        ),
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    model = DeepSeekChatModel(
        "key",
        "http://127.0.0.1:32124",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    object.__setattr__(model, "_test_only_capture_exact_request_identity", True)
    try:
        result = await complete_bounded_validation_reselection(
            model=model,
            messages=[{"role": "user", "content": "choose"}],
            raw="{}",
            instruction="choose again",
            temperature=0.8,
            timeout_seconds=1.0,
            parent_call_id="call:metered-correction-identity",
            tools=list(contract.provider_tools),
            tool_choice=contract.provider_tool_choice,
            tool_contract_identity=contract.identity.request_identity_material(),
            unwrap_tool_result=contract.unwrap,
        )
    finally:
        await model.aclose()

    assert captured_headers["x-girl-agent-request-identity"] == result.winning_request_hash


@pytest.mark.parametrize(
    "select_role_changes",
    [False, True],
    ids=["required-null-is-semantically-omitted", "both-selected-in-one-tool-call"],
)
@pytest.mark.asyncio
async def test_deepseek_strict_forced_tool_transports_generic_role_changes(
    select_role_changes: bool,
) -> None:
    contract = InboundToolContracts().contract_for(
        phase="final",
        capabilities=qq_expression_capabilities("napcat", media_request_available=True),
        recall_allowed=False,
        schema_dialect="deepseek-strict",
    )
    commitment = {
        "target_stage": "friend",
        "commitment_code": "mutual_friendship",
        "persistence": "durable",
        "visible_text_span": "那我们就是朋友了。",
    }
    interaction_act = {
        "operation": "declare",
        "status_code": "等我下次带上",
        "source_scope": "delivered_expression",
        "source_text_span": "那本书还在我这里，下次带给你。",
        "interaction_act_ref": None,
        "act_kind": "物品后续携带",
        "subject_role": "self",
        "counterparty_roles": ["current_counterpart"],
        "object_ref": None,
        "object_label": "那本书",
    }
    appraisal = _appraisal() | {
        "components": [
            {
                "component_id": None,
                "dimension": "warmth",
                "target_intensity_bp": 4200,
            }
        ],
        "episode_id": None,
        "resolution_summary": None,
        "relationship_commitment": commitment if select_role_changes else None,
        "interaction_act": interaction_act if select_role_changes else None,
    }
    arguments = {
        "result_kind": "decision",
        "appraisal_draft": appraisal,
        "expression_draft": {
            "private_turn_state": None,
            "timing_choice": "now",
            "turn_posture": None,
            "cadence": "conversational",
            "beats": [
                {
                    "modality": "text",
                    "text": "那我们就是朋友了。那本书还在我这里，下次带给你。",
                    "reaction_id": None,
                    "sticker_id": None,
                }
            ],
            "delay_seconds": None,
            "expires_after_seconds": None,
            "stance": "坦诚",
            "brief_rationale": "我想清楚地说出来。",
            "impulse_summary": None,
            "confidence": 7300,
            "variation_profile": None,
            "response_expectation": None,
            "response_expectation_assessment": None,
            "world_claims": [],
            "media_request": "none",
            "media_source_refs": [],
        },
    }
    arguments_json = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    captured: dict[str, object] = {}
    captured_headers: dict[str, str] = {}
    captured_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured.update(payload)
        captured_headers.update(request.headers)
        captured_paths.append(request.url.path)
        assert payload["tools"] == list(contract.provider_tools)
        assert payload["tool_choice"] == contract.provider_tool_choice
        function = payload["tools"][0]["function"]
        Draft202012Validator(function["parameters"]).validate(arguments)
        appraisal_schema = function["parameters"]["properties"]["appraisal_draft"]
        assert {"relationship_commitment", "interaction_act"} <= set(appraisal_schema["required"])
        expression_schema = function["parameters"]["properties"]["expression_draft"]
        for field_name in ("world_claims", "media_source_refs"):
            assert expression_schema["properties"][field_name]["type"] == "array"
            assert "anyOf" not in expression_schema["properties"][field_name]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": contract.identity.tool_name,
                                        "arguments": arguments_json,
                                    },
                                }
                            ]
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    model = DeepSeekChatModel(
        "key",
        "http://127.0.0.1:32124",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    object.__setattr__(model, "_test_only_capture_exact_request_identity", True)
    parent_call_id = f"call:strict-role-changes:{select_role_changes}"
    messages = [{"role": "user", "content": "choose"}]
    try:
        result = await complete_bounded_validation_reselection(
            model=model,
            messages=messages,
            raw="{}",
            instruction="choose again",
            temperature=0.8,
            timeout_seconds=1.0,
            parent_call_id=parent_call_id,
            tools=list(contract.provider_tools),
            tool_choice=contract.provider_tool_choice,
            tool_contract_identity=contract.identity.request_identity_material(),
            unwrap_tool_result=contract.unwrap,
        )
    finally:
        await model.aclose()

    expected_identity = _provider_invocation_identity(
        parent_call_id=parent_call_id,
        purpose="validation_reselection",
        messages=[
            *messages,
            {"role": "assistant", "content": "{}"},
            {"role": "user", "content": "choose again"},
        ],
        temperature=0.8,
        tools=list(contract.provider_tools),
        tool_choice=contract.provider_tool_choice,
        tool_contract_identity=contract.identity.request_identity_material(),
    )
    assert captured_paths == ["/beta/chat/completions"]
    assert "response_format" not in captured
    assert (
        captured_headers["x-girl-agent-request-identity"]
        == result.winning_request_hash
        == expected_identity.request_hash
    )
    selected_appraisal = json.loads(result.raw)["appraisal_draft"]
    appraisal_wire = AppraisalDraftWire.model_validate_json(
        json.dumps(selected_appraisal, ensure_ascii=False), strict=True
    )
    if not select_role_changes:
        semantic_appraisal = appraisal_wire.model_dump(mode="json", exclude_none=True)
        assert "relationship_commitment" not in semantic_appraisal
        assert "interaction_act" not in semantic_appraisal
    else:
        assert appraisal_wire.relationship_commitment is not None
        assert appraisal_wire.relationship_commitment.model_dump(mode="json") == commitment
        assert appraisal_wire.interaction_act is not None
        assert appraisal_wire.interaction_act.operation == "declare"
        assert appraisal_wire.interaction_act.status_code == "等我下次带上"


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


def test_stream_later_head_preserves_the_full_deferred_beat_budget() -> None:
    capabilities = QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
        update={"max_beats": 6, "max_later_beats": 2}
    )
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="stream",
        capabilities=capabilities,
        recall_allowed=False,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    decision = next(
        branch
        for branch in parameters["anyOf"]
        if branch["properties"]["result_kind"]["enum"] == ["decision"]
    )
    events = decision["properties"]["events"]
    assert isinstance(events, dict)
    items = events["items"]
    assert isinstance(items, dict)
    event_variants = items["anyOf"]
    head = next(
        item
        for item in event_variants
        if _object_schema(item)["properties"]["type"]["enum"] == ["head"]
    )
    head_schema = _object_schema(head)
    later = next(
        branch
        for branch in head_schema["anyOf"]
        if _object_schema(branch)["properties"]["timing_choice"]["enum"] == ["later"]
    )

    beats = head_schema["properties"]["beats"]
    assert isinstance(beats, dict)
    assert beats["maxItems"] == 2
    assert "beats" in _object_schema(later)["required"]


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
