import json

import httpx
import pytest

from companion_daemon.llm import DeepSeekChatModel
from companion_daemon.world_v2.biographical_claim_authority import (
    biographical_coordinate_authorities,
)
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.expression_draft import (
    ExpressionDraft,
    TEXT_ONLY_EXPRESSION_CAPABILITIES,
    build_source_ref_alias_table,
    qq_expression_capabilities,
    validate_expression_private_turn_state,
    world_claim_source_ref_aliases_by_scope,
)
from companion_daemon.world_v2.structured_expression_reselection_model import (
    EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT,
    StructuredExpressionReselectionModel,
    expression_reselection_output_contract,
    expression_reselection_tool_contract,
    normalize_expression_reselection_output,
    normalize_realtime_expression_reselection_output,
)


_WORLD_CLAIM_ALIASES = {
    "current_world": ("s1",),
    "past_world": (),
    "counterpart_history": (),
    "shared_history": (),
    "stable_identity": ("s2",),
}


def _contract_messages(contract: dict[str, object]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Choose freely and return the contracted JSON."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "contract": "source-closure-reselection.2",
                    "output_contract": contract,
                },
                separators=(",", ":"),
            ),
        },
    ]


def _strict_schema(payload: dict[str, object]) -> dict[str, object]:
    response_format = payload["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    envelope = response_format["json_schema"]
    assert isinstance(envelope, dict)
    assert envelope["name"] == "expression_source_reselection_direct_v1"
    assert envelope["strict"] is True
    schema = envelope["schema"]
    assert isinstance(schema, dict)
    return schema


def _assert_closed_objects_are_fully_required(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _assert_closed_objects_are_fully_required(item)
        return
    if not isinstance(value, dict):
        return
    if value.get("type") == "object":
        properties = value.get("properties")
        if isinstance(properties, dict):
            assert value.get("additionalProperties") is False
            assert list(value.get("required", ())) == list(properties)
    for child in value.values():
        _assert_closed_objects_are_fully_required(child)


def _assert_local_schema_refs_resolve(schema: dict[str, object], value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _assert_local_schema_refs_resolve(schema, item)
        return
    if not isinstance(value, dict):
        return
    local_ref = value.get("$ref")
    if isinstance(local_ref, str):
        assert local_ref.startswith("#/")
        target: object = schema
        for part in local_ref[2:].split("/"):
            assert isinstance(target, dict)
            assert part in target
            target = target[part]
    for child in value.values():
        _assert_local_schema_refs_resolve(schema, child)


def _assert_provider_schema_omits_unsupported_unique_items(value: object) -> None:
    if isinstance(value, list):
        for item in value:
            _assert_provider_schema_omits_unsupported_unique_items(item)
        return
    if not isinstance(value, dict):
        return
    assert "uniqueItems" not in value
    for child in value.values():
        _assert_provider_schema_omits_unsupported_unique_items(child)


@pytest.mark.asyncio
async def test_source_reselection_uses_capability_bound_strict_expression_schema() -> None:
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "expression_draft": {
                                        "private_turn_state": {
                                            "contract": "private-turn-state.1",
                                            "inner_state_summary": "我现在只想说眼前真实的感觉。",
                                            "attended_source_refs": ["s1"],
                                        },
                                        "timing_choice": "now",
                                        "cadence": "conversational",
                                        "beats": [
                                            {
                                                "modality": "text",
                                                "text": "我现在有点想听你继续说。",
                                                "reaction_id": None,
                                                "sticker_id": None,
                                            }
                                        ],
                                        "delay_position_bp": None,
                                        "expires_after_seconds": None,
                                        "stance": "present",
                                        "brief_rationale": "Choose from the present state.",
                                        "impulse_summary": None,
                                        "confidence": 8000,
                                        "variation_profile": None,
                                        "response_expectation": None,
                                        "response_expectation_assessment": None,
                                        "world_claims": [],
                                    },
                                    "episode_disposition": "complete_without_more",
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            },
        )

    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")
    contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("s1", "s2"),
        world_claim_source_ref_aliases_by_scope=_WORLD_CLAIM_ALIASES,
        response_expectation_assessment_required=False,
        combined=False,
    )
    model = StructuredExpressionReselectionModel(
        "key",
        "https://api.openai.com/v1",
        "role-reselector",
        transport=httpx.MockTransport(handler),
    )

    raw = await model.complete_json(_contract_messages(contract), temperature=0.0)

    assert json.loads(raw)["expression_draft"]["beats"][0]["text"] == ("我现在有点想听你继续说。")
    schema = _strict_schema(captured_payload)
    _assert_closed_objects_are_fully_required(schema)
    _assert_provider_schema_omits_unsupported_unique_items(schema)
    assert list(schema["properties"]) == ["expression_draft", "episode_disposition"]
    expression = schema["properties"]["expression_draft"]
    assert isinstance(expression, dict)
    branches = expression["anyOf"]
    now_branches = [
        branch for branch in branches if branch["properties"]["timing_choice"]["enum"] == ["now"]
    ]
    later_branch = next(
        branch for branch in branches if branch["properties"]["timing_choice"]["enum"] == ["later"]
    )
    silent_branch = next(
        branch for branch in branches if branch["properties"]["timing_choice"]["enum"] == ["silent"]
    )
    assert len(now_branches) == 1
    assert "typing_prefix_count" not in now_branches[0]["properties"]
    for branch in branches:
        assert next(iter(branch["properties"])) == "private_turn_state"
        assert branch["properties"]["cadence"]["enum"] == [
            "rapid",
            "conversational",
            "hesitant",
            "escalating",
        ]
    assert now_branches[0]["properties"]["beats"]["minItems"] == 1
    assert now_branches[0]["properties"]["beats"]["maxItems"] == capabilities.max_beats
    assert later_branch["properties"]["beats"]["maxItems"] == capabilities.max_later_beats
    assert silent_branch["properties"]["beats"]["maxItems"] == 0
    private_state = schema["$defs"]["PrivateTurnState"]
    assert private_state["properties"]["inner_state_summary"]["pattern"] == "\\S"
    assert private_state["properties"]["attended_source_refs"]["items"]["enum"] == [
        "s1",
        "s2",
    ]
    beat_branches = schema["$defs"]["VisibleExpressionBeatDraftChoice"]["anyOf"]
    assert [branch["properties"]["modality"]["enum"][0] for branch in beat_branches] == list(
        modality for modality in capabilities.modalities if modality != "typing"
    )
    ordered_beat_branches = schema["$defs"]["ExpressionBeatDraftChoice"]["anyOf"]
    assert [
        branch["properties"]["modality"]["enum"][0]
        for branch in ordered_beat_branches
    ] == list(capabilities.modalities)
    claim_branches = schema["$defs"]["WorldClaimDraft"]["anyOf"]
    claim_by_scope = {branch["properties"]["scope"]["enum"][0]: branch for branch in claim_branches}
    assert set(claim_by_scope) == {"current_world", "stable_identity"}
    assert claim_by_scope["current_world"]["properties"]["source_refs"] == {
        "type": "array",
        "items": {"type": "string", "enum": ["s1"]},
        "minItems": 1,
        "maxItems": 1,
    }
    assert claim_by_scope["stable_identity"]["properties"]["source_refs"]["items"]["enum"] == ["s2"]
    assert contract["contract"] == EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT
    assert contract["provider_message_bound"] is True
    assert isinstance(contract["schema_sha256"], str)
    assert contract["schema_sha256"].startswith("sha256:")
    await model.aclose()


