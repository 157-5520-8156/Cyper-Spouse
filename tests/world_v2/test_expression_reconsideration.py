from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.action_pump import ActionPump
from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    CompanionIdentityFrame,
)
from companion_daemon.world_v2.expression_reconsideration import (
    expression_beat_is_gated,
    expression_reconsideration_events_for_observation,
    expression_reconsideration_trigger_event,
    expression_reconsideration_trigger_id,
)
from companion_daemon.world_v2.expression_reconsideration_runtime import (
    ExpressionReconsiderationDecision,
    ExpressionReconsiderationRuntime,
)
from companion_daemon.world_v2.expression_reconsideration_model_adapter import (
    AuditedReplacementReconsiderationReviewer,
    ExpressionReconsiderationChatModelAdapter,
)
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.private_turn_state import PrivateTurnState
from companion_daemon.world_v2.proposal_envelope import MinimalProposal
from companion_daemon.world_v2.reducers import ReducerState, make_projection, reduce_event
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    BudgetReservation,
    ClaimLease,
    CommitResult,
    ExpressionBeatLifecycleEntry,
    ExpressionBeatProjection,
    ExpressionPlanLifecycleEntry,
    ExpressionPlanProjection,
    Observation,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD_ID = "world:expression-reconsideration"


def _observation() -> tuple[Observation, WorldEvent]:
    observation = Observation(
        schema_version="world-v2.1",
        observation_id="observation:user-interjection:1",
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interjection:1",
        causation_id="platform:message:1",
        correlation_id="conversation:1",
        source="platform:test",
        source_event_id="message:1",
        actor="user:test",
        channel="test",
        payload_ref="ingress:message:1",
        payload_hash="sha256:" + "1" * 64,
        text="等等，我刚刚还有话想说。",
        received_at=NOW,
    )
    return observation, WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:trigger:observation:1",
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key="observation:message:1",
        payload=observation.model_dump(mode="json"),
    )


def _action() -> Action:
    return Action(
        schema_version="world-v2.1",
        action_id="action:expression:two",
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:expression:1",
        causation_id="event:acceptance:1",
        correlation_id="conversation:1",
        kind="reply",
        layer="external_action",
        intent_ref="proposal:1:intent:two",
        actor="companion:test",
        target="user:test",
        payload_ref="payload:expression:two",
        payload_hash="sha256:" + "2" * 64,
        expression_plan_id="plan:expression:1",
        expression_beat_id="beat:expression:2",
        idempotency_key="action:expression:two",
        budget_reservation_id="reservation:expression:two",
        state="authorized",
        recovery_policy="effect_once",
    )


def _state() -> ReducerState:
    action = _action()
    return ReducerState(
        message_observations=(),
        expression_plans=(
            ExpressionPlanProjection(
                acceptance_id="acceptance:1",
                proposal_id="proposal:1",
                expression_change_id="change:expression:1",
                plan_id="plan:expression:1",
                event_ref="event:plan:1",
                event_payload_hash="a" * 64,
                history=(
                    ExpressionPlanLifecycleEntry(
                        state="authorized",
                        event_ref="event:plan:1",
                        event_payload_hash="a" * 64,
                    ),
                ),
            ),
        ),
        expression_beats=(
            ExpressionBeatProjection(
                acceptance_id="acceptance:1",
                proposal_id="proposal:1",
                expression_change_id="change:expression:1",
                plan_id="plan:expression:1",
                beat_id="beat:expression:2",
                payload_ref=action.payload_ref,
                payload_hash=action.payload_hash,
                action_id=action.action_id,
                cancel_policy="cancel-before-dispatch",
                reconsider_policy="reconsider-on-new-observation",
                merge_policy="merge-with-new-observation",
                event_ref="event:beat:2",
                event_payload_hash="b" * 64,
                history=(
                    ExpressionBeatLifecycleEntry(
                        state="authorized",
                        event_ref="event:beat:2",
                        event_payload_hash="b" * 64,
                    ),
                ),
            ),
        ),
        actions=(action,),
        pending_actions=(action,),
    )


