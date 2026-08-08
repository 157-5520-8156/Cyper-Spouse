from __future__ import annotations
import json


from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    TriggerMessage,
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
