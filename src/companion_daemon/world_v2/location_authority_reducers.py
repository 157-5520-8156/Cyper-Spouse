"""Pure `.16.0` LocationAuthority reducer.

DORMANT — no producer: no production ledger holds a committed ``V2Location*``
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
from .goal_situation_schemas import DomainOperatorAuthorityBinding
from .location_authority_events import (
    V2LocationChangedPayload,
    v2_location_evidence_refs,
    v2_location_mutation_hash,
)
from .location_authority_contract import require_location_event_operation
from .location_authority_schemas import (
    LocationCompensationCauseAuthority,
    V2LocationProjection,
    V2LocationTransitionProjection,
    v2_location_semantic_fingerprint,
)
from .schemas import (
    ActorAuthorityProjection,
    CommittedWorldEventRef,
)


V2_LOCATION_POLICY_REFS = ("policy:v2-location-authority.1",)
V2_LOCATION_POLICY_VERSION = "v2-location-authority-policy.1"
V2_LOCATION_OPERATOR_OPERATION = "v2_location_governance"
_POLICY_ARTIFACT = {
    "version": V2_LOCATION_POLICY_VERSION,
    "installed_lanes": ["operator", "compensation"],
    "movement_capabilities": [],
    "selection_modes": ["direct"],
    "scene_visibility_is_disclosure_authority": False,
    "privacy": "lifetime-max",
    "compensation": "exact-latest-operator-lineage",
    "zero_cascade": True,
}
V2_LOCATION_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_POLICY_ARTIFACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()

_PRIVACY_RANK = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def reduce_v2_location(
    locations: tuple[V2LocationProjection, ...],
    history: tuple[V2LocationTransitionProjection, ...],
    payload: V2LocationChangedPayload,
    *,
    event_type: str,
    event_id: str,
    logical_time: datetime,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
) -> tuple[tuple[V2LocationProjection, ...], tuple[V2LocationTransitionProjection, ...]]:
    require_location_event_operation(event_type=event_type, operation=payload.operation)
    if payload.accepted_change_hash != v2_location_mutation_hash(payload) or (
        payload.evidence_refs != v2_location_evidence_refs(payload)
    ):
        raise ValueError("location accepted change hash or evidence binding is invalid")
    if payload.policy_refs != V2_LOCATION_POLICY_REFS or (
        payload.policy_version != V2_LOCATION_POLICY_VERSION
        or payload.policy_digest != V2_LOCATION_POLICY_DIGEST
    ):
        raise ValueError("location mutation references an uninstalled policy")
    if payload.selection_mode == "random_draw":
        raise ValueError("random_authority_not_installed")
    if payload.authority_lane in {"settlement", "deliberative"}:
        raise ValueError("location_movement_authority_not_installed")

    after = payload.location_after
    if (
        after.origin.accepted_event_ref != event_id
        or after.origin.change_id != payload.change_id
        or after.origin.transition_id != payload.transition_id
        or after.origin.policy_refs != V2_LOCATION_POLICY_REFS
        or after.updated_at != logical_time
        or after.semantic_fingerprint
        != v2_location_semantic_fingerprint(
            actor_ref=after.actor_ref,
            values=after.values,
            policy_refs=after.origin.policy_refs,
        )
    ):
        raise ValueError("location after image is not exact or event-pinned")
    if (
        any(item.transition_id == payload.transition_id for item in history)
        or any(item.change_id == payload.change_id for item in history)
        or any(item.accepted_event_ref == event_id for item in history)
    ):
        raise ValueError("location transition identity already exists")

    matches = [item for item in locations if item.actor_ref == after.actor_ref]
    if len(matches) > 1:
        raise ValueError("actor has duplicate current location heads")
    current = matches[0] if matches else None

    if payload.operation == "establish":
        _resolve_operator(
            payload.cause_authority,
            actor_authorities=actor_authorities,
            committed_events=committed_events,
            logical_time=logical_time,
            evaluated_world_revision=payload.evaluated_world_revision,
        )
        if current is not None:
            raise ValueError("actor location is already established")
        if (
            payload.location_before is not None
            or payload.expected_entity_revision != 0
            or after.entity_revision != 1
            or after.values.since != logical_time
        ):
            raise ValueError("location establish must create one event-pinned revision")
    elif payload.operation == "change":
        _resolve_operator(
            payload.cause_authority,
            actor_authorities=actor_authorities,
            committed_events=committed_events,
            logical_time=logical_time,
            evaluated_world_revision=payload.evaluated_world_revision,
        )
        _validate_current_and_revision(current, payload)
        assert current is not None
        if after.values == current.values:
            raise ValueError("location change is an exact semantic no-op")
        _validate_chronology_and_privacy(current, after, logical_time)
    else:
        cause = payload.cause_authority
        if not isinstance(cause, LocationCompensationCauseAuthority):
            raise ValueError("location compensation lacks typed target authority")
        _resolve_operator(
            cause.operator_authority,
            actor_authorities=actor_authorities,
            committed_events=committed_events,
            logical_time=logical_time,
            evaluated_world_revision=payload.evaluated_world_revision,
        )
        _validate_current_and_revision(current, payload)
        assert current is not None
        _validate_compensation(
            current,
            after,
            payload,
            cause,
            history=history,
            committed_events=committed_events,
            logical_time=logical_time,
        )

    replacement = after
    new_heads = tuple(
        sorted(
            (*(item for item in locations if item.actor_ref != after.actor_ref), replacement),
            key=lambda item: item.actor_ref,
        )
    )
    transition = V2LocationTransitionProjection(
        transition_id=payload.transition_id,
        actor_ref=after.actor_ref,
        entity_revision=after.entity_revision,
        operation=payload.operation,
        authority_lane=payload.authority_lane,
        values_before=payload.location_before.values if payload.location_before else None,
        values_after=after.values,
        semantic_fingerprint_after=after.semantic_fingerprint,
        change_id=payload.change_id,
        policy_refs=payload.policy_refs,
        accepted_event_ref=event_id,
        accepted_at=logical_time,
        cause_authority=payload.cause_authority,
        compensates_transition_id=(
            payload.cause_authority.target_transition_id
            if isinstance(payload.cause_authority, LocationCompensationCauseAuthority)
            else None
        ),
    )
    return new_heads, (*history, transition)


def _validate_current_and_revision(
    current: V2LocationProjection | None, payload: V2LocationChangedPayload
) -> None:
    after = payload.location_after
    if (
        current is None
        or payload.location_before != current
        or payload.expected_entity_revision != current.entity_revision
        or after.entity_revision != current.entity_revision + 1
        or after.actor_ref != current.actor_ref
        or after.updated_at < current.updated_at
    ):
        raise ValueError("location before image or entity revision is stale")


def _validate_chronology_and_privacy(
    current: V2LocationProjection,
    after: V2LocationProjection,
    logical_time: datetime,
) -> None:
    identity_changed = (
        current.values.location_ref != after.values.location_ref
        or current.values.zone_ref != after.values.zone_ref
    )
    expected_since = logical_time if identity_changed else current.values.since
    if after.values.since != expected_since or after.values.since > after.updated_at:
        raise ValueError("location since does not follow identity-change chronology")
    if _PRIVACY_RANK[after.values.privacy_class] < _PRIVACY_RANK[current.values.privacy_class]:
        raise ValueError("location privacy cannot weaken across its lifetime")


def _resolve_operator(
    cause: object,
    *,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
    evaluated_world_revision: int,
) -> None:
    if not isinstance(cause, DomainOperatorAuthorityBinding):
        raise ValueError("location mutation lacks operator authority")
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
        or cause.required_operation != V2_LOCATION_OPERATOR_OPERATION
        or cause.required_operation not in authority.values.allowed_operations
        or cause.authority_values_hash != _canonical_hash(authority.values)
        or authority.policy_version != "actor-authority-policy.2"
        or authority.policy_digest != ACTOR_AUTHORITY_V2_POLICY_DIGEST
        or cause.authority_policy_digest != authority.policy_digest
    ):
        raise ValueError("location operator cause lacks active exact ActorAuthority")


def _validate_compensation(
    current: V2LocationProjection,
    after: V2LocationProjection,
    payload: V2LocationChangedPayload,
    cause: LocationCompensationCauseAuthority,
    *,
    history: tuple[V2LocationTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
) -> None:
    same_actor = tuple(item for item in history if item.actor_ref == current.actor_ref)
    target = next(
        (item for item in same_actor if item.transition_id == cause.target_transition_id),
        None,
    )
    target_event = next(
        (item for item in committed_events if item.event_id == cause.target_accepted_event_ref),
        None,
    )
    expected_target_event_type = (
        ("V2LocationChangeCompensated" if target.operation == "compensate" else "V2LocationChanged")
        if target is not None
        else None
    )
    if (
        target is None
        or not same_actor
        or target != same_actor[-1]
        or target.entity_revision != cause.target_entity_revision
        or target.accepted_event_ref != cause.target_accepted_event_ref
        or target.values_before is None
        or target.values_after != current.values
        or target.entity_revision != current.entity_revision
        or target.semantic_fingerprint_after != current.semantic_fingerprint
        or target.change_id != current.origin.change_id
        or target.transition_id != current.origin.transition_id
        or target.policy_refs != current.origin.policy_refs
        or target.accepted_event_ref != current.origin.accepted_event_ref
        or target.accepted_at != current.updated_at
        or target_event is None
        or target_event.event_type != expected_target_event_type
        or target_event.world_revision != cause.target_accepted_world_revision
        or target_event.payload_hash != cause.target_accepted_payload_hash
        or target_event.logical_time != target.accepted_at
        or target_event.world_revision > payload.evaluated_world_revision
    ):
        raise ValueError("location compensation target is not exact latest transition")
    effective_lane = _effective_lane(target, same_actor)
    if cause.expected_target_lane is not None and cause.expected_target_lane != effective_lane:
        raise ValueError("location compensation expected lane is not authoritative")
    required_privacy = max(
        (
            target.values_before.privacy_class,
            current.values.privacy_class,
            cause.correction_basis.privacy_class,
            cause.correction_rationale.privacy_class,
        ),
        key=_PRIVACY_RANK.__getitem__,
    )
    expected_values = target.values_before.model_copy(update={"privacy_class": required_privacy})
    if (
        after.values == current.values
        or after.values != expected_values
        or after.values.since > logical_time
    ):
        raise ValueError("location compensation must restore exact prior values and privacy max")


def _effective_lane(
    target: V2LocationTransitionProjection,
    same_actor: tuple[V2LocationTransitionProjection, ...],
) -> str:
    current = target
    visited: set[str] = set()
    while current.operation == "compensate":
        if current.transition_id in visited or current.compensates_transition_id is None:
            raise ValueError("location compensation lineage is invalid or cyclic")
        visited.add(current.transition_id)
        current = next(
            (
                item
                for item in same_actor
                if item.transition_id == current.compensates_transition_id
            ),
            None,
        )
        if current is None:
            raise ValueError("location compensation lineage is incomplete")
    if current.authority_lane != "operator":
        raise ValueError("location compensation lineage is not operator-authorized")
    return current.authority_lane


def _canonical_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[attr-defined]
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
