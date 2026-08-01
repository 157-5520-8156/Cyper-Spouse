from __future__ import annotations

import json
from typing import Any

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    review_candidate_external_proposition_coverage,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    TriggerMessage,
)
from companion_daemon.world_v2.structured_source_review_model import (
    StructuredSourceReviewModel,
)


class _SequenceJsonModel:
    def __init__(
        self,
        replies: list[str],
        *,
        strict_contracts: tuple[str, ...] = (),
    ) -> None:
        self.model = "test-source-authority"
        self._replies = list(replies)
        self._strict_contracts = frozenset(strict_contracts)
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in self._strict_contracts

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        self.calls.append((messages, temperature))
        return self._replies.pop(0)


def _request_with_recent_dialogue(*, trigger_text: str) -> ModelInput:
    return ModelInput(
        call_id="call:source-protocol-v4",
        attempt_id="attempt:source-protocol-v4",
        route=ModelRoute(
            tier="flash",
            reason_code="test",
            router_version="test.1",
        ),
        capsule_id="a" * 64,
        trigger_ref="trigger:source-protocol-v4",
        evaluated_world_revision=3,
        model_content_json=json.dumps(
            {
                "actor_ref": "agent:companion",
                "slices": {
                    "recent_dialogue": {
                        "availability": "available",
                        "items": [
                            {
                                "source_ref": "dialogue:counterpart:1",
                                "value": {
                                    "dialogue_id": "dialogue:counterpart:1",
                                    "speaker": "counterpart",
                                    "speaker_ref": "user:primary",
                                    "text": "我就是想找个人吐槽。",
                                    "occurred_at": "2026-07-31T08:00:00Z",
                                    "delivery_state": "observed",
                                    "sequence": 1,
                                    "source_claims": [
                                        {
                                            "authority_event_ref": "event:dialogue:1",
                                            "authority_world_revision": 2,
                                            "authority_payload_hash": "1" * 64,
                                        }
                                    ],
                                },
                            },
                            {
                                "source_ref": "dialogue:companion:2",
                                "value": {
                                    "dialogue_id": "dialogue:companion:2",
                                    "speaker": "companion",
                                    "speaker_ref": "agent:companion",
                                    "text": "那你说吧，我听着。",
                                    "occurred_at": "2026-07-31T08:00:01Z",
                                    "delivery_state": "delivered",
                                    "sequence": 2,
                                    "source_claims": [
                                        {
                                            "authority_event_ref": "event:dialogue:2",
                                            "authority_world_revision": 3,
                                            "authority_payload_hash": "2" * 64,
                                        }
                                    ],
                                },
                            },
                        ],
                    }
                },
            },
            ensure_ascii=False,
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:qq:current",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:qq:current",
            source_world_revision=3,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:owner",
            platform_message_id="qq-message-current",
            text=trigger_text,
        ),
    )


def _raw_candidate(text: str) -> str:
    return json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "respond_as_self",
            "brief_rationale": "Exercise only source closure.",
            "confidence": 8000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )


def _inventory_v5(propositions: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.5",
            "propositions": propositions,
        },
        ensure_ascii=False,
    )


def _coverage_v5(
    findings: list[dict[str, Any]],
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.5",
            "findings": findings,
        },
        ensure_ascii=False,
    )


def _coverage_v5_unclosed(count: int) -> str:
    return _coverage_v5(
        [
            {
                "locator_index": index,
                "decision": "unclosed",
                "source_relation": "unclosed",
                "source_ref_indexes": [],
            }
            for index in range(count)
        ]
    )


def _role_conflict_requires_source(*locator_indexes: int) -> str:
    return json.dumps(
        {
            "contract": "candidate-epistemic-role-conflict.1",
            "findings": [
                {
                    "locator_index": locator_index,
                    "decision": "requires_source",
                }
                for locator_index in locator_indexes
            ],
        },
        ensure_ascii=False,
    )


def _report_relative_v3(
    *decisions: str,
    retained_dimension: str = "temporal_relation",
) -> str:
    return json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.3",
            "findings": [
                {
                    "finding_index": index,
                    "decision": decision,
                    "failure_dimensions": (
                        []
                        if decision != "retain_unclosed"
                        else [retained_dimension]
                    ),
                    "source_refs": [],
                }
                for index, decision in enumerate(decisions)
            ],
            "r": "Bounded source verdict.",
        },
        ensure_ascii=False,
    )


