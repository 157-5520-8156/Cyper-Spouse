from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import sqlite3

import pytest

from companion_daemon.world_v2.errors import ConcurrencyConflict, LedgerIntegrityError
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    BudgetReservation,
    BudgetSettlement,
    WorldEvent,
)
from companion_daemon.world_v2.reducers import ReducerState
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

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    with pytest.raises(LedgerIntegrityError, match="commit (request hash|result)"):
        reopened.lookup_event_commit("event-1")
    reopened.close()


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

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    with pytest.raises(LedgerIntegrityError, match="revisions are discontinuous"):
        reopened.lookup_event_commit("event-2")
    reopened.close()


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

    reopened = SQLiteWorldLedger(path=path, world_id="world-sqlite-test")
    with pytest.raises(LedgerIntegrityError):
        reopened.rebuild()
    reopened.close()


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
            reducer_bundle_version="world-v2-reducers.8",
        )
        == ledger.project()
    )
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
        head = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?",
            ("world-sqlite-test",),
        ).fetchone()
        assert head is not None
        current_state = ReducerState.model_validate_json(head[0])
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
            "SELECT reducer_bundle_version, state_json FROM world_v2_heads"
        ).fetchone()
        assert migrated is not None
        assert migrated[0] == "world-v2-reducers.8"
        assert "pending_actions" in json.loads(migrated[1])


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
        head = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?",
            ("world-sqlite-test",),
        ).fetchone()
        assert head is not None
        state = ReducerState.model_validate_json(head[0])
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
               SET semantic_hash = ?, reducer_bundle_version = ?
               WHERE world_id = ?""",
            (legacy_hash, "world-v2-reducers.2", "world-sqlite-test"),
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
        state_json = connection.execute(
            "SELECT state_json FROM world_v2_heads WHERE world_id = ?",
            ("world-sqlite-test",),
        ).fetchone()[0]
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
               SET semantic_hash = ?, reducer_bundle_version = ?
               WHERE world_id = ?""",
            (legacy_hash, "world-v2-reducers.3", "world-sqlite-test"),
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
        raw_state = json.loads(
            connection.execute(
                "SELECT state_json FROM world_v2_heads WHERE world_id = ?",
                ("world-sqlite-test",),
            ).fetchone()[0]
        )
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
        raw_state = json.loads(
            connection.execute(
                "SELECT state_json FROM world_v2_heads WHERE world_id = ?",
                ("world-sqlite-test",),
            ).fetchone()[0]
        )
        for key in ("proposal_ids", "proposal_revisions", "acceptance_decisions"):
            raw_state.pop(key, None)
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
