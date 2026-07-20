from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.batch_invariants import validate_commit_batch
from companion_daemon.world_v2.context_capsule import ContextCapsuleCompiler
from companion_daemon.world_v2.action_pump import ActionPump
from companion_daemon.world_v2.deferred_reply_runtime import DeferredReplyRuntime
from companion_daemon.world_v2.deliberation import Deliberation, ModelRoute, RouteRequest
from companion_daemon.world_v2.expression_plan_acceptance import ExpressionPlanBudgetPolicy
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import context_capsule_compiler_from_ledger
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    BudgetAccount, ClockObservation, Observation, ProjectionCursor, ProviderReceipt, WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.expression_reconsideration_runtime import ExpressionReconsiderationRuntime
from companion_daemon.world_v2.social_action_acceptance import (
    SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION,
    SocialDeferredPolicy,
)
from companion_daemon.world_v2.social_action_acceptance import social_deferred_manifest_hash
from companion_daemon.world_v2.social_action_draft import SocialActionDraftDeliberationAdapter
from companion_daemon.world_v2.social_action_worker import SocialActionWorker


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
WORLD = "world:social-action"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _ChatModel:
    model = "fixture:social"

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = 0

    async def complete(self, messages, *, temperature=0.8):
        del messages, temperature
        self.calls += 1
        return self.output


class _BarrierChatModel(_ChatModel):
    def __init__(self, output: str) -> None:
        super().__init__(output)
        self.ready = asyncio.Event()

    async def complete(self, messages, *, temperature=0.8):
        del messages, temperature
        self.calls += 1
        if self.calls == 2:
            self.ready.set()
        await self.ready.wait()
        return self.output


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, world_id=WORLD,
        event_type=event_type, logical_time=NOW, created_at=NOW, actor="system:test",
        source="test", trace_id="trace:social", causation_id=event_id,
        correlation_id="conversation:social", idempotency_key=event_id, payload=payload,
    )


def _observation(*, suffix: str = "1", text: str = "你先忙，晚点再聊。") -> Observation:
    return Observation(
        schema_version="world-v2.1", observation_id=f"observation:social:{suffix}", world_id=WORLD,
        logical_time=NOW, created_at=NOW, trace_id="trace:social", causation_id=f"inbound:{suffix}",
        correlation_id="conversation:social", source="test", source_event_id=f"message:{suffix}",
        actor="user:primary", channel="test", payload_ref=f"payload:source:{suffix}",
        payload_hash="sha256:" + suffix[0] * 64, text=text, received_at=NOW,
        reply_context={"target": "user:primary"},
    )


async def _setup(*, output: str, budget_limit: int = 100):
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit((_event("event:start", "WorldStarted", {}),),
        expected_world_revision=0, expected_deliberation_revision=0)
    projection = ledger.project()
    if projection.logical_time != NOW:
        ledger.commit((_event("event:clock", "ClockAdvanced", {
            "logical_time_from": (projection.logical_time or NOW - timedelta(seconds=1)).isoformat(),
            "logical_time_to": NOW.isoformat(),
        }),), expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision)
    projection = ledger.project()
    ledger.commit((_event("event:budget", "BudgetAccountConfigured", {"account": BudgetAccount(
        account_id="account:chat", category="chat", window_id="window:1", limit=budget_limit,
    ).model_dump(mode="json")}),), expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision)
    await WorldRuntime(world_id=WORLD, ledger=ledger).ingest(_observation())
    model = _ChatModel(output)
    worker = _make_worker(ledger=ledger, issuer=issuer, model=model)
    return ledger, worker, model


def _make_worker(*, ledger, issuer: AcceptedLedgerBatchIssuer, model: _ChatModel) -> SocialActionWorker:
    adapter = SocialActionDraftDeliberationAdapter(model=model)
    capsules: ContextCapsuleCompiler = context_capsule_compiler_from_ledger(ledger=ledger)
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=capsules,
        deliberation=Deliberation(router=_Router(), main_model=adapter, quick_recovery=adapter),
        companion_actor_ref="actor:companion",
    )
    return SocialActionWorker(
        ledger=ledger, pinned_turn=turn, batch_issuer=issuer,
        policy=SocialDeferredPolicy(expression=ExpressionPlanBudgetPolicy(
            account_id="account:chat", amount_limit_per_action=3, actor="actor:companion",
            allowed_targets=("user:primary",), recovery_policy="effect_once",
        )),
    )


class _CancelReviewer:
    async def review(self, **_kwargs):
        return "cancel"


