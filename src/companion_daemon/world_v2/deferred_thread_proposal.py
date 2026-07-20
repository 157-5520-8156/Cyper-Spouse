"""Compile one model-chosen deferred reply into a typed unfinished Thread proposal."""

from __future__ import annotations

import hashlib
import json

from .event_identity import domain_idempotency_key
from .proposal_audit_schemas import ProposalAuditProjection
from .proposal_envelope import validate_proposal_envelope
from .schemas import (
    EvidenceRef,
    MessageObservationRef,
    ProjectionCursor,
    ThreadOrigin,
    ThreadProjection,
    ThreadProposalProjection,
    ThreadProposedMutation,
    ThreadValues,
    WorldEvent,
    thread_semantic_fingerprint,
)
from .thread_events import ThreadChangedPayload, thread_mutation_hash


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class DeferredThreadProposalCompiler:
    """Create a typed proposal, never a Thread, from the shared main audit."""

    POLICY_REFS = ("policy:thread-v1",)

    def __init__(self, *, ledger) -> None:
        self.ledger = ledger

    def record(
        self,
        *,
        audit: ProposalAuditProjection,
        cursor: ProjectionCursor,
        source_observation: MessageObservationRef,
        source_event: WorldEvent,
    ) -> tuple[ThreadChangedPayload, ProjectionCursor]:
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
        if (
            audit.evaluated_world_revision != cursor.world_revision
            or proposal.evaluated_world_revision != cursor.world_revision
            or proposal.trigger_ref != source_event.event_id
            or len(proposal.action_intents) != 1
            or proposal.action_intents[0].kind != "followup"
            or proposal.action_intents[0].due_window is None
        ):
            raise ValueError("deferred thread compiler requires one current shared followup audit")
        located = self.ledger.lookup_event_commit(source_event.event_id)
        committed_ref = self.ledger.resolve_committed_event_refs(
            (source_event.event_id,), at_world_revision=cursor.world_revision
        ).get(source_event.event_id)
        if (
            located is None
            or located[0] != source_event
            or committed_ref is None
            or committed_ref.world_revision != source_observation.world_revision
            or committed_ref.payload_hash != source_event.payload_hash
            or source_observation.event_payload_hash != source_event.payload_hash
        ):
            raise ValueError("deferred thread compiler source is not exact observation authority")
        root = {
            "contract": "deferred-thread-proposal.1",
            "world": self.ledger.world_id,
            "decision_proposal": proposal.proposal_id,
            "proposal_hash": proposal.proposal_hash,
            "source_event": source_event.event_id,
        }
        typed_id = "proposal:deferred-thread:" + _digest(root)
        projection = self.ledger.project_at(cursor)
        existing = next((item for item in projection.thread_proposals
                         if item.proposal_id == typed_id), None)
        if existing is not None:
            payload = ThreadChangedPayload.model_validate_json(existing.proposed_mutation.payload_json)
            return payload, cursor
        evidence = EvidenceRef(
            ref_id=source_observation.observation_id,
            evidence_type="observed_message",
            claim_purpose="conversation_continuity",
            source_world_revision=source_observation.world_revision,
            immutable_hash=source_event.payload_hash,
        )
        due = proposal.action_intents[0].due_window
        assert due is not None
        thread_id = "thread:reply-reconsideration:" + _digest(root)
        change_id = "change:deferred-thread:" + _digest({**root, "role": "change"})
        transition_id = "transition:deferred-thread:" + _digest({**root, "role": "transition"})
        acceptance_id = "acceptance:deferred-thread:" + _digest(root)
        mutation_event_ref = "event:deferred-thread:opened:" + _digest(root)
        values = ThreadValues(
            kind="reply_reconsideration",
            subject_ref=source_observation.observation_id,
            conversation_ref="conversation:source:" + _digest({
                "actor": source_observation.actor,
                "channel": source_observation.channel,
            }),
            anchor_evidence_refs=(evidence,), source_evidence_refs=(evidence,),
            importance_bp=5_000,
            due_window={"opens_at": due[0], "closes_at": due[1]},
            expires_at=due[1],
            resolution_contract_ref="resolution:followup-receipt:" + _digest(proposal.action_intents[0].intent_id),
            privacy_class="private", status="open",
        )
        origin = ThreadOrigin(
            change_id=change_id, transition_id=transition_id,
            policy_refs=self.POLICY_REFS, accepted_event_ref=mutation_event_ref,
        )
        at = projection.logical_time or source_event.logical_time
        thread = ThreadProjection(
            thread_id=thread_id, entity_revision=1,
            semantic_fingerprint=thread_semantic_fingerprint(
                kind=values.kind, subject_ref=values.subject_ref,
                conversation_ref=values.conversation_ref,
                anchor_evidence_refs=values.anchor_evidence_refs,
                resolution_contract_ref=values.resolution_contract_ref,
                policy_refs=origin.policy_refs,
            ),
            values=values, origin=origin, opened_at=at, updated_at=at,
        )
        raw: dict[str, object] = {
            "change_id": change_id, "transition_id": transition_id,
            "expected_entity_revision": 0, "evidence_refs": (evidence,),
            "policy_refs": self.POLICY_REFS, "acceptance_id": acceptance_id,
            "proposal_id": typed_id, "evaluated_world_revision": cursor.world_revision,
            "accepted_change_hash": "0" * 64, "operation": "open",
            "thread_before": None, "thread_after": thread,
            "compensates_transition_id": None,
        }
        raw["accepted_change_hash"] = thread_mutation_hash(raw)
        payload = ThreadChangedPayload.model_validate(raw)
        typed = ThreadProposalProjection(
            proposal_id=typed_id, proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:thread.1", transition_kind="open",
            change_id=change_id, transition_id=transition_id,
            evaluated_world_revision=cursor.world_revision, expected_entity_revision=0,
            proposed_change_hash=payload.accepted_change_hash,
            evidence_refs=(evidence,), policy_refs=self.POLICY_REFS,
            proposed_mutation=ThreadProposedMutation(
                event_type="ThreadOpened", payload_json=_canonical(payload.model_dump(mode="json"))
            ),
        )
        event_payload = typed.model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:deferred-thread:proposal:" + _digest(root),
            world_id=self.ledger.world_id, event_type="ProposalRecorded",
            logical_time=at, created_at=source_event.created_at,
            actor="worker:deferred-thread-proposal", source="deferred-thread-proposal.1",
            trace_id=source_event.trace_id, causation_id=audit.event_ref,
            correlation_id=source_event.correlation_id,
            idempotency_key=(domain_idempotency_key(
                event_type="ProposalRecorded", world_id=self.ledger.world_id, payload=event_payload
            ) or "world-v2:deferred-thread-proposal:" + _digest(root)),
            payload=event_payload,
        )
        commit = self.ledger.commit_at_cursor(
            (event,), expected_cursor=cursor,
            commit_id="commit:deferred-thread-proposal:" + _digest(root),
        )
        return payload, ProjectionCursor(
            world_revision=commit.world_revision,
            deliberation_revision=commit.deliberation_revision,
            ledger_sequence=commit.ledger_sequence,
        )


__all__ = ["DeferredThreadProposalCompiler"]
