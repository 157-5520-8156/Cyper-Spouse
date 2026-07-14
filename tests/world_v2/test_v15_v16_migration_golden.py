from __future__ import annotations

import base64
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import ProjectionCursor
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "v15_v16_migration_golden.json"
)
EVENT_BACKED_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "v15_world_occurrence.sqlite.sql.gz.b64"
)
V15_WORLD_ID = "world:v15-v16-golden"
V15_WORLD_REVISION = 42
V15_SOURCE_COMMIT = "72eae89"
V15_SEMANTIC_HASH = (
    "733999720952251d7690393d56561bcd41180b6334c1630d6087e2bb5a008429"
)
EVENT_BACKED_WORLD_ID = "world-v2-life-test"
EVENT_LOG_HASH = "38f8a60540e2b620d9824309d9585c021bd796f78cbb30c2f6f6c7136a1d597e"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _legacy_verifier() -> SQLiteWorldLedger:
    # _legacy_semantic_hash is deliberately a pure migration boundary: it
    # needs the persisted world identity but no live SQLite connection.
    ledger = object.__new__(SQLiteWorldLedger)
    ledger._world_id = V15_WORLD_ID
    return ledger


def _restore_event_backed_database(path: Path) -> None:
    encoded = "".join(EVENT_BACKED_FIXTURE_PATH.read_text(encoding="ascii").splitlines())
    sql = gzip.decompress(base64.b64decode(encoded)).decode("utf-8")
    with sqlite3.connect(path) as connection:
        connection.executescript(sql)


