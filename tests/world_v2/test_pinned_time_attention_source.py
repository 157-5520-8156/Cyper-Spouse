from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    review_expression_source_closure,
    review_expression_source_closure_appeal,
)
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.expression_draft import (
    PrivateTurnStateValidationError,
    TEXT_ONLY_EXPRESSION_CAPABILITIES,
    build_source_ref_alias_table,
    expression_hard_boundary_manifest,
    validate_expression_private_turn_state,
)
from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
)


class _TemporalSourceClosureReviewer:
    """Effect-seam reviewer that never receives turn-local private state."""

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        del temperature
        request = json.loads(messages[-1]["content"])
        assert request["output_contract"]["contract"] == "source-closure-review.7"
        assert "private_turn_state" not in request
        return json.dumps(
            {
                "ci": [],
                "v": [],
                "p": [],
                "r": "No semantic rejection beyond deterministic source authority.",
            }
        )


class _PinnedTimeAppealReviewer:
    """Reviewer that wrongly tries to promote attention-only time evidence."""

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        del temperature
        request = json.loads(messages[-1]["content"])
        assert request["output_contract"]["contract"] == "source-closure-appeal.4"
        assert request["rejected_categories"] == {"ci": [0], "v": [], "p": []}
        return json.dumps(
            {
                "ci": [],
                "v": [],
                "p": [],
                "r": (
                    "Incorrectly treats attention-only pinned time as sufficient "
                    "world-claim authority."
                ),
            }
        )


def _request_with_pinned_time() -> tuple[ModelInput, str]:
    model_content_json = compact_chat_model_facing_context(
        json.dumps(
            {
                "world_id": "world:time-attention",
                "actor_ref": "agent:companion",
                "trigger_ref": "event:observation:current",
                "world_revision": 31,
                "logical_time": "2026-07-30T01:12:00+08:00",
                "slices": {
                    "current_situation": {
                        "availability": "available",
                        "items": [
                            {
                                "item_ref": "situation:late-night",
                                "value": {
                                    "logical_time": "2026-07-30T01:12:00+08:00",
                                    "time_segment": "late_night",
                                },
                            }
                        ],
                    }
                },
            }
        )
    )
    pinned_time_ref = json.loads(model_content_json)["pinned_time"]["source_ref"]
    return (
        ModelInput(
            call_id="call:time-attention",
            attempt_id="attempt:time-attention",
            route=ModelRoute(
                tier="flash",
                reason_code="test",
                router_version="test.1",
            ),
            capsule_id="a" * 64,
            trigger_ref="event:observation:current",
            evaluated_world_revision=31,
            model_content_json=model_content_json,
            trigger_message=TriggerMessage(
                event_ref="event:observation:current",
                event_payload_hash="sha256:" + "b" * 64,
                observation_ref="observation:current",
                source_world_revision=31,
                actor="user:primary",
                channel="qq",
                reply_target="conversation:qq:c2c:owner",
                text="还没睡吗？",
            ),
        ),
        pinned_time_ref,
    )


def test_pinned_time_exposes_the_verified_local_clock_without_relabeling_utc() -> None:
    compact = json.loads(
        compact_chat_model_facing_context(
            json.dumps(
                {
                    "world_id": "world:time-attention",
                    "actor_ref": "agent:companion",
                    "trigger_ref": "event:observation:current",
                    "world_revision": 31,
                    "logical_time": "2026-07-29T17:12:00+00:00",
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": "situation:late-night",
                                    "value": {
                                        "logical_time": "2026-07-30T01:12:00+08:00",
                                        "time_segment": "late_night",
                                    },
                                }
                            ],
                        }
                    },
                }
            )
        )
    )

    assert compact["pinned_time"]["logical_time"] == "2026-07-29T17:12:00+00:00"
    assert compact["pinned_time"]["local_logical_time"] == "2026-07-30T01:12:00+08:00"
    pinned_item = compact["slices"]["pinned_time"]["items"][0]["value"]
    assert pinned_item["logical_time"] == "2026-07-29T17:12:00+00:00"
    assert pinned_item["local_logical_time"] == "2026-07-30T01:12:00+08:00"


