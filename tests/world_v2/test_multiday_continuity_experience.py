from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.affect_chat_model_adapter import AffectDraftDeliberationAdapter
from companion_daemon.world_v2.appraisal_chat_model_adapter import AppraisalDraftDeliberationAdapter
from companion_daemon.world_v2.chat_model_deliberation_adapter import ChatModelDeliberationAdapter
from companion_daemon.world_v2.deliberation import ModelRoute, RouteRequest
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.relationship_draft_deliberation_adapter import (
    RelationshipDraftDeliberationAdapter,
)
from companion_daemon.world_v2.world_turn_runtime import InboundTurn


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _ReplyChat:
    model = "test-multiday-reply"

    def __init__(self) -> None:
        self.requests: list[list[dict[str, str]]] = []
        self.responses: list[tuple[str, str]] = []

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.requests.append(messages)
        request_text = messages[-1]["content"]
        context_consumed = bool(self.responses) and (
            '"dimension":"hurt"' in request_text or '"dimension": "hurt"' in request_text
        )
        if context_consumed:
            response_text = "我还记得那点不舒服，我们先慢一点说。"
            stance = "acknowledge_briefly"
        else:
            response_text = "我还在想这件事，你慢慢说。"
            stance = "acknowledge_briefly"
        self.responses.append((response_text, stance))
        return json.dumps(
            {
                "response_text": response_text,
                "stance": stance,
                "brief_rationale": "Keep the ongoing emotional context present without inventing a fact.",
                "confidence": 7_200,
            },
            ensure_ascii=False,
        )


class _AppraisalChat:
    model = "test-multiday-appraisal"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "appraise": True,
                "affect": "open",
                "brief_rationale": "The user explicitly described disappointment.",
                "behavior_tendency": "hold_space",
                "stance": "attend_with_distance",
                "display_strategy": "restrained_boundary",
                "confidence": 8_400,
                "meanings": [{"meaning": "boundary_violation", "confidence": 8_200}],
                "attribution": "user",
                "severity": 7_800,
                "components": [{"dimension": "hurt", "intensity_bp": 6_200}],
            },
            ensure_ascii=False,
        )


class _RelationshipChat:
    model = "test-multiday-relationship"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "decision": "signal",
                "signal_code": "reliability_follow_through",
                "confidence_bp": 7_400,
                "persistence": "durable",
                "rationale_code": "accepted_reliability_evidence",
                "suggested_deltas": {
                    "trust_bp": 240,
                    "closeness_bp": 40,
                    "respect_bp": 160,
                    "reliability_bp": 260,
                    "mutuality_bp": 20,
                    "repair_confidence_bp": 0,
                },
            },
            ensure_ascii=False,
        )


class _Transport:
    provider = "platform:test"

    async def send(self, request):  # type: ignore[no-untyped-def]
        return PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:multiday:{request.idempotency_key}",
            provider_ref=f"message:multiday:{request.idempotency_key}",
            status="delivered",
            received_at=NOW,
            raw_payload_hash="sha256:" + "a" * 64,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


@pytest.mark.asyncio
async def test_three_day_affect_decay_and_relationship_memory_change_the_next_turn(
    tmp_path: Path,
) -> None:
    reply = _ReplyChat()
    config = WorldV2TurnApplicationConfig(
        world_id="world:multiday-continuity",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:multiday-continuity",
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "multiday-continuity.sqlite",
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=ChatModelDeliberationAdapter(model=reply),
        quick_recovery=ChatModelDeliberationAdapter(model=reply),
        appraisal_model=AppraisalDraftDeliberationAdapter(model=_AppraisalChat()),
        affect_model=AffectDraftDeliberationAdapter(model=_AppraisalChat()),
        relationship_model=RelationshipDraftDeliberationAdapter(model=_RelationshipChat()),
        transport=_Transport(),
        now=NOW,
    )
    try:
        first = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:multiday-source",
                text="你刚才让我有点失望，感觉你没把我说的事当回事。",
                observed_at=NOW,
                trace_id="trace:multiday-source",
            )
        )
        assert first.status == "action_authorized"
        for _ in range(6):
            result = await app.drain_background_once()
            if result is not None and getattr(result, "status", None) == "idle":
                break
        initial = app._ledger.project()  # noqa: SLF001 - experience replay evidence
        initial_hurt = initial.affect_episodes[0].components[0].intensity_bp
        initial_trust = initial.relationship_states[0].variables.trust_bp

        previous = NOW
        for day in range(1, 4):
            at = NOW + timedelta(days=day)
            await app.tick(
                tick_id=f"multiday:{day}",
                logical_time_from=previous,
                logical_time_to=at,
                observed_at=at,
                trace_id=f"trace:multiday:{day}",
                causation_id="scheduler:multiday",
                correlation_id="correlation:multiday",
                reason="multi-day-continuity",
            )
            previous = at

        final = app._ledger.project()  # noqa: SLF001 - experience replay evidence
        assert final.affect_episodes[0].components[0].intensity_bp < initial_hurt
        assert final.relationship_states[0].variables.trust_bp == initial_trust

        second = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:multiday-consumer",
                text="那我们继续说吧。",
                observed_at=previous,
                trace_id="trace:multiday-consumer",
            )
        )
        assert second.status == "action_authorized"
        context = json.loads(reply.requests[-1][-1]["content"])["request"]
        payload = json.loads(context["model_content_json"])
        assert any(
            item["value"]["components"][0]["dimension"] == "hurt"
            for item in payload["slices"]["affect_episodes"]["items"]
        )
        assert any(
            item["value"]["variables"]["trust_bp"] == initial_trust
            for item in payload["slices"]["relationship_slice"]["items"]
        )
        assert len(reply.responses) >= 2
        assert reply.responses[-1][1] == "acknowledge_briefly"
        assert reply.responses[-1][0] != reply.responses[0][0]
    finally:
        app.close()
