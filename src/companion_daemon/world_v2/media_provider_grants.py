"""Ledger-backed enforcement for media-provider Actions.

The old capability/consent/privacy projections remain useful for shadow
evaluation, but they are intentionally not executable.  A real media provider
call requires an explicit ``ProviderMediaGrant`` whose source revisions carry
the enforcement origin.  This module is the single verifier used by both the
``ActionAuthorized`` reducer and the ActionPump immediately before dispatch.
"""

from __future__ import annotations

from datetime import datetime

from .schema_core import FrozenModel
from .schemas import Action, LedgerProjection, ProviderMediaGrant, ProviderMediaGrantBinding


PROVIDER_MEDIA_ACTION_KINDS = frozenset(
    {"media_planning", "media_render", "media_inspection"}
)


class ProviderMediaGrantRecordedPayload(FrozenModel):
    grant: ProviderMediaGrant


def is_provider_media_action(action: Action) -> bool:
    return action.kind in PROVIDER_MEDIA_ACTION_KINDS


def require_provider_media_grant(
    *, action: Action, projection: LedgerProjection, logical_time: datetime
) -> ProviderMediaGrant:
    """Return the exact active grant or fail closed before a provider call."""

    if not is_provider_media_action(action):
        raise ValueError("provider media grant verifier received a non-provider-media Action")
    if action.layer != "media_action" or action.provider_media_grant is None:
        raise ValueError("provider media Action lacks an enforcement grant binding")
    binding = action.provider_media_grant
    matches = tuple(item for item in projection.provider_media_grants if item.grant_id == binding.grant_id)
    if len(matches) != 1 or matches[0].entity_revision != binding.grant_revision:
        raise ValueError("provider media grant binding is missing or stale")
    grant = matches[0]
    if grant.capability_kind != action.kind or grant.provider_ref != action.target:
        raise ValueError("provider media grant does not bind this Action kind and provider")
    if grant.actor_ref != action.actor:
        raise ValueError("provider media grant actor does not bind this Action")
    if logical_time < grant.issued_at or (grant.expires_at is not None and logical_time >= grant.expires_at):
        raise ValueError("provider media grant is outside its validity window")

    capability = _exact(
        projection.capability_grants, "grant_id", grant.capability_grant_id,
        "provider media capability grant",
    )
    if capability.entity_revision != grant.capability_grant_revision:
        raise ValueError("provider media capability grant revision is stale")
    if (
        not capability.origin.enforcement_eligible
        or capability.origin.attestation_environment != "enforcement"
        or capability.values.state != "active"
        or capability.values.capability_kind != action.kind
        or capability.values.actor_ref != action.actor
        or "provider:media" not in capability.values.target_scope_refs
        or not _active(capability.values.valid_from, capability.values.expires_at, logical_time)
    ):
        raise ValueError("provider media capability is not enforcement eligible and active")

    consent = _exact(projection.consent_grants, "consent_id", grant.consent_id, "provider media consent")
    if consent.entity_revision != grant.consent_revision:
        raise ValueError("provider media consent revision is stale")
    if (
        not consent.origin.enforcement_eligible
        or consent.origin.attestation_environment != "enforcement"
        or consent.values.status != "active"
        or consent.values.grantor_ref != grant.subject_ref
        or consent.values.grantee_ref != action.actor
        or action.kind not in consent.values.action_scope_refs
        or "data:attachment" not in consent.values.data_scope_refs
        or not _active(consent.values.valid_from, consent.values.expires_at, logical_time)
    ):
        raise ValueError("provider media consent is not enforcement eligible and active")

    privacy = _exact(
        projection.privacy_policies, "policy_id", grant.privacy_policy_id, "provider media privacy policy"
    )
    if privacy.entity_revision != grant.privacy_policy_revision:
        raise ValueError("provider media privacy policy revision is stale")
    if (
        not privacy.origin.enforcement_eligible
        or privacy.origin.attestation_environment != "enforcement"
        or privacy.values.status != "active"
        or privacy.values.subject_ref != grant.subject_ref
        or "data:attachment" not in privacy.values.data_class_refs
        or "viewer:media_provider" not in privacy.values.viewer_rule_refs
        or "media:private_only" not in privacy.values.media_rule_refs
        or not _active(privacy.values.effective_at, privacy.values.expires_at, logical_time)
    ):
        raise ValueError("provider media privacy policy is not enforcement eligible and active")
    return grant


def validate_provider_media_grant_record(
    *, grant: ProviderMediaGrant, projection: LedgerProjection, logical_time: datetime
) -> None:
    """Reducer-time validation before recording the immutable grant."""

    if grant.issued_at != logical_time:
        raise ValueError("provider media grant must be issued at authoritative logical time")
    synthetic_action = Action.model_construct(
        schema_version="world-v2.1",
        action_id="validation:provider-media-grant",
        world_id="grant-validation",
        logical_time=logical_time,
        created_at=logical_time,
        trace_id="validation",
        causation_id="validation",
        correlation_id="validation",
        kind=grant.capability_kind,
        layer="media_action",
        intent_ref="validation",
        actor=grant.actor_ref,
        target=grant.provider_ref,
        payload_ref="validation",
        payload_hash="validation",
        provider_media_grant=ProviderMediaGrantBinding(grant_id=grant.grant_id, grant_revision=1),
        idempotency_key="validation",
        budget_reservation_id="validation",
        state="authorized",
        recovery_policy="none",
    )
    # Reuse the dispatch verifier, replacing only the just-created immutable
    # collection.  The Action is never persisted; this closes the same set of
    # source bindings at grant and at dispatch time.
    require_provider_media_grant(
        action=synthetic_action,
        projection=projection.model_copy(update={"provider_media_grants": (*projection.provider_media_grants, grant)}),
        logical_time=logical_time,
    )


def _active(start: datetime, end: datetime | None, logical_time: datetime) -> bool:
    return start <= logical_time and (end is None or logical_time < end)


def _exact(items: tuple[object, ...], attribute: str, value: str, label: str):
    matches = tuple(item for item in items if getattr(item, attribute) == value)
    if len(matches) != 1:
        raise ValueError(f"{label} is missing or ambiguous")
    return matches[0]


__all__ = [
    "PROVIDER_MEDIA_ACTION_KINDS",
    "ProviderMediaGrantRecordedPayload",
    "is_provider_media_action",
    "require_provider_media_grant",
    "validate_provider_media_grant_record",
]
