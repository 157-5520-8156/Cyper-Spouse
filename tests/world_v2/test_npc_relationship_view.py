"""Per-NPC relationship reading: derived projection, weight tilt, advisory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.life_author_seed import (
    NpcInitiativeCandidate,
    ReviewedLifeOutcome,
    ReviewedNpcInitiatedEvent,
)
from companion_daemon.world_v2.life_development_draft import (
    LifeDevelopmentNpcCapability,
)
from companion_daemon.world_v2.npc_initiative_weight_policy import (
    NpcInitiativeWeightPolicy,
)
from companion_daemon.world_v2.npc_relationship_view import (
    RESTING_CLOSENESS_BP,
    SharedHistoryEvidence,
    npc_relationship_advisories,
    npc_relationship_readings,
    npc_shared_history_evidence,
)
from companion_daemon.world_v2.schemas import (
    DueWindow,
    NpcProjection,
    WorldOccurrenceProjection,
)


NOW = datetime(2026, 7, 20, 3, 0, tzinfo=UTC)


def _npc(npc_id: str = "literature-fan") -> NpcProjection:
    return NpcProjection(
        npc_id=npc_id,
        entity_revision=1,
        stable_identity_ref="reviewed-person:fan-yuan",
        privacy_class="personal",
        status="active",
    )


def _settled_occurrence(
    *,
    occurrence_id: str,
    npc_id: str = "literature-fan",
    settled_at: datetime,
    participant_refs: tuple[str, ...] | None = None,
) -> WorldOccurrenceProjection:
    return WorldOccurrenceProjection(
        occurrence_id=occurrence_id,
        entity_revision=3,
        trigger_ref=f"trigger:{occurrence_id}",
        participant_refs=(
            participant_refs
            if participant_refs is not None
            else ("agent:companion", f"npc:{npc_id}")
        ),
        location_ref="location:library",
        time_window=DueWindow(
            opens_at=settled_at - timedelta(hours=1), closes_at=settled_at
        ),
        candidate_outcome_refs=(f"candidate:{occurrence_id}:1",),
        settled_outcome_ref=f"candidate:{occurrence_id}:1",
        visibility="personal",
        status="settled",
        activated_at=settled_at - timedelta(hours=1),
        result_id=f"result:{occurrence_id}",
        result_payload_ref=f"content:{occurrence_id}",
        result_payload_hash="sha256:" + "0" * 64,
        settled_at=settled_at,
        settlement_event_ref=f"event:settled:{occurrence_id}",
        settlement_world_revision=7,
        settlement_payload_hash="0" * 64,
    )


class _Projection:
    def __init__(self, *, npcs=(), world_occurrences=(), logical_time=NOW):
        self.npcs = npcs
        self.world_occurrences = world_occurrences
        self.logical_time = logical_time


class _ProjectionRejectingPrivateAppraisalReads(_Projection):
    @property
    def appraisals(self) -> object:
        raise AssertionError("NPC relationship view cannot read protagonist Appraisal")


def test_reading_warms_only_with_settled_shared_history() -> None:
    stranger = npc_relationship_readings(
        _Projection(npcs=(_npc(),)),
        protagonist_actor_ref="agent:companion",
    )
    assert stranger[0].closeness_bp == RESTING_CLOSENESS_BP
    assert stranger[0].settled_shared_count == 0
    assert stranger[0].source_event_refs == ()

    shared = npc_relationship_readings(
        _Projection(
            npcs=(_npc(),),
            world_occurrences=(
                _settled_occurrence(occurrence_id="o1", settled_at=NOW - timedelta(days=1)),
                _settled_occurrence(occurrence_id="o2", settled_at=NOW - timedelta(days=2)),
                _settled_occurrence(occurrence_id="o3", settled_at=NOW - timedelta(days=20)),
            ),
        ),
        protagonist_actor_ref="agent:companion",
    )
    assert shared[0].settled_shared_count == 3
    assert shared[0].closeness_bp > RESTING_CLOSENESS_BP
    assert shared[0].familiarity_bp > 0
    assert shared[0].last_shared_at == NOW - timedelta(days=1)
    assert "event:settled:o1" in shared[0].source_event_refs
    assert "friction_bp" not in type(shared[0]).model_fields
    assert "protagonist_friction_bp" not in LifeDevelopmentNpcCapability.model_fields

    private_appraisal_trap = npc_relationship_readings(
        _ProjectionRejectingPrivateAppraisalReads(
            npcs=(_npc(),),
            world_occurrences=(
                _settled_occurrence(
                    occurrence_id="o1", settled_at=NOW - timedelta(days=1)
                ),
            ),
        ),
        protagonist_actor_ref="agent:companion",
    )
    assert private_appraisal_trap == npc_relationship_readings(
        _Projection(
            npcs=(_npc(),),
            world_occurrences=(
                _settled_occurrence(
                    occurrence_id="o1", settled_at=NOW - timedelta(days=1)
                ),
            ),
        ),
        protagonist_actor_ref="agent:companion",
    )


def test_solo_npc_occurrence_is_not_shared_history_with_protagonist() -> None:
    reading = npc_relationship_readings(
        _Projection(
            npcs=(_npc(),),
            world_occurrences=(
                _settled_occurrence(
                    occurrence_id="solo",
                    settled_at=NOW - timedelta(hours=1),
                    participant_refs=("npc:literature-fan",),
                ),
            ),
        ),
        protagonist_actor_ref="agent:companion",
    )[0]

    assert reading.settled_shared_count == 0
    assert reading.closeness_bp == RESTING_CLOSENESS_BP
    assert reading.familiarity_bp == 0
    assert reading.source_event_refs == ()


def test_shared_history_evidence_is_neutral_and_source_closed() -> None:
    evidence = npc_shared_history_evidence(
        _Projection(
            npcs=(_npc(),),
            world_occurrences=(
                _settled_occurrence(occurrence_id="o1", settled_at=NOW - timedelta(days=1)),
                _settled_occurrence(occurrence_id="o2", settled_at=NOW - timedelta(days=20)),
            ),
        ),
        protagonist_actor_ref="agent:companion",
    )

    assert evidence == (
        SharedHistoryEvidence(
            npc_ref="npc:literature-fan",
            settled_shared_count=2,
            last_shared_at=NOW - timedelta(days=1),
            source_event_refs=("event:settled:o1", "event:settled:o2"),
        ),
    )
    assert "closeness_bp" not in type(evidence[0]).model_fields
    assert "familiarity_bp" not in type(evidence[0]).model_fields


def _candidate(kind: str) -> NpcInitiativeCandidate:
    return NpcInitiativeCandidate(
        token={"shared_time": "1" * 64, "friction": "2" * 64, "small_favor": "3" * 64}[kind],
        event=ReviewedNpcInitiatedEvent(
            id=f"event-{kind.replace('_', '-')}",
            initiative_kind=kind,  # type: ignore[arg-type]
            npc_id="literature-fan",
            location_id="campus-library",
            summary="范予安过来了。",
            privacy="personal",
            local_windows=("09:00-18:00",),
            weekdays=(0, 1, 2, 3, 4, 5, 6),
            duration_minutes=30,
            base_chance_bp=1_000,
            outcomes=(
                ReviewedLifeOutcome(id="a", text="聊了一会儿。", privacy="personal"),
                ReviewedLifeOutcome(id="b", text="没聊几句就散了。", privacy="personal"),
            ),
        ),
        npc_ref="npc:literature-fan",
        location_ref="location:library",
        availability_hash="4" * 64,
    )


def test_weight_policy_v2_tilts_only_by_settled_relationship_reading() -> None:
    policy = NpcInitiativeWeightPolicy()
    assert policy.version == "npc-initiative-weight.2"
    shared_time = _candidate("shared_time")
    friction = _candidate("friction")

    close = npc_relationship_readings(
        _Projection(
            npcs=(_npc(),),
            world_occurrences=tuple(
                _settled_occurrence(
                    occurrence_id=f"o{index}", settled_at=NOW - timedelta(days=index + 1)
                )
                for index in range(4)
            ),
        ),
        protagonist_actor_ref="agent:companion",
    )
    distant_weights = policy.compile(candidates=(shared_time, friction))
    close_weights = policy.compile(
        candidates=(shared_time, friction), npc_relationships=close
    )
    # Closeness invites shared time; it never becomes a gate.
    assert close_weights[shared_time.token] > distant_weights[shared_time.token]

    assert close_weights[friction.token] < distant_weights[friction.token]


def test_advisory_is_ledger_backed_and_silent_without_history() -> None:
    assert npc_relationship_advisories(
        _Projection(npcs=(_npc(),)),
        protagonist_actor_ref="agent:companion",
    ) == ()
    advisories = npc_relationship_advisories(
        _Projection(
            npcs=(_npc(),),
            world_occurrences=(
                _settled_occurrence(occurrence_id="o1", settled_at=NOW - timedelta(days=1)),
                _settled_occurrence(occurrence_id="o2", settled_at=NOW - timedelta(days=3)),
            ),
        ),
        protagonist_actor_ref="agent:companion",
    )
    assert len(advisories) == 1
    advisory = advisories[0]
    assert advisory.kind == "npc_relationships"
    assert set(advisory.source_refs) == {"event:settled:o1", "event:settled:o2"}
    assert len(advisory.candidates) == 1
    assert "reviewed-person:fan-yuan" in advisory.candidates[0].value
    assert len(advisory.candidates[0].value) <= 256


def test_advisory_does_not_author_relationship_meaning_from_shared_history() -> None:
    advisories = npc_relationship_advisories(
        _Projection(
            npcs=(_npc(),),
            world_occurrences=(
                _settled_occurrence(occurrence_id="o1", settled_at=NOW - timedelta(days=1)),
                _settled_occurrence(occurrence_id="o2", settled_at=NOW - timedelta(days=3)),
            ),
        ),
        protagonist_actor_ref="agent:companion",
    )

    value = advisories[0].candidates[0].value
    assert "走得挺近" not in value
    assert "慢慢熟起来" not in value
    assert "有点疏远" not in value