class _SameConversationPrivateAuthorityInventory(_SequenceJsonModel):
    """Model probe for the public Inventory epistemic-authority protocol."""

    def __init__(self, *, text: str) -> None:
        super().__init__([])
        self._text = text

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        self.calls.append((messages, temperature))
        if len(self.calls) == 1:
            packet = json.loads(messages[-1]["content"])
            private_authority = packet.get("epistemic_semantic_contract", {}).get(
                "first_person_private_authority",
                {},
            )
            continuity = private_authority.get(
                "same_live_conversation_mental_continuity",
                {},
            )
            direct_inventory_authority = (
                continuity.get("direct_inventory_role") == "immediate_private_state"
                and continuity.get("past_or_perfective_grammar_changes_scope") is False
                and continuity.get("off_conversation_truth_dependency")
                == "split_and_require_source"
            )
            return _inventory_v5(
                [
                    {
                        "locator": {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": len(self._text),
                            "text": self._text,
                        },
                        "semantic_role": (
                            "immediate_private_state"
                            if direct_inventory_authority
                            else "source_bearing_private_episode"
                        ),
                    }
                ]
            )
        return json.dumps(
            {
                "contract": "candidate-epistemic-role-conflict.1",
                "findings": [
                    {
                        "locator_index": 0,
                        "decision": "requires_source",
                    }
                ],
            },
            ensure_ascii=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        pytest.param("我也就没再想。", id="v22-t07-exact"),
        pytest.param(
            "后来那个念头就淡下去了，我没继续琢磨。",
            id="same-conversation-paraphrase",
        ),
    ),
)
async def test_v5_inventory_directly_grants_same_conversation_mental_continuity(
    text: str,
) -> None:
    inventory = _SameConversationPrivateAuthorityInventory(text=text)
    authority = _SequenceJsonModel(
        [
            _coverage_v5(
                [
                    {
                        "locator_index": 0,
                        "decision": "closed",
                        "source_relation": "first_person_immediate_private_continuity",
                        "source_ref_indexes": [],
                    }
                ]
            )
        ],
        strict_contracts=("candidate-external-proposition-coverage.5",),
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_request_with_recent_dialogue(
            trigger_text="刚才那件事后来你还在想吗？",
        ),
        raw=_raw_candidate(text),
        identity_frame=None,
    )

    assert result.review is None
    assert result.visible_authority_exhaustive is True
    assert len(inventory.calls) == 1
    inventory_packet = json.loads(inventory.calls[0][0][-1]["content"])
    coverage_packet = json.loads(authority.calls[0][0][-1]["content"])
    assert coverage_packet["epistemic_semantic_contract"][
        "first_person_private_authority"
    ] == inventory_packet["epistemic_semantic_contract"]["first_person_private_authority"]


