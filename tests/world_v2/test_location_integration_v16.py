from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

from nacl.signing import SigningKey
import pytest

from companion_daemon.world_v2.actor_authority_events import (
    ROOT_KEYSET_DIGEST,
    actor_authority_mutation_hash,
    root_envelope_signature_message,
)
from companion_daemon.world_v2.actor_authority_reducers import (
    ACTOR_AUTHORITY_V2_POLICY_DIGEST,
)
from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.event_catalog import event_contract
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.goal_situation_schemas import (
    DomainOperatorAuthorityBinding,
    RandomDrawBinding,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.location_authority_events import (
    V2LocationChangedPayload,
    v2_location_evidence_refs,
    v2_location_mutation_hash,
)
from companion_daemon.world_v2.location_authority_reducers import (
    V2_LOCATION_POLICY_DIGEST,
    V2_LOCATION_POLICY_REFS,
    V2_LOCATION_POLICY_VERSION,
)
from companion_daemon.world_v2.location_authority_schemas import (
    V2LocationOrigin,
    V2LocationProposalProjection,
    V2LocationProjection,
    V2LocationProposedMutation,
    V2LocationValues,
    v2_location_semantic_fingerprint,
)
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import (
    ActorAuthorityValues,
    LedgerProjection,
    ProjectionCursor,
    WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.typed_proposal_families import family_for_mutation


NOW = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
WORLD = "world:location-integration"
ROOT_SIGNING_KEY = SigningKey(bytes.fromhex("11" * 32))


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def world_event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    source: str = "test:location-integration",
) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id=WORLD,
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="actor:companion",
        source=source,
        trace_id="trace:location-integration",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:location-integration",
        idempotency_key=identity or event_id,
        payload=payload,
    )


def bootstrap_event() -> WorldEvent:
    values = ActorAuthorityValues(
        principal_ref="operator:deployment",
        principal_kind="deployment_operator",
        credential_ref="credential:location-integration",
        allowed_operations=("v2_location_governance",),
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        status="active",
    )
    raw: dict[str, object] = {
        "world_id": WORLD,
        "authority_id": "actor-authority:location-integration",
        "transition_id": "transition:actor-authority:location-integration",
        "operation": "bootstrap",
        "expected_entity_revision": 0,
        "values_before": None,
        "values_after": values.model_dump(mode="json"),
        "policy_version": "actor-authority-policy.2",
        "policy_digest": ACTOR_AUTHORITY_V2_POLICY_DIGEST,
        "changed_at": NOW.isoformat(),
        "compensates_transition_id": None,
        "root_proof": {
            "keyset_version": "deployment-root-keyset.1",
            "keyset_digest": ROOT_KEYSET_DIGEST,
            "root_key_id": "test-only:development-root-1",
            "nonce": "nonce:location-integration:1234567890",
            "signed_mutation_hash": "0" * 64,
            "signature_hex": "0" * 128,
        },
    }
    mutation_hash = actor_authority_mutation_hash(raw)
    raw["root_proof"]["signed_mutation_hash"] = mutation_hash  # type: ignore[index]
    event_id = "event:actor-authority:location-integration"
    identity = domain_idempotency_key(
        event_type="ActorAuthorityBootstrapped", world_id=WORLD, payload=raw
    )
    raw["root_proof"]["signature_hex"] = ROOT_SIGNING_KEY.sign(  # type: ignore[index]
        root_envelope_signature_message(
            schema_version="world-v2.1",
            world_id=WORLD,
            event_type="ActorAuthorityBootstrapped",
            event_id=event_id,
            actor="actor:companion",
            source="deployment-root-ingress",
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:location-integration",
            causation_id=f"cause:{event_id}",
            correlation_id="correlation:location-integration",
            idempotency_key=identity or event_id,
            mutation_hash=mutation_hash,
        )
    ).signature.hex()
    return world_event(
        event_id,
        "ActorAuthorityBootstrapped",
        raw,
        source="deployment-root-ingress",
    )


