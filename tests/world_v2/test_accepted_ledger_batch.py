from __future__ import annotations

import copy
from datetime import UTC, datetime
import pickle

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import (
    AcceptedLedgerBatchError,
    AcceptedLedgerBatchIssuer,
)
from companion_daemon.world_v2.activity_lifecycle_acceptance_manifest import (
    ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
)
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent


WORLD_ID = "world:accepted-ledger-batch"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
CURSOR = ProjectionCursor(world_revision=2, deliberation_revision=3, ledger_sequence=5)


def _event(*, event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:accepted-ledger-batch",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:accepted-ledger-batch",
        idempotency_key=f"identity:{event_id}",
        payload=payload,
    )


def _events() -> tuple[WorldEvent, ...]:
    return (
        _event(
            event_id="event:acceptance",
            event_type="AcceptanceRecorded",
            payload={"manifest_version": "acceptance-manifest.3"},
        ),
        _event(
            event_id="event:fact",
            event_type="FactCommittedV2",
            payload={"payload_contract": "fact-commit-materialized.2"},
        ),
    )


def _issue(issuer: AcceptedLedgerBatchIssuer):
    return issuer.issue(
        world_id=WORLD_ID,
        expected_cursor=CURSOR,
        events=_events(),
        manifest_hash="a" * 64,
        registry_digest="b" * 64,
        commit_id="commit:accepted-ledger-batch",
    )


def test_issuer_binds_full_cursor_exact_order_and_commit_id() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    handle = _issue(issuer)

    events, commit_id = issuer.verify(
        handle=handle, world_id=WORLD_ID, expected_cursor=CURSOR
    )

    assert events == _events()
    assert commit_id == "commit:accepted-ledger-batch"
    with pytest.raises(AcceptedLedgerBatchError, match="ledger authority"):
        issuer.verify(
            handle=handle,
            world_id=WORLD_ID,
            expected_cursor=CURSOR.model_copy(update={"ledger_sequence": 4}),
        )


def test_handle_is_issuer_scoped_and_not_copyable_or_serializable() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    handle = _issue(issuer)

    with pytest.raises(AcceptedLedgerBatchError, match="another issuer"):
        AcceptedLedgerBatchIssuer().verify(
            handle=handle, world_id=WORLD_ID, expected_cursor=CURSOR
        )
    for operation in (lambda: copy.copy(handle), lambda: copy.deepcopy(handle), lambda: pickle.dumps(handle)):
        with pytest.raises(TypeError):
            operation()


def test_issuer_requires_a_v3_acceptance_followed_by_effects() -> None:
    issuer = AcceptedLedgerBatchIssuer()

    with pytest.raises(AcceptedLedgerBatchError, match="begin with"):
        issuer.issue(
            world_id=WORLD_ID,
            expected_cursor=CURSOR,
            events=(_events()[1], _events()[0]),
            manifest_hash="a" * 64,
            registry_digest="b" * 64,
            commit_id="commit:reordered",
        )


def test_issuer_accepts_the_closed_activity_lifecycle_manifest_family() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    events = (
        _event(
            event_id="event:activity-acceptance",
            event_type="AcceptanceRecorded",
            payload={"manifest_version": ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION},
        ),
        _events()[1],
    )

    handle = issuer.issue(
        world_id=WORLD_ID,
        expected_cursor=CURSOR,
        events=events,
        manifest_hash="a" * 64,
        registry_digest="b" * 64,
        commit_id="commit:activity-lifecycle",
    )

    assert issuer.verify(
        handle=handle, world_id=WORLD_ID, expected_cursor=CURSOR
    ) == (events, "commit:activity-lifecycle")
