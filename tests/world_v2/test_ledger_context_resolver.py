from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel

import companion_daemon.world_v2.ledger as ledger_module
import companion_daemon.world_v2.sqlite_ledger as sqlite_ledger_module
from companion_daemon.world_v2.context_capsule import ContextCapsuleCompiler
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger import LedgerPort, WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    LedgerProjectionContextResolver,
    _bounded_domain_items,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.situation_compiler import SituationCompiler
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from test_appraisal_authority import (
    accepted_payload,
    authorized_batch,
    commit as commit_appraisal,
    event as appraisal_event,
    prepare_claimed_interaction,
    record_proposal,
)
from test_character_core_authority import initialized_character_ledger


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _event(world_id: str, event_id: str = "event:start") -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type="WorldStarted",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:context-ledger",
        causation_id="cause:context-ledger",
        correlation_id="correlation:context-ledger",
        idempotency_key=f"identity:{event_id}",
        payload={},
    )


def _observation(world_id: str, index: int) -> WorldEvent:
    event_id = f"event:observation:{index}"
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:context-ledger",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:context-ledger",
        idempotency_key=f"identity:{event_id}",
        payload={"observation_id": f"observation:{index}"},
    )


def _empty_ledger(kind=WorldLedger.in_memory, *, world_id="world:context-empty"):
    ledger = kind(world_id=world_id)
    ledger.commit([_event(world_id)], expected_world_revision=0, expected_deliberation_revision=0)
    return ledger


class CountingLedger:
    def __init__(self, delegate: LedgerPort) -> None:
        self.delegate = delegate
        self.project_at_calls = 0
        self.resolved_batches: list[tuple[str, ...]] = []
        self.lookups: list[str] = []

    @property
    def world_id(self):
        return self.delegate.world_id

    @property
    def blocks_event_loop(self):
        return self.delegate.blocks_event_loop

    def project_at(self, cursor):
        self.project_at_calls += 1
        return self.delegate.project_at(cursor)

    def resolve_committed_event_refs(self, event_ids, *, at_world_revision):
        self.resolved_batches.append(tuple(event_ids))
        return self.delegate.resolve_committed_event_refs(
            event_ids, at_world_revision=at_world_revision
        )

    def resolve_initial_world_event_ref(self, *, at_world_revision):
        return self.delegate.resolve_initial_world_event_ref(at_world_revision=at_world_revision)

    def lookup_event_commit(self, event_id):
        self.lookups.append(event_id)
        return self.delegate.lookup_event_commit(event_id)

    def project(self):
        return self.delegate.project()

    def commit(
        self, events, *, expected_world_revision, expected_deliberation_revision, commit_id=None
    ):
        return self.delegate.commit(
            events,
            expected_world_revision=expected_world_revision,
            expected_deliberation_revision=expected_deliberation_revision,
            commit_id=commit_id,
        )


def _compiler(ledger: LedgerPort) -> ContextCapsuleCompiler:
    return context_capsule_compiler_from_ledger(ledger=ledger)


def test_real_ledger_resolves_situation_core_and_authoritative_empty_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _, core = initialized_character_ledger(monkeypatch)
    counted = CountingLedger(ledger)
    projection = ledger.project()
    query = query_from_projection(
        projection, actor_ref="actor:companion", trigger_ref="event:incoming"
    )

    first = _compiler(counted).compile(query)
    second = _compiler(counted).compile(query)

    assert first.model_dump_json() == second.model_dump_json()
    assert first.current_situation.availability == "available"
    assert first.character_core.items[0].item_ref == core.core_id
    assert first.relevant_facts.availability == "available"
    assert first.relevant_facts.items == ()
    assert first.open_threads.availability == "available"
    assert first.active_memory_candidates.availability == "available"
    assert first.relationship_slice.availability == "unavailable"
    assert first.private_impressions.availability == "unavailable"
    assert counted.project_at_calls == 2
    # Situation and CharacterCore request only their consumed refs; no full replay API exists.
    assert all(len(batch) <= 1 for batch in counted.resolved_batches)
    assert set(counted.lookups) == {core.origin.accepted_event_ref}


