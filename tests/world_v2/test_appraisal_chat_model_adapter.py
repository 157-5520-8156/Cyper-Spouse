from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.appraisal_chat_model_adapter import (
    AppraisalDraftDeliberationAdapter,
    FastAppraisalDraftDeliberationAdapter,
)
from companion_daemon.world_v2.affect_target_bounds import (
    AFFECT_DIMENSIONS,
    AffectTargetDimensionLowerBound,
    AffectTargetLowerBounds,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    TriggerMessage,
    ValidationTechnicalFailure,
)
from companion_daemon.world_v2.proposal_envelope import DecisionProposal, ProposalEvidenceRef


def _bounds(*, hurt_minimum_bp: int) -> AffectTargetLowerBounds:
    return AffectTargetLowerBounds(
        source_world_revision=3,
        source_deliberation_revision=0,
        source_ledger_sequence=0,
        bounds=tuple(
            AffectTargetDimensionLowerBound(
                dimension=dimension,
                baseline_bp=hurt_minimum_bp if dimension == "hurt" else 0,
                installed_decay_floor_bp=300,
                installed_residue_bp=500,
                minimum_target_intensity_bp=(
                    hurt_minimum_bp if dimension == "hurt" else 500
                ),
                baseline_calibration_revision=2 if dimension == "hurt" else None,
                baseline_policy_version=(
                    "affect-baseline-policy.1" if dimension == "hurt" else None
                ),
                baseline_basis_hash="c" * 64 if dimension == "hurt" else None,
            )
            for dimension in AFFECT_DIMENSIONS
        ),
    )


