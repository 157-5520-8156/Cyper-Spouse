"""Pure, revision-pinned Context Capsule compilation.

The compiler deliberately performs no retrieval.  Its inputs are already-resolved,
typed projection slices from one world revision.  It only bounds, canonicalizes and
source-binds those inputs for deliberation; it does not infer behaviour or prose.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .context_resolver import (
    ContextCompileQuery,
    TrustedInternalContextResolver,
    context_query_hash,
    resolver_capability_is_valid,
)
from .memory_retrieval import MemoryRetrievalItem
from .schema_core import PrivacyClass
from .schemas import (
    AffectEpisodeProjection,
    AppraisalProjection,
    BudgetAccount,
    CapabilityStateProjection,
    CharacterCoreProjection,
    ExperienceProjection,
    FactProjection,
    MemoryCandidateProjection,
    PrivateImpressionProjection,
    RelationshipStateProjection,
    ThreadProjection,
)
from .situation_compiler import SituationProjection
from .world_life_context import WorldLifeContextItem
from .perception_result_context import PerceptionResultContextItem
from .recent_dialogue import RecentDialogueItem


T = TypeVar("T")
SliceName = Literal[
    "character_core",
    "current_situation",
    "recent_dialogue",
    "relationship_slice",
    "appraisals",
    "affect_episodes",
    "open_threads",
    "relevant_facts",
    "recent_experiences",
    "world_life",
    "perception_results",
    "active_memory_candidates",
    "available_capabilities",
    "action_budget",
    "private_impressions",
    "advisories",
]
TruncationReason = Literal[
    "item_budget",
    "field_budget",
    "character_budget",
    "source_envelope_budget",
    "global_character_budget",
]

MAX_INPUT_ITEMS_PER_SLICE = 256
MAX_RESOLVER_DOMAIN_SCAN_ITEMS = 4_096
MAX_SOURCE_REFS_PER_ITEM = 32
MAX_SOURCE_REF_CHARACTERS = 256
MAX_ITEM_SERIALIZED_CHARACTERS = 64_000
MAX_INPUT_SERIALIZED_CHARACTERS_PER_SLICE = 1_000_000


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FactRecallItem(_FrozenModel):
    """Model-facing Fact semantics closed over Fact + Observation authority.

    The persistent Fact deliberately retains an opaque value ref/hash.  This
    read model recovers no value by inference: it exposes only the exact text
    of the Observation which the accepted Fact assertion binds.
    """

    fact_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    predicate_code: str = Field(min_length=1, max_length=128)
    source_excerpt: str = Field(min_length=1, max_length=4_096)
    confidence_bp: int = Field(ge=1, le=10_000)
    privacy_class: PrivacyClass
    status: Literal["active"] = "active"
    committed_at: datetime
    updated_at: datetime
    accepted_fact_event_ref: str = Field(min_length=1)
    accepted_fact_world_revision: int = Field(ge=1)
    accepted_fact_payload_hash: str = Field(min_length=64, max_length=64)
    observation_event_ref: str = Field(min_length=1)
    observation_world_revision: int = Field(ge=1)
    observation_event_payload_hash: str = Field(min_length=64, max_length=64)
    source_observation_id: str = Field(min_length=1)
    assertion_payload_ref: str = Field(min_length=1)
    assertion_payload_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def authority_is_distinct_and_ordered(self) -> "FactRecallItem":
        for value in (
            self.accepted_fact_payload_hash,
            self.observation_event_payload_hash,
            self.assertion_payload_hash,
        ):
            _validate_hex_digest(value, label="Fact recall authority hash")
        if self.accepted_fact_event_ref == self.observation_event_ref:
            raise ValueError("Fact recall requires distinct Fact and Observation events")
        if self.observation_world_revision >= self.accepted_fact_world_revision:
            raise ValueError("Fact recall Observation must precede its accepted Fact")
        return self


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


RESOLVER_ID = "context-capsule-resolver"
RESOLVER_VERSION = "context-capsule-resolver.1"
RESOLUTION_POLICY_VERSION = "context-capsule-resolution-policy.1"
RANK_POLICY_VERSION = "context-capsule-rank-policy.1"
RANK_DOMAIN_IMPORTANCE_BP: dict[SliceName, int] = {
    "character_core": 10_000,
    "current_situation": 10_000,
    "recent_dialogue": 9_500,
    "relationship_slice": 8_500,
    "appraisals": 8_500,
    "affect_episodes": 8_500,
    "open_threads": 8_000,
    # Durable user facts must not be globally evicted behind generic old
    # dialogue/appraisal envelopes.  A real two-part recall probe otherwise
    # retained the name Fact but dropped the equally active preference Fact.
    "relevant_facts": 9_000,
    "recent_experiences": 7_000,
    "world_life": 7_250,
    "perception_results": 8_000,
    "active_memory_candidates": 7_500,
    "available_capabilities": 6_000,
    "action_budget": 6_000,
    "private_impressions": 8_000,
    "advisories": 5_000,
}
RANK_WEIGHT_BP = {"domain_importance": 4_000, "typed_signal": 4_000, "recency": 2_000}
RANK_RECENCY_WINDOW_SECONDS = 7 * 24 * 60 * 60
RANK_POLICY_DIGEST = _hash(
    {
        "policy_version": RANK_POLICY_VERSION,
        "arithmetic": "integer-basis-points",
        "signals": ("domain_importance", "intensity", "strength", "recency"),
        "domain_importance_bp": RANK_DOMAIN_IMPORTANCE_BP,
        "weights_bp": RANK_WEIGHT_BP,
        "recency_window_seconds": RANK_RECENCY_WINDOW_SECONDS,
        "tie_break": "item_ref_ascending",
    }
)
RESOLUTION_POLICY_DIGEST = _hash(
    {
        "resolver_id": RESOLVER_ID,
        "resolver_version": RESOLVER_VERSION,
        "policy_version": RESOLUTION_POLICY_VERSION,
        "rank_policy_version": RANK_POLICY_VERSION,
        "rank_policy_digest": RANK_POLICY_DIGEST,
        "max_selected_items_per_slice": MAX_INPUT_ITEMS_PER_SLICE,
        "max_domain_scan_items": MAX_RESOLVER_DOMAIN_SCAN_ITEMS,
    }
)
_COMPILER_AUTHORITY = object()
_COMPILER_PROVENANCE_VERSION = "context-capsule-compiler.1"


def _compiler_result_tag(result_hash: str) -> str:
    return _hash(
        {
            "compiler_version": _COMPILER_PROVENANCE_VERSION,
            "result_hash": result_hash,
        }
    )


def canonical_value_hash(value: BaseModel) -> str:
    """Digest the complete typed value; truncation never changes this authority hash."""

    return _hash(value.model_dump(mode="json"))


def _validate_hex_digest(value: str, *, label: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


class ResolvedSourceBinding(_FrozenModel):
    source_kind: Literal[
        "committed_event", "execution_receipt", "projection_snapshot", "immutable_payload"
    ]
    authority_type: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=MAX_SOURCE_REF_CHARACTERS)
    source_world_revision: int = Field(ge=0)
    immutable_hash: str = Field(min_length=64, max_length=64)

    @field_validator("immutable_hash")
    @classmethod
    def immutable_hash_is_hex(cls, value: str) -> str:
        return _validate_hex_digest(value, label="source binding immutable hash")


def source_bindings_hash(bindings: tuple[ResolvedSourceBinding, ...]) -> str:
    return _hash(tuple(item.model_dump(mode="json") for item in bindings))


class ResolverProof(_FrozenModel):
    resolver_id: Literal["context-capsule-resolver"]
    resolver_version: Literal["context-capsule-resolver.1"]
    policy_digest: str = Field(min_length=64, max_length=64)
    world_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    pinned_world_revision: int = Field(ge=0)
    slice_name: SliceName
    query_ref: str = Field(min_length=1, max_length=256)
    window_ref: str = Field(min_length=1, max_length=256)
    policy_version: Literal["context-capsule-resolution-policy.1"]
    completeness: Literal["complete"]
    privacy_floor: PrivacyClass | None = None
    explicit_authority_refs: tuple[str, ...] = Field(max_length=MAX_SOURCE_REFS_PER_ITEM)
    authority_refs_digest: str = Field(min_length=64, max_length=64)
    result_set_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def proof_digests_are_canonical(self) -> ResolverProof:
        for label, value in (
            ("resolver policy digest", self.policy_digest),
            ("resolver snapshot hash", self.snapshot_hash),
            ("resolver authority refs digest", self.authority_refs_digest),
            ("resolver result set hash", self.result_set_hash),
        ):
            _validate_hex_digest(value, label=label)
        if self.explicit_authority_refs != tuple(sorted(set(self.explicit_authority_refs))):
            raise ValueError("resolver explicit authority refs must be unique and sorted")
        if self.policy_digest != RESOLUTION_POLICY_DIGEST:
            raise ValueError("resolver policy digest is not installed")
        if self.authority_refs_digest != authority_refs_digest(self.explicit_authority_refs):
            raise ValueError("resolver authority refs digest is invalid")
        return self


class ResolvedItemMetadata(_FrozenModel):
    item_ref: str = Field(min_length=1, max_length=256)
    rank_score_bp: int = Field(ge=0, le=10_000)
    privacy_class: PrivacyClass
    source_bindings: tuple[ResolvedSourceBinding, ...] = Field(
        min_length=1, max_length=MAX_SOURCE_REFS_PER_ITEM
    )
    source_hash: str = Field(min_length=64, max_length=64)
    value_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def hashes_and_refs_are_exact(self) -> ResolvedItemMetadata:
        identities = tuple(
            (
                item.source_kind,
                item.authority_type,
                item.ref,
                item.source_world_revision,
                item.immutable_hash,
            )
            for item in self.source_bindings
        )
        if identities != tuple(sorted(set(identities))):
            raise ValueError("resolved item source bindings must be unique and sorted")
        _validate_hex_digest(self.source_hash, label="resolved item source hash")
        _validate_hex_digest(self.value_hash, label="resolved item value hash")
        if self.source_hash != source_bindings_hash(self.source_bindings):
            raise ValueError("resolved item source hash does not match source bindings")
        return self


def resolved_result_set_hash(
    slice_name: SliceName, metadata: tuple[ResolvedItemMetadata, ...]
) -> str:
    return _hash(
        {
            "slice_name": slice_name,
            "items": tuple(
                {
                    "item_ref": item.item_ref,
                    "value_hash": item.value_hash,
                    "source_bindings": tuple(
                        binding.model_dump(mode="json") for binding in item.source_bindings
                    ),
                    "rank_score_bp": item.rank_score_bp,
                    "privacy_class": item.privacy_class,
                }
                for item in sorted(metadata, key=lambda value: value.item_ref)
            ),
        }
    )


def authority_refs_digest(refs: tuple[str, ...]) -> str:
    return _hash({"authority_refs": refs})


class ResolvedSlice(_FrozenModel, Generic[T]):
    """Typed material plus the immutable authority that resolved it."""

    world_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    pinned_world_revision: int = Field(ge=0)
    value: T
    resolver_proof: ResolverProof
    item_metadata: tuple[ResolvedItemMetadata, ...] = Field(max_length=MAX_INPUT_ITEMS_PER_SLICE)

    @model_validator(mode="after")
    def source_identity_is_canonical(self) -> ResolvedSlice[T]:
        _validate_hex_digest(self.snapshot_hash, label="resolved slice snapshot hash")
        return self


class InnerAdvisoryCandidate(_FrozenModel):
    """One explicitly non-authoritative candidate retained for Deliberation."""

    candidate_ref: str = Field(min_length=1)
    value: str = Field(min_length=1, max_length=256)
    weight_bp: int = Field(ge=0, le=10_000)
    confidence_bp: int = Field(ge=0, le=10_000)


class InnerAdvisoryProjection(_FrozenModel):
    """Non-authoritative, source-bound candidate coordinates for one deliberation."""

    advisory_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    candidate_refs: tuple[str, ...] = Field(min_length=1)
    # ``candidate_refs`` alone made an advisory impossible for a model to use:
    # it conveyed opaque identities but none of the classifier's candidate
    # meaning.  The compact summaries remain read-only hints, never state.
    candidates: tuple[InnerAdvisoryCandidate, ...] = Field(default=(), max_length=8)
    confidence_bp: int = Field(ge=0, le=10_000)
    expiry: datetime
    producer_version: str = Field(min_length=1)

    @field_validator("expiry")
    @classmethod
    def expiry_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("advisory expiry must be timezone-aware")
        return value

    @model_validator(mode="after")
    def references_are_unique(self) -> InnerAdvisoryProjection:
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("advisory source refs must be unique")
        if len(self.candidate_refs) != len(set(self.candidate_refs)):
            raise ValueError("advisory candidate refs must be unique")
        if (
            self.candidates
            and tuple(item.candidate_ref for item in self.candidates) != self.candidate_refs
        ):
            raise ValueError("advisory candidate summaries must match candidate refs")
        return self


class SliceBudget(_FrozenModel):
    max_items: int = Field(default=8, ge=0)
    max_fields: int = Field(default=96, ge=0)
    max_characters: int = Field(default=4_000, ge=0)


class ContextCapsuleBudgetPolicy(_FrozenModel):
    """Independent caps prevent one verbose domain from consuming every slice."""

    hard_max_characters: int = Field(default=32_000, ge=0)
    character_core: SliceBudget = Field(
        default_factory=lambda: SliceBudget(max_items=1, max_fields=96, max_characters=6_000)
    )
    current_situation: SliceBudget = Field(
        default_factory=lambda: SliceBudget(max_items=1, max_fields=96, max_characters=12_000)
    )
    recent_dialogue: SliceBudget = Field(
        # Short-horizon dialogue is deliberately smaller than durable Fact /
        # Memory context. Eight verified utterances preserve local reference
        # and tone without allowing duplicated delivery provenance to evict a
        # two-part long-term recall under the global prompt cap.
        default_factory=lambda: SliceBudget(max_items=8, max_fields=96, max_characters=10_000)
    )
    relationship_slice: SliceBudget = Field(
        # A current relationship head includes hysteresis and exact accepted
        # source bindings.  Two thousand characters can reject that single
        # item wholesale, which makes accepted relationship changes invisible
        # to ordinary chat.  Keep exactly one bounded head with enough room for
        # its complete authority envelope.
        default_factory=lambda: SliceBudget(max_items=1, max_fields=48, max_characters=4_000)
    )
    appraisals: SliceBudget = Field(
        # A mixed same-turn appraisal carries several weighted hypotheses plus
        # exact Observation and accepted-event authority.  The generic 4k
        # envelope can drop that one fresh item wholesale.
        default_factory=lambda: SliceBudget(max_items=8, max_fields=96, max_characters=6_500)
    )
    # An accepted episode carries its appraisal hypotheses and immutable
    # provenance.  Four thousand characters can reject the entire highest
    # priority episode, leaving the next deliberation affect-blind.  Reserve
    # enough room for one fully source-bound episode, including a merged
    # multi-stimulus lineage; the global capsule cap and per-slice selection
    # still bound total context.
    affect_episodes: SliceBudget = Field(
        default_factory=lambda: SliceBudget(max_items=8, max_fields=96, max_characters=16_000)
    )
    open_threads: SliceBudget = Field(
        default_factory=lambda: SliceBudget(max_items=12, max_fields=144, max_characters=4_000)
    )
    relevant_facts: SliceBudget = Field(
        # A verified FactRecall item deliberately carries both the accepted
        # Fact and source Observation authority.  At 4k, that proof envelope
        # retained only one of two active facts in the real 32-turn journey:
        # the user's name survived while their drink preference vanished.
        # Reserve enough for at least two independently sourced recalls; the
        # item/field/global caps still bound the lane and provider-facing
        # compaction removes the cryptographic proof noise.
        default_factory=lambda: SliceBudget(max_items=16, max_fields=192, max_characters=12_000)
    )
    recent_experiences: SliceBudget = Field(default_factory=SliceBudget)
    world_life: SliceBudget = Field(default_factory=SliceBudget)
    perception_results: SliceBudget = Field(
        default_factory=lambda: SliceBudget(max_items=4, max_fields=72, max_characters=4_000)
    )
    active_memory_candidates: SliceBudget = Field(
        # Two compact source excerpts are the minimum useful cross-turn recall
        # unit for questions that join identity and preference/history.  The
        # former 4k default frequently retained only one otherwise-active
        # memory, making an explicit two-part recall question look forgotten.
        default_factory=lambda: SliceBudget(max_items=8, max_fields=128, max_characters=8_000)
    )
    available_capabilities: SliceBudget = Field(
        default_factory=lambda: SliceBudget(max_items=8, max_fields=96, max_characters=2_000)
    )
    action_budget: SliceBudget = Field(
        default_factory=lambda: SliceBudget(max_items=8, max_fields=80, max_characters=2_000)
    )
    private_impressions: SliceBudget = Field(default_factory=SliceBudget)
    advisories: SliceBudget = Field(
        # One source-bound advisory item includes resolver metadata as well as
        # its compact alternatives.  The former 3k cap routinely retained
        # only the first classification, making user-affect/thread/interrupt
        # alternatives disappear before the main model could weigh them.
        # Eight kilobytes fits the complete bounded semantic matrix while the
        # capsule-wide 32k hard cap still limits total prompt growth.
        default_factory=lambda: SliceBudget(max_items=12, max_fields=96, max_characters=8_000)
    )


class ContextCapsuleRequest(_FrozenModel):
    world_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    actor_ref: str = Field(min_length=1, max_length=256)
    consumer_scope: Literal["deliberation_internal"]
    trigger_ref: str = Field(min_length=1)
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    logical_time: datetime | None = None
    relationship_evaluation_requested: bool = False
    situation: ResolvedSlice[SituationProjection]
    recent_dialogue: ResolvedSlice[tuple[RecentDialogueItem, ...]] | None = None
    character_core: ResolvedSlice[CharacterCoreProjection] | None = None
    relationship_slice: ResolvedSlice[RelationshipStateProjection] | None = None
    appraisals: ResolvedSlice[tuple[AppraisalProjection, ...]] | None = None
    affect_episodes: ResolvedSlice[tuple[AffectEpisodeProjection, ...]] | None = None
    open_threads: ResolvedSlice[tuple[ThreadProjection, ...]] | None = None
    relevant_facts: ResolvedSlice[tuple[FactProjection | FactRecallItem, ...]] | None = None
    recent_experiences: ResolvedSlice[tuple[ExperienceProjection, ...]] | None = None
    world_life: ResolvedSlice[tuple[WorldLifeContextItem, ...]] | None = None
    perception_results: ResolvedSlice[tuple[PerceptionResultContextItem, ...]] | None = None
    active_memory_candidates: (
        ResolvedSlice[tuple[MemoryCandidateProjection | MemoryRetrievalItem, ...]] | None
    ) = None
    available_capabilities: ResolvedSlice[tuple[CapabilityStateProjection, ...]] | None = None
    action_budget: ResolvedSlice[tuple[BudgetAccount, ...]] | None = None
    private_impressions: ResolvedSlice[tuple[PrivateImpressionProjection, ...]] | None = None
    advisories: ResolvedSlice[tuple[InnerAdvisoryProjection, ...]] | None = None

    @field_validator("logical_time")
    @classmethod
    def logical_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Context Capsule logical time must be timezone-aware")
        return value


class TruncationEntry(_FrozenModel):
    slice_name: SliceName
    reason: TruncationReason
    omitted_count: int = Field(ge=1)


class SliceBudgetUsage(_FrozenModel):
    max_items: int = Field(ge=0)
    max_fields: int = Field(ge=0)
    max_characters: int = Field(ge=0)
    used_items: int = Field(ge=0)
    used_fields: int = Field(ge=0)
    used_characters: int = Field(ge=0)

    @model_validator(mode="after")
    def usage_does_not_exceed_caps(self) -> SliceBudgetUsage:
        if (
            self.used_items > self.max_items
            or self.used_fields > self.max_fields
            or self.used_characters > self.max_characters
        ):
            raise ValueError("Context Capsule slice usage exceeds its budget")
        return self


class CapsuleItem(_FrozenModel):
    item_ref: str = Field(min_length=1)
    rank_score_bp: int = Field(ge=0, le=10_000)
    privacy_class: PrivacyClass
    source_bindings: tuple[ResolvedSourceBinding, ...] = Field(min_length=1)
    source_hash: str = Field(min_length=64, max_length=64)
    value_hash: str = Field(min_length=64, max_length=64)
    included_fields: tuple[str, ...]
    payload_json: str
    character_count: int = Field(ge=0)

    @model_validator(mode="after")
    def payload_accounting_is_exact(self) -> CapsuleItem:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("capsule item payload must be an object")
        if self.payload_json != _canonical_json(decoded):
            raise ValueError("capsule item payload must be canonical JSON")
        if self.character_count != len(self.payload_json):
            raise ValueError("capsule item character accounting is inconsistent")
        if self.included_fields != tuple(sorted(decoded)):
            raise ValueError("capsule included fields do not match payload")
        if self.source_hash != source_bindings_hash(self.source_bindings):
            raise ValueError("capsule item source hash does not match source bindings")
        if self.value_hash != _hash(decoded):
            raise ValueError("capsule item value hash does not match payload")
        return self


class CapsuleSlice(_FrozenModel):
    availability: Literal["available", "unavailable"]
    unavailable_reason: Literal["authority_unavailable"] | None = None
    pinned_world_revision: int | None = Field(default=None, ge=0)
    source_refs: tuple[str, ...] = ()
    source_hash: str | None = Field(default=None, min_length=64, max_length=64)
    resolver_proof: ResolverProof | None = None
    slice_hash: str | None = Field(default=None, min_length=64, max_length=64)
    items: tuple[CapsuleItem, ...] = ()
    model_content_json: str
    budget: SliceBudgetUsage
    truncated: bool = False

    @model_validator(mode="after")
    def availability_has_explicit_authority(self) -> CapsuleSlice:
        if self.availability == "unavailable":
            if self.unavailable_reason != "authority_unavailable":
                raise ValueError("unavailable Capsule slice requires a reason")
            if (
                self.source_refs
                or self.source_hash is not None
                or self.slice_hash is not None
                or self.items
                or self.pinned_world_revision is not None
                or self.resolver_proof is not None
            ):
                raise ValueError("unavailable Capsule slice cannot claim authority")
        elif (
            self.unavailable_reason is not None
            or self.pinned_world_revision is None
            or self.source_hash is None
            or self.slice_hash is None
            or self.resolver_proof is None
        ):
            raise ValueError("available Capsule slice requires complete source authority")
        if self.availability == "available":
            expected = _hash(
                {
                    "pinned_world_revision": self.pinned_world_revision,
                    "source_refs": self.source_refs,
                    "source_hash": self.source_hash,
                    "resolver_proof": self.resolver_proof.model_dump(mode="json"),
                    "items": tuple(item.model_dump(mode="json") for item in self.items),
                }
            )
            if self.slice_hash != expected:
                raise ValueError("Context Capsule slice hash is invalid")
        decoded = json.loads(self.model_content_json)
        if self.model_content_json != _canonical_json(decoded):
            raise ValueError("Capsule slice model content must be canonical JSON")
        if self.budget.used_characters != len(self.model_content_json):
            raise ValueError("Capsule slice model content budget is inconsistent")
        return self


class ContextBudgetAudit(_FrozenModel):
    hard_max_characters: int = Field(ge=0)
    used_characters: int = Field(ge=0)
    slice_content_characters: int = Field(ge=0)
    framing_characters: int = Field(ge=0)
    used_by_slice: tuple[tuple[SliceName, int], ...]
    truncation_log: tuple[TruncationEntry, ...]

    @model_validator(mode="after")
    def totals_are_exact_and_bounded(self) -> ContextBudgetAudit:
        if len(self.used_by_slice) != len({name for name, _ in self.used_by_slice}):
            raise ValueError("Context Capsule slice budget identities must be unique")
        if self.slice_content_characters != sum(value for _, value in self.used_by_slice):
            raise ValueError("Context Capsule slice character total is inconsistent")
        if self.used_characters != self.slice_content_characters + self.framing_characters:
            raise ValueError("Context Capsule character total does not include framing")
        if self.used_characters > self.hard_max_characters:
            raise ValueError("Context Capsule exceeds its global character budget")
        return self


class RelationshipEvaluationSource(_FrozenModel):
    """Compact, source-bound authority descriptor for the relationship lane."""

    item_ref: str = Field(min_length=1)
    source_bindings: tuple[ResolvedSourceBinding, ...] = Field(min_length=1)
    source_hash: str = Field(min_length=64, max_length=64)
    value_hash: str = Field(min_length=64, max_length=64)


class RelationshipEvaluationContext(_FrozenModel):
    """Bounded relationship/appraisal view for a post-appraisal deliberation.

    This is deliberately not a second relationship authority.  It is a compact
    model-facing view whose values and source descriptors are derived from the
    same pinned resolver output as the ordinary slices.  In particular, it
    keeps the exact triggering appraisal available even when accumulated
    generic slices cannot fit their whole-item envelopes.
    """

    subject_ref: str = Field(min_length=1)
    trigger_appraisal_id: str = Field(min_length=1)
    appraisal_summary_json: str = Field(min_length=2)
    relationship_summary_json: str = Field(min_length=2)
    appraisal_source: RelationshipEvaluationSource
    relationship_source: RelationshipEvaluationSource | None = None

    @field_validator("appraisal_summary_json", "relationship_summary_json")
    @classmethod
    def summaries_are_canonical_objects(cls, value: str) -> str:
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("relationship evaluation summary is invalid JSON") from exc
        if not isinstance(decoded, dict) or _canonical_json(decoded) != value:
            raise ValueError("relationship evaluation summary must be canonical JSON object")
        return value


class ContextCapsule(_FrozenModel):
    capsule_id: str = Field(min_length=64, max_length=64)
    provenance_kind: Literal["trusted_resolver_compiled", "test_only_untrusted"]
    compiler_result_hash: str = Field(min_length=64, max_length=64)
    compiler_result_tag: str | None = Field(default=None, min_length=64, max_length=64)
    world_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    actor_ref: str = Field(min_length=1)
    consumer_scope: Literal["deliberation_internal"]
    trigger_ref: str = Field(min_length=1)
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    logical_time: datetime | None
    character_core: CapsuleSlice
    current_situation: CapsuleSlice
    recent_dialogue: CapsuleSlice
    relationship_slice: CapsuleSlice
    appraisals: CapsuleSlice
    affect_episodes: CapsuleSlice
    open_threads: CapsuleSlice
    relevant_facts: CapsuleSlice
    recent_experiences: CapsuleSlice
    world_life: CapsuleSlice
    perception_results: CapsuleSlice | None = None
    active_memory_candidates: CapsuleSlice
    available_capabilities: CapsuleSlice
    action_budget: CapsuleSlice
    private_impressions: CapsuleSlice
    advisories: CapsuleSlice
    relationship_evaluation: RelationshipEvaluationContext | None = None
    model_content_json: str
    budget: ContextBudgetAudit

    @model_validator(mode="after")
    def capsule_identity_binds_complete_output(self) -> ContextCapsule:
        material = {
            "provenance_kind": self.provenance_kind,
            "compiler_result_hash": self.compiler_result_hash,
            "compiler_result_tag": self.compiler_result_tag,
            "world_id": self.world_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "actor_ref": self.actor_ref,
            "consumer_scope": self.consumer_scope,
            "trigger_ref": self.trigger_ref,
            "world_revision": self.world_revision,
            "deliberation_revision": self.deliberation_revision,
            "ledger_sequence": self.ledger_sequence,
            "logical_time": self.logical_time.isoformat() if self.logical_time else None,
            "character_core": self.character_core.model_dump(mode="json"),
            "current_situation": self.current_situation.model_dump(mode="json"),
            "recent_dialogue": self.recent_dialogue.model_dump(mode="json"),
            "relationship_slice": self.relationship_slice.model_dump(mode="json"),
            "appraisals": self.appraisals.model_dump(mode="json"),
            "affect_episodes": self.affect_episodes.model_dump(mode="json"),
            "open_threads": self.open_threads.model_dump(mode="json"),
            "relevant_facts": self.relevant_facts.model_dump(mode="json"),
            "recent_experiences": self.recent_experiences.model_dump(mode="json"),
            "world_life": self.world_life.model_dump(mode="json"),
            "active_memory_candidates": self.active_memory_candidates.model_dump(mode="json"),
            "available_capabilities": self.available_capabilities.model_dump(mode="json"),
            "action_budget": self.action_budget.model_dump(mode="json"),
            "private_impressions": self.private_impressions.model_dump(mode="json"),
            "advisories": self.advisories.model_dump(mode="json"),
            "model_content_json": self.model_content_json,
            "budget": self.budget.model_dump(mode="json"),
        }
        if self.relationship_evaluation is not None:
            material["relationship_evaluation"] = self.relationship_evaluation.model_dump(
                mode="json"
            )
        if self.perception_results is not None:
            material["perception_results"] = self.perception_results.model_dump(mode="json")
        result_material = dict(material)
        for field in ("provenance_kind", "compiler_result_hash", "compiler_result_tag"):
            result_material.pop(field)
        if self.compiler_result_hash != _hash(result_material):
            raise ValueError("Context Capsule compiler result hash is invalid")
        if self.capsule_id != _hash(material):
            raise ValueError("Context Capsule identity is invalid")
        if self.provenance_kind == "trusted_resolver_compiled":
            if self.compiler_result_tag != _compiler_result_tag(self.compiler_result_hash):
                raise ValueError("trusted Context Capsule compiler tag is invalid")
        elif self.compiler_result_tag is not None:
            raise ValueError("test-only Context Capsule cannot claim a compiler tag")
        decoded = json.loads(self.model_content_json)
        if self.model_content_json != _canonical_json(decoded):
            raise ValueError("Context Capsule model content must be canonical JSON")
        if self.budget.used_characters != len(self.model_content_json):
            raise ValueError("Context Capsule model content budget is inconsistent")
        return self


_ITEM_IDS: dict[SliceName, str] = {
    "character_core": "core_id",
    "current_situation": "actor_ref",
    "recent_dialogue": "dialogue_id",
    "relationship_slice": "relationship_id",
    "appraisals": "appraisal_id",
    "affect_episodes": "episode_id",
    "open_threads": "thread_id",
    "relevant_facts": "fact_id",
    "recent_experiences": "experience_id",
    "world_life": "occurrence_id",
    "perception_results": "result_id",
    "active_memory_candidates": "candidate_id",
    "available_capabilities": "grant_id",
    "action_budget": "account_id",
    "private_impressions": "impression_id",
    "advisories": "advisory_id",
}


def _values(bound: ResolvedSlice[object]) -> tuple[BaseModel, ...]:
    value = bound.value
    if isinstance(value, tuple):
        return value
    return (value,)


def _identity(slice_name: SliceName, item: BaseModel) -> str:
    field = _ITEM_IDS[slice_name]
    identity = getattr(item, field)
    if slice_name == "action_budget":
        identity = f"{identity}:{getattr(item, 'window_id')}"
    return str(identity)


def _empty_usage(limit: SliceBudget, *, used_characters: int) -> SliceBudgetUsage:
    return SliceBudgetUsage(
        max_items=limit.max_items,
        max_fields=limit.max_fields,
        max_characters=limit.max_characters,
        used_items=0,
        used_fields=0,
        used_characters=used_characters,
    )


def _unavailable(limit: SliceBudget) -> CapsuleSlice:
    content = _canonical_json({"availability": "unavailable"})
    if len(content) > limit.max_characters:
        raise ValueError("slice budget cannot represent explicit unavailable authority")
    return CapsuleSlice(
        availability="unavailable",
        unavailable_reason="authority_unavailable",
        model_content_json=content,
        budget=_empty_usage(limit, used_characters=len(content)),
    )


_PRIVACY_RANK: dict[PrivacyClass, int] = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def _strictest_privacy(values: tuple[PrivacyClass, ...]) -> PrivacyClass | None:
    return max(values, key=_PRIVACY_RANK.__getitem__) if values else None


def _derived_privacy_floor(slice_name: SliceName, item: BaseModel) -> PrivacyClass | None:
    conservative: dict[SliceName, PrivacyClass] = {
        "character_core": "withhold",
        "current_situation": "private",
        "recent_dialogue": "private",
        "relationship_slice": "private",
        "appraisals": "private",
        "affect_episodes": "private",
        "open_threads": "private",
        "relevant_facts": "personal",
        "recent_experiences": "personal",
        "world_life": "personal",
        "perception_results": "private",
        "active_memory_candidates": "personal",
        "available_capabilities": "private",
        "action_budget": "withhold",
        "private_impressions": "withhold",
        "advisories": "private",
    }
    typed: list[PrivacyClass] = [conservative[slice_name]]
    if slice_name == "current_situation":
        situation = item
        scene_visibility = getattr(situation, "scene_visibility", None)
        if scene_visibility in {"public", "shareable", "private"}:
            typed.append(scene_visibility)
        for field in (
            "location_slice",
            "resource_pressure",
            "attention_slice",
            "social_environment",
            "plan_relation",
        ):
            privacy = getattr(getattr(situation, field), "privacy_class", None)
            if privacy is not None:
                typed.append(privacy)
        for field in (
            "activity_slices",
            "goal_slices",
            "resource_slices",
            "commitment_slices",
        ):
            typed.extend(
                privacy
                for child in getattr(situation, field)
                if (privacy := getattr(child, "privacy_class", None)) is not None
            )
        return _strictest_privacy(tuple(typed))
    if slice_name == "affect_episodes":
        typed.append(item.privacy_class)
    if slice_name in {"open_threads", "recent_experiences"}:
        typed.append(item.values.privacy_class)
    if slice_name == "relevant_facts":
        typed.append(
            item.privacy_class if isinstance(item, FactRecallItem) else item.values.privacy_class
        )
    if slice_name == "active_memory_candidates":
        typed.append(
            getattr(getattr(item, "values", None), "privacy_ceiling", None)
            or getattr(item, "privacy_ceiling")
        )
    return _strictest_privacy(tuple(typed))


def _typed_source_refs(slice_name: SliceName, item: BaseModel) -> tuple[str, ...] | None:
    if slice_name == "current_situation":
        refs = tuple(source.event_ref for source in item.source_revisions)
        return tuple(sorted(set(refs))) or None
    if slice_name == "recent_dialogue" and isinstance(item, RecentDialogueItem):
        return tuple(sorted(claim.authority_event_ref for claim in item.source_claims))
    if slice_name == "private_impressions":
        origin = getattr(item, "origin", None)
        refs = set(item.source_refs)
        if origin is not None and origin.accepted_event_ref:
            refs.add(origin.accepted_event_ref)
        return tuple(sorted(refs)) or None
    if slice_name == "advisories":
        return tuple(sorted(set(item.source_refs)))
    if slice_name == "world_life" and isinstance(item, WorldLifeContextItem):
        refs = {item.source.authority_event_ref}
        if item.content is not None:
            refs.add(item.content.descriptor_event_ref)
        return tuple(sorted(refs))
    if slice_name == "perception_results" and isinstance(item, PerceptionResultContextItem):
        return tuple(sorted({item.source.result_event_ref, item.source.receipt_event_ref}))
    if slice_name == "relevant_facts" and isinstance(item, FactProjection):
        # The accepted Fact event is the whole immutable authority for the
        # projection.  Its observation id stays an internal Fact anchor.
        return (item.origin.accepted_event_ref,)
    if slice_name == "relevant_facts" and isinstance(item, FactRecallItem):
        return tuple(sorted((item.accepted_fact_event_ref, item.observation_event_ref)))
    if slice_name == "active_memory_candidates" and isinstance(item, MemoryRetrievalItem):
        return tuple(sorted({source.authority_event_ref for source in item.source_excerpts}))

    refs: set[str] = set()
    origin = getattr(item, "origin", None)
    for field in ("accepted_event_ref", "event_ref"):
        value = getattr(origin, field, None)
        if value:
            refs.add(value)
    values = getattr(item, "values", None)
    for evidence in getattr(values, "source_evidence_refs", ()):
        refs.add(evidence.ref_id)
    for binding in getattr(values, "source_bindings", ()):
        value = getattr(binding, "authority_event_ref", None) or getattr(
            binding, "receipt_id", None
        )
        if value:
            refs.add(value)
    for evidence in getattr(item, "evidence_refs", ()):
        refs.add(evidence.ref_id)
    return tuple(sorted(refs)) or None


def _typed_source_authorities(item: BaseModel) -> tuple[tuple[str, str, int, str], ...]:
    if isinstance(item, RecentDialogueItem):
        return tuple(
            (
                "committed_event",
                claim.authority_event_ref,
                claim.authority_world_revision,
                claim.authority_payload_hash,
            )
            for claim in item.source_claims
        )
    if isinstance(item, FactProjection):
        # The Fact reducer validates its immutable evidence closure.  Context
        # pins the resulting accepted Fact event, while the retained evidence
        # uses durable observation identities rather than committed event ids.
        return ()
    if isinstance(item, FactRecallItem):
        return tuple(
            sorted(
                (
                    (
                        "committed_event",
                        item.accepted_fact_event_ref,
                        item.accepted_fact_world_revision,
                        item.accepted_fact_payload_hash,
                    ),
                    (
                        "committed_event",
                        item.observation_event_ref,
                        item.observation_world_revision,
                        item.observation_event_payload_hash,
                    ),
                )
            )
        )
    if isinstance(item, WorldLifeContextItem):
        authorities = {
            (
                "committed_event",
                item.source.authority_event_ref,
                item.source.authority_world_revision,
                item.source.authority_payload_hash,
            )
        }
        if item.content is not None:
            authorities.add(
                (
                    "committed_event",
                    item.content.descriptor_event_ref,
                    item.content.descriptor_world_revision,
                    item.content.descriptor_payload_hash,
                )
            )
        return tuple(sorted(authorities))
    if isinstance(item, PerceptionResultContextItem):
        return (
            (
                "committed_event",
                item.source.receipt_event_ref,
                item.source.receipt_world_revision,
                item.source.receipt_payload_hash,
            ),
            (
                "committed_event",
                item.source.result_event_ref,
                item.source.result_world_revision,
                item.source.result_payload_hash,
            ),
        )
    authorities: set[tuple[str, str, int, str]] = set()
    values = getattr(item, "values", None)
    for evidence in (
        *getattr(values, "source_evidence_refs", ()),
        *getattr(item, "evidence_refs", ()),
    ):
        authorities.add(
            (
                "committed_event",
                evidence.ref_id,
                evidence.source_world_revision,
                evidence.immutable_hash,
            )
        )
    for binding in getattr(values, "source_bindings", ()):
        ref = getattr(binding, "authority_event_ref", None)
        revision = getattr(binding, "authority_world_revision", None)
        immutable_hash = getattr(binding, "authority_payload_hash", None)
        if ref is not None and revision is not None and immutable_hash is not None:
            authorities.add(("committed_event", ref, revision, immutable_hash))
    if isinstance(item, MemoryRetrievalItem):
        for source in item.source_excerpts:
            authorities.add(
                (
                    "committed_event",
                    source.authority_event_ref,
                    source.authority_world_revision,
                    source.authority_payload_hash,
                )
            )
    return tuple(sorted(authorities))


def _typed_source_hashes(item: BaseModel) -> tuple[tuple[str, str, str], ...]:
    values = getattr(item, "values", None)
    hashes: set[tuple[str, str, str]] = set()
    for binding in getattr(values, "source_bindings", ()):
        ref = getattr(binding, "receipt_id", None)
        immutable_hash = getattr(binding, "receipt_hash", None)
        if ref is not None and immutable_hash is not None:
            hashes.add(("execution_receipt", ref, immutable_hash))
    return tuple(sorted(hashes))


def _slice_model_content(
    *,
    slice_name: SliceName,
    source_refs: tuple[str, ...],
    source_hash: str,
    resolver_proof: ResolverProof,
    items: tuple[CapsuleItem, ...],
    model_content_profile: Literal["general", "proactive_decision"] = "general",
) -> str:
    def model_item(item: CapsuleItem) -> dict[str, object]:
        value = json.loads(item.payload_json)
        material: dict[str, object] = {
            "item_ref": item.item_ref,
            "rank_score_bp": item.rank_score_bp,
            "privacy_class": item.privacy_class,
            "source_bindings": tuple(
                binding.model_dump(mode="json") for binding in item.source_bindings
            ),
            "source_hash": item.source_hash,
            "value_hash": item.value_hash,
            "value": value,
        }
        if slice_name == "recent_dialogue":
            # The full cryptographic closure remains on ``CapsuleItem`` and in
            # the resolver proof. Repeating every acceptance/payload/receipt
            # hash inside the model prompt made four delivered lines cost more
            # context than the dialogue itself and could evict two active
            # memories. The model needs the verified text and stable source
            # identities, not duplicate hash material.
            material.pop("source_bindings")
            if isinstance(value, dict):
                material["value"] = {
                    key: field_value
                    for key, field_value in value.items()
                    if key not in {"source_claims", "sidecar_ref", "sidecar_hash"}
                }
        if model_content_profile == "proactive_decision" and slice_name in {
            "character_core",
            "current_situation",
        }:
            # The trusted CapsuleItem below remains a complete whole item with
            # its exact authority closure.  This request-specific model view
            # removes only duplicated proof bookkeeping, never semantic state.
            material.pop("source_bindings")
            material.pop("source_hash")
            material.pop("value_hash")
            if slice_name == "current_situation" and isinstance(value, dict):
                material["value"] = {
                    key: field_value
                    for key, field_value in value.items()
                    if key
                    not in {
                        "world_id",
                        "authority_snapshot_hash",
                        "situation_policy_input_hash",
                        "compiled_at_world_revision",
                        "actor_ref",
                        "source_revisions",
                        "policy_versions",
                        "internal_semantic_hash",
                    }
                }
        return material

    content: dict[str, object] = {
        "availability": "available",
        "source_refs": source_refs,
        "source_hash": source_hash,
        "resolver_proof": resolver_proof.model_dump(mode="json"),
        "items": tuple(model_item(item) for item in items),
    }
    if slice_name == "recent_dialogue":
        # The exact refs remain in CapsuleSlice.source_refs and CapsuleItem;
        # the model-facing packet only needs proof that the verified authority
        # set is fixed. Long provider-generated ids are otherwise repeated at
        # the slice and item levels despite carrying no conversational meaning.
        content["source_ref_count"] = len(source_refs)
        content.pop("source_refs")
    return _canonical_json(content)


def _make_available_slice(
    *,
    slice_name: SliceName,
    bound: ResolvedSlice[object],
    limit: SliceBudget,
    items: tuple[CapsuleItem, ...],
    source_refs: tuple[str, ...],
    source_hash: str,
    truncated: bool,
    model_content_profile: Literal["general", "proactive_decision"] = "general",
) -> CapsuleSlice:
    content = _slice_model_content(
        slice_name=slice_name,
        source_refs=source_refs,
        source_hash=source_hash,
        resolver_proof=bound.resolver_proof,
        items=items,
        model_content_profile=model_content_profile,
    )
    fields = sum(len(item.included_fields) for item in items)
    material = {
        "pinned_world_revision": bound.pinned_world_revision,
        "source_refs": source_refs,
        "source_hash": source_hash,
        "resolver_proof": bound.resolver_proof.model_dump(mode="json"),
        "items": tuple(item.model_dump(mode="json") for item in items),
    }
    return CapsuleSlice(
        availability="available",
        pinned_world_revision=bound.pinned_world_revision,
        source_refs=source_refs,
        source_hash=source_hash,
        resolver_proof=bound.resolver_proof,
        slice_hash=_hash(material),
        items=items,
        model_content_json=content,
        budget=SliceBudgetUsage(
            max_items=limit.max_items,
            max_fields=limit.max_fields,
            max_characters=limit.max_characters,
            used_items=len(items),
            used_fields=fields,
            used_characters=len(content),
        ),
        truncated=truncated,
    )


def _compile_slice(
    *,
    slice_name: SliceName,
    bound: ResolvedSlice[object] | None,
    limit: SliceBudget,
    model_content_profile: Literal["general", "proactive_decision"] = "general",
) -> tuple[CapsuleSlice, tuple[TruncationEntry, ...]]:
    if bound is None:
        return _unavailable(limit), ()

    values = _values(bound)
    if len(values) > MAX_INPUT_ITEMS_PER_SLICE:
        raise ValueError(f"{slice_name} exceeds the resolved input item limit")
    if len(values) != len(bound.item_metadata):
        raise ValueError(f"{slice_name} item metadata does not cover every typed value")
    identities = tuple(_identity(slice_name, item) for item in values)
    if len(identities) != len(set(identities)):
        raise ValueError(f"{slice_name} contains duplicate item identities")
    values_by_id = dict(zip(identities, values, strict=True))
    metadata_by_id = {item.item_ref: item for item in bound.item_metadata}
    if len(metadata_by_id) != len(bound.item_metadata) or set(metadata_by_id) != set(identities):
        raise ValueError(f"{slice_name} metadata identities do not match typed values")
    resolved: list[tuple[BaseModel, ResolvedItemMetadata]] = []
    input_characters = 0
    for item_ref, item in values_by_id.items():
        metadata = metadata_by_id[item_ref]
        material = item.model_dump(mode="json")
        encoded = _canonical_json(material)
        if len(encoded) > MAX_ITEM_SERIALIZED_CHARACTERS:
            raise ValueError(f"{slice_name} contains an oversized typed value")
        input_characters += len(encoded)
        if input_characters > MAX_INPUT_SERIALIZED_CHARACTERS_PER_SLICE:
            raise ValueError(f"{slice_name} exceeds the serialized input limit")
        if metadata.value_hash != _hash(material):
            raise ValueError(f"{slice_name} value hash does not match typed authority")
        if any(
            binding.source_world_revision > bound.pinned_world_revision
            for binding in metadata.source_bindings
        ):
            raise ValueError(f"{slice_name} source binding is newer than the pinned snapshot")
        derived_floor = _derived_privacy_floor(slice_name, item)
        proof_floor = bound.resolver_proof.privacy_floor
        if derived_floor is None and proof_floor is None:
            raise ValueError(f"{slice_name} requires a resolver-proven privacy floor")
        effective_floor = _strictest_privacy(
            tuple(value for value in (derived_floor, proof_floor) if value is not None)
        )
        if effective_floor is None or (
            _PRIVACY_RANK[metadata.privacy_class] < _PRIVACY_RANK[effective_floor]
        ):
            raise ValueError(f"{slice_name} metadata privacy downgrades typed authority")
        resolved.append((item, metadata))
    # A proactive opportunity is not merely another classifier hint: it is
    # the exact semantic subject of the proactive lane's model request.  Keep
    # it ahead of optional advisories so both the per-slice cap and the global
    # tail-eviction policy preserve its source-bound contract.
    resolved.sort(
        key=lambda pair: (
            0
            if slice_name == "advisories"
            and isinstance(pair[0], InnerAdvisoryProjection)
            and pair[0].kind == "proactive_opportunity"
            else 1,
            -pair[1].rank_score_bp,
            pair[1].item_ref,
        )
    )
    eligible = resolved
    log: list[TruncationEntry] = []
    over_item_limit = eligible[limit.max_items :]
    eligible = eligible[: limit.max_items]
    if over_item_limit:
        log.append(
            TruncationEntry(
                slice_name=slice_name,
                reason="item_budget",
                omitted_count=len(over_item_limit),
            )
        )
    remaining_fields = limit.max_fields
    output: list[CapsuleItem] = []
    omitted_fields = 0
    omitted_characters = 0
    for item, metadata in eligible:
        item_ref = metadata.item_ref
        material = item.model_dump(mode="json")
        if len(material) > remaining_fields:
            omitted_fields += 1
            continue
        candidate = CapsuleItem(
            item_ref=item_ref,
            rank_score_bp=metadata.rank_score_bp,
            privacy_class=metadata.privacy_class,
            source_bindings=metadata.source_bindings,
            source_hash=metadata.source_hash,
            value_hash=metadata.value_hash,
            included_fields=tuple(sorted(material)),
            payload_json=_canonical_json(material),
            character_count=len(_canonical_json(material)),
        )
        selected_metadata = tuple(metadata_by_id[item.item_ref] for item in (*output, candidate))
        selected_refs = tuple(
            sorted({binding.ref for meta in selected_metadata for binding in meta.source_bindings})
        )
        selected_hash = _hash(
            {
                "snapshot_hash": bound.snapshot_hash,
                "resolver_proof": bound.resolver_proof.model_dump(mode="json"),
                "item_source_hashes": tuple(meta.source_hash for meta in selected_metadata),
            }
        )
        candidate_content = _slice_model_content(
            slice_name=slice_name,
            source_refs=selected_refs,
            source_hash=selected_hash,
            resolver_proof=bound.resolver_proof,
            items=(*output, candidate),
            model_content_profile=model_content_profile,
        )
        if len(candidate_content) > limit.max_characters:
            omitted_characters += 1
            continue
        output.append(candidate)
        remaining_fields -= len(material)
    if omitted_fields:
        log.append(
            TruncationEntry(
                slice_name=slice_name,
                reason="field_budget",
                omitted_count=omitted_fields,
            )
        )
    if omitted_characters:
        log.append(
            TruncationEntry(
                slice_name=slice_name,
                reason="character_budget",
                omitted_count=omitted_characters,
            )
        )
    selected_metadata = tuple(metadata_by_id[item.item_ref] for item in output)
    source_refs = tuple(
        sorted(
            {binding.ref for metadata in selected_metadata for binding in metadata.source_bindings}
        )
    )
    source_hash = _hash(
        {
            "snapshot_hash": bound.snapshot_hash,
            "resolver_proof": bound.resolver_proof.model_dump(mode="json"),
            "item_source_hashes": tuple(metadata.source_hash for metadata in selected_metadata),
        }
    )
    minimum_content = _slice_model_content(
        slice_name=slice_name,
        source_refs=source_refs,
        source_hash=source_hash,
        resolver_proof=bound.resolver_proof,
        items=tuple(output),
        model_content_profile=model_content_profile,
    )
    if len(minimum_content) > limit.max_characters:
        if slice_name in {"character_core", "current_situation"}:
            raise ValueError(f"{slice_name} minimum whole-item budget is not satisfied")
        # Optional domains must not make an otherwise valid turn unavailable
        # merely because their resolver proof/source envelope grew beyond a
        # small lane budget.  Mark the entire slice unavailable instead of
        # emitting an unauditable partial envelope; required situation/core
        # remain fail-closed above.
        return _unavailable(limit), tuple(
            (
                *log,
                TruncationEntry(
                    slice_name=slice_name,
                    reason="source_envelope_budget",
                    omitted_count=max(1, len(values)),
                ),
            )
        )
    compiled = _make_available_slice(
        slice_name=slice_name,
        bound=bound,
        limit=limit,
        items=tuple(output),
        source_refs=source_refs,
        source_hash=source_hash,
        truncated=bool(log),
        model_content_profile=model_content_profile,
    )
    if compiled.budget.used_characters > limit.max_characters:
        raise ValueError(f"{slice_name} budget cannot represent its source envelope")
    if slice_name in {"character_core", "current_situation"} and values and not output:
        raise ValueError(f"{slice_name} minimum whole-item budget is not satisfied")
    return compiled, tuple(log)


def _validate_input_contract(request: ContextCapsuleRequest) -> None:
    bound_slices: tuple[tuple[SliceName, ResolvedSlice[object] | None], ...] = (
        ("current_situation", request.situation),
        ("recent_dialogue", request.recent_dialogue),
        ("character_core", request.character_core),
        ("relationship_slice", request.relationship_slice),
        ("appraisals", request.appraisals),
        ("affect_episodes", request.affect_episodes),
        ("open_threads", request.open_threads),
        ("relevant_facts", request.relevant_facts),
        ("recent_experiences", request.recent_experiences),
        ("world_life", request.world_life),
        ("active_memory_candidates", request.active_memory_candidates),
        ("available_capabilities", request.available_capabilities),
        ("action_budget", request.action_budget),
        ("private_impressions", request.private_impressions),
        ("advisories", request.advisories),
    )
    if request.perception_results is not None:
        bound_slices = (*bound_slices, ("perception_results", request.perception_results))
    if any(
        bound is not None and bound.pinned_world_revision != request.world_revision
        for _, bound in bound_slices
    ):
        raise ValueError("every Context Capsule slice must share the pinned world revision")
    _validate_hex_digest(request.snapshot_hash, label="Context Capsule snapshot hash")
    for slice_name, bound in bound_slices:
        if bound is None:
            continue
        if (
            bound.world_id != request.world_id
            or bound.snapshot_id != request.snapshot_id
            or bound.snapshot_hash != request.snapshot_hash
        ):
            raise ValueError("every Context Capsule slice must share world and snapshot identity")
        if len(bound.item_metadata) > MAX_INPUT_ITEMS_PER_SLICE:
            raise ValueError("resolved slice exceeds the metadata item limit")
        for metadata in bound.item_metadata:
            ResolvedItemMetadata.model_validate(metadata.model_dump())
            if slice_name != "current_situation" and any(
                binding.source_kind == "projection_snapshot" for binding in metadata.source_bindings
            ):
                raise ValueError("projection_snapshot authority is reserved for Situation")
        proof = bound.resolver_proof
        if (
            proof.world_id != request.world_id
            or proof.snapshot_id != request.snapshot_id
            or proof.snapshot_hash != request.snapshot_hash
            or proof.pinned_world_revision != request.world_revision
            or proof.slice_name != slice_name
        ):
            raise ValueError("resolver proof does not match its world snapshot slice")
        values = _values(bound)
        if len(values) > MAX_INPUT_ITEMS_PER_SLICE:
            raise ValueError(f"{slice_name} exceeds the resolved input item limit")
        if len(values) != len(bound.item_metadata):
            raise ValueError(f"{slice_name} item metadata does not cover every typed value")
        value_ids = tuple(_identity(slice_name, value) for value in values)
        metadata_ids = tuple(metadata.item_ref for metadata in bound.item_metadata)
        if len(value_ids) != len(set(value_ids)):
            raise ValueError(f"{slice_name} contains duplicate typed item identities")
        if len(metadata_ids) != len(set(metadata_ids)):
            raise ValueError(f"{slice_name} contains duplicate metadata identities")
        if set(value_ids) != set(metadata_ids):
            raise ValueError(f"{slice_name} metadata identities do not match typed values")
        if proof.result_set_hash != resolved_result_set_hash(slice_name, bound.item_metadata):
            raise ValueError("resolver proof result set hash is invalid")
        binding_refs = tuple(
            sorted(
                {
                    binding.ref
                    for metadata in bound.item_metadata
                    for binding in metadata.source_bindings
                }
            )
        )
        if proof.explicit_authority_refs != binding_refs:
            raise ValueError("resolver explicit authority refs do not match source bindings")
        metadata_by_id = {metadata.item_ref: metadata for metadata in bound.item_metadata}
        for value in values:
            metadata = metadata_by_id[_identity(slice_name, value)]
            item_binding_refs = tuple(sorted({binding.ref for binding in metadata.source_bindings}))
            typed_refs = _typed_source_refs(slice_name, value)
            if typed_refs is not None and typed_refs != item_binding_refs:
                raise ValueError("resolved item bindings do not match its typed authority refs")
            binding_authorities = {
                (
                    binding.source_kind,
                    binding.ref,
                    binding.source_world_revision,
                    binding.immutable_hash,
                )
                for binding in metadata.source_bindings
            }
            if not set(_typed_source_authorities(value)).issubset(binding_authorities):
                raise ValueError(
                    "resolved source binding hash/revision contradicts typed authority"
                )
            binding_hashes = {
                (binding.source_kind, binding.ref, binding.immutable_hash)
                for binding in metadata.source_bindings
            }
            if not set(_typed_source_hashes(value)).issubset(binding_hashes):
                raise ValueError("resolved receipt binding hash contradicts typed authority")
    if request.situation.value.world_id != request.world_id:
        raise ValueError("Situation belongs to a different world")
    if request.situation.value.actor_ref != request.actor_ref:
        raise ValueError("Situation belongs to a different actor")
    if (
        request.character_core is not None
        and request.character_core.value.actor_ref != request.actor_ref
    ):
        raise ValueError("Character Core belongs to a different actor")
    _validate_hex_digest(
        request.situation.value.authority_snapshot_hash,
        label="Situation authority snapshot hash",
    )
    situation_metadata = request.situation.item_metadata
    if len(situation_metadata) != 1:
        raise ValueError("Situation requires one resolved item authority")
    situation_bindings = tuple(
        (
            binding.source_kind,
            binding.authority_type,
            binding.ref,
            binding.source_world_revision,
            binding.immutable_hash,
        )
        for binding in situation_metadata[0].source_bindings
    )
    if request.situation.value.source_revisions:
        expected_bindings = tuple(
            sorted(
                (
                    "committed_event",
                    f"situation_source:{source.domain}",
                    source.event_ref,
                    source.source_world_revision,
                    source.payload_hash,
                )
                for source in request.situation.value.source_revisions
            )
        )
        if situation_bindings != expected_bindings:
            raise ValueError("Situation bindings do not match typed source revisions")
    elif situation_bindings != (
        (
            "projection_snapshot",
            "LedgerProjection",
            request.snapshot_id,
            request.world_revision,
            request.situation.value.authority_snapshot_hash,
        ),
    ):
        raise ValueError("Situation source binding does not match its authority snapshot hash")
    situation_material = request.situation.value.model_dump(
        mode="json", exclude={"internal_semantic_hash"}
    )
    if request.situation.value.internal_semantic_hash != _hash(situation_material):
        raise ValueError("Situation internal semantic hash is invalid")
    if request.situation.value.compiled_at_world_revision != request.world_revision:
        raise ValueError("Situation revision does not match the Context Capsule")
    if request.situation.value.logical_time != request.logical_time:
        raise ValueError("Situation logical time does not match the Context Capsule")
    if request.affect_episodes is not None and any(
        item.status != "active" for item in request.affect_episodes.value
    ):
        raise ValueError("Context Capsule accepts only active affect episodes")
    if request.appraisals is not None and any(
        item.status != "active" for item in request.appraisals.value
    ):
        raise ValueError("Context Capsule accepts only active appraisals")
    if request.open_threads is not None and any(
        item.values.status != "open" for item in request.open_threads.value
    ):
        raise ValueError("Context Capsule accepts only open threads")
    if request.relevant_facts is not None and any(
        (item.status if isinstance(item, FactRecallItem) else item.values.status) != "active"
        for item in request.relevant_facts.value
    ):
        raise ValueError("Context Capsule accepts only active facts")
    if request.active_memory_candidates is not None and any(
        isinstance(item, MemoryCandidateProjection) and item.values.status != "active"
        for item in request.active_memory_candidates.value
    ):
        raise ValueError("Context Capsule accepts only active memory candidates")
    if request.available_capabilities is not None and any(
        item.values.state != "active" for item in request.available_capabilities.value
    ):
        raise ValueError("Context Capsule accepts only active capabilities")
    if request.private_impressions is not None and any(
        item.status != "active" for item in request.private_impressions.value
    ):
        raise ValueError("Context Capsule accepts only active private impressions")
    if request.advisories is not None:
        if request.logical_time is None or any(
            item.expiry <= request.logical_time for item in request.advisories.value
        ):
            raise ValueError("Context Capsule cannot include an expired advisory")
        resolved_sources = {
            binding.ref
            for metadata in request.advisories.item_metadata
            for binding in metadata.source_bindings
        }
        if any(
            not set(item.source_refs).issubset(resolved_sources)
            for item in request.advisories.value
        ):
            raise ValueError("advisory sources must be resolved by the advisory slice")


def _relationship_evaluation_context(
    request: ContextCapsuleRequest,
) -> RelationshipEvaluationContext | None:
    """Derive the relationship lane's compact view from untruncated authority."""

    if not request.relationship_evaluation_requested or request.appraisals is None:
        return None
    appraisals = _values(request.appraisals)
    matches = tuple(
        item
        for item in appraisals
        if isinstance(item, AppraisalProjection)
        and item.origin.accepted_event_ref == request.trigger_ref
    )
    if len(matches) != 1:
        return None
    appraisal = matches[0]
    appraisal_metadata = next(
        item for item in request.appraisals.item_metadata if item.item_ref == appraisal.appraisal_id
    )
    appraisal_summary = {
        "status": appraisal.status,
        "confidence_bp": appraisal.confidence_bp,
        "expires_at": appraisal.expires_at.isoformat(),
        "hypotheses": [
            {
                "meaning": item.meaning,
                "attribution": item.attribution,
                "controllability": item.controllability,
                "severity": item.severity,
                "weight_bp": item.weight_bp,
            }
            for item in appraisal.hypotheses
        ],
    }
    relationship = None
    relationship_source = None
    if request.relationship_slice is not None:
        states = _values(request.relationship_slice)
        matching_states = tuple(
            item
            for item in states
            if isinstance(item, RelationshipStateProjection)
            and item.subject_ref == appraisal.subject_ref
        )
        if len(matching_states) == 1:
            relationship = matching_states[0]
            metadata = next(
                item
                for item in request.relationship_slice.item_metadata
                if item.item_ref == relationship.relationship_id
            )
            relationship_source = RelationshipEvaluationSource(
                item_ref=metadata.item_ref,
                source_bindings=metadata.source_bindings,
                source_hash=metadata.source_hash,
                value_hash=metadata.value_hash,
            )
    relationship_summary = (
        {
            "stage": relationship.stage,
            "variables": relationship.variables.model_dump(mode="json"),
            "temperature": relationship.temperature,
        }
        if relationship is not None
        else {
            "stage": "stranger",
            "variables": {
                "trust_bp": 0,
                "closeness_bp": 0,
                "respect_bp": 0,
                "reliability_bp": 0,
                "mutuality_bp": 0,
                "repair_confidence_bp": 0,
            },
            "temperature": "ordinary",
        }
    )
    return RelationshipEvaluationContext(
        subject_ref=appraisal.subject_ref,
        trigger_appraisal_id=appraisal.appraisal_id,
        appraisal_summary_json=_canonical_json(appraisal_summary),
        relationship_summary_json=_canonical_json(relationship_summary),
        appraisal_source=RelationshipEvaluationSource(
            item_ref=appraisal_metadata.item_ref,
            source_bindings=appraisal_metadata.source_bindings,
            source_hash=appraisal_metadata.source_hash,
            value_hash=appraisal_metadata.value_hash,
        ),
        relationship_source=relationship_source,
    )


