from __future__ import annotations

import json
from hashlib import sha256


from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    ModelUsageProvenance,
    TriggerMessage,
)
from companion_daemon.world_v2.proposal_envelope import ProposalEvidenceRef


def _usage(*, ref: str, input_tokens: int, output_tokens: int) -> ModelUsageProvenance:
    material = {
        "usage_contract": "model-usage.1",
        "route_class": "chat",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": 0,
        "token_provenance": "provider_reported",
        "transport": "provider_api",
        "provider": "stream-tail-audit-fixture",
        "provider_usage_ref": ref,
    }
    digest = sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ModelUsageProvenance(**material, provider_usage_hash=digest)


def _request() -> ModelInput:
    return ModelInput(
        call_id="call:character-interior-stream-audit",
        attempt_id="attempt:character-interior-stream-audit",
        route=ModelRoute(
            tier="flash",
            reason_code="ordinary",
            router_version="test.1",
        ),
        capsule_id="a" * 64,
        trigger_ref="event:observation:stream-audit",
        evaluated_world_revision=3,
        model_content_json=json.dumps({"world_revision": 3}),
        trigger_evidence=(
            ProposalEvidenceRef(
                ref_id="observation:stream-audit",
                evidence_kind="observed_message",
                source_world_revision=3,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:stream-audit",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:stream-audit",
            source_world_revision=3,
            actor="user:primary",
            channel="qq_c2c",
            reply_target="qq:user:1",
            text="我在听。",
        ),
    )


class _InteriorStreamModel:
    model = "fixture:character-interior-stream"
    reports_exact_request_emission = True

    def __init__(self) -> None:
        self.calls = 0

    async def complete_json_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ) -> tuple[str, ModelUsageProvenance]:
        del messages, temperature
        self.calls += 1
        raw = json.dumps(
            {
                "protocol": "character-interior-events.1",
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "stay_present",
                    "stance": "present",
                    "display_strategy": "natural",
                    "confidence": 6_000,
                },
                "events": [
                    {
                        "type": "head",
                        "timing_choice": "now",
                        "beat": {"modality": "text", "text": "先说第一句。"},
                        "cadence": "conversational",
                        "stance": "continue_in_two_bubbles",
                        "brief_rationale": "I chose two separate bubbles.",
                        "confidence": 7_800,
                        "world_claims": [],
                    },
                    {
                        "type": "beat",
                        "beat": {"modality": "text", "text": "再接第二句。"},
                        "world_claims": [],
                    },
                    {"type": "end"},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        boundary = raw.index(',{"type":"beat"')
        if on_text_delta is not None:
            on_text_delta(raw[:boundary])
            on_text_delta(raw[boundary:])
        return raw, _usage(
            ref="usage:physical-character-stream",
            input_tokens=20,
            output_tokens=8,
        )


class _TailReviewer:
    model = "fixture:tail-source-reviewer"

    def __init__(self, *, fail_after_first: bool = False) -> None:
        self.calls = 0
        self.fail_after_first = fail_after_first

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, ModelUsageProvenance]:
        del messages, temperature
        self.calls += 1
        if self.fail_after_first and self.calls > 1:
            raise TimeoutError("reviewer unavailable")
        raw = json.dumps(
            {
                "ci": [],
                "v": [],
                "p": [],
                "visible_findings": [],
                "r": "The visible message has no external factual premise.",
            },
            ensure_ascii=False,
        )
        return raw, _usage(
            ref=f"usage:tail-reviewer:{self.calls}",
            input_tokens=4,
        output_tokens=2,
    )
