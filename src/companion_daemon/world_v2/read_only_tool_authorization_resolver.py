"""Deterministic selection of a current enforcement tool authorization triple."""

from __future__ import annotations

from datetime import datetime

from .read_only_tool_authorization import TOOL_DATA_SCOPE
from .schemas import LedgerProjection, ReadOnlyToolAuthorizationBinding


class ProjectionReadOnlyToolAuthorizationResolver:
    """Resolve exactly one active capability, consent and privacy revision.

    Selection is deliberately fail-closed.  A model never supplies grant IDs,
    and a shadow authorization is not an eligible candidate.  Dispatch repeats
    this same binding against the final projection immediately before the RPC.
    """

    def resolve(
        self,
        *,
        projection: LedgerProjection,
        actor_ref: str,
        subject_ref: str,
        target: str,
        logical_time: object,
    ) -> ReadOnlyToolAuthorizationBinding:
        if not isinstance(logical_time, datetime):
            raise ValueError("tool authorization needs a logical time")
        data_scope = TOOL_DATA_SCOPE.get(target)
        if data_scope is None:
            raise ValueError("tool target is unsupported")
        capabilities = tuple(
            item
            for item in projection.capability_grants
            if item.origin.enforcement_eligible
            and item.origin.attestation_environment == "enforcement"
            and item.values.state == "active"
            and item.values.capability_kind == "read_only_tool"
            and item.values.actor_ref == actor_ref
            and target in item.values.target_scope_refs
            and "constraint:read-only" in item.values.constraint_refs
            and _active(item.values.valid_from, item.values.expires_at, logical_time)
        )
        consents = tuple(
            item
            for item in projection.consent_grants
            if item.origin.enforcement_eligible
            and item.origin.attestation_environment == "enforcement"
            and item.values.status == "active"
            and item.values.grantor_ref == subject_ref
            and item.values.grantee_ref == actor_ref
            and "read_only_tool" in item.values.action_scope_refs
            and data_scope in item.values.data_scope_refs
            and not item.values.channel_scope_refs
            and _active(item.values.valid_from, item.values.expires_at, logical_time)
        )
        policies = tuple(
            item
            for item in projection.privacy_policies
            if item.origin.enforcement_eligible
            and item.origin.attestation_environment == "enforcement"
            and item.values.status == "active"
            and item.values.subject_ref == subject_ref
            and data_scope in item.values.data_class_refs
            and {"viewer:companion", "viewer:platform_adapter"}
            <= set(item.values.viewer_rule_refs)
            and _active(item.values.effective_at, item.values.expires_at, logical_time)
        )
        if len(capabilities) != 1 or len(consents) != 1 or len(policies) != 1:
            raise ValueError("enforcement tool authorization is missing or ambiguous")
        return ReadOnlyToolAuthorizationBinding(
            subject_ref=subject_ref,
            capability_grant_id=capabilities[0].grant_id,
            capability_grant_revision=capabilities[0].entity_revision,
            consent_id=consents[0].consent_id,
            consent_revision=consents[0].entity_revision,
            privacy_policy_id=policies[0].policy_id,
            privacy_policy_revision=policies[0].entity_revision,
        )


def _active(start: datetime, end: datetime | None, at: datetime) -> bool:
    return start <= at and (end is None or at < end)


__all__ = ["ProjectionReadOnlyToolAuthorizationResolver"]