def test_user_interjection_opens_one_deterministic_reconsideration_trigger_for_undispatched_beat() -> None:
    observation, event = _observation()

    trigger = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    state = reduce_event(reduce_event(_state(), event), trigger)

    assert trigger.payload()["process"]["trigger_id"] == expression_reconsideration_trigger_id(
        world_id=WORLD_ID,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
        observation_id=observation.observation_id,
    )
    assert len(state.trigger_processes) == 1
    assert state.trigger_processes[0].process_kind == "expression_reconsideration"
    assert state.trigger_processes[0].state == "open"


def test_interjection_gates_every_remaining_beat_of_a_multi_beat_plan_in_stable_order() -> None:
    observation, source_event = _observation()
    first = _action()
    second = first.model_copy(
        update={
            "action_id": "action:expression:three",
            "expression_beat_id": "beat:expression:3",
            "intent_ref": "proposal:1:intent:three",
            "payload_ref": "payload:expression:three",
            "payload_hash": "sha256:" + "3" * 64,
            "idempotency_key": "action:expression:three",
            "budget_reservation_id": "reservation:expression:three",
        }
    )
    third = first.model_copy(
        update={
            "action_id": "action:expression:four",
            "expression_beat_id": "beat:expression:4",
            "intent_ref": "proposal:1:intent:four",
            "payload_ref": "payload:expression:four",
            "payload_hash": "sha256:" + "4" * 64,
            "idempotency_key": "action:expression:four",
            "budget_reservation_id": "reservation:expression:four",
        }
    )
    existing = _state().expression_beats[0]
    state = _state().model_copy(
        update={
            "actions": (first, second, third),
            "pending_actions": (first, second, third),
            "expression_beats": (
                existing,
                existing.model_copy(
                    update={
                        "beat_id": second.expression_beat_id,
                        "action_id": second.action_id,
                        "payload_ref": second.payload_ref,
                        "payload_hash": second.payload_hash,
                        "dependency_beat_ids": (existing.beat_id,),
                        "event_ref": "event:beat:3",
                    }
                ),
                existing.model_copy(
                    update={
                        "beat_id": third.expression_beat_id,
                        "action_id": third.action_id,
                        "payload_ref": third.payload_ref,
                        "payload_hash": third.payload_hash,
                        "dependency_beat_ids": (second.expression_beat_id,),
                        "event_ref": "event:beat:4",
                    }
                ),
            ),
        }
    )
    projection = make_projection(
        world_id=WORLD_ID,
        world_revision=1,
        deliberation_revision=0,
        ledger_sequence=1,
        state=state,
    )

    triggers = expression_reconsideration_events_for_observation(
        projection=projection, observation=observation, source_event=source_event
    )

    assert len(triggers) == 3
    assert [item.payload()["process"]["trigger_ref"] for item in triggers] == sorted(
        item.payload()["process"]["trigger_ref"] for item in triggers
    )


class _BlockedLedger:
    blocks_event_loop = False
    world_id = WORLD_ID

    def __init__(self, projection) -> None:
        self._projection = projection

    def project(self):
        return self._projection


class _Executor:
    def __init__(self) -> None:
        self.dispatch_calls = 0

    async def dispatch(self, _action):
        self.dispatch_calls += 1
        raise AssertionError("a reconsideration-gated beat must not dispatch")

    async def lookup_result(self, _action):
        raise AssertionError("a reconsideration-gated beat must not recover-dispatch")


@pytest.mark.asyncio
async def test_open_interjection_gate_prevents_old_beat_from_reaching_executor() -> None:
    observation, source_event = _observation()
    state = reduce_event(_state(), source_event)
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    state = reduce_event(state, trigger_event)
    projection = make_projection(
        world_id=WORLD_ID,
        world_revision=1,
        deliberation_revision=1,
        ledger_sequence=2,
        state=state,
    )
    executor = _Executor()
    pump = ActionPump(
        ledger=_BlockedLedger(projection),
        executor=executor,
        settle=lambda _result: None,
        owner_id="pump:test",
    )

    result = await pump.drain_once()

    assert result.status == "idle"
    assert executor.dispatch_calls == 0


