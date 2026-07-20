from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from legacy_migration_support import (
    legacy_state_json,
    read_head_state_json,
    strip_v16_state_fields,
)

from companion_daemon.world_v2.errors import ConcurrencyConflict, LedgerIntegrityError
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    BudgetReservation,
    BudgetSettlement,
    ProjectionCursor,
    WorldEvent,
)
from companion_daemon.world_v2.reducers import REDUCER_BUNDLE_VERSION, ReducerState
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def event(event_id: str, observation_id: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id="world-sqlite-test",
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


def test_sqlite_ledger_survives_restart_and_retries_atomic_commit(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    first = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    events = [event("event-1", "obs-1"), event("event-2", "obs-2")]
    committed = first.commit(
        events,
        commit_id="commit-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    first.close()

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project().observation_refs == ("obs-1", "obs-2")
    assert reopened.rebuild() == reopened.project()
    assert (
        reopened.commit(
            events,
            commit_id="commit-1",
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
        == committed
    )
    assert reopened.project().world_revision == 2
    reopened.close()


def test_wal_maintenance_is_thresholded_and_passive(tmp_path) -> None:
    path = tmp_path / "wal-maintenance.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-wal-maintenance", "obs-wal-maintenance")],
        commit_id="commit-wal-maintenance",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    skipped = ledger.maintain_wal_if_needed(threshold_bytes=10**12)
    assert skipped.status == "skipped"
    assert skipped.wal_bytes_before == skipped.wal_bytes_after

    checkpointed = ledger.maintain_wal_if_needed(
        threshold_bytes=1,
        min_interval_seconds=0,
    )
    assert checkpointed.status == "checkpointed"
    assert checkpointed.busy is False
    assert checkpointed.checkpointed_frames > 0
    assert checkpointed.wal_bytes_after <= checkpointed.wal_bytes_before

    throttled = ledger.maintain_wal_if_needed(
        threshold_bytes=1,
        min_interval_seconds=60,
    )
    assert throttled.status == "skipped"
    ledger.close()


def test_projection_performance_counters_distinguish_head_reads_from_history_replay(
    tmp_path,
) -> None:
    ledger = SQLiteWorldLedger(path=tmp_path / "projection-shape.sqlite3", world_id="world-sqlite-test")
    startup = ledger.performance_counters()
    committed = ledger.commit(
        [event("event-perf-1", "obs-perf-1")],
        commit_id="commit-perf-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    cursor = ProjectionCursor(
        world_revision=committed.world_revision,
        deliberation_revision=committed.deliberation_revision,
        ledger_sequence=committed.ledger_sequence,
    )

    assert ledger.project_at(cursor).observation_refs == ("obs-perf-1",)
    assert ledger.project().observation_refs == ("obs-perf-1",)
    hot = ledger.performance_counters()
    assert hot.project_at_head_hits == startup.project_at_head_hits + 1
    assert hot.head_projection_cache_hits > startup.head_projection_cache_hits
    assert hot.historical_replay_calls == startup.historical_replay_calls
    assert hot.total_replay_calls == startup.total_replay_calls

    zero = ProjectionCursor(world_revision=0, deliberation_revision=0, ledger_sequence=0)
    assert ledger.project_at(zero).ledger_sequence == 0
    historical = ledger.performance_counters()
    assert historical.historical_replay_calls == hot.historical_replay_calls + 1
    assert historical.total_replay_calls == hot.total_replay_calls + 1
    ledger.close()


def test_commit_seeded_head_cache_invalidates_on_external_append(tmp_path) -> None:
    path = tmp_path / "commit-seeded-head-external-append.sqlite3"
    left = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    right = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    first = left.commit(
        [event("event-head-seed-1", "obs-head-seed-1")],
        commit_id="commit-head-seed-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    assert left.project().observation_refs == ("obs-head-seed-1",)
    seeded = left.performance_counters()
    assert seeded.head_projection_cache_hits >= 1

    right.commit(
        [event("event-head-seed-2", "obs-head-seed-2")],
        commit_id="commit-head-seed-2",
        expected_world_revision=first.world_revision,
        expected_deliberation_revision=first.deliberation_revision,
    )
    assert left.project().observation_refs == (
        "obs-head-seed-1",
        "obs-head-seed-2",
    )
    refreshed = left.performance_counters()
    assert refreshed.head_projection_cache_hits == seeded.head_projection_cache_hits
    assert left.project().observation_refs[-1] == "obs-head-seed-2"
    hot = left.performance_counters()
    assert hot.head_projection_cache_hits == refreshed.head_projection_cache_hits + 1
    left.close()
    right.close()


def test_commit_tail_cache_never_mutates_a_previously_returned_projection(tmp_path) -> None:
    ledger = SQLiteWorldLedger(
        path=tmp_path / "projection-snapshot-immutability.sqlite3",
        world_id="world-sqlite-test",
    )
    ledger.commit(
        [event("event-snapshot-a", "obs-snapshot-a")],
        commit_id="commit-snapshot-a",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    projection_a = ledger.project()
    cached_state = ledger._head_state_cache  # noqa: SLF001 - canonical-byte equivalence
    assert cached_state is not None
    cursor_a = ProjectionCursor(
        world_revision=projection_a.world_revision,
        deliberation_revision=projection_a.deliberation_revision,
        ledger_sequence=projection_a.ledger_sequence,
    )
    encoded_a, combined_hash_a = ledger._encode_state_and_hash(  # noqa: SLF001
        cached_state, cursor_a
    )
    assert ledger._decode_state(encoded_a) == cached_state  # noqa: SLF001
    assert combined_hash_a == ledger._state_hash(cached_state, cursor_a)  # noqa: SLF001
    frozen_json = projection_a.model_dump_json()
    frozen_hash = projection_a.semantic_hash
    ledger.commit(
        [event("event-snapshot-b", "obs-snapshot-b")],
        commit_id="commit-snapshot-b",
        expected_world_revision=projection_a.world_revision,
        expected_deliberation_revision=projection_a.deliberation_revision,
    )

    assert projection_a.model_dump_json() == frozen_json
    assert projection_a.semantic_hash == frozen_hash
    assert projection_a.observation_refs == ("obs-snapshot-a",)
    assert ledger.project().observation_refs == ("obs-snapshot-a", "obs-snapshot-b")
    ledger.close()


def test_verified_lookup_tracks_same_process_head_without_replay(tmp_path) -> None:
    ledger = SQLiteWorldLedger(
        path=tmp_path / "same-process-verified-prefix.sqlite3",
        world_id="world-sqlite-test",
    )
    assert ledger.project().ledger_sequence == 0
    before = ledger.performance_counters()
    committed = ledger.commit(
        [event("event-same-process", "obs-same-process")],
        commit_id="commit-same-process",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    located = ledger.lookup_event_commit("event-same-process")
    assert located == (event("event-same-process", "obs-same-process"), committed)
    assert ledger.project().ledger_sequence == committed.ledger_sequence
    after = ledger.performance_counters()
    assert after.total_replay_calls == before.total_replay_calls
    assert after.historical_replay_calls == before.historical_replay_calls
    ledger.close()


def test_verified_lookup_reuses_immutable_event_after_first_verification(tmp_path) -> None:
    ledger = SQLiteWorldLedger(
        path=tmp_path / "verified-event-cache.sqlite3",
        world_id="world-sqlite-test",
    )
    committed = ledger.commit(
        [event("event-cached", "obs-cached")],
        commit_id="commit-cached",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    assert ledger.lookup_event_commit("event-cached") == (
        event("event-cached", "obs-cached"),
        committed,
    )
    after_first = ledger.performance_counters()
    assert ledger.lookup_event_commit("event-cached") == (
        event("event-cached", "obs-cached"),
        committed,
    )
    after_second = ledger.performance_counters()

    # External writers are still checked by PRAGMA data_version before this
    # cache is consulted. Re-reading one immutable event in the same verified
    # history prefix must not re-project the head or re-hash its whole commit.
    assert after_second.head_projection_reads == after_first.head_projection_reads
    assert after_second.total_replay_calls == after_first.total_replay_calls
    ledger.close()


def test_verified_lookup_revalidates_cross_connection_change_fail_closed(tmp_path) -> None:
    path = tmp_path / "cross-connection-verified-prefix.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-cross-connection", "obs-cross-connection")],
        commit_id="commit-cross-connection",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    # Establish the process-local verified-prefix/head cache before an
    # unsupported writer changes immutable history through another connection.
    assert ledger.lookup_event_commit("event-cross-connection") is not None
    with sqlite3.connect(path) as connection:
        connection.execute(
            """UPDATE world_v2_events
               SET event_json = replace(event_json, 'obs-cross-connection', 'obs-tampered')
               WHERE event_id = 'event-cross-connection'"""
        )

    with pytest.raises(LedgerIntegrityError):
        ledger.lookup_event_commit("event-cross-connection")
    ledger.close()


def test_verified_lookup_accepts_cross_connection_append_only_after_revalidation(tmp_path) -> None:
    path = tmp_path / "cross-connection-append.sqlite3"
    left = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    right = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    before = left.performance_counters()
    committed = right.commit(
        [event("event-external-append", "obs-external-append")],
        commit_id="commit-external-append",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    assert left.lookup_event_commit("event-external-append") == (
        event("event-external-append", "obs-external-append"),
        committed,
    )
    after = left.performance_counters()
    assert after.total_replay_calls == before.total_replay_calls + 1
    left.close()
    right.close()


def test_external_sidecar_write_does_not_replay_unchanged_ledger_history(tmp_path) -> None:
    path = tmp_path / "sidecar-data-version.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    committed = ledger.commit(
        [event("event-sidecar-version", "obs-sidecar-version")],
        commit_id="commit-sidecar-version",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    assert ledger.lookup_event_commit("event-sidecar-version") == (
        event("event-sidecar-version", "obs-sidecar-version"), committed,
    )
    before = ledger.performance_counters()
    with sqlite3.connect(path) as sidecar:
        sidecar.execute("CREATE TABLE IF NOT EXISTS benchmark_sidecar (value TEXT)")
        sidecar.execute("INSERT INTO benchmark_sidecar (value) VALUES ('immutable-content')")

    assert ledger.lookup_event_commit("event-sidecar-version") is not None
    after = ledger.performance_counters()
    assert after.total_replay_calls == before.total_replay_calls
    ledger.close()


@pytest.mark.parametrize(
    "tamper_sql",
    [
        "UPDATE world_v2_commits SET request_hash = 'bad' WHERE commit_id = 'commit-1'",
        """UPDATE world_v2_commits
           SET result_json = '{"world_revision":999,"deliberation_revision":0,
                               "ledger_sequence":999,"event_ids":["event-X"]}'
           WHERE commit_id = 'commit-1'""",
    ],
)
def test_lookup_event_commit_rejects_tampered_commit_metadata(tmp_path, tamper_sql) -> None:
    path = tmp_path / "world-v2.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-1", "obs-1")],
        commit_id="commit-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.close()

    with sqlite3.connect(path) as connection:
        connection.execute(tamper_sql)

    with pytest.raises(LedgerIntegrityError, match="commit (request hash|result)"):
        SQLiteWorldLedger(path=path, world_id="world-sqlite-test")


def test_lookup_event_commit_rejects_coordinated_predecessor_revision_tampering(
    tmp_path,
) -> None:
    path = tmp_path / "world-v2.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    first = ledger.commit(
        [event("event-1", "obs-1")],
        commit_id="commit-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [event("event-2", "obs-2")],
        commit_id="commit-2",
        expected_world_revision=first.world_revision,
        expected_deliberation_revision=first.deliberation_revision,
    )
    ledger.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_events SET world_revision = world_revision + 100"
        )
        connection.execute(
            """UPDATE world_v2_commits
               SET result_json = replace(result_json, '"world_revision":2',
                                                       '"world_revision":102')
               WHERE commit_id = 'commit-2'"""
        )

    with pytest.raises(LedgerIntegrityError, match="revisions are discontinuous"):
        SQLiteWorldLedger(path=path, world_id="world-sqlite-test")


def test_sqlite_ledger_compare_and_swap_across_instances(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    left = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    right = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")

    left.commit(
        [event("event-1", "obs-1")],
        commit_id="commit-left",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    with pytest.raises(ConcurrencyConflict):
        right.commit(
            [event("event-2", "obs-2")],
            commit_id="commit-right",
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
    left.close()
    right.close()


def test_sqlite_rebuild_detects_tampered_event_envelope(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-1", "obs-1")],
        commit_id="commit-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_events SET event_json = replace(event_json, 'obs-1', 'obs-X')"
        )

    with pytest.raises(LedgerIntegrityError):
        SQLiteWorldLedger(path=path, world_id="world-sqlite-test")


def test_sqlite_rebuild_upcasts_verified_legacy_event_bytes(tmp_path) -> None:
    path = tmp_path / "world-v2-legacy.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-legacy", "obs-legacy")],
        commit_id="commit-legacy",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        current_json = connection.execute(
            "SELECT event_json FROM world_v2_events WHERE event_id = 'event-legacy'"
        ).fetchone()[0]
        legacy = json.loads(current_json)
        legacy["schema_version"] = "world-v2.0"
        legacy_payload = json.dumps(
            {"observation_ref": "obs-legacy"},
            sort_keys=True,
            separators=(",", ":"),
        )
        legacy["payload_json"] = legacy_payload
        legacy["payload_hash"] = hashlib.sha256(legacy_payload.encode()).hexdigest()
        legacy_json = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
        connection.execute(
            "UPDATE world_v2_events SET event_json = ?, event_hash = ? "
            "WHERE event_id = 'event-legacy'",
            (legacy_json, hashlib.sha256(legacy_json.encode()).hexdigest()),
        )

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.rebuild() == expected
    reopened.close()


def test_sqlite_rebuild_selects_only_installed_replay_artifacts(tmp_path) -> None:
    path = tmp_path / "world-v2-replay-target.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-replay-target", "obs-replay-target")],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )

    assert (
        ledger.rebuild(
            target_schema_version="world-v2.1",
            reducer_bundle_version=REDUCER_BUNDLE_VERSION,
        )
        == ledger.project()
    )
    with pytest.raises(ValueError, match="not installed"):
        ledger.rebuild(reducer_bundle_version="world-v2-reducers.13")
    with pytest.raises(ValueError, match="not installed"):
        ledger.rebuild(reducer_bundle_version="world-v1-reducers.9")
    with pytest.raises(ValueError, match="target schema.*not installed"):
        ledger.rebuild(target_schema_version="world-v3.0")
    ledger.close()


