from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from world_v2_application import (
    build_sqlite_world_v2_test_application,
    compose_fixture_character_interior,
)

from companion_daemon.world_v2.deliberation import ModelRoute, RouteRequest
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
)
from companion_daemon.world_v2.character_interior.inbound_author import (
    _InboundCharacterAuthor as InboundCharacterAuthor,
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
        expression = {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": (
                    "那点受伤还在，但已经没有最初那么强；我想带着这份余波慢一点继续。"
                    if context_consumed
                    else "她说失望时我确实被刺了一下；我想先承认这份不舒服，再听她往下说。"
                ),
                "attended_source_refs": [],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": response_text}],
            "stance": stance,
            "brief_rationale": (
                "Keep the ongoing emotional context present without inventing a fact."
            ),
            "confidence": 7_200,
            "world_claims": [],
        }
        appraisal = (
            {
                "appraise": False,
                "brief_rationale": "No new emotional shift on the later turn.",
                "behavior_tendency": "hold_space",
                "stance": "attend_with_distance",
                "display_strategy": "restrained_boundary",
                "confidence": 6_000,
            }
            if context_consumed
            else {
                "appraise": True,
                "affect": "open",
                "brief_rationale": "The user explicitly described disappointment.",
                "behavior_tendency": "hold_space",
                "stance": "attend_with_distance",
                "display_strategy": "restrained_boundary",
                "confidence": 8_400,
                "meanings": [
                    {"meaning": "她觉得我没有认真对待她，因此这句话也让我感到关系里的受伤", "confidence": 8_200}
                ],
                "attribution": "user",
                "severity": 7_800,
                "components": [
                    {"dimension": "hurt", "target_intensity_bp": 6_200}
                ],
            }
        )
        return json.dumps(
            {
                "appraisal_draft": appraisal,
                "expression_draft": expression,
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
async def test_three_day_affect_decay_changes_the_next_unified_interior_turn(
    tmp_path: Path,
) -> None:
    reply = _ReplyChat()
    cognition = InboundCharacterAuthor(flash_model=reply)
    config = WorldV2TurnApplicationConfig(
        world_id="world:multiday-continuity",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:multiday-continuity",
        character_memory_enabled=False,
    )
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "multiday-continuity.sqlite",
        config=config,
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
        provider_request = json.loads(reply.requests[-1][-1]["content"])
        affect_material = provider_request["inner_life_snapshot"]["materials"][
            "affect"
        ]
        assert any(
            item["components"][0]["dimension"] == "hurt"
            for item in affect_material
        )
        assert len(reply.responses) >= 2
        assert reply.responses[-1][1] == "acknowledge_briefly"
        # The model receives the changed state; local code no longer rewrites
        # its visible response to force a variation.
    finally:
        app.close()
