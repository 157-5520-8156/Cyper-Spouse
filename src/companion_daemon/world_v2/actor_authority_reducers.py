"""Pure, root-verified ActorAuthority reducers."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .actor_authority_events import (
    ActorAuthorityMutationPayload,
    ROOT_KEYSET_DIGEST,
    ROOT_KEYSET_VERSION,
    actor_authority_mutation_hash,
    validate_actor_authority_event_operation,
    verify_deployment_root_attestation,
)
from .schemas import (
    ActorAuthorityOrigin,
    ActorAuthorityProjection,
    ActorAuthorityTransitionProjection,
    ActorAuthorityValues,
    WorldEvent,
)


_LEGACY_ALLOWED_OPERATIONS = (
    "actor_authority_rotation",
    "capability_grant",
    "character_core_governance",
    "consent_grant",
    "privacy_policy",
)
_V2_ALLOWED_OPERATIONS = tuple(
    sorted(
        (*_LEGACY_ALLOWED_OPERATIONS,
         "v2_attention_governance",
         "v2_goal_governance",
         "v2_location_governance",
         "v2_resource_governance")
    )
)
_POLICY = {
    "policy_version": "actor-authority-policy.1",
    "root_keyset_version": ROOT_KEYSET_VERSION,
    "root_anchor_digest": ROOT_KEYSET_DIGEST,
    "signature_algorithm": "ed25519",
    "compensation": "latest-safe-noncredential-rotation-only",
}
ACTOR_AUTHORITY_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_POLICY, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
_V2_POLICY = {
    **_POLICY,
    "policy_version": "actor-authority-policy.2",
    "allowed_operations": _V2_ALLOWED_OPERATIONS,
}
ACTOR_AUTHORITY_V2_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_V2_POLICY, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
ACTOR_AUTHORITY_POLICY_REGISTRY = {
    "actor-authority-policy.1": (
        ACTOR_AUTHORITY_POLICY_DIGEST,
        frozenset(_LEGACY_ALLOWED_OPERATIONS),
    ),
    "actor-authority-policy.2": (
        ACTOR_AUTHORITY_V2_POLICY_DIGEST,
        frozenset(_V2_ALLOWED_OPERATIONS),
    ),
}


def reduce_actor_authority(
    authorities: tuple[ActorAuthorityProjection, ...],
    history: tuple[ActorAuthorityTransitionProjection, ...],
    consumed_root_nonces: tuple[str, ...],
    payload: ActorAuthorityMutationPayload,
    *,
    event: WorldEvent,
    logical_time: datetime,
    accepted_world_revision: int,
) -> tuple[
    tuple[ActorAuthorityProjection, ...],
    tuple[ActorAuthorityTransitionProjection, ...],
    tuple[str, ...],
]:
    validate_actor_authority_event_operation(event.event_type, payload.operation)
    if payload.world_id != event.world_id:
        raise ValueError("actor authority root proof belongs to another world")
    if event.logical_time != logical_time or payload.changed_at != logical_time:
        raise ValueError("actor authority transition must use authoritative logical time")
    installed = ACTOR_AUTHORITY_POLICY_REGISTRY.get(payload.policy_version)
    if installed is None or payload.policy_digest != installed[0]:
        raise ValueError("actor authority transition references an uninstalled policy")
    allowed_operations = installed[1]
    if not set(payload.values_after.allowed_operations).issubset(allowed_operations) or (
        payload.values_before is not None
        and not set(payload.values_before.allowed_operations).issubset(allowed_operations)
    ):
        raise ValueError("actor authority operations are not installed for policy version")
    _verify_root_proof(payload, event)
    nonce_key = f"{event.world_id}|{payload.root_proof.root_key_id}|{payload.root_proof.nonce}"
    if nonce_key in consumed_root_nonces:
        raise ValueError("deployment root proof nonce is already consumed")
    if any(item.transition_id == payload.transition_id for item in history):
        raise ValueError("actor authority transition already exists")

    matches = [
        (index, item)
        for index, item in enumerate(authorities)
        if item.authority_id == payload.authority_id
    ]
    if len(matches) > 1:
        raise ValueError("duplicate actor authority state")
    current = matches[0][1] if matches else None
    index = matches[0][0] if matches else None

    if payload.operation == "bootstrap":
        if current is not None:
            raise ValueError("actor authority is already bootstrapped")
        if any(
            item.values.principal_ref == payload.values_after.principal_ref
            and item.values.status == "active"
            for item in authorities
        ):
            raise ValueError("active actor authority principal already exists")
        if payload.values_after.status != "active":
            raise ValueError("actor authority bootstrap must be active")
        _require_active_window(payload.values_after, logical_time)
        _require_unused_credential(
            payload.values_after.credential_ref,
            authority_id=payload.authority_id,
            authorities=authorities,
            history=history,
        )
        revision = 1
    else:
        if current is None or current.entity_revision != payload.expected_entity_revision:
            raise ValueError("stale actor authority transition")
        if current.values != payload.values_before:
            raise ValueError("actor authority before values are stale")
        if current.values.status != "active":
            raise ValueError("revoked actor authority cannot transition")
        revision = current.entity_revision + 1
        if payload.operation == "rotate":
            if payload.values_after.status != "active":
                raise ValueError("actor authority rotation must remain active")
            _require_active_window(payload.values_after, logical_time)
            if payload.values_after.principal_ref != current.values.principal_ref:
                raise ValueError("actor authority rotation changed principal")
            if payload.values_after == current.values:
                raise ValueError("actor authority rotation is a semantic no-op")
            if payload.values_after.credential_ref != current.values.credential_ref:
                _require_unused_credential(
                    payload.values_after.credential_ref,
                    authority_id=payload.authority_id,
                    authorities=authorities,
                    history=history,
                )
        elif payload.operation == "revoke":
            if payload.values_after != current.values.model_copy(update={"status": "revoked"}):
                raise ValueError("actor authority revoke may only change status")
        else:
            target = next(
                (item for item in history if item.transition_id == payload.compensates_transition_id),
                None,
            )
            same_authority_history = tuple(
                item for item in history if item.authority_id == payload.authority_id
            )
            if target is None or target.authority_id != payload.authority_id:
                raise ValueError("actor authority compensation target belongs to another authority")
            if not same_authority_history or same_authority_history[-1] != target:
                raise ValueError("actor authority compensation target must be authority latest")
            if target.authority_revision != current.entity_revision:
                raise ValueError("actor authority compensation target revision is stale")
            if current.values != target.values_after:
                raise ValueError("actor authority compensation current values do not match target")
            if target.operation != "rotate" or target.values_before is None:
                raise ValueError("actor authority compensation cannot reactivate revoked credentials")
            if target.values_before.credential_ref != target.values_after.credential_ref:
                raise ValueError("actor authority compensation cannot restore an old credential")
            if payload.values_after != target.values_before:
                raise ValueError("actor authority compensation must restore exact prior values")
            _require_active_window(payload.values_after, logical_time)
            if any(item.compensates_transition_id == target.transition_id for item in history):
                raise ValueError("actor authority transition is already compensated")

    proof_hash = hashlib.sha256(bytes.fromhex(payload.root_proof.signature_hex)).hexdigest()
    nonce_hash = hashlib.sha256(payload.root_proof.nonce.encode("utf-8")).hexdigest()
    projection = ActorAuthorityProjection(
        authority_id=payload.authority_id,
        entity_revision=revision,
        values=payload.values_after,
        policy_version=payload.policy_version,
        policy_digest=payload.policy_digest,
        origin=ActorAuthorityOrigin(
            transition_id=payload.transition_id,
            event_ref=event.event_id,
            root_key_id=payload.root_proof.root_key_id,
            root_keyset_version=payload.root_proof.keyset_version,
            root_keyset_digest=payload.root_proof.keyset_digest,
            root_nonce_hash=nonce_hash,
            root_proof_hash=proof_hash,
        ),
        updated_at=logical_time,
    )
    transition = ActorAuthorityTransitionProjection(
        transition_id=payload.transition_id,
        authority_id=payload.authority_id,
        authority_revision=revision,
        operation=payload.operation,
        values_before=payload.values_before,
        values_after=payload.values_after,
        policy_version=payload.policy_version,
        policy_digest=payload.policy_digest,
        root_key_id=payload.root_proof.root_key_id,
        root_keyset_version=payload.root_proof.keyset_version,
        root_keyset_digest=payload.root_proof.keyset_digest,
        root_nonce_hash=nonce_hash,
        root_proof_hash=proof_hash,
        accepted_event_ref=event.event_id,
        accepted_world_revision=accepted_world_revision,
        accepted_payload_hash=event.payload_hash,
        changed_at=payload.changed_at,
        compensates_transition_id=payload.compensates_transition_id,
    )
    updated = (
        (*authorities, projection)
        if index is None
        else (*authorities[:index], projection, *authorities[index + 1 :])
    )
    return updated, (*history, transition), (*consumed_root_nonces, nonce_key)


def _require_unused_credential(
    credential_ref: str,
    *,
    authority_id: str,
    authorities: tuple[ActorAuthorityProjection, ...],
    history: tuple[ActorAuthorityTransitionProjection, ...],
) -> None:
    """Prevent a credential lineage from ever being rebound or resurrected."""

    if any(
        item.values.credential_ref == credential_ref
        and item.authority_id != authority_id
        for item in authorities
    ):
        raise ValueError("actor authority credential lineage is already assigned")
    if any(
        item.authority_id != authority_id
        and (
            item.values_after.credential_ref == credential_ref
            or (
                item.values_before is not None
                and item.values_before.credential_ref == credential_ref
            )
        )
        for item in history
    ):
        raise ValueError("actor authority credential lineage is already assigned")
    if any(
        item.authority_id == authority_id
        and item.values_after.credential_ref == credential_ref
        for item in history
    ):
        raise ValueError("actor authority credential lineage cannot be reused")


def _require_active_window(
    values: ActorAuthorityValues, logical_time: datetime
) -> None:
    valid_from = values.valid_from
    expires_at = values.expires_at
    if valid_from > logical_time:
        raise ValueError("active actor authority is not valid yet")
    if expires_at is not None and logical_time >= expires_at:
        raise ValueError("active actor authority is expired")


def _verify_root_proof(payload: ActorAuthorityMutationPayload, event: WorldEvent) -> None:
    mutation_hash = actor_authority_mutation_hash(payload)
    verify_deployment_root_attestation(
        proof=payload.root_proof, event=event, mutation_hash=mutation_hash
    )
