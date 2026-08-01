from __future__ import annotations

import json
from hashlib import sha256

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
    _ExpressionRecoveryContextStore,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    TriggerMessage,
    ValidationTechnicalFailure,
)
from companion_daemon.world_v2.isolated_source_closure_trace import (
    BoundedSourceClosureTraceCollector,
    capture_isolated_source_closure_trace,
)


class _SequenceModel:
    model = "fixture-role"

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


def _request() -> ModelInput:
    return ModelInput(
        call_id="call:source-recovery",
        attempt_id="attempt:source-recovery",
        route=ModelRoute(tier="flash", reason_code="fixture", router_version="fixture.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:source-recovery",
        evaluated_world_revision=3,
        evaluated_deliberation_revision=2,
        evaluated_ledger_sequence=9,
        model_content_json='{"slices":{}}',
        trigger_message=TriggerMessage(
            actor="user:primary",
            channel="qq",
            text="你刚才说到哪了？",
            source_world_revision=3,
            reply_target="conversation:qq:c2c:owner",
            event_ref="event:message:1",
            observation_ref="observation:message:1",
            event_payload_hash="sha256:" + "b" * 64,
        ),
    )


def _draft(text: str) -> str:
    return json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "choose_for_this_turn",
            "brief_rationale": "Fixture role choice.",
            "confidence": 7_500,
            "world_claims": [],
        },
        ensure_ascii=False,
    )


def _source_reselection(text: str) -> str:
    """Return the negotiated realtime wire used only by corrective calls."""

    return json.dumps(
        {
            "expression_draft": {
                "private_turn_state": None,
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [
                    {
                        "modality": "text",
                        "text": text,
                        "reaction_id": None,
                        "sticker_id": None,
                    }
                ],
                "delay_position_bp": None,
                "expires_after_seconds": None,
                "stance": "choose_for_this_turn",
                "brief_rationale": "Fixture role choice.",
                "impulse_summary": None,
                "confidence": 7_500,
                "variation_profile": None,
                "response_expectation": None,
                "response_expectation_assessment": None,
                "world_claims": [],
            },
            "episode_disposition": None,
        },
        ensure_ascii=False,
    )


