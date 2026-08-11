from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import unicodedata

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.interaction_act_schemas import (
    InteractionActMutation,
    InteractionActParticipantStatus,
    InteractionActProjection,
    InteractionActSourceRef,
)
from companion_daemon.world_v2.proposal_audit_schemas import RecordedModelResultAudit
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import (
    InteractionActProposalProjection,
    Observation,
    WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 8, 11, 16, 0, tzinfo=UTC)
WORLD = "world:v54-migration"
V53 = "world-v2-reducers.53"
V54 = "world-v2-reducers.54"
V55 = "world-v2-reducers.55"
V56 = "world-v2-reducers.56"
EC50_SOURCE_COMMIT = "ec50d9f28e33459272f644fd4900673509bd045f"
V54_SOURCE_COMMIT = "3fe665570554bd8e95acd1040822a2eb070e6dc0"
V55_SOURCE_COMMIT = "2fd930a66ba6b49e322997e30a65b8551ac9e0a2"


EC50_V53_SEED_SCRIPT = textwrap.dedent(
    """
    from datetime import UTC, datetime
    import hashlib
    import json
    from pathlib import Path
    import sys

    from companion_daemon.world_v2.event_identity import domain_idempotency_key
    from companion_daemon.world_v2 import reducers
    from companion_daemon.world_v2.schemas import Observation, WorldEvent
    from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


    path = Path(sys.argv[1])
    world_id = sys.argv[2]
    now = datetime(2026, 8, 11, 16, 0, tzinfo=UTC)
    text = "真实 ec50 旧消息"
    observation = Observation(
        schema_version="world-v2.1",
        observation_id="observation:authentic-ec50",
        world_id=world_id,
        logical_time=now,
        created_at=now,
        trace_id="trace:authentic-ec50",
        causation_id="qq:message:authentic-ec50",
        correlation_id="qq:message:authentic-ec50",
        source="platform:qq",
        source_event_id="qq:message:authentic-ec50",
        actor="user:primary",
        channel="qq",
        payload_ref="ingress:qq:message:authentic-ec50",
        payload_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        received_at=now,
        reply_context={"target": "conversation:qq:c2c:primary"},
    )
    payload = observation.model_dump(mode="json")
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:observation:authentic-ec50",
        world_id=world_id,
        event_type="ObservationRecorded",
        logical_time=now,
        created_at=now,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded",
            world_id=world_id,
            payload=payload,
        )
        or observation.observation_id,
        payload=payload,
    )
    ledger = SQLiteWorldLedger(path=path, world_id=world_id)
    result = ledger.commit(
        (event,),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    projection = ledger.project()
    ledger.close()
    print(
        json.dumps(
            {
                "cursor": [
                    result.world_revision,
                    result.deliberation_revision,
                    result.ledger_sequence,
                ],
                "module_file": reducers.__file__,
                "projection_bundle": projection.reducer_bundle_version,
                "reducer_bundle": reducers.REDUCER_BUNDLE_VERSION,
            },
            sort_keys=True,
        )
    )
    """
)