class _TriggerLedger:
    blocks_event_loop = False
    world_id = WORLD_ID

    def __init__(self, state: ReducerState) -> None:
        self._state = state
        self._events: dict[str, WorldEvent] = {}
        self._world_revision = 0
        self._deliberation_revision = 0
        self._sequence = 0

    def project(self):
        return make_projection(
            world_id=self.world_id,
            world_revision=self._world_revision,
            deliberation_revision=self._deliberation_revision,
            ledger_sequence=self._sequence,
            state=self._state,
        )

    def lookup_event_commit(self, event_id: str):
        event = self._events.get(event_id)
        return None if event is None else (event, None)

    def commit(self, events, **_kwargs):
        for event in events:
            self._state = reduce_event(self._state, event)
            self._events[event.event_id] = event
            self._sequence += 1
            if event.event_type in {"ObservationRecorded"}:
                self._world_revision += 1
            else:
                self._deliberation_revision += 1
        return CommitResult(
            world_revision=self._world_revision,
            deliberation_revision=self._deliberation_revision,
            ledger_sequence=self._sequence,
            event_ids=tuple(event.event_id for event in events),
        )

    def commit_at_cursor(self, events, **kwargs):
        return self.commit(events, **kwargs)


class _CursorCheckingTriggerLedger(_TriggerLedger):
    """Tiny in-memory ledger enforcing the production CAS arguments."""

    def commit(self, events, **kwargs):
        expected_world = kwargs.get("expected_world_revision")
        expected_deliberation = kwargs.get("expected_deliberation_revision")
        if expected_world is not None and expected_world != self._world_revision:
            raise ConcurrencyConflict("stale world revision")
        if (
            expected_deliberation is not None
            and expected_deliberation != self._deliberation_revision
        ):
            raise ConcurrencyConflict("stale deliberation revision")
        return super().commit(events, **kwargs)

    def commit_at_cursor(self, events, **kwargs):
        expected = kwargs.get("expected_cursor")
        if expected is not None and (
            expected.world_revision != self._world_revision
            or expected.deliberation_revision != self._deliberation_revision
            or expected.ledger_sequence != self._sequence
        ):
            raise ConcurrencyConflict("stale projection cursor")
        return super().commit(events, **kwargs)


@pytest.mark.asyncio
async def test_missing_reviewer_keeps_claimed_gate_and_never_implicitly_continues_old_beat() -> None:
    observation, source_event = _observation()
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    ledger = _TriggerLedger(_state())
    ledger.commit((source_event, trigger_event))

    result = await ExpressionReconsiderationRuntime(
        ledger=ledger, owner_id="worker:reconsideration"
    ).drain_one()

    assert result.status == "awaiting_review"
    assert ledger.project().trigger_processes[0].state == "claimed"
    assert ledger.project().actions[0].state == "authorized"


class _ContinueReviewer:
    async def review(self, **_kwargs):
        return "continue"


class _BrokenReviewer:
    async def review(self, **_kwargs):
        raise ValueError("invalid model JSON")


@pytest.mark.asyncio
async def test_invalid_role_decision_remains_retryable_without_crashing_the_scheduler() -> None:
    observation, source_event = _observation()
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    ledger = _TriggerLedger(_state())
    ledger.commit((source_event, trigger_event))

    result = await ExpressionReconsiderationRuntime(
        ledger=ledger,
        owner_id="worker:reconsideration",
        reviewer=_BrokenReviewer(),
    ).drain_one()

    assert result.status == "awaiting_review"
    assert ledger.project().trigger_processes[0].state == "claimed"
    assert ledger.project().actions[0].state == "authorized"


@pytest.mark.asyncio
async def test_only_explicit_reviewer_continuation_releases_expression_gate() -> None:
    observation, source_event = _observation()
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    ledger = _TriggerLedger(_state())
    ledger.commit((source_event, trigger_event))

    result = await ExpressionReconsiderationRuntime(
        ledger=ledger,
        owner_id="worker:reconsideration",
        reviewer=_ContinueReviewer(),
    ).drain_one()

    assert result.status == "continued"
    assert ledger.project().trigger_processes[0].state == "terminal"


