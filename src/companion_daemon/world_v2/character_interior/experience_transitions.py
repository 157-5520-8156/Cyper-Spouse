"""Source-closed long-horizon choices authored inside one Interior experience.

This module is deliberately a capability boundary, not a behaviour policy.
It tells the character which already-authoritative Goal/Thread/Commitment heads
and which retention-eligible source are available at the pinned cursor.  The
character may choose one transition or none.  It may not invent an entity,
revision, evidence ref, or content authority.

The resulting :class:`TypedChange` is still inert by itself.  The settlement
object at the bottom of this module is the sole adapter that can compile that
exact authored choice into the already-installed typed domain authorities.
It records a separate proposal and only then accepts the mutation under CAS.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..commitment_events import CommitmentChangedPayload, commitment_mutation_hash
from ..errors import ConcurrencyConflict
from ..event_identity import domain_idempotency_key
from ..memory_events import (
    MemoryCandidateChangedPayload,
    memory_candidate_mutation_hash,
    memory_source_evidence,
)
from ..memory_reducers import MEMORY_POLICY_REFS
from ..proposal_envelope import (
    CanonicalTypedPayload,
    CommitmentPayload,
    DecisionProposal,
    MemoryCandidatePayload,
    ProposalEvidenceRef,
    ThreadPayload,
    TypedChange,
    validate_proposal_envelope,
)
from ..schema_core import EvidenceRef, FrozenModel, PrivacyClass, canonicalize_json_value
from ..schemas import (
    CommitmentFulfillmentContract,
    CommitmentOrigin,
    CommitmentProjection,
    CommitmentProposedMutation,
    CommitmentProposalProjection,
    CommitmentValues,
    DueWindow,
    MEMORY_SALIENCE_MATRIX_DIGEST,
    MemoryCandidateOrigin,
    MemoryCandidateProjection,
    MemoryCandidateProposedMutation,
    MemoryCandidateProposalProjection,
    MemoryCandidateValues,
    MemorySalienceVector,
    MemorySourceBinding,
    ProjectionCursor,
    ThreadOrigin,
    ThreadProjection,
    ThreadProposedMutation,
    ThreadProposalProjection,
    ThreadValues,
    WorldEvent,
    commitment_semantic_fingerprint,
    memory_candidate_semantic_fingerprint,
    memory_source_authority_id,
    memory_source_cluster_fingerprint,
    thread_semantic_fingerprint,
)
from ..thread_events import ThreadChangedPayload, thread_mutation_hash


_GOAL_REASON_KINDS = (
    "priority_shift",
    "resource_constraint",
    "uncertainty",
    "relationship_consideration",
    "priority_restored",
    "constraint_resolved",
    "renewed_intent",
    "no_longer_desired",
    "superseded",
    "infeasible",
    "values_changed",
    "context_changed",
)
_MEMORY_RATIONALES = (
    "identity_relevance",
    "relationship_continuity",
    "boundary_relevance",
    "unfinished_business",
    "repeated_pattern",
    "future_utility",
    "emotional_salience",
    "world_continuity",
)
EXPERIENCE_SETTLEMENT_POLICY_REF = "policy:character-interior-experience-settlement.1"


def _canonical(value: object) -> str:
    return json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _aware(value: datetime | None, *, field: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _datetime_wire(value: object) -> object:
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


class _ExperienceDraftBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source_refs: list[str] = Field(min_length=1, max_length=16)
    reason_summary: str = Field(min_length=1, max_length=480)

    @model_validator(mode="after")
    def sources_are_unique(self) -> "_ExperienceDraftBase":
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("experience transition source refs must be unique")
        return self


class GoalExperienceTransitionDraft(_ExperienceDraftBase):
    """A choice over an already-authoritative Goal head.

    Goal creation is intentionally absent.  The current Goal authority has no
    content-addressed outcome author, so accepting free text here would create
    an opaque second truth source.
    """

    domain: Literal["goal"]
    operation: Literal["pause", "resume", "abandon"]
    target_id: str = Field(min_length=1, max_length=512)
    expected_entity_revision: int = Field(ge=1)
    reason_kind: Literal[*_GOAL_REASON_KINDS]


class ThreadExperienceTransitionDraft(_ExperienceDraftBase):
    domain: Literal["thread"]
    operation: Literal["open", "update", "resolve", "cancel"]
    target_id: str | None = Field(default=None, min_length=1, max_length=512)
    expected_entity_revision: int = Field(ge=0)
    thread_kind: (
        Literal[
            "question_pending",
            "topic_open",
            "repair_open",
            "external_result_pending",
            "coordination_pending",
            "reply_reconsideration",
        ]
        | None
    ) = None
    importance_bp: int | None = Field(default=None, ge=0, le=10_000)
    due_at: datetime | None = None
    expires_at: datetime | None = None
    resolution_kind: Literal["answered", "skipped"] | None = None
    cancellation_reason_code: (
        Literal["user_withdrew", "obsolete", "invalid", "duplicate"] | None
    ) = None

    @field_validator("due_at", "expires_at", mode="before")
    @classmethod
    def parse_datetime_wire(cls, value: object) -> object:
        return _datetime_wire(value)

    @model_validator(mode="after")
    def operation_shape_is_closed(self) -> "ThreadExperienceTransitionDraft":
        _aware(self.due_at, field="thread due_at")
        _aware(self.expires_at, field="thread expires_at")
        if self.due_at is not None and self.expires_at is not None:
            if self.expires_at < self.due_at:
                raise ValueError("thread expiry cannot precede due time")
        if self.operation == "open":
            if (
                self.target_id is not None
                or self.expected_entity_revision != 0
                or self.thread_kind is None
                or self.importance_bp is None
                or self.resolution_kind is not None
                or self.cancellation_reason_code is not None
            ):
                raise ValueError("thread open needs new-head fields only")
        elif self.operation == "update":
            if (
                self.target_id is None
                or self.expected_entity_revision < 1
                or self.thread_kind is not None
                or self.importance_bp is None
                or self.resolution_kind is not None
                or self.cancellation_reason_code is not None
            ):
                raise ValueError("thread update needs one offered head and revised timing")
        elif self.operation == "resolve":
            if (
                self.target_id is None
                or self.expected_entity_revision < 1
                or self.thread_kind is not None
                or self.importance_bp is not None
                or self.due_at is not None
                or self.expires_at is not None
                or self.resolution_kind is None
                or self.cancellation_reason_code is not None
            ):
                raise ValueError("thread resolve needs one offered head and resolution kind")
        elif (
            self.target_id is None
            or self.expected_entity_revision < 1
            or self.thread_kind is not None
            or self.importance_bp is not None
            or self.due_at is not None
            or self.expires_at is not None
            or self.resolution_kind is not None
            or self.cancellation_reason_code is None
        ):
            raise ValueError("thread cancel needs one offered head and cancellation reason")
        return self


class CommitmentExperienceTransitionDraft(_ExperienceDraftBase):
    domain: Literal["commitment"]
    operation: Literal["open", "release"]
    target_id: str | None = Field(default=None, min_length=1, max_length=512)
    expected_entity_revision: int = Field(ge=0)
    thread_id: str | None = Field(default=None, min_length=1, max_length=512)
    importance_bp: int | None = Field(default=None, ge=0, le=10_000)
    due_at: datetime | None = None
    persistence: Literal["session", "durable"] | None = None
    release_reason_code: (
        Literal[
            "user_withdrew",
            "obsolete",
            "precondition_failed",
            "boundary_or_safety_conflict",
            "operator_correction",
        ]
        | None
    ) = None

    @field_validator("due_at", mode="before")
    @classmethod
    def parse_datetime_wire(cls, value: object) -> object:
        return _datetime_wire(value)

    @model_validator(mode="after")
    def operation_shape_is_closed(self) -> "CommitmentExperienceTransitionDraft":
        _aware(self.due_at, field="commitment due_at")
        if self.operation == "open":
            if (
                self.target_id is not None
                or self.expected_entity_revision != 0
                or self.thread_id is None
                or self.importance_bp is None
                or self.due_at is None
                or self.persistence is None
                or self.release_reason_code is not None
            ):
                raise ValueError("commitment open needs one offered Thread contract")
        elif (
            self.target_id is None
            or self.expected_entity_revision < 1
            or self.thread_id is not None
            or self.importance_bp is not None
            or self.due_at is not None
            or self.persistence is not None
            or self.release_reason_code is None
        ):
            raise ValueError("commitment release needs one offered head and reason")
        return self


class MemorySalienceDraft(BaseModel):
    """Character-authored salience; policy derives retrieval strength from it."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    autobiographical_relevance_bp: int = Field(ge=0, le=10_000)
    relationship_relevance_bp: int = Field(ge=0, le=10_000)
    emotional_residue_bp: int = Field(ge=0, le=10_000)
    unfinished_business_bp: int = Field(ge=0, le=10_000)
    recurrence_bp: int = Field(ge=0, le=10_000)
    novelty_bp: int = Field(ge=0, le=10_000)
    future_utility_bp: int = Field(ge=0, le=10_000)
    world_continuity_bp: int = Field(ge=0, le=10_000)

    @property
    def retrieval_strength_bp(self) -> int:
        weighted = (
            self.autobiographical_relevance_bp * 15
            + self.relationship_relevance_bp * 15
            + self.emotional_residue_bp * 10
            + self.unfinished_business_bp * 15
            + self.recurrence_bp * 15
            + self.novelty_bp * 5
            + self.future_utility_bp * 15
            + self.world_continuity_bp * 10
        )
        return weighted // 100


