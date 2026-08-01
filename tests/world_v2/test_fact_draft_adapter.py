from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json

import pytest

from companion_daemon.world_v2.fact_draft_adapter import (
    FactDraftTechnicalFailure,
    FactObservationSource,
    FactObservationProposalAdapter,
    _PREDICATE_GUIDE,
    materialize_fact_observation_draft,
)
from companion_daemon.world_v2.fact_reducers import INSTALLED_FACT_PREDICATE_CARDINALITY
from companion_daemon.world_v2.schemas import Observation, WorldEvent


NOW = datetime(2026, 7, 15, 17, 0, tzinfo=UTC)


class _BlockedChat:
    model = "blocked-fact-draft"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del messages, temperature
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


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


@pytest.mark.asyncio
async def test_fact_draft_provider_has_a_stable_total_deadline() -> None:
    observation, event = _observation()

    with pytest.raises(FactDraftTechnicalFailure) as failure:
        await FactObservationProposalAdapter(
            model=_BlockedChat(),
            timeout_seconds=0.1,
        ).propose(
            observation=observation,
            observation_event=event,
            source_world_revision=1,
        )

    assert failure.value.failure_code == "provider_timeout"


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


class _BatchChat:
    model = "test-fact-batch"

    def __init__(self, *, cross_ground: bool = False) -> None:
        self.calls = 0
        self.cross_ground = cross_ground

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.1
        self.calls += 1
        request = json.loads(messages[1]["content"])
        first, second = request["observations"]
        second_result = (
            {
                "retain": True,
                "predicate_code": "preference.likes",
                # This text belongs only to the first Observation and must
                # never become evidence for the second one.
                "value": "乌龙茶",
                "privacy_class": "personal",
                "confidence": 8300,
                "rationale": "cross-source",
            }
            if self.cross_ground
            else {"retain": False}
        )
        return json.dumps(
            {
                "decisions": [
                    {"observation_id": first["observation_id"], "result": {"retain": False}},
                    {"observation_id": second["observation_id"], "result": second_result},
                ]
            },
            ensure_ascii=False,
        )


def _second_observation() -> tuple[Observation, WorldEvent]:
    first, _event = _observation()
    text = "今天早点休息。"
    observation = first.model_copy(
        update={
            "observation_id": "observation:fact-draft:second",
            "source_event_id": "test:fact-draft:second",
            "payload_ref": "payload:fact-draft:second",
            "payload_hash": hashlib.sha256(text.encode()).hexdigest(),
            "text": text,
        }
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:observation:fact-draft:second",
        world_id=observation.world_id,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key="observation:fact-draft:second",
        payload=observation.model_dump(mode="json"),
    )
    return observation, event


@pytest.mark.asyncio
async def test_adapter_no_change_does_not_create_a_proposal() -> None:
    observation, event = _observation()
    assert await FactObservationProposalAdapter(model=_Chat()).propose(
        observation=observation, observation_event=event, source_world_revision=1,
    ) is None


@pytest.mark.asyncio
async def test_batch_adapter_decides_each_exact_source_in_one_call() -> None:
    first, first_event = _observation()
    second, second_event = _second_observation()
    chat = _BatchChat()

    decisions = await FactObservationProposalAdapter(model=chat).propose_batch(
        sources=(
            FactObservationSource(first, first_event, 1),
            FactObservationSource(second, second_event, 2),
        ),
        evaluated_world_revision=2,
    )

    assert decisions == (None, None)
    assert chat.calls == 1


@pytest.mark.asyncio
async def test_batch_adapter_rejects_cross_observation_grounding() -> None:
    first, first_event = _observation()
    second, second_event = _second_observation()
    chat = _BatchChat(cross_ground=True)

    with pytest.raises(FactDraftTechnicalFailure, match="invalid_output"):
        await FactObservationProposalAdapter(model=chat).propose_batch(
            sources=(
                FactObservationSource(first, first_event, 1),
                FactObservationSource(second, second_event, 2),
            ),
            evaluated_world_revision=2,
        )

    assert chat.calls == 2


def test_normalizes_provider_probability_confidence_to_basis_points() -> None:
    observation, event = _observation()

    proposal = materialize_fact_observation_draft(
        raw=json.dumps({
            "retain": True,
            "predicate_code": "preference.likes",
            "value": "乌龙茶",
            "privacy_class": "personal",
            "confidence": 0.83,
            "rationale": "The preference is explicitly stated.",
        }),
        observation=observation,
        observation_event=event,
        source_world_revision=1,
    )

    assert proposal is not None
    assert proposal.confidence == 8300


def test_accepts_a_fenced_json_answer_without_widening_validation() -> None:
    observation, event = _observation()

    proposal = materialize_fact_observation_draft(
        raw=(
            "好的，以下是分类结果：\n```json\n"
            + json.dumps({
                "retain": True,
                "predicate_code": "preference.likes",
                "value": "乌龙茶",
                "privacy_class": "personal",
                "confidence": 8300,
                "rationale": "The preference is explicitly stated.",
            }, ensure_ascii=False)
            + "\n```"
        ),
        observation=observation,
        observation_event=event,
        source_world_revision=1,
    )

    assert proposal is not None