def test_active_appraisal_hypotheses_are_source_bound_into_the_next_capsule() -> None:
    from companion_daemon.world_v2.appraisal_events import appraisal_mutation_hash

    ledger = WorldLedger.in_memory(world_id="world-v2-appraisal-authority")
    ledger.commit(
        [appraisal_event("event:appraisal-world-started", "WorldStarted", {})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger, trigger, evidence = prepare_claimed_interaction(ledger)
    payload = accepted_payload(ledger, trigger, evidence)
    appraisal = payload["appraisal"]
    assert isinstance(appraisal, dict)
    appraisal["subject_ref"] = "actor:companion"
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)
    record_proposal(ledger, trigger, evidence, payload)
    commit_appraisal(ledger, authorized_batch(trigger, payload))

    capsule = _compiler(ledger).compile(
        query_from_projection(
            ledger.project(), actor_ref="actor:companion", trigger_ref="event:next-turn"
        )
    )

    assert capsule.appraisals.availability == "available"
    assert len(capsule.appraisals.items) == 1
    assert '"meaning":"disappointment"' in capsule.appraisals.items[0].payload_json
    assert "message-event:1" in {
        binding.ref for binding in capsule.appraisals.items[0].source_bindings
    }


def test_query_snapshot_or_cursor_swap_is_rejected_before_resolution() -> None:
    ledger = _empty_ledger()
    projection = ledger.project()
    query = query_from_projection(
        projection, actor_ref="actor:companion", trigger_ref="event:incoming"
    )
    resolver = LedgerProjectionContextResolver(
        ledger=ledger, situation_compiler=SituationCompiler()
    )

    with pytest.raises(ValueError, match="exact Context query cursor"):
        resolver.resolve(query.model_copy(update={"snapshot_hash": "f" * 64}))
    with pytest.raises(ValueError, match="exact Context query cursor"):
        resolver.resolve(query.model_copy(update={"snapshot_id": "projection:swapped"}))
    with pytest.raises(ValueError):
        resolver.resolve(query.model_copy(update={"ledger_sequence": 0}))


class TamperedResolverLedger(CountingLedger):
    def __init__(self, delegate: LedgerPort, target_ref: str) -> None:
        super().__init__(delegate)
        self.target_ref = target_ref

    def resolve_committed_event_refs(self, event_ids, *, at_world_revision):
        resolved = super().resolve_committed_event_refs(
            event_ids, at_world_revision=at_world_revision
        )
        return {
            ref: (
                value.model_copy(update={"payload_hash": "f" * 64})
                if ref == self.target_ref
                else value
            )
            for ref, value in resolved.items()
        }


def test_tampered_committed_origin_hash_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _, core = initialized_character_ledger(monkeypatch)
    tampered = TamperedResolverLedger(ledger, core.origin.accepted_event_ref)
    query = query_from_projection(
        ledger.project(), actor_ref="actor:companion", trigger_ref="event:incoming"
    )

    with pytest.raises(ValueError, match="contradicts its committed event"):
        _compiler(tampered).compile(query)


class TamperedEventTypeLedger(CountingLedger):
    def __init__(self, delegate: LedgerPort, target_ref: str) -> None:
        super().__init__(delegate)
        self.target_ref = target_ref

    def resolve_committed_event_refs(self, event_ids, *, at_world_revision):
        resolved = super().resolve_committed_event_refs(
            event_ids, at_world_revision=at_world_revision
        )
        return {
            ref: (
                value.model_copy(update={"event_type": "WorldOccurrenceSettled"})
                if ref == self.target_ref
                else value
            )
            for ref, value in resolved.items()
        }


def test_tampered_committed_event_type_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger, _, core = initialized_character_ledger(monkeypatch)
    tampered = TamperedEventTypeLedger(ledger, core.origin.accepted_event_ref)
    query = query_from_projection(
        ledger.project(), actor_ref="actor:companion", trigger_ref="event:incoming"
    )

    with pytest.raises(ValueError, match="contradicts its committed event"):
        _compiler(tampered).compile(query)


def test_sqlite_reopen_replays_identical_capsule_bytes(tmp_path: Path) -> None:
    path = tmp_path / "context-resolver.sqlite3"
    world_id = "world:context-sqlite"
    ledger = SQLiteWorldLedger(path=path, world_id=world_id)
    _empty_ledger(lambda *, world_id: ledger, world_id=world_id)
    projection = ledger.project()
    query = query_from_projection(
        projection, actor_ref="actor:companion", trigger_ref="event:incoming"
    )
    before = _compiler(ledger).compile(query).model_dump_json()
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=world_id)
    after = _compiler(reopened).compile(query).model_dump_json()
    reopened.close()

    assert after == before


