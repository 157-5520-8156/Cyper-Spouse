from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.event_ecology_media import (
    EcologyPolicy,
    EventEcologyMediaCandidateRuntime,
)
from companion_daemon.world_v2.media_v2 import InMemoryImmutableMediaPayloadStore
from companion_daemon.world_v2.media_v2 import media_payload_hash
from companion_daemon.world_v2.schemas import CommittedWorldEventRef, ProjectionCursor


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _ref(name: str, event_type: str, *, at: datetime = NOW) -> CommittedWorldEventRef:
    return CommittedWorldEventRef(
        event_id=f"event:{name}", event_type=event_type, world_revision=1,
        payload_hash=hashlib.sha256(name.encode()).hexdigest(), logical_time=at,
    )


class _Ledger:
    world_id = "world:event-ecology"

    def __init__(self, projection: SimpleNamespace) -> None:
        self._projection = projection
        self.commits = []

    def project(self):
        return self._projection

    def commit_at_cursor(self, events, *, expected_cursor, commit_id):  # type: ignore[no-untyped-def]
        assert expected_cursor == ProjectionCursor(
            world_revision=self._projection.world_revision,
            deliberation_revision=self._projection.deliberation_revision,
            ledger_sequence=self._projection.ledger_sequence,
        )
        self.commits.append((events, commit_id))
        candidates = list(self._projection.photo_candidates)
        opportunities = list(self._projection.media_opportunities)
        for event in events:
            if event.event_type == "PhotoCandidateOpened":
                candidates.append(event.payload()["candidate"])
            elif event.event_type == "MediaOpportunityFrozen":
                opportunities.append(event.payload()["opportunity"])
        # Runtime normally sees reducer-hydrated types.  Hydrate only the two
        # output shapes here, retaining the fake as a narrow ledger adapter.
        from companion_daemon.world_v2.media_v2 import MediaOpportunity, PhotoCandidate

        self._projection.photo_candidates = tuple(
            item if isinstance(item, PhotoCandidate) else PhotoCandidate.model_validate_json(json.dumps(item))
            for item in candidates
        )
        self._projection.media_opportunities = tuple(
            item if isinstance(item, MediaOpportunity) else MediaOpportunity.model_validate_json(json.dumps(item))
            for item in opportunities
        )
        self._projection.ledger_sequence += len(events)
        self._projection.world_revision += len(events)


def _projection(*, refs, plans=(), occurrences=(), experiences=(), facts=(), npcs=()):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        world_revision=10, deliberation_revision=0, ledger_sequence=10, logical_time=NOW,
        committed_world_event_refs=tuple(refs), plans=tuple(plans),
        world_occurrences=tuple(occurrences), experiences=tuple(experiences), facts=tuple(facts),
        npcs=tuple(npcs), photo_candidates=(), media_opportunities=(),
    )


def _origin(ref: str, *, at: datetime = NOW):
    return SimpleNamespace(accepted_event_ref=ref, accepted_at=at)


