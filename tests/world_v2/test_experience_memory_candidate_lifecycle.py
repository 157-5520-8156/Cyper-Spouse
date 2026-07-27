from __future__ import annotations

import pytest

from companion_daemon.world_v2.experience_memory_candidate_lifecycle import (
    ExperienceMemoryCandidateLifecycle,
)
from companion_daemon.world_v2.fact_memory_draft import FactMemoryRetentionDraft
from companion_daemon.world_v2.life_content_store import InMemoryImmutableLifeContentStore
from companion_daemon.world_v2.schemas import (
    MEMORY_SALIENCE_MATRIX_DIGEST,
    MemorySalienceVector,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger

import test_experience_authority as authority


class _CrashAfterOpenLifecycle(ExperienceMemoryCandidateLifecycle):
    def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self._writes = 0

    def _record_and_accept(self, **kwargs):  # type: ignore[no-untyped-def]
        self._writes += 1
        if self._writes == 2:
            raise RuntimeError("simulated crash after pending open")
        return super()._record_and_accept(**kwargs)


def test_experience_memory_lifecycle_accepts_only_the_exact_committed_experience(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = authority.initialized(
        kind=lambda *, world_id: SQLiteWorldLedger(
            path=tmp_path / "experience-memory.sqlite3", world_id=world_id
        )
    )
    value = authority.mutation(
        authority.experience(),
        proposal_id="proposal:experience-memory",
        evaluated_world_revision=ledger.project().world_revision,
    )
    authority.record_accept_mutate(ledger, value)
    projection = ledger.project()
    experience = projection.experiences[0]
    transition = projection.experience_transitions[0]
    event, commit = ledger.lookup_event_commit(experience.origin.accepted_event_ref)
    draft = FactMemoryRetentionDraft(
        cue_kind="world_continuity",
        retention_rationales=("world_continuity",),
        salience=MemorySalienceVector(
            autobiographical_relevance_bp=7_000,
            relationship_relevance_bp=2_000,
            emotional_residue_bp=2_000,
            unfinished_business_bp=1_000,
            recurrence_bp=3_000,
            novelty_bp=6_000,
            future_utility_bp=5_000,
            world_continuity_bp=9_000,
            matrix_digest=MEMORY_SALIENCE_MATRIX_DIGEST,
        ),
    )

    candidate = ExperienceMemoryCandidateLifecycle(
        ledger=ledger,
        actor="worker:test",
        source="test:experience-memory",
    ).accept(
        experience=experience,
        transition=transition,
        experience_event=event,
        experience_world_revision=commit.world_revision,
        draft=draft,
        logical_time=authority.NOW,
        created_at=authority.NOW,
        trace_id="trace:experience-memory",
        correlation_id="conversation:experience-memory",
    )

    assert candidate is not None
    assert candidate.values.status == "active"
    assert candidate.values.source_bindings[0].source_kind == "experience"
    assert candidate.values.source_bindings[0].source_id == experience.experience_id
    assert any(
        item.candidate_id == candidate.candidate_id
        and item.values.status == "active"
        for item in ledger.project().memory_candidates
    )


def test_experience_memory_lifecycle_rejects_missing_content_authority(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = authority.initialized(
        kind=lambda *, world_id: SQLiteWorldLedger(
            path=tmp_path / "experience-memory-missing-content.sqlite3", world_id=world_id
        )
    )
    value = authority.mutation(
        authority.experience(),
        proposal_id="proposal:experience-memory-missing-content",
        evaluated_world_revision=ledger.project().world_revision,
    )
    authority.record_accept_mutate(ledger, value)
    projection = ledger.project()
    experience = projection.experiences[0]
    transition = projection.experience_transitions[0]
    event, commit = ledger.lookup_event_commit(experience.origin.accepted_event_ref)
    draft = FactMemoryRetentionDraft(
        cue_kind="world_continuity",
        retention_rationales=("world_continuity",),
        salience=MemorySalienceVector(
            autobiographical_relevance_bp=7_000,
            relationship_relevance_bp=2_000,
            emotional_residue_bp=2_000,
            unfinished_business_bp=1_000,
            recurrence_bp=3_000,
            novelty_bp=6_000,
            future_utility_bp=5_000,
            world_continuity_bp=9_000,
            matrix_digest=MEMORY_SALIENCE_MATRIX_DIGEST,
        ),
    )

    lifecycle = ExperienceMemoryCandidateLifecycle(
        ledger=ledger,
        actor="worker:test",
        source="test:experience-memory",
        content_store=InMemoryImmutableLifeContentStore(),
    )
    with pytest.raises(ValueError, match="LifeContentRecorded descriptor"):
        lifecycle.accept(
            experience=experience,
            transition=transition,
            experience_event=event,
            experience_world_revision=commit.world_revision,
            draft=draft,
            logical_time=authority.NOW,
            created_at=authority.NOW,
            trace_id="trace:experience-memory-missing-content",
            correlation_id="conversation:experience-memory-missing-content",
        )


def test_experience_memory_lifecycle_resumes_pending_after_crash(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ledger = authority.initialized(
        kind=lambda *, world_id: SQLiteWorldLedger(
            path=tmp_path / "experience-memory-resume.sqlite3",
            world_id=world_id,
        )
    )
    value = authority.mutation(
        authority.experience(),
        proposal_id="proposal:experience-memory-resume",
        evaluated_world_revision=ledger.project().world_revision,
    )
    authority.record_accept_mutate(ledger, value)
    projection = ledger.project()
    experience = projection.experiences[0]
    transition = projection.experience_transitions[0]
    event, commit = ledger.lookup_event_commit(experience.origin.accepted_event_ref)
    draft = FactMemoryRetentionDraft(
        cue_kind="world_continuity",
        retention_rationales=("world_continuity",),
        salience=MemorySalienceVector(
            autobiographical_relevance_bp=7_000,
            relationship_relevance_bp=2_000,
            emotional_residue_bp=2_000,
            unfinished_business_bp=1_000,
            recurrence_bp=3_000,
            novelty_bp=6_000,
            future_utility_bp=5_000,
            world_continuity_bp=9_000,
            matrix_digest=MEMORY_SALIENCE_MATRIX_DIGEST,
        ),
    )
    crashed = _CrashAfterOpenLifecycle(
        ledger=ledger,
        actor="worker:test",
        source="test:experience-memory",
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashed.accept(
            experience=experience,
            transition=transition,
            experience_event=event,
            experience_world_revision=commit.world_revision,
            draft=draft,
            logical_time=authority.NOW,
            created_at=authority.NOW,
            trace_id="trace:experience-memory-resume",
            correlation_id="conversation:experience-memory-resume",
        )
    assert ledger.project().memory_candidates[0].values.status == "pending"

    active = ExperienceMemoryCandidateLifecycle(
        ledger=ledger,
        actor="worker:test",
        source="test:experience-memory",
    ).accept(
        experience=experience,
        transition=transition,
        experience_event=event,
        experience_world_revision=commit.world_revision,
        draft=draft.model_copy(
            update={
                "cue_kind": "novelty",
                "retention_rationales": ("novelty",),
            }
        ),
        logical_time=authority.NOW,
        created_at=authority.NOW,
        trace_id="trace:experience-memory-resume",
        correlation_id="conversation:experience-memory-resume",
    )

    assert active is not None
    assert active.values.status == "active"
    assert active.values.cue_kind == "world_continuity"
    assert active.values.retention_rationales == ("world_continuity",)
