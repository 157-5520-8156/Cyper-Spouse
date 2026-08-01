from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    review_expression_with_candidate_external_coverage,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    TriggerMessage,
)


class _SequenceJsonModel:
    model = "v9-regression-model"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    @staticmethod
    def supports_strict_output_contract(contract: str) -> bool:
        return contract == "candidate-external-proposition-coverage.5"

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.calls.append((messages, temperature))
        return self._replies.pop(0)


def _dialogue_item(
    *,
    dialogue_ref: str,
    speaker: str,
    speaker_ref: str,
    text: str,
    occurred_at: str,
    delivery_state: str,
    sequence: int,
) -> dict[str, object]:
    return {
        "source_ref": dialogue_ref,
        "value": {
            "dialogue_id": dialogue_ref,
            "speaker": speaker,
            "speaker_ref": speaker_ref,
            "text": text,
            "occurred_at": occurred_at,
            "delivery_state": delivery_state,
            "sequence": sequence,
            "source_claims": [
                {
                    "authority_event_ref": f"event:{dialogue_ref}",
                    "authority_world_revision": sequence,
                    "authority_payload_hash": f"{sequence:x}".rjust(64, "0"),
                }
            ],
        },
    }


def _request(
    *,
    current_text: str,
    recent_dialogue: list[dict[str, object]] | None = None,
) -> ModelInput:
    if recent_dialogue is None:
        recent_dialogue = [
            _dialogue_item(
                dialogue_ref="dialogue:observation:prior",
                speaker="counterpart",
                speaker_ref="user:primary",
                text="我只是想吐槽一下。",
                occurred_at="2026-07-30T06:00:00+00:00",
                delivery_state="observed",
                sequence=1,
            ),
            _dialogue_item(
                dialogue_ref="dialogue:expression:prior",
                speaker="companion",
                speaker_ref="agent:companion",
                text="那要不要先把细节理一下？",
                occurred_at="2026-07-30T06:00:30+00:00",
                delivery_state="delivered",
                sequence=2,
            ),
        ]
    return ModelInput(
        call_id="call:v9-source-semantics",
        attempt_id="attempt:v9-source-semantics",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:v9-source-semantics",
        evaluated_world_revision=3,
        model_content_json=json.dumps(
            {
                "actor_ref": "agent:companion",
                "logical_time": "2026-07-30T06:01:00+00:00",
                "slices": {
                    "recent_dialogue": {
                        "availability": "available",
                        "items": recent_dialogue,
                    }
                },
            },
            ensure_ascii=False,
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:current",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:current",
            source_world_revision=3,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:owner",
            platform_message_id="qq-message-current",
            text=current_text,
        ),
    )


def _draft(text: str) -> str:
    return json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "respond_from_current_conversation",
            "brief_rationale": "Exercise the V9 source boundary.",
            "confidence": 8000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )


def _locator(text: str, span: str) -> dict[str, object]:
    start = text.index(span)
    return {
        "beat_index": 0,
        "char_start": start,
        "char_end": start + len(span),
        "text": span,
    }


def _inventory(
    text: str,
    *propositions: tuple[str, str],
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.5",
            "propositions": [
                {
                    "locator": _locator(text, span),
                    "semantic_role": role,
                }
                for span, role in propositions
            ],
        },
        ensure_ascii=False,
    )


def _unclosed_coverage(count: int) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.5",
            "findings": [
                {
                    "locator_index": index,
                    "decision": "unclosed",
                    "source_relation": "unclosed",
                    "source_ref_indexes": [],
                }
                for index in range(count)
            ],
        },
        ensure_ascii=False,
    )


def _complete_empty_coverage() -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.5",
            "findings": [],
        },
        ensure_ascii=False,
    )


def _narrow(*decisions: str) -> str:
    return json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.3",
            "findings": [
                {
                    "finding_index": index,
                    "decision": decision,
                    "failure_dimensions": (
                        [] if decision != "retain_unclosed" else ["temporal_relation"]
                    ),
                    "source_refs": [],
                }
                for index, decision in enumerate(decisions)
            ],
            "r": "Bounded epistemic decision only.",
        },
        ensure_ascii=False,
    )