def test_strict_reselection_schema_has_no_dangling_local_refs() -> None:
    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")
    contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("s1", "s2"),
        world_claim_source_ref_aliases_by_scope=_WORLD_CLAIM_ALIASES,
        response_expectation_assessment_required=False,
        combined=False,
    )
    model = StructuredExpressionReselectionModel(
        "key",
        "https://api.openai.com/v1",
        "role-reselector",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_messages(contract),
            temperature=0.0,
            json_object=True,
        )
    )

    _assert_local_schema_refs_resolve(schema, schema)


def test_expression_reselection_contract_binds_current_capabilities() -> None:
    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")

    contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("s2", "s1", "s1"),
        world_claim_source_ref_aliases_by_scope=_WORLD_CLAIM_ALIASES,
        response_expectation_assessment_required=True,
        combined=False,
    )

    assert contract["contract"] == EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT
    assert contract["profile_id"] == capabilities.profile_id
    assert contract["modalities"] == list(capabilities.modalities)
    assert contract["reaction_ids"] == [item.option_id for item in capabilities.reaction_options]
    assert contract["sticker_ids"] == [item.option_id for item in capabilities.sticker_options]
    assert contract["max_beats"] == capabilities.max_beats
    assert contract["max_later_beats"] == capabilities.max_later_beats
    assert contract["private_turn_state_required"] is True
    assert contract["cadence_required"] is True
    assert contract["allowed_source_ref_aliases"] == ["s1", "s2"]
    assert contract["world_claim_source_ref_aliases_by_scope"] == {
        scope: list(refs) for scope, refs in _WORLD_CLAIM_ALIASES.items()
    }
    assert contract["response_expectation_assessment_required"] is True
    assert contract["provider_message_bound"] is True
    assert isinstance(contract["schema_sha256"], str)
    assert contract["schema_sha256"].startswith("sha256:")


