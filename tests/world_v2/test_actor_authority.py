from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

from nacl.signing import SigningKey
import pytest

from legacy_migration_support import legacy_state_json

import companion_daemon.world_v2.actor_authority_reducers as actor_authority_reducers
from companion_daemon.world_v2.actor_authority_events import (
    ACTOR_AUTHORITY_PAYLOAD_MODELS,
    ROOT_KEYSET_DIGEST,
    ROOT_PUBLIC_KEYS,
    actor_authority_mutation_hash,
    installed_root_keyset_digest,
    root_envelope_signature_message,
)
from companion_daemon.world_v2.actor_authority_reducers import (
    ACTOR_AUTHORITY_POLICY_DIGEST,
    ACTOR_AUTHORITY_V2_POLICY_DIGEST,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.projection import ProjectionAuthority, ProjectionGrant
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import (
    ActorAuthorityTransitionProjection,
    ActorAuthorityValues,
    LedgerProjection,
    WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
WORLD = "world-actor-authority"
ROOT_SIGNING_KEY = SigningKey(bytes.fromhex("11" * 32))


def test_actor_authority_event_family_is_declared() -> None:
    assert frozenset(ACTOR_AUTHORITY_PAYLOAD_MODELS) == {
        "ActorAuthorityBootstrapped",
        "ActorAuthorityRotated",
        "ActorAuthorityRevoked",
        "ActorAuthorityCompensated",
    }


def test_legacy_actor_authority_policy_cannot_claim_v2_domain_operation(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    after = values().model_copy(
        update={
            "allowed_operations": tuple(
                sorted((*values().allowed_operations, "v2_goal_governance"))
            )
        }
    )
    payload = signed_payload(
        operation="bootstrap",
        transition_id="transition:legacy-v2-escalation",
        expected_revision=0,
        before=None,
        after=after,
    )
    with pytest.raises(ValueError, match="operations are not installed"):
        WorldLedger.in_memory(world_id=WORLD).commit(
            [event("event:legacy-v2-escalation", "ActorAuthorityBootstrapped", payload)],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )


def test_actor_authority_policy_v2_installs_domain_operations(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    after = values().model_copy(
        update={
            "allowed_operations": tuple(
                sorted(
                    (
                        *values().allowed_operations,
                        "v2_attention_governance",
                        "v2_goal_governance",
                        "v2_location_governance",
                        "v2_resource_governance",
                    )
                )
            )
        }
    )
    payload = signed_payload(
        operation="bootstrap",
        transition_id="transition:v2-domain-authority",
        expected_revision=0,
        before=None,
        after=after,
        policy_version="actor-authority-policy.2",
        policy_digest=ACTOR_AUTHORITY_V2_POLICY_DIGEST,
    )
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit(
        [event("event:v2-domain-authority", "ActorAuthorityBootstrapped", payload)],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    authority = ledger.project().actor_authorities[0]
    assert authority.policy_version == "actor-authority-policy.2"
    assert authority.policy_digest == ACTOR_AUTHORITY_V2_POLICY_DIGEST
    assert "v2_goal_governance" in authority.values.allowed_operations


def test_actor_authority_policy_v2_rejects_wrong_digest(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    payload = signed_payload(
        operation="bootstrap",
        transition_id="transition:v2-wrong-policy-digest",
        expected_revision=0,
        before=None,
        after=values(),
        policy_version="actor-authority-policy.2",
        policy_digest="f" * 64,
    )
    with pytest.raises(ValueError, match="uninstalled policy"):
        WorldLedger.in_memory(world_id=WORLD).commit(
            [
                event(
                    "event:v2-wrong-policy-digest",
                    "ActorAuthorityBootstrapped",
                    payload,
                )
            ],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )


def test_mutation_hash_canonicalizes_equivalent_utc_spellings() -> None:
    payload = signed_payload(
        operation="bootstrap",
        transition_id="transition:canonical-time",
        expected_revision=0,
        before=None,
        after=values(),
    )
    zulu = json.loads(json.dumps(payload))
    offset = json.loads(json.dumps(payload))
    for material in (zulu, offset):
        material["root_proof"]["signed_mutation_hash"] = "0" * 64
    zulu["changed_at"] = "2026-07-14T12:00:00Z"
    zulu["values_after"]["valid_from"] = "2026-07-14T12:00:00Z"
    offset["changed_at"] = "2026-07-14T12:00:00+00:00"
    offset["values_after"]["valid_from"] = "2026-07-14T12:00:00+00:00"
    assert actor_authority_mutation_hash(zulu) == actor_authority_mutation_hash(offset)


def test_uninstalled_root_keyset_digest_fails_before_reduction() -> None:
    payload = signed_payload(
        operation="bootstrap",
        transition_id="transition:wrong-keyset",
        expected_revision=0,
        before=None,
        after=values(),
    )
    payload["root_proof"]["keyset_digest"] = "f" * 64
    with pytest.raises(ValueError, match="keyset digest is not installed"):
        ACTOR_AUTHORITY_PAYLOAD_MODELS[
            "ActorAuthorityBootstrapped"
        ].model_validate_json(
            json.dumps(payload)
        )


def test_root_keyset_artifact_and_exported_alias_are_immutable() -> None:
    original_digest = installed_root_keyset_digest()
    with pytest.raises(TypeError):
        ROOT_PUBLIC_KEYS["deployment-root:production-1"] = "f" * 64  # type: ignore[index]
    copied_alias = dict(ROOT_PUBLIC_KEYS)
    copied_alias["deployment-root:production-1"] = "f" * 64
    assert installed_root_keyset_digest() == original_digest == ROOT_KEYSET_DIGEST
    assert ROOT_PUBLIC_KEYS["deployment-root:production-1"] != "f" * 64


def test_rebinding_a_reducer_key_alias_cannot_detach_verifier_from_digest(
    monkeypatch,
) -> None:
    attacker_key = SigningKey(bytes.fromhex("33" * 32))
    monkeypatch.setattr(
        actor_authority_reducers,
        "ROOT_PUBLIC_KEYS",
        {
            "deployment-root:production-1": attacker_key.verify_key.encode().hex(),
        },
        raising=False,
    )
    payload = signed_payload(
        operation="bootstrap",
        transition_id="transition:detached-verifier",
        expected_revision=0,
        before=None,
        after=values(),
        root_key_id="deployment-root:production-1",
    )
    forged = event(
        "event:detached-verifier",
        "ActorAuthorityBootstrapped",
        payload,
        signing_key=attacker_key,
    )
    with pytest.raises(ValueError, match="deployment root signature"):
        WorldLedger.in_memory(world_id=WORLD).commit(
            [forged],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )


def values(
    *,
    principal_ref: str = "operator:geoff",
    credential_ref: str = "credential:operator:1",
    status: str = "active",
    valid_from: datetime = NOW,
    expires_at: datetime | None = NOW + timedelta(days=365),
) -> ActorAuthorityValues:
    return ActorAuthorityValues(
        principal_ref=principal_ref,
        principal_kind="deployment_operator",
        credential_ref=credential_ref,
        allowed_operations=(
            "actor_authority_rotation",
            "capability_grant",
            "consent_grant",
            "privacy_policy",
        ),
        valid_from=valid_from,
        expires_at=expires_at,
        status=status,
    )


def signed_payload(
    *,
    operation: str,
    transition_id: str,
    expected_revision: int,
    before: ActorAuthorityValues | None,
    after: ActorAuthorityValues,
    compensates: str | None = None,
    authority_id: str = "actor-authority:operator:geoff",
    world_id: str = WORLD,
    nonce: str | None = None,
    root_key_id: str = "test-only:development-root-1",
    policy_version: str = "actor-authority-policy.1",
    policy_digest: str = ACTOR_AUTHORITY_POLICY_DIGEST,
) -> dict[str, object]:
    raw: dict[str, object] = {
        "world_id": world_id,
        "authority_id": authority_id,
        "transition_id": transition_id,
        "operation": operation,
        "expected_entity_revision": expected_revision,
        "values_before": before.model_dump(mode="json") if before else None,
        "values_after": after.model_dump(mode="json"),
        "policy_version": policy_version,
        "policy_digest": policy_digest,
        "changed_at": NOW.isoformat(),
        "compensates_transition_id": compensates,
        "root_proof": {
            "keyset_version": "deployment-root-keyset.1",
            "keyset_digest": ROOT_KEYSET_DIGEST,
            "root_key_id": root_key_id,
            "nonce": nonce or f"nonce:{transition_id}:1234567890",
            "signed_mutation_hash": "0" * 64,
            "signature_hex": "0" * 128,
        },
    }
    digest = actor_authority_mutation_hash(raw)
    raw["root_proof"]["signed_mutation_hash"] = digest  # type: ignore[index]
    return raw


def event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    world_id: str = WORLD,
    actor: str = "attacker-controlled-string",
    signing_key: SigningKey = ROOT_SIGNING_KEY,
) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type, world_id=world_id, payload=payload
    )
    identity = identity or event_id
    material = dict(payload)
    material["root_proof"] = dict(payload["root_proof"])
    digest = actor_authority_mutation_hash(material)
    material["root_proof"]["signature_hex"] = signing_key.sign(  # type: ignore[index]
        root_envelope_signature_message(
            schema_version="world-v2.1",
            world_id=world_id,
            event_type=event_type,
            event_id=event_id,
            actor=actor,
            source="deployment-root-ingress",
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:actor-authority",
            causation_id=f"cause:{event_id}",
            correlation_id="correlation:actor-authority",
            idempotency_key=identity,
            mutation_hash=digest,
        )
    ).signature.hex()
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor=actor,
        source="deployment-root-ingress",
        trace_id="trace:actor-authority",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:actor-authority",
        idempotency_key=identity,
        payload=material,
    )


def observation(
    *, world_id: str = WORLD, event_id: str = "event:init-time"
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:init",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:init",
        idempotency_key=event_id,
        payload={"observation_id": f"observation:{event_id}"},
    )


def seeded_ledger(*, world_id: str = WORLD) -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=world_id)
    ledger.commit(
        [observation(world_id=world_id)],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return ledger


def commit_actor(
    ledger: WorldLedger | SQLiteWorldLedger,
    event_id: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    projection = ledger.project()
    ledger.commit(
        [event(event_id, event_type, payload)],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def test_root_signature_not_event_actor_bootstraps_authority(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    payload = signed_payload(
        operation="bootstrap",
        transition_id="transition:bootstrap",
        expected_revision=0,
        before=None,
        after=values(),
    )
    ledger.commit(
        [event("event:bootstrap", "ActorAuthorityBootstrapped", payload)],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    projection = ledger.project()
    assert projection.actor_authorities[0].values.principal_ref == "operator:geoff"
    assert projection.actor_authorities[0].entity_revision == 1
    assert ledger.rebuild() == projection

    tampered_event = event("event:tampered", "ActorAuthorityBootstrapped", payload)
    tampered = tampered_event.model_copy(update={"actor": "changed-after-signing"})
    other = WorldLedger.in_memory(world_id=WORLD)
    other.commit(
        [observation(event_id="event:init-time:other")],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    with pytest.raises(ValueError, match="deployment root signature"):
        other.commit(
            [tampered],
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )


def test_actor_authority_can_bootstrap_at_genesis_without_fake_observation(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = WorldLedger.in_memory(world_id=WORLD)
    commit_actor(
        ledger,
        "event:genesis:bootstrap",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:genesis:bootstrap",
            expected_revision=0,
            before=None,
            after=values(),
        ),
    )
    projection = ledger.project()
    assert projection.world_revision == 1
    assert projection.logical_time is None
    assert projection.actor_authorities[0].entity_revision == 1
    assert ledger.rebuild() == projection


def test_test_root_is_explicitly_gated_and_bad_signature_fails(monkeypatch) -> None:
    payload = signed_payload(
        operation="bootstrap",
        transition_id="transition:gated",
        expected_revision=0,
        before=None,
        after=values(),
    )
    ledger = seeded_ledger()
    with pytest.raises(ValueError, match="test-only deployment root is disabled"):
        commit_actor(ledger, "event:gated", "ActorAuthorityBootstrapped", payload)

    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    bad = event("event:bad-signature", "ActorAuthorityBootstrapped", payload)
    raw = bad.payload()
    raw["root_proof"]["signature_hex"] = "ff" * 64
    invalid = WorldEvent.from_payload(
        **{
            **bad.model_dump(exclude={"payload_json", "payload_hash"}),
            "payload": raw,
        }
    )
    with pytest.raises(ValueError, match="deployment root signature is invalid"):
        ledger.commit(
            [invalid],
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )


@pytest.mark.parametrize(
    ("invalid_values", "message"),
    [
        (
            values(
                valid_from=NOW + timedelta(seconds=1),
                expires_at=NOW + timedelta(days=1),
            ),
            "not valid yet",
        ),
        (
            values(
                valid_from=NOW - timedelta(days=2),
                expires_at=NOW - timedelta(seconds=1),
            ),
            "is expired",
        ),
        (
            values(
                valid_from=NOW - timedelta(days=1),
                expires_at=NOW,
            ),
            "is expired",
        ),
    ],
)
def test_bootstrap_rejects_future_expired_and_expiry_boundary_authority(
    monkeypatch, invalid_values: ActorAuthorityValues, message: str
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = WorldLedger.in_memory(world_id=WORLD)
    payload = signed_payload(
        operation="bootstrap",
        transition_id=f"transition:invalid-window:{message}",
        expected_revision=0,
        before=None,
        after=invalid_values,
    )
    with pytest.raises(ValueError, match=message):
        commit_actor(
            ledger,
            f"event:invalid-window:{message}",
            "ActorAuthorityBootstrapped",
            payload,
        )


@pytest.mark.parametrize(
    "invalid_values",
    [
        values(
            credential_ref="credential:operator:future",
            valid_from=NOW + timedelta(seconds=1),
            expires_at=NOW + timedelta(days=1),
        ),
        values(
            credential_ref="credential:operator:expired",
            valid_from=NOW - timedelta(days=2),
            expires_at=NOW,
        ),
    ],
)
def test_rotation_cannot_project_inactive_values_as_active(
    monkeypatch, invalid_values: ActorAuthorityValues
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    initial = values()
    commit_actor(
        ledger,
        "event:window:bootstrap",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:window:bootstrap",
            expected_revision=0,
            before=None,
            after=initial,
        ),
    )
    with pytest.raises(ValueError, match="(not valid yet|is expired)"):
        commit_actor(
            ledger,
            f"event:window:rotate:{invalid_values.credential_ref}",
            "ActorAuthorityRotated",
            signed_payload(
                operation="rotate",
                transition_id=f"transition:window:rotate:{invalid_values.credential_ref}",
                expected_revision=1,
                before=initial,
                after=invalid_values,
            ),
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("actor", "different-actor"),
        ("source", "different-source"),
        ("trace_id", "different-trace"),
        ("event_type", "ActorAuthorityRotated"),
        ("world_id", "world:other"),
    ],
)
def test_signed_envelope_rejects_cross_world_type_and_metadata_tampering(
    monkeypatch, field: str, replacement: str
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    payload = signed_payload(
        operation="bootstrap",
        transition_id=f"transition:tamper:{field}",
        expected_revision=0,
        before=None,
        after=values(),
    )
    signed = event(
        f"event:tamper:{field}", "ActorAuthorityBootstrapped", payload
    )
    tampered = signed.model_copy(update={field: replacement})
    ledger = seeded_ledger(world_id=str(tampered.world_id))
    with pytest.raises(
        ValueError, match="(signature|event operation|another world|domain identity)"
    ):
        ledger.commit(
            [tampered],
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )


def test_identity_is_stable_and_includes_authority_revision_where_required() -> None:
    bootstrap = signed_payload(
        operation="bootstrap",
        transition_id="transition:identity:bootstrap",
        expected_revision=2,
        before=None,
        after=values(),
    )
    changed_revision = {**bootstrap, "expected_entity_revision": 99}
    assert domain_idempotency_key(
        event_type="ActorAuthorityBootstrapped", world_id=WORLD, payload=bootstrap
    ) == domain_idempotency_key(
        event_type="ActorAuthorityBootstrapped",
        world_id=WORLD,
        payload=changed_revision,
    )
    rotate = signed_payload(
        operation="rotate",
        transition_id="transition:identity:rotate",
        expected_revision=1,
        before=values(),
        after=values().model_copy(update={"expires_at": NOW + timedelta(days=730)}),
    )
    rotated_revision = {**rotate, "expected_entity_revision": 2}
    assert domain_idempotency_key(
        event_type="ActorAuthorityRotated", world_id=WORLD, payload=rotate
    ) != domain_idempotency_key(
        event_type="ActorAuthorityRotated",
        world_id=WORLD,
        payload=rotated_revision,
    )


def test_nonce_replay_duplicate_bootstrap_and_stale_cas_fail(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    initial = values()
    bootstrap = signed_payload(
        operation="bootstrap",
        transition_id="transition:bootstrap:cas",
        expected_revision=0,
        before=None,
        after=initial,
        nonce="nonce:shared:1234567890",
    )
    commit_actor(ledger, "event:bootstrap:cas", "ActorAuthorityBootstrapped", bootstrap)

    duplicate = signed_payload(
        operation="bootstrap",
        transition_id="transition:bootstrap:duplicate",
        expected_revision=0,
        before=None,
        after=initial,
    )
    with pytest.raises(ValueError, match="already bootstrapped"):
        commit_actor(
            ledger,
            "event:bootstrap:duplicate",
            "ActorAuthorityBootstrapped",
            duplicate,
        )

    rotated = initial.model_copy(update={"expires_at": NOW + timedelta(days=730)})
    replay = signed_payload(
        operation="rotate",
        transition_id="transition:nonce:replay",
        expected_revision=1,
        before=initial,
        after=rotated,
        nonce="nonce:shared:1234567890",
    )
    with pytest.raises(ValueError, match="nonce is already consumed"):
        commit_actor(ledger, "event:nonce:replay", "ActorAuthorityRotated", replay)

    stale = signed_payload(
        operation="rotate",
        transition_id="transition:stale",
        expected_revision=2,
        before=initial,
        after=rotated,
    )
    with pytest.raises(ValueError, match="stale actor authority transition"):
        commit_actor(ledger, "event:stale", "ActorAuthorityRotated", stale)


def test_rotate_revoke_lifecycle_and_revoked_authority_is_terminal(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    initial = values()
    rotated = initial.model_copy(update={"credential_ref": "credential:operator:2"})
    revoked = rotated.model_copy(update={"status": "revoked"})
    commit_actor(
        ledger,
        "event:lifecycle:bootstrap",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:lifecycle:bootstrap",
            expected_revision=0,
            before=None,
            after=initial,
        ),
    )
    commit_actor(
        ledger,
        "event:lifecycle:rotate",
        "ActorAuthorityRotated",
        signed_payload(
            operation="rotate",
            transition_id="transition:lifecycle:rotate",
            expected_revision=1,
            before=initial,
            after=rotated,
        ),
    )
    commit_actor(
        ledger,
        "event:lifecycle:revoke",
        "ActorAuthorityRevoked",
        signed_payload(
            operation="revoke",
            transition_id="transition:lifecycle:revoke",
            expected_revision=2,
            before=rotated,
            after=revoked,
        ),
    )
    assert ledger.project().actor_authorities[0].values.status == "revoked"
    with pytest.raises(ValueError, match="revoked actor authority cannot transition"):
        commit_actor(
            ledger,
            "event:lifecycle:after-revoke",
            "ActorAuthorityRotated",
            signed_payload(
                operation="rotate",
                transition_id="transition:lifecycle:after-revoke",
                expected_revision=3,
                before=revoked,
                after=rotated,
            ),
        )


def test_credential_lineage_is_globally_unique_even_after_revoke(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    original = values()
    commit_actor(
        ledger,
        "event:credential:bootstrap-a",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:credential:bootstrap-a",
            expected_revision=0,
            before=None,
            after=original,
        ),
    )
    commit_actor(
        ledger,
        "event:credential:revoke-a",
        "ActorAuthorityRevoked",
        signed_payload(
            operation="revoke",
            transition_id="transition:credential:revoke-a",
            expected_revision=1,
            before=original,
            after=original.model_copy(update={"status": "revoked"}),
        ),
    )
    other = values(principal_ref="operator:other")
    with pytest.raises(ValueError, match="credential lineage is already assigned"):
        commit_actor(
            ledger,
            "event:credential:bootstrap-b",
            "ActorAuthorityBootstrapped",
            signed_payload(
                operation="bootstrap",
                transition_id="transition:credential:bootstrap-b",
                expected_revision=0,
                before=None,
                after=other,
                authority_id="actor-authority:operator:other",
            ),
        )


def test_compensation_uses_same_authority_latest_and_never_restores_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    a0 = values()
    a1 = a0.model_copy(update={"expires_at": NOW + timedelta(days=730)})
    commit_actor(
        ledger,
        "event:comp:a-bootstrap",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:comp:a-bootstrap",
            expected_revision=0,
            before=None,
            after=a0,
        ),
    )
    commit_actor(
        ledger,
        "event:comp:a-rotate",
        "ActorAuthorityRotated",
        signed_payload(
            operation="rotate",
            transition_id="transition:comp:a-rotate",
            expected_revision=1,
            before=a0,
            after=a1,
        ),
    )
    b0 = values(
        principal_ref="operator:other", credential_ref="credential:operator:other"
    )
    commit_actor(
        ledger,
        "event:comp:b-bootstrap",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:comp:b-bootstrap",
            expected_revision=0,
            before=None,
            after=b0,
            authority_id="actor-authority:operator:other",
        ),
    )
    commit_actor(
        ledger,
        "event:comp:a-compensate",
        "ActorAuthorityCompensated",
        signed_payload(
            operation="compensate",
            transition_id="transition:comp:a-compensate",
            expected_revision=2,
            before=a1,
            after=a0,
            compensates="transition:comp:a-rotate",
        ),
    )
    assert ledger.project().actor_authorities[0].values == a0

    cross = signed_payload(
        operation="compensate",
        transition_id="transition:comp:cross",
        expected_revision=1,
        before=b0,
        after=b0,
        compensates="transition:comp:a-compensate",
        authority_id="actor-authority:operator:other",
    )
    with pytest.raises(ValueError, match="belongs to another authority"):
        commit_actor(ledger, "event:comp:cross", "ActorAuthorityCompensated", cross)

    credential_ledger = seeded_ledger()
    changed_credential = a0.model_copy(
        update={"credential_ref": "credential:operator:new"}
    )
    commit_actor(
        credential_ledger,
        "event:credential-comp:bootstrap",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:credential-comp:bootstrap",
            expected_revision=0,
            before=None,
            after=a0,
        ),
    )
    commit_actor(
        credential_ledger,
        "event:credential-comp:rotate",
        "ActorAuthorityRotated",
        signed_payload(
            operation="rotate",
            transition_id="transition:credential-comp:rotate",
            expected_revision=1,
            before=a0,
            after=changed_credential,
        ),
    )
    with pytest.raises(ValueError, match="cannot restore an old credential"):
        commit_actor(
            credential_ledger,
            "event:credential-comp:compensate",
            "ActorAuthorityCompensated",
            signed_payload(
                operation="compensate",
                transition_id="transition:credential-comp:compensate",
                expected_revision=2,
                before=changed_credential,
                after=a0,
                compensates="transition:credential-comp:rotate",
            ),
        )


def test_actor_authority_has_no_projection_grant_or_action_side_effect(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    grant = ProjectionGrant(
        world_id=WORLD,
        viewer_id="adapter:test",
        viewer_kind="platform_adapter",
        permissions=frozenset({"projection:actions:status"}),
        redaction_policy="platform-v1",
        action_targets=frozenset({"action:test"}),
    )
    projection_authority = ProjectionAuthority(grants=(grant,), signing_key=b"x" * 32)
    before = ledger.project()
    commit_actor(
        ledger,
        "event:no-side-effect",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:no-side-effect",
            expected_revision=0,
            before=None,
            after=values(),
        ),
    )
    after = ledger.project()
    assert before.actions == after.actions == ()
    assert before.pending_actions == after.pending_actions == ()
    assert projection_authority is not None
    assert all(not isinstance(item, ProjectionGrant) for item in after.actor_authorities)


def test_event_family_and_signed_operation_must_agree(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    payload = signed_payload(
        operation="bootstrap",
        transition_id="transition:operation-mismatch",
        expected_revision=0,
        before=None,
        after=values(),
    )
    mismatch = event(
        "event:operation-mismatch", "ActorAuthorityRotated", payload
    )
    with pytest.raises(ValueError, match="event type does not match operation"):
        ledger.commit(
            [mismatch],
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )


def test_actor_authority_state_rejects_broken_lineage_and_nonce_index(
    monkeypatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    commit_actor(
        ledger,
        "event:state-invariant",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:state-invariant",
            expected_revision=0,
            before=None,
            after=values(),
        ),
    )
    projection = ledger.project()
    state = ReducerState(
        actor_authorities=projection.actor_authorities,
        actor_authority_transitions=projection.actor_authority_transitions,
        consumed_actor_root_nonces=projection.consumed_actor_root_nonces,
        committed_world_event_refs=projection.committed_world_event_refs,
    )
    raw = state.model_dump(mode="json")
    raw["consumed_actor_root_nonces"] = []
    with pytest.raises(ValueError, match="consume one root nonce"):
        ReducerState.model_validate_json(json.dumps(raw))

    raw = state.model_dump(mode="json")
    raw["actor_authorities"][0]["entity_revision"] = 2
    with pytest.raises(ValueError, match="does not match lineage head"):
        ReducerState.model_validate_json(json.dumps(raw))


def test_sqlite_actor_authority_survives_restart_rebuild_and_detects_tamper(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    path = tmp_path / "actor-authority.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit(
        [observation()],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    commit_actor(
        ledger,
        "event:sqlite:bootstrap",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:sqlite:bootstrap",
            expected_revision=0,
            before=None,
            after=values(),
        ),
    )
    expected = ledger.project()
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()

    with sqlite3.connect(path) as connection:
        raw = json.loads(
            connection.execute(
                "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
            ).fetchone()[0]
        )
        raw["actor_authorities"][0]["values"]["principal_ref"] = "operator:tampered"
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ? WHERE world_id = ?",
            (json.dumps(raw, separators=(",", ":")), WORLD),
        )
    with pytest.raises(LedgerIntegrityError, match="head state( hash)? is invalid"):
        SQLiteWorldLedger(path=path, world_id=WORLD)


def test_actor_transition_binding_is_v16_only_and_legacy_injection_is_rejected(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    commit_actor(
        ledger,
        "event:binding:bootstrap",
        "ActorAuthorityBootstrapped",
        signed_payload(
            operation="bootstrap",
            transition_id="transition:binding:bootstrap",
            expected_revision=0,
            before=None,
            after=values(),
        ),
    )
    projection = ledger.project()
    state = SQLiteWorldLedger._state_from_projection(projection)
    legacy = state.semantic_payload(
        world_id=WORLD,
        world_revision=projection.world_revision,
        reducer_bundle_version="world-v2-reducers.15",
    )
    current = state.semantic_payload(
        world_id=WORLD,
        world_revision=projection.world_revision,
        reducer_bundle_version="world-v2-reducers.16",
    )
    assert set(current["actor_authority_transitions"][0]) - set(
        legacy["actor_authority_transitions"][0]
    ) == {
        "accepted_event_ref",
        "accepted_world_revision",
        "accepted_payload_hash",
    }
    legacy_hash = hashlib.sha256(
        json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert legacy_hash == "d2eb4ff96bd2e1a6395c4b9edcc5a2f031b49671038540411b1de96c909574b5"
    missing = projection.actor_authority_transitions[0].model_dump(mode="python")
    missing.pop("accepted_event_ref")
    missing.pop("accepted_world_revision")
    missing.pop("accepted_payload_hash")
    with pytest.raises(ValueError, match="requires exact accepted event binding"):
        ActorAuthorityTransitionProjection.model_validate(missing)

    sqlite = SQLiteWorldLedger(path=tmp_path / "legacy-binding.sqlite3", world_id=WORLD)
    with pytest.raises(LedgerIntegrityError, match="legacy head state is invalid"):
        sqlite._legacy_semantic_hash(
            state_json=json.dumps(state.model_dump(mode="json")),
            world_revision=projection.world_revision,
            reducer_bundle_version="world-v2-reducers.15",
        )
    sqlite.close()


def test_same_tick_actor_events_cannot_swap_transition_bindings(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = seeded_ledger()
    for suffix in ("a", "b"):
        commit_actor(
            ledger,
            f"event:binding:{suffix}",
            "ActorAuthorityBootstrapped",
            signed_payload(
                operation="bootstrap",
                transition_id=f"transition:binding:{suffix}",
                expected_revision=0,
                before=None,
                after=values(
                    principal_ref=f"operator:{suffix}",
                    credential_ref=f"credential:{suffix}",
                ),
                authority_id=f"actor-authority:{suffix}",
                nonce=f"nonce:binding:{suffix}:1234567890",
            ),
        )
    raw = ledger.project().model_dump(mode="json")
    first, second = raw["actor_authority_transitions"]
    fields = (
        "accepted_event_ref",
        "accepted_world_revision",
        "accepted_payload_hash",
    )
    first_binding = {field: first[field] for field in fields}
    second_binding = {field: second[field] for field in fields}
    first.update(second_binding)
    second.update(first_binding)
    with pytest.raises(
        ValueError, match="accepted event binding|head origin|event revisions must be canonical"
    ):
        LedgerProjection.model_validate_json(json.dumps(raw))


def test_sqlite_migrates_verified_v7_head_to_actor_authority_bundle(tmp_path) -> None:
    path = tmp_path / "actor-authority-v7.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit(
        [observation()],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        raw_state = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0]
        state = ReducerState.model_validate_json(raw_state)
        semantic = state.semantic_payload(
            world_id=WORLD,
            world_revision=1,
            reducer_bundle_version="world-v2-reducers.7",
        )
        semantic.pop("actor_authorities")
        semantic.pop("actor_authority_transitions")
        semantic.pop("consumed_actor_root_nonces")
        for key in (
            "capability_grants", "capability_transitions", "consent_grants",
            "consent_transitions", "privacy_policies", "privacy_transitions",
            "consumed_authorization_root_nonces", "consumed_authorization_challenge_ids",
            "consumed_authorization_source_ids",
        ):
            semantic.pop(key)
        legacy_hash = hashlib.sha256(
            json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ? "
            "WHERE world_id = ?",
            (legacy_state_json(raw_state), legacy_hash, "world-v2-reducers.7", WORLD),
        )

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project().reducer_bundle_version == "world-v2-reducers.22"
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_sqlite_rejects_tampered_v7_head_during_actor_bundle_migration(
    tmp_path,
) -> None:
    path = tmp_path / "actor-authority-v7-tampered.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit(
        [observation()],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.close()
    with sqlite3.connect(path) as connection:
        raw_state = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0]
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ? "
            "WHERE world_id = ?",
            (legacy_state_json(raw_state), "0" * 64, "world-v2-reducers.7", WORLD),
        )
    with pytest.raises(LedgerIntegrityError, match="legacy head semantic hash is invalid"):
        SQLiteWorldLedger(path=path, world_id=WORLD)