V55_STREAM_AUDIT_APPEND_SCRIPT = textwrap.dedent(
    """
    from datetime import UTC, datetime
    import hashlib
    import json
    from pathlib import Path
    import sys

    from companion_daemon.world_v2.deliberation import (
        DeliberationResult,
        ModelResultAudit,
        ModelRoute,
        PhysicalProviderInvocationAudit,
    )
    from companion_daemon.world_v2.proposal_audit import (
        ProposalAuditContext,
        ProposalAuditRecorder,
    )
    from companion_daemon.world_v2 import reducers
    from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


    def digest(value):
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


    path = Path(sys.argv[1])
    world_id = sys.argv[2]
    now = datetime(2026, 8, 11, 16, 1, tzinfo=UTC)
    ledger = SQLiteWorldLedger(path=path, world_id=world_id)
    before = ledger.project()
    physical_call_id = "model-call:authentic-v55:physical"
    head_call_id = "model-call:authentic-v55:head"
    tail_call_id = "model-call:authentic-v55:tail"
    request_hash = hashlib.sha256(b"authentic-v55-physical-request").hexdigest()
    tail_response_hash = hashlib.sha256(b"authentic-v55-tail-response").hexdigest()
    physical = PhysicalProviderInvocationAudit(
        model_call_id=physical_call_id,
        request_hash=request_hash,
        model_id="model:authentic-v55",
        model_version="2026-08",
        outcome="completed",
        response_hash=hashlib.sha256(b"authentic-v55-full-response").hexdigest(),
        usage_status="unresolved",
        semantic_model_call_ids=(head_call_id, tail_call_id),
    )
    tail = ModelResultAudit(
        model_call_id=tail_call_id,
        parent_model_call_id=physical_call_id,
        semantic_stream_part="tail",
        model_result_ref=(
            "model-result:"
            + digest(
                {
                    "model_call_id": tail_call_id,
                    "response_hash": tail_response_hash,
                }
            )
        ),
        attempt_id="attempt:authentic-v55:stream-tail",
        route=ModelRoute(
            tier="flash",
            reason_code="authentic_v55_stream_tail",
            router_version="router.1",
        ),
        model_id="model:authentic-v55",
        model_version="2026-08",
        request_hash=request_hash,
        response_hash=tail_response_hash,
        status="candidate_returned",
        slot="primary",
        outcome="returned",
        physical_provider_audits=(physical,),
    )
    capsule_id = hashlib.sha256(b"authentic-v55-capsule").hexdigest()
    result = DeliberationResult(
        result_id=(
            "deliberation:"
            + digest(
                {
                    "capsule_id": capsule_id,
                    "proposal_hash": None,
                    "attempt_audits": [tail.model_dump(mode="json")],
                }
            )
        ),
        capsule_id=capsule_id,
        proposal=None,
        audit=tail,
        attempt_audits=(tail,),
    )
    committed = ProposalAuditRecorder(ledger=ledger).record(
        result,
        ProposalAuditContext(
            world_id=world_id,
            trigger_ref="trigger:authentic-v55:stream-tail",
            logical_time=now,
            created_at=now,
            actor="character:celia",
            source="world-v2-deliberation",
            trace_id="trace:authentic-v55:stream-tail",
            causation_id="attempt:authentic-v55:stream-tail",
            correlation_id="trigger:authentic-v55:stream-tail",
            evaluated_world_revision=before.world_revision,
            expected_commit_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
            expected_ledger_sequence=before.ledger_sequence,
        ),
    )
    projection = ledger.project()
    ledger.close()
    print(
        json.dumps(
            {
                "cursor": [
                    committed.world_revision,
                    committed.deliberation_revision,
                    committed.cursor.ledger_sequence,
                ],
                "projection_bundle": projection.reducer_bundle_version,
                "reducer_bundle": reducers.REDUCER_BUNDLE_VERSION,
                "audit_contracts": [
                    audit.audit_contract for audit in projection.model_result_audits
                ],
            },
            sort_keys=True,
        )
    )
    """
)


def _message_event() -> WorldEvent:
    observation = Observation(
        schema_version="world-v2.1",
        observation_id="observation:v53-message",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:v53-message",
        causation_id="qq:message:v53",
        correlation_id="qq:message:v53",
        source="platform:qq",
        source_event_id="qq:message:v53",
        actor="user:primary",
        channel="qq",
        payload_ref="ingress:qq:message:v53",
        payload_hash="sha256:" + hashlib.sha256("旧消息".encode()).hexdigest(),
        text="旧消息",
        received_at=NOW,
        reply_context={"target": "conversation:qq:c2c:primary"},
    )
    payload = observation.model_dump(mode="json")
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:observation:v53-message",
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="ObservationRecorded",
            world_id=WORLD,
            payload=payload,
        )
        or observation.observation_id,
        payload=payload,
    )