def test_expression_reselection_tool_contract_is_lossless_and_strict() -> None:
    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")
    output_contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("s1", "s2"),
        world_claim_source_ref_aliases_by_scope=_WORLD_CLAIM_ALIASES,
        response_expectation_assessment_required=False,
        combined=False,
    )

    compiled = expression_reselection_tool_contract(output_contract)

    assert compiled.provider_tool_choice == {
        "type": "function",
        "function": {"name": "character_expression_reselection_v1"},
    }
    assert len(compiled.provider_tools) == 1
    function = compiled.provider_tools[0]["function"]
    assert isinstance(function, dict)
    assert function["name"] == "character_expression_reselection_v1"
    parameters = function["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["type"] == "object"
    assert "$defs" not in parameters
    _assert_closed_objects_are_fully_required(parameters)

    authored = {
        "expression_draft": {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "我想把这一句说清楚。",
                "attended_source_refs": ["s1"],
            },
            "timing_choice": "silent",
            "cadence": "conversational",
            "beats": [],
            "delay_position_bp": None,
            "expires_after_seconds": None,
            "stance": "quiet",
            "brief_rationale": "这次先不打扰。",
            "impulse_summary": None,
            "confidence": 7000,
            "variation_profile": None,
            "response_expectation": None,
            "response_expectation_assessment": None,
            "world_claims": [],
        },
        "episode_disposition": "complete_without_more",
    }
    raw = json.dumps(authored, ensure_ascii=False, separators=(",", ":"))
    assert json.loads(compiled.unwrap(raw)) == authored
    with pytest.raises(ValueError, match="expression reselection tool"):
        compiled.unwrap(json.dumps({"expression_draft": authored["expression_draft"]}))
    with pytest.raises(ValueError, match="expression reselection tool"):
        compiled.unwrap(
            json.dumps(
                {
                    **authored,
                    "unexpected": True,
                }
            )
        )


@pytest.mark.asyncio
async def test_expression_reselection_tool_contract_survives_deepseek_http_adapter() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
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
                                        "name": "character_expression_reselection_v1",
                                        "arguments": '{"expression_draft":{},"episode_disposition":null}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")
    output_contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("s1",),
        world_claim_source_ref_aliases_by_scope={
            **_WORLD_CLAIM_ALIASES,
            "stable_identity": (),
        },
        response_expectation_assessment_required=False,
        combined=False,
    )
    compiled = expression_reselection_tool_contract(output_contract)
    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com/v1",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )

    raw = await model.complete_json(
        [{"role": "user", "content": "return the tool result"}],
        temperature=0.0,
        tools=list(compiled.provider_tools),
        tool_choice=compiled.provider_tool_choice,
    )

    assert json.loads(raw) == {
        "expression_draft": {},
        "episode_disposition": None,
    }
    assert captured["tools"] == list(compiled.provider_tools)
    assert captured["tool_choice"] == compiled.provider_tool_choice
    assert "response_format" not in captured
    await model.aclose()


@pytest.mark.asyncio
async def test_structured_reselection_tool_route_omits_json_response_format() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
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
                                        "name": "character_expression_reselection_v1",
                                        "arguments": "{\"expression_draft\":{},\"episode_disposition\":null}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")
    output_contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("s1",),
        world_claim_source_ref_aliases_by_scope={
            **_WORLD_CLAIM_ALIASES,
            "stable_identity": (),
        },
        response_expectation_assessment_required=False,
        combined=False,
    )
    compiled = expression_reselection_tool_contract(output_contract)
    model = StructuredExpressionReselectionModel(
        "key",
        "https://api.openai.com/v1",
        "role-reselector",
        transport=httpx.MockTransport(handler),
    )

    raw = await model.complete_json(
        _contract_messages(output_contract),
        temperature=0.0,
        tools=list(compiled.provider_tools),
        tool_choice=compiled.provider_tool_choice,
    )

    assert json.loads(raw) == {"expression_draft": {}, "episode_disposition": None}
    assert captured["tools"] == list(compiled.provider_tools)
    assert captured["tool_choice"] == compiled.provider_tool_choice
    assert "response_format" not in captured
    await model.aclose()


