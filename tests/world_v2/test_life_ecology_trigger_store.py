from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

import pytest

from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.life_ecology_runtime import LifeEcologyRunKey
from companion_daemon.world_v2.life_ecology_trigger_store import (
    LedgerLifeEcologyTriggerStore,
    life_ecology_trigger_id,
)
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


WORLD_ID = "world:life-ecology-trigger-store"
START = datetime(2026, 7, 16, 11, 59, tzinfo=UTC)
NOW = START + timedelta(minutes=1)


def _clock_wake() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:life-ecology:wake:clock",
        world_id=WORLD_ID,
        event_type="ClockAdvanced",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:clock",
        source="test:life-ecology-trigger-store",
        trace_id="trace:wake",
        causation_id="event:world-started",
        correlation_id="correlation:wake",
        idempotency_key="test:life-ecology:wake:clock",
        payload={
            "logical_time_from": START.isoformat(),
            "logical_time_to": NOW.isoformat(),
        },
    )


def _ledger() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    ledger.commit(
        (_clock_wake(),), expected_world_revision=0, expected_deliberation_revision=0
    )
    return ledger


def _seed_clock(ledger) -> None:  # type: ignore[no-untyped-def]
    ledger.commit(
        (_clock_wake(),), expected_world_revision=0, expected_deliberation_revision=0
    )


def _key() -> LifeEcologyRunKey:
    return LifeEcologyRunKey(
        world_id=WORLD_ID,
        wake_event_ref="event:life-ecology:wake:clock",
        catalog_version="life-ecology.1",
    )


@pytest.mark.asyncio
async def test_ledger_store_survives_restart_and_completion_is_idempotent() -> None:
    ledger = _ledger()
    key = _key()
    first = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:first")

    owned = await first.claim_or_join(
        key=key, trace_id="trace:first", correlation_id="correlation:first"
    )
    assert owned.state == "owned"

    # A new adapter instance represents process restart: it reads the
    # committed claim instead of keeping an in-memory ownership map.
    restarted = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:restart")
    joined = await restarted.claim_or_join(
        key=key, trace_id="trace:restart", correlation_id="correlation:restart"
    )
    assert joined == owned.model_copy(update={"state": "joined"})

    await first.complete(key=key, trigger_id=owned.trigger_id, outcome="idle")
    await first.complete(key=key, trigger_id=owned.trigger_id, outcome="idle")
    completed = await restarted.claim_or_join(
        key=key, trace_id="trace:terminal", correlation_id="correlation:terminal"
    )
    assert completed == owned.model_copy(update={"state": "completed"})

    projection = ledger.project()
    assert projection.trigger_processes == ()
    assert projection.life_ecology_schedule is not None
    assert projection.life_ecology_schedule.last_outcome_ref == "life-ecology:idle"
    cadence = projection.life_ecology_schedule.next_consideration_at - NOW
    assert timedelta(minutes=45) <= cadence <= timedelta(hours=8)
    assert sum(
        item.event_type == "RandomDrawRecorded"
        for item in projection.committed_world_event_refs
    ) == 1
    assert ledger.project().world_revision == 2
    assert ledger.project().deliberation_revision == 3


