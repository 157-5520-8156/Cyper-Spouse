from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from companion_daemon.world_v2.random_authority import RandomAuthority


NOW = datetime(2026, 7, 16, 23, tzinfo=UTC)


def test_draw_is_canonical_replayable_and_persisted_once() -> None:
    commits, stored = [], {}
    projection = SimpleNamespace(world_revision=1, deliberation_revision=0, ledger_sequence=1, logical_time=NOW)
    ledger = SimpleNamespace(
        world_id="world:random", project=lambda: projection,
        lookup_event_commit=lambda event_id: (stored[event_id], object()) if event_id in stored else None,
        commit_at_cursor=lambda events, expected_cursor, commit_id: (
            commits.append((events, expected_cursor, commit_id)),
            stored.update({event.event_id: event for event in events}),
        ),
    )
    authority = RandomAuthority(ledger=ledger)

    first = authority.draw(
        attempt_id="media:attempt:1", candidate_refs=("candidate:b", "candidate:a"),
        catalog_version="media-selection.1", logical_time=NOW, actor="worker:random",
        trace_id="trace:random", correlation_id="correlation:random",
    )
    second = authority.draw(
        attempt_id="media:attempt:1", candidate_refs=("candidate:a", "candidate:b"),
        catalog_version="media-selection.1", logical_time=NOW, actor="worker:random",
        trace_id="trace:random", correlation_id="correlation:random",
    )

    assert first == second
    assert first.candidate_refs == ("candidate:a", "candidate:b")
    assert first.selected_candidate_ref in first.candidate_refs
    assert len(commits) == 1