def revoke_event(projection: LedgerProjection) -> WorldEvent:
    authority = projection.actor_authorities[0]
    revoked = authority.values.model_copy(update={"status": "revoked"})
    event_id = "event:actor-authority:location-revoked"
    raw: dict[str, object] = {
        "world_id": WORLD,
        "authority_id": authority.authority_id,
        "transition_id": "transition:actor-authority:location-revoked",
        "operation": "revoke",
        "expected_entity_revision": authority.entity_revision,
        "values_before": authority.values.model_dump(mode="json"),
        "values_after": revoked.model_dump(mode="json"),
        "policy_version": "actor-authority-policy.2",
        "policy_digest": ACTOR_AUTHORITY_V2_POLICY_DIGEST,
        "changed_at": NOW.isoformat(),
        "compensates_transition_id": None,
        "root_proof": {
            "keyset_version": "deployment-root-keyset.1",
            "keyset_digest": ROOT_KEYSET_DIGEST,
            "root_key_id": "test-only:development-root-1",
            "nonce": "nonce:location-revoke:1234567890",
            "signed_mutation_hash": "0" * 64,
            "signature_hex": "0" * 128,
        },
    }
    mutation_hash = actor_authority_mutation_hash(raw)
    raw["root_proof"]["signed_mutation_hash"] = mutation_hash  # type: ignore[index]
    identity = domain_idempotency_key(
        event_type="ActorAuthorityRevoked", world_id=WORLD, payload=raw
    )
    raw["root_proof"]["signature_hex"] = ROOT_SIGNING_KEY.sign(  # type: ignore[index]
        root_envelope_signature_message(
            schema_version="world-v2.1",
            world_id=WORLD,
            event_type="ActorAuthorityRevoked",
            event_id=event_id,
            actor="actor:companion",
            source="deployment-root-ingress",
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:location-integration",
            causation_id=f"cause:{event_id}",
            correlation_id="correlation:location-integration",
            idempotency_key=identity or event_id,
            mutation_hash=mutation_hash,
        )
    ).signature.hex()
    return world_event(
        event_id,
        "ActorAuthorityRevoked",
        raw,
        source="deployment-root-ingress",
    )


def operator_binding(projection: LedgerProjection) -> DomainOperatorAuthorityBinding:
    authority = projection.actor_authorities[0]
    source = next(
        item
        for item in projection.committed_world_event_refs
        if item.event_id == authority.origin.event_ref
    )
    return DomainOperatorAuthorityBinding(
        authority_id=authority.authority_id,
        authority_revision=authority.entity_revision,
        principal_ref=authority.values.principal_ref,
        authority_event_ref=source.event_id,
        authority_world_revision=source.world_revision,
        authority_payload_hash=source.payload_hash,
        authority_values_hash=canonical_hash(authority.values.model_dump(mode="json")),
        authority_policy_digest=authority.policy_digest,
        authorization_contract="deployment-actor-authority:v16-domain.1",
        required_operation="v2_location_governance",
    )


