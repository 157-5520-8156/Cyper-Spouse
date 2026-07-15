from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


WORLD_ID = "world-v19-migration"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _observation_event() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event-v19-migration",
        world_id=WORLD_ID,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace-v19-migration",
        causation_id="cause-v19-migration",
        correlation_id="correlation-v19-migration",
        idempotency_key="event-v19-migration",
        payload={"observation_id": "obs-v19-migration"},
    )


def test_sqlite_migrates_verified_v18_head_without_fabricating_v19_fields(tmp_path) -> None:
    path = tmp_path / "v18-to-v19.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    ledger.commit(
        [_observation_event()],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD_ID,)
        ).fetchone()
        assert row is not None
        legacy_state = json.loads(row[0])
        # These fields did not exist in the .18 persisted state schema.
        legacy_state.pop("fact_commit_proposal_audits_v2")
        legacy_state.pop("acceptance_manifests_v3")
        state = ReducerState.model_validate_json(
            json.dumps(legacy_state, ensure_ascii=False, separators=(",", ":")),
            context={"source_reducer_bundle": "world-v2-reducers.18"},
        )
        payload = state.semantic_payload(
            world_id=WORLD_ID,
            world_revision=1,
            reducer_bundle_version="world-v2-reducers.18",
        )
        legacy_hash = hashlib.sha256(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """UPDATE world_v2_heads
               SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?, state_hash = ?
               WHERE world_id = ?""",
            (
                json.dumps(legacy_state, ensure_ascii=False, separators=(",", ":")),
                legacy_hash,
                "world-v2-reducers.18",
                "0" * 64,
                WORLD_ID,
            ),
        )

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    projection = migrated.project()
    assert projection == expected
    assert projection.reducer_bundle_version == "world-v2-reducers.21"
    assert projection.fact_commit_proposal_audits_v2 == ()
    assert projection.acceptance_manifests_v3 == ()
    assert migrated.rebuild() == projection
    migrated.close()


def test_v18_legacy_hash_rejects_nonempty_v19_state_fields(tmp_path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "v19-injected.sqlite3", world_id=WORLD_ID)
    forged_state = ReducerState().model_dump(mode="json")
    forged_state["fact_commit_proposal_audits_v2"] = [{"forged": "audit"}]

    with pytest.raises(LedgerIntegrityError, match="legacy head state is invalid"):
        ledger._legacy_semantic_hash(
            state_json=json.dumps(forged_state, separators=(",", ":")),
            world_revision=0,
            reducer_bundle_version="world-v2-reducers.18",
        )
    ledger.close()


def test_sqlite_migrates_verified_v19_head_without_fabricating_reply_state(tmp_path) -> None:
    path = tmp_path / "v19-to-v20.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    ledger.commit(
        [_observation_event()], expected_world_revision=0, expected_deliberation_revision=0
    )
    ledger.close()
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?", (WORLD_ID,)
        ).fetchone()
        assert row is not None
        state_json = json.loads(row[0])
        for key in (
            "minimal_reply_manifests",
            "stored_message_payloads",
            "expression_plans",
            "expression_beats",
        ):
            state_json.pop(key)
        state = ReducerState.model_validate_json(json.dumps(state_json, separators=(",", ":")))
        legacy_hash = hashlib.sha256(
            json.dumps(
                state.semantic_payload(
                    world_id=WORLD_ID,
                    world_revision=1,
                    reducer_bundle_version="world-v2-reducers.19",
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, "
            "reducer_bundle_version = ?, state_hash = ? WHERE world_id = ?",
            (
                json.dumps(state_json, separators=(",", ":")),
                legacy_hash,
                "world-v2-reducers.19",
                "0" * 64,
                WORLD_ID,
            ),
        )

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    assert migrated.project().reducer_bundle_version == "world-v2-reducers.21"
    assert migrated.project().minimal_reply_manifests == ()
    assert migrated.project().stored_message_payloads == ()
    migrated.close()


def test_v19_legacy_hash_rejects_nonempty_v20_reply_state(tmp_path) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "v20-injected.sqlite3", world_id=WORLD_ID)
    forged_state = ReducerState().model_dump(mode="json")
    forged_state["minimal_reply_manifests"] = [{"forged": "reply"}]
    with pytest.raises(LedgerIntegrityError, match="legacy head state is invalid"):
        ledger._legacy_semantic_hash(
            state_json=json.dumps(forged_state, separators=(",", ":")),
            world_revision=0,
            reducer_bundle_version="world-v2-reducers.19",
        )
    ledger.close()
