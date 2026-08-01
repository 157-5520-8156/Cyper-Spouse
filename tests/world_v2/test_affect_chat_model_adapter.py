from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.affect_chat_model_adapter import AffectDraftDeliberationAdapter
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    ValidationTechnicalFailure,
)
from companion_daemon.world_v2.proposal_envelope import DecisionProposal, ProposalEvidenceRef


def _request(
    *,
    accepted_event_ref: str = "event:appraisal-accepted:1",
    minimum_hurt_target_bp: int | None = None,
) -> ModelInput:
    dimensions = (
        "anger",
        "anxiety",
        "hurt",
        "joy",
        "loneliness",
        "resentment",
        "sadness",
        "warmth",
    )
    affect_target_bounds = (
        {
            "contract_version": "affect-target-lower-bounds.1",
            "source_world_revision": 7,
            "source_deliberation_revision": 0,
            "source_ledger_sequence": 0,
            "bounds": tuple(
                {
                    "dimension": dimension,
                    "baseline_bp": (
                        minimum_hurt_target_bp if dimension == "hurt" else 0
                    ),
                    "installed_decay_floor_bp": 300,
                    "installed_residue_bp": 500,
                    "minimum_target_intensity_bp": (
                        minimum_hurt_target_bp if dimension == "hurt" else 500
                    ),
                    "baseline_calibration_revision": (
                        2 if dimension == "hurt" else None
                    ),
                    "baseline_policy_version": (
                        "affect-baseline-policy.1" if dimension == "hurt" else None
                    ),
                    "baseline_basis_hash": "c" * 64 if dimension == "hurt" else None,
                }
                for dimension in dimensions
            ),
        }
        if minimum_hurt_target_bp is not None
        else None
    )
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
        affect_target_bounds=affect_target_bounds,
    )


class _Model:
    model = "test-affect"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(messages)
        return self.reply


class _SequencedModel:
    model = "test-affect"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(messages)
        return self.replies.pop(0)


@pytest.mark.asyncio
async def test_adapter_asks_the_same_model_to_reselect_a_target_below_the_pinned_bound() -> None:
    model = _SequencedModel(
        [
            '{"affect":"open","brief_rationale":"The hurt remains active.",'
            '"behavior_tendency":"pause","stance":"guarded","display_strategy":"restrained",'
            '"confidence":7000,"components":['
            '{"dimension":"hurt","target_intensity_bp":100}]}',
            '{"affect":"open","brief_rationale":"The hurt remains active.",'
            '"behavior_tendency":"pause","stance":"guarded","display_strategy":"restrained",'
            '"confidence":7000,"components":['
            '{"dimension":"hurt","target_intensity_bp":4300}]}',
        ]
    )

    output = await AffectDraftDeliberationAdapter(model=model).propose(
        _request(minimum_hurt_target_bp=4200)
    )
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(model.calls) == 2
    correction = model.calls[1][-1]["content"]
    assert "dimension=hurt" in correction
    assert "selected=100" in correction
    assert "minimum=4200" in correction
    assert proposal.proposed_changes[0].payload.value()["component_targets"] == [
        {"dimension": "hurt", "target_intensity_bp": 4300}
    ]


@pytest.mark.asyncio
async def test_adapter_records_technical_failure_when_reselected_target_is_still_illegal() -> None:
    below_bound = (
        '{"affect":"open","brief_rationale":"The hurt remains active.",'
        '"behavior_tendency":"pause","stance":"guarded","display_strategy":"restrained",'
        '"confidence":7000,"components":['
        '{"dimension":"hurt","target_intensity_bp":100}]}'
    )
    model = _SequencedModel([below_bound, below_bound])

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await AffectDraftDeliberationAdapter(model=model).propose(
            _request(minimum_hurt_target_bp=4200)
        )

    assert len(model.calls) == 2
    assert caught.value.failure_code == "affect_target_reselection_invalid"


@pytest.mark.asyncio
async def test_adapter_binds_an_open_episode_to_the_current_accepted_appraisal() -> None:
    model = _Model(
        json.dumps(
            {
                "affect": "open",
                "brief_rationale": "The accepted appraisal may leave residual hurt.",
                "behavior_tendency": "hold_space",
                "stance": "care_despite_hurt",
                "display_strategy": "partial_disclosure",
                "confidence": 7300,
                "components": [
                    {"dimension": "hurt", "target_intensity_bp": 4200}
                ],
            }
        )
    )
    adapter = AffectDraftDeliberationAdapter(model=model)

    output = await adapter.propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert proposal.affect_decision == "propose"
    assert proposal.evidence_refs[0].ref_id == "event:appraisal-accepted:1"
    payload = proposal.proposed_changes[0].payload.value()
    assert payload["appraisal_change_refs"] == ["change:appraisal:1"]
    assert "target_intensity_bp" in model.calls[0][0]["content"]
    assert "not an amount to add" in model.calls[0][0]["content"]
    assert payload["component_targets"] == [
        {"dimension": "hurt", "target_intensity_bp": 4200}
    ]


@pytest.mark.asyncio
async def test_adapter_fails_closed_when_the_trigger_has_no_unique_active_appraisal() -> None:
    adapter = AffectDraftDeliberationAdapter(
        model=_Model(
            '{"affect":"open","brief_rationale":"x","behavior_tendency":"hold",'
            '"stance":"guarded","display_strategy":"withhold","confidence":5000,'
            '"components":[{"dimension":"anger","target_intensity_bp":3000}]}'
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