def location_payload(
    projection: LedgerProjection,
    *,
    event_id: str,
    proposal_id: str,
) -> tuple[V2LocationChangedPayload, V2LocationProposalProjection]:
    origin = V2LocationOrigin(
        change_id="change:location:establish",
        transition_id="transition:location:establish",
        policy_refs=V2_LOCATION_POLICY_REFS,
        accepted_event_ref=event_id,
    )
    values = V2LocationValues(
        location_ref="location:apartment",
        zone_ref="zone:study",
        scene_visibility="private",
        privacy_class="private",
        since=NOW,
    )
    after = V2LocationProjection(
        actor_ref="actor:companion",
        entity_revision=1,
        semantic_fingerprint=v2_location_semantic_fingerprint(
            actor_ref="actor:companion",
            values=values,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        updated_at=NOW,
    )
    raw = {
        "change_id": origin.change_id,
        "transition_id": origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": (),
        "policy_refs": V2_LOCATION_POLICY_REFS,
        "acceptance_id": "acceptance:location:establish",
        "proposal_id": proposal_id,
        "evaluated_world_revision": projection.world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": "establish",
        "authority_lane": "operator",
        "selection_mode": "direct",
        "random_draw_binding": None,
        "location_before": None,
        "location_after": after,
        "cause_authority": operator_binding(projection),
        "policy_version": V2_LOCATION_POLICY_VERSION,
        "policy_digest": V2_LOCATION_POLICY_DIGEST,
    }
    raw["evidence_refs"] = v2_location_evidence_refs(raw)
    raw["accepted_change_hash"] = v2_location_mutation_hash(raw)
    payload = V2LocationChangedPayload.model_validate(raw)
    mutation_json = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    proposal = V2LocationProposalProjection(
        proposal_id=proposal_id,
        transition_kind="establish",
        change_id=payload.change_id,
        transition_id=payload.transition_id,
        evaluated_world_revision=payload.evaluated_world_revision,
        expected_entity_revision=payload.expected_entity_revision,
        proposed_change_hash=payload.accepted_change_hash,
        evidence_refs=payload.evidence_refs,
        policy_refs=payload.policy_refs,
        proposed_mutation=V2LocationProposedMutation(
            event_type="V2LocationChanged",
            payload_json=mutation_json,
        ),
    )
    return payload, proposal


def seed_operator(ledger: WorldLedger | SQLiteWorldLedger) -> LedgerProjection:
    ledger.commit(
        [
            world_event(
                "event:clock:location-integration",
                "ClockAdvanced",
                {
                    "logical_time_from": (NOW - timedelta(minutes=1)).isoformat(),
                    "logical_time_to": NOW.isoformat(),
                },
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    clocked = ledger.project()
    ledger.commit(
        [bootstrap_event()],
        expected_world_revision=clocked.world_revision,
        expected_deliberation_revision=clocked.deliberation_revision,
    )
    return ledger.project()


def test_location_typed_roundtrip_reopen_rebuild_and_project_at(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    path = tmp_path / "location-v16.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    before = seed_operator(ledger)
    payload, proposal = location_payload(
        before,
        event_id="event:location:establish",
        proposal_id="proposal:location:establish",
    )
    ledger.commit(
        [
            world_event(
                "event:proposal:location:establish",
                "ProposalRecorded",
                proposal.model_dump(mode="json"),
            )
        ],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    proposed = ledger.project()
    proposed_cursor = ProjectionCursor(
        world_revision=proposed.world_revision,
        deliberation_revision=proposed.deliberation_revision,
        ledger_sequence=proposed.ledger_sequence,
    )
    ghost_proposal = proposed.model_dump(mode="json")
    ghost_proposal["actor_authority_transitions"] = []
    ghost_proposal["committed_world_event_refs"] = []
    with pytest.raises(ValueError, match="future world revision|exact operator authority"):
        LedgerProjection.model_validate_json(json.dumps(ghost_proposal))
    for attack in ("policy", "random"):
        attacked_proposal = proposal.model_dump(mode="json")
        if attack == "policy":
            attacked_payload = payload.model_copy(
                update={
                    "policy_refs": ("policy:v2-location-authority:forged",),
                    "accepted_change_hash": "0" * 64,
                }
            )
            attacked_proposal["policy_refs"] = list(attacked_payload.policy_refs)
        else:
            attacked_payload = payload.model_copy(
                update={
                    "selection_mode": "random_draw",
                    "random_draw_binding": RandomDrawBinding(
                        draw_event_ref="event:draw:forged",
                        draw_world_revision=1,
                        draw_payload_hash="d" * 64,
                        attempt_id="attempt:draw:forged",
                        candidate_set_hash="c" * 64,
                        selected_candidate_ref="location:apartment",
                        catalog_version="location-candidates.1",
                        sampler_version="sampler.1",
                    ),
                    "accepted_change_hash": "0" * 64,
                }
            )
        attacked_payload = attacked_payload.model_copy(
            update={
                "accepted_change_hash": v2_location_mutation_hash(attacked_payload)
            }
        )
        mutation = attacked_payload.model_dump(mode="json")
        attacked_proposal["proposed_change_hash"] = attacked_payload.accepted_change_hash
        attacked_proposal["proposed_mutation"]["payload_json"] = json.dumps(
            mutation, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        attacked_projection = proposed.model_dump(mode="json")
        attacked_projection["location_proposals"] = [attacked_proposal]
        with pytest.raises(ValueError, match="uninstalled policy|RandomAuthority"):
            LedgerProjection.model_validate_json(json.dumps(attacked_projection))
    acceptance = {
        "proposal_id": payload.proposal_id,
        "evaluated_world_revision": payload.evaluated_world_revision,
        "acceptance_id": payload.acceptance_id,
        "status": "accepted",
        "accepted_change_id": payload.change_id,
        "accepted_change_hash": payload.accepted_change_hash,
    }
    ledger.commit(
        [
            world_event(
                "event:acceptance:location:establish",
                "AcceptanceRecorded",
                acceptance,
            ),
            world_event(
                "event:location:establish",
                "V2LocationChanged",
                payload.model_dump(mode="json"),
            ),
        ],
        expected_world_revision=proposed.world_revision,
        expected_deliberation_revision=proposed.deliberation_revision,
    )
    located = ledger.project()
    assert located.locations == (payload.location_after,)
    assert len(located.location_transitions) == 1
    assert located.location_proposals == located.location_proposal_ids == ()
    assert located.goals == before.goals
    ledger.commit(
        [revoke_event(located)],
        expected_world_revision=located.world_revision,
        expected_deliberation_revision=located.deliberation_revision,
    )
    expected = ledger.project()
    assert expected.locations == located.locations
    assert expected.location_transitions == located.location_transitions

    stale_origin = payload.location_after.origin.model_copy(
        update={
            "change_id": "change:location:after-revoke",
            "transition_id": "transition:location:after-revoke",
            "accepted_event_ref": "event:location:after-revoke",
        }
    )
    stale_values = payload.location_after.values.model_copy(
        update={"zone_ref": "zone:hallway", "since": NOW}
    )
    stale_after = V2LocationProjection(
        actor_ref=payload.location_after.actor_ref,
        entity_revision=2,
        semantic_fingerprint=v2_location_semantic_fingerprint(
            actor_ref=payload.location_after.actor_ref,
            values=stale_values,
            policy_refs=stale_origin.policy_refs,
        ),
        values=stale_values,
        origin=stale_origin,
        updated_at=NOW,
    )
    stale_raw = payload.model_dump(mode="python")
    stale_raw.update(
        proposal_id="proposal:location:after-revoke",
        acceptance_id="acceptance:location:after-revoke",
        change_id=stale_origin.change_id,
        transition_id=stale_origin.transition_id,
        evaluated_world_revision=expected.world_revision,
        expected_entity_revision=1,
        operation="change",
        location_before=payload.location_after,
        location_after=stale_after,
        accepted_change_hash="0" * 64,
    )
    stale_raw["accepted_change_hash"] = v2_location_mutation_hash(stale_raw)
    stale_payload = V2LocationChangedPayload.model_validate(stale_raw)
    stale_proposal = V2LocationProposalProjection(
        proposal_id=stale_payload.proposal_id,
        transition_kind="change",
        change_id=stale_payload.change_id,
        transition_id=stale_payload.transition_id,
        evaluated_world_revision=stale_payload.evaluated_world_revision,
        expected_entity_revision=1,
        proposed_change_hash=stale_payload.accepted_change_hash,
        evidence_refs=stale_payload.evidence_refs,
        policy_refs=stale_payload.policy_refs,
        proposed_mutation=V2LocationProposedMutation(
            event_type="V2LocationChanged",
            payload_json=json.dumps(
                stale_payload.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    with pytest.raises(ValueError, match="ActorAuthority"):
        ledger.commit(
            [
                world_event(
                    "event:proposal:location:after-revoke",
                    "ProposalRecorded",
                    stale_proposal.model_dump(mode="json"),
                )
            ],
            expected_world_revision=expected.world_revision,
            expected_deliberation_revision=expected.deliberation_revision,
        )
    assert ledger.project() == expected
    assert ledger.project_at(proposed_cursor).locations == ()
    assert ledger.project_at(proposed_cursor).location_proposals == (proposal,)
    assert ledger.rebuild() == expected
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_location_registry_catalog_identity_and_proposal_dry_run_cas(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = WorldLedger.in_memory(world_id=WORLD)
    before = seed_operator(ledger)
    payload, proposal = location_payload(
        before,
        event_id="event:location:establish",
        proposal_id="proposal:location:establish",
    )
    family = family_for_mutation("V2LocationChanged")
    assert family is not None
    assert family.contract_ref == "proposal-contract:v2-location.1"
    assert event_contract("V2LocationChanged").allowed_predecessors == (
        "AcceptanceRecorded",
    )
    mutation_identity = domain_idempotency_key(
        event_type="V2LocationChanged",
        world_id=WORLD,
        payload=payload.model_dump(mode="json"),
    )
    assert mutation_identity is not None

    stale_raw = proposal.model_dump(mode="json")
    stale_raw["expected_entity_revision"] = 1
    stale_raw["proposed_mutation"] = dict(stale_raw["proposed_mutation"])
    decoded = json.loads(stale_raw["proposed_mutation"]["payload_json"])
    decoded["expected_entity_revision"] = 1
    decoded["accepted_change_hash"] = "0" * 64
    decoded["accepted_change_hash"] = v2_location_mutation_hash(decoded)
    stale_raw["proposed_change_hash"] = decoded["accepted_change_hash"]
    stale_raw["proposed_mutation"]["payload_json"] = json.dumps(
        decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    stale = V2LocationProposalProjection.model_validate_json(json.dumps(stale_raw))
    with pytest.raises(ValueError, match="establish must create"):
        ledger.commit(
            [
                world_event(
                    "event:proposal:location:stale",
                    "ProposalRecorded",
                    stale.model_dump(mode="json"),
                )
            ],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    assert ledger.project() == before


@pytest.mark.parametrize(
    "field",
    ("locations", "location_transitions", "location_proposals", "location_proposal_ids"),
)
def test_legacy_heads_cannot_inject_location_authority_fields(tmp_path, field: str) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / f"legacy-{field}.sqlite3", world_id=WORLD)
    with pytest.raises(LedgerIntegrityError, match="legacy head state is invalid"):
        ledger._legacy_semantic_hash(
            state_json=json.dumps({field: []}),
            world_revision=0,
            reducer_bundle_version="world-v2-reducers.15",
        )
    ledger.close()


def test_location_state_validation_rejects_forged_lineage(monkeypatch) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = WorldLedger.in_memory(world_id=WORLD)
    before = seed_operator(ledger)
    payload, proposal = location_payload(
        before,
        event_id="event:location:establish",
        proposal_id="proposal:location:establish",
    )
    ledger.commit(
        [world_event("event:proposal:location", "ProposalRecorded", proposal.model_dump(mode="json"))],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [
            world_event(
                "event:acceptance:location",
                "AcceptanceRecorded",
                {
                    "proposal_id": payload.proposal_id,
                    "evaluated_world_revision": payload.evaluated_world_revision,
                    "acceptance_id": payload.acceptance_id,
                    "status": "accepted",
                    "accepted_change_id": payload.change_id,
                    "accepted_change_hash": payload.accepted_change_hash,
                },
            ),
            world_event(
                payload.location_after.origin.accepted_event_ref,
                "V2LocationChanged",
                payload.model_dump(mode="json"),
            ),
        ],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=1,
    )
    state = SQLiteWorldLedger._state_from_projection(ledger.project())
    raw = state.model_dump(mode="json")
    raw["location_transitions"][0]["policy_refs"] = ["policy:forged"]
    with pytest.raises(ValueError, match="uninstalled policy"):
        ReducerState.model_validate_json(json.dumps(raw))

    projection_raw = ledger.project().model_dump(mode="json")
    projection_raw["locations"][0]["origin"]["accepted_event_ref"] = "event:forged"
    with pytest.raises(ValueError, match="latest transition"):
        LedgerProjection.model_validate_json(json.dumps(projection_raw))

    ghost = ledger.project().model_dump(mode="json")
    ghost["actor_authority_transitions"] = []
    ghost["committed_world_event_refs"] = []
    with pytest.raises(ValueError, match="committed mutation event|operator authority"):
        LedgerProjection.model_validate_json(json.dumps(ghost))

    current_projection = ledger.project()
    ghost_after = payload.location_after.model_copy(
        update={
            "origin": payload.location_after.origin.model_copy(
                update={
                    "change_id": "change:location:ghost-establish",
                    "transition_id": "transition:location:ghost-establish",
                    "accepted_event_ref": "event:location:ghost-establish",
                }
            )
        }
    )
    ghost_raw = payload.model_dump(mode="python")
    ghost_raw.update(
        proposal_id="proposal:location:ghost-establish",
        acceptance_id="acceptance:location:ghost-establish",
        change_id=ghost_after.origin.change_id,
        transition_id=ghost_after.origin.transition_id,
        evaluated_world_revision=current_projection.world_revision,
        location_after=ghost_after,
        accepted_change_hash="0" * 64,
    )
    ghost_raw["accepted_change_hash"] = v2_location_mutation_hash(ghost_raw)
    ghost_payload = V2LocationChangedPayload.model_validate(ghost_raw)
    ghost_proposal = V2LocationProposalProjection(
        proposal_id=ghost_payload.proposal_id,
        transition_kind="establish",
        change_id=ghost_payload.change_id,
        transition_id=ghost_payload.transition_id,
        evaluated_world_revision=ghost_payload.evaluated_world_revision,
        expected_entity_revision=0,
        proposed_change_hash=ghost_payload.accepted_change_hash,
        evidence_refs=ghost_payload.evidence_refs,
        policy_refs=ghost_payload.policy_refs,
        proposed_mutation=V2LocationProposedMutation(
            event_type="V2LocationChanged",
            payload_json=json.dumps(
                ghost_payload.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    ghost_state = current_projection.model_dump(mode="json")
    ghost_state["location_proposals"] = [ghost_proposal.model_dump(mode="json")]
    ghost_state["location_proposal_ids"] = [ghost_proposal.proposal_id]
    ghost_state["proposal_ids"].append(ghost_proposal.proposal_id)
    with pytest.raises(ValueError, match="embedded CAS"):
        LedgerProjection.model_validate_json(json.dumps(ghost_state))
