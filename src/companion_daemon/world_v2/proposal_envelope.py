"""Strict, inert proposal value objects for the World v2 acceptance boundary.

These models deliberately have no ledger, reducer, model, or execution dependency.  They
describe what a producer proposed; they do not authorize an action or make a world fact true.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import re
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from .schema_core import FrozenModel, PrivacyClass


ProposalKind = Literal["decision", "continuation", "minimal"]
ActionLayer = Literal[
    "internal_state_transition",
    "world_event",
    "external_action",
    "media_action",
    "read_only_tool",
]
PROPOSAL_SCHEMA_REGISTRY_VERSION = "world-v2-proposals.1"
_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"
_MAX_PAYLOAD_JSON_BYTES = 65_536
_MAX_PAYLOAD_JSON_DEPTH = 32
_MAX_PAYLOAD_JSON_NODES = 4_096
_MAX_ENVELOPE_NODES = 20_000
_MAX_ENVELOPE_UTF8_BYTES = 262_144
_MAX_ID_LENGTH = 256
_MAX_REF_LENGTH = 512
BoundedLabel = Annotated[str, Field(min_length=1, max_length=128)]
BoundedRef = Annotated[str, Field(min_length=1, max_length=_MAX_REF_LENGTH)]

# This is the executable form of plan section 4.1.  A new payload or transition requires a
# registry version bump; accepting an arbitrary mapping here would silently create a second
# mutation seam.
CHANGE_TRANSITION_REGISTRY: dict[str, frozenset[str]] = {
    "fact_transition": frozenset({"commit", "correct", "withdraw", "compensate"}),
    "experience_transition": frozenset({"commit"}),
    "character_core_revision": frozenset({"initialize", "revise", "compensate"}),
    "goal_transition": frozenset(
        {
            "open",
            "progress",
            "pause",
            "resume",
            "block",
            "unblock",
            "complete",
            "abandon",
            "expire",
            "compensate",
        }
    ),
    "resource_transition": frozenset({"adjust", "clock_adjust", "compensate"}),
    "attention_transition": frozenset({"change", "expire", "compensate"}),
    "activity_transition": frozenset({"plan", "start", "pause", "resume", "complete", "abandon"}),
    "location_transition": frozenset({"change", "compensate"}),
    "world_occurrence_transition": frozenset({"commit", "cancel", "expire"}),
    "social_encounter_transition": frozenset({"start", "end"}),
    "outcome_settlement": frozenset({"settle"}),
    "npc_relationship_adjustment": frozenset({"adjust"}),
    "appraisal_transition": frozenset({"activate", "contradict", "expire", "supersede"}),
    "affect_transition": frozenset({"open", "update", "resolve", "supersede"}),
    "private_impression_transition": frozenset(
        {"open", "support", "contradict", "expire", "revise"}
    ),
    "relationship_adjustment": frozenset({"adjust", "compensate"}),
    "boundary_transition": frozenset({"open", "revise", "close"}),
    "thread_transition": frozenset({"open", "update", "resolve", "cancel", "expire"}),
    "interaction_bid_transition": frozenset({"open", "update", "resolve", "withdraw", "expire"}),
    "commitment_transition": frozenset({"open", "due", "fulfill", "break", "release"}),
    "memory_candidate_transition": frozenset(
        {
            "open",
            "accept",
            "reject",
            "revise",
            "reinforce",
            "forget",
            "compensate",
        }
    ),
    "expression_plan_transition": frozenset(
        {"accept", "reconsider", "cancel", "supersede", "settle"}
    ),
    "photo_candidate_transition": frozenset({"open", "select", "skip", "expire"}),
    "media_continuation": frozenset({"plan_to_render", "render_to_inspect", "inspect_to_delivery"}),
    "media_repair_transition": frozenset({"authorize", "abandon"}),
    "grant_request": frozenset({"request", "grant", "revoke"}),
}

JsonType = type | tuple[type, ...]


class _PayloadContract:
    def __init__(
        self,
        required: dict[str, JsonType],
        optional: dict[str, JsonType] | None = None,
    ) -> None:
        self.required = required
        self.optional = optional or {}


# Field-level contracts are intentionally local to the envelope schema registry.  Deeper domain
# invariants (predecessor state, hashes against ledger bytes, privacy policy) remain Acceptance's
# job, but an arbitrary ``{}`` can no longer masquerade as a typed mutation.
PAYLOAD_CONTRACTS: dict[str, _PayloadContract] = {
    "fact_transition": _PayloadContract(
        {
            "before_image": (dict, type(None)),
            "after_image": (dict, type(None)),
            "subject": str,
            "predicate": str,
            "cardinality": str,
            "conflict_key": str,
            "value_hash": str,
            "assertion_binding": dict,
            "anchor_evidence": list,
            "source_evidence": list,
            "privacy": str,
        }
    ),
    "experience_transition": _PayloadContract(
        {
            "immutable_experience": dict,
            "source_bindings": list,
            "participants": list,
            "time_range": dict,
            "summary_payload_hash": str,
            "privacy": str,
        }
    ),
    "character_core_revision": _PayloadContract(
        {
            "before_image": (dict, type(None)),
            "after_image": (dict, type(None)),
            "field_classes": list,
            "evidence_window": dict,
            "policy_digest": str,
        }
    ),
    "goal_transition": _PayloadContract(
        {
            "before_image": (dict, type(None)),
            "after_image": (dict, type(None)),
            "goal_id": str,
            "outcome_ref": (str, type(None)),
            "importance": int,
            "progress": int,
            "due": (str, type(None)),
            "blockers": list,
            "completion_contract": dict,
        }
    ),
    "resource_transition": _PayloadContract(
        {"resource_kind": str, "before": int, "delta": int, "after": int, "cause": str},
        {"clock_binding": dict},
    ),
    "attention_transition": _PayloadContract(
        {
            "before": (dict, type(None)),
            "after": (dict, type(None)),
            "focus": (dict, type(None)),
            "cause": str,
        },
        {"expiry": (str, type(None)), "clock_binding": dict},
    ),
    "activity_transition": _PayloadContract(
        {"activity_id": str, "plan_ref": str, "phase": str, "participants": list, "location": str}
    ),
    "location_transition": _PayloadContract(
        {
            "from_location": (str, type(None)),
            "to_location": (str, type(None)),
            "visibility": str,
            "transition_class": str,
            "cause": str,
        }
    ),
    "world_occurrence_transition": _PayloadContract(
        {
            "occurrence_id": str,
            "participants": list,
            "location": str,
            "window": dict,
            "preconditions": list,
        }
    ),
    "social_encounter_transition": _PayloadContract(
        {"encounter_id": str, "participants": list, "location": str, "visibility": str}
    ),
    "outcome_settlement": _PayloadContract(
        {
            "outcome_proposal_id": str,
            "result_id": str,
            "entity_id": str,
            "entity_revision": int,
            "observations": list,
            "result_payload": dict,
        }
    ),
    "npc_relationship_adjustment": _PayloadContract(
        {"npc_id": str, "variable_deltas": dict, "policy_version": str, "cause": str}
    ),
    "appraisal_transition": _PayloadContract(
        {
            "appraisal_id": str,
            "meaning_candidates": list,
            "attribution": str,
            "severity": int,
            "confidence": int,
            "expiry": (str, type(None)),
        }
    ),
    "affect_transition": _PayloadContract(
        {
            "episode_id": str,
            "appraisal_change_refs": list,
            "component_deltas": dict,
            "decay_config": dict,
            "residue_config": dict,
        }
    ),
    "private_impression_transition": _PayloadContract(
        {
            "impression_id": str,
            "interpretations": list,
            "confidence": int,
            "expiry": (str, type(None)),
            "contradiction": (dict, type(None)),
        }
    ),
    "relationship_adjustment": _PayloadContract(
        {
            "relationship_id": str,
            "variable_deltas": dict,
            "policy_version": str,
            "contradiction_group": str,
        }
    ),
    "boundary_transition": _PayloadContract(
        {"boundary_id": str, "scope": str, "strength": int}, {"expiry": (str, type(None))}
    ),
    "thread_transition": _PayloadContract(
        {"thread_id": str, "thread_kind": str, "importance": int, "due": (str, type(None))},
        {"resolution_ref": (str, type(None))},
    ),
    "interaction_bid_transition": _PayloadContract(
        {
            "bid_id": str,
            "goal": str,
            "hoped_response": str,
            "pressure": int,
            "audience": str,
            "due": (str, type(None)),
        }
    ),
    "commitment_transition": _PayloadContract(
        {
            "commitment_id": str,
            "content_ref": str,
            "importance": int,
            "due": (str, type(None)),
            "persistence": str,
        }
    ),
    "memory_candidate_transition": _PayloadContract(
        {
            "before_image": (dict, type(None)),
            "after_image": (dict, type(None)),
            "candidate_id": str,
            "source_refs": list,
            "retention_rationale": str,
            "privacy_ceiling": str,
            "retrieval_strength": int,
        }
    ),
    "expression_plan_transition": _PayloadContract(
        {
            "plan_id": str,
            "overall_intent": str,
            "beat_drafts": list,
            "ordering_policy": str,
            "terminal_policy": str,
        }
    ),
    "photo_candidate_transition": _PayloadContract(
        {"candidate_id": str, "event_refs": list, "family": str, "privacy_ceiling": str}
    ),
    "media_continuation": _PayloadContract(
        {
            "workflow_step_id": str,
            "opportunity_ref": str,
            "plan_ref": str,
            "artifact_ref": (str, type(None)),
            "inspection_ref": (str, type(None)),
            "next_action_payload_hash": str,
        }
    ),
    "media_repair_transition": _PayloadContract(
        {
            "repair_attempt_id": str,
            "plan_ref": str,
            "artifact_ref": str,
            "inspection_ref": str,
            "defect_scope": list,
        }
    ),
    "grant_request": _PayloadContract(
        {
            "grant_kind": str,
            "actor": str,
            "scope": str,
            "constraints": dict,
            "expiry": (str, type(None)),
        }
    ),
}


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    if not isinstance(value, dict):
        raise ValueError("typed payload must be a JSON object")
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("typed payload must contain canonical JSON values") from exc


def _reject_excessive_json_nesting(value: str) -> None:
    """Bound nesting before ``json.loads`` so hostile input cannot exhaust parser recursion."""

    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_PAYLOAD_JSON_DEPTH:
                raise ValueError("canonical_json exceeds maximum nesting depth")
        elif character in "]}":
            depth -= 1


def _json_node_count(value: Any) -> int:
    count = 0
    pending = [value]
    while pending:
        item = pending.pop()
        count += 1
        if count > _MAX_PAYLOAD_JSON_NODES:
            return count
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return count


def _matches_json_type(value: Any, expected: JsonType) -> bool:
    expected_types = expected if isinstance(expected, tuple) else (expected,)
    if int in expected_types and isinstance(value, bool):
        return False
    return isinstance(value, expected_types)


def _validate_payload_contract(kind: str, value: dict[str, Any]) -> None:
    contract = PAYLOAD_CONTRACTS[kind]
    missing = set(contract.required) - set(value)
    if missing:
        raise ValueError(f"{kind} payload missing required fields: {sorted(missing)}")
    unknown = set(value) - set(contract.required) - set(contract.optional)
    if unknown:
        raise ValueError(f"{kind} payload contains unknown fields: {sorted(unknown)}")
    for field_name, expected in {**contract.required, **contract.optional}.items():
        if field_name in value and not _matches_json_type(value[field_name], expected):
            raise ValueError(f"{kind} payload field {field_name!r} has invalid type")


def _preflight_untrusted_envelope(value: Any) -> None:
    """Bound an untrusted graph before serialization or Pydantic recursive validation."""

    pending = [value]
    seen: set[int] = set()
    nodes = 0
    utf8_bytes = 0
    while pending:
        item = pending.pop()
        nodes += 1
        if nodes > _MAX_ENVELOPE_NODES:
            raise ValueError("proposal envelope exceeds maximum node count")
        if isinstance(item, str):
            utf8_bytes += len(item.encode("utf-8"))
            if utf8_bytes > _MAX_ENVELOPE_UTF8_BYTES:
                raise ValueError("proposal envelope exceeds maximum UTF-8 size")
            continue
        if item is None or isinstance(item, (int, float, bool, datetime)):
            continue
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(item, BaseModel):
            try:
                pending.extend(getattr(item, name) for name in type(item).model_fields)
            except AttributeError as exc:
                raise ValueError("proposal model is missing required fields") from exc
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, list)):
            pending.extend(item)
        else:
            raise ValueError(f"proposal envelope contains unsupported value type: {type(item)!r}")


def _validate_serialized_envelope_size(value: Any) -> None:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
            default=lambda item: item.isoformat() if isinstance(item, datetime) else None,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("proposal envelope must be an acyclic JSON-compatible value") from exc
    if len(serialized) > _MAX_ENVELOPE_UTF8_BYTES:
        raise ValueError("proposal envelope exceeds maximum serialized size")


class ProposalEvidenceRef(FrozenModel):
    """Revision-pinned evidence available to acceptance; not a natural-language citation."""

    ref_id: str = Field(min_length=1, max_length=_MAX_REF_LENGTH)
    evidence_kind: Literal[
        "committed_fact",
        "committed_experience",
        "committed_world_event",
        "settled_world_event",
        "settled_external_result",
        "observed_message",
        "active_plan",
    ]
    source_world_revision: int = Field(ge=1)
    immutable_hash: str = Field(pattern=_HASH_PATTERN)


class TypedObjectBinding(FrozenModel):
    object_ref: BoundedRef
    schema_version: BoundedLabel
    payload_hash: str = Field(pattern=_HASH_PATTERN)


class SourceBinding(FrozenModel):
    ref_id: BoundedRef
    source_world_revision: int = Field(ge=1)
    immutable_hash: str = Field(pattern=_HASH_PATTERN)


class NamedFixedPoint(FrozenModel):
    name: BoundedLabel
    value: int = Field(ge=-1_000_000_000, le=1_000_000_000)


class TimeRange(FrozenModel):
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.ends_at < self.starts_at:
            raise ValueError("time range must be ordered")
        return self


class DueWindow(FrozenModel):
    not_before: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.expires_at <= self.not_before:
            raise ValueError("due window must end after it starts")
        return self


class FactPayload(FrozenModel):
    before_image: TypedObjectBinding | None
    after_image: TypedObjectBinding | None
    subject: BoundedRef
    predicate: BoundedLabel
    cardinality: Literal["one", "many"]
    conflict_key: BoundedRef
    value_hash: str = Field(pattern=_HASH_PATTERN)
    assertion_binding: TypedObjectBinding
    anchor_evidence: list[SourceBinding] = Field(min_length=1, max_length=32)
    source_evidence: list[SourceBinding] = Field(min_length=1, max_length=64)
    privacy: PrivacyClass


class ExperiencePayload(FrozenModel):
    immutable_experience: TypedObjectBinding
    source_bindings: list[SourceBinding] = Field(min_length=1, max_length=64)
    participants: list[BoundedRef] = Field(max_length=32)
    time_range: TimeRange
    summary_payload_hash: str = Field(pattern=_HASH_PATTERN)
    privacy: PrivacyClass


class CharacterCorePayload(FrozenModel):
    before_image: TypedObjectBinding | None
    after_image: TypedObjectBinding | None
    field_classes: list[BoundedLabel] = Field(min_length=1, max_length=32)
    evidence_window: TimeRange
    policy_digest: str = Field(pattern=_HASH_PATTERN)


class GoalPayload(FrozenModel):
    before_image: TypedObjectBinding | None
    after_image: TypedObjectBinding | None
    goal_id: BoundedRef
    outcome_ref: BoundedRef | None
    importance: int = Field(ge=0, le=10_000)
    progress: int = Field(ge=0, le=10_000)
    due: datetime | None
    blockers: list[BoundedRef] = Field(max_length=32)
    completion_contract: TypedObjectBinding


class ResourcePayload(FrozenModel):
    resource_kind: BoundedLabel
    before: int
    delta: int
    after: int
    cause: BoundedRef
    clock_binding: SourceBinding | None = None


class AttentionPayload(FrozenModel):
    before: TypedObjectBinding | None
    after: TypedObjectBinding | None
    focus: TypedObjectBinding | None
    cause: BoundedRef
    expiry: datetime | None = None
    clock_binding: SourceBinding | None = None


class ActivityPayload(FrozenModel):
    activity_id: BoundedRef
    plan_ref: BoundedRef
    phase: BoundedLabel
    participants: list[BoundedRef] = Field(max_length=32)
    location: BoundedRef


class LocationPayload(FrozenModel):
    from_location: BoundedRef | None
    to_location: BoundedRef | None
    visibility: PrivacyClass
    transition_class: BoundedLabel
    cause: BoundedRef


class WorldOccurrencePayload(FrozenModel):
    occurrence_id: BoundedRef
    participants: list[BoundedRef] = Field(max_length=32)
    location: BoundedRef
    window: DueWindow
    preconditions: list[BoundedRef] = Field(max_length=32)


class SocialEncounterPayload(FrozenModel):
    encounter_id: BoundedRef
    participants: list[BoundedRef] = Field(min_length=1, max_length=32)
    location: BoundedRef
    visibility: PrivacyClass


class OutcomeSettlementPayload(FrozenModel):
    outcome_proposal_id: BoundedRef
    result_id: BoundedRef
    entity_id: BoundedRef
    entity_revision: int = Field(ge=0)
    observations: list[SourceBinding] = Field(min_length=1, max_length=64)
    result_payload: TypedObjectBinding


class RelationshipDeltaPayload(FrozenModel):
    npc_id: BoundedRef
    variable_deltas: list[NamedFixedPoint] = Field(min_length=1, max_length=32)
    policy_version: BoundedLabel
    cause: BoundedRef


class MeaningCandidate(FrozenModel):
    meaning: BoundedLabel
    confidence: int = Field(ge=0, le=10_000)


class AppraisalPayload(FrozenModel):
    appraisal_id: BoundedRef
    meaning_candidates: list[MeaningCandidate] = Field(min_length=1, max_length=16)
    attribution: BoundedRef
    severity: int = Field(ge=0, le=10_000)
    confidence: int = Field(ge=0, le=10_000)
    expiry: datetime | None


class AffectPayload(FrozenModel):
    episode_id: BoundedRef
    appraisal_change_refs: list[BoundedRef] = Field(min_length=1, max_length=32)
    component_deltas: list[NamedFixedPoint] = Field(min_length=1, max_length=32)
    decay_config: TypedObjectBinding
    residue_config: TypedObjectBinding


class InterpretationCandidate(FrozenModel):
    interpretation: str = Field(min_length=1, max_length=240)
    confidence: int = Field(ge=0, le=10_000)


class PrivateImpressionPayload(FrozenModel):
    impression_id: BoundedRef
    interpretations: list[InterpretationCandidate] = Field(min_length=1, max_length=16)
    confidence: int = Field(ge=0, le=10_000)
    expiry: datetime | None
    contradiction: SourceBinding | None


class RelationshipAdjustmentPayload(FrozenModel):
    relationship_id: BoundedRef
    variable_deltas: list[NamedFixedPoint] = Field(min_length=1, max_length=32)
    policy_version: BoundedLabel
    contradiction_group: BoundedRef


class BoundaryPayload(FrozenModel):
    boundary_id: BoundedRef
    scope: BoundedLabel
    strength: int = Field(ge=0, le=10_000)
    expiry: datetime | None = None


class ThreadPayload(FrozenModel):
    thread_id: BoundedRef
    thread_kind: BoundedLabel
    importance: int = Field(ge=0, le=10_000)
    due: datetime | None
    resolution_ref: BoundedRef | None = None


class InteractionBidPayload(FrozenModel):
    bid_id: BoundedRef
    goal: BoundedLabel
    hoped_response: BoundedLabel
    pressure: int = Field(ge=0, le=10_000)
    audience: BoundedRef
    due: datetime | None


class CommitmentPayload(FrozenModel):
    commitment_id: BoundedRef
    content_ref: BoundedRef
    importance: int = Field(ge=0, le=10_000)
    due: datetime | None
    persistence: BoundedLabel


class MemoryCandidatePayload(FrozenModel):
    before_image: TypedObjectBinding | None
    after_image: TypedObjectBinding | None
    candidate_id: BoundedRef
    source_refs: list[SourceBinding] = Field(min_length=1, max_length=64)
    retention_rationale: str = Field(min_length=1, max_length=240)
    privacy_ceiling: PrivacyClass
    retrieval_strength: int = Field(ge=0, le=10_000)


class EncryptedInlinePayload(FrozenModel):
    ciphertext_ref: BoundedRef
    key_ref: BoundedRef
    plaintext_hash: str = Field(pattern=_HASH_PATTERN)


class ExpressionBeatDraft(FrozenModel):
    beat_id: BoundedRef
    payload_ref: BoundedRef | None = None
    inline_encrypted_payload: EncryptedInlinePayload | None = None
    inline_text: str | None = Field(default=None, min_length=1, max_length=4_096)
    materialized_payload_ref: BoundedRef | None = None
    payload_hash: str = Field(pattern=_HASH_PATTERN)
    content_type: BoundedLabel
    dependency_beat_ids: list[BoundedRef] = Field(max_length=32)
    delay_window: DueWindow | None
    cancel_policy: BoundedLabel
    reconsider_policy: BoundedLabel
    merge_policy: BoundedLabel


class ExpressionPlanPayload(FrozenModel):
    plan_id: BoundedRef
    overall_intent: str = Field(min_length=1, max_length=240)
    beat_drafts: list[ExpressionBeatDraft] = Field(min_length=1, max_length=32)
    ordering_policy: BoundedLabel
    terminal_policy: BoundedLabel


class PhotoCandidatePayload(FrozenModel):
    candidate_id: BoundedRef
    event_refs: list[SourceBinding] = Field(min_length=1, max_length=64)
    family: BoundedLabel
    privacy_ceiling: PrivacyClass


class MediaContinuationPayload(FrozenModel):
    workflow_step_id: BoundedRef
    opportunity_ref: BoundedRef
    plan_ref: BoundedRef
    artifact_ref: BoundedRef | None
    inspection_ref: BoundedRef | None
    next_action_payload_hash: str = Field(pattern=_HASH_PATTERN)


class MediaRepairPayload(FrozenModel):
    repair_attempt_id: BoundedRef
    plan_ref: BoundedRef
    artifact_ref: BoundedRef
    inspection_ref: BoundedRef
    defect_scope: list[BoundedLabel] = Field(min_length=1, max_length=32)


class GrantRequestPayload(FrozenModel):
    grant_kind: BoundedLabel
    actor: BoundedRef
    scope: BoundedLabel
    constraints: TypedObjectBinding
    expiry: datetime | None


PAYLOAD_MODEL_REGISTRY: dict[str, type[FrozenModel]] = {
    "fact_transition": FactPayload,
    "experience_transition": ExperiencePayload,
    "character_core_revision": CharacterCorePayload,
    "goal_transition": GoalPayload,
    "resource_transition": ResourcePayload,
    "attention_transition": AttentionPayload,
    "activity_transition": ActivityPayload,
    "location_transition": LocationPayload,
    "world_occurrence_transition": WorldOccurrencePayload,
    "social_encounter_transition": SocialEncounterPayload,
    "outcome_settlement": OutcomeSettlementPayload,
    "npc_relationship_adjustment": RelationshipDeltaPayload,
    "appraisal_transition": AppraisalPayload,
    "affect_transition": AffectPayload,
    "private_impression_transition": PrivateImpressionPayload,
    "relationship_adjustment": RelationshipAdjustmentPayload,
    "boundary_transition": BoundaryPayload,
    "thread_transition": ThreadPayload,
    "interaction_bid_transition": InteractionBidPayload,
    "commitment_transition": CommitmentPayload,
    "memory_candidate_transition": MemoryCandidatePayload,
    "expression_plan_transition": ExpressionPlanPayload,
    "photo_candidate_transition": PhotoCandidatePayload,
    "media_continuation": MediaContinuationPayload,
    "media_repair_transition": MediaRepairPayload,
    "grant_request": GrantRequestPayload,
}


class CanonicalTypedPayload(FrozenModel):
    """Opaque-but-canonical payload bytes selected by a closed payload schema registry.

    Domain-specific acceptance code may decode these bytes into the corresponding typed domain
    proposal.  Keeping only canonical bytes here prevents a mutable/free ``dict`` from becoming
    an accidental authority surface while retaining a cycle-free envelope layer.
    """

    payload_schema: str = Field(min_length=1, max_length=128)
    payload_version: Literal[1] = 1
    canonical_json: str = Field(min_length=2, max_length=_MAX_PAYLOAD_JSON_BYTES)

    @classmethod
    def from_value(cls, *, payload_schema: str, value: dict[str, Any]) -> Self:
        return cls(
            payload_schema=payload_schema, payload_version=1, canonical_json=_canonical_json(value)
        )

    @model_validator(mode="after")
    def bytes_are_canonical_json_object(self) -> Self:
        if len(self.canonical_json.encode("utf-8")) > _MAX_PAYLOAD_JSON_BYTES:
            raise ValueError("canonical_json exceeds maximum UTF-8 byte size")
        _reject_excessive_json_nesting(self.canonical_json)
        try:
            parsed = json.loads(self.canonical_json)
        except (TypeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("canonical_json must be valid JSON") from exc
        if _json_node_count(parsed) > _MAX_PAYLOAD_JSON_NODES:
            raise ValueError("canonical_json exceeds maximum node count")
        if _canonical_json(parsed) != self.canonical_json:
            raise ValueError("canonical_json must use sorted compact canonical encoding")
        return self

    @property
    def payload_hash(self) -> str:
        return _sha256(self.canonical_json)

    def value(self) -> dict[str, Any]:
        parsed = json.loads(self.canonical_json)
        assert isinstance(parsed, dict)
        return parsed


class TypedChange(FrozenModel):
    change_id: str = Field(min_length=1, max_length=_MAX_ID_LENGTH)
    kind: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=_MAX_REF_LENGTH)
    expected_entity_revision: int | None = Field(default=None, ge=0)
    transition: str = Field(min_length=1, max_length=64)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)
    preconditions: tuple[str, ...] = Field(default=(), max_length=64)
    policy_refs: tuple[str, ...] = Field(default=(), max_length=64)
    payload: CanonicalTypedPayload

    @model_validator(mode="after")
    def kind_transition_and_payload_are_registered(self) -> Self:
        transitions = CHANGE_TRANSITION_REGISTRY.get(self.kind)
        if transitions is None:
            raise ValueError(f"unknown typed change kind: {self.kind}")
        if self.transition not in transitions:
            raise ValueError(f"illegal transition {self.transition!r} for {self.kind}")
        expected_schema = f"{self.kind}.v1"
        if self.payload.payload_schema != expected_schema or self.payload.payload_version != 1:
            raise ValueError(f"payload schema/version must be {expected_schema}/1 for {self.kind}")
        try:
            TypeAdapter(PAYLOAD_MODEL_REGISTRY[self.kind]).validate_json(
                self.payload.canonical_json, strict=True
            )
        except Exception as exc:
            raise ValueError(f"{self.kind} payload violates its nested typed schema") from exc
        for label, refs in (
            ("evidence_refs", self.evidence_refs),
            ("preconditions", self.preconditions),
            ("policy_refs", self.policy_refs),
        ):
            if len(set(refs)) != len(refs):
                raise ValueError(f"{label} must not contain duplicates")
            if any(not ref for ref in refs):
                raise ValueError(f"{label} must not contain empty refs")
            if any(len(ref) > _MAX_REF_LENGTH for ref in refs):
                raise ValueError(f"{label} contains an oversized ref")
        return self


class ProposalActionIntent(FrozenModel):
    """A proposed value object.  It intentionally has no action ID, state, lease, or grant."""

    intent_id: str = Field(min_length=1, max_length=_MAX_ID_LENGTH)
    kind: str = Field(min_length=1, max_length=64)
    layer: ActionLayer
    target: str = Field(min_length=1, max_length=_MAX_REF_LENGTH)
    payload_ref: str = Field(min_length=1, max_length=_MAX_REF_LENGTH)
    payload_hash: str = Field(pattern=_HASH_PATTERN)
    causal_change_id: str | None = None
    beat_ref: str | None = None
    dependencies: tuple[str, ...] = Field(default=(), max_length=64)
    due_window: tuple[datetime, datetime] | None = None

    @model_validator(mode="after")
    def dependency_and_due_contracts_are_coherent(self) -> Self:
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("intent dependencies must not contain duplicates")
        if any(
            not dependency or len(dependency) > _MAX_REF_LENGTH for dependency in self.dependencies
        ):
            raise ValueError("intent dependencies must contain bounded non-empty refs")
        if self.intent_id in self.dependencies:
            raise ValueError("an intent cannot depend on itself")
        if self.due_window is not None and self.due_window[1] <= self.due_window[0]:
            raise ValueError("due_window must end after it starts")
        return self


class ReferencedSummary(FrozenModel):
    change_ref: str = Field(min_length=1, max_length=_MAX_REF_LENGTH)
    summary: str = Field(min_length=1, max_length=240)


class AppraisalSummary(ReferencedSummary):
    pass


class VariationProfile(FrozenModel):
    deviation_kind: BoundedLabel
    deviation_intensity: int = Field(ge=0, le=10_000)
    change_phase: BoundedLabel
    sampling_mode: BoundedLabel
    recovery_posture: BoundedLabel


class ProposalEnvelope(FrozenModel):
    proposal_id: str = Field(min_length=1, max_length=_MAX_ID_LENGTH)
    proposal_kind: ProposalKind
    trigger_ref: str = Field(min_length=1, max_length=_MAX_REF_LENGTH)
    evaluated_world_revision: int = Field(ge=0)
    schema_registry_version: Literal["world-v2-proposals.1"] = PROPOSAL_SCHEMA_REGISTRY_VERSION
    evidence_refs: tuple[ProposalEvidenceRef, ...] = Field(default=(), max_length=128)
    proposed_changes: tuple[TypedChange, ...] = Field(default=(), max_length=64)
    action_intents: tuple[ProposalActionIntent, ...] = Field(default=(), max_length=64)
    confidence: int = Field(ge=0, le=10_000)
    brief_rationale: str = Field(min_length=1, max_length=240)

    EXPRESSION_ACTION_KINDS: ClassVar[frozenset[str]] = frozenset(
        {"reply", "followup", "proactive_message"}
    )

    @model_validator(mode="after")
    def identifiers_evidence_and_expression_beats_are_bound(self) -> Self:
        evidence_ids = [ref.ref_id for ref in self.evidence_refs]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("duplicate evidence ref_id")
        if any(
            ref.source_world_revision > self.evaluated_world_revision for ref in self.evidence_refs
        ):
            raise ValueError("proposal evidence cannot come from a future world revision")
        change_ids = [change.change_id for change in self.proposed_changes]
        if len(set(change_ids)) != len(change_ids):
            raise ValueError("duplicate change_id")
        intent_ids = [intent.intent_id for intent in self.action_intents]
        if len(set(intent_ids)) != len(intent_ids):
            raise ValueError("duplicate intent_id")
        intent_id_set = set(intent_ids)
        dependency_graph = {
            intent.intent_id: set(intent.dependencies) for intent in self.action_intents
        }
        if any(dependencies - intent_id_set for dependencies in dependency_graph.values()):
            raise ValueError("intent dependency must reference an intent in the proposal")
        resolved_intents: set[str] = set()
        remaining_intents = dict(dependency_graph)
        while remaining_intents:
            ready = {
                intent_id
                for intent_id, dependencies in remaining_intents.items()
                if dependencies.issubset(resolved_intents)
            }
            if not ready:
                raise ValueError("intent dependencies must form an acyclic graph")
            resolved_intents.update(ready)
            for intent_id in ready:
                del remaining_intents[intent_id]

        evidence_set = set(evidence_ids)
        change_set = set(change_ids)
        for change in self.proposed_changes:
            unknown = set(change.evidence_refs) - evidence_set
            if unknown:
                raise ValueError(
                    f"change evidence_refs are absent from envelope: {sorted(unknown)}"
                )
        for intent in self.action_intents:
            if intent.causal_change_id is not None and intent.causal_change_id not in change_set:
                raise ValueError("intent causal_change_id must reference a proposed change")

        beats: dict[tuple[str, str], tuple[str, str]] = {}
        for change in self.proposed_changes:
            if change.kind != "expression_plan_transition":
                continue
            drafts = change.payload.value().get("beat_drafts")
            if not isinstance(drafts, list):
                raise ValueError("expression payload must contain beat_drafts array")
            if len(drafts) > 32:
                raise ValueError("expression payload permits at most 32 beat drafts")
            local_ids: set[str] = set()
            local_dependencies: dict[str, tuple[str, ...]] = {}
            for draft in drafts:
                if not isinstance(draft, dict):
                    raise ValueError("each expression beat draft must be an object")
                beat_id = draft.get("beat_id")
                payload_hash = draft.get("payload_hash")
                if not isinstance(beat_id, str) or not beat_id:
                    raise ValueError("expression beat requires beat_id")
                if len(beat_id) > _MAX_ID_LENGTH:
                    raise ValueError("expression beat_id is oversized")
                if beat_id in local_ids:
                    raise ValueError("expression beat IDs must be unique within a change")
                if not isinstance(payload_hash, str) or not re.fullmatch(
                    _HASH_PATTERN, payload_hash
                ):
                    raise ValueError("expression beat requires a sha256 payload_hash")
                payload_ref = draft.get("payload_ref")
                inline_payload = draft.get("inline_encrypted_payload")
                inline_text = draft.get("inline_text")
                has_payload_ref = isinstance(payload_ref, str) and bool(payload_ref)
                has_inline_payload = (
                    isinstance(inline_payload, str)
                    and bool(inline_payload)
                    or isinstance(inline_payload, dict)
                    and bool(inline_payload)
                )
                has_inline_text = isinstance(inline_text, str) and bool(inline_text)
                if has_inline_text and len(inline_text) > 4_096:
                    raise ValueError("inline expression text exceeds maximum length")
                if sum((has_payload_ref, has_inline_payload, has_inline_text)) != 1:
                    raise ValueError(
                        "expression beat requires exactly one payload_ref, inline encrypted payload, or inline_text"
                    )
                materialized_ref = draft.get("materialized_payload_ref")
                if has_payload_ref:
                    if materialized_ref is not None:
                        raise ValueError(
                            "referenced expression beat cannot redefine materialized ref"
                        )
                    bound_payload_ref = payload_ref
                else:
                    if not isinstance(materialized_ref, str) or not materialized_ref:
                        raise ValueError("inline expression beat requires materialized_payload_ref")
                    bound_payload_ref = materialized_ref
                if len(bound_payload_ref) > _MAX_REF_LENGTH:
                    raise ValueError("expression beat payload binding is oversized")
                for required_text in (
                    "content_type",
                    "cancel_policy",
                    "reconsider_policy",
                    "merge_policy",
                ):
                    if not isinstance(draft.get(required_text), str) or not draft[required_text]:
                        raise ValueError(f"expression beat requires {required_text}")
                    if len(draft[required_text]) > 128:
                        raise ValueError(f"expression beat {required_text} is oversized")
                if "delay_window" not in draft:
                    raise ValueError("expression beat requires delay_window")
                dependencies = draft.get("dependency_beat_ids")
                if not isinstance(dependencies, list) or any(
                    not isinstance(item, str) or not item for item in dependencies
                ):
                    raise ValueError("expression beat requires dependency_beat_ids array")
                if len(set(dependencies)) != len(dependencies) or beat_id in dependencies:
                    raise ValueError("expression beat dependencies must be unique and non-self")
                local_ids.add(beat_id)
                local_dependencies[beat_id] = tuple(dependencies)
                key = (change.change_id, beat_id)
                if key in beats:
                    raise ValueError("expression beat binding must be unique")
                beats[key] = (payload_hash, bound_payload_ref)
            if any(set(deps) - local_ids for deps in local_dependencies.values()):
                raise ValueError("expression beat dependency must reference a beat in the plan")
            remaining = dict(local_dependencies)
            resolved: set[str] = set()
            while remaining:
                ready = {
                    beat_id
                    for beat_id, dependencies in remaining.items()
                    if set(dependencies).issubset(resolved)
                }
                if not ready:
                    raise ValueError("expression beat dependencies must be acyclic")
                resolved.update(ready)
                for beat_id in ready:
                    del remaining[beat_id]

        for intent in self.action_intents:
            is_expression_action = intent.kind in self.EXPRESSION_ACTION_KINDS
            if is_expression_action and (
                intent.causal_change_id is None or intent.beat_ref is None
            ):
                raise ValueError("expression action requires causal_change_id and beat_ref")
            if intent.beat_ref is None:
                continue
            if intent.causal_change_id is None:
                raise ValueError("beat_ref requires causal_change_id")
            expected_binding = beats.get((intent.causal_change_id, intent.beat_ref))
            if expected_binding is None:
                raise ValueError("beat_ref must identify a beat in its expression change")
            expected_hash, expected_payload_ref = expected_binding
            if expected_hash != intent.payload_hash:
                raise ValueError("action payload hash must match its expression beat payload hash")
            if expected_payload_ref != intent.payload_ref:
                raise ValueError(
                    "action payload_ref must match its expression beat payload binding"
                )
        return self

    @property
    def proposal_hash(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return _sha256(canonical)


class DecisionProposal(ProposalEnvelope):
    proposal_kind: Literal["decision"] = "decision"
    appraisals: tuple[AppraisalSummary, ...] = Field(default=(), max_length=32)
    affect_tendencies: tuple[BoundedLabel, ...] = Field(default=(), max_length=32)
    drives: tuple[BoundedLabel, ...] = Field(default=(), max_length=3)
    conflicts: tuple[BoundedLabel, ...] = Field(default=(), max_length=32)
    activity_transition: ReferencedSummary | None = None
    behavior_tendency: BoundedLabel
    variation_profile: VariationProfile | None = None
    stance: BoundedLabel
    display_strategy: BoundedLabel
    conversation_thread_changes: tuple[ReferencedSummary, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def summary_views_reference_their_typed_changes(self) -> Self:
        kinds = {change.change_id: change.kind for change in self.proposed_changes}
        if len({summary.change_ref for summary in self.appraisals}) != len(self.appraisals):
            raise ValueError("appraisal summary change refs must be unique")
        for summary in self.appraisals:
            if kinds.get(summary.change_ref) != "appraisal_transition":
                raise ValueError("appraisal summary must reference an appraisal_transition change")
        if self.activity_transition is not None and (
            kinds.get(self.activity_transition.change_ref) != "activity_transition"
        ):
            raise ValueError("activity summary must reference an activity_transition change")
        for summary in self.conversation_thread_changes:
            if kinds.get(summary.change_ref) != "thread_transition":
                raise ValueError("thread summary must reference a thread_transition change")
        if len({item.change_ref for item in self.conversation_thread_changes}) != len(
            self.conversation_thread_changes
        ):
            raise ValueError("thread summary change refs must be unique")
        return self


class ContinuationProposal(ProposalEnvelope):
    proposal_kind: Literal["continuation"] = "continuation"
    workflow_kind: Literal["media_continuation"]
    upstream_result_refs: tuple[BoundedRef, ...] = Field(min_length=1, max_length=16)
    continuation_step: Literal["plan_to_render", "render_to_inspect", "inspect_to_delivery"]

    @model_validator(mode="after")
    def continuation_has_only_registered_mechanical_authority(self) -> Self:
        if any(change.kind != "media_continuation" for change in self.proposed_changes):
            raise ValueError("continuation may contain only media_continuation changes")
        if not self.proposed_changes:
            raise ValueError("continuation requires a media_continuation change")
        if len(self.proposed_changes) != 1 or len(self.action_intents) != 1:
            raise ValueError("continuation requires exactly one media change and one action intent")
        if any(change.transition != self.continuation_step for change in self.proposed_changes):
            raise ValueError("continuation_step must match every continuation change")
        evidence_ids = {ref.ref_id for ref in self.evidence_refs}
        if len(set(self.upstream_result_refs)) != len(self.upstream_result_refs):
            raise ValueError("upstream_result_refs must be unique")
        if not set(self.upstream_result_refs).issubset(evidence_ids):
            raise ValueError("upstream_result_refs must be present in envelope evidence")
        evidence_by_id = {ref.ref_id: ref for ref in self.evidence_refs}
        allowed_evidence_kinds = {
            "plan_to_render": {"settled_external_result", "settled_world_event"},
            "render_to_inspect": {"settled_external_result"},
            "inspect_to_delivery": {"settled_external_result", "settled_world_event"},
        }[self.continuation_step]
        if any(
            evidence_by_id[ref_id].evidence_kind not in allowed_evidence_kinds
            for ref_id in self.upstream_result_refs
        ):
            raise ValueError(f"{self.continuation_step} upstream evidence must be a settled result")
        change = self.proposed_changes[0]
        if not set(self.upstream_result_refs).issubset(set(change.evidence_refs)):
            raise ValueError("media continuation change must cite every upstream result")
        action = self.action_intents[0]
        allowed_action = {
            "plan_to_render": "media_render",
            "render_to_inspect": "media_inspection",
            "inspect_to_delivery": "media_delivery",
        }[self.continuation_step]
        if action.kind != allowed_action or action.layer != "media_action":
            raise ValueError(f"{self.continuation_step} permits only {allowed_action} media_action")
        if action.causal_change_id != change.change_id or action.beat_ref is not None:
            raise ValueError("continuation action must be causally bound only to its media change")
        payload = change.payload.value()
        if action.payload_hash != payload["next_action_payload_hash"]:
            raise ValueError("continuation action hash must match frozen next action payload hash")
        return self


class MinimalProposal(ProposalEnvelope):
    proposal_kind: Literal["minimal"] = "minimal"
    source_model_result: str = Field(min_length=1, max_length=_MAX_REF_LENGTH)
    response_text: str = Field(min_length=1, max_length=4_096)
    stance: Literal["defer", "acknowledge_briefly", "answer_without_world_claims"]
    fact_claims: tuple[()] = ()

    @model_validator(mode="after")
    def minimal_authority_cannot_smuggle_persistent_or_external_work(self) -> Self:
        if self.fact_claims:
            raise ValueError("minimal fact_claims must be empty")
        if any(change.kind != "expression_plan_transition" for change in self.proposed_changes):
            raise ValueError("minimal proposal permits only expression plan changes")
        if len(self.proposed_changes) > 1:
            raise ValueError("minimal proposal permits at most one expression plan change")
        if any(intent.kind not in {"reply", "followup"} for intent in self.action_intents):
            raise ValueError("minimal action intent kind must be reply or followup")
        if any(intent.layer != "external_action" for intent in self.action_intents):
            raise ValueError("minimal actions must be external message actions")
        if len(self.action_intents) > 1:
            raise ValueError("minimal proposal permits at most one reply action")
        if self.proposed_changes:
            drafts = self.proposed_changes[0].payload.value().get("beat_drafts")
            if not isinstance(drafts, list) or len(drafts) != 1:
                raise ValueError("minimal expression plan must contain exactly one beat")
            if len(self.action_intents) != 1:
                raise ValueError("minimal expression plan requires exactly one reply action")
            draft = drafts[0]
            assert isinstance(draft, dict)
            if draft.get("inline_text") != self.response_text:
                raise ValueError("minimal response_text must equal its inline expression beat")
            if draft.get("payload_hash") != _sha256(self.response_text):
                raise ValueError("minimal response_text hash must equal its expression beat hash")
        elif self.action_intents:
            raise ValueError("minimal reply action requires its single-beat expression plan")
        return self


ProposalInput = Annotated[
    DecisionProposal | ContinuationProposal | MinimalProposal,
    Field(discriminator="proposal_kind"),
]
_PROPOSAL_INPUT_ADAPTER = TypeAdapter(ProposalInput)


def validate_proposal_envelope(value: Any) -> ProposalInput:
    """Revalidate an untrusted proposal at the acceptance seam.

    Callers must use this function even when they have received an apparent proposal instance:
    Pydantic's low-level ``model_construct`` intentionally skips validators.  Serializing an
    instance back to primitive values before union validation closes that bypass and also selects
    the subtype from the frozen discriminator.
    """

    _preflight_untrusted_envelope(value)
    if isinstance(value, ProposalEnvelope):
        value = value.model_dump(mode="python", round_trip=True, warnings=False)
    # Model adapters carry decoded JSON.  JSON arrays are necessarily Python
    # lists, whereas our immutable contracts intentionally expose tuples.  A
    # strict ``validate_python`` would therefore reject every ordinary wire
    # proposal before the envelope validators get a chance to evaluate it.
    # Re-enter through Pydantic's strict JSON path after bounded serialization:
    # JSON keeps tuple/list wire equivalence while still rejecting Python-side
    # coercions at the authority seam.
    _validate_serialized_envelope_size(value)
    try:
        wire_json = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
            default=lambda item: item.isoformat() if isinstance(item, datetime) else None,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("proposal envelope must be JSON-compatible") from exc
    return _PROPOSAL_INPUT_ADAPTER.validate_json(wire_json, strict=True)


__all__ = [
    "AppraisalSummary",
    "CanonicalTypedPayload",
    "CHANGE_TRANSITION_REGISTRY",
    "ContinuationProposal",
    "DecisionProposal",
    "ExpressionBeatDraft",
    "MinimalProposal",
    "PROPOSAL_SCHEMA_REGISTRY_VERSION",
    "PAYLOAD_MODEL_REGISTRY",
    "ProposalActionIntent",
    "ProposalEnvelope",
    "ProposalEvidenceRef",
    "ReferencedSummary",
    "TypedChange",
    "TypedObjectBinding",
    "VariationProfile",
    "validate_proposal_envelope",
]
