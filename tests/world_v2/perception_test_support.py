"""Authorization fixture owned by the perception vertical tests.

Keep this deliberately local to the vertical: perception needs a distinct
capability, action, and private data scope, so it must not depend on a mutable
generic read-only-tool helper owned by another test family.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
from typing import Literal

from nacl.signing import SigningKey

from companion_daemon.world_v2.actor_authority_events import (
    ROOT_KEYSET_DIGEST,
    actor_authority_mutation_hash,
    root_envelope_signature_message,
)
from companion_daemon.world_v2.actor_authority_reducers import ACTOR_AUTHORITY_POLICY_DIGEST
from companion_daemon.world_v2.authorization_events import (
    CAPABILITY_POLICY_DIGEST,
    CONSENT_POLICY_DIGEST,
    ENFORCEMENT_EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST,
    PRIVACY_POLICY_DIGEST,
    authorization_intent_hash,
    authorization_mutation_hash,
    authorization_scope_hash,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import WorldEvent


_ROOT_KEY = SigningKey(bytes.fromhex("11" * 32))


def perception_authorized_ledger(
    monkeypatch,
    *,
    world_id: str,
    now: datetime,
    actor: str,
    subject: str,
    analysis_kind: Literal["vision", "transcription"],
) -> tuple[WorldLedger, dict[str, object]]:
    """Build exact active enforcement authority for one perception analysis."""

    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = WorldLedger.in_memory(world_id=world_id)
    ledger.commit(
        (
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:world-started:perception",
                world_id=world_id,
                event_type="WorldStarted",
                logical_time=now,
                created_at=now,
                actor="system:test",
                source="test",
                trace_id="trace:perception-auth",
                causation_id="cause:perception-auth",
                correlation_id="correlation:perception-auth",
                idempotency_key="world-started:perception",
                payload={},
            ),
        ),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    user_authority = "authority:user:perception"
    operator_authority = "authority:operator:perception"
    _commit(
        ledger,
        world_id=world_id,
        now=now,
        event_id="event:authority:user:perception",
        event_type="ActorAuthorityBootstrapped",
        payload=_actor_bootstrap(
            world_id=world_id,
            now=now,
            authority_id=user_authority,
            principal=subject,
            principal_kind="user_consent_principal",
            operations=("capability_grant", "consent_grant", "privacy_policy"),
            transition_id="transition:authority:user:perception",
        ),
    )
    _commit(
        ledger,
        world_id=world_id,
        now=now,
        event_id="event:authority:operator:perception",
        event_type="ActorAuthorityBootstrapped",
        payload=_actor_bootstrap(
            world_id=world_id,
            now=now,
            authority_id=operator_authority,
            principal="operator:test",
            principal_kind="deployment_operator",
            operations=("capability_grant",),
            transition_id="transition:authority:operator:perception",
        ),
    )
    data_scope = {
        "vision": "data:image_content",
        "transcription": "data:audio_content",
    }[analysis_kind]
    _commit_authorization(
        ledger,
        world_id=world_id,
        now=now,
        domain="capability",
        entity_id="capability:perception",
        values={
            "capability_kind": "perception_tool",
            "actor_ref": actor,
            "target_scope_refs": [f"perception:{analysis_kind}"],
            "constraint_refs": ["constraint:read-only"],
            "valid_from": now.isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
            "state": "active",
        },
        authority_id=operator_authority,
        principal="operator:test",
    )
    _commit_authorization(
        ledger,
        world_id=world_id,
        now=now,
        domain="consent",
        entity_id="consent:perception",
        values={
            "grantor_ref": subject,
            "grantee_ref": actor,
            "action_scope_refs": ["perception_tool"],
            "data_scope_refs": [data_scope],
            "channel_scope_refs": [],
            "valid_from": now.isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
            "revocable": True,
            "status": "active",
        },
        authority_id=user_authority,
        principal=subject,
    )
    _commit_authorization(
        ledger,
        world_id=world_id,
        now=now,
        domain="privacy",
        entity_id="privacy:perception",
        values={
            "subject_ref": subject,
            "data_class_refs": [data_scope],
            "viewer_rule_refs": ["viewer:companion", "viewer:platform_adapter"],
            "media_rule_refs": [],
            "retention_rule_refs": ["retention:session"],
            "effective_at": now.isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
            "status": "active",
        },
        authority_id=user_authority,
        principal=subject,
    )
    return ledger, {
        "subject_ref": subject,
        "capability_grant_id": "capability:perception",
        "capability_grant_revision": 1,
        "consent_id": "consent:perception",
        "consent_revision": 1,
        "privacy_policy_id": "privacy:perception",
        "privacy_policy_revision": 1,
    }


def _actor_bootstrap(
    *,
    world_id: str,
    now: datetime,
    authority_id: str,
    principal: str,
    principal_kind: str,
    operations: tuple[str, ...],
    transition_id: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "world_id": world_id,
        "authority_id": authority_id,
        "transition_id": transition_id,
        "operation": "bootstrap",
        "expected_entity_revision": 0,
        "values_before": None,
        "values_after": {
            "principal_ref": principal,
            "principal_kind": principal_kind,
            "credential_ref": f"credential:{principal}",
            "allowed_operations": list(operations),
            "valid_from": now.isoformat(),
            "expires_at": (now + timedelta(days=1)).isoformat(),
            "status": "active",
        },
        "policy_version": "actor-authority-policy.1",
        "policy_digest": ACTOR_AUTHORITY_POLICY_DIGEST,
        "changed_at": now.isoformat(),
        "compensates_transition_id": None,
        "root_proof": _proof(transition_id),
    }
    payload["root_proof"]["signed_mutation_hash"] = actor_authority_mutation_hash(payload)  # type: ignore[index]
    return payload


def _commit_authorization(
    ledger: WorldLedger,
    *,
    world_id: str,
    now: datetime,
    domain: Literal["capability", "consent", "privacy"],
    entity_id: str,
    values: dict[str, object],
    authority_id: str,
    principal: str,
) -> None:
    transition_id = f"transition:{domain}:perception"
    event_type = {
        "capability": "CapabilityGranted",
        "consent": "ConsentGranted",
        "privacy": "PrivacyPolicyRevised",
    }[domain]
    policy_version, policy_digest = {
        "capability": ("capability-policy.1", CAPABILITY_POLICY_DIGEST),
        "consent": ("consent-policy.1", CONSENT_POLICY_DIGEST),
        "privacy": ("privacy-policy.1", PRIVACY_POLICY_DIGEST),
    }[domain]
    payload: dict[str, object] = {
        "world_id": world_id,
        "entity_id": entity_id,
        "transition_id": transition_id,
        "operation": "revise" if domain == "privacy" else "grant",
        "expected_entity_revision": 0,
        "values_before": None,
        "values_after": values,
        "authority_id": authority_id,
        "expected_authority_revision": 1,
        "attested_principal_ref": principal,
        "attestation_mode": "root_attested_external_principal_action.1",
        "attestation_environment": "enforcement",
        "principal_action_evidence": {
            "source_event_ref": f"evidence:{transition_id}",
            "payload_hash": "a" * 64,
            "authenticated_principal_ref": principal,
            "action_ref": f"authorization:{domain}:{'revise' if domain == 'privacy' else 'grant'}",
            "scope_hash": "b" * 64,
            "intent_hash": "d" * 64,
            "challenge_ref": f"challenge:{transition_id}",
            "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "authentication_policy_version": "external-principal-auth.enforcement.1",
            "authentication_policy_digest": ENFORCEMENT_EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST,
        },
        "policy_version": policy_version,
        "policy_digest": policy_digest,
        "changed_at": now.isoformat(),
        "compensates_transition_id": None,
        "root_proof": _proof(transition_id),
    }
    evidence = payload["principal_action_evidence"]
    assert isinstance(evidence, dict)
    evidence["scope_hash"] = authorization_scope_hash(domain, values)
    evidence["intent_hash"] = authorization_intent_hash(domain, payload)
    _commit(
        ledger,
        world_id=world_id,
        now=now,
        event_id=f"event:{domain}:perception",
        event_type=event_type,
        payload=payload,
    )


def _proof(transition_id: str) -> dict[str, str]:
    return {
        "keyset_version": "deployment-root-keyset.1",
        "keyset_digest": ROOT_KEYSET_DIGEST,
        "root_key_id": "test-only:development-root-1",
        "nonce": f"nonce:{transition_id}",
        "signed_mutation_hash": "0" * 64,
        "signature_hex": "0" * 128,
    }


def _commit(
    ledger: WorldLedger,
    *,
    world_id: str,
    now: datetime,
    event_id: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    if event_type.startswith("ActorAuthority"):
        digest = actor_authority_mutation_hash(json.loads(json.dumps(payload)))
    else:
        proof = payload["root_proof"]
        assert isinstance(proof, dict)
        proof["signed_mutation_hash"] = authorization_mutation_hash(event_type, payload)
        digest = authorization_mutation_hash(event_type, json.loads(json.dumps(payload)))
    identity = domain_idempotency_key(event_type=event_type, world_id=world_id, payload=payload)
    assert identity is not None
    proof = payload["root_proof"]
    assert isinstance(proof, dict)
    proof["signature_hex"] = _ROOT_KEY.sign(
        root_envelope_signature_message(
            schema_version="world-v2.1",
            world_id=world_id,
            event_type=event_type,
            event_id=event_id,
            actor="test:root",
            source="test:root",
            logical_time=now,
            created_at=now,
            trace_id="trace:perception-auth",
            causation_id=f"cause:{event_id}",
            correlation_id="correlation:perception-auth",
            idempotency_key=identity,
            mutation_hash=digest,
        )
    ).signature.hex()
    projection = ledger.project()
    ledger.commit(
        (
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=event_id,
                world_id=world_id,
                event_type=event_type,
                logical_time=now,
                created_at=now,
                actor="test:root",
                source="test:root",
                trace_id="trace:perception-auth",
                causation_id=f"cause:{event_id}",
                correlation_id="correlation:perception-auth",
                idempotency_key=identity,
                payload=payload,
            ),
        ),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
