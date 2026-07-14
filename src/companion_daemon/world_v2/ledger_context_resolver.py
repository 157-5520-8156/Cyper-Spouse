"""Ledger-backed composition seam for revision-pinned Context Capsules.

This module is intentionally conservative: a projection value is exposed only
when every typed authority reference can be resolved at the requested cursor.
Missing authority makes that whole domain unavailable; it is never replaced by
an inferred reference or hash.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context_capsule import (
    ContextCapsuleBudgetPolicy,
    ContextCapsuleCompiler,
    ContextCapsuleRequest,
    MAX_INPUT_ITEMS_PER_SLICE,
    MAX_RESOLVER_DOMAIN_SCAN_ITEMS,
    RANK_DOMAIN_IMPORTANCE_BP,
    RANK_RECENCY_WINDOW_SECONDS,
    RANK_WEIGHT_BP,
    RESOLUTION_POLICY_DIGEST,
    RESOLUTION_POLICY_VERSION,
    RESOLVER_ID,
    RESOLVER_VERSION,
    ResolvedItemMetadata,
    ResolvedSlice,
    ResolvedSourceBinding,
    ResolverProof,
    SliceName,
    authority_refs_digest,
    canonical_value_hash,
    resolved_result_set_hash,
    source_bindings_hash,
)
from .context_resolver import (
    ContextCompileQuery,
    ResolvedContextResult,
    TrustedInternalContextResolver,
    context_query_hash,
    projection_snapshot_id,
)
from .ledger import LedgerPort
from .schema_core import PrivacyClass
from .schemas import CommittedWorldEventRef, LedgerProjection
from .situation_compiler import SituationCompiler, request_from_ledger_projection


_PRIVACY_FLOOR: dict[SliceName, PrivacyClass] = {
    "character_core": "withhold",
    "current_situation": "private",
    "relationship_slice": "private",
    "affect_episodes": "private",
    "open_threads": "private",
    "relevant_facts": "personal",
    "recent_experiences": "personal",
    "active_memory_candidates": "personal",
    "available_capabilities": "private",
    "action_budget": "withhold",
    "private_impressions": "withhold",
    "advisories": "private",
}
_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}

_ITEM_ID: dict[SliceName, str] = {
    "character_core": "core_id",
    "current_situation": "actor_ref",
    "relationship_slice": "relationship_id",
    "affect_episodes": "episode_id",
    "open_threads": "thread_id",
    "relevant_facts": "fact_id",
    "recent_experiences": "experience_id",
    "active_memory_candidates": "candidate_id",
    "available_capabilities": "grant_id",
    "action_budget": "account_id",
    "private_impressions": "impression_id",
    "advisories": "advisory_id",
}


class ContextRelevanceScope(BaseModel):
    """Explicit actor/subject boundary for a ledger-backed Context resolver."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    actor_ref: str = Field(min_length=1, max_length=256)
    related_subject_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def refs_are_canonical(self) -> ContextRelevanceScope:
        if self.related_subject_refs != tuple(sorted(set(self.related_subject_refs))):
            raise ValueError("Context relevance subject refs must be unique and sorted")
        if self.actor_ref in self.related_subject_refs:
            raise ValueError("Context actor must not be repeated as a related subject")
        return self

    @property
    def subject_refs(self) -> frozenset[str]:
        return frozenset((self.actor_ref, *self.related_subject_refs))

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


def context_capsule_compiler_from_ledger(
    *,
    ledger: LedgerPort,
    situation_compiler: SituationCompiler | None = None,
    policy: ContextCapsuleBudgetPolicy | None = None,
    relevance_scope: ContextRelevanceScope | None = None,
) -> ContextCapsuleCompiler:
    """Composition-root factory for the production ledger-backed seam."""

    return ContextCapsuleCompiler(
        resolver=LedgerProjectionContextResolver(
            ledger=ledger,
            situation_compiler=situation_compiler or SituationCompiler(),
            relevance_scope=relevance_scope,
        ),
        policy=policy,
    )


def _item_ref(slice_name: SliceName, item: BaseModel) -> str:
    identity = str(getattr(item, _ITEM_ID[slice_name]))
    if slice_name == "action_budget":
        identity = f"{identity}:{item.window_id}"
    return identity