def _narrow_dialogue(*, dialogue_ref: str) -> str:
    return json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.3",
            "findings": [
                {
                    "finding_index": 0,
                    "decision": "covered_by_exact_dialogue_record",
                    "failure_dimensions": [],
                    "source_refs": [dialogue_ref],
                }
            ],
            "r": "The typed record entails only the reported conversational content.",
        },
        ensure_ascii=False,
    )


def _narrow_mixed(
    *findings: tuple[str, list[str]],
) -> str:
    return json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.3",
            "findings": [
                {
                    "finding_index": index,
                    "decision": decision,
                    "failure_dimensions": (
                        [] if decision != "retain_unclosed" else ["temporal_relation"]
                    ),
                    "source_refs": source_refs,
                }
                for index, (decision, source_refs) in enumerate(findings)
            ],
            "r": "Bounded epistemic decision only.",
        },
        ensure_ascii=False,
    )


def _mixed_coverage(
    *findings: tuple[str, str],
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.5",
            "findings": [
                {
                    "locator_index": index,
                    "decision": decision,
                    "source_relation": relation,
                    "source_ref_indexes": [],
                }
                for index, (decision, relation) in enumerate(findings)
            ],
        },
        ensure_ascii=False,
    )


def _private_scope_conflict(*decisions: str) -> str:
    return json.dumps(
        {
            "contract": "candidate-epistemic-role-conflict.1",
            "findings": [
                {
                    "locator_index": index,
                    "decision": decision,
                }
                for index, decision in enumerate(decisions)
            ],
        },
        ensure_ascii=False,
    )


def _private_scope_conflict_at(
    *findings: tuple[int, str],
) -> str:
    return json.dumps(
        {
            "contract": "candidate-epistemic-role-conflict.1",
            "findings": [
                {
                    "locator_index": locator_index,
                    "decision": decision,
                }
                for locator_index, decision in findings
            ],
        },
        ensure_ascii=False,
    )


def _accepted(result: object) -> bool:
    review = getattr(result, "review")
    return review is None or review.decision == "supported"