def test_provider_advertised_biographical_coordinate_is_valid_private_attention() -> None:
    context = {
        "world_id": "world:strict-reselection-biography",
        "actor_ref": "agent:companion",
        "world_revision": 4,
        "logical_time": "2026-08-01T08:00:00+00:00",
        "slices": {
            "world_life": {
                "availability": "available",
                "source_refs": ["event:clock:4"],
                "items": [
                    {
                        "item_ref": "biography:current",
                        "source_hash": "a" * 64,
                        "value_hash": "b" * 64,
                        "source_bindings": [],
                        "value": {
                            "context_kind": "biographical_context",
                            "logical_at": "2026-08-01T08:00:00+00:00",
                            "season": "summer",
                        },
                    }
                ],
            }
        },
    }
    request = ModelInput(
        call_id="call:strict-reselection-biography",
        attempt_id="attempt:strict-reselection-biography",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:strict-reselection-biography",
        evaluated_world_revision=4,
        model_content_json=json.dumps(context),
        trigger_message=TriggerMessage(
            event_ref="event:observation:strict-reselection-biography",
            event_payload_hash="sha256:" + "c" * 64,
            observation_ref="observation:strict-reselection-biography",
            source_world_revision=4,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:10001",
            platform_message_id="qq-message:strict-reselection-biography",
            text="今天好热。",
        ),
    )
    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")
    aliases = build_source_ref_alias_table(request=request)
    coordinate_ref = biographical_coordinate_authorities(context)[0].source_ref
    coordinate_alias = aliases.alias_for(coordinate_ref) or coordinate_ref
    contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=tuple(
            sorted(aliases.alias_for(ref) or ref for ref in aliases.canonical_refs)
        ),
        world_claim_source_ref_aliases_by_scope=world_claim_source_ref_aliases_by_scope(
            request=request,
            source_ref_aliases=aliases,
        ),
        response_expectation_assessment_required=False,
        combined=False,
    )
    assert coordinate_alias in contract["allowed_source_ref_aliases"]

    state = validate_expression_private_turn_state(
        value={
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "我注意到了现在正值盛夏。",
                "attended_source_refs": [coordinate_alias],
            }
        },
        request=request,
        capabilities=capabilities,
        source_ref_aliases=aliases,
    )

    assert state is not None
    assert state.attended_source_refs == (coordinate_ref,)

    with pytest.raises(ValueError, match="private_turn_state.unpinned_source"):
        validate_expression_private_turn_state(
            value={
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "这条引用只是伪装成同类坐标。",
                    "attended_source_refs": ["biography-coordinate:sha256:" + "d" * 64],
                }
            },
            request=request,
            capabilities=capabilities,
            source_ref_aliases=aliases,
        )


@pytest.mark.asyncio
async def test_reselection_removes_reaction_without_provider_message_binding() -> None:
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ignored":true}'}}]},
        )

    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")
    contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=(),
        world_claim_source_ref_aliases_by_scope={scope: () for scope in _WORLD_CLAIM_ALIASES},
        response_expectation_assessment_required=True,
        combined=False,
        provider_message_bound=False,
    )
    model = StructuredExpressionReselectionModel(
        "key",
        "https://api.openai.com/v1",
        "role-reselector",
        transport=httpx.MockTransport(handler),
    )

    await model.complete_json(_contract_messages(contract), temperature=0.0)

    schema = _strict_schema(captured_payload)
    beat_choices = schema["$defs"]["VisibleExpressionBeatDraftChoice"]["anyOf"]
    assert [branch["properties"]["modality"]["enum"][0] for branch in beat_choices] == [
        "text",
        "sticker",
    ]
    assert (
        schema["$defs"]["PrivateTurnState"]["properties"]["attended_source_refs"]["maxItems"] == 0
    )
    expression_branches = schema["properties"]["expression_draft"]["anyOf"]
    assert all(
        branch["properties"]["world_claims"]["maxItems"] == 0 for branch in expression_branches
    )
    assessment = schema["properties"]["expression_draft"]["anyOf"][0]["properties"][
        "response_expectation_assessment"
    ]
    assert assessment == {"$ref": "#/$defs/ResponseExpectationAssessmentDraft"}
    await model.aclose()