@pytest.mark.asyncio
async def test_completion_retry_before_compact_schedule_watermark_is_idempotent() -> None:
    ledger = _ledger()
    store = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:first")
    first_key = _key()
    first = await store.claim_or_join(
        key=first_key, trace_id="trace:first", correlation_id="correlation:first"
    )
    await store.complete(key=first_key, trigger_id=first.trigger_id, outcome="idle")

    later = NOW + timedelta(minutes=10)
    later_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:life-ecology:wake:later",
        world_id=WORLD_ID,
        event_type="ClockAdvanced",
        logical_time=later,
        created_at=later,
        actor="worker:clock",
        source="test:life-ecology-trigger-store",
        trace_id="trace:wake:later",
        causation_id=_clock_wake().event_id,
        correlation_id="correlation:wake:later",
        idempotency_key="test:life-ecology:wake:later",
        payload={
            "logical_time_from": NOW.isoformat(),
            "logical_time_to": later.isoformat(),
        },
    )
    projection = ledger.project()
    ledger.commit(
        (later_event,),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    later_key = LifeEcologyRunKey(
        world_id=WORLD_ID,
        wake_event_ref=later_event.event_id,
        catalog_version="life-ecology.1",
    )
    second = await store.claim_or_join(
        key=later_key, trace_id="trace:later", correlation_id="correlation:later"
    )
    await store.complete(key=later_key, trigger_id=second.trigger_id, outcome="idle")
    revision_after_second = ledger.project().deliberation_revision

    await store.complete(key=first_key, trigger_id=first.trigger_id, outcome="idle")

    assert ledger.project().deliberation_revision == revision_after_second
    with pytest.raises(ValueError, match="terminal outcome conflicts"):
        await store.complete(
            key=first_key,
            trigger_id=first.trigger_id,
            outcome="provider_failed",
        )


@pytest.mark.asyncio
async def test_unprocessed_wake_before_watermark_cannot_be_forged_as_completed() -> None:
    ledger = _ledger()
    unprocessed_at = NOW + timedelta(minutes=10)
    completed_at = NOW + timedelta(minutes=20)

    def clock(event_id: str, at: datetime, previous: datetime) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=WORLD_ID,
            event_type="ClockAdvanced",
            logical_time=at,
            created_at=at,
            actor="worker:clock",
            source="test:life-ecology-trigger-store",
            trace_id=f"trace:{event_id}",
            causation_id=_clock_wake().event_id,
            correlation_id=f"correlation:{event_id}",
            idempotency_key=f"test:{event_id}",
            payload={
                "logical_time_from": previous.isoformat(),
                "logical_time_to": at.isoformat(),
            },
        )

    skipped = clock("event:life-ecology:wake:skipped", unprocessed_at, NOW)
    later = clock("event:life-ecology:wake:completed", completed_at, unprocessed_at)
    projection = ledger.project()
    ledger.commit(
        (skipped, later),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    store = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:first")
    later_key = LifeEcologyRunKey(
        world_id=WORLD_ID,
        wake_event_ref=later.event_id,
        catalog_version="life-ecology.1",
    )
    later_claim = await store.claim_or_join(
        key=later_key, trace_id="trace:later", correlation_id="correlation:later"
    )
    await store.complete(
        key=later_key,
        trigger_id=later_claim.trigger_id,
        outcome="idle",
    )
    skipped_key = LifeEcologyRunKey(
        world_id=WORLD_ID,
        wake_event_ref=skipped.event_id,
        catalog_version="life-ecology.1",
    )

    with pytest.raises(ValueError, match="unavailable"):
        await store.complete(
            key=skipped_key,
            trigger_id=life_ecology_trigger_id(
                world_id=WORLD_ID,
                wake_event_ref=skipped.event_id,
                catalog_version="life-ecology.1",
            ),
            outcome="idle",
        )
    claim = await store.claim_or_join(
        key=skipped_key,
        trace_id="trace:skipped",
        correlation_id="correlation:skipped",
    )
    assert claim.state == "owned"


@pytest.mark.asyncio
async def test_sqlite_ledger_store_restart_reads_the_same_trigger_process(tmp_path) -> None:
    path = tmp_path / "life-ecology.sqlite3"
    key = _key()
    first = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    _seed_clock(first)
    first_store = LedgerLifeEcologyTriggerStore(ledger=first, owner_id="worker:durable")
    owned = await first_store.claim_or_join(
        key=key, trace_id="trace:durable", correlation_id="correlation:durable"
    )
    first.close()

    restarted = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    restarted_store = LedgerLifeEcologyTriggerStore(
        ledger=restarted, owner_id="worker:durable"
    )
    assert await restarted_store.claim_or_join(
        key=key, trace_id="trace:restart", correlation_id="correlation:restart"
    ) == owned.model_copy(update={"state": "joined"})
    await restarted_store.complete(key=key, trigger_id=owned.trigger_id, outcome="idle")
    restarted.close()

    verified = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    terminal = await LedgerLifeEcologyTriggerStore(
        ledger=verified, owner_id="worker:later"
    ).claim_or_join(key=key, trace_id="trace:verified", correlation_id="correlation:verified")
    assert terminal == owned.model_copy(update={"state": "completed"})
    projection = verified.project()
    assert projection.trigger_processes == ()
    assert projection.life_ecology_schedule is not None
    assert projection.life_ecology_schedule.last_outcome_ref == "life-ecology:idle"
    verified.close()


@pytest.mark.asyncio
async def test_sqlite_migrates_v33_terminal_ecology_head_to_current_bundle(
    tmp_path,
) -> None:
    path = tmp_path / "life-ecology-v33.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    _seed_clock(ledger)
    store = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:migration")
    key = _key()
    claim = await store.claim_or_join(
        key=key,
        trace_id="trace:migration",
        correlation_id="correlation:migration",
    )
    await store.complete(key=key, trigger_id=claim.trigger_id, outcome="idle")
    compact = ledger.project()
    assert compact.life_ecology_schedule is not None
    assert compact.completed_trigger_ids == ()

    legacy_state = ledger._state_from_projection(compact).model_copy(  # noqa: SLF001
        update={"completed_trigger_ids": (claim.trigger_id,)}
    )
    legacy_payload = legacy_state.semantic_payload(
        world_id=WORLD_ID,
        world_revision=compact.world_revision,
        reducer_bundle_version="world-v2-reducers.33",
    )
    legacy_semantic_hash = hashlib.sha256(
        json.dumps(
            legacy_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cursor = ProjectionCursor(
        world_revision=compact.world_revision,
        deliberation_revision=compact.deliberation_revision,
        ledger_sequence=compact.ledger_sequence,
    )
    legacy_state_json = ledger._encode_state(legacy_state)  # noqa: SLF001
    canonical_legacy_state = json.dumps(
        json.loads(legacy_state_json),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_state_hash = hashlib.sha256(
        ledger._state_hash_material(  # noqa: SLF001
            canonical_state=canonical_legacy_state,
            cursor=cursor,
            reducer_bundle_version="world-v2-reducers.33",
        )
    ).hexdigest()
    ledger.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM world_v2_head_state_items WHERE world_id=?",
            (WORLD_ID,),
        )
        connection.execute(
            """UPDATE world_v2_heads
                  SET state_json=?, semantic_hash=?, state_hash=?,
                      reducer_bundle_version='world-v2-reducers.33'
                WHERE world_id=?""",
            (
                legacy_state_json,
                legacy_semantic_hash,
                "0" * 64,
                WORLD_ID,
            ),
        )

    with pytest.raises(LedgerIntegrityError, match="legacy head state hash is invalid"):
        SQLiteWorldLedger(path=path, world_id=WORLD_ID)

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_heads SET state_hash=? WHERE world_id=?",
            (legacy_state_hash, WORLD_ID),
        )

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    try:
        projection = migrated.project()
        assert projection.reducer_bundle_version == "world-v2-reducers.54"
        assert projection.completed_trigger_ids == ()
        assert projection.life_ecology_schedule == compact.life_ecology_schedule
        assert migrated.rebuild() == projection
    finally:
        migrated.close()


@pytest.mark.asyncio
async def test_ledger_store_claim_or_join_is_atomic_across_competing_instances() -> None:
    ledger = _ledger()
    key = _key()
    first = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:first")
    second = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:second")

    claims = await asyncio.gather(
        first.claim_or_join(key=key, trace_id="trace:first", correlation_id="correlation:first"),
        second.claim_or_join(key=key, trace_id="trace:second", correlation_id="correlation:second"),
    )

    assert sorted(claim.state for claim in claims) == ["joined", "owned"]
    trigger_id = life_ecology_trigger_id(
        world_id=WORLD_ID,
        wake_event_ref=key.wake_event_ref,
        catalog_version=key.catalog_version,
    )
    assert {claim.trigger_id for claim in claims} == {trigger_id}
    process = ledger.project().trigger_processes
    assert len(process) == 1
    assert process[0].state == "claimed"
    assert len(process[0].attempt_ids) == 1
    assert ledger.project().deliberation_revision == 2


@pytest.mark.asyncio
async def test_ledger_store_reclaims_only_an_expired_claim_with_preserved_lineage() -> None:
    ledger = _ledger()
    key = _key()
    first = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:first", lease_seconds=1)
    owned = await first.claim_or_join(
        key=key, trace_id="trace:first", correlation_id="correlation:first"
    )

    later = NOW + timedelta(seconds=2)
    ledger.commit(
        (
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:life-ecology:wake:later-clock",
                world_id=WORLD_ID,
                event_type="ClockAdvanced",
                logical_time=later,
                created_at=later,
                actor="worker:clock",
                source="test:life-ecology-trigger-store",
                trace_id="trace:later-clock",
                causation_id="event:life-ecology:wake:clock",
                correlation_id="correlation:later-clock",
                idempotency_key="test:life-ecology:wake:later-clock",
                payload={
                    "logical_time_from": NOW.isoformat(),
                    "logical_time_to": later.isoformat(),
                },
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=2,
    )

    recovered = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:recovery")
    claim = await recovered.claim_or_join(
        key=key, trace_id="trace:recovery", correlation_id="correlation:recovery"
    )
    assert claim == owned.model_copy(update={"state": "owned"})
    process = ledger.project().trigger_processes[0]
    assert process.state == "claimed"
    assert process.claim_lease is not None
    assert process.claim_lease.owner_id == "worker:recovery"
    assert len(process.attempt_ids) == 2
    assert ledger.project().world_revision == 2
    assert ledger.project().deliberation_revision == 3


@pytest.mark.asyncio
async def test_failed_ecology_runs_back_off_for_ten_thirty_then_120_minutes() -> None:
    ledger = _ledger()
    store = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:backoff")
    wake_ref = _key().wake_event_ref
    logical_time = NOW

    for index, delay in enumerate((600, 1800, 7200), start=1):
        key = LifeEcologyRunKey(
            world_id=WORLD_ID,
            wake_event_ref=wake_ref,
            catalog_version="life-ecology.1",
        )
        claim = await store.claim_or_join(
            key=key,
            trace_id=f"trace:backoff:{index}",
            correlation_id=f"correlation:backoff:{index}",
        )
        await store.complete(
            key=key,
            trigger_id=claim.trigger_id,
            outcome="failed_safe",
        )
        projection = ledger.project()
        assert projection.life_ecology_schedule is not None
        assert projection.life_ecology_schedule.consecutive_failures == index
        assert (
            projection.life_ecology_schedule.next_consideration_at - logical_time
        ).total_seconds() == delay
        assert projection.trigger_processes == ()
        if index == 3:
            break

        next_time = projection.life_ecology_schedule.next_consideration_at
        next_ref = f"event:life-ecology:wake:backoff:{index + 1}"
        ledger.commit(
            (
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id=next_ref,
                    world_id=WORLD_ID,
                    event_type="ClockAdvanced",
                    logical_time=next_time,
                    created_at=next_time,
                    actor="worker:clock",
                    source="test:life-ecology-trigger-store",
                    trace_id=f"trace:wake:{index + 1}",
                    causation_id=wake_ref,
                    correlation_id=f"correlation:wake:{index + 1}",
                    idempotency_key=f"test:life-ecology:wake:backoff:{index + 1}",
                    payload={
                        "logical_time_from": logical_time.isoformat(),
                        "logical_time_to": next_time.isoformat(),
                    },
                ),
            ),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
        logical_time = next_time
        wake_ref = next_ref


@pytest.mark.asyncio
async def test_structured_technical_failure_uses_backoff_and_exposes_its_code() -> None:
    ledger = _ledger()
    store = LedgerLifeEcologyTriggerStore(
        ledger=ledger, owner_id="worker:structured-failure"
    )
    key = _key()
    claim = await store.claim_or_join(
        key=key,
        trace_id="trace:structured-failure",
        correlation_id="correlation:structured-failure",
    )

    await store.complete(
        key=key,
        trigger_id=claim.trigger_id,
        outcome="technical_failure.media.type_error",
    )

    schedule = ledger.project().life_ecology_schedule
    assert schedule is not None
    assert schedule.consecutive_failures == 1
    assert schedule.last_failure_code == "media.type_error"
    assert (schedule.next_consideration_at - schedule.last_completed_at).total_seconds() == 600


@pytest.mark.asyncio
async def test_deterministic_followup_probe_does_not_move_ambient_cadence() -> None:
    ledger = _ledger()
    store = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:cadence")
    first_key = _key()
    first = await store.claim_or_join(
        key=first_key,
        trace_id="trace:cadence:first",
        correlation_id="correlation:cadence",
    )
    await store.complete(
        key=first_key,
        trigger_id=first.trigger_id,
        outcome="life_development_no_op",
    )
    original = ledger.project().life_ecology_schedule
    assert original is not None

    probe_time = NOW + timedelta(minutes=10)
    probe_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:life-ecology:wake:cadence-probe",
        world_id=WORLD_ID,
        event_type="ClockAdvanced",
        logical_time=probe_time,
        created_at=probe_time,
        actor="worker:clock",
        source="test:life-ecology-trigger-store",
        trace_id="trace:cadence:probe",
        causation_id=first_key.wake_event_ref,
        correlation_id="correlation:cadence",
        idempotency_key="test:life-ecology:wake:cadence-probe",
        payload={
            "logical_time_from": NOW.isoformat(),
            "logical_time_to": probe_time.isoformat(),
        },
    )
    projection = ledger.project()
    ledger.commit(
        (probe_event,),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    probe_key = LifeEcologyRunKey(
        world_id=WORLD_ID,
        wake_event_ref=probe_event.event_id,
        catalog_version="life-ecology.1",
    )
    probe = await store.claim_or_join(
        key=probe_key,
        trace_id="trace:cadence:probe",
        correlation_id="correlation:cadence",
    )
    await store.complete(
        key=probe_key,
        trigger_id=probe.trigger_id,
        outcome="cooldown",
    )

    assert ledger.project().life_ecology_schedule == original


@pytest.mark.asyncio
async def test_semantic_stimuli_join_open_cadence_window_without_redrawing() -> None:
    ledger = _ledger()
    store = LedgerLifeEcologyTriggerStore(
        ledger=ledger,
        owner_id="worker:semantic-window",
    )
    first_key = _key()
    first = await store.claim_or_join(
        key=first_key,
        trace_id="trace:semantic-window:first",
        correlation_id="correlation:semantic-window",
    )
    await store.complete(
        key=first_key,
        trigger_id=first.trigger_id,
        outcome="activity_transitioned",
    )
    first_schedule = ledger.project().life_ecology_schedule
    assert first_schedule is not None
    first_due = first_schedule.next_consideration_at

    second_time = NOW + timedelta(minutes=1)
    second_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:life-ecology:wake:semantic-window:second",
        world_id=WORLD_ID,
        event_type="ClockAdvanced",
        logical_time=second_time,
        created_at=second_time,
        actor="worker:clock",
        source="test:life-ecology-trigger-store",
        trace_id="trace:semantic-window:second",
        causation_id=first_key.wake_event_ref,
        correlation_id="correlation:semantic-window",
        idempotency_key="test:life-ecology:wake:semantic-window:second",
        payload={
            "logical_time_from": NOW.isoformat(),
            "logical_time_to": second_time.isoformat(),
        },
    )
    projection = ledger.project()
    ledger.commit(
        (second_event,),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    second_key = LifeEcologyRunKey(
        world_id=WORLD_ID,
        wake_event_ref=second_event.event_id,
        catalog_version="life-ecology.1",
    )
    second = await store.claim_or_join(
        key=second_key,
        trace_id="trace:semantic-window:second",
        correlation_id="correlation:semantic-window",
    )
    await store.complete(
        key=second_key,
        trigger_id=second.trigger_id,
        outcome="aftermath_occurrence_opened",
    )

    completed = ledger.project()
    assert completed.life_ecology_schedule is not None
    assert completed.life_ecology_schedule.next_consideration_at == first_due
    assert (
        completed.life_ecology_schedule.last_outcome_ref
        == "life-ecology:aftermath_occurrence_opened"
    )
    assert sum(
        item.event_type == "RandomDrawRecorded"
        for item in completed.committed_world_event_refs
    ) == 1


@pytest.mark.asyncio
async def test_successful_ecology_resets_backoff_without_adding_idle_delay() -> None:
    ledger = _ledger()
    store = LedgerLifeEcologyTriggerStore(ledger=ledger, owner_id="worker:success")
    key = _key()

    claim = await store.claim_or_join(
        key=key,
        trace_id="trace:success",
        correlation_id="correlation:success",
    )
    await store.complete(
        key=key,
        trigger_id=claim.trigger_id,
        outcome="author_planned",
    )

    schedule = ledger.project().life_ecology_schedule
    assert schedule is not None
    assert schedule.consecutive_failures == 0
    assert schedule.next_consideration_at == schedule.last_completed_at
