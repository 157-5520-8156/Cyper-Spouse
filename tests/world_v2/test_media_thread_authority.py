from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from legacy_migration_support import read_head_state_json

from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.media_thread_proposal_compiler import (
    MediaDeliveryThreadProposalCompiler,
    MediaThreadProposalCompilerError,
)
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import ProjectionCursor
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger

from test_interaction_bid_authority import NOW, WORLD, _digest, _prepared_ledger, _result


def _cursor(ledger) -> ProjectionCursor:
    head = ledger.project()
    return ProjectionCursor(
        world_revision=head.world_revision,
        deliberation_revision=head.deliberation_revision,
        ledger_sequence=head.ledger_sequence,
    )


def _audit(ledger, source, source_revision):
    change = TypedChange(
        change_id="change:media-thread:1",
        kind="media_delivery_thread_transition",
        target_id="thread:media:1",
        transition="open",
        expected_entity_revision=0,
        evidence_refs=(source.event_id,),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="media_delivery_thread_transition.v1",
            value={
                "thread_id": "thread:media:1",
                "thread_kind": "topic_open",
                "subject_ref": "subject:photo",
                "conversation_ref": "conversation:1",
                "importance": 3200,
                "resolution_contract_ref": "resolution-contract:photo-followup",
                "expires_at": None,
                "privacy_class": "private",
            },
        ),
    )
    proposal = DecisionProposal(
        proposal_id="proposal:media-thread:1",
        trigger_ref=source.event_id,
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=source.event_id,
                evidence_kind="committed_world_event",
                source_world_revision=source_revision,
                immutable_hash="sha256:" + source.payload_hash,
            ),
        ),
        proposed_changes=(change,),
        action_intents=(),
        confidence=7300,
        brief_rationale="A shared photo can leave a private, low-priority follow-up thread.",
        behavior_tendency="offer",
        stance="invite",
        display_strategy="private",
    )
    base = _result()
    result = DeliberationResult(
        result_id="deliberation:"
        + _digest(
            {
                "capsule_id": base.capsule_id,
                "proposal_hash": proposal.proposal_hash,
                "attempt_audits": [base.audit.model_dump(mode="json")],
            }
        ),
        capsule_id=base.capsule_id,
        proposal=proposal,
        audit=base.audit,
        attempt_audits=(base.audit,),
    )
    head = ledger.project()
    recorded = ProposalAuditRecorder(ledger=ledger).record(
        result,
        ProposalAuditContext(
            world_id=WORLD,
            trigger_ref=source.event_id,
            logical_time=NOW,
            created_at=NOW,
            actor="agent:companion",
            source="test",
            trace_id="trace:interaction-bid",
            causation_id="cause:proposal",
            correlation_id="correlation:interaction-bid",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
            expected_ledger_sequence=head.ledger_sequence,
        ),
    )
    return proposal, recorded


def test_delivery_can_open_thread_only_through_dedicated_atomic_lane() -> None:
    # The independent media-delivery thread author was retired by the
    # CharacterInterior migration (same lane as the interaction bid).  Opening
    # and claiming the delivery trigger derives a deterministic retired
    # outcome; the old parallel worker must never compile a thread on its own.
    ledger, source, source_revision = _prepared_ledger()
    projection = ledger.project()
    process = projection.trigger_processes[0]
    assert process.process_kind == "media_delivery_interaction"
    assert process.state == "terminal"
    assert process.runtime_outcome_ref == (
        "retired-technical:media-delivery-interaction-author-removed"
    )
    assert process.trigger_id in projection.completed_trigger_ids
    # The retired lane cannot be re-armed: a compiler pinned at the current
    # cursor must refuse the retired (non-claimed) source trigger.
    proposal, audited = _audit(ledger, source, source_revision)
    with pytest.raises(MediaThreadProposalCompilerError) as excinfo:
        MediaDeliveryThreadProposalCompiler(ledger=ledger).record(
            world_id=WORLD, cursor=audited.cursor, proposal_id=proposal.proposal_id
        )
    assert excinfo.value.code == (
        "media_thread_proposal_compiler.delivery_source_unavailable"
    )


def test_sqlite_migrates_v31_head_without_inventing_media_thread_proposals(tmp_path) -> None:
    path = tmp_path / "media-thread-v31.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    from test_interaction_bid_authority import _event

    ledger.commit(
        [_event("WorldStarted", {}, "media-thread-migrate")],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.close()
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT world_revision FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()
        assert row is not None
        state = ReducerState.model_validate_json(read_head_state_json(connection, WORLD))
        semantic = state.semantic_payload(
            world_id=WORLD,
            world_revision=int(row[0]),
            reducer_bundle_version="world-v2-reducers.31",
        )
        legacy_hash = hashlib.sha256(
            json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        connection.execute(
            "UPDATE world_v2_heads SET semantic_hash = ?, reducer_bundle_version = ?, state_hash = '' WHERE world_id = ?",
            (legacy_hash, "world-v2-reducers.31", WORLD),
        )
    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.53"
    assert migrated.project().media_thread_proposals == ()
    assert migrated.rebuild() == migrated.project()
