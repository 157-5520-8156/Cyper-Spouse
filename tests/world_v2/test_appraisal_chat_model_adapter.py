from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.appraisal_chat_model_adapter import AppraisalDraftDeliberationAdapter
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    TriggerMessage,
)
from companion_daemon.world_v2.proposal_envelope import DecisionProposal, ProposalEvidenceRef


def _request() -> ModelInput:
    return ModelInput(
        call_id="call:appraisal:1",
        attempt_id="attempt:appraisal:1",
        route=ModelRoute(tier="flash", reason_code="background", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="event:observation:1",
        evaluated_world_revision=3,
        model_content_json='{"capsule":"authoritative"}',
        trigger_evidence=(
            ProposalEvidenceRef(
                ref_id="observation:1",
                evidence_kind="observed_message",
                source_world_revision=3,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:1",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:1",
            source_world_revision=3,
            actor="user:primary",
            channel="simulator",
            reply_target="user:primary",
            text="你刚刚的回复让我有点失望。",
        ),
    )


class _Model:
    model = "test-appraiser"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return self.reply


@pytest.mark.asyncio
async def test_adapter_materializes_a_bound_fallible_appraisal() -> None:
    model = _Model(
        json.dumps(
            {
                "appraise": True,
                "brief_rationale": "The wording may signal a missed connection, but it remains uncertain.",
                "behavior_tendency": "hold_space",
                "stance": "attend",
                "display_strategy": "withhold",
                "confidence": 7600,
                "meanings": [
                    {"meaning": "disappointment", "confidence": 7200},
                    {"meaning": "misunderstanding", "confidence": 2800},
                ],
                "attribution": "user",
                "severity": 5800,
            }
        )
    )

    output = await AppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert proposal.proposal_kind == "decision"
    assert proposal.appraisals[0].change_ref == proposal.proposed_changes[0].change_id
    assert proposal.evidence_refs[0].ref_id == "observation:1"
    payload = proposal.proposed_changes[0].payload.value()
    assert payload["meaning_candidates"][0]["meaning"] == "disappointment"
    assert "AppraisalDraft" in model.calls[0][0]["content"]
    assert '"trigger_evidence"' in model.calls[0][1]["content"]


@pytest.mark.asyncio
async def test_adapter_can_intentionally_produce_a_no_change_decision() -> None:
    model = _Model(
        '{"appraise":false,"brief_rationale":"No material relational signal.",'
        '"behavior_tendency":"observe","stance":"wait","display_strategy":"withhold",'
        '"confidence":3000}'
    )

    output = await AppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert proposal.proposed_changes == ()
    assert proposal.affect_decision == "no_change"


@pytest.mark.asyncio
async def test_adapter_fails_closed_for_meanings_outside_the_domain_matrix() -> None:
    model = _Model(
        '{"appraise":true,"brief_rationale":"bad","behavior_tendency":"observe",'
        '"stance":"wait","display_strategy":"withhold","confidence":5000,'
        '"meanings":[{"meaning":"jealousy","confidence":5000}],"attribution":"user","severity":5000}'
    )

    with pytest.raises(ValueError, match="meaning"):
        await AppraisalDraftDeliberationAdapter(model=model).propose(_request())
