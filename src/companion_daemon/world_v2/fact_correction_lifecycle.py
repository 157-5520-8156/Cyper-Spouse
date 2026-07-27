"""Source-bound correction of an occupied single-valued Fact slot."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .event_identity import domain_idempotency_key
from .fact_accepted_contracts import FactCommitIntentV2
from .fact_events import FactChangedPayload, fact_mutation_hash
from .schema_core import EvidenceRef
from .schemas import (
    FactAssertionBinding,
    FactOrigin,
    FactProjection,
    FactProposalProjection,
    FactProposedMutation,
    FactValues,
    Observation,
    ProjectionCursor,
    WorldEvent,
    fact_semantic_fingerprint,
)
from .sqlite_ledger import SQLiteWorldLedger


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class FactCorrectionLifecycle:
    """Apply the model-selected current assertion as a typed Fact correction."""

    def __init__(self, *, ledger: SQLiteWorldLedger, actor: str, source: str) -> None:
        self._ledger = ledger
        self._actor = actor
        self._source = source

    def correct(
        self,
        *,
        before: FactProjection,
        intent: FactCommitIntentV2,
        observation: Observation,
        observation_event: WorldEvent,
        observation_world_revision: int,
        logical_time: datetime,
        created_at: datetime,
    ) -> FactProjection:
        return self._transition(
            operation="correct",
            before=before,
            intent=intent,
            observation=observation,
            observation_event=observation_event,
            observation_world_revision=observation_world_revision,
            logical_time=logical_time,
            created_at=created_at,
        )

    def withdraw(
        self,
        *,
        before: FactProjection,
        observation: Observation,
        observation_event: WorldEvent,
        observation_world_revision: int,
        logical_time: datetime,
        created_at: datetime,
    ) -> FactProjection:
        intent = FactCommitIntentV2(
            subject_ref=before.values.subject_ref,
            predicate_code=before.values.predicate_code,
            value_ref=before.values.value_ref,
            value_hash=f"sha256:{before.values.value_hash}",
            assertion_source_ref=observation.observation_id,
            evidence_uses=(
                {
                    "evidence_ref": observation.observation_id,
                    "purpose": "current_fact",
                    "anchor": True,
                },
            ),
            confidence_bp=before.values.confidence_bp,
            privacy_class=before.values.privacy_class,
        )
        return self._transition(
            operation="withdraw",
            before=before,
            intent=intent,
            observation=observation,
            observation_event=observation_event,
            observation_world_revision=observation_world_revision,
            logical_time=logical_time,
            created_at=created_at,
        )

    def _transition(
        self,
        *,
        operation: str,
        before: FactProjection,
        intent: FactCommitIntentV2,
        observation: Observation,
        observation_event: WorldEvent,
        observation_world_revision: int,
        logical_time: datetime,
        created_at: datetime,
    ) -> FactProjection:
        if (
            before.values.status != "active"
            or before.values.cardinality != "single"
            or before.values.subject_ref != intent.subject_ref
            or before.values.predicate_code != intent.predicate_code
            or (
                operation == "correct"
                and before.values.value_hash
                == intent.value_hash.removeprefix("sha256:")
            )
            or operation not in {"correct", "withdraw"}
        ):
            raise ValueError("Fact transition requires one matching active single slot")
        if (
            observation_event.event_type != "ObservationRecorded"
            or observation.observation_id != intent.assertion_source_ref
            or observation_event.payload_hash
            != hashlib.sha256(observation_event.payload_json.encode()).hexdigest()
            or observation_world_revision < 1
        ):
            raise ValueError("Fact correction requires one exact committed observation")

        base_identity = _digest(
            {
                "fact_id": before.fact_id,
                "before_revision": before.entity_revision,
                "observation_event_id": observation_event.event_id,
                "observation_payload_hash": observation_event.payload_hash,
                "value_hash": intent.value_hash,
                "operation": operation,
            }
        )
        with self._ledger.serialized_commit_sequence():
            projection = self._ledger.project()
            existing = next(
                (
                    item
                    for item in projection.fact_proposals
                    if item.transition_kind == operation
                    and (
                        candidate := FactChangedPayload.model_validate_json(
                            item.proposed_mutation.payload_json
                        )
                    ).fact_before
                    == before
                    and candidate.fact_after.values.source_evidence_refs[-1].ref_id
                    == observation.observation_id
                ),
                None,
            )
            if (
                existing is not None
                and existing.evaluated_world_revision < projection.world_revision
            ):
                stale_identity = _digest(existing.proposal_id)
                stale_payload = {
                    "acceptance_id": f"acceptance:fact-transition:stale:{stale_identity}",
                    "status": "stale",
                    "proposal_id": existing.proposal_id,
                    "evaluated_world_revision": existing.evaluated_world_revision,
                }
                stale = self._event(
                    event_id=f"event:fact-transition:stale:{stale_identity}",
                    event_type="AcceptanceRecorded",
                    payload=stale_payload,
                    logical_time=logical_time,
                    created_at=created_at,
                    causation_id=observation_event.event_id,
                    trace_id=observation.trace_id,
                    correlation_id=observation.correlation_id,
                )
                self._ledger.commit(
                    (stale,),
                    expected_world_revision=projection.world_revision,
                    expected_deliberation_revision=projection.deliberation_revision,
                )
                projection = self._ledger.project()
                existing = None
            if existing is None:
                identity = _digest(
                    {
                        "base_identity": base_identity,
                        "evaluated_world_revision": projection.world_revision,
                    }
                )
                proposal_id = f"proposal:fact-correction:{identity}"
                mutation = self._mutation(
                    before=before,
                    intent=intent,
                    observation=observation,
                    observation_event=observation_event,
                    observation_world_revision=observation_world_revision,
                    evaluated_world_revision=projection.world_revision,
                    logical_time=logical_time,
                    identity=identity,
                    operation=operation,
                )
                proposal = FactProposalProjection(
                    proposal_id=proposal_id,
                    proposal_encoding="typed-authority-v1",
                    authority_contract_ref="proposal-contract:fact.1",
                    transition_kind=operation,  # type: ignore[arg-type]
                    change_id=mutation.change_id,
                    transition_id=mutation.transition_id,
                    evaluated_world_revision=mutation.evaluated_world_revision,
                    expected_entity_revision=mutation.expected_entity_revision,
                    proposed_change_hash=mutation.accepted_change_hash,
                    evidence_refs=mutation.evidence_refs,
                    policy_refs=mutation.policy_refs,
                    proposed_mutation=FactProposedMutation(
                        event_type={
                            "correct": "FactCorrected",
                            "withdraw": "FactWithdrawn",
                        }[operation],  # type: ignore[arg-type]
                        payload_json=_canonical(mutation.model_dump(mode="json")),
                    ),
                )
                proposal_event = self._event(
                    event_id=f"event:fact-correction:proposal:{identity}",
                    event_type="ProposalRecorded",
                    payload=proposal.model_dump(mode="json"),
                    logical_time=logical_time,
                    created_at=created_at,
                    causation_id=observation_event.event_id,
                    trace_id=observation.trace_id,
                    correlation_id=observation.correlation_id,
                )
                self._ledger.commit(
                    (proposal_event,),
                    expected_world_revision=projection.world_revision,
                    expected_deliberation_revision=projection.deliberation_revision,
                )
            else:
                proposal = existing
                proposal_id = proposal.proposal_id
                identity = proposal_id.removeprefix("proposal:fact-correction:")
                mutation = FactChangedPayload.model_validate_json(
                    proposal.proposed_mutation.payload_json
                )
                proposal_event = self._ledger.lookup_event_commit(
                    f"event:fact-correction:proposal:{identity}"
                )
                if (
                    proposal.transition_kind != operation
                    or mutation.fact_before != before
                    or mutation.fact_after.values.source_evidence_refs[-1].ref_id
                    != observation.observation_id
                    or proposal_event is None
                ):
                    raise ValueError("durable Fact correction proposal does not match retry source")

            latest = self._ledger.project()
            cursor = ProjectionCursor(
                world_revision=latest.world_revision,
                deliberation_revision=latest.deliberation_revision,
                ledger_sequence=latest.ledger_sequence,
            )
            acceptance_payload = {
                "acceptance_id": mutation.acceptance_id,
                "status": "accepted",
                "proposal_id": mutation.proposal_id,
                "evaluated_world_revision": mutation.evaluated_world_revision,
                "accepted_change_id": mutation.change_id,
                "accepted_change_hash": mutation.accepted_change_hash,
            }
            acceptance = self._event(
                event_id=f"event:fact-correction:acceptance:{identity}",
                event_type="AcceptanceRecorded",
                payload=acceptance_payload,
                logical_time=logical_time,
                created_at=created_at,
                causation_id=f"event:fact-correction:proposal:{identity}",
                trace_id=observation.trace_id,
                correlation_id=observation.correlation_id,
            )
            corrected_event = self._event(
                event_id=mutation.fact_after.origin.accepted_event_ref,
                event_type={
                    "correct": "FactCorrected",
                    "withdraw": "FactWithdrawn",
                }[operation],
                payload=mutation.model_dump(mode="json"),
                logical_time=logical_time,
                created_at=created_at,
                causation_id=acceptance.event_id,
                trace_id=observation.trace_id,
                correlation_id=observation.correlation_id,
            )
            self._ledger.commit_at_cursor(
                (acceptance, corrected_event),
                expected_cursor=cursor,
            )
            return mutation.fact_after

    def _mutation(
        self,
        *,
        before: FactProjection,
        intent: FactCommitIntentV2,
        observation: Observation,
        observation_event: WorldEvent,
        observation_world_revision: int,
        evaluated_world_revision: int,
        logical_time: datetime,
        identity: str,
        operation: str,
    ) -> FactChangedPayload:
        old_values_hash = hashlib.sha256(
            _canonical(before.values.model_dump(mode="json")).encode()
        ).hexdigest()
        evidence = (
            *before.values.source_evidence_refs,
            EvidenceRef(
                ref_id=before.origin.accepted_event_ref,
                evidence_type="committed_fact",
                claim_purpose="current_fact",
                source_world_revision=next(
                    item.world_revision
                    for item in self._ledger.project().committed_world_event_refs
                    if item.event_id == before.origin.accepted_event_ref
                ),
                immutable_hash=old_values_hash,
            ),
            EvidenceRef(
                ref_id=observation.observation_id,
                evidence_type="observed_message",
                claim_purpose="current_fact",
                source_world_revision=observation_world_revision,
                immutable_hash=observation_event.payload_hash,
            ),
        )
        privacy_rank = {
            "public": 0,
            "shareable": 1,
            "personal": 2,
            "private": 3,
            "withhold": 4,
        }
        privacy = max(
            (before.values.privacy_class, intent.privacy_class),
            key=privacy_rank.__getitem__,
        )
        if operation == "correct":
            values = FactValues(
                subject_ref=before.values.subject_ref,
                predicate_code=before.values.predicate_code,
                cardinality=before.values.cardinality,
                conflict_key=before.values.conflict_key,
                value_ref=intent.value_ref,
                value_hash=intent.value_hash.removeprefix("sha256:"),
                assertion_binding=FactAssertionBinding(
                    source_kind="observed_message",
                    source_ref=observation.observation_id,
                    asserted_subject_ref=observation.actor,
                    actor_ref=observation.actor,
                    channel=observation.channel,
                    payload_ref=observation.payload_ref,
                    content_payload_hash=observation.payload_hash,
                ),
                anchor_evidence_refs=before.values.anchor_evidence_refs,
                source_evidence_refs=evidence,
                confidence_bp=intent.confidence_bp,
                privacy_class=privacy,
            )
        else:
            values = before.values.model_copy(
                update={
                    "source_evidence_refs": evidence,
                    "privacy_class": privacy,
                    "status": "withdrawn",
                    "withdrawal_reason_code": "source_retracted",
                    "withdrawal_evidence_ref": observation.observation_id,
                }
            )
        transition_id = f"transition:fact-correction:{identity}"
        event_id = f"event:fact-corrected:{identity}"
        origin = FactOrigin(
            change_id=f"change:fact-correction:{identity}",
            transition_id=transition_id,
            policy_refs=before.origin.policy_refs,
            accepted_event_ref=event_id,
        )
        after = FactProjection(
            fact_id=before.fact_id,
            entity_revision=before.entity_revision + 1,
            semantic_fingerprint=fact_semantic_fingerprint(
                subject_ref=values.subject_ref,
                predicate_code=values.predicate_code,
                cardinality=values.cardinality,
                conflict_key=values.conflict_key,
                value_hash=values.value_hash,
                assertion_binding=values.assertion_binding,
                anchor_evidence_refs=values.anchor_evidence_refs,
                policy_refs=origin.policy_refs,
            ),
            values=values,
            origin=origin,
            committed_at=before.committed_at,
            updated_at=logical_time,
        )
        raw = {
            "change_id": origin.change_id,
            "transition_id": transition_id,
            "expected_entity_revision": before.entity_revision,
            "evidence_refs": evidence,
            "policy_refs": origin.policy_refs,
            "acceptance_id": f"acceptance:fact-correction:{identity}",
            "proposal_id": f"proposal:fact-correction:{identity}",
            "evaluated_world_revision": evaluated_world_revision,
            "accepted_change_hash": "0" * 64,
            "operation": operation,
            "fact_before": before,
            "fact_after": after,
            "compensates_transition_id": None,
        }
        raw["accepted_change_hash"] = fact_mutation_hash(raw)
        return FactChangedPayload.model_validate(raw)

    def _event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        logical_time: datetime,
        created_at: datetime,
        causation_id: str,
        trace_id: str,
        correlation_id: str,
    ) -> WorldEvent:
        identity = domain_idempotency_key(
            event_type=event_type,
            world_id=self._ledger.world_id,
            payload=payload,
        )
        if identity is None:
            raise ValueError(f"Fact correction has no event identity for {event_type}")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=logical_time,
            created_at=created_at,
            actor=self._actor,
            source=self._source,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=identity,
            payload=payload,
        )


__all__ = ["FactCorrectionLifecycle"]
