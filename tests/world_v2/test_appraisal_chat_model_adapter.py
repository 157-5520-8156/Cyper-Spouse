from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.appraisal_chat_model_adapter import (
    AppraisalDraftDeliberationAdapter,
    FastAppraisalDraftDeliberationAdapter,
)
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
    assert proposal.affect_decision == "no_change"
    assert "AppraisalDraft" in model.calls[0][0]["content"]
    assert "before the visible reply" in model.calls[0][0]["content"]
    assert "virtual companion" not in model.calls[0][0]["content"].lower()
    assert "display_strategy" in model.calls[0][0]["content"]
    assert '"trigger_evidence"' in model.calls[0][1]["content"]


@pytest.mark.asyncio
async def test_fast_adapter_expands_only_its_small_enumerated_result() -> None:
    model = _Model(
        json.dumps(
            {
                "appraise": True,
                "meaning": "disappointment",
                "attribution": "user",
                "severity": 5000,
                "confidence": 7000,
                "affect": "open",
                "affect_dimension": "hurt",
                "affect_intensity_bp": 3000,
            }
        )
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert proposal.appraisals
    assert proposal.affect_decision == "propose"
    assert proposal.proposed_changes[0].kind == "appraisal_transition"
    assert "AppraisalDraft" not in model.calls[0][1]["content"]


@pytest.mark.asyncio
async def test_fast_adapter_accepts_a_safe_small_model_key_typo_but_not_new_semantics() -> None:
    model = _Model(
        '{"apraise":false,"meaning":"ordinary","attribution":"unknown",'
        '"severity":0,"confidence":0,"affect":"open",'
        '"affect_dimension":"not-a-dimension","affect_intensity_bp":0}'
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert proposal.proposed_changes == ()
    assert proposal.affect_decision == "no_change"


@pytest.mark.asyncio
async def test_appraisal_prompt_keeps_values_but_omits_capsule_proof_noise() -> None:
    noisy_context = json.dumps(
        {
            "world_id": "world:test",
            "actor_ref": "agent:companion",
            "trigger_ref": "event:observation:1",
            "world_revision": 3,
            "logical_time": "2026-07-17T00:00:00+00:00",
            "slices": {
                "recent_dialogue": {
                    "availability": "available",
                    "source_refs": ["event:observation:1"],
                    "source_hash": "a" * 64,
                    "resolver_proof": {"large": "x" * 4_000},
                    "items": [
                        {
                            "item_ref": "dialogue:user:1",
                            "privacy_class": "private",
                            "source_hash": "b" * 64,
                            "value_hash": "c" * 64,
                            "source_bindings": [
                                {"ref": "event:observation:1", "hash": "d" * 64}
                            ],
                            "value": {
                                "speaker": "user",
                                "text": "你刚刚的回复让我有点失望。",
                            },
                        }
                    ],
                }
            },
        },
        ensure_ascii=False,
    )
    request = _request().model_copy(update={"model_content_json": noisy_context})
    model = _Model(
        '{"appraise":false,"brief_rationale":"No durable shift.",'
        '"behavior_tendency":"observe","stance":"wait",'
        '"display_strategy":"withhold","confidence":3000}'
    )

    await AppraisalDraftDeliberationAdapter(model=model).propose(request)

    supplied = json.loads(model.calls[0][1]["content"])["request"]
    compact = json.loads(supplied["model_content_json"])
    dialogue = compact["slices"]["recent_dialogue"]
    assert dialogue["items"][0]["value"]["text"] == "你刚刚的回复让我有点失望。"
    assert dialogue["items"][0]["source_ref"] == "dialogue:user:1"
    assert "resolver_proof" not in dialogue
    assert len(json.dumps(compact, ensure_ascii=False)) < len(noisy_context) // 4
    # Proposal materialization and local audit continue to receive the full,
    # authoritative ModelInput; only the provider-facing derivative is compact.
    assert request.model_content_json == noisy_context


@pytest.mark.asyncio
async def test_adapter_materializes_same_turn_appraisal_and_affect_in_one_proposal() -> None:
    model = _Model(
        json.dumps(
            {
                "appraise": True,
                "affect": "open",
                "brief_rationale": "The missed connection is significant enough to carry into this turn.",
                "behavior_tendency": "repair",
                "stance": "attend",
                "display_strategy": "restrained_acknowledgement",
                "confidence": 7600,
                "meanings": [
                    {"meaning": "disappointment", "confidence": 7200},
                    {"meaning": "misunderstanding", "confidence": 2800},
                ],
                "attribution": "companion",
                "severity": 5800,
                "components": [
                    {"dimension": "hurt", "intensity_bp": 3600},
                    {"dimension": "sadness", "intensity_bp": 1800},
                ],
            }
        )
    )

    output = await AppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert [change.kind for change in proposal.proposed_changes] == [
        "appraisal_transition",
        "affect_transition",
    ]
    appraisal_change, affect_change = proposal.proposed_changes
    affect_payload = affect_change.payload.value()
    assert affect_payload["appraisal_change_refs"] == [appraisal_change.change_id]
    assert affect_payload["component_deltas"] == [
        {"name": "hurt", "value": 3600},
        {"name": "sadness", "value": 1800},
    ]
    assert proposal.affect_decision == "propose"
    assert proposal.affect_tendencies == ("hurt", "sadness")
    assert proposal.behavior_tendency == "repair"
    assert proposal.display_strategy == "restrained_acknowledgement"


@pytest.mark.asyncio
async def test_materialized_fields_are_part_of_the_proposal_identity() -> None:
    base = {
        "appraise": True,
        "brief_rationale": "The interaction carries a durable relational meaning.",
        "behavior_tendency": "repair",
        "stance": "attend",
        "display_strategy": "restrained_acknowledgement",
        "confidence": 7600,
        "meanings": [{"meaning": "disappointment", "confidence": 7200}],
        "attribution": "companion",
        "severity": 5800,
    }
    changed = {**base, "display_strategy": "direct_acknowledgement"}

    first = await AppraisalDraftDeliberationAdapter(
        model=_Model(json.dumps(base))
    ).propose(_request())
    second = await AppraisalDraftDeliberationAdapter(
        model=_Model(json.dumps(changed))
    ).propose(_request())
    first_proposal = DecisionProposal.model_validate_json(json.dumps(first.raw_proposal))
    second_proposal = DecisionProposal.model_validate_json(json.dumps(second.raw_proposal))

    assert first_proposal.proposal_id != second_proposal.proposal_id
    assert first_proposal.proposed_changes[0].change_id != second_proposal.proposed_changes[0].change_id


@pytest.mark.asyncio
async def test_adapter_rejects_open_affect_without_an_appraisal() -> None:
    model = _Model(
        '{"appraise":false,"affect":"open","brief_rationale":"carry it",'
        '"behavior_tendency":"withdraw","stance":"wait","display_strategy":"withhold",'
        '"confidence":5000,"components":[{"dimension":"hurt","intensity_bp":3000}]}'
    )

    with pytest.raises(ValueError, match="requires appraise=true"):
        await AppraisalDraftDeliberationAdapter(model=model).propose(_request())


@pytest.mark.asyncio
async def test_adapter_fails_closed_for_affect_dimensions_outside_the_domain_matrix() -> None:
    model = _Model(
        '{"appraise":true,"affect":"open","brief_rationale":"carry it",'
        '"behavior_tendency":"observe","stance":"wait","display_strategy":"withhold",'
        '"confidence":5000,"meanings":[{"meaning":"disappointment","confidence":5000}],'
        '"attribution":"user","severity":5000,'
        '"components":[{"dimension":"jealousy","intensity_bp":3000}]}'
    )

    with pytest.raises(ValueError, match="component"):
        await AppraisalDraftDeliberationAdapter(model=model).propose(_request())


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
