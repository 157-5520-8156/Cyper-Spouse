from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel

import companion_daemon.world_v2.ledger as ledger_module
import companion_daemon.world_v2.sqlite_ledger as sqlite_ledger_module
from companion_daemon.world_v2.context_capsule import (
    ContextCapsuleCompiler,
    InnerAdvisoryCandidate,
    InnerAdvisoryProjection,
)
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger import LedgerPort, WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    LedgerProjectionContextResolver,
    _bounded_domain_items,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.schemas import BudgetAccount, BudgetReservation, WorldEvent
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
        self.commit_calls = 0

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
        self.commit_calls += 1
        return self.delegate.commit(
            events,
            expected_world_revision=expected_world_revision,
            expected_deliberation_revision=expected_deliberation_revision,
            commit_id=commit_id,
        )


def _compiler(ledger: LedgerPort) -> ContextCapsuleCompiler:
    return context_capsule_compiler_from_ledger(ledger=ledger)


def test_default_scope_includes_only_the_committed_incoming_actor() -> None:
    world_id = "world:context-interlocutor"
    ledger = WorldLedger.in_memory(world_id=world_id)
    incoming = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:incoming",
        world_id=world_id,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user:primary",
        source="test",
        trace_id="trace:incoming",
        causation_id="cause:incoming",
        correlation_id="correlation:incoming",
        idempotency_key="identity:incoming",
        payload={"observation_id": "observation:incoming"},
    )
    ledger.commit(
        [_event(world_id), incoming], expected_world_revision=0, expected_deliberation_revision=0
    )
    projection = ledger.project()
    query = query_from_projection(
        projection, actor_ref="actor:companion", trigger_ref=incoming.event_id
    )
    resolver = LedgerProjectionContextResolver(
        ledger=ledger, situation_compiler=SituationCompiler()
    )

    scope = resolver._scope_for_query(query, projection)

    assert scope.actor_ref == "actor:companion"
    assert scope.related_subject_refs == ("user:primary",)


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
    assert first.logical_time.isoformat() == "2026-07-15T23:00:00+08:00"
    assert first.current_situation.availability == "available"
    assert first.character_core.items[0].item_ref == core.core_id
    assert first.relevant_facts.availability == "available"
    assert first.relevant_facts.items == ()
    assert first.open_threads.availability == "available"
    assert first.active_memory_candidates.availability == "available"
    assert first.relationship_slice.availability == "unavailable"
    # Private impressions now have a reducer-owned, source-bound authority
    # path, so an empty authority result is distinguishable from absence.
    assert first.private_impressions.availability == "available"
    assert first.private_impressions.items == ()
    assert first.available_capabilities.availability == "available"
    assert first.available_capabilities.items == ()
    # Advisories still exist only as a source-bound per-turn overlay below.
    assert first.advisories.availability == "unavailable"
    assert counted.project_at_calls == 2
    # Situation and CharacterCore request only their consumed refs; no full replay API exists.
    assert all(len(batch) <= 1 for batch in counted.resolved_batches)
    assert set(counted.lookups) == {core.origin.accepted_event_ref}
    assert counted.commit_calls == 0


def test_context_resolution_has_no_ledger_write_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _, _ = initialized_character_ledger(monkeypatch)
    counted = CountingLedger(ledger)
    before = ledger.project()

    _compiler(counted).compile(
        query_from_projection(
            before, actor_ref="actor:companion", trigger_ref="event:incoming"
        )
    )

    assert counted.commit_calls == 0
    assert ledger.project() == before


def test_context_window_reports_complete_source_bound_truncation() -> None:
    world_id = "world:context-window"
    ledger = WorldLedger.in_memory(world_id=world_id)
    events = [_event(world_id)]
    for index in range(12):
        account = BudgetAccount(
            account_id=f"account:{index:02d}",
            category="chat",
            window_id="window:day",
            limit=100,
        )
        events.append(
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=f"event:budget:{index:02d}",
                world_id=world_id,
                event_type="BudgetAccountConfigured",
                logical_time=NOW,
                created_at=NOW,
                actor="system:test",
                source="test",
                trace_id="trace:context-window",
                causation_id=f"cause:budget:{index:02d}",
                correlation_id="correlation:context-window",
                idempotency_key=f"identity:budget:{index:02d}",
                payload={"account": account.model_dump(mode="json")},
            )
        )
    ledger.commit(events, expected_world_revision=0, expected_deliberation_revision=0)
    projection = ledger.project()

    capsule = _compiler(ledger).compile(
        query_from_projection(
            projection, actor_ref="actor:companion", trigger_ref="event:start"
        )
    )

    assert capsule.action_budget.availability == "available"
    assert capsule.action_budget.truncated is True
    assert len(capsule.action_budget.items) == 1
    assert all(len(item.source_bindings) == 1 for item in capsule.action_budget.items)
    budget_omissions = tuple(
        entry
        for entry in capsule.budget.truncation_log
        if entry.slice_name == "action_budget"
    )
    assert {(entry.reason, entry.omitted_count) for entry in budget_omissions} == {
        ("item_budget", 4),
        ("character_budget", 7),
    }
    assert sum(entry.omitted_count for entry in budget_omissions) == 11


