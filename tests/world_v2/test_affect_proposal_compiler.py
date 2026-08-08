from __future__ import annotations

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.affect_proposal_compiler import AffectProposalCompiler
from companion_daemon.world_v2.affect_acceptance_runtime import AffectAcceptanceRuntime
from companion_daemon.world_v2.deliberation import DeliberationResult
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.schemas import ProjectionCursor
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger

from test_affect_acceptance_runtime import (
    _accept_ready_appraisal,
    _record_ready_affect_proposal,
)
from test_appraisal_authority import WORLD_ID
from test_proposal_audit import NOW, _digest, _result


def test_compiler_records_a_source_bound_open_affect_candidate() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    from test_appraisal_authority import event

    ledger.commit(
        [event("event:world-started", "WorldStarted", {})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    _accept_ready_appraisal(ledger=ledger, issuer=issuer)
    appraisal = ledger.project().appraisals[0]
    evidence = appraisal.evidence_refs[0]
    base = _result()
    change = TypedChange(
        change_id="change:decision:affect:1",
        kind="affect_transition",
        target_id=f"affect:{appraisal.source_cluster_ref}",
        transition="open",
        expected_entity_revision=0,
        evidence_refs=(evidence.ref_id,),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="affect_transition.v1",
            value={
                "episode_id": "hint:ignored",
                "appraisal_change_refs": [appraisal.origin.change_id],
                "component_deltas": [{"name": "hurt", "value": 4200}],
                "decay_config": {
                    "object_ref": "policy:decay:standard",
                    "schema_version": "affect-decay.1",
                    "payload_hash": "sha256:" + "a" * 64,
                },
                "residue_config": {
                    "object_ref": "policy:residue:standard",
                    "schema_version": "affect-residue.1",
                    "payload_hash": "sha256:" + "b" * 64,
                },
            },
        ),
    )
    proposal = DecisionProposal(
        proposal_id="proposal:generic-affect:1",
        trigger_ref="trigger:generic-affect:1",
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=evidence.ref_id,
                evidence_kind="observed_message",
                source_world_revision=evidence.source_world_revision,
                immutable_hash="sha256:" + str(evidence.immutable_hash),
            ),
        ),
        proposed_changes=(change,),
        action_intents=(),
        confidence=8000,
        brief_rationale="The model proposes a bounded residual hurt episode.",
        affect_decision="propose",
        behavior_tendency="hold_space",
        stance="care_despite_hurt",
        display_strategy="partial_disclosure",
    )
    audit = base.audit
    result = DeliberationResult(
        result_id="deliberation:"
        + _digest(
            {
                "capsule_id": base.capsule_id,
                "proposal_hash": proposal.proposal_hash,
                "attempt_audits": [audit.model_dump(mode="json")],
            }
        ),
        capsule_id=base.capsule_id,
        proposal=proposal,
        audit=audit,
        attempt_audits=(audit,),
    )
    head = ledger.project()
    recorded = ProposalAuditRecorder(ledger=ledger).record(
        result,
        ProposalAuditContext(
            world_id=WORLD_ID,
            trigger_ref=proposal.trigger_ref,
            logical_time=NOW,
            created_at=NOW,
            actor="agent:companion",
            source="test",
            trace_id="trace:generic-affect",
            causation_id="cause:generic-affect",
            correlation_id="correlation:generic-affect",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
            expected_ledger_sequence=head.ledger_sequence,
        ),
    )
    compilation = AffectProposalCompiler(ledger=ledger).record(
        world_id=WORLD_ID, cursor=recorded.cursor, proposal_id=proposal.proposal_id
    )

    assert compilation.status == "candidate_recorded"
    typed = ledger.project().affect_proposals[0]
    assert typed.source_audit is not None
    assert typed.source_audit.proposal_event_ref == ledger.project().proposal_audits[0].event_ref
    assert typed.proposed_mutation.event_type == "AffectEpisodeOpened"
    assert compilation.commit is not None
    runtime = AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    accepted = runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(
            cursor=ProjectionCursor(
                world_revision=compilation.commit.world_revision,
                deliberation_revision=compilation.commit.deliberation_revision,
                ledger_sequence=compilation.commit.ledger_sequence,
            ),
            proposal_id=typed.proposal_id,
        ),
        actor="worker:affect",
        source="test:affect-acceptance",
    )
    assert accepted.world_revision == ledger.project().world_revision
    assert ledger.project().affect_episodes[0].components[0].intensity_bp == 4200
    capsule = context_capsule_compiler_from_ledger(
        ledger=ledger,
        relevance_scope=ContextRelevanceScope(
            actor_ref="agent:companion", related_subject_refs=("interaction:user:1",)
        ),
    ).compile(
        query_from_projection(
            ledger.project(), actor_ref="agent:companion", trigger_ref="event:next-turn"
        )
    )
    assert capsule.affect_episodes.availability == "available"
    assert '"dimension":"hurt"' in capsule.affect_episodes.items[0].payload_json


