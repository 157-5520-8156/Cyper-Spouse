from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

from nacl.signing import SigningKey
import pytest

from companion_daemon.world_v2.actor_authority_events import (
    ROOT_KEYSET_DIGEST,
    actor_authority_mutation_hash,
    root_envelope_signature_message,
)
from companion_daemon.world_v2.actor_authority_reducers import (
    ACTOR_AUTHORITY_POLICY_DIGEST,
)
from companion_daemon.world_v2.authorization_events import (
    AUTHORIZATION_PAYLOAD_MODELS,
    CAPABILITY_POLICY_DIGEST,
    CONSENT_POLICY_DIGEST,
    EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST,
    PRIVACY_POLICY_DIGEST,
    authorization_mutation_hash,
    authorization_intent_hash,
    authorization_scope_hash,
)
from companion_daemon.world_v2.authorization_shadow import (
    ShadowAuthorizationRequest,
    evaluate_authorization_shadow,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.projection import ProjectionGrant
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
WORLD = "world-domain-authorization"
ROOT_KEY = SigningKey(bytes.fromhex("11" * 32))
AUTHORITY_ID = "actor-authority:user:geoff"
PRINCIPAL = "user:geoff"
OPERATOR_AUTHORITY_ID = "actor-authority:operator:geoff"
OPERATOR_PRINCIPAL = "operator:geoff"
ACTION_ACTOR = "companion:zhizhi"


def _sign_event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type, world_id=WORLD, payload=payload
    )
    assert identity is not None
    material = json.loads(json.dumps(payload))
    digest = (
        actor_authority_mutation_hash(material)
        if event_type.startswith("ActorAuthority")
        else authorization_mutation_hash(event_type, material)
    )
    material["root_proof"]["signature_hex"] = ROOT_KEY.sign(
        root_envelope_signature_message(
            schema_version="world-v2.1",
            world_id=WORLD,
            event_type=event_type,
            event_id=event_id,
            actor="untrusted-envelope-actor",
            source="deployment-root-ingress",
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:authorization",
            causation_id=f"cause:{event_id}",
            correlation_id="correlation:authorization",
            idempotency_key=identity,
            mutation_hash=digest,
        )
    ).signature.hex()
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="untrusted-envelope-actor",
        source="deployment-root-ingress",
        trace_id="trace:authorization",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:authorization",
        idempotency_key=identity,
        payload=material,
    )


def _proof(transition_id: str) -> dict[str, object]:
    return {
        "keyset_version": "deployment-root-keyset.1",
        "keyset_digest": ROOT_KEYSET_DIGEST,
        "root_key_id": "test-only:development-root-1",
        "nonce": f"authorization-nonce:{transition_id}",
        "signed_mutation_hash": "0" * 64,
        "signature_hex": "0" * 128,
    }


def _actor_bootstrap_payload(
    *,
    authority_id: str = AUTHORITY_ID,
    principal_ref: str = PRINCIPAL,
    principal_kind: str = "user_consent_principal",
    credential_ref: str = "credential:user:opaque",
    allowed_operations: list[str] | None = None,
    transition_id: str = "transition:actor-bootstrap",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "world_id": WORLD,
        "authority_id": authority_id,
        "transition_id": transition_id,
        "operation": "bootstrap",
        "expected_entity_revision": 0,
        "values_before": None,
        "values_after": {
            "principal_ref": principal_ref,
            "principal_kind": principal_kind,
            "credential_ref": credential_ref,
            "allowed_operations": allowed_operations
            or ["capability_grant", "consent_grant", "privacy_policy"],
            "valid_from": NOW.isoformat(),
            "expires_at": (NOW + timedelta(days=365)).isoformat(),
            "status": "active",
        },
        "policy_version": "actor-authority-policy.1",
        "policy_digest": ACTOR_AUTHORITY_POLICY_DIGEST,
        "changed_at": NOW.isoformat(),
        "compensates_transition_id": None,
        "root_proof": _proof(transition_id),
    }
    payload["root_proof"]["signed_mutation_hash"] = actor_authority_mutation_hash(  # type: ignore[index]
        payload
    )
    return payload


def _operator_revoke_payload() -> dict[str, object]:
    before = _actor_bootstrap_payload(
        authority_id=OPERATOR_AUTHORITY_ID,
        principal_ref=OPERATOR_PRINCIPAL,
        principal_kind="deployment_operator",
        credential_ref="credential:operator:opaque",
        allowed_operations=["capability_grant"],
        transition_id="transition:unused-bootstrap-shape",
    )["values_after"]
    transition_id = "transition:operator:revoke"
    after = {**before, "status": "revoked"}  # type: ignore[arg-type]
    payload: dict[str, object] = {
        "world_id": WORLD,
        "authority_id": OPERATOR_AUTHORITY_ID,
        "transition_id": transition_id,
        "operation": "revoke",
        "expected_entity_revision": 1,
        "values_before": before,
        "values_after": after,
        "policy_version": "actor-authority-policy.1",
        "policy_digest": ACTOR_AUTHORITY_POLICY_DIGEST,
        "changed_at": NOW.isoformat(),
        "compensates_transition_id": None,
        "root_proof": _proof(transition_id),
    }
    payload["root_proof"]["signed_mutation_hash"] = actor_authority_mutation_hash(  # type: ignore[index]
        payload
    )
    return payload


