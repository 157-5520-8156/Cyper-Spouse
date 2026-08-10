from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from time import perf_counter_ns

import httpx
from jsonschema import Draft202012Validator, ValidationError
import pytest

from companion_daemon.llm import DeepSeekChatModel
from companion_daemon.world_v2.character_interior import CharacterInterior, InteriorOpportunity
from companion_daemon.world_v2.character_interior.contracts import (
    FACET_NAMES,
    _InteriorCapabilityManifest,
)
from companion_daemon.world_v2.character_interior.ports import _InteriorRoleRequest
from companion_daemon.world_v2.character_interior.structured_role import (
    PurposeDecisionContract,
    StructuredCharacterRoleFaculty as _ProductionStructuredCharacterRoleFaculty,
    StructuredRoleResultError,
)
from companion_daemon.world_v2.character_interior.structured_role_tool_contract import (
    StructuredRoleToolContracts,
)
from companion_daemon.world_v2.character_interior.production import (
    compose_fixture_character_interior,
)
from companion_daemon.world_v2.expression_draft import QQ_NAPCAT_EXPRESSION_CAPABILITIES
from companion_daemon.world_v2.schemas import ProjectionCursor


_NOW = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)
_CURSOR = ProjectionCursor(
    world_revision=23,
    deliberation_revision=11,
    ledger_sequence=71,
)

_TEST_GENERIC_CONTRACT = PurposeDecisionContract(
    purpose="generic",
    payload_contract="test-character-interior-generic-decision.1",
    capability_kind=None,
)


class StructuredCharacterRoleFaculty(_ProductionStructuredCharacterRoleFaculty):
    """Install the open generic purpose only inside this contract test module."""

    def __init__(self, *args, purpose_contracts=(), **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(
            *args,
            purpose_contracts=(_TEST_GENERIC_CONTRACT, *purpose_contracts),
            **kwargs,
        )


class _Projection:
    async def project(self, *, subject):
        del subject
        return {
            "world_id": "world:test",
            "actor_ref": "character:zhizhi",
            "cursor": _CURSOR,
            "logical_time": _NOW,
            "situation": {
                "availability": "available",
                "content": {"activity": "sorting photos after lunch"},
                "source_refs": ("source:situation",),
            },
            "continuity": {
                "availability": "available",
                "content": {"open_thread": "a place mentioned earlier"},
                "source_refs": ("source:continuity",),
            },
            "facets": {
                name: {
                    "availability": "available",
                    "content": {"summary": f"current {name}"},
                    "source_refs": (f"source:{name}",),
                }
                for name in FACET_NAMES
            },
        }


class _ProjectionOnlyRole:
    name = "projection-only-test-role"

    async def experience(self, request):  # pragma: no cover - projection only
        raise AssertionError(request)

    async def consider(self, request):  # pragma: no cover - projection only
        raise AssertionError(request)


class _QueueModel:
    model = "deepseek-chat"

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[list[dict[str, str]], float]] = []
        self.fallback = _ForbiddenFallback()

    async def complete(self, messages, *, temperature=0.8):
        self.calls.append((messages, temperature))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def complete_json(
        self,
        messages,
        *,
        temperature=0.8,
        tools=None,
        tool_choice=None,
    ):
        del tools, tool_choice
        return await self.complete(messages, temperature=temperature)


class _RequiredToolQueueModel(_QueueModel):
    supports_required_tool_choice = True

    def __init__(self, *responses: object) -> None:
        super().__init__(*responses)
        self.tool_calls: list[tuple[list[dict[str, object]], object]] = []

    async def complete_json(
        self,
        messages,
        *,
        temperature=0.8,
        tools,
        tool_choice,
    ):
        self.calls.append((messages, temperature))
        self.tool_calls.append((tools, tool_choice))
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _FailingRequiredToolModel:
    model = "required-tool-failure"
    supports_required_tool_choice = True

    def __init__(self) -> None:
        self.forced_calls = 0
        self.plain_calls = 0

    async def complete_json(
        self,
        messages,
        *,
        temperature=0.8,
        tools,
        tool_choice,
    ):
        del messages, temperature, tools, tool_choice
        self.forced_calls += 1
        raise TypeError("provider required-tool transport failed")

    async def complete(self, messages, *, temperature=0.8):
        del messages, temperature
        self.plain_calls += 1
        return _result(status="silent")


class _ForbiddenFallback:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature=0.8):  # pragma: no cover
        del messages, temperature
        self.calls += 1
        return '{"status":"silent"}'


def _manifest(*tokens: str, kind: str = "media_selection") -> _InteriorCapabilityManifest:
    payload: dict[str, object] = {"offered_tokens": list(tokens)}
    if kind == "expression_reconsideration":
        payload["allowed_dispositions"] = list(tokens)
    if kind == "private_impression_reflection":
        payload.update(
            {
                "short_tokens": list(tokens),
                "anchor_short_tokens": list(tokens[:1]),
                "existing_impression_short_tokens": list(tokens[1:]),
                "expiry_conditions": [
                    "until_appraisal_contradicted",
                    "until_counter_evidence",
                    "until_relationship_stage_changes",
                    "one_month_without_support",
                ],
            }
        )
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _InteriorCapabilityManifest(
        capability_ref=f"capability:{kind}:71",
        capability_kind=kind,
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=("source:private_self",),
    )


