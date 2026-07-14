"""Read-only shadow evaluation of ledger-backed authorization projections."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from .authorization_events import (
    ACTION_SCOPES,
    CAPABILITY_KINDS,
    CHANNEL_SCOPES,
    DATA_SCOPES,
    MEDIA_RULES,
    RETENTION_RULES,
    TARGET_SCOPES,
    VIEWER_RULES,
)
from .schemas import FrozenModel, LedgerProjection


class ShadowAuthorizationRequest(FrozenModel):
    action_actor_ref: str
    data_subject_ref: str
    capability_kind: str
    action_content_type: str
    effect_class: str
    third_party_target: bool
    target_scope_refs: tuple[str, ...]
    action_scope_refs: tuple[str, ...]
    data_scope_refs: tuple[str, ...] = ()
    channel_scope_refs: tuple[str, ...] = ()
    viewer_rule_refs: tuple[str, ...] = ()
    media_rule_refs: tuple[str, ...] = ()
    retention_rule_refs: tuple[str, ...] = ()
    logical_time: datetime


class ShadowAuthorizationDecision(FrozenModel):
    request: ShadowAuthorizationRequest
    would_allow: bool
    reason_codes: tuple[str, ...]
    capability_revision: int | None = None
    consent_revision: int | None = None
    privacy_revision: int | None = None
    attestation_modes: tuple[
        Literal["root_attested_external_principal_action.1"], ...
    ] = ()
    attestation_environment: Literal["shadow"] = "shadow"
    principal_possession_status: Literal["not_evaluated"] = "not_evaluated"
    root_attestation_verified: bool = False
    external_action_asserted: bool = False
    enforcement_eligible: Literal[False] = False


def evaluate_authorization_shadow(
    projection: LedgerProjection, request: ShadowAuthorizationRequest
) -> ShadowAuthorizationDecision:
    """Report a replayable decision without authorizing or executing anything."""

    reasons: list[str] = []
    if (
        request.capability_kind not in CAPABILITY_KINDS
        or not set(request.target_scope_refs) <= TARGET_SCOPES
        or not set(request.action_scope_refs) <= ACTION_SCOPES
        or not set(request.data_scope_refs) <= DATA_SCOPES
        or not set(request.channel_scope_refs) <= CHANNEL_SCOPES
        or not set(request.viewer_rule_refs) <= VIEWER_RULES
        or not set(request.media_rule_refs) <= MEDIA_RULES
        or not set(request.retention_rule_refs) <= RETENTION_RULES
    ):
        reasons.append("unknown_scope")
    mandatory = _mandatory_requirements(request)
    if mandatory is None:
        reasons.append("mandatory_scope_not_derivable")
    else:
        required_data, required_channels, required_viewers, required_media = mandatory
        if (
            not required_data <= set(request.data_scope_refs)
            or required_channels != set(request.channel_scope_refs)
            or not required_viewers <= set(request.viewer_rule_refs)
            or not required_media <= set(request.media_rule_refs)
            or len(request.retention_rule_refs) != 1
        ):
            reasons.append("mandatory_scope_missing")
    expected_content = {
        "message_send": "text",
        "media_send": "media",
        "reaction_send": "reaction",
        "read_only_tool": "tool_result",
    }.get(request.capability_kind)
    if expected_content is None or request.action_content_type != expected_content:
        reasons.append("action_properties_unknown")
    if request.action_scope_refs != (request.capability_kind,):
        reasons.append("action_scope_mismatch")
    if request.capability_kind in {"message_send", "media_send", "reaction_send"} and set(
        request.channel_scope_refs
    ) != set(request.target_scope_refs):
        reasons.append("channel_target_mismatch")
    if request.capability_kind == "read_only_tool" and request.effect_class != "read_only":
        reasons.append("action_effect_not_allowed")

    capability = next(
        (
            item
            for item in projection.capability_grants
            if item.values.actor_ref == request.action_actor_ref
            and item.values.capability_kind == request.capability_kind
            and set(request.target_scope_refs) <= set(item.values.target_scope_refs)
            and _active_at(
                item.values.state,
                item.values.valid_from,
                item.values.expires_at,
                request.logical_time,
            )
            and _constraints_match(item.values.constraint_refs, request)
        ),
        None,
    )
    if capability is None:
        reasons.append("capability_missing")

    consent = next(
        (
            item
            for item in projection.consent_grants
            if item.values.grantor_ref == request.data_subject_ref
            and item.values.grantee_ref == request.action_actor_ref
            and set(request.action_scope_refs) <= set(item.values.action_scope_refs)
            and set(request.data_scope_refs) <= set(item.values.data_scope_refs)
            and set(request.channel_scope_refs) <= set(item.values.channel_scope_refs)
            and _active_at(
                item.values.status,
                item.values.valid_from,
                item.values.expires_at,
                request.logical_time,
            )
        ),
        None,
    )
    if consent is None:
        reasons.append("consent_missing")

    privacy = next(
        (
            item
            for item in projection.privacy_policies
            if item.values.subject_ref == request.data_subject_ref
            and set(request.data_scope_refs) <= set(item.values.data_class_refs)
            and set(request.viewer_rule_refs) <= set(item.values.viewer_rule_refs)
            and set(request.media_rule_refs) <= set(item.values.media_rule_refs)
            and set(request.retention_rule_refs) <= set(item.values.retention_rule_refs)
            and _active_at(
                item.values.status,
                item.values.effective_at,
                item.values.expires_at,
                request.logical_time,
            )
        ),
        None,
    )
    if privacy is None:
        reasons.append("privacy_policy_missing")

    selected = tuple(
        item for item in (capability, consent, privacy) if item is not None
    )
    modes = tuple(sorted({item.origin.attestation_mode for item in selected}))
    if any(item.origin.attestation_environment != "shadow" for item in selected):
        reasons.append("non_shadow_attestation_rejected")
    return ShadowAuthorizationDecision(
        request=request,
        would_allow=not reasons,
        reason_codes=tuple(dict.fromkeys(reasons)),
        capability_revision=capability.entity_revision if capability else None,
        consent_revision=consent.entity_revision if consent else None,
        privacy_revision=privacy.entity_revision if privacy else None,
        attestation_modes=modes,
        root_attestation_verified=len(selected) == 3,
        external_action_asserted=len(selected) == 3,
    )


def _active_at(
    status: str,
    valid_from: datetime,
    expires_at: datetime | None,
    logical_time: datetime,
) -> bool:
    return (
        status == "active"
        and valid_from <= logical_time
        and (expires_at is None or logical_time < expires_at)
    )


def _constraints_match(
    constraints: tuple[str, ...], request: ShadowAuthorizationRequest
) -> bool:
    if "constraint:text-only" in constraints and request.action_content_type != "text":
        return False
    if "constraint:read-only" in constraints and request.effect_class != "read_only":
        return False
    if "constraint:no-third-party" in constraints and request.third_party_target:
        return False
    return True


def _mandatory_requirements(
    request: ShadowAuthorizationRequest,
) -> tuple[set[str], set[str], set[str], set[str]] | None:
    targets = set(request.target_scope_refs)
    if not targets:
        return None
    viewers = {"viewer:companion", "viewer:platform_adapter"}
    if request.capability_kind in {"message_send", "reaction_send"}:
        if not all(item.startswith("channel:") for item in targets):
            return None
        return {"data:message_content"}, targets, viewers, set()
    if request.capability_kind == "media_send":
        if not all(item.startswith("channel:") for item in targets):
            return None
        return (
            {"data:attachment", "data:message_content"},
            targets,
            viewers,
            {"media:share_allowed"},
        )
    if request.capability_kind == "read_only_tool":
        if not all(item.startswith("tool:") for item in targets):
            return None
        tool_data = {
            "tool:weather": "data:location",
            "tool:web_search": "data:message_content",
            "tool:calendar_read": "data:user_profile",
        }
        if not targets <= set(tool_data):
            return None
        return {tool_data[item] for item in targets}, set(), viewers, set()
    return None