def test_private_turn_state_can_copy_the_pinned_time_alias_but_not_forge_one() -> None:
    request, pinned_time_ref = _request_with_pinned_time()
    aliases = build_source_ref_alias_table(request=request)
    pinned_time_alias = aliases.alias_for(pinned_time_ref)
    assert pinned_time_alias == "T1"
    with pytest.raises(ValueError, match="unknown source-ref alias"):
        aliases.expand("T999")

    state = validate_expression_private_turn_state(
        value={
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "已经深夜了，我有点困，也在想要不要继续聊。",
                "attended_source_refs": [pinned_time_alias],
            },
            "timing_choice": "silent",
        },
        request=request,
        capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
        source_ref_aliases=aliases,
    )

    assert state is not None
    assert state.attended_source_refs == (pinned_time_ref,)

    forged_ref = "pinned-time:sha256:" + "0" * 64
    with pytest.raises(PrivateTurnStateValidationError) as exc_info:
        validate_expression_private_turn_state(
            value={
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "现在是清晨。",
                    "attended_source_refs": [forged_ref],
                },
                "timing_choice": "silent",
            },
            request=request,
            capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
                update={"private_turn_state_mode": "required"}
            ),
            source_ref_aliases=aliases,
        )
    assert exc_info.value.code == "private_turn_state.unpinned_source"


def test_private_turn_state_pins_proactive_context_without_an_inbound_message() -> None:
    request, pinned_time_ref = _request_with_pinned_time()
    proactive_request = request.model_copy(update={"trigger_message": None})
    aliases = build_source_ref_alias_table(request=proactive_request)

    state = validate_expression_private_turn_state(
        value={
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "这会儿想起她了，但还在掂量要不要打扰。",
                "attended_source_refs": [aliases.alias_for(pinned_time_ref)],
            },
            "timing_choice": "silent",
        },
        request=proactive_request,
        capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
        source_ref_aliases=aliases,
    )

    assert state is not None
    assert state.attended_source_refs == (pinned_time_ref,)


def test_pinned_time_alias_is_declared_attention_only_not_world_claim_authority() -> None:
    request, pinned_time_ref = _request_with_pinned_time()
    aliases = build_source_ref_alias_table(request=request)

    manifest = expression_hard_boundary_manifest(
        request=request,
        source_ref_aliases=aliases,
    )

    assert manifest["contract"] == "expression-hard-boundaries.8"
    assert manifest["private_turn_state"]["attended_source_refs"] == {
        "maximum_items": 8,
        "unique": True,
        "authority": "attention_provenance_only_not_world_fact_authority",
        "attention_only_not_fact_authority": ["T1"],
        "additional_attention_only_source_ref_aliases": {
            "T1": pinned_time_ref,
        },
    }
    assert manifest["source_ref_aliases"] == {}
    assert all("T1" not in refs for refs in manifest["world_claim_source_refs"].values())
    assert aliases.expand("T1") == pinned_time_ref


def test_temporal_alias_requires_a_full_digest_in_the_pinned_time_slice() -> None:
    request, _ = _request_with_pinned_time()
    valid_shape_outside_time_slice = "pinned-time:sha256:" + "1" * 64
    invalid_shape_inside_time_slice = "pinned-time:sha256:" + "g" * 64
    context = json.loads(request.model_content_json)
    context["slices"] = {
        "relevant_facts": {
            "availability": "available",
            "items": [
                {
                    "source_ref": valid_shape_outside_time_slice,
                    "value": {"fact": "This is not a pinned-time item."},
                }
            ],
        },
        "pinned_time": {
            "availability": "available",
            "items": [
                {
                    "source_ref": invalid_shape_inside_time_slice,
                    "attention_source_refs": [invalid_shape_inside_time_slice],
                    "value": {"time_segment": "late_night"},
                }
            ],
        },
    }
    request = request.model_copy(
        update={
            "model_content_json": json.dumps(
                context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        }
    )

    aliases = build_source_ref_alias_table(request=request)

    assert {
        aliases.alias_for(valid_shape_outside_time_slice),
        aliases.alias_for(invalid_shape_inside_time_slice),
    } == {"S1", "S2"}


def test_canonical_t1_cannot_be_stolen_by_the_temporal_alias_namespace() -> None:
    request, pinned_time_ref = _request_with_pinned_time()
    context = json.loads(request.model_content_json)
    slices = context["slices"]
    assert isinstance(slices, dict)
    slices["relevant_facts"] = {
        "availability": "available",
        "items": [
            {
                "source_ref": "T1",
                "value": {"fact": "A canonical ref may use the reserved shape."},
            }
        ],
    }
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )

    aliases = build_source_ref_alias_table(request=request)

    assert aliases.expand("T1") == "T1"
    assert aliases.alias_for(pinned_time_ref) == "T2"