def _mutation(
    *,
    domain: str,
    operation: str,
    transition_id: str,
    entity_id: str,
    expected_revision: int,
    before: dict[str, object] | None,
    after: dict[str, object],
    compensates: str | None = None,
    authority_revision: int = 1,
    attested_principal: str | None = None,
    nonce: str | None = None,
    evidence_source_event_ref: str | None = None,
    evidence_payload_hash: str = "a" * 64,
    challenge_ref: str | None = None,
    evidence_expires_at: datetime | None = None,
) -> dict[str, object]:
    policy = {
        "capability": ("capability-policy.1", CAPABILITY_POLICY_DIGEST),
        "consent": ("consent-policy.1", CONSENT_POLICY_DIGEST),
        "privacy": ("privacy-policy.1", PRIVACY_POLICY_DIGEST),
    }[domain]
    authority_id = OPERATOR_AUTHORITY_ID if domain == "capability" else AUTHORITY_ID
    attested_principal = attested_principal or (
        OPERATOR_PRINCIPAL if domain == "capability" else PRINCIPAL
    )
    proof = _proof(transition_id)
    if nonce is not None:
        proof["nonce"] = nonce
    payload: dict[str, object] = {
        "world_id": WORLD,
        "entity_id": entity_id,
        "transition_id": transition_id,
        "operation": operation,
        "expected_entity_revision": expected_revision,
        "values_before": before,
        "values_after": after,
        "authority_id": authority_id,
        "expected_authority_revision": authority_revision,
        "attested_principal_ref": attested_principal,
        "attestation_mode": "root_attested_external_principal_action.1",
        "attestation_environment": "shadow",
        "principal_action_evidence": {
            "source_event_ref": evidence_source_event_ref
            or f"external-authentication:{transition_id}",
            "payload_hash": evidence_payload_hash,
            "authenticated_principal_ref": attested_principal,
            "action_ref": f"authorization:{domain}:{operation}",
            "scope_hash": "b" * 64,
            "intent_hash": "d" * 64,
            "challenge_ref": challenge_ref or f"challenge:{transition_id}:123456",
            "observed_at": NOW.isoformat(),
            "expires_at": (evidence_expires_at or NOW + timedelta(minutes=5)).isoformat(),
            "authentication_policy_version": "external-principal-auth.1",
            "authentication_policy_digest": EXTERNAL_PRINCIPAL_AUTH_POLICY_DIGEST,
        },
        "policy_version": policy[0],
        "policy_digest": policy[1],
        "changed_at": NOW.isoformat(),
        "compensates_transition_id": compensates,
        "root_proof": proof,
    }
    payload["principal_action_evidence"]["scope_hash"] = authorization_scope_hash(  # type: ignore[index]
        domain, after
    )
    payload["principal_action_evidence"]["intent_hash"] = authorization_intent_hash(  # type: ignore[index]
        domain, payload
    )
    return payload


def _capability_values(
    *,
    state: str = "active",
    targets: list[str] | None = None,
    constraints: list[str] | None = None,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "capability_kind": "message_send",
        "actor_ref": ACTION_ACTOR,
        "target_scope_refs": targets or ["channel:qq"],
        "constraint_refs": constraints if constraints is not None else ["constraint:text-only"],
        "valid_from": NOW.isoformat(),
        "expires_at": (expires_at or NOW + timedelta(days=30)).isoformat(),
        "state": state,
    }


def _consent_values(
    *,
    status: str = "active",
    grantor: str = PRINCIPAL,
    grantee: str = ACTION_ACTOR,
    revocable: bool = True,
    expires_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "grantor_ref": grantor,
        "grantee_ref": grantee,
        "action_scope_refs": ["message_send"],
        "data_scope_refs": ["data:message_content"],
        "channel_scope_refs": ["channel:qq"],
        "valid_from": NOW.isoformat(),
        "expires_at": (expires_at or NOW + timedelta(days=30)).isoformat(),
        "revocable": revocable,
        "status": status,
    }


def _privacy_values(
    *, status: str = "active", subject: str = PRINCIPAL
) -> dict[str, object]:
    return {
        "subject_ref": subject,
        "data_class_refs": ["data:message_content"],
        "viewer_rule_refs": ["viewer:companion", "viewer:platform_adapter"],
        "media_rule_refs": ["media:private_only"],
        "retention_rule_refs": ["retention:30d"],
        "effective_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(days=30)).isoformat(),
        "status": status,
    }


