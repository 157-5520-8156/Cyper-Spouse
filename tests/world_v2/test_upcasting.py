from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json

import pytest

from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.upcasting import upcast_event


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _legacy_observation() -> dict[str, object]:
    payload_json = json.dumps(
        {"observation_ref": "obs-legacy"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "schema_version": "world-v2.0",
        "event_id": "evt-legacy",
        "world_id": "world-1",
        "event_type": "ObservationRecorded",
        "logical_time": NOW,
        "created_at": NOW,
        "actor": "user",
        "source": "fixture",
        "trace_id": "trace-1",
        "causation_id": "cause-1",
        "correlation_id": "corr-1",
        "idempotency_key": "legacy-key",
        "payload_json": payload_json,
        "payload_hash": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
    }


def test_legacy_event_is_upcast_deterministically_without_mutating_fixture() -> None:
    legacy = _legacy_observation()
    untouched = deepcopy(legacy)

    first = upcast_event(legacy)
    second = upcast_event(legacy)

    assert isinstance(first, WorldEvent)
    assert first == second
    assert legacy == untouched
    assert first.schema_version == "world-v2.1"
    assert first.event_id == "evt-legacy"
    assert first.payload() == {"observation_id": "obs-legacy"}
    assert first.payload_hash == hashlib.sha256(
        first.payload_json.encode("utf-8")
    ).hexdigest()


def test_current_event_replay_is_identity_preserving() -> None:
    current = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="evt-current",
        world_id="world-1",
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user",
        source="fixture",
        trace_id="trace-1",
        causation_id="cause-1",
        correlation_id="corr-1",
        idempotency_key="current-key",
        payload={"observation_id": "obs-current"},
    )

    replayed = upcast_event(current.model_dump())

    assert replayed == current


def test_upcast_rejects_tampered_legacy_payload_before_transforming_it() -> None:
    legacy = _legacy_observation()
    legacy["payload_json"] = '{"observation_ref":"tampered"}'

    with pytest.raises(LedgerIntegrityError, match="payload hash"):
        upcast_event(legacy)


def test_upcast_rejects_an_unregistered_schema_path() -> None:
    legacy = _legacy_observation()
    legacy["schema_version"] = "world-v1.9"

    with pytest.raises(ValueError, match="no deterministic upcaster"):
        upcast_event(legacy)


def test_upcast_never_rewrites_opaque_nested_evidence() -> None:
    legacy = _legacy_observation()
    payload = {
        "observation_ref": "obs-legacy",
        "provider_evidence": {
            "schema_version": "world-v2.0",
            "nested": [{"schema_version": "vendor-v1"}],
        },
    }
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    legacy["payload_json"] = payload_json
    legacy["payload_hash"] = hashlib.sha256(payload_json.encode()).hexdigest()

    upgraded = upcast_event(legacy)

    assert upgraded.payload()["provider_evidence"] == payload["provider_evidence"]
