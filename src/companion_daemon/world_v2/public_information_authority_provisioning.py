"""Root-attested deployment authority for the public-information channel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
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
    CAPABILITY_POLICY_V2_DIGEST,
    ENFORCEMENT_EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST,
    authorization_intent_hash,
    authorization_mutation_hash,
    authorization_scope_hash,
)
from .event_identity import domain_idempotency_key
from .external_world_perception.production_attention import (
    public_information_capability_id,
)
from .schemas import WorldEvent


PUBLIC_INFORMATION_OPERATOR_AUTHORITY_ID = "authority:world-v2:public-information-operator"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PublicInformationAuthorityProvisioningResult:
    committed_event_ids: tuple[str, ...]
    already_present: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return True


class PublicInformationAuthorityProvisioner:
    """Install one idempotent public-web read capability at the ledger head."""

    def __init__(
        self,
        *,
        ledger,
        signing_key_hex: str,
        companion_actor_ref: str,
        registry_content_hash: str,
        operator_ref: str = "operator:girl-agent",
    ) -> None:
        if not companion_actor_ref or not operator_ref:
            raise ValueError("public information authority requires actor and operator refs")
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
            raise ValueError("supplied signing key does not match an installed deployment root")
        self._ledger = ledger
        self._companion_actor_ref = companion_actor_ref
        self._operator_ref = operator_ref
        self._capability_id = public_information_capability_id(registry_content_hash)

    def ensure(self) -> PublicInformationAuthorityProvisioningResult:
        projection = self._ledger.project()
        if projection.logical_time is None:
            raise ValueError("public information provisioning requires a World clock")
        committed: list[str] = []
        present: list[str] = []

        authority = self._active_authority_for(
            projection,
            principal_ref=self._operator_ref,
            required_operations=("capability_grant",),
        )
        if authority is None:
            committed.extend(self._commit_actor_authority())
        else:
            values = authority.values
            if (
                values.status != "active"
                or values.principal_ref != self._operator_ref
                or "capability_grant" not in values.allowed_operations
            ):
                raise ValueError("existing public information operator authority conflicts")
            present.append(authority.authority_id)

        projection = self._ledger.project()
        authority = self._active_authority_for(
            projection,
            principal_ref=self._operator_ref,
            required_operations=("capability_grant",),
        )
        if authority is None:
            raise RuntimeError("public information operator authority was not committed")
        grant = next(
            (item for item in projection.capability_grants if item.grant_id == self._capability_id),
            None,
        )
        if grant is None:
            committed.extend(
                self._commit_capability(
                    authority_id=authority.authority_id,
                    authority_revision=authority.entity_revision,
                )
            )
        else:
            values = grant.values
            if (
                values.state != "active"
                or values.actor_ref != self._companion_actor_ref
                or values.capability_kind != "public_information_read"
                or tuple(values.target_scope_refs) != ("channel:public_information",)
                or tuple(values.constraint_refs) != ("constraint:read-only",)
                or not grant.origin.enforcement_eligible
            ):
                raise ValueError("existing public information capability conflicts")
            present.append(self._capability_id)
        return PublicInformationAuthorityProvisioningResult(
            committed_event_ids=tuple(committed),
            already_present=tuple(present),
        )

    @staticmethod
    def _active_authority_for(
        projection: object,
        *,
        principal_ref: str,
        required_operations: tuple[str, ...],
    ) -> object | None:
        for item in projection.actor_authorities:  # type: ignore[attr-defined]
            if (
                item.values.principal_ref == principal_ref
                and item.values.status == "active"
                and all(
                    operation in item.values.allowed_operations for operation in required_operations
                )
            ):
                return item
        return None

    def _commit_actor_authority(self) -> list[str]:
        logical_time = self._logical_time()
        transition_id = f"transition:{PUBLIC_INFORMATION_OPERATOR_AUTHORITY_ID}"
        payload: dict[str, object] = {
            "world_id": self._ledger.world_id,
            "authority_id": PUBLIC_INFORMATION_OPERATOR_AUTHORITY_ID,
            "transition_id": transition_id,
            "operation": "bootstrap",
            "expected_entity_revision": 0,
            "values_before": None,
            "values_after": {
                "principal_ref": self._operator_ref,
                "principal_kind": "deployment_operator",
                "credential_ref": f"credential:{self._operator_ref}",
                "allowed_operations": ["capability_grant"],
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
            event_id=f"event:public-information-authority:{PUBLIC_INFORMATION_OPERATOR_AUTHORITY_ID}",
            event_type="ActorAuthorityBootstrapped",
            payload=payload,
            mutation_hash=actor_authority_mutation_hash(payload),
            logical_time=logical_time,
        )

    def _commit_capability(self, *, authority_id: str, authority_revision: int) -> list[str]:
        logical_time = self._logical_time()
        transition_id = f"transition:{self._capability_id}"
        values: dict[str, object] = {
            "capability_kind": "public_information_read",
            "actor_ref": self._companion_actor_ref,
            "target_scope_refs": ["channel:public_information"],
            "constraint_refs": ["constraint:read-only"],
            "valid_from": logical_time.isoformat(),
            "expires_at": None,
            "state": "active",
        }
        payload: dict[str, object] = {
            "world_id": self._ledger.world_id,
            "entity_id": self._capability_id,
            "transition_id": transition_id,
            "operation": "grant",
            "expected_entity_revision": 0,
            "values_before": None,
            "values_after": values,
            "authority_id": authority_id,
            "expected_authority_revision": authority_revision,
            "attested_principal_ref": self._operator_ref,
            "attestation_mode": "root_attested_external_principal_action.1",
            "attestation_environment": "enforcement",
            "principal_action_evidence": {
                "source_event_ref": f"evidence:{transition_id}",
                "payload_hash": _digest({"evidence": transition_id}),
                "authenticated_principal_ref": self._operator_ref,
                "action_ref": "authorization:capability:grant",
                "scope_hash": authorization_scope_hash("capability", values),
                "intent_hash": "0" * 64,
                "challenge_ref": f"challenge:{transition_id}",
                "observed_at": logical_time.isoformat(),
                "expires_at": (logical_time + timedelta(minutes=5)).isoformat(),
                "authentication_policy_version": "external-principal-auth.enforcement.1",
                "authentication_policy_digest": ENFORCEMENT_EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST,
            },
            "policy_version": "capability-policy.2",
            "policy_digest": CAPABILITY_POLICY_V2_DIGEST,
            "changed_at": logical_time.isoformat(),
            "compensates_transition_id": None,
            "root_proof": self._unsigned_proof(transition_id),
        }
        payload["principal_action_evidence"]["intent_hash"] = authorization_intent_hash(
            "capability", payload
        )
        payload["root_proof"]["signed_mutation_hash"] = authorization_mutation_hash(
            "CapabilityGranted", payload
        )
        return self._commit_signed(
            event_id=f"event:public-information-authority:{self._capability_id}",
            event_type="CapabilityGranted",
            payload=payload,
            mutation_hash=authorization_mutation_hash("CapabilityGranted", payload),
            logical_time=logical_time,
        )

    def _logical_time(self) -> datetime:
        logical_time = self._ledger.project().logical_time
        if logical_time is None:
            raise ValueError("public information provisioning requires a World clock")
        return logical_time

    def _unsigned_proof(self, transition_id: str) -> dict[str, object]:
        return {
            "keyset_version": ROOT_KEYSET_VERSION,
            "keyset_digest": ROOT_KEYSET_DIGEST,
            "root_key_id": self._root_key_id,
            "nonce": "nonce:"
            + _digest({"world": self._ledger.world_id, "transition": transition_id})[:32],
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
            event_type=event_type,
            world_id=self._ledger.world_id,
            payload=dict(payload),
        )
        if identity is None:
            raise ValueError(f"no identity contract for {event_type}")
        mutable = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        actor = "system:public-information-authority-provisioning"
        source = "world-v2:public-information-authority-provisioning"
        trace_id = f"trace:public-information-authority:{event_id}"
        causation_id = f"provision:{event_id}"
        correlation_id = "correlation:public-information-authority-provisioning"
        mutable["root_proof"]["signature_hex"] = self._signing_key.sign(
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
            payload=mutable,
        )
        self._ledger.commit(
            (event,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
        return [event.event_id]


__all__ = [
    "PUBLIC_INFORMATION_OPERATOR_AUTHORITY_ID",
    "PublicInformationAuthorityProvisioner",
    "PublicInformationAuthorityProvisioningResult",
]
