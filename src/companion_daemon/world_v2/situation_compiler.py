"""Pure, source-bound Situation compilation for the World v2 `.16` bundle.

Situation is a deterministic read model.  It owns no mutable state and makes no
behavioural or affective decisions; callers must supply an immutable authority
snapshot pinned to one committed world revision.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from datetime import datetime
import hashlib
import hmac
import json
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .attention_authority_schemas import (
    V2AttentionProjection,
    v2_attention_semantic_fingerprint,
)
from .goal_situation_schemas import V2GoalProjection
from .location_authority_schemas import (
    V2LocationProjection,
    v2_location_semantic_fingerprint,
)
from .local_chronology import LocalChronology
from .resource_authority_schemas import (
    ResourceKind,
    V2ResourceProjection,
    v2_resource_semantic_fingerprint,
)
from .resource_authority_reducers import (
    RESOURCE_BAND_POLICY_DIGEST,
    RESOURCE_BAND_POLICY_VERSION,
    derive_resource_band,
)
from .schema_core import FrozenModel, PrivacyClass, canonicalize_json_value
from .schemas import (
    ClockTransitionProjection,
    CommitmentProjection,
    CommittedWorldEventRef,
    LedgerProjection,
    PlanStateProjection,
    plan_authority_binding_hash,
    plan_authority_projection_hash,
)


Availability = Literal["available", "unavailable", "redacted"]
UnavailableReason = Literal[
    "no_authority", "not_applicable", "privacy_ceiling", "budget_truncated"
]
DueRelation = Literal["none", "future", "open", "overdue"]
ResourcePressure = Literal["low", "moderate", "high", "critical"]
TimeSegment = Literal["late_night", "morning", "afternoon", "evening", "night"]

_RESOURCE_KINDS: tuple[ResourceKind, ...] = (
    "cognitive_capacity",
    "physical_energy",
    "social_capacity",
)
_PRIVACY_RANK: dict[PrivacyClass, int] = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            canonicalize_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _serialized_digest(value: object) -> str:
    """Hash an already JSON-shaped public value without changing its offsets."""

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


_TIME_SEGMENT_CATALOG = {
    "late_night": (0, 5),
    "morning": (5, 12),
    "afternoon": (12, 18),
    "evening": (18, 22),
    "night": (22, 24),
}
_RESOURCE_PRESSURE_CATALOG = {
    "depleted": "critical",
    "low": "high",
    "moderate": "moderate",
    "high": "low",
    "full": "low",
}
_ORDERING_POLICY = {
    "goals": ("importance_desc", "due_relation", "goal_id"),
    "resources": ("resource_kind",),
    "sources": ("identity",),
}
_PRIVACY_POLICY = {
    "meet": "strictest",
    "withhold_internal_only": True,
    "redaction_is_not_unavailable": True,
}
_BUDGET_POLICY = {"stable_prefix": True, "minimum_items": 1, "maximum_items": 64}
_VIEWER_POLICY = {
    "version": "situation-viewer-policy.16.0",
    "internal": {
        "viewer_ref": "viewer:world-runtime-internal",
        "privacy_classes": list(_PRIVACY_RANK),
        "max_items": 64,
    },
    "external": {
        "privacy_classes": ["public", "shareable"],
        "max_items": 16,
        "private_grant_capabilities": [],
    },
}
_VIEWER_POLICY_DIGEST = _digest(_VIEWER_POLICY)


class SituationPolicy(FrozenModel):
    situation_policy_version: Literal["situation-policy.16.0"] = "situation-policy.16.0"
    time_segment_catalog_digest: str = Field(min_length=64, max_length=64)
    resource_pressure_policy_digest: str = Field(min_length=64, max_length=64)
    privacy_policy_digest: str = Field(min_length=64, max_length=64)
    ordering_policy_digest: str = Field(min_length=64, max_length=64)
    budget_policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def policy_is_installed(self) -> SituationPolicy:
        if self != default_situation_policy():
            raise ValueError("Situation request does not reference the installed Situation policy")
        return self


def default_situation_policy() -> SituationPolicy:
    # model_construct avoids recursive validation while still returning the
    # exact frozen installed artifact.
    return SituationPolicy.model_construct(
        situation_policy_version="situation-policy.16.0",
        time_segment_catalog_digest=_digest(_TIME_SEGMENT_CATALOG),
        resource_pressure_policy_digest=_digest(_RESOURCE_PRESSURE_CATALOG),
        privacy_policy_digest=_digest(_PRIVACY_POLICY),
        ordering_policy_digest=_digest(_ORDERING_POLICY),
        budget_policy_digest=_digest(_BUDGET_POLICY),
    )


class ViewerScope(FrozenModel):
    scope_kind: Literal["internal", "viewer"]
    viewer_ref: str = Field(min_length=1)
    allowed_privacy_classes: tuple[PrivacyClass, ...]
    max_items_per_collection: int = Field(ge=1, le=64, strict=True)
    viewer_policy_version: Literal["situation-viewer-policy.16.0"] = (
        "situation-viewer-policy.16.0"
    )
    viewer_policy_digest: str = Field(min_length=64, max_length=64)
    viewer_scope_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def scope_is_canonical(self) -> ViewerScope:
        expected_classes = tuple(
            sorted(set(self.allowed_privacy_classes), key=_PRIVACY_RANK.__getitem__)
        )
        if self.allowed_privacy_classes != expected_classes:
            raise ValueError("viewer privacy classes must be canonical and unique")
        if self.scope_kind == "internal" and set(self.allowed_privacy_classes) != set(
            _PRIVACY_RANK
        ):
            raise ValueError("internal viewer must receive the full privacy lattice")
        if self.scope_kind == "internal" and (
            self.viewer_ref != "viewer:world-runtime-internal"
            or self.max_items_per_collection != 64
        ):
            raise ValueError("internal Situation viewer identity is fixed")
        if self.scope_kind == "viewer" and (
            not set(self.allowed_privacy_classes).issubset({"public", "shareable"})
            or self.max_items_per_collection > 16
        ):
            raise ValueError("private external Situation viewer authority is not installed")
        if self.viewer_policy_digest != _VIEWER_POLICY_DIGEST:
            raise ValueError("Situation viewer policy is not installed")
        material = self.model_dump(mode="json", exclude={"viewer_scope_digest"})
        if self.viewer_scope_digest != _digest(material):
            raise ValueError("viewer scope digest is invalid")
        return self


def viewer_scope(
    *,
    viewer_ref: str,
    allowed_privacy_classes: tuple[PrivacyClass, ...],
    max_items_per_collection: int,
) -> ViewerScope:
    ordered = tuple(sorted(set(allowed_privacy_classes), key=_PRIVACY_RANK.__getitem__))
    material = {
        "scope_kind": "viewer",
        "viewer_ref": viewer_ref,
        "allowed_privacy_classes": ordered,
        "max_items_per_collection": max_items_per_collection,
        "viewer_policy_version": "situation-viewer-policy.16.0",
        "viewer_policy_digest": _VIEWER_POLICY_DIGEST,
    }
    return ViewerScope(**material, viewer_scope_digest=_digest(material))


def default_internal_viewer_scope() -> ViewerScope:
    classes = tuple(sorted(_PRIVACY_RANK, key=_PRIVACY_RANK.__getitem__))
    material = {
        "scope_kind": "internal",
        "viewer_ref": "viewer:world-runtime-internal",
        "allowed_privacy_classes": classes,
        "max_items_per_collection": 64,
        "viewer_policy_version": "situation-viewer-policy.16.0",
        "viewer_policy_digest": _VIEWER_POLICY_DIGEST,
    }
    return ViewerScope(**material, viewer_scope_digest=_digest(material))


def request_from_ledger_projection(
    projection: LedgerProjection,
    *,
    actor_ref: str,
    event_resolver: SituationEventResolver,
    viewer: ViewerScope | None = None,
    policy: SituationPolicy | None = None,
) -> SituationCompileRequest:
    """Build the production compiler input from one validated ledger projection.

    Legacy Plan heads without explicit owner authority remain absent.  Current
    heads are included only from their exact immutable accepted-event origin.
    """

    event_by_id: dict[str, CommittedWorldEventRef] = {}

    def source(event_ref: str) -> SourceBinding:
        event = event_by_id.get(event_ref)
        if event is None:
            raise ValueError("Situation head origin is absent from committed event authority")
        if event.world_revision > projection.world_revision:
            raise ValueError("Situation event index returned a future authority")
        return SourceBinding(
            world_id=projection.world_id,
            world_revision=event.world_revision,
            event_ref=event.event_id,
            payload_hash=event.payload_hash,
        )

    locations = tuple(item for item in projection.locations if item.actor_ref == actor_ref)
    attentions = tuple(item for item in projection.attentions if item.actor_ref == actor_ref)
    plans = tuple(
        item
        for item in projection.plans
        if item.owner_actor_ref == actor_ref and item.authority_origin is not None
    )
    if len(locations) > 1 or len(attentions) > 1:
        raise ValueError("Situation projection contains multiple singleton actor heads")
    logical_source: SourceBinding | None = None
    logical_clock: ClockTransitionProjection | None = None
    logical_event_ref: str | None = None
    if projection.logical_time is not None:
        matching_clocks = tuple(
            item
            for item in projection.clock_transition_history
            if item.logical_time_to == projection.logical_time
        )
        if matching_clocks:
            logical_clock = max(
                matching_clocks, key=lambda item: item.computed_world_revision
            )
            logical_event_ref = logical_clock.clock_event_ref
        else:
            start = event_resolver.resolve_initial_world_event_ref(
                at_world_revision=projection.world_revision
            )
            if (
                start.event_type != "WorldStarted"
                or start.logical_time != projection.logical_time
            ):
                raise ValueError("Situation initial logical time authority is invalid")
            event_by_id[start.event_id] = start
            logical_event_ref = start.event_id
    required_event_ids = {
        *(
            item.origin.accepted_event_ref
            for item in projection.goals
            if item.actor_ref == actor_ref
        ),
        *(
            item.origin.accepted_event_ref
            for item in projection.resources
            if item.actor_ref == actor_ref
        ),
        *(
            item.origin.accepted_event_ref
            for item in projection.commitments
            if item.values.owner_ref == actor_ref
        ),
        *(item.authority_origin.accepted_event_ref for item in plans),
    }
    if locations:
        required_event_ids.add(locations[0].origin.accepted_event_ref)
    if attentions:
        required_event_ids.add(attentions[0].origin.accepted_event_ref)
    if logical_event_ref is not None:
        required_event_ids.add(logical_event_ref)
    unresolved = tuple(sorted(required_event_ids - set(event_by_id)))
    event_by_id.update(
        event_resolver.resolve_committed_event_refs(
            unresolved, at_world_revision=projection.world_revision
        )
    )
    if set(event_by_id) != required_event_ids:
        raise ValueError("Situation source resolver did not return every consumed event")
    if logical_event_ref is not None:
        logical_source = source(logical_event_ref)
    commitments = tuple(
        BoundCommitmentHead(
            source=source(item.origin.accepted_event_ref), actor_ref=actor_ref, head=item
        )
        for item in projection.commitments
        if item.values.owner_ref == actor_ref
    )
    goals = tuple(
        BoundGoalHead(source=source(item.origin.accepted_event_ref), head=item)
        for item in projection.goals
        if item.actor_ref == actor_ref
    )
    location = (
        BoundLocationHead(
            source=source(locations[0].origin.accepted_event_ref), head=locations[0]
        )
        if locations
        else None
    )
    resources = tuple(
        BoundResourceHead(source=source(item.origin.accepted_event_ref), head=item)
        for item in projection.resources
        if item.actor_ref == actor_ref
    )
    attention = (
        BoundAttentionHead(
            source=source(attentions[0].origin.accepted_event_ref), head=attentions[0]
        )
        if attentions
        else None
    )
    bound_plans = tuple(
        BoundPlanHead(
            source=source(item.authority_origin.accepted_event_ref),
            actor_ref=actor_ref,
            projection_hash=_digest(item.model_dump(mode="json")),
            head=item,
        )
        for item in plans
    )
    consumed_events = tuple(
        sorted(event_by_id.values(), key=lambda item: (item.world_revision, item.event_id))
    )
    snapshot = SituationAuthoritySnapshot(
        world_id=projection.world_id,
        actor_ref=actor_ref,
        pinned_world_revision=projection.world_revision,
        logical_time=projection.logical_time,
        logical_time_source=logical_source,
        logical_clock_projection=logical_clock,
        committed_events=consumed_events,
        goals=goals,
        location=location,
        resources=resources,
        attention=attention,
        plans=bound_plans,
        commitments=commitments,
    )
    return SituationCompileRequest(
        world_id=projection.world_id,
        actor_ref=actor_ref,
        pinned_world_revision=projection.world_revision,
        logical_time=projection.logical_time,
        authority_snapshot=snapshot,
        policy=policy or default_situation_policy(),
        viewer_scope=viewer or default_internal_viewer_scope(),
    )


class SituationEventResolver(Protocol):
    def resolve_committed_event_refs(
        self, event_ids: tuple[str, ...], *, at_world_revision: int
    ) -> Mapping[str, CommittedWorldEventRef]: ...

    def resolve_initial_world_event_ref(
        self, *, at_world_revision: int
    ) -> CommittedWorldEventRef: ...


class SourceBinding(FrozenModel):
    world_id: str = Field(min_length=1)
    world_revision: int = Field(ge=1)
    event_ref: str = Field(min_length=1)
    payload_hash: str = Field(min_length=64, max_length=64)


def _require_exact_source(
    source: SourceBinding, event_by_id: dict[str, CommittedWorldEventRef]
) -> None:
    event = event_by_id.get(source.event_ref)
    if event is None or (
        event.world_revision != source.world_revision
        or event.payload_hash != source.payload_hash
    ):
        raise ValueError("Situation source binding does not match committed event authority")


class BoundGoalHead(FrozenModel):
    source: SourceBinding
    head: V2GoalProjection


class BoundLocationHead(FrozenModel):
    source: SourceBinding
    head: V2LocationProjection


class BoundResourceHead(FrozenModel):
    source: SourceBinding
    head: V2ResourceProjection


class BoundAttentionHead(FrozenModel):
    source: SourceBinding
    head: V2AttentionProjection


class BoundPlanHead(FrozenModel):
    source: SourceBinding
    actor_ref: str = Field(min_length=1)
    projection_hash: str = Field(min_length=64, max_length=64)
    head: PlanStateProjection

    @model_validator(mode="after")
    def projection_hash_is_derived(self) -> BoundPlanHead:
        if self.projection_hash != _digest(self.head.model_dump(mode="json")):
            raise ValueError("Plan snapshot projection hash is invalid")
        return self


class BoundCommitmentHead(FrozenModel):
    source: SourceBinding
    actor_ref: str = Field(min_length=1)
    head: CommitmentProjection


class AttentionExpiryDueBinding(FrozenModel):
    trigger_ref: str = Field(min_length=1)
    trigger_world_revision: int = Field(ge=1)
    trigger_payload_hash: str = Field(min_length=64, max_length=64)
    actor_ref: str = Field(min_length=1)
    attention_entity_revision: int = Field(ge=1)
    attention_semantic_fingerprint: str = Field(min_length=64, max_length=64)
    attention_event_ref: str = Field(min_length=1)
    clock_event_ref: str = Field(min_length=1)
    clock_entity_revision: int = Field(ge=1)
    clock_world_revision: int = Field(ge=1)
    clock_payload_hash: str = Field(min_length=64, max_length=64)
    clock_projection_hash: str = Field(min_length=64, max_length=64)
    policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def chronology_is_possible(self) -> AttentionExpiryDueBinding:
        if self.clock_world_revision >= self.trigger_world_revision:
            raise ValueError("Attention due trigger must follow its bound Clock event")
        return self


class SituationAuthoritySnapshot(FrozenModel):
    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    pinned_world_revision: int = Field(ge=0)
    logical_time: datetime | None
    logical_time_source: SourceBinding | None = None
    logical_clock_projection: ClockTransitionProjection | None = None
    committed_events: tuple[CommittedWorldEventRef, ...] = ()
    goals: tuple[BoundGoalHead, ...] = ()
    location: BoundLocationHead | None = None
    resources: tuple[BoundResourceHead, ...] = ()
    attention: BoundAttentionHead | None = None
    attention_expiry_due: tuple[AttentionExpiryDueBinding, ...] = ()
    plans: tuple[BoundPlanHead, ...] = ()
    commitments: tuple[BoundCommitmentHead, ...] = ()

    @model_validator(mode="after")
    def heads_share_snapshot_authority(self) -> SituationAuthoritySnapshot:
        if self.attention_expiry_due:
            raise ValueError("attention_expiry_authority_not_installed")
        event_by_id = {item.event_id: item for item in self.committed_events}
        if len(event_by_id) != len(self.committed_events):
            raise ValueError("Situation snapshot contains duplicate committed events")
        if self.logical_time is None:
            if self.logical_time_source is not None or self.logical_clock_projection is not None:
                raise ValueError("unstarted Situation cannot claim a logical-time authority")
        else:
            if self.logical_time_source is None:
                raise ValueError("started Situation requires a logical-time authority")
            logical_event = event_by_id.get(self.logical_time_source.event_ref)
            if logical_event is None or logical_event.logical_time != self.logical_time:
                raise ValueError("Situation logical time is not committed authority")
            if logical_event.event_type == "ClockAdvanced":
                clock = self.logical_clock_projection
                if clock is None or (
                    clock.clock_event_ref != logical_event.event_id
                    or clock.computed_world_revision != logical_event.world_revision
                    or clock.payload_hash != logical_event.payload_hash
                    or clock.logical_time_to != self.logical_time
                ):
                    raise ValueError("Situation logical time lacks exact Clock authority")
            elif logical_event.event_type != "WorldStarted" or self.logical_clock_projection is not None:
                raise ValueError("Situation logical time source kind is unsupported")
        bound = (*self.goals, *self.resources)
        if self.location is not None:
            bound = (*bound, self.location)
        if self.attention is not None:
            bound = (*bound, self.attention)
        for item in bound:
            if item.source.world_id != self.world_id:
                raise ValueError("Situation authority head belongs to another world")
            if item.source.world_revision > self.pinned_world_revision:
                raise ValueError("Situation authority head comes from a future revision")
            if item.head.actor_ref != self.actor_ref:
                raise ValueError("Situation authority head belongs to another actor")
            if item.source.event_ref != item.head.origin.accepted_event_ref:
                raise ValueError("Situation source event does not match head origin")
            _require_exact_source(item.source, event_by_id)
            source_event = event_by_id[item.source.event_ref]
            if isinstance(item, BoundGoalHead):
                if not source_event.event_type.startswith("V2Goal"):
                    raise ValueError("Goal source event type is not authoritative")
            elif isinstance(item, BoundResourceHead):
                expected_fingerprint = v2_resource_semantic_fingerprint(
                    actor_ref=item.head.actor_ref,
                    resource_kind=item.head.resource_kind,
                    values=item.head.values,
                    policy_refs=item.head.origin.policy_refs,
                )
                if (
                    item.head.semantic_fingerprint != expected_fingerprint
                    or not source_event.event_type.startswith("V2Resource")
                ):
                    raise ValueError("Resource head/source authority is invalid")
            elif isinstance(item, BoundLocationHead):
                expected_fingerprint = v2_location_semantic_fingerprint(
                    actor_ref=item.head.actor_ref,
                    values=item.head.values,
                    policy_refs=item.head.origin.policy_refs,
                )
                if (
                    item.head.semantic_fingerprint != expected_fingerprint
                    or not source_event.event_type.startswith("V2Location")
                ):
                    raise ValueError("Location head/source authority is invalid")
            else:
                expected_fingerprint = v2_attention_semantic_fingerprint(
                    actor_ref=item.head.actor_ref,
                    values=item.head.values,
                    policy_refs=item.head.origin.policy_refs,
                )
                if (
                    item.head.semantic_fingerprint != expected_fingerprint
                    or not source_event.event_type.startswith("V2Attention")
                ):
                    raise ValueError("Attention head/source authority is invalid")
        for item in (*self.plans, *self.commitments):
            if item.source.world_id != self.world_id:
                raise ValueError("Situation authority head belongs to another world")
            if item.source.world_revision > self.pinned_world_revision:
                raise ValueError("Situation authority head comes from a future revision")
            if item.actor_ref != self.actor_ref:
                raise ValueError("Situation authority head belongs to another actor")
        plan_event_types = {
            "planned": {"ActivityPlanned"},
            "active": {"ActivityStarted", "ActivityResumed"},
            "paused": {"ActivityPaused"},
            "completed": {"ActivityCompleted"},
            "abandoned": {"ActivityAbandoned"},
        }
        for item in self.plans:
            origin = item.head.authority_origin
            owner = item.head.owner_actor_ref
            if origin is None or owner is None or owner == "legacy:unknown-owner":
                raise ValueError("Situation Plan lacks current owner authority")
            if item.actor_ref != owner:
                raise ValueError("Plan wrapper does not bind its authoritative owner")
            if item.source.event_ref != origin.accepted_event_ref:
                raise ValueError("Situation Plan source does not match authority origin")
            _require_exact_source(item.source, event_by_id)
            source_event = event_by_id[item.source.event_ref]
            if (
                source_event.event_type != origin.accepted_event_type
                or source_event.event_type not in plan_event_types[item.head.status]
                or source_event.world_revision != origin.accepted_world_revision
                or source_event.payload_hash != origin.accepted_payload_hash
                or source_event.logical_time != origin.accepted_at
                or origin.authority_projection_hash
                != plan_authority_projection_hash(item.head)
                or origin.binding_hash
                != plan_authority_binding_hash(
                    plan_id=item.head.plan_id,
                    owner_actor_ref=owner,
                    entity_revision=item.head.entity_revision,
                    transition_id=origin.transition_id,
                    event_type=source_event.event_type,
                    accepted_event_ref=source_event.event_id,
                    accepted_world_revision=source_event.world_revision,
                    accepted_payload_hash=source_event.payload_hash,
                    accepted_at=source_event.logical_time,
                    projection_hash=origin.authority_projection_hash,
                )
            ):
                raise ValueError("Situation Plan authority binding is invalid")
        for item in self.commitments:
            if item.actor_ref != item.head.values.owner_ref:
                raise ValueError("Commitment wrapper does not bind its authoritative owner")
            if item.source.event_ref != item.head.origin.accepted_event_ref:
                raise ValueError("Situation source event does not match head origin")
            _require_exact_source(item.source, event_by_id)
        if self.logical_time_source is not None:
            _require_exact_source(self.logical_time_source, event_by_id)
        goal_ids = tuple(item.head.goal_id for item in self.goals)
        if len(goal_ids) != len(set(goal_ids)):
            raise ValueError("Situation snapshot contains duplicate Goal heads")
        resource_kinds = tuple(item.head.resource_kind for item in self.resources)
        if len(resource_kinds) != len(set(resource_kinds)):
            raise ValueError("Situation snapshot contains duplicate Resource heads")
        plan_ids = tuple(item.head.plan_id for item in self.plans)
        if len(plan_ids) != len(set(plan_ids)):
            raise ValueError("Situation snapshot contains duplicate Plan heads")
        commitment_ids = tuple(item.head.commitment_id for item in self.commitments)
        if len(commitment_ids) != len(set(commitment_ids)):
            raise ValueError("Situation snapshot contains duplicate Commitment heads")
        trigger_refs = tuple(item.trigger_ref for item in self.attention_expiry_due)
        if len(trigger_refs) != len(set(trigger_refs)):
            raise ValueError("Situation snapshot contains duplicate Attention due triggers")
        for item in self.resources:
            values = item.head.values
            if (
                values.band_policy_version != RESOURCE_BAND_POLICY_VERSION
                or values.band_policy_digest != RESOURCE_BAND_POLICY_DIGEST
                or values.derived_band != derive_resource_band(values.value_bp)
            ):
                raise ValueError("Resource head does not use the installed pinned band policy")
        return self


class SituationCompileRequest(FrozenModel):
    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    pinned_world_revision: int = Field(ge=0)
    logical_time: datetime | None
    authority_snapshot: SituationAuthoritySnapshot
    policy: SituationPolicy
    viewer_scope: ViewerScope

    @model_validator(mode="after")
    def request_matches_snapshot(self) -> SituationCompileRequest:
        if (
            self.world_id != self.authority_snapshot.world_id
            or self.actor_ref != self.authority_snapshot.actor_ref
            or self.pinned_world_revision != self.authority_snapshot.pinned_world_revision
            or self.logical_time != self.authority_snapshot.logical_time
        ):
            raise ValueError("Situation request does not match its pinned authority snapshot")
        return self


class SourceRevision(FrozenModel):
    identity: str = Field(min_length=1)
    domain: Literal[
        "goal",
        "location",
        "resource",
        "attention",
        "attention_due",
        "clock",
        "plan",
        "commitment",
        "logical_time",
    ]
    entity_ref: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    source_world_revision: int = Field(ge=1)
    event_ref: str = Field(min_length=1)
    payload_hash: str = Field(min_length=64, max_length=64)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def identity_is_derived(self) -> SourceRevision:
        if self.identity != f"{self.domain}:{self.entity_ref}":
            raise ValueError("Situation source identity is not canonical")
        return self


class LocationSlice(FrozenModel):
    availability: Availability
    reason: UnavailableReason | None = None
    location_ref: str | None = None
    zone_ref: str | None = None
    scene_visibility: Literal["private", "shareable", "public"] | None = None
    privacy_class: PrivacyClass | None = None


class GoalSlice(FrozenModel):
    availability: Availability = "available"
    reason: UnavailableReason | None = None
    goal_id: str | None = None
    status: Literal["active", "paused", "blocked"] | None = None
    importance_bp: int | None = Field(default=None, ge=0, le=10_000)
    progress_bp: int | None = Field(default=None, ge=0, le=10_000)
    due_relation: DueRelation | None = None
    blocker_count: int | None = Field(default=None, ge=0)
    privacy_class: PrivacyClass | None = None


class ResourceSlice(FrozenModel):
    resource_kind: ResourceKind
    availability: Availability
    reason: UnavailableReason | None = None
    value_bp: int | None = Field(default=None, ge=0, le=10_000)
    derived_band: Literal["depleted", "low", "moderate", "high", "full"] | None = None
    band_policy_version: str | None = None
    band_policy_digest: str | None = None
    privacy_class: PrivacyClass | None = None


class PressureSlice(FrozenModel):
    availability: Availability
    reason: UnavailableReason | None = None
    value: ResourcePressure | None = None


class AttentionSlice(FrozenModel):
    availability: Availability
    reason: UnavailableReason | None = None
    mode: str | None = None
    focus_ref: str | None = None
    allocation_bp: int | None = Field(default=None, ge=0, le=10_000)
    interruptibility_bp: int | None = Field(default=None, ge=0, le=10_000)
    expires_at: datetime | None = None
    privacy_class: PrivacyClass | None = None
    transition_due: bool = False
    due_trigger_ref: str | None = None


class ActivitySlice(FrozenModel):
    availability: Availability = "available"
    reason: UnavailableReason | None = None
    plan_id: str | None = None
    activity_kind: str | None = None
    status: Literal["planned", "active", "paused"] | None = None
    importance_bp: int | None = Field(default=None, ge=0, le=10_000)
    participant_refs: tuple[str, ...] = ()
    location_ref: str | None = None
    privacy_class: PrivacyClass | None = None


class CommitmentSlice(FrozenModel):
    availability: Availability = "available"
    reason: UnavailableReason | None = None
    commitment_id: str | None = None
    status: Literal["open", "due"] | None = None
    importance_bp: int | None = Field(default=None, ge=0, le=10_000)
    due_relation: DueRelation | None = None
    privacy_class: PrivacyClass | None = None


class SocialEnvironmentSlice(FrozenModel):
    availability: Availability
    reason: UnavailableReason | None = None
    relation: Literal["alone", "with_others"] | None = None
    participant_refs: tuple[str, ...] = ()
    privacy_class: PrivacyClass | None = None


class PlanRelationSlice(FrozenModel):
    availability: Availability
    reason: UnavailableReason | None = None
    relation: Literal[
        "active", "paused", "planned_future", "planned_open", "planned_overdue"
    ] | None = None
    plan_id: str | None = None
    privacy_class: PrivacyClass | None = None


class SituationProjection(FrozenModel):
    world_id: str = Field(min_length=1)
    authority_snapshot_hash: str = Field(min_length=64, max_length=64)
    situation_policy_input_hash: str = Field(min_length=64, max_length=64)
    compiled_at_world_revision: int = Field(ge=0)
    actor_ref: str = Field(min_length=1)
    logical_time: datetime | None
    time_segment: TimeSegment | None
    location_slice: LocationSlice
    activity_slices: tuple[ActivitySlice, ...]
    goal_slices: tuple[GoalSlice, ...]
    resource_slices: tuple[ResourceSlice, ...]
    resource_pressure: PressureSlice
    attention_slice: AttentionSlice
    social_environment: SocialEnvironmentSlice
    plan_relation: PlanRelationSlice
    commitment_slices: tuple[CommitmentSlice, ...]
    scene_visibility: Literal["private", "shareable", "public"] | None
    source_revisions: tuple[SourceRevision, ...]
    policy_versions: tuple[str, ...]
    internal_semantic_hash: str = Field(min_length=64, max_length=64)


class ViewerSituationProjection(FrozenModel):
    source_internal_semantic_hash: str = Field(min_length=64, max_length=64)
    viewer_scope_digest: str = Field(min_length=64, max_length=64)
    location_slice: LocationSlice
    activity_slices: tuple[ActivitySlice, ...]
    goal_slices: tuple[GoalSlice, ...]
    resource_slices: tuple[ResourceSlice, ...]
    resource_pressure: PressureSlice
    attention_slice: AttentionSlice
    social_environment: SocialEnvironmentSlice
    plan_relation: PlanRelationSlice
    commitment_slices: tuple[CommitmentSlice, ...]
    truncation_reasons: tuple[str, ...]
    capsule_budget_policy_digest: str = Field(min_length=64, max_length=64)
    viewer_projection_hash: str = Field(min_length=64, max_length=64)


class SituationCompileResult(FrozenModel):
    internal: SituationProjection | None
    viewer_projection: ViewerSituationProjection


class SituationCompileCache:
    """Disposable authenticated cache; the key is supplied by the owning runtime."""

    def __init__(self, *, signing_key: bytes, max_entries: int = 128) -> None:
        if len(signing_key) < 32:
            raise ValueError("Situation cache signing key must contain at least 32 bytes")
        if max_entries < 1:
            raise ValueError("Situation cache must retain at least one entry")
        self._signing_key = signing_key
        self._max_entries = max_entries
        self._internal_values: OrderedDict[str, str] = OrderedDict()

    def _signature(self, *, cache_kind: str, key: str, value: object) -> str:
        material = json.dumps(
            {"cache_kind": cache_kind, "cache_key": key, "value": value},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hmac.new(self._signing_key, material, hashlib.sha256).hexdigest()

    def get_internal(self, key: str) -> SituationProjection | None:
        raw = self._internal_values.get(key)
        if raw is None:
            return None
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict) or envelope.get("cache_key") != key:
                raise ValueError("internal Situation cache key mismatch")
            if not hmac.compare_digest(
                str(envelope.get("signature", "")),
                self._signature(
                    cache_kind="internal", key=key, value=envelope.get("value")
                ),
            ):
                raise ValueError("internal Situation cache signature is invalid")
            value = SituationProjection.model_validate_json(
                json.dumps(envelope.get("value"), ensure_ascii=False, separators=(",", ":"))
            )
        except (TypeError, ValueError):
            self._internal_values.pop(key, None)
            return None
        material = value.model_dump(mode="json", exclude={"internal_semantic_hash"})
        if value.internal_semantic_hash != _serialized_digest(material):
            self._internal_values.pop(key, None)
            return None
        self._internal_values.move_to_end(key)
        return value

    def put_internal(self, key: str, value: SituationProjection) -> None:
        dumped = value.model_dump(mode="json")
        self._internal_values[key] = json.dumps(
            {
                "cache_key": key,
                "value": dumped,
                "signature": self._signature(
                    cache_kind="internal", key=key, value=dumped
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._internal_values.move_to_end(key)
        while len(self._internal_values) > self._max_entries:
            self._internal_values.popitem(last=False)


class SituationCompiler:
    """Compile and redact a pinned authority snapshot without I/O or mutation."""

    def __init__(
        self,
        cache: SituationCompileCache | None = None,
        *,
        local_chronology: LocalChronology | None = None,
    ) -> None:
        self._cache = cache
        self._local_chronology = local_chronology or LocalChronology()

    def compile(self, request: SituationCompileRequest) -> SituationCompileResult:
        # Revalidate so model_copy cannot bypass the strict public seam.
        request = SituationCompileRequest.model_validate(request.model_dump(mode="python"))
        authority_snapshot_hash = _digest(_canonical_snapshot_material(request.authority_snapshot))
        situation_policy_input_hash = _digest(
            request.policy.model_dump(mode="json", exclude={"budget_policy_digest"})
        )
        internal_key = _digest(
            {
                "authority_snapshot_hash": authority_snapshot_hash,
                "situation_policy_input_hash": situation_policy_input_hash,
                "local_timezone": self._local_chronology.timezone_name,
            }
        )
        cached_internal = (
            self._cache.get_internal(internal_key) if self._cache is not None else None
        )
        if cached_internal is not None and (
            cached_internal.world_id != request.world_id
            or cached_internal.actor_ref != request.actor_ref
            or cached_internal.compiled_at_world_revision != request.pinned_world_revision
            or cached_internal.logical_time != request.logical_time
            or cached_internal.authority_snapshot_hash != authority_snapshot_hash
            or cached_internal.situation_policy_input_hash != situation_policy_input_hash
        ):
            cached_internal = None
        if cached_internal is None:
            internal = self._compile_internal(request)
            if self._cache is not None:
                self._cache.put_internal(internal_key, internal)
        else:
            internal = cached_internal
        # Privacy projection is deliberately recomputed on every request.  A
        # cache hit may skip internal aggregation, never viewer authorization.
        recomputed_viewer = self._project_viewer(internal, request.viewer_scope, request.policy)
        viewer = recomputed_viewer
        return SituationCompileResult(
            internal=internal if request.viewer_scope.scope_kind == "internal" else None,
            viewer_projection=viewer,
        )

    def _compile_internal(self, request: SituationCompileRequest) -> SituationProjection:
        snapshot = request.authority_snapshot
        sources: list[SourceRevision] = []
        if snapshot.logical_time_source is not None:
            logical_event = next(
                item
                for item in snapshot.committed_events
                if item.event_id == snapshot.logical_time_source.event_ref
            )
            sources.append(
                SourceRevision(
                    identity=f"logical_time:{logical_event.event_id}",
                    domain="logical_time",
                    entity_ref=logical_event.event_id,
                    entity_revision=(
                        snapshot.logical_clock_projection.computed_world_revision
                        if snapshot.logical_clock_projection is not None
                        else 1
                    ),
                    source_world_revision=logical_event.world_revision,
                    event_ref=logical_event.event_id,
                    payload_hash=logical_event.payload_hash,
                    semantic_fingerprint=(
                        _digest(snapshot.logical_clock_projection.model_dump(mode="json"))
                        if snapshot.logical_clock_projection is not None
                        else _digest(logical_event.model_dump(mode="json"))
                    ),
                )
            )
        goals: list[GoalSlice] = []
        for bound in snapshot.goals:
            if bound.head.values.status in {"completed", "abandoned", "expired"}:
                continue
            values = bound.head.values
            goals.append(
                GoalSlice(
                    goal_id=bound.head.goal_id,
                    status=values.status,
                    importance_bp=values.importance_bp,
                    progress_bp=values.progress_bp,
                    due_relation=_due_relation(values.due_window, snapshot.logical_time),
                    blocker_count=len(values.blockers),
                    privacy_class=values.privacy_class,
                )
            )
            sources.append(
                _source_revision(
                    "goal", bound.head.goal_id, bound.head.entity_revision,
                    bound.source, bound.head.semantic_fingerprint,
                )
            )
        goals.sort(key=lambda item: (-int(item.importance_bp or 0), item.due_relation or "", item.goal_id or ""))

        if snapshot.location is None:
            location = LocationSlice(availability="unavailable", reason="no_authority")
            scene_visibility = None
        else:
            head = snapshot.location.head
            location = LocationSlice(
                availability="available",
                location_ref=head.values.location_ref,
                zone_ref=head.values.zone_ref,
                scene_visibility=head.values.scene_visibility,
                privacy_class=head.values.privacy_class,
            )
            scene_visibility = head.values.scene_visibility
            sources.append(
                _source_revision(
                    "location", head.actor_ref, head.entity_revision,
                    snapshot.location.source, head.semantic_fingerprint,
                )
            )

        resources_by_kind = {item.head.resource_kind: item for item in snapshot.resources}
        resource_slices: list[ResourceSlice] = []
        pressure_values: list[ResourcePressure] = []
        for kind in _RESOURCE_KINDS:
            bound = resources_by_kind.get(kind)
            if bound is None:
                resource_slices.append(
                    ResourceSlice(
                        resource_kind=kind, availability="unavailable", reason="no_authority"
                    )
                )
                continue
            values = bound.head.values
            resource_slices.append(
                ResourceSlice(
                    resource_kind=kind,
                    availability="available",
                    value_bp=values.value_bp,
                    derived_band=values.derived_band,
                    band_policy_version=values.band_policy_version,
                    band_policy_digest=values.band_policy_digest,
                    privacy_class=values.privacy_class,
                )
            )
            pressure_values.append(_RESOURCE_PRESSURE_CATALOG[values.derived_band])
            sources.append(
                _source_revision(
                    "resource", f"{bound.head.actor_ref}:{kind}", bound.head.entity_revision,
                    bound.source, bound.head.semantic_fingerprint,
                )
            )
        pressure = (
            PressureSlice(availability="unavailable", reason="no_authority")
            if not pressure_values
            else PressureSlice(
                availability="available",
                value=max(pressure_values, key=("low", "moderate", "high", "critical").index),
            )
        )

        attention = self._attention_slice(snapshot, sources)
        activities, social, plan_relation = self._activity_slices(snapshot, sources)
        commitments = self._commitment_slices(snapshot, sources)
        policies = (
            request.policy.situation_policy_version,
            request.policy.time_segment_catalog_digest,
            request.policy.resource_pressure_policy_digest,
            request.policy.privacy_policy_digest,
            request.policy.ordering_policy_digest,
        )
        local_time = self._local_chronology.localize(request.logical_time)
        draft = SituationProjection(
            world_id=request.world_id,
            authority_snapshot_hash=_digest(_canonical_snapshot_material(snapshot)),
            situation_policy_input_hash=_digest(
                request.policy.model_dump(mode="json", exclude={"budget_policy_digest"})
            ),
            compiled_at_world_revision=request.pinned_world_revision,
            actor_ref=request.actor_ref,
            logical_time=local_time,
            time_segment=_time_segment(local_time),
            location_slice=location,
            activity_slices=activities,
            goal_slices=tuple(goals),
            resource_slices=tuple(resource_slices),
            resource_pressure=pressure,
            attention_slice=attention,
            social_environment=social,
            plan_relation=plan_relation,
            commitment_slices=commitments,
            scene_visibility=scene_visibility,
            source_revisions=tuple(sorted(sources, key=lambda item: item.identity)),
            policy_versions=policies,
            internal_semantic_hash="0" * 64,
        )
        material = draft.model_dump(mode="json", exclude={"internal_semantic_hash"})
        return draft.model_copy(update={"internal_semantic_hash": _serialized_digest(material)})

    def _attention_slice(
        self, snapshot: SituationAuthoritySnapshot, sources: list[SourceRevision]
    ) -> AttentionSlice:
        if snapshot.attention is None:
            if snapshot.attention_expiry_due:
                raise ValueError("Attention due trigger exists without an Attention head")
            return AttentionSlice(availability="unavailable", reason="no_authority")
        bound = snapshot.attention
        head = bound.head
        matching: list[AttentionExpiryDueBinding] = []
        for due in snapshot.attention_expiry_due:
            if (
                due.actor_ref != snapshot.actor_ref
                or due.attention_entity_revision != head.entity_revision
                or due.attention_semantic_fingerprint != head.semantic_fingerprint
                or due.attention_event_ref != bound.source.event_ref
            ):
                raise ValueError("Attention due trigger does not bind the current Attention head")
            if due.trigger_world_revision > snapshot.pinned_world_revision:
                raise ValueError("Attention due trigger comes from a future revision")
            if head.values.expires_at is None or snapshot.logical_time is None or (
                snapshot.logical_time < head.values.expires_at
            ):
                raise ValueError("Attention due trigger precedes the committed Attention expiry")
            matching.append(due)
        if len(matching) > 1:
            raise ValueError("current Attention revision has multiple due triggers")
        values = head.values
        sources.append(
            _source_revision(
                "attention", head.actor_ref, head.entity_revision,
                bound.source, head.semantic_fingerprint,
            )
        )
        if matching:
            due = matching[0]
            sources.append(
                SourceRevision(
                    identity=f"attention_due:{due.trigger_ref}",
                    domain="attention_due",
                    entity_ref=due.trigger_ref,
                    entity_revision=1,
                    source_world_revision=due.trigger_world_revision,
                    event_ref=due.trigger_ref,
                    payload_hash=due.trigger_payload_hash,
                    semantic_fingerprint=_digest(due.model_dump(mode="json")),
                )
            )
            sources.append(
                SourceRevision(
                    identity=f"clock:{due.clock_event_ref}",
                    domain="clock",
                    entity_ref=due.clock_event_ref,
                    entity_revision=due.clock_entity_revision,
                    source_world_revision=due.clock_world_revision,
                    event_ref=due.clock_event_ref,
                    payload_hash=due.clock_payload_hash,
                    semantic_fingerprint=due.clock_projection_hash,
                )
            )
        return AttentionSlice(
            availability="available",
            mode=values.mode,
            focus_ref=values.focus_ref,
            allocation_bp=values.allocation_bp,
            interruptibility_bp=values.interruptibility_bp,
            expires_at=values.expires_at,
            privacy_class=values.privacy_class,
            transition_due=bool(matching),
            due_trigger_ref=matching[0].trigger_ref if matching else None,
        )

    def _activity_slices(
        self, snapshot: SituationAuthoritySnapshot, sources: list[SourceRevision]
    ) -> tuple[tuple[ActivitySlice, ...], SocialEnvironmentSlice, PlanRelationSlice]:
        active = [
            item for item in snapshot.plans if item.head.status in {"planned", "active", "paused"}
        ]
        active.sort(key=lambda item: (-item.head.importance_bp, item.head.plan_id))
        activities: list[ActivitySlice] = []
        participants: set[str] = set()
        for item in active:
            head = item.head
            canonical_participants = tuple(sorted(set(head.participant_refs)))
            participants.update(canonical_participants)
            activities.append(
                ActivitySlice(
                    plan_id=head.plan_id,
                    activity_kind=head.activity_kind,
                    status=head.status,
                    importance_bp=head.importance_bp,
                    participant_refs=canonical_participants,
                    location_ref=head.location_ref,
                    privacy_class=head.privacy_class,
                )
            )
            sources.append(
                SourceRevision(
                    identity=f"plan:{head.plan_id}",
                    domain="plan",
                    entity_ref=head.plan_id,
                    entity_revision=head.entity_revision,
                    source_world_revision=item.source.world_revision,
                    event_ref=item.source.event_ref,
                    payload_hash=item.source.payload_hash,
                    semantic_fingerprint=item.projection_hash,
                )
            )
        if not active:
            social = SocialEnvironmentSlice(
                availability="unavailable", reason="no_authority"
            )
            relation = PlanRelationSlice(availability="unavailable", reason="no_authority")
        else:
            strictest_privacy = max(
                (item.head.privacy_class for item in active), key=_PRIVACY_RANK.__getitem__
            )
            social = SocialEnvironmentSlice(
                availability="available",
                relation="with_others" if participants else "alone",
                participant_refs=tuple(sorted(participants)),
                privacy_class=strictest_privacy,
            )
            primary = active[0].head
            if primary.status == "active":
                relation_value = "active"
            elif primary.status == "paused":
                relation_value = "paused"
            else:
                due = _due_relation(primary.scheduled_window, snapshot.logical_time)
                relation_value = {
                    "none": "planned_future",
                    "future": "planned_future",
                    "open": "planned_open",
                    "overdue": "planned_overdue",
                }[due]
            relation = PlanRelationSlice(
                availability="available",
                relation=relation_value,
                plan_id=primary.plan_id,
                privacy_class=primary.privacy_class,
            )
        return tuple(activities), social, relation

    def _commitment_slices(
        self, snapshot: SituationAuthoritySnapshot, sources: list[SourceRevision]
    ) -> tuple[CommitmentSlice, ...]:
        active = [item for item in snapshot.commitments if item.head.values.status in {"open", "due"}]
        active.sort(
            key=lambda item: (
                item.head.values.due_window.closes_at,
                item.head.commitment_id,
            )
        )
        result: list[CommitmentSlice] = []
        for item in active:
            head = item.head
            result.append(
                CommitmentSlice(
                    commitment_id=head.commitment_id,
                    status=head.values.status,
                    importance_bp=head.values.importance_bp,
                    due_relation=_due_relation(head.values.due_window, snapshot.logical_time),
                    privacy_class=head.values.privacy_class,
                )
            )
            sources.append(
                SourceRevision(
                    identity=f"commitment:{head.commitment_id}",
                    domain="commitment",
                    entity_ref=head.commitment_id,
                    entity_revision=head.entity_revision,
                    source_world_revision=item.source.world_revision,
                    event_ref=item.source.event_ref,
                    payload_hash=item.source.payload_hash,
                    semantic_fingerprint=head.semantic_fingerprint,
                )
            )
        return tuple(result)

    def _project_viewer(
        self, internal: SituationProjection, scope: ViewerScope, policy: SituationPolicy
    ) -> ViewerSituationProjection:
        location = _redact_location(internal.location_slice, scope)
        activities = tuple(_redact_activity(item, scope) for item in internal.activity_slices)
        goals = tuple(_redact_goal(item, scope) for item in internal.goal_slices)
        resources = tuple(_redact_resource(item, scope) for item in internal.resource_slices)
        attention = _redact_attention(internal.attention_slice, scope)
        commitments = tuple(
            _redact_commitment(item, scope) for item in internal.commitment_slices
        )
        truncations: list[str] = []
        if len(goals) > scope.max_items_per_collection:
            goals = goals[: scope.max_items_per_collection]
            truncations.append("goal_slices:budget_truncated")
        if len(activities) > scope.max_items_per_collection:
            activities = activities[: scope.max_items_per_collection]
            truncations.append("activity_slices:budget_truncated")
        if len(resources) > scope.max_items_per_collection:
            resources = resources[: scope.max_items_per_collection]
            truncations.append("resource_slices:budget_truncated")
        if len(commitments) > scope.max_items_per_collection:
            commitments = commitments[: scope.max_items_per_collection]
            truncations.append("commitment_slices:budget_truncated")
        pressure = internal.resource_pressure
        if any(item.availability == "redacted" for item in resources):
            pressure = PressureSlice(availability="redacted", reason="privacy_ceiling")
        draft = ViewerSituationProjection(
            source_internal_semantic_hash=internal.internal_semantic_hash,
            viewer_scope_digest=scope.viewer_scope_digest,
            location_slice=location,
            activity_slices=activities,
            goal_slices=goals,
            resource_slices=resources,
            resource_pressure=pressure,
            attention_slice=attention,
            social_environment=_redact_social(internal.social_environment, scope),
            plan_relation=_redact_plan_relation(internal.plan_relation, scope),
            commitment_slices=commitments,
            truncation_reasons=tuple(truncations),
            capsule_budget_policy_digest=policy.budget_policy_digest,
            viewer_projection_hash="0" * 64,
        )
        material = draft.model_dump(mode="json", exclude={"viewer_projection_hash"})
        return draft.model_copy(update={"viewer_projection_hash": _digest(material)})


def _source_revision(
    domain: Literal["goal", "location", "resource", "attention"],
    entity_ref: str,
    entity_revision: int,
    source: SourceBinding,
    semantic_fingerprint: str,
) -> SourceRevision:
    return SourceRevision(
        identity=f"{domain}:{entity_ref}",
        domain=domain,
        entity_ref=entity_ref,
        entity_revision=entity_revision,
        source_world_revision=source.world_revision,
        event_ref=source.event_ref,
        payload_hash=source.payload_hash,
        semantic_fingerprint=semantic_fingerprint,
    )


def _canonical_snapshot_material(snapshot: SituationAuthoritySnapshot) -> dict[str, object]:
    material = snapshot.model_dump(mode="json")
    material["goals"] = sorted(
        material["goals"], key=lambda item: item["head"]["goal_id"]
    )
    material["resources"] = sorted(
        material["resources"], key=lambda item: item["head"]["resource_kind"]
    )
    material["plans"] = sorted(
        material["plans"], key=lambda item: item["head"]["plan_id"]
    )
    material["commitments"] = sorted(
        material["commitments"], key=lambda item: item["head"]["commitment_id"]
    )
    material["attention_expiry_due"] = sorted(
        material["attention_expiry_due"], key=lambda item: item["trigger_ref"]
    )
    # This is an event log, not a set: canonical world revision order is
    # authority and must be retained independently of caller tuple order.
    material["committed_events"] = sorted(
        material["committed_events"],
        key=lambda item: (item["world_revision"], item["event_id"]),
    )
    return material


def _due_relation(window: object | None, logical_time: datetime | None) -> DueRelation:
    if window is None:
        return "none"
    if logical_time is None:
        return "future"
    starts_at = getattr(window, "starts_at", None) or getattr(window, "opens_at")
    ends_at = getattr(window, "ends_at", None) or getattr(window, "closes_at")
    if logical_time < starts_at:
        return "future"
    if logical_time <= ends_at:
        return "open"
    return "overdue"


def _time_segment(logical_time: datetime | None) -> TimeSegment | None:
    if logical_time is None:
        return None
    hour = logical_time.hour
    for name, (start, end) in _TIME_SEGMENT_CATALOG.items():
        if start <= hour < end:
            return name
    raise ValueError("logical time is outside the installed time segment catalog")


def _visible(privacy: PrivacyClass | None, scope: ViewerScope) -> bool:
    return privacy is None or privacy in scope.allowed_privacy_classes


def _redact_location(value: LocationSlice, scope: ViewerScope) -> LocationSlice:
    if value.availability != "available" or _visible(value.privacy_class, scope):
        return value
    return LocationSlice(availability="redacted", reason="privacy_ceiling")


def _redact_goal(value: GoalSlice, scope: ViewerScope) -> GoalSlice:
    if _visible(value.privacy_class, scope):
        return value
    return GoalSlice(availability="redacted", reason="privacy_ceiling")


def _redact_resource(value: ResourceSlice, scope: ViewerScope) -> ResourceSlice:
    if value.availability != "available" or _visible(value.privacy_class, scope):
        return value
    return ResourceSlice(
        resource_kind=value.resource_kind,
        availability="redacted",
        reason="privacy_ceiling",
    )


def _redact_attention(value: AttentionSlice, scope: ViewerScope) -> AttentionSlice:
    if value.availability != "available" or _visible(value.privacy_class, scope):
        return value
    return AttentionSlice(availability="redacted", reason="privacy_ceiling")


def _redact_activity(value: ActivitySlice, scope: ViewerScope) -> ActivitySlice:
    if _visible(value.privacy_class, scope):
        return value
    return ActivitySlice(availability="redacted", reason="privacy_ceiling")


def _redact_commitment(value: CommitmentSlice, scope: ViewerScope) -> CommitmentSlice:
    if _visible(value.privacy_class, scope):
        return value
    return CommitmentSlice(availability="redacted", reason="privacy_ceiling")


def _redact_social(
    value: SocialEnvironmentSlice, scope: ViewerScope
) -> SocialEnvironmentSlice:
    if value.availability != "available" or _visible(value.privacy_class, scope):
        return value
    return SocialEnvironmentSlice(availability="redacted", reason="privacy_ceiling")


def _redact_plan_relation(value: PlanRelationSlice, scope: ViewerScope) -> PlanRelationSlice:
    if value.availability != "available" or _visible(value.privacy_class, scope):
        return value
    return PlanRelationSlice(availability="redacted", reason="privacy_ceiling")
