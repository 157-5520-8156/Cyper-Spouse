"""Production authoring seam for sidecar-backed outcome candidates.

An occurrence's candidate matrix is world authority, while its readable prose
lives in the immutable sidecar.  This coordinator is the only production
writer that joins them: it persists complete bytes first and then appends one
``WorldOccurrenceCommitted`` event whose embedded candidate descriptors bind
the exact refs and hashes.  A failed ledger CAS can therefore leave only an
unreferenced sidecar orphan; it can never leave ledger-visible candidate prose
without immutable bytes.

The ledger event is intentionally a single event, rather than a second
descriptor event.  ``candidate_outcomes`` is already the frozen, revision-one
descriptor projection of a WorldOccurrence, so keeping it in the occurrence
commit makes the candidate matrix atomic and replay-stable.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from pydantic import Field, model_validator

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .life_content_store import (
    ImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from .schema_core import FrozenModel, PrivacyClass
from .schemas import (
    CommitResult,
    EvidenceRef,
    OutcomeCandidateDescriptor,
    ProjectionCursor,
    WorldEvent,
    WorldOccurrenceProjection,
)


_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}


class OutcomeCandidateContent(FrozenModel):
    """Complete, immutable authoring input for one frozen outcome candidate."""

    candidate_result_ref: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    result_payload_ref: str = Field(min_length=1)
    result_payload_hash: str = Field(min_length=1)
    privacy_class: PrivacyClass
    content_ref: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=12_000)

    @property
    def content_payload_hash(self) -> str:
        return life_content_payload_hash(self.text)

    def descriptor(self) -> OutcomeCandidateDescriptor:
        return OutcomeCandidateDescriptor(
            candidate_result_ref=self.candidate_result_ref,
            result_id=self.result_id,
            result_payload_ref=self.result_payload_ref,
            result_payload_hash=self.result_payload_hash,
            privacy_class=self.privacy_class,
            content_ref=self.content_ref,
            content_payload_hash=self.content_payload_hash,
        )

    def sidecar_record(self) -> StoredLifeContent:
        return StoredLifeContent(
            content_ref=self.content_ref,
            content_kind="outcome_candidate",
            content_payload_hash=self.content_payload_hash,
            text=self.text,
        )


class OccurrenceContentCommitRequest(FrozenModel):
    """The full authority image for one production occurrence commit.

    The supplied occurrence must be the pre-materialization image: callers
    declare only candidate refs, never hand-author ``candidate_outcomes`` or a
    sidecar hash.  That prevents a production writer from silently committing
    semantic candidates whose text was never installed in the sidecar.
    """

    world_id: str = Field(min_length=1)
    occurrence: WorldOccurrenceProjection
    candidate_contents: tuple[OutcomeCandidateContent, ...] = Field(min_length=1)
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    policy_refs: tuple[str, ...] = ()
    logical_time: datetime
    created_at: datetime
    actor: str = Field(min_length=1)
    source: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    causation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    schema_version: str = Field(default="world-v2.1", min_length=1)

    @model_validator(mode="after")
    def is_a_complete_unmaterialized_occurrence(self) -> OccurrenceContentCommitRequest:
        if self.occurrence.status != "committed" or self.occurrence.entity_revision != 1:
            raise ValueError("production occurrence content requires a new committed occurrence")
        if self.occurrence.candidate_outcomes:
            raise ValueError("production occurrence content must materialize candidate descriptors itself")
        if self.occurrence.candidate_outcome_refs != tuple(
            item.candidate_result_ref for item in self.candidate_contents
        ):
            raise ValueError("candidate content refs must exactly match occurrence candidate refs")
        if len({item.content_ref for item in self.candidate_contents}) != len(self.candidate_contents):
            raise ValueError("candidate content refs must be unique")
        if len({item.candidate_result_ref for item in self.candidate_contents}) != len(
            self.candidate_contents
        ):
            raise ValueError("candidate result refs must be unique")
        if len({item.result_id for item in self.candidate_contents}) != len(self.candidate_contents):
            raise ValueError("candidate result ids must be unique")
        if any(
            _PRIVACY_RANK[item.privacy_class] < _PRIVACY_RANK[self.occurrence.visibility]
            for item in self.candidate_contents
        ):
            raise ValueError("candidate content cannot weaken occurrence privacy")
        if self.logical_time.tzinfo is None or self.logical_time.utcoffset() is None:
            raise ValueError("occurrence content logical time must be timezone-aware")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("occurrence content created time must be timezone-aware")
        # ``model_copy(update=...)`` intentionally does not re-run Pydantic
        # validators.  Validate the exact projection that will enter the
        # ledger before any sidecar write, so malformed authoring input cannot
        # manufacture unbounded orphan records.
        WorldOccurrenceProjection.model_validate(
            self.occurrence.model_copy(
                update={
                    "candidate_outcomes": tuple(
                        item.descriptor() for item in self.candidate_contents
                    )
                }
            ).model_dump()
        )
        return self


class OccurrenceContentCoordinator:
    """CAS-safe writer for sidecar-backed WorldOccurrence candidates."""

    def __init__(self, *, ledger: LedgerPort, store: ImmutableLifeContentStore) -> None:
        if store is None:
            raise ValueError("occurrence content coordinator requires an immutable sidecar store")
        self._ledger = ledger
        self._store = store

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    def commit(self, request: OccurrenceContentCommitRequest) -> CommitResult:
        """Install bytes then atomically append the matching occurrence authority.

        ``put_if_absent`` is deliberately before the ledger write.  It is
        idempotent for byte-identical retries and makes a failed CAS harmless:
        the only possible residue is an orphan record, which no reader can
        discover without the failed occurrence descriptor.
        """

        if request.world_id != self._ledger.world_id:
            raise ValueError("occurrence content request belongs to another world")
        for candidate in request.candidate_contents:
            self._store.put_if_absent(candidate.sidecar_record())

        occurrence = request.occurrence.model_copy(
            update={
                "candidate_outcomes": tuple(
                    candidate.descriptor() for candidate in request.candidate_contents
                )
            }
        )
        payload = {
            "change_id": request.change_id,
            "transition_id": request.transition_id,
            "expected_entity_revision": 0,
            "evidence_refs": [item.model_dump(mode="json") for item in request.evidence_refs],
            "policy_refs": list(request.policy_refs),
            "occurrence": occurrence.model_dump(mode="json"),
        }
        event_id = _occurrence_commit_event_id(world_id=request.world_id, payload=payload)
        event = WorldEvent.from_payload(
            schema_version=request.schema_version,
            event_id=event_id,
            world_id=request.world_id,
            event_type="WorldOccurrenceCommitted",
            logical_time=request.logical_time,
            created_at=request.created_at,
            actor=request.actor,
            source=request.source,
            trace_id=request.trace_id,
            causation_id=request.causation_id,
            correlation_id=request.correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="WorldOccurrenceCommitted",
                    world_id=request.world_id,
                    payload=payload,
                )
                or f"occurrence-content:{request.occurrence.occurrence_id}:{request.transition_id}"
            ),
            payload=payload,
        )
        projection = self._ledger.project()
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        return self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=cursor,
            commit_id=f"occurrence-content:{_stable_digest(request.world_id, payload)}",
        )


def _occurrence_commit_event_id(*, world_id: str, payload: dict[str, object]) -> str:
    return f"event:occurrence-content:{_stable_digest(world_id, payload)}"


def _stable_digest(world_id: str, payload: dict[str, object]) -> str:
    encoded = json.dumps(
        {"world_id": world_id, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "OccurrenceContentCommitRequest",
    "OccurrenceContentCoordinator",
    "OutcomeCandidateContent",
]
