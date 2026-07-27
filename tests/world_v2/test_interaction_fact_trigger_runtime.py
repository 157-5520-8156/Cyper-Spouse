from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.fact_draft_adapter import FactObservationProposalAdapter
from companion_daemon.world_v2.fact_correction_lifecycle import FactCorrectionLifecycle
from companion_daemon.world_v2.fact_memory_candidate_lifecycle import FactMemoryCandidateLifecycle
from companion_daemon.world_v2.fact_memory_decision import (
    FactMemoryDecisionRecordedPayload,
    canonical_fact_memory_decision_json,
    fact_memory_decision_hash,
)
from companion_daemon.world_v2.fact_memory_draft import FactMemoryRetentionDraft
from companion_daemon.world_v2.fact_memory_draft import FactMemoryDraftAdapter
from companion_daemon.world_v2.fact_trigger import (
    fact_memory_decision_event_id,
    interaction_fact_decision_event_id,
    interaction_fact_failure_event_id,
    interaction_fact_trigger_event,
)
from companion_daemon.world_v2.fact_v2_acceptance_runtime import FactV2AcceptanceRuntime
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.ledger_context_resolver import (
    context_capsule_compiler_from_ledger,
    fact_recall_items,
    historical_fact_recall_items,
)
from companion_daemon.world_v2.interaction_fact_trigger_runtime import (
    FactTriggerRunResult,
    InteractionFactTriggerRuntime,
)
from companion_daemon.world_v2.interaction_fact_decision import (
    InteractionFactDecisionRecordedPayload,
    canonical_interaction_fact_decision_json,
    interaction_fact_decision_hash,
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


def _advance_clock(
    ledger: SQLiteWorldLedger,
    *,
    to: datetime,
    suffix: str,
) -> None:
    projection = ledger.project()
    assert projection.logical_time is not None
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:interaction-fact:retry-clock:{suffix}",
        world_id=WORLD_ID,
        event_type="ClockAdvanced",
        logical_time=to,
        created_at=to,
        actor="clock:test",
        source="test:interaction-fact-retry",
        trace_id=f"trace:interaction-fact:retry-clock:{suffix}",
        causation_id=f"cause:interaction-fact:retry-clock:{suffix}",
        correlation_id="correlation:interaction-fact",
        idempotency_key=f"identity:interaction-fact:retry-clock:{suffix}",
        payload={
            "logical_time_from": projection.logical_time.isoformat(),
            "logical_time_to": to.isoformat(),
        },
    )
    ledger.commit(
        (event,),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
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


@pytest.mark.asyncio
async def test_claim_rejects_a_clock_advance_after_projection_instead_of_reducing_stale_time(
    tmp_path,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-stale-claim.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (
            interaction_fact_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_FactChat()),
        owner_id="worker:interaction-fact",
    )
    stale = ledger.project()
    process = next(
        item for item in stale.trigger_processes if item.process_kind == "interaction_fact"
    )
    _advance_clock(ledger, to=NOW + timedelta(minutes=1), suffix="claim-race")

    with pytest.raises(ConcurrencyConflict, match="stale projection cursor"):
        await runtime._claim_or_reclaim(
            process=process,
            source_event=observation_event,
            projection=stale,
        )

    projected = ledger.project()
    assert projected.logical_time == NOW + timedelta(minutes=1)
    assert next(
        item for item in projected.trigger_processes if item.trigger_id == process.trigger_id
    ).state == "open"
    ledger.close()


class _InvalidFactChat:
    model = "test-invalid-fact"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.1
        return "not-json"


class _RecoveringMemoryChat:
    model = "test-recovering-memory"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        self.calls += 1
        assert temperature == 0.15
        if self.calls <= 2:
            return "not-json"
        return json.dumps(
            {
                "retain": True,
                "cue_kind": "future_utility",
                "retention_rationales": ["future_utility"],
                "salience": {
                    "autobiographical_relevance_bp": 6500,
                    "relationship_relevance_bp": 2000,
                    "emotional_residue_bp": 0,
                    "unfinished_business_bp": 0,
                    "recurrence_bp": 1000,
                    "novelty_bp": 3000,
                    "future_utility_bp": 7600,
                    "world_continuity_bp": 1000,
                },
            }
        )


class _RetainingMemoryChat:
    model = "test-retaining-memory"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.15
        self.calls += 1
        corrected = self.calls > 1
        return json.dumps(
            {
                "retain": True,
                "cue_kind": (
                    "future_utility" if corrected else "world_continuity"
                ),
                "retention_rationales": [
                    "future_utility" if corrected else "world_continuity"
                ],
                "salience": {
                    "autobiographical_relevance_bp": 9100 if corrected else 7000,
                    "relationship_relevance_bp": 2500,
                    "emotional_residue_bp": 0,
                    "unfinished_business_bp": 0,
                    "recurrence_bp": 1500,
                    "novelty_bp": 3500,
                    "future_utility_bp": 9300 if corrected else 6000,
                    "world_continuity_bp": 4200 if corrected else 8000,
                },
            }
        )


class _ChangingMemoryChat:
    model = "test-changing-memory"

    def __init__(self, *, retain_first: bool = True) -> None:
        self.calls = 0
        self.retain_first = retain_first

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.15
        self.calls += 1
        if not self.retain_first or self.calls > 1:
            return '{"retain":false}'
        return json.dumps(
            {
                "retain": True,
                "cue_kind": "world_continuity",
                "retention_rationales": ["world_continuity"],
                "salience": {
                    "autobiographical_relevance_bp": 7000,
                    "relationship_relevance_bp": 2500,
                    "emotional_residue_bp": 0,
                    "unfinished_business_bp": 0,
                    "recurrence_bp": 1500,
                    "novelty_bp": 3500,
                    "future_utility_bp": 6000,
                    "world_continuity_bp": 8000,
                },
            }
        )


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
        text = (
            "我之前喜欢喝什么来着？"
            if index == 12
            else f"这是随后第 {index + 1} 条普通消息。"
        )
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
async def test_fact_decision_retries_unrelated_cursor_advance_without_reasking_model(
    tmp_path,
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-decision-cas-retry.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (
            interaction_fact_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    chat = _FactChat()
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=chat),
        owner_id="worker:interaction-fact",
    )
    original = ledger.commit_at_cursor
    advanced = False

    def advance_once(events, *, expected_cursor, commit_id=None):  # type: ignore[no-untyped-def]
        nonlocal advanced
        if (
            not advanced
            and any(item.event_type == "InteractionFactDecisionRecorded" for item in events)
        ):
            advanced = True
            projected = ledger.project_at(expected_cursor)
            assert projected.logical_time is not None
            at = projected.logical_time + timedelta(minutes=1)
            clock = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:interaction-fact:decision-cas-clock",
                world_id=WORLD_ID,
                event_type="ClockAdvanced",
                logical_time=at,
                created_at=at,
                actor="clock:test",
                source="test:interaction-fact-decision-cas",
                trace_id=observation.trace_id,
                causation_id=observation_event.event_id,
                correlation_id=observation.correlation_id,
                idempotency_key="identity:interaction-fact:decision-cas-clock",
                payload={
                    "logical_time_from": projected.logical_time.isoformat(),
                    "logical_time_to": at.isoformat(),
                },
            )
            original((clock,), expected_cursor=expected_cursor)
            raise ConcurrencyConflict("simulated unrelated clock advance")
        return original(events, expected_cursor=expected_cursor, commit_id=commit_id)

    monkeypatch.setattr(ledger, "commit_at_cursor", advance_once)
    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    assert chat.calls == 1
    assert len(ledger.project().facts) == 1
    ledger.close()


