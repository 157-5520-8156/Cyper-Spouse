from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.fact_memory_draft import (
    FactMemoryDraftTechnicalFailure,
)
from companion_daemon.world_v2.life_ecology_runtime import (
    LifeEcologyAvailability,
    LifeEcologyRunClaim,
    LifeEcologyRuntime,
)
from companion_daemon.world_v2.life_aftermath_runtime import (
    LifeAftermathModelFailure,
)
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    LifeEcologyScheduleProjection,
    WorldEvent,
)


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
        return self.event, SimpleNamespace(
            world_revision=7,
            deliberation_revision=3,
            ledger_sequence=11,
            event_ids=(self.event.event_id,),
        )


class _TriggerStore:
    def __init__(
        self,
        claims: list[str] | None = None,
        *,
        complete_raises: bool = False,
    ) -> None:
        self._claims = iter(claims or ["owned"])
        self.complete_raises = complete_raises
        self.claims = []
        self.completed = []

    async def claim_or_join(self, *, key, trace_id: str, correlation_id: str):  # type: ignore[no-untyped-def]
        self.claims.append((key, trace_id, correlation_id))
        state = next(self._claims)
        return LifeEcologyRunClaim(
            trigger_id=f"life-ecology:{key.wake_event_ref}", state=state
        )

    async def complete(self, *, key, trigger_id: str, outcome: str):  # type: ignore[no-untyped-def]
        if self.complete_raises:
            raise OSError("trigger store offline")
        self.completed.append((key, trigger_id, outcome))


