"""Pure shadow-domain reducers for capability, consent, and privacy grants."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from .actor_authority_events import verify_deployment_root_attestation
from .authorization_events import (
    AuthorizationPayload,
    authorization_domain,
    authorization_mutation_hash,
    principal_action_evidence_hash,
    principal_action_challenge_identity,
    principal_action_source_identity,
    validate_authorization_event_operation,
)
from .schemas import (
    ActorAuthorityProjection,
    AuthorizationOrigin,
    CapabilityStateProjection,
    CapabilityTransitionProjection,
    ConsentStateProjection,
    ConsentTransitionProjection,
    PrivacyPolicyProjection,
    PrivacyTransitionProjection,
    WorldEvent,
)


AuthorizationProjection = (
    CapabilityStateProjection | ConsentStateProjection | PrivacyPolicyProjection
)
AuthorizationTransition = (
    CapabilityTransitionProjection
    | ConsentTransitionProjection
    | PrivacyTransitionProjection
)

_REQUIRED_AUTHORITY_OPERATION = {
    "capability": "capability_grant",
    "consent": "consent_grant",
    "privacy": "privacy_policy",
}


def reduce_authorization(
    projections: tuple[AuthorizationProjection, ...],
    history: tuple[AuthorizationTransition, ...],
    consumed_root_nonces: tuple[str, ...],
    consumed_challenges: tuple[str, ...],
    consumed_sources: tuple[str, ...],
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    payload: AuthorizationPayload,
    *,
    event: WorldEvent,
    logical_time: datetime,
) -> tuple[
    tuple[AuthorizationProjection, ...],
    tuple[AuthorizationTransition, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    domain = authorization_domain(event.event_type)
    validate_authorization_event_operation(event.event_type, payload.operation)
    if payload.world_id != event.world_id:
        raise ValueError("authorization attestation belongs to another world")
    if payload.changed_at != logical_time or event.logical_time != logical_time:
        raise ValueError("authorization transition must use authoritative logical time")
    mutation_hash = authorization_mutation_hash(event.event_type, payload)
    proof_hash, nonce_hash = verify_deployment_root_attestation(
        proof=payload.root_proof,
        event=event,
        mutation_hash=mutation_hash,
    )
    nonce_key = (
        f"{event.world_id}|{payload.root_proof.root_key_id}|{payload.root_proof.nonce}"
    )
    if nonce_key in consumed_root_nonces:
        raise ValueError("authorization root proof nonce is already consumed")
    if any(item.transition_id == payload.transition_id for item in history):
        raise ValueError("authorization transition already exists")
    evidence_hash = principal_action_evidence_hash(payload.principal_action_evidence)
    challenge_identity = principal_action_challenge_identity(
        event.world_id, payload.principal_action_evidence
    )
    source_identity = principal_action_source_identity(
        event.world_id, payload.principal_action_evidence
    )
    if challenge_identity in consumed_challenges:
        raise ValueError("authorization principal action challenge is already consumed")
    if source_identity in consumed_sources:
        raise ValueError("authorization principal action source is already consumed")

    authority = _resolve_authority(actor_authorities, payload, domain, logical_time)
    _validate_domain_principal(domain, payload, authority)
    origin = AuthorizationOrigin(
        transition_id=payload.transition_id,
        event_ref=event.event_id,
        authority_id=authority.authority_id,
        authority_revision=authority.entity_revision,
        attested_principal_ref=payload.attested_principal_ref,
        attestation_mode=payload.attestation_mode,
        attestation_environment=payload.attestation_environment,
        evidence_hash=evidence_hash,
        root_key_id=payload.root_proof.root_key_id,
        root_keyset_digest=payload.root_proof.keyset_digest,
        root_nonce_hash=nonce_hash,
        root_proof_hash=proof_hash,
    )

    matches = [
        (index, item)
        for index, item in enumerate(projections)
        if _entity_id(item) == payload.entity_id
    ]
    if len(matches) > 1:
        raise ValueError("duplicate authorization projection")
    current = matches[0][1] if matches else None
    index = matches[0][0] if matches else None
    create_operation = "grant" if domain != "privacy" else "revise"
    if payload.expected_entity_revision == 0:
        if current is not None or payload.operation != create_operation:
            raise ValueError("authorization entity is already created")
        _require_active_values(payload.values_after, logical_time)
        revision = 1
    else:
        if current is None or current.entity_revision != payload.expected_entity_revision:
            raise ValueError("stale authorization transition")
        if current.values != payload.values_before:
            raise ValueError("authorization before values are stale")
        if _status(current.values, domain) != "active":
            raise ValueError("inactive authorization cannot transition")
        revision = current.entity_revision + 1
        _validate_lifecycle(domain, payload, current, history, logical_time)

    projection = _make_projection(
        domain=domain,
        entity_id=payload.entity_id,
        revision=revision,
        payload=payload,
        origin=origin,
        logical_time=logical_time,
    )
    transition = _make_transition(
        domain=domain,
        entity_id=payload.entity_id,
        revision=revision,
        payload=payload,
        origin=origin,
        logical_time=logical_time,
    )
    updated = (
        (*projections, projection)
        if index is None
        else (*projections[:index], projection, *projections[index + 1 :])
    )
    return (
        updated,
        (*history, transition),
        (*consumed_root_nonces, nonce_key),
        (*consumed_challenges, challenge_identity),
        (*consumed_sources, source_identity),
    )


def _resolve_authority(
    authorities: tuple[ActorAuthorityProjection, ...],
    payload: AuthorizationPayload,
    domain: str,
    logical_time: datetime,
) -> ActorAuthorityProjection:
    matches = [item for item in authorities if item.authority_id == payload.authority_id]
    if len(matches) != 1:
        raise ValueError("authorization actor authority is missing or ambiguous")
    authority = matches[0]
    if authority.entity_revision != payload.expected_authority_revision:
        raise ValueError("authorization actor authority revision is stale")
    values = authority.values
    if values.status != "active":
        raise ValueError("authorization actor authority is inactive")
    if values.valid_from > logical_time or (
        values.expires_at is not None and logical_time >= values.expires_at
    ):
        raise ValueError("authorization actor authority is outside its validity window")
    required = _REQUIRED_AUTHORITY_OPERATION[domain]
    if required not in values.allowed_operations:
        raise ValueError("actor authority does not allow authorization operation")
    if payload.attested_principal_ref != values.principal_ref:
        raise ValueError("attested principal does not match actor authority")
    return authority


def _validate_domain_principal(
    domain: str,
    payload: AuthorizationPayload,
    authority: ActorAuthorityProjection,
) -> None:
    values = payload.values_after
    if domain == "capability" and authority.values.principal_kind not in {
        "deployment_operator",
        "service_operator",
    }:
        raise ValueError("capability grant requires an operator principal")
    if domain == "consent":
        if authority.values.principal_kind != "user_consent_principal":
            raise ValueError("consent requires a user consent principal")
        if values.grantor_ref != authority.values.principal_ref:
            raise ValueError("consent grantor does not match attested principal")
    if domain == "privacy":
        if authority.values.principal_kind != "user_consent_principal":
            raise ValueError("privacy policy requires a user consent principal")
        if values.subject_ref != authority.values.principal_ref:
            raise ValueError("privacy subject does not match attested principal")


def _status(values: object, domain: str) -> str:
    return values.state if domain == "capability" else values.status


def _require_active_values(values: object, logical_time: datetime) -> None:
    if _status(values, "capability" if hasattr(values, "state") else "other") != "active":
        raise ValueError("authorization creation or revision must be active")
    valid_from = values.effective_at if hasattr(values, "effective_at") else values.valid_from
    if valid_from > logical_time:
        raise ValueError("authorization is not valid yet")
    if values.expires_at is not None and logical_time >= values.expires_at:
        raise ValueError("authorization is expired")


def _validate_lifecycle(
    domain: str,
    payload: AuthorizationPayload,
    current: AuthorizationProjection,
    history: tuple[AuthorizationTransition, ...],
    logical_time: datetime,
) -> None:
    if payload.operation in {"grant", "revise"}:
        _require_active_values(payload.values_after, logical_time)
        if payload.values_after == current.values:
            raise ValueError("authorization revision is a semantic no-op")
        return
    if payload.operation == "revoke":
        if domain == "consent" and not current.values.revocable:
            raise ValueError("non-revocable consent cannot be revoked")
        expected = current.values.model_copy(
            update={"state" if domain == "capability" else "status": "revoked"}
        )
        if payload.values_after != expected:
            raise ValueError("authorization revoke may only change status")
        return
    target = next(
        (
            item
            for item in history
            if item.transition_id == payload.compensates_transition_id
        ),
        None,
    )
    lineage = tuple(item for item in history if _transition_entity_id(item) == payload.entity_id)
    if target is None or _transition_entity_id(target) != payload.entity_id:
        raise ValueError("authorization compensation target belongs to another entity")
    if not lineage or lineage[-1] != target:
        raise ValueError("authorization compensation target must be entity latest")
    if target.operation not in {"revise"} or target.values_before is None:
        raise ValueError("authorization compensation target is not safely reversible")
    if current.values != target.values_after or payload.values_after != target.values_before:
        raise ValueError("authorization compensation must restore exact prior values")
    if not _is_narrower_or_equal(domain, payload.values_after, current.values):
        raise ValueError("authorization compensation cannot expand scope")
    _require_active_values(payload.values_after, logical_time)
    if any(item.compensates_transition_id == target.transition_id for item in history):
        raise ValueError("authorization transition is already compensated")


def _is_narrower_or_equal(domain: str, restored: object, current: object) -> bool:
    if domain == "capability":
        return (
            restored.capability_kind == current.capability_kind
            and restored.actor_ref == current.actor_ref
            and set(restored.target_scope_refs) <= set(current.target_scope_refs)
            and set(restored.constraint_refs) >= set(current.constraint_refs)
            and _window_no_broader(restored, current, "valid_from")
        )
    if domain == "consent":
        return (
            restored.grantor_ref == current.grantor_ref
            and restored.grantee_ref == current.grantee_ref
            and set(restored.action_scope_refs) <= set(current.action_scope_refs)
            and set(restored.data_scope_refs) <= set(current.data_scope_refs)
            and set(restored.channel_scope_refs) <= set(current.channel_scope_refs)
            and _window_no_broader(restored, current, "valid_from")
        )
    return (
        restored.subject_ref == current.subject_ref
        and set(restored.data_class_refs) <= set(current.data_class_refs)
        and set(restored.viewer_rule_refs) <= set(current.viewer_rule_refs)
        and set(restored.media_rule_refs) <= set(current.media_rule_refs)
        and set(restored.retention_rule_refs) <= set(current.retention_rule_refs)
        and _window_no_broader(restored, current, "effective_at")
    )


def _window_no_broader(restored: object, current: object, start_field: str) -> bool:
    if getattr(restored, start_field) < getattr(current, start_field):
        return False
    if current.expires_at is None:
        return True
    return restored.expires_at is not None and restored.expires_at <= current.expires_at


def _entity_id(item: AuthorizationProjection) -> str:
    if isinstance(item, CapabilityStateProjection):
        return item.grant_id
    if isinstance(item, ConsentStateProjection):
        return item.consent_id
    return item.policy_id


def _transition_entity_id(item: AuthorizationTransition) -> str:
    if isinstance(item, CapabilityTransitionProjection):
        return item.grant_id
    if isinstance(item, ConsentTransitionProjection):
        return item.consent_id
    return item.policy_id


def _make_projection(
    *,
    domain: Literal["capability", "consent", "privacy"],
    entity_id: str,
    revision: int,
    payload: AuthorizationPayload,
    origin: AuthorizationOrigin,
    logical_time: datetime,
) -> AuthorizationProjection:
    common: dict[str, Any] = {
        "entity_revision": revision,
        "values": payload.values_after,
        "policy_version": payload.policy_version,
        "policy_digest": payload.policy_digest,
        "origin": origin,
        "updated_at": logical_time,
    }
    if domain == "capability":
        return CapabilityStateProjection(grant_id=entity_id, **common)
    if domain == "consent":
        return ConsentStateProjection(consent_id=entity_id, **common)
    return PrivacyPolicyProjection(policy_id=entity_id, **common)


def _make_transition(
    *,
    domain: Literal["capability", "consent", "privacy"],
    entity_id: str,
    revision: int,
    payload: AuthorizationPayload,
    origin: AuthorizationOrigin,
    logical_time: datetime,
) -> AuthorizationTransition:
    common: dict[str, Any] = {
        "transition_id": payload.transition_id,
        "entity_revision": revision,
        "operation": payload.operation,
        "values_before": payload.values_before,
        "values_after": payload.values_after,
        "origin": origin,
        "changed_at": logical_time,
        "compensates_transition_id": payload.compensates_transition_id,
    }
    if domain == "capability":
        return CapabilityTransitionProjection(grant_id=entity_id, **common)
    if domain == "consent":
        return ConsentTransitionProjection(consent_id=entity_id, **common)
    return PrivacyTransitionProjection(policy_id=entity_id, **common)