def _proactive_manifest() -> _InteriorCapabilityManifest:
    payload_json = json.dumps(
        {
            "contract": "character-interior-proactive-capability.1",
            "expression_capabilities": QQ_NAPCAT_EXPRESSION_CAPABILITIES.prompt_value(),
            "source_opportunity": {"source_kind": "spontaneous_contact"},
            "target_ref": "user:primary",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _InteriorCapabilityManifest(
        capability_ref="capability:proactive:test",
        capability_kind="proactive_contact",
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=("source:private_self",),
    )


def _world_stimulus_manifest() -> _InteriorCapabilityManifest:
    payload_json = json.dumps(
        {
            "contract": "character-interior-world-stimulus-capability.1",
            "process_kind": "settled_world_appraisal",
            "stimulus_kind": "settled_world_occurrence",
            "source_event": {
                "event_id": "source:world-occurrence",
                "event_type": "WorldOccurrenceSettled",
            },
            "result_choices": ["no_change", "activate"],
            "relationship_subject_refs": [],
            "active_affect_heads": [],
            "affect_target_lower_bounds": {"bounds": []},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _InteriorCapabilityManifest(
        capability_ref="capability:world-stimulus:test",
        capability_kind="world_stimulus_appraisal",
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=("source:private_self", "source:world-occurrence"),
    )


def _private_impression_manifest() -> _InteriorCapabilityManifest:
    payload_json = json.dumps(
        {
            "contract": "character-interior-private-impression-capability.1",
            "offered_tokens": ["s0", "s1", "s2"],
            "short_tokens": ["s0", "s1", "s2"],
            "anchor_short_tokens": ["s0"],
            "existing_impression_short_tokens": ["s2"],
            "expiry_conditions": [
                "until_appraisal_contradicted",
                "until_counter_evidence",
                "until_relationship_stage_changes",
                "one_month_without_support",
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _InteriorCapabilityManifest(
        capability_ref="capability:private-impression:test",
        capability_kind="private_impression_reflection",
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=("source:private_self", "source:appraisal"),
    )


def _memory_retention_manifest(
    *,
    kind: str = "fact_memory_retention",
) -> _InteriorCapabilityManifest:
    payload = {
        "source_kind": (
            "verified_user_fact"
            if kind == "fact_memory_retention"
            else "companion_lived_experience"
        ),
        "predicate_code": "preference.likes",
        (
            "verified_source_text"
            if kind == "fact_memory_retention"
            else "verified_experience_text"
        ): (
            "用户明确说过自己喜欢乌龙茶。"
            if kind == "fact_memory_retention"
            else "她记得那次一起在雨里赶末班车。"
        ),
    }
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _InteriorCapabilityManifest(
        capability_ref=f"capability:{kind}:71",
        capability_kind=kind,
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=("source:private_self",),
    )


def _private_impression_result(*, status: str = "no_change") -> str:
    if status == "no_change":
        proposal = []
    else:
        proposal = [
            {
                "proposal_type": "private_impression_transition",
                "decision": "retain",
                "predecessor_refs": [],
                "source_refs": ["s0"],
                "reflection_summary": "A tentative interpretation remains revisable.",
                "confidence_bp": 5_500,
                "expiry_condition": "until_counter_evidence",
            }
        ]
    return json.dumps(
        {
            "status": status,
            "summary": "The moment has a tentative private meaning." if proposal else "No new private reading formed.",
            "attended_source_refs": ["source:private_self"],
            "decision": None,
            "recall_query": None,
            "proposals": proposal,
        },
        ensure_ascii=False,
    )


def _world_stimulus_no_change_result() -> str:
    return json.dumps(
        {
            "status": "no_change",
            "summary": "这次变化没有让我需要立刻调整什么。",
            "attended_source_refs": ["source:private_self"],
            "decision": None,
            "recall_query": None,
            "proposals": [
                {
                    "proposal_type": "world_stimulus_appraisal_result",
                    "decision": "no_change",
                    "brief_rationale": "我看到了，但现在没有形成新的感受。",
                    "behavior_tendency": "继续做自己的事",
                    "stance": "暂时保持原来的看法",
                    "display_strategy": "不对外表达",
                    "confidence": 5600,
                    "meaning_candidates": None,
                    "attribution": None,
                    "severity": None,
                    "expiry": None,
                    "affect_transition": None,
                    "relationship_signal": None,
                    "aspiration_transition": None,
                    "experience_transition": None,
                }
            ],
        },
        ensure_ascii=False,
    )


def _proactive_provider_payload_schema() -> dict[str, object]:
    contract = StructuredRoleToolContracts().proactive_contact(
        capability_payload=_proactive_manifest().payload,
        recall_allowed=True,
    )
    function = contract.provider_tools[0]["function"]
    parameters = function["parameters"]
    decision_branch = parameters["anyOf"][0]
    return decision_branch["properties"]["decision"]["properties"]["payload"]


def _valid_proactive_payload(**updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "timing_choice": "now",
        "cadence": "conversational",
        "beats": [{"modality": "text", "text": "想到你了。"}],
        "stance": "warm",
        "brief_rationale": "I want to say this now.",
        "impulse_summary": "The counterpart crossed my mind.",
        "confidence": 6500,
        "world_claims": [],
    }
    value.update(updates)
    return value


def _source_bound_media_manifest() -> _InteriorCapabilityManifest:
    payload_json = json.dumps(
        {
            "candidates": [
                {
                    "token": "media-token:source-bound",
                    "source_refs": ["event:image-evidence:1"],
                }
            ]
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    import hashlib

    return _InteriorCapabilityManifest(
        capability_ref="capability:media-selection:source-bound",
        capability_kind="media_selection",
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=("source:private_self", "event:image-evidence:1"),
    )


def _life_development_manifest() -> _InteriorCapabilityManifest:
    opens_at = _NOW + timedelta(hours=2)
    closes_at = _NOW + timedelta(hours=4)
    opportunity = {
        "decision": "propose",
        "authored_subject_ref": "character:zhizhi",
        "causal_authority": "character_choice",
        "outcome_resolution_authority": "character_choice",
        "premise_scope": "external_opportunity",
        "premise": "朋友临时问她要不要一起去看露天电影。",
        "premise_claim_refs": ["local:claim:screening"],
        "claim_declarations": [
            {
                "claim_id": "local:claim:screening",
                "summary": "今晚有一场可以自由参加的露天电影。",
                "scope": "novel_world_generation",
                "subject_scope": "world_environment",
                "source_refs": [],
            }
        ],
        "timing": {
            "mode": "later",
            "opens_at": opens_at.isoformat(),
            "closes_at": closes_at.isoformat(),
        },
        "anchor_refs": ["source:private_self"],
        "entity_refs": ["npc:friend"],
        "privacy_class": "shareable",
        "outcomes": [
            {
                "experienced_by_ref": "character:zhizhi",
                "text": "电影按计划放完了。",
                "privacy_class": "shareable",
                "relative_plausibility_weight": 1,
                "claim_refs": ["local:claim:screening"],
                "provisional_npcs": [],
                "dynamic_life_direction": None,
            },
            {
                "experienced_by_ref": "character:zhizhi",
                "text": "临时下雨，放映提前结束了。",
                "privacy_class": "shareable",
                "relative_plausibility_weight": 1,
                "claim_refs": ["local:claim:screening"],
                "provisional_npcs": [],
                "dynamic_life_direction": None,
            },
        ],
    }
    payload_json = json.dumps(
        {
            "external_opportunity": opportunity,
            "executable_envelope": {
                "opens_at": opens_at.isoformat(),
                "closes_at": closes_at.isoformat(),
                "participant_refs": ["npc:friend"],
            },
            "active_aspiration_source_refs": ["aspiration:travel"],
            "output_contract": {
                "no_op": {"decision": "no_op"},
                "accept": {"decision": "accept"},
            },
            "cross_field_authority": {
                "contract_version": "life-development-character-choice-authority.1"
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    import hashlib

    return _InteriorCapabilityManifest(
        capability_ref="capability:life-development-choice:71",
        capability_kind="life_development_choice",
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=("source:private_self",),
    )


async def _request(
    *,
    phase: str = "consider",
    purpose: str = "generic",
    capability_manifest: _InteriorCapabilityManifest | None = None,
    correction_ordinal: int = 0,
    correction_failure_code: str | None = None,
    correction_failure_detail: str | None = None,
) -> _InteriorRoleRequest:
    opportunity = InteriorOpportunity(
        opportunity_ref="opportunity:71",
        inner_turn_ref="turn:71",
        world_id="world:test",
        actor_ref="character:zhizhi",
        trigger_ref="trigger:71",
        cursor=_CURSOR,
        logical_time=_NOW,
        purpose=purpose,
        source_refs=("source:private_self",),
        capability_manifest=capability_manifest,
        context_note="One source-bound opportunity became available.",
    )
    interior = CharacterInterior(projection=_Projection(), role=_ProjectionOnlyRole())
    snapshot = await interior.project(opportunity)
    return _InteriorRoleRequest(
        inner_turn_id="character-inner-turn:test:71",
        phase=phase,
        subject_ref="subject:71",
        trigger_ref="trigger:71",
        purpose=purpose,
        context_note=opportunity.context_note,
        subject_source_refs=opportunity.source_refs,
        capability_manifest=capability_manifest,
        snapshot=snapshot,
        correction_ordinal=correction_ordinal,
        correction_failure_code=correction_failure_code,
        correction_failure_detail=correction_failure_detail,
    )


def _result(
    *,
    status: str,
    decision: dict[str, object] | None = None,
    recall_query: str | None = None,
) -> str:
    return json.dumps(
        {
            "status": status,
            "summary": f"private {status}",
            "attended_source_refs": ["source:private_self"],
            "decision": decision,
            "recall_query": recall_query,
            "proposals": [],
        },
        ensure_ascii=False,
    )


def _life_choice_result(completion: dict[str, object]) -> str:
    return _result(
        status="decision",
        decision={
            "source_refs": ["source:private_self"],
            "payload": {"completion": completion},
        },
    )


@pytest.mark.asyncio
async def test_consider_preserves_model_silence_without_substitute_message() -> None:
    model = _QueueModel(_result(status="silent"))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.consider(await _request())

    assert result["status"] == "silent"
    assert result["decision"] is None
    assert result["summary"] == "private silent"
    assert result["author_lineage"]["model_id"] == "deepseek-chat"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_decision_is_source_capability_and_author_lineage_bound() -> None:
    manifest = _manifest("media-token:1", "media-token:2")
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "decision": "select",
                    "selected_token": "media-token:2",
                },
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    result = await role.consider(
        await _request(purpose="media_selection", capability_manifest=manifest)
    )

    assert result["decision"] == {
        "contract": "character-interior-purpose-decision.1",
        "purpose": "media_selection",
        "source_refs": ["source:private_self"],
        "capability_ref": manifest.capability_ref,
        "capability_payload_hash": manifest.payload_hash,
        "payload": {
            "contract": "character-interior-media-selection-decision.1",
            "decision": "select",
            "selected_token": "media-token:2",
        },
    }
    lineage = result["author_lineage"]
    assert lineage["contract"] == "character-interior-author-lineage.1"
    assert lineage["model_id"] == "deepseek-chat-v4"
    assert lineage["model_call_id"].startswith("model-call:character-interior:sha256:")
    assert lineage["request_hash"].startswith("sha256:")
    assert lineage["response_hash"].startswith("sha256:")
    assert lineage["attempt_ordinal"] == 0
    assert lineage["parent_model_call_id"] is None


@pytest.mark.asyncio
async def test_bare_decision_payload_requires_explicit_decision_source_refs() -> None:

    manifest = _manifest("media-token:1", "media-token:2")
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "decision": "select",
                "selected_token": "media-token:2",
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    with pytest.raises(StructuredRoleResultError, match="role_result_schema_invalid"):
        await role.consider(await _request(purpose="media_selection", capability_manifest=manifest))


@pytest.mark.asyncio
async def test_bare_decision_payload_without_attended_sources_stays_invalid() -> None:
    manifest = _manifest("media-token:1", "media-token:2")
    model = _RequiredToolQueueModel(
        json.dumps(
            {
                "status": "decision",
                "summary": "A choice without an authored source binding.",
                "attended_source_refs": [],
                "decision": {"decision": "select", "selected_token": "media-token:2"},
                "recall_query": None,
                "proposals": [],
            }
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    with pytest.raises(StructuredRoleResultError, match="role_result_schema_invalid"):
        await role.consider(await _request(purpose="media_selection", capability_manifest=manifest))


@pytest.mark.asyncio
async def test_typed_proposal_duplicate_decision_echo_is_not_a_second_choice() -> None:
    manifest = _manifest("appraisal:h1", kind="private_impression_reflection")
    model = _RequiredToolQueueModel(
        _result(
            status="transition",
            decision=None,
        )
    )
    raw = json.loads(model.responses[0])
    raw["decision"] = "retain"
    raw["proposals"] = [
        {
            "proposal_type": "private_impression_transition",
            "decision": "retain",
            "predecessor_refs": [],
            "source_refs": ["appraisal:h1"],
            "reflection_summary": "A tentative impression remains open.",
            "confidence_bp": 5000,
            "expiry_condition": "one_month_without_support",
        }
    ]
    model.responses[0] = json.dumps(raw)
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    result = await role.experience(
        await _request(
            phase="experience",
            purpose="private_impression_reflection",
            capability_manifest=manifest,
        )
    )

    assert result["decision"] is None
    assert result["proposals"][0]["payload"]["decision"] == "retain"


@pytest.mark.asyncio
async def test_typed_proposal_in_generic_slot_keeps_its_authored_semantics() -> None:
    manifest = _manifest("appraisal:h1", kind="private_impression_reflection")
    proposal = {
        "proposal_type": "private_impression_transition",
        "decision": "retain",
        "predecessor_refs": [],
        "source_refs": ["appraisal:h1"],
        "reflection_summary": "A tentative impression remains open.",
        "confidence_bp": 5000,
        "expiry_condition": "one_month_without_support",
    }
    model = _RequiredToolQueueModel(
        json.dumps(
            {
                "status": "decision",
                "summary": "A tentative impression remains open.",
                "attended_source_refs": ["source:private_self"],
                "decision": proposal,
                "recall_query": None,
                "proposals": [],
            }
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    result = await role.experience(
        await _request(
            phase="experience",
            purpose="private_impression_reflection",
            capability_manifest=manifest,
        )
    )

    assert result["status"] == "transition"
    assert result["decision"] is None
    assert result["proposals"][0]["payload"]["decision"] == "retain"


@pytest.mark.asyncio
async def test_life_choice_nested_payload_is_wrapped_without_changing_authored_choice() -> None:
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"decision": "no_op"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    result = await role.consider(
        await _request(
            purpose="life_development_choice",
            capability_manifest=_life_development_manifest(),
        )
    )

    assert result["decision"]["payload"]["completion"]["decision"] == "no_op"


@pytest.mark.asyncio
async def test_life_choice_uses_one_versioned_forced_tool_and_preserves_accept_choice() -> None:
    manifest = _life_development_manifest()
    completion = {
        "decision": "accept",
        "intention_summary": "我想和朋友一起去看露天电影。",
        "importance_bp": 5200,
        "opens_at": (_NOW + timedelta(hours=2, minutes=30)).isoformat(),
        "closes_at": (_NOW + timedelta(hours=3, minutes=30)).isoformat(),
        "participant_refs": ["npc:friend"],
        "crystallized_aspiration_source_ref": "aspiration:travel",
    }
    model = _RequiredToolQueueModel(_life_choice_result(completion))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")

    result = await role.consider(
        await _request(
            purpose="life_development_choice",
            capability_manifest=manifest,
        )
    )

    assert result["decision"]["payload"]["completion"]["decision"] == "accept"
    assert result["decision"]["payload"]["completion"]["participant_refs"] == [
        "npc:friend"
    ]
    assert model.tool_calls[0][1] == {
        "type": "function",
        "function": {"name": "character_role_life_development_choice_v1"},
    }


def test_life_choice_tool_schema_binds_participants_and_aspiration_sources() -> None:
    manifest = _life_development_manifest()
    contract = StructuredRoleToolContracts().life_development_choice(
        capability_payload=manifest.payload,
        source_refs=manifest.source_refs,
        recall_allowed=False,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    Draft202012Validator.check_schema(parameters)

    decision_branch = parameters["anyOf"][0]
    payload = decision_branch["properties"]["decision"]["properties"]["payload"]
    completion = payload["properties"]["completion"]
    accept = next(
        branch
        for branch in completion["anyOf"]
        if branch["properties"]["decision"].get("const") == "accept"
    )
    assert accept["properties"]["participant_refs"]["items"]["enum"] == [
        "npc:friend"
    ]
    assert accept["properties"]["participant_refs"]["uniqueItems"] is True
    assert accept["properties"]["crystallized_aspiration_source_ref"]["anyOf"][0][
        "enum"
    ] == ["aspiration:travel"]

    validator = Draft202012Validator(parameters)
    valid = {
        "status": "decision",
        "summary": "I want to take this opportunity.",
        "attended_source_refs": ["source:private_self"],
        "decision": {
            "source_refs": ["source:private_self"],
            "payload": {
                "completion": {
                    "decision": "accept",
                    "intention_summary": "和朋友去看电影。",
                    "importance_bp": 5000,
                    "participant_refs": ["npc:friend"],
                    "crystallized_aspiration_source_ref": "aspiration:travel",
                }
            },
        },
        "recall_query": None,
        "proposals": [],
    }
    assert list(validator.iter_errors(valid)) == []
    invalid = json.loads(json.dumps(valid))
    invalid["decision"]["payload"]["completion"]["participant_refs"] = [
        "npc:not-offered"
    ]
    with pytest.raises(ValidationError):
        validator.validate(invalid)
    half_timing = json.loads(json.dumps(valid))
    half_timing["decision"]["payload"]["completion"]["opens_at"] = (
        _NOW + timedelta(hours=2)
    ).isoformat()
    with pytest.raises(ValidationError):
        validator.validate(half_timing)

    empty_participants = dict(manifest.payload)
    empty_participants["external_opportunity"] = dict(
        manifest.payload["external_opportunity"]
    )
    empty_participants["external_opportunity"]["entity_refs"] = []
    empty_contract = StructuredRoleToolContracts().life_development_choice(
        capability_payload=empty_participants,
        source_refs=manifest.source_refs,
        recall_allowed=False,
    )
    empty_parameters = empty_contract.provider_tools[0]["function"]["parameters"]
    Draft202012Validator.check_schema(empty_parameters)
    empty_decision_branch = empty_parameters["anyOf"][0]
    empty_payload = empty_decision_branch["properties"]["decision"]["properties"]["payload"]
    empty_accept = next(
        branch
        for branch in empty_payload["properties"]["completion"]["anyOf"]
        if branch["properties"]["decision"].get("const") == "accept"
    )
    assert empty_accept["properties"]["participant_refs"]["maxItems"] == 0


@pytest.mark.asyncio
async def test_life_choice_required_tool_reaches_deepseek_http_boundary() -> None:
    captured: dict[str, object] = {}
    raw_result = _life_choice_result({"decision": "no_op"})

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
                                        "name": "character_role_life_development_choice_v1",
                                        "arguments": raw_result,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await StructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-v4-flash",
        ).consider(
            await _request(
                purpose="life_development_choice",
                capability_manifest=_life_development_manifest(),
            )
        )
    finally:
        await model.aclose()

    assert result["decision"]["payload"]["completion"]["decision"] == "no_op"
    assert "response_format" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "character_role_life_development_choice_v1"},
    }
    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_life_development_choice_v1"


@pytest.mark.asyncio
async def test_life_choice_without_required_tool_support_fails_closed() -> None:
    model = _QueueModel(_life_choice_result({"decision": "no_op"}))

    with pytest.raises(StructuredRoleResultError) as raised:
        await StructuredCharacterRoleFaculty(
            model=model,
            model_id="plain-json-only",
        ).consider(
            await _request(
                purpose="life_development_choice",
                capability_manifest=_life_development_manifest(),
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


@pytest.mark.asyncio
async def test_correction_prompt_includes_exact_wire_failure_detail() -> None:
    model = _QueueModel(_result(status="silent"))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    await role.consider(
        await _request(
            correction_ordinal=1,
            correction_failure_code="role_result_schema_invalid",
            correction_failure_detail=(
                "private impression predecessors must also be selected sources"
            ),
        )
    )

    correction = json.loads(model.calls[0][0][1]["content"])["correction"]
    assert correction["failure_detail"].startswith(
        "private impression predecessors must also be selected sources"
    )


@pytest.mark.asyncio
async def test_outcome_selection_is_one_capability_bound_interior_decision() -> None:
    manifest = _manifest(
        "candidate:quiet-afternoon",
        "candidate:unexpected-invitation",
        kind="outcome_selection",
    )
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "selected_token": "candidate:unexpected-invitation",
                    "character_life_direction": None,
                },
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    result = await role.consider(
        await _request(purpose="outcome_selection", capability_manifest=manifest)
    )

    assert result["decision"] == {
        "contract": "character-interior-purpose-decision.1",
        "purpose": "outcome_selection",
        "source_refs": ["source:private_self"],
        "capability_ref": manifest.capability_ref,
        "capability_payload_hash": manifest.payload_hash,
        "payload": {
            "contract": "character-interior-outcome-selection-decision.1",
            "selected_token": "candidate:unexpected-invitation",
            "character_life_direction": None,
        },
    }


@pytest.mark.asyncio
async def test_outcome_selection_rejects_a_candidate_outside_the_manifest() -> None:
    role = StructuredCharacterRoleFaculty(
        model=_RequiredToolQueueModel(
            _result(
                status="decision",
                decision={
                    "source_refs": ["source:private_self"],
                    "payload": {
                        "selected_token": "candidate:not-offered",
                        "character_life_direction": None,
                    },
                },
            )
        ),
        model_id="deepseek-chat-v4",
    )

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="outcome_selection",
                capability_manifest=_manifest(
                    "candidate:offered",
                    kind="outcome_selection",
                ),
            )
        )

    assert raised.value.code == "selected_token_not_offered"


@pytest.mark.asyncio
async def test_outcome_selection_uses_one_versioned_forced_tool() -> None:
    manifest = _manifest(
        "candidate:quiet-afternoon",
        "candidate:unexpected-invitation",
        kind="outcome_selection",
    )
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "selected_token": "candidate:unexpected-invitation",
                    "character_life_direction": None,
                },
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")

    result = await role.consider(
        await _request(purpose="outcome_selection", capability_manifest=manifest)
    )

    assert result["decision"]["payload"]["selected_token"] == (
        "candidate:unexpected-invitation"
    )
    assert len(model.tool_calls) == 1
    tools, tool_choice = model.tool_calls[0]
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_role_outcome_selection_v1"},
    }
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_outcome_selection_v1"


def test_outcome_selection_tool_schema_closes_offered_tokens_and_direction() -> None:
    contract = StructuredRoleToolContracts().outcome_selection(
        capability_payload=_manifest(
            "candidate:quiet-afternoon",
            "candidate:unexpected-invitation",
            kind="outcome_selection",
        ).payload,
        source_refs=("source:private_self",),
        recall_allowed=True,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    Draft202012Validator.check_schema(parameters)
    decision = next(
        branch
        for branch in parameters["anyOf"]
        if branch["properties"]["status"]["enum"] == ["decision"]
    )
    payload = decision["properties"]["decision"]["properties"]["payload"]
    assert payload["properties"]["selected_token"]["enum"] == [
        "candidate:quiet-afternoon",
        "candidate:unexpected-invitation",
    ]
    decision_source_refs = decision["properties"]["decision"]["properties"]["source_refs"]
    assert decision_source_refs["minItems"] == 1
    assert decision_source_refs["maxItems"] == 1
    assert decision_source_refs["items"]["enum"] == ["source:private_self"]
    assert decision_source_refs["prefixItems"] == [{"const": "source:private_self"}]
    assert payload["properties"]["character_life_direction"] == {"type": "null"}
    assert set(parameters["anyOf"][1]["properties"]["status"]["enum"]) == {
        "recall_request"
    }

    direction_contract = StructuredRoleToolContracts().outcome_selection(
        capability_payload={
            **_manifest(
                "candidate:quiet-afternoon",
                "candidate:unexpected-invitation",
                kind="outcome_selection",
            ).payload,
            "allow_character_life_direction": True,
        },
        source_refs=("source:private_self",),
        recall_allowed=False,
    )
    direction_parameters = direction_contract.provider_tools[0]["function"]["parameters"]
    Draft202012Validator.check_schema(direction_parameters)
    direction_branch = direction_parameters["anyOf"][0]
    direction_payload = direction_branch["properties"]["decision"]["properties"]["payload"]
    assert direction_payload["properties"]["character_life_direction"].get("anyOf")
    assert len(direction_parameters["anyOf"]) == 1
    Draft202012Validator(direction_parameters).validate(
        {
            "status": "decision",
            "summary": "A possibility feels worth carrying.",
            "attended_source_refs": [],
            "decision": {
                "source_refs": ["source:private_self"],
                "payload": {
                    "selected_token": "candidate:unexpected-invitation",
                    "character_life_direction": {
                        "coordinate_ref": "biography:direction.new-project",
                        "summary": "keep making room for work that feels like mine",
                        "context_tags": ["direction.experiment"],
                        "replaces_context_tag_prefixes": ["direction.old-plan"],
                        "privacy_class": "personal",
                    },
                },
            },
            "recall_query": None,
            "proposals": [],
        }
    )


@pytest.mark.asyncio
async def test_outcome_selection_without_required_tool_support_fails_closed() -> None:
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "selected_token": "candidate:offered",
                    "character_life_direction": None,
                },
            },
        )
    )

    with pytest.raises(StructuredRoleResultError) as raised:
        await StructuredCharacterRoleFaculty(
            model=model,
            model_id="plain-json-only",
        ).consider(
            await _request(
                purpose="outcome_selection",
                capability_manifest=_manifest(
                    "candidate:offered",
                    kind="outcome_selection",
                ),
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


@pytest.mark.asyncio
async def test_outcome_selection_required_tool_reaches_deepseek_http_boundary() -> None:
    captured: dict[str, object] = {}
    raw_result = _result(
        status="decision",
        decision={
            "source_refs": ["source:private_self"],
            "payload": {
                "selected_token": "candidate:unexpected-invitation",
                "character_life_direction": None,
            },
        },
    )

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
                                        "name": "character_role_outcome_selection_v1",
                                        "arguments": raw_result,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await StructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-v4-flash",
        ).consider(
            await _request(
                purpose="outcome_selection",
                capability_manifest=_manifest(
                    "candidate:quiet-afternoon",
                    "candidate:unexpected-invitation",
                    kind="outcome_selection",
                ),
            )
        )
    finally:
        await model.aclose()

    assert result["decision"]["payload"]["selected_token"] == (
        "candidate:unexpected-invitation"
    )
    assert "response_format" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "character_role_outcome_selection_v1"},
    }
    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_outcome_selection_v1"


def _activity_lifecycle_result(
    *,
    decision: str = "select",
    selected_token: str | None = "opening:second",
) -> str:
    payload: dict[str, object] = {"decision": decision}
    if decision == "select":
        payload["selected_token"] = selected_token
    return _result(
        status="decision",
        decision={
            "source_refs": ["source:private_self"],
            "payload": payload,
        },
    )


@pytest.mark.asyncio
async def test_activity_lifecycle_choice_is_role_owned_and_capability_bound() -> None:
    manifest = _manifest(
        "opening:first",
        "opening:second",
        kind="activity_lifecycle_choice",
    )
    model = _RequiredToolQueueModel(
        _activity_lifecycle_result(selected_token="opening:second")
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")

    result = await role.consider(
        await _request(
            purpose="activity_lifecycle_choice",
            capability_manifest=manifest,
        )
    )

    assert result["decision"] == {
        "contract": "character-interior-purpose-decision.1",
        "purpose": "activity_lifecycle_choice",
        "source_refs": ["source:private_self"],
        "capability_ref": manifest.capability_ref,
        "capability_payload_hash": manifest.payload_hash,
        "payload": {
            "contract": "character-interior-activity-lifecycle-choice.1",
            "decision": "select",
            "selected_token": "opening:second",
        },
    }
    assert model.tool_calls[0][1] == {
        "type": "function",
        "function": {"name": "character_role_activity_lifecycle_choice_v1"},
    }


@pytest.mark.asyncio
async def test_activity_lifecycle_choice_preserves_role_owned_no_op() -> None:
    manifest = _manifest("opening:first", kind="activity_lifecycle_choice")
    model = _RequiredToolQueueModel(_activity_lifecycle_result(decision="no_op"))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")

    result = await role.consider(
        await _request(
            purpose="activity_lifecycle_choice",
            capability_manifest=manifest,
        )
    )

    assert result["decision"]["payload"] == {
        "contract": "character-interior-activity-lifecycle-choice.1",
        "decision": "no_op",
    }


def test_activity_lifecycle_choice_tool_schema_closes_tokens_and_no_op_shape() -> None:
    contract = StructuredRoleToolContracts().activity_lifecycle_choice(
        capability_payload=_manifest(
            "opening:first",
            "opening:second",
            kind="activity_lifecycle_choice",
        ).payload,
        source_refs=("source:private_self",),
        recall_allowed=False,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    Draft202012Validator.check_schema(parameters)
    assert len(parameters["anyOf"]) == 1
    decision_branch = parameters["anyOf"][0]
    payload = decision_branch["properties"]["decision"]["properties"]["payload"]
    assert payload["properties"]["selected_token"]["anyOf"]
    select_branch = next(
        branch
        for branch in payload["anyOf"]
        if branch["properties"]["decision"]["enum"] == ["select"]
    )
    assert select_branch["properties"]["selected_token"]["enum"] == [
        "opening:first",
        "opening:second",
    ]
    Draft202012Validator(parameters).validate(
        {
            "status": "decision",
            "summary": "I am not taking this opening today.",
            "attended_source_refs": ["source:private_self"],
            "decision": {
                "source_refs": ["source:private_self"],
                "payload": {"decision": "no_op"},
            },
            "recall_query": None,
            "proposals": [],
        }
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(parameters).validate(
            {
                "status": "decision",
                "summary": "I choose an unavailable opening.",
                "attended_source_refs": [],
                "decision": {
                    "source_refs": ["source:private_self"],
                    "payload": {
                        "decision": "select",
                        "selected_token": "opening:not-offered",
                    },
                },
                "recall_query": None,
                "proposals": [],
            }
        )


@pytest.mark.asyncio
async def test_activity_lifecycle_choice_required_tool_reaches_deepseek_http_boundary() -> None:
    captured: dict[str, object] = {}
    raw_result = _activity_lifecycle_result(selected_token="opening:second")

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
                                        "name": "character_role_activity_lifecycle_choice_v1",
                                        "arguments": raw_result,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await StructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-v4-flash",
        ).consider(
            await _request(
                purpose="activity_lifecycle_choice",
                capability_manifest=_manifest(
                    "opening:first",
                    "opening:second",
                    kind="activity_lifecycle_choice",
                ),
            )
        )
    finally:
        await model.aclose()

    assert result["decision"]["payload"]["selected_token"] == "opening:second"
    assert "response_format" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "character_role_activity_lifecycle_choice_v1"},
    }
    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_activity_lifecycle_choice_v1"


@pytest.mark.asyncio
async def test_activity_lifecycle_choice_without_required_tool_support_fails_closed() -> None:
    model = _QueueModel(_activity_lifecycle_result())
    role = StructuredCharacterRoleFaculty(model=model, model_id="plain-json-only")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="activity_lifecycle_choice",
                capability_manifest=_manifest(
                    "opening:first",
                    kind="activity_lifecycle_choice",
                ),
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


@pytest.mark.asyncio
async def test_proactive_contact_is_one_capability_bound_interior_decision() -> None:
    manifest = _proactive_manifest()
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "timing_choice": "silent",
                    "cadence": "conversational",
                    "beats": [],
                    "stance": "keeping the thought private",
                    "brief_rationale": "she does not want to send it now",
                    "impulse_summary": "the conversation crossed her mind",
                    "confidence": 6400,
                    "world_claims": [],
                },
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    result = await role.consider(
        await _request(purpose="proactive_contact", capability_manifest=manifest)
    )

    assert result["decision"]["purpose"] == "proactive_contact"
    assert result["decision"]["payload"]["contract"] == (
        "character-interior-proactive-contact-decision.1"
    )
    assert result["decision"]["payload"]["timing_choice"] == "silent"


@pytest.mark.asyncio
async def test_world_stimulus_appraisal_uses_one_versioned_forced_tool() -> None:
    model = _RequiredToolQueueModel(_world_stimulus_no_change_result())
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")

    result = await role.experience(
        await _request(
            phase="experience",
            purpose="world_stimulus_appraisal",
            capability_manifest=_world_stimulus_manifest(),
        )
    )

    assert result["status"] == "no_change"
    assert result["proposals"][0]["proposal_type"] == "world_stimulus_appraisal_result"
    assert len(model.tool_calls) == 1
    tools, tool_choice = model.tool_calls[0]
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_role_world_stimulus_appraisal_v1"},
    }
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_world_stimulus_appraisal_v1"


@pytest.mark.asyncio
async def test_private_impression_reflection_uses_one_versioned_forced_tool() -> None:
    model = _RequiredToolQueueModel(_private_impression_result(status="transition"))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")

    result = await role.experience(
        await _request(
            phase="experience",
            purpose="private_impression_reflection",
            capability_manifest=_private_impression_manifest(),
        )
    )

    assert result["status"] == "transition"
    assert result["proposals"][0]["payload"]["source_refs"] == ["s0"]
    assert len(model.tool_calls) == 1
    tools, tool_choice = model.tool_calls[0]
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_role_private_impression_reflection_v1"},
    }
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_private_impression_reflection_v1"


@pytest.mark.asyncio
async def test_private_impression_provider_view_uses_tokens_not_authority_refs() -> None:
    secret_source = "private-impression:secret-compiled-source"
    secret_event = "event:secret-private-impression"
    payload = {
        "contract": "character-interior-private-impression-capability.1",
        "reflection_capsule": {
            "sources": [{"source_ref": secret_source, "authority_event_ref": secret_event}]
        },
        "reflection_sources": [
            {
                "source_ref": secret_source,
                "authority_event_ref": secret_event,
                "source_kind": "existing_impression",
                "short_token": "s0",
                "value_json": json.dumps(
                    {
                        "impression_id": "impression:secret",
                        "reflection_summary": "A tentative meaning remains open.",
                        "confidence_bp": 5100,
                    },
                    ensure_ascii=False,
                ),
            }
        ],
        "short_tokens": ["s0"],
        "existing_impression_short_tokens": ["s0"],
        "anchor_short_tokens": ["s0"],
        "token_map": {"s0": secret_source},
        "anchor_source_refs": [secret_source],
        "expiry_conditions": ["until_counter_evidence"],
    }
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    manifest = _InteriorCapabilityManifest(
        capability_ref="capability:private-impression:provider-view",
        capability_kind="private_impression_reflection",
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=("source:private_self",),
    )
    model = _RequiredToolQueueModel(_private_impression_result(status="transition"))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")

    await role.experience(
        await _request(
            phase="experience",
            purpose="private_impression_reflection",
            capability_manifest=manifest,
        )
    )

    messages = model.calls[0][0]
    provider_user_content = messages[1]["content"]
    assert secret_source not in provider_user_content
    assert secret_event not in provider_user_content
    assert "impression:secret" not in provider_user_content
    assert '"short_token":"s0"' in provider_user_content
    assert "A tentative meaning remains open." in provider_user_content
    assert '"token_map"' not in provider_user_content


def test_private_impression_tool_schema_preserves_no_change_tokens_and_expiry() -> None:
    contract = StructuredRoleToolContracts().private_impression_reflection(
        capability_payload=_private_impression_manifest().payload,
        recall_allowed=True,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    Draft202012Validator.check_schema(parameters)

    statuses = {
        status
        for branch in parameters["anyOf"]
        for status in branch["properties"]["status"]["enum"]
    }
    assert {"transition", "no_change", "recall_request"} <= statuses
    transition = next(
        branch
        for branch in parameters["anyOf"]
        if branch["properties"]["status"]["enum"] == ["transition"]
    )
    proposal = transition["properties"]["proposals"]["items"]
    assert proposal["properties"]["source_refs"]["items"]["enum"] == ["s0", "s1", "s2"]
    assert proposal["properties"]["predecessor_refs"]["items"]["enum"] == ["s2"]
    assert proposal["properties"]["expiry_condition"]["enum"] == [
        "until_appraisal_contradicted",
        "until_counter_evidence",
        "until_relationship_stage_changes",
        "one_month_without_support",
    ]
    no_change = next(
        branch
        for branch in parameters["anyOf"]
        if branch["properties"]["status"]["enum"] == ["no_change"]
    )
    assert no_change["properties"]["proposals"]["maxItems"] == 0


def test_private_impression_tool_schema_rejects_nonexistent_predecessor_token() -> None:
    contract = StructuredRoleToolContracts().private_impression_reflection(
        capability_payload=_private_impression_manifest().payload,
        recall_allowed=False,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    invalid = json.loads(_private_impression_result(status="transition"))
    invalid["proposals"][0].update(
        {
            "decision": "consolidate",
            "predecessor_refs": ["s1"],
            "source_refs": ["s0", "s1"],
        }
    )
    errors = list(Draft202012Validator(parameters).iter_errors(invalid))
    assert errors


@pytest.mark.parametrize("decision", ("consolidate", "supersede"))
def test_private_impression_tool_schema_requires_a_predecessor_for_replacement(
    decision: str,
) -> None:
    contract = StructuredRoleToolContracts().private_impression_reflection(
        capability_payload=_private_impression_manifest().payload,
        recall_allowed=False,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    invalid = json.loads(_private_impression_result(status="transition"))
    invalid["proposals"][0].update(
        {
            "decision": decision,
            "predecessor_refs": [],
            "source_refs": ["s0"],
        }
    )

    assert list(Draft202012Validator(parameters).iter_errors(invalid))


@pytest.mark.asyncio
async def test_private_impression_required_tool_reaches_deepseek_http_boundary() -> None:
    captured: dict[str, object] = {}
    raw_result = _private_impression_result()

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
                                        "name": "character_role_private_impression_reflection_v1",
                                        "arguments": raw_result,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await StructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-v4-flash",
        ).experience(
            await _request(
                phase="experience",
                purpose="private_impression_reflection",
                capability_manifest=_private_impression_manifest(),
            )
        )
    finally:
        await model.aclose()

    assert result["status"] == "no_change"
    assert "response_format" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "character_role_private_impression_reflection_v1"},
    }
    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_private_impression_reflection_v1"


@pytest.mark.asyncio
async def test_world_stimulus_required_tool_reaches_deepseek_http_boundary() -> None:
    captured: dict[str, object] = {}
    raw_result = _world_stimulus_no_change_result()

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
                                        "name": "character_role_world_stimulus_appraisal_v1",
                                        "arguments": raw_result,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await StructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-v4-flash",
        ).experience(
            await _request(
                phase="experience",
                purpose="world_stimulus_appraisal",
                capability_manifest=_world_stimulus_manifest(),
            )
        )
    finally:
        await model.aclose()

    assert result["status"] == "no_change"
    assert "response_format" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "character_role_world_stimulus_appraisal_v1"},
    }
    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "character_role_world_stimulus_appraisal_v1"