def _request(*, hurt_minimum_bp: int | None = None) -> ModelInput:
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
        affect_target_bounds=(
            _bounds(hurt_minimum_bp=hurt_minimum_bp)
            if hurt_minimum_bp is not None
            else None
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


class _SequencedModel:
    model = "qwen-local"

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return self.replies[len(self.calls) - 1]


class _JsonOnlyModel:
    model = "qwen-local-json"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.json_calls: list[list[dict[str, str]]] = []

    async def complete(self, _messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        raise AssertionError("the local JSON-capable provider should use JSON mode")

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        self.json_calls.append(messages)
        return self.reply


@pytest.mark.asyncio
async def test_full_appraisal_reselects_instead_of_clamping_a_below_bound_affect_target() -> None:
    model = _SequencedModel(
        [
            '{"appraise":true,"affect":"open","brief_rationale":"It still matters.",'
            '"behavior_tendency":"pause","stance":"guarded","display_strategy":"restrained",'
            '"confidence":7000,"meanings":[{"meaning":"disappointment","confidence":7000}],'
            '"attribution":"user","severity":6000,"components":['
            '{"dimension":"hurt","target_intensity_bp":100}]}',
            '{"appraise":true,"affect":"open","brief_rationale":"It still matters.",'
            '"behavior_tendency":"pause","stance":"guarded","display_strategy":"restrained",'
            '"confidence":7000,"meanings":[{"meaning":"disappointment","confidence":7000}],'
            '"attribution":"user","severity":6000,"components":['
            '{"dimension":"hurt","target_intensity_bp":4400}]}',
        ]
    )

    output = await AppraisalDraftDeliberationAdapter(model=model).propose(
        _request(hurt_minimum_bp=4200)
    )
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(model.calls) == 2
    correction = model.calls[1][-1]["content"]
    assert "dimension=hurt" in correction
    assert "selected=100" in correction
    assert "minimum=4200" in correction
    assert proposal.proposed_changes[1].payload.value()["component_targets"] == [
        {"dimension": "hurt", "target_intensity_bp": 4400}
    ]


@pytest.mark.asyncio
async def test_fast_appraisal_reselects_instead_of_clamping_a_below_bound_affect_target() -> None:
    model = _SequencedModel(
        [
            '{"appraise":true,"brief_rationale":"It still matters.",'
            '"behavior_tendency":"pause","stance":"guarded","display_strategy":"restrained",'
            '"confidence":7000,"meaning":"disappointment","attribution":"user",'
            '"severity":6000,"open_affect":true,"affect_dimension":"hurt",'
            '"affect_target_intensity_bp":100}',
            '{"appraise":true,"brief_rationale":"It still matters.",'
            '"behavior_tendency":"pause","stance":"guarded","display_strategy":"restrained",'
            '"confidence":7000,"meaning":"disappointment","attribution":"user",'
            '"severity":6000,"open_affect":true,"affect_dimension":"hurt",'
            '"affect_target_intensity_bp":4500}',
        ]
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(
        _request(hurt_minimum_bp=4200)
    )
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(model.calls) == 2
    correction = model.calls[1][-1]["content"]
    assert "dimension=hurt" in correction
    assert "selected=100" in correction
    assert "minimum=4200" in correction
    assert proposal.proposed_changes[1].payload.value()["component_targets"] == [
        {"dimension": "hurt", "target_intensity_bp": 4500}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_factory,replies",
    [
        (
            lambda model: AppraisalDraftDeliberationAdapter(model=model),
            [
                '{"appraise":true,"affect":"open","brief_rationale":"It still matters.",'
                '"behavior_tendency":"pause","stance":"guarded","display_strategy":"restrained",'
                '"confidence":7000,"meanings":[{"meaning":"disappointment","confidence":7000}],'
                '"attribution":"user","severity":6000,"components":['
                '{"dimension":"hurt","target_intensity_bp":100}]}'
            ]
            * 2,
        ),
        (
            lambda model: FastAppraisalDraftDeliberationAdapter(model=model),
            [
                '{"appraise":true,"brief_rationale":"It still matters.",'
                '"behavior_tendency":"pause","stance":"guarded","display_strategy":"restrained",'
                '"confidence":7000,"meaning":"disappointment","attribution":"user",'
                '"severity":6000,"open_affect":true,"affect_dimension":"hurt",'
                '"affect_target_intensity_bp":100}'
            ]
            * 2,
        ),
    ],
    ids=["full", "fast"],
)
async def test_appraisal_records_technical_failure_when_reselection_is_still_illegal(
    adapter_factory,
    replies: list[str],
) -> None:
    model = _SequencedModel(replies)

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await adapter_factory(model).propose(_request(hurt_minimum_bp=4200))

    assert len(model.calls) == 2
    assert caught.value.failure_code == "affect_target_reselection_invalid"


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
async def test_appraisal_prompt_allows_model_to_notice_sustained_ordinary_interaction() -> None:
    model = _Model(
        '{"appraise":false,"brief_rationale":"Nothing changed for her.",'
        '"behavior_tendency":"continue","stance":"present","display_strategy":"natural",'
        '"confidence":5000}'
    )

    await AppraisalDraftDeliberationAdapter(model=model).propose(_request())

    system = model.calls[0][0]["content"]
    assert "sustained ordinary interaction" in system
    assert "no message count" in system
    assert "may still choose appraise=false" in system


@pytest.mark.asyncio
async def test_fast_wire_contract_uses_boolean_open_affect_and_maps_to_internal_affect() -> None:
    model = _Model(
        json.dumps(
            {
                "appraise": True,
                "brief_rationale": "A missed connection matters to her.",
                "behavior_tendency": "pause_and_consider",
                "stance": "guarded",
                "display_strategy": "restrained",
                "meaning": "disappointment",
                "attribution": "user",
                "severity": 5000,
                "confidence": 7000,
                "open_affect": True,
                "affect_dimension": "hurt",
                "affect_target_intensity_bp": 3000,
            }
        )
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    system = model.calls[0][0]["content"]
    assert '"open_affect":bool' in system
    assert '"affect_target_intensity_bp":0到10000整数' in system
    assert '"affect":"no_change或open"' not in system
    assert proposal.affect_decision == "propose"
    assert proposal.affect_tendencies == ("hurt",)
    assert proposal.proposed_changes[1].payload.value()["component_targets"] == [
        {"dimension": "hurt", "target_intensity_bp": 3000}
    ]


@pytest.mark.asyncio
async def test_fast_adapter_expands_only_its_small_enumerated_result() -> None:
    model = _Model(
        json.dumps(
            {
                "appraise": True,
                "brief_rationale": "A missed connection matters to her.",
                "behavior_tendency": "pause_and_consider",
                "stance": "guarded",
                "display_strategy": "restrained",
                "meaning": "disappointment",
                "attribution": "user",
                "severity": 5000,
                "confidence": 7000,
                "open_affect": True,
                "affect_dimension": "hurt",
                "affect_target_intensity_bp": 3000,
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
async def test_fast_adapter_reasks_after_a_small_model_key_typo() -> None:
    model = _SequencedModel(
        [
            '{"apraise":false,"brief_rationale":"No durable shift.",'
            '"behavior_tendency":"stay_present","stance":"open",'
            '"display_strategy":"natural","confidence":0,"meaning":"ordinary",'
            '"attribution":"unknown","severity":0,"open_affect":false,'
            '"affect_dimension":null,"affect_target_intensity_bp":0}',
            '{"appraise":false,"brief_rationale":"No durable shift.",'
            '"behavior_tendency":"stay_present","stance":"open",'
            '"display_strategy":"natural","confidence":0,"meaning":"ordinary",'
            '"attribution":"unknown","severity":0,"open_affect":false,'
            '"affect_dimension":null,"affect_target_intensity_bp":0}',
        ]
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(model.calls) == 2
    assert "fast_appraisal.keys.invalid" in model.calls[1][-1]["content"]
    assert proposal.proposed_changes == ()
    assert proposal.affect_decision == "no_change"


@pytest.mark.asyncio
async def test_fast_adapter_reasks_same_model_after_real_qwen_shape_error() -> None:
    """A schema miss is corrected by the model, never by a local emotion map."""

    model = _SequencedModel(
        [
            # Captured from the production local Qwen endpoint: ``uncertainty``
            # is a valid appraisal meaning but not an Affect dimension. The
            # private semantic fields complete the current strict contract.
            '{"appraise":true,"brief_rationale":"She is unsure what this means.",'
            '"behavior_tendency":"pause","stance":"uncertain",'
            '"display_strategy":"restrained","meaning":"uncertainty","attribution":"user",'
            '"severity":8000,"confidence":7000,"open_affect":true,'
            '"affect_dimension":"uncertainty","affect_target_intensity_bp":7000}',
            '{"appraise":true,"brief_rationale":"Her disappointment may reflect a missed connection.",'
            '"behavior_tendency":"pause_and_consider","stance":"guarded",'
            '"display_strategy":"restrained","meaning":"misunderstanding",'
            '"attribution":"user","severity":7200,"confidence":7600,'
            '"open_affect":true,"affect_dimension":"hurt","affect_target_intensity_bp":6400}',
        ]
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(model.calls) == 2
    correction = model.calls[1][-1]["content"]
    assert "fast_appraisal.affect_dimension.invalid" in correction
    assert "依据原始上下文重新选择合法值" in correction
    assert "系统不会指定语义答案" in correction
    assert output.winning_model_call_id is not None
    assert output.winning_model_call_id != _request().call_id
    assert output.winning_request_hash is not None
    assert proposal.proposed_changes[0].payload.value()["meaning_candidates"] == [
        {"meaning": "misunderstanding", "confidence": 7600}
    ]
    assert proposal.affect_tendencies == ("hurt",)
    assert proposal.behavior_tendency == "pause_and_consider"
    assert proposal.stance == "guarded"
    assert proposal.display_strategy == "restrained"


@pytest.mark.asyncio
async def test_fast_adapter_rejects_an_emotion_word_in_the_boolean_open_affect_field() -> None:
    model = _SequencedModel(
        [
            '{"appraise":true,"brief_rationale":"她感到这次落空很重要。",'
            '"behavior_tendency":"先想想","stance":"保留",'
            '"display_strategy":"克制","meaning":"disappointment","attribution":"user",'
            '"severity":6500,"confidence":7000,"open_affect":"hurt",'
            '"affect_dimension":"hurt","affect_target_intensity_bp":5000}',
            '{"appraise":true,"brief_rationale":"她感到这次落空很重要。",'
            '"behavior_tendency":"先想想","stance":"保留",'
            '"display_strategy":"克制","meaning":"disappointment","attribution":"user",'
            '"severity":6500,"confidence":7000,"open_affect":true,'
            '"affect_dimension":"hurt","affect_target_intensity_bp":5000}',
        ]
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(model.calls) == 2
    assert "fast_appraisal.open_affect.invalid" in model.calls[1][-1]["content"]
    assert proposal.affect_decision == "propose"
    assert proposal.affect_tendencies == ("hurt",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_reply,expected_error",
    [
        (
            '{"appraise":false,"behavior_tendency":"stay_present","stance":"open",'
            '"display_strategy":"natural","confidence":6200,"meaning":"ordinary",'
            '"attribution":"unknown","severity":0,"open_affect":false,'
            '"affect_dimension":null,"affect_target_intensity_bp":0}',
            "fast_appraisal.keys.invalid",
        ),
        (
            '{"appraise":false,"brief_rationale":"","behavior_tendency":"stay_present",'
            '"stance":"open","display_strategy":"natural","confidence":6200,'
            '"meaning":"ordinary","attribution":"unknown","severity":0,'
            '"open_affect":false,"affect_dimension":null,"affect_target_intensity_bp":0}',
            "fast_appraisal.brief_rationale.invalid",
        ),
    ],
)
async def test_fast_adapter_reasks_when_required_private_semantics_are_missing_or_empty(
    invalid_reply: str,
    expected_error: str,
) -> None:
    model = _SequencedModel(
        [
            invalid_reply,
            '{"appraise":false,"brief_rationale":"No durable shift.",'
            '"behavior_tendency":"stay_present","stance":"open",'
            '"display_strategy":"natural","confidence":6200,"meaning":"ordinary",'
            '"attribution":"unknown","severity":0,"open_affect":false,'
            '"affect_dimension":null,"affect_target_intensity_bp":0}',
        ]
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(model.calls) == 2
    assert expected_error in model.calls[1][-1]["content"]
    assert proposal.brief_rationale == "No durable shift."
    assert proposal.behavior_tendency == "stay_present"


@pytest.mark.asyncio
async def test_fast_adapter_never_translates_model_prose_into_an_emotion_enum() -> None:
    model = _SequencedModel(
        [
            '{"appraise":true,"brief_rationale":"她觉得失望。",'
            '"behavior_tendency":"先想想","stance":"保留",'
            '"display_strategy":"克制","confidence":7000,"meaning":"失望",'
            '"attribution":"user","severity":6500,"open_affect":false,'
            '"affect_dimension":null,"affect_target_intensity_bp":0}',
            '{"appraise":true,"brief_rationale":"她把这理解成一次落空。",'
            '"behavior_tendency":"先想想","stance":"保留",'
            '"display_strategy":"克制","confidence":7000,"meaning":"disappointment",'
            '"attribution":"user","severity":6500,"open_affect":false,'
            '"affect_dimension":null,"affect_target_intensity_bp":0}',
        ]
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(model.calls) == 2
    assert "fast_appraisal.meaning.invalid" in model.calls[1][-1]["content"]
    assert (
        proposal.proposed_changes[0].payload.value()["meaning_candidates"][0]["meaning"]
        == "disappointment"
    )


@pytest.mark.asyncio
async def test_fast_adapter_uses_provider_json_mode_when_available() -> None:
    model = _JsonOnlyModel(
        '{"appraise":false,"brief_rationale":"No durable shift.",'
        '"behavior_tendency":"stay_present","stance":"open",'
        '"display_strategy":"natural","confidence":6200,"meaning":"ordinary",'
        '"attribution":"unknown","severity":0,"open_affect":false,'
        '"affect_dimension":null,"affect_target_intensity_bp":0}'
    )

    output = await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(model.json_calls) == 1
    assert proposal.proposed_changes == ()


@pytest.mark.asyncio
async def test_fast_appraisal_contract_is_structural_and_carries_compact_character_context() -> (
    None
):
    model = _Model(
        '{"appraise":false,"brief_rationale":"No durable shift.",'
        '"behavior_tendency":"stay_present","stance":"open",'
        '"display_strategy":"natural","confidence":6200,"meaning":"ordinary",'
        '"attribution":"unknown","severity":0,"open_affect":false,'
        '"affect_dimension":null,"affect_target_intensity_bp":0}'
    )

    await FastAppraisalDraftDeliberationAdapter(model=model).propose(_request())

    system = model.calls[0][0]["content"]
    supplied = json.loads(model.calls[0][1]["content"])
    assert "必须恰好包含全部12个键" in system
    assert "所有自由文本描述角色自己的" in system
    assert "不要求已经在消息里显式说出" in system
    assert "如果用户明确表达" not in system
    assert "普通消息必须" not in system
    assert supplied["current_message"]["text"] == "你刚刚的回复让我有点失望。"
    assert supplied["character_context"] == {"capsule": "authoritative"}
    assert "call_id" not in supplied
    assert "trigger_evidence" not in supplied


@pytest.mark.asyncio
async def test_fast_appraisal_context_is_bounded_to_current_self_and_recent_dialogue() -> None:
    dialogue_items = [
        {
            "item_ref": f"dialogue:{index}",
            "value": {
                "speaker": "user" if index % 2 == 0 else "companion",
                "text": f"recent line {index}",
            },
        }
        for index in range(8)
    ]
    raw_context = json.dumps(
        {
            "logical_time": "2026-07-29T21:00:00+08:00",
            "slices": {
                "character_core": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "character-core:1",
                            "value": {
                                "values": {
                                    "slow_evolving": {
                                        "autonomy_style": "self_directed",
                                    }
                                }
                            },
                        }
                    ],
                },
                "recent_dialogue": {
                    "availability": "available",
                    "items": dialogue_items,
                },
                "procedural_guidance": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "irrelevant:bulk",
                            "value": {"text": "IRRELEVANT_BULK_" + ("x" * 80_000)},
                        }
                    ],
                },
            },
        }
    )
    request = _request().model_copy(update={"model_content_json": raw_context})
    model = _Model(
        '{"appraise":false,"brief_rationale":"No durable shift.",'
        '"behavior_tendency":"stay_present","stance":"open",'
        '"display_strategy":"natural","confidence":6200,"meaning":"ordinary",'
        '"attribution":"unknown","severity":0,"open_affect":false,'
        '"affect_dimension":null,"affect_target_intensity_bp":0}'
    )

    await FastAppraisalDraftDeliberationAdapter(model=model).propose(request)

    supplied = json.loads(model.calls[0][1]["content"])
    context = supplied["character_context"]
    assert set(context) == {"logical_time", "current_self_state", "recent_dialogue"}
    assert context["current_self_state"]["stable_self"][0]["source_ref"] == "character-core:1"
    retained_dialogue = context["recent_dialogue"]["items"]
    assert len(retained_dialogue) == 4
    assert all(
        item["source_ref"] in {f"dialogue:{index}" for index in range(8)}
        for item in retained_dialogue
    )
    assert "IRRELEVANT_BULK_" not in model.calls[0][1]["content"]
    assert len(model.calls[0][1]["content"]) < 12_000


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
                            "source_bindings": [{"ref": "event:observation:1", "hash": "d" * 64}],
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
    # The compact view now carries one bounded, source-bearing pinned-time
    # item so the local self-state model can distinguish an actual late-night
    # turn from an invented time of day. Proof noise still dominates the
    # fixture and must be removed by a wide margin.
    assert len(json.dumps(compact, ensure_ascii=False)) < len(noisy_context) // 3
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
                    {"dimension": "hurt", "target_intensity_bp": 3600},
                    {"dimension": "sadness", "target_intensity_bp": 1800},
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
    assert affect_payload["component_targets"] == [
        {"dimension": "hurt", "target_intensity_bp": 3600},
        {"dimension": "sadness", "target_intensity_bp": 1800},
    ]
    assert "target_intensity_bp" in model.calls[0][0]["content"]
    assert "rather than an amount to add" in model.calls[0][0]["content"]
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

    first = await AppraisalDraftDeliberationAdapter(model=_Model(json.dumps(base))).propose(
        _request()
    )
    second = await AppraisalDraftDeliberationAdapter(model=_Model(json.dumps(changed))).propose(
        _request()
    )
    first_proposal = DecisionProposal.model_validate_json(json.dumps(first.raw_proposal))
    second_proposal = DecisionProposal.model_validate_json(json.dumps(second.raw_proposal))

    assert first_proposal.proposal_id != second_proposal.proposal_id
    assert (
        first_proposal.proposed_changes[0].change_id
        != second_proposal.proposed_changes[0].change_id
    )


@pytest.mark.asyncio
async def test_adapter_rejects_open_affect_without_an_appraisal() -> None:
    model = _Model(
        '{"appraise":false,"affect":"open","brief_rationale":"carry it",'
        '"behavior_tendency":"withdraw","stance":"wait","display_strategy":"withhold",'
        '"confidence":5000,"components":[{"dimension":"hurt","target_intensity_bp":3000}]}'
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
        '"components":[{"dimension":"jealousy","target_intensity_bp":3000}]}'
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
