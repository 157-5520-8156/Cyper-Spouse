"""Compile a bounded delivered-media deliberation into a dedicated Thread lane."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .media_thread_events import (
    MediaDeliveryThreadProposalRecordedPayload,
    media_thread_mutation_hash,
)
from .schema_core import EvidenceRef, FrozenModel
from .schemas import (
    CommitResult,
    ProjectionCursor,
    ThreadOrigin,
    ThreadProjection,
    ThreadValues,
    WorldEvent,
    thread_semantic_fingerprint,
)


_CONTRACT = "media-delivery-thread-proposal-compiler.1"
_POLICY = ("policy:thread-v1",)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MediaThreadProposalCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"media_thread_proposal_compiler.{code}"
        super().__init__(self.code)


class MediaThreadProposalCompilation(FrozenModel):
    status: Literal["candidate_recorded"]
    source_proposal_id: str
    typed_proposal_id: str
    commit: CommitResult


class MediaDeliveryThreadProposalCompiler:
    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger, self._reader = ledger, DecisionProposalAuthorityReader(ledger=ledger)

    def record(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> MediaThreadProposalCompilation:
        authority = self._reader.read(
            self._reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        change, source_event, source_commit, trigger, operation = self._verify(authority, cursor)
        raw = change.payload.value()
        identity = _digest(
            {"contract": _CONTRACT, "audit": authority.audit.event_ref, "change": change.change_id}
        )
        typed_id = f"proposal:media-delivery-thread:{identity}"
        thread_event_id = f"event:media-delivery-thread:{operation}:" + _digest(
            {"thread": raw["thread_id"], "proposal": typed_id}
        )
        evidence = EvidenceRef(
            ref_id=source_event.event_id,
            evidence_type="committed_world_event",
            claim_purpose="conversation_continuity",
            source_world_revision=source_commit.world_revision,
            immutable_hash=source_event.payload_hash,
        )
        current = next(
            (
                item
                for item in self._ledger.project_at(cursor).threads
                if item.thread_id == raw["thread_id"]
            ),
            None,
        )
        if operation == "open":
            if current is not None:
                raise MediaThreadProposalCompilerError("thread_exists")
            values = ThreadValues(
                kind=raw["thread_kind"],
                subject_ref=raw["subject_ref"],
                conversation_ref=raw["conversation_ref"],
                anchor_evidence_refs=(evidence,),
                source_evidence_refs=(evidence,),
                importance_bp=raw["importance"],
                resolution_contract_ref=raw["resolution_contract_ref"],
                privacy_class=raw.get("privacy_class", "private"),
                expires_at=self._parse_time(raw.get("expires_at")),
            )
            before, expected = None, 0
        else:
            if current is None or current.values.status != "open":
                raise MediaThreadProposalCompilerError("thread_not_open")
            if current.origin.policy_refs != _POLICY:
                raise MediaThreadProposalCompilerError("thread_policy_not_dedicated")
            refs = current.values.source_evidence_refs
            if evidence.ref_id not in {item.ref_id for item in refs}:
                refs = (*refs, evidence)
            values = current.values.model_copy(
                update={
                    "source_evidence_refs": refs,
                    "importance_bp": raw["importance"],
                    "expires_at": self._parse_time(raw.get("expires_at")),
                    "privacy_class": raw.get("privacy_class", current.values.privacy_class),
                }
            )
            before, expected = current, current.entity_revision
        transition_id = "transition:media-delivery-thread:" + _digest({"proposal": typed_id})
        after = ThreadProjection(
            thread_id=raw["thread_id"],
            entity_revision=expected + 1,
            semantic_fingerprint=thread_semantic_fingerprint(
                kind=values.kind,
                subject_ref=values.subject_ref,
                conversation_ref=values.conversation_ref,
                anchor_evidence_refs=values.anchor_evidence_refs,
                resolution_contract_ref=values.resolution_contract_ref,
                policy_refs=_POLICY,
            ),
            values=values,
            origin=ThreadOrigin(
                change_id=change.change_id,
                transition_id=transition_id,
                policy_refs=_POLICY,
                accepted_event_ref=thread_event_id,
            ),
            opened_at=source_event.logical_time if before is None else before.opened_at,
            updated_at=source_event.logical_time,
        )
        material: dict[str, object] = {
            "media_thread_proposal_id": typed_id,
            "decision_proposal_id": authority.proposal.proposal_id,
            "change_id": change.change_id,
            "transition_id": transition_id,
            "operation": operation,
            "evaluated_world_revision": cursor.world_revision,
            "expected_entity_revision": expected,
            "delivery_id": source_event.payload()["delivery"]["delivery_id"],
            "delivery_event_ref": source_event.event_id,
            "delivery_event_payload_hash": source_event.payload_hash,
            "deliberation_trigger_id": trigger.trigger_id,
            "thread_before": before,
            "thread_after": after,
            "evidence_refs": values.source_evidence_refs,
            "policy_refs": _POLICY,
            "confidence_bp": authority.proposal.confidence,
        }
        material["proposed_change_hash"] = media_thread_mutation_hash(material)
        payload = MediaDeliveryThreadProposalRecordedPayload.model_validate(material)
        key = domain_idempotency_key(
            event_type="MediaDeliveryThreadProposalRecorded",
            world_id=world_id,
            payload=payload.model_dump(mode="json"),
        )
        if key is None:
            raise MediaThreadProposalCompilerError("proposal_identity_missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:media-delivery-thread-proposal:{identity}",
            world_id=world_id,
            event_type="MediaDeliveryThreadProposalRecorded",
            logical_time=source_event.logical_time,
            created_at=source_event.created_at,
            actor="worker:media-thread-proposal-compiler",
            source=_CONTRACT,
            trace_id=source_event.trace_id,
            causation_id=authority.audit.event_ref,
            correlation_id=source_event.correlation_id,
            idempotency_key=key,
            payload=payload.model_dump(mode="json"),
        )
        commit = self._ledger.commit(
            [event],
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            commit_id="commit:media-delivery-thread-proposal:"
            + _digest({"cursor": cursor.model_dump(mode="json"), "proposal": typed_id}),
        )
        return MediaThreadProposalCompilation(
            status="candidate_recorded",
            source_proposal_id=proposal_id,
            typed_proposal_id=typed_id,
            commit=commit,
        )

    def _verify(self, authority, cursor):
        if len(authority.proposal.proposed_changes) != 1:
            raise MediaThreadProposalCompilerError("change_count_invalid")
        change = authority.proposal.proposed_changes[0]
        if change.kind != "media_delivery_thread_transition" or change.transition not in {
            "open",
            "update",
        }:
            raise MediaThreadProposalCompilerError("change_invalid")
        operation = change.transition
        raw = change.payload.value()
        if change.target_id != raw.get("thread_id"):
            raise MediaThreadProposalCompilerError("thread_identity_invalid")
        source_event = self._event(authority.audit.trigger_ref)
        source_commit = self._ledger.lookup_event_commit(source_event.event_id)
        if source_event.event_type != "MediaDeliveryShared" or source_commit is None:
            raise MediaThreadProposalCompilerError("source_not_delivered_media")
        projection = self._ledger.project_at(cursor)
        delivery = source_event.payload().get("delivery")
        trigger = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "media_delivery_interaction"
                and item.state == "claimed"
                and item.claim_lease is not None
                and item.source_evidence_ref == source_event.event_id
            ),
            None,
        )
        if (
            not isinstance(delivery, dict)
            or not any(
                item.delivery_id == delivery.get("delivery_id")
                for item in projection.media_deliveries
            )
            or trigger is None
        ):
            raise MediaThreadProposalCompilerError("delivery_source_unavailable")
        if (
            len(change.evidence_refs) != 1
            or change.evidence_refs[0] != source_event.event_id
            or authority.proposal.evaluated_world_revision != cursor.world_revision
        ):
            raise MediaThreadProposalCompilerError("source_evidence_invalid")
        evidence = next(
            (
                item
                for item in authority.proposal.evidence_refs
                if item.ref_id == source_event.event_id
            ),
            None,
        )
        if (
            evidence is None
            or evidence.evidence_kind != "committed_world_event"
            or evidence.source_world_revision != source_commit[1].world_revision
            or evidence.immutable_hash.removeprefix("sha256:") != source_event.payload_hash
        ):
            raise MediaThreadProposalCompilerError("source_evidence_invalid")
        return change, source_event, source_commit[1], trigger, operation

    def _event(self, event_id: str) -> WorldEvent:
        located = self._ledger.lookup_event_commit(event_id)
        if located is None:
            raise MediaThreadProposalCompilerError("source_event_missing")
        return located[0]

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        raise MediaThreadProposalCompilerError("time_invalid")


__all__ = [
    "MediaThreadProposalCompilation",
    "MediaDeliveryThreadProposalCompiler",
    "MediaThreadProposalCompilerError",
]