def test_world_stimulus_tool_schema_keeps_no_change_and_transition_open() -> None:
    compiler = StructuredRoleToolContracts()
    contract = compiler.world_stimulus_appraisal(
        capability_payload=_world_stimulus_manifest().payload,
        recall_allowed=True,
    )
    function = contract.provider_tools[0]["function"]
    parameters = function["parameters"]
    Draft202012Validator.check_schema(parameters)

    assert parameters["type"] == "object"
    branches = parameters["anyOf"]
    statuses = {
        status
        for branch in branches
        for status in branch["properties"]["status"]["enum"]
    }
    assert {"no_change", "transition", "recall_request"} <= statuses
    proposal_items = next(
        branch["properties"]["proposals"]["items"]
        for branch in branches
        if branch["properties"]["status"]["enum"] == ["no_change"]
    )
    assert proposal_items["properties"]["proposal_type"]["const"] == (
        "world_stimulus_appraisal_result"
    )


@pytest.mark.asyncio
async def test_proactive_contact_uses_one_versioned_forced_tool_at_http_boundary() -> None:
    captured: dict[str, object] = {}
    raw_result = _result(
        status="decision",
        decision={
            "source_refs": ["source:private_self"],
            "payload": {
                "timing_choice": "silent",
                "cadence": "conversational",
                "beats": [],
                "stance": "keeping the thought private",
                "brief_rationale": "she does not want to send it now",
                "impulse_summary": "the conversation crossed her mind",
                "confidence": 6400,
                "world_claims": [],
            },
        },
    )

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
                                        "name": "character_role_proactive_contact_v1",
                                        "arguments": raw_result,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await StructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-v4-flash",
        ).consider(
            await _request(
                purpose="proactive_contact",
                capability_manifest=_proactive_manifest(),
            )
        )
    finally:
        await model.aclose()

    assert result["decision"]["payload"]["timing_choice"] == "silent"
    assert "response_format" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "character_role_proactive_contact_v1"},
    }
    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    function = tools[0]["function"]
    assert function["name"] == "character_role_proactive_contact_v1"
    branches = function["parameters"]["anyOf"]
    decision = branches[0]
    assert decision["properties"]["status"]["enum"] == ["decision"]
    assert {
        "status",
        "summary",
        "attended_source_refs",
        "decision",
        "recall_query",
        "proposals",
    } <= set(decision["required"])
    payload = decision["properties"]["decision"]["properties"]["payload"]
    assert {
        "timing_choice",
        "cadence",
        "beats",
        "stance",
        "brief_rationale",
        "impulse_summary",
        "confidence",
        "world_claims",
    } <= set(payload["required"])
    assert len(branches) == 2
    assert branches[1]["properties"]["status"]["enum"] == ["recall_request"]
    assert decision["properties"]["proposals"]["maxItems"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        _valid_proactive_payload(beats=[]),
        _valid_proactive_payload(delay_seconds=30),
        _valid_proactive_payload(expires_after_seconds=90),
        _valid_proactive_payload(turn_posture="yield"),
        _valid_proactive_payload(
            beats=[
                {"modality": "text", "text": "先说一句。"},
                {"modality": "typing"},
            ]
        ),
        _valid_proactive_payload(
            timing_choice="later",
            turn_posture="interject",
            delay_seconds=30,
            expires_after_seconds=90,
        ),
        _valid_proactive_payload(
            timing_choice="later",
            beats=[],
            delay_seconds=30,
            expires_after_seconds=90,
        ),
        _valid_proactive_payload(
            timing_choice="later",
            delay_seconds=None,
            expires_after_seconds=None,
        ),
        _valid_proactive_payload(
            timing_choice="later",
            beats=[{"modality": "reaction", "reaction_id": "face:178"}],
            delay_seconds=30,
            expires_after_seconds=90,
        ),
        _valid_proactive_payload(impulse_summary=None),
        _valid_proactive_payload(
            beats=[{"modality": "text", "reaction_id": "heart"}],
        ),
        _valid_proactive_payload(
            world_claims=[
                {
                    "claim_text": "I am sorting photos now.",
                    "scope": "current_world",
                    "source_refs": [],
                }
            ],
        ),
        _valid_proactive_payload(
            timing_choice="silent",
            beats=[],
            delay_seconds=30,
            expires_after_seconds=90,
        ),
        _valid_proactive_payload(
            timing_choice="silent",
            beats=[],
            response_expectation={
                "hoped_response": "reply",
                "pressure_bp": 1,
                "importance_bp": 1,
                "wait_seconds": 30,
                "expires_after_seconds": 60,
            },
        ),
        _valid_proactive_payload(
            timing_choice="silent",
            turn_posture="interject",
            beats=[],
        ),
    ],
    ids=(
        "now-empty",
        "now-delay",
        "now-expiry",
        "now-yield",
        "terminal-typing",
        "later-interject",
        "later-empty",
        "later-null-due-window",
        "later-non-text",
        "null-impulse",
        "text-with-reaction-payload",
        "grounded-claim-without-sources",
        "silent-due-window",
        "silent-response-expectation",
        "silent-interject",
    ),
)
def test_proactive_provider_schema_rejects_host_invalid_timing_shapes(
    payload: dict[str, object],
) -> None:
    validator = Draft202012Validator(_proactive_provider_payload_schema())

    assert list(validator.iter_errors(payload))


