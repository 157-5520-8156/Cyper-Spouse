"""Enforcement-grade authorization for immutable read-only tool Actions."""

from __future__ import annotations

from datetime import datetime

from .schemas import Action, LedgerProjection, ReadOnlyToolAuthorizationBinding


_TOOL_DATA_SCOPE = {
    "tool:weather": "data:location",
    "tool:web_search": "data:message_content",
    "tool:calendar_read": "data:user_profile",
}
_REQUIRED_VIEWERS = frozenset({"viewer:companion", "viewer:platform_adapter"})


def require_read_only_tool_authorization(
    *, action: Action, projection: LedgerProjection, logical_time: datetime
) -> ReadOnlyToolAuthorizationBinding:
    """Verify the exact authorization revisions before an external lookup.

    This deliberately does not use the shadow evaluator: a shadow decision is
    diagnostic only and can never release user-derived query material.
    """

    if action.kind != "read_only_tool" or action.layer != "read_only_tool":
        raise ValueError("tool authorization verifier received a non-tool Action")
    binding = action.read_only_tool_authorization
    if binding is None:
        raise ValueError("read-only tool Action lacks enforcement authorization binding")
    required_data = _TOOL_DATA_SCOPE.get(action.target)
    if required_data is None:
        raise ValueError("read-only tool Action has an unsupported target")

    capability = _exact(projection.capability_grants, "grant_id", binding.capability_grant_id, "tool capability")
    if capability.entity_revision != binding.capability_grant_revision:
        raise ValueError("tool capability revision is stale")
    if (
        not capability.origin.enforcement_eligible
        or capability.origin.attestation_environment != "enforcement"
        or capability.values.state != "active"
        or capability.values.capability_kind != "read_only_tool"
        or capability.values.actor_ref != action.actor
        or action.target not in capability.values.target_scope_refs
        or "constraint:read-only" not in capability.values.constraint_refs
        or not _active(capability.values.valid_from, capability.values.expires_at, logical_time)
    ):
        raise ValueError("tool capability is not enforcement eligible and active")

    consent = _exact(projection.consent_grants, "consent_id", binding.consent_id, "tool consent")
    if consent.entity_revision != binding.consent_revision:
        raise ValueError("tool consent revision is stale")
    if (
        not consent.origin.enforcement_eligible
        or consent.origin.attestation_environment != "enforcement"
        or consent.values.status != "active"
        or consent.values.grantor_ref != binding.subject_ref
        or consent.values.grantee_ref != action.actor
        or "read_only_tool" not in consent.values.action_scope_refs
        or required_data not in consent.values.data_scope_refs
        or consent.values.channel_scope_refs
        or not _active(consent.values.valid_from, consent.values.expires_at, logical_time)
    ):
        raise ValueError("tool consent is not enforcement eligible and active")

    privacy = _exact(projection.privacy_policies, "policy_id", binding.privacy_policy_id, "tool privacy policy")
    if privacy.entity_revision != binding.privacy_policy_revision:
        raise ValueError("tool privacy policy revision is stale")
    if (
        not privacy.origin.enforcement_eligible
        or privacy.origin.attestation_environment != "enforcement"
        or privacy.values.status != "active"
        or privacy.values.subject_ref != binding.subject_ref
        or required_data not in privacy.values.data_class_refs
        or not _REQUIRED_VIEWERS <= set(privacy.values.viewer_rule_refs)
        or not _active(privacy.values.effective_at, privacy.values.expires_at, logical_time)
    ):
        raise ValueError("tool privacy policy is not enforcement eligible and active")
    return binding


def _active(start: datetime, end: datetime | None, logical_time: datetime) -> bool:
    return start <= logical_time and (end is None or logical_time < end)


def _exact(items: tuple[object, ...], attribute: str, value: str, label: str):
    matches = tuple(item for item in items if getattr(item, attribute) == value)
    if len(matches) != 1:
        raise ValueError(f"{label} is missing or ambiguous")
    return matches[0]


__all__ = ["require_read_only_tool_authorization"]