class _DeliveredExecutor:
    async def dispatch(self, action):
        return ProviderReceipt(
            provider_receipt_id="receipt:social-followup", action_id=action.action_id,
            idempotency_key=action.idempotency_key, provider="fixture:chat",
            provider_ref="provider:social-followup", status="delivered", cost_actual=1,
            received_at=NOW + timedelta(minutes=1), raw_payload_hash="f" * 64,
        )

    async def lookup_result(self, action):
        del action
        return None


class _CrashAfterCancellationRuntime(ExpressionReconsiderationRuntime):
    async def _release_interrupted_commitment(self, **_kwargs) -> None:
        raise RuntimeError("simulated crash after cancellation commit")


@pytest.mark.asyncio
async def test_model_defer_atomically_opens_commitment_expression_budget_and_followup() -> None:
    ledger, worker, model = await _setup(output=(
        '{"choice":"defer","response_text":"忙完我来找你。","delay_seconds":60,'
        '"expires_after_seconds":600,"brief_rationale":"手头的事情尚未结束","confidence":7200}'
    ))

    result = await worker.run_observation("observation:social:1")
    duplicate = await worker.run_observation("observation:social:1")
    projection = ledger.project()

    assert result.status == "deferred"
    assert duplicate.status == "duplicate"
    assert model.calls == 1
    assert len(projection.commitments) == len(projection.expression_plans) == len(projection.threads) == 1
    assert len(projection.expression_beats) == len(projection.budget_reservations) == 1
    assert projection.actions[0].kind == "followup"
    assert projection.actions[0].not_before == NOW + timedelta(seconds=60)
    assert projection.commitments[0].values.fulfillment_contract.expected_action_id == result.action_id
    assert projection.threads[0].values.kind == "reply_reconsideration"
    assert projection.threads[0].values.due_window == projection.commitments[0].values.due_window


@pytest.mark.asyncio
async def test_model_no_reply_is_a_durable_terminal_audit_without_action() -> None:
    ledger, worker, model = await _setup(output=(
        '{"choice":"no_reply","brief_rationale":"关系尚浅且这句话无需接续","confidence":6100}'
    ))

    first = await worker.run_observation("observation:social:1")
    second = await worker.run_observation("observation:social:1")

    assert first.status == second.status == "no_reply"
    assert model.calls == 1
    assert len(ledger.project().proposal_audits) == 1
    assert ledger.project().actions == ()
    assert ledger.project().commitments == ()
    terminal = next(item for item in ledger.project().trigger_processes
        if item.process_kind == "social_action_deliberation")
    assert terminal.state == "terminal"
    assert terminal.source_evidence_ref == "event:trigger:observation:test:message:1"
    assert terminal.runtime_outcome_ref == f"social-action-decision:no_reply:{first.proposal_id}"


@pytest.mark.asyncio
async def test_model_reply_now_remains_an_inert_proposal_for_the_existing_reply_lane() -> None:
    ledger, worker, model = await _setup(output=(
        '{"choice":"reply_now","response_text":"我在。","brief_rationale":"现在回应更自然",'
        '"confidence":6800}'
    ))

    result = await worker.run_observation("observation:social:1")

    assert result.status == "reply_now_proposed"
    assert model.calls == 1
    assert len(ledger.project().proposal_audits) == 1
    assert ledger.project().actions == ()


@pytest.mark.asyncio
async def test_budget_exhaustion_does_not_authorize_partial_social_effects_or_repeat_model() -> None:
    ledger, worker, model = await _setup(output=(
        '{"choice":"defer","response_text":"晚些说。","delay_seconds":30,'
        '"expires_after_seconds":300,"brief_rationale":"暂时无法完整回应","confidence":7000}'
    ), budget_limit=2)

    first = await worker.run_observation("observation:social:1")
    second = await worker.run_observation("observation:social:1")

    assert first.status == second.status == "budget_exhausted"
    assert model.calls == 1
    assert ledger.project().actions == ()
    assert ledger.project().commitments == ()
    assert ledger.project().expression_plans == ()
    terminal = next(item for item in ledger.project().trigger_processes
        if item.process_kind == "social_action_deliberation")
    assert terminal.runtime_outcome_ref == (
        f"social-action-decision:budget_exhausted:{first.proposal_id}"
    )