def test_sqlite_atomically_migrates_verified_v1_head_from_event_bytes(tmp_path) -> None:
    path = tmp_path / "world-v2-v1-head.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-v1-migration", "obs-v1-migration")],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        head_state_json = read_head_state_json(connection, "world-sqlite-test")
        current_state = ReducerState.model_validate_json(head_state_json)
        legacy_payload = current_state.semantic_payload(
            world_id="world-sqlite-test",
            world_revision=1,
            reducer_bundle_version="world-v2-reducers.1",
        )
        legacy_payload.pop("pending_actions")
        for key in (
            "npcs",
            "plans",
            "world_occurrences",
            "outcome_observations",
            "experiences",
            "committed_world_event_refs",
            "appraisals",
            "affect_baselines",
            "affect_episodes",
            "relationship_signals",
            "relationship_adjustments",
            "relationship_states",
            "boundaries",
            "message_observations",
            "operator_observations",
            "actor_authorities",
            "actor_authority_transitions",
            "consumed_actor_root_nonces",
            "capability_grants",
            "capability_transitions",
            "consent_grants",
            "consent_transitions",
            "privacy_policies",
            "privacy_transitions",
            "consumed_authorization_root_nonces",
            "consumed_authorization_challenge_ids",
            "consumed_authorization_source_ids",
        ):
            legacy_payload.pop(key)
        legacy_hash = hashlib.sha256(
            json.dumps(
                legacy_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        legacy_state = current_state.model_dump(mode="json")
        strip_v16_state_fields(legacy_state)
        legacy_state.pop("pending_actions")
        connection.execute(
            "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?",
            (
                json.dumps(legacy_state, separators=(",", ":")),
                legacy_hash,
            ),
        )
        connection.execute("ALTER TABLE world_v2_heads DROP COLUMN reducer_bundle_version")

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()

    with sqlite3.connect(path) as connection:
        migrated = connection.execute(
            "SELECT reducer_bundle_version FROM world_v2_heads"
        ).fetchone()
        assert migrated is not None
        assert migrated[0] == REDUCER_BUNDLE_VERSION
        assert "pending_actions" in json.loads(
            read_head_state_json(connection, "world-sqlite-test")
        )


def test_sqlite_atomically_migrates_verified_v2_head_to_life_bundle(tmp_path) -> None:
    path = tmp_path / "world-v2-v2-head.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-v2-migration", "obs-v2-migration")],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        head_state_json = read_head_state_json(connection, "world-sqlite-test")
        state = ReducerState.model_validate_json(head_state_json)
        legacy_payload = state.semantic_payload(
            world_id="world-sqlite-test",
            world_revision=1,
            reducer_bundle_version="world-v2-reducers.2",
        )
        for key in (
            "npcs",
            "plans",
            "world_occurrences",
            "outcome_observations",
            "experiences",
            "committed_world_event_refs",
            "appraisals",
            "affect_baselines",
            "affect_episodes",
            "relationship_signals",
            "relationship_adjustments",
            "relationship_states",
            "boundaries",
            "message_observations",
            "operator_observations",
            "actor_authorities",
            "actor_authority_transitions",
            "consumed_actor_root_nonces",
            "capability_grants",
            "capability_transitions",
            "consent_grants",
            "consent_transitions",
            "privacy_policies",
            "privacy_transitions",
            "consumed_authorization_root_nonces",
            "consumed_authorization_challenge_ids",
            "consumed_authorization_source_ids",
        ):
            legacy_payload.pop(key)
        legacy_hash = hashlib.sha256(
            json.dumps(
                legacy_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """UPDATE world_v2_heads
               SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?
               WHERE world_id = ?""",
            (
                legacy_state_json(head_state_json),
                legacy_hash,
                "world-v2-reducers.2",
                "world-sqlite-test",
            ),
        )

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_sqlite_atomically_migrates_verified_v3_head_to_appraisal_bundle(tmp_path) -> None:
    path = tmp_path / "world-v2-v3-head.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-v3-migration", "obs-v3-migration")],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        state_json = read_head_state_json(connection, "world-sqlite-test")
        state = ReducerState.model_validate_json(state_json)
        payload = state.semantic_payload(
            world_id="world-sqlite-test",
            world_revision=1,
            reducer_bundle_version="world-v2-reducers.3",
        )
        payload.pop("appraisals")
        payload.pop("affect_baselines")
        payload.pop("affect_episodes")
        payload.pop("relationship_signals")
        payload.pop("relationship_adjustments")
        payload.pop("relationship_states")
        payload.pop("boundaries")
        payload.pop("message_observations")
        payload.pop("operator_observations")
        payload.pop("actor_authorities")
        payload.pop("actor_authority_transitions")
        payload.pop("consumed_actor_root_nonces")
        for key in (
            "capability_grants", "capability_transitions", "consent_grants",
            "consent_transitions", "privacy_policies", "privacy_transitions",
            "consumed_authorization_root_nonces", "consumed_authorization_challenge_ids",
            "consumed_authorization_source_ids",
        ):
            payload.pop(key)
        for ref in payload["committed_world_event_refs"]:
            ref.pop("continuation_refs", None)
        legacy_hash = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """UPDATE world_v2_heads
               SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?
               WHERE world_id = ?""",
            (
                legacy_state_json(state_json),
                legacy_hash,
                "world-v2-reducers.3",
                "world-sqlite-test",
            ),
        )

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_sqlite_rejects_unreleased_v4_bundle_instead_of_reinterpreting_it(
    tmp_path,
) -> None:
    path = tmp_path / "world-v2-unreleased-v4.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_heads SET reducer_bundle_version = ?",
            ("world-v2-reducers.4",),
        )

    with pytest.raises(LedgerIntegrityError, match="no migration path"):
        SQLiteWorldLedger(path=path, world_id="world-sqlite-test")


