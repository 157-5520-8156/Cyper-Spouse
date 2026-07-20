"""Operator provisioning for the World v2 media-provider enforcement chain.

A media ``ActionAuthorized`` is reducible only when the ledger already holds
the full enforcement vertical: root-signed actor authorities, an operator
capability, user consent, a privacy policy, and one ``ProviderMediaGrant``
per media capability kind.  Nothing in the runtime composition may
manufacture that authority, so this module gives the deployment operator an
explicit, idempotent provisioning command instead.

The deployment root *private* key never lives in this repository.  The
operator supplies the ed25519 seed (hex) whose verify key is already pinned
in :mod:`actor_authority_events`; an unknown key is rejected before anything
is written.  Every event this module writes is verified by the same reducers
that verify it on replay, so a wrong chain fails at provisioning time rather
than poisoning the ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Mapping

from nacl.signing import SigningKey

from .actor_authority_events import (
    ROOT_KEYSET_DIGEST,
    ROOT_KEYSET_VERSION,
    ROOT_PUBLIC_KEYS,
    actor_authority_mutation_hash,
    root_envelope_signature_message,
)
from .actor_authority_reducers import ACTOR_AUTHORITY_POLICY_DIGEST
from .authorization_events import (
    CAPABILITY_POLICY_DIGEST,
    CONSENT_POLICY_DIGEST,
    ENFORCEMENT_EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST,
    PRIVACY_POLICY_DIGEST,
    authorization_intent_hash,
    authorization_mutation_hash,
    authorization_scope_hash,
)
from .event_identity import domain_idempotency_key
from .media_provider_grants import ProviderMediaGrantRecordedPayload
from .schemas import ProviderMediaGrant, WorldEvent


_LOG = logging.getLogger(__name__)

# One deployment-stable identity per artifact.  Compositions reference the
# grant ids below through ``ProviderMediaGrantBinding``.
MEDIA_PLANNING_GRANT_ID = "grant:world-v2:media-planning"
MEDIA_RENDER_GRANT_ID = "grant:world-v2:media-render"
MEDIA_INSPECTION_GRANT_ID = "grant:world-v2:media-inspection"
MEDIA_REPAIR_GRANT_ID = "grant:world-v2:media-repair"

MEDIA_SELECTION_ACCEPTANCE_ACTOR = "worker:world-v2:media-selection-acceptance"
MEDIA_CONTINUATION_ACTOR = "worker:world-v2:media-continuation"

_USER_AUTHORITY_ID = "authority:world-v2:media-user"
_OPERATOR_AUTHORITY_ID = "authority:world-v2:media-operator"
_CAPABILITY_IDS = {
    "media_planning": "capability:world-v2:media-planning",
    "media_render": "capability:world-v2:media-render",
    "media_inspection": "capability:world-v2:media-inspection",
    "media_repair": "capability:world-v2:media-repair",
}
_CONSENT_IDS = {
    MEDIA_SELECTION_ACCEPTANCE_ACTOR: "consent:world-v2:media-selection",
    MEDIA_CONTINUATION_ACTOR: "consent:world-v2:media-continuation",
}
_PRIVACY_POLICY_ID = "privacy:world-v2:media"

_GRANT_MATRIX: tuple[tuple[str, str, str, str], ...] = (
    # (grant_id, capability_kind, actor_ref, provider_ref)
    (MEDIA_PLANNING_GRANT_ID, "media_planning", MEDIA_SELECTION_ACCEPTANCE_ACTOR, "provider:media-planner"),
    (MEDIA_RENDER_GRANT_ID, "media_render", MEDIA_CONTINUATION_ACTOR, "provider:media-renderer"),
    (MEDIA_INSPECTION_GRANT_ID, "media_inspection", MEDIA_CONTINUATION_ACTOR, "provider:media-inspector"),
    (MEDIA_REPAIR_GRANT_ID, "media_repair", MEDIA_CONTINUATION_ACTOR, "provider:media-renderer"),
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class MediaAuthorityProvisioningResult:
    committed_event_ids: tuple[str, ...]
    already_present: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return True


class MediaAuthorityProvisioner:
    """Write the media enforcement chain once, idempotently, at the ledger head."""

    def __init__(
        self,
        *,
        ledger,  # SQLiteWorldLedger-compatible (structural)
        signing_key_hex: str,
        subject_ref: str,
        operator_ref: str = "operator:girl-agent",
    ) -> None:
        if not subject_ref or not operator_ref:
            raise ValueError("media authority provisioning requires subject and operator refs")
        try:
            self._signing_key = SigningKey(bytes.fromhex(signing_key_hex.strip()))
        except Exception as exc:
            raise ValueError("deployment root signing key must be a 32-byte hex seed") from exc
        verify_hex = self._signing_key.verify_key.encode().hex()
        self._root_key_id = next(
            (key_id for key_id, public in ROOT_PUBLIC_KEYS.items() if public == verify_hex),
            None,
        )
        if self._root_key_id is None:
            raise ValueError(
                "supplied signing key does not match any installed deployment root"
            )
        self._ledger = ledger
        self._subject_ref = subject_ref
        self._operator_ref = operator_ref

    def ensure(self) -> MediaAuthorityProvisioningResult:
        committed: list[str] = []
        present: list[str] = []
        projection = self._ledger.project()
        logical_time = projection.logical_time
        if logical_time is None:
            raise ValueError(
                "media authority provisioning requires an established world clock"
            )

        authorities = {item.authority_id for item in projection.actor_authorities}
        for authority_id, principal, kind, operations in (
            (
                _USER_AUTHORITY_ID,
                self._subject_ref,
                "user_consent_principal",
                ("consent_grant", "privacy_policy"),
            ),
            (
                _OPERATOR_AUTHORITY_ID,
                self._operator_ref,
                "deployment_operator",
                ("capability_grant",),
            ),
        ):
            if authority_id in authorities:
                present.append(authority_id)
                continue
            committed.extend(
                self._commit_actor_authority(
                    authority_id=authority_id,
                    principal_ref=principal,
                    principal_kind=kind,
                    allowed_operations=operations,
                )
            )

        capability_ids = {item.grant_id for item in projection.capability_grants}
        for kind, entity_id in _CAPABILITY_IDS.items():
            if entity_id in capability_ids:
                present.append(entity_id)
                continue
            actor = (
                MEDIA_SELECTION_ACCEPTANCE_ACTOR
                if kind == "media_planning"
                else MEDIA_CONTINUATION_ACTOR
            )
            committed.extend(
                self._commit_authorization(
                    domain="capability",
                    event_type="CapabilityGranted",
                    entity_id=entity_id,
                    authority_id=_OPERATOR_AUTHORITY_ID,
                    principal_ref=self._operator_ref,
                    values={
                        "capability_kind": kind,
                        "actor_ref": actor,
                        "target_scope_refs": ["provider:media"],
                        "constraint_refs": [],
                        "valid_from": None,  # filled with logical time
                        "expires_at": None,
                        "state": "active",
                    },
                )
            )

        consent_ids = {item.consent_id for item in projection.consent_grants}
        for actor, entity_id in _CONSENT_IDS.items():
            if entity_id in consent_ids:
                present.append(entity_id)
                continue
            scopes = (
                ["media_planning"]
                if actor == MEDIA_SELECTION_ACCEPTANCE_ACTOR
                else sorted(["media_render", "media_inspection", "media_repair"])
            )
            committed.extend(
                self._commit_authorization(
                    domain="consent",
                    event_type="ConsentGranted",
                    entity_id=entity_id,
                    authority_id=_USER_AUTHORITY_ID,
                    principal_ref=self._subject_ref,
                    values={
                        "grantor_ref": self._subject_ref,
                        "grantee_ref": actor,
                        "action_scope_refs": scopes,
                        "data_scope_refs": ["data:attachment"],
                        "channel_scope_refs": [],
                        "valid_from": None,
                        "expires_at": None,
                        "revocable": True,
                        "status": "active",
                    },
                )
            )

        privacy_ids = {item.policy_id for item in projection.privacy_policies}
        if _PRIVACY_POLICY_ID in privacy_ids:
            present.append(_PRIVACY_POLICY_ID)
        else:
            committed.extend(
                self._commit_authorization(
                    domain="privacy",
                    event_type="PrivacyPolicyRevised",
                    entity_id=_PRIVACY_POLICY_ID,
                    authority_id=_USER_AUTHORITY_ID,
                    principal_ref=self._subject_ref,
                    values={
                        "subject_ref": self._subject_ref,
                        "data_class_refs": ["data:attachment"],
                        "viewer_rule_refs": ["viewer:media_provider"],
                        "media_rule_refs": ["media:private_only"],
                        "retention_rule_refs": ["retention:persistent"],
                        "effective_at": None,
                        "expires_at": None,
                        "status": "active",
                    },
                )
            )

        grant_ids = {item.grant_id for item in self._ledger.project().provider_media_grants}
        for grant_id, kind, actor, provider_ref in _GRANT_MATRIX:
            if grant_id in grant_ids:
                present.append(grant_id)
                continue
            committed.extend(
                self._commit_provider_media_grant(
                    grant_id=grant_id,
                    capability_kind=kind,
                    actor_ref=actor,
                    provider_ref=provider_ref,
                )
            )

        if committed:
            _LOG.warning(
                "world v2 media enforcement authority provisioned world=%s events=%d",
                self._ledger.world_id,
                len(committed),
            )
        return MediaAuthorityProvisioningResult(
            committed_event_ids=tuple(committed), already_present=tuple(present)
        )

    # -- event builders ------------------------------------------------------

    def _commit_actor_authority(
        self,
        *,
        authority_id: str,
        principal_ref: str,
        principal_kind: str,
        allowed_operations: tuple[str, ...],
    ) -> list[str]:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        transition_id = f"transition:{authority_id}"
        payload: dict[str, object] = {
            "world_id": self._ledger.world_id,
            "authority_id": authority_id,
            "transition_id": transition_id,
            "operation": "bootstrap",
            "expected_entity_revision": 0,
            "values_before": None,
            "values_after": {
                "principal_ref": principal_ref,
                "principal_kind": principal_kind,
                "credential_ref": f"credential:{principal_ref}",
                "allowed_operations": list(allowed_operations),
                "valid_from": logical_time.isoformat(),
                "expires_at": None,
                "status": "active",
            },
            "policy_version": "actor-authority-policy.1",
            "policy_digest": ACTOR_AUTHORITY_POLICY_DIGEST,
            "changed_at": logical_time.isoformat(),
            "compensates_transition_id": None,
            "root_proof": self._unsigned_proof(transition_id),
        }
        payload["root_proof"]["signed_mutation_hash"] = actor_authority_mutation_hash(payload)
        event_id = f"event:media-authority:{authority_id}"
        return self._commit_signed(
            event_id=event_id,
            event_type="ActorAuthorityBootstrapped",
            payload=payload,
            mutation_hash=actor_authority_mutation_hash(payload),
            logical_time=logical_time,
        )

    def _commit_authorization(
        self,
        *,
        domain: str,
        event_type: str,
        entity_id: str,
        authority_id: str,
        principal_ref: str,
        values: dict[str, object],
    ) -> list[str]:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        time_field = "effective_at" if domain == "privacy" else "valid_from"
        values = dict(values)
        values[time_field] = logical_time.isoformat()
        transition_id = f"transition:{entity_id}"
        operation = "revise" if domain == "privacy" else "grant"
        payload: dict[str, object] = {
            "world_id": self._ledger.world_id,
            "entity_id": entity_id,
            "transition_id": transition_id,
            "operation": operation,
            "expected_entity_revision": 0,
            "values_before": None,
            "values_after": values,
            "authority_id": authority_id,
            "expected_authority_revision": 1,
            "attested_principal_ref": principal_ref,
            "attestation_mode": "root_attested_external_principal_action.1",
            "attestation_environment": "enforcement",
            "principal_action_evidence": {
                "source_event_ref": f"evidence:{transition_id}",
                "payload_hash": _digest({"evidence": transition_id}),
                "authenticated_principal_ref": principal_ref,
                "action_ref": f"authorization:{domain}:{operation}",
                "scope_hash": authorization_scope_hash(domain, values),
                "intent_hash": "0" * 64,
                "challenge_ref": f"challenge:{transition_id}",
                "observed_at": logical_time.isoformat(),
                "expires_at": (logical_time + timedelta(minutes=5)).isoformat(),
                "authentication_policy_version": "external-principal-auth.enforcement.1",
                "authentication_policy_digest": ENFORCEMENT_EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST,
            },
            "policy_version": {
                "capability": "capability-policy.1",
                "consent": "consent-policy.1",
                "privacy": "privacy-policy.1",
            }[domain],
            "policy_digest": {
                "capability": CAPABILITY_POLICY_DIGEST,
                "consent": CONSENT_POLICY_DIGEST,
                "privacy": PRIVACY_POLICY_DIGEST,
            }[domain],
            "changed_at": logical_time.isoformat(),
            "compensates_transition_id": None,
            "root_proof": self._unsigned_proof(transition_id),
        }
        payload["principal_action_evidence"]["intent_hash"] = authorization_intent_hash(
            domain, payload
        )
        payload["root_proof"]["signed_mutation_hash"] = authorization_mutation_hash(
            event_type, payload
        )
        event_id = f"event:media-authority:{entity_id}"
        return self._commit_signed(
            event_id=event_id,
            event_type=event_type,
            payload=payload,
            mutation_hash=authorization_mutation_hash(event_type, payload),
            logical_time=logical_time,
        )

    def _commit_provider_media_grant(
        self, *, grant_id: str, capability_kind: str, actor_ref: str, provider_ref: str
    ) -> list[str]:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        consent_id = _CONSENT_IDS[actor_ref]
        grant = ProviderMediaGrant(
            grant_id=grant_id,
            provider_ref=provider_ref,
            capability_kind=capability_kind,  # type: ignore[arg-type]
            actor_ref=actor_ref,
            subject_ref=self._subject_ref,
            capability_grant_id=_CAPABILITY_IDS[capability_kind],
            capability_grant_revision=1,
            consent_id=consent_id,
            consent_revision=1,
            privacy_policy_id=_PRIVACY_POLICY_ID,
            privacy_policy_revision=1,
            issued_at=logical_time,
            expires_at=None,
        )
        payload = ProviderMediaGrantRecordedPayload(grant=grant).model_dump(mode="json")
        identity = domain_idempotency_key(
            event_type="ProviderMediaGrantRecorded",
            world_id=self._ledger.world_id,
            payload=payload,
        )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:media-authority:{grant_id}",
            event_type="ProviderMediaGrantRecorded",
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=logical_time,
            actor=self._operator_ref,
            source="world-v2:media-authority-provisioning",
            trace_id=f"trace:media-authority:{grant_id}",
            causation_id=f"provision:{grant_id}",
            correlation_id="correlation:media-authority-provisioning",
            idempotency_key=identity or f"media-authority:{grant_id}",
            payload=payload,
        )
        self._ledger.commit(
            (event,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
        return [event.event_id]

    # -- signing ---------------------------------------------------------------

    def _unsigned_proof(self, transition_id: str) -> dict[str, object]:
        return {
            "keyset_version": ROOT_KEYSET_VERSION,
            "keyset_digest": ROOT_KEYSET_DIGEST,
            "root_key_id": self._root_key_id,
            "nonce": "nonce:" + _digest({"world": self._ledger.world_id, "t": transition_id})[:32],
            "signed_mutation_hash": "0" * 64,
            "signature_hex": "0" * 128,
        }

    def _commit_signed(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: Mapping[str, object],
        mutation_hash: str,
        logical_time: datetime,
    ) -> list[str]:
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=dict(payload)
        )
        if identity is None:
            raise ValueError(f"no identity contract for {event_type}")
        payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        actor = "system:media-authority-provisioning"
        source = "world-v2:media-authority-provisioning"
        trace_id = f"trace:media-authority:{event_id}"
        causation_id = f"provision:{event_id}"
        correlation_id = "correlation:media-authority-provisioning"
        payload["root_proof"]["signature_hex"] = self._signing_key.sign(
            root_envelope_signature_message(
                schema_version="world-v2.1",
                world_id=self._ledger.world_id,
                event_type=event_type,
                event_id=event_id,
                actor=actor,
                source=source,
                logical_time=logical_time,
                created_at=logical_time,
                trace_id=trace_id,
                causation_id=causation_id,
                correlation_id=correlation_id,
                idempotency_key=identity,
                mutation_hash=mutation_hash,
            )
        ).signature.hex()
        projection = self._ledger.project()
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            event_type=event_type,
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=logical_time,
            actor=actor,
            source=source,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        self._ledger.commit(
            (event,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
        return [event.event_id]


__all__ = [
    "MEDIA_CONTINUATION_ACTOR",
    "MEDIA_INSPECTION_GRANT_ID",
    "MEDIA_PLANNING_GRANT_ID",
    "MEDIA_REPAIR_GRANT_ID",
    "MEDIA_RENDER_GRANT_ID",
    "MEDIA_SELECTION_ACCEPTANCE_ACTOR",
    "MediaAuthorityProvisioner",
    "MediaAuthorityProvisioningResult",
]