def _typed_refs(item: BaseModel) -> tuple[str, ...] | None:
    refs: set[str] = set()
    origin = getattr(item, "origin", None)
    for field in ("accepted_event_ref", "event_ref"):
        if value := getattr(origin, field, None):
            refs.add(value)
    values = getattr(item, "values", None)
    for evidence in getattr(values, "source_evidence_refs", ()):
        refs.add(evidence.ref_id)
    for binding in getattr(values, "source_bindings", ()):
        # Receipt authority has a typed immutable hash but no committed world
        # revision.  It cannot satisfy a committed-event-only ledger resolver.
        if getattr(binding, "receipt_id", None) is not None:
            return None
        if value := getattr(binding, "authority_event_ref", None):
            refs.add(value)
    for evidence in getattr(item, "evidence_refs", ()):
        refs.add(evidence.ref_id)
    return tuple(sorted(refs)) or None


def _typed_authority_claims(
    item: BaseModel,
) -> tuple[tuple[str, int, str], ...] | None:
    """Return exact embedded event claims, or None for an incomplete claim."""

    values = getattr(item, "values", None)
    claims: set[tuple[str, int, str]] = set()
    evidence_values = (
        *getattr(values, "source_evidence_refs", ()),
        *getattr(item, "evidence_refs", ()),
    )
    for evidence in evidence_values:
        if evidence.source_world_revision is None or not evidence.immutable_hash:
            return None
        claims.add((evidence.ref_id, evidence.source_world_revision, evidence.immutable_hash))
    for binding in getattr(values, "source_bindings", ()):
        ref = getattr(binding, "authority_event_ref", None)
        if ref is None:
            continue
        revision = getattr(binding, "authority_world_revision", None)
        immutable_hash = getattr(binding, "authority_payload_hash", None)
        if revision is None or immutable_hash is None:
            return None
        claims.add((ref, revision, immutable_hash))
    return tuple(sorted(claims))


def _privacy(slice_name: SliceName, item: BaseModel) -> PrivacyClass:
    values = getattr(item, "values", None)
    candidates: list[PrivacyClass] = [_PRIVACY_FLOOR[slice_name]]
    for value in (
        getattr(item, "privacy_class", None),
        getattr(values, "privacy_class", None),
        getattr(values, "privacy_ceiling", None),
    ):
        if value in _PRIVACY_RANK:
            candidates.append(value)
    return max(candidates, key=_PRIVACY_RANK.__getitem__)