def _context_model_content(
    request: ContextCapsuleRequest,
    slices: dict[str, CapsuleSlice],
    relationship_evaluation: RelationshipEvaluationContext | None,
) -> str:
    material: dict[str, object] = {
        "world_id": request.world_id,
        "snapshot_id": request.snapshot_id,
        "snapshot_hash": request.snapshot_hash,
        "actor_ref": request.actor_ref,
        "consumer_scope": request.consumer_scope,
        "trigger_ref": request.trigger_ref,
        "world_revision": request.world_revision,
        "deliberation_revision": request.deliberation_revision,
        "logical_time": request.logical_time.isoformat() if request.logical_time else None,
        "slices": {name: json.loads(slice_.model_content_json) for name, slice_ in slices.items()},
    }
    if relationship_evaluation is not None:
        material["relationship_evaluation"] = relationship_evaluation.model_dump(mode="json")
    return _canonical_json(material)


def _replace_model_content(compiled: CapsuleSlice, content: str) -> CapsuleSlice:
    """Swap only the model-facing view of an available slice.

    Every authority field (items, hashes, resolver proof) is retained exactly;
    the degraded view is a presentation decision under the global character
    budget, never a change to what this capsule can prove.
    """

    return compiled.model_copy(
        update={
            "model_content_json": content,
            "budget": compiled.budget.model_copy(update={"used_characters": len(content)}),
            "truncated": True,
        }
    )