def test_sqlite_migrates_v5_head_without_affect_projection_fields(tmp_path) -> None:
    path = tmp_path / "world-v2-v5-head.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-v5-migration", "obs-v5-migration")],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = ledger.project()
    ledger.close()

    with sqlite3.connect(path) as connection:
        raw_state = json.loads(read_head_state_json(connection, "world-sqlite-test"))
        state = ReducerState.model_validate_json(json.dumps(raw_state, separators=(",", ":")))
        semantic = state.semantic_payload(
            world_id="world-sqlite-test",
            world_revision=1,
            reducer_bundle_version="world-v2-reducers.5",
        )
        semantic.pop("affect_baselines")
        semantic.pop("affect_episodes")
        semantic.pop("relationship_signals")
        semantic.pop("relationship_adjustments")
        semantic.pop("relationship_states")
        semantic.pop("boundaries")
        semantic.pop("actor_authorities")
        semantic.pop("actor_authority_transitions")
        semantic.pop("consumed_actor_root_nonces")
        for key in (
            "capability_grants", "capability_transitions", "consent_grants",
            "consent_transitions", "privacy_policies", "privacy_transitions",
            "consumed_authorization_root_nonces", "consumed_authorization_challenge_ids",
            "consumed_authorization_source_ids",
        ):
            semantic.pop(key)
        legacy_hash = hashlib.sha256(
            json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        raw_state.pop("affect_baselines", None)
        raw_state.pop("affect_episodes", None)
        raw_state.pop("affect_proposals", None)
        raw_state.pop("affect_proposal_ids", None)
        strip_v16_state_fields(raw_state)
        connection.execute(
            """UPDATE world_v2_heads
               SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?
               WHERE world_id = ?""",
            (
                json.dumps(raw_state, separators=(",", ":")),
                legacy_hash,
                "world-v2-reducers.5",
                "world-sqlite-test",
            ),
        )

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


@pytest.mark.parametrize(
    ("case", "legacy_payload"),
    [
        ("minimal", {"status": "rejected", "acceptance_id": "legacy:acceptance"}),
        (
            "partial",
            {
                "status": "rejected",
                "acceptance_id": "legacy:acceptance",
                "proposal_id": "legacy:proposal",
            },
        ),
        (
            "unknown-proposal",
            {
                "status": "rejected",
                "acceptance_id": "legacy:acceptance",
                "proposal_id": "legacy:unknown",
                "evaluated_world_revision": 0,
            },
        ),
    ],
)
def test_sqlite_isolates_legacy_v3_unbound_acceptance_audit(
    tmp_path, case: str, legacy_payload: dict[str, object]
) -> None:
    path = tmp_path / f"world-v2-v3-legacy-acceptance-{case}.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")

    def audit_event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
        identity = domain_idempotency_key(
            event_type=event_type,
            world_id="world-sqlite-test",
            payload=payload,
        )
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id="world-sqlite-test",
            event_type=event_type,
            logical_time=NOW,
            created_at=NOW,
            actor="system:test",
            source="test",
            trace_id="trace:legacy-acceptance",
            causation_id=f"cause:{event_id}",
            correlation_id="correlation:legacy-acceptance",
            idempotency_key=identity or f"identity:{event_id}",
            payload=payload,
        )

    ledger.commit(
        [
            audit_event(
                "legacy-proposal",
                "ProposalRecorded",
                {"proposal_id": "legacy:proposal", "evaluated_world_revision": 0},
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [
            audit_event(
                "legacy-acceptance",
                "AcceptanceRecorded",
                {
                    "status": "rejected",
                    "acceptance_id": "legacy:acceptance",
                    "proposal_id": "legacy:proposal",
                    "evaluated_world_revision": 0,
                },
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=1,
    )
    ledger.close()

    with sqlite3.connect(path) as connection:
        raw_event = json.loads(
            connection.execute(
                "SELECT event_json FROM world_v2_events WHERE event_id = ?",
                ("legacy-acceptance",),
            ).fetchone()[0]
        )
        raw_event["payload_json"] = json.dumps(
            legacy_payload, sort_keys=True, separators=(",", ":")
        )
        raw_event["payload_hash"] = hashlib.sha256(
            raw_event["payload_json"].encode("utf-8")
        ).hexdigest()
        encoded_event = json.dumps(
            raw_event,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        connection.execute(
            "UPDATE world_v2_events SET event_json = ?, event_hash = ? WHERE event_id = ?",
            (
                encoded_event,
                hashlib.sha256(encoded_event.encode("utf-8")).hexdigest(),
                "legacy-acceptance",
            ),
        )
        raw_state = json.loads(read_head_state_json(connection, "world-sqlite-test"))
        for key in ("proposal_ids", "proposal_revisions", "acceptance_decisions"):
            raw_state.pop(key, None)
        strip_v16_state_fields(raw_state)
        acceptance_ref = next(
            item
            for item in raw_state["committed_world_event_refs"]
            if item["event_id"] == "legacy-acceptance"
        )
        acceptance_ref["payload_hash"] = raw_event["payload_hash"]
        acceptance_ref.pop("continuation_refs", None)
        legacy_state = ReducerState.model_validate_json(
            json.dumps(raw_state, separators=(",", ":"))
        )
        semantic = legacy_state.semantic_payload(
            world_id="world-sqlite-test",
            world_revision=1,
            reducer_bundle_version="world-v2-reducers.3",
        )
        semantic.pop("appraisals")
        semantic.pop("affect_baselines")
        semantic.pop("affect_episodes")
        semantic.pop("relationship_signals")
        semantic.pop("relationship_adjustments")
        semantic.pop("relationship_states")
        semantic.pop("boundaries")
        semantic.pop("message_observations")
        semantic.pop("operator_observations")
        semantic.pop("actor_authorities")
        semantic.pop("actor_authority_transitions")
        semantic.pop("consumed_actor_root_nonces")
        for key in (
            "capability_grants", "capability_transitions", "consent_grants",
            "consent_transitions", "privacy_policies", "privacy_transitions",
            "consumed_authorization_root_nonces", "consumed_authorization_challenge_ids",
            "consumed_authorization_source_ids",
        ):
            semantic.pop(key)
        for ref in semantic["committed_world_event_refs"]:
            ref.pop("continuation_refs", None)
        legacy_hash = hashlib.sha256(
            json.dumps(
                semantic,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        connection.execute(
            """UPDATE world_v2_heads
               SET state_json = ?, semantic_hash = ?, reducer_bundle_version = ?
               WHERE world_id = ?""",
            (
                json.dumps(raw_state, separators=(",", ":")),
                legacy_hash,
                "world-v2-reducers.3",
                "world-sqlite-test",
            ),
        )

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project().acceptance_decisions == ()
    assert reopened.project().committed_world_event_refs[-1].event_type == (
        "LegacyAcceptanceAuditRecorded"
    )
    assert reopened.rebuild() == reopened.project()
    reopened.close()


def test_sqlite_project_normalizes_malformed_head_as_integrity_error(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE world_v2_heads SET world_revision = 'not-an-integer'")

    with pytest.raises(LedgerIntegrityError, match="cursor"):
        SQLiteWorldLedger(path=path, world_id="world-sqlite-test")


def test_sqlite_checkpoint_hash_binds_full_projection_cursor(tmp_path) -> None:
    path = tmp_path / "world-v2-cursor-tamper.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [event("event-cursor", "obs-cursor")],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.close()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE world_v2_heads SET deliberation_revision = 9, ledger_sequence = 9"
        )

    with pytest.raises(LedgerIntegrityError, match="state hash"):
        SQLiteWorldLedger(path=path, world_id="world-sqlite-test")


def test_sqlite_head_preserves_authorized_actions_across_restart(tmp_path) -> None:
    path = tmp_path / "world-v2.sqlite3"
    action = Action(
        schema_version="world-v2.1",
        action_id="action-1",
        world_id="world-sqlite-test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        kind="reply",
        layer="external_action",
        intent_ref="intent-1",
        actor="companion:test",
        target="user:test",
        payload_ref="payload:1",
        payload_hash="sha256:payload-1",
        idempotency_key="world-sqlite-test:intent-1:reply",
        budget_reservation_id="budget-1",
        state="authorized",
        recovery_policy="effect_once",
    )
    authorized = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event-action-authorized-1",
        world_id="world-sqlite-test",
        event_type="ActionAuthorized",
        logical_time=NOW,
        created_at=NOW,
        actor="system:acceptance",
        source="acceptance",
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        idempotency_key="action-authorized:action-1",
        payload={"action": action.model_dump(mode="json")},
    )
    reservation = BudgetReservation(
        reservation_id="budget-1",
        account_id="budget-account-chat",
        action_id="action-1",
        category="chat",
        amount_limit=10_000,
    )
    account = BudgetAccount(
        account_id="budget-account-chat",
        category="chat",
        window_id="test-window",
        limit=1_000_000,
    )
    configured = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event-budget-account-configured",
        world_id="world-sqlite-test",
        event_type="BudgetAccountConfigured",
        logical_time=NOW,
        created_at=NOW,
        actor="system:acceptance",
        source="acceptance",
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        idempotency_key="budget-account:chat:test-window",
        payload={"account": account.model_dump(mode="json")},
    )
    reserved = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event-budget-reserved-1",
        world_id="world-sqlite-test",
        event_type="BudgetReserved",
        logical_time=NOW,
        created_at=NOW,
        actor="system:acceptance",
        source="acceptance",
        trace_id="trace-action-1",
        causation_id="acceptance-1",
        correlation_id="conversation-1",
        idempotency_key="budget-reserved:budget-1",
        payload={"reservation": reservation.model_dump(mode="json")},
    )

    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [configured, reserved, authorized],
        commit_id="commit-action-1",
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    assert reopened.project().actions == (action,)
    assert reopened.rebuild() == reopened.project()
    reopened.close()


def test_budget_overrun_with_other_reservations_survives_restart(tmp_path) -> None:
    path = tmp_path / "world-v2-budget-overrun.sqlite3"

    def domain_event(event_id: str, event_type: str, payload: dict[str, object]):
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id="world-sqlite-test",
            event_type=event_type,
            logical_time=NOW,
            created_at=NOW,
            actor="system:test",
            source="test",
            trace_id="trace-budget-overrun",
            causation_id="acceptance-budget-overrun",
            correlation_id="conversation-budget-overrun",
            idempotency_key=event_id,
            payload=payload,
        )

    account = BudgetAccount(
        account_id="account-chat",
        category="chat",
        window_id="window-1",
        limit=100,
    )
    first = BudgetReservation(
        reservation_id="reservation-1",
        account_id=account.account_id,
        action_id="action-1",
        category="chat",
        amount_limit=60,
    )
    second = BudgetReservation(
        reservation_id="reservation-2",
        account_id=account.account_id,
        action_id="action-2",
        category="chat",
        amount_limit=40,
    )
    ledger = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    ledger.commit(
        [
            domain_event(
                "event-account",
                "BudgetAccountConfigured",
                {"account": account.model_dump(mode="json")},
            ),
            domain_event(
                "event-reservation-1",
                "BudgetReserved",
                {"reservation": first.model_dump(mode="json")},
            ),
            domain_event(
                "event-reservation-2",
                "BudgetReserved",
                {"reservation": second.model_dump(mode="json")},
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    settlement = BudgetSettlement(
        settlement_id="settlement-1",
        reservation_id=first.reservation_id,
        action_id=first.action_id,
        result_id="result-1",
        state="settled",
        cost_actual=120,
        cost_delta=120,
    )
    ledger.commit(
        [
            domain_event(
                "event-settlement",
                "BudgetSettled",
                {"settlement": settlement.model_dump(mode="json")},
            )
        ],
        expected_world_revision=3,
        expected_deliberation_revision=0,
    )
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    projection = reopened.project()
    assert projection.budget_accounts[0].spent == 120
    assert projection.budget_accounts[0].reserved == 40
    assert projection.budget_accounts[0].overrun == 20
    assert reopened.rebuild() == projection
    reopened.close()


def test_empty_aspirations_stay_out_of_durable_state_bytes_and_hash() -> None:
    """A field added within one bundle must not invalidate pre-existing heads.

    ``aspirations`` joined ``ReducerState`` without a reducer-bundle bump, so
    worlds persisted before the field existed must recompute byte-identical
    state hashes.  An empty tuple therefore stays out of the durable dump
    entirely; the key may appear only once a world actually plants one.
    """

    from companion_daemon.world_v2.reducers import ReducerState
    from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger

    dumped = SQLiteWorldLedger._state_dump(ReducerState())

    assert "aspirations" not in dumped
    assert '"aspirations"' not in SQLiteWorldLedger._encode_state(ReducerState())
