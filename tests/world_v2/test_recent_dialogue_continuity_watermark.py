from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib

from companion_daemon.world_v2.conversation_continuity import (
    ConversationContinuityCompiler,
)
from companion_daemon.world_v2.recent_dialogue import RecentDialogueCompiler
from companion_daemon.world_v2.schemas import (
    Action,
    CommittedWorldEventRef,
    ExecutionReceipt,
    ExpressionPlanManifestBeatRef,
    ExpressionPlanManifestRef,
    ExpressionPlanProjection,
    LedgerProjection,
    MessageObservationRef,
    Observation,
    StoredMessagePayloadProjection,
    WorldEvent,
)


NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)
WORLD_ID = "world:recent-dialogue-ack-watermark"


class _EventLookup:
    def __init__(self, events: tuple[WorldEvent, ...]) -> None:
        self._events = {item.event_id: item for item in events}

    def lookup_event_commit(self, event_id: str):  # type: ignore[no-untyped-def]
        event = self._events.get(event_id)
        return None if event is None else (event, object())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _event_ref(event: WorldEvent, *, world_revision: int) -> CommittedWorldEventRef:
    return CommittedWorldEventRef(
        event_id=event.event_id,
        event_type=event.event_type,
        world_revision=world_revision,
        payload_hash=event.payload_hash,
        logical_time=event.logical_time,
    )


def _world_event(
    *,
    event_id: str,
    event_type: str,
    logical_time: datetime,
    payload: dict[str, object],
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=logical_time,
        created_at=logical_time,
        actor="system:test",
        source="test",
        trace_id=f"trace:{event_id}",
        causation_id=f"cause:{event_id}",
        correlation_id="conversation:ack-watermark",
        idempotency_key=f"idempotency:{event_id}",
        payload=payload,
    )


