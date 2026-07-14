"""Root-attested, shadow-only capability, consent, and privacy contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .actor_authority_events import DeploymentRootProof
from .schemas import (
    CapabilityGrantValues,
    ConsentGrantValues,
    FrozenModel,
    PrincipalActionEvidence,
    PrivacyPolicyValues,
)


CAPABILITY_KINDS = frozenset(
    {"message_send", "media_send", "reaction_send", "read_only_tool"}
)
TARGET_SCOPES = frozenset(
    {
        "channel:qq",
        "channel:wechat",
        "channel:http",
        "tool:weather",
        "tool:web_search",
        "tool:calendar_read",
    }
)
ACTION_SCOPES = CAPABILITY_KINDS
DATA_SCOPES = frozenset(
    {"data:message_content", "data:user_profile", "data:attachment", "data:location"}
)
CHANNEL_SCOPES = frozenset({"channel:qq", "channel:wechat", "channel:http"})
VIEWER_RULES = frozenset(
    {"viewer:companion", "viewer:operator", "viewer:room_renderer", "viewer:platform_adapter"}
)
MEDIA_RULES = frozenset(
    {"media:private_only", "media:share_allowed", "media:auto_delivery_allowed"}
)
RETENTION_RULES = frozenset(
    {"retention:session", "retention:30d", "retention:persistent"}
)
def _policy_digest(name: str, matrix: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"name": name, "matrix": matrix},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST = _policy_digest(
    "external-principal-auth.1",
    {"maximum_ttl_seconds": 600, "environment": "shadow"},
)


CAPABILITY_POLICY_DIGEST = _policy_digest(
    "capability-policy.1",
    {"kinds": sorted(CAPABILITY_KINDS), "targets": sorted(TARGET_SCOPES)},
)
CONSENT_POLICY_DIGEST = _policy_digest(
    "consent-policy.1",
    {
        "actions": sorted(ACTION_SCOPES),
        "data": sorted(DATA_SCOPES),
        "channels": sorted(CHANNEL_SCOPES),
    },
)
PRIVACY_POLICY_DIGEST = _policy_digest(
    "privacy-policy.1",
    {
        "data": sorted(DATA_SCOPES),
        "viewers": sorted(VIEWER_RULES),
        "media": sorted(MEDIA_RULES),
        "retention": sorted(RETENTION_RULES),
    },
)


class _AuthorizationMutationBase(FrozenModel):
    world_id: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    authority_id: str = Field(min_length=1)
    expected_authority_revision: int = Field(ge=1)
    attested_principal_ref: str = Field(min_length=1)
    attestation_mode: Literal["root_attested_external_principal_action.1"]
    attestation_environment: Literal["shadow"]
    principal_action_evidence: PrincipalActionEvidence
    changed_at: datetime
    compensates_transition_id: str | None = None
    root_proof: DeploymentRootProof


class CapabilityMutationPayload(_AuthorizationMutationBase):
    operation: Literal["grant", "revise", "revoke", "compensate"]
    values_before: CapabilityGrantValues | None = None
    values_after: CapabilityGrantValues
    policy_version: Literal["capability-policy.1"]
    policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_contract(self) -> CapabilityMutationPayload:
        _validate_mutation(self, "capability", CAPABILITY_POLICY_DIGEST)
        return self


class ConsentMutationPayload(_AuthorizationMutationBase):
    operation: Literal["grant", "revise", "revoke", "compensate"]
    values_before: ConsentGrantValues | None = None
    values_after: ConsentGrantValues
    policy_version: Literal["consent-policy.1"]
    policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_contract(self) -> ConsentMutationPayload:
        _validate_mutation(self, "consent", CONSENT_POLICY_DIGEST)
        return self


class PrivacyMutationPayload(_AuthorizationMutationBase):
    operation: Literal["revise", "revoke", "compensate"]
    values_before: PrivacyPolicyValues | None = None
    values_after: PrivacyPolicyValues
    policy_version: Literal["privacy-policy.1"]
    policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_contract(self) -> PrivacyMutationPayload:
        _validate_mutation(self, "privacy", PRIVACY_POLICY_DIGEST)
        return self


AuthorizationPayload = CapabilityMutationPayload | ConsentMutationPayload | PrivacyMutationPayload

_CAPABILITY_EVENTS = {
    "CapabilityGranted": "grant",
    "CapabilityRevised": "revise",
    "CapabilityRevoked": "revoke",
    "CapabilityCompensated": "compensate",
}
_CONSENT_EVENTS = {
    "ConsentGranted": "grant",
    "ConsentRevised": "revise",
    "ConsentRevoked": "revoke",
    "ConsentCompensated": "compensate",
}
_PRIVACY_EVENTS = {
    "PrivacyPolicyRevised": "revise",
    "PrivacyPolicyRevoked": "revoke",
    "PrivacyPolicyCompensated": "compensate",
}
AUTHORIZATION_EVENT_OPERATIONS = {
    **_CAPABILITY_EVENTS,
    **_CONSENT_EVENTS,
    **_PRIVACY_EVENTS,
}
AUTHORIZATION_PAYLOAD_MODELS = {
    **{name: CapabilityMutationPayload for name in _CAPABILITY_EVENTS},
    **{name: ConsentMutationPayload for name in _CONSENT_EVENTS},
    **{name: PrivacyMutationPayload for name in _PRIVACY_EVENTS},
}


def authorization_domain(event_type: str) -> Literal["capability", "consent", "privacy"]:
    if event_type in _CAPABILITY_EVENTS:
        return "capability"
    if event_type in _CONSENT_EVENTS:
        return "consent"
    if event_type in _PRIVACY_EVENTS:
        return "privacy"
    raise ValueError("unknown authorization event type")


def validate_authorization_event_operation(event_type: str, operation: str) -> None:
    if AUTHORIZATION_EVENT_OPERATIONS.get(event_type) != operation:
        raise ValueError("authorization event type does not match operation")


def authorization_scope_hash(domain: str, values: object) -> str:
    material = to_jsonable_python(values)
    if not isinstance(material, dict):
        raise TypeError("authorization values must be an object")
    if domain == "capability":
        scope = {
            key: material.get(key)
            for key in ("capability_kind", "actor_ref", "target_scope_refs", "constraint_refs")
        }
    elif domain == "consent":
        scope = {
            key: material.get(key)
            for key in (
                "grantor_ref",
                "grantee_ref",
                "action_scope_refs",
                "data_scope_refs",
                "channel_scope_refs",
            )
        }
    elif domain == "privacy":
        scope = {
            key: material.get(key)
            for key in (
                "subject_ref",
                "data_class_refs",
                "viewer_rule_refs",
                "media_rule_refs",
                "retention_rule_refs",
            )
        }
    else:
        raise ValueError("unknown authorization domain")
    return hashlib.sha256(
        json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def principal_action_evidence_hash(evidence: PrincipalActionEvidence) -> str:
    return hashlib.sha256(
        json.dumps(
            evidence.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def principal_action_challenge_identity(
    world_id: str, evidence: PrincipalActionEvidence
) -> str:
    material = {
        "world_id": world_id,
        "challenge_ref": evidence.challenge_ref,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def principal_action_source_identity(
    world_id: str, evidence: PrincipalActionEvidence
) -> str:
    material = {
        "world_id": world_id,
        "source_event_ref": evidence.source_event_ref,
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def authorization_intent_hash(domain: str, payload: Mapping[str, Any]) -> str:
    evidence = PrincipalActionEvidence.model_validate_json(
        json.dumps(payload["principal_action_evidence"])
    ).model_dump(mode="json")
    evidence.pop("intent_hash", None)
    values_type = {
        "capability": CapabilityGrantValues,
        "consent": ConsentGrantValues,
        "privacy": PrivacyPolicyValues,
    }[domain]
    values_after = values_type.model_validate_json(
        json.dumps(payload["values_after"])
    ).model_dump(mode="json")
    material = {
        "world_id": payload.get("world_id"),
        "domain": domain,
        "operation": payload.get("operation"),
        "entity_id": payload.get("entity_id"),
        "transition_id": payload.get("transition_id"),
        "expected_entity_revision": payload.get("expected_entity_revision"),
        "authority_id": payload.get("authority_id"),
        "expected_authority_revision": payload.get("expected_authority_revision"),
        "attested_principal_ref": payload.get("attested_principal_ref"),
        "attestation_mode": payload.get("attestation_mode"),
        "attestation_environment": payload.get("attestation_environment"),
        "policy_version": payload.get("policy_version"),
        "policy_digest": payload.get("policy_digest"),
        "changed_at": datetime.fromisoformat(
            str(payload.get("changed_at")).replace("Z", "+00:00")
        ).isoformat(),
        "values_after": values_after,
        "evidence": evidence,
    }
    return hashlib.sha256(
        json.dumps(
            material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def authorization_mutation_hash(
    event_type: str, payload: AuthorizationPayload | Mapping[str, Any]
) -> str:
    domain = authorization_domain(event_type)
    material = (
        payload.model_dump(mode="json")
        if isinstance(
            payload,
            (CapabilityMutationPayload, ConsentMutationPayload, PrivacyMutationPayload),
        )
        else to_jsonable_python(dict(payload))
    )
    proof = dict(material["root_proof"])
    proof.pop("signed_mutation_hash", None)
    proof.pop("signature_hex", None)
    material["root_proof"] = proof
    canonical = _canonical_unsigned(domain, material)
    return hashlib.sha256(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _canonical_unsigned(domain: str, material: Mapping[str, Any]) -> dict[str, Any]:
    copied = json.loads(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    evidence = PrincipalActionEvidence.model_validate_json(
        json.dumps(copied["principal_action_evidence"])
    ).model_dump(mode="json")
    values_type = {
        "capability": CapabilityGrantValues,
        "consent": ConsentGrantValues,
        "privacy": PrivacyPolicyValues,
    }[domain]
    copied["principal_action_evidence"] = evidence
    copied["values_after"] = values_type.model_validate_json(
        json.dumps(copied["values_after"])
    ).model_dump(mode="json")
    if copied.get("values_before") is not None:
        copied["values_before"] = values_type.model_validate_json(
            json.dumps(copied["values_before"])
        ).model_dump(mode="json")
    copied["changed_at"] = datetime.fromisoformat(
        str(copied["changed_at"]).replace("Z", "+00:00")
    ).isoformat()
    return copied


def _validate_mutation(
    payload: AuthorizationPayload, domain: str, expected_policy_digest: str
) -> None:
    if payload.policy_digest != expected_policy_digest:
        raise ValueError("authorization mutation references an uninstalled policy")
    proof = payload.root_proof
    event_type = next(
        name
        for name, operation in AUTHORIZATION_EVENT_OPERATIONS.items()
        if authorization_domain(name) == domain and operation == payload.operation
    )
    if proof.signed_mutation_hash != authorization_mutation_hash(event_type, payload):
        raise ValueError("authorization root proof hash does not match mutation")
    create_operation = "grant" if domain != "privacy" else "revise"
    if payload.expected_entity_revision == 0:
        if payload.operation != create_operation or payload.values_before is not None:
            raise ValueError("authorization creation shape is invalid")
    elif payload.values_before is None:
        raise ValueError("authorization transition requires prior values")
    if payload.expected_entity_revision > 0 and payload.operation == "grant":
        raise ValueError("existing authorization must use revise")
    if (payload.operation == "compensate") != (
        payload.compensates_transition_id is not None
    ):
        raise ValueError("authorization compensation target is inconsistent")
    if payload.principal_action_evidence.authenticated_principal_ref != payload.attested_principal_ref:
        raise ValueError("authorization evidence principal does not match attestation")
    expected_action = f"authorization:{domain}:{payload.operation}"
    if payload.principal_action_evidence.action_ref != expected_action:
        raise ValueError("authorization evidence action does not match mutation")
    if payload.principal_action_evidence.scope_hash != authorization_scope_hash(
        domain, payload.values_after
    ):
        raise ValueError("authorization evidence scope hash does not match mutation")
    if (
        payload.principal_action_evidence.authentication_policy_version
        != "external-principal-auth.1"
        or payload.principal_action_evidence.authentication_policy_digest
        != EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST
    ):
        raise ValueError("external principal authentication policy is not installed")
    if payload.principal_action_evidence.intent_hash != authorization_intent_hash(
        domain, payload.model_dump(mode="json")
    ):
        raise ValueError("authorization evidence intent hash does not match mutation")
    if not (
        payload.principal_action_evidence.observed_at
        <= payload.changed_at
        < payload.principal_action_evidence.expires_at
    ):
        raise ValueError("authorization evidence is outside its validity window")
    if (
        payload.principal_action_evidence.expires_at
        - payload.principal_action_evidence.observed_at
    ).total_seconds() > 600:
        raise ValueError("authorization evidence validity exceeds maximum ttl")
    if domain == "consent" and not payload.values_after.revocable:
        raise ValueError("shadow consent must remain revocable")
    if domain == "capability":
        values = payload.values_after
        targets = set(values.target_scope_refs)
        if values.capability_kind == "read_only_tool":
            if not all(item.startswith("tool:") for item in targets):
                raise ValueError("tool capability requires tool target scopes")
        elif not all(item.startswith("channel:") for item in targets):
            raise ValueError("communication capability requires channel target scopes")
        constraints = set(values.constraint_refs)
        if "constraint:read-only" in constraints and values.capability_kind != "read_only_tool":
            raise ValueError("read-only constraint requires tool capability")
        if "constraint:text-only" in constraints and values.capability_kind != "message_send":
            raise ValueError("text-only constraint requires message capability")
    if domain == "privacy":
        media = set(payload.values_after.media_rule_refs)
        if "media:private_only" in media and media & {
            "media:share_allowed",
            "media:auto_delivery_allowed",
        }:
            raise ValueError("private-only privacy rule conflicts with sharing")
        if "media:auto_delivery_allowed" in media and "media:share_allowed" not in media:
            raise ValueError("automatic media delivery requires sharing permission")
        if len(payload.values_after.retention_rule_refs) != 1:
            raise ValueError("privacy policy requires exactly one retention rule")
    for values in (payload.values_before, payload.values_after):
        if values is None:
            continue
        for name, value in values.model_dump().items():
            if name.endswith("_refs") and isinstance(value, tuple):
                if tuple(sorted(value)) != value or len(value) != len(set(value)):
                    raise ValueError("authorization scopes must be sorted and unique")