def _event_log_hash(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT * FROM world_v2_events ORDER BY ledger_sequence"
        ).fetchall()
    assert len(rows) == 16
    encoded = json.dumps(
        rows, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def test_v15_golden_is_literal_and_covers_nonempty_authority_domains() -> None:
    fixture = _fixture()
    assert fixture == {
        "semantic_hash": V15_SEMANTIC_HASH,
        "source_commit": V15_SOURCE_COMMIT,
        "state_json": fixture["state_json"],
        "world_id": V15_WORLD_ID,
        "world_revision": V15_WORLD_REVISION,
    }
    state_json = fixture["state_json"]
    assert isinstance(state_json, str)
    raw = json.loads(state_json)

    for field in (
        "message_observations",
        "threads",
        "thread_transitions",
        "commitments",
        "commitment_transitions",
        "facts",
        "fact_transitions",
        "experiences",
        "experience_transitions",
        "memory_candidates",
        "memory_candidate_transitions",
        "character_core_transitions",
    ):
        assert raw[field], f"frozen .15 golden must exercise {field}"
    assert raw["character_core"] is not None

    occurrence = raw["world_occurrences"][0]
    assert occurrence["status"] == "settled"
    assert occurrence["candidate_outcome_refs"] == [
        "outcome:legacy-success",
        "outcome:legacy-other",
    ]
    assert "settled_outcome_ref" not in occurrence


def test_v15_golden_hash_is_verified_by_the_v16_legacy_reader() -> None:
    fixture = _fixture()
    state_json = fixture["state_json"]
    assert isinstance(state_json, str)

    actual = _legacy_verifier()._legacy_semantic_hash(
        state_json=state_json,
        world_revision=V15_WORLD_REVISION,
        reducer_bundle_version="world-v2-reducers.15",
    )
    assert actual == V15_SEMANTIC_HASH

    # Missing the chosen outcome is accepted only in the explicit legacy
    # parse context. A live .16 state cannot silently enter this shape.
    with pytest.raises(ValueError, match="settled occurrence requires one candidate"):
        ReducerState.model_validate_json(state_json)
    legacy_state = ReducerState.model_validate_json(
        state_json,
        context={"source_reducer_bundle": "world-v2-reducers.15"},
    )
    assert legacy_state.world_occurrences[0].settled_outcome_ref is None


def test_v15_reader_rejects_injected_v16_outcome_field() -> None:
    fixture = _fixture()
    state_json = fixture["state_json"]
    assert isinstance(state_json, str)
    raw = json.loads(state_json)
    raw["world_occurrences"][0]["settled_outcome_ref"] = "outcome:legacy-success"

    with pytest.raises(LedgerIntegrityError, match="legacy head state is invalid"):
        _legacy_verifier()._legacy_semantic_hash(
            state_json=json.dumps(
                raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            world_revision=V15_WORLD_REVISION,
            reducer_bundle_version="world-v2-reducers.15",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("owner_actor_ref", "actor:forged"),
        ("authority_origin", {"accepted_event_ref": "event:forged"}),
    ),
)
def test_v15_reader_rejects_injected_plan_authority_field(
    field: str, value: object
) -> None:
    fixture = _fixture()
    state_json = fixture["state_json"]
    assert isinstance(state_json, str)
    raw = json.loads(state_json)
    raw["plans"] = [{field: value}]

    with pytest.raises(LedgerIntegrityError, match="legacy head state is invalid"):
        _legacy_verifier()._legacy_semantic_hash(
            state_json=json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            world_revision=V15_WORLD_REVISION,
            reducer_bundle_version="world-v2-reducers.15",
        )


def test_event_backed_v15_sqlite_migrates_reopens_and_replays_v16(
    tmp_path: Path,
) -> None:
    path = tmp_path / "event-backed-v15.sqlite3"
    _restore_event_backed_database(path)

    with sqlite3.connect(path) as connection:
        legacy_head = connection.execute(
            "SELECT world_revision, deliberation_revision, ledger_sequence, "
            "reducer_bundle_version, semantic_hash FROM world_v2_heads "
            "WHERE world_id = ?",
            (EVENT_BACKED_WORLD_ID,),
        ).fetchone()
    assert legacy_head == (
        11,
        5,
        16,
        "world-v2-reducers.15",
        "d6ff61c1185f101ec93d6509c5efdc93ddabd28e97c337fb846cf4d015fc9e59",
    )
    assert _event_log_hash(path) == EVENT_LOG_HASH

    first_ledger = SQLiteWorldLedger(path=path, world_id=EVENT_BACKED_WORLD_ID)
    first = first_ledger.project()
    assert first.reducer_bundle_version == "world-v2-reducers.17"
    assert (
        first.world_revision,
        first.deliberation_revision,
        first.ledger_sequence,
    ) == (11, 5, 16)
    assert len(first.world_occurrences) == 1
    occurrence = first.world_occurrences[0]
    assert occurrence.status == "settled"
    assert occurrence.settled_outcome_ref == "result:tea-good"
    assert len(first.clock_transition_history) == 1
    clock = first.clock_transition_history[0]
    assert clock.computed_world_revision == 1
    assert clock.clock_event_ref == "clock-life"
    assert first.goals == ()

    settled_cursor = ProjectionCursor(
        world_revision=9,
        deliberation_revision=4,
        ledger_sequence=13,
    )
    settled_first = first_ledger.project_at(settled_cursor)
    assert settled_first.world_occurrences[0].settled_outcome_ref == "result:tea-good"
    assert settled_first.clock_transition_history == first.clock_transition_history
    assert first_ledger.rebuild() == first
    head_cursor = ProjectionCursor(
        world_revision=first.world_revision,
        deliberation_revision=first.deliberation_revision,
        ledger_sequence=first.ledger_sequence,
    )
    assert first_ledger.project_at(head_cursor) == first
    first_ledger.close()
    assert _event_log_hash(path) == EVENT_LOG_HASH

    second_ledger = SQLiteWorldLedger(path=path, world_id=EVENT_BACKED_WORLD_ID)
    second = second_ledger.project()
    assert second == first
    assert second_ledger.rebuild() == second
    assert second_ledger.project_at(head_cursor) == second
    assert second_ledger.project_at(settled_cursor) == settled_first
    second_ledger.close()
    assert _event_log_hash(path) == EVENT_LOG_HASH


@pytest.mark.parametrize(
    "field",
    (
        "clock_transition_history",
        "goals",
        "goal_transitions",
        "goal_proposals",
        "goal_proposal_ids",
    ),
)
@pytest.mark.parametrize("injected", ([], [{"forged": True}]))
def test_v15_reader_rejects_each_v16_only_authority_key_even_when_empty(
    field: str,
    injected: list[object],
) -> None:
    fixture = _fixture()
    state_json = fixture["state_json"]
    assert isinstance(state_json, str)
    raw = json.loads(state_json)
    raw[field] = injected

    with pytest.raises(LedgerIntegrityError, match="legacy head state is invalid"):
        _legacy_verifier()._legacy_semantic_hash(
            state_json=json.dumps(
                raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            world_revision=V15_WORLD_REVISION,
            reducer_bundle_version="world-v2-reducers.15",
        )