class _RankValues(BaseModel):
    confidence_bp: int


class _RankedFact(BaseModel):
    fact_id: str
    values: _RankValues
    updated_at: datetime


def test_selection_is_bounded_and_deterministic_before_authority_lookup() -> None:
    candidates = tuple(
        _RankedFact(
            fact_id=f"fact:{index:04d}",
            values=_RankValues(confidence_bp=index),
            updated_at=NOW,
        )
        for index in range(300)
    )

    selected = _bounded_domain_items("relevant_facts", candidates, NOW)

    assert selected is not None
    assert len(selected) == 256
    # Fixed-point rounding creates ties, resolved by ascending stable ID.
    assert selected[0].fact_id == "fact:0298"
    assert selected[1].fact_id == "fact:0299"
    assert selected[-1].fact_id == "fact:0043"
    assert _bounded_domain_items("relevant_facts", candidates * 14, NOW) is None


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_head_cursor_never_invokes_historical_reducer_replay(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world_id = f"world:context-no-replay:{kind}"
    if kind == "memory":
        ledger: WorldLedger | SQLiteWorldLedger = WorldLedger.in_memory(world_id=world_id)
    else:
        ledger = SQLiteWorldLedger(path=tmp_path / "no-replay.sqlite3", world_id=world_id)
    events = (_event(world_id), *(_observation(world_id, index) for index in range(200)))
    ledger.commit(events, expected_world_revision=0, expected_deliberation_revision=0)
    query = query_from_projection(
        ledger.project(), actor_ref="actor:companion", trigger_ref="event:incoming"
    )

    def replay_is_forbidden(*args, **kwargs):
        raise AssertionError("head Context resolution replayed historical events")

    monkeypatch.setattr(ledger_module, "reduce_event", replay_is_forbidden)
    monkeypatch.setattr(sqlite_ledger_module, "reduce_event", replay_is_forbidden)

    capsule = _compiler(ledger).compile(query)

    assert capsule.world_revision == 201
    assert capsule.ledger_sequence == 201
    if isinstance(ledger, SQLiteWorldLedger):
        ledger.close()


def test_stale_cursor_fails_before_historical_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = _empty_ledger(world_id="world:context-stale")
    stale_query = query_from_projection(
        ledger.project(), actor_ref="actor:companion", trigger_ref="event:incoming"
    )
    head = ledger.project()
    ledger.commit(
        [_observation(ledger.world_id, 1)],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )

    def replay_is_forbidden(*args, **kwargs):
        raise AssertionError("stale Context query replayed historical events")

    monkeypatch.setattr(ledger_module, "reduce_event", replay_is_forbidden)

    with pytest.raises(ValueError, match="indexed projection reader"):
        _compiler(ledger).compile(stale_query)


def test_explicit_actor_relevance_scope_is_bound_into_every_proof() -> None:
    ledger = _empty_ledger(world_id="world:context-scope")
    query = query_from_projection(
        ledger.project(), actor_ref="actor:companion", trigger_ref="event:incoming"
    )
    first_scope = ContextRelevanceScope(
        actor_ref="actor:companion", related_subject_refs=("user:one",)
    )
    second_scope = ContextRelevanceScope(
        actor_ref="actor:companion", related_subject_refs=("user:two",)
    )

    first = context_capsule_compiler_from_ledger(
        ledger=ledger, relevance_scope=first_scope
    ).compile(query)
    second = context_capsule_compiler_from_ledger(
        ledger=ledger, relevance_scope=second_scope
    ).compile(query)

    assert first.current_situation.resolver_proof is not None
    assert second.current_situation.resolver_proof is not None
    assert (
        first.current_situation.resolver_proof.window_ref
        != second.current_situation.resolver_proof.window_ref
    )
    assert first.capsule_id != second.capsule_id

    wrong_actor = ContextRelevanceScope(actor_ref="actor:other")
    with pytest.raises(ValueError, match="another actor"):
        context_capsule_compiler_from_ledger(ledger=ledger, relevance_scope=wrong_actor).compile(
            query
        )
