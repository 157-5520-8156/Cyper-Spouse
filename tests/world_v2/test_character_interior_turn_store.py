from __future__ import annotations

from datetime import UTC, datetime, timedelta

from companion_daemon.world_v2.character_interior.turn_store import (
    _TurnCoordinationRequest,
    _InMemoryCharacterInteriorTurnStore,
    open_sqlite_character_interior_turn_store,
)


def _request(*, world_id: str = "world:one", turn_id: str = "turn:one") -> _TurnCoordinationRequest:
    return _TurnCoordinationRequest(
        world_id=world_id,
        actor_ref="agent:companion",
        inner_turn_id=turn_id,
        phase="experience",
        purpose="inbound_turn",
        subject_ref="observation:one",
        trigger_ref="trigger:one",
        cursor_json='{"world_revision":1,"deliberation_revision":1,"ledger_sequence":1}',
        request_hash="a" * 64,
        snapshot_id="snapshot:one",
        snapshot_hash="b" * 64,
        capability_hash="c" * 64,
    )


def _complete(store, request: _TurnCoordinationRequest, *, now: datetime) -> None:
    acquired = store.acquire(
        request=request,
        owner_id="runtime:one",
        now=now,
        lease_seconds=30,
    )
    checkpointed = store.checkpoint(
        request=request,
        owner_id="runtime:one",
        lease_token=acquired.record.lease_token or "",
        attempt_ordinal=acquired.record.attempt_ordinal,
        authored_state_json='{"status":"silent"}',
        authored_state_hash="d" * 64,
        now=now,
    )
    store.complete(
        request=request,
        owner_id="runtime:one",
        lease_token=checkpointed.lease_token or "",
        attempt_ordinal=checkpointed.attempt_ordinal,
        terminal_result_json='{"status":"model_silent"}',
        terminal_result_hash="e" * 64,
        now=now,
    )


def test_in_memory_distinguishes_same_owner_runtime_leases() -> None:
    store = _InMemoryCharacterInteriorTurnStore()
    request = _request()
    now = datetime(2026, 8, 8, tzinfo=UTC)

    first = store.acquire(request=request, owner_id="runtime:same", now=now, lease_seconds=30)
    second = store.acquire(request=request, owner_id="runtime:same", now=now, lease_seconds=30)

    assert first.status == "acquired"
    assert second.status == "owned_elsewhere"
    assert second.record.lease_token == first.record.lease_token


def test_sqlite_checkpoint_recovered_after_lease_expiry_without_second_model_call(tmp_path) -> None:
    path = tmp_path / "turns.sqlite"
    first_store = open_sqlite_character_interior_turn_store(path=path, world_id="world:one")
    second_store = open_sqlite_character_interior_turn_store(path=path, world_id="world:one")
    request = _request()
    now = datetime(2026, 8, 8, tzinfo=UTC)

    claimed = first_store.acquire(
        request=request,
        owner_id="runtime:first",
        now=now,
        lease_seconds=30,
    )
    checkpointed = first_store.checkpoint(
        request=request,
        owner_id="runtime:first",
        lease_token=claimed.record.lease_token or "",
        attempt_ordinal=claimed.record.attempt_ordinal,
        authored_state_json='{"status":"silent"}',
        authored_state_hash="d" * 64,
        now=now,
    )
    blocked = second_store.acquire(
        request=request,
        owner_id="runtime:second",
        now=now + timedelta(seconds=1),
        lease_seconds=30,
    )
    recovered = second_store.acquire(
        request=request,
        owner_id="runtime:second",
        now=now + timedelta(seconds=31),
        lease_seconds=30,
    )

    assert blocked.status == "owned_elsewhere"
    assert recovered.status == "recovered"
    assert recovered.record.authored_state_json == checkpointed.authored_state_json
    assert recovered.record.lease_token != checkpointed.lease_token

    second_store.complete(
        request=request,
        owner_id="runtime:second",
        lease_token=recovered.record.lease_token or "",
        attempt_ordinal=recovered.record.attempt_ordinal,
        terminal_result_json='{"status":"model_silent"}',
        terminal_result_hash="e" * 64,
        now=now + timedelta(seconds=31),
    )
    assert second_store.health(world_id="world:one", actor_ref="agent:companion") == {
        "scope": "world_actor",
        "pending_claim_count": 0,
        "checkpointed_claim_count": 0,
        "terminal_turn_count": 1,
        "expired_claim_count": 0,
        "recovered_attempt_count": 1,
    }
    first_store.close()
    second_store.close()


def test_sqlite_prune_is_world_scoped(tmp_path) -> None:
    path = tmp_path / "turns.sqlite"
    first_store = open_sqlite_character_interior_turn_store(path=path, world_id="world:one")
    second_store = open_sqlite_character_interior_turn_store(path=path, world_id="world:two")
    now = datetime(2026, 8, 8, tzinfo=UTC)
    _complete(first_store, _request(world_id="world:one", turn_id="turn:one"), now=now)
    _complete(second_store, _request(world_id="world:two", turn_id="turn:two"), now=now)

    assert first_store.prune_terminal(
        world_id="world:one", before=now + timedelta(seconds=1)
    ) == 1
    assert first_store.health(world_id="world:one", actor_ref="agent:companion")[
        "terminal_turn_count"
    ] == 0
    assert second_store.health(world_id="world:two", actor_ref="agent:companion")[
        "terminal_turn_count"
    ] == 1
    first_store.close()
    second_store.close()
