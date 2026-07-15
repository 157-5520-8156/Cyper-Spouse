from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.affect_chat_model_adapter import AffectDraftDeliberationAdapter
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute
from companion_daemon.world_v2.proposal_envelope import DecisionProposal, ProposalEvidenceRef


def _request(*, accepted_event_ref: str = "event:appraisal-accepted:1") -> ModelInput:
    return ModelInput(
        call_id="call:affect:1",
        attempt_id="attempt:affect:1",
        route=ModelRoute(tier="flash", reason_code="background", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref=accepted_event_ref,
        evaluated_world_revision=7,
        model_content_json=json.dumps(
            {
                "appraisals": {
                    "items": [
                        {
                            "origin": {
                                "change_id": "change:appraisal:1",
                                "accepted_event_ref": accepted_event_ref,
                            }
                        }
                    ]
                }
            }
        ),
        trigger_evidence=(
            ProposalEvidenceRef(
                ref_id=accepted_event_ref,
                evidence_kind="committed_world_event",
                source_world_revision=6,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
    )


class _Model:
    model = "test-affect"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return self.reply


@pytest.mark.asyncio
async def test_adapter_binds_an_open_episode_to_the_current_accepted_appraisal() -> None:
    adapter = AffectDraftDeliberationAdapter(
        model=_Model(
            json.dumps(
                {
                    "affect": "open",
                    "brief_rationale": "The accepted appraisal may leave residual hurt.",
                    "behavior_tendency": "hold_space",
                    "stance": "care_despite_hurt",
                    "display_strategy": "partial_disclosure",
                    "confidence": 7300,
                    "components": [{"dimension": "hurt", "intensity_bp": 4200}],
                }
            )
        )
    )

    output = await adapter.propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert proposal.affect_decision == "propose"
    assert proposal.evidence_refs[0].ref_id == "event:appraisal-accepted:1"
    payload = proposal.proposed_changes[0].payload.value()
    assert payload["appraisal_change_refs"] == ["change:appraisal:1"]
    assert payload["component_deltas"] == [{"name": "hurt", "value": 4200}]


@pytest.mark.asyncio
async def test_adapter_fails_closed_when_the_trigger_has_no_unique_active_appraisal() -> None:
    adapter = AffectDraftDeliberationAdapter(
        model=_Model(
            '{"affect":"open","brief_rationale":"x","behavior_tendency":"hold",'
            '"stance":"guarded","display_strategy":"withhold","confidence":5000,'
            '"components":[{"dimension":"anger","intensity_bp":3000}]}'
        )
    )
    request = _request().model_copy(update={"model_content_json": "{}"})

    with pytest.raises(ValueError, match="exactly one active appraisal"):
        await adapter.propose(request)


@pytest.mark.asyncio
async def test_adapter_no_change_does_not_create_a_transition() -> None:
    adapter = AffectDraftDeliberationAdapter(
        model=_Model(
            '{"affect":"no_change","brief_rationale":"No lasting shift.",'
            '"behavior_tendency":"observe","stance":"wait","display_strategy":"withhold",'
            '"confidence":2600}'
        )
    )

    output = await adapter.propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert proposal.affect_decision == "no_change"
    assert proposal.proposed_changes == ()
