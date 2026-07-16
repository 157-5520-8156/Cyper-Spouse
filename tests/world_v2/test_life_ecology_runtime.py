from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.life_ecology_runtime import (
    LifeEcologyAvailability,
    LifeEcologyRunClaim,
    LifeEcologyRuntime,
)
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, WorldEvent


NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)


def _event(name: str, event_type: str = "ClockAdvanced") -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:{name}",
        event_type=event_type,
        world_id="world:life-ecology",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:clock",
        source="test",
        trace_id="trace:wake",
        causation_id="event:world-started",
        correlation_id="correlation:wake",
        idempotency_key=f"test:{name}",
        payload={"name": name},
    )


class _Ledger:
    world_id = "world:life-ecology"
    blocks_event_loop = False

    def __init__(self, event: WorldEvent | None) -> None:
        self.event = event
        self._projection = SimpleNamespace(
            logical_time=NOW,
            world_revision=7,
            deliberation_revision=3,
            ledger_sequence=11,
            committed_world_event_refs=()
            if event is None
            else (
                CommittedWorldEventRef(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    world_revision=7,
                    payload_hash=event.payload_hash,
                    logical_time=event.logical_time,
                ),
            ),
        )

    def project(self):  # type: ignore[no-untyped-def]
        return self._projection

    def lookup_event_commit(self, event_id: str):  # type: ignore[no-untyped-def]
        if self.event is None or event_id != self.event.event_id:
            return None
        return self.event, SimpleNamespace(world_revision=7, deliberation_revision=3, ledger_sequence=11)


class _TriggerStore:
    def __init__(self, claims: list[str] | None = None) -> None:
        self._claims = iter(claims or ["owned"])
        self.claims = []
        self.completed = []

    async def claim_or_join(self, *, key, trace_id: str, correlation_id: str):  # type: ignore[no-untyped-def]
        self.claims.append((key, trace_id, correlation_id))
        state = next(self._claims)
        return LifeEcologyRunClaim(
            trigger_id=f"life-ecology:{key.wake_event_ref}", state=state
        )

    async def complete(self, *, key, trigger_id: str, outcome: str):  # type: ignore[no-untyped-def]
        self.completed.append((key, trigger_id, outcome))


class _Media:
    def __init__(self, *, status: str = "idle") -> None:
        self.status = status
        self.calls = []

    def drain_once(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return SimpleNamespace(status=self.status)


@pytest.mark.asyncio
async def test_life_ecology_rejects_a_wake_that_is_not_exactly_committed() -> None:
    event = _event("clock")
    ledger = _Ledger(event)
    # The projection and immutable event bytes disagree: a caller cannot use
    # a merely similarly named wake to reach the media ecology.
    ledger._projection.committed_world_event_refs = (
        CommittedWorldEventRef(
            event_id=event.event_id,
            event_type=event.event_type,
            world_revision=7,
            payload_hash=hashlib.sha256(b"different").hexdigest(),
            logical_time=NOW,
        ),
    )
    trigger_store, media = _TriggerStore(), _Media()
    runtime = LifeEcologyRuntime(
        ledger=ledger,
        trigger_store=trigger_store,
        media_followup=media,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id, trace_id="trace:invalid", correlation_id="correlation:invalid"
    )

    assert result.status == "rejected"
    assert result.reason_code == "life_ecology.wake_not_exactly_committed"
    assert trigger_store.claims == []
    assert media.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        "installed_but_scheduler_disabled",
        "authority_only",
        "adapter_only",
        "paused_by_budget",
        "blocked_by_missing_capability",
    ],
)
async def test_life_ecology_makes_non_active_installation_explicitly_unavailable(state: str) -> None:
    event = _event("clock")
    trigger_store, media = _TriggerStore(), _Media()
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        availability=LifeEcologyAvailability(state=state),  # type: ignore[arg-type]
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id, trace_id="trace:off", correlation_id="correlation:off"
    )

    assert runtime.availability().state == state
    assert result.status == "unavailable"
    assert result.reason_code == f"life_ecology.{state}"
    assert trigger_store.claims == []
    assert media.calls == []


@pytest.mark.asyncio
async def test_life_ecology_owns_one_valid_durable_wake_then_fans_out_once_and_reports_idle() -> None:
    event = _event("clock")
    trigger_store, media = _TriggerStore(), _Media(status="created")
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        availability=LifeEcologyAvailability(state="installed_and_active", catalog_version="life.1"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id, trace_id="trace:run", correlation_id="correlation:run"
    )

    assert result.status == "idle"
    assert result.media_followup_status == "created"
    assert len(trigger_store.claims) == len(trigger_store.completed) == len(media.calls) == 1
    key, trace_id, correlation_id = trigger_store.claims[0]
    assert (key.world_id, key.wake_event_ref, key.catalog_version) == (
        "world:life-ecology", event.event_id, "life.1"
    )
    assert (trace_id, correlation_id) == ("trace:run", "correlation:run")
    assert media.calls == [{
        "wake_event_ref": event.event_id,
        "logical_time": NOW,
        "actor": "worker:life-ecology",
        "trace_id": "trace:run",
        "correlation_id": "correlation:run",
    }]
    assert trigger_store.completed[0][2] == "idle"


@pytest.mark.asyncio
async def test_life_ecology_retries_an_exact_older_wake_at_the_current_logical_time() -> None:
    event = _event("older-clock")
    ledger = _Ledger(event)
    later = NOW + timedelta(minutes=5)
    ledger._projection.logical_time = later
    ledger._projection.world_revision = 9
    trigger_store, media = _TriggerStore(), _Media()
    runtime = LifeEcologyRuntime(
        ledger=ledger,
        trigger_store=trigger_store,
        media_followup=media,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id, trace_id="trace:late-retry", correlation_id="correlation:late-retry"
    )

    assert result.status == "idle"
    assert media.calls[0]["logical_time"] == later


@pytest.mark.asyncio
async def test_life_ecology_joins_a_completed_durable_run_without_repeating_media_followup() -> None:
    event = _event("clock")
    trigger_store, media = _TriggerStore(claims=["completed"]), _Media()
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id, trace_id="trace:replay", correlation_id="correlation:replay"
    )

    assert result.status == "joined_existing"
    assert result.reason_code == "life_ecology.run_completed"
    assert media.calls == []
    assert trigger_store.completed == []


@pytest.mark.asyncio
async def test_life_ecology_joins_an_in_progress_durable_run_without_second_media_owner() -> None:
    event = _event("clock")
    trigger_store, media = _TriggerStore(claims=["joined"]), _Media()
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id, trace_id="trace:joined", correlation_id="correlation:joined"
    )

    assert result.status == "joined_existing"
    assert result.reason_code == "life_ecology.run_in_progress"
    assert media.calls == []
    assert trigger_store.completed == []
