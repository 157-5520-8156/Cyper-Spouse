from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.fact_accepted_contracts import (
    FactCommitIntentV2,
    FactEvidenceUseV2,
)
from companion_daemon.world_v2.fact_proof_backed_evidence import (
    FactEvidenceResolutionError,
    ProofBackedFactEvidenceResolverV2,
)
from companion_daemon.world_v2.ledger import ObservationEventLocator
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import (
    SQLiteProofBackedObservationReader,
    SQLiteWorldLedger,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WORLD = "world:fact-proof-evidence"


def _commit(ledger: SQLiteWorldLedger, event: WorldEvent) -> None:
    projection = ledger.project()
    ledger.commit(
        (event,),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def _cursor(ledger: SQLiteWorldLedger) -> ProjectionCursor:
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _message(*, observation_id: str) -> WorldEvent:
    payload = {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": observation_id,
        "world_id": WORLD,
        "logical_time": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "trace_id": "trace:fact-proof",
        "causation_id": f"cause:{observation_id}",
        "correlation_id": "correlation:fact-proof",
        "source": "test",
        "source_event_id": f"source:{observation_id}",
        "actor": "user:primary",
        "channel": "chat",
        "payload_ref": f"payload:{observation_id}",
        "payload_hash": "a" * 64,
        "received_at": NOW.isoformat(),
    }
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:{observation_id}",
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user:primary",
        source="test",
        trace_id="trace:fact-proof",
        causation_id=f"cause:{observation_id}",
        correlation_id="correlation:fact-proof",
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded", world_id=WORLD, payload=payload
        )
        or "unreachable",
        payload=payload,
    )


def _operator(*, observation_id: str) -> WorldEvent:
    payload = {"observation_id": observation_id, "observation_hash": "b" * 64}
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:{observation_id}",
        world_id=WORLD,
        event_type="OperatorObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:operator",
        source="test",
        trace_id="trace:fact-proof",
        causation_id=f"cause:{observation_id}",
        correlation_id="correlation:fact-proof",
        idempotency_key=domain_idempotency_key(
            event_type="OperatorObservationRecorded", world_id=WORLD, payload=payload
        )
        or "unreachable",
        payload=payload,
    )


def _intent(*uses: FactEvidenceUseV2) -> FactCommitIntentV2:
    return FactCommitIntentV2(
        subject_ref="user:primary",
        predicate_code="profile.display_name",
        value_ref="value:alice",
        value_hash="sha256:" + "c" * 64,
        assertion_source_ref=uses[0].evidence_ref,
        evidence_uses=uses,
        confidence_bp=9000,
        privacy_class="personal",
    )


def test_resolver_derives_message_evidence_and_assertion_from_proof_backed_event(tmp_path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "message.sqlite3", world_id=WORLD)
    event = _message(observation_id="message:profile")
    _commit(ledger, event)
    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    handle = reader.pin(world_id=WORLD, cursor=_cursor(ledger))
    resolver = ProofBackedFactEvidenceResolverV2(reader=reader)
    locator = ObservationEventLocator.for_message(
        world_id=WORLD,
        observation_id="message:profile",
        source="test",
        source_event_id="source:message:profile",
    )

    resolved = resolver.resolve(
        handle=handle,
        intent=_intent(
            FactEvidenceUseV2(
                evidence_ref="message:profile", purpose="current_fact", anchor=True
            )
        ),
        locators=(locator,),
    )

    assert resolved.evidence_refs[0].ref_id == "message:profile"
    assert resolved.evidence_refs[0].evidence_type == "observed_message"
    assert resolved.evidence_refs[0].claim_purpose == "current_fact"
    assert resolved.evidence_refs[0].source_world_revision == 1
    assert resolved.evidence_refs[0].immutable_hash == event.payload_hash
    assert resolved.assertion_binding.source_kind == "observed_message"
    assert resolved.assertion_binding.actor_ref == "user:primary"
    assert resolved.assertion_binding.channel == "chat"
    assert resolved.assertion_binding.payload_ref == "payload:message:profile"
    assert resolved.assertion_binding.content_payload_hash == "a" * 64
    ledger.close()


def test_resolver_derives_operator_evidence_and_assertion_from_proof_backed_event(tmp_path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "operator.sqlite3", world_id=WORLD)
    # The v2 resolved-evidence contract requires a positive world revision.
    # A normal world already has committed history before an operator Fact
    # observation is admitted.
    _commit(ledger, _message(observation_id="message:before-operator"))
    event = _operator(observation_id="operator:profile")
    _commit(ledger, event)
    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    handle = reader.pin(world_id=WORLD, cursor=_cursor(ledger))
    resolver = ProofBackedFactEvidenceResolverV2(reader=reader)
    locator = ObservationEventLocator.for_operator(
        world_id=WORLD, observation_id="operator:profile"
    )

    resolved = resolver.resolve(
        handle=handle,
        intent=_intent(
            FactEvidenceUseV2(
                evidence_ref="operator:profile", purpose="current_fact", anchor=True
            )
        ),
        locators=(locator,),
    )

    assert resolved.evidence_refs[0].evidence_type == "operator_observation"
    assert resolved.evidence_refs[0].source_world_revision >= 1
    assert resolved.evidence_refs[0].immutable_hash == "b" * 64
    assert resolved.assertion_binding.source_kind == "operator_observation"
    assert resolved.assertion_binding.actor_ref is None
    assert resolved.assertion_binding.channel is None
    assert resolved.assertion_binding.payload_ref is None
    assert resolved.assertion_binding.content_payload_hash == "b" * 64
    ledger.close()


def test_resolver_refuses_authenticated_locator_missing_without_alias_fallback(tmp_path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "missing.sqlite3", world_id=WORLD)
    old = _message(observation_id="message:old")
    _commit(ledger, old)
    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    handle = reader.pin(world_id=WORLD, cursor=_cursor(ledger))
    _commit(ledger, _message(observation_id="message:new"))
    resolver = ProofBackedFactEvidenceResolverV2(reader=reader)
    locator = ObservationEventLocator.for_message(
        world_id=WORLD,
        observation_id="message:new",
        source="test",
        source_event_id="source:message:new",
    )

    with pytest.raises(FactEvidenceResolutionError, match="locator_missing"):
        resolver.resolve(
            handle=handle,
            intent=_intent(
                FactEvidenceUseV2(
                    evidence_ref="message:new", purpose="current_fact", anchor=True
                )
            ),
            locators=(locator,),
        )
    ledger.close()


def test_resolver_requires_exact_one_locator_for_each_intent_evidence_ref(tmp_path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "refs.sqlite3", world_id=WORLD)
    event = _message(observation_id="message:one")
    _commit(ledger, event)
    reader = SQLiteProofBackedObservationReader(ledger=ledger)
    handle = reader.pin(world_id=WORLD, cursor=_cursor(ledger))
    resolver = ProofBackedFactEvidenceResolverV2(reader=reader)
    locator = ObservationEventLocator.for_message(
        world_id=WORLD,
        observation_id="message:one",
        source="test",
        source_event_id="source:message:one",
    )

    with pytest.raises(FactEvidenceResolutionError, match="exactly enumerate"):
        resolver.resolve(
            handle=handle,
            intent=_intent(
                FactEvidenceUseV2(
                    evidence_ref="message:other", purpose="current_fact", anchor=True
                )
            ),
            locators=(locator,),
        )
    ledger.close()