def _build_archived_database(
    *,
    tmp_path: Path,
    database_path: Path,
    source_commit: str,
    expected_bundle: str,
    source_name: str,
    append_v55_stream_audit: bool = False,
) -> tuple[int, int, int]:
    repository_root = Path(__file__).resolve().parents[2]
    resolved = subprocess.run(
        ["git", "rev-parse", f"{source_commit}^{{commit}}"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert resolved == source_commit

    source_root = tmp_path / source_name
    source_root.mkdir()
    archive_path = tmp_path / "ec50-source.tar"
    with archive_path.open("wb") as archive_stream:
        subprocess.run(
            [
                "git",
                "archive",
                "--format=tar",
                source_commit,
                "src/companion_daemon",
            ],
            cwd=repository_root,
            check=True,
            stdout=archive_stream,
        )
    with tarfile.open(archive_path, mode="r:") as archive:
        archive.extractall(source_root, filter="data")

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    seeded = subprocess.run(
        [sys.executable, "-c", EC50_V53_SEED_SCRIPT, str(database_path), WORLD],
        cwd=source_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    metadata = json.loads(seeded.stdout.splitlines()[-1])
    assert metadata["reducer_bundle"] == expected_bundle
    assert metadata["projection_bundle"] == expected_bundle
    assert Path(str(metadata["module_file"])).is_relative_to(source_root)
    cursor = metadata["cursor"]
    if append_v55_stream_audit:
        appended = subprocess.run(
            [
                sys.executable,
                "-c",
                V55_STREAM_AUDIT_APPEND_SCRIPT,
                str(database_path),
                WORLD,
            ],
            cwd=source_root,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        metadata = json.loads(appended.stdout.splitlines()[-1])
        assert metadata["reducer_bundle"] == expected_bundle
        assert metadata["projection_bundle"] == expected_bundle
        assert metadata["audit_contracts"] == [
            "model-result-audit.6",
            "model-result-audit.6",
        ]
        cursor = metadata["cursor"]
    assert isinstance(cursor, list)
    assert len(cursor) == 3
    assert all(isinstance(value, int) for value in cursor)
    return int(cursor[0]), int(cursor[1]), int(cursor[2])


def _event_rows_as_bytes(path: Path) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            """SELECT world_id, ledger_sequence, world_revision,
                      deliberation_revision, commit_id, event_id,
                      idempotency_key, event_json, event_hash
               FROM world_v2_events ORDER BY ledger_sequence"""
        ).fetchall()
    return tuple((*row[:7], str(row[7]).encode("utf-8"), row[8]) for row in rows)


def _head_coordinates(path: Path) -> tuple[int, int, int, str]:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """SELECT world_revision, deliberation_revision, ledger_sequence,
                      reducer_bundle_version
               FROM world_v2_heads WHERE world_id = ?""",
            (WORLD,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1]), int(row[2]), str(row[3])


def test_authentic_v53_message_head_migrates_to_v56_without_rewriting_history(
    tmp_path,
) -> None:
    path = tmp_path / "v53-message-head.sqlite3"
    source_cursor = _build_archived_database(
        tmp_path=tmp_path,
        database_path=path,
        source_commit=EC50_SOURCE_COMMIT,
        expected_bundle=V53,
        source_name="ec50-v53-source",
    )
    old_events = _event_rows_as_bytes(path)
    old_head = _head_coordinates(path)
    assert old_events
    assert old_head == (*source_cursor, V53)

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    projection = migrated.project()
    assert projection.reducer_bundle_version == V56
    assert projection.message_observations[0].actor == "user:primary"
    assert projection.message_observations[0].channel == "qq"
    assert (
        projection.message_observations[0].payload_ref
        == "ingress:qq:message:authentic-ec50"
    )
    expected_text_hash = "sha256:" + hashlib.sha256(
        unicodedata.normalize("NFC", "真实 ec50 旧消息").encode("utf-8")
    ).hexdigest()
    assert projection.message_observations[0].normalized_text_hash == expected_text_hash
    assert projection.message_observations[0].reply_context_present is True
    assert projection.message_observations[0].reply_target == "conversation:qq:c2c:primary"
    assert (
        projection.world_revision,
        projection.deliberation_revision,
        projection.ledger_sequence,
    ) == old_head[:3]
    assert migrated.rebuild() == projection
    migrated.close()

    assert _event_rows_as_bytes(path) == old_events
    assert _head_coordinates(path) == (*old_head[:3], V56)

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    cold_projection = reopened.project()
    assert cold_projection == projection
    assert reopened.rebuild() == projection
    reopened.close()
    assert _event_rows_as_bytes(path) == old_events


def test_v56_head_survives_cold_reopen_and_full_rebuild(tmp_path) -> None:
    path = tmp_path / "v56-cold-reopen.sqlite3"
    first = SQLiteWorldLedger(path=path, world_id=WORLD)
    first.commit(
        (_message_event(),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    expected = first.project()
    assert expected.reducer_bundle_version == V56
    first.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_authentic_v54_head_migrates_to_v56_without_rewriting_history(
    tmp_path,
) -> None:
    path = tmp_path / "v54-to-v56.sqlite3"
    source_cursor = _build_archived_database(
        tmp_path=tmp_path,
        database_path=path,
        source_commit=V54_SOURCE_COMMIT,
        expected_bundle=V54,
        source_name="v54-source",
    )
    old_events = _event_rows_as_bytes(path)
    assert _head_coordinates(path) == (*source_cursor, V54)

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    projection = migrated.project()
    assert projection.reducer_bundle_version == V56
    assert migrated.rebuild() == projection
    migrated.close()

    assert _event_rows_as_bytes(path) == old_events
    assert _head_coordinates(path) == (*source_cursor, V56)


def test_authentic_v55_stream_audit_migrates_to_v56_without_rewriting_history(
    tmp_path,
) -> None:
    path = tmp_path / "v55-stream-audit-to-v56.sqlite3"
    source_cursor = _build_archived_database(
        tmp_path=tmp_path,
        database_path=path,
        source_commit=V55_SOURCE_COMMIT,
        expected_bundle=V55,
        source_name="v55-stream-audit-source",
        append_v55_stream_audit=True,
    )
    old_events = _event_rows_as_bytes(path)
    assert len(old_events) == 3
    assert _head_coordinates(path) == (*source_cursor, V55)

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    projection = migrated.project()
    assert projection.reducer_bundle_version == V56
    assert [
        audit.model_call_id for audit in projection.model_result_audits
    ] == [
        "model-call:authentic-v55:tail",
        "model-call:authentic-v55:physical",
    ]
    assert [
        audit.audit_contract for audit in projection.model_result_audits
    ] == [
        "model-result-audit.6",
        "model-result-audit.6",
    ]
    semantic_tail = RecordedModelResultAudit.model_validate_json(
        projection.model_result_audits[0].audit_json
    )
    physical_terminal = RecordedModelResultAudit.model_validate_json(
        projection.model_result_audits[1].audit_json
    )
    assert semantic_tail.parent_model_call_id == physical_terminal.model_call_id
    assert semantic_tail.semantic_stream_part == "tail"
    assert semantic_tail.request_hash == physical_terminal.request_hash
    assert physical_terminal.status == "provider_completed"
    assert physical_terminal.response_hash == hashlib.sha256(
        b"authentic-v55-full-response"
    ).hexdigest()
    assert physical_terminal.semantic_model_call_ids == (
        "model-call:authentic-v55:head",
        "model-call:authentic-v55:tail",
    )
    assert (
        projection.world_revision,
        projection.deliberation_revision,
        projection.ledger_sequence,
    ) == source_cursor
    assert migrated.rebuild() == projection
    migrated.close()

    assert _event_rows_as_bytes(path) == old_events
    assert _head_coordinates(path) == (*source_cursor, V56)

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.project() == projection
    assert reopened.rebuild() == projection
    reopened.close()
    assert _event_rows_as_bytes(path) == old_events


def test_pending_interaction_act_proposal_does_not_change_world_semantic_payload() -> None:
    source = InteractionActSourceRef(
        authority_kind="observed_message",
        source_event_ref="event:observation:proposal-only",
        source_world_revision=1,
        source_payload_hash="a" * 64,
        source_actor_ref="user:primary",
    )
    act = InteractionActProjection(
        interaction_act_id="interaction-act:sha256:" + "b" * 64,
        entity_revision=1,
        conversation_ref="conversation:qq:c2c:primary",
        subject_ref="user:primary",
        counterparty_refs=("actor:companion",),
        act_kind="offer",
        participant_statuses=(
            InteractionActParticipantStatus(
                actor_ref="user:primary",
                status_code="等待下次交接",
                source_ref=source,
                source_text_span="下次带给你",
                updated_at=NOW,
            ),
        ),
        source_refs=(source,),
        opened_at=NOW,
        updated_at=NOW,
        origin_transition_id="interaction-act-transition:sha256:" + "c" * 64,
    )
    mutation = InteractionActMutation(
        transition_id=act.origin_transition_id,
        operation="declare",
        expected_entity_revision=0,
        act_before=None,
        act_after=act,
        source_ref=source,
        source_text_span="下次带给你",
        role_output_hash="d" * 64,
    )
    proposal = InteractionActProposalProjection(
        proposal_id="proposal:interaction-act:proposal-only",
        proposal_hash="sha256:" + "e" * 64,
        change_id="change:interaction-act:proposal-only",
        accepted_change_hash="f" * 64,
        evaluated_world_revision=1,
        mutation_payload_hash="1" * 64,
        mutation=mutation,
        recorded_event_ref="event:interaction-act:proposal-only",
        recorded_event_payload_hash="2" * 64,
    )
    baseline = ReducerState()
    proposal_only = baseline.model_copy(
        update={"interaction_act_proposals": (proposal,)}
    )

    assert proposal_only.semantic_payload(
        world_id=WORLD,
        world_revision=1,
    ) == baseline.semantic_payload(
        world_id=WORLD,
        world_revision=1,
    )
