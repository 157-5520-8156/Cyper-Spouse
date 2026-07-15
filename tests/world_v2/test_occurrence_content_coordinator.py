from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    SQLiteImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OccurrenceContentCoordinator,
    OutcomeCandidateContent,
)
from companion_daemon.world_v2.outcome_candidate_reader import OutcomeCandidateReader
from companion_daemon.world_v2.schemas import (
    ClockObservation,
    DueWindow,
    EvidenceRef,
    ProjectionCursor,
    WorldEvent,
    WorldOccurrenceProjection,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


WORLD_ID = "world:occurrence-content"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
OPERATOR_HASH = "a" * 64


def _seed(ledger: WorldLedger) -> None:
    clock = ClockObservation(
        schema_version="world-v2.1",
        tick_id="occurrence-content-seed",
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:occurrence-content",
        causation_id="cause:clock-seed",
        correlation_id="correlation:occurrence-content",
        logical_time_from=NOW - timedelta(minutes=1),
        logical_time_to=NOW,
        reason="test_seed",
    )
    clock_event = WorldEvent.from_payload(
        schema_version=clock.schema_version,
        event_id="event:clock:occurrence-content-seed",
        world_id=WORLD_ID,
        event_type="ClockAdvanced",
        logical_time=NOW,
        created_at=NOW,
        actor="system:clock",
        source="test",
        trace_id=clock.trace_id,
        causation_id=clock.causation_id,
        correlation_id=clock.correlation_id,
        idempotency_key="clock:occurrence-content-seed",
        payload=clock.model_dump(mode="json"),
    )
    _commit_at_head(ledger, clock_event)
    payload = {"observation_id": "operator:occurrence", "observation_hash": OPERATOR_HASH}
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:operator:occurrence",
        world_id=WORLD_ID,
        event_type="OperatorObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="operator:test",
        source="test",
        trace_id="trace:occurrence-content",
        causation_id="cause:seed",
        correlation_id="correlation:occurrence-content",
        idempotency_key=domain_idempotency_key(
            event_type="OperatorObservationRecorded", world_id=WORLD_ID, payload=payload
        ) or "identity:operator:occurrence",
        payload=payload,
    )
    _commit_at_head(ledger, event)


def _commit_at_head(ledger, event: WorldEvent) -> None:
    projection = ledger.project()
    ledger.commit_at_cursor(
        (event,),
        expected_cursor=ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        ),
    )


def _request(*, text: str = "炉上的水正好沸起来，茶叶舒展开了。") -> OccurrenceContentCommitRequest:
    candidate = OutcomeCandidateContent(
        candidate_result_ref="candidate:tea-ready",
        result_id="result:tea-ready",
        result_payload_ref="payload:tea-ready",
        result_payload_hash="result-hash:tea-ready",
        privacy_class="private",
        content_ref="content:outcome:tea-ready",
        text=text,
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:tea",
        entity_revision=1,
        trigger_ref="trigger:tea",
        participant_refs=("actor:companion",),
        location_ref="room:kitchen",
        time_window=DueWindow(opens_at=NOW, closes_at=NOW + timedelta(minutes=10)),
        candidate_outcome_refs=(candidate.candidate_result_ref,),
        visibility="private",
        status="committed",
    )
    return OccurrenceContentCommitRequest(
        world_id=WORLD_ID,
        occurrence=occurrence,
        candidate_contents=(candidate,),
        change_id="change:occurrence:tea",
        transition_id="transition:occurrence:tea",
        evidence_refs=(
            EvidenceRef(
                ref_id="operator:occurrence",
                evidence_type="operator_observation",
                claim_purpose="future_plan",
                immutable_hash=OPERATOR_HASH,
            ),
        ),
        policy_refs=("policy:life-v1",),
        logical_time=NOW,
        created_at=NOW,
        actor="system:occurrence-author",
        source="test",
        trace_id="trace:occurrence-content",
        causation_id="cause:occurrence",
        correlation_id="correlation:occurrence-content",
    )


def test_coordinator_commits_complete_sidecar_backed_candidate_matrix() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    store = InMemoryImmutableLifeContentStore()
    _seed(ledger)

    result = OccurrenceContentCoordinator(ledger=ledger, store=store).commit(_request())

    assert result.world_revision == 2
    occurrence = ledger.project().world_occurrences[0]
    candidate = occurrence.candidate_outcomes[0]
    assert candidate.content_ref == "content:outcome:tea-ready"
    assert candidate.content_payload_hash == life_content_payload_hash("炉上的水正好沸起来，茶叶舒展开了。")
    assert store.read_exact(content_ref=candidate.content_ref) == StoredLifeContent(
        content_ref=candidate.content_ref,
        content_kind="outcome_candidate",
        content_payload_hash=candidate.content_payload_hash,
        text="炉上的水正好沸起来，茶叶舒展开了。",
    )
    active = occurrence.model_copy(
        update={"status": "active", "entity_revision": 2, "activated_at": NOW}
    )
    assert OutcomeCandidateReader(store=store).read(
        occurrence=active, viewer_privacy_ceiling="private"
    ).candidates[0].text == "炉上的水正好沸起来，茶叶舒展开了。"
    assert OutcomeCandidateReader(store=InMemoryImmutableLifeContentStore()).read(
        occurrence=active, viewer_privacy_ceiling="private"
    ).suppressions[0].reason == "content_missing"
    tampered_store = InMemoryImmutableLifeContentStore()
    tampered_text = "茶其实已经凉透了。"
    tampered_store.put_if_absent(
        StoredLifeContent(
            content_ref=candidate.content_ref,
            content_kind="outcome_candidate",
            content_payload_hash=life_content_payload_hash(tampered_text),
            text=tampered_text,
        )
    )
    assert OutcomeCandidateReader(store=tampered_store).read(
        occurrence=active, viewer_privacy_ceiling="private"
    ).suppressions[0].reason == "hash_mismatch"