class _Compiler:
    """Narrow ecology fake; the compiler's pin/provenance contract is tested separately."""

    def __init__(self) -> None:
        self.requests = []

    def compile(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        body = json.dumps(
            {
                "image_event_snapshot": {
                    "schema_version": "world-image-event-snapshot-v1",
                    "category": request.category,
                }
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return SimpleNamespace(
            snapshot_body=body,
            snapshot_ref="sidecar:test-compiled:" + request.candidate.candidate_id,
            snapshot_hash=media_payload_hash(body),
        )


def test_ecology_derives_diverse_candidates_only_from_existing_shareable_authority() -> None:
    active = _ref("activity", "ActivityStarted", at=NOW - timedelta(minutes=10))
    result = _ref("result", "WorldOccurrenceSettled", at=NOW - timedelta(minutes=8))
    experience = _ref("experience", "ExperienceCommitted", at=NOW - timedelta(minutes=6))
    environment = _ref("environment", "FactCommitted", at=NOW - timedelta(minutes=4))
    food = _ref("food", "FactCommitted", at=NOW - timedelta(minutes=2))
    private = _ref("private", "ActivityStarted", at=NOW - timedelta(minutes=1))
    projection = _projection(
        refs=(active, result, experience, environment, food, private),
        plans=(
            SimpleNamespace(
                status="active", authority_origin=_origin(active.event_id, at=active.logical_time),
                privacy_class="shareable", last_transitioned_at=active.logical_time,
                activity_kind="walk", location_ref="location:park", participant_refs=("companion:celia",),
            ),
            SimpleNamespace(
                status="active", authority_origin=_origin(private.event_id, at=private.logical_time),
                privacy_class="private", last_transitioned_at=private.logical_time,
                activity_kind="sleep", location_ref="location:home", participant_refs=("companion:celia",),
            ),
        ),
        occurrences=(SimpleNamespace(
            status="settled", settlement_event_ref=result.event_id, visibility="shareable",
            settled_at=result.logical_time, location_ref="location:cafe",
            participant_refs=("companion:celia", "npc:friend"), settled_outcome_ref="outcome:tea-ready",
        ),),
        experiences=(SimpleNamespace(
            origin=_origin(experience.event_id),
            values=SimpleNamespace(
                privacy_class="shareable", occurred_to=experience.logical_time,
                summary_ref="life:coffee", summary_payload_hash="c" * 64,
                participant_refs=("companion:celia", "user:1"),
            ),
        ),),
        facts=(
            SimpleNamespace(
                origin=_origin(environment.event_id), updated_at=environment.logical_time,
                values=SimpleNamespace(
                    status="active", privacy_class="public", predicate_code="environment.weather",
                    subject_ref="world:outside", value_ref="fact-value:rain", value_hash="d" * 64,
                ),
            ),
            SimpleNamespace(
                origin=_origin(food.event_id), updated_at=food.logical_time,
                values=SimpleNamespace(
                    status="active", privacy_class="shareable", predicate_code="meal.visible_food",
                    subject_ref="activity:brunch", value_ref="fact-value:noodles", value_hash="e" * 64,
                ),
            ),
        ),
        npcs=(SimpleNamespace(npc_id="npc:friend", privacy_class="public"),),
    )
    ledger = _Ledger(projection)
    store = InMemoryImmutableMediaPayloadStore()
    compiler = _Compiler()
    runtime = EventEcologyMediaCandidateRuntime(
        ledger=ledger, sidecar=store,
        policy=EcologyPolicy(
            max_candidates_per_drain=8, max_opportunities_per_day=8,
            direct_preview_compatibility=True,
        ),
        compiler=compiler,
    )

    result_value = runtime.drain_once(
        wake_event_ref=food.event_id, logical_time=NOW, actor="worker:event-ecology",
        trace_id="trace:ecology", correlation_id="correlation:ecology",
    )

    assert result_value.status == "created"
    assert len(result_value.candidate_ids) == 5
    assert all("private" not in value for value in result_value.candidate_ids)
    events, _ = ledger.commits[0]
    assert [event.event_type for event in events] == [
        "PhotoCandidateOpened", "MediaOpportunityFrozen",
    ] * 5
    snapshots = [
        store.read_exact(payload_ref=item.event_snapshot_ref).body  # type: ignore[union-attr]
        for item in projection.media_opportunities
    ]
    categories = {json.loads(body)["image_event_snapshot"]["category"] for body in snapshots}
    assert categories == {
        "activity_process", "npc_shared_outcome", "shared_experience", "place_environment", "object_or_food",
    }
    # Snapshot contains only declared coordinates.  In particular it did not
    # manufacture weather prose, food description, an NPC name, or a pose.
    assert {request.category for request in compiler.requests} == categories


def test_ecology_is_replay_safe_and_category_cooldown_uses_persisted_opportunities() -> None:
    first = _ref("first", "ActivityStarted", at=NOW - timedelta(minutes=2))
    second = _ref("second", "ActivityStarted", at=NOW - timedelta(minutes=1))
    plan_one = SimpleNamespace(
        status="active", authority_origin=_origin(first.event_id, at=first.logical_time),
        privacy_class="shareable", last_transitioned_at=first.logical_time,
        activity_kind="walk", location_ref="location:park", participant_refs=("companion:celia",),
    )
    projection = _projection(refs=(first,), plans=(plan_one,))
    ledger = _Ledger(projection)
    runtime = EventEcologyMediaCandidateRuntime(
        ledger=ledger,
        sidecar=InMemoryImmutableMediaPayloadStore(),
        policy=EcologyPolicy(direct_preview_compatibility=True),
        compiler=_Compiler(),
    )

    created = runtime.drain_once(
        wake_event_ref=first.event_id, logical_time=NOW, actor="worker:event-ecology",
        trace_id="trace:1", correlation_id="correlation:1",
    )
    assert created.status == "created"
    # Exact replay joins the source-bound candidate rather than re-emitting it.
    assert runtime.drain_once(
        wake_event_ref=first.event_id, logical_time=NOW, actor="worker:event-ecology",
        trace_id="trace:1", correlation_id="correlation:1",
    ).status == "idle"

    projection.committed_world_event_refs = (first, second)
    projection.plans = (
        plan_one,
        SimpleNamespace(
            status="active", authority_origin=_origin(second.event_id, at=second.logical_time),
            privacy_class="shareable", last_transitioned_at=second.logical_time,
            activity_kind="market", location_ref="location:market", participant_refs=("companion:celia",),
        ),
    )
    # Different source, same category, still within the persisted 6h bucket.
    assert runtime.drain_once(
        wake_event_ref=second.event_id, logical_time=NOW, actor="worker:event-ecology",
        trace_id="trace:2", correlation_id="correlation:2",
    ).status == "idle"


def test_ecology_recovers_an_older_committed_wake_at_the_current_logical_time() -> None:
    wake = _ref("clock", "ClockAdvanced", at=NOW)
    activity = _ref("activity", "ActivityStarted", at=NOW)
    projection = _projection(
        refs=(wake, activity),
        plans=(SimpleNamespace(
            status="active", authority_origin=_origin(activity.event_id, at=NOW),
            privacy_class="shareable", last_transitioned_at=NOW,
            activity_kind="walk", location_ref="location:park", participant_refs=("companion:celia",),
        ),),
    )
    later = NOW + timedelta(minutes=5)
    projection.logical_time = later
    ledger = _Ledger(projection)
    runtime = EventEcologyMediaCandidateRuntime(
        ledger=ledger,
        sidecar=InMemoryImmutableMediaPayloadStore(),
        policy=EcologyPolicy(direct_preview_compatibility=True),
        compiler=_Compiler(),
    )

    result = runtime.drain_once(
        wake_event_ref=wake.event_id, logical_time=later, actor="worker:event-ecology",
        trace_id="trace:recovery", correlation_id="correlation:recovery",
    )

    assert result.status == "created"
    assert len(result.candidate_ids) == 1


def test_ecology_refuses_an_uncommitted_or_non_life_wake() -> None:
    start = _ref("start", "WorldStarted")
    projection = _projection(refs=(start,))
    runtime = EventEcologyMediaCandidateRuntime(
        ledger=_Ledger(projection), sidecar=InMemoryImmutableMediaPayloadStore()
    )

    with pytest.raises(ValueError, match="committed life/clock/worker wake"):
        runtime.drain_once(
            wake_event_ref=start.event_id, logical_time=NOW, actor="worker:event-ecology",
            trace_id="trace:reject", correlation_id="correlation:reject",
        )
    with pytest.raises(ValueError, match="committed life/clock/worker wake"):
        runtime.drain_once(
            wake_event_ref="event:not-committed", logical_time=NOW, actor="worker:event-ecology",
            trace_id="trace:reject", correlation_id="correlation:reject",
        )


def test_ecology_fails_closed_before_any_ledger_write_when_snapshot_sidecar_is_unavailable() -> None:
    source = _ref("activity-sidecar", "ActivityStarted")
    projection = _projection(
        refs=(source,),
        plans=(SimpleNamespace(
            status="active", authority_origin=_origin(source.event_id), privacy_class="shareable",
            last_transitioned_at=NOW, activity_kind="walk", location_ref="location:park",
            participant_refs=("companion:celia",),
        ),),
    )
    ledger = _Ledger(projection)

    class _UnavailableSidecar:
        def put_if_absent(self, _record) -> None:  # type: ignore[no-untyped-def]
            raise OSError("sidecar offline")

        def read_exact(self, *, payload_ref: str):  # type: ignore[no-untyped-def]
            del payload_ref
            return None

    runtime = EventEcologyMediaCandidateRuntime(
        ledger=ledger,
        sidecar=_UnavailableSidecar(),
        policy=EcologyPolicy(direct_preview_compatibility=True),
        compiler=_Compiler(),
    )
    with pytest.raises(OSError, match="sidecar offline"):
        runtime.drain_once(
            wake_event_ref=source.event_id, logical_time=NOW, actor="worker:event-ecology",
            trace_id="trace:sidecar", correlation_id="correlation:sidecar",
        )


def test_ecology_does_not_keep_the_direct_freeze_path_enabled_by_default() -> None:
    source = _ref("default-disabled", "ActivityStarted")
    projection = _projection(
        refs=(source,),
        plans=(SimpleNamespace(
            status="active", authority_origin=_origin(source.event_id), privacy_class="shareable",
            last_transitioned_at=NOW, activity_kind="walk", location_ref="location:park",
            participant_refs=("companion:celia",),
        ),),
    )
    ledger = _Ledger(projection)
    compiler = _Compiler()

    result = EventEcologyMediaCandidateRuntime(
        ledger=ledger, sidecar=InMemoryImmutableMediaPayloadStore(), compiler=compiler,
    ).drain_once(
        wake_event_ref=source.event_id, logical_time=NOW, actor="worker:event-ecology",
        trace_id="trace:default", correlation_id="correlation:default",
    )

    assert result.status == "idle"
    assert compiler.requests == []
    assert ledger.commits == []
    assert ledger.commits == []