@pytest.mark.asyncio
async def test_new_user_interjection_gates_and_can_cancel_deferred_followup() -> None:
    ledger, worker, _model = await _setup(output=(
        '{"choice":"defer","response_text":"晚些继续。","delay_seconds":60,'
        '"expires_after_seconds":600,"brief_rationale":"现在先停一下","confidence":7000}'
    ))
    accepted = await worker.run_observation("observation:social:1")
    assert accepted.status == "deferred"

    await WorldRuntime(world_id=WORLD, ledger=ledger).ingest(
        _observation(suffix="2", text="等等，不用回刚才那句了。")
    )
    projection = ledger.project()
    gate = next(item for item in projection.trigger_processes
        if item.process_kind == "expression_reconsideration")
    assert gate.state == "open"

    reviewed = await ExpressionReconsiderationRuntime(
        ledger=ledger, owner_id="worker:reconsider", reviewer=_CancelReviewer(),
    ).drain_one()

    assert reviewed.status == "cancelled"
    projection = ledger.project()
    assert next(item for item in projection.actions if item.action_id == accepted.action_id).state == "cancelled"
    assert projection.budget_reservations[0].state == "released"
    commitment = next(item for item in projection.commitments
        if item.commitment_id == accepted.commitment_id)
    assert commitment.values.status == "released"
    assert commitment.values.settlement_reason_code == "user_withdrew"


