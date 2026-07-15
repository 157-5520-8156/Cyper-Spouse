from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from legacy_migration_support import legacy_state_json

from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.projection import InternalProjectionReader
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.relationship_events import (
    BoundaryChangedPayload,
    RelationshipSignalAcceptedPayload,
    RelationshipSlowVariableAdjustedPayload,
    relationship_mutation_hash,
)
from companion_daemon.world_v2.relationship_reducers import RELATIONSHIP_POLICY_DIGEST
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import (
    BoundaryProjection,
    EvidenceRef,
    RelationshipBoundaryOrigin,
    RelationshipHysteresisProjection,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    RelationshipSignalOrigin,
    RelationshipSignalProjection,
    RelationshipVariableDeltas,
    RelationshipVariablesProjection,
    WorldEvent,
    relationship_signal_fingerprint,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
WORLD = "world-relationship-authority"
EVIDENCE_HASH = "a" * 64


def event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    identity = domain_idempotency_key(event_type=event_type, world_id=WORLD, payload=payload)
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:relationship",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:relationship",
        idempotency_key=identity or f"identity:{event_id}",
        payload=payload,
    )


def canonical(value: dict[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evidence() -> EvidenceRef:
    return EvidenceRef(
        ref_id="operator:relationship",
        evidence_type="operator_observation",
        claim_purpose="private_hypothesis",
        immutable_hash=EVIDENCE_HASH,
    )


def authorized(model_type, **values):
    raw = {
        "change_id": values.pop("change_id"),
        "transition_id": values.pop("transition_id"),
        "expected_entity_revision": values.pop("expected_entity_revision"),
        "evidence_refs": values.pop("evidence_refs", (evidence(),)),
        "policy_refs": values.pop("policy_refs"),
        "acceptance_id": values.pop("acceptance_id"),
        "proposal_id": values.pop("proposal_id"),
        "evaluated_world_revision": values.pop("evaluated_world_revision"),
        "accepted_change_hash": "0" * 64,
        **values,
    }
    if model_type is RelationshipSlowVariableAdjustedPayload:
        raw.setdefault("compensates_adjustment_id", None)
        raw.setdefault("commitment_refs", ())
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    return model_type.model_validate(raw)


def proposal(payload, *, transition_kind: str) -> RelationshipProposalProjection:
    event_type = {
        RelationshipSignalAcceptedPayload: "RelationshipSignalAccepted",
        RelationshipSlowVariableAdjustedPayload: "RelationshipSlowVariableAdjusted",
        BoundaryChangedPayload: "BoundaryChanged",
    }[type(payload)]
    return RelationshipProposalProjection(
        proposal_id=payload.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:relationship.1",
        transition_kind=transition_kind,
        change_id=payload.change_id,
        transition_id=payload.transition_id,
        evaluated_world_revision=payload.evaluated_world_revision,
        expected_entity_revision=payload.expected_entity_revision,
        proposed_change_hash=payload.accepted_change_hash,
        evidence_refs=payload.evidence_refs,
        policy_refs=payload.policy_refs,
        proposed_mutation=RelationshipProposedMutation(
            event_type=event_type,
            payload_json=canonical(payload.model_dump(mode="json")),
        ),
    )


def decide_and_mutate(ledger: WorldLedger, payload, mutation_type: str) -> None:
    projection = ledger.project()
    acceptance = {
        "acceptance_id": payload.acceptance_id,
        "status": "accepted",
        "proposal_id": payload.proposal_id,
        "evaluated_world_revision": payload.evaluated_world_revision,
        "accepted_change_id": payload.change_id,
        "accepted_change_hash": payload.accepted_change_hash,
    }
    ledger.commit(
        [
            event(f"event:{payload.acceptance_id}", "AcceptanceRecorded", acceptance),
            event(
                payload.signal.origin.accepted_event_ref
                if isinstance(payload, RelationshipSignalAcceptedPayload)
                else payload.boundary.origin.accepted_event_ref
                if isinstance(payload, BoundaryChangedPayload)
                else f"event:{payload.adjustment_id}",
                mutation_type,
                payload.model_dump(mode="json"),
            ),
        ],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def record_proposal(ledger: WorldLedger, value: RelationshipProposalProjection) -> None:
    projection = ledger.project()
    ledger.commit(
        [event(f"event:{value.proposal_id}", "ProposalRecorded", value.model_dump(mode="json"))],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def initialized_ledger() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit(
        [
            event("event:init", "ObservationRecorded", {"observation_id": "obs:init"}),
            event(
                "event:init-operator",
                "OperatorObservationRecorded",
                {"observation_id": "operator:relationship", "observation_hash": EVIDENCE_HASH},
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return ledger


def new_signal_payload(*, event_ref: str = "event:test-signal") -> RelationshipSignalAcceptedPayload:
    refs = (evidence(),)
    policy_refs = ("policy:relationship-signal-v1",)
    signal = RelationshipSignalProjection(
        signal_id="signal:test",
        semantic_fingerprint=relationship_signal_fingerprint(
            subject_ref="user:geoff",
            signal_code="test_signal",
            evidence_refs=refs,
            policy_refs=policy_refs,
        ),
        entity_revision=1,
        subject_ref="user:geoff",
        signal_code="test_signal",
        confidence_bp=8_000,
        persistence="durable",
        rationale_code="test_signal",
        evidence_refs=refs,
        origin=RelationshipSignalOrigin(
            change_id="change:test-signal",
            transition_id="transition:test-signal",
            policy_refs=policy_refs,
            accepted_event_ref=event_ref,
        ),
        accepted_at=NOW,
    )
    return authorized(
        RelationshipSignalAcceptedPayload,
        change_id=signal.origin.change_id,
        transition_id=signal.origin.transition_id,
        expected_entity_revision=0,
        policy_refs=signal.origin.policy_refs,
        acceptance_id="acceptance:test-signal",
        proposal_id="proposal:test-signal",
        evaluated_world_revision=1,
        signal=signal,
    )


def accepted_decision(payload) -> dict[str, object]:
    return {
        "acceptance_id": payload.acceptance_id,
        "status": "accepted",
        "proposal_id": payload.proposal_id,
        "evaluated_world_revision": payload.evaluated_world_revision,
        "accepted_change_id": payload.change_id,
        "accepted_change_hash": payload.accepted_change_hash,
    }


def test_unknown_typed_contract_fails_closed_but_unmarked_same_kind_stays_legacy() -> None:
    ledger = initialized_ledger()
    unknown = {
        "proposal_id": "proposal:unknown-contract",
        "proposal_kind": "relationship_transition",
        "proposal_encoding": "typed-authority-v1",
        "authority_contract_ref": "proposal-contract:not-installed.1",
        "evaluated_world_revision": 1,
    }
    with pytest.raises(ValueError, match="not installed"):
        ledger.commit(
            [event("event:unknown-contract", "ProposalRecorded", unknown)],
            expected_world_revision=1,
            expected_deliberation_revision=1,
        )

    legacy = {
        "proposal_id": "proposal:legacy-relationship",
        "proposal_kind": "relationship_transition",
        "authority_contract_ref": "proposal-contract:legacy-audit.1",
        "evaluated_world_revision": 1,
    }
    ledger.commit(
        [event("event:legacy-relationship", "ProposalRecorded", legacy)],
        expected_world_revision=1,
        expected_deliberation_revision=1,
    )
    with pytest.raises(ValueError, match="accepted decision"):
        ledger.commit(
            [
                event(
                    "event:legacy-accepted",
                    "AcceptanceRecorded",
                    {
                        "acceptance_id": "acceptance:legacy",
                        "status": "accepted",
                        "proposal_id": legacy["proposal_id"],
                        "evaluated_world_revision": 1,
                        "accepted_change_id": "change:legacy",
                        "accepted_change_hash": "b" * 64,
                    },
                )
            ],
            expected_world_revision=1,
            expected_deliberation_revision=2,
        )


@pytest.mark.parametrize("status", ("rejected", "stale"))
def test_rejected_or_stale_decision_discards_pending_typed_proposal(status: str) -> None:
    ledger = initialized_ledger()
    payload = new_signal_payload()
    record_proposal(ledger, proposal(payload, transition_kind="signal"))
    if status == "stale":
        ledger.commit(
            [event("event:make-stale", "ObservationRecorded", {"observation_id": "obs:later"})],
            expected_world_revision=1,
            expected_deliberation_revision=2,
        )
    projection = ledger.project()
    ledger.commit(
        [
            event(
                f"event:{status}",
                "AcceptanceRecorded",
                {
                    "acceptance_id": f"acceptance:{status}",
                    "status": status,
                    "proposal_id": payload.proposal_id,
                    "evaluated_world_revision": payload.evaluated_world_revision,
                },
            )
        ],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    assert ledger.project().relationship_proposals == ()


def test_relationship_mutation_rejects_orphan_and_non_adjacent_acceptance() -> None:
    orphan_ledger = initialized_ledger()
    orphan = new_signal_payload()
    with pytest.raises(ValueError, match="adjacent revision-pinned AcceptanceRecorded"):
        orphan_ledger.commit(
            [event(orphan.signal.origin.accepted_event_ref, "RelationshipSignalAccepted", orphan.model_dump(mode="json"))],
            expected_world_revision=1,
            expected_deliberation_revision=1,
        )

    ledger = initialized_ledger()
    payload = new_signal_payload(event_ref="event:non-adjacent-signal")
    record_proposal(ledger, proposal(payload, transition_kind="signal"))
    with pytest.raises(ValueError, match="immediately after"):
        ledger.commit(
            [
                event("event:non-adjacent-acceptance", "AcceptanceRecorded", accepted_decision(payload)),
                event("event:interposed", "ObservationRecorded", {"observation_id": "obs:interposed"}),
                event(payload.signal.origin.accepted_event_ref, "RelationshipSignalAccepted", payload.model_dump(mode="json")),
            ],
            expected_world_revision=1,
            expected_deliberation_revision=2,
        )


def test_ledger_rejects_same_signal_evidence_under_new_ids_but_allows_competing_meaning() -> None:
    ledger = initialized_ledger()
    first = new_signal_payload(event_ref="event:first-semantic-signal")
    record_proposal(ledger, proposal(first, transition_kind="signal"))
    decide_and_mutate(ledger, first, "RelationshipSignalAccepted")

    def candidate(
        *, signal_id: str, signal_code: str, proposal_id: str, claim_purpose: str
    ):
        refs = (
            EvidenceRef(
                ref_id="operator:relationship",
                evidence_type="operator_observation",
                claim_purpose=claim_purpose,
                immutable_hash=EVIDENCE_HASH,
            ),
        )
        policy_refs = ("policy:relationship-signal-v1",)
        candidate_signal = RelationshipSignalProjection(
            signal_id=signal_id,
            semantic_fingerprint=relationship_signal_fingerprint(
                subject_ref="user:geoff",
                signal_code=signal_code,
                evidence_refs=refs,
                policy_refs=policy_refs,
            ),
            entity_revision=1,
            subject_ref="user:geoff",
            signal_code=signal_code,
            confidence_bp=4_000,
            persistence="session",
            contradiction_group_ref="group:changed-metadata",
            rationale_code="changed_metadata_does_not_create_evidence",
            evidence_refs=refs,
            origin=RelationshipSignalOrigin(
                change_id=f"change:{signal_id}",
                transition_id=f"transition:{signal_id}",
                policy_refs=policy_refs,
                accepted_event_ref=f"event:{signal_id}",
            ),
            accepted_at=NOW,
        )
        return authorized(
            RelationshipSignalAcceptedPayload,
            change_id=candidate_signal.origin.change_id,
            transition_id=candidate_signal.origin.transition_id,
            expected_entity_revision=0,
            policy_refs=policy_refs,
            acceptance_id=f"acceptance:{signal_id}",
            proposal_id=proposal_id,
            evaluated_world_revision=3,
            evidence_refs=refs,
            signal=candidate_signal,
        )

    duplicate = candidate(
        signal_id="signal:new-id-same-evidence",
        signal_code="test_signal",
        proposal_id="proposal:duplicate-semantic",
        claim_purpose="action_authorization",
    )
    with pytest.raises(ValueError, match="semantic evidence already exists"):
        record_proposal(ledger, proposal(duplicate, transition_kind="signal"))

    competing = candidate(
        signal_id="signal:competing-meaning",
        signal_code="competing_interpretation",
        proposal_id="proposal:competing-meaning",
        claim_purpose="action_authorization",
    )
    record_proposal(ledger, proposal(competing, transition_kind="signal"))
    assert ledger.project().relationship_proposals[0].proposal_id == competing.proposal_id

def exercise_relationship_authority(ledger) -> object:
    ledger.commit(
        [
            event("event:clock-source", "ObservationRecorded", {"observation_id": "obs:clock"}),
            event(
                "event:operator",
                "OperatorObservationRecorded",
                {"observation_id": "operator:relationship", "observation_hash": EVIDENCE_HASH},
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    signal = RelationshipSignalProjection(
        signal_id="signal:reliability",
        semantic_fingerprint=relationship_signal_fingerprint(
            subject_ref="user:geoff",
            signal_code="reliability_observed",
            evidence_refs=(evidence(),),
            policy_refs=("policy:relationship-signal-v1",),
        ),
        entity_revision=1,
        subject_ref="user:geoff",
        signal_code="reliability_observed",
        confidence_bp=8_000,
        persistence="durable",
        contradiction_group_ref="group:reliability",
        rationale_code="settled_interaction_signal",
        evidence_refs=(evidence(),),
        origin=RelationshipSignalOrigin(
            change_id="change:signal",
            transition_id="transition:signal",
            policy_refs=("policy:relationship-signal-v1",),
            accepted_event_ref="event:signal-mutation",
        ),
        accepted_at=NOW,
    )
    signal_payload = authorized(
        RelationshipSignalAcceptedPayload,
        change_id="change:signal",
        transition_id="transition:signal",
        expected_entity_revision=0,
        policy_refs=("policy:relationship-signal-v1",),
        acceptance_id="acceptance:signal",
        proposal_id="proposal:signal",
        evaluated_world_revision=1,
        signal=signal,
    )
    record_proposal(ledger, proposal(signal_payload, transition_kind="signal"))
    assert ledger.project().relationship_signals == ()
    decide_and_mutate(ledger, signal_payload, "RelationshipSignalAccepted")

    before = RelationshipVariablesProjection()
    after = RelationshipVariablesProjection(trust_bp=300)
    adjustment = authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id="change:adjustment",
        transition_id="transition:adjustment",
        expected_entity_revision=0,
        policy_refs=("policy:relationship-v1",),
        acceptance_id="acceptance:adjustment",
        proposal_id="proposal:adjustment",
        evaluated_world_revision=3,
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id="adjustment:1",
        operation="adjust",
        signal_refs=(signal.signal_id,),
        proposed_deltas=RelationshipVariableDeltas(trust_bp=400),
        accepted_deltas=RelationshipVariableDeltas(trust_bp=300),
        variables_before=before,
        variables_after=after,
        stage_before="stranger",
        stage_after="stranger",
        hysteresis_before=RelationshipHysteresisProjection(),
        hysteresis_after=RelationshipHysteresisProjection(),
        confidence_bp=8_000,
        persistence="durable",
        contradiction_group_ref="group:reliability",
        rationale_code="reliability_observed",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=NOW,
    )
    record_proposal(ledger, proposal(adjustment, transition_kind="adjust"))
    decide_and_mutate(ledger, adjustment, "RelationshipSlowVariableAdjusted")
    state = ledger.project().relationship_states[0]
    assert state.origin is not None
    assert state.origin.accepted_event_ref == "event:adjustment:1"
    assert state.origin.change_id == adjustment.change_id

    boundary = BoundaryProjection(
        boundary_id="boundary:privacy",
        entity_revision=1,
        subject_ref="user:geoff",
        scope_ref="scope:private-media",
        strength_bp=8_000,
        status="active",
        evidence_refs=(evidence(),),
        origin=RelationshipBoundaryOrigin(
            change_id="change:boundary",
            transition_id="transition:boundary",
            policy_refs=("policy:boundary-v1",),
            accepted_event_ref="event:boundary-mutation",
        ),
        policy_version="boundary-policy.1",
        opened_at=NOW,
        updated_at=NOW,
    )
    boundary_payload = authorized(
        BoundaryChangedPayload,
        change_id="change:boundary",
        transition_id="transition:boundary",
        expected_entity_revision=0,
        policy_refs=("policy:boundary-v1",),
        acceptance_id="acceptance:boundary",
        proposal_id="proposal:boundary",
        evaluated_world_revision=5,
        operation="open",
        boundary=boundary,
    )
    record_proposal(ledger, proposal(boundary_payload, transition_kind="boundary_open"))
    decide_and_mutate(ledger, boundary_payload, "BoundaryChanged")

    duplicate_boundary = boundary.model_copy(
        update={
            "boundary_id": "boundary:privacy:duplicate",
            "origin": RelationshipBoundaryOrigin(
                change_id="change:boundary:duplicate",
                transition_id="transition:boundary:duplicate",
                policy_refs=("policy:boundary-v1",),
                accepted_event_ref="event:boundary-duplicate",
            ),
        }
    )
    duplicate_boundary_payload = authorized(
        BoundaryChangedPayload,
        change_id="change:boundary:duplicate",
        transition_id="transition:boundary:duplicate",
        expected_entity_revision=0,
        evidence_refs=duplicate_boundary.evidence_refs,
        policy_refs=("policy:boundary-v1",),
        acceptance_id="acceptance:boundary:duplicate",
        proposal_id="proposal:boundary:duplicate",
        evaluated_world_revision=7,
        operation="open",
        boundary=duplicate_boundary,
    )
    with pytest.raises(ValueError, match="subject scope"):
        record_proposal(
            ledger,
            proposal(duplicate_boundary_payload, transition_kind="boundary_open"),
        )

    compensation = authorized(
        RelationshipSlowVariableAdjustedPayload,
        change_id="change:compensation",
        transition_id="transition:compensation",
        expected_entity_revision=1,
        policy_refs=("policy:relationship-v1",),
        acceptance_id="acceptance:compensation",
        proposal_id="proposal:compensation",
        evaluated_world_revision=7,
        relationship_id="relationship:user:geoff",
        subject_ref="user:geoff",
        adjustment_id="adjustment:compensation",
        operation="compensate",
        signal_refs=(signal.signal_id,),
        proposed_deltas=RelationshipVariableDeltas(trust_bp=-300),
        accepted_deltas=RelationshipVariableDeltas(trust_bp=-300),
        variables_before=after,
        variables_after=before,
        stage_before="stranger",
        stage_after="stranger",
        hysteresis_before=RelationshipHysteresisProjection(),
        hysteresis_after=RelationshipHysteresisProjection(),
        confidence_bp=10_000,
        persistence="durable",
        contradiction_group_ref="group:reliability",
        rationale_code="correction",
        policy_version="relationship-policy.1",
        policy_digest=RELATIONSHIP_POLICY_DIGEST,
        adjusted_at=NOW,
        compensates_adjustment_id="adjustment:1",
    )
    record_proposal(ledger, proposal(compensation, transition_kind="compensate"))
    decide_and_mutate(ledger, compensation, "RelationshipSlowVariableAdjusted")

    projection = ledger.project()
    assert projection.relationship_signals == (signal,)
    assert projection.relationship_states[0].variables == before
    assert projection.relationship_adjustments[0].proposed_deltas.trust_bp == 400
    assert projection.relationship_adjustments[0].accepted_deltas.trust_bp == 300
    assert projection.relationship_adjustments[1].compensates_adjustment_id == "adjustment:1"
    assert projection.boundaries == (boundary,)
    assert projection.relationship_states[0].stage == "stranger"
    assert ledger.rebuild() == projection
    snapshot = InternalProjectionReader(ledger=ledger).snapshot(world_id=WORLD)
    assert snapshot.relationship_state == projection.relationship_states[0]
    assert snapshot.relationship_boundaries == (boundary,)
    assert next(
        item for item in snapshot.slice_windows if item.slice_name == "relationship_state"
    ).availability == "available"
    return projection


def test_typed_relationship_authority_replays_signal_adjustment_and_boundary() -> None:
    exercise_relationship_authority(WorldLedger.in_memory(world_id=WORLD))


def test_sqlite_relationship_authority_survives_restart(tmp_path) -> None:
    path = tmp_path / "relationship-authority.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    expected = exercise_relationship_authority(ledger)
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_sqlite_migrates_verified_v6_head_to_relationship_bundle(tmp_path) -> None:
    path = tmp_path / "relationship-v6-migration.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit(
        [event("event:v6-observation", "ObservationRecorded", {"observation_id": "obs:v6"})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0]
        state = ReducerState.model_validate_json(raw)
        semantic = state.semantic_payload(
            world_id=WORLD,
            world_revision=1,
            reducer_bundle_version="world-v2-reducers.6",
        )
        for key in (
            "relationship_signals",
            "relationship_adjustments",
            "relationship_states",
            "boundaries",
            "actor_authorities",
            "actor_authority_transitions",
            "consumed_actor_root_nonces",
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
        legacy_hash = hashlib.sha256(canonical(semantic).encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ? "
            "WHERE world_id = ?",
            (legacy_state_json(raw), legacy_hash, "world-v2-reducers.6", WORLD),
        )

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project().reducer_bundle_version == "world-v2-reducers.31"
    assert reopened.project() == expected
    reopened.close()


def test_sqlite_rejects_tampered_relationship_checkpoint(tmp_path) -> None:
    path = tmp_path / "relationship-tamper.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    exercise_relationship_authority(ledger)
    ledger.close()

    with sqlite3.connect(path) as connection:
        raw = json.loads(
            connection.execute(
                "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD,)
            ).fetchone()[0]
        )
        raw["relationship_states"][0]["variables"]["trust_bp"] = 9_999
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ? WHERE world_id = ?",
            (canonical(raw), WORLD),
        )

    with pytest.raises(LedgerIntegrityError, match="state hash is invalid"):
        SQLiteWorldLedger(path=path, world_id=WORLD)


def test_proposal_only_changes_checkpoint_state_hash_not_semantic_hash(tmp_path) -> None:
    path = tmp_path / "relationship-proposal-hashes.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit(
        [
            event("event:hash-init", "ObservationRecorded", {"observation_id": "obs:hash"}),
            event(
                "event:hash-operator",
                "OperatorObservationRecorded",
                {"observation_id": "operator:relationship", "observation_hash": EVIDENCE_HASH},
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    semantic_before = ledger.project().semantic_hash
    with sqlite3.connect(path) as connection:
        state_hash_before = connection.execute(
            "SELECT state_hash FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0]

    payload = new_signal_payload(event_ref="event:hash-signal")
    record_proposal(ledger, proposal(payload, transition_kind="signal"))
    assert ledger.project().semantic_hash == semantic_before
    with sqlite3.connect(path) as connection:
        state_hash_after = connection.execute(
            "SELECT state_hash FROM world_v2_heads WHERE world_id = ?", (WORLD,)
        ).fetchone()[0]
    assert state_hash_after != state_hash_before
    ledger.close()