def _cursor(ledger: WorldLedger | SQLiteWorldLedger) -> ProjectionCursor:
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _accept_initial_episode(
    *, ledger: WorldLedger | SQLiteWorldLedger, issuer: AcceptedLedgerBatchIssuer
) -> None:
    payload = _record_ready_affect_proposal(ledger)
    runtime = AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(
            cursor=_cursor(ledger), proposal_id=str(payload["proposal_id"])
        ),
        actor="worker:affect",
        source="test:affect-acceptance",
    )


def _record_role_authored_transition(
    *,
    ledger: WorldLedger | SQLiteWorldLedger,
    transition: str,
) -> tuple[ProjectionCursor, str]:
    target = ledger.project().affect_episodes[0]
    appraisal = ledger.project().appraisals[0]
    evidence = appraisal.evidence_refs[0]
    if transition == "update":
        transition_payload = {
            "episode_id": target.episode_id,
            "appraisal_change_refs": [appraisal.origin.change_id],
            "component_targets": [
                {
                    "component_id": target.components[0].component_id,
                    "dimension": target.components[0].dimension,
                    "target_intensity_bp": 1800,
                }
            ],
        }
    elif transition == "resolve":
        transition_payload = {
            "episode_id": target.episode_id,
            "appraisal_change_refs": [appraisal.origin.change_id],
            "resolution_summary": "I no longer hold this episode as unfinished after reconsidering it.",
        }
    elif transition == "supersede":
        transition_payload = {
            "episode_id": target.episode_id,
            "appraisal_change_refs": [appraisal.origin.change_id],
            "component_targets": [
                {"dimension": "warmth", "target_intensity_bp": 3600}
            ],
            "decay_config": {
                "object_ref": "policy:decay:standard",
                "schema_version": "affect-decay.1",
                "payload_hash": "sha256:" + "a" * 64,
            },
            "residue_config": {
                "object_ref": "policy:residue:standard",
                "schema_version": "affect-residue.1",
                "payload_hash": "sha256:" + "b" * 64,
            },
        }
    else:  # pragma: no cover - the parametrized seam is closed above.
        raise AssertionError(transition)
    base = _result()
    proposal_id = f"proposal:generic-affect:{transition}"
    change = TypedChange(
        change_id=f"change:decision:affect:{transition}",
        kind="affect_transition",
        target_id=target.episode_id,
        transition=transition,
        expected_entity_revision=target.entity_revision,
        evidence_refs=(evidence.ref_id,),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="affect_transition.v1",
            value=transition_payload,
        ),
    )
    proposal = DecisionProposal(
        proposal_id=proposal_id,
        trigger_ref=f"trigger:generic-affect:{transition}",
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=evidence.ref_id,
                evidence_kind="observed_message",
                source_world_revision=evidence.source_world_revision,
                immutable_hash="sha256:" + str(evidence.immutable_hash),
            ),
        ),
        proposed_changes=(change,),
        action_intents=(),
        confidence=8000,
        brief_rationale=f"The character chose to {transition} an existing Affect episode.",
        affect_decision="propose",
        behavior_tendency="let_the_changed_feeling_exist",
        stance="self_authored_reappraisal",
        display_strategy="not_mechanically_expressed",
    )
    audit = base.audit
    result = DeliberationResult(
        result_id="deliberation:"
        + _digest(
            {
                "capsule_id": base.capsule_id,
                "proposal_hash": proposal.proposal_hash,
                "attempt_audits": [audit.model_dump(mode="json")],
            }
        ),
        capsule_id=base.capsule_id,
        proposal=proposal,
        audit=audit,
        attempt_audits=(audit,),
    )
    head = ledger.project()
    recorded = ProposalAuditRecorder(ledger=ledger).record(
        result,
        ProposalAuditContext(
            world_id=WORLD_ID,
            trigger_ref=proposal.trigger_ref,
            logical_time=head.logical_time or NOW,
            created_at=head.logical_time or NOW,
            actor="agent:companion",
            source="test",
            trace_id=f"trace:generic-affect:{transition}",
            causation_id=f"cause:generic-affect:{transition}",
            correlation_id=f"correlation:generic-affect:{transition}",
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
            expected_ledger_sequence=head.ledger_sequence,
        ),
    )
    assert all(ledger.lookup_event_commit(event_id) is not None for event_id in recorded.event_ids)
    return recorded.cursor, proposal_id