def test_answered_observation_outside_companion_tail_does_not_take_pending_slot() -> None:
    events: list[WorldEvent] = []
    refs: list[CommittedWorldEventRef] = []
    observations: list[MessageObservationRef] = []
    manifests: list[ExpressionPlanManifestRef] = []
    plans: list[ExpressionPlanProjection] = []
    actions: list[Action] = []
    receipts: list[ExecutionReceipt] = []
    stored_payloads: list[StoredMessagePayloadProjection] = []
    revision = 0

    def append_event(event: WorldEvent) -> CommittedWorldEventRef:
        nonlocal revision
        revision += 1
        events.append(event)
        ref = _event_ref(event, world_revision=revision)
        refs.append(ref)
        return ref

    for index in range(1, 6):
        at = NOW + timedelta(minutes=index)
        observation = Observation(
            schema_version="world-v2.1",
            observation_id=f"observation:{index}",
            world_id=WORLD_ID,
            logical_time=at,
            created_at=at,
            trace_id=f"trace:observation:{index}",
            causation_id=f"message:{index}",
            correlation_id="conversation:ack-watermark",
            source="platform:test",
            source_event_id=f"message:{index}",
            actor="user:primary",
            channel="qq_c2c",
            payload_ref=f"payload:observation:{index}",
            payload_hash="sha256:" + _hash(f"用户消息 {index}"),
            text=f"用户消息 {index}",
            received_at=at,
        )
        observation_event = _world_event(
            event_id=f"event:observation:{index}",
            event_type="ObservationRecorded",
            logical_time=at,
            payload=observation.model_dump(mode="json"),
        )
        observation_ref = append_event(observation_event)
        observations.append(
            MessageObservationRef(
                observation_id=observation.observation_id,
                source=observation.source,
                source_event_id=observation.source_event_id,
                content_payload_hash=observation.payload_hash,
                event_payload_hash=observation_event.payload_hash,
                world_revision=observation_ref.world_revision,
                actor=observation.actor,
                channel=observation.channel,
                payload_ref=observation.payload_ref,
            )
        )

        plan_id = f"plan:{index}"
        beat_id = f"beat:{index}"
        action_id = f"action:{index}"
        acceptance_event = _world_event(
            event_id=f"event:acceptance:{index}",
            event_type="ExpressionPlanAccepted",
            logical_time=at,
            payload={"plan_id": plan_id},
        )
        append_event(acceptance_event)
        payload_event = _world_event(
            event_id=f"event:payload:{index}",
            event_type="ExpressionPayloadStored",
            logical_time=at,
            payload={"payload_ref": f"payload:reply:{index}"},
        )
        append_event(payload_event)
        receipt = ExecutionReceipt(
            receipt_id=f"receipt:{index}",
            result_id=f"result:{index}",
            action_id=action_id,
            provider="test",
            provider_ref=f"provider-message:{index}",
            source_event_id=f"provider-event:{index}",
            receipt_kind="ack",
            observed_state="provider_accepted",
            is_terminal=False,
            cost_actual=0,
            received_at=at + timedelta(seconds=1),
            raw_payload_hash=_hash(f"receipt:{index}"),
        )
        receipt_event = _world_event(
            event_id=f"event:receipt:{index}",
            event_type="ExecutionReceiptRecorded",
            logical_time=at,
            payload={"receipt": receipt.model_dump(mode="json")},
        )
        append_event(receipt_event)

        action = Action.model_construct(
            action_id=action_id,
            expression_plan_id=plan_id,
            expression_beat_id=beat_id,
            state="provider_accepted",
        )
        actions.append(action)
        receipts.append(receipt)
        plans.append(
            ExpressionPlanProjection.model_construct(
                plan_id=plan_id,
                state="authorized",
                history=(),
            )
        )
        payload_hash = "sha256:" + _hash(f"角色回复 {index}")
        stored_payloads.append(
            StoredMessagePayloadProjection(
                acceptance_id=f"acceptance:{index}",
                proposal_id=f"proposal:{index}",
                payload_ref=f"payload:reply:{index}",
                payload_hash=payload_hash,
                text=f"角色回复 {index}",
                content_type="text/plain",
                event_ref=payload_event.event_id,
                event_payload_hash=payload_event.payload_hash,
            )
        )
        beat = ExpressionPlanManifestBeatRef.model_construct(
            beat_id=beat_id,
            payload_ref=f"payload:reply:{index}",
            payload_hash=payload_hash,
            text=f"角色回复 {index}",
            action=action,
        )
        manifests.append(
            ExpressionPlanManifestRef.model_construct(
                acceptance_id=f"acceptance:{index}",
                proposal_id=f"proposal:{index}",
                plan_id=plan_id,
                acceptance_event_ref=acceptance_event.event_id,
                social_source_observation_event_ref=observation_event.event_id,
                beats=(beat,),
            )
        )

    current_at = NOW + timedelta(minutes=6)
    current_observation = Observation(
        schema_version="world-v2.1",
        observation_id="observation:current",
        world_id=WORLD_ID,
        logical_time=current_at,
        created_at=current_at,
        trace_id="trace:observation:current",
        causation_id="message:current",
        correlation_id="conversation:ack-watermark",
        source="platform:test",
        source_event_id="message:current",
        actor="user:primary",
        channel="qq_c2c",
        payload_ref="payload:observation:current",
        payload_hash="sha256:" + _hash("当前消息"),
        text="当前消息",
        received_at=current_at,
    )
    current_event = _world_event(
        event_id="event:observation:current",
        event_type="ObservationRecorded",
        logical_time=current_at,
        payload=current_observation.model_dump(mode="json"),
    )
    current_ref = append_event(current_event)
    observations.append(
        MessageObservationRef(
            observation_id=current_observation.observation_id,
            source=current_observation.source,
            source_event_id=current_observation.source_event_id,
            content_payload_hash=current_observation.payload_hash,
            event_payload_hash=current_event.payload_hash,
            world_revision=current_ref.world_revision,
            actor=current_observation.actor,
            channel=current_observation.channel,
            payload_ref=current_observation.payload_ref,
        )
    )

    projection = LedgerProjection.model_construct(
        committed_world_event_refs=tuple(refs),
        message_observations=tuple(observations),
        expression_plan_manifests=tuple(manifests),
        minimal_reply_manifests=(),
        proposal_audits=(),
        expression_plans=tuple(plans),
        actions=tuple(actions),
        execution_receipts=tuple(receipts),
        stored_message_payloads=tuple(stored_payloads),
        expression_payload_descriptors=(),
    )
    compiler = RecentDialogueCompiler(
        ledger=_EventLookup(tuple(events)),  # type: ignore[arg-type]
        max_companion_items=4,
    )
    compiled = compiler.compile_with_acknowledgements(
        projection=projection,
        actor_ref="agent:companion",
        subject_refs=frozenset({"user:primary"}),
    )
    replayed = compiler.compile_with_acknowledgements(
        projection=projection.model_copy(
            update={"expression_plan_manifests": tuple(reversed(manifests))}
        ),
        actor_ref="agent:companion",
        subject_refs=frozenset({"user:primary"}),
    )
    accepted_but_not_visible = compiler.compile_with_acknowledgements(
        projection=projection.model_copy(
            update={
                "actions": (
                    actions[0].model_copy(update={"state": "authorized"}),
                    *actions[1:],
                ),
                "execution_receipts": tuple(receipts[1:]),
            }
        ),
        actor_ref="agent:companion",
        subject_refs=frozenset({"user:primary"}),
    )
    unknown_after_acceptance = ExecutionReceipt(
        receipt_id="receipt:1:unknown",
        result_id="result:1:unknown",
        action_id=actions[0].action_id,
        provider="test",
        provider_ref="provider-message:1",
        source_event_id="provider-event:1:unknown",
        receipt_kind="terminal",
        observed_state="unknown",
        is_terminal=True,
        cost_actual=0,
        received_at=NOW + timedelta(minutes=7),
        raw_payload_hash=_hash("receipt:1:unknown"),
    )
    unknown_after_acceptance_event = _world_event(
        event_id="event:receipt:1:unknown",
        event_type="ExecutionReceiptRecorded",
        logical_time=NOW + timedelta(minutes=7),
        payload={"receipt": unknown_after_acceptance.model_dump(mode="json")},
    )
    unknown_after_acceptance_ref = append_event(unknown_after_acceptance_event)
    accepted_then_unknown_projection = projection.model_copy(
        update={
            "committed_world_event_refs": (
                *projection.committed_world_event_refs,
                unknown_after_acceptance_ref,
            ),
            "expression_plan_manifests": (manifests[0],),
            "actions": (
                actions[0].model_copy(update={"state": "unknown"}),
                *actions[1:],
            ),
            "execution_receipts": (*receipts, unknown_after_acceptance),
        }
    )
    cold_replayed_after_unknown = RecentDialogueCompiler(
        ledger=_EventLookup(tuple(events)),  # type: ignore[arg-type]
        max_companion_items=4,
    ).compile_with_acknowledgements(
        projection=accepted_then_unknown_projection,
        actor_ref="agent:companion",
        subject_refs=frozenset({"user:primary"}),
    )
    never_accepted_unknown_projection = projection.model_copy(
        update={
            "committed_world_event_refs": (
                *(
                    item
                    for item in projection.committed_world_event_refs
                    if item.event_id != "event:receipt:1"
                ),
                unknown_after_acceptance_ref,
            ),
            "expression_plan_manifests": (manifests[0],),
            "actions": (
                actions[0].model_copy(update={"state": "unknown"}),
                *actions[1:],
            ),
            "execution_receipts": (unknown_after_acceptance,),
        }
    )
    cold_replayed_never_accepted = RecentDialogueCompiler(
        ledger=_EventLookup(tuple(events)),  # type: ignore[arg-type]
        max_companion_items=4,
    ).compile_with_acknowledgements(
        projection=never_accepted_unknown_projection,
        actor_ref="agent:companion",
        subject_refs=frozenset({"user:primary"}),
    )

    assert len(tuple(item for item in compiled.dialogue if item.speaker == "companion")) == 4
    assert "event:observation:1" in compiled.acknowledged_observation_event_refs
    assert replayed == compiled
    assert (
        "event:observation:1"
        not in accepted_but_not_visible.acknowledged_observation_event_refs
    )
    assert (
        "event:observation:1"
        in cold_replayed_after_unknown.acknowledged_observation_event_refs
    )
    assert any(
        item.dialogue_id == "dialogue:expression:plan:1:beat:1"
        and item.delivery_state == "provider_accepted"
        for item in cold_replayed_after_unknown.dialogue
    )
    assert (
        "event:observation:1"
        not in cold_replayed_never_accepted.acknowledged_observation_event_refs
    )
    assert all(
        item.dialogue_id != "dialogue:expression:plan:1:beat:1"
        for item in cold_replayed_never_accepted.dialogue
    )

    continuity = ConversationContinuityCompiler(max_items=4).compile(
        dialogue=compiled.dialogue,
        trigger_ref=current_event.event_id,
        acknowledged_observation_event_refs=(compiled.acknowledged_observation_event_refs),
    )

    selected = {item.dialogue_id: item for item in continuity.dialogue}
    assert "dialogue:observation:observation:1" not in selected
    assert all("pending_interaction" not in item.continuity_reasons for item in continuity.dialogue)