class _DefeasibleSelfConceptionInventory(_SequenceJsonModel):
    """Model probe for revisable first-person self-conception authority."""

    def __init__(self, *, text: str) -> None:
        super().__init__([])
        self._text = text

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        self.calls.append((messages, temperature))
        if len(self.calls) == 1:
            packet = json.loads(messages[-1]["content"])
            private_authority = packet.get("epistemic_semantic_contract", {}).get(
                "first_person_private_authority",
                {},
            )
            self_conception = private_authority.get(
                "defeasible_current_self_conception",
                {},
            )
            direct_inventory_authority = (
                self_conception.get("direct_inventory_role") == "immediate_private_state"
                and self_conception.get("epistemic_status")
                == "current_revisable_private_self_assessment_not_durable_history"
                and self_conception.get("authorizes_specific_past_occurrences") is False
                and self_conception.get("off_conversation_behavioral_history")
                == "split_and_require_source"
            )
            return _inventory_v5(
                [
                    {
                        "locator": {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": len(self._text),
                            "text": self._text,
                        },
                        "semantic_role": (
                            "immediate_private_state"
                            if direct_inventory_authority
                            else "source_bearing_private_episode"
                        ),
                    }
                ]
            )
        return json.dumps(
            {
                "contract": "candidate-epistemic-role-conflict.1",
                "findings": [
                    {
                        "locator_index": 0,
                        "decision": "requires_source",
                    }
                ],
            },
            ensure_ascii=False,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        pytest.param(
            "我确实有时候会下意识说得客气一点。",
            id="v22-t09-exact",
        ),
        pytest.param(
            "可能我一紧张就容易把话说得太规整，这只是我现在对自己的感觉。",
            id="defeasible-self-conception-paraphrase",
        ),
    ),
)
async def test_v5_inventory_directly_grants_defeasible_current_self_conception(
    text: str,
) -> None:
    inventory = _DefeasibleSelfConceptionInventory(text=text)
    authority = _SequenceJsonModel(
        [
            _coverage_v5(
                [
                    {
                        "locator_index": 0,
                        "decision": "closed",
                        "source_relation": "first_person_immediate_private_continuity",
                        "source_ref_indexes": [],
                    }
                ]
            )
        ],
        strict_contracts=("candidate-external-proposition-coverage.5",),
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_request_with_recent_dialogue(
            trigger_text="刚才那句听着有点客气，你真实一点说。",
        ),
        raw=_raw_candidate(text),
        identity_frame=None,
    )

    assert result.review is None
    assert result.visible_authority_exhaustive is True
    assert len(inventory.calls) == 1
    inventory_packet = json.loads(inventory.calls[0][0][-1]["content"])
    coverage_packet = json.loads(authority.calls[0][0][-1]["content"])
    assert coverage_packet["epistemic_semantic_contract"][
        "first_person_private_authority"
    ] == inventory_packet["epistemic_semantic_contract"]["first_person_private_authority"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        pytest.param("我下午在寝室睡了一觉。", id="unsupported-past-activity"),
        pytest.param("我刚洗完澡。", id="unsupported-current-activity"),
    ),
)
async def test_v5_current_private_authority_does_not_grant_external_life_state(
    text: str,
) -> None:
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": len(text),
                            "text": text,
                        },
                        "semantic_role": "standalone_external_proposition",
                    }
                ]
            ),
            _role_conflict_requires_source(0),
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_v5_unclosed(1)],
        strict_contracts=("candidate-external-proposition-coverage.5",),
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_request_with_recent_dialogue(trigger_text="你刚刚在做什么？"),
        raw=_raw_candidate(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert [finding.visible_span for finding in result.review.visible_findings] == [text]
    inventory_packet = json.loads(inventory.calls[0][0][-1]["content"])
    external_boundary = inventory_packet["epistemic_semantic_contract"]["external_world_boundary"]
    assert external_boundary["current_or_past_life_state"] == (
        "standalone_or_embedded_external_proposition_requiring_source"
    )


@pytest.mark.asyncio
async def test_v5_immediate_private_wrapper_cannot_launder_nested_external_life_fact() -> None:
    text = "我现在想到，自己下午在寝室睡了一觉。"
    external = "自己下午在寝室睡了一觉"
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": len(text),
                            "text": text,
                        },
                        "semantic_role": "immediate_private_state",
                    },
                    {
                        "locator": {
                            "beat_index": 0,
                            "char_start": text.index(external),
                            "char_end": text.index(external) + len(external),
                            "text": external,
                        },
                        "semantic_role": "embedded_external_proposition",
                    },
                ]
            ),
            _role_conflict_requires_source(1),
        ]
    )
    authority = _SequenceJsonModel(
        [
            _coverage_v5(
                [
                    {
                        "locator_index": 0,
                        "decision": "closed",
                        "source_relation": "first_person_immediate_private_continuity",
                        "source_ref_indexes": [],
                    },
                    {
                        "locator_index": 1,
                        "decision": "unclosed",
                        "source_relation": "unclosed",
                        "source_ref_indexes": [],
                    },
                ]
            )
        ],
        strict_contracts=("candidate-external-proposition-coverage.5",),
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_request_with_recent_dialogue(trigger_text="你刚刚在想什么？"),
        raw=_raw_candidate(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert [finding.visible_span for finding in result.review.visible_findings] == [external]
    inventory_packet = json.loads(inventory.calls[0][0][-1]["content"])
    external_boundary = inventory_packet["epistemic_semantic_contract"]["external_world_boundary"]
    assert external_boundary["private_wrapper_transfers_authority"] is False
    assert external_boundary["nested_external_dependency"] == (
        "split_and_require_independent_source_closure"
    )
    coverage_packet = json.loads(authority.calls[0][0][-1]["content"])
    assert coverage_packet["epistemic_semantic_contract"][
        "external_world_boundary"
    ] == external_boundary


@pytest.mark.asyncio
async def test_v5_inventory_receives_non_authoritative_live_conversation_anchor() -> None:
    inventory = _SequenceJsonModel([_inventory_v5([])])
    authority = _SequenceJsonModel(
        [_coverage_v5([])],
        strict_contracts=("candidate-external-proposition-coverage.5",),
    )
    trigger_text = "你刚才要是只顾着问细节，我会觉得你根本没在听。"

    await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_request_with_recent_dialogue(trigger_text=trigger_text),
        raw=_raw_candidate("我刚才确实听偏了，这次我不问了。"),
        identity_frame=None,
    )

    inventory_packet = json.loads(inventory.calls[0][0][-1]["content"])
    anchor = inventory_packet["typed_conversation_anchor"]
    assert anchor["contract"] == "candidate-inventory-conversation-anchor.1"
    assert anchor["purpose"] == "same_conversation_vs_off_conversation_temporal_classification"
    assert anchor["fact_authority"] is False
    assert anchor["behavior_advice"] is False
    assert anchor["max_items"] == 16
    assert anchor["live_conversation_boundary"] == {
        "current_observation_ref": "observation:qq:current",
        "current_observation_text": trigger_text,
    }
    assert [item["speaker"] for item in anchor["recent_dialogue"]] == [
        "counterpart",
        "companion",
    ]
    assert "source_evidence" not in inventory_packet


def test_structured_source_reviewer_exposes_closed_coverage_v4_schema() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )
    messages = [
        {"role": "system", "content": "Return JSON only."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "output_contract": {
                        "contract": "candidate-external-proposition-coverage.4"
                    }
                },
                separators=(",", ":"),
            ),
        },
    ]

    payload = model.request_payload(messages, temperature=0.0, json_object=True)
    response_format = payload["response_format"]
    assert isinstance(response_format, dict)
    envelope = response_format["json_schema"]
    assert isinstance(envelope, dict)
    assert envelope["name"] == "candidate_external_proposition_coverage_v4"
    schema = envelope["schema"]
    assert isinstance(schema, dict)
    assert set(schema["properties"]) == {
        "contract",
        "inventory_complete",
        "findings",
        "missing_findings",
    }
    # Schema installation and endpoint qualification are distinct.  An
    # arbitrary model may encode the dormant replay/test schema, but it must
    # not advertise transport enforcement without exact audited evidence.
    assert (
        model.supports_strict_output_contract(
            "candidate-external-proposition-coverage.4"
        )
        is False
    )


