from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.deferred_reply_runtime import DeferredReplyRuntime, ReplyLaterCommand
from companion_daemon.world_v2.action_pump import ActionPump
from companion_daemon.world_v2.activity_plan_runtime import (
    ActivityPlanCommand,
    ActivityPlanRuntime,
    ActivityPlanTransitionCommand,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    BudgetAccount,
    ClockObservation,
    Observation,
    ProjectionCursor,
    ProviderReceipt,
    WorldEvent,
)


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


def _event(event_id: str, event_type: str, payload: dict[str, object], *, at: datetime = NOW) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, world_id="world:reply-later",
        event_type=event_type, logical_time=at, created_at=at, actor="system:test",
        source="test", trace_id="trace:reply-later", causation_id=event_id,
        correlation_id="correlation:reply-later", idempotency_key=event_id, payload=payload,
    )


async def _initialized() -> tuple[WorldLedger, WorldRuntime]:
    ledger = WorldLedger.in_memory(world_id="world:reply-later")
    ledger.commit((_event("event:start", "WorldStarted", {}),), expected_world_revision=0, expected_deliberation_revision=0)
    projection = ledger.project()
    ledger.commit((_event("event:clock", "ClockAdvanced", {
        "logical_time_from": (NOW - timedelta(seconds=1)).isoformat(),
        "logical_time_to": NOW.isoformat(),
    }),), expected_world_revision=projection.world_revision, expected_deliberation_revision=projection.deliberation_revision)
    projection = ledger.project()
    ledger.commit((_event("event:account", "BudgetAccountConfigured", {
        "account": BudgetAccount(account_id="account:chat", category="chat", window_id="window:day", limit=100).model_dump(mode="json"),
    }),), expected_world_revision=projection.world_revision, expected_deliberation_revision=projection.deliberation_revision)
    runtime = WorldRuntime(world_id="world:reply-later", ledger=ledger)
    await runtime.ingest(Observation(
        schema_version="world-v2.1", observation_id="observation:source", world_id="world:reply-later",
        logical_time=NOW, created_at=NOW, trace_id="trace:reply-later", causation_id="inbound:1",
        correlation_id="correlation:reply-later", source="test", source_event_id="message:1",
        actor="user:primary", channel="test", payload_ref="payload:source", payload_hash="a" * 64,
        text="你先忙，晚点再回我也行。", received_at=NOW, reply_context={"target": "user:primary"},
    ))
    return ledger, runtime


def _command() -> ReplyLaterCommand:
    return ReplyLaterCommand(
        command_id="command:reply-later:1", world_id="world:reply-later",
        source_observation_id="observation:source", commitment_id="commitment:reply-later:1",
        action_id="action:reply-later:1", target="user:primary", payload_ref="payload:reply-later:1",
        payload_hash="b" * 64, content_ref="content:reply-later:1", content_hash="c" * 64,
        due_opens_at=NOW + timedelta(minutes=1), due_closes_at=NOW + timedelta(minutes=2),
        importance_bp=5000, budget_account_id="account:chat", budget_amount=3, recovery_policy="effect_once",
    )


def test_defer_is_source_bound_idempotent_and_restart_safe() -> None:
    ledger, _runtime = asyncio.run(_initialized())
    command = _command()
    first = DeferredReplyRuntime(ledger=ledger).defer(command, logical_time=NOW, created_at=NOW,
        trace_id="trace:reply-later", causation_id="cause:defer", correlation_id="correlation:reply-later")
    second = DeferredReplyRuntime(ledger=ledger).defer(command, logical_time=NOW, created_at=NOW,
        trace_id="trace:reply-later", causation_id="cause:defer", correlation_id="correlation:reply-later")
    projection = ledger.project()
    assert first == second
    assert [(item.commitment_id, item.values.status) for item in projection.commitments] == [("commitment:reply-later:1", "open")]
    assert [(item.action_id, item.state) for item in projection.actions] == [("action:reply-later:1", "authorized")]
    assert projection.budget_reservations[0].action_id == "action:reply-later:1"


def test_clock_marks_deferred_reply_due_without_inventing_completion() -> None:
    ledger, runtime = asyncio.run(_initialized())
    DeferredReplyRuntime(ledger=ledger).defer(_command(), logical_time=NOW, created_at=NOW,
        trace_id="trace:reply-later", causation_id="cause:defer", correlation_id="correlation:reply-later")
    before = ledger.project()
    clock = ClockObservation(
        schema_version="world-v2.1", tick_id="tick:reply-later-due", world_id="world:reply-later",
        logical_time=NOW + timedelta(minutes=1), created_at=NOW + timedelta(minutes=1),
        trace_id="trace:reply-later", causation_id="cause:clock", correlation_id="correlation:reply-later",
        logical_time_from=NOW, logical_time_to=NOW + timedelta(minutes=1), reason="test",
    )
    asyncio.run(runtime.advance(clock))
    clock_event = ledger.lookup_event_commit("event:trigger:clock:tick:reply-later-due")[0]
    events = DeferredReplyRuntime(ledger=ledger).clock_events(projection=before, clock_event=clock_event)
    current = ledger.project()
    ledger.commit_at_cursor(events, expected_cursor=ProjectionCursor(
        world_revision=current.world_revision, deliberation_revision=current.deliberation_revision,
        ledger_sequence=current.ledger_sequence), commit_id="commit:reply-later-due")
    projection = ledger.project()
    assert projection.commitments[0].values.status == "due"
    assert projection.plans == ()
    assert projection.actions[0].state == "authorized"