def _collapsed_slice_view(compiled: CapsuleSlice) -> CapsuleSlice:
    """Collapse an emptied available slice to an explicit minimal model view.

    After the global envelope evicted every item, the remaining resolver
    proof/source envelope carries no conversational content.  The collapsed
    view keeps the omission visible to the model while the slice retains its
    full source authority for auditing.
    """

    content = _canonical_json(
        {
            "availability": "available",
            "content_omitted": True,
            "truncated": True,
        }
    )
    return _replace_model_content(compiled, content)


def _character_truncated_head_view(
    compiled: CapsuleSlice, *, preview_characters: int
) -> CapsuleSlice:
    """Bound a mandatory head slice by characters as the final degradation.

    The trusted CapsuleItem keeps its complete payload and hash closure; only
    the model-facing view is reduced to a bounded payload prefix so prompt
    construction can always terminate inside the global budget.
    """

    material: dict[str, object] = {
        "availability": "available",
        "truncated": True,
        "items": [
            {
                "item_ref": item.item_ref,
                "rank_score_bp": item.rank_score_bp,
                "privacy_class": item.privacy_class,
                "value_preview": item.payload_json[: max(0, preview_characters)],
            }
            for item in compiled.items
        ],
    }
    return _replace_model_content(compiled, _canonical_json(material))