class _Media:
    def __init__(
        self, *, status: str = "idle", raises: Exception | None = None
    ) -> None:
        self.status = status
        self.raises = raises
        self.calls = []

    def drain_once(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(status=self.status)


class _Activity:
    def __init__(
        self,
        *,
        status: str = "no_op",
        raises: bool = False,
        reason_code: str | None = None,
    ) -> None:
        self.status = status
        self.raises = raises
        self.reason_code = reason_code
        self.calls = []

    async def advance_once(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("activity failed")
        return SimpleNamespace(status=self.status, reason_code=self.reason_code)


class _OpenWorld:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = []

    def advance_once(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return SimpleNamespace(status=self.status)


class _LifeDevelopment:
    def __init__(self, status: str, *, reason_code: str | None = None) -> None:
        self.status = status
        self.reason_code = reason_code
        self.calls = []

    async def advance_once(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return SimpleNamespace(status=self.status, reason_code=self.reason_code)


class _Biographical:
    def __init__(self, status: str) -> None:
        self.status = status
        self.calls = []

    async def advance_once(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        return SimpleNamespace(status=self.status)


class _Aftermath:
    def __init__(
        self,
        *,
        status: str = "no_op",
        raises: Exception | None = None,
    ) -> None:
        self.status = status
        self.raises = raises
        self.calls = []

    async def advance_once(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return SimpleNamespace(status=self.status)


@pytest.mark.asyncio
async def test_life_ecology_accepts_exact_clock_from_a_multi_world_event_commit() -> None:
    """A tick may atomically append ClockAdvanced plus affect decay events."""

    event = _event("clock-with-affect-decay")
    ledger = _Ledger(event)
    ledger.lookup_event_commit = lambda event_id: (  # type: ignore[method-assign]
        (
            event,
            SimpleNamespace(
                # CommitResult carries the revision after the whole batch;
                # the clock's committed ref carries its event-level revision.
                world_revision=8,
                deliberation_revision=3,
                ledger_sequence=12,
                event_ids=(event.event_id, "event:affect-decayed"),
            ),
        )
        if event_id == event.event_id
        else None
    )
    trigger_store, media = _TriggerStore(), _Media()
    runtime = LifeEcologyRuntime(
        ledger=ledger,
        trigger_store=trigger_store,
        media_followup=media,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:multi-event-clock",
        correlation_id="correlation:multi-event-clock",
    )

    assert result.status == "idle"
    assert len(trigger_store.claims) == 1
    assert len(media.calls) == 1


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
async def test_life_development_cooldown_still_drains_non_model_media_work() -> None:
    event = _event("clock-during-cooldown")
    ledger = _Ledger(event)
    ledger._projection.life_ecology_schedule = LifeEcologyScheduleProjection(
        last_trigger_id="trigger:previous",
        last_wake_event_ref="event:previous-clock",
        last_outcome_ref="life-ecology:failed_safe",
        last_completed_at=NOW - timedelta(minutes=1),
        next_consideration_at=NOW + timedelta(minutes=29),
        consecutive_failures=2,
    )
    trigger_store, media = _TriggerStore(), _Media()
    development = _LifeDevelopment("no_op")
    runtime = LifeEcologyRuntime(
        ledger=ledger,
        trigger_store=trigger_store,
        media_followup=media,
        life_development_followup=development,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:cooldown",
        correlation_id="correlation:cooldown",
    )

    assert result.status == "idle"
    assert development.calls == []
    assert len(trigger_store.claims) == 1
    assert len(media.calls) == 1
    assert trigger_store.completed[0][2] == "cooldown"


@pytest.mark.asyncio
async def test_open_life_cadence_does_not_block_a_due_activity_transition() -> None:
    event = _event("clock-due-activity-during-development-cadence")
    ledger = _Ledger(event)
    ledger._projection.life_ecology_schedule = LifeEcologyScheduleProjection(
        last_trigger_id="trigger:previous-no-op",
        last_wake_event_ref="event:previous-clock",
        last_outcome_ref="life-ecology:life_development_no_op",
        last_completed_at=NOW - timedelta(minutes=1),
        next_consideration_at=NOW + timedelta(hours=3),
        consecutive_failures=0,
    )
    ledger._projection.plans = (
        SimpleNamespace(
            status="planned",
            scheduled_window=SimpleNamespace(
                opens_at=NOW,
                closes_at=NOW + timedelta(hours=1),
            ),
        ),
    )
    ledger._projection.world_occurrences = ()
    ledger._projection.experiences = ()
    ledger._projection.pending_biographical_settlements = ()
    trigger_store, media = _TriggerStore(), _Media()
    activity = _Activity(status="transitioned")
    development = _LifeDevelopment("no_op")
    runtime = LifeEcologyRuntime(
        ledger=ledger,
        trigger_store=trigger_store,
        media_followup=media,
        activity_followup=activity,
        life_development_followup=development,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:due-activity",
        correlation_id="correlation:due-activity",
    )

    assert result.status == "advanced"
    assert result.activity_followup_status == "transitioned"
    assert len(activity.calls) == 1
    assert development.calls == []
    assert trigger_store.completed[0][2] == "activity_transitioned"


@pytest.mark.asyncio
async def test_open_life_cadence_does_not_block_a_biographical_boundary() -> None:
    event = _event("clock-biographical-boundary-during-development-cadence")
    ledger = _Ledger(event)
    ledger._projection.life_ecology_schedule = LifeEcologyScheduleProjection(
        last_trigger_id="trigger:previous-no-op",
        last_wake_event_ref="event:previous-clock",
        last_outcome_ref="life-ecology:life_development_no_op",
        last_completed_at=NOW - timedelta(minutes=1),
        next_consideration_at=NOW + timedelta(hours=3),
        consecutive_failures=0,
    )
    trigger_store, media = _TriggerStore(), _Media()
    biography = _Biographical("transitioned")
    development = _LifeDevelopment("no_op")
    runtime = LifeEcologyRuntime(
        ledger=ledger,
        trigger_store=trigger_store,
        media_followup=media,
        biographical_followup=biography,
        life_development_followup=development,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:biographical-boundary",
        correlation_id="correlation:biographical-boundary",
    )

    assert result.status == "advanced"
    assert result.biographical_followup_status == "transitioned"
    assert len(biography.calls) == 1
    assert development.calls == []
    assert trigger_store.completed[0][2] == "biographical_transitioned"


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
@pytest.mark.asyncio
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
async def test_life_ecology_runs_an_explicit_activity_followup_before_media_and_reports_advance() -> None:
    event = _event("clock")
    trigger_store, media, activity = _TriggerStore(), _Media(status="created"), _Activity(status="transitioned")
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        activity_followup=activity,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id, trace_id="trace:activity", correlation_id="correlation:activity"
    )

    assert result.status == "advanced"
    assert result.activity_followup_status == "transitioned"
    assert activity.calls[0]["trigger_id"] == f"life-ecology:{event.event_id}"
    assert len(media.calls) == 1
    assert trigger_store.completed[0][2] == "activity_transitioned"


@pytest.mark.asyncio
async def test_life_ecology_fails_safe_without_media_when_activity_followup_fails() -> None:
    event = _event("clock")
    trigger_store, media, activity = _TriggerStore(), _Media(), _Activity(raises=True)
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        activity_followup=activity,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id, trace_id="trace:activity-fail", correlation_id="correlation:activity-fail"
    )

    assert result.status == "failed_safe"
    assert result.reason_code == "life_ecology.activity_followup_failed"
    assert media.calls == []
    assert trigger_store.completed[0][2] == "failed_safe"


@pytest.mark.asyncio
async def test_activity_character_failure_uses_the_shared_technical_retry_lane() -> None:
    event = _event("clock-activity-character-failure")
    trigger_store, media = _TriggerStore(), _Media()
    activity = _Activity(
        status="technical_failure",
        reason_code="activity_lifecycle.role_result_not_json",
    )
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        activity_followup=activity,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:activity-character-failure",
        correlation_id="correlation:activity-character-failure",
    )

    assert result.status == "deferred"
    assert result.reason_code == "life_ecology.activity_lifecycle_technical_failure"
    assert result.activity_followup_status == "technical_failure"
    assert result.technical_failure_code == "activity_lifecycle.role_result_not_json"
    assert trigger_store.completed[0][2] == (
        "technical_failure.activity_lifecycle.role_result_not_json"
    )
    assert media.calls == []


@pytest.mark.asyncio
async def test_life_ecology_persists_a_retryable_media_failure_code() -> None:
    event = _event("clock-media-failure")
    trigger_store = _TriggerStore()
    media = _Media(raises=TypeError("candidate contract mismatch"))
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:media-failure",
        correlation_id="correlation:media-failure",
    )

    assert result.status == "failed_safe"
    assert result.technical_failure_code == "media.type_error"
    assert result.reason_code == "life_ecology.media_followup_failed.media.type_error"
    assert trigger_store.completed[0][2] == "technical_failure.media.type_error"


@pytest.mark.asyncio
async def test_life_ecology_does_not_claim_an_unpersisted_failure_code() -> None:
    event = _event("clock-media-failure-store-offline")
    trigger_store = _TriggerStore(complete_raises=True)
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=_Media(raises=TypeError("candidate contract mismatch")),
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:media-failure-store-offline",
        correlation_id="correlation:media-failure-store-offline",
    )

    assert result.status == "failed_safe"
    assert result.reason_code == "life_ecology.technical_failure_persistence_failed"
    assert result.technical_failure_code is None
    assert trigger_store.completed == []


@pytest.mark.asyncio
async def test_life_ecology_keeps_model_deferred_wake_recoverable_instead_of_terminalizing_it() -> None:
    event = _event("clock-open-world-deferred")
    trigger_store, media, open_world = _TriggerStore(), _Media(), _OpenWorld("deferred")
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        open_world_followup=open_world,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:open-world-deferred",
        correlation_id="correlation:open-world-deferred",
    )

    assert result.status == "deferred"
    assert result.reason_code == "life_ecology.open_world_deferred"
    assert result.open_world_followup_status == "deferred"
    assert media.calls == []
    assert trigger_store.completed == []


