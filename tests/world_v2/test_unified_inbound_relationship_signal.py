from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.character_interior.inbound_appraisal_wire import (
    _proposal_from_draft,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelOutput,
    ModelRoute,
    TriggerMessage,
)
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    TypedChange,
)
from companion_daemon.world_v2.production_proposal_grammar import (
    PRODUCTION_PROPOSAL_GRAMMARS,
)
from companion_daemon.world_v2.character_interior.inbound_author import (
    _merge_cognition_outputs,
)
from companion_daemon.world_v2.unified_inbound_decision import (
    UnifiedInboundDecisionError,
    inspect_unified_inbound_decision,
)


def _request() -> ModelInput:
    return ModelInput(
        call_id="call:unified-relationship",
        attempt_id="attempt:unified-relationship",
        route=ModelRoute(
            tier="flash",
            reason_code="test",
            router_version="test.1",
        ),
        capsule_id="a" * 64,
        trigger_ref="event:observation:relationship",
        evaluated_world_revision=3,
        evaluated_deliberation_revision=0,
        evaluated_ledger_sequence=3,
        model_content_json=json.dumps(
            {"logical_time": "2026-08-04T12:00:00+08:00"},
            separators=(",", ":"),
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:relationship",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:relationship",
            source_world_revision=3,
            actor="user:primary",
            channel="qq:c2c",
            reply_target="qq:user:primary",
            text="我今天还是特地来和你说一声。",
        ),
    )


def _relationship_draft() -> dict[str, object]:
    return {
        "signal_code": "她把这次持续出现理解成更可靠的互相惦记",
        "confidence_bp": 7300,
        "persistence": "durable",
        "rationale_code": "这不是一次性的礼貌，而是她自己的关系理解",
        "suggested_deltas": {
            "trust_bp": 120,
            "closeness_bp": 180,
            "respect_bp": 40,
            "reliability_bp": 130,
            "mutuality_bp": 160,
            "repair_confidence_bp": 20,
        },
    }


def test_same_inbound_cognition_can_author_a_relationship_signal_without_a_second_model() -> None:
    proposal = DecisionProposal.model_validate_json(
        json.dumps(
            _proposal_from_draft(
                raw=json.dumps(
                    {
                        "appraise": False,
                        "brief_rationale": "这句话不需要另开情绪，但她确实重新理解了彼此的持续互动。",
                        "behavior_tendency": "自然接住",
                        "stance": "更愿意靠近",
                        "display_strategy": "不刻意宣告",
                        "confidence": 7300,
                        "relationship_signal": _relationship_draft(),
                    },
                    ensure_ascii=False,
                ),
                request=_request(),
            ),
        )
    )
    proposal = proposal.model_copy(update={"timing_choice": "silent"})

    shape = inspect_unified_inbound_decision(proposal)

    assert shape.appraisal is None
    assert shape.affect is None
    assert shape.relationship is not None
    assert shape.relationship.evidence_refs == ("event:observation:relationship",)
    assert shape.relationship.payload.value() == {
        "subject_ref": "user:primary",
        **_relationship_draft(),
    }
    evidence = next(
        item
        for item in proposal.evidence_refs
        if item.ref_id == "event:observation:relationship"
    )
    assert evidence.evidence_kind == "committed_world_event"
    assert evidence.immutable_hash == "sha256:" + "b" * 64


def test_unified_inbound_shape_rejects_a_second_relationship_signal() -> None:
    request = _request()
    proposal = DecisionProposal.model_validate_json(
        json.dumps(
            _proposal_from_draft(
                raw=json.dumps(
                    {
                        "appraise": False,
                        "brief_rationale": "关系理解改变，但没有独立情绪变化。",
                        "behavior_tendency": "继续交流",
                        "stance": "更亲近",
                        "display_strategy": "自然",
                        "confidence": 7000,
                        "relationship_signal": _relationship_draft(),
                    },
                    ensure_ascii=False,
                ),
                request=request,
            ),
        )
    )
    proposal = proposal.model_copy(update={"timing_choice": "silent"})
    relationship = next(
        change for change in proposal.proposed_changes if change.kind == "relationship_signal"
    )
    duplicate = TypedChange(
        change_id=relationship.change_id + ":duplicate",
        kind="relationship_signal",
        target_id=relationship.target_id + ":duplicate",
        expected_entity_revision=0,
        transition="suggest",
        evidence_refs=relationship.evidence_refs,
        payload=CanonicalTypedPayload.from_value(
            payload_schema="relationship_signal.v1",
            value=relationship.payload.value(),
        ),
    )
    invalid = proposal.model_copy(
        update={"proposed_changes": (*proposal.proposed_changes, duplicate)}
    )

    with pytest.raises(
        UnifiedInboundDecisionError,
        match="relationship_count_invalid",
    ):
        inspect_unified_inbound_decision(invalid)


def test_relationship_signal_is_not_inferred_by_local_code_when_role_omits_it() -> None:
    proposal = DecisionProposal.model_validate_json(
        json.dumps(
            _proposal_from_draft(
                raw=json.dumps(
                    {
                        "appraise": False,
                        "brief_rationale": "她认为这次互动无需形成持久主观变化。",
                        "behavior_tendency": "自然回应",
                        "stance": "平常",
                        "display_strategy": "自然",
                        "confidence": 6200,
                    },
                    ensure_ascii=False,
                ),
                request=_request(),
            ),
        )
    )

    assert not any(
        change.kind == "relationship_signal" for change in proposal.proposed_changes
    )


def test_paired_materializers_keep_relationship_in_the_one_inbound_proposal() -> None:
    appraisal = ModelOutput(
        model_id="same-character",
        model_version="test.1",
        raw_proposal=_proposal_from_draft(
            raw=json.dumps(
                {
                    "appraise": False,
                    "brief_rationale": "她形成了关系理解，但这次没有单独的情绪变化。",
                    "behavior_tendency": "自然回应",
                    "stance": "更愿意靠近",
                    "display_strategy": "自然",
                    "confidence": 7100,
                    "relationship_signal": _relationship_draft(),
                },
                ensure_ascii=False,
            ),
            request=_request(),
        ),
    )
    expression_proposal = DecisionProposal(
        proposal_id="proposal:expression:silent",
        trigger_ref="event:observation:relationship",
        evaluated_world_revision=3,
        evidence_refs=(),
        proposed_changes=(),
        action_intents=(),
        confidence=6800,
        brief_rationale="她选择这次不把关系理解直接说出来。",
        behavior_tendency="保留",
        stance="安静接住",
        display_strategy="不表达",
        timing_choice="silent",
    )
    expression = ModelOutput(
        model_id="same-character",
        model_version="test.1",
        raw_proposal=expression_proposal.model_dump(mode="json"),
    )

    merged_output = _merge_cognition_outputs(
        appraisal=appraisal,
        expression=expression,
    )
    merged = DecisionProposal.model_validate_json(
        json.dumps(merged_output.raw_proposal)
    )

    assert inspect_unified_inbound_decision(merged).relationship is not None
    PRODUCTION_PROPOSAL_GRAMMARS["chat_reply"].validate(merged)
