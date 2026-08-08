from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.character_interior.inbound_wire import (
    review_candidate_external_proposition_coverage,
    review_expression_source_closure,
)
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.structured_source_review_model import (
    StructuredSourceReviewModel,
)


class _SequenceJsonModel:
    model = "source-continuity-reviewer"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.calls.append((messages, temperature))
        return self._replies.pop(0)


def _request(*, text: str = "我刚又想起项目这事。") -> ModelInput:
    return ModelInput(
        call_id="call:source-continuity",
        attempt_id="attempt:source-continuity",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:current",
        evaluated_world_revision=9,
        model_content_json="{}",
        trigger_message=TriggerMessage(
            event_ref="event:observation:current",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:current",
            source_world_revision=9,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:owner",
            platform_message_id="qq-current",
            text=text,
        ),
    )


def _draft(text: str) -> str:
    return json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "continue_from_exact_dialogue",
            "brief_rationale": "Use the bounded recent dialogue proof.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )


def _recent_dialogue_context(
    *,
    dialogue_ref: str,
    speaker: str,
    speaker_ref: str,
    text: str,
    delivery_state: str,
    sequence: int,
) -> str:
    return json.dumps(
        {
            "actor_ref": "agent:companion",
            "logical_time": "2026-07-30T12:00:00+08:00",
            "slices": {
                "recent_dialogue": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": dialogue_ref,
                            "source_ref": dialogue_ref,
                            "attention_source_refs": [dialogue_ref],
                            "value": {
                                "dialogue_id": dialogue_ref,
                                "speaker": speaker,
                                "speaker_ref": speaker_ref,
                                "text": text,
                                "occurred_at": "2026-07-30T11:59:00+08:00",
                                "delivery_state": delivery_state,
                                "sequence": sequence,
                                "privacy_class": "private",
                                "source_claims": [
                                    {
                                        "authority_event_ref": f"event:{dialogue_ref}",
                                        "authority_world_revision": max(1, sequence // 100),
                                        "authority_payload_hash": "c" * 64,
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        },
        ensure_ascii=False,
    )


def _primary_rejection(visible_span: str) -> str:
    return json.dumps(
        {
            "ci": [],
            "v": ["undeclared_external_assertion"],
            "p": [],
            "visible_findings": [
                {
                    "category": "undeclared_external_assertion",
                    "visible_span": visible_span,
                    "claim_index": None,
                    "source_relation": "unclosed",
                    "source_refs": [],
                }
            ],
            "r": "The span refers to an earlier conversational action.",
        },
        ensure_ascii=False,
    )


def _dialogue_coverage(dialogue_ref: str) -> str:
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
            "r": "The delivered record proves that exact earlier conversational action.",
        },
        ensure_ascii=False,
    )


def _retained(*failure_dimensions: str) -> str:
    return json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.3",
            "findings": [
                {
                    "finding_index": 0,
                    "decision": "retain_unclosed",
                    "failure_dimensions": list(failure_dimensions),
                    "source_refs": [],
                }
            ],
            "r": "The dialogue record does not entail the complete proposition.",
        },
        ensure_ascii=False,
    )


def _private_continuity() -> str:
    return json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.3",
            "findings": [
                {
                    "finding_index": 0,
                    "decision": (
                        "covered_by_first_person_immediate_private_continuity"
                    ),
                    "failure_dimensions": [],
                    "source_refs": [],
                }
            ],
            "r": "The complete proposition is the companion's current private impression.",
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_delivered_companion_dialogue_can_prove_the_companion_asked_about_a_project() -> None:
    dialogue_ref = "dialogue:expression:plan:previous:beat:1"
    visible_span = "我刚才问了句项目进度。"
    reviewer = _SequenceJsonModel(
        [_primary_rejection(visible_span), _dialogue_coverage(dialogue_ref)]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_request(),
        raw=_draft(visible_span),
        identity_frame=None,
        model_visible_context_json=_recent_dialogue_context(
            dialogue_ref=dialogue_ref,
            speaker="companion",
            speaker_ref="agent:companion",
            text="项目进度现在怎么样了？",
            delivery_state="delivered",
            sequence=801,
        ),
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert (
        result.review.visible_findings[0].source_relation
        == "exact_dialogue_record_coverage"
    )
    assert result.review.visible_findings[0].source_refs == (dialogue_ref,)
    adjudication_request = json.loads(reviewer.calls[1][0][1]["content"])
    assert adjudication_request["typed_recent_dialogue_proof"] == [
        {
            "dialogue_ref": dialogue_ref,
            "speaker": "companion",
            "speaker_ref": "agent:companion",
            "text": "项目进度现在怎么样了？",
            "occurred_at": "2026-07-30T11:59:00+08:00",
            "delivery_state": "delivered",
            "sequence": 801,
            "epistemic_status": "companion_delivered_expression_record_only",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delivery_state", "speaker_ref"),
    [
        ("provider_accepted", "agent:companion"),
        ("delivered", "agent:forged"),
    ],
)
@pytest.mark.asyncio
async def test_unverified_companion_dialogue_is_not_visible_conversation_proof(
    delivery_state: str,
    speaker_ref: str,
) -> None:
    dialogue_ref = "dialogue:expression:plan:unconfirmed:beat:1"
    visible_span = "我刚才问了句项目进度。"
    reviewer = _SequenceJsonModel(
        [
            _primary_rejection(visible_span),
            _dialogue_coverage(dialogue_ref),
            _dialogue_coverage(dialogue_ref),
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_request(),
        raw=_draft(visible_span),
        identity_frame=None,
        model_visible_context_json=_recent_dialogue_context(
            dialogue_ref=dialogue_ref,
            speaker="companion",
            speaker_ref=speaker_ref,
            text="项目进度现在怎么样了？",
            delivery_state=delivery_state,
            sequence=801,
        ),
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    first_adjudication = json.loads(reviewer.calls[1][0][1]["content"])
    assert first_adjudication["typed_recent_dialogue_proof"] == []
    assert len(reviewer.calls) == 3


@pytest.mark.asyncio
async def test_counterpart_dialogue_proves_only_that_the_counterpart_reported_it() -> None:
    dialogue_ref = "dialogue:observation:older-complaint"
    visible_span = "你刚才说想找个人吐槽。"
    reviewer = _SequenceJsonModel(
        [_primary_rejection(visible_span), _dialogue_coverage(dialogue_ref)]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_request(),
        raw=_draft(visible_span),
        identity_frame=None,
        model_visible_context_json=_recent_dialogue_context(
            dialogue_ref=dialogue_ref,
            speaker="counterpart",
            speaker_ref="user:primary",
            text="今天遇到点破事，想找个人吐槽。",
            delivery_state="observed",
            sequence=701,
        ),
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    request_payload = json.loads(reviewer.calls[1][0][1]["content"])
    assert request_payload["typed_recent_dialogue_proof"][0]["epistemic_status"] == (
        "counterpart_report_record_only"
    )
    assert "cannot promote that report to objective truth" in reviewer.calls[1][0][0][
        "content"
    ]


@pytest.mark.asyncio
async def test_counterpart_intention_report_does_not_prove_a_completed_venting_event() -> None:
    dialogue_ref = "dialogue:observation:older-complaint"
    visible_span = "今天下午听你吐槽完，我一直在想这事。"
    reviewer = _SequenceJsonModel(
        [
            _primary_rejection(visible_span),
            _retained("logical_modality", "added_external_premise"),
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_request(),
        raw=_draft(visible_span),
        identity_frame=None,
        model_visible_context_json=_recent_dialogue_context(
            dialogue_ref=dialogue_ref,
            speaker="counterpart",
            speaker_ref="user:primary",
            text="今天遇到点破事，想找个人吐槽。",
            delivery_state="observed",
            sequence=701,
        ),
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.semantic_failure_dimensions == (
        "logical_modality",
        "added_external_premise",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "visible_span",
    [
        "我刚才那句确实有点端着，我承认。",
        "我刚其实在想，你今天好像心情挺不错。",
    ],
)
@pytest.mark.asyncio
async def test_complete_private_epistemic_proposition_is_not_stripped_into_a_fact(
    visible_span: str,
) -> None:
    reviewer = _SequenceJsonModel(
        [_primary_rejection(visible_span), _private_continuity()]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_request(text="我看你说话有点不自然。"),
        raw=_draft(visible_span),
        identity_frame=None,
        model_visible_context_json=json.dumps(
            {
                "actor_ref": "agent:companion",
                "slices": {
                    "recent_dialogue": {
                        "availability": "available",
                        "items": [],
                    }
                },
            }
        ),
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    request_payload = json.loads(reviewer.calls[1][0][1]["content"])
    assert request_payload["disputed_findings"][0]["visible_span"] == visible_span
    assert request_payload["proposition_locator_contract"][
        "semantic_unit"
    ] == "complete_epistemic_proposition"
    assert request_payload["proposition_locator_contract"][
        "host_keyword_classifier"
    ] is False


@pytest.mark.asyncio
async def test_dialogue_refs_must_be_returned_in_replay_causal_order() -> None:
    earlier_ref = "dialogue:expression:plan:earlier:beat:1"
    later_ref = "dialogue:observation:later"
    context = json.loads(
        _recent_dialogue_context(
            dialogue_ref=earlier_ref,
            speaker="companion",
            speaker_ref="agent:companion",
            text="项目进度现在怎么样了？",
            delivery_state="delivered",
            sequence=701,
        )
    )
    later_item = json.loads(
        _recent_dialogue_context(
            dialogue_ref=later_ref,
            speaker="counterpart",
            speaker_ref="user:primary",
            text="现在还挺高兴的。",
            delivery_state="observed",
            sequence=801,
        )
    )["slices"]["recent_dialogue"]["items"][0]
    context["slices"]["recent_dialogue"]["items"].append(later_item)
    visible_span = "因为听见你挺高兴，所以我刚才问了项目进度。"
    reversed_coverage = json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.3",
            "findings": [
                {
                    "finding_index": 0,
                    "decision": "covered_by_exact_dialogue_record",
                    "failure_dimensions": [],
                    "source_refs": [later_ref, earlier_ref],
                }
            ],
            "r": "Invalidly reversed record order.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [_primary_rejection(visible_span), reversed_coverage, reversed_coverage]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_request(),
        raw=_draft(visible_span),
        identity_frame=None,
        model_visible_context_json=json.dumps(context, ensure_ascii=False),
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    packet = json.loads(reviewer.calls[1][0][1]["content"])[
        "typed_recent_dialogue_proof"
    ]
    assert [(item["dialogue_ref"], item["sequence"]) for item in packet] == [
        (earlier_ref, 701),
        (later_ref, 801),
    ]
    assert "do not prove a causal relation or old motive" in reviewer.calls[1][0][0][
        "content"
    ]


@pytest.mark.asyncio
async def test_later_counterpart_state_cannot_cause_an_earlier_companion_question() -> None:
    earlier_ref = "dialogue:expression:plan:earlier:beat:1"
    later_ref = "dialogue:observation:later"
    context = json.loads(
        _recent_dialogue_context(
            dialogue_ref=earlier_ref,
            speaker="companion",
            speaker_ref="agent:companion",
            text="项目进度现在怎么样了？",
            delivery_state="delivered",
            sequence=701,
        )
    )
    context["slices"]["recent_dialogue"]["items"].append(
        json.loads(
            _recent_dialogue_context(
                dialogue_ref=later_ref,
                speaker="counterpart",
                speaker_ref="user:primary",
                text="现在还挺高兴的。",
                delivery_state="observed",
                sequence=801,
            )
        )["slices"]["recent_dialogue"]["items"][0]
    )
    visible_span = "因为听见你挺高兴，所以我刚才才问了项目进度。"
    reviewer = _SequenceJsonModel(
        [_primary_rejection(visible_span), _retained("temporal_relation")]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_request(),
        raw=_draft(visible_span),
        identity_frame=None,
        model_visible_context_json=json.dumps(context, ensure_ascii=False),
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.semantic_failure_dimensions == ("temporal_relation",)


@pytest.mark.asyncio
async def test_candidate_coverage_accepts_only_an_exact_typed_dialogue_record() -> None:
    dialogue_ref = "dialogue:expression:plan:previous:beat:1"
    visible_span = "我刚才问了句项目进度。"
    locator = {
        "beat_index": 0,
        "char_start": 0,
        "char_end": len(visible_span),
        "text": visible_span,
    }
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [locator],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-coverage.1",
                    "findings": [
                        {
                            "locator": locator,
                            "decision": "closed",
                            "source_relation": "exact_dialogue_record_coverage",
                            "source_refs": [dialogue_ref],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_request(),
        raw=_draft(visible_span),
        identity_frame=None,
        model_visible_context_json=_recent_dialogue_context(
            dialogue_ref=dialogue_ref,
            speaker="companion",
            speaker_ref="agent:companion",
            text="项目进度现在怎么样了？",
            delivery_state="delivered",
            sequence=801,
        ),
    )

    assert result.review is None
    coverage_request = json.loads(authority.calls[0][0][1]["content"])
    assert coverage_request["typed_recent_dialogue_proof"][0]["dialogue_ref"] == (
        dialogue_ref
    )
    assert coverage_request["proposition_locator_contract"][
        "semantic_unit"
    ] == "complete_epistemic_proposition"


def test_report_relative_v3_has_a_closed_dialogue_coordinate_schema() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )
    payload = model.request_payload(
        [
            {"role": "system", "content": "Return JSON."},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "output_contract": {
                            "contract": (
                                "report-relative-entailment-adjudication.3"
                            )
                        }
                    },
                    separators=(",", ":"),
                ),
            },
        ],
        temperature=0.0,
        json_object=True,
    )

    envelope = payload["response_format"]["json_schema"]
    assert envelope["name"] == "report_relative_entailment_adjudication_v3"
    schema = envelope["schema"]
    assert schema["additionalProperties"] is False
    finding_schema = schema["properties"]["findings"]["items"]
    assert finding_schema["additionalProperties"] is False
    assert set(finding_schema["required"]) == {
        "finding_index",
        "decision",
        "failure_dimensions",
        "source_refs",
    }
    assert "covered_by_exact_dialogue_record" in finding_schema["properties"][
        "decision"
    ]["enum"]