@pytest.mark.asyncio
async def test_life_ecology_uses_the_open_development_followup() -> None:
    event = _event("clock-open-life-development")
    trigger_store, media = _TriggerStore(), _Media()
    development = _LifeDevelopment("occurrence_committed")
    open_world = _OpenWorld("committed")
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        life_development_followup=development,
        open_world_followup=open_world,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:open-life-development",
        correlation_id="correlation:open-life-development",
    )

    assert result.status == "advanced"
    assert result.life_development_followup_status == "occurrence_committed"
    assert development.calls == [{
        "wake_event_ref": event.event_id,
        "trace_id": "trace:open-life-development",
        "correlation_id": "correlation:open-life-development",
    }]
    assert open_world.calls == []
    assert len(media.calls) == 1
    assert trigger_store.completed[0][2] == "life_development_occurrence_committed"


@pytest.mark.asyncio
async def test_life_development_technical_failure_schedules_retry_instead_of_idle_completion() -> None:
    event = _event("clock-open-life-technical-failure")
    trigger_store, media = _TriggerStore(), _Media()
    development = _LifeDevelopment(
        "technical_failure",
        reason_code="life_development.world_author_unavailable",
    )
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        life_development_followup=development,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:open-life-technical-failure",
        correlation_id="correlation:open-life-technical-failure",
    )

    assert result.status == "deferred"
    assert result.reason_code == "life_ecology.life_development_technical_failure"
    assert result.life_development_followup_status == "technical_failure"
    assert result.technical_failure_code == "life_development.world_author_unavailable"
    assert trigger_store.completed[0][2] == (
        "technical_failure.life_development.world_author_unavailable"
    )


