from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.relationship_adjustment_acceptance_runtime import (
    RelationshipAdjustmentAcceptanceRuntime,
)
from companion_daemon.world_v2.relationship_adjustment_compiler import (
    RelationshipAdjustmentCompiler,
)
from companion_daemon.world_v2.relationship_adjustment_trigger import (
    relationship_adjustment_trigger_open_event,
)
from companion_daemon.world_v2.relationship_adjustment_worker import (
    RelationshipAdjustmentWorker,
)
from companion_daemon.world_v2.relationship_events import (
    RelationshipSignalAcceptedPayload,
    relationship_mutation_hash,
)
from companion_daemon.world_v2.schemas import (
    ClaimLease,
    ProjectionCursor,
    RelationshipVariableDeltas,
    WorldEvent,
)
from test_relationship_authority import (
    EVIDENCE_HASH,
    WORLD,
    decide_and_mutate,
    event,
    new_signal_payload,
    proposal,
    record_proposal,
)


def _cursor(ledger: WorldLedger) -> ProjectionCursor:
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _accepted_signal_with_suggestion(
    *, suggested_deltas: RelationshipVariableDeltas
) -> RelationshipSignalAcceptedPayload:
    """Build an accepted signal whose model advice is part of its audit hash."""

    original = new_signal_payload()
    raw = original.model_dump(mode="json")
    raw["signal"] = original.signal.model_copy(
        update={"suggested_deltas": suggested_deltas}
    ).model_dump(mode="json")
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    return RelationshipSignalAcceptedPayload.model_validate_json(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _claim_adjustment_trigger(*, ledger: WorldLedger, signal_event: WorldEvent) -> None:
    """Materialize the scheduler-owned, source-bound claim used by the compiler."""

    before_open = ledger.project()
    opened = relationship_adjustment_trigger_open_event(
        signal_event=signal_event, logical_time=before_open.logical_time
    )
    ledger.commit(
        [opened],
        expected_world_revision=before_open.world_revision,
        expected_deliberation_revision=before_open.deliberation_revision,
    )
    projection = ledger.project()
    process = projection.trigger_processes[0]
    assert projection.logical_time is not None
    claimed = process.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id="worker:relationship-adjustment",
                attempt_id="attempt:relationship-adjustment:1",
                acquired_at=projection.logical_time,
                expires_at=projection.logical_time + timedelta(seconds=120),
            ),
            "attempt_ids": ("attempt:relationship-adjustment:1",),
        }
    )
    payload = {"process": claimed.model_dump(mode="json")}
    identity = domain_idempotency_key(
        event_type="TriggerProcessClaimed", world_id=WORLD, payload=payload
    )
    assert identity is not None
    claim = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:relationship-adjustment:claimed:1",
        world_id=WORLD,
        event_type="TriggerProcessClaimed",
        logical_time=projection.logical_time,
        created_at=signal_event.created_at,
        actor="worker:relationship-adjustment",
        source="test:relationship-adjustment-vertical",
        trace_id=signal_event.trace_id,
        causation_id=opened.event_id,
        correlation_id=signal_event.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )
    ledger.commit(
        [claim],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def test_accepted_signal_compiles_adjusts_and_is_consumed_once() -> None:
    """The deterministic vertical carries audited advice to one slow-state update."""

    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit(
        [
            event("event:init", "ObservationRecorded", {"observation_id": "obs:init"}),
            event(
                "event:init-operator",
                "OperatorObservationRecorded",
                {
                    "observation_id": "operator:relationship",
                    "observation_hash": EVIDENCE_HASH,
                },
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    signal = _accepted_signal_with_suggestion(
        suggested_deltas=RelationshipVariableDeltas(trust_bp=320, closeness_bp=-140)
    )
    record_proposal(ledger, proposal(signal, transition_kind="signal"))
    decide_and_mutate(ledger, signal, "RelationshipSignalAccepted")
    signal_event = ledger.lookup_event_commit(signal.signal.origin.accepted_event_ref)
    assert signal_event is not None
    _claim_adjustment_trigger(ledger=ledger, signal_event=signal_event[0])

    compiler = RelationshipAdjustmentCompiler(ledger=ledger)
    compiled = compiler.record(
        world_id=WORLD,
        cursor=_cursor(ledger),
        signal_id=signal.signal.signal_id,
    )

    assert compiled.status == "candidate_recorded"
    assert compiled.typed_proposal_id is not None
    candidate = ledger.project().relationship_proposals
    assert len(candidate) == 1
    runtime = RelationshipAdjustmentAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(
            cursor=_cursor(ledger), proposal_id=compiled.typed_proposal_id
        ),
        actor="worker:relationship-adjustment",
        source="test:relationship-adjustment-vertical",
    )

    projection = ledger.project()
    assert projection.relationship_proposals == ()
    assert len(projection.relationship_adjustments) == 1
    adjustment = projection.relationship_adjustments[0]
    assert adjustment.signal_refs == (signal.signal.signal_id,)
    assert adjustment.proposed_deltas == signal.signal.suggested_deltas
    assert adjustment.accepted_deltas == signal.signal.suggested_deltas
    assert projection.relationship_states[0].variables.trust_bp == 320
    # The proposal preserves the negative suggestion, while the relationship
    # variable itself cannot cross its installed zero floor.
    assert projection.relationship_states[0].variables.closeness_bp == 0
    assert ledger.rebuild() == projection

    repeated = compiler.record(
        world_id=WORLD,
        cursor=_cursor(ledger),
        signal_id=signal.signal.signal_id,
    )
    assert repeated.status == "no_change"
    assert ledger.project() == projection


def test_adjustment_worker_reuses_a_recorded_pending_candidate() -> None:
    """A restart after proposal recording accepts that exact candidate once."""

    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit(
        [
            event("event:init", "ObservationRecorded", {"observation_id": "obs:init"}),
            event(
                "event:init-operator",
                "OperatorObservationRecorded",
                {
                    "observation_id": "operator:relationship",
                    "observation_hash": EVIDENCE_HASH,
                },
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    signal = _accepted_signal_with_suggestion(
        suggested_deltas=RelationshipVariableDeltas(reliability_bp=275)
    )
    record_proposal(ledger, proposal(signal, transition_kind="signal"))
    decide_and_mutate(ledger, signal, "RelationshipSignalAccepted")
    located = ledger.lookup_event_commit(signal.signal.origin.accepted_event_ref)
    assert located is not None
    signal_event = located[0]
    _claim_adjustment_trigger(ledger=ledger, signal_event=signal_event)

    compiler = RelationshipAdjustmentCompiler(ledger=ledger)
    pending = compiler.record(
        world_id=WORLD,
        cursor=_cursor(ledger),
        signal_id=signal.signal.signal_id,
    )
    assert pending.status == "candidate_recorded"
    assert pending.typed_proposal_id is not None

    acceptance = RelationshipAdjustmentAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    worker = RelationshipAdjustmentWorker(
        ledger=ledger,
        compiler=compiler,
        acceptance=acceptance,
        actor="worker:relationship-adjustment",
    )
    result = asyncio.run(
        worker.process(
            world_id=WORLD,
            cursor=_cursor(ledger),
            signal_event=signal_event,
        )
    )

    assert result.status == "accepted"
    assert result.typed_proposal_id == pending.typed_proposal_id
    assert result.compile_commit is None
    assert result.acceptance_commit is not None
    projection = ledger.project()
    assert projection.relationship_proposals == ()
    assert len(projection.relationship_adjustments) == 1
    assert projection.relationship_adjustments[0].signal_refs == (signal.signal.signal_id,)
