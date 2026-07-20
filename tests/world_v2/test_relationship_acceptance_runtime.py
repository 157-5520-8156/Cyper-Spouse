from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.relationship_acceptance_runtime import (
    RelationshipAcceptanceError,
    RelationshipAcceptanceRuntime,
    relationship_mutation_event_id,
)
from companion_daemon.world_v2.relationship_events import (
    RelationshipSignalAcceptedPayload,
    relationship_mutation_hash,
)
from companion_daemon.world_v2.schemas import (
    ProjectionCursor,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    RelationshipSignalOrigin,
    RelationshipSignalProjection,
    relationship_signal_fingerprint,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger

from test_relationship_authority import EVIDENCE_HASH, NOW, WORLD, evidence, event


def _cursor(runtime: RelationshipAcceptanceRuntime) -> ProjectionCursor:
    head = runtime.ledger.project()
    return ProjectionCursor(
        world_revision=head.world_revision,
        deliberation_revision=head.deliberation_revision,
        ledger_sequence=head.ledger_sequence,
    )


def _record_ready_signal_proposal(runtime: RelationshipAcceptanceRuntime) -> dict[str, object]:
    ledger = runtime.ledger
    expected_revision = ledger.project().world_revision
    proposal_id = "proposal:relationship:signal:1"
    change_id = "change:relationship:signal:1"
    transition_id = "transition:relationship:signal:1"
    policy_refs = ("policy:relationship-signal-v1",)
    refs = (evidence(),)
    mutation_event_id = relationship_mutation_event_id(
        world_id=WORLD,
        proposal_id=proposal_id,
        transition_id=transition_id,
        event_type="RelationshipSignalAccepted",
    )
    signal = RelationshipSignalProjection(
        signal_id="signal:relationship:1",
        semantic_fingerprint=relationship_signal_fingerprint(
            subject_ref="user:geoff",
            signal_code="felt_heard",
            evidence_refs=refs,
            policy_refs=policy_refs,
        ),
        entity_revision=1,
        subject_ref="user:geoff",
        signal_code="felt_heard",
        confidence_bp=8_000,
        persistence="durable",
        rationale_code="test_signal",
        evidence_refs=refs,
        origin=RelationshipSignalOrigin(
            change_id=change_id,
            transition_id=transition_id,
            policy_refs=policy_refs,
            accepted_event_ref=mutation_event_id,
        ),
        accepted_at=NOW,
    )
    payload: dict[str, object] = {
        "change_id": change_id,
        "transition_id": transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": [item.model_dump(mode="json") for item in refs],
        "policy_refs": list(policy_refs),
        "acceptance_id": "acceptance:relationship:signal:1",
        "proposal_id": proposal_id,
        "evaluated_world_revision": expected_revision,
        "accepted_change_hash": "0" * 64,
        "signal": signal.model_dump(mode="json"),
    }
    payload["accepted_change_hash"] = relationship_mutation_hash(payload)
    assert RelationshipSignalAcceptedPayload.model_validate_json(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    proposal = RelationshipProposalProjection(
        proposal_id=proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:relationship.1",
        transition_kind="signal",
        change_id=change_id,
        transition_id=transition_id,
        evaluated_world_revision=expected_revision,
        expected_entity_revision=0,
        proposed_change_hash=str(payload["accepted_change_hash"]),
        evidence_refs=refs,
        policy_refs=policy_refs,
        proposed_mutation=RelationshipProposedMutation(
            event_type="RelationshipSignalAccepted",
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )
    head = ledger.project()
    ledger.commit(
        [event("event:relationship-proposed", "ProposalRecorded", proposal.model_dump(mode="json"))],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    return payload


def _runtime(*, sqlite_path=None) -> tuple[RelationshipAcceptanceRuntime, dict[str, object]]:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = (
        SQLiteWorldLedger(path=sqlite_path, world_id=WORLD, accepted_batch_issuer=issuer)
        if sqlite_path is not None
        else WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    )
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
    runtime = RelationshipAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    return runtime, _record_ready_signal_proposal(runtime)


def test_relationship_runtime_commits_one_closed_signal_batch() -> None:
    runtime, payload = _runtime()

    result = runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"])),
        actor="worker:relationship",
        source="test:relationship-acceptance",
    )

    projection = runtime.ledger.project()
    assert result.world_revision == projection.world_revision
    assert projection.relationship_proposals == ()
    assert projection.relationship_signals[0].signal_code == "felt_heard"
    acceptance, mutation = (
        runtime.ledger.lookup_event_commit(event_id)[0] for event_id in result.event_ids
    )
    manifest = acceptance.payload()
    assert manifest["manifest_version"] == "relationship-acceptance.1"
    assert manifest["mutation_event_id"] == mutation.event_id
    assert manifest["mutation_payload_hash"] == mutation.payload_hash

    with pytest.raises(RelationshipAcceptanceError, match="proposal_not_persisted"):
        runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"]))


def test_relationship_runtime_replays_from_sqlite(tmp_path) -> None:
    runtime, payload = _runtime(sqlite_path=tmp_path / "relationship.sqlite3")
    runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"])),
        actor="worker:relationship",
        source="test:relationship-acceptance",
    )
    expected = runtime.ledger.project()
    assert runtime.ledger.rebuild() == expected
    runtime.close()

    reopened = SQLiteWorldLedger(path=tmp_path / "relationship.sqlite3", world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()
