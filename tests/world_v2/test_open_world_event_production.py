from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.activity_plan_runtime import (
    ActivityPlanCommand,
    ActivityPlanTransitionCommand,
)
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.world_turn_runtime import InboundTurn


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("open-world production seam does not deliberate a reply")


class _MainModel:
    async def propose(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("open-world production seam does not deliberate a reply")


class _QuickRecovery:
    async def recover(self, _request, _failure):  # type: ignore[no-untyped-def]
        raise AssertionError("open-world production seam does not deliberate a reply")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("the scenario never drains a chat Action")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _OpenWorldModel:
    model = "test-open-world-model"

    async def complete(self, messages, *, temperature: float = 0.4):  # type: ignore[no-untyped-def]
        del temperature
        payload = json.loads(messages[-1]["content"])
        selected = payload["situations"][0]
        return json.dumps(
            {
                "decision": "select",
                "situation_token": selected["token"],
                "moment": "她在活动间隙注意到一处细小变化，顺手记在了心里。",
                "moment_scope": "subjective",
            },
            ensure_ascii=False,
        )


@pytest.mark.asyncio
async def test_production_open_world_model_turns_active_plan_into_replayable_occurrence(
    tmp_path: Path,
) -> None:
    config = WorldV2TurnApplicationConfig(
        world_id="world:open-world-production",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:open-world-production",
        life_ecology=LifeEcologyComposition.production_v1(),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "open-world-production.sqlite",
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        open_world_event_model=_OpenWorldModel(),
        now=NOW,
    )
    try:
        await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:open-world-source",
                text="我去公园走走。",
                observed_at=NOW,
                trace_id="trace:open-world-source",
            )
        )
        source = "observation:test:user.1:message:open-world-source"
        planned = await app.plan_activity(
            ActivityPlanCommand(
                command_id="command:open-world-plan",
                world_id=config.world_id,
                source_observation_id=source,
                plan_id="plan:open-world",
                activity_id="activity:open-world",
                activity_kind="walk",
                importance_bp=4_000,
                location_ref="location:park",
                privacy_class="shareable",
            ),
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:open-world-plan",
            causation_id=source,
            correlation_id="correlation:open-world",
        )
        started = await app.transition_activity(
            ActivityPlanTransitionCommand(
                command_id="command:open-world-start",
                world_id=config.world_id,
                source_observation_id=source,
                plan_id="plan:open-world",
                operation="start",
            ),
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:open-world-start",
            causation_id=planned.event_ids[-1],
            correlation_id="correlation:open-world",
        )

        result = await app.advance_life_ecology_once(
            wake_event_ref=started.event_ids[-1],
            trace_id="trace:open-world-wake",
            correlation_id="correlation:open-world",
        )

        assert result.status == "advanced"
        assert result.open_world_followup_status == "committed"
        projection = app._ledger.project()  # noqa: SLF001 - production replay evidence
        assert len(projection.world_occurrences) == 1
        occurrence = projection.world_occurrences[0]
        assert occurrence.status == "active"
        assert occurrence.location_ref == "location:park"
        assert any(
            item.event.event_type == "ProposalRecorded"
            and item.event.payload().get("proposal_kind") == "open_world_event"
            for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
        )

        # A later wake must settle the model-authored occurrence through the
        # ordinary aftermath/experience path; the candidate hash must remain
        # the exact immutable sidecar hash rather than a synthetic placeholder.
        later = NOW.replace(minute=20)
        await app.tick(
            tick_id="open-world:settle",
            logical_time_from=NOW,
            logical_time_to=later,
            observed_at=later,
            trace_id="trace:open-world-settle",
            causation_id="scheduler:open-world",
            correlation_id="correlation:open-world",
            reason="open-world-settlement",
        )
        recovery_at = later.replace(minute=21)
        await app.tick(
            tick_id="open-world:experience",
            logical_time_from=later,
            logical_time_to=recovery_at,
            observed_at=recovery_at,
            trace_id="trace:open-world-experience",
            causation_id="scheduler:open-world",
            correlation_id="correlation:open-world",
            reason="open-world-experience-recovery",
        )
        settled = app._ledger.project()  # noqa: SLF001
        assert settled.world_occurrences[0].status == "settled"
        assert len(settled.experiences) == 1
        result_ref = settled.world_occurrences[0].result_payload_ref
        result = app._life_content_store.read_exact(content_ref=result_ref)  # noqa: SLF001
        assert result is not None
        assert result.content_payload_hash == settled.world_occurrences[0].result_payload_hash
    finally:
        app.close()