@pytest.mark.asyncio
async def test_role_may_continue_unsent_tail_after_first_beat_reached_provider() -> None:
    """A delivered sibling is context, not a local-policy veto of the role."""

    observation, source_event = _observation()
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    pending = _action()
    sent = pending.model_copy(
        update={
            "action_id": "action:expression:one",
            "expression_beat_id": "beat:expression:1",
            "intent_ref": "proposal:1:intent:one",
            "payload_ref": "payload:expression:one",
            "payload_hash": "sha256:" + "1" * 64,
            "idempotency_key": "action:expression:one",
            "budget_reservation_id": "reservation:expression:one",
            "state": "provider_accepted",
            "claim_lease": ClaimLease(
                owner_id="pump:test",
                attempt_id="attempt:expression:one",
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=2),
            ),
        }
    )
    state = _state().model_copy(
        update={
            "actions": (sent, pending),
            "pending_actions": (sent, pending),
            "budget_accounts": (
                BudgetAccount(
                    account_id="account:chat:1",
                    category="chat",
                    window_id="window:1",
                    limit=100,
                    reserved=10,
                ),
            ),
            "budget_reservations": (
                BudgetReservation(
                    reservation_id=pending.budget_reservation_id,
                    account_id="account:chat:1",
                    action_id=pending.action_id,
                    category="chat",
                    amount_limit=10,
                ),
            ),
        }
    )
    ledger = _TriggerLedger(state)
    ledger.commit((source_event, trigger_event))

    result = await ExpressionReconsiderationRuntime(
        ledger=ledger,
        owner_id="worker:reconsideration",
        reviewer=_ContinueReviewer(),
    ).drain_one()

    projection = ledger.project()
    assert result.status == "continued"
    assert next(item for item in projection.actions if item.action_id == pending.action_id).state == (
        "authorized"
    )
    assert sent.action_id not in {
        item.action_id for item in projection.actions if item.state == "cancelled"
    }


class _CancelReviewer:
    async def review(self, **_kwargs):
        return ExpressionReconsiderationDecision(
            disposition="supersede",
            rationale_ref="decision-rationale:interjection:1",
            replacement_plan_ref="proposal:expression:replacement:1",
        )


@pytest.mark.asyncio
async def test_replacement_decision_atomically_cancels_undispatched_action_and_releases_budget() -> None:
    """An LLM may reject stale prose, but cannot alter a dispatched payload."""

    observation, source_event = _observation()
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    action = _action()
    state = _state().model_copy(
        update={
            "budget_accounts": (
                BudgetAccount(
                    account_id="account:chat:1", category="chat", window_id="window:1", limit=100,
                    reserved=10,
                ),
            ),
            "budget_reservations": (
                BudgetReservation(
                    reservation_id=action.budget_reservation_id, account_id="account:chat:1",
                    action_id=action.action_id, category="chat", amount_limit=10,
                ),
            ),
        }
    )
    ledger = _TriggerLedger(state)
    ledger.commit((source_event, trigger_event))

    result = await ExpressionReconsiderationRuntime(
        ledger=ledger, owner_id="worker:reconsideration", reviewer=_CancelReviewer()
    ).drain_one()

    projection = ledger.project()
    assert result.status == "replacement_required"
    assert result.disposition == "supersede"
    assert projection.actions[0].state == "cancelled"
    assert projection.budget_reservations[0].state == "released"
    assert projection.budget_accounts[0].reserved == 0
    assert projection.expression_plans[0].state == "terminated"
    assert projection.expression_plans[0].history[-1].terminal_disposition == "superseded"
    assert projection.expression_beats[0].state == "terminated"
    assert projection.trigger_processes[0].state == "terminal"
    assert "replacement:1" in (projection.trigger_processes[0].runtime_outcome_ref or "")


@pytest.mark.asyncio
async def test_restart_does_not_duplicate_cancel_after_terminal_reconsideration() -> None:
    """Crash/restart after a cancel must not re-emit ActionCancelled.

    Plan §5: beat 1 delivered / undispatched cancelled once; restart replay
    recovers commitments without duplicating the cancellation.
    """

    observation, source_event = _observation()
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    action = _action()
    state = _state().model_copy(
        update={
            "budget_accounts": (
                BudgetAccount(
                    account_id="account:chat:1",
                    category="chat",
                    window_id="window:1",
                    limit=100,
                    reserved=10,
                ),
            ),
            "budget_reservations": (
                BudgetReservation(
                    reservation_id=action.budget_reservation_id,
                    account_id="account:chat:1",
                    action_id=action.action_id,
                    category="chat",
                    amount_limit=10,
                ),
            ),
        }
    )
    ledger = _TriggerLedger(state)
    ledger.commit((source_event, trigger_event))

    first = await ExpressionReconsiderationRuntime(
        ledger=ledger, owner_id="worker:reconsideration", reviewer=_CancelReviewer()
    ).drain_one()
    cancel_ids = {
        event_id
        for event_id, event in ledger._events.items()
        if event.event_type == "ActionCancelled"
    }
    assert first.status == "replacement_required"
    assert len(cancel_ids) == 1

    # Simulate process restart: fresh runtime, same durable ledger head.
    second = await ExpressionReconsiderationRuntime(
        ledger=ledger, owner_id="worker:reconsideration", reviewer=_CancelReviewer()
    ).drain_one()
    cancel_ids_after = {
        event_id
        for event_id, event in ledger._events.items()
        if event.event_type == "ActionCancelled"
    }
    assert second.status == "idle"
    assert cancel_ids_after == cancel_ids
    assert ledger.project().actions[0].state == "cancelled"