class _RetryingChat:
    """First answer paraphrases the value; the corrective pass fixes it."""

    model = "test-fact-retry"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(messages)
        if len(self.calls) == 1:
            return json.dumps({
                "retain": True, "predicate_code": "preference.likes",
                "value": "喜欢喝茶（乌龙）", "privacy_class": "personal",
                "confidence": 8300, "rationale": "paraphrased value",
            }, ensure_ascii=False)
        return json.dumps({
            "retain": True, "predicate_code": "preference.likes",
            "value": "乌龙茶", "privacy_class": "personal",
            "confidence": 8300, "rationale": "The preference is explicitly stated.",
        }, ensure_ascii=False)


@pytest.mark.asyncio
async def test_adapter_gives_one_corrective_retry_for_a_fixable_draft() -> None:
    observation, event = _observation()
    chat = _RetryingChat()

    proposal = await FactObservationProposalAdapter(model=chat).propose(
        observation=observation, observation_event=event, source_world_revision=1,
    )

    assert proposal is not None
    assert len(chat.calls) == 2
    # The corrective turn carries the violated contract and the prior answer.
    assert chat.calls[1][-2]["role"] == "assistant"
    assert "violated the contract" in chat.calls[1][-1]["content"]


class _AlwaysInvalidChat:
    model = "test-fact-invalid"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return '{"retain": true, "predicate_code": "unknown"}'


@pytest.mark.asyncio
async def test_adapter_fails_closed_after_exactly_one_retry() -> None:
    observation, event = _observation()
    chat = _AlwaysInvalidChat()

    with pytest.raises(FactDraftTechnicalFailure, match="invalid_output"):
        await FactObservationProposalAdapter(model=chat).propose(
            observation=observation, observation_event=event, source_world_revision=1,
        )

    assert chat.calls == 2


def test_predicate_guide_stays_in_sync_with_the_installed_catalog() -> None:
    assert set(_PREDICATE_GUIDE) == set(INSTALLED_FACT_PREDICATE_CARDINALITY)


def test_everyday_life_predicates_materialize_from_casual_statements() -> None:
    text = "好嘛，明天还得打国赛，先睡了"
    observation = Observation(
        schema_version="world-v2.1", observation_id="observation:fact-draft-life",
        world_id="world:fact-draft", logical_time=NOW, created_at=NOW,
        trace_id="trace:fact-draft", causation_id="cause:fact-draft",
        correlation_id="correlation:fact-draft", source="platform:test",
        source_event_id="test:fact-draft-life", actor="user:fact-draft", channel="test",
        payload_ref="payload:fact-draft-life",
        payload_hash=hashlib.sha256(text.encode()).hexdigest(),
        text=text, received_at=NOW,
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1", event_id="event:observation:fact-draft-life",
        world_id=observation.world_id, event_type="ObservationRecorded", logical_time=NOW,
        created_at=NOW, actor=observation.actor, source=observation.source,
        trace_id=observation.trace_id, causation_id=observation.causation_id,
        correlation_id=observation.correlation_id, idempotency_key="observation:fact-draft-life",
        payload=observation.model_dump(mode="json"),
    )

    proposal = materialize_fact_observation_draft(
        raw=json.dumps({
            "retain": True,
            "predicate_code": "schedule.commitment",
            "value": "明天还得打国赛",
            "privacy_class": "personal",
            "confidence": 8800,
            "rationale": "The user states a scheduled contest tomorrow.",
        }, ensure_ascii=False),
        observation=observation, observation_event=event, source_world_revision=1,
    )

    assert proposal is not None
    intent = json.loads(proposal.proposed_changes[0].payload.canonical_json)
    assert intent["predicate_code"] == "schedule.commitment"
    assert intent["value_hash"] == "sha256:" + hashlib.sha256("明天还得打国赛".encode()).hexdigest()


def test_prompt_lists_every_installed_predicate_with_its_cardinality() -> None:
    observation, _event = _observation()

    system = FactObservationProposalAdapter._messages(
        observation,
        source_world_revision=1,
    )[0]["content"]

    for code, cardinality in INSTALLED_FACT_PREDICATE_CARDINALITY.items():
        assert f"{code} ({cardinality})" in system


def test_tightens_broad_model_privacy_to_the_direct_message_floor() -> None:
    observation, event = _observation()

    proposal = materialize_fact_observation_draft(
        raw=json.dumps({
            "retain": True,
            "predicate_code": "preference.likes",
            "value": "乌龙茶",
            "privacy_class": "public",
            "confidence": 9000,
            "rationale": "The preference is explicitly stated.",
        }),
        observation=observation,
        observation_event=event,
        source_world_revision=1,
    )

    assert proposal is not None
    intent = json.loads(proposal.proposed_changes[0].payload.canonical_json)
    assert intent["privacy_class"] == "personal"