@pytest.mark.asyncio
async def test_cancelled_followup_commitment_recovers_after_restart_gap() -> None:
    ledger, worker, _model = await _setup(output=(
        '{"choice":"defer","response_text":"等会儿说。","delay_seconds":60,'
        '"expires_after_seconds":600,"brief_rationale":"稍后接续","confidence":7000}'
    ))
    accepted = await worker.run_observation("observation:social:1")
    await WorldRuntime(world_id=WORLD, ledger=ledger).ingest(
        _observation(suffix="2", text="不用再回上一句了。")
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await _CrashAfterCancellationRuntime(
            ledger=ledger, owner_id="worker:crash", reviewer=_CancelReviewer(),
        ).drain_one()
    crashed_projection = ledger.project()
    assert crashed_projection.expression_plans[0].state == "terminated"
    assert crashed_projection.expression_beats[0].state == "terminated"
    assert crashed_projection.actions[0].state == "cancelled"
    assert crashed_projection.budget_reservations[0].state == "released"
    assert next(item for item in ledger.project().commitments
        if item.commitment_id == accepted.commitment_id).values.status == "open"

    recovered = await ExpressionReconsiderationRuntime(
        ledger=ledger, owner_id="worker:restart", reviewer=_CancelReviewer(),
    ).drain_one()
    assert recovered.status == "idle"
    assert next(item for item in ledger.project().commitments
        if item.commitment_id == accepted.commitment_id).values.status == "released"


@pytest.mark.asyncio
async def test_concurrent_workers_create_only_one_deferred_effect_chain() -> None:
    ledger, _worker, _model = await _setup(output=(
        '{"choice":"no_reply","brief_rationale":"setup only","confidence":6000}'
    ))
    model = _BarrierChatModel(
        '{"choice":"defer","response_text":"我一会儿回来。","delay_seconds":60,'
        '"expires_after_seconds":600,"brief_rationale":"稍后接续","confidence":7000}'
    )
    issuer = ledger._accepted_batch_issuer
    workers = (
        _make_worker(ledger=ledger, issuer=issuer, model=model),
        _make_worker(ledger=ledger, issuer=issuer, model=model),
    )

    results = await asyncio.gather(*(
        item.run_observation("observation:social:1") for item in workers
    ))

    assert {item.status for item in results} <= {"deferred", "stale", "duplicate"}
    assert sum(item.status == "deferred" for item in results) == 1
    projection = ledger.project()
    assert len(projection.actions) == len(projection.commitments) == len(projection.threads) == 1
    assert len(projection.expression_plans) == len(projection.budget_reservations) == 1


@pytest.mark.asyncio
async def test_restart_recovers_acceptance_committed_before_terminal_decision() -> None:
    ledger, worker, model = await _setup(output=(
        '{"choice":"defer","response_text":"我稍后回来。","delay_seconds":60,'
        '"expires_after_seconds":600,"brief_rationale":"稍后接续","confidence":7000}'
    ))
    original = worker._record_terminal_decision

    def crash_after_acceptance(**_kwargs):
        raise RuntimeError("simulated crash before terminal decision")

    worker._record_terminal_decision = crash_after_acceptance
    with pytest.raises(RuntimeError, match="simulated crash"):
        await worker.run_observation("observation:social:1")
    assert len(ledger.project().actions) == len(ledger.project().commitments) == 1
    assert not any(item.process_kind == "social_action_deliberation"
        for item in ledger.project().trigger_processes)

    worker._record_terminal_decision = original
    recovered = await worker.run_observation("observation:social:1")
    assert recovered.status == "duplicate"
    assert model.calls == 1
    terminal = next(item for item in ledger.project().trigger_processes
        if item.process_kind == "social_action_deliberation")
    assert terminal.state == "terminal"


@pytest.mark.asyncio
async def test_social_manifest_cannot_cross_bind_source_or_bypass_recorder() -> None:
    ledger, worker, _model = await _setup(output=(
        '{"choice":"defer","response_text":"晚点见。","delay_seconds":60,'
        '"expires_after_seconds":600,"brief_rationale":"稍后接续","confidence":7000}'
    ))
    accepted = await worker.run_observation("observation:social:1")
    assert accepted.status == "deferred"
    committed_events = tuple(item.event for item in ledger._events)
    acceptance_index = next(
        index
        for index, event in enumerate(committed_events)
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION
    )
    events = committed_events[acceptance_index:acceptance_index + 9]
    evaluated = events[0].payload()["evaluated_world_revision"]

    with pytest.raises(ValueError, match="recorder_capability_required"):
        validate_commit_batch(
            events, expected_world_revision=evaluated, accepted_manifest_v3_authorized=False
        )

    raw = events[0].payload()
    raw["source_observation_id"] = "observation:social:forged"
    raw["manifest_hash"] = social_deferred_manifest_hash(raw)
    forged = WorldEvent.from_payload(
        schema_version=events[0].schema_version,
        event_id=events[0].event_id,
        world_id=events[0].world_id,
        event_type=events[0].event_type,
        logical_time=events[0].logical_time,
        created_at=events[0].created_at,
        actor=events[0].actor,
        source=events[0].source,
        trace_id=events[0].trace_id,
        causation_id=events[0].causation_id,
        correlation_id=events[0].correlation_id,
        idempotency_key=events[0].idempotency_key,
        payload=raw,
    )
    with pytest.raises(ValueError, match="does_not_match_manifest"):
        validate_commit_batch(
            (forged, *events[1:]),
            expected_world_revision=evaluated,
            accepted_manifest_v3_authorized=True,
        )

    # A forged source event ref/hash can be internally self-consistent while
    # pointing at the wrong committed event.  Replay the exact prefix and
    # prove the reducer cross-checks it against the audit trigger.
    clone_issuer = AcceptedLedgerBatchIssuer()
    clone = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=clone_issuer)
    accepted_event_ids = {item.event_id for item in events}
    prefix_commits = [
        stored
        for stored in ledger._commit_events.values()
        if not any(item.event.event_id in accepted_event_ids for item in stored)
    ]
    for index, stored in enumerate(prefix_commits):
        cursor = ProjectionCursor(
            world_revision=clone.project().world_revision,
            deliberation_revision=clone.project().deliberation_revision,
            ledger_sequence=clone.project().ledger_sequence,
        )
        clone.commit_at_cursor(
            tuple(item.event for item in stored), expected_cursor=cursor,
            commit_id=f"clone-prefix:{index}"
        )
    start = clone.lookup_event_commit("event:start")[0]
    cross = events[0].payload()
    cross["source_observation_event_ref"] = start.event_id
    cross["manifest_hash"] = social_deferred_manifest_hash(cross)
    cross_event = WorldEvent.from_payload(
        schema_version=events[0].schema_version, event_id=events[0].event_id,
        world_id=events[0].world_id, event_type=events[0].event_type,
        logical_time=events[0].logical_time, created_at=events[0].created_at,
        actor=events[0].actor, source=events[0].source, trace_id=events[0].trace_id,
        causation_id=events[0].causation_id, correlation_id=events[0].correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type=events[0].event_type, world_id=WORLD, payload=cross
        ) or "unreachable",
        payload=cross,
    )
    cursor = ProjectionCursor(
        world_revision=clone.project().world_revision,
        deliberation_revision=clone.project().deliberation_revision,
        ledger_sequence=clone.project().ledger_sequence,
    )
    handle = clone_issuer.issue(
        world_id=WORLD, expected_cursor=cursor, events=(cross_event, *events[1:]),
        manifest_hash=cross["manifest_hash"], registry_digest="a" * 64,
        commit_id="commit:forged-cross-source",
    )
    with pytest.raises(ValueError, match="source or policy separation"):
        clone.commit_accepted(handle, expected_cursor=cursor)


