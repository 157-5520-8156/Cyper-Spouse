"""Root-signed ActorAuthority mutation contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
import os
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from .schemas import ActorAuthorityValues, FrozenModel, WorldEvent


ROOT_KEYSET_VERSION = "deployment-root-keyset.1"
_ROOT_KEYSET_ITEMS = (
    (
        "deployment-root:production-1",
        "e906091515984b3aef1f4e7200959d594d88632eaf4a9c0ff6b5bba82aba6212",
    ),
    (
        "test-only:development-root-1",
        "d04ab232742bb4ab3a1368bd4615e4e6d0224ab71a016baf8520a332c9778737",
    ),
)
ROOT_PUBLIC_KEYS = MappingProxyType(dict(_ROOT_KEYSET_ITEMS))
ROOT_KEYSET_DIGEST = hashlib.sha256(
    json.dumps(dict(_ROOT_KEYSET_ITEMS), sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
).hexdigest()


def installed_root_keyset_digest() -> str:
    """Recompute the digest from the exact immutable artifact used for verification."""

    return hashlib.sha256(
        json.dumps(dict(ROOT_PUBLIC_KEYS), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def installed_root_verify_key(
    *, root_key_id: str, claimed_keyset_digest: str
) -> str | None:
    """Resolve a verifier only from the exact claimed frozen keyset."""

    if claimed_keyset_digest != installed_root_keyset_digest():
        return None
    return ROOT_PUBLIC_KEYS.get(root_key_id)


def verify_deployment_root_attestation(
    *, proof: DeploymentRootProof, event: WorldEvent, mutation_hash: str
) -> tuple[str, str]:
    """Verify the one frozen deployment-root envelope attestation scheme."""

    if proof.signed_mutation_hash != mutation_hash:
        raise ValueError("deployment root proof hash does not match mutation")
    public_key = installed_root_verify_key(
        root_key_id=proof.root_key_id,
        claimed_keyset_digest=proof.keyset_digest,
    )
    if proof.keyset_version != ROOT_KEYSET_VERSION or public_key is None:
        raise ValueError("deployment root anchor is not installed")
    if (
        proof.root_key_id.startswith("test-only:")
        and os.environ.get("WORLD_V2_ENABLE_INSECURE_TEST_ROOT") != "1"
    ):
        raise ValueError("test-only deployment root is disabled")
    message = root_envelope_signature_message(
        schema_version=event.schema_version,
        world_id=event.world_id,
        event_type=event.event_type,
        event_id=event.event_id,
        actor=event.actor,
        source=event.source,
        logical_time=event.logical_time,
        created_at=event.created_at,
        trace_id=event.trace_id,
        causation_id=event.causation_id,
        correlation_id=event.correlation_id,
        idempotency_key=event.idempotency_key,
        mutation_hash=mutation_hash,
    )
    try:
        VerifyKey(bytes.fromhex(public_key)).verify(
            message, bytes.fromhex(proof.signature_hex)
        )
    except (BadSignatureError, ValueError) as exc:
        raise ValueError("deployment root signature is invalid") from exc
    return (
        hashlib.sha256(bytes.fromhex(proof.signature_hex)).hexdigest(),
        hashlib.sha256(proof.nonce.encode("utf-8")).hexdigest(),
    )


class DeploymentRootProof(FrozenModel):
    keyset_version: Literal["deployment-root-keyset.1"]
    keyset_digest: str = Field(min_length=64, max_length=64)
    root_key_id: str = Field(min_length=1)
    nonce: str = Field(min_length=16)
    signed_mutation_hash: str = Field(min_length=64, max_length=64)
    signature_hex: str = Field(min_length=128, max_length=128)


class _UnsignedDeploymentRootProof(FrozenModel):
    keyset_version: Literal["deployment-root-keyset.1"]
    keyset_digest: str = Field(min_length=64, max_length=64)
    root_key_id: str = Field(min_length=1)
    nonce: str = Field(min_length=16)


class _UnsignedActorAuthorityMutation(FrozenModel):
    world_id: str = Field(min_length=1)
    authority_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    operation: Literal["bootstrap", "rotate", "revoke", "compensate"]
    expected_entity_revision: int = Field(ge=0)
    values_before: ActorAuthorityValues | None = None
    values_after: ActorAuthorityValues
    policy_version: Literal["actor-authority-policy.1"]
    policy_digest: str = Field(min_length=64, max_length=64)
    changed_at: datetime
    compensates_transition_id: str | None = None
    root_proof: _UnsignedDeploymentRootProof


class ActorAuthorityMutationPayload(FrozenModel):
    world_id: str = Field(min_length=1)
    authority_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    operation: Literal["bootstrap", "rotate", "revoke", "compensate"]
    expected_entity_revision: int = Field(ge=0)
    values_before: ActorAuthorityValues | None = None
    values_after: ActorAuthorityValues
    policy_version: Literal["actor-authority-policy.1"]
    policy_digest: str = Field(min_length=64, max_length=64)
    changed_at: datetime
    compensates_transition_id: str | None = None
    root_proof: DeploymentRootProof

    @model_validator(mode="after")
    def mutation_shape_and_hash_are_explicit(self) -> ActorAuthorityMutationPayload:
        if self.root_proof.keyset_digest != ROOT_KEYSET_DIGEST:
            raise ValueError("deployment root keyset digest is not installed")
        if self.root_proof.signed_mutation_hash != actor_authority_mutation_hash(self):
            raise ValueError("deployment root proof hash does not match mutation")
        if self.operation == "bootstrap":
            if self.expected_entity_revision != 0 or self.values_before is not None:
                raise ValueError("actor authority bootstrap must create revision one")
        elif self.expected_entity_revision < 1 or self.values_before is None:
            raise ValueError("actor authority transition requires prior values")
        if (self.operation == "compensate") != (self.compensates_transition_id is not None):
            raise ValueError("actor authority compensation target is inconsistent")
        return self


ACTOR_AUTHORITY_PAYLOAD_MODELS = {
    "ActorAuthorityBootstrapped": ActorAuthorityMutationPayload,
    "ActorAuthorityRotated": ActorAuthorityMutationPayload,
    "ActorAuthorityRevoked": ActorAuthorityMutationPayload,
    "ActorAuthorityCompensated": ActorAuthorityMutationPayload,
}

_EVENT_OPERATIONS = {
    "ActorAuthorityBootstrapped": "bootstrap",
    "ActorAuthorityRotated": "rotate",
    "ActorAuthorityRevoked": "revoke",
    "ActorAuthorityCompensated": "compensate",
}


def validate_actor_authority_event_operation(event_type: str, operation: str) -> None:
    if _EVENT_OPERATIONS.get(event_type) != operation:
        raise ValueError("actor authority event type does not match operation")


def actor_authority_mutation_hash(
    payload: ActorAuthorityMutationPayload | Mapping[str, Any],
) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, ActorAuthorityMutationPayload)
        else to_jsonable_python(dict(payload))
    )
    proof = dict(material["root_proof"])
    proof.pop("signed_mutation_hash", None)
    proof.pop("signature_hex", None)
    material["root_proof"] = proof
    canonical = _UnsignedActorAuthorityMutation.model_validate_json(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    ).model_dump(mode="json")
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def root_envelope_signature_message(
    *,
    schema_version: str,
    world_id: str,
    event_type: str,
    event_id: str,
    actor: str,
    source: str,
    logical_time: datetime,
    created_at: datetime,
    trace_id: str,
    causation_id: str,
    correlation_id: str,
    idempotency_key: str,
    mutation_hash: str,
) -> bytes:
    material = {
        "actor": actor,
        "causation_id": causation_id,
        "correlation_id": correlation_id,
        "created_at": created_at.isoformat(),
        "event_id": event_id,
        "event_type": event_type,
        "idempotency_key": idempotency_key,
        "logical_time": logical_time.isoformat(),
        "mutation_hash": mutation_hash,
        "schema_version": schema_version,
        "source": source,
        "trace_id": trace_id,
        "world_id": world_id,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return b"girl-agent:actor-authority-envelope:v1:" + encoded
