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
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger_context_resolver import context_capsule_compiler_from_ledger
from companion_daemon.world_v2.interaction_fact_trigger_runtime import (
    InteractionFactTriggerRuntime,
)
from companion_daemon.world_v2.schemas import Observation, WorldEvent
from companion_daemon.world_v2.schemas import MEMORY_SALIENCE_MATRIX_DIGEST, MemorySalienceVector
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 19, 0, tzinfo=UTC)
WORLD_ID = "world:interaction-fact"


def _observation() -> tuple[Observation, WorldEvent]:
    text = "我叫丁奥轩，最近很喜欢喝桂花乌龙。"
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

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        self.calls += 1
        assert "丁奥轩" in messages[1]["content"] and "桂花乌龙" in messages[1]["content"]
        assert temperature == 0.1
        return json.dumps(
            {
                "retain": True,
                "predicate_code": "preference.likes",
                "value": "桂花乌龙",
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
    started = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-fact:started",
        world_id=WORLD_ID,
        event_type="WorldStarted",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test:interaction-fact",
        trace_id="trace:interaction-fact:started",
        causation_id="cause:interaction-fact:started",
        correlation_id="correlation:interaction-fact",
        idempotency_key="identity:interaction-fact:started",
        payload={},
    )
    ledger.commit((started,), expected_world_revision=0, expected_deliberation_revision=0)
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=1, expected_deliberation_revision=0)
    trigger = interaction_fact_trigger_event(
        observation=observation, observation_event=observation_event
    )
    ledger.commit((trigger,), expected_world_revision=2, expected_deliberation_revision=0)
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

    # The semantic source must remain recallable after the bounded recent
    # dialogue window no longer contains the message which established it.
    latest_event = observation_event
    for index in range(13):
        text = f"这是随后第 {index + 1} 条普通消息。"
        filler = Observation(
            schema_version="world-v2.1",
            observation_id=f"observation:interaction-fact:filler:{index}",
            world_id=WORLD_ID,
            logical_time=NOW,
            created_at=NOW,
            trace_id=f"trace:interaction-fact:filler:{index}",
            causation_id=f"cause:interaction-fact:filler:{index}",
            correlation_id="correlation:interaction-fact",
            source="test:interaction-fact",
            source_event_id=f"source:interaction-fact:filler:{index}",
            actor=observation.actor,
            channel="test",
            payload_ref=f"payload:interaction-fact:filler:{index}",
            payload_hash=hashlib.sha256(text.encode()).hexdigest(),
            text=text,
            received_at=NOW,
        )
        filler_payload = filler.model_dump(mode="json")
        latest_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:interaction-fact:filler:{index}",
            world_id=WORLD_ID,
            event_type="ObservationRecorded",
            logical_time=NOW,
            created_at=NOW,
            actor=filler.actor,
            source=filler.source,
            trace_id=filler.trace_id,
            causation_id=filler.causation_id,
            correlation_id=filler.correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ObservationRecorded",
                world_id=WORLD_ID,
                payload=filler_payload,
            ) or "unreachable",
            payload=filler_payload,
        )
        cursor = ledger.project()
        ledger.commit(
            (latest_event,),
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
    projection = ledger.project()
    capsule = context_capsule_compiler_from_ledger(ledger=ledger).compile(
        query_from_projection(
            projection,
            actor_ref="actor:companion",
            trigger_ref=latest_event.event_id,
        )
    )
    assert "丁奥轩" not in capsule.recent_dialogue.model_content_json
    assert "丁奥轩" in capsule.relevant_facts.model_content_json
    assert "桂花乌龙" in capsule.relevant_facts.model_content_json
    recalled = json.loads(capsule.relevant_facts.items[0].payload_json)
    assert recalled["predicate_code"] == "preference.likes"
    assert recalled["source_excerpt"] == "我叫丁奥轩，最近很喜欢喝桂花乌龙。"
    assert len(capsule.relevant_facts.items[0].source_bindings) == 2

    class SourceOverrideLedger:
        def __init__(self, override):  # type: ignore[no-untyped-def]
            self._override = override

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(ledger, name)

        def lookup_event_commit(self, event_id):  # type: ignore[no-untyped-def]
            if event_id == observation_event.event_id:
                return self._override
            return ledger.lookup_event_commit(event_id)

    missing = context_capsule_compiler_from_ledger(
        ledger=SourceOverrideLedger(None)  # type: ignore[arg-type]
    ).compile(query_from_projection(
        projection, actor_ref="actor:companion", trigger_ref=latest_event.event_id
    ))
    assert missing.relevant_facts.items == ()

    forged_observation = observation.model_copy(update={
        "text": "伪造的语义文本。",
        "payload_hash": hashlib.sha256("伪造的语义文本。".encode()).hexdigest(),
    })
    forged_payload = forged_observation.model_dump(mode="json")
    forged_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=observation_event.event_id,
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=forged_observation.actor,
        source=forged_observation.source,
        trace_id=forged_observation.trace_id,
        causation_id=forged_observation.causation_id,
        correlation_id=forged_observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded", world_id=WORLD_ID, payload=forged_payload
        ) or "unreachable",
        payload=forged_payload,
    )
    original_commit = ledger.lookup_event_commit(observation_event.event_id)
    assert original_commit is not None
    forged = context_capsule_compiler_from_ledger(
        ledger=SourceOverrideLedger((forged_event, original_commit[1]))  # type: ignore[arg-type]
    ).compile(query_from_projection(
        projection, actor_ref="actor:companion", trigger_ref=latest_event.event_id
    ))
    assert forged.relevant_facts.items == ()
    ledger.close()