class MemoryCandidateExperienceTransitionDraft(_ExperienceDraftBase):
    domain: Literal["memory_candidate"]
    operation: Literal["retain"]
    source_token: str = Field(min_length=1, max_length=512)
    cue_kind: Literal[
        "identity",
        "relationship",
        "boundary",
        "unfinished_business",
        "repeated_pattern",
        "future_utility",
        "emotional_residue",
        "world_continuity",
    ]
    retention_rationales: list[Literal[*_MEMORY_RATIONALES]] = Field(
        min_length=1,
        max_length=8,
    )
    salience: MemorySalienceDraft

    @model_validator(mode="after")
    def retention_reasons_are_unique(self) -> "MemoryCandidateExperienceTransitionDraft":
        if len(self.retention_rationales) != len(set(self.retention_rationales)):
            raise ValueError("memory retention rationales must be unique")
        return self


ExperienceTransitionDraft = Annotated[
    GoalExperienceTransitionDraft
    | ThreadExperienceTransitionDraft
    | CommitmentExperienceTransitionDraft
    | MemoryCandidateExperienceTransitionDraft,
    Field(discriminator="domain"),
]


class _GoalHeadCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    target_id: str
    entity_revision: int = Field(ge=1)
    status: Literal["active", "paused", "blocked"]
    authority_source_ref: str
    allowed_operations: tuple[Literal["pause", "resume", "abandon"], ...]


class _ThreadHeadCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    target_id: str
    entity_revision: int = Field(ge=1)
    authority_source_ref: str
    kind: str
    importance_bp: int = Field(ge=0, le=10_000)
    due_at: datetime | None
    expires_at: datetime | None
    allowed_operations: tuple[Literal["update", "resolve", "cancel"], ...]


class _CommitmentHeadCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    target_id: str
    entity_revision: int = Field(ge=1)
    authority_source_ref: str
    importance_bp: int = Field(ge=0, le=10_000)
    due_at: datetime
    persistence: Literal["session", "durable"]
    allowed_operations: tuple[Literal["release"], ...] = ("release",)


class _CommitmentThreadCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    thread_id: str
    entity_revision: int = Field(ge=1)
    authority_source_ref: str
    resolution_contract_ref: str
    privacy_class: PrivacyClass