def test_reselection_contract_digest_rejects_tampered_capabilities() -> None:
    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")
    contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("s1",),
        world_claim_source_ref_aliases_by_scope={
            **_WORLD_CLAIM_ALIASES,
            "stable_identity": (),
        },
        response_expectation_assessment_required=False,
        combined=False,
    )
    contract["max_beats"] = 9
    model = StructuredExpressionReselectionModel(
        "key",
        "https://api.openai.com/v1",
        "role-reselector",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    with pytest.raises(ValueError, match="schema digest mismatch"):
        model.request_payload(
            _contract_messages(contract),
            temperature=0.0,
            json_object=True,
        )


def test_reselection_tool_contract_rejects_tampered_contract_identity() -> None:
    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")
    contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("s1",),
        world_claim_source_ref_aliases_by_scope={
            **_WORLD_CLAIM_ALIASES,
            "stable_identity": (),
        },
        response_expectation_assessment_required=False,
        combined=False,
    )
    contract["contract"] = "expression-source-reselection-direct.legacy"

    with pytest.raises(ValueError, match="contract identity"):
        expression_reselection_tool_contract(contract)


def test_direct_contract_fails_closed_for_combined_cognition_wire() -> None:
    capabilities = qq_expression_capabilities("napcat", recorded_cadence_mode="shadow")

    with pytest.raises(ValueError, match="combined cognition"):
        expression_reselection_output_contract(
            capabilities=capabilities,
            allowed_source_ref_aliases=("s1",),
            world_claim_source_ref_aliases_by_scope={
                **_WORLD_CLAIM_ALIASES,
                "stable_identity": (),
            },
            response_expectation_assessment_required=False,
            combined=True,
        )


def test_strict_reselection_wire_normalizes_only_model_owned_positions() -> None:
    raw = json.dumps(
        {
            "expression_draft": {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我想停一下再把这件事说清楚。",
                    "attended_source_refs": ["s1"],
                },
                "timing_choice": "later",
                "cadence": "hesitant",
                "typing_prefix_count": 0,
                "beats": [
                    {
                        "modality": "text",
                        "text": "我等会儿跟你说。",
                        "reaction_id": None,
                        "sticker_id": None,
                    }
                ],
                "delay_position_bp": 2_500,
                "expires_after_seconds": 3_602,
                "stance": "pause_then_return",
                "brief_rationale": "I want a short pause before replying.",
                "impulse_summary": None,
                "confidence": 7_000,
                "variation_profile": None,
                "response_expectation": {
                    "hoped_response": "对方愿意等我一下",
                    "pressure_bp": 500,
                    "importance_bp": 4_000,
                    "wait_position_bp": 5_000,
                    "expires_after_seconds": 3_600,
                },
                "response_expectation_assessment": None,
                "world_claims": [],
            },
            "episode_disposition": "complete_without_more",
        },
        ensure_ascii=False,
    )

    normalized = json.loads(normalize_expression_reselection_output(raw))

    draft = normalized["expression_draft"]
    assert "typing_prefix_count" not in draft
    assert "delay_position_bp" not in draft
    assert draft["delay_seconds"] == 901
    assert draft["expires_after_seconds"] == 3_602
    expectation = draft["response_expectation"]
    assert "wait_position_bp" not in expectation
    assert expectation["wait_seconds"] == 1_814
    assert expectation["expires_after_seconds"] == 3_600