@pytest.mark.parametrize(
    "payload",
    [
        _valid_proactive_payload(),
        _valid_proactive_payload(
            turn_posture="interject",
            beats=[
                {"modality": "typing"},
                {"modality": "reaction", "reaction_id": "heart"},
                {"modality": "text", "text": "刚刚忽然想到你。"},
            ],
        ),
        _valid_proactive_payload(
            timing_choice="later",
            turn_posture="yield",
            beats=[
                {"modality": "text", "text": "晚一点想跟你说件事。"},
                {"modality": "text", "text": "等我整理好思绪。"},
            ],
            delay_seconds=60,
            expires_after_seconds=600,
        ),
        _valid_proactive_payload(
            timing_choice="silent",
            turn_posture="yield",
            beats=[],
        ),
    ],
    ids=("now", "now-multibeat", "later", "silent"),
)
def test_proactive_provider_schema_preserves_all_legal_timing_coordinates(
    payload: dict[str, object],
) -> None:
    validator = Draft202012Validator(_proactive_provider_payload_schema())

    assert list(validator.iter_errors(payload)) == []


def test_proactive_provider_schema_documents_relative_expiry_dialect_gap() -> None:
    from companion_daemon.world_v2.proactive_action import ProactiveDraft

    payload = _valid_proactive_payload(
        timing_choice="later",
        delay_seconds=90,
        expires_after_seconds=60,
    )

    # Standard JSON Schema cannot compare two sibling numeric values.  The
    # provider receives the rule in the function description, while the same
    # canonical ProactiveDraft remains the fail-closed acceptance authority.
    assert (
        list(Draft202012Validator(_proactive_provider_payload_schema()).iter_errors(payload)) == []
    )
    with pytest.raises(ValueError, match="expiry must follow"):
        ProactiveDraft.model_validate_json(json.dumps(payload))


