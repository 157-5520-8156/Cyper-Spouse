"""Split head-state storage: per-item persistence, migration and WAL behavior.

``world_v2_heads.state_json`` used to hold the whole reducer state as one
monotonically growing document, so every commit physically rewrote megabytes
and the shared WAL grew by the full state size per commit.  These tests pin
the replacement contract: per-item rows, byte-identical state hashes, an
automatic in-code migration for legacy rows, fail-closed reads, and WAL
growth proportional to the commit's delta rather than to world age.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

from legacy_migration_support import read_head_state_json

from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.expression_payload_store import (
    SQLiteImmutableExpressionPayloadStore,
    StoredExpressionPayload,
    expression_payload_hash,
)
from companion_daemon.world_v2.life_content_store import (
    SQLiteImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.media_v2 import (
    SQLiteImmutableMediaPayloadStore,
    StoredMediaPayload,
    media_payload_hash,
)
from companion_daemon.world_v2.qq_ingress_policy import QQIngressFragment, SQLiteQQIngressStore
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.sqlite_ledger import (
    _HEAD_STATE_SENTINEL,
    SQLiteWorldLedger,
    load_head_state_json,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
WORLD = "world-head-split-test"


def event(event_id: str, observation_id: str, world_id: str = WORLD) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace-1",
        causation_id="cause-1",
        correlation_id="correlation-1",
        idempotency_key=event_id,
        payload={"observation_id": observation_id},
    )


def commit_one(ledger: SQLiteWorldLedger, index: int, world_id: str = WORLD) -> None:
    head = ledger.project()
    ledger.commit(
        [event(f"event-{index}", f"obs-{index}", world_id)],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )


def test_commits_persist_split_items_under_the_exact_state_hash(tmp_path) -> None:
    path = tmp_path / "split-basic.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    for index in range(5):
        commit_one(ledger, index)
    projection = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        state_json, epoch = connection.execute(
            "SELECT state_json, storage_epoch FROM world_v2_heads WHERE world_id = ?",
            (WORLD,),
        ).fetchone()
        assert state_json == _HEAD_STATE_SENTINEL
        assert epoch >= 5
        item_count = connection.execute(
            "SELECT COUNT(*) FROM world_v2_head_state_items WHERE world_id = ?",
            (WORLD,),
        ).fetchone()[0]
        assert item_count > 0
        refs = connection.execute(
            "SELECT COUNT(*) FROM world_v2_head_state_items "
            "WHERE world_id = ? AND field = 'observation_refs' AND idx >= 0",
            (WORLD,),
        ).fetchone()[0]
        assert refs == 5
        state = ReducerState.model_validate_json(load_head_state_json(connection, WORLD))
        assert state.observation_refs == tuple(f"obs-{i}" for i in range(5))

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == projection
    assert reopened.rebuild() == projection
    commit_one(reopened, 99)
    assert reopened.project().observation_refs[-1] == "obs-99"
    reopened.close()


def test_legacy_full_row_heads_migrate_for_every_world_in_the_file(tmp_path) -> None:
    path = tmp_path / "split-migration.sqlite3"
    other_world = "world-head-split-other"
    first = SQLiteWorldLedger(path=path, world_id=WORLD)
    commit_one(first, 1)
    expected_first = first.project()
    first.close()
    second = SQLiteWorldLedger(path=path, world_id=other_world)
    commit_one(second, 2, other_world)
    expected_second = second.project()
    second.close()

    # Reconstruct the pre-split database shape: full-row documents, no items.
    with sqlite3.connect(path) as connection:
        for world_id in (WORLD, other_world):
            full = read_head_state_json(connection, world_id)
            connection.execute(
                "UPDATE world_v2_heads SET state_json = ? WHERE world_id = ?",
                (full, world_id),
            )
        connection.execute("DELETE FROM world_v2_head_state_items")

    # Opening one world's ledger migrates every verifiable row in the file.
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected_first
    reopened.close()
    with sqlite3.connect(path) as connection:
        rows = dict(
            connection.execute("SELECT world_id, state_json FROM world_v2_heads")
        )
        assert rows[WORLD] == _HEAD_STATE_SENTINEL
        assert rows[other_world] == _HEAD_STATE_SENTINEL
    other = SQLiteWorldLedger(path=path, world_id=other_world)
    assert other.project() == expected_second
    assert other.rebuild() == expected_second
    other.close()


def test_tampered_state_item_row_fails_closed(tmp_path) -> None:
    path = tmp_path / "split-tamper.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    commit_one(ledger, 1)
    ledger.close()

    with sqlite3.connect(path) as connection:
        changed = connection.execute(
            "UPDATE world_v2_head_state_items SET item_json = '\"obs-forged\"' "
            "WHERE world_id = ? AND field = 'observation_refs' AND idx = 0",
            (WORLD,),
        ).rowcount
        assert changed == 1
    with pytest.raises(LedgerIntegrityError, match="split head state"):
        SQLiteWorldLedger(path=path, world_id=WORLD)


def test_open_ledger_rejects_cross_connection_item_tamper_before_commit(tmp_path) -> None:
    path = tmp_path / "split-tamper-live.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    commit_one(ledger, 1)
    assert ledger.project().observation_refs == ("obs-1",)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_head_state_items SET item_json = '\"obs-forged\"' "
            "WHERE world_id = ? AND field = 'observation_refs' AND idx = 0",
            (WORLD,),
        )
    with pytest.raises(LedgerIntegrityError):
        commit_one(ledger, 2)
    ledger.close()


def test_missing_item_rows_fail_closed(tmp_path) -> None:
    path = tmp_path / "split-missing-items.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    commit_one(ledger, 1)
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM world_v2_head_state_items WHERE world_id = ?", (WORLD,)
        )
    with pytest.raises(LedgerIntegrityError, match="split head state"):
        SQLiteWorldLedger(path=path, world_id=WORLD)


def test_state_storage_wal_cost_per_commit_does_not_grow_with_world_age(
    tmp_path, monkeypatch
) -> None:
    """The state write must cost the delta's pages, not the document's.

    The derived prefix-proof tables have their own append cost per commit
    that predates and is independent of head-state storage, so it is stubbed
    out here to observe the state persistence in isolation.  The legacy
    format rewrote the whole ``state_json`` row every commit: its WAL cost
    grew with the document.  The split format touches a bounded set of item
    rows, so late commits must cost the same pages as early ones.
    """

    path = tmp_path / "split-wal-growth.sqlite3"
    wal = tmp_path / "split-wal-growth.sqlite3-wal"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    monkeypatch.setattr(
        ledger, "_persist_prefix_new_commit_locked", lambda **_kwargs: None
    )

    def wal_bytes() -> int:
        try:
            return wal.stat().st_size
        except FileNotFoundError:
            return 0

    deltas: list[int] = []
    for index in range(120):
        before = wal_bytes()
        commit_one(ledger, index)
        deltas.append(wal_bytes() - before)
    early = sum(deltas[10:20]) / 10
    late = sum(deltas[110:120]) / 10
    ledger.close()

    assert late <= early * 1.5
    assert late < 128 * 1024


def test_commit_upserts_only_the_changed_state_items(tmp_path) -> None:
    """A one-observation commit must not rewrite the whole grown state."""

    path = tmp_path / "split-delta-ops.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    for index in range(30):
        commit_one(ledger, index)

    captured: list[object] = []
    original = SQLiteWorldLedger._encode_state_delta

    def capture(state, cursor):
        result = original(ledger, state, cursor)
        captured.append(ledger._pending_head_state_ops)
        return result

    ledger._encode_state_delta = capture
    commit_one(ledger, 99)
    ledger.close()

    (ops,) = captured
    assert not ops.full_rewrite
    assert not ops.field_deletes
    assert not ops.tail_deletes
    # One observation appends to a handful of tuple fields and replaces a few
    # single elements; it must never scale with the 30 turns of history.
    assert 0 < len(ops.upserts) <= 16
    appended_fields = {field for field, _, _ in ops.upserts}
    assert "observation_refs" in appended_fields
    assert "committed_world_event_refs" in appended_fields


def test_consecutive_commits_never_re_encode_the_whole_state(tmp_path) -> None:
    """The commit hot path must stay incremental for state and semantic bytes.

    encode_ms regressed to seconds in production because, although the state
    fragments were patched incrementally, every commit still re-dumped the
    whole reducer state for the semantic hash and rebuilt the projection from
    scratch.  These counters pin all three lanes: state fragments, semantic
    fragments, and the reused head projection.
    """

    from companion_daemon.world_v2.reducers import semantic_hash as full_semantic_hash

    path = tmp_path / "split-incremental-encode.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    # The open pre-warms both fragment caches from the verified head.
    assert ledger._semantic_sharing_verified  # noqa: SLF001
    assert ledger._state_fragment_cache is not None  # noqa: SLF001

    for index in range(8):
        commit_one(ledger, index)
    assert ledger._full_state_encode_count == 0  # noqa: SLF001
    assert ledger._semantic_full_count == 0  # noqa: SLF001
    assert ledger._incremental_state_encode_count == 8  # noqa: SLF001
    assert ledger._semantic_incremental_count == 8  # noqa: SLF001

    # The incrementally patched semantic hash must equal a from-scratch
    # computation over the exact same state.
    projection = ledger.project()
    state = ledger._head_state_cache  # noqa: SLF001
    assert state is not None
    assert projection.semantic_hash == full_semantic_hash(
        world_id=WORLD, world_revision=projection.world_revision, state=state
    )
    ledger.close()

    # Cold start: reopening replays and byte-verifies the whole ledger, then
    # pre-warms the caches, so even the first commit of a new process never
    # re-encodes the grown state.
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == projection
    assert reopened._semantic_sharing_verified  # noqa: SLF001
    commit_one(reopened, 99)
    assert reopened._full_state_encode_count == 0  # noqa: SLF001
    assert reopened._semantic_full_count == 0  # noqa: SLF001
    assert reopened._incremental_state_encode_count == 1  # noqa: SLF001
    assert reopened.project().observation_refs[-1] == "obs-99"
    reopened.close()


def test_wal_truncate_checkpoint_succeeds_after_ledger_and_sidecar_activity(
    tmp_path,
) -> None:
    """No store may retain a read snapshot that pins the WAL reset point."""

    path = tmp_path / "shared-wal-truncate.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    commit_one(ledger, 1)

    life = SQLiteImmutableLifeContentStore(path=str(path), world_id=WORLD)
    life_text = "她在窗边看了一会儿雨。"
    life.put_if_absent(
        StoredLifeContent(
            content_ref="life:1",
            content_kind="occurrence_result",
            content_payload_hash=life_content_payload_hash(life_text),
            text=life_text,
        )
    )
    assert life.read_exact(content_ref="life:1") is not None
    assert life.read_exact(content_ref="life:absent") is None

    expression = SQLiteImmutableExpressionPayloadStore(path=str(path), world_id=WORLD)
    payload = StoredExpressionPayload(
        payload_ref="expr:1",
        payload_hash=expression_payload_hash("你好"),
        content_type="text/plain",
        privacy_class="private",
        payload_kind="referenced",
        encoded_payload="你好",
    )
    expression.put_if_absent(payload)
    # The idempotent-replay path (existing row, no write) must not linger in
    # an implicit transaction either.
    expression.put_if_absent(payload)
    assert expression.read_exact(payload_ref="expr:1") is not None

    media = SQLiteImmutableMediaPayloadStore(path=str(path), world_id=WORLD)
    media.put_if_absent(
        StoredMediaPayload(
            payload_ref="media:1",
            payload_hash=media_payload_hash("{}"),
            content_type="application/json",
            body="{}",
        )
    )
    assert media.read_exact(payload_ref="media:1") is not None

    ingress = SQLiteQQIngressStore(tmp_path / "shared-wal-truncate.sqlite3")
    ingress.submit(
        QQIngressFragment(
            source_event_id="qq:1",
            recipient_id="10001",
            observed_at=NOW,
            content_shape="text",
            text="在吗",
        ),
        received_at=NOW,
    )
    ingress.claim_due(now=NOW + timedelta(seconds=30))

    commit_one(ledger, 2)

    with sqlite3.connect(path) as checkpointer:
        checkpointer.execute("PRAGMA busy_timeout = 2000")
        busy, log_frames, checkpointed = checkpointer.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
    assert busy == 0
    assert checkpointed == log_frames
    wal = tmp_path / "shared-wal-truncate.sqlite3-wal"
    assert wal.stat().st_size == 0

    # The ledger keeps working after the WAL was reset underneath it.
    commit_one(ledger, 3)
    assert ledger.project().observation_refs[-1] == "obs-3"
    ledger.close()
    life.close()
    expression.close()
    media.close()


def test_head_state_document_shape_is_unchanged_after_split(tmp_path) -> None:
    """The reassembled document must equal the legacy full-row encoding."""

    path = tmp_path / "split-document-shape.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    for index in range(3):
        commit_one(ledger, index)
    expected_document = json.loads(
        SQLiteWorldLedger._encode_state(ledger._head_state_cache)
    )
    ledger.close()
    with sqlite3.connect(path) as connection:
        assert json.loads(load_head_state_json(connection, WORLD)) == expected_document