def _review(
    *,
    boundaries: tuple[str, ...] = (),
    visible_text_failures: tuple[str, ...] | None = None,
    private_turn_state_failures: tuple[str, ...] | None = None,
    visible_span: str = "我刚洗完澡",
) -> str:
    visible = (
        visible_text_failures
        if visible_text_failures is not None
        else (
            ("occurrence_or_status_authority_mismatch",)
            if "visible_text" in boundaries
            else ()
        )
    )
    private = (
        private_turn_state_failures
        if private_turn_state_failures is not None
        else (
            ("occurrence_or_status_authority_mismatch",)
            if "private_turn_state" in boundaries
            else ()
        )
    )
    return json.dumps(
        {
            "ci": [],
            "v": list(visible),
            "p": list(private),
            "visible_findings": [
                {
                    "category": category,
                    "visible_span": visible_span,
                    "claim_index": None,
                    "source_relation": "unclosed",
                    "source_refs": [],
                }
                for category in dict.fromkeys((*visible, *private))
            ],
            "r": "The visible candidate contains an unclosed external occurrence.",
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_backup_inherits_only_final_rejected_candidate_hash_and_categories() -> None:
    rejected = _draft("我刚洗完澡。")
    accepted = _draft("我刚才没接好，现在重新说。")
    store = _ExpressionRecoveryContextStore()
    primary_model = _SequenceModel([rejected, _source_reselection("我刚洗完澡。")])
    backup_model = _SequenceModel([accepted])
    reviewer = _SequenceModel(
        [
            _review(boundaries=("visible_text",)),
            _review(boundaries=("visible_text",)),
            _review(),
        ]
    )
    primary = ChatModelDeliberationAdapter(
        model=primary_model,
        source_closure_reviewer=reviewer,
        recovery_context_store=store,
    )
    backup = ChatModelDeliberationAdapter(
        model=backup_model,
        source_closure_reviewer=reviewer,
        recovery_context_store=store,
    )
    request = _request()
    trace = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(trace):
        with pytest.raises(
            ValidationTechnicalFailure,
            match="authored_expression_reselection_invalid",
        ):
            await primary.propose(request)
    output = await backup.recover(request, "corrective_invalid")

    assert output.raw_proposal["action_intents"]
    assert len(primary_model.calls) == 2
    assert len(backup_model.calls) == 1
    recovery_user = json.loads(backup_model.calls[0][0][1]["content"])
    inherited = recovery_user["prior_source_closure_failure"]
    assert inherited["contract"] == "source-closure-recovery-failure.2"
    assert inherited["authority"] == "categorical_failure_only_not_context_or_evidence"
    assert inherited["failure_stage"] == "corrected_candidate_final_rejection"
    final_rejection = next(
        event for event in trace.snapshot() if event.stage == "corrected_rejection"
    )
    assert inherited["rejected_candidate_sha256"] == final_rejection.candidate_sha256
    assert inherited["rejected_candidate_sha256"] != sha256(
        rejected.encode("utf-8")
    ).hexdigest()
    assert inherited["rejected_categories"] == {
        "ci": [],
        "v": ["occurrence_or_status_authority_mismatch"],
        "p": [],
    }
    rendered_backup_request = json.dumps(
        backup_model.calls[0][0],
        ensure_ascii=False,
    )
    assert "我刚洗完澡" not in rendered_backup_request
    assert "untrusted_draft_json" not in rendered_backup_request
    assert "untrusted_draft_source_ref_aliases" not in rendered_backup_request
    assert "The visible candidate contains an unclosed external occurrence." not in (
        rendered_backup_request
    )


@pytest.mark.asyncio
async def test_backup_recover_emits_initial_rejection_to_isolated_trace() -> None:
    rejected = _draft("我刚洗完澡。")
    model = _SequenceModel([rejected, _source_reselection("我现在重新接住这句话。")])
    reviewer = _SequenceModel(
        [
            _review(boundaries=("visible_text",)),
            _review(),
        ]
    )
    backup = ChatModelDeliberationAdapter(
        model=model,
        source_closure_reviewer=reviewer,
    )
    trace = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(trace):
        output = await backup.recover(_request(), "primary_source_closure_rejected")

    assert output.raw_proposal["action_intents"]
    assert len(model.calls) == 2
    assert len(reviewer.calls) == 2
    assert [event.stage for event in trace.snapshot()] == [
        "initial_rejection"
    ]
    assert trace.snapshot()[0].visible_beat_texts == ("我刚洗完澡。",)


@pytest.mark.asyncio
async def test_source_rejection_is_not_inherited_by_a_different_attempt_identity() -> None:
    rejected = _draft("我刚洗完澡。")
    accepted = _draft("我现在想重新接住这句话。")
    store = _ExpressionRecoveryContextStore()
    primary = ChatModelDeliberationAdapter(
        model=_SequenceModel([rejected, rejected]),
        source_closure_reviewer=_SequenceModel(
            [
                _review(boundaries=("visible_text",)),
                _review(boundaries=("visible_text",)),
                _review(boundaries=("visible_text",)),
                _review(boundaries=("visible_text",)),
            ]
        ),
        recovery_context_store=store,
    )
    request = _request()

    with pytest.raises(ValidationTechnicalFailure):
        await primary.propose(request)

    backup_model = _SequenceModel([accepted])
    backup = ChatModelDeliberationAdapter(
        model=backup_model,
        source_closure_reviewer=_SequenceModel([_review()]),
        recovery_context_store=store,
    )
    changed = request.model_copy(
        update={
            "call_id": "call:different-attempt",
            "attempt_id": "attempt:different",
        }
    )
    await backup.recover(changed, "corrective_invalid")

    recovery_user = json.loads(backup_model.calls[0][0][1]["content"])
    assert "prior_source_closure_failure" not in recovery_user


@pytest.mark.asyncio
@pytest.mark.parametrize("release_method", ["accept_candidate", "discard_candidate"])
async def test_released_candidate_does_not_leave_rejected_bytes_for_recovery(
    release_method: str,
) -> None:
    rejected = _draft("我刚洗完澡。")
    store = _ExpressionRecoveryContextStore()
    primary = ChatModelDeliberationAdapter(
        model=_SequenceModel([rejected, rejected]),
        source_closure_reviewer=_SequenceModel(
            [
                _review(boundaries=("visible_text",)),
                _review(boundaries=("visible_text",)),
                _review(boundaries=("visible_text",)),
                _review(boundaries=("visible_text",)),
            ]
        ),
        recovery_context_store=store,
    )
    request = _request()
    with pytest.raises(ValidationTechnicalFailure):
        await primary.propose(request)

    getattr(primary, release_method)(request)
    backup_model = _SequenceModel([_draft("我现在重新说。")])
    backup = ChatModelDeliberationAdapter(
        model=backup_model,
        source_closure_reviewer=_SequenceModel([_review()]),
        recovery_context_store=store,
    )
    await backup.recover(request, "corrective_invalid")

    recovery_user = json.loads(backup_model.calls[0][0][1]["content"])
    assert "prior_source_closure_failure" not in recovery_user