def test_recall_extension_keeps_existing_temporal_aliases_stable() -> None:
    request, pinned_time_ref = _request_with_pinned_time()
    initial_aliases = build_source_ref_alias_table(request=request)
    recall_ref = "memory-candidate:sha256:" + "2" * 64
    context = json.loads(request.model_content_json)
    slices = context["slices"]
    assert isinstance(slices, dict)
    slices["active_memory_candidates"] = {
        "availability": "available",
        "items": [
            {
                "source_ref": recall_ref,
                "value": {"summary": "A source-bound recalled experience."},
            }
        ],
    }
    recalled_request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )

    extended_aliases = build_source_ref_alias_table(
        request=recalled_request,
        existing=initial_aliases,
    )

    assert extended_aliases.entries[: len(initial_aliases.entries)] == (initial_aliases.entries)
    assert extended_aliases.alias_for(pinned_time_ref) == "T1"
    assert extended_aliases.alias_for(recall_ref) == "S1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("summary", "cite_time"),
    (
        ("已经深夜了。", True),
        ("现在是清晨。", True),
        ("已经深夜了。", False),
    ),
)
async def test_source_closure_skips_silent_turn_local_private_time_state(
    summary: str,
    cite_time: bool,
) -> None:
    request, pinned_time_ref = _request_with_pinned_time()
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": summary,
                "attended_source_refs": [pinned_time_ref] if cite_time else [],
            },
            "timing_choice": "silent",
            "beats": [],
            "stance": "choose_from_current_private_state",
            "brief_rationale": "The role model owns whether to continue this turn.",
            "confidence": 7_500,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    result = await review_expression_source_closure(
        reviewer=_TemporalSourceClosureReviewer(),  # type: ignore[arg-type]
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is None


@pytest.mark.asyncio
async def test_source_closure_rejects_pinned_time_used_as_current_world_claim_even_if_reviewer_misses_it() -> (
    None
):
    request, pinned_time_ref = _request_with_pinned_time()
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "已经深夜了。",
                "attended_source_refs": [pinned_time_ref],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "已经深夜了。"}],
            "stance": "mention_the_current_time",
            "brief_rationale": "Exercise the exact temporal source boundary.",
            "confidence": 7_500,
            "world_claims": [
                {
                    "claim_text": "现在是深夜。",
                    "scope": "current_world",
                    "source_refs": [pinned_time_ref],
                }
            ],
        },
        ensure_ascii=False,
    )

    result = await review_expression_source_closure(
        reviewer=_TemporalSourceClosureReviewer(),  # type: ignore[arg-type]
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_claim_indexes == (0,)


@pytest.mark.asyncio
async def test_source_closure_appeal_cannot_promote_pinned_time_into_world_claim_authority() -> (
    None
):
    request, pinned_time_ref = _request_with_pinned_time()
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "已经深夜了。",
                "attended_source_refs": [pinned_time_ref],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "已经深夜了。"}],
            "stance": "mention_the_current_time",
            "brief_rationale": "Exercise the exact temporal source boundary.",
            "confidence": 7_500,
            "world_claims": [
                {
                    "claim_text": "现在是深夜。",
                    "scope": "current_world",
                    "source_refs": [pinned_time_ref],
                }
            ],
        },
        ensure_ascii=False,
    )
    initial = await review_expression_source_closure(
        reviewer=_TemporalSourceClosureReviewer(),  # type: ignore[arg-type]
        request=request,
        raw=raw,
        identity_frame=None,
    )
    assert initial.review is not None

    appealed = await review_expression_source_closure_appeal(
        reviewer=_PinnedTimeAppealReviewer(),  # type: ignore[arg-type]
        request=request,
        raw=raw,
        disputed_review=initial.review,
        identity_frame=None,
    )

    assert appealed.review is not None
    assert appealed.review.decision == "unsupported"
    assert appealed.review.unsupported_claim_indexes == (0,)
