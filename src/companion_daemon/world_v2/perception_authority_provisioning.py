"""Operator provisioning for the World v2 perception enforcement chain.

A perception ``ActionAuthorized`` is reducible only when the ledger already
holds the perception vertical's full enforcement authority: root-signed actor
authorities, a read-only ``perception_tool`` capability for the companion, the
user's consent covering ``data:image_content``, and a privacy policy naming
the companion and platform adapter as the only viewers.  Nothing in runtime
composition may manufacture that authority, so this module mirrors the media
lane's explicit, idempotent operator provisioning command.

The chain is deliberately vision-only: transcription stays unprovisioned (and
therefore fail-closed) until an audio archive exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
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
from .schemas import WorldEvent


_LOG = logging.getLogger(__name__)

PERCEPTION_VISION_CAPABILITY_ID = "capability:world-v2:perception-vision"
PERCEPTION_CONSENT_ID = "consent:world-v2:perception"
PERCEPTION_PRIVACY_POLICY_ID = "privacy:world-v2:perception"

_USER_AUTHORITY_ID = "authority:world-v2:perception-user"
_OPERATOR_AUTHORITY_ID = "authority:world-v2:perception-operator"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PerceptionAuthorityProvisioningResult:
    committed_event_ids: tuple[str, ...]
    already_present: tuple[str, ...]


class PerceptionAuthorityProvisioner:
    """Write the perception enforcement chain once, idempotently, at the head."""

    def __init__(
        self,
        *,
        ledger,  # SQLiteWorldLedger-compatible (structural)
        signing_key_hex: str,
        subject_ref: str,
        companion_actor_ref: str = "agent:companion",
        operator_ref: str = "operator:girl-agent",
    ) -> None:
        if not subject_ref or not companion_actor_ref or not operator_ref:
            raise ValueError(
                "perception authority provisioning requires subject, actor and operator refs"
            )
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
            raise ValueError("supplied signing key does not match any installed deployment root")
        self._ledger = ledger
        self._subject_ref = subject_ref
        self._companion_actor_ref = companion_actor_ref
        self._operator_ref = operator_ref

    @staticmethod
    def _active_authority_for(
        projection, *, principal_ref: str, required_operations: tuple[str, ...]
    ) -> str | None:
        """Resolve an existing active authority covering the needed operations.

        The actor-authority reducer allows exactly one *active* authority per
        principal, so a sibling enforcement chain (for example the media
        provisioner) may already own this principal's authority.  Reusing it
        is the intended shape; bootstrapping a second one is rejected.
        """

        for item in projection.actor_authorities:
            if (
                item.values.principal_ref == principal_ref
                and item.values.status == "active"
                and all(
                    operation in item.values.allowed_operations
                    for operation in required_operations
                )
            ):
                return item.authority_id
        return None

    def ensure(self) -> PerceptionAuthorityProvisioningResult:
        committed: list[str] = []
        present: list[str] = []
        projection = self._ledger.project()
        if projection.logical_time is None:
            raise ValueError("perception authority provisioning requires an established world clock")

        resolved_authority_ids: dict[str, str] = {}
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
            existing = self._active_authority_for(
                projection, principal_ref=principal, required_operations=operations
            )
            if existing is not None:
                resolved_authority_ids[authority_id] = existing
                present.append(existing)
                continue
            committed.extend(
                self._commit_actor_authority(
                    authority_id=authority_id,
                    principal_ref=principal,
                    principal_kind=kind,
                    allowed_operations=operations,
                )
            )
            resolved_authority_ids[authority_id] = authority_id

        capability_ids = {item.grant_id for item in self._ledger.project().capability_grants}
        if PERCEPTION_VISION_CAPABILITY_ID in capability_ids:
            present.append(PERCEPTION_VISION_CAPABILITY_ID)
        else:
            committed.extend(
                self._commit_authorization(
                    domain="capability",
                    event_type="CapabilityGranted",
                    entity_id=PERCEPTION_VISION_CAPABILITY_ID,
                    authority_id=resolved_authority_ids[_OPERATOR_AUTHORITY_ID],
                    principal_ref=self._operator_ref,
                    values={
                        "capability_kind": "perception_tool",
                        "actor_ref": self._companion_actor_ref,
                        "target_scope_refs": ["perception:vision"],
                        "constraint_refs": ["constraint:read-only"],
                        "valid_from": None,
                        "expires_at": None,
                        "state": "active",
                    },
                )
            )

        consent_ids = {item.consent_id for item in self._ledger.project().consent_grants}
        if PERCEPTION_CONSENT_ID in consent_ids:
            present.append(PERCEPTION_CONSENT_ID)
        else:
            committed.extend(
                self._commit_authorization(
                    domain="consent",
                    event_type="ConsentGranted",
                    entity_id=PERCEPTION_CONSENT_ID,
                    authority_id=resolved_authority_ids[_USER_AUTHORITY_ID],
                    principal_ref=self._subject_ref,
                    values={
                        "grantor_ref": self._subject_ref,
                        "grantee_ref": self._companion_actor_ref,
                        "action_scope_refs": ["perception_tool"],
                        "data_scope_refs": ["data:image_content"],
                        "channel_scope_refs": [],
                        "valid_from": None,
                        "expires_at": None,
                        "revocable": True,
                        "status": "active",
                    },
                )
            )

        privacy_ids = {item.policy_id for item in self._ledger.project().privacy_policies}
        if PERCEPTION_PRIVACY_POLICY_ID in privacy_ids:
            present.append(PERCEPTION_PRIVACY_POLICY_ID)
        else:
            committed.extend(
                self._commit_authorization(
                    domain="privacy",
                    event_type="PrivacyPolicyRevised",
                    entity_id=PERCEPTION_PRIVACY_POLICY_ID,
                    authority_id=resolved_authority_ids[_USER_AUTHORITY_ID],
                    principal_ref=self._subject_ref,
                    values={
                        "subject_ref": self._subject_ref,
                        "data_class_refs": ["data:image_content"],
                        "viewer_rule_refs": ["viewer:companion", "viewer:platform_adapter"],
                        "media_rule_refs": [],
                        "retention_rule_refs": ["retention:persistent"],
                        "effective_at": None,
                        "expires_at": None,
                        "status": "active",
                    },
                )
            )

        if committed:
            _LOG.warning(
                "world v2 perception enforcement authority provisioned world=%s events=%d",
                self._ledger.world_id,
                len(committed),
            )
        return PerceptionAuthorityProvisioningResult(
            committed_event_ids=tuple(committed), already_present=tuple(present)
        )

    # -- event builders --------------------------------------------------------

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
        return self._commit_signed(
            event_id=f"event:perception-authority:{authority_id}",
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
                "authentication_policy_digest": (
                    ENFORCEMENT_EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST
                ),
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
        return self._commit_signed(
            event_id=f"event:perception-authority:{entity_id}",
            event_type=event_type,
            payload=payload,
            mutation_hash=authorization_mutation_hash(event_type, payload),
            logical_time=logical_time,
        )

    # -- signing ------------------------------------------------------------------

    def _unsigned_proof(self, transition_id: str) -> dict[str, object]:
        return {
            "keyset_version": ROOT_KEYSET_VERSION,
            "keyset_digest": ROOT_KEYSET_DIGEST,
            "root_key_id": self._root_key_id,
            "nonce": "nonce:"
            + _digest({"world": self._ledger.world_id, "t": transition_id})[:32],
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
        logical_time,
    ) -> list[str]:
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=dict(payload)
        )
        if identity is None:
            raise ValueError(f"no identity contract for {event_type}")
        payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        actor = "system:perception-authority-provisioning"
        source = "world-v2:perception-authority-provisioning"
        trace_id = f"trace:perception-authority:{event_id}"
        causation_id = f"provision:{event_id}"
        correlation_id = "correlation:perception-authority-provisioning"
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
    "PERCEPTION_CONSENT_ID",
    "PERCEPTION_PRIVACY_POLICY_ID",
    "PERCEPTION_VISION_CAPABILITY_ID",
    "PerceptionAuthorityProvisioner",
    "PerceptionAuthorityProvisioningResult",
]
