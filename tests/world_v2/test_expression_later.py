from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    _proposal_from_model_text,
)
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.expression_draft import TEXT_ONLY_EXPRESSION_CAPABILITIES


def test_later_text_beats_preserve_the_models_multi_message_expression() -> None:
    """A deferred return keeps the model's chosen message count and wording."""

    request = ModelInput(
        call_id="call:merge-probe",
        attempt_id="attempt:merge-probe",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:merge-probe",
        evaluated_world_revision=3,
        model_content_json=json.dumps(
            {"logical_time": datetime(2026, 7, 20, 2, 30, tzinfo=UTC).isoformat(), "slices": {}}
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:merge",
            event_payload_hash="sha256:" + "c" * 64,
            observation_ref="observation:merge",
            source_world_revision=3,
            actor="user:primary",
            channel="test",
            reply_target="user:primary",
            text="还在吗？",
        ),
    )
    raw = json.dumps(
        {
            "timing_choice": "later",
            "delay_seconds": 21_600,
            "expires_after_seconds": 43_200,
            "beats": [
                {"modality": "text", "text": "我想晚点再接着说"},
                {"modality": "text", "text": "等我理一理"},
            ],
            "stance": "hold_for_now",
            "brief_rationale": "I want to return later rather than answer now.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    proposal = _proposal_from_model_text(
        raw=raw,
        request=request,
        capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES,
        quick_recovery=False,
    )

    assert proposal["timing_choice"] == "later"
    payload = json.loads(proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert [item["inline_text"] for item in payload["beat_drafts"]] == [
        "我想晚点再接着说",
        "等我理一理",
    ]
    assert proposal["action_intents"][0]["kind"] == "followup"

    with pytest.raises(ValueError, match="deferred-effect limit|text modality"):
        _proposal_from_model_text(
            raw=json.dumps(
                {
                    "timing_choice": "later",
                    "delay_seconds": 1_200,
                    "expires_after_seconds": 7_200,
                    "beats": [
                        {"modality": "typing"},
                        {"modality": "text", "text": "等我一下"},
                    ],
                    "stance": "hold_for_now",
                    "brief_rationale": "A non-text effect is not installed for later delivery.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            request=request,
            capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES,
            quick_recovery=False,
        )
