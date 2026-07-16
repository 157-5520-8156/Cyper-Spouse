"""Fail-closed deterministic resolver for perception enforcement grants."""

from __future__ import annotations
from datetime import datetime
from .schemas import LedgerProjection, PerceptionAuthorizationBinding

_DATA = {
    "perception:vision": "data:image_content",
    "perception:transcription": "data:audio_content",
}


class ProjectionPerceptionAuthorizationResolver:
    def resolve(
        self,
        *,
        projection: LedgerProjection,
        actor_ref: str,
        subject_ref: str,
        target: str,
        logical_time: object,
    ) -> PerceptionAuthorizationBinding:
        if not isinstance(logical_time, datetime) or target not in _DATA:
            raise ValueError("perception authorization needs a supported target and logical time")

        def active(start, end) -> bool:
            return start <= logical_time and (end is None or logical_time < end)

        caps = tuple(
            x
            for x in projection.capability_grants
            if x.origin.enforcement_eligible
            and x.origin.attestation_environment == "enforcement"
            and x.values.state == "active"
            and x.values.capability_kind == "perception_tool"
            and x.values.actor_ref == actor_ref
            and target in x.values.target_scope_refs
            and "constraint:read-only" in x.values.constraint_refs
            and active(x.values.valid_from, x.values.expires_at)
        )
        consents = tuple(
            x
            for x in projection.consent_grants
            if x.origin.enforcement_eligible
            and x.origin.attestation_environment == "enforcement"
            and x.values.status == "active"
            and x.values.grantor_ref == subject_ref
            and x.values.grantee_ref == actor_ref
            and "perception_tool" in x.values.action_scope_refs
            and _DATA[target] in x.values.data_scope_refs
            and not x.values.channel_scope_refs
            and active(x.values.valid_from, x.values.expires_at)
        )
        policies = tuple(
            x
            for x in projection.privacy_policies
            if x.origin.enforcement_eligible
            and x.origin.attestation_environment == "enforcement"
            and x.values.status == "active"
            and x.values.subject_ref == subject_ref
            and _DATA[target] in x.values.data_class_refs
            and {"viewer:companion", "viewer:platform_adapter"} <= set(x.values.viewer_rule_refs)
            and active(x.values.effective_at, x.values.expires_at)
        )
        if len(caps) != 1 or len(consents) != 1 or len(policies) != 1:
            raise ValueError("perception enforcement authorization is missing or ambiguous")
        return PerceptionAuthorizationBinding(
            subject_ref=subject_ref,
            capability_grant_id=caps[0].grant_id,
            capability_grant_revision=caps[0].entity_revision,
            consent_id=consents[0].consent_id,
            consent_revision=consents[0].entity_revision,
            privacy_policy_id=policies[0].policy_id,
            privacy_policy_revision=policies[0].entity_revision,
        )


__all__ = ["ProjectionPerceptionAuthorizationResolver"]