class _DeferReviewer:
    async def review(self, **_kwargs):
        return ExpressionReconsiderationDecision(disposition="defer")


@pytest.mark.asyncio
async def test_defer_is_durable_and_keeps_the_old_payload_gated() -> None:
    observation, source_event = _observation()
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    ledger = _TriggerLedger(_state())
    ledger.commit((source_event, trigger_event))

    result = await ExpressionReconsiderationRuntime(
        ledger=ledger, owner_id="worker:reconsideration", reviewer=_DeferReviewer()
    ).drain_one()

    projection = ledger.project()
    assert result.status == "deferred"
    assert projection.actions[0].state == "authorized"
    assert expression_beat_is_gated(
        projection=projection, plan_id="plan:expression:1", beat_id="beat:expression:2"
    )


class _CountingCancelReviewer:
    def __init__(self) -> None:
        self.calls = 0

    async def review(self, **_kwargs):
        self.calls += 1
        return "cancel"


def _interjection(sequence: int) -> tuple[Observation, WorldEvent]:
    observation, _event = _observation()
    observation = observation.model_copy(
        update={
            "observation_id": f"observation:user-interjection:{sequence}",
            "source_event_id": f"message:{sequence}",
            "payload_ref": f"ingress:message:{sequence}",
        }
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:trigger:observation:{sequence}",
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=f"observation:message:{sequence}",
        payload=observation.model_dump(mode="json"),
    )
    return observation, event


class _HeadAdvanceReviewer:
    def __init__(self, disposition: str = "continue") -> None:
        self.disposition = disposition
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cursors: list[ProjectionCursor] = []

    async def review(self, **kwargs):  # type: ignore[no-untyped-def]
        self.cursors.append(kwargs["cursor"])
        if len(self.cursors) == 1:
            self.started.set()
            await self.release.wait()
        return self.disposition


@pytest.mark.asyncio
async def test_reconsideration_redecides_when_ledger_advances_during_role_review() -> None:
    """A decision may mutate only the exact World Context it was shown."""

    observation, source_event = _observation()
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    ledger = _CursorCheckingTriggerLedger(_state())
    ledger.commit((source_event, trigger_event))
    reviewer = _HeadAdvanceReviewer()
    runtime = ExpressionReconsiderationRuntime(
        ledger=ledger,
        owner_id="worker:reconsideration",
        reviewer=reviewer,
    )

    first_run = asyncio.create_task(runtime.drain_one())
    await asyncio.wait_for(reviewer.started.wait(), timeout=1)
    _new_observation, new_observation_event = _interjection(2)
    ledger.commit((new_observation_event,))
    reviewer.release.set()
    first = await asyncio.wait_for(first_run, timeout=1)

    assert first.status == "awaiting_review"
    after_conflict = ledger.project()
    assert after_conflict.trigger_processes[0].state == "claimed"
    assert after_conflict.actions[0].state == "authorized"

    second = await runtime.drain_one()

    assert second.status == "continued"
    assert ledger.project().trigger_processes[0].state == "terminal"
    assert len(reviewer.cursors) == 2
    assert reviewer.cursors[0] != reviewer.cursors[1]