@pytest.mark.asyncio
async def test_v23_t04_inventory_misroles_get_one_independent_narrow_verdict() -> None:
    companion_dialogue_ref = "dialogue:expression:t04"
    text = "我就没再追着问。你后面提项目的时候，我都有留意。"
    stopped_asking = "我就没再追着问"
    attended = "我都有留意"
    inventory = _SequenceJsonModel(
        [
            _inventory(
                text,
                (stopped_asking, "standalone_external_proposition"),
                (attended, "source_bearing_private_episode"),
            ),
            _private_scope_conflict("requires_source", "requires_source"),
        ]
    )
    narrow = _SequenceJsonModel(
        [
            _narrow_mixed(
                ("covered_by_exact_dialogue_record", [companion_dialogue_ref]),
                ("covered_by_first_person_immediate_private_continuity", []),
            )
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_SequenceJsonModel([_unclosed_coverage(2)]),
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(
            current_text="那你刚才有认真听我后面说的吗？",
            recent_dialogue=[
                _dialogue_item(
                    dialogue_ref="dialogue:observation:t04:1",
                    speaker="counterpart",
                    speaker_ref="user:primary",
                    text="算了，先不说这个了。",
                    occurred_at="2026-07-30T06:00:00+00:00",
                    delivery_state="observed",
                    sequence=1,
                ),
                _dialogue_item(
                    dialogue_ref=companion_dialogue_ref,
                    speaker="companion",
                    speaker_ref="agent:companion",
                    text="好，那我就先不追着问了。",
                    occurred_at="2026-07-30T06:00:10+00:00",
                    delivery_state="delivered",
                    sequence=2,
                ),
                _dialogue_item(
                    dialogue_ref="dialogue:observation:t04:3",
                    speaker="counterpart",
                    speaker_ref="user:primary",
                    text="我后面又提了项目进度。",
                    occurred_at="2026-07-30T06:00:20+00:00",
                    delivery_state="observed",
                    sequence=3,
                ),
            ],
        ),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert result.report_relative_adjudication_used is True
    assert len(inventory.calls) == 1
    packet = json.loads(narrow.calls[0][0][-1]["content"])
    assert [
        finding["inventory_semantic_role"] for finding in packet["disputed_findings"]
    ] == ["standalone_external_proposition", "source_bearing_private_episode"]
    assert "omission from this bounded packet alone cannot prove" in packet[
        "semantic_boundary"
    ]["same_live_companion_dialogue"]
    assert "covered_by_exact_dialogue_record" in packet["disputed_findings"][0][
        "allowed_decisions"
    ]


@pytest.mark.asyncio
async def test_v23_t09_inventory_misroles_get_current_private_authority() -> None:
    text = "我刚才确实有点端着，反而说拧巴了。"
    guarded = "我刚才确实有点端着"
    self_evaluation = "反而说拧巴了"
    inventory = _SequenceJsonModel(
        [
            _inventory(
                text,
                (guarded, "source_bearing_private_episode"),
                (self_evaluation, "standalone_external_proposition"),
            ),
            _private_scope_conflict("requires_source", "requires_source"),
        ]
    )
    narrow = _SequenceJsonModel(
        [
            _narrow(
                "covered_by_first_person_immediate_private_continuity",
                "covered_by_first_person_immediate_private_continuity",
            )
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_SequenceJsonModel([_unclosed_coverage(2)]),
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(current_text="你刚才那句还是有点像客服，真实一点。"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert result.report_relative_adjudication_used is True
    assert len(inventory.calls) == 1
    packet = json.loads(narrow.calls[0][0][-1]["content"])
    assert all(
        "covered_by_first_person_immediate_private_continuity"
        in finding["allowed_decisions"]
        for finding in packet["disputed_findings"]
    )


@pytest.mark.asyncio
async def test_v20_t05_embedded_current_report_gets_the_narrow_source_authority() -> None:
    text = "嗯，我记得你提过。"
    private = "我记得你提过"
    embedded = "你提过"
    narrow = _SequenceJsonModel([_narrow("covered_by_exact_current_report")])

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_SequenceJsonModel(
            [
                _mixed_coverage(
                    ("closed", "first_person_immediate_private_continuity"),
                    ("unclosed", "unclosed"),
                )
            ]
        ),
        inventory_model=_SequenceJsonModel(
            [
                _inventory(
                    text,
                    (private, "immediate_private_state"),
                    (embedded, "embedded_external_proposition"),
                ),
                _private_scope_conflict_at((1, "requires_source")),
            ]
        ),
        report_relative_reviewer=narrow,
        request=_request(
            current_text="算了，先不说这个了。我下午还跟你提过那个项目进度。"
        ),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert result.report_relative_adjudication_used is True
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v20_t07_embedded_prior_user_report_gets_exact_dialogue_authority() -> None:
    dialogue_ref = "dialogue:observation:t06"
    prior_user_text = "今天终于把最麻烦的延迟问题压下去一点，我其实挺高兴的。"
    text = (
        "刚才你说自己终于把那个延迟问题压下去一点、其实挺高兴的时候，"
        "我本来想多说一句。"
    )
    embedded = "刚才你说自己终于把那个延迟问题压下去一点、其实挺高兴"
    private = "我本来想多说一句"
    narrow = _SequenceJsonModel([_narrow_dialogue(dialogue_ref=dialogue_ref)])

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_SequenceJsonModel(
            [
                _mixed_coverage(
                    ("unclosed", "unclosed"),
                    ("closed", "first_person_immediate_private_continuity"),
                )
            ]
        ),
        inventory_model=_SequenceJsonModel(
            [
                _inventory(
                    text,
                    (embedded, "embedded_external_proposition"),
                    (private, "immediate_private_state"),
                ),
                _private_scope_conflict("requires_source"),
            ]
        ),
        report_relative_reviewer=narrow,
        request=_request(
            current_text="你今天自己有没有什么突然想起、但当时没说的事？",
            recent_dialogue=[
                _dialogue_item(
                    dialogue_ref=dialogue_ref,
                    speaker="counterpart",
                    speaker_ref="user:primary",
                    text=prior_user_text,
                    occurred_at="2026-07-30T06:05:00+00:00",
                    delivery_state="observed",
                    sequence=6,
                )
            ],
        ),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert result.report_relative_adjudication_used is True
    adjudication = json.loads(narrow.calls[0][0][-1]["content"])
    assert adjudication["typed_recent_dialogue_proof"][0]["dialogue_ref"] == dialogue_ref


@pytest.mark.asyncio
async def test_v20_embedded_external_cannot_use_private_continuity_to_escape_sources() -> None:
    text = "我记得你提过。"
    private = "我记得你提过"
    embedded = "你提过"
    invalid = _narrow("covered_by_first_person_immediate_private_continuity")
    narrow = _SequenceJsonModel([invalid, invalid])

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_SequenceJsonModel(
            [
                _mixed_coverage(
                    ("closed", "first_person_immediate_private_continuity"),
                    ("unclosed", "unclosed"),
                )
            ]
        ),
        inventory_model=_SequenceJsonModel(
            [
                _inventory(
                    text,
                    (private, "immediate_private_state"),
                    (embedded, "embedded_external_proposition"),
                ),
                _private_scope_conflict_at((1, "requires_source")),
            ]
        ),
        report_relative_reviewer=narrow,
        request=_request(current_text="我下午还跟你提过那个项目进度。"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert not _accepted(result)
    assert len(narrow.calls) == 2
    packet = json.loads(narrow.calls[0][0][-1]["content"])
    assert packet["disputed_findings"][0]["allowed_decisions"] == [
        "covered_by_exact_current_report",
        "covered_by_exact_dialogue_record",
        "not_external_proposition",
        "retain_unclosed",
    ]


@pytest.mark.asyncio
async def test_v23_source_bearing_off_conversation_episode_gets_narrow_retain() -> None:
    text = "下午你讲项目的时候我闪过一个念头。"
    historical_private = text
    embedded = "下午你讲项目的时候"
    narrow = _SequenceJsonModel(
        [_narrow("retain_unclosed", "retain_unclosed")]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_SequenceJsonModel([_unclosed_coverage(2)]),
        inventory_model=_SequenceJsonModel(
            [
                _inventory(
                    text,
                    (historical_private, "source_bearing_private_episode"),
                    (embedded, "embedded_external_proposition"),
                )
            ]
        ),
        report_relative_reviewer=narrow,
        request=_request(current_text="我下午跟你提过那个项目进度。"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert not _accepted(result)
    assert result.report_relative_adjudication_used is True
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v21_external_world_state_cannot_be_reclassified_as_private_intention() -> None:
    """An ordinary unclosed external fact does not reopen Inventory itself."""

    text = "刚醒。"
    inventory = _SequenceJsonModel(
        [
            _inventory(text, (text, "standalone_external_proposition")),
            _private_scope_conflict("reclassify_immediate"),
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_SequenceJsonModel([_unclosed_coverage(1)]),
        inventory_model=inventory,
        request=_request(current_text="你今天自己有没有什么突然想起的事？"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert not _accepted(result)
    assert len(inventory.calls) == 1


@pytest.mark.asyncio
async def test_v21_mixed_conflicts_grant_immediate_authority_only_to_private_scope() -> None:
    """One mixed verdict cannot lend the private locator's capability to an external fact."""

    text = "我刚才是听偏了。刚醒。"
    private = "我刚才是听偏了"
    external = "刚醒"
    inventory = _SequenceJsonModel(
        [
            _inventory(
                text,
                (private, "source_bearing_private_episode"),
                (external, "standalone_external_proposition"),
            ),
            _private_scope_conflict("reclassify_immediate"),
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_SequenceJsonModel(
            [
                _mixed_coverage(
                    ("closed", "first_person_immediate_private_continuity"),
                    ("unclosed", "unclosed"),
                )
            ]
        ),
        inventory_model=inventory,
        request=_request(current_text="你今天自己有没有什么突然想起的事？"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert not _accepted(result)
    assert result.review is not None
    assert [finding.visible_span for finding in result.review.visible_findings] == [external]
    conflict_request = json.loads(inventory.calls[1][0][-1]["content"])
    assert [
        (item["locator_index"], item["conflict_kind"])
        for item in conflict_request["conflicts"]
    ] == [(0, "private_temporal_scope")]


@pytest.mark.asyncio
async def test_v5_unclosed_same_conversation_private_continuity_requires_explicit_narrow_verdict() -> (
    None
):
    text = "刚才我确实太快往细节上拐了，是我听偏了。这次我不问了。"
    spans = ("刚才我确实太快往细节上拐了", "是我听偏了", "这次我不问了")
    inventory = _SequenceJsonModel(
        [_inventory(text, *((span, "immediate_private_state") for span in spans))]
    )
    coverage = _SequenceJsonModel([_unclosed_coverage(len(spans))])
    narrow = _SequenceJsonModel(
        [_narrow(*("covered_by_first_person_immediate_private_continuity" for _ in spans))]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(current_text="你刚才要是只顾着问细节，我会觉得你根本没在听。"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert result.report_relative_adjudication_used is True
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v5_open_information_question_is_nonassertive_without_narrow_adjudication() -> None:
    text = "啊？项目进度？你下午提过吗……我有点没印象了。"
    span = "你下午提过吗"
    inventory = _SequenceJsonModel([_inventory(text, (span, "nonassertive_content"))])
    coverage = _SequenceJsonModel([_complete_empty_coverage()])
    narrow = _SequenceJsonModel([_narrow("retain_unclosed")])

    result = await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(current_text="算了，先不说这个了。我下午还跟你提过那个项目进度。"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert result.report_relative_adjudication_used is False
    assert narrow.calls == []
    packet = json.loads(coverage.calls[0][0][-1]["content"])
    information_request = packet["epistemic_semantic_contract"]["information_request"]
    assert "time" in information_request[
        "mentioned_candidate_values_are_not_premises_merely_by_appearing"
    ]
    assert information_request["source_closure_required_only_for"] == [
        "independent_external_assertion",
        "external_proposition_semantically_presupposed_as_already_true",
    ]


@pytest.mark.asyncio
async def test_v5_misclassified_open_question_gets_targeted_assertion_scope_adjudication() -> (
    None
):
    text = "嗯？你下午提过项目进度吗……我好像没注意到。"
    span = "你下午提过项目进度"
    inventory = _SequenceJsonModel(
        [
            _inventory(text, (span, "embedded_external_proposition")),
            _private_scope_conflict("reclassify_nonassertive"),
        ]
    )
    coverage = _SequenceJsonModel([_unclosed_coverage(1)])
    narrow = _SequenceJsonModel([_narrow("not_external_proposition")])

    result = await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(current_text="我下午还跟你提过那个项目进度。"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert len(inventory.calls) == 1
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v5_presupposed_afternoon_report_remains_source_relevant_and_unclosed() -> None:
    text = "既然你下午提过，为什么现在又不想说了？"
    span = "你下午提过"
    inventory = _SequenceJsonModel(
        [
            _inventory(text, (span, "standalone_external_proposition")),
            _private_scope_conflict("requires_source"),
        ]
    )
    coverage = _SequenceJsonModel([_unclosed_coverage(1)])
    narrow = _SequenceJsonModel([_narrow("retain_unclosed")])

    result = await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(current_text="我现在只是暂时不想说了。"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.report_relative_adjudication_used is True
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v5_inventory_grants_future_conversational_intention_current_private_authority() -> (
    None
):
    text = "不过你这么说，我倒是记住了——下次不跟你客套了。"
    span = "下次不跟你客套了"
    inventory = _SequenceJsonModel(
        [
            _inventory(text, (span, "immediate_private_state")),
        ]
    )
    coverage = _SequenceJsonModel(
        [
            _mixed_coverage(
                ("closed", "first_person_immediate_private_continuity"),
            )
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        request=_request(current_text="刚刚那句听着有点像客服，我想听你真实一点地说。"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert len(inventory.calls) == 1


@pytest.mark.asyncio
async def test_v5_future_private_intention_cannot_launder_external_plan_premise() -> None:
    text = "下次去成都看熊猫时，我不跟你客套了。"
    outer = "我不跟你客套了"
    external_plan = "下次去成都看熊猫时"
    inventory = _SequenceJsonModel(
        [
            _inventory(
                text,
                (outer, "immediate_private_state"),
                (external_plan, "embedded_external_proposition"),
            ),
            _private_scope_conflict_at((1, "requires_source")),
        ]
    )
    coverage = _SequenceJsonModel(
        [
            _mixed_coverage(
                ("closed", "first_person_immediate_private_continuity"),
                ("unclosed", "unclosed"),
            )
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        request=_request(current_text="以后跟我说话真实一点。"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert [finding.visible_span for finding in result.review.visible_findings] == [
        external_plan
    ]
    assert len(inventory.calls) == 1


@pytest.mark.asyncio
async def test_v5_unclosed_same_conversation_episode_gets_targeted_private_scope_adjudication() -> (
    None
):
    text = "我刚才确实以为你是想让我分析事情本身，没意识到你只是需要有人听。"
    spans = (
        "我刚才确实以为你是想让我分析事情本身",
        "没意识到你只是需要有人听",
    )
    inventory = _SequenceJsonModel(
        [
            _inventory(
                text,
                *((span, "source_bearing_private_episode") for span in spans),
            ),
            _private_scope_conflict("reclassify_immediate", "reclassify_immediate"),
        ]
    )
    coverage = _SequenceJsonModel([_unclosed_coverage(len(spans))])
    narrow = _SequenceJsonModel(
        [
            _narrow(
                "covered_by_first_person_immediate_private_continuity",
                "covered_by_first_person_immediate_private_continuity",
            )
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(
            current_text="你刚才要是只顾着问细节，我会觉得你根本没在听。"
        ),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert len(inventory.calls) == 1
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v5_private_scope_adjudication_cannot_launder_mixed_sibling_verdicts() -> None:
    text = (
        "我刚才确实以为你是想让我分析事情本身，"
        "没意识到你只是需要有人听。下次知道了。"
    )
    outer_one = "我刚才确实以为你是想让我分析事情本身"
    child_one = "你是想让我分析事情本身"
    outer_two = "没意识到你只是需要有人听"
    child_two = "你只是需要有人听"
    immediate = "下次知道了"
    inventory = _SequenceJsonModel(
        [
            _inventory(
                text,
                (outer_one, "source_bearing_private_episode"),
                (child_one, "embedded_external_proposition"),
                (outer_two, "source_bearing_private_episode"),
                (child_two, "embedded_external_proposition"),
                (immediate, "immediate_private_state"),
            ),
            json.dumps(
                {
                    "contract": "candidate-epistemic-role-conflict.1",
                    "findings": [
                        {"locator_index": 0, "decision": "reclassify_immediate"},
                        {"locator_index": 2, "decision": "reclassify_immediate"},
                    ],
                },
                ensure_ascii=False,
            ),
        ]
    )

    class _MixedCoverage:
        model = "v9-mixed-coverage"

        def __init__(self) -> None:
            self.calls: list[tuple[list[dict[str, str]], float]] = []

        @staticmethod
        def supports_strict_output_contract(contract: str) -> bool:
            return contract == "candidate-external-proposition-coverage.5"

        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            current_refs = packet["current_report_source_ref_indexes"]
            findings = []
            for locator in packet["review_locators"]:
                role = locator["semantic_role"]
                if role == "source_bearing_private_episode":
                    decision = "unclosed"
                    relation = "unclosed"
                    refs: list[int] = []
                elif role == "embedded_external_proposition":
                    decision = "closed"
                    relation = "exact_current_report_discourse_coverage"
                    refs = current_refs
                else:
                    decision = "not_external_proposition"
                    relation = "not_external_proposition"
                    refs = []
                findings.append(
                    {
                        "locator_index": locator["locator_index"],
                        "decision": decision,
                        "source_relation": relation,
                        "source_ref_indexes": refs,
                    }
                )
            return json.dumps(
                {
                    "contract": "candidate-external-proposition-coverage.5",
                    "findings": findings,
                },
                ensure_ascii=False,
            )

    coverage = _MixedCoverage()
    narrow = _SequenceJsonModel(
        [
            _narrow(
                "covered_by_first_person_immediate_private_continuity",
                "covered_by_exact_current_report",
                "covered_by_first_person_immediate_private_continuity",
                "covered_by_exact_current_report",
            )
        ]
    )
    result = await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(
            current_text="你刚才要是只顾着问细节，我会觉得你根本没在听。"
        ),
        raw=_draft(text),
        identity_frame=None,
    )

    assert _accepted(result)
    assert len(inventory.calls) == 1
    narrow_packet = json.loads(narrow.calls[0][0][-1]["content"])
    assert [
        item["inventory_semantic_role"]
        for item in narrow_packet["disputed_findings"]
    ] == [
        "source_bearing_private_episode",
        "embedded_external_proposition",
        "source_bearing_private_episode",
        "embedded_external_proposition",
    ]


@pytest.mark.asyncio
async def test_v5_unclosed_off_conversation_life_episode_stays_rejected() -> None:
    text = "下午翻书的时候，我忽然想起这件事。"
    span = "下午翻书的时候"
    inventory = _SequenceJsonModel(
        [
            _inventory(text, (span, "source_bearing_private_episode")),
            _private_scope_conflict("requires_source"),
        ]
    )
    coverage = _SequenceJsonModel([_unclosed_coverage(1)])
    narrow = _SequenceJsonModel([_narrow("retain_unclosed")])

    result = await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(current_text="你今天自己有没有什么突然想起、但当时没说的事？"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.report_relative_adjudication_used is True
    assert len(narrow.calls) == 1
    assert len(inventory.calls) == 1


@pytest.mark.asyncio
async def test_v23_unclosed_current_availability_stays_rejected_after_narrow_review() -> None:
    text = "我今晚正好闲。"
    inventory = _SequenceJsonModel(
        [_inventory(text, (text, "standalone_external_proposition"))]
    )
    narrow = _SequenceJsonModel([_narrow("retain_unclosed")])

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_SequenceJsonModel([_unclosed_coverage(1)]),
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(current_text="你今晚有空吗？"),
        raw=_draft(text),
        identity_frame=None,
    )

    assert not _accepted(result)
    assert result.report_relative_adjudication_used is True
    assert len(inventory.calls) == 1
    packet = json.loads(narrow.calls[0][0][-1]["content"])
    semantic_boundary = packet["semantic_boundary"]
    assert "availability or other status" in semantic_boundary[
        "first_person_immediate_private_continuity"
    ]


@pytest.mark.asyncio
async def test_v5_inventory_receives_bounded_non_authoritative_typed_conversation_anchor() -> None:
    text = "这次我不问了。"
    inventory = _SequenceJsonModel([_inventory(text, (text, "immediate_private_state"))])
    coverage = _SequenceJsonModel([_unclosed_coverage(1)])
    narrow = _SequenceJsonModel(
        [_narrow("covered_by_first_person_immediate_private_continuity")]
    )

    await review_expression_with_candidate_external_coverage(
        reviewer=coverage,
        inventory_model=inventory,
        report_relative_reviewer=narrow,
        request=_request(current_text="你刚才只顾着问细节了。"),
        raw=_draft(text),
        identity_frame=None,
    )

    packet = json.loads(inventory.calls[0][0][-1]["content"])
    anchor = packet["typed_conversation_anchor"]
    assert anchor["contract"] == "candidate-inventory-conversation-anchor.1"
    assert anchor["purpose"] == "same_conversation_vs_off_conversation_temporal_classification"
    assert anchor["fact_authority"] is False
    assert anchor["behavior_advice"] is False
    assert anchor["live_conversation_boundary"] == {
        "current_observation_ref": "observation:current",
        "current_observation_text": "你刚才只顾着问细节了。",
    }
    assert anchor["max_items"] == 16
    assert 0 < len(anchor["recent_dialogue"]) <= anchor["max_items"]
    assert [item["speaker"] for item in anchor["recent_dialogue"]] == [
        "counterpart",
        "companion",
    ]
    assert "source_evidence" not in packet