def test_budget_accounts_are_source_bound_to_their_complete_event_lineage() -> None:
    world_id = "world:context-budget-authority"
    ledger = WorldLedger.in_memory(world_id=world_id)
    account = BudgetAccount(
        account_id="account:chat",
        category="chat",
        window_id="window:day",
        limit=100,
    )
    reservation = BudgetReservation(
        reservation_id="reservation:chat",
        account_id=account.account_id,
        action_id="action:chat",
        category="chat",
        amount_limit=7,
    )
    ledger.commit(
        [
            _event(world_id),
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:budget-account",
                world_id=world_id,
                event_type="BudgetAccountConfigured",
                logical_time=NOW,
                created_at=NOW,
                actor="system:test",
                source="test",
                trace_id="trace:budget",
                causation_id="cause:budget-account",
                correlation_id="correlation:budget",
                idempotency_key="identity:budget-account",
                payload={"account": account.model_dump(mode="json")},
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:budget-reserved",
                world_id=world_id,
                event_type="BudgetReserved",
                logical_time=NOW,
                created_at=NOW,
                actor="system:test",
                source="test",
                trace_id="trace:budget",
                causation_id="cause:budget-reserved",
                correlation_id="correlation:budget",
                idempotency_key="identity:budget-reserved",
                payload={"reservation": reservation.model_dump(mode="json")},
            )
        ],
        expected_world_revision=2,
        expected_deliberation_revision=0,
    )

    projection = ledger.project()
    query = query_from_projection(
        projection, actor_ref="actor:companion", trigger_ref="event:budget-reserved"
    )
    capsule = _compiler(ledger).compile(query)
    replay = _compiler(ledger).compile(query)

    assert replay.model_dump_json() == capsule.model_dump_json()
    assert capsule.action_budget.availability == "available"
    assert len(capsule.action_budget.items) == 1
    item = capsule.action_budget.items[0]
    assert '"reserved":7' in item.payload_json
    assert {binding.ref for binding in item.source_bindings} == {
        "event:budget-account",
        "event:budget-reserved",
    }


def test_advisory_overlay_is_available_only_after_same_cursor_source_binding() -> None:
    world_id = "world:context-advisory-overlay"
    ledger = _empty_ledger(world_id=world_id)
    projection = ledger.project()
    query = query_from_projection(
        projection, actor_ref="actor:companion", trigger_ref="event:start"
    )
    assert query.logical_time is not None
    advisory = InnerAdvisoryProjection(
        advisory_id="advisory:1",
        kind="appraisal.negative",
        source_refs=("event:start",),
        candidate_refs=("advisory:1:candidate:1",),
        candidates=(
            InnerAdvisoryCandidate(
                candidate_ref="advisory:1:candidate:1",
                value="disappointment",
                weight_bp=7000,
                confidence_bp=7000,
            ),
        ),
        confidence_bp=7000,
        # Advisory expiry is compared to the pinned logical clock, never the
        # process wall clock.  Derive it from that same query to keep this
        # contract stable when the full suite runs with a different date.
        expiry=query.logical_time + timedelta(minutes=1),
        producer_version="test-classifier.1",
    )

    counted = CountingLedger(ledger)
    compiler = _compiler(counted)
    prepared = compiler.prepare_for_deliberation(query)
    base = compiler.finalize_prepared(prepared).capsule
    capsule = compiler.compile_prepared_with_advisories(
        prepared, (advisory,)
    ).capsule

    assert counted.project_at_calls == 1
    assert base.advisories.availability == "unavailable"
    assert capsule.advisories.availability == "available"
    assert capsule.advisories.source_refs == ("event:start",)
    assert capsule.advisories.items[0].item_ref == advisory.advisory_id
    with pytest.raises(ValueError, match="another compiler"):
        _compiler(ledger).finalize_prepared(prepared)


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