def _evict_last_item(
    *,
    slice_name: SliceName,
    compiled: CapsuleSlice,
    bound: ResolvedSlice[object],
    limit: SliceBudget,
    model_content_profile: Literal["general", "proactive_decision"] = "general",
) -> CapsuleSlice:
    retained = compiled.items[:-1]
    source_refs = tuple(
        sorted({binding.ref for item in retained for binding in item.source_bindings})
    )
    source_hash = _hash(
        {
            "snapshot_hash": bound.snapshot_hash,
            "resolver_proof": bound.resolver_proof.model_dump(mode="json"),
            "item_source_hashes": tuple(item.source_hash for item in retained),
        }
    )
    return _make_available_slice(
        slice_name=slice_name,
        bound=bound,
        limit=limit,
        items=retained,
        source_refs=source_refs,
        source_hash=source_hash,
        truncated=True,
        model_content_profile=model_content_profile,
    )


def _compile_resolved_context(
    request: ContextCapsuleRequest,
    *,
    policy: ContextCapsuleBudgetPolicy | None = None,
    _authority: object | None = None,
    model_content_profile: Literal["general", "proactive_decision"] = "general",
) -> ContextCapsule:
    """Compile a deterministic, bounded packet without I/O, inference or randomness."""

    request = ContextCapsuleRequest.model_validate(
        request.model_dump(mode="python", warnings="error")
    )
    _validate_input_contract(request)
    supplied_policy = policy or ContextCapsuleBudgetPolicy()
    active_policy = ContextCapsuleBudgetPolicy.model_validate(
        supplied_policy.model_dump(mode="python", warnings="error")
    )
    inputs: tuple[tuple[SliceName, ResolvedSlice[object] | None], ...] = (
        ("character_core", request.character_core),
        ("current_situation", request.situation),
        ("recent_dialogue", request.recent_dialogue),
        ("relationship_slice", request.relationship_slice),
        ("appraisals", request.appraisals),
        ("affect_episodes", request.affect_episodes),
        ("open_threads", request.open_threads),
        ("relevant_facts", request.relevant_facts),
        ("recent_experiences", request.recent_experiences),
        ("world_life", request.world_life),
        ("active_memory_candidates", request.active_memory_candidates),
        ("available_capabilities", request.available_capabilities),
        ("action_budget", request.action_budget),
        ("private_impressions", request.private_impressions),
        ("advisories", request.advisories),
    )
    if request.perception_results is not None:
        inputs = (*inputs, ("perception_results", request.perception_results))
    slices: dict[str, CapsuleSlice] = {}
    bounds = {name: bound for name, bound in inputs}
    truncation_log: list[TruncationEntry] = []
    for slice_name, bound in inputs:
        compiled, entries = _compile_slice(
            slice_name=slice_name,
            bound=bound,
            limit=getattr(active_policy, slice_name),
            model_content_profile=model_content_profile,
        )
        slices[slice_name] = compiled
        truncation_log.extend(entries)

    relationship_evaluation = _relationship_evaluation_context(request)
    model_content = _context_model_content(request, slices, relationship_evaluation)
    global_omissions: dict[SliceName, int] = {}
    # Preserve one unit of the state that gives an otherwise capable reply its
    # interpersonal continuity.  Treating these like ordinary ranked items
    # allowed a large capability/budget/advisory envelope to evict the sole
    # relationship, affect episode, or unfinished thread.  The model would
    # then have durable state in the ledger but be unable to use it in chat.
    # Additional items remain rank-evictable, so this is a minimum continuity
    # floor rather than an unbounded prompt reservation.
    minimum_retained_items: dict[SliceName, int] = {
        "character_core": 1,
        "current_situation": 1,
        "recent_dialogue": 8,
        "relationship_slice": 1,
        "appraisals": 1,
        "affect_episodes": 1,
        "open_threads": 1,
        # Two independently sourced durable facts are the minimum useful unit
        # for ordinary compound recall (for example identity + preference).
        # Without this floor the global envelope kept only the first Fact even
        # when the fact slice itself had ample budget.
        "relevant_facts": 2,
        "world_life": 1,
        "active_memory_candidates": 2,
        # An advisory overlay is the semantic decision matrix explicitly
        # requested by the current lane.  Keep the whole bounded matrix
        # together: dropping the thread or boundary coordinate while keeping
        # only affect would make the model see a distorted interpretation of
        # the same utterance.  The compiler already caps the slice and the
        # global envelope remains the final safety bound.
        "advisories": len(slices["advisories"].items),
    }
    proactive_advisory_present = any(
        json.loads(item.payload_json).get("kind") == "proactive_opportunity"
        for item in slices["advisories"].items
    )
    # Character-truncation state for the mandatory head slices (final tier).
    # ``None`` means the head still shows its ordinary whole-item view; a
    # non-negative number is the current bounded payload preview length.
    head_preview_characters: dict[SliceName, int | None] = {
        "character_core": None,
        "current_situation": None,
    }
    collapse_attempted: set[SliceName] = set()

    def _floor_eviction_candidate() -> SliceName | None:
        """Tier 1: the pre-existing rank eviction above continuity floors.

        Every compilation this tier can satisfy follows the historical
        eviction order and produces the same capsule.
        """

        candidates = [
            (slice_.items[-1].rank_score_bp, name)
            for name, slice_ in slices.items()
            if len(slice_.items) > minimum_retained_items.get(name, 0)
        ]
        if candidates:
            return min(candidates, key=lambda item: (item[0], item[1]))[1]
        return None

    def _collapse_candidate() -> SliceName | None:
        """Tier 3: the largest emptied available envelope not yet collapsed.

        An emptied available slice still spends hundreds of characters on its
        resolver-proof/source envelope.  That envelope carries no content the
        model could use, so collapsing its view is the cheapest degradation
        once ordinary eviction has nothing left to remove.
        """

        candidates = [
            (slice_.budget.used_characters, name)
            for name, slice_ in slices.items()
            if slice_.availability == "available"
            and not slice_.items
            and name not in collapse_attempted
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0], item[1]))[1]

    def _deep_eviction_candidate() -> SliceName | None:
        """Tiers 4-6: degrade the protected continuity set itself.

        Tier 4 takes every protected non-head, non-advisory slice down to one
        item. Tier 5 then degrades advisories item-by-item by rank; the
        per-slice sort keeps a proactive_opportunity advisory at the head, so
        it is the last advisory standing. Tier 6 allows zero items everywhere
        except the mandatory heads; a remaining
        proactive_opportunity advisory is the semantic subject of its lane,
        so it outlasts every other optional single item.
        """

        candidates = [
            (slice_.items[-1].rank_score_bp, name)
            for name, slice_ in slices.items()
            if name not in {"character_core", "current_situation"}
            and name != "advisories"
            and len(slice_.items) > 1
        ]
        if candidates:
            return min(candidates, key=lambda item: (item[0], item[1]))[1]
        if len(slices["advisories"].items) > 1:
            return "advisories"
        last_candidates = [
            (
                1 if name == "advisories" and proactive_advisory_present else 0,
                slice_.items[-1].rank_score_bp,
                name,
            )
            for name, slice_ in slices.items()
            if name not in {"character_core", "current_situation"} and slice_.items
        ]
        if last_candidates:
            return min(last_candidates)[2]
        return None

    def _evict(selected_name: SliceName) -> None:
        selected_bound = bounds[selected_name]
        if selected_bound is None:  # pragma: no cover - guarded by available items
            raise RuntimeError("available Capsule slice lost its resolved authority")
        slices[selected_name] = _evict_last_item(
            slice_name=selected_name,
            compiled=slices[selected_name],
            bound=selected_bound,
            limit=getattr(active_policy, selected_name),
            model_content_profile=model_content_profile,
        )
        global_omissions[selected_name] = global_omissions.get(selected_name, 0) + 1

    while len(model_content) > active_policy.hard_max_characters:
        selected_name = _floor_eviction_candidate()
        if selected_name is not None:
            _evict(selected_name)
            model_content = _context_model_content(request, slices, relationship_evaluation)
            continue
        collapse_name = _collapse_candidate()
        if collapse_name is not None:
            collapse_attempted.add(collapse_name)
            collapsed = _collapsed_slice_view(slices[collapse_name])
            if collapsed.budget.used_characters < slices[collapse_name].budget.used_characters:
                slices[collapse_name] = collapsed
                global_omissions[collapse_name] = global_omissions.get(collapse_name, 0) + 1
                model_content = _context_model_content(
                    request, slices, relationship_evaluation
                )
            continue
        selected_name = _deep_eviction_candidate()
        if selected_name is not None:
            _evict(selected_name)
            model_content = _context_model_content(request, slices, relationship_evaluation)
            continue
        # Tier 7: every optional envelope is gone and only the mandatory
        # heads remain.  Bound their model views by characters (halving the
        # payload preview each pass) so the loop provably terminates with a
        # legal, explicitly truncated capsule instead of raising on this
        # required prompt-construction path.
        truncatable = [
            (slices[name].budget.used_characters, name)
            for name, preview in head_preview_characters.items()
            if slices[name].items and (preview is None or preview > 0)
        ]
        if not truncatable:
            # Structural floor: the framing plus minimal slice views alone
            # exceed the configured budget.  No resolved data can reach this
            # branch; only a deployment budget below the capsule's fixed
            # representation cost does.
            raise ValueError(
                "global Context Capsule budget is below the minimum whole-item budget "
                "for required envelopes"
            )
        _, head_name = max(truncatable, key=lambda item: (item[0], item[1]))
        current_preview = head_preview_characters[head_name]
        if current_preview is None:
            next_preview = max(
                len(item.payload_json) for item in slices[head_name].items
            ) // 2
        else:
            next_preview = current_preview // 2
        head_preview_characters[head_name] = next_preview
        slices[head_name] = _character_truncated_head_view(
            slices[head_name], preview_characters=next_preview
        )
        global_omissions[head_name] = global_omissions.get(head_name, 0) + 1
        model_content = _context_model_content(request, slices, relationship_evaluation)
    truncation_log.extend(
        TruncationEntry(
            slice_name=name,
            reason="global_character_budget",
            omitted_count=count,
        )
        for name, count in sorted(global_omissions.items())
    )

    used_by_slice = tuple((name, slices[name].budget.used_characters) for name, _ in inputs)
    slice_content_characters = sum(value for _, value in used_by_slice)
    used_characters = len(model_content)

    budget = ContextBudgetAudit(
        hard_max_characters=active_policy.hard_max_characters,
        used_characters=used_characters,
        slice_content_characters=slice_content_characters,
        framing_characters=used_characters - slice_content_characters,
        used_by_slice=used_by_slice,
        truncation_log=tuple(truncation_log),
    )
    result_material = {
        "world_id": request.world_id,
        "snapshot_id": request.snapshot_id,
        "snapshot_hash": request.snapshot_hash,
        "actor_ref": request.actor_ref,
        "consumer_scope": request.consumer_scope,
        "trigger_ref": request.trigger_ref,
        "world_revision": request.world_revision,
        "deliberation_revision": request.deliberation_revision,
        "ledger_sequence": request.ledger_sequence,
        "logical_time": request.logical_time.isoformat() if request.logical_time else None,
        **{name: value.model_dump(mode="json") for name, value in slices.items()},
        "model_content_json": model_content,
        "budget": budget.model_dump(mode="json"),
    }
    if relationship_evaluation is not None:
        result_material["relationship_evaluation"] = relationship_evaluation.model_dump(mode="json")
    compiler_result_hash = _hash(result_material)
    trusted = _authority is _COMPILER_AUTHORITY
    provenance_kind = "trusted_resolver_compiled" if trusted else "test_only_untrusted"
    compiler_result_tag = _compiler_result_tag(compiler_result_hash) if trusted else None
    material = {
        "provenance_kind": provenance_kind,
        "compiler_result_hash": compiler_result_hash,
        "compiler_result_tag": compiler_result_tag,
        **result_material,
    }
    return ContextCapsule(
        capsule_id=_hash(material),
        provenance_kind=provenance_kind,
        compiler_result_hash=compiler_result_hash,
        compiler_result_tag=compiler_result_tag,
        world_id=request.world_id,
        snapshot_id=request.snapshot_id,
        snapshot_hash=request.snapshot_hash,
        actor_ref=request.actor_ref,
        consumer_scope=request.consumer_scope,
        trigger_ref=request.trigger_ref,
        world_revision=request.world_revision,
        deliberation_revision=request.deliberation_revision,
        ledger_sequence=request.ledger_sequence,
        logical_time=request.logical_time,
        relationship_evaluation=relationship_evaluation,
        model_content_json=model_content,
        budget=budget,
        **slices,
    )


