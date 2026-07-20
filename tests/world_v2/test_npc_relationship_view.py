"""Per-NPC relationship reading: derived projection, weight tilt, advisory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.life_author_seed import (
    NpcInitiativeCandidate,
    ReviewedLifeOutcome,
    ReviewedNpcInitiatedEvent,
)
from companion_daemon.world_v2.npc_initiative import NpcInitiativeWeightPolicy
from companion_daemon.world_v2.npc_relationship_view import (
    RESTING_CLOSENESS_BP,
    npc_relationship_advisories,
    npc_relationship_readings,
)
from companion_daemon.world_v2.schemas import (
    AppraisalHypothesis,
    AppraisalOrigin,
    AppraisalProjection,
    DueWindow,
    EvidenceRef,
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
    *, occurrence_id: str, npc_id: str = "literature-fan", settled_at: datetime
) -> WorldOccurrenceProjection:
    return WorldOccurrenceProjection(
        occurrence_id=occurrence_id,
        entity_revision=3,
        trigger_ref=f"trigger:{occurrence_id}",
        participant_refs=("agent:companion", f"npc:{npc_id}"),
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


def _conflict_appraisal(*, weight_bp: int = 8_000) -> AppraisalProjection:
    return AppraisalProjection(
        appraisal_id="appraisal:npc-conflict:1",
        entity_revision=1,
        subject_ref="npc:literature-fan",
        source_cluster_ref="cluster:npc:1",
        origin=AppraisalOrigin(
            change_id="change:npc-appraisal:1",
            transition_id="transition:npc-appraisal:1",
            policy_refs=("policy:appraisal-v1",),
            matrix_catalog_version="appraisal-matrix.1",
            clustering_policy_version="source-clustering.1",
            accepted_event_ref="event:npc-appraisal-accepted:1",
        ),
        hypotheses=(
            AppraisalHypothesis(
                hypothesis_id="meaning:npc-conflict",
                meaning="npc_conflict",
                attribution="npc",
                controllability="partly_controllable",
                severity="moderate",
                weight_bp=weight_bp,
            ),
            AppraisalHypothesis(
                hypothesis_id="meaning:ordinary",
                meaning="ordinary",
                attribution="situation",
                controllability="uncontrollable",
                severity="low",
                weight_bp=10_000 - weight_bp,
            ),
        ),
        evidence_refs=(
            EvidenceRef(
                ref_id="event:settled:friction",
                evidence_type="committed_world_event",
                claim_purpose="past_experience",
                source_world_revision=6,
                immutable_hash="0" * 64,
            ),
        ),
        confidence_bp=7_500,
        accepted_at=NOW - timedelta(hours=3),
        expires_at=NOW + timedelta(days=3),
    )


class _Projection:
    def __init__(self, *, npcs=(), world_occurrences=(), appraisals=(), logical_time=NOW):
        self.npcs = npcs
        self.world_occurrences = world_occurrences
        self.appraisals = appraisals
        self.logical_time = logical_time


def test_reading_warms_with_shared_history_and_cools_with_friction() -> None:
    stranger = npc_relationship_readings(_Projection(npcs=(_npc(),)))
    assert stranger[0].closeness_bp == RESTING_CLOSENESS_BP
    assert stranger[0].settled_shared_count == 0
    assert stranger[0].source_event_refs == ()

    shared = npc_relationship_readings(_Projection(
        npcs=(_npc(),),
        world_occurrences=(
            _settled_occurrence(occurrence_id="o1", settled_at=NOW - timedelta(days=1)),
            _settled_occurrence(occurrence_id="o2", settled_at=NOW - timedelta(days=2)),
            _settled_occurrence(occurrence_id="o3", settled_at=NOW - timedelta(days=20)),
        ),
    ))
    assert shared[0].settled_shared_count == 3
    assert shared[0].closeness_bp > RESTING_CLOSENESS_BP
    assert shared[0].familiarity_bp > 0
    assert shared[0].last_shared_at == NOW - timedelta(days=1)
    assert "event:settled:o1" in shared[0].source_event_refs

    frictioned = npc_relationship_readings(_Projection(
        npcs=(_npc(),),
        world_occurrences=(
            _settled_occurrence(occurrence_id="o1", settled_at=NOW - timedelta(days=1)),
        ),
        appraisals=(_conflict_appraisal(),),
    ))
    assert frictioned[0].friction_bp > 0
    assert frictioned[0].closeness_bp < npc_relationship_readings(_Projection(
        npcs=(_npc(),),
        world_occurrences=(
            _settled_occurrence(occurrence_id="o1", settled_at=NOW - timedelta(days=1)),
        ),
    ))[0].closeness_bp
    # An expired conflict no longer counts as live friction.
    expired = _conflict_appraisal().model_copy(
        update={"expires_at": NOW - timedelta(minutes=1)}
    )
    calm = npc_relationship_readings(_Projection(
        npcs=(_npc(),), appraisals=(expired,),
    ))
    assert calm[0].friction_bp == 0


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


def test_weight_policy_v2_tilts_by_this_npcs_own_reading() -> None:
    policy = NpcInitiativeWeightPolicy()
    assert policy.version == "npc-initiative-weight.2"
    shared_time = _candidate("shared_time")
    friction = _candidate("friction")

    close = npc_relationship_readings(_Projection(
        npcs=(_npc(),),
        world_occurrences=tuple(
            _settled_occurrence(
                occurrence_id=f"o{index}", settled_at=NOW - timedelta(days=index + 1)
            )
            for index in range(4)
        ),
    ))
    distant_weights = policy.compile(candidates=(shared_time, friction))
    close_weights = policy.compile(
        candidates=(shared_time, friction), npc_relationships=close
    )
    # Closeness invites shared time; it never becomes a gate.
    assert close_weights[shared_time.token] > distant_weights[shared_time.token]

    frictioned = npc_relationship_readings(_Projection(
        npcs=(_npc(),), appraisals=(_conflict_appraisal(),),
    ))
    friction_weights = policy.compile(
        candidates=(shared_time, friction), npc_relationships=frictioned
    )
    assert friction_weights[friction.token] > distant_weights[friction.token]


def test_advisory_is_ledger_backed_and_silent_without_history() -> None:
    assert npc_relationship_advisories(_Projection(npcs=(_npc(),))) == ()
    advisories = npc_relationship_advisories(_Projection(
        npcs=(_npc(),),
        world_occurrences=(
            _settled_occurrence(occurrence_id="o1", settled_at=NOW - timedelta(days=1)),
            _settled_occurrence(occurrence_id="o2", settled_at=NOW - timedelta(days=3)),
        ),
    ))
    assert len(advisories) == 1
    advisory = advisories[0]
    assert advisory.kind == "npc_relationships"
    assert set(advisory.source_refs) == {"event:settled:o1", "event:settled:o2"}
    assert len(advisory.candidates) == 1
    assert "reviewed-person:fan-yuan" in advisory.candidates[0].value
    assert len(advisory.candidates[0].value) <= 256