def test_proactive_tool_contract_hot_compile_is_cached_below_one_millisecond() -> None:
    compiler = StructuredRoleToolContracts()
    manifest = _proactive_manifest()
    expected = compiler.proactive_contact(
        capability_payload=manifest.payload,
        recall_allowed=True,
    )

    started = perf_counter_ns()
    observed = [
        compiler.proactive_contact(
            capability_payload=manifest.payload,
            recall_allowed=True,
        )
        for _ in range(100)
    ]
    elapsed_per_call_ns = (perf_counter_ns() - started) / len(observed)

    assert all(item is expected for item in observed)
    assert elapsed_per_call_ns < 1_000_000


def test_proactive_tool_contract_first_capability_specialization_stays_outside_ttft_budget() -> (
    None
):
    StructuredRoleToolContracts.precompile()
    manifest = _proactive_manifest()
    unique_profile = dict(manifest.payload["expression_capabilities"])
    unique_profile["profile_id"] = "expression:cold-specialization-test.1"

    started = perf_counter_ns()
    contract = StructuredRoleToolContracts().proactive_contact(
        capability_payload={"expression_capabilities": unique_profile},
        recall_allowed=False,
    )
    elapsed_ns = perf_counter_ns() - started

    assert contract.identity.capabilities_sha256.startswith("sha256:")
    assert elapsed_ns < 30_000_000


@pytest.mark.asyncio
async def test_proactive_contact_without_required_tool_support_fails_closed() -> None:
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "timing_choice": "silent",
                    "cadence": "conversational",
                    "beats": [],
                    "stance": "private",
                    "brief_rationale": "not sending",
                    "impulse_summary": "thought of them",
                    "confidence": 5000,
                    "world_claims": [],
                },
            },
        )
    )

    with pytest.raises(StructuredRoleResultError) as raised:
        await StructuredCharacterRoleFaculty(
            model=model,
            model_id="plain-json-only",
        ).consider(
            await _request(
                purpose="proactive_contact",
                capability_manifest=_proactive_manifest(),
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


@pytest.mark.asyncio
async def test_world_stimulus_without_required_tool_support_fails_closed() -> None:
    model = _QueueModel(_world_stimulus_no_change_result())

    with pytest.raises(StructuredRoleResultError) as raised:
        await StructuredCharacterRoleFaculty(
            model=model,
            model_id="plain-json-only",
        ).experience(
            await _request(
                phase="experience",
                purpose="world_stimulus_appraisal",
                capability_manifest=_world_stimulus_manifest(),
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


@pytest.mark.asyncio
async def test_proactive_required_tool_failure_never_retries_as_plain_json() -> None:
    model = _FailingRequiredToolModel()

    with pytest.raises(TypeError, match="required-tool transport failed"):
        await StructuredCharacterRoleFaculty(
            model=model,
            model_id=model.model,
        ).consider(
            await _request(
                purpose="proactive_contact",
                capability_manifest=_proactive_manifest(),
            )
        )

    assert model.forced_calls == 1
    assert model.plain_calls == 0


@pytest.mark.asyncio
async def test_proactive_forced_tool_preserves_selective_recall_choice() -> None:
    model = _RequiredToolQueueModel(
        json.dumps(
            {
                "status": "recall_request",
                "summary": "A memory may matter before deciding whether to reach out.",
                "attended_source_refs": ["source:private_self"],
                "decision": None,
                "recall_query": "the last unfinished conversation",
                "proposals": [],
            }
        )
    )

    result = await StructuredCharacterRoleFaculty(
        model=model,
        model_id="deepseek-v4-flash",
    ).consider(
        await _request(
            purpose="proactive_contact",
            capability_manifest=_proactive_manifest(),
        )
    )

    assert result["status"] == "recall_request"
    assert result["recall_query"] == "the last unfinished conversation"
    assert result["decision"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "timing_choice": "silent",
            "cadence": "hesitant",
            "beats": [],
            "stance": "keeping this private",
            "brief_rationale": "I do not want to send it now.",
            "impulse_summary": "The counterpart crossed my mind.",
            "confidence": 6100,
            "world_claims": [],
        },
        {
            "timing_choice": "now",
            "cadence": "rapid",
            "beats": [{"modality": "text", "text": "刚刚忽然想到你。"}],
            "stance": "openly warm",
            "brief_rationale": "I want to say this now.",
            "impulse_summary": "A present fact made me want contact.",
            "confidence": 7200,
            "world_claims": [
                {
                    "claim_text": "I am sorting photos now.",
                    "scope": "current_world",
                    "source_refs": ["source:private_self"],
                }
            ],
        },
        {
            "timing_choice": "later",
            "cadence": "conversational",
            "beats": [
                {"modality": "text", "text": "晚一点想跟你说件事。"},
                {"modality": "text", "text": "等我整理好思绪。"},
            ],
            "delay_seconds": 60,
            "expires_after_seconds": 600,
            "stance": "deliberate",
            "brief_rationale": "I want to wait, then send both beats.",
            "impulse_summary": "The thought matters but not immediately.",
            "confidence": 6800,
            "world_claims": [],
        },
    ],
)
async def test_proactive_forced_tool_preserves_every_role_owned_choice(
    payload: dict[str, object],
) -> None:
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": payload,
            },
        )
    )

    result = await StructuredCharacterRoleFaculty(
        model=model,
        model_id="deepseek-v4-flash",
    ).consider(
        await _request(
            purpose="proactive_contact",
            capability_manifest=_proactive_manifest(),
        )
    )

    assert result["summary"] == "private decision"
    assert result["attended_source_refs"] == ("source:private_self",)
    assert result["decision"]["source_refs"] == ["source:private_self"]
    assert result["decision"]["payload"] == {
        "contract": "character-interior-proactive-contact-decision.1",
        **payload,
    }


@pytest.mark.asyncio
async def test_proactive_correction_uses_the_same_forced_tool_and_parent_identity() -> None:
    payload = {
        "timing_choice": "silent",
        "cadence": "conversational",
        "beats": [],
        "stance": "quiet",
        "brief_rationale": "I choose not to send this.",
        "impulse_summary": "A passing thought stayed private.",
        "confidence": 5000,
        "world_claims": [],
    }
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={"source_refs": ["source:private_self"], "payload": payload},
        ),
        _result(
            status="decision",
            decision={"source_refs": ["source:private_self"], "payload": payload},
        ),
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")
    manifest = _proactive_manifest()

    initial = await role.consider(
        await _request(purpose="proactive_contact", capability_manifest=manifest)
    )
    corrected = await role.consider(
        await _request(
            purpose="proactive_contact",
            capability_manifest=manifest,
            correction_ordinal=1,
            correction_failure_code="role_result_schema_invalid",
            correction_failure_detail="timing_choice was missing",
        )
    )

    assert len(model.tool_calls) == 2
    assert model.tool_calls[0] == model.tool_calls[1]
    assert corrected["author_lineage"]["attempt_ordinal"] == 1
    assert (
        corrected["author_lineage"]["parent_model_call_id"]
        == (initial["author_lineage"]["model_call_id"])
    )
    assert (
        corrected["author_lineage"]["request_hash"] != (initial["author_lineage"]["request_hash"])
    )