@pytest.mark.parametrize(
    ("expiry_seconds", "position_bp", "expected_delay_seconds"),
    (
        (86_401, 0, 1),
        (86_401, 10_000, 86_400),
        (172_800, 0, 1),
        (172_800, 10_000, 86_400),
    ),
)
def test_strict_later_position_stays_within_materializer_delay_bound(
    expiry_seconds: int,
    position_bp: int,
    expected_delay_seconds: int,
) -> None:
    raw = json.dumps(
        {
            "expression_draft": {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我想晚些再把这件事说完整。",
                    "attended_source_refs": [],
                },
                "timing_choice": "later",
                "cadence": "hesitant",
                "typing_prefix_count": 0,
                "beats": [
                    {
                        "modality": "text",
                        "text": "我晚些来找你。",
                        "reaction_id": None,
                        "sticker_id": None,
                    }
                ],
                "delay_position_bp": position_bp,
                "expires_after_seconds": expiry_seconds,
                "stance": "return_later",
                "brief_rationale": "Choose the latest executable point in the interval.",
                "impulse_summary": None,
                "confidence": 7_000,
                "variation_profile": None,
                "response_expectation": None,
                "response_expectation_assessment": None,
                "world_claims": [],
            },
            "episode_disposition": None,
        },
        ensure_ascii=False,
    )

    normalized = json.loads(normalize_expression_reselection_output(raw))["expression_draft"]

    draft = ExpressionDraft.model_validate_json(
        json.dumps(normalized, ensure_ascii=False),
        strict=True,
    )
    assert draft.delay_seconds == expected_delay_seconds
    assert draft.expires_after_seconds == expiry_seconds


@pytest.mark.parametrize(
    ("expiry_seconds", "position_bp", "expected_wait_seconds"),
    (
        (86_401, 0, 30),
        (86_401, 10_000, 86_400),
        (172_800, 0, 30),
        (172_800, 10_000, 86_400),
    ),
)
def test_strict_expectation_position_stays_within_materializer_wait_bound(
    expiry_seconds: int,
    position_bp: int,
    expected_wait_seconds: int,
) -> None:
    raw = json.dumps(
        {
            "expression_draft": {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我想把话留在这里，让对方自己决定什么时候接。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "cadence": "conversational",
                "typing_prefix_count": 0,
                "beats": [
                    {
                        "modality": "text",
                        "text": "你想说的时候再接着说就好。",
                        "reaction_id": None,
                        "sticker_id": None,
                    }
                ],
                "delay_position_bp": None,
                "expires_after_seconds": None,
                "stance": "leave_space",
                "brief_rationale": "Leave the invitation open without pressure.",
                "impulse_summary": None,
                "confidence": 7_000,
                "variation_profile": None,
                "response_expectation": {
                    "hoped_response": "对方想继续时自然接话",
                    "pressure_bp": 0,
                    "importance_bp": 2_000,
                    "wait_position_bp": position_bp,
                    "expires_after_seconds": expiry_seconds,
                },
                "response_expectation_assessment": None,
                "world_claims": [],
            },
            "episode_disposition": None,
        },
        ensure_ascii=False,
    )

    normalized = json.loads(normalize_expression_reselection_output(raw))["expression_draft"]

    draft = ExpressionDraft.model_validate_json(
        json.dumps(normalized, ensure_ascii=False),
        strict=True,
    )
    assert draft.response_expectation is not None
    assert draft.response_expectation.wait_seconds == expected_wait_seconds
    assert draft.response_expectation.expires_after_seconds == expiry_seconds


def test_strict_reselection_wire_preserves_interleaved_typing_order() -> None:
    raw = json.dumps(
        {
            "expression_draft": {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我有点犹豫，但还是想分两句说。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "cadence": "hesitant",
                "beats": [
                    {
                        "modality": "text",
                        "text": "其实我刚才有点不高兴。",
                        "reaction_id": None,
                        "sticker_id": None,
                    },
                    {
                        "modality": "typing",
                        "text": None,
                        "reaction_id": None,
                        "sticker_id": None,
                    },
                    {
                        "modality": "text",
                        "text": "但不是不想理你。",
                        "reaction_id": None,
                        "sticker_id": None,
                    },
                ],
                "delay_position_bp": None,
                "expires_after_seconds": None,
                "stance": "honest",
                "brief_rationale": "Say what I actually feel.",
                "impulse_summary": None,
                "confidence": 7_500,
                "variation_profile": None,
                "response_expectation": None,
                "response_expectation_assessment": None,
                "world_claims": [],
            },
            "episode_disposition": None,
        },
        ensure_ascii=False,
    )

    draft = json.loads(normalize_realtime_expression_reselection_output(raw))[
        "expression_draft"
    ]

    assert [beat["modality"] for beat in draft["beats"]] == [
        "text",
        "typing",
        "text",
    ]