@pytest.mark.asyncio
async def test_fact_decision_cas_loser_executes_the_ledger_winner(
    tmp_path,
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-decision-winner.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (
            interaction_fact_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    chat = _FactChat()
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=chat),
        owner_id="worker:interaction-fact",
    )
    original = ledger.commit_at_cursor
    raced = False

    def install_no_change_winner(events, *, expected_cursor, commit_id=None):  # type: ignore[no-untyped-def]
        nonlocal raced
        decision_event = next(
            (
                item
                for item in events
                if item.event_type == "InteractionFactDecisionRecorded"
            ),
            None,
        )
        if not raced and decision_event is not None:
            raced = True
            local = InteractionFactDecisionRecordedPayload.model_validate_json(
                decision_event.payload_json
            )
            decision_json = canonical_interaction_fact_decision_json(
                {"decision": "no_change"}
            )
            winner_payload = local.model_copy(
                update={
                    "decision_kind": "no_change",
                    "decision_json": decision_json,
                    "decision_hash": interaction_fact_decision_hash(decision_json),
                }
            ).model_dump(mode="json")
            winner = WorldEvent.from_payload(
                schema_version=decision_event.schema_version,
                event_id=decision_event.event_id,
                world_id=decision_event.world_id,
                event_type=decision_event.event_type,
                logical_time=decision_event.logical_time,
                created_at=decision_event.created_at,
                actor=decision_event.actor,
                source=decision_event.source,
                trace_id=decision_event.trace_id,
                causation_id=decision_event.causation_id,
                correlation_id=decision_event.correlation_id,
                idempotency_key=domain_idempotency_key(
                    event_type=decision_event.event_type,
                    world_id=decision_event.world_id,
                    payload=winner_payload,
                )
                or "unreachable",
                payload=winner_payload,
            )
            original((winner,), expected_cursor=expected_cursor)
            raise ConcurrencyConflict("simulated competing model decision")
        return original(events, expected_cursor=expected_cursor, commit_id=commit_id)

    monkeypatch.setattr(ledger, "commit_at_cursor", install_no_change_winner)
    result = await runtime.drain_one()

    assert result.work_status == "no_change"
    assert chat.calls == 1
    assert ledger.project().facts == ()
    projection = ledger.project()
    trigger_id = projection.trigger_processes[0].trigger_id
    fact_context_hash = hashlib.sha256(b"[]").hexdigest()
    recorded = ledger.lookup_event_commit(
        interaction_fact_decision_event_id(
            trigger_id=trigger_id,
            fact_context_hash=fact_context_hash,
        )
    )
    assert recorded is not None
    assert recorded[0].payload()["decision_kind"] == "no_change"
    ledger.close()


@pytest.mark.asyncio
async def test_invalid_fact_model_output_is_audited_for_retry_without_world_effect(
    tmp_path,
) -> None:
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

    assert result.work_status == "technical_failure"
    assert ledger.project().facts == ()
    process = ledger.project().trigger_processes[0]
    assert process.state == "claimed"
    assert process.claim_lease is not None
    failure = ledger.lookup_event_commit(
        interaction_fact_failure_event_id(
            trigger_id=process.trigger_id,
            attempt_id=process.claim_lease.attempt_id,
        )
    )
    assert failure is not None
    assert failure[0].event_type == "InteractionFactTechnicalFailureRecorded"
    assert await runtime.drain_one() == FactTriggerRunResult(
        trigger_id="", status="idle"
    )

    first_payload = failure[0].payload()
    first_retry_at = datetime.fromisoformat(str(first_payload["next_retry_at"]))
    _advance_clock(ledger, to=first_retry_at, suffix="second")
    second_failure = await runtime.drain_one()
    assert second_failure.work_status == "technical_failure"
    second_process = ledger.project().trigger_processes[0]
    assert second_process.claim_lease is not None
    second_event = ledger.lookup_event_commit(
        interaction_fact_failure_event_id(
            trigger_id=second_process.trigger_id,
            attempt_id=second_process.claim_lease.attempt_id,
        )
    )
    assert second_event is not None
    second_payload = second_event[0].payload()
    second_failed_at = datetime.fromisoformat(str(second_payload["failed_at"]))
    second_retry_at = datetime.fromisoformat(str(second_payload["next_retry_at"]))
    assert second_retry_at - second_failed_at == timedelta(minutes=30)

    _advance_clock(ledger, to=second_retry_at, suffix="third")
    third_failure = await runtime.drain_one()
    assert third_failure.work_status == "technical_failure"
    third_process = ledger.project().trigger_processes[0]
    assert third_process.claim_lease is not None
    third_event = ledger.lookup_event_commit(
        interaction_fact_failure_event_id(
            trigger_id=third_process.trigger_id,
            attempt_id=third_process.claim_lease.attempt_id,
        )
    )
    assert third_event is not None
    third_payload = third_event[0].payload()
    third_failed_at = datetime.fromisoformat(str(third_payload["failed_at"]))
    third_retry_at = datetime.fromisoformat(str(third_payload["next_retry_at"]))
    assert third_retry_at - third_failed_at == timedelta(minutes=120)
    ledger.close()