@pytest.mark.asyncio
async def test_stale_cancel_decision_cannot_retire_a_beat_in_a_newer_world() -> None:
    observation, source_event = _observation()
    trigger_event = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    )
    action = _action()
    state = _state().model_copy(
        update={
            "budget_accounts": (
                BudgetAccount(
                    account_id="account:chat:1",
                    category="chat",
                    window_id="window:1",
                    limit=100,
                    reserved=10,
                ),
            ),
            "budget_reservations": (
                BudgetReservation(
                    reservation_id=action.budget_reservation_id,
                    account_id="account:chat:1",
                    action_id=action.action_id,
                    category="chat",
                    amount_limit=10,
                ),
            ),
        }
    )
    ledger = _CursorCheckingTriggerLedger(state)
    ledger.commit((source_event, trigger_event))
    reviewer = _HeadAdvanceReviewer("cancel")
    runtime = ExpressionReconsiderationRuntime(
        ledger=ledger,
        owner_id="worker:reconsideration",
        reviewer=reviewer,
    )

    first_run = asyncio.create_task(runtime.drain_one())
    await asyncio.wait_for(reviewer.started.wait(), timeout=1)
    _new_observation, new_observation_event = _interjection(2)
    ledger.commit((new_observation_event,))
    reviewer.release.set()
    first = await asyncio.wait_for(first_run, timeout=1)

    assert first.status == "awaiting_review"
    after_conflict = ledger.project()
    assert after_conflict.trigger_processes[0].state == "claimed"
    assert after_conflict.actions[0].state == "authorized"
    assert after_conflict.budget_reservations[0].state == "reserved"

    second = await runtime.drain_one()

    assert second.status == "cancelled"
    final = ledger.project()
    assert final.trigger_processes[0].state == "terminal"
    assert final.actions[0].state == "cancelled"
    assert len(reviewer.cursors) == 2
    assert reviewer.cursors[0] != reviewer.cursors[1]


@pytest.mark.asyncio
async def test_gate_backlog_on_one_beat_drains_after_first_cancel_makes_the_rest_moot() -> None:
    """Recovery semantics for gates opened while production had no reviewer.

    Several user interjections each opened a durable gate on the same frozen
    beat.  Once a reviewer is attached, the first drained gate carries the one
    real decision; every later gate on the now-retired beat completes as a
    recorded moot continuation without another model call.
    """

    action = _action()
    state = _state().model_copy(
        update={
            "budget_accounts": (
                BudgetAccount(
                    account_id="account:chat:1", category="chat", window_id="window:1",
                    limit=100, reserved=10,
                ),
            ),
            "budget_reservations": (
                BudgetReservation(
                    reservation_id=action.budget_reservation_id, account_id="account:chat:1",
                    action_id=action.action_id, category="chat", amount_limit=10,
                ),
            ),
        }
    )
    ledger = _TriggerLedger(state)
    for sequence in (1, 2, 3):
        observation, source_event = _interjection(sequence)
        ledger.commit(
            (
                source_event,
                expression_reconsideration_trigger_event(
                    world_id=WORLD_ID,
                    source_event=source_event,
                    observation=observation,
                    plan_id="plan:expression:1",
                    beat_id="beat:expression:2",
                ),
            )
        )
    reviewer = _CountingCancelReviewer()
    runtime = ExpressionReconsiderationRuntime(
        ledger=ledger, owner_id="worker:reconsideration", reviewer=reviewer
    )

    first = await runtime.drain_one()
    second = await runtime.drain_one()
    third = await runtime.drain_one()
    idle = await runtime.drain_one()

    projection = ledger.project()
    assert first.status == "cancelled"
    assert {second.status, third.status} == {"moot"}
    assert idle.status == "idle"
    assert reviewer.calls == 1
    assert projection.actions[0].state == "cancelled"
    assert projection.budget_reservations[0].state == "released"
    assert all(item.state == "terminal" for item in projection.trigger_processes)
    assert not expression_beat_is_gated(
        projection=projection, plan_id="plan:expression:1", beat_id="beat:expression:2"
    )


