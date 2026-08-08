from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

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
from companion_daemon.world_v2.character_interior.production import (
    compose_fixture_character_interior,
)
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


class _ForbiddenFallback:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature=0.8):  # pragma: no cover
        del messages, temperature
        self.calls += 1
        return '{"status":"silent"}'


def _manifest(*tokens: str, kind: str = "media_selection") -> _InteriorCapabilityManifest:
    payload_json = json.dumps(
        {"offered_tokens": list(tokens)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    import hashlib

    return _InteriorCapabilityManifest(
        capability_ref=f"capability:{kind}:71",
        capability_kind=kind,
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=("source:private_self",),
    )


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
    model = _QueueModel(
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
async def test_bare_decision_payload_is_rejected_without_host_source_binding() -> None:

    manifest = _manifest("media-token:1", "media-token:2")
    model = _QueueModel(
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
        await role.consider(
            await _request(purpose="media_selection", capability_manifest=manifest)
        )


@pytest.mark.asyncio
async def test_outcome_selection_is_one_capability_bound_interior_decision() -> None:
    manifest = _manifest(
        "candidate:quiet-afternoon",
        "candidate:unexpected-invitation",
        kind="outcome_selection",
    )
    model = _QueueModel(
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
        model=_QueueModel(
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
async def test_proactive_contact_is_one_capability_bound_interior_decision() -> None:
    manifest = _manifest("send:qq", kind="proactive_contact")
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {
                    "timing_choice": "silent",
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
async def test_expression_reconsideration_requires_an_explicit_role_disposition() -> None:
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


@pytest.mark.asyncio
async def test_private_impression_experience_normalizes_one_exact_typed_proposal() -> None:
    manifest = _manifest("appraisal:h1", kind="private_impression_reflection")
    model = _QueueModel(
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
    } <= registered


@pytest.mark.asyncio
async def test_media_no_op_is_an_explicit_character_decision_without_a_token() -> None:
    manifest = _manifest("media-token:1")
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

    result = await role.consider(
        await _request(purpose="media_selection", capability_manifest=manifest)
    )

    assert result["decision"]["payload"] == {
        "contract": "character-interior-media-selection-decision.1",
        "decision": "no_op",
    }


@pytest.mark.asyncio
async def test_media_generic_silence_is_rejected_in_favor_of_explicit_no_op() -> None:
    role = StructuredCharacterRoleFaculty(
        model=_QueueModel(_result(status="silent")),
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
    model = _QueueModel(
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
    model = _QueueModel(
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
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"selected_token": "opaque-token:1"},
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
        model=_QueueModel(response),
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
    model = _QueueModel(
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
    model = _QueueModel(
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
    model = _QueueModel(
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"completion": invalid},
            },
        ),
        _result(
            status="decision",
            decision={
                "source_refs": ["source:private_self"],
                "payload": {"completion": corrected},
            },
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


    model = _QueueModel(
        json.dumps(
            {
                "proposal_type": "world_stimulus_appraisal_result",
                "decision": "activate",
                "brief_rationale": "this changed how I feel",
                "behavior_tendency": "respond",
                "stance": "moved",
                "display_strategy": "share softly",
                "confidence": 7000,
                "meaning_candidates": [
                    {"meaning": "connection", "confidence": 7000}
                ],
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
                "source_event": {"event_id": "source:private_self", "event_type": "WorldOccurrenceSettled"},
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
        payload_hash=(
            "sha256:" + hashlib.sha256(stimulus_payload_json.encode()).hexdigest()
        ),
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
