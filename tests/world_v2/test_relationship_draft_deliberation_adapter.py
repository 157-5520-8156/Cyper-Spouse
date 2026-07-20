from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute
from companion_daemon.world_v2.proposal_envelope import DecisionProposal, ProposalEvidenceRef
from companion_daemon.world_v2.relationship_draft_deliberation_adapter import (
    RelationshipDraftDeliberationAdapter,
)


ACCEPTED_APPRAISAL = "event:appraisal-accepted:1"


def _request(*, relationships: tuple[dict[str, object], ...] = ()) -> ModelInput:
    return ModelInput(
        call_id="call:relationship:1",
        attempt_id="attempt:relationship:1",
        route=ModelRoute(tier="flash", reason_code="background", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref=ACCEPTED_APPRAISAL,
        evaluated_world_revision=7,
        model_content_json=json.dumps(
            {
                "actor_ref": "actor:companion",
                "slices": {
                    "relationship_slice": {
                        "availability": "available",
                        "items": [
                            {"value": value}
                            for value in relationships
                        ],
                    },
                    "appraisals": {
                        "availability": "available",
                        "items": [
                            {
                                "value": {
                                    "subject_ref": "user:geoff",
                                    "origin": {
                                        "accepted_event_ref": ACCEPTED_APPRAISAL,
                                        "change_id": "change:appraisal:1",
                                    },
                                    "hypotheses": [
                                        {"meaning": "reliability_confirmed", "weight_bp": 7200}
                                    ],
                                }
                            }
                        ],
                    },
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        trigger_evidence=(
            ProposalEvidenceRef(
                ref_id=ACCEPTED_APPRAISAL,
                evidence_kind="committed_world_event",
                source_world_revision=6,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
    )


class _Model:
    model = "test-relationship"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return self.reply


@pytest.mark.asyncio
async def test_materializes_signal_with_subject_pinned_from_relationship_slice() -> None:
    model = _Model(
        json.dumps(
            {
                "decision": "signal",
                "signal_code": "reliability_follow_through",
                "confidence_bp": 7200,
                "persistence": "durable",
                "rationale_code": "accepted_reliability_evidence",
                "suggested_deltas": {
                    "trust_bp": 320,
                    "closeness_bp": 80,
                    "respect_bp": 100,
                    "reliability_bp": 300,
                    "mutuality_bp": 0,
                    "repair_confidence_bp": 40,
                },
            }
        )
    )
    adapter = RelationshipDraftDeliberationAdapter(model=model)

    output = await adapter.propose(
        _request(relationships=(
            {"relationship_id": "relationship:geoff", "subject_ref": "user:geoff", "stage": "friend"},
        ))
    )
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert model.calls == 1
    assert len(proposal.proposed_changes) == 1
    change = proposal.proposed_changes[0]
    assert (change.kind, change.transition) == ("relationship_signal", "suggest")
    assert change.evidence_refs == (ACCEPTED_APPRAISAL,)
    assert change.payload.value() == {
        "subject_ref": "user:geoff",
        "signal_code": "reliability_follow_through",
        "confidence_bp": 7200,
        "persistence": "durable",
        "rationale_code": "accepted_reliability_evidence",
        "suggested_deltas": {
            "trust_bp": 320,
            "closeness_bp": 80,
            "respect_bp": 100,
            "reliability_bp": 300,
            "mutuality_bp": 0,
            "repair_confidence_bp": 40,
        },
    }


@pytest.mark.asyncio
async def test_first_accepted_appraisal_uses_a_source_bound_virtual_stranger() -> None:
    model = _Model(
        json.dumps(
            {
                "decision": "signal",
                "signal_code": "first_reliability_signal",
                "confidence_bp": 6000,
                "persistence": "session",
                "rationale_code": "accepted_appraisal_first_signal",
                "suggested_deltas": {
                    "trust_bp": 20,
                    "closeness_bp": 0,
                    "respect_bp": 20,
                    "reliability_bp": 20,
                    "mutuality_bp": 0,
                    "repair_confidence_bp": 0,
                },
            }
        )
    )
    output = await RelationshipDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert model.calls == 1
    assert proposal.proposed_changes[0].payload.value()["subject_ref"] == "user:geoff"


@pytest.mark.asyncio
async def test_multiple_counterpart_subjects_fail_closed() -> None:
    model = _Model('{"decision":"no_change"}')
    adapter = RelationshipDraftDeliberationAdapter(model=model)

    with pytest.raises(ValueError, match="exactly one counterpart"):
        await adapter.propose(
            _request(relationships=(
                {"subject_ref": "user:geoff"},
                {"subject_ref": "user:other"},
            ))
        )
    assert model.calls == 0


class _CapturingModel:
    model = "test-relationship-capture"

    def __init__(self) -> None:
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.messages.append(messages)
        return '{"decision":"no_change"}'


def _rich_request() -> ModelInput:
    base = _request(relationships=({"subject_ref": "user:geoff", "stage": "acquaintance"},))
    material = json.loads(base.model_content_json)
    material["slices"]["recent_dialogue"] = {
        "availability": "available",
        "items": [
            {"value": {"speaker": "counterpart", "text": "那你会心疼我嘛", "sequence": 1}},
            {"value": {"speaker": "companion", "text": "会呀，怎么突然这么问", "sequence": 2}},
        ],
    }
    material["slices"]["affect_episodes"] = {
        "availability": "available",
        "items": [
            {
                "value": {
                    "status": "active",
                    "components": [{"dimension": "warmth", "intensity_bp": 3200}],
                }
            },
            {
                "value": {
                    "status": "resolved",
                    "components": [{"dimension": "anxiety", "intensity_bp": 900}],
                }
            },
        ],
    }
    return base.model_copy(
        update={
            "model_content_json": json.dumps(material, sort_keys=True, separators=(",", ":"))
        }
    )


@pytest.mark.asyncio
async def test_draft_capsule_carries_bounded_dialogue_and_affect_context() -> None:
    model = _CapturingModel()

    await RelationshipDraftDeliberationAdapter(model=model).propose(_rich_request())

    assert len(model.messages) == 1
    capsule = json.loads(model.messages[0][1]["content"])
    assert capsule["recent_dialogue_summaries"] == (
        ["counterpart: 那你会心疼我嘛", "companion: 会呀，怎么突然这么问"]
    )
    assert capsule["active_affect_summaries"] == ["warmth 3200bp"]
    # The trigger appraisal stays the centrepiece, not a duplicated summary.
    assert capsule["recent_appraisal_summaries"] == []


@pytest.mark.asyncio
async def test_missing_optional_slices_still_produce_a_draft() -> None:
    model = _CapturingModel()

    await RelationshipDraftDeliberationAdapter(model=model).propose(
        _request(relationships=({"subject_ref": "user:geoff", "stage": "friend"},))
    )

    capsule = json.loads(model.messages[0][1]["content"])
    assert capsule["recent_dialogue_summaries"] == []
    assert capsule["active_affect_summaries"] == []


@pytest.mark.asyncio
async def test_recovery_returns_no_change_without_model_or_relationship_mutation() -> None:
    model = _Model('{"decision":"signal"}')
    output = await RelationshipDraftDeliberationAdapter(model=model).recover(
        _request(relationships=({"subject_ref": "user:geoff"},)), "timeout"
    )
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert model.calls == 0
    assert proposal.proposed_changes == ()
