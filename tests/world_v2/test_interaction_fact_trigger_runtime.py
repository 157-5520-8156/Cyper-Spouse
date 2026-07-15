from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.fact_draft_adapter import FactObservationProposalAdapter
from companion_daemon.world_v2.fact_memory_candidate_lifecycle import FactMemoryCandidateLifecycle
from companion_daemon.world_v2.fact_memory_draft import FactMemoryRetentionDraft
from companion_daemon.world_v2.fact_trigger import interaction_fact_trigger_event
from companion_daemon.world_v2.fact_v2_acceptance_runtime import FactV2AcceptanceRuntime
from companion_daemon.world_v2.interaction_fact_trigger_runtime import (
    InteractionFactTriggerRuntime,
)
from companion_daemon.world_v2.schemas import Observation, WorldEvent
from companion_daemon.world_v2.schemas import MEMORY_SALIENCE_MATRIX_DIGEST, MemorySalienceVector
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)
WORLD_ID = "world:interaction-fact"


def _observation() -> tuple[Observation, WorldEvent]:
    text = "我最近很喜欢喝乌龙茶。"
    observation = Observation(
        schema_version="world-v2.1",
        observation_id="observation:interaction-fact:1",
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-fact",
        causation_id="cause:interaction-fact",
        correlation_id="correlation:interaction-fact",
        source="test:interaction-fact",
        source_event_id="source:interaction-fact:1",
        actor="user:interaction-fact",
        channel="test",
        payload_ref="payload:interaction-fact:1",
        payload_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        received_at=NOW,
    )
    payload = observation.model_dump(mode="json")
    return observation, WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-fact:observation:1",
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded", world_id=WORLD_ID, payload=payload
        )
        or "unreachable",
        payload=payload,
    )


class _FactChat:
    model = "test-fact"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert "乌龙茶" in messages[1]["content"]
        assert temperature == 0.1
        return json.dumps(
            {
                "retain": True,
                "predicate_code": "preference.likes",
                "value": "乌龙茶",
                "privacy_class": "personal",
                "confidence": 8600,
                "rationale": "Explicit durable preference.",
            }
        )


class _InvalidFactChat:
    model = "test-invalid-fact"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.1
        return "not-json"


@pytest.mark.asyncio
async def test_fact_trigger_accepts_one_source_bound_fact_and_completes(tmp_path) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    trigger = interaction_fact_trigger_event(
        observation=observation, observation_event=observation_event
    )
    ledger.commit((trigger,), expected_world_revision=1, expected_deliberation_revision=0)
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_FactChat()),
        owner_id="worker:interaction-fact",
    )

    result = await runtime.drain_one()

    assert result.status == "processed"
    assert result.work_status == "accepted"
    projection = ledger.project()
    assert projection.facts[0].values.subject_ref == observation.actor
    assert projection.facts[0].values.assertion_binding.source_ref == observation.observation_id
    assert projection.trigger_processes[0].state == "terminal"
    assert ledger.rebuild() == projection
    ledger.close()


@pytest.mark.asyncio
async def test_invalid_fact_model_output_is_terminal_and_has_no_world_effect(tmp_path) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-invalid.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    trigger = interaction_fact_trigger_event(
        observation=observation, observation_event=observation_event
    )
    ledger.commit((trigger,), expected_world_revision=1, expected_deliberation_revision=0)
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_InvalidFactChat()),
        owner_id="worker:interaction-fact",
    )

    result = await runtime.drain_one()

    assert result.work_status == "no_change"
    assert ledger.project().facts == ()
    assert ledger.project().trigger_processes[0].state == "terminal"
    ledger.close()


@pytest.mark.asyncio
async def test_accepted_fact_becomes_an_active_source_bound_memory_candidate(tmp_path) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-memory.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    trigger = interaction_fact_trigger_event(
        observation=observation, observation_event=observation_event
    )
    ledger.commit((trigger,), expected_world_revision=1, expected_deliberation_revision=0)
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_FactChat()),
        owner_id="worker:interaction-fact",
    )
    assert (await runtime.drain_one()).work_status == "accepted"
    before = ledger.project()
    fact = before.facts[0]
    transition = before.fact_transitions[-1]
    stored = ledger.lookup_event_commit(fact.origin.accepted_event_ref)
    assert stored is not None
    fact_event, fact_commit = stored
    draft = FactMemoryRetentionDraft(
        cue_kind="future_utility",
        retention_rationales=("future_utility",),
        salience=MemorySalienceVector(
            autobiographical_relevance_bp=6500,
            relationship_relevance_bp=2000,
            emotional_residue_bp=0,
            unfinished_business_bp=0,
            recurrence_bp=1000,
            novelty_bp=3000,
            future_utility_bp=7600,
            world_continuity_bp=1000,
            matrix_digest=MEMORY_SALIENCE_MATRIX_DIGEST,
        ),
    )

    candidate = FactMemoryCandidateLifecycle(
        ledger=ledger,
        actor="worker:interaction-memory",
        source="test:interaction-memory",
    ).accept(
        fact=fact,
        transition=transition,
        fact_event=fact_event,
        fact_world_revision=fact_commit.world_revision,
        draft=draft,
        logical_time=NOW,
        created_at=NOW,
        trace_id=observation.trace_id,
        correlation_id=observation.correlation_id,
    )

    assert candidate is not None and candidate.values.status == "active"
    projected = ledger.project()
    assert projected.memory_candidates == (candidate,)
    assert projected.memory_candidates[0].values.source_bindings[0].authority_event_ref == fact_event.event_id
    assert ledger.rebuild() == projected
    ledger.close()