def _ledger(monkeypatch) -> WorldLedger:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit(
        [
            _sign_event(
                "event:actor-bootstrap:user",
                "ActorAuthorityBootstrapped",
                _actor_bootstrap_payload(),
            ),
            _sign_event(
                "event:actor-bootstrap:operator",
                "ActorAuthorityBootstrapped",
                _actor_bootstrap_payload(
                    authority_id=OPERATOR_AUTHORITY_ID,
                    principal_ref=OPERATOR_PRINCIPAL,
                    principal_kind="deployment_operator",
                    credential_ref="credential:operator:opaque",
                    allowed_operations=["capability_grant"],
                    transition_id="transition:actor-bootstrap:operator",
                ),
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return ledger


def _commit(ledger: WorldLedger, event_id: str, event_type: str, payload: dict[str, object]) -> None:
    payload["root_proof"]["signed_mutation_hash"] = authorization_mutation_hash(  # type: ignore[index]
        event_type, payload
    )
    projection = ledger.project()
    ledger.commit(
        [_sign_event(event_id, event_type, payload)],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def _prepared_event(
    event_id: str, event_type: str, payload: dict[str, object]
) -> WorldEvent:
    payload["root_proof"]["signed_mutation_hash"] = authorization_mutation_hash(  # type: ignore[index]
        event_type, payload
    )
    return _sign_event(event_id, event_type, payload)


def test_three_authorization_domains_are_separate_typed_families() -> None:
    assert {
        "CapabilityGranted",
        "ConsentGranted",
        "PrivacyPolicyRevised",
    } <= set(AUTHORIZATION_PAYLOAD_MODELS)
    assert len({AUTHORIZATION_PAYLOAD_MODELS[name] for name in AUTHORIZATION_PAYLOAD_MODELS}) == 3


def test_root_attested_grants_pin_exact_actor_authority_and_project(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    capability = _mutation(
        domain="capability",
        operation="grant",
        transition_id="transition:capability:grant",
        entity_id="capability:message:qq",
        expected_revision=0,
        before=None,
        after=_capability_values(),
    )
    consent = _mutation(
        domain="consent",
        operation="grant",
        transition_id="transition:consent:grant",
        entity_id="consent:message:qq",
        expected_revision=0,
        before=None,
        after=_consent_values(),
    )
    privacy = _mutation(
        domain="privacy",
        operation="revise",
        transition_id="transition:privacy:revise",
        entity_id="privacy:user:geoff",
        expected_revision=0,
        before=None,
        after=_privacy_values(),
    )
    _commit(ledger, "event:capability:grant", "CapabilityGranted", capability)
    _commit(ledger, "event:consent:grant", "ConsentGranted", consent)
    _commit(ledger, "event:privacy:revise", "PrivacyPolicyRevised", privacy)
    projection = ledger.project()
    assert projection.capability_grants[0].origin.authority_id == OPERATOR_AUTHORITY_ID
    assert (
        projection.consent_grants[0].origin.attestation_mode
        == "root_attested_external_principal_action.1"
    )
    assert projection.consent_grants[0].origin.attestation_environment == "shadow"
    assert projection.consent_grants[0].origin.evidence_hash
    assert projection.privacy_policies[0].entity_revision == 1
    assert projection.actions == ()
    assert ledger.rebuild() == projection


def test_shadow_evaluator_reports_only_and_unknown_scope_fails_closed(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    for event_id, event_type, payload in (
        (
            "event:shadow:capability",
            "CapabilityGranted",
            _mutation(
                domain="capability",
                operation="grant",
                transition_id="transition:shadow:capability",
                entity_id="capability:shadow",
                expected_revision=0,
                before=None,
                after=_capability_values(),
            ),
        ),
        (
            "event:shadow:consent",
            "ConsentGranted",
            _mutation(
                domain="consent",
                operation="grant",
                transition_id="transition:shadow:consent",
                entity_id="consent:shadow",
                expected_revision=0,
                before=None,
                after=_consent_values(),
            ),
        ),
        (
            "event:shadow:privacy",
            "PrivacyPolicyRevised",
            _mutation(
                domain="privacy",
                operation="revise",
                transition_id="transition:shadow:privacy",
                entity_id="privacy:shadow",
                expected_revision=0,
                before=None,
                after=_privacy_values(),
            ),
        ),
    ):
        _commit(ledger, event_id, event_type, payload)
    before = ledger.project()
    allowed = evaluate_authorization_shadow(
        before,
        ShadowAuthorizationRequest(
            action_actor_ref=ACTION_ACTOR,
            data_subject_ref=PRINCIPAL,
            capability_kind="message_send",
            action_content_type="text",
            effect_class="external_message",
            third_party_target=False,
            target_scope_refs=("channel:qq",),
            action_scope_refs=("message_send",),
            data_scope_refs=("data:message_content",),
            channel_scope_refs=("channel:qq",),
            viewer_rule_refs=("viewer:companion", "viewer:platform_adapter"),
            media_rule_refs=("media:private_only",),
            retention_rule_refs=("retention:30d",),
            logical_time=NOW,
        ),
    )
    assert allowed.would_allow is True
    assert allowed.attestation_modes == (
        "root_attested_external_principal_action.1",
    )
    assert allowed.enforcement_eligible is False
    denied = evaluate_authorization_shadow(
        before,
        allowed.request.model_copy(update={"target_scope_refs": ("channel:unknown",)}),
    )
    assert denied.would_allow is False
    assert "unknown_scope" in denied.reason_codes
    underreported = evaluate_authorization_shadow(
        before,
        allowed.request.model_copy(
            update={
                "data_scope_refs": (),
                "channel_scope_refs": (),
                "viewer_rule_refs": (),
                "retention_rule_refs": (),
            }
        ),
    )
    assert underreported.would_allow is False
    assert "mandatory_scope_missing" in underreported.reason_codes
    missing_adapter = evaluate_authorization_shadow(
        before,
        allowed.request.model_copy(
            update={"viewer_rule_refs": ("viewer:companion",)}
        ),
    )
    assert missing_adapter.would_allow is False
    assert "mandatory_scope_missing" in missing_adapter.reason_codes
    assert ledger.project() == before


def test_consent_grantor_must_equal_pinned_user_consent_principal(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    invalid = _consent_values()
    invalid["grantor_ref"] = "user:someone-else"
    payload = _mutation(
        domain="consent",
        operation="grant",
        transition_id="transition:consent:wrong-grantor",
        entity_id="consent:wrong-grantor",
        expected_revision=0,
        before=None,
        after=invalid,
    )
    with pytest.raises(ValueError, match="grantor.*principal"):
        _commit(ledger, "event:consent:wrong-grantor", "ConsentGranted", payload)


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda payload: payload.update(expected_authority_revision=2), "revision is stale"),
        (
            lambda payload: payload.update(authority_id=AUTHORITY_ID),
            "operator principal",
        ),
    ],
)
def test_capability_requires_exact_operator_authority(
    monkeypatch, mutator, message: str
) -> None:
    ledger = _ledger(monkeypatch)
    payload = _mutation(
        domain="capability",
        operation="grant",
        transition_id=f"transition:capability:authority:{message}",
        entity_id=f"capability:authority:{message}",
        expected_revision=0,
        before=None,
        after=_capability_values(),
    )
    mutator(payload)
    evidence = payload["principal_action_evidence"]
    evidence["authenticated_principal_ref"] = (  # type: ignore[index]
        PRINCIPAL if payload["authority_id"] == AUTHORITY_ID else OPERATOR_PRINCIPAL
    )
    payload["attested_principal_ref"] = evidence["authenticated_principal_ref"]  # type: ignore[index]
    evidence["intent_hash"] = authorization_intent_hash("capability", payload)  # type: ignore[index]
    with pytest.raises(ValueError, match=message):
        _commit(
            ledger,
            f"event:capability:authority:{message}",
            "CapabilityGranted",
            payload,
        )


def test_stable_evidence_identity_blocks_cross_entity_replay(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    common = {
        "evidence_source_event_ref": "external-authentication:shared",
        "evidence_payload_hash": "e" * 64,
        "challenge_ref": "challenge:server-issued:shared",
    }
    first = _mutation(
        domain="capability",
        operation="grant",
        transition_id="transition:evidence:first",
        entity_id="capability:evidence:first",
        expected_revision=0,
        before=None,
        after=_capability_values(),
        **common,
    )
    _commit(ledger, "event:evidence:first", "CapabilityGranted", first)
    replay = _mutation(
        domain="capability",
        operation="grant",
        transition_id="transition:evidence:replay",
        entity_id="capability:evidence:other-entity",
        expected_revision=0,
        before=None,
        after=_capability_values(targets=["channel:http"]),
        nonce="authorization-nonce:different-root-proof",
        **common,
    )
    with pytest.raises(ValueError, match="(challenge|source) is already consumed"):
        _commit(ledger, "event:evidence:replay", "CapabilityGranted", replay)


@pytest.mark.parametrize("reuse_axis", ["challenge", "source"])
def test_evidence_challenge_and_source_are_independently_single_use(
    monkeypatch, reuse_axis: str
) -> None:
    ledger = _ledger(monkeypatch)
    source = "external-authentication:independent-source"
    payload_hash = "9" * 64
    challenge = "challenge:server-issued:independent"
    first = _mutation(
        domain="capability",
        operation="grant",
        transition_id=f"transition:evidence-independent:first:{reuse_axis}",
        entity_id=f"capability:evidence-independent:first:{reuse_axis}",
        expected_revision=0,
        before=None,
        after=_capability_values(),
        evidence_source_event_ref=source,
        evidence_payload_hash=payload_hash,
        challenge_ref=challenge,
    )
    _commit(
        ledger,
        f"event:evidence-independent:first:{reuse_axis}",
        "CapabilityGranted",
        first,
    )
    replay = _mutation(
        domain="capability",
        operation="grant",
        transition_id=f"transition:evidence-independent:replay:{reuse_axis}",
        entity_id=f"capability:evidence-independent:replay:{reuse_axis}",
        expected_revision=0,
        before=None,
        after=_capability_values(targets=["channel:http"]),
        nonce=f"authorization-nonce:independent:{reuse_axis}",
        evidence_source_event_ref=(
            "external-authentication:different-source"
            if reuse_axis == "challenge"
            else source
        ),
        evidence_payload_hash=(
            "8" * 64 if reuse_axis == "challenge" else "7" * 64
        ),
        challenge_ref=(
            challenge
            if reuse_axis == "challenge"
            else "challenge:server-issued:different"
        ),
    )
    with pytest.raises(ValueError, match=f"{reuse_axis} is already consumed"):
        _commit(
            ledger,
            f"event:evidence-independent:replay:{reuse_axis}",
            "CapabilityGranted",
            replay,
        )


def test_evidence_ttl_policy_and_full_values_are_bound(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    too_long = _mutation(
        domain="capability",
        operation="grant",
        transition_id="transition:evidence:ttl",
        entity_id="capability:evidence:ttl",
        expected_revision=0,
        before=None,
        after=_capability_values(),
        evidence_expires_at=NOW + timedelta(minutes=11),
    )
    with pytest.raises(ValueError, match="maximum ttl"):
        _commit(ledger, "event:evidence:ttl", "CapabilityGranted", too_long)

    extended = _mutation(
        domain="capability",
        operation="grant",
        transition_id="transition:evidence:extended-values",
        entity_id="capability:evidence:extended-values",
        expected_revision=0,
        before=None,
        after=_capability_values(),
    )
    extended["values_after"]["expires_at"] = (NOW + timedelta(days=365)).isoformat()  # type: ignore[index]
    extended["principal_action_evidence"]["scope_hash"] = authorization_scope_hash(  # type: ignore[index]
        "capability", extended["values_after"]
    )
    with pytest.raises(ValueError, match="intent hash"):
        _commit(
            ledger,
            "event:evidence:extended-values",
            "CapabilityGranted",
            extended,
        )


@pytest.mark.parametrize(
    "invalid_values",
    [
        _capability_values(targets=["tool:weather"]),
        {
            **_capability_values(),
            "capability_kind": "media_send",
            "constraint_refs": ["constraint:text-only"],
        },
        {**_capability_values(), "constraint_refs": ["constraint:unknown"]},
        {
            **_privacy_values(),
            "media_rule_refs": ["media:private_only", "media:share_allowed"],
        },
        {
            **_privacy_values(),
            "media_rule_refs": ["media:auto_delivery_allowed"],
        },
        {**_privacy_values(), "retention_rule_refs": []},
    ],
)
def test_invalid_scope_and_privacy_matrix_combinations_fail_closed(
    monkeypatch, invalid_values: dict[str, object]
) -> None:
    ledger = _ledger(monkeypatch)
    domain = "privacy" if "subject_ref" in invalid_values else "capability"
    event_type = "PrivacyPolicyRevised" if domain == "privacy" else "CapabilityGranted"
    with pytest.raises(ValueError):
        payload = _mutation(
            domain=domain,
            operation="revise" if domain == "privacy" else "grant",
            transition_id=f"transition:invalid-matrix:{domain}:{len(json.dumps(invalid_values))}",
            entity_id=f"invalid-matrix:{domain}:{len(json.dumps(invalid_values))}",
            expected_revision=0,
            before=None,
            after=invalid_values,
        )
        _commit(
            ledger,
            f"event:invalid-matrix:{domain}:{len(json.dumps(invalid_values))}",
            event_type,
            payload,
        )


def test_shadow_subject_chain_and_constraints_cannot_be_spliced(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    for event_id, event_type, payload in (
        (
            "event:chain:capability",
            "CapabilityGranted",
            _mutation(
                domain="capability",
                operation="grant",
                transition_id="transition:chain:capability",
                entity_id="capability:chain",
                expected_revision=0,
                before=None,
                after=_capability_values(
                    constraints=["constraint:no-third-party", "constraint:text-only"]
                ),
            ),
        ),
        (
            "event:chain:consent",
            "ConsentGranted",
            _mutation(
                domain="consent",
                operation="grant",
                transition_id="transition:chain:consent",
                entity_id="consent:chain",
                expected_revision=0,
                before=None,
                after=_consent_values(grantee="companion:someone-else"),
            ),
        ),
        (
            "event:chain:privacy",
            "PrivacyPolicyRevised",
            _mutation(
                domain="privacy",
                operation="revise",
                transition_id="transition:chain:privacy",
                entity_id="privacy:chain",
                expected_revision=0,
                before=None,
                after=_privacy_values(),
            ),
        ),
    ):
        _commit(ledger, event_id, event_type, payload)
    request = ShadowAuthorizationRequest(
        action_actor_ref=ACTION_ACTOR,
        data_subject_ref=PRINCIPAL,
        capability_kind="message_send",
        action_content_type="text",
        effect_class="external_message",
        third_party_target=False,
        target_scope_refs=("channel:qq",),
        action_scope_refs=("message_send",),
        data_scope_refs=("data:message_content",),
        channel_scope_refs=("channel:qq",),
        viewer_rule_refs=("viewer:companion",),
        media_rule_refs=("media:private_only",),
        retention_rule_refs=("retention:30d",),
        logical_time=NOW,
    )
    decision = evaluate_authorization_shadow(ledger.project(), request)
    assert decision.would_allow is False
    assert "consent_missing" in decision.reason_codes
    constrained = evaluate_authorization_shadow(
        ledger.project(),
        request.model_copy(update={"third_party_target": True}),
    )
    assert constrained.would_allow is False
    assert "capability_missing" in constrained.reason_codes


def test_revision_revoke_and_compensation_never_expand_scope(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    initial = _capability_values()
    broadened = _capability_values(targets=["channel:http", "channel:qq"])
    _commit(
        ledger,
        "event:lifecycle:grant",
        "CapabilityGranted",
        _mutation(
            domain="capability",
            operation="grant",
            transition_id="transition:lifecycle:grant",
            entity_id="capability:lifecycle",
            expected_revision=0,
            before=None,
            after=initial,
        ),
    )
    with pytest.raises(ValueError, match="use revise"):
        _commit(
            ledger,
            "event:lifecycle:grant-again",
            "CapabilityGranted",
            _mutation(
                domain="capability",
                operation="grant",
                transition_id="transition:lifecycle:grant-again",
                entity_id="capability:lifecycle",
                expected_revision=1,
                before=initial,
                after=broadened,
            ),
        )
    _commit(
        ledger,
        "event:lifecycle:revise",
        "CapabilityRevised",
        _mutation(
            domain="capability",
            operation="revise",
            transition_id="transition:lifecycle:revise",
            entity_id="capability:lifecycle",
            expected_revision=1,
            before=initial,
            after=broadened,
        ),
    )
    _commit(
        ledger,
        "event:lifecycle:compensate",
        "CapabilityCompensated",
        _mutation(
            domain="capability",
            operation="compensate",
            transition_id="transition:lifecycle:compensate",
            entity_id="capability:lifecycle",
            expected_revision=2,
            before=broadened,
            after=initial,
            compensates="transition:lifecycle:revise",
        ),
    )
    assert ledger.project().capability_grants[0].values.target_scope_refs == (
        "channel:qq",
    )

    narrow = _capability_values(targets=["channel:http"])
    expanded = _capability_values(targets=["channel:http", "channel:qq"])
    other = _ledger(monkeypatch)
    _commit(
        other,
        "event:unsafe:grant",
        "CapabilityGranted",
        _mutation(
            domain="capability",
            operation="grant",
            transition_id="transition:unsafe:grant",
            entity_id="capability:unsafe",
            expected_revision=0,
            before=None,
            after=expanded,
        ),
    )
    _commit(
        other,
        "event:unsafe:revise",
        "CapabilityRevised",
        _mutation(
            domain="capability",
            operation="revise",
            transition_id="transition:unsafe:revise",
            entity_id="capability:unsafe",
            expected_revision=1,
            before=expanded,
            after=narrow,
        ),
    )
    with pytest.raises(ValueError, match="cannot expand scope"):
        _commit(
            other,
            "event:unsafe:compensate",
            "CapabilityCompensated",
            _mutation(
                domain="capability",
                operation="compensate",
                transition_id="transition:unsafe:compensate",
                entity_id="capability:unsafe",
                expected_revision=2,
                before=narrow,
                after=expanded,
                compensates="transition:unsafe:revise",
            ),
        )


def test_shadow_mechanically_expires_without_writing_expiry_or_actions(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    payload = _mutation(
        domain="capability",
        operation="grant",
        transition_id="transition:expiry:capability",
        entity_id="capability:expiry",
        expected_revision=0,
        before=None,
        after=_capability_values(expires_at=NOW + timedelta(seconds=1)),
    )
    _commit(ledger, "event:expiry:capability", "CapabilityGranted", payload)
    before = ledger.project()
    decision = evaluate_authorization_shadow(
        before,
        ShadowAuthorizationRequest(
            action_actor_ref=ACTION_ACTOR,
            data_subject_ref=PRINCIPAL,
            capability_kind="message_send",
            action_content_type="text",
            effect_class="external_message",
            third_party_target=False,
            target_scope_refs=("channel:qq",),
            action_scope_refs=("message_send",),
            logical_time=NOW + timedelta(seconds=1),
        ),
    )
    assert decision.would_allow is False
    assert "capability_missing" in decision.reason_codes
    assert decision.enforcement_eligible is False
    assert ledger.project() == before
    assert before.actions == ()


def test_same_batch_authority_revoke_then_use_is_atomic_and_rejected(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    capability = _mutation(
        domain="capability",
        operation="grant",
        transition_id="transition:same-batch:capability",
        entity_id="capability:same-batch",
        expected_revision=0,
        before=None,
        after=_capability_values(),
    )
    before = ledger.project()
    with pytest.raises(ValueError, match="(revision is stale|inactive)"):
        ledger.commit(
            [
                _sign_event(
                    "event:same-batch:operator-revoke",
                    "ActorAuthorityRevoked",
                    _operator_revoke_payload(),
                ),
                _prepared_event(
                    "event:same-batch:capability",
                    "CapabilityGranted",
                    capability,
                ),
            ],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    assert ledger.project() == before


def test_authorization_stale_cas_and_user_revocation(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    initial = _consent_values()
    _commit(
        ledger,
        "event:consent-lifecycle:grant",
        "ConsentGranted",
        _mutation(
            domain="consent",
            operation="grant",
            transition_id="transition:consent-lifecycle:grant",
            entity_id="consent:lifecycle",
            expected_revision=0,
            before=None,
            after=initial,
        ),
    )
    revised = {**initial, "data_scope_refs": ["data:attachment", "data:message_content"]}
    with pytest.raises(ValueError, match="stale authorization transition"):
        _commit(
            ledger,
            "event:consent-lifecycle:stale",
            "ConsentRevised",
            _mutation(
                domain="consent",
                operation="revise",
                transition_id="transition:consent-lifecycle:stale",
                entity_id="consent:lifecycle",
                expected_revision=2,
                before=initial,
                after=revised,
            ),
        )
    revoked = {**initial, "status": "revoked"}
    _commit(
        ledger,
        "event:consent-lifecycle:revoke",
        "ConsentRevoked",
        _mutation(
            domain="consent",
            operation="revoke",
            transition_id="transition:consent-lifecycle:revoke",
            entity_id="consent:lifecycle",
            expected_revision=1,
            before=initial,
            after=revoked,
        ),
    )
    assert ledger.project().consent_grants[0].values.status == "revoked"

    invalid = _mutation(
        domain="consent",
        operation="grant",
        transition_id="transition:consent:non-revocable",
        entity_id="consent:non-revocable",
        expected_revision=0,
        before=None,
        after=_consent_values(revocable=False),
    )
    with pytest.raises(ValueError, match="must remain revocable"):
        _commit(ledger, "event:consent:non-revocable", "ConsentGranted", invalid)


def test_projection_grant_cannot_substitute_for_domain_shadow_grants(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    projection_grant = ProjectionGrant(
        world_id=WORLD,
        viewer_id=ACTION_ACTOR,
        viewer_kind="platform_adapter",
        permissions=frozenset({"projection:actions:status"}),
        redaction_policy="platform-v1",
        action_targets=frozenset({"action:any"}),
    )
    request = ShadowAuthorizationRequest(
        action_actor_ref=ACTION_ACTOR,
        data_subject_ref=PRINCIPAL,
        capability_kind="message_send",
        action_content_type="text",
        effect_class="external_message",
        third_party_target=False,
        target_scope_refs=("channel:qq",),
        action_scope_refs=("message_send",),
        logical_time=NOW,
    )
    decision = evaluate_authorization_shadow(ledger.project(), request)
    assert projection_grant.viewer_id == ACTION_ACTOR
    assert decision.would_allow is False
    assert decision.enforcement_eligible is False
    assert ledger.project().actions == ()


def test_authorization_envelope_signature_and_nonce_replay_fail_closed(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    shared_nonce = "authorization-nonce:shared-across-domains"
    first = _mutation(
        domain="capability",
        operation="grant",
        transition_id="transition:root-nonce:first",
        entity_id="capability:root-nonce",
        expected_revision=0,
        before=None,
        after=_capability_values(),
        nonce=shared_nonce,
    )
    signed = _prepared_event(
        "event:authorization:envelope", "CapabilityGranted", first
    )
    tampered = signed.model_copy(update={"actor": "changed-after-root-signature"})
    projection = ledger.project()
    with pytest.raises(ValueError, match="deployment root signature"):
        ledger.commit(
            [tampered],
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
    _commit(ledger, "event:root-nonce:first", "CapabilityGranted", first)
    replay = _mutation(
        domain="consent",
        operation="grant",
        transition_id="transition:root-nonce:replay",
        entity_id="consent:root-nonce",
        expected_revision=0,
        before=None,
        after=_consent_values(),
        nonce=shared_nonce,
    )
    with pytest.raises(ValueError, match="root proof nonce is already consumed"):
        _commit(ledger, "event:root-nonce:replay", "ConsentGranted", replay)


def test_external_authentication_policy_digest_is_installed(monkeypatch) -> None:
    ledger = _ledger(monkeypatch)
    payload = _mutation(
        domain="consent",
        operation="grant",
        transition_id="transition:evidence:policy-tamper",
        entity_id="consent:evidence-policy",
        expected_revision=0,
        before=None,
        after=_consent_values(),
    )
    evidence = payload["principal_action_evidence"]
    evidence["authentication_policy_digest"] = "f" * 64  # type: ignore[index]
    evidence["intent_hash"] = authorization_intent_hash("consent", payload)  # type: ignore[index]
    with pytest.raises(ValueError, match="authentication policy is not installed"):
        _commit(
            ledger,
            "event:evidence:policy-tamper",
            "ConsentGranted",
            payload,
        )


def test_sqlite_roundtrip_and_verified_v8_to_v9_migration(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    path = tmp_path / "authorization-roundtrip.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit(
        [
            _sign_event(
                "event:sqlite:actor-user",
                "ActorAuthorityBootstrapped",
                _actor_bootstrap_payload(),
            ),
            _sign_event(
                "event:sqlite:actor-operator",
                "ActorAuthorityBootstrapped",
                _actor_bootstrap_payload(
                    authority_id=OPERATOR_AUTHORITY_ID,
                    principal_ref=OPERATOR_PRINCIPAL,
                    principal_kind="deployment_operator",
                    credential_ref="credential:operator:opaque",
                    allowed_operations=["capability_grant"],
                    transition_id="transition:sqlite:actor-operator",
                ),
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    payload = _mutation(
        domain="capability",
        operation="grant",
        transition_id="transition:sqlite:capability",
        entity_id="capability:sqlite",
        expected_revision=0,
        before=None,
        after=_capability_values(),
    )
    projection = ledger.project()
    ledger.commit(
        [_prepared_event("event:sqlite:capability", "CapabilityGranted", payload)],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
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
        raw["capability_grants"][0]["values"]["actor_ref"] = "companion:tampered"
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ? WHERE world_id = ?",
            (json.dumps(raw, separators=(",", ":")), WORLD),
        )
    with pytest.raises(LedgerIntegrityError, match="head state( hash)? is invalid"):
        SQLiteWorldLedger(path=path, world_id=WORLD)

    migration_path = tmp_path / "authorization-v8.sqlite3"
    old = SQLiteWorldLedger(path=migration_path, world_id=WORLD)
    observation = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:v8:observation",
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:v8",
        causation_id="cause:v8",
        correlation_id="correlation:v8",
        idempotency_key="event:v8:observation",
        payload={"observation_id": "observation:v8"},
    )
    old.commit(
        [observation],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    old_expected = old.project()
    old.close()
    with sqlite3.connect(migration_path) as connection:
        raw_state = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0]
        state = ReducerState.model_validate_json(raw_state)
        semantic = state.semantic_payload(
            world_id=WORLD,
            world_revision=1,
            reducer_bundle_version="world-v2-reducers.8",
        )
        for key in (
            "capability_grants",
            "capability_transitions",
            "consent_grants",
            "consent_transitions",
            "privacy_policies",
            "privacy_transitions",
            "consumed_authorization_root_nonces",
            "consumed_authorization_challenge_ids",
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
            "UPDATE world_v2_heads SET semantic_hash = ?, reducer_bundle_version = ? "
            "WHERE world_id = ?",
            (legacy_hash, "world-v2-reducers.8", WORLD),
        )
    migrated = SQLiteWorldLedger(path=migration_path, world_id=WORLD)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.10"
    assert migrated.project() == old_expected
    assert migrated.rebuild() == old_expected
    migrated.close()


def test_sqlite_rejects_tampered_v8_authorization_migration_head(tmp_path) -> None:
    path = tmp_path / "authorization-v8-tampered.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_heads SET semantic_hash = ?, reducer_bundle_version = ?",
            ("0" * 64, "world-v2-reducers.8"),
        )
    with pytest.raises(LedgerIntegrityError, match="legacy head semantic hash is invalid"):
        SQLiteWorldLedger(path=path, world_id=WORLD)