@pytest.mark.asyncio
async def test_proactive_request_audit_binds_exact_tool_schema_and_identity() -> None:
    manifest = _proactive_manifest()
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "timing_choice": "silent",
                    "cadence": "conversational",
                    "beats": [],
                    "stance": "private",
                    "brief_rationale": "I choose silence.",
                    "impulse_summary": "A passing thought.",
                    "confidence": 5000,
                    "world_claims": [],
                },
            },
        )
    )
    result = await StructuredCharacterRoleFaculty(
        model=model,
        model_id="deepseek-v4-flash",
    ).consider(await _request(purpose="proactive_contact", capability_manifest=manifest))
    contract = StructuredRoleToolContracts().proactive_contact(
        capability_payload=manifest.payload,
        recall_allowed=True,
    )
    request_identity = {
        "messages": model.calls[0][0],
        "tools": list(contract.provider_tools),
        "tool_choice": contract.provider_tool_choice,
        "tool_contract_identity": contract.identity.request_identity_material(),
    }
    expected_hash = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                request_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )

    assert result["author_lineage"]["request_hash"] == expected_hash
    identity = contract.identity.request_identity_material()
    assert identity["contract_id"] == "character-role-forced-tool"
    assert identity["purpose"] == "proactive_contact"
    assert identity["version"] == "1"
    assert identity["tool_name"] == "character_role_proactive_contact_v1"
    assert identity["schema_sha256"].startswith("sha256:")
    assert identity["contract_sha256"].startswith("sha256:")


@pytest.mark.asyncio
async def test_expression_reconsideration_requires_an_explicit_role_disposition() -> None:
    manifest = _manifest("continue", "cancel", kind="expression_reconsideration")
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"disposition": "cancel"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    result = await role.consider(
        await _request(
            purpose="expression_reconsideration",
            capability_manifest=manifest,
        )
    )

    assert result["decision"]["payload"] == {
        "contract": "character-interior-expression-reconsideration-decision.1",
        "disposition": "cancel",
    }
    assert len(model.tool_calls) == 1
    tools, tool_choice = model.tool_calls[0]
    assert tools[0]["function"]["name"] == "character_role_expression_reconsideration_v1"
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_role_expression_reconsideration_v1"},
    }
    parameters = tools[0]["function"]["parameters"]
    Draft202012Validator.check_schema(parameters)
    decision_branch = parameters["anyOf"][0]
    payload_schema = decision_branch["properties"]["decision"]["properties"]["payload"]
    assert payload_schema["properties"]["disposition"]["enum"] == ["continue", "cancel"]
    Draft202012Validator(parameters).validate(
        json.loads(
            _result(
                status="decision",
                decision={
                    "source_refs": ["source:private_self"],
                    "payload": {"disposition": "cancel"},
                },
            )
        )
    )
    assert parameters["anyOf"][1]["properties"]["status"]["enum"] == ["recall_request"]


@pytest.mark.asyncio
async def test_expression_reconsideration_has_no_plain_provider_fallback() -> None:
    manifest = _manifest("continue", "cancel", kind="expression_reconsideration")
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"disposition": "cancel"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="expression_reconsideration",
                capability_manifest=manifest,
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


@pytest.mark.asyncio
async def test_expression_reconsideration_rejects_disposition_outside_capability() -> None:
    manifest = _manifest("continue", "cancel", kind="expression_reconsideration")
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"disposition": "merge"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="expression_reconsideration",
                capability_manifest=manifest,
            )
        )

    assert raised.value.code == "role_result_schema_invalid"
    assert len(model.tool_calls) == 1


@pytest.mark.asyncio
async def test_expression_reconsideration_rejects_malformed_capability_before_provider() -> None:
    manifest = _manifest("not-a-disposition", kind="expression_reconsideration")
    model = _RequiredToolQueueModel(_result(status="silent"))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="expression_reconsideration",
                capability_manifest=manifest,
            )
        )

    assert raised.value.code == "role_result_schema_invalid"
    assert model.calls == []


@pytest.mark.asyncio
async def test_fact_memory_retention_uses_one_versioned_forced_tool() -> None:
    manifest = _memory_retention_manifest()
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "retain": True,
                    "cue_kind": "relationship",
                    "retention_rationales": ["relationship_continuity"],
                    "salience": {
                        "autobiographical_relevance_bp": 6200,
                        "relationship_relevance_bp": 7800,
                        "emotional_residue_bp": 4200,
                        "unfinished_business_bp": 1200,
                        "recurrence_bp": 2400,
                        "novelty_bp": 3100,
                        "future_utility_bp": 6900,
                        "world_continuity_bp": 4800,
                    },
                },
            }
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.consider(
        await _request(
            purpose="fact_memory_retention",
            capability_manifest=manifest,
        )
    )

    assert result["decision"]["payload"]["retain"] is True
    assert len(model.tool_calls) == 1
    tools, tool_choice = model.tool_calls[0]
    assert tools[0]["function"]["name"] == "character_role_fact_memory_retention_v1"
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_role_fact_memory_retention_v1"},
    }


@pytest.mark.asyncio
async def test_memory_retention_without_required_tool_support_fails_closed() -> None:
    manifest = _memory_retention_manifest()
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"retain": False},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="fact_memory_retention",
                capability_manifest=manifest,
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


@pytest.mark.asyncio
async def test_experience_memory_retention_uses_its_own_versioned_tool() -> None:
    manifest = _memory_retention_manifest(kind="experience_memory_retention")
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"retain": False},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.consider(
        await _request(
            purpose="experience_memory_retention",
            capability_manifest=manifest,
        )
    )

    assert result["decision"]["payload"] == {
        "contract": "character-interior-experience-memory-retention.1",
        "retain": False,
    }
    tools, tool_choice = model.tool_calls[0]
    assert tools[0]["function"]["name"] == (
        "character_role_experience_memory_retention_v1"
    )
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_role_experience_memory_retention_v1"},
    }


def test_memory_retention_tool_schema_closes_no_change_and_retain_branches() -> None:
    manifest = _memory_retention_manifest()
    contract = StructuredRoleToolContracts().memory_retention(
        purpose="fact_memory_retention",
        capability_payload=manifest.payload,
        source_refs=manifest.source_refs,
        recall_allowed=True,
    )
    parameters = contract.provider_tools[0]["function"]["parameters"]
    Draft202012Validator.check_schema(parameters)
    validator = Draft202012Validator(parameters)
    valid_no_change = json.loads(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"retain": False},
            },
        )
    )
    validator.validate(valid_no_change)
    valid_retain = json.loads(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "retain": True,
                    "cue_kind": "future_utility",
                    "retention_rationales": ["future_utility"],
                    "salience": {
                        name: 1000
                        for name in (
                            "autobiographical_relevance_bp",
                            "relationship_relevance_bp",
                            "emotional_residue_bp",
                            "unfinished_business_bp",
                            "recurrence_bp",
                            "novelty_bp",
                            "future_utility_bp",
                            "world_continuity_bp",
                        )
                    },
                },
            },
        )
    )
    validator.validate(valid_retain)
    with pytest.raises(ValidationError):
        validator.validate(
            {
                **valid_no_change,
                "decision": {
                    **valid_no_change["decision"],
                    "payload": {"retain": True},
                },
            }
        )


@pytest.mark.asyncio
async def test_fact_memory_retention_required_tool_reaches_deepseek_http_boundary() -> None:
    captured: dict[str, object] = {}
    raw_result = _result(
        status="decision",
        decision={
            "source_refs": ["source:private_self"],
            "payload": {"retain": False},
        },
    )

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
                                        "name": "character_role_fact_memory_retention_v1",
                                        "arguments": raw_result,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await StructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-v4-flash",
        ).consider(
            await _request(
                purpose="fact_memory_retention",
                capability_manifest=_memory_retention_manifest(),
            )
        )
    finally:
        await model.aclose()

    assert result["decision"]["payload"]["retain"] is False
    assert "response_format" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "character_role_fact_memory_retention_v1"},
    }
    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_fact_memory_retention_v1"


@pytest.mark.asyncio
async def test_memory_withdrawal_review_uses_offered_disposition_tool() -> None:
    manifest = _manifest("retain", "forget", kind="memory_withdrawal_review")
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"selected_token": "forget"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.consider(
        await _request(
            purpose="memory_withdrawal_review",
            capability_manifest=manifest,
        )
    )

    assert result["decision"]["payload"] == {
        "contract": "character-interior-memory-withdrawal-review.1",
        "selected_token": "forget",
    }
    tools, tool_choice = model.tool_calls[0]
    assert tools[0]["function"]["name"] == (
        "character_role_memory_withdrawal_review_v1"
    )
    payload_schema = tools[0]["function"]["parameters"]["anyOf"][0]["properties"][
        "decision"
    ]["properties"]["payload"]
    assert payload_schema["properties"]["selected_token"]["enum"] == [
        "retain",
        "forget",
    ]
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_role_memory_withdrawal_review_v1"},
    }


@pytest.mark.asyncio
async def test_memory_withdrawal_review_without_required_tool_support_fails_closed() -> None:
    manifest = _manifest("retain", "forget", kind="memory_withdrawal_review")
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"selected_token": "forget"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="memory_withdrawal_review",
                capability_manifest=manifest,
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


@pytest.mark.asyncio
async def test_private_impression_experience_normalizes_one_exact_typed_proposal() -> None:
    manifest = _manifest("appraisal:h1", kind="private_impression_reflection")
    model = _RequiredToolQueueModel(
        json.dumps(
            {
                "status": "transition",
                "summary": "She formed a tentative private reading.",
                "attended_source_refs": ["source:private_self"],
                "decision": None,
                "recall_query": None,
                "proposals": [
                    {
                        "proposal_type": "private_impression_transition",
                        "decision": "retain",
                        "predecessor_refs": [],
                        "source_refs": ["appraisal:h1"],
                        "reflection_summary": "Maybe this mattered more than he said.",
                        "confidence_bp": 6100,
                        "expiry_condition": "until_counter_evidence",
                    }
                ],
            }
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")

    result = await role.experience(
        await _request(
            phase="experience",
            purpose="private_impression_reflection",
            capability_manifest=manifest,
        )
    )

    assert result["proposals"] == (
        {
            "contract": "character-interior-typed-proposal.1",
            "proposal_type": "private_impression_transition",
            "purpose": "private_impression_reflection",
            "source_refs": ["source:private_self"],
            "capability_ref": manifest.capability_ref,
            "capability_payload_hash": manifest.payload_hash,
            "payload": {
                "contract": "character-interior-private-impression-transition.1",
                "decision": "retain",
                "predecessor_refs": [],
                "source_refs": ["appraisal:h1"],
                "reflection_summary": "Maybe this mattered more than he said.",
                "confidence_bp": 6100,
                "expiry_condition": "until_counter_evidence",
            },
        },
    )


def test_structured_role_declares_every_builtin_capability_purpose_to_registry() -> None:
    role = StructuredCharacterRoleFaculty(
        model=_QueueModel(_result(status="silent")),
        model_id="deepseek-chat-v4",
    )
    interior = CharacterInterior(projection=_Projection(), role=role)

    registered = set(interior.runtime_health()["purpose_faculties"])

    assert {
        "media_selection",
        "external_perception_attention",
        "qq_attachment_perception",
        "proactive_contact",
        "expression_reconsideration",
        "private_impression_reflection",
        "outcome_selection",
        "life_development_choice",
    } <= registered


@pytest.mark.asyncio
async def test_media_no_op_is_an_explicit_character_decision_without_a_token() -> None:
    manifest = _manifest("media-token:1")
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"decision": "no_op"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.consider(
        await _request(purpose="media_selection", capability_manifest=manifest)
    )

    assert result["decision"]["payload"] == {
        "contract": "character-interior-media-selection-decision.1",
        "decision": "no_op",
    }


@pytest.mark.asyncio
async def test_media_selection_uses_required_tool_and_offered_candidate_enum() -> None:
    manifest = _manifest("media-token:1", "media-token:2")
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "decision": "select",
                    "selected_token": "media-token:2",
                },
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")

    result = await role.consider(
        await _request(purpose="media_selection", capability_manifest=manifest)
    )

    assert result["decision"]["payload"]["selected_token"] == "media-token:2"
    tools, tool_choice = model.tool_calls[0]
    assert tools[0]["function"]["name"] == "character_role_media_selection_v1"
    payload_schema = tools[0]["function"]["parameters"]["anyOf"][0]["properties"][
        "decision"
    ]["properties"]["payload"]
    select_branch = next(
        branch
        for branch in payload_schema["anyOf"]
        if branch["properties"]["decision"]["enum"] == ["select"]
    )
    assert select_branch["properties"]["selected_token"]["enum"] == [
        "media-token:1",
        "media-token:2",
    ]
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_role_media_selection_v1"},
    }