@pytest.mark.asyncio
async def test_npc_ecology_technical_failure_uses_shared_10_30_120_retry_lane() -> None:
    event = _event("clock-npc-ecology-technical-failure")
    trigger_store, media = _TriggerStore(), _Media()
    development = _LifeDevelopment("no_op", reason_code="quiet")
    npc_ecology = _LifeDevelopment(
        "technical_failure", reason_code="npc_ecology.actor_model_failure"
    )
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        life_development_followup=development,
        npc_initiative_followup=npc_ecology,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:npc-ecology-failure",
        correlation_id="correlation:npc-ecology-failure",
    )

    assert result.status == "failed_safe"
    assert result.reason_code == "life_ecology.npc_ecology_technical_failure"
    assert result.technical_failure_code.startswith("npc_ecology.")
    assert trigger_store.completed[0][2].startswith(
        "technical_failure.npc_ecology."
    )


@pytest.mark.asyncio
async def test_npc_ecology_does_not_spend_tokens_before_shared_schedule_is_due() -> None:
    event = _event("clock-npc-ecology-before-retry")
    ledger = _Ledger(event)
    ledger._projection.life_ecology_schedule = LifeEcologyScheduleProjection(
        last_trigger_id="trigger:prior",
        last_wake_event_ref="event:prior",
        last_outcome_ref=(
            "life-ecology:technical_failure.npc_ecology.actor_invalid_after_repair"
        ),
        last_completed_at=NOW - timedelta(minutes=10),
        next_consideration_at=NOW + timedelta(minutes=20),
        consecutive_failures=2,
        last_failure_code="npc_ecology.actor_invalid_after_repair",
    )
    trigger_store, media = _TriggerStore(), _Media()
    development = _LifeDevelopment("no_op", reason_code="quiet")
    npc_ecology = _LifeDevelopment("state_advanced", reason_code="considered")
    runtime = LifeEcologyRuntime(
        ledger=ledger,
        trigger_store=trigger_store,
        media_followup=media,
        life_development_followup=development,
        npc_initiative_followup=npc_ecology,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:npc-before-retry",
        correlation_id="correlation:npc-before-retry",
    )

    assert result.status == "idle"
    assert development.calls == []
    assert npc_ecology.calls == []
    assert trigger_store.completed[0][2] == "cooldown"


@pytest.mark.asyncio
async def test_aftermath_model_failure_keeps_its_precise_technical_retry_code() -> None:
    event = _event("clock-aftermath-technical-failure")
    trigger_store, media = _TriggerStore(), _Media()
    aftermath = _Aftermath(
        raises=LifeAftermathModelFailure(
            "audited failure",
            failure_code="corrective_invalid",
        )
    )
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        aftermath_followup=aftermath,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:aftermath-technical-failure",
        correlation_id="correlation:aftermath-technical-failure",
    )

    assert result.status == "failed_safe"
    assert result.technical_failure_code == "aftermath.corrective_invalid"
    assert trigger_store.completed[0][2] == (
        "technical_failure.aftermath.corrective_invalid"
    )
    assert media.calls == []


@pytest.mark.asyncio
async def test_memory_postprocess_failure_does_not_abort_the_life_ecology_round() -> None:
    event = _event("clock-memory-postprocess-failure")
    trigger_store, media = _TriggerStore(), _Media()
    aftermath = _Aftermath(
        raises=FactMemoryDraftTechnicalFailure("provider_exception")
    )
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        aftermath_followup=aftermath,
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:memory-postprocess-failure",
        correlation_id="correlation:memory-postprocess-failure",
    )

    assert result.status == "idle"
    assert result.aftermath_followup_status == "memory_technical_failure"
    assert result.technical_failure_code == "memory.provider_exception"
    assert len(media.calls) == 1
    assert trigger_store.completed[0][2] == "idle"