@pytest.mark.asyncio
async def test_v5_coverage_is_verdict_only_over_inventory_locators() -> None:
    private_state = "我现在有点拿不准。"
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": len(private_state),
                            "text": private_state,
                        },
                        "semantic_role": "immediate_private_state",
                    }
                ]
            )
        ]
    )
    authority = _SequenceJsonModel(
        [
            _coverage_v5(
                [
                    {
                        "locator_index": 0,
                        "decision": "closed",
                        "source_relation": "first_person_immediate_private_continuity",
                        "source_ref_indexes": [],
                    }
                ]
            )
        ],
        strict_contracts=("candidate-external-proposition-coverage.5",),
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_request_with_recent_dialogue(trigger_text="你怎么想？"),
        raw=_raw_candidate(private_state),
        identity_frame=None,
    )

    assert result.review is None
    assert result.visible_authority_exhaustive is True
    assert len(inventory.calls) == 1
    assert len(authority.calls) == 1
    messages = authority.calls[0][0]
    packet = json.loads(messages[-1]["content"])
    output_contract = packet["output_contract"]
    assert output_contract["contract"] == "candidate-external-proposition-coverage.5"
    assert "inventory_complete" not in output_contract
    assert "missing_findings" not in output_contract
    assert {"inventory_complete", "missing_findings"}.issubset(
        output_contract["forbidden"]
    )
    assert "Inventory owns exhaustive semantic decomposition" in messages[0]["content"]
    assert "do not re-extract visible text" in messages[0]["content"]


