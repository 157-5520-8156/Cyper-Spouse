"""Pure `.16.0` ResourceAuthority reducer with closed installed capabilities.

DORMANT — no producer: no production ledger holds a committed ``V2Resource*``
event and no runtime constructs these payloads (the tests guard replay
semantics only).  Before wiring a producer, read the Producer-First Authority
rule in CONTEXT.md and record the activation verdict in
``configs/mechanism_closure.yaml`` (``v16-situation-constituents``).
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .actor_authority_reducers import ACTOR_AUTHORITY_V2_POLICY_DIGEST
from .goal_situation_schemas import (
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    InternalIntentionBasis,
)
from .resource_authority_contract import require_resource_event_operation
from .resource_authority_events import (
    V2ResourceChangedPayload,
    v2_resource_evidence_refs,
    v2_resource_mutation_hash,
)
from .resource_authority_schemas import (
    ResourceCompensationCauseAuthority,
    ResourceOperatorCorrectionBasis,
    ResourceSelfAssessmentCorrectionBasis,
    V2ResourceProjection,
    V2ResourceTransitionProjection,
    validate_v2_resource_authority_state,
    v2_resource_semantic_fingerprint,
)
from .schema_core import canonicalize_json_value
from .schemas import ActorAuthorityProjection, CommittedWorldEventRef


RESOURCE_KINDS = (
    "cognitive_capacity",
    "physical_energy",
    "social_capacity",
)
RESOURCE_BAND_POLICY_VERSION = "resource-band-policy.1"
_RESOURCE_BAND_INTERVALS = (
    ("depleted", 0, 999),
    ("low", 1000, 3499),
    ("moderate", 3500, 6499),
    ("high", 6500, 8999),
    ("full", 9000, 10000),
)
_RESOURCE_BAND_POLICY_ARTIFACT = {
    "version": RESOURCE_BAND_POLICY_VERSION,
    "resource_kinds": list(RESOURCE_KINDS),
    "intervals": [list(item) for item in _RESOURCE_BAND_INTERVALS],
    "arithmetic": "integer_basis_points",
}
RESOURCE_BAND_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        _RESOURCE_BAND_POLICY_ARTIFACT, sort_keys=True, separators=(",", ":")
    ).encode()
).hexdigest()

V2_RESOURCE_POLICY_REFS = ("policy:v2-resource-authority.1",)
V2_RESOURCE_POLICY_VERSION = "v2-resource-authority-policy.1"
V2_RESOURCE_OPERATOR_OPERATION = "v2_resource_governance"
_V2_RESOURCE_POLICY_ARTIFACT = {
    "version": V2_RESOURCE_POLICY_VERSION,
    "installed_lanes": ["operator", "deliberative", "compensation"],
    "resource_kinds": list(RESOURCE_KINDS),
    "band_policy_version": RESOURCE_BAND_POLICY_VERSION,
    "band_policy_digest": RESOURCE_BAND_POLICY_DIGEST,
    "settlement_adjustment_capabilities": [],
    "clock_recovery_capabilities": [],
    "random_selection_capabilities": [],
    "reclassification_policies": [],
    "privacy": "lifetime-max",
    "same_tick": True,
    "zero_cascade": True,
}
V2_RESOURCE_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_V2_RESOURCE_POLICY_ARTIFACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()

V2_RESOURCE_INTERNAL_BASIS_POLICY_VERSION = "v2-resource-self-regulation.1"
_V2_RESOURCE_INTERNAL_BASIS_POLICY_ARTIFACT = {
    "version": V2_RESOURCE_INTERNAL_BASIS_POLICY_VERSION,
    "intention_kind": "resource_self_regulation",
    "capabilities": ["resource_state_change", "resource_self_assessment_correction"],
    "external_fact_authority": False,
}
V2_RESOURCE_INTERNAL_BASIS_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        _V2_RESOURCE_INTERNAL_BASIS_POLICY_ARTIFACT,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()

_PRIVACY_RANK = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def derive_resource_band(value_bp: int) -> str:
    for band, lower, upper in _RESOURCE_BAND_INTERVALS:
        if lower <= value_bp <= upper:
            return band
    raise ValueError("resource value is outside integer basis-point bounds")


def reduce_v2_resource(
    resources: tuple[V2ResourceProjection, ...],
    history: tuple[V2ResourceTransitionProjection, ...],
    payload: V2ResourceChangedPayload,
    *,
    event_type: str,
    event_id: str,
    logical_time: datetime,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
) -> tuple[
    tuple[V2ResourceProjection, ...], tuple[V2ResourceTransitionProjection, ...]
]:
    validate_v2_resource_authority_state(
        resources,
        history,
        (),
        (),
        global_proposal_ids=(),
        committed_events=committed_events,
        logical_time=logical_time,
    ) if (resources or history) else None
    require_resource_event_operation(event_type=event_type, operation=payload.operation)
    if payload.accepted_change_hash != v2_resource_mutation_hash(payload) or (
        payload.evidence_refs != v2_resource_evidence_refs(payload)
    ):
        raise ValueError("resource accepted change hash or evidence binding is invalid")
    if (
        payload.policy_refs != V2_RESOURCE_POLICY_REFS
        or payload.policy_version != V2_RESOURCE_POLICY_VERSION
        or payload.policy_digest != V2_RESOURCE_POLICY_DIGEST
    ):
        raise ValueError("resource mutation references an uninstalled policy")
    if payload.selection_mode != "direct" or payload.random_draw_binding is not None:
        raise ValueError("random_authority_not_installed")
    if payload.authority_lane == "settlement":
        raise ValueError("resource_settlement_authority_not_installed")

    after = payload.resource_after
    _validate_projection(after, logical_time=logical_time, event_id=event_id)
    if any(
        item.transition_id == payload.transition_id
        or item.change_id == payload.change_id
        or item.accepted_event_ref == event_id
        for item in history
    ):
        raise ValueError("resource transition identity already exists")
    matches = [
        item
        for item in resources
        if item.actor_ref == after.actor_ref and item.resource_kind == after.resource_kind
    ]
    if len(matches) > 1:
        raise ValueError("actor has duplicate current Resource heads for one kind")
    current = matches[0] if matches else None

    if payload.operation == "initialize":
        _resolve_operator(
            payload.cause_authority,
            actor_authorities=actor_authorities,
            committed_events=committed_events,
            logical_time=logical_time,
            evaluated_world_revision=payload.evaluated_world_revision,
        )
        if current is not None:
            raise ValueError("Resource kind is already initialized for actor")
        if payload.resource_before is not None or after.entity_revision != 1:
            raise ValueError("Resource initialize must create revision one")
    elif payload.operation == "adjust":
        _validate_current_and_revision(current, payload, logical_time=logical_time)
        assert current is not None
        if payload.adjust_kind == "reclassify":
            raise ValueError("resource_reclassification_policy_not_installed")
        if payload.adjust_kind != "state_change":
            raise ValueError("unknown Resource adjustment kind")
        if payload.authority_lane == "operator":
            _resolve_operator(
                payload.cause_authority,
                actor_authorities=actor_authorities,
                committed_events=committed_events,
                logical_time=logical_time,
                evaluated_world_revision=payload.evaluated_world_revision,
            )
            privacy_floor = current.values.privacy_class
        else:
            privacy_floor = _resolve_internal_deliberation(
                payload.cause_authority,
                actor_ref=after.actor_ref,
                logical_time=logical_time,
                evaluated_world_revision=payload.evaluated_world_revision,
            )
        delta = payload.delta_bp
        if (
            delta is None
            or isinstance(delta, bool)
            or not isinstance(delta, int)
            or delta == 0
            or current.values.value_bp + delta != after.values.value_bp
        ):
            raise ValueError("Resource state_change must conserve one exact nonzero delta")
        if after.values == current.values:
            raise ValueError("Resource state_change is an exact semantic no-op")
        _require_privacy_floor(after.values.privacy_class, current.values.privacy_class, privacy_floor)
    else:
        _validate_current_and_revision(current, payload, logical_time=logical_time)
        assert current is not None
        cause = payload.cause_authority
        if not isinstance(cause, ResourceCompensationCauseAuthority):
            raise ValueError("Resource compensation lacks typed target authority")
        _validate_compensation(
            current,
            after,
            payload,
            cause,
            history=history,
            actor_authorities=actor_authorities,
            committed_events=committed_events,
            logical_time=logical_time,
        )

    new_heads = tuple(
        sorted(
            (
                *(
                    item
                    for item in resources
                    if (item.actor_ref, item.resource_kind)
                    != (after.actor_ref, after.resource_kind)
                ),
                after,
            ),
            key=lambda item: (item.actor_ref, item.resource_kind),
        )
    )
    before_values = payload.resource_before.values if payload.resource_before else None
    transition = V2ResourceTransitionProjection(
        transition_id=payload.transition_id,
        actor_ref=after.actor_ref,
        resource_kind=after.resource_kind,
        entity_revision=after.entity_revision,
        operation=payload.operation,
        adjust_kind=payload.adjust_kind,
        authority_lane=payload.authority_lane,
        value_before=before_values.value_bp if before_values else None,
        delta_bp=payload.delta_bp,
        value_after=after.values.value_bp,
        band_before=before_values.derived_band if before_values else None,
        band_after=after.values.derived_band,
        values_before=before_values,
        values_after=after.values,
        semantic_fingerprint_after=after.semantic_fingerprint,
        change_id=payload.change_id,
        policy_refs=payload.policy_refs,
        policy_version=payload.policy_version,
        policy_digest=payload.policy_digest,
        accepted_event_ref=event_id,
        accepted_at=logical_time,
        cause_authority=payload.cause_authority,
        compensates_transition_id=(
            cause.target_transition_id
            if isinstance((cause := payload.cause_authority), ResourceCompensationCauseAuthority)
            else None
        ),
    )
    return new_heads, (*history, transition)


def _validate_projection(
    projection: V2ResourceProjection, *, logical_time: datetime, event_id: str
) -> None:
    values = projection.values
    if (
        projection.updated_at != logical_time
        or projection.origin.accepted_event_ref != event_id
        or projection.origin.change_id == ""
        or projection.origin.transition_id == ""
        or projection.origin.policy_refs != V2_RESOURCE_POLICY_REFS
        or values.band_policy_version != RESOURCE_BAND_POLICY_VERSION
        or values.band_policy_digest != RESOURCE_BAND_POLICY_DIGEST
        or values.derived_band != derive_resource_band(values.value_bp)
    ):
        raise ValueError("Resource projection has invalid event pin, band, or policy")
    expected = v2_resource_semantic_fingerprint(
        actor_ref=projection.actor_ref,
        resource_kind=projection.resource_kind,
        values=values,
        policy_refs=projection.origin.policy_refs,
    )
    if projection.semantic_fingerprint != expected:
        raise ValueError("Resource projection semantic fingerprint is invalid")


def _validate_current_and_revision(
    current: V2ResourceProjection | None,
    payload: V2ResourceChangedPayload,
    *,
    logical_time: datetime,
) -> None:
    after = payload.resource_after
    if (
        current is None
        or payload.resource_before != current
        or payload.expected_entity_revision != current.entity_revision
        or after.entity_revision != current.entity_revision + 1
        or after.actor_ref != current.actor_ref
        or after.resource_kind != current.resource_kind
        or current.updated_at > logical_time
    ):
        raise ValueError("Resource before image or entity revision is stale")
    _validate_committed_head(current)


def _validate_committed_head(current: V2ResourceProjection) -> None:
    expected = v2_resource_semantic_fingerprint(
        actor_ref=current.actor_ref,
        resource_kind=current.resource_kind,
        values=current.values,
        policy_refs=current.origin.policy_refs,
    )
    if (
        current.origin.policy_refs != V2_RESOURCE_POLICY_REFS
        or current.values.band_policy_version != RESOURCE_BAND_POLICY_VERSION
        or current.values.band_policy_digest != RESOURCE_BAND_POLICY_DIGEST
        or current.values.derived_band != derive_resource_band(current.values.value_bp)
        or current.semantic_fingerprint != expected
    ):
        raise ValueError("current Resource head is not valid under its pinned policy")


def _resolve_internal_deliberation(
    cause: object,
    *,
    actor_ref: str,
    logical_time: datetime,
    evaluated_world_revision: int,
) -> str:
    if not isinstance(cause, DeliberativeCauseAuthority) or not isinstance(
        cause.basis, InternalIntentionBasis
    ):
        raise ValueError("Resource deliberation requires internal self-regulation")
    basis = cause.basis
    if (
        basis.actor_ref != actor_ref
        or basis.evaluated_world_revision != evaluated_world_revision
        or basis.logical_time != logical_time
        or basis.intention_kind != "resource_self_regulation"
        or basis.policy_version != V2_RESOURCE_INTERNAL_BASIS_POLICY_VERSION
        or basis.policy_digest != V2_RESOURCE_INTERNAL_BASIS_POLICY_DIGEST
    ):
        raise ValueError("Resource internal self-regulation basis is not exact or capable")
    return max(
        (basis.privacy_class, basis.rationale.privacy_class), key=_PRIVACY_RANK.__getitem__
    )


def _resolve_operator(
    cause: object,
    *,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
    evaluated_world_revision: int,
) -> None:
    if not isinstance(cause, DomainOperatorAuthorityBinding):
        raise ValueError("Resource mutation lacks operator authority")
    authority = next(
        (
            item
            for item in actor_authorities
            if item.authority_id == cause.authority_id
            and item.entity_revision == cause.authority_revision
        ),
        None,
    )
    committed = next(
        (item for item in committed_events if item.event_id == cause.authority_event_ref),
        None,
    )
    if (
        authority is None
        or committed is None
        or committed.event_type
        not in {
            "ActorAuthorityBootstrapped",
            "ActorAuthorityRotated",
            "ActorAuthorityCompensated",
        }
        or committed.world_revision != cause.authority_world_revision
        or committed.payload_hash != cause.authority_payload_hash
        or committed.world_revision > evaluated_world_revision
        or authority.origin.event_ref != committed.event_id
        or authority.values.principal_ref != cause.principal_ref
        or authority.values.principal_kind != "deployment_operator"
        or authority.values.status != "active"
        or authority.values.valid_from > logical_time
        or (authority.values.expires_at is not None and authority.values.expires_at <= logical_time)
        or cause.required_operation != V2_RESOURCE_OPERATOR_OPERATION
        or cause.required_operation not in authority.values.allowed_operations
        or cause.authority_values_hash != _canonical_hash(authority.values)
        or authority.policy_version != "actor-authority-policy.2"
        or authority.policy_digest != ACTOR_AUTHORITY_V2_POLICY_DIGEST
        or cause.authority_policy_digest != authority.policy_digest
    ):
        raise ValueError("Resource operator cause lacks active exact ActorAuthority")


def _validate_compensation(
    current: V2ResourceProjection,
    after: V2ResourceProjection,
    payload: V2ResourceChangedPayload,
    cause: ResourceCompensationCauseAuthority,
    *,
    history: tuple[V2ResourceTransitionProjection, ...],
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
) -> None:
    same_resource = tuple(
        item
        for item in history
        if (item.actor_ref, item.resource_kind) == (current.actor_ref, current.resource_kind)
    )
    target = next(
        (item for item in same_resource if item.transition_id == cause.target_transition_id),
        None,
    )
    target_event = next(
        (item for item in committed_events if item.event_id == cause.target_accepted_event_ref),
        None,
    )
    expected_event_type = {
        "initialize": "V2ResourceStateInitialized",
        "adjust": "V2ResourceStateAdjusted",
        "compensate": "V2ResourceTransitionCompensated",
    }.get(target.operation if target is not None else "")
    if (
        target is None
        or not same_resource
        or target != max(same_resource, key=lambda item: item.entity_revision)
        or target.values_before is None
        or target.entity_revision != cause.target_entity_revision
        or target.entity_revision != current.entity_revision
        or target.accepted_event_ref != cause.target_accepted_event_ref
        or target.values_after != current.values
        or target.semantic_fingerprint_after != current.semantic_fingerprint
        or target.change_id != current.origin.change_id
        or target.transition_id != current.origin.transition_id
        or target.policy_refs != current.origin.policy_refs
        or target.accepted_event_ref != current.origin.accepted_event_ref
        or target.accepted_at != current.updated_at
        or target_event is None
        or target_event.event_type != expected_event_type
        or target_event.world_revision != cause.target_accepted_world_revision
        or target_event.payload_hash != cause.target_accepted_payload_hash
        or target_event.logical_time != target.accepted_at
        or target_event.world_revision > payload.evaluated_world_revision
    ):
        raise ValueError("Resource compensation target is not exact latest transition")
    effective_lane = _effective_lane(target, same_resource)
    if cause.expected_target_lane is not None and cause.expected_target_lane != effective_lane:
        raise ValueError("Resource compensation expected lane is not authoritative")

    privacy_sources = [
        target.values_before.privacy_class,
        current.values.privacy_class,
        cause.correction_basis.privacy_class,
        cause.correction_rationale.privacy_class,
    ]
    if effective_lane == "operator":
        if not isinstance(cause.correction_basis, ResourceOperatorCorrectionBasis):
            raise ValueError("operator Resource transition needs operator correction basis")
        _resolve_operator(
            cause.operator_authority,
            actor_authorities=actor_authorities,
            committed_events=committed_events,
            logical_time=logical_time,
            evaluated_world_revision=payload.evaluated_world_revision,
        )
    else:
        if (
            not isinstance(cause.correction_basis, ResourceSelfAssessmentCorrectionBasis)
            or cause.operator_authority is not None
        ):
            raise ValueError("deliberative Resource transition needs self-assessment correction")
        intention = cause.correction_basis.new_intention
        if intention.logical_time != logical_time:
            raise ValueError("Resource compensation intention time is not exact")
        privacy_sources.append(
            _resolve_internal_deliberation(
                DeliberativeCauseAuthority(basis=intention),
                actor_ref=current.actor_ref,
                logical_time=logical_time,
                evaluated_world_revision=payload.evaluated_world_revision,
            )
        )
    required_privacy = max(privacy_sources, key=_PRIVACY_RANK.__getitem__)
    expected_values = target.values_before.model_copy(
        update={"privacy_class": required_privacy}
    )
    if after.values != expected_values:
        raise ValueError("Resource compensation must restore prior values with lifetime privacy")


def _effective_lane(
    target: V2ResourceTransitionProjection,
    same_resource: tuple[V2ResourceTransitionProjection, ...],
) -> str:
    current = target
    visited: set[str] = set()
    while current.operation == "compensate":
        if current.transition_id in visited or current.compensates_transition_id is None:
            raise ValueError("Resource compensation lineage is invalid or cyclic")
        visited.add(current.transition_id)
        current = next(
            (
                item
                for item in same_resource
                if item.transition_id == current.compensates_transition_id
            ),
            None,
        )
        if current is None:
            raise ValueError("Resource compensation lineage is incomplete")
    if current.authority_lane not in {"operator", "deliberative"}:
        raise ValueError("Resource compensation lineage has unsupported effective lane")
    return current.authority_lane


def _require_privacy_floor(actual: str, *sources: str) -> None:
    required = max(sources, key=_PRIVACY_RANK.__getitem__)
    if _PRIVACY_RANK[actual] < _PRIVACY_RANK[required]:
        raise ValueError("Resource privacy cannot weaken below lifetime or cause floor")


def _canonical_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[attr-defined]
    return hashlib.sha256(
        json.dumps(
            canonicalize_json_value(value), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