@pytest.mark.asyncio
async def test_aftermath_retry_wait_defers_other_life_lanes_without_calling_models() -> None:
    event = _event("clock-aftermath-retry-wait")
    trigger_store, media = _TriggerStore(), _Media()
    runtime = LifeEcologyRuntime(
        ledger=_Ledger(event),
        trigger_store=trigger_store,
        media_followup=media,
        aftermath_followup=_Aftermath(status="retry_wait"),
        life_development_followup=_LifeDevelopment("occurrence_committed"),
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )

    result = await runtime.advance_once(
        wake_event_ref=event.event_id,
        trace_id="trace:aftermath-retry-wait",
        correlation_id="correlation:aftermath-retry-wait",
    )

    assert result.status == "deferred"
    assert result.aftermath_followup_status == "retry_wait"
    assert trigger_store.completed[0][2] == "cooldown"
    assert media.calls == []


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


@pytest.mark.asyncio
async def test_life_ecology_activates_a_due_planned_occurrence() -> None:
    """A later-mode committed occurrence whose window has opened must be
    activated by the ecology pass; otherwise it would never reach settlement."""

    from datetime import timedelta

    from companion_daemon.world_v2.event_identity import domain_idempotency_key
    from companion_daemon.world_v2.ledger import WorldLedger
    from companion_daemon.world_v2.life_events import (
        WorldOccurrenceCommittedPayload,
    )
    from companion_daemon.world_v2.schemas import ClockObservation
    from companion_daemon.world_v2.schemas import (
        DueWindow,
        EvidenceRef,
        WorldOccurrenceProjection,
    )

    ledger = WorldLedger.in_memory(world_id="world:life-ecology")
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:life-development:due-plan",
        entity_revision=1,
        trigger_ref="event:life-development:proposal:due-plan",
        participant_refs=("agent:companion",),
        time_window=DueWindow(
            opens_at=NOW - timedelta(minutes=5),
            closes_at=NOW + timedelta(hours=2),
        ),
        candidate_outcome_refs=("candidate:due-plan",),
        visibility="shareable",
        status="committed",
    )
    wake = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:clock:due-plan",
        event_type="ClockAdvanced",
        world_id="world:life-ecology",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:clock",
        source="test",
        trace_id="trace:due-plan-clock",
        causation_id="scheduler:due-plan",
        correlation_id="correlation:due-plan-clock",
        idempotency_key="due-plan-clock",
        payload=ClockObservation(
            schema_version="world-v2.1",
            tick_id="due-plan",
            world_id="world:life-ecology",
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:due-plan-clock",
            causation_id="scheduler:due-plan",
            correlation_id="correlation:due-plan-clock",
            logical_time_from=NOW - timedelta(minutes=10),
            logical_time_to=NOW,
            reason="test",
        ).model_dump(mode="json"),
    )
    committed_payload = WorldOccurrenceCommittedPayload(
        change_id="change:due-plan",
        transition_id="transition:due-plan",
        expected_entity_revision=0,
        evidence_refs=(
            EvidenceRef(
                ref_id=wake.event_id,
                evidence_type="committed_world_event",
                claim_purpose="future_plan",
                source_world_revision=1,
                immutable_hash=wake.payload_hash,
            ),
        ),
        policy_refs=("policy:life-ecology.1",),
        occurrence=occurrence,
    ).model_dump(mode="json")
    committed = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:life-development:occurrence:due-plan",
        event_type="WorldOccurrenceCommitted",
        world_id="world:life-ecology",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:world-v2:life-development",
        source="world-v2:life-development",
        trace_id="trace:due-plan",
        causation_id="event:life-development:proposal:due-plan",
        correlation_id="correlation:due-plan",
        idempotency_key=(
            domain_idempotency_key(
                event_type="WorldOccurrenceCommitted",
                world_id="world:life-ecology",
                payload=committed_payload,
            )
            or "due-plan-committed"
        ),
        payload=committed_payload,
    )
    ledger.commit(
        (wake,),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    fresh = ledger.project()
    ledger.commit(
        (committed,),
        expected_world_revision=fresh.world_revision,
        expected_deliberation_revision=fresh.deliberation_revision,
    )

    runtime = LifeEcologyRuntime(
        ledger=ledger,
        trigger_store=_TriggerStore(),
        media_followup=_Media(),
        availability=LifeEcologyAvailability(state="installed_and_active"),
    )
    result = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:due-plan-activation",
        correlation_id="correlation:due-plan-activation",
    )

    assert result.status == "idle"
    activated = next(
        item
        for item in ledger.project().world_occurrences
        if item.occurrence_id == "occurrence:life-development:due-plan"
    )
    assert activated.status == "active"
    assert activated.activated_at == NOW
    assert ledger.lookup_event_commit(
        "event:life-ecology:activate:life-development:due-plan"
    ) is not None