class _MemorySourceCapability(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source_token: str
    source_kind: Literal["fact", "experience", "terminal_thread"]
    source_id: str
    source_entity_revision: int = Field(ge=1)
    authority_event_ref: str
    authority_world_revision: int = Field(ge=1)
    authority_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_values_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    privacy_ceiling: PrivacyClass


class ExperienceTransitionCapability(BaseModel):
    """Typed capability offered to one exact Interior stimulus turn."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    contract: Literal["character-interior-experience-transitions.1"] = (
        "character-interior-experience-transitions.1"
    )
    current_source_ref: str
    goal_open_available: Literal[False] = False
    goal_open_unavailable_reason: Literal["goal_content_authority_unavailable"] = (
        "goal_content_authority_unavailable"
    )
    goal_heads: tuple[_GoalHeadCapability, ...] = ()
    thread_open_available: bool
    thread_heads: tuple[_ThreadHeadCapability, ...] = ()
    commitment_open_threads: tuple[_CommitmentThreadCapability, ...] = ()
    commitment_heads: tuple[_CommitmentHeadCapability, ...] = ()
    memory_sources: tuple[_MemorySourceCapability, ...] = ()

    def model_view(self) -> dict[str, object]:
        """Compact model view: identities/lifecycle only, no proof hashes."""

        value = self.model_dump(mode="json")
        for source in value["memory_sources"]:
            source.pop("authority_payload_hash", None)
            source.pop("source_values_hash", None)
        return value


def _committed_authority(projection: object, event_ref: str) -> object | None:
    return next(
        (
            item
            for item in getattr(projection, "committed_world_event_refs", ())
            if item.event_id == event_ref
        ),
        None,
    )


def _memory_source(
    *,
    projection: object,
    source_event: object,
) -> _MemorySourceCapability | None:
    event_ref = source_event.event_id
    stimulus_authority = _committed_authority(projection, event_ref)
    if stimulus_authority is None:
        return None

    source_kind: Literal["fact", "experience", "terminal_thread"]
    source: object | None
    source = next(
        (
            item
            for item in getattr(projection, "facts", ())
            if item.origin.accepted_event_ref == event_ref and item.values.status == "active"
        ),
        None,
    )
    if source is not None:
        source_kind = "fact"
        source_id = source.fact_id
        privacy = source.values.privacy_class
        source_authority_ref = source.origin.accepted_event_ref
    else:
        source = next(
            (
                item
                for item in getattr(projection, "experiences", ())
                if item.origin.accepted_event_ref == event_ref
                or any(
                    getattr(binding, "authority_event_ref", None) == event_ref
                    or getattr(binding, "receipt_id", None) == event_ref
                    for binding in item.values.source_bindings
                )
            ),
            None,
        )
        if source is not None:
            source_kind = "experience"
            source_id = source.experience_id
            privacy = source.values.privacy_class
            source_authority_ref = source.origin.accepted_event_ref
        else:
            transition = next(
                (
                    item
                    for item in getattr(projection, "thread_transitions", ())
                    if item.accepted_event_ref == event_ref
                    and item.values_after.status in {"resolved", "cancelled", "expired"}
                ),
                None,
            )
            if transition is None:
                return None
            source_kind = "terminal_thread"
            source_id = transition.thread_id
            source = transition.values_after
            privacy = source.privacy_class
            source_authority_ref = transition.accepted_event_ref

    authority = _committed_authority(projection, source_authority_ref)
    if authority is None:
        return None

    entity_revision = (
        source.entity_revision if hasattr(source, "entity_revision") else transition.entity_revision
    )
    values_hash = (
        source.semantic_fingerprint
        if hasattr(source, "semantic_fingerprint")
        else _digest(source.model_dump(mode="json"))
    )
    material = {
        "source_kind": source_kind,
        "source_id": source_id,
        "source_entity_revision": entity_revision,
        "authority_event_ref": source_authority_ref,
        "authority_world_revision": authority.world_revision,
        "authority_payload_hash": authority.payload_hash,
        "source_values_hash": values_hash,
        "privacy_ceiling": privacy,
    }
    binding = MemorySourceBinding(
        source_kind=source_kind,
        source_id=source_id,
        source_entity_revision=entity_revision,
        authority_event_ref=source_authority_ref,
        authority_world_revision=authority.world_revision,
        authority_payload_hash=authority.payload_hash,
        source_values_hash=values_hash,
    )
    authority_id = memory_source_authority_id(binding)
    if any(
        authority_id in item.values.consumed_source_authority_ids
        for item in getattr(projection, "memory_candidates", ())
    ):
        return None
    return _MemorySourceCapability(
        source_token="memory-source:" + _digest(material),
        **material,
    )


def build_experience_transition_capability(
    *,
    projection: object,
    actor_ref: str,
    source_event: object,
) -> ExperienceTransitionCapability:
    """Offer only heads and sources proven at ``projection``'s cursor."""

    source_ref = source_event.event_id
    if _committed_authority(projection, source_ref) is None:
        raise ValueError("experience transition source is not committed at this cursor")

    goals: list[_GoalHeadCapability] = []
    for item in getattr(projection, "goals", ()):
        if item.actor_ref != actor_ref or item.values.status not in {"active", "paused", "blocked"}:
            continue
        if _committed_authority(projection, item.origin.accepted_event_ref) is None:
            continue
        allowed: tuple[Literal["pause", "resume", "abandon"], ...]
        if item.values.status == "active":
            allowed = ("pause", "abandon")
        elif item.values.status == "paused":
            allowed = ("resume", "abandon")
        else:
            allowed = ("abandon",)
        goals.append(
            _GoalHeadCapability(
                target_id=item.goal_id,
                entity_revision=item.entity_revision,
                status=item.values.status,
                authority_source_ref=item.origin.accepted_event_ref,
                allowed_operations=allowed,
            )
        )

    threads: list[_ThreadHeadCapability] = []
    commitment_threads: list[_CommitmentThreadCapability] = []
    for item in getattr(projection, "threads", ()):
        if item.values.status != "open":
            continue
        if _committed_authority(projection, item.origin.accepted_event_ref) is None:
            continue
        due_at = item.values.due_window.closes_at if item.values.due_window is not None else None
        threads.append(
            _ThreadHeadCapability(
                target_id=item.thread_id,
                entity_revision=item.entity_revision,
                authority_source_ref=item.origin.accepted_event_ref,
                kind=item.values.kind,
                importance_bp=item.values.importance_bp,
                due_at=due_at,
                expires_at=item.values.expires_at,
                allowed_operations=("update", "resolve", "cancel"),
            )
        )
        commitment_threads.append(
            _CommitmentThreadCapability(
                thread_id=item.thread_id,
                entity_revision=item.entity_revision,
                authority_source_ref=item.origin.accepted_event_ref,
                resolution_contract_ref=item.values.resolution_contract_ref,
                privacy_class=item.values.privacy_class,
            )
        )

    commitments: list[_CommitmentHeadCapability] = []
    for item in getattr(projection, "commitments", ()):
        if item.values.owner_ref != actor_ref or item.values.status not in {"open", "due"}:
            continue
        if _committed_authority(projection, item.origin.accepted_event_ref) is None:
            continue
        commitments.append(
            _CommitmentHeadCapability(
                target_id=item.commitment_id,
                entity_revision=item.entity_revision,
                authority_source_ref=item.origin.accepted_event_ref,
                importance_bp=item.values.importance_bp,
                due_at=item.values.due_window.closes_at,
                persistence=item.values.persistence_level,
            )
        )

    memory = _memory_source(projection=projection, source_event=source_event)
    return ExperienceTransitionCapability(
        current_source_ref=source_ref,
        thread_open_available=True,
        goal_heads=tuple(sorted(goals, key=lambda item: item.target_id)[:16]),
        thread_heads=tuple(sorted(threads, key=lambda item: item.target_id)[:16]),
        commitment_open_threads=tuple(
            sorted(commitment_threads, key=lambda item: item.thread_id)[:16]
        ),
        commitment_heads=tuple(sorted(commitments, key=lambda item: item.target_id)[:16]),
        memory_sources=(() if memory is None else (memory,)),
    )


def validate_experience_transition_draft(
    draft: ExperienceTransitionDraft,
    *,
    capability: ExperienceTransitionCapability,
) -> None:
    """Reject stale, invented, or cross-capability material before any write."""

    selected = set(draft.source_refs)
    if capability.current_source_ref not in selected:
        raise ValueError("experience transition omitted its current stimulus authority")

    if isinstance(draft, GoalExperienceTransitionDraft):
        head = next(
            (item for item in capability.goal_heads if item.target_id == draft.target_id),
            None,
        )
        if (
            head is None
            or draft.expected_entity_revision != head.entity_revision
            or draft.operation not in head.allowed_operations
            or selected != {capability.current_source_ref, head.authority_source_ref}
        ):
            raise ValueError("goal transition is outside the offered head capability")
        return

    if isinstance(draft, ThreadExperienceTransitionDraft):
        if draft.operation == "open":
            if not capability.thread_open_available or selected != {capability.current_source_ref}:
                raise ValueError("thread open is outside the offered source capability")
            return
        head = next(
            (item for item in capability.thread_heads if item.target_id == draft.target_id),
            None,
        )
        if (
            head is None
            or draft.expected_entity_revision != head.entity_revision
            or draft.operation not in head.allowed_operations
            or selected != {capability.current_source_ref, head.authority_source_ref}
        ):
            raise ValueError("thread transition is outside the offered head capability")
        return

    if isinstance(draft, CommitmentExperienceTransitionDraft):
        if draft.operation == "open":
            thread = next(
                (
                    item
                    for item in capability.commitment_open_threads
                    if item.thread_id == draft.thread_id
                ),
                None,
            )
            if thread is None or selected != {
                capability.current_source_ref,
                thread.authority_source_ref,
            }:
                raise ValueError("commitment open lacks an offered Thread contract")
            return
        head = next(
            (item for item in capability.commitment_heads if item.target_id == draft.target_id),
            None,
        )
        if (
            head is None
            or draft.expected_entity_revision != head.entity_revision
            or selected != {capability.current_source_ref, head.authority_source_ref}
        ):
            raise ValueError("commitment release is outside the offered head capability")
        return

    source = next(
        (item for item in capability.memory_sources if item.source_token == draft.source_token),
        None,
    )
    if source is None or selected != {
        capability.current_source_ref,
        source.authority_event_ref,
    }:
        raise ValueError("memory retention source is not an offered authority")


def _binding(*, object_ref: str, schema: str, material: object) -> dict[str, object]:
    return {
        "object_ref": object_ref,
        "schema_version": schema,
        "payload_hash": "sha256:" + _digest(material),
    }


def materialize_experience_transition_change(
    draft: ExperienceTransitionDraft,
    *,
    capability: ExperienceTransitionCapability,
    projection: object,
    identity: str,
) -> TypedChange:
    """Materialize one validated choice into the existing typed envelope."""

    validate_experience_transition_draft(draft, capability=capability)
    change_id = f"change:character-interior:experience:{draft.domain}:{identity}"
    if isinstance(draft, GoalExperienceTransitionDraft):
        current = next(item for item in projection.goals if item.goal_id == draft.target_id)
        contract = current.values.completion_contract
        payload = {
            "before_image": _binding(
                object_ref=current.goal_id,
                schema="v2-goal-head.1",
                material=current.model_dump(mode="json"),
            ),
            "after_image": _binding(
                object_ref=current.goal_id,
                schema="v2-goal-intention-draft.1",
                material={
                    "head": current.semantic_fingerprint,
                    "operation": draft.operation,
                    "reason_kind": draft.reason_kind,
                    "reason_summary": draft.reason_summary,
                },
            ),
            "goal_id": current.goal_id,
            "outcome_ref": current.values.outcome_ref,
            "importance": current.values.importance_bp,
            "progress": current.values.progress_bp,
            "due": (
                current.values.due_window.ends_at.isoformat()
                if current.values.due_window is not None
                else None
            ),
            "blockers": [item.blocker_id for item in current.values.blockers],
            "completion_contract": _binding(
                object_ref=(
                    contract.contract_id
                    if contract is not None
                    else "goal-completion-contract:unavailable:" + current.goal_id
                ),
                schema="v2-goal-completion-contract.1",
                material=(
                    contract.model_dump(mode="json")
                    if contract is not None
                    else {"available": False}
                ),
            ),
        }
        return TypedChange(
            change_id=change_id,
            kind="v2_goal_transition",
            target_id=current.goal_id,
            transition=draft.operation,
            expected_entity_revision=draft.expected_entity_revision,
            evidence_refs=tuple(draft.source_refs),
            policy_refs=(EXPERIENCE_SETTLEMENT_POLICY_REF,),
            payload=CanonicalTypedPayload.from_value(
                payload_schema="v2_goal_transition.v1",
                value={
                    **payload,
                    "reason_kind": draft.reason_kind,
                    "reason_summary": draft.reason_summary,
                },
            ),
        )

    if isinstance(draft, ThreadExperienceTransitionDraft):
        current = (
            next(item for item in projection.threads if item.thread_id == draft.target_id)
            if draft.target_id is not None
            else None
        )
        target_id = (
            current.thread_id if current is not None else "thread:character-interior:" + identity
        )
        kind = current.values.kind if current is not None else draft.thread_kind
        importance = (
            draft.importance_bp if draft.importance_bp is not None else current.values.importance_bp
        )
        due = (
            draft.due_at
            if draft.due_at is not None
            else (
                current.values.due_window.closes_at
                if current is not None and current.values.due_window is not None
                else None
            )
        )
        resolution_ref = capability.current_source_ref if draft.operation == "resolve" else None
        return TypedChange(
            change_id=change_id,
            kind="thread_transition",
            target_id=target_id,
            transition=draft.operation,
            expected_entity_revision=draft.expected_entity_revision,
            evidence_refs=tuple(draft.source_refs),
            policy_refs=(EXPERIENCE_SETTLEMENT_POLICY_REF,),
            payload=CanonicalTypedPayload.from_value(
                payload_schema="thread_transition.v1",
                value={
                    "thread_id": target_id,
                    "thread_kind": kind,
                    "importance": importance,
                    "due": due.isoformat() if due is not None else None,
                    "resolution_ref": resolution_ref,
                    "expires_at": (
                        (
                            draft.expires_at
                            if draft.expires_at is not None
                            else (current.values.expires_at if current is not None else None)
                        ).isoformat()
                        if (
                            draft.expires_at is not None
                            or (current is not None and current.values.expires_at is not None)
                        )
                        else None
                    ),
                    "resolution_kind": draft.resolution_kind,
                    "cancellation_reason_code": draft.cancellation_reason_code,
                    "reason_summary": draft.reason_summary,
                },
            ),
        )

    if isinstance(draft, CommitmentExperienceTransitionDraft):
        current = (
            next(item for item in projection.commitments if item.commitment_id == draft.target_id)
            if draft.target_id is not None
            else None
        )
        target_id = (
            current.commitment_id
            if current is not None
            else "commitment:character-interior:" + identity
        )
        content_ref = current.values.content_ref if current is not None else draft.thread_id
        importance = current.values.importance_bp if current is not None else draft.importance_bp
        due = current.values.due_window.closes_at if current is not None else draft.due_at
        persistence = current.values.persistence_level if current is not None else draft.persistence
        return TypedChange(
            change_id=change_id,
            kind="commitment_transition",
            target_id=target_id,
            transition=draft.operation,
            expected_entity_revision=draft.expected_entity_revision,
            evidence_refs=tuple(draft.source_refs),
            policy_refs=(EXPERIENCE_SETTLEMENT_POLICY_REF,),
            payload=CanonicalTypedPayload.from_value(
                payload_schema="commitment_transition.v1",
                value={
                    "commitment_id": target_id,
                    "content_ref": content_ref,
                    "importance": importance,
                    "due": due.isoformat() if due is not None else None,
                    "persistence": persistence,
                    "release_reason_code": draft.release_reason_code,
                    "reason_summary": draft.reason_summary,
                },
            ),
        )

    source = next(
        item for item in capability.memory_sources if item.source_token == draft.source_token
    )
    target_id = "memory:character-interior:" + identity
    source_binding = {
        "ref_id": source.authority_event_ref,
        "source_world_revision": source.authority_world_revision,
        "immutable_hash": "sha256:" + source.authority_payload_hash,
    }
    return TypedChange(
        change_id=change_id,
        kind="memory_candidate_transition",
        target_id=target_id,
        transition="open",
        expected_entity_revision=0,
        evidence_refs=tuple(draft.source_refs),
        policy_refs=(EXPERIENCE_SETTLEMENT_POLICY_REF,),
        payload=CanonicalTypedPayload.from_value(
            payload_schema="memory_candidate_transition.v1",
            value={
                "before_image": None,
                "after_image": _binding(
                    object_ref=target_id,
                    schema="memory-candidate-draft.1",
                    material=draft.model_dump(mode="json"),
                ),
                "candidate_id": target_id,
                "source_refs": [source_binding],
                "retention_rationale": ";".join(draft.retention_rationales),
                "privacy_ceiling": source.privacy_ceiling,
                "retrieval_strength": draft.salience.retrieval_strength_bp,
                "source_descriptors": [
                    {
                        "source_kind": source.source_kind,
                        "source_id": source.source_id,
                        "source_entity_revision": source.source_entity_revision,
                        "authority_event_ref": source.authority_event_ref,
                        "authority_world_revision": source.authority_world_revision,
                        "authority_payload_hash": source.authority_payload_hash,
                        "source_values_hash": source.source_values_hash,
                    }
                ],
                "cue_kind": draft.cue_kind,
                "retention_rationales": list(draft.retention_rationales),
                "salience": draft.salience.model_dump(mode="json"),
                "reason_summary": draft.reason_summary,
            },
        ),
    )


_LIVE_EXPERIENCE_KINDS = frozenset(
    {
        "v2_goal_transition",
        "thread_transition",
        "commitment_transition",
        "memory_candidate_transition",
    }
)
_THREAD_POLICY_REFS = ("policy:thread-v1",)
_COMMITMENT_POLICY_REFS = ("policy:commitment-v1",)


def _projection_cursor(projection: object) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


class ExperienceTransitionSettlementResult(FrozenModel):
    status: Literal["no_change", "accepted"]
    source_proposal_id: str
    typed_proposal_id: str | None = None
    mutation_event_ref: str | None = None
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class _CompiledExperienceAuthority:
    proposal: object
    mutation: object
    mutation_event_type: str
    proposal_event_id: str
    acceptance_event_id: str
    mutation_event_id: str
    proposal_id: str
    acceptance_id: str


class ExperienceTransitionSettlement:
    """Settle one model-authored long-horizon choice through typed authority.

    This adapter never chooses an operation or fills missing character text.
    It only translates the complete, source-closed ``TypedChange`` into the
    existing Goal/Thread/Commitment/Memory proposal contracts.  Proposal and
    accepted mutation remain separate commits, and every identity is stable
    across restart.
    """

    def __init__(
        self,
        *,
        ledger: object,
        owner_id: str,
        companion_actor_ref: str,
        source: str = "world-v2:character-interior-experience-settlement",
    ) -> None:
        if not owner_id or not companion_actor_ref:
            raise ValueError("experience settlement composition is incomplete")
        self._ledger = ledger
        self._owner_id = owner_id
        self._companion_actor_ref = companion_actor_ref
        self._source = source

    @property
    def ledger(self) -> object:
        return self._ledger

    async def process(
        self,
        *,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> ExperienceTransitionSettlementResult:
        def run() -> ExperienceTransitionSettlementResult:
            return self._process(
                audit_cursor=audit_cursor,
                current_cursor=current_cursor,
                proposal_id=proposal_id,
                source_event=source_event,
            )

        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(run)
        return run()

    def is_pending(
        self,
        *,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> bool:
        del current_cursor
        audit, proposal, change = self._authored_change(
            audit_cursor=audit_cursor,
            proposal_id=proposal_id,
            source_event=source_event,
        )
        del audit, proposal
        if change is None:
            return False
        return not self._accepted_change(self._ledger.project(), change=change)

    def _process(
        self,
        *,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> ExperienceTransitionSettlementResult:
        audit, authored, change = self._authored_change(
            audit_cursor=audit_cursor,
            proposal_id=proposal_id,
            source_event=source_event,
        )
        if change is None:
            return ExperienceTransitionSettlementResult(
                status="no_change",
                source_proposal_id=proposal_id,
            )
        projected = self._ledger.project()
        if _projection_cursor(projected).ledger_sequence < current_cursor.ledger_sequence:
            raise ValueError("experience settlement current cursor is from the future")
        accepted = self._accepted_change(projected, change=change)
        if accepted is not None:
            return ExperienceTransitionSettlementResult(
                status="accepted",
                source_proposal_id=proposal_id,
                typed_proposal_id=accepted[0],
                mutation_event_ref=accepted[1],
                replayed=True,
            )

        for _attempt in range(8):
            projected = self._ledger.project()
            accepted = self._accepted_change(projected, change=change)
            if accepted is not None:
                return ExperienceTransitionSettlementResult(
                    status="accepted",
                    source_proposal_id=proposal_id,
                    typed_proposal_id=accepted[0],
                    mutation_event_ref=accepted[1],
                    replayed=True,
                )
            compiled = self._compile(
                projection=projected,
                authored=authored,
                audit_event_ref=audit.event_ref,
                change=change,
                source_event=source_event,
            )
            proposal_event = self._proposal_event(
                compiled=compiled,
                audit_event_ref=audit.event_ref,
                source_event=source_event,
                logical_time=projected.logical_time or source_event.logical_time,
            )
            located = self._ledger.lookup_event_commit(compiled.proposal_event_id)
            if located is None:
                try:
                    self._ledger.commit_at_cursor(
                        (proposal_event,),
                        expected_cursor=_projection_cursor(projected),
                        commit_id="commit:character-interior:experience:proposal:"
                        + _digest([self._ledger.world_id, compiled.proposal_id]),
                    )
                except ConcurrencyConflict:
                    continue
            elif located[0] != proposal_event:
                raise ValueError("experience typed proposal identity has conflicting bytes")

            projected = self._ledger.project()
            accepted = self._accepted_change(projected, change=change)
            if accepted is not None:
                return ExperienceTransitionSettlementResult(
                    status="accepted",
                    source_proposal_id=proposal_id,
                    typed_proposal_id=accepted[0],
                    mutation_event_ref=accepted[1],
                    replayed=True,
                )
            evaluated = compiled.proposal.evaluated_world_revision
            if projected.world_revision != evaluated:
                self._record_stale_if_needed(
                    compiled=compiled,
                    source_event=source_event,
                    projection=projected,
                )
                continue

            acceptance_event, mutation_event = self._accepted_events(
                compiled=compiled,
                source_event=source_event,
                logical_time=projected.logical_time or source_event.logical_time,
            )
            try:
                self._ledger.commit_at_cursor(
                    (acceptance_event, mutation_event),
                    expected_cursor=_projection_cursor(projected),
                    commit_id="commit:character-interior:experience:accept:"
                    + _digest([self._ledger.world_id, compiled.proposal_id]),
                )
            except ConcurrencyConflict:
                continue
            return ExperienceTransitionSettlementResult(
                status="accepted",
                source_proposal_id=proposal_id,
                typed_proposal_id=compiled.proposal_id,
                mutation_event_ref=compiled.mutation_event_id,
            )
        raise ConcurrencyConflict("experience settlement exhausted bounded CAS retries")

    def _authored_change(
        self,
        *,
        audit_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> tuple[object, DecisionProposal, TypedChange | None]:
        projection = self._ledger.project_at(audit_cursor)
        audit = next(
            (
                item
                for item in projection.proposal_audits
                if item.proposal_id == proposal_id and item.trigger_ref == source_event.event_id
            ),
            None,
        )
        if audit is None:
            raise ValueError("experience settlement source audit is unavailable")
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
        if not isinstance(proposal, DecisionProposal):
            raise ValueError("experience settlement source is not a decision")
        changes = tuple(
            item
            for item in proposal.proposed_changes
            if item.kind in _LIVE_EXPERIENCE_KINDS
            and item.policy_refs == (EXPERIENCE_SETTLEMENT_POLICY_REF,)
        )
        if len(changes) > 1:
            raise ValueError("one Interior experience authored multiple durable directions")
        return audit, proposal, (changes[0] if changes else None)

    @staticmethod
    def _accepted_change(
        projection: object,
        *,
        change: TypedChange,
    ) -> tuple[str, str] | None:
        collections = {
            "v2_goal_transition": getattr(projection, "goal_transitions", ()),
            "thread_transition": getattr(projection, "thread_transitions", ()),
            "commitment_transition": getattr(projection, "commitment_transitions", ()),
            "memory_candidate_transition": getattr(projection, "memory_candidate_transitions", ()),
        }
        for transition in collections[change.kind]:
            if transition.change_id != change.change_id:
                continue
            proposal_id = next(
                (
                    item.proposal_id
                    for item in getattr(projection, "acceptance_decisions", ())
                    if item.status == "accepted" and item.accepted_change_id == change.change_id
                ),
                "proposal:accepted:" + change.change_id,
            )
            return proposal_id, transition.accepted_event_ref
        return None

    def _compile(
        self,
        *,
        projection: object,
        authored: DecisionProposal,
        audit_event_ref: str,
        change: TypedChange,
        source_event: WorldEvent,
    ) -> _CompiledExperienceAuthority:
        evidence = self._selected_evidence(authored=authored, change=change)
        root = _digest(
            [
                self._ledger.world_id,
                audit_event_ref,
                change.change_id,
                projection.world_revision,
            ]
        )
        ids = {
            "proposal_id": f"proposal:character-interior:experience:{root}",
            "acceptance_id": f"acceptance:character-interior:experience:{root}",
            "transition_id": f"transition:character-interior:experience:{root}",
            "proposal_event_id": f"event:character-interior:experience:proposal:{root}",
            "acceptance_event_id": f"event:character-interior:experience:acceptance:{root}",
            "mutation_event_id": f"event:character-interior:experience:mutation:{root}",
        }
        if change.kind == "v2_goal_transition":
            proposal, mutation, event_type = self._compile_goal(
                projection=projection,
                change=change,
                source_event=source_event,
                ids=ids,
            )
        elif change.kind == "thread_transition":
            proposal, mutation, event_type = self._compile_thread(
                projection=projection,
                change=change,
                evidence=evidence,
                source_event=source_event,
                ids=ids,
            )
        elif change.kind == "commitment_transition":
            proposal, mutation, event_type = self._compile_commitment(
                projection=projection,
                change=change,
                evidence=evidence,
                source_event=source_event,
                ids=ids,
            )
        else:
            proposal, mutation, event_type = self._compile_memory(
                projection=projection,
                change=change,
                source_event=source_event,
                ids=ids,
            )
        return _CompiledExperienceAuthority(
            proposal=proposal,
            mutation=mutation,
            mutation_event_type=event_type,
            proposal_event_id=ids["proposal_event_id"],
            acceptance_event_id=ids["acceptance_event_id"],
            mutation_event_id=ids["mutation_event_id"],
            proposal_id=ids["proposal_id"],
            acceptance_id=ids["acceptance_id"],
        )

    @staticmethod
    def _selected_evidence(
        *,
        authored: DecisionProposal,
        change: TypedChange,
    ) -> tuple[EvidenceRef, ...]:
        by_ref: dict[str, ProposalEvidenceRef] = {
            item.ref_id: item for item in authored.evidence_refs
        }
        if any(ref not in by_ref for ref in change.evidence_refs):
            raise ValueError("experience transition evidence escaped its source audit")
        return tuple(
            EvidenceRef(
                ref_id=ref,
                evidence_type=by_ref[ref].evidence_kind,
                claim_purpose="future_plan",
                source_world_revision=by_ref[ref].source_world_revision,
                immutable_hash=by_ref[ref].immutable_hash.removeprefix("sha256:"),
            )
            for ref in change.evidence_refs
        )

    @staticmethod
    def _merge_evidence(
        inherited: tuple[EvidenceRef, ...],
        selected: tuple[EvidenceRef, ...],
    ) -> tuple[EvidenceRef, ...]:
        merged: list[EvidenceRef] = []
        seen: set[str] = set()
        for item in (*inherited, *selected):
            if item.ref_id in seen:
                continue
            seen.add(item.ref_id)
            merged.append(item)
        return tuple(merged)

    def _compile_goal(self, **_: object) -> tuple[object, object, str]:
        # GoalAuthority carries a richer internal-intention/content contract
        # than the legacy envelope.  Until that complete author is wired, a
        # Goal choice remains retryable and cannot be turned into a guessed
        # after-image by this mechanical settlement adapter.
        raise ValueError("v2 Goal lifecycle content authority is unavailable")

    def _compile_thread(
        self,
        *,
        projection: object,
        change: TypedChange,
        evidence: tuple[EvidenceRef, ...],
        source_event: WorldEvent,
        ids: dict[str, str],
    ) -> tuple[ThreadProposalProjection, ThreadChangedPayload, str]:
        intent = ThreadPayload.model_validate_json(change.payload.canonical_json, strict=True)
        if intent.reason_summary is None:
            raise ValueError("thread transition lacks live character authority")
        logical_time = projection.logical_time or source_event.logical_time
        current = next(
            (item for item in projection.threads if item.thread_id == change.target_id),
            None,
        )
        if change.transition == "open":
            if current is not None or change.expected_entity_revision != 0:
                raise ValueError("thread open target is no longer available")
            due_window = (
                None
                if intent.due is None
                else DueWindow(opens_at=logical_time, closes_at=intent.due)
            )
            values = ThreadValues(
                kind=intent.thread_kind,
                subject_ref=source_event.event_id,
                conversation_ref=source_event.correlation_id,
                anchor_evidence_refs=evidence,
                source_evidence_refs=evidence,
                importance_bp=intent.importance,
                due_window=due_window,
                expires_at=intent.expires_at,
                resolution_contract_ref=(
                    "resolution:character-interior:thread:"
                    + _digest([intent.thread_kind, source_event.event_id])
                ),
                privacy_class="private",
                status="open",
            )
            opened_at = logical_time
            event_type = "ThreadOpened"
            before = None
        else:
            if (
                current is None
                or current.values.status != "open"
                or current.entity_revision != change.expected_entity_revision
            ):
                raise ValueError("thread transition target is stale")
            merged = self._merge_evidence(current.values.source_evidence_refs, evidence)
            if change.transition == "update":
                if intent.due is None:
                    due_window = None
                elif current.values.due_window is not None:
                    due_window = current.values.due_window.model_copy(
                        update={"closes_at": intent.due}
                    )
                else:
                    due_window = DueWindow(opens_at=logical_time, closes_at=intent.due)
                values = current.values.model_copy(
                    update={
                        "source_evidence_refs": merged,
                        "importance_bp": intent.importance,
                        "due_window": due_window,
                        "expires_at": intent.expires_at,
                    }
                )
                event_type = "ThreadUpdated"
            elif change.transition == "resolve":
                if intent.resolution_ref != source_event.event_id or intent.resolution_kind is None:
                    raise ValueError("thread resolution lacks exact authored result")
                values = current.values.model_copy(
                    update={
                        "source_evidence_refs": merged,
                        "status": "resolved",
                        "resolution_kind": intent.resolution_kind,
                        "resolution_ref": intent.resolution_ref,
                    }
                )
                event_type = "ThreadResolved"
            elif change.transition == "cancel":
                if intent.cancellation_reason_code is None:
                    raise ValueError("thread cancellation lacks character reason")
                values = current.values.model_copy(
                    update={
                        "source_evidence_refs": merged,
                        "status": "cancelled",
                        "cancellation_reason_code": intent.cancellation_reason_code,
                        "cancellation_evidence_ref": source_event.event_id,
                    }
                )
                event_type = "ThreadCancelled"
            else:
                raise ValueError("unsupported CharacterInterior thread transition")
            opened_at = current.opened_at
            before = current

        origin = ThreadOrigin(
            change_id=change.change_id,
            transition_id=ids["transition_id"],
            policy_refs=_THREAD_POLICY_REFS,
            accepted_event_ref=ids["mutation_event_id"],
        )
        after = ThreadProjection(
            thread_id=change.target_id,
            entity_revision=(1 if before is None else before.entity_revision + 1),
            semantic_fingerprint=thread_semantic_fingerprint(
                kind=values.kind,
                subject_ref=values.subject_ref,
                conversation_ref=values.conversation_ref,
                anchor_evidence_refs=values.anchor_evidence_refs,
                resolution_contract_ref=values.resolution_contract_ref,
                policy_refs=origin.policy_refs,
            ),
            values=values,
            origin=origin,
            opened_at=opened_at,
            updated_at=logical_time,
        )
        raw = {
            "change_id": change.change_id,
            "transition_id": ids["transition_id"],
            "expected_entity_revision": change.expected_entity_revision or 0,
            "evidence_refs": after.values.source_evidence_refs,
            "policy_refs": _THREAD_POLICY_REFS,
            "acceptance_id": ids["acceptance_id"],
            "proposal_id": ids["proposal_id"],
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "operation": change.transition,
            "thread_before": before,
            "thread_after": after,
            "compensates_transition_id": None,
        }
        raw["accepted_change_hash"] = thread_mutation_hash(raw)
        mutation = ThreadChangedPayload.model_validate(raw)
        proposal = ThreadProposalProjection(
            proposal_id=ids["proposal_id"],
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:thread.1",
            transition_kind=change.transition,
            change_id=change.change_id,
            transition_id=ids["transition_id"],
            evaluated_world_revision=projection.world_revision,
            expected_entity_revision=change.expected_entity_revision or 0,
            proposed_change_hash=mutation.accepted_change_hash,
            evidence_refs=mutation.evidence_refs,
            policy_refs=_THREAD_POLICY_REFS,
            proposed_mutation=ThreadProposedMutation(
                event_type=event_type,
                payload_json=_canonical(mutation.model_dump(mode="json")),
            ),
        )
        return proposal, mutation, event_type

    def _compile_commitment(
        self,
        *,
        projection: object,
        change: TypedChange,
        evidence: tuple[EvidenceRef, ...],
        source_event: WorldEvent,
        ids: dict[str, str],
    ) -> tuple[CommitmentProposalProjection, CommitmentChangedPayload, str]:
        intent = CommitmentPayload.model_validate_json(change.payload.canonical_json, strict=True)
        if intent.reason_summary is None:
            raise ValueError("commitment transition lacks live character authority")
        if self._companion_actor_ref != "actor:companion":
            raise ValueError("commitment authority actor contract is unavailable")
        logical_time = projection.logical_time or source_event.logical_time
        current = next(
            (item for item in projection.commitments if item.commitment_id == change.target_id),
            None,
        )
        if change.transition == "open":
            if current is not None or change.expected_entity_revision != 0 or intent.due is None:
                raise ValueError("commitment open target is unavailable")
            thread = next(
                (
                    item
                    for item in projection.threads
                    if item.thread_id == intent.content_ref and item.values.status == "open"
                ),
                None,
            )
            if thread is None:
                raise ValueError("commitment Thread authority is stale")
            anchor = next(
                (item for item in evidence if item.ref_id == thread.origin.accepted_event_ref),
                None,
            )
            if anchor is None:
                raise ValueError("commitment omitted its Thread authority")
            contract = CommitmentFulfillmentContract(
                contract_kind="thread_resolution",
                evidence_type="committed_world_event",
                expected_event_type="ThreadResolved",
                expected_thread_id=thread.thread_id,
                contract_version="commitment-fulfillment-contract.1",
            )
            values = CommitmentValues(
                subject_ref=thread.values.subject_ref,
                content_ref=thread.thread_id,
                content_hash=thread.semantic_fingerprint,
                anchor_evidence_refs=(anchor,),
                source_evidence_refs=evidence,
                importance_bp=intent.importance,
                due_window=DueWindow(opens_at=logical_time, closes_at=intent.due),
                persistence_level=intent.persistence,
                fulfillment_contract=contract,
                privacy_class=thread.values.privacy_class,
                status="open",
            )
            before = None
            opened_at = logical_time
            event_type = "PrivateCommitmentOpened"
        elif change.transition == "release":
            if (
                current is None
                or current.values.status not in {"open", "due"}
                or current.entity_revision != change.expected_entity_revision
                or intent.release_reason_code is None
            ):
                raise ValueError("commitment release target is stale or incomplete")
            values = current.values.model_copy(
                update={
                    "source_evidence_refs": self._merge_evidence(
                        current.values.source_evidence_refs,
                        evidence,
                    ),
                    "status": "released",
                    "settlement_evidence_ref": source_event.event_id,
                    "settlement_reason_code": intent.release_reason_code,
                }
            )
            before = current
            opened_at = current.opened_at
            event_type = "PrivateCommitmentReleased"
        else:
            raise ValueError("unsupported CharacterInterior commitment transition")

        origin = CommitmentOrigin(
            change_id=change.change_id,
            transition_id=ids["transition_id"],
            policy_refs=_COMMITMENT_POLICY_REFS,
            accepted_event_ref=ids["mutation_event_id"],
        )
        after = CommitmentProjection(
            commitment_id=change.target_id,
            entity_revision=(1 if before is None else before.entity_revision + 1),
            semantic_fingerprint=commitment_semantic_fingerprint(
                owner_ref=self._companion_actor_ref,
                subject_ref=values.subject_ref,
                content_ref=values.content_ref,
                content_hash=values.content_hash,
                anchor_evidence_refs=values.anchor_evidence_refs,
                fulfillment_contract=values.fulfillment_contract,
                predecessor_commitment_ref=values.predecessor_commitment_ref,
                lineage_kind=values.lineage_kind,
                policy_refs=origin.policy_refs,
            ),
            values=values,
            origin=origin,
            opened_at=opened_at,
            updated_at=logical_time,
        )
        raw = {
            "change_id": change.change_id,
            "transition_id": ids["transition_id"],
            "expected_entity_revision": change.expected_entity_revision or 0,
            "evidence_refs": after.values.source_evidence_refs,
            "policy_refs": _COMMITMENT_POLICY_REFS,
            "acceptance_id": ids["acceptance_id"],
            "proposal_id": ids["proposal_id"],
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "operation": change.transition,
            "commitment_before": before,
            "commitment_after": after,
        }
        raw["accepted_change_hash"] = commitment_mutation_hash(raw)
        mutation = CommitmentChangedPayload.model_validate(raw)
        proposal = CommitmentProposalProjection(
            proposal_id=ids["proposal_id"],
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:commitment.1",
            transition_kind=change.transition,
            change_id=change.change_id,
            transition_id=ids["transition_id"],
            evaluated_world_revision=projection.world_revision,
            expected_entity_revision=change.expected_entity_revision or 0,
            proposed_change_hash=mutation.accepted_change_hash,
            evidence_refs=mutation.evidence_refs,
            policy_refs=_COMMITMENT_POLICY_REFS,
            proposed_mutation=CommitmentProposedMutation(
                event_type=event_type,
                payload_json=_canonical(mutation.model_dump(mode="json")),
            ),
        )
        return proposal, mutation, event_type

    def _compile_memory(
        self,
        *,
        projection: object,
        change: TypedChange,
        source_event: WorldEvent,
        ids: dict[str, str],
    ) -> tuple[MemoryCandidateProposalProjection, MemoryCandidateChangedPayload, str]:
        intent = MemoryCandidatePayload.model_validate_json(
            change.payload.canonical_json,
            strict=True,
        )
        if (
            intent.reason_summary is None
            or intent.cue_kind is None
            or intent.salience is None
            or len(intent.source_descriptors) != 1
            or not intent.retention_rationales
        ):
            raise ValueError("memory transition lacks complete character authority")
        if change.transition != "open" or change.expected_entity_revision != 0:
            raise ValueError("CharacterInterior memory may only open a source candidate")
        if any(item.candidate_id == change.target_id for item in projection.memory_candidates):
            raise ValueError("memory candidate target already exists")
        descriptor = intent.source_descriptors[0]
        source = MemorySourceBinding.model_validate(descriptor.model_dump(mode="json"))
        committed = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == source.authority_event_ref
            ),
            None,
        )
        if (
            committed is None
            or committed.world_revision != source.authority_world_revision
            or committed.payload_hash != source.authority_payload_hash
        ):
            raise ValueError("memory source authority is stale")
        authority_id = memory_source_authority_id(source)
        if any(
            authority_id in item.values.consumed_source_authority_ids
            for item in projection.memory_candidates
        ):
            raise ValueError("memory source authority was already consumed")
        salience = MemorySalienceVector(
            **intent.salience.model_dump(mode="json"),
            matrix_digest=MEMORY_SALIENCE_MATRIX_DIGEST,
        )
        evidence = (memory_source_evidence(source),)
        logical_time = projection.logical_time or source_event.logical_time
        values = MemoryCandidateValues(
            summary_ref=source.source_id,
            summary_payload_hash=source.source_values_hash,
            cue_kind=intent.cue_kind,
            source_bindings=(source,),
            consumed_source_authority_ids=(authority_id,),
            retention_rationales=tuple(intent.retention_rationales),
            future_use_refs=(),
            privacy_ceiling=intent.privacy_ceiling,
            salience=salience,
            review_due_at=None,
            status="pending",
            retrieval_strength_bp=intent.retrieval_strength,
            reinforcement_count=0,
        )
        origin = MemoryCandidateOrigin(
            change_id=change.change_id,
            transition_id=ids["transition_id"],
            policy_refs=MEMORY_POLICY_REFS,
            accepted_event_ref=ids["mutation_event_id"],
        )
        cluster = memory_source_cluster_fingerprint(
            values=values,
            policy_refs=MEMORY_POLICY_REFS,
        )
        after = MemoryCandidateProjection(
            candidate_id=change.target_id,
            entity_revision=1,
            semantic_fingerprint=memory_candidate_semantic_fingerprint(
                values=values,
                policy_refs=MEMORY_POLICY_REFS,
            ),
            source_cluster_fingerprint=cluster,
            source_cluster_lineage=(cluster,),
            values=values,
            origin=origin,
            opened_at=logical_time,
            updated_at=logical_time,
        )
        raw = {
            "change_id": change.change_id,
            "transition_id": ids["transition_id"],
            "expected_entity_revision": 0,
            "evidence_refs": evidence,
            "policy_refs": MEMORY_POLICY_REFS,
            "acceptance_id": ids["acceptance_id"],
            "proposal_id": ids["proposal_id"],
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "operation": "open",
            "candidate_before": None,
            "candidate_after": after,
        }
        raw["accepted_change_hash"] = memory_candidate_mutation_hash(raw)
        mutation = MemoryCandidateChangedPayload.model_validate(raw)
        event_type = "MemoryCandidateOpened"
        proposal = MemoryCandidateProposalProjection(
            proposal_id=ids["proposal_id"],
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:memory-candidate.1",
            transition_kind="open",
            change_id=change.change_id,
            transition_id=ids["transition_id"],
            evaluated_world_revision=projection.world_revision,
            expected_entity_revision=0,
            proposed_change_hash=mutation.accepted_change_hash,
            evidence_refs=mutation.evidence_refs,
            policy_refs=MEMORY_POLICY_REFS,
            proposed_mutation=MemoryCandidateProposedMutation(
                event_type=event_type,
                payload_json=_canonical(mutation.model_dump(mode="json")),
            ),
        )
        return proposal, mutation, event_type

    def _world_event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        source_event: WorldEvent,
        logical_time: datetime,
        causation_id: str,
    ) -> WorldEvent:
        identity = domain_idempotency_key(
            event_type=event_type,
            world_id=self._ledger.world_id,
            payload=payload,
        )
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=causation_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity
            or "world-v2:character-interior:experience:" + _digest([event_type, payload]),
            payload=payload,
        )

    def _proposal_event(
        self,
        *,
        compiled: _CompiledExperienceAuthority,
        audit_event_ref: str,
        source_event: WorldEvent,
        logical_time: datetime,
    ) -> WorldEvent:
        return self._world_event(
            event_id=compiled.proposal_event_id,
            event_type="ProposalRecorded",
            payload=compiled.proposal.model_dump(mode="json"),
            source_event=source_event,
            logical_time=logical_time,
            causation_id=audit_event_ref,
        )

    def _accepted_events(
        self,
        *,
        compiled: _CompiledExperienceAuthority,
        source_event: WorldEvent,
        logical_time: datetime,
    ) -> tuple[WorldEvent, WorldEvent]:
        acceptance_payload = {
            "acceptance_id": compiled.acceptance_id,
            "status": "accepted",
            "proposal_id": compiled.proposal_id,
            "evaluated_world_revision": compiled.proposal.evaluated_world_revision,
            "accepted_change_id": compiled.proposal.change_id,
            "accepted_change_hash": compiled.proposal.proposed_change_hash,
        }
        acceptance = self._world_event(
            event_id=compiled.acceptance_event_id,
            event_type="AcceptanceRecorded",
            payload=acceptance_payload,
            source_event=source_event,
            logical_time=logical_time,
            causation_id=compiled.proposal_event_id,
        )
        mutation = self._world_event(
            event_id=compiled.mutation_event_id,
            event_type=compiled.mutation_event_type,
            payload=compiled.mutation.model_dump(mode="json"),
            source_event=source_event,
            logical_time=logical_time,
            causation_id=acceptance.event_id,
        )
        return acceptance, mutation

    def _record_stale_if_needed(
        self,
        *,
        compiled: _CompiledExperienceAuthority,
        source_event: WorldEvent,
        projection: object,
    ) -> None:
        existing = next(
            (
                item
                for item in projection.acceptance_decisions
                if item.proposal_id == compiled.proposal_id
            ),
            None,
        )
        if existing is not None:
            if existing.status != "stale":
                raise ValueError("experience proposal already has a non-stale decision")
            return
        event = self._world_event(
            event_id=compiled.acceptance_event_id + ":stale",
            event_type="AcceptanceRecorded",
            payload={
                "acceptance_id": compiled.acceptance_id + ":stale",
                "status": "stale",
                "proposal_id": compiled.proposal_id,
                "evaluated_world_revision": compiled.proposal.evaluated_world_revision,
            },
            source_event=source_event,
            logical_time=projection.logical_time or source_event.logical_time,
            causation_id=compiled.proposal_event_id,
        )
        try:
            self._ledger.commit_at_cursor(
                (event,),
                expected_cursor=_projection_cursor(projection),
                commit_id="commit:character-interior:experience:stale:"
                + _digest([self._ledger.world_id, compiled.proposal_id]),
            )
        except ConcurrencyConflict:
            return


__all__ = [
    "CommitmentExperienceTransitionDraft",
    "ExperienceTransitionCapability",
    "ExperienceTransitionDraft",
    "ExperienceTransitionSettlement",
    "ExperienceTransitionSettlementResult",
    "EXPERIENCE_SETTLEMENT_POLICY_REF",
    "GoalExperienceTransitionDraft",
    "MemoryCandidateExperienceTransitionDraft",
    "MemorySalienceDraft",
    "ThreadExperienceTransitionDraft",
    "build_experience_transition_capability",
    "materialize_experience_transition_change",
    "validate_experience_transition_draft",
]
