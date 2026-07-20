"""Small, explicit factory for enforcement-grade authorization test ledgers."""

from __future__ import annotations

from datetime import datetime, timedelta
import json

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


# Matches the deliberately installed insecure development root used by the
# World v2 authorization contract tests.
ROOT_KEY = SigningKey(bytes.fromhex("11" * 32))


def enforcement_tool_ledger(
    monkeypatch,
    *,
    world_id: str,
    now: datetime,
    actor: str,
    subject: str,
    target: str = "tool:weather",
    capability_kind: str = "read_only_tool",
    action_scope: str = "read_only_tool",
    data_scope: str = "data:location",
) -> tuple[WorldLedger, dict[str, object]]:
    """Build a replayable ledger with exact active tool authorization sources."""

    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    user_authority = "authority:user:test"
    operator_authority = "authority:operator:test"
    ledger = WorldLedger.in_memory(world_id=world_id)
    _commit(
        ledger,
        world_id,
        now,
        "event:authority:user",
        "ActorAuthorityBootstrapped",
        _actor_bootstrap(
            world_id,
            now,
            user_authority,
            subject,
            "user_consent_principal",
            ("capability_grant", "consent_grant", "privacy_policy"),
            "transition:authority:user",
        ),
    )
    _commit(
        ledger,
        world_id,
        now,
        "event:authority:operator",
        "ActorAuthorityBootstrapped",
        _actor_bootstrap(
            world_id,
            now,
            operator_authority,
            "operator:test",
            "deployment_operator",
            ("capability_grant",),
            "transition:authority:operator",
        ),
    )
    values = (
        (
            "capability",
            "capability:tool",
            {
                "capability_kind": capability_kind,
                "actor_ref": actor,
                "target_scope_refs": [target],
                "constraint_refs": ["constraint:read-only"],
                "valid_from": now.isoformat(),
                "expires_at": (now + timedelta(days=1)).isoformat(),
                "state": "active",
            },
            operator_authority,
            "operator:test",
        ),
        (
            "consent",
            "consent:tool",
            {
                "grantor_ref": subject,
                "grantee_ref": actor,
                "action_scope_refs": [action_scope],
                "data_scope_refs": [data_scope],
                "channel_scope_refs": [],
                "valid_from": now.isoformat(),
                "expires_at": (now + timedelta(days=1)).isoformat(),
                "revocable": True,
                "status": "active",
            },
            user_authority,
            subject,
        ),
        (
            "privacy",
            "privacy:tool",
            {
                "subject_ref": subject,
                "data_class_refs": [data_scope],
                "viewer_rule_refs": ["viewer:companion", "viewer:platform_adapter"],
                "media_rule_refs": [],
                "retention_rule_refs": ["retention:session"],
                "effective_at": now.isoformat(),
                "expires_at": (now + timedelta(days=1)).isoformat(),
                "status": "active",
            },
            user_authority,
            subject,
        ),
    )
    for domain, entity_id, after, authority_id, principal in values:
        event_type = {
            "capability": "CapabilityGranted",
            "consent": "ConsentGranted",
            "privacy": "PrivacyPolicyRevised",
        }[domain]
        transition = f"transition:{domain}:tool"
        _commit(
            ledger,
            world_id,
            now,
            f"event:{domain}:tool",
            event_type,
            _authorization_mutation(
                world_id,
                now,
                domain,
                entity_id,
                after,
                authority_id,
                principal,
                transition,
            ),
        )
    return ledger, {
        "subject_ref": subject,
        "capability_grant_id": "capability:tool",
        "capability_grant_revision": 1,
        "consent_id": "consent:tool",
        "consent_revision": 1,
        "privacy_policy_id": "privacy:tool",
        "privacy_policy_revision": 1,
    }


def _actor_bootstrap(world_id, now, authority_id, principal, kind, operations, transition):
    payload = {
        "world_id": world_id,
        "authority_id": authority_id,
        "transition_id": transition,
        "operation": "bootstrap",
        "expected_entity_revision": 0,
        "values_before": None,
        "values_after": {
            "principal_ref": principal,
            "principal_kind": kind,
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
        "root_proof": _proof(transition),
    }
    payload["root_proof"]["signed_mutation_hash"] = actor_authority_mutation_hash(payload)
    return payload


def _authorization_mutation(
    world_id, now, domain, entity_id, after, authority_id, principal, transition
):
    policy = {
        "capability": ("capability-policy.1", CAPABILITY_POLICY_DIGEST),
        "consent": ("consent-policy.1", CONSENT_POLICY_DIGEST),
        "privacy": ("privacy-policy.1", PRIVACY_POLICY_DIGEST),
    }[domain]
    payload = {
        "world_id": world_id,
        "entity_id": entity_id,
        "transition_id": transition,
        "operation": "grant" if domain != "privacy" else "revise",
        "expected_entity_revision": 0,
        "values_before": None,
        "values_after": after,
        "authority_id": authority_id,
        "expected_authority_revision": 1,
        "attested_principal_ref": principal,
        "attestation_mode": "root_attested_external_principal_action.1",
        "attestation_environment": "enforcement",
        "principal_action_evidence": {
            "source_event_ref": f"evidence:{transition}",
            "payload_hash": "a" * 64,
            "authenticated_principal_ref": principal,
            "action_ref": f"authorization:{domain}:{'grant' if domain != 'privacy' else 'revise'}",
            "scope_hash": "b" * 64,
            "intent_hash": "d" * 64,
            "challenge_ref": f"challenge:{transition}",
            "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
            "authentication_policy_version": "external-principal-auth.enforcement.1",
            "authentication_policy_digest": ENFORCEMENT_EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST,
        },
        "policy_version": policy[0],
        "policy_digest": policy[1],
        "changed_at": now.isoformat(),
        "compensates_transition_id": None,
        "root_proof": _proof(transition),
    }
    payload["principal_action_evidence"]["scope_hash"] = authorization_scope_hash(domain, after)
    payload["principal_action_evidence"]["intent_hash"] = authorization_intent_hash(domain, payload)
    return payload


def _proof(transition):
    return {
        "keyset_version": "deployment-root-keyset.1",
        "keyset_digest": ROOT_KEYSET_DIGEST,
        "root_key_id": "test-only:development-root-1",
        "nonce": f"nonce:{transition}",
        "signed_mutation_hash": "0" * 64,
        "signature_hex": "0" * 128,
    }


def _commit(ledger, world_id, now, event_id, event_type, payload):
    if event_type.startswith("ActorAuthority"):
        digest = actor_authority_mutation_hash(json.loads(json.dumps(payload)))
    else:
        payload["root_proof"]["signed_mutation_hash"] = authorization_mutation_hash(
            event_type, payload
        )
        digest = authorization_mutation_hash(event_type, json.loads(json.dumps(payload)))
    identity = domain_idempotency_key(event_type=event_type, world_id=world_id, payload=payload)
    assert identity is not None
    payload["root_proof"]["signature_hex"] = ROOT_KEY.sign(
        root_envelope_signature_message(
            schema_version="world-v2.1",
            world_id=world_id,
            event_type=event_type,
            event_id=event_id,
            actor="test:root",
            source="test:root",
            logical_time=now,
            created_at=now,
            trace_id="trace:auth",
            causation_id=f"cause:{event_id}",
            correlation_id="correlation:auth",
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
                trace_id="trace:auth",
                causation_id=f"cause:{event_id}",
                correlation_id="correlation:auth",
                idempotency_key=identity,
                payload=payload,
            ),
        ),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