@pytest.mark.asyncio
async def test_fact_trigger_joins_existing_audit_after_crash_without_reasking_model(
    tmp_path, monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-audit-recovery.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (interaction_fact_trigger_event(
            observation=observation, observation_event=observation_event
        ),),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    acceptance = FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer)
    chat = _FactChat()
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=acceptance,
        adapter=FactObservationProposalAdapter(model=chat),
        owner_id="worker:interaction-fact",
    )
    original = FactV2AcceptanceRuntime.pin_proposal

    def crash_after_audit(_self, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after proposal audit")

    monkeypatch.setattr(FactV2AcceptanceRuntime, "pin_proposal", crash_after_audit)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await runtime.drain_one()
    assert chat.calls == 1
    assert len(ledger.project().fact_commit_proposal_audits_v2) == 1

    monkeypatch.setattr(FactV2AcceptanceRuntime, "pin_proposal", original)
    recovered = await runtime.drain_one()

    assert recovered.work_status == "accepted"
    assert chat.calls == 1
    assert len(ledger.project().facts) == 1
    assert ledger.project().trigger_processes[0].state == "terminal"
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


class _SingleSlotChat:
    """Answer a single-cardinality predicate whose value follows the message."""

    model = "test-single-slot"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        value = "杭州" if "杭州" in messages[1]["content"] else "上海"
        return json.dumps(
            {
                "retain": True,
                "predicate_code": "location.home",
                "value": value,
                "privacy_class": "personal",
                "confidence": 9000,
                "rationale": "The user states where they live.",
            },
            ensure_ascii=False,
        )


def _home_observation(index: int, text: str) -> tuple[Observation, WorldEvent]:
    observation = Observation(
        schema_version="world-v2.1",
        observation_id=f"observation:interaction-fact:home:{index}",
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        trace_id=f"trace:interaction-fact:home:{index}",
        causation_id=f"cause:interaction-fact:home:{index}",
        correlation_id="correlation:interaction-fact",
        source="test:interaction-fact",
        source_event_id=f"source:interaction-fact:home:{index}",
        actor="user:interaction-fact",
        channel="test",
        payload_ref=f"payload:interaction-fact:home:{index}",
        payload_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        received_at=NOW,
    )
    payload = observation.model_dump(mode="json")
    return observation, WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:interaction-fact:home:{index}",
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


@pytest.mark.asyncio
async def test_conflicting_single_slot_fact_completes_instead_of_poisoning(tmp_path) -> None:
    """A durably rejected acceptance must consume its trigger, not retry forever.

    The commit-only fact lane cannot correct a single-cardinality slot; a
    second, different value would otherwise fail acceptance on every future
    background pass and starve the queue behind it.
    """

    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-conflict.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_SingleSlotChat()),
        owner_id="worker:interaction-fact",
    )
    first, first_event = _home_observation(1, "我住在杭州。")
    ledger.commit((first_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (interaction_fact_trigger_event(observation=first, observation_event=first_event),),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert (await runtime.drain_one()).work_status == "accepted"

    second, second_event = _home_observation(2, "我现在住上海了。")
    cursor = ledger.project()
    ledger.commit(
        (second_event,),
        expected_world_revision=cursor.world_revision,
        expected_deliberation_revision=cursor.deliberation_revision,
    )
    cursor = ledger.project()
    ledger.commit(
        (interaction_fact_trigger_event(observation=second, observation_event=second_event),),
        expected_world_revision=cursor.world_revision,
        expected_deliberation_revision=cursor.deliberation_revision,
    )

    result = await runtime.drain_one()

    assert result.status == "processed"
    assert result.work_status == "no_change"
    projection = ledger.project()
    assert len(projection.facts) == 1  # The original slot value is untouched.
    assert all(item.state == "terminal" for item in projection.trigger_processes)
    assert (await runtime.drain_one()).status == "idle"
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