class TrustedContextCapsuleHandle:
    """Process-local, non-serializable proof that the compiler issued a Capsule.

    The handle is an architectural capability for a non-hostile composition
    root.  It is not a sandbox against arbitrary code executing in-process.
    """

    __slots__ = ("__capsule",)

    def __init__(self, capsule: ContextCapsule, *, _authority: object | None = None) -> None:
        if _authority is not _COMPILER_AUTHORITY:
            raise ValueError("trusted Context Capsule handles are compiler-issued")
        if capsule.provenance_kind != "trusted_resolver_compiled":
            raise ValueError("trusted handle cannot wrap a test-only Capsule")
        self.__capsule = capsule

    @property
    def capsule(self) -> ContextCapsule:
        return self.__capsule

    def __reduce__(self) -> object:
        raise TypeError("trusted Context Capsule handles cannot be serialized")


class PreparedContextCapsuleHandle:
    """Opaque one-resolution Context material awaiting an optional advisory overlay.

    The base capsule and the overlay must consume the same typed resolver result.
    Keeping that result behind a compiler-instance capability removes a second
    full projection resolution without exposing a caller-mutable Context request.
    """

    __slots__ = ("__capsule", "__issuer", "__query", "__resolved")

    def __init__(
        self,
        *,
        query: ContextCompileQuery,
        resolved: ContextCapsuleRequest,
        capsule: ContextCapsule,
        issuer: object,
    ) -> None:
        self.__query = query
        self.__resolved = resolved
        self.__capsule = capsule
        self.__issuer = issuer

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self) -> object:
        raise TypeError("prepared Context Capsule handles cannot be serialized")