class _SingleSlotChat:
    """Answer a single-cardinality predicate whose value follows the message."""

    model = "test-single-slot"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        current_text = json.loads(messages[1]["content"])["text"]
        value = "杭州" if "杭州" in current_text else "上海"
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


class _WithdrawalChat:
    model = "test-single-slot-withdrawal"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        request = json.loads(messages[1]["content"])
        current_text = request["text"]
        if "不住" in current_text:
            if any(
                "上海" in item["source_text"]
                for item in request["current_single_facts"]
            ):
                return '{"retain":false}'
            return json.dumps(
                {
                    "decision": "withdraw",
                    "predicate_code": "location.home",
                    "confidence": 9_200,
                    "rationale": "The user explicitly retracts the current residence.",
                }
            )
        return json.dumps(
            {
                "retain": True,
                "predicate_code": "location.home",
                "value": "杭州",
                "privacy_class": "personal",
                "confidence": 9_000,
                "rationale": "The user states where they live.",
            }
        )


class _ResidenceChat:
    model = "test-residence-ordering"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        request = json.loads(messages[1]["content"])
        current_text = request["text"]
        if "不住" in current_text:
            if any(
                "上海" in item["source_text"]
                for item in request["current_single_facts"]
            ):
                return '{"retain":false}'
            return json.dumps(
                {
                    "decision": "withdraw",
                    "predicate_code": "location.home",
                    "confidence": 9_200,
                    "rationale": "The user explicitly retracts the current residence.",
                }
            )
        value = "上海" if "上海" in current_text else "杭州"
        return json.dumps(
            {
                "retain": True,
                "predicate_code": "location.home",
                "value": value,
                "privacy_class": "personal",
                "confidence": 9_000,
                "rationale": "The user states where they live.",
            }
        )


class _LegacyAwareResidenceChat:
    """Do not let an older statement overwrite a newer exact source."""

    model = "test-legacy-aware-residence"

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[dict[str, object]] = []

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        request = json.loads(messages[1]["content"])
        self.requests.append(request)
        current_text = request["text"]
        if (
            "上海" in current_text
            and any(
                "北京" in item["source_text"]
                for item in request["current_single_facts"]
            )
        ):
            return json.dumps(
                {
                    "retain": True,
                    "predicate_code": "location.home",
                    "value": "上海",
                    "privacy_class": "private",
                    "confidence": 8_100,
                    "rationale": "The older statement is retained after reviewing newer context.",
                },
                ensure_ascii=False,
            )
        value = next(
            place
            for place in ("杭州", "上海", "北京")
            if place in current_text
        )
        return json.dumps(
            {
                "retain": True,
                "predicate_code": "location.home",
                "value": value,
                "privacy_class": "personal",
                "confidence": 9_000,
                "rationale": "The user states where they live.",
            },
            ensure_ascii=False,
        )


class _NoChangeChat:
    model = "test-no-fact"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del messages, temperature
        self.calls += 1
        return '{"retain":false}'


def _home_observation(
    index: int,
    text: str,
    *,
    at: datetime = NOW,
) -> tuple[Observation, WorldEvent]:
    observation = Observation(
        schema_version="world-v2.1",
        observation_id=f"observation:interaction-fact:home:{index}",
        world_id=WORLD_ID,
        logical_time=at,
        created_at=at,
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
        received_at=at,
    )
    payload = observation.model_dump(mode="json")
    return observation, WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:interaction-fact:home:{index}",
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=at,
        created_at=at,
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
        memory_adapter=FactMemoryDraftAdapter(model=_RetainingMemoryChat()),
        memory_lifecycle=FactMemoryCandidateLifecycle(
            ledger=ledger,
            actor="worker:interaction-memory",
            source="test:interaction-memory",
        ),
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
    assert result.work_status == "accepted"
    projection = ledger.project()
    assert len(projection.facts) == 1
    assert projection.facts[0].entity_revision == 2
    assert projection.facts[0].values.value_hash == hashlib.sha256("上海".encode()).hexdigest()
    assert projection.fact_transitions[-1].operation == "correct"
    assert len(projection.memory_candidates) == 1
    assert projection.memory_candidates[0].entity_revision == 3
    assert (
        projection.memory_candidates[0].values.source_bindings[0].authority_event_ref
        == projection.facts[0].origin.accepted_event_ref
    )
    memory_values = projection.memory_candidates[0].values
    assert memory_values.summary_ref == (
        "summary:source:" + projection.facts[0].origin.accepted_event_ref
    )
    assert memory_values.cue_kind == "future_utility"
    assert memory_values.retention_rationales == ("future_utility",)
    assert memory_values.salience.autobiographical_relevance_bp == 9100
    assert memory_values.salience.future_utility_bp == 9300
    current = fact_recall_items(
        ledger=ledger,
        projection=projection,
        facts=(projection.facts[0],),
    )
    historical = historical_fact_recall_items(
        ledger=ledger,
        projection=projection,
        subject_refs=frozenset({second.actor}),
    )
    assert len(current) == 1 and "上海" in current[0].source_excerpt
    assert len(historical) == 1 and "杭州" in historical[0].source_excerpt
    assert historical[0].valid_to == projection.fact_transitions[-1].accepted_at
    assert all(item.state == "terminal" for item in projection.trigger_processes)
    assert (await runtime.drain_one()).status == "idle"
    ledger.close()


