from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json

import pytest

from companion_daemon.world_v2.fact_draft_adapter import (
    FactObservationProposalAdapter,
    materialize_fact_observation_draft,
)
from companion_daemon.world_v2.schemas import Observation, WorldEvent


NOW = datetime(2026, 7, 15, 17, 0, tzinfo=UTC)


def _observation() -> tuple[Observation, WorldEvent]:
    text = "我最近很喜欢喝乌龙茶。"
    observation = Observation(
        schema_version="world-v2.1", observation_id="observation:fact-draft",
        world_id="world:fact-draft", logical_time=NOW, created_at=NOW,
        trace_id="trace:fact-draft", causation_id="cause:fact-draft",
        correlation_id="correlation:fact-draft", source="platform:test",
        source_event_id="test:fact-draft", actor="user:fact-draft", channel="test",
        payload_ref="payload:fact-draft", payload_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text, received_at=NOW,
    )
    return observation, WorldEvent.from_payload(
        schema_version="world-v2.1", event_id="event:observation:fact-draft",
        world_id=observation.world_id, event_type="ObservationRecorded", logical_time=NOW,
        created_at=NOW, actor=observation.actor, source=observation.source,
        trace_id=observation.trace_id, causation_id=observation.causation_id,
        correlation_id=observation.correlation_id, idempotency_key="observation:fact-draft",
        payload=observation.model_dump(mode="json"),
    )


def test_materializes_one_fact_proposal_from_an_exact_message_substring() -> None:
    observation, event = _observation()
    proposal = materialize_fact_observation_draft(
        raw=json.dumps({"retain": True, "predicate_code": "preference.likes", "value": "乌龙茶", "privacy_class": "personal", "confidence": 8300, "rationale": "The preference is explicitly stated."}),
        observation=observation, observation_event=event, source_world_revision=1,
    )
    assert proposal is not None
    intent = json.loads(proposal.proposed_changes[0].payload.canonical_json)
    assert intent["subject_ref"] == observation.actor
    assert intent["assertion_source_ref"] == observation.observation_id
    assert intent["value_hash"] == "sha256:" + hashlib.sha256("乌龙茶".encode()).hexdigest()


@pytest.mark.parametrize("raw", [
    {"retain": False, "rationale": "no"},
    {"retain": True, "predicate_code": "preference.likes", "value": "咖啡", "privacy_class": "personal", "confidence": 8000, "rationale": "invented"},
    {"retain": True, "predicate_code": "unknown", "value": "乌龙茶", "privacy_class": "personal", "confidence": 8000, "rationale": "bad predicate"},
])
def test_rejects_unbounded_or_unsourced_fact_drafts(raw: dict[str, object]) -> None:
    observation, event = _observation()
    with pytest.raises(ValueError):
        materialize_fact_observation_draft(
            raw=json.dumps(raw), observation=observation, observation_event=event,
            source_world_revision=1,
        )


class _Chat:
    model = "test-fact"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.1
        return '{"retain":false}'


@pytest.mark.asyncio
async def test_adapter_no_change_does_not_create_a_proposal() -> None:
    observation, event = _observation()
    assert await FactObservationProposalAdapter(model=_Chat()).propose(
        observation=observation, observation_event=event, source_world_revision=1,
    ) is None