class ContextCapsuleCompiler:
    """Production seam: resolve internally, then compile the pinned trusted result."""

    def __init__(
        self,
        *,
        resolver: TrustedInternalContextResolver,
        policy: ContextCapsuleBudgetPolicy | None = None,
    ) -> None:
        capability = getattr(resolver, "capability", None)
        if capability is None or not resolver_capability_is_valid(resolver, capability):
            raise ValueError("Context Capsule resolver lacks trusted internal capability")
        supplied_policy = policy or ContextCapsuleBudgetPolicy()
        self._policy = ContextCapsuleBudgetPolicy.model_validate(
            supplied_policy.model_dump(mode="python", warnings="error")
        )
        self._resolver = resolver
        self._prepared_issuer = object()

    def _resolve(
        self, query: ContextCompileQuery
    ) -> tuple[ContextCompileQuery, ContextCapsuleRequest]:
        pinned_query = ContextCompileQuery.model_validate(
            query.model_dump(mode="python", warnings="error")
        )
        result = self._resolver.resolve(pinned_query)
        if not resolver_capability_is_valid(self._resolver, result.capability):
            raise ValueError("resolved Context result has the wrong resolver capability")
        if result.query_hash != context_query_hash(pinned_query):
            raise ValueError("resolved Context result belongs to another query")
        if not isinstance(result.resolved_context, ContextCapsuleRequest):
            raise TypeError("trusted resolver returned an unsupported Context result")
        resolved = ContextCapsuleRequest.model_validate(
            result.resolved_context.model_dump(mode="python", warnings="error")
        )
        query_material = (
            "world_id",
            "snapshot_id",
            "snapshot_hash",
            "actor_ref",
            "consumer_scope",
            "trigger_ref",
            "world_revision",
            "deliberation_revision",
            "ledger_sequence",
            "logical_time",
        )
        if any(
            getattr(resolved, field) != getattr(pinned_query, field) for field in query_material
        ):
            raise ValueError("trusted resolver result does not match the compile query")
        return pinned_query, resolved

    def compile(self, query: ContextCompileQuery) -> ContextCapsule:
        _, resolved = self._resolve(query)
        return _compile_resolved_context(
            resolved, policy=self._policy, _authority=_COMPILER_AUTHORITY
        )

    def compile_for_deliberation(self, query: ContextCompileQuery) -> TrustedContextCapsuleHandle:
        return self.finalize_prepared(self.prepare_for_deliberation(query))

    def prepare_for_deliberation(
        self,
        query: ContextCompileQuery,
        *,
        relationship_evaluation: bool = False,
    ) -> PreparedContextCapsuleHandle:
        """Resolve one pinned Context exactly once before optional enrichment."""

        pinned_query, resolved = self._resolve(query)
        if relationship_evaluation:
            resolved = resolved.model_copy(update={"relationship_evaluation_requested": True})
        capsule = _compile_resolved_context(
            resolved, policy=self._policy, _authority=_COMPILER_AUTHORITY
        )
        return PreparedContextCapsuleHandle(
            query=pinned_query,
            resolved=resolved,
            capsule=capsule,
            issuer=self._prepared_issuer,
        )

    def _prepared_material(
        self, prepared: PreparedContextCapsuleHandle
    ) -> tuple[ContextCompileQuery, ContextCapsuleRequest, ContextCapsule]:
        if type(prepared) is not PreparedContextCapsuleHandle or not prepared.issued_by(
            self._prepared_issuer
        ):
            raise ValueError("prepared Context handle belongs to another compiler")
        return (
            object.__getattribute__(prepared, "_PreparedContextCapsuleHandle__query"),
            object.__getattribute__(prepared, "_PreparedContextCapsuleHandle__resolved"),
            object.__getattribute__(prepared, "_PreparedContextCapsuleHandle__capsule"),
        )

    def finalize_prepared(
        self, prepared: PreparedContextCapsuleHandle
    ) -> TrustedContextCapsuleHandle:
        """Finalize a base prepared result without another resolver read."""

        _, _, capsule = self._prepared_material(prepared)
        return TrustedContextCapsuleHandle(capsule, _authority=_COMPILER_AUTHORITY)

    def compile_for_relationship_deliberation(
        self, query: ContextCompileQuery
    ) -> TrustedContextCapsuleHandle:
        """Compile a normal capsule plus the bounded post-appraisal relation view.

        Only the relationship lane requests this overlay.  Other deliberations
        retain their generic context shape and cannot accidentally depend on a
        relationship-specific compact projection.
        """

        return self.finalize_prepared(
            self.prepare_for_deliberation(query, relationship_evaluation=True)
        )

    def compile_for_deliberation_with_advisories(
        self,
        query: ContextCompileQuery,
        advisories: tuple[InnerAdvisoryProjection, ...],
        *,
        model_content_profile: Literal["general", "proactive_decision"] = "general",
    ) -> TrustedContextCapsuleHandle:
        """Compile one trusted capsule with resolver-verified advisory candidates.

        Advisory outputs are intentionally supplied as ordinary data.  The ledger
        resolver must re-resolve every source reference at the same cursor before
        they can enter a Context Capsule; this method is not a mutation path.
        """

        prepared = self.prepare_for_deliberation(query)
        return self.compile_prepared_with_advisories(
            prepared, advisories, model_content_profile=model_content_profile
        )

    def compile_prepared_with_advisories(
        self,
        prepared: PreparedContextCapsuleHandle,
        advisories: tuple[InnerAdvisoryProjection, ...],
        *,
        model_content_profile: Literal["general", "proactive_decision"] = "general",
    ) -> TrustedContextCapsuleHandle:
        """Attach verified advisories to an already resolved pinned Context."""

        pinned_query, resolved, _ = self._prepared_material(prepared)
        build_slice = getattr(self._resolver, "resolve_advisory_slice", None)
        if not callable(build_slice):
            raise ValueError("Context resolver does not support advisory overlays")
        advisory_slice = build_slice(pinned_query, advisories)
        if not isinstance(advisory_slice, ResolvedSlice):
            raise TypeError("Context resolver returned an unsupported advisory slice")
        enriched = resolved.model_copy(update={"advisories": advisory_slice})
        capsule = _compile_resolved_context(
            enriched,
            policy=self._policy,
            _authority=_COMPILER_AUTHORITY,
            model_content_profile=model_content_profile,
        )
        return TrustedContextCapsuleHandle(capsule, _authority=_COMPILER_AUTHORITY)