@pytest.mark.asyncio
async def test_model_can_withdraw_a_single_fact_without_inventing_a_replacement(tmp_path) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-withdraw.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_WithdrawalChat()),
        owner_id="worker:interaction-fact",
    )
    first, first_event = _home_observation(11, "我住在杭州。")
    ledger.commit((first_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (interaction_fact_trigger_event(observation=first, observation_event=first_event),),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert (await runtime.drain_one()).work_status == "accepted"

    second, second_event = _home_observation(12, "我已经不住杭州了。")
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

    assert (await runtime.drain_one()).work_status == "accepted"
    projection = ledger.project()
    assert projection.facts[0].values.status == "withdrawn"
    assert projection.fact_transitions[-1].operation == "withdraw"
    assert fact_recall_items(
        ledger=ledger,
        projection=projection,
        facts=(),
    ) == ()
    historical = historical_fact_recall_items(
        ledger=ledger,
        projection=projection,
        subject_refs=frozenset({second.actor}),
    )
    assert len(historical) == 1 and "杭州" in historical[0].source_excerpt
    ledger.close()


@pytest.mark.asyncio
async def test_withdrawal_rejoins_durable_model_decision_after_crash(
    tmp_path,
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-withdraw-decision-recovery.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    chat = _WithdrawalChat()
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=chat),
        owner_id="worker:interaction-fact",
    )
    first, first_event = _home_observation(31, "我住在杭州。")
    ledger.commit((first_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (interaction_fact_trigger_event(observation=first, observation_event=first_event),),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert (await runtime.drain_one()).work_status == "accepted"

    second, second_event = _home_observation(32, "我已经不住杭州了。")
    cursor = ledger.project()
    ledger.commit(
        (
            second_event,
            interaction_fact_trigger_event(
                observation=second,
                observation_event=second_event,
            ),
        ),
        expected_world_revision=cursor.world_revision,
        expected_deliberation_revision=cursor.deliberation_revision,
    )
    original_withdraw = FactCorrectionLifecycle.withdraw

    def crash_after_decision(_self, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after durable withdrawal decision")

    monkeypatch.setattr(FactCorrectionLifecycle, "withdraw", crash_after_decision)
    with pytest.raises(RuntimeError, match="durable withdrawal decision"):
        await runtime.drain_one()
    calls_after_decision = chat.calls
    trigger_id = next(
        item.trigger_id
        for item in ledger.project().trigger_processes
        if item.source_evidence_ref == second.observation_id
    )
    projection = ledger.project()
    fact_context = runtime._single_fact_authority_context(
        projection,
        subject_ref=second.actor,
    )
    fact_context_hash = hashlib.sha256(
        json.dumps(
            fact_context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    recorded = ledger.lookup_event_commit(
        interaction_fact_decision_event_id(
            trigger_id=trigger_id,
            fact_context_hash=fact_context_hash,
        )
    )
    assert recorded is not None
    assert recorded[0].payload()["decision_kind"] == "withdraw"

    monkeypatch.setattr(FactCorrectionLifecycle, "withdraw", original_withdraw)
    assert (await runtime.drain_one()).work_status == "accepted"
    assert chat.calls == calls_after_decision
    assert ledger.project().facts[0].values.status == "withdrawn"
    ledger.close()


@pytest.mark.asyncio
async def test_older_withdrawal_is_reconsidered_against_newer_fact_context(
    tmp_path,
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-stale-withdrawal.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    chat = _ResidenceChat()
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=chat),
        owner_id="worker:interaction-fact",
    )
    first, first_event = _home_observation(41, "我住在杭州。")
    ledger.commit((first_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (interaction_fact_trigger_event(observation=first, observation_event=first_event),),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert (await runtime.drain_one()).work_status == "accepted"

    old, old_event = _home_observation(42, "我已经不住杭州了。")
    cursor = ledger.project()
    ledger.commit(
        (
            old_event,
            interaction_fact_trigger_event(observation=old, observation_event=old_event),
        ),
        expected_world_revision=cursor.world_revision,
        expected_deliberation_revision=cursor.deliberation_revision,
    )
    original_withdraw = FactCorrectionLifecycle.withdraw

    def crash_after_old_decision(_self, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after old withdrawal decision")

    monkeypatch.setattr(FactCorrectionLifecycle, "withdraw", crash_after_old_decision)
    with pytest.raises(RuntimeError, match="old withdrawal decision"):
        await runtime.drain_one()
    calls_after_old_decision = chat.calls

    newer, newer_event = _home_observation(43, "我现在住上海了。")
    cursor = ledger.project()
    ledger.commit(
        (
            newer_event,
            interaction_fact_trigger_event(
                observation=newer,
                observation_event=newer_event,
            ),
        ),
        expected_world_revision=cursor.world_revision,
        expected_deliberation_revision=cursor.deliberation_revision,
    )
    monkeypatch.setattr(FactCorrectionLifecycle, "withdraw", original_withdraw)

    assert (await runtime.drain_one()).work_status == "accepted"
    assert (await runtime.drain_one()).work_status == "no_change"
    projection = ledger.project()
    assert projection.facts[0].values.status == "active"
    assert projection.facts[0].values.value_hash == hashlib.sha256(
        "上海".encode()
    ).hexdigest()
    assert chat.calls == calls_after_old_decision + 2
    stale_process = next(
        item
        for item in projection.trigger_processes
        if item.source_evidence_ref == old.observation_id
    )
    assert stale_process.runtime_outcome_ref is not None
    assert stale_process.runtime_outcome_ref.endswith("no-change")
    ledger.close()


@pytest.mark.asyncio
async def test_legacy_fact_proposal_is_reconsidered_after_fact_context_changes(
    tmp_path,
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-legacy-context.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    chat = _LegacyAwareResidenceChat()
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=chat),
        owner_id="worker:interaction-fact",
    )
    first, first_event = _home_observation(61, "我住在杭州。")
    ledger.commit((first_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (interaction_fact_trigger_event(observation=first, observation_event=first_event),),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert (await runtime.drain_one()).work_status == "accepted"

    older, older_event = _home_observation(62, "我现在住上海了。")
    cursor = ledger.project()
    ledger.commit(
        (
            older_event,
            interaction_fact_trigger_event(
                observation=older,
                observation_event=older_event,
            ),
        ),
        expected_world_revision=cursor.world_revision,
        expected_deliberation_revision=cursor.deliberation_revision,
    )
    record_decision = runtime._record_decision
    correct = FactCorrectionLifecycle.correct

    async def omit_new_decision_audit(**kwargs):  # type: ignore[no-untyped-def]
        # Simulate a proposal produced by the pre-InteractionFactDecision
        # runtime: the Fact proposal audit is durable, but no context epoch is.
        return kwargs["decision"]

    def crash_after_legacy_proposal(_self, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated old runtime crash after proposal audit")

    monkeypatch.setattr(runtime, "_record_decision", omit_new_decision_audit)
    monkeypatch.setattr(FactCorrectionLifecycle, "correct", crash_after_legacy_proposal)
    with pytest.raises(RuntimeError, match="old runtime crash"):
        await runtime.drain_one()
    assert chat.calls == 2

    monkeypatch.setattr(runtime, "_record_decision", record_decision)
    monkeypatch.setattr(FactCorrectionLifecycle, "correct", correct)
    _advance_clock(
        ledger,
        to=NOW + timedelta(minutes=1),
        suffix="legacy-newer-observation",
    )
    newer, newer_event = _home_observation(
        63,
        "我现在住北京了。",
        at=NOW + timedelta(minutes=1),
    )
    cursor = ledger.project()
    ledger.commit(
        (
            newer_event,
            interaction_fact_trigger_event(
                observation=newer,
                observation_event=newer_event,
            ),
        ),
        expected_world_revision=cursor.world_revision,
        expected_deliberation_revision=cursor.deliberation_revision,
    )

    assert (await runtime.drain_one()).work_status == "accepted"
    assert chat.requests[-1]["observation_logical_time"] == (
        NOW + timedelta(minutes=1)
    ).isoformat()
    monkeypatch.setattr(FactCorrectionLifecycle, "correct", crash_after_legacy_proposal)
    with pytest.raises(RuntimeError, match="old runtime crash"):
        await runtime.drain_one()
    recovery_request = chat.requests[-1]
    assert recovery_request["observation_logical_time"] == NOW.isoformat()
    current_sources = recovery_request["current_single_facts"]
    assert isinstance(current_sources, list)
    assert current_sources[0]["fact_entity_revision"] == 2
    assert current_sources[0]["fact_accepted_event_ref"]
    assert current_sources[0]["fact_accepted_world_revision"]
    assert current_sources[0]["fact_updated_at"]
    assert current_sources[0]["source_observation_logical_time"] == (
        NOW + timedelta(minutes=1)
    ).isoformat()
    assert chat.calls == 4

    monkeypatch.setattr(FactCorrectionLifecycle, "correct", correct)
    _advance_clock(
        ledger,
        to=NOW + timedelta(minutes=2),
        suffix="legacy-contextual-audit-recovery",
    )
    # The role decision is already durable.  The new World epoch must re-key
    # its normalized proposal without asking the model again.
    assert (await runtime.drain_one()).work_status == "accepted"
    projection = ledger.project()
    assert chat.calls == 4
    assert projection.facts[0].values.value_hash == hashlib.sha256(
        "上海".encode()
    ).hexdigest()
    legacy_audits = tuple(
        item
        for item in projection.fact_commit_proposal_audits_v2
        if json.loads(item.proposal_json)["trigger_ref"] == older_event.event_id
    )
    assert len(legacy_audits) == 3
    assert len({item.proposal_id for item in legacy_audits}) == 3
    old_process = next(
        item
        for item in projection.trigger_processes
        if item.source_evidence_ref == older.observation_id
    )
    assert old_process.runtime_outcome_ref is not None
    assert ":corrected:" in old_process.runtime_outcome_ref
    ledger.close()


@pytest.mark.asyncio
async def test_no_change_rejoins_durable_model_decision_after_crash(
    tmp_path,
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-no-change-decision-recovery.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (
            interaction_fact_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    chat = _NoChangeChat()
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=chat),
        owner_id="worker:interaction-fact",
    )
    original_complete = runtime._complete

    async def crash_after_decision(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after durable no-change decision")

    monkeypatch.setattr(runtime, "_complete", crash_after_decision)
    with pytest.raises(RuntimeError, match="durable no-change decision"):
        await runtime.drain_one()
    assert chat.calls == 1

    monkeypatch.setattr(runtime, "_complete", original_complete)
    recovered = await runtime.drain_one()
    assert recovered.work_status == "no_change"
    assert chat.calls == 1
    assert ledger.project().trigger_processes[0].state == "terminal"
    ledger.close()


@pytest.mark.asyncio
async def test_correction_recovers_when_clock_advances_after_its_proposal(
    tmp_path,
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-correction-recovery.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_SingleSlotChat()),
        owner_id="worker:interaction-fact",
    )
    first, first_event = _home_observation(21, "我住在杭州。")
    ledger.commit((first_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (interaction_fact_trigger_event(observation=first, observation_event=first_event),),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert (await runtime.drain_one()).work_status == "accepted"

    second, second_event = _home_observation(22, "我现在住上海了。")
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

    original_commit_at_cursor = ledger.commit_at_cursor

    def interrupt_after_proposal(events, *, expected_cursor, commit_id=None):  # type: ignore[no-untyped-def]
        if any(item.event_type == "FactCorrected" for item in events):
            raise RuntimeError("simulated process loss after durable correction proposal")
        return original_commit_at_cursor(
            events,
            expected_cursor=expected_cursor,
            commit_id=commit_id,
        )

    monkeypatch.setattr(ledger, "commit_at_cursor", interrupt_after_proposal)
    with pytest.raises(RuntimeError, match="simulated process loss"):
        await runtime.drain_one()
    interrupted = ledger.project()
    old_proposal = next(
        item for item in interrupted.fact_proposals if item.transition_kind == "correct"
    )

    monkeypatch.setattr(ledger, "commit_at_cursor", original_commit_at_cursor)
    _advance_clock(ledger, to=NOW + timedelta(minutes=1), suffix="correction-recovery")

    assert (await runtime.drain_one()).work_status == "accepted"
    recovered = ledger.project()
    decisions = {
        item.proposal_id: item.status for item in recovered.acceptance_decisions
    }
    assert decisions[old_proposal.proposal_id] == "stale"
    assert any(
        proposal_id.startswith("proposal:fact-correction:")
        and proposal_id != old_proposal.proposal_id
        and status == "accepted"
        for proposal_id, status in decisions.items()
    )
    assert recovered.facts[0].entity_revision == 2
    assert recovered.facts[0].values.value_hash == hashlib.sha256("上海".encode()).hexdigest()
    ledger.close()


@pytest.mark.asyncio
async def test_accepted_fact_memory_resumes_a_pending_candidate_after_crash(
    tmp_path,
    monkeypatch,
) -> None:
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
    # Classification happened at NOW, but another background writer may move
    # World time before this two-stage lifecycle acquires the commit lock.
    advanced_time = NOW + timedelta(minutes=2)
    _advance_clock(ledger, to=advanced_time, suffix="memory-lifecycle-race")

    interrupted = FactMemoryCandidateLifecycle(
        ledger=ledger,
        actor="worker:interaction-memory",
        source="test:interaction-memory",
    )
    record_transition = interrupted._record_and_accept

    def crash_before_acceptance(**kwargs):  # type: ignore[no-untyped-def]
        if kwargs["operation"] == "accept":
            raise RuntimeError("simulated process crash after durable open")
        return record_transition(**kwargs)

    monkeypatch.setattr(interrupted, "_record_and_accept", crash_before_acceptance)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        interrupted.accept(
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
    pending = ledger.project().memory_candidates[0]
    assert pending.values.status == "pending"
    assert pending.opened_at == advanced_time

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
    assert candidate.updated_at == advanced_time
    projected = ledger.project()
    assert projected.memory_candidates == (candidate,)
    assert projected.memory_candidates[0].values.source_bindings[0].authority_event_ref == fact_event.event_id
    assert ledger.rebuild() == projected
    ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("supersede_fact", [False, True])
async def test_fact_memory_settles_proposal_only_crash_window(
    tmp_path,
    monkeypatch,
    supersede_fact: bool,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / f"fact-memory-proposal-crash-{supersede_fact}.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(
            ledger=ledger,
            batch_issuer=issuer,
        ),
        adapter=FactObservationProposalAdapter(model=_SingleSlotChat()),
        owner_id="worker:interaction-fact",
    )
    observation, observation_event = _home_observation(71, "我住在杭州。")
    ledger.commit(
        (observation_event,),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        (
            interaction_fact_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert (await runtime.drain_one()).work_status == "accepted"
    projection = ledger.project()
    fact = projection.facts[0]
    transition = projection.fact_transitions[-1]
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
    lifecycle = FactMemoryCandidateLifecycle(
        ledger=ledger,
        actor="worker:interaction-memory",
        source="test:interaction-memory",
    )
    commit = ledger.commit

    def crash_after_proposal(
        events,
        *,
        expected_world_revision,
        expected_deliberation_revision,
        commit_id=None,
    ):  # type: ignore[no-untyped-def]
        if any(item.event_type == "MemoryCandidateOpened" for item in events):
            raise RuntimeError("simulated crash after memory proposal")
        return commit(
            events,
            expected_world_revision=expected_world_revision,
            expected_deliberation_revision=expected_deliberation_revision,
            commit_id=commit_id,
        )

    monkeypatch.setattr(ledger, "commit", crash_after_proposal)
    with pytest.raises(RuntimeError, match="after memory proposal"):
        lifecycle.accept(
            fact=fact,
            transition=transition,
            fact_event=fact_event,
            fact_world_revision=fact_commit.world_revision,
            draft=draft,
            logical_time=projection.logical_time or NOW,
            created_at=projection.logical_time or NOW,
            trace_id=observation.trace_id,
            correlation_id=observation.correlation_id,
        )
    interrupted = ledger.project()
    orphan = interrupted.memory_candidate_proposals[0]
    assert interrupted.memory_candidates == ()
    assert not any(
        item.proposal_id == orphan.proposal_id
        for item in interrupted.acceptance_decisions
    )
    monkeypatch.setattr(ledger, "commit", commit)

    if supersede_fact:
        newer, newer_event = _home_observation(72, "我现在住上海了。")
        cursor = ledger.project()
        ledger.commit(
            (
                newer_event,
                interaction_fact_trigger_event(
                    observation=newer,
                    observation_event=newer_event,
                ),
            ),
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
        assert (await runtime.drain_one()).work_status == "accepted"
    else:
        _advance_clock(
            ledger,
            to=(projection.logical_time or NOW) + timedelta(minutes=2),
            suffix="memory-proposal-crash",
        )

    recovered = lifecycle.accept(
        fact=fact,
        transition=transition,
        fact_event=fact_event,
        fact_world_revision=fact_commit.world_revision,
        draft=draft,
        logical_time=projection.logical_time or NOW,
        created_at=projection.logical_time or NOW,
        trace_id=observation.trace_id,
        correlation_id=observation.correlation_id,
    )
    final = ledger.project()
    old_decision = next(
        item
        for item in final.acceptance_decisions
        if item.proposal_id == orphan.proposal_id
    )
    assert old_decision.status == "stale"
    if supersede_fact:
        assert recovered is None
        assert final.memory_candidates == ()
    else:
        assert recovered is not None
        assert recovered.values.status == "active"
        accepted = tuple(
            item
            for item in final.acceptance_decisions
            if item.status == "accepted"
            and item.proposal_id.startswith("proposal:transition:memory:")
        )
        assert len(accepted) == 2
        assert all(item.proposal_id != orphan.proposal_id for item in accepted)
    assert ledger.rebuild() == final
    ledger.close()


@pytest.mark.asyncio
async def test_fact_memory_retain_decision_is_rejoined_after_crash(
    tmp_path,
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-memory-decision-recovery.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (
            interaction_fact_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    memory_chat = _ChangingMemoryChat()
    lifecycle = FactMemoryCandidateLifecycle(
        ledger=ledger,
        actor="worker:interaction-memory",
        source="test:interaction-memory",
    )
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_FactChat()),
        memory_adapter=FactMemoryDraftAdapter(model=memory_chat),
        memory_lifecycle=lifecycle,
        owner_id="worker:interaction-fact",
    )
    original_accept = lifecycle.accept

    def crash_after_memory_decision(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after durable memory decision")

    monkeypatch.setattr(lifecycle, "accept", crash_after_memory_decision)
    with pytest.raises(RuntimeError, match="durable memory decision"):
        await runtime.drain_one()
    interrupted = ledger.project()
    fact = interrupted.facts[0]
    trigger_id = interrupted.trigger_processes[0].trigger_id
    assert ledger.lookup_event_commit(
        fact_memory_decision_event_id(
            trigger_id=trigger_id,
            fact_authority_event_ref=fact.origin.accepted_event_ref,
        )
    ) is not None
    assert memory_chat.calls == 1

    monkeypatch.setattr(lifecycle, "accept", original_accept)
    assert (await runtime.drain_one()).work_status == "no_change"
    assert memory_chat.calls == 1
    assert ledger.project().memory_candidates[0].values.status == "active"
    ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("local_retain", [True, False])
async def test_fact_memory_decision_cas_loser_uses_ledger_winner(
    tmp_path,
    monkeypatch,
    local_retain: bool,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / f"fact-memory-cas-winner-{local_retain}.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (
            interaction_fact_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    memory_chat = _ChangingMemoryChat(retain_first=local_retain)
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_FactChat()),
        memory_adapter=FactMemoryDraftAdapter(model=memory_chat),
        memory_lifecycle=FactMemoryCandidateLifecycle(
            ledger=ledger,
            actor="worker:interaction-memory",
            source="test:interaction-memory",
        ),
        owner_id="worker:interaction-fact",
    )
    original = ledger.commit_at_cursor

    def install_competing_winner(events, *, expected_cursor, commit_id=None):  # type: ignore[no-untyped-def]
        decision_event = next(
            (
                item
                for item in events
                if item.event_type == "FactMemoryDecisionRecorded"
            ),
            None,
        )
        if decision_event is None:
            return original(
                events,
                expected_cursor=expected_cursor,
                commit_id=commit_id,
            )
        local = FactMemoryDecisionRecordedPayload.model_validate_json(
            decision_event.payload_json
        )
        if local_retain:
            winner_kind = "no_change"
            winner_value: object = {"decision": "no_change"}
        else:
            winner_kind = "retain"
            winner_value = FactMemoryRetentionDraft(
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
            ).model_dump(mode="json")
        decision_json = canonical_fact_memory_decision_json(winner_value)
        winner_payload = local.model_copy(
            update={
                "decision_kind": winner_kind,
                "decision_json": decision_json,
                "decision_hash": fact_memory_decision_hash(decision_json),
            }
        ).model_dump(mode="json")
        winner = WorldEvent.from_payload(
            schema_version=decision_event.schema_version,
            event_id=decision_event.event_id,
            world_id=decision_event.world_id,
            event_type=decision_event.event_type,
            logical_time=decision_event.logical_time,
            created_at=decision_event.created_at,
            actor=decision_event.actor,
            source=decision_event.source,
            trace_id=decision_event.trace_id,
            causation_id=decision_event.causation_id,
            correlation_id=decision_event.correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type=decision_event.event_type,
                world_id=decision_event.world_id,
                payload=winner_payload,
            )
            or "unreachable",
            payload=winner_payload,
        )
        original((winner,), expected_cursor=expected_cursor)
        raise ConcurrencyConflict("simulated competing Fact-memory decision")

    monkeypatch.setattr(ledger, "commit_at_cursor", install_competing_winner)
    result = await runtime.drain_one()
    projection = ledger.project()

    assert result.work_status == "accepted"
    assert memory_chat.calls == 1
    assert bool(projection.memory_candidates) is (not local_retain)
    ledger.close()


@pytest.mark.asyncio
async def test_fact_memory_no_change_decision_is_rejoined_after_crash(
    tmp_path,
    monkeypatch,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-memory-no-change-recovery.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (
            interaction_fact_trigger_event(
                observation=observation,
                observation_event=observation_event,
            ),
        ),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    memory_chat = _ChangingMemoryChat(retain_first=False)
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_FactChat()),
        memory_adapter=FactMemoryDraftAdapter(model=memory_chat),
        memory_lifecycle=FactMemoryCandidateLifecycle(
            ledger=ledger,
            actor="worker:interaction-memory",
            source="test:interaction-memory",
        ),
        owner_id="worker:interaction-fact",
    )
    original_complete = runtime._complete

    async def crash_after_memory_no_change(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated crash after memory no-change")

    monkeypatch.setattr(runtime, "_complete", crash_after_memory_no_change)
    with pytest.raises(RuntimeError, match="memory no-change"):
        await runtime.drain_one()
    assert memory_chat.calls == 1

    monkeypatch.setattr(runtime, "_complete", original_complete)
    assert (await runtime.drain_one()).work_status == "no_change"
    assert memory_chat.calls == 1
    assert ledger.project().memory_candidates == ()
    ledger.close()


@pytest.mark.asyncio
async def test_corrected_fact_not_retained_does_not_let_system_forget_old_memory(
    tmp_path,
) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-memory-correction-no-retain.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    memory_chat = _ChangingMemoryChat()
    lifecycle = FactMemoryCandidateLifecycle(
        ledger=ledger,
        actor="worker:interaction-memory",
        source="test:interaction-memory",
    )
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_SingleSlotChat()),
        memory_adapter=FactMemoryDraftAdapter(model=memory_chat),
        memory_lifecycle=lifecycle,
        owner_id="worker:interaction-fact",
    )
    first, first_event = _home_observation(51, "我住在杭州。")
    ledger.commit((first_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (interaction_fact_trigger_event(observation=first, observation_event=first_event),),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    assert (await runtime.drain_one()).work_status == "accepted"
    first_projection = ledger.project()
    assert first_projection.memory_candidates[0].values.status == "active"
    old_fact = first_projection.facts[0]
    old_transition = first_projection.fact_transitions[-1]
    old_stored = ledger.lookup_event_commit(old_fact.origin.accepted_event_ref)
    assert old_stored is not None

    second, second_event = _home_observation(52, "我现在住上海了。")
    cursor = ledger.project()
    ledger.commit(
        (
            second_event,
            interaction_fact_trigger_event(
                observation=second,
                observation_event=second_event,
            ),
        ),
        expected_world_revision=cursor.world_revision,
        expected_deliberation_revision=cursor.deliberation_revision,
    )
    assert (await runtime.drain_one()).work_status == "accepted"
    projection = ledger.project()
    assert memory_chat.calls == 2
    assert projection.memory_candidates[0].values.status == "active"
    assert projection.memory_candidates[0].entity_revision == 2
    assert projection.memory_candidate_transitions[-1].operation == "accept"
    transition_count = len(projection.memory_candidate_transitions)
    assert lifecycle.accept(
        fact=old_fact,
        transition=old_transition,
        fact_event=old_stored[0],
        fact_world_revision=old_stored[1].world_revision,
        draft=FactMemoryRetentionDraft(
            cue_kind=projection.memory_candidates[0].values.cue_kind,
            retention_rationales=(
                projection.memory_candidates[0].values.retention_rationales
            ),
            salience=projection.memory_candidates[0].values.salience,
        ),
        logical_time=projection.logical_time or NOW,
        created_at=projection.logical_time or NOW,
        trace_id=first.trace_id,
        correlation_id=first.correlation_id,
    ) is None
    assert len(ledger.project().memory_candidate_transitions) == transition_count
    ledger.close()


@pytest.mark.asyncio
async def test_memory_technical_failure_preserves_fact_trigger_for_retry(tmp_path) -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=tmp_path / "interaction-fact-memory-retry.sqlite3",
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    observation, observation_event = _observation()
    ledger.commit((observation_event,), expected_world_revision=0, expected_deliberation_revision=0)
    ledger.commit(
        (interaction_fact_trigger_event(
            observation=observation,
            observation_event=observation_event,
        ),),
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    later = NOW + timedelta(minutes=10)
    clock = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-fact:memory-retry:clock",
        world_id=WORLD_ID,
        event_type="ClockAdvanced",
        logical_time=later,
        created_at=later,
        actor="clock:test",
        source="test:interaction-memory",
        trace_id="trace:interaction-fact:memory-retry:clock",
        causation_id=observation_event.event_id,
        correlation_id=observation.correlation_id,
        idempotency_key="identity:interaction-fact:memory-retry:clock",
        payload={
            "logical_time_from": NOW.isoformat(),
            "logical_time_to": later.isoformat(),
        },
    )
    before_clock = ledger.project()
    ledger.commit(
        (clock,),
        expected_world_revision=before_clock.world_revision,
        expected_deliberation_revision=before_clock.deliberation_revision,
    )
    memory_chat = _RecoveringMemoryChat()
    runtime = InteractionFactTriggerRuntime(
        ledger=ledger,
        acceptance=FactV2AcceptanceRuntime.compose(ledger=ledger, batch_issuer=issuer),
        adapter=FactObservationProposalAdapter(model=_FactChat()),
        memory_adapter=FactMemoryDraftAdapter(model=memory_chat),
        memory_lifecycle=FactMemoryCandidateLifecycle(
            ledger=ledger,
            actor="worker:interaction-memory",
            source="test:interaction-memory",
        ),
        owner_id="worker:interaction-fact",
    )

    first_failure = await runtime.drain_one()
    failed = ledger.project()
    assert first_failure.work_status == "technical_failure"
    assert len(failed.facts) == 1
    assert failed.memory_candidates == ()
    assert failed.trigger_processes[0].state == "claimed"

    # A new Fact opportunity is allowed to pass the failed retry instead of
    # being starved behind the oldest claimed trigger.
    second = observation.model_copy(
        update={
            "observation_id": "observation:interaction-fact:2",
            "source_event_id": "source:interaction-fact:2",
            "payload_ref": "payload:interaction-fact:2",
            "trace_id": "trace:interaction-fact:2",
            "causation_id": observation_event.event_id,
            "logical_time": later,
            "created_at": later,
            "received_at": later,
        }
    )
    second_payload = second.model_dump(mode="json")
    second_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-fact:observation:2",
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=later,
        created_at=later,
        actor=second.actor,
        source=second.source,
        trace_id=second.trace_id,
        causation_id=second.causation_id,
        correlation_id=second.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded",
            world_id=WORLD_ID,
            payload=second_payload,
        )
        or "unreachable",
        payload=second_payload,
    )
    cursor = ledger.project()
    ledger.commit(
        (
            second_event,
            interaction_fact_trigger_event(
                observation=second,
                observation_event=second_event,
            ),
        ),
        expected_world_revision=cursor.world_revision,
        expected_deliberation_revision=cursor.deliberation_revision,
    )
    passed = await runtime.drain_one()
    assert passed.work_status == "no_change"
    states = {
        item.source_evidence_ref: item.state
        for item in ledger.project().trigger_processes
        if item.process_kind == "interaction_fact"
    }
    assert states[observation.observation_id] == "claimed"
    assert states[second.observation_id] == "terminal"

    waiting = await runtime.drain_one()
    assert waiting.status == "idle"

    retry_at = later + timedelta(minutes=10)
    before_retry = ledger.project()
    retry_clock = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-fact:memory-retry:clock:2",
        world_id=WORLD_ID,
        event_type="ClockAdvanced",
        logical_time=retry_at,
        created_at=retry_at,
        actor="clock:test",
        source="test:interaction-memory",
        trace_id="trace:interaction-fact:memory-retry:clock:2",
        causation_id=second_event.event_id,
        correlation_id=observation.correlation_id,
        idempotency_key="identity:interaction-fact:memory-retry:clock:2",
        payload={
            "logical_time_from": later.isoformat(),
            "logical_time_to": retry_at.isoformat(),
        },
    )
    ledger.commit(
        (retry_clock,),
        expected_world_revision=before_retry.world_revision,
        expected_deliberation_revision=before_retry.deliberation_revision,
    )
    recovered = await runtime.drain_one()
    projection = ledger.project()

    assert recovered.work_status == "no_change"
    assert len(projection.facts) == 1
    assert len(projection.memory_candidates) == 1
    assert all(
        item.state == "terminal"
        for item in projection.trigger_processes
        if item.process_kind == "interaction_fact"
    )
    ledger.close()
