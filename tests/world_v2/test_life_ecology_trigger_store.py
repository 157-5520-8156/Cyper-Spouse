from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.life_ecology_runtime import LifeEcologyRunKey
from companion_daemon.world_v2.life_ecology_trigger_store import (
    LedgerLifeEcologyTriggerStore,
    life_ecology_trigger_id,
)
from companion_daemon.world_v2.schemas import WorldEvent
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

    process = ledger.project().trigger_processes
    assert len(process) == 1
    assert process[0].state == "terminal"
    assert process[0].runtime_outcome_ref == "life-ecology:idle"
    assert ledger.project().world_revision == 1
    assert ledger.project().deliberation_revision == 3


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
    assert verified.project().trigger_processes[0].runtime_outcome_ref == "life-ecology:idle"
    verified.close()


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