@pytest.mark.parametrize(
    "duplicate_field",
    ("attended_source_refs", "world_claim.source_refs"),
)
def test_realtime_reselection_rejects_duplicate_source_refs_locally(
    duplicate_field: str,
) -> None:
    expression = {
        "private_turn_state": {
            "contract": "private-turn-state.1",
            "inner_state_summary": "我只引用当前真正注意到的证据。",
            "attended_source_refs": ["s1"],
        },
        "timing_choice": "now",
        "cadence": "conversational",
        "beats": [
            {
                "modality": "text",
                "text": "我知道了。",
                "reaction_id": None,
                "sticker_id": None,
            }
        ],
        "delay_position_bp": None,
        "expires_after_seconds": None,
        "stance": "present",
        "brief_rationale": "Stay with the current evidence.",
        "impulse_summary": None,
        "confidence": 7_500,
        "variation_profile": None,
        "response_expectation": None,
        "response_expectation_assessment": None,
        "world_claims": [],
    }
    if duplicate_field == "attended_source_refs":
        expression["private_turn_state"]["attended_source_refs"] = ["s1", "s1"]
    else:
        expression["world_claims"] = [
            {
                "claim_text": "眼前这件事发生了。",
                "scope": "current_world",
                "source_refs": ["s1", "s1"],
            }
        ]
    raw = json.dumps(
        {
            "expression_draft": expression,
            "episode_disposition": "complete_without_more",
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="duplicate source refs"):
        normalize_realtime_expression_reselection_output(raw)


def test_legacy_canonical_expression_wire_remains_byte_compatible() -> None:
    raw = json.dumps(
        {
            "timing_choice": "silent",
            "cadence": "conversational",
            "beats": [],
            "delay_seconds": None,
            "expires_after_seconds": None,
            "stance": "quiet",
            "brief_rationale": "Historical canonical replay fixture.",
            "confidence": 5_000,
            "response_expectation": None,
            "response_expectation_assessment": None,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    assert normalize_expression_reselection_output(raw) == raw
    assert ExpressionDraft.model_validate_json(raw, strict=True).timing_choice == "silent"


def test_realtime_reselection_rejects_a_historical_canonical_expression_wire() -> None:
    raw = json.dumps(
        {
            "timing_choice": "silent",
            "cadence": "conversational",
            "beats": [],
            "delay_seconds": None,
            "expires_after_seconds": None,
            "stance": "quiet",
            "brief_rationale": "Historical canonical replay fixture.",
            "confidence": 5_000,
            "response_expectation": None,
            "response_expectation_assessment": None,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="strict expression reselection requires its envelope"):
        normalize_realtime_expression_reselection_output(raw)
    assert normalize_expression_reselection_output(raw) == raw


def test_text_only_strict_schema_cannot_inject_typing_before_materialization() -> None:
    capabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )
    contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("s1",),
        world_claim_source_ref_aliases_by_scope={
            **_WORLD_CLAIM_ALIASES,
            "stable_identity": (),
        },
        response_expectation_assessment_required=False,
        combined=False,
    )
    model = StructuredExpressionReselectionModel(
        "key",
        "https://api.openai.com/v1",
        "role-reselector",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_messages(contract),
            temperature=0.0,
            json_object=True,
        )
    )
    now_branches = [
        branch
        for branch in schema["properties"]["expression_draft"]["anyOf"]
        if branch["properties"]["timing_choice"]["enum"] == ["now"]
    ]

    assert len(now_branches) == 1
    assert "typing_prefix_count" not in now_branches[0]["properties"]
    beat_branches = schema["$defs"]["ExpressionBeatDraftChoice"]["anyOf"]
    assert [branch["properties"]["modality"]["enum"] for branch in beat_branches] == [
        ["text"]
    ]
    strict_raw = json.dumps(
        {
            "expression_draft": {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我现在想直接说这一句。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [
                    {
                        "modality": "text",
                        "text": "我在。",
                        "reaction_id": None,
                        "sticker_id": None,
                    }
                ],
                "delay_position_bp": None,
                "expires_after_seconds": None,
                "stance": "present",
                "brief_rationale": "Say the chosen reply directly.",
                "impulse_summary": None,
                "confidence": 8_000,
                "variation_profile": None,
                "response_expectation": None,
                "response_expectation_assessment": None,
                "world_claims": [],
            },
            "episode_disposition": None,
        },
        ensure_ascii=False,
    )
    normalized = json.loads(normalize_expression_reselection_output(strict_raw))["expression_draft"]

    draft = ExpressionDraft.model_validate_json(
        json.dumps(normalized, ensure_ascii=False),
        strict=True,
    )
    assert [beat.modality for beat in draft.beats] == ["text"]