@pytest.mark.parametrize("transition", ["update", "resolve", "supersede"])
def test_role_authored_affect_lifecycle_transitions_accept_and_cold_replay(
    tmp_path, transition: str
) -> None:
    from datetime import timedelta

    from test_appraisal_authority import event

    path = tmp_path / f"affect-{transition}.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID, accepted_batch_issuer=issuer)
    ledger.commit(
        [event("event:world-started", "WorldStarted", {})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    _accept_ready_appraisal(ledger=ledger, issuer=issuer)
    _accept_initial_episode(ledger=ledger, issuer=issuer)
    head = ledger.project()
    assert head.logical_time is not None
    origin = head.logical_time
    later = origin + timedelta(minutes=30)
    ledger.commit(
        [
            event(
                f"event:clock-before-{transition}",
                "ClockAdvanced",
                {
                    "logical_time_from": origin.isoformat(),
                    "logical_time_to": later.isoformat(),
                },
                at=later,
            )
        ],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    audit_cursor, proposal_id = _record_role_authored_transition(
        ledger=ledger, transition=transition
    )

    compilation = AffectProposalCompiler(ledger=ledger).record(
        world_id=WORLD_ID,
        cursor=audit_cursor,
        proposal_id=proposal_id,
    )
    assert compilation.status == "candidate_recorded"
    assert compilation.acceptance_cursor is not None
    runtime = AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    handle = runtime.pin_proposal(
        cursor=compilation.acceptance_cursor,
        proposal_id=str(compilation.typed_proposal_id),
    )
    accepted = runtime.accept_runtime_owned(
        handle=handle,
        actor="worker:affect",
        source="test:affect-acceptance",
    )
    assert (
        runtime.accept_runtime_owned(
            handle=handle,
            actor="worker:affect",
            source="test:affect-acceptance",
        )
        == accepted
    )

    projection = ledger.project()
    if transition == "update":
        assert len(projection.affect_episodes) == 1
        assert projection.affect_episodes[0].status == "active"
        assert projection.affect_episodes[0].entity_revision == 2
        assert projection.affect_episodes[0].components[0].intensity_bp == 1800
        assert projection.affect_episodes[0].updated_at == later
    elif transition == "resolve":
        assert len(projection.affect_episodes) == 1
        assert projection.affect_episodes[0].status == "resolved"
        assert projection.affect_episodes[0].entity_revision == 2
        assert projection.affect_episodes[0].resolution_refs
    else:
        assert len(projection.affect_episodes) == 2
        predecessor, successor = projection.affect_episodes
        assert predecessor.status == "superseded"
        assert predecessor.superseded_by_episode_id == successor.episode_id
        assert successor.status == "active"
        assert successor.supersedes_episode_id == predecessor.episode_id
        assert successor.components[0].dimension == "warmth"

    expected = ledger.project()
    assert ledger.rebuild() == expected
    ledger.close()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()
