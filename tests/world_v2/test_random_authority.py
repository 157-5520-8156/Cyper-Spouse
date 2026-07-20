from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.random_authority import (
    RandomAuthority,
    RandomDrawRecordedPayload,
)


NOW = datetime(2026, 7, 16, 23, tzinfo=UTC)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


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
    assert first.seed_hash == _digest({
        "world": "world:random",
        "time": NOW.isoformat(),
        "attempt": "media:attempt:1",
        "candidates": ("candidate:a", "candidate:b"),
        "catalog": "media-selection.1",
    })
    assert len(commits) == 1


def test_draw_rejoins_after_clock_moves_when_seed_instant_is_the_original_wake() -> None:
    commits, stored = [], {}
    head = {"logical_time": NOW}
    ledger = SimpleNamespace(
        world_id="world:random-recovery",
        project=lambda: SimpleNamespace(
            world_revision=1, deliberation_revision=0, ledger_sequence=1,
            logical_time=head["logical_time"],
        ),
        lookup_event_commit=lambda event_id: (stored[event_id], object()) if event_id in stored else None,
        commit_at_cursor=lambda events, expected_cursor, commit_id: (
            commits.append((events, expected_cursor, commit_id)),
            stored.update({event.event_id: event for event in events}),
        ),
    )
    authority = RandomAuthority(ledger=ledger)
    first = authority.draw(
        attempt_id="life:attempt:1", candidate_refs=("candidate:a", "candidate:b"),
        catalog_version="life.1", logical_time=NOW, seed_instant=NOW,
        actor="worker:random", trace_id="trace:random", correlation_id="correlation:random",
    )
    head["logical_time"] = NOW + timedelta(minutes=5)
    recovered = authority.draw(
        attempt_id="life:attempt:1", candidate_refs=("candidate:a", "candidate:b"),
        catalog_version="life.1", logical_time=head["logical_time"], seed_instant=NOW,
        actor="worker:random", trace_id="trace:random", correlation_id="correlation:random",
    )

    assert recovered == first
    assert len(commits) == 1


def test_weighted_draw_records_canonical_normalized_authority_and_replays() -> None:
    commits, stored = [], {}
    projection = SimpleNamespace(
        world_revision=1, deliberation_revision=0, ledger_sequence=1, logical_time=NOW
    )
    ledger = SimpleNamespace(
        world_id="world:weighted-random", project=lambda: projection,
        lookup_event_commit=lambda event_id: (stored[event_id], object()) if event_id in stored else None,
        commit_at_cursor=lambda events, expected_cursor, commit_id: (
            commits.append((events, expected_cursor, commit_id)),
            stored.update({event.event_id: event for event in events}),
        ),
    )
    authority = RandomAuthority(ledger=ledger)

    first = authority.draw(
        attempt_id="life:weighted:1", candidate_refs=("candidate:b", "candidate:a"),
        candidate_weights={"candidate:a": 1, "candidate:b": 3},
        weight_policy_version="life-author-weight.1",
        catalog_version="life.2", logical_time=NOW, actor="worker:random",
        trace_id="trace:weighted", correlation_id="correlation:weighted",
    )
    replayed = authority.draw(
        attempt_id="life:weighted:1", candidate_refs=("candidate:a", "candidate:b"),
        candidate_weights={"candidate:b": 3, "candidate:a": 1},
        weight_policy_version="life-author-weight.1",
        catalog_version="life.2", logical_time=NOW, actor="worker:random",
        trace_id="trace:weighted", correlation_id="correlation:weighted",
    )

    assert replayed == first
    assert first.sampler_version == "random-authority.2"
    assert first.weight_policy_version == "life-author-weight.1"
    assert [(item.candidate_ref, item.weight_ppm) for item in first.weight_vector] == [
        ("candidate:a", 250_000), ("candidate:b", 750_000),
    ]
    assert first.weight_vector_hash is not None
    assert len(commits) == 1


def test_v2_weight_vector_hash_rejects_tampering() -> None:
    with pytest.raises(ValueError, match="weight vector hash"):
        RandomDrawRecordedPayload(
            draw_id="draw:tampered", attempt_id="attempt:tampered",
            candidate_refs=("candidate:a",),
            candidate_set_hash=_digest(("candidate:a",)), selected_candidate_ref="candidate:a",
            seed_hash="8" * 64, catalog_version="life.2",
            sampler_version="random-authority.2",
            weight_policy_version="life-author-weight.1",
            weight_vector=({"candidate_ref": "candidate:a", "weight_ppm": 1_000_000},),
            weight_vector_hash="9" * 64,
        )


def test_v1_payload_without_weight_fields_remains_cold_replay_compatible() -> None:
    refs = ("candidate:a", "candidate:b")
    payload = RandomDrawRecordedPayload.model_validate({
        "draw_id": "draw:legacy", "attempt_id": "attempt:legacy",
        "candidate_refs": refs, "candidate_set_hash": _digest(refs),
        "selected_candidate_ref": "candidate:a", "seed_hash": "8" * 64,
        "catalog_version": "life.1", "sampler_version": "random-authority.1",
    })

    assert payload.sampler_version == "random-authority.1"
    assert payload.weight_vector == ()
    assert payload.weight_vector_hash is None
    assert payload.weight_policy_version is None


def test_weighted_upgrade_rejoins_an_existing_v1_attempt() -> None:
    commits, stored = [], {}
    projection = SimpleNamespace(
        world_revision=1, deliberation_revision=0, ledger_sequence=1,
        logical_time=NOW,
    )
    ledger = SimpleNamespace(
        world_id="world:upgrade", project=lambda: projection,
        lookup_event_commit=lambda event_id: (
            (stored[event_id], object()) if event_id in stored else None
        ),
        commit_at_cursor=lambda events, expected_cursor, commit_id: (
            commits.append((events, expected_cursor, commit_id)),
            stored.update({event.event_id: event for event in events}),
        ),
    )
    authority = RandomAuthority(ledger=ledger)
    legacy = authority.draw(
        attempt_id="life:upgrade:1", candidate_refs=("candidate:a", "candidate:b"),
        catalog_version="life.1", logical_time=NOW, actor="worker:random",
        trace_id="trace:upgrade", correlation_id="correlation:upgrade",
    )

    recovered = authority.draw(
        attempt_id="life:upgrade:1", candidate_refs=("candidate:a", "candidate:b"),
        candidate_weights={"candidate:a": 1, "candidate:b": 3},
        weight_policy_version="life-author-weight.1",
        catalog_version="life.1", logical_time=NOW, actor="worker:random",
        trace_id="trace:upgrade", correlation_id="correlation:upgrade",
    )

    assert recovered == legacy
    assert recovered.sampler_version == "random-authority.1"
    assert len(commits) == 1