class _DeliveredExecutor:
    async def dispatch(self, action):
        return ProviderReceipt(
            provider_receipt_id="receipt:reply-later", action_id=action.action_id,
            idempotency_key=action.idempotency_key, provider="fake:chat", provider_ref="provider:reply-later",
            status="delivered", cost_actual=1, received_at=NOW + timedelta(minutes=1), raw_payload_hash="d" * 64,
        )

    async def lookup_result(self, action):
        del action
        return None


def test_delivered_reply_later_fulfills_exact_private_commitment() -> None:
    ledger, runtime = asyncio.run(_initialized())
    deferred = DeferredReplyRuntime(ledger=ledger)
    deferred.defer(_command(), logical_time=NOW, created_at=NOW,
        trace_id="trace:reply-later", causation_id="cause:defer", correlation_id="correlation:reply-later")
    before = ledger.project()
    clock = ClockObservation(
        schema_version="world-v2.1", tick_id="tick:reply-later-deliver", world_id="world:reply-later",
        logical_time=NOW + timedelta(minutes=1), created_at=NOW + timedelta(minutes=1),
        trace_id="trace:reply-later", causation_id="cause:clock", correlation_id="correlation:reply-later",
        logical_time_from=NOW, logical_time_to=NOW + timedelta(minutes=1), reason="test",
    )
    asyncio.run(runtime.advance(clock))
    clock_event = ledger.lookup_event_commit("event:trigger:clock:tick:reply-later-deliver")[0]
    current = ledger.project()
    ledger.commit_at_cursor(deferred.clock_events(projection=before, clock_event=clock_event), expected_cursor=ProjectionCursor(
        world_revision=current.world_revision, deliberation_revision=current.deliberation_revision,
        ledger_sequence=current.ledger_sequence), commit_id="commit:reply-later-deliver-due")
    pump = ActionPump(ledger=ledger, executor=_DeliveredExecutor(), settle=runtime.settle, owner_id="worker:test")
    asyncio.run(pump.drain_once())  # authorized -> scheduled
    asyncio.run(pump.drain_once())  # dispatch -> receipt settlement
    deferred.settle_terminal_action(action_id="action:reply-later:1", logical_time=NOW + timedelta(minutes=1),
        created_at=NOW + timedelta(minutes=1), trace_id="trace:reply-later", causation_id="cause:receipt",
        correlation_id="correlation:reply-later")
    projection = ledger.project()
    assert projection.actions[0].state == "delivered"
    assert projection.commitments[0].values.status == "fulfilled"


def _plan_command(*, plan_id: str, activity_id: str, supersedes: str | None = None) -> ActivityPlanCommand:
    return ActivityPlanCommand(
        command_id=f"command:{plan_id}", world_id="world:reply-later", source_observation_id="observation:source",
        plan_id=plan_id, activity_id=activity_id, activity_kind="study", importance_bp=4000,
        supersedes_plan_id=supersedes,
    )


def test_activity_replacement_abandons_predecessor_without_completed_experience() -> None:
    ledger, _runtime = asyncio.run(_initialized())
    plans = ActivityPlanRuntime(ledger=ledger, owner_actor_ref="actor:companion")
    plans.plan(_plan_command(plan_id="plan:old", activity_id="activity:old"), logical_time=NOW, created_at=NOW,
        trace_id="trace:plan", causation_id="cause:plan", correlation_id="correlation:plan")
    plans.replace(_plan_command(plan_id="plan:new", activity_id="activity:new", supersedes="plan:old"),
        predecessor_plan_id="plan:old", logical_time=NOW, created_at=NOW, trace_id="trace:plan",
        causation_id="cause:replace", correlation_id="correlation:plan")
    projection = ledger.project()
    assert [(item.plan_id, item.status, item.supersedes_plan_id) for item in projection.plans] == [
        ("plan:old", "abandoned", None), ("plan:new", "planned", "plan:old")
    ]
    assert projection.experiences == ()
    # Cancellation maps to the same explicit abandoned terminal, rather than
    # inventing a completed activity fact.
    plans.transition(ActivityPlanTransitionCommand(command_id="command:cancel-new", world_id="world:reply-later",
        source_observation_id="observation:source", plan_id="plan:new", operation="abandon"), logical_time=NOW,
        created_at=NOW, trace_id="trace:plan", causation_id="cause:cancel", correlation_id="correlation:plan")
    assert ledger.project().plans[1].status == "abandoned"
