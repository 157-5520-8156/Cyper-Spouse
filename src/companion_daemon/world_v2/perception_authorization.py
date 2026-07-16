"""Enforcement checks for user-media perception Actions."""

from __future__ import annotations

from datetime import datetime

from .schemas import Action, LedgerProjection, PerceptionAuthorizationBinding


_DATA_SCOPE = {
    "perception:vision": "data:image_content",
    "perception:transcription": "data:audio_content",
}
_VIEWERS = frozenset({"viewer:companion", "viewer:platform_adapter"})


def require_perception_authorization(
    *, action: Action, projection: LedgerProjection, logical_time: datetime
) -> PerceptionAuthorizationBinding:
    if action.kind not in {"vision", "transcription"} or action.layer != "perception_tool":
        raise ValueError("perception authorization verifier received another Action kind")
    binding = action.perception_authorization
    if binding is None:
        raise ValueError("perception Action lacks enforcement authorization")
    required_data = _DATA_SCOPE.get(action.target)
    if required_data is None or action.target != f"perception:{action.kind}":
        raise ValueError("perception Action target is unsupported")
    capability = _one(
        projection.capability_grants,
        "grant_id",
        binding.capability_grant_id,
        "perception capability",
    )
    if capability.entity_revision != binding.capability_grant_revision or not (
        capability.origin.enforcement_eligible
        and capability.origin.attestation_environment == "enforcement"
        and capability.values.state == "active"
        and capability.values.capability_kind == "perception_tool"
        and capability.values.actor_ref == action.actor
        and action.target in capability.values.target_scope_refs
        and "constraint:read-only" in capability.values.constraint_refs
        and _active(capability.values.valid_from, capability.values.expires_at, logical_time)
    ):
        raise ValueError("perception capability is not enforcement eligible and active")
    consent = _one(
        projection.consent_grants, "consent_id", binding.consent_id, "perception consent"
    )
    if consent.entity_revision != binding.consent_revision or not (
        consent.origin.enforcement_eligible
        and consent.origin.attestation_environment == "enforcement"
        and consent.values.status == "active"
        and consent.values.grantor_ref == binding.subject_ref
        and consent.values.grantee_ref == action.actor
        and "perception_tool" in consent.values.action_scope_refs
        and required_data in consent.values.data_scope_refs
        and not consent.values.channel_scope_refs
        and _active(consent.values.valid_from, consent.values.expires_at, logical_time)
    ):
        raise ValueError("perception consent is not enforcement eligible and active")
    privacy = _one(
        projection.privacy_policies,
        "policy_id",
        binding.privacy_policy_id,
        "perception privacy policy",
    )
    if privacy.entity_revision != binding.privacy_policy_revision or not (
        privacy.origin.enforcement_eligible
        and privacy.origin.attestation_environment == "enforcement"
        and privacy.values.status == "active"
        and privacy.values.subject_ref == binding.subject_ref
        and required_data in privacy.values.data_class_refs
        and _VIEWERS <= set(privacy.values.viewer_rule_refs)
        and _active(privacy.values.effective_at, privacy.values.expires_at, logical_time)
    ):
        raise ValueError("perception privacy policy is not enforcement eligible and active")
    return binding


def _active(start: datetime, end: datetime | None, at: datetime) -> bool:
    return start <= at and (end is None or at < end)


def _one(items: tuple[object, ...], attribute: str, value: str, label: str):
    found = tuple(item for item in items if getattr(item, attribute) == value)
    if len(found) != 1:
        raise ValueError(f"{label} is missing or ambiguous")
    return found[0]


__all__ = ["require_perception_authorization"]