@pytest.mark.asyncio
async def test_media_selection_without_required_tool_support_fails_closed() -> None:
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"decision": "no_op"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="media_selection",
                capability_manifest=_manifest("media-token:1"),
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


def test_media_selection_rejects_conflicting_candidate_token_views() -> None:
    with pytest.raises(ValueError, match="token views disagree"):
        StructuredRoleToolContracts().media_selection(
            capability_payload={
                "offered_tokens": ["media-token:offered"],
                "candidates": [{"token": "media-token:candidate"}],
            },
            source_refs=("source:private_self",),
            recall_allowed=False,
        )


@pytest.mark.asyncio
async def test_media_selection_required_tool_reaches_deepseek_http_boundary() -> None:
    captured: dict[str, object] = {}
    raw_result = _result(
        status="decision",
        decision={
            "source_refs": ["source:private_self"],
            "payload": {"decision": "no_op"},
        },
    )

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
                                        "name": "character_role_media_selection_v1",
                                        "arguments": raw_result,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await _ProductionStructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-v4-flash",
        ).consider(
            await _request(
                purpose="media_selection",
                capability_manifest=_manifest("media-token:1"),
            )
        )
    finally:
        await model.aclose()

    assert result["decision"]["payload"]["decision"] == "no_op"
    assert "response_format" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "character_role_media_selection_v1"},
    }
    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_media_selection_v1"


@pytest.mark.asyncio
async def test_media_generic_silence_is_rejected_in_favor_of_explicit_no_op() -> None:
    role = StructuredCharacterRoleFaculty(
        model=_RequiredToolQueueModel(_result(status="silent")),
        model_id="deepseek-chat",
    )

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="media_selection",
                capability_manifest=_manifest("media-token:1"),
            )
        )

    assert raised.value.code == "phase_status_invalid"


@pytest.mark.asyncio
async def test_media_select_requires_the_selected_candidate_source_closure() -> None:
    manifest = _source_bound_media_manifest()
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "decision": "select",
                    "selected_token": "media-token:source-bound",
                },
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="media_selection",
                capability_manifest=manifest,
            )
        )

    assert raised.value.code == "media_selection_source_unclosed"


@pytest.mark.asyncio
async def test_role_can_request_one_selective_recall() -> None:
    model = _QueueModel(
        _result(
            status="recall_request",
            recall_query="the previous time this place mattered to me",
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.consider(await _request())

    assert result["status"] == "recall_request"
    assert result["recall_query"] == "the previous time this place mattered to me"
    assert result["author_lineage"]["model_id"] == "deepseek-chat"


@pytest.mark.asyncio
async def test_experience_allows_private_transition_without_expression_script() -> None:
    model = _QueueModel(
        _result(status="transition").replace(
            '"proposals": []',
            '"proposals": [{"proposal_type":"affect","source_refs":["source:private_self"]}]',
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.experience(await _request(phase="experience"))

    assert result["status"] == "transition"
    assert result["proposals"][0]["proposal_type"] == "affect"


@pytest.mark.asyncio
async def test_unoffered_token_is_a_precise_structural_failure() -> None:
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "decision": "select",
                    "selected_token": "media-token:not-offered",
                },
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="media_selection",
                capability_manifest=_manifest("media-token:1"),
            )
        )

    assert raised.value.code == "selected_token_not_offered"


@pytest.mark.asyncio
async def test_qq_perception_purpose_closes_selection_over_offered_tokens() -> None:
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "decision": "select",
                    "selected_token": "opaque-token:1",
                },
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.consider(
        await _request(
            purpose="qq_attachment_perception",
            capability_manifest=_manifest("opaque-token:1", kind="qq_attachment_perception"),
        )
    )

    assert result["decision"]["payload"]["contract"] == (
        "character-interior-qq-attachment-perception-decision.1"
    )
    tools, tool_choice = model.tool_calls[0]
    assert tools[0]["function"]["name"] == "character_role_qq_attachment_perception_v1"
    payload_schema = tools[0]["function"]["parameters"]["anyOf"][0]["properties"][
        "decision"
    ]["properties"]["payload"]
    select_branch = next(
        branch
        for branch in payload_schema["anyOf"]
        if branch["properties"]["decision"]["enum"] == ["select"]
    )
    assert select_branch["properties"]["selected_token"]["enum"] == [
        "opaque-token:1"
    ]
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_role_qq_attachment_perception_v1"},
    }


@pytest.mark.asyncio
async def test_qq_perception_without_required_tool_support_fails_closed() -> None:
    model = _QueueModel(_result(status="silent"))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="qq_attachment_perception",
                capability_manifest=_manifest(
                    "opaque-token:1", kind="qq_attachment_perception"
                ),
            )
        )

    assert raised.value.code == "required_tool_choice_unsupported"
    assert model.calls == []


@pytest.mark.asyncio
async def test_qq_perception_required_tool_reaches_deepseek_http_boundary() -> None:
    captured: dict[str, object] = {}
    raw_result = _result(
        status="decision",
        decision={
            "source_refs": ["source:private_self"],
            "payload": {"decision": "select", "selected_token": "opaque-token:1"},
        },
    )

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
                                        "name": "character_role_qq_attachment_perception_v1",
                                        "arguments": raw_result,
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    model = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await _ProductionStructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-v4-flash",
        ).consider(
            await _request(
                purpose="qq_attachment_perception",
                capability_manifest=_manifest(
                    "opaque-token:1", kind="qq_attachment_perception"
                ),
            )
        )
    finally:
        await model.aclose()

    assert result["decision"]["payload"]["selected_token"] == "opaque-token:1"
    assert "response_format" not in captured
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "character_role_qq_attachment_perception_v1"},
    }
    tools = captured["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["function"]["name"] == "character_role_qq_attachment_perception_v1"


@pytest.mark.asyncio
async def test_qq_perception_preserves_role_owned_no_op() -> None:
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"decision": "no_op"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-v4-flash")

    result = await role.consider(
        await _request(
            purpose="qq_attachment_perception",
            capability_manifest=_manifest(
                "opaque-token:1", kind="qq_attachment_perception"
            ),
        )
    )

    assert result["decision"]["payload"] == {
        "contract": "character-interior-qq-attachment-perception-decision.1",
        "decision": "no_op",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purpose", "manifest", "payload"),
    (
        ("media_selection", _manifest("media-token:1"), {"decision": "no_op"}),
        (
            "qq_attachment_perception",
            _manifest("opaque-token:1", kind="qq_attachment_perception"),
            {"selected_token": "opaque-token:1"},
        ),
        (
            "external_perception_attention",
            None,
            {"selections": []},
        ),
    ),
)
@pytest.mark.asyncio
async def test_pure_capability_purposes_reject_domain_proposals(
    purpose: str,
    manifest: _InteriorCapabilityManifest | None,
    payload: dict[str, object],
) -> None:
    if manifest is None:
        manifest = _external_manifest()
    response = _result(
        status="decision",
        decision={
            "source_refs": ["source:private_self"],
            "payload": payload,
        },
    ).replace(
        '"proposals": []',
        '"proposals": [{"proposal_type":"affect","source_refs":["source:private_self"]}]',
    )
    role = StructuredCharacterRoleFaculty(
        model=(
            _RequiredToolQueueModel(response)
            if purpose in {"media_selection", "qq_attachment_perception"}
            else _QueueModel(response)
        ),
        model_id="deepseek-chat",
    )

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose=purpose,
                capability_manifest=manifest,
            )
        )

    assert raised.value.code == "purpose_proposals_not_allowed"


def _external_manifest(*, deployment_mode: str = "shadow") -> _InteriorCapabilityManifest:
    candidates = [
        {
            "candidate_token": f"candidate:{index}",
            "candidate_ref": f"candidate:{index}",
            "exact_signal_revisions": [f"signal-revision:{index}"],
            "accessible_channels": [
                {
                    "channel_ref": "channel:public-feed",
                    "accessible_source_ids": ["source:public-feed"],
                    "evidence_refs": ["capability:public-feed"],
                }
            ],
            "model_visible_material": [
                {
                    "signal_revision_ref": f"signal-revision:{index}",
                    "source_id": "source:public-feed",
                }
            ],
        }
        for index in (1, 2)
    ]
    payload: dict[str, object] = {
        "contract": "external-perception-attention-capability.1",
        "deployment_mode": deployment_mode,
        "candidates": candidates,
    }
    if deployment_mode == "live":
        payload["durable_snapshots"] = [
            {
                "signal_revision_ref": f"signal-revision:{index}",
                "headline": f"fixture headline {index}",
                "licensed_summary": f"fixture summary {index}",
                "may_quote": False,
            }
            for index in (1, 2)
        ]
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    import hashlib

    return _InteriorCapabilityManifest(
        capability_ref="capability:external-perception:71",
        capability_kind="external_perception_attention",
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        # Revision ids are sidecar capability tokens.  Only the committed
        # channel authority is a source ref understood by the World ledger.
        source_refs=("capability:public-feed",),
    )


@pytest.mark.asyncio
async def test_external_attention_allows_character_to_select_zero_candidates() -> None:
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"selections": []},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.consider(
        await _request(
            purpose="external_perception_attention",
            capability_manifest=_external_manifest(),
        )
    )

    assert result["decision"]["payload"] == {
        "contract": "external-perception-attention-decision.1",
        "selections": [],
    }


@pytest.mark.asyncio
async def test_capability_proposal_failure_gets_one_same_author_correction() -> None:
    decision = {
        "source_refs": ["source:private_self"],
        "payload": {"selections": []},
    }
    invalid = _result(status="decision", decision=decision).replace(
        '"proposals": []',
        '"proposals": [{"proposal_type":"affect","source_refs":["source:private_self"]}]',
    )
    model = _QueueModel(invalid, _result(status="decision", decision=decision))
    manifest = _external_manifest()
    interior = CharacterInterior(
        projection=_Projection(),
        role=StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat"),
    )
    opportunity = InteriorOpportunity(
        opportunity_ref="opportunity:external:71",
        inner_turn_ref="turn:external:71",
        world_id="world:test",
        actor_ref="character:zhizhi",
        trigger_ref="trigger:external:71",
        cursor=_CURSOR,
        logical_time=_NOW,
        purpose="external_perception_attention",
        source_refs=("source:private_self",),
        capability_manifest=manifest,
    )

    result = await interior.consider(opportunity)

    assert result.status == "decided"
    assert len(model.calls) == 2
    correction = json.loads(model.calls[1][0][-1]["content"])["correction"]
    assert correction["failure_code"] == "purpose_proposals_not_allowed"
    assert "proposals" in correction["failure_detail"]