def test_coordinator_rejects_conflicting_sidecar_bytes_without_ledger_authority() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    store = InMemoryImmutableLifeContentStore()
    _seed(ledger)
    request = _request()
    store.put_if_absent(
        StoredLifeContent(
            content_ref="content:outcome:tea-ready",
            content_kind="outcome_candidate",
            content_payload_hash=life_content_payload_hash("已经被另一段文本占用。"),
            text="已经被另一段文本占用。",
        )
    )

    with pytest.raises(ValueError, match="already bound"):
        OccurrenceContentCoordinator(ledger=ledger, store=store).commit(request)

    assert ledger.project().world_occurrences == ()


def test_coordinator_rejects_candidate_content_that_weakens_occurrence_privacy() -> None:
    request = _request().model_copy(
        update={
            "candidate_contents": (
                OutcomeCandidateContent(
                    candidate_result_ref="candidate:tea-ready",
                    result_id="result:tea-ready",
                    result_payload_ref="payload:tea-ready",
                    result_payload_hash="result-hash:tea-ready",
                    privacy_class="public",
                    content_ref="content:outcome:tea-ready",
                    text="不应降低 private occurrence 的候选内容可见性。",
                ),
            )
        }
    )

    with pytest.raises(ValueError, match="cannot weaken occurrence privacy"):
        OccurrenceContentCommitRequest.model_validate(request.model_dump())


def test_invalid_candidate_matrix_is_rejected_before_any_sidecar_write() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    store = InMemoryImmutableLifeContentStore()
    base = _request()
    duplicate = OutcomeCandidateContent(
        candidate_result_ref="candidate:tea-ready",
        result_id="result:tea-spilled",
        result_payload_ref="payload:tea-spilled",
        result_payload_hash="result-hash:tea-spilled",
        privacy_class="private",
        content_ref="content:outcome:tea-spilled",
        text="茶水洒在台面上。",
    )
    raw = base.model_dump()
    raw["occurrence"]["candidate_outcome_refs"] = (
        "candidate:tea-ready",
        "candidate:tea-ready",
    )
    raw["candidate_contents"] = (*raw["candidate_contents"], duplicate.model_dump())

    with pytest.raises(ValueError, match="candidate result refs must be unique"):
        OccurrenceContentCommitRequest.model_validate(raw)

    assert store.read_exact(content_ref="content:outcome:tea-ready") is None
    assert ledger.project().world_occurrences == ()


def test_failed_ledger_write_leaves_only_an_unreferenced_sidecar_orphan() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    store = InMemoryImmutableLifeContentStore()
    # Deliberately omit _seed: reducer rejects the unresolved evidence after
    # the safe first sidecar write.
    request = _request()

    with pytest.raises(ValueError, match="lived-world mutation requires authoritative logical time"):
        OccurrenceContentCoordinator(ledger=ledger, store=store).commit(request)

    assert ledger.project().world_occurrences == ()
    assert store.read_exact(content_ref="content:outcome:tea-ready") is not None


class _CursorConflictLedger:
    """Test double: another writer wins immediately after sidecar installation."""

    world_id = WORLD_ID

    def __init__(self) -> None:
        self._delegate = WorldLedger.in_memory(world_id=WORLD_ID)

    def project(self):  # type: ignore[no-untyped-def]
        return self._delegate.project()

    def commit_at_cursor(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ConcurrencyConflict("stale projection cursor")


def test_cursor_conflict_after_sidecar_write_leaves_only_an_orphan() -> None:
    store = InMemoryImmutableLifeContentStore()
    ledger = _CursorConflictLedger()

    with pytest.raises(ConcurrencyConflict, match="stale projection cursor"):
        OccurrenceContentCoordinator(ledger=ledger, store=store).commit(_request())

    assert store.read_exact(content_ref="content:outcome:tea-ready") is not None


def test_sqlite_restart_reuses_exact_sidecar_and_ledger_commit(tmp_path) -> None:
    path = str(tmp_path / "world.sqlite")
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    store = SQLiteImmutableLifeContentStore(path=path, world_id=WORLD_ID)
    request = _request()
    try:
        _seed(ledger)
        first = OccurrenceContentCoordinator(ledger=ledger, store=store).commit(request)
    finally:
        store.close()
        ledger.close()

    restored_ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    restored_store = SQLiteImmutableLifeContentStore(path=path, world_id=WORLD_ID)
    try:
        second = OccurrenceContentCoordinator(ledger=restored_ledger, store=restored_store).commit(
            request
        )
        assert second == first
        occurrence = restored_ledger.project().world_occurrences[0]
        record = restored_store.read_exact(content_ref=occurrence.candidate_outcomes[0].content_ref)
        assert record is not None
        assert record.text == "炉上的水正好沸起来，茶叶舒展开了。"
    finally:
        restored_store.close()
        restored_ledger.close()