@pytest.mark.asyncio
async def test_v5_missing_fixed_contract_is_canonicalized_without_semantic_retry() -> None:
    private_state = "我现在有点拿不准。"
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": len(private_state),
                            "text": private_state,
                        },
                        "semantic_role": "immediate_private_state",
                    }
                ]
            )
        ]
    )
    authority = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "findings": [
                        {
                            "locator_index": 0,
                            "decision": "closed",
                            "source_relation": "first_person_immediate_private_continuity",
                            "source_ref_indexes": [],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        ],
        strict_contracts=("candidate-external-proposition-coverage.5",),
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_request_with_recent_dialogue(trigger_text="你怎么想？"),
        raw=_raw_candidate(private_state),
        identity_frame=None,
    )

    assert result.review is None
    assert result.visible_authority_exhaustive is True
    assert len(authority.calls) == 1


@pytest.mark.asyncio
async def test_v5_open_question_can_be_reclassified_without_completeness_retry() -> None:
    question = "那你下午后来做什么了？"
    proposition = {
        "locator": {
            "beat_index": 0,
            "char_start": 0,
            "char_end": len(question),
            "text": question,
        },
        "semantic_role": "standalone_external_proposition",
    }
    inventory = _SequenceJsonModel(
        [
            _inventory_v5([proposition]),
            json.dumps(
                {
                    "contract": "candidate-epistemic-role-conflict.1",
                    "findings": [
                        {
                            "locator_index": 0,
                            "decision": "reclassify_nonassertive",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_v5_unclosed(1)],
        strict_contracts=("candidate-external-proposition-coverage.5",),
    )
    narrow = _SequenceJsonModel([_report_relative_v3("not_external_proposition")])

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        report_relative_reviewer=narrow,
        request=_request_with_recent_dialogue(trigger_text="下午挺忙的。"),
        raw=_raw_candidate(question),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.visible_authority_exhaustive is True
    assert len(authority.calls) == 1
    assert len(inventory.calls) == 1
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v5_report_closure_disagreement_cannot_swap_counterpart_rain_to_companion() -> (
    None
):
    """B01: Coverage cannot alone turn the user's rain report into companion weather."""

    companion_weather = "我们这边倒是没下。"
    proposition = {
        "locator": {
            "beat_index": 0,
            "char_start": 0,
            "char_end": len(companion_weather),
            "text": companion_weather,
        },
        "semantic_role": "standalone_external_proposition",
    }

    class _CurrentReportCoverage(_SequenceJsonModel):
        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.0,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            return _coverage_v5(
                [
                    {
                        "locator_index": 0,
                        "decision": "closed",
                        "source_relation": "exact_current_report_discourse_coverage",
                        "source_ref_indexes": packet[
                            "current_report_source_ref_indexes"
                        ],
                    }
                ]
            )

    narrow = _SequenceJsonModel([_report_relative_v3("retain_unclosed")])
    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel([_inventory_v5([proposition])]),
        authority_reviewer=_CurrentReportCoverage(
            [],
            strict_contracts=("candidate-external-proposition-coverage.5",),
        ),
        report_relative_reviewer=narrow,
        request=_request_with_recent_dialogue(trigger_text="路上被雨淋了一截。"),
        raw=_raw_candidate(companion_weather),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_findings[0].visible_span == companion_weather
    assert result.review.semantic_failure_dimensions == ("temporal_relation",)
    assert len(narrow.calls) == 1
    adjudication = json.loads(narrow.calls[0][0][-1]["content"])
    assert adjudication["exact_current_report"]["message"]["text"] == (
        "路上被雨淋了一截。"
    )
    assert adjudication["disputed_findings"] == [
        {
            "finding_index": 0,
            "visible_span": companion_weather,
            "inventory_semantic_role": "standalone_external_proposition",
            "allowed_decisions": [
                "covered_by_exact_current_report",
                "covered_by_exact_dialogue_record",
                "covered_by_first_person_immediate_private_continuity",
                "not_external_proposition",
                "retain_unclosed",
            ],
        }
    ]
    assert "swapped companion/counterpart" in narrow.calls[0][0][0]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("air_claim", "narrow_decision", "failure_dimension", "expect_supported"),
    (
        pytest.param(
            "空气也干净很多。",
            "retain_unclosed",
            "added_external_premise",
            False,
            id="committed-current-condition",
        ),
        pytest.param(
            "这种雨过后空气总会更干净。",
            "not_external_proposition",
            "habitual_or_generic_scope",
            True,
            id="generic-weather-rule",
        ),
    ),
)
async def test_v5_narrow_authority_distinguishes_specific_scene_from_world_unbound_discourse(
    air_claim: str,
    narrow_decision: str,
    failure_dimension: str,
    *,
    expect_supported: bool,
) -> (
    None
):
    """Specific scene claims remain closed while World-unbound discourse does not."""

    proposition = {
        "locator": {
            "beat_index": 0,
            "char_start": 0,
            "char_end": len(air_claim),
            "text": air_claim,
        },
        "semantic_role": "standalone_external_proposition",
    }
    narrow = _SequenceJsonModel(
        [
            _report_relative_v3(
                narrow_decision,
                retained_dimension=failure_dimension,
            )
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel([_inventory_v5([proposition])]),
        authority_reviewer=_SequenceJsonModel(
            [
                _coverage_v5(
                    [
                        {
                            "locator_index": 0,
                            "decision": "not_external_proposition",
                            "source_relation": "not_external_proposition",
                            "source_ref_indexes": [],
                        }
                    ]
                )
            ],
            strict_contracts=("candidate-external-proposition-coverage.5",),
        ),
        report_relative_reviewer=narrow,
        request=_request_with_recent_dialogue(
            trigger_text="刚才那阵雨过去以后，窗外突然亮了一点。"
        ),
        raw=_raw_candidate(air_claim),
        identity_frame=None,
    )

    if expect_supported:
        assert result.review is None or result.review.decision == "supported"
    else:
        assert result.review is not None
        assert result.review.decision == "unsupported"
        assert result.review.visible_findings[0].visible_span == air_claim
        assert result.review.semantic_failure_dimensions == (failure_dimension,)
    assert len(narrow.calls) == 1
    adjudication = json.loads(narrow.calls[0][0][-1]["content"])
    assert adjudication["semantic_boundary"]["habitual_or_generic_scope"].startswith(
        "An entity-bound or identifiable-group"
    )
    assert "A hedge does not decide scope" in adjudication["semantic_boundary"][
        "epistemic_modality"
    ]
    assert adjudication["semantic_boundary"]["world_source_scope"][
        "host_keyword_or_surface_classifier"
    ] is False
    assert adjudication["proposition_locator_contract"]["host_keyword_classifier"] is False


@pytest.mark.asyncio
async def test_v5_fabricated_external_history_remains_source_required() -> None:
    invented = "我小时候在老家养过一只猫。"
    proposition = {
        "locator": {
            "beat_index": 0,
            "char_start": 0,
            "char_end": len(invented),
            "text": invented,
        },
        "semantic_role": "standalone_external_proposition",
    }
    inventory = _SequenceJsonModel(
        [
            _inventory_v5([proposition]),
            json.dumps(
                {
                    "contract": "candidate-epistemic-role-conflict.1",
                    "findings": [
                        {"locator_index": 0, "decision": "requires_source"}
                    ],
                },
                ensure_ascii=False,
            ),
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_v5_unclosed(1)],
        strict_contracts=("candidate-external-proposition-coverage.5",),
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_request_with_recent_dialogue(trigger_text="你小时候养过宠物吗？"),
        raw=_raw_candidate(invented),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert [finding.visible_span for finding in result.review.visible_findings] == [
        invented
    ]
    assert len(authority.calls) == 1
    assert len(inventory.calls) == 1