@pytest.mark.asyncio
async def test_external_attention_allows_multiple_source_closed_selections() -> None:
    selections = [
        {
            "candidate_ref": f"candidate:{index}",
            "exact_signal_revision_refs": [f"signal-revision:{index}"],
            "selected_channel_ref": "channel:public-feed",
            "subjective_summary": f"my reading {index}",
            "epistemic_notes": "",
            "attended_context_refs": ["source:private_self"],
        }
        for index in (1, 2)
    ]
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": [
                    "source:private_self",
                    "capability:public-feed",
                ],
                "payload": {"selections": selections},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    result = await role.consider(
        await _request(
            purpose="external_perception_attention",
            capability_manifest=_external_manifest(),
        )
    )

    assert result["decision"]["payload"]["selections"] == selections


@pytest.mark.asyncio
async def test_external_attention_rejects_unoffered_candidate_without_local_ignore() -> None:
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "selections": [
                        {
                            "candidate_ref": "candidate:not-offered",
                            "exact_signal_revision_refs": ["signal:not-offered"],
                            "selected_channel_ref": "channel:not-offered",
                            "subjective_summary": "invented",
                            "epistemic_notes": "",
                            "attended_context_refs": [],
                        }
                    ]
                },
            },
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    with pytest.raises(StructuredRoleResultError) as raised:
        await role.consider(
            await _request(
                purpose="external_perception_attention",
                capability_manifest=_external_manifest(),
            )
        )

    assert raised.value.code == "external_attention_candidate_not_offered"


@pytest.mark.asyncio
async def test_additional_purpose_contract_extends_structure_not_role_behavior() -> None:
    contract = PurposeDecisionContract(
        purpose="future_private_capability",
        payload_contract="character-interior-future-private-decision.1",
        capability_kind="future_private_capability",
        offered_token_fields=("offered_tokens",),
        selected_token_required=True,
    )
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"selected_token": "future-token:1", "free_reason": "mine"},
            },
        )
    )
    role = StructuredCharacterRoleFaculty(
        model=model,
        model_id="deepseek-chat",
        purpose_contracts=(contract,),
    )

    result = await role.consider(
        await _request(
            purpose="future_private_capability",
            capability_manifest=_manifest(
                "future-token:1",
                kind="future_private_capability",
            ),
        )
    )

    assert result["decision"]["payload"] == {
        "contract": "character-interior-future-private-decision.1",
        "selected_token": "future-token:1",
        "free_reason": "mine",
    }


@pytest.mark.asyncio
async def test_core_requested_correction_uses_same_author_and_names_exact_failure() -> None:
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "decision": "select",
                    "selected_token": "media-token:not-offered",
                },
            },
        ),
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "decision": "select",
                    "selected_token": "media-token:1",
                },
            },
        ),
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")
    manifest = _manifest("media-token:1")
    initial = await _request(purpose="media_selection", capability_manifest=manifest)
    with pytest.raises(StructuredRoleResultError):
        await role.consider(initial)

    corrected = await role.consider(
        await _request(
            purpose="media_selection",
            capability_manifest=manifest,
            correction_ordinal=1,
            correction_failure_code="selected_token_not_offered",
        )
    )

    assert len(model.calls) == 2
    correction_payload = json.loads(model.calls[1][0][-1]["content"])
    assert correction_payload["correction"]["failure_code"] == ("selected_token_not_offered")
    assert "offered token" in correction_payload["correction"]["failure_detail"]
    assert corrected["author_lineage"]["attempt_ordinal"] == 1
    assert corrected["author_lineage"]["parent_model_call_id"].startswith(
        "model-call:character-interior:sha256:"
    )


@pytest.mark.asyncio
async def test_character_interior_performs_same_author_structural_correction() -> None:
    model = _RequiredToolQueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "decision": "select",
                    "selected_token": "media-token:not-offered",
                },
            },
        ),
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "decision": "select",
                    "selected_token": "media-token:1",
                },
            },
        ),
    )
    manifest = _manifest("media-token:1")
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")
    interior = CharacterInterior(projection=_Projection(), role=role)
    opportunity = InteriorOpportunity(
        opportunity_ref="opportunity:71",
        inner_turn_ref="turn:71",
        world_id="world:test",
        actor_ref="character:zhizhi",
        trigger_ref="trigger:71",
        cursor=_CURSOR,
        logical_time=_NOW,
        purpose="media_selection",
        source_refs=("source:private_self",),
        capability_manifest=manifest,
        context_note="One source-bound media choice became available.",
    )

    decision = await interior.consider(opportunity)

    assert decision.status == "decided"
    assert decision.failure_code is None
    assert decision.decision is not None
    assert decision.decision["payload"]["selected_token"] == "media-token:1"
    assert decision.author_lineage is not None
    assert decision.author_lineage.attempt_ordinal == 1
    assert decision.instant_private_self is not None
    assert decision.instant_private_self.summary == "private decision"
    assert decision.instant_private_self.attended_source_refs == ("source:private_self",)
    assert len(model.calls) == 2
    correction_payload = json.loads(model.calls[1][0][-1]["content"])
    assert correction_payload["correction"]["failure_code"] == ("selected_token_not_offered")


@pytest.mark.asyncio
async def test_life_development_cross_field_failure_is_corrected_inside_one_inner_turn() -> None:
    manifest = _life_development_manifest()
    invalid = {
        "decision": "accept",
        "intention_summary": "我想去看看。",
        "importance_bp": 5200,
        "opens_at": (_NOW + timedelta(hours=2, minutes=30)).isoformat(),
        "closes_at": (_NOW + timedelta(hours=3, minutes=30)).isoformat(),
        "participant_refs": ["user:not-offered"],
        "crystallized_aspiration_source_ref": "aspiration:travel",
    }
    corrected = {**invalid, "participant_refs": ["npc:friend"]}
    model = _RequiredToolQueueModel(
        *(
            json.dumps(
                {
                    "status": "decision",
                    "summary": "private decision",
                    "attended_source_refs": ["source:private_self"],
                    "decision": {
                        "source_refs": ["source:private_self"],
                        "payload": {"completion": item},
                    },
                    "recall_query": None,
                    "proposals": [],
                },
                ensure_ascii=False,
            )
            for item in (invalid, corrected)
        ),
    )
    interior = CharacterInterior(
        projection=_Projection(),
        role=StructuredCharacterRoleFaculty(
            model=model,
            model_id="deepseek-chat",
        ),
    )
    opportunity = InteriorOpportunity(
        opportunity_ref="opportunity:life-development:71",
        inner_turn_ref="turn:life-development:71",
        world_id="world:test",
        actor_ref="character:zhizhi",
        trigger_ref="trigger:71",
        cursor=_CURSOR,
        logical_time=_NOW,
        purpose="life_development_choice",
        source_refs=("source:private_self",),
        capability_manifest=manifest,
        context_note="One executable life opportunity is available.",
    )

    result = await interior.consider(opportunity)

    assert result.status == "decided", result
    assert result.author_lineage is not None
    assert result.author_lineage.attempt_ordinal == 1
    assert result.author_lineage.parent_model_call_id is not None
    assert result.decision is not None
    assert result.decision["payload"]["completion"] == {
        **corrected,
        "opens_at": corrected["opens_at"].replace("+00:00", "Z"),
        "closes_at": corrected["closes_at"].replace("+00:00", "Z"),
    }
    assert len(model.calls) == 2
    initial_request = json.loads(model.calls[0][0][-1]["content"])
    corrected_request = json.loads(model.calls[1][0][-1]["content"])
    assert corrected_request["capability_manifest"] == initial_request["capability_manifest"]
    assert corrected_request["correction"]["failure_code"] == ("unsupported_character_participant")


@pytest.mark.asyncio
async def test_provider_failure_is_not_replaced_by_discovered_fallback() -> None:
    model = _QueueModel(TimeoutError("author provider unavailable"))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    with pytest.raises(TimeoutError, match="author provider unavailable"):
        await role.consider(await _request())

    assert model.fallback.calls == 0
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_prompt_exposes_all_facets_and_no_engagement_behavior_recipe() -> None:
    model = _QueueModel(_result(status="no_change"))
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat")

    await role.experience(await _request(phase="experience"))

    prompt = "\n".join(item["content"] for item in model.calls[0][0]).lower()
    assert all(name in prompt for name in FACET_NAMES)
    payload = json.loads(model.calls[0][0][-1]["content"])
    assert "inner_life_snapshot" in payload
    assert "instant_private_self" not in payload
    assert "instant private self" in prompt
    assert "selective_recall" in prompt
    wire = payload["wire_contract"]
    assert wire["placement_rules"]["generic_decision"].startswith(
        "When status is decision, decision MUST be an object"
    )
    assert wire["shape_example"]["generic_decision"]["decision"] == {
        "source_refs": ["<one supplied pinned source ref>"],
        "payload": {"<purpose-specific field>": "<role-authored value>"},
    }
    for forbidden in (
        "always reply",
        "never stay silent",
        "ask a question",
        "keep the conversation going",
        "be warm",
        "be helpful",
        "engagement objective",
    ):
        assert forbidden not in prompt


def test_fixture_composition_installs_the_structured_author_as_primary() -> None:
    interior = compose_fixture_character_interior(model=_QueueModel(_result(status="silent")))

    health = interior.runtime_health()

    assert health["primary_author_faculty"] == "structured-character-role"
    assert health["projection_contract"] == "subject_bound"


@pytest.mark.asyncio
async def test_bare_world_stimulus_proposal_is_rejected_without_host_authored_envelope() -> None:

    model = _RequiredToolQueueModel(
        json.dumps(
            {
                "proposal_type": "world_stimulus_appraisal_result",
                "decision": "activate",
                "brief_rationale": "this changed how I feel",
                "behavior_tendency": "respond",
                "stance": "moved",
                "display_strategy": "share softly",
                "confidence": 7000,
                "meaning_candidates": [{"meaning": "connection", "confidence": 7000}],
                "attribution": "situation",
                "severity": 4000,
            },
            ensure_ascii=False,
        )
    )
    role = StructuredCharacterRoleFaculty(model=model, model_id="deepseek-chat-v4")
    from companion_daemon.world_v2.schema_core import canonicalize_json_value

    stimulus_payload_json = json.dumps(
        canonicalize_json_value(
            {
                "contract": "character-interior-world-stimulus-capability.1",
                "process_kind": "npc_world_appraisal",
                "stimulus_kind": "settled_world_occurrence",
                "source_event": {
                    "event_id": "source:private_self",
                    "event_type": "WorldOccurrenceSettled",
                },
                "result_choices": ["no_change", "activate"],
            }
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    manifest = _InteriorCapabilityManifest(
        capability_ref="capability:world-stimulus:1",
        capability_kind="world_stimulus_appraisal",
        payload_json=stimulus_payload_json,
        payload_hash=("sha256:" + hashlib.sha256(stimulus_payload_json.encode()).hexdigest()),
        source_refs=("source:private_self",),
    )
    with pytest.raises(StructuredRoleResultError, match="role_result_schema_invalid"):
        await role.experience(
            await _request(
                phase="experience",
                purpose="world_stimulus_appraisal",
                capability_manifest=manifest,
            )
        )