@pytest.mark.asyncio
async def test_delivered_followup_receipt_fulfills_exact_social_commitment() -> None:
    ledger, worker, _model = await _setup(output=(
        '{"choice":"defer","response_text":"我忙完了。","delay_seconds":60,'
        '"expires_after_seconds":600,"brief_rationale":"稍后自然接续","confidence":7600}'
    ))
    accepted = await worker.run_observation("observation:social:1")
    assert accepted.status == "deferred" and accepted.action_id is not None
    deferred = DeferredReplyRuntime(ledger=ledger)
    before = ledger.project()
    clock = ClockObservation(
        schema_version="world-v2.1", tick_id="tick:social-due", world_id=WORLD,
        logical_time=NOW + timedelta(minutes=1), created_at=NOW + timedelta(minutes=1),
        trace_id="trace:social", causation_id="cause:clock", correlation_id="conversation:social",
        logical_time_from=NOW, logical_time_to=NOW + timedelta(minutes=1), reason="test",
    )
    runtime = WorldRuntime(world_id=WORLD, ledger=ledger)
    await runtime.advance(clock)
    clock_event = ledger.lookup_event_commit("event:trigger:clock:tick:social-due")[0]
    current = ledger.project()
    ledger.commit_at_cursor(
        deferred.clock_events(projection=before, clock_event=clock_event),
        expected_cursor=ProjectionCursor(world_revision=current.world_revision,
            deliberation_revision=current.deliberation_revision,
            ledger_sequence=current.ledger_sequence),
        commit_id="commit:social-due",
    )
    pump = ActionPump(ledger=ledger, executor=_DeliveredExecutor(), settle=runtime.settle,
        owner_id="worker:action")
    await pump.drain_once()
    await pump.drain_once()
    deferred.settle_terminal_action(
        action_id=accepted.action_id, logical_time=NOW + timedelta(minutes=1),
        created_at=NOW + timedelta(minutes=1), trace_id="trace:social",
        causation_id="cause:receipt", correlation_id="conversation:social",
    )
    projection = ledger.project()
    assert next(item for item in projection.actions if item.action_id == accepted.action_id).state == "delivered"
    assert next(item for item in projection.commitments
        if item.commitment_id == accepted.commitment_id).values.status == "fulfilled"


@pytest.mark.asyncio
async def test_sqlite_restart_joins_accepted_defer_without_second_model_call(tmp_path) -> None:
    path = tmp_path / "social-action.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit((_event("event:start", "WorldStarted", {}),),
        expected_world_revision=0, expected_deliberation_revision=0)
    projection = ledger.project()
    if projection.logical_time != NOW:
        ledger.commit((_event("event:clock", "ClockAdvanced", {
            "logical_time_from": (projection.logical_time or NOW - timedelta(seconds=1)).isoformat(),
            "logical_time_to": NOW.isoformat(),
        }),), expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision)
    projection = ledger.project()
    ledger.commit((_event("event:budget", "BudgetAccountConfigured", {"account": BudgetAccount(
        account_id="account:chat", category="chat", window_id="window:1", limit=100,
    ).model_dump(mode="json")}),), expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision)
    await WorldRuntime(world_id=WORLD, ledger=ledger).ingest(_observation())
    first_model = _ChatModel(
        '{"choice":"defer","response_text":"等我一下。","delay_seconds":60,'
        '"expires_after_seconds":600,"brief_rationale":"稍后回来","confidence":7000}'
    )
    first = await _make_worker(ledger=ledger, issuer=issuer, model=first_model).run_observation(
        "observation:social:1"
    )
    assert first.status == "deferred"
    ledger.close()

    reopened_issuer = AcceptedLedgerBatchIssuer()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD,
        accepted_batch_issuer=reopened_issuer)
    unused_model = _ChatModel('{"choice":"no_reply","brief_rationale":"不应调用"}')
    duplicate = await _make_worker(ledger=reopened, issuer=reopened_issuer,
        model=unused_model).run_observation("observation:social:1")
    assert duplicate.status == "duplicate"
    assert duplicate.action_id == first.action_id
    assert unused_model.calls == 0
    await WorldRuntime(world_id=WORLD, ledger=reopened).ingest(
        _observation(suffix="2", text="这条不用再补了。")
    )
    cancelled = await ExpressionReconsiderationRuntime(
        ledger=reopened,
        owner_id="worker:sqlite-reconsider",
        reviewer=_CancelReviewer(),
    ).drain_one()
    assert cancelled.status == "cancelled"
    terminal_before_restart = reopened.project()
    assert terminal_before_restart.expression_plans[0].state == "terminated"
    assert terminal_before_restart.expression_beats[0].state == "terminated"
    assert reopened.rebuild() == reopened.project()
    reopened.close()

    final = SQLiteWorldLedger(
        path=path,
        world_id=WORLD,
        accepted_batch_issuer=AcceptedLedgerBatchIssuer(),
    )
    assert final.project() == terminal_before_restart
    assert final.rebuild() == terminal_before_restart
    final.close()