def _recency_bp(item: BaseModel, logical_time: datetime | None) -> int:
    if logical_time is None:
        return 0
    instants = (
        getattr(item, "updated_at", None),
        getattr(item, "last_supported", None),
        getattr(item, "opened_at", None),
        getattr(getattr(item, "values", None), "occurred_to", None),
    )
    instant = next((value for value in instants if value is not None), None)
    if instant is None:
        return 0
    age_seconds = max(0, int((logical_time - instant).total_seconds()))
    # Linear seven-day fixed-point window.  Integer arithmetic is replay stable.
    return max(0, 10_000 - age_seconds * 10_000 // RANK_RECENCY_WINDOW_SECONDS)


def _signal_bp(slice_name: SliceName, item: BaseModel) -> int:
    values = getattr(item, "values", None)
    direct = (
        getattr(values, "importance_bp", None),
        getattr(values, "retrieval_strength_bp", None),
        getattr(item, "confidence_bp", None),
        getattr(values, "confidence_bp", None),
        getattr(item, "strength_bp", None),
    )
    for value in direct:
        if isinstance(value, int):
            return max(0, min(10_000, value))
    if slice_name == "affect_episodes":
        components = getattr(item, "components", ())
        intensities = [getattr(value, "intensity_bp", 0) for value in components]
        return max(intensities, default=0)
    return RANK_DOMAIN_IMPORTANCE_BP[slice_name]


def _rank(slice_name: SliceName, item: BaseModel, logical_time: datetime | None) -> int:
    total_weight = sum(RANK_WEIGHT_BP.values())
    return (
        RANK_DOMAIN_IMPORTANCE_BP[slice_name] * RANK_WEIGHT_BP["domain_importance"]
        + _signal_bp(slice_name, item) * RANK_WEIGHT_BP["typed_signal"]
        + _recency_bp(item, logical_time) * RANK_WEIGHT_BP["recency"]
    ) // total_weight


def _bounded_domain_items(
    slice_name: SliceName,
    items: tuple[BaseModel, ...],
    logical_time: datetime | None,
) -> tuple[BaseModel, ...] | None:
    """Apply the installed bounded selection policy before any ledger lookup."""

    if len(items) > MAX_RESOLVER_DOMAIN_SCAN_ITEMS:
        return None
    return tuple(
        sorted(
            items,
            key=lambda item: (
                -_rank(slice_name, item, logical_time),
                _item_ref(slice_name, item),
            ),
        )[:MAX_INPUT_ITEMS_PER_SLICE]
    )


def _binding(event: CommittedWorldEventRef) -> ResolvedSourceBinding:
    return ResolvedSourceBinding(
        source_kind="committed_event",
        authority_type=event.event_type,
        ref=event.event_id,
        source_world_revision=event.world_revision,
        immutable_hash=event.payload_hash,
    )


class LedgerProjectionContextResolver(TrustedInternalContextResolver):
    """Resolve Context domains from exactly one ledger projection cursor."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        situation_compiler: SituationCompiler,
        relevance_scope: ContextRelevanceScope | None = None,
    ) -> None:
        super().__init__()
        self._ledger = ledger
        self._situation_compiler = situation_compiler
        self._relevance_scope = relevance_scope

    def resolve(self, query: ContextCompileQuery) -> ResolvedContextResult:
        scope = self._relevance_scope or ContextRelevanceScope(actor_ref=query.actor_ref)
        if scope.actor_ref != query.actor_ref:
            raise ValueError("Context relevance scope belongs to another actor")
        head = self._ledger.project()
        if (
            head.world_revision != query.world_revision
            or head.deliberation_revision != query.deliberation_revision
            or head.ledger_sequence != query.ledger_sequence
        ):
            raise ValueError("historical Context resolution requires an indexed projection reader")
        projection = self._ledger.project_at(query.cursor)
        self._validate_projection(query, projection)

        situation_result = self._situation_compiler.compile(
            request_from_ledger_projection(
                projection, actor_ref=query.actor_ref, event_resolver=self._ledger
            )
        )
        if situation_result.internal is None:
            raise ValueError("internal Situation compilation did not return internal authority")
        situation = situation_result.internal

        subject_refs = scope.subject_refs
        scoped_facts = tuple(
            item
            for item in projection.facts
            if item.values.status != "withdrawn" and item.values.subject_ref in subject_refs
        )
        scoped_threads = tuple(
            item for item in projection.threads if item.values.subject_ref in subject_refs
        )
        scoped_experiences = tuple(
            item
            for item in projection.experiences
            if hasattr(item, "origin")
            and query.actor_ref in item.values.participant_refs
            and set(item.values.participant_refs).issubset(subject_refs)
        )
        scoped_source_ids = {
            *(item.fact_id for item in scoped_facts),
            *(item.thread_id for item in scoped_threads),
            *(item.experience_id for item in scoped_experiences),
        }
        scoped_memories = tuple(
            item
            for item in projection.memory_candidates
            if item.values.status == "active"
            and all(
                binding.source_id in scoped_source_ids for binding in item.values.source_bindings
            )
        )
        appraisal_by_id = {item.appraisal_id: item for item in projection.appraisals}
        active_affect = tuple(
            item for item in projection.affect_episodes if item.status == "active"
        )
        affect_refs = {
            ref.appraisal_id
            for item in active_affect
            for component in item.components
            for ref in component.appraisal_refs
        }
        scoped_affect: tuple[BaseModel, ...] | None
        if not affect_refs.issubset(appraisal_by_id):
            scoped_affect = None
        else:
            scoped_affect = tuple(
                item
                for item in active_affect
                if all(
                    appraisal_by_id[ref.appraisal_id].subject_ref in subject_refs
                    for component in item.components
                    for ref in component.appraisal_refs
                )
            )

        domains: dict[SliceName, tuple[BaseModel, ...] | None] = {
            "character_core": (
                (projection.character_core,)
                if projection.character_core is not None
                and projection.character_core.actor_ref == query.actor_ref
                else None
            ),
            # Relationship heads have no origin/evidence binding in LedgerProjection.
            "relationship_slice": None,
            "affect_episodes": scoped_affect,
            "open_threads": tuple(item for item in scoped_threads if item.values.status == "open"),
            "relevant_facts": scoped_facts,
            "recent_experiences": scoped_experiences,
            "active_memory_candidates": scoped_memories,
            "available_capabilities": tuple(
                item
                for item in projection.capability_grants
                if item.values.actor_ref == query.actor_ref and item.values.state == "active"
            ),
            # Budget state lacks immutable per-account origin in the current schema.
            "action_budget": () if not projection.budget_accounts else None,
            # Private impressions are not installed in LedgerProjection yet.
            "private_impressions": None,
            "advisories": None,
        }
        domains = {
            slice_name: (
                None
                if items is None
                else _bounded_domain_items(slice_name, items, query.logical_time)
            )
            for slice_name, items in domains.items()
        }

        refs_by_item: dict[tuple[SliceName, str], tuple[str, ...] | None] = {}
        required_refs: set[str] = set()
        for slice_name, items in domains.items():
            if items is None:
                continue
            for item in items:
                refs = _typed_refs(item)
                refs_by_item[(slice_name, _item_ref(slice_name, item))] = refs
                if refs is not None:
                    required_refs.update(refs)

        resolved_events = self._resolve_exact(required_refs, query.world_revision)
        resolved: dict[str, object] = {
            "situation": self._situation_slice(query, situation, scope),
        }
        request_fields = {
            "character_core": "character_core",
            "relationship_slice": "relationship_slice",
            "affect_episodes": "affect_episodes",
            "open_threads": "open_threads",
            "relevant_facts": "relevant_facts",
            "recent_experiences": "recent_experiences",
            "active_memory_candidates": "active_memory_candidates",
            "available_capabilities": "available_capabilities",
            "action_budget": "action_budget",
            "private_impressions": "private_impressions",
            "advisories": "advisories",
        }
        for slice_name, field in request_fields.items():
            items = domains[slice_name]
            if items is None:
                resolved[field] = None
                continue
            built = self._domain_slice(
                query, slice_name, items, refs_by_item, resolved_events, scope
            )
            resolved[field] = built

        request = ContextCapsuleRequest(
            world_id=query.world_id,
            snapshot_id=query.snapshot_id,
            snapshot_hash=query.snapshot_hash,
            actor_ref=query.actor_ref,
            consumer_scope=query.consumer_scope,
            trigger_ref=query.trigger_ref,
            world_revision=query.world_revision,
            deliberation_revision=query.deliberation_revision,
            ledger_sequence=query.ledger_sequence,
            logical_time=query.logical_time,
            **resolved,
        )
        return ResolvedContextResult(
            query_hash=context_query_hash(query),
            capability=self.capability,
            resolved_context=request,
        )

    @staticmethod
    def _validate_projection(query: ContextCompileQuery, projection: LedgerProjection) -> None:
        if (
            projection.world_id != query.world_id
            or projection.world_revision != query.world_revision
            or projection.deliberation_revision != query.deliberation_revision
            or projection.ledger_sequence != query.ledger_sequence
            or projection_snapshot_id(projection) != query.snapshot_id
            or projection.semantic_hash != query.snapshot_hash
            or projection.logical_time != query.logical_time
        ):
            raise ValueError("ledger projection does not match the exact Context query cursor")

    def _resolve_exact(
        self, refs: Iterable[str], world_revision: int
    ) -> dict[str, CommittedWorldEventRef]:
        requested = tuple(sorted(set(refs)))
        if not requested:
            return {}
        resolved = self._ledger.resolve_committed_event_refs(
            requested, at_world_revision=world_revision
        )
        if set(resolved) - set(requested):
            raise ValueError("ledger event resolver returned unrequested authority")
        for ref, event in resolved.items():
            if event.event_id != ref or event.world_revision > world_revision:
                raise ValueError("ledger event resolver returned invalid pinned authority")
            stored = self._ledger.lookup_event_commit(ref)
            if stored is None:
                raise ValueError("resolved Context authority is absent from the ledger")
            stored_event, commit = stored
            if (
                stored_event.event_id != ref
                or stored_event.event_type != event.event_type
                or stored_event.payload_hash != event.payload_hash
                or commit.world_revision != event.world_revision
                or commit.world_revision > world_revision
            ):
                raise ValueError("resolved Context authority contradicts its committed event")
        return resolved

    @staticmethod
    def _proof(
        query: ContextCompileQuery,
        slice_name: SliceName,
        metadata: tuple[ResolvedItemMetadata, ...],
        scope: ContextRelevanceScope,
    ) -> ResolverProof:
        refs = tuple(sorted({binding.ref for item in metadata for binding in item.source_bindings}))
        return ResolverProof(
            resolver_id=RESOLVER_ID,
            resolver_version=RESOLVER_VERSION,
            policy_digest=RESOLUTION_POLICY_DIGEST,
            world_id=query.world_id,
            snapshot_id=query.snapshot_id,
            snapshot_hash=query.snapshot_hash,
            pinned_world_revision=query.world_revision,
            slice_name=slice_name,
            query_ref=query.trigger_ref,
            window_ref=(
                f"cursor:{query.world_revision}:{query.deliberation_revision}:"
                f"{query.ledger_sequence}:scope:{scope.digest[:16]}"
            ),
            policy_version=RESOLUTION_POLICY_VERSION,
            completeness="complete",
            privacy_floor=_PRIVACY_FLOOR[slice_name],
            explicit_authority_refs=refs,
            authority_refs_digest=authority_refs_digest(refs),
            result_set_hash=resolved_result_set_hash(slice_name, metadata),
        )

    def _situation_slice(
        self,
        query: ContextCompileQuery,
        situation: BaseModel,
        scope: ContextRelevanceScope,
    ) -> ResolvedSlice:
        if situation.source_revisions:
            bindings = tuple(
                sorted(
                    (
                        ResolvedSourceBinding(
                            source_kind="committed_event",
                            authority_type=f"situation_source:{source.domain}",
                            ref=source.event_ref,
                            source_world_revision=source.source_world_revision,
                            immutable_hash=source.payload_hash,
                        )
                        for source in situation.source_revisions
                    ),
                    key=lambda item: (
                        item.source_kind,
                        item.authority_type,
                        item.ref,
                        item.source_world_revision,
                        item.immutable_hash,
                    ),
                )
            )
        else:
            bindings = (
                ResolvedSourceBinding(
                    source_kind="projection_snapshot",
                    authority_type="LedgerProjection",
                    ref=query.snapshot_id,
                    source_world_revision=query.world_revision,
                    immutable_hash=situation.authority_snapshot_hash,
                ),
            )
        metadata = (
            ResolvedItemMetadata(
                item_ref=query.actor_ref,
                rank_score_bp=10_000,
                privacy_class=_privacy("current_situation", situation),
                source_bindings=bindings,
                source_hash=source_bindings_hash(bindings),
                value_hash=canonical_value_hash(situation),
            ),
        )
        return ResolvedSlice(
            world_id=query.world_id,
            snapshot_id=query.snapshot_id,
            snapshot_hash=query.snapshot_hash,
            pinned_world_revision=query.world_revision,
            value=situation,
            resolver_proof=self._proof(query, "current_situation", metadata, scope),
            item_metadata=metadata,
        )

    def _domain_slice(
        self,
        query: ContextCompileQuery,
        slice_name: SliceName,
        items: tuple[BaseModel, ...],
        refs_by_item: dict[tuple[SliceName, str], tuple[str, ...] | None],
        events: dict[str, CommittedWorldEventRef],
        scope: ContextRelevanceScope,
    ) -> ResolvedSlice | None:
        metadata: list[ResolvedItemMetadata] = []
        for item in items:
            item_ref = _item_ref(slice_name, item)
            refs = refs_by_item[(slice_name, item_ref)]
            if refs is None or any(ref not in events for ref in refs):
                return None
            claims = _typed_authority_claims(item)
            if claims is None or any(
                events[ref].world_revision != revision or events[ref].payload_hash != immutable_hash
                for ref, revision, immutable_hash in claims
            ):
                return None
            bindings = tuple(
                sorted(
                    (_binding(events[ref]) for ref in refs),
                    key=lambda value: (
                        value.source_kind,
                        value.authority_type,
                        value.ref,
                        value.source_world_revision,
                        value.immutable_hash,
                    ),
                )
            )
            metadata.append(
                ResolvedItemMetadata(
                    item_ref=item_ref,
                    rank_score_bp=_rank(slice_name, item, query.logical_time),
                    privacy_class=_privacy(slice_name, item),
                    source_bindings=bindings,
                    source_hash=source_bindings_hash(bindings),
                    value_hash=canonical_value_hash(item),
                )
            )
        ordered = tuple(
            sorted(
                zip(items, metadata, strict=True),
                key=lambda pair: (-pair[1].rank_score_bp, pair[1].item_ref),
            )
        )
        sorted_items = tuple(pair[0] for pair in ordered)
        sorted_metadata = tuple(pair[1] for pair in ordered)
        value: BaseModel | tuple[BaseModel, ...]
        if slice_name in {"character_core", "relationship_slice"}:
            if len(sorted_items) != 1:
                return None
            value = sorted_items[0]
        else:
            value = sorted_items
        return ResolvedSlice(
            world_id=query.world_id,
            snapshot_id=query.snapshot_id,
            snapshot_hash=query.snapshot_hash,
            pinned_world_revision=query.world_revision,
            value=value,
            resolver_proof=self._proof(query, slice_name, sorted_metadata, scope),
            item_metadata=sorted_metadata,
        )