def test_nonempty_context_is_byte_equivalent_across_memory_and_sqlite(
    tmp_path: Path,
) -> None:
    world_id = "world:context-adapter-equivalence"
    account = BudgetAccount(
        account_id="account:chat",
        category="chat",
        window_id="window:day",
        limit=100,
    )

    def populate(ledger: LedgerPort) -> None:
        configured = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:budget",
            world_id=world_id,
            event_type="BudgetAccountConfigured",
            logical_time=NOW,
            created_at=NOW,
            actor="system:test",
            source="test",
            trace_id="trace:adapter-equivalence",
            causation_id="cause:budget",
            correlation_id="correlation:adapter-equivalence",
            idempotency_key="identity:budget",
            payload={"account": account.model_dump(mode="json")},
        )
        ledger.commit(
            [_event(world_id), configured],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )

    memory = WorldLedger.in_memory(world_id=world_id)
    sqlite = SQLiteWorldLedger(path=tmp_path / "equivalent.sqlite3", world_id=world_id)
    populate(memory)
    populate(sqlite)

    memory_projection = memory.project()
    sqlite_projection = sqlite.project()
    memory_capsule = _compiler(memory).compile(
        query_from_projection(
            memory_projection, actor_ref="actor:companion", trigger_ref="event:start"
        )
    )
    sqlite_capsule = _compiler(sqlite).compile(
        query_from_projection(
            sqlite_projection, actor_ref="actor:companion", trigger_ref="event:start"
        )
    )

    assert sqlite_projection == memory_projection
    assert sqlite_capsule.model_dump_json() == memory_capsule.model_dump_json()
    assert sqlite_capsule.action_budget.availability == "available"
    assert sqlite_capsule.action_budget.items[0].item_ref == "account:chat:window:day"
    sqlite.close()


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


def test_sqlite_context_compile_after_each_commit_reuses_incremental_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twenty/thirty-turn growth uses one canonical pass and no head decode."""

    world_id = "world:context-incremental-head"
    ledger = SQLiteWorldLedger(
        path=tmp_path / "context-incremental-head.sqlite3", world_id=world_id
    )
    ledger.commit(
        [_event(world_id)],
        commit_id="commit:context-incremental-head:start",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    compiler = _compiler(ledger)
    decode_calls = 0
    legacy_state_hash_calls = 0
    canonical_sizes: dict[int, int] = {}
    original_decode = ledger._decode_state  # noqa: SLF001 - performance seam assertion
    original_state_hash = ledger._state_hash  # noqa: SLF001
    original_encode_and_hash = ledger._encode_state_and_hash  # noqa: SLF001

    def counted_decode(value: str):
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(value)

    def counted_state_hash(state, cursor):
        nonlocal legacy_state_hash_calls
        legacy_state_hash_calls += 1
        return original_state_hash(state, cursor)

    def counted_encode_and_hash(state, cursor):
        encoded, state_hash = original_encode_and_hash(state, cursor)
        canonical_sizes[cursor.ledger_sequence] = len(encoded.encode("utf-8"))
        return encoded, state_hash

    original_encode_delta = ledger._encode_state_delta  # noqa: SLF001

    def sized_encode_delta(state, cursor):
        # The incremental commit path no longer materializes the canonical
        # document; its per-field UTF-8 chunks carry the equivalent size.
        result = original_encode_delta(state, cursor)
        fragment_bytes = ledger._state_fragment_bytes  # noqa: SLF001
        assert fragment_bytes is not None
        canonical_sizes[cursor.ledger_sequence] = sum(
            len(chunk) for chunk in fragment_bytes[1].values()
        )
        return result

    monkeypatch.setattr(ledger, "_decode_state", counted_decode)
    monkeypatch.setattr(ledger, "_state_hash", counted_state_hash)
    monkeypatch.setattr(ledger, "_encode_state_and_hash", counted_encode_and_hash)
    monkeypatch.setattr(ledger, "_encode_state_delta", sized_encode_delta)
    try:
        checkpoints: dict[int, int] = {}
        for index in range(1, 31):
            head = ledger.project()
            trigger = _observation(world_id, index)
            ledger.commit(
                [trigger],
                commit_id=f"commit:context-incremental-head:{index}",
                expected_world_revision=head.world_revision,
                expected_deliberation_revision=head.deliberation_revision,
            )
            before = ledger.performance_counters()
            projection = ledger.project()
            query = query_from_projection(
                projection,
                actor_ref="actor:companion",
                trigger_ref=trigger.event_id,
            )
            first = compiler.compile(query)
            after = ledger.performance_counters()
            second = compiler.compile(query)

            # Building the exact post-commit projection and Context should be
            # served entirely from the commit-produced verified head.  A miss
            # here decodes and validates state_json whose size grows with the
            # whole conversation rather than with this turn's delta.
            read_delta = after.head_projection_reads - before.head_projection_reads
            hit_delta = after.head_projection_cache_hits - before.head_projection_cache_hits
            assert read_delta >= 3
            assert hit_delta == read_delta
            assert after.historical_replay_calls == before.historical_replay_calls
            assert second.capsule_id == first.capsule_id
            assert second.model_dump(mode="json") == first.model_dump(mode="json")
            if index in {20, 30}:
                checkpoints[index] = canonical_sizes[projection.ledger_sequence]
        assert decode_calls == 0
        assert legacy_state_hash_calls == 0
        assert len(canonical_sizes) == 30
        assert checkpoints[30] > checkpoints[20]
    finally:
        ledger.close()


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
