from __future__ import annotations

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

from test_affect_acceptance_runtime import _accept_ready_appraisal
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