@pytest.mark.asyncio
async def test_gate_on_already_dispatched_action_completes_moot_without_a_model_call() -> None:
    """A gate legitimately opened earlier may outlive its beat's dispatch.

    The reducer only opens gates for un-dispatched beats, but a previously
    opened durable gate can still be sitting there after the action settled
    (for example after a continue decision released a sibling gate).  Draining
    it must not consult the reviewer or attempt an impossible cancellation.
    """

    observation, source_event = _observation()
    process = TriggerProcess.model_validate_json(
        json.dumps(
            expression_reconsideration_trigger_event(
                world_id=WORLD_ID,
                source_event=source_event,
                observation=observation,
                plan_id="plan:expression:1",
                beat_id="beat:expression:2",
            ).payload()["process"]
        )
    )
    action = _action().model_copy(
        update={
            "state": "delivered",
            "claim_lease": ClaimLease(
                owner_id="pump:test",
                attempt_id="attempt:pump:1",
                acquired_at=NOW,
                expires_at=NOW + timedelta(seconds=120),
            ),
        }
    )
    state = _state().model_copy(
        update={
            "actions": (action,),
            "pending_actions": (),
            "trigger_processes": (process,),
        }
    )
    ledger = _TriggerLedger(state)
    ledger.commit((source_event,))
    reviewer = _CountingCancelReviewer()

    result = await ExpressionReconsiderationRuntime(
        ledger=ledger, owner_id="worker:reconsideration", reviewer=reviewer
    ).drain_one()

    projection = ledger.project()
    assert result.status == "moot"
    assert reviewer.calls == 0
    assert projection.trigger_processes[0].state == "terminal"
    assert "moot-gate:action-delivered" in (
        projection.trigger_processes[0].runtime_outcome_ref or ""
    )
    assert not expression_beat_is_gated(
        projection=projection, plan_id="plan:expression:1", beat_id="beat:expression:2"
    )


class _DecisionModel:
    model = "test-decision"

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float):
        assert temperature == 0.25
        self.calls.append(messages)
        return self.raw


@pytest.mark.asyncio
async def test_chat_model_adapter_cannot_inject_new_payload_or_unbound_replacement() -> None:
    observation, source_event = _observation()
    trigger = expression_reconsideration_trigger_event(
        world_id=WORLD_ID, source_event=source_event, observation=observation,
        plan_id="plan:expression:1", beat_id="beat:expression:2",
    ).payload()["process"]
    process = TriggerProcess.model_validate_json(json.dumps(trigger))
    accepted = await ExpressionReconsiderationChatModelAdapter(
        model=_DecisionModel('{"disposition":"cancel"}')
    ).review(
        process=process,
        observation_event=source_event,
        cursor=ProjectionCursor(world_revision=1, deliberation_revision=1, ledger_sequence=2),
    )

    assert accepted.disposition == "cancel"
    assert accepted.rationale_ref and accepted.rationale_ref.startswith("model-decision:")


@pytest.mark.asyncio
async def test_replacement_lookup_uses_the_same_pinned_cursor_as_the_role_decision() -> None:
    observation, source_event = _observation()
    trigger = expression_reconsideration_trigger_event(
        world_id=WORLD_ID,
        source_event=source_event,
        observation=observation,
        plan_id="plan:expression:1",
        beat_id="beat:expression:2",
    ).payload()["process"]
    process = TriggerProcess.model_validate_json(json.dumps(trigger))
    cursor = ProjectionCursor(world_revision=1, deliberation_revision=1, ledger_sequence=2)
    projected_at: list[ProjectionCursor] = []
    old_private_turn_state = PrivateTurnState(
        inner_state_summary="刚才本来想把自己的近况说完，但新消息让我重新衡量是否还合适。",
        attended_source_refs=(
            "dialogue:expression:previous",
            "experience:self:shift-1",
        ),
    )
    old_proposal = MinimalProposal(
        proposal_id="proposal:1",
        trigger_ref="event:observation:previous",
        evaluated_world_revision=1,
        confidence=7000,
        brief_rationale="保留先前表达选择的同轮审计。",
        private_turn_state=old_private_turn_state,
        source_model_result="model-result:previous",
        response_text="这条是尚未发出的旧表达。",
        stance="defer",
    )

    def project_at(requested: ProjectionCursor):
        projected_at.append(requested)
        return SimpleNamespace(
            proposal_audits=(
                SimpleNamespace(
                    event_ref="event:proposal:old-expression",
                    trigger_ref=old_proposal.trigger_ref,
                    proposal_id=old_proposal.proposal_id,
                    proposal_json=old_proposal.model_dump_json(),
                ),
                SimpleNamespace(
                    event_ref="event:proposal:new-observation",
                    trigger_ref=source_event.event_id,
                    proposal_id="proposal:new-observation",
                ),
            ),
            expression_plan_manifests=(
                SimpleNamespace(proposal_id="proposal:new-observation"),
            ),
            expression_beats=(
                SimpleNamespace(
                    beat_id="beat:expression:2",
                    proposal_id=old_proposal.proposal_id,
                    payload_ref="payload:expression:pending",
                    event_ref="event:beat:2",
                ),
            ),
            stored_message_payloads=(
                SimpleNamespace(
                    payload_ref="payload:expression:previous",
                    text="上一条已经发出的消息。",
                ),
                SimpleNamespace(
                    payload_ref="payload:expression:pending",
                    text="这条是尚未发出的旧表达。",
                ),
            ),
        )

    model = _DecisionModel('{"disposition":"merge"}')

    async def role_context_at(
        requested: ProjectionCursor,
        requested_event: WorldEvent,
        requested_projection: object,
    ) -> dict[str, object]:
        assert requested == cursor
        assert requested_event == source_event
        assert requested_projection is not None
        return {
            "current_self_state": {
                "contract": "current-self-state.1",
                "affect": [
                    {
                        "components": [{"dimension": "warmth", "intensity_bp": 6400}],
                        "source_ref": "affect-episode:accepted:1",
                    }
                ],
                "relationship": [
                    {
                        "stage": "close_friend",
                        "source_ref": "relationship:user:test",
                    }
                ],
                "recent_self_experiences": {
                    "availability": "available",
                    "items": [
                        {
                            "summary": "下午刚从实习下班。",
                            "source_ref": "experience:self:shift-1",
                        }
                    ],
                },
                "source_refs": [
                    "affect-episode:accepted:1",
                    "experience:self:shift-1",
                    "relationship:user:test",
                ],
            },
            "recent_dialogue": {
                "availability": "available",
                "items": [
                    {
                        "value": {
                            "speaker_ref": "user:test",
                            "text": "等等，我刚刚还有话想说。",
                        },
                        "source_ref": "dialogue:observation:user-interjection:1",
                    },
                    {
                        "value": {
                            "speaker_ref": "companion:test",
                            "text": "上一条已经发出的消息。",
                        },
                        "source_ref": "dialogue:expression:previous",
                    },
                ],
            },
        }

    decision = await AuditedReplacementReconsiderationReviewer(
        reviewer=ExpressionReconsiderationChatModelAdapter(
            model=model
        ),
        project_at=project_at,
        identity_frame=CompanionIdentityFrame(
            companion_name="枝枝",
            counterpart_name="geoff",
            personality_frame="有自己的生活和判断。",
        ),
        role_context_at=role_context_at,
    ).review(process=process, observation_event=source_event, cursor=cursor)

    assert projected_at == [cursor]
    assert decision.replacement_plan_ref == "event:proposal:new-observation"
    supplied = json.loads(model.calls[0][1]["content"])
    context = supplied["conversation_context"]
    assert context["identity_frame"]["value"]["companion_name"] == "枝枝"
    assert context["identity_frame"]["source_refs"]["stable_identity"].startswith(
        "identity-frame:sha256:"
    )
    assert context["current_self_state"]["affect"][0]["source_ref"] == (
        "affect-episode:accepted:1"
    )
    assert context["current_self_state"]["relationship"][0]["stage"] == "close_friend"
    assert context["current_self_state"]["recent_self_experiences"]["items"][0][
        "source_ref"
    ] == "experience:self:shift-1"
    assert {
        item["value"]["speaker_ref"] for item in context["recent_dialogue"]["items"]
    } == {"user:test", "companion:test"}
    assert context["old_pending_expression"] == {
        "beat_id": "beat:expression:2",
        "payload_ref": "payload:expression:pending",
        "source_ref": "event:beat:2",
        "text": "这条是尚未发出的旧表达。",
    }
    assert context["old_private_turn_state"] == {
        "value": old_private_turn_state.model_dump(mode="json"),
        "proposal_id": old_proposal.proposal_id,
        "proposal_ref": "event:proposal:old-expression",
        "authority": "turn_local_audit_only",
        "fact_authority": False,
    }
    with pytest.raises(ValueError, match="unsupported"):
        ExpressionReconsiderationChatModelAdapter._decision(
            '{"disposition":"new_beat","inline_text":"you should not see this"}'
        )


@pytest.mark.asyncio
async def test_ingress_atomically_opens_reconsideration_gate_before_any_new_turn_work() -> None:
    observation, _source_event = _observation()
    ledger = _TriggerLedger(_state())
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)

    outcome = await runtime.ingest(observation)

    assert outcome.status == "observed_only"
    assert len(ledger.project().trigger_processes) == 1
    assert ledger.project().trigger_processes[0].state == "open"
