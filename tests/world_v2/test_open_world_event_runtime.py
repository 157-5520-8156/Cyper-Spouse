from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from companion_daemon.world_v2.open_world_event_draft import (
    OpenWorldEventSituation,
    parse_open_world_event_draft,
)
from companion_daemon.world_v2.open_world_event_runtime import (
    ActivePlanSituationSource,
    OpenWorldEventRuntime,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import DueWindow, EvidenceRef, NpcProjection, PlanStateProjection
from test_life_projection import WORLD_ID, commit, event, mutation


NOW = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)


def _situations() -> tuple[OpenWorldEventSituation, ...]:
    return (
        OpenWorldEventSituation(
            token="situation-token-cafe-cat",
            event_kind="noticed_small_thing",
            safe_summary="在当前已确认的咖啡店处境里，注意到一个小而具体的变化。",
            participant_tokens=(),
            location_token="location:cafe",
            privacy="shareable",
            duration_minutes=8,
        ),
        OpenWorldEventSituation(
            token="situation-token-npc-friction",
            event_kind="npc_friction",
            safe_summary="与当前已确认在场的 NPC 有一次短暂摩擦。",
            participant_tokens=("npc:lin",),
            location_token="location:cafe",
            privacy="personal",
            duration_minutes=12,
        ),
    )


def test_open_world_draft_can_choose_an_offered_situation_and_short_moment() -> None:
    draft = parse_open_world_event_draft(
        raw=json.dumps(
            {
                "decision": "select",
                "situation_token": "situation-token-cafe-cat",
                "moment": "她在门口看见一只猫停了一会儿，顺手记下了这个小插曲。",
                "moment_scope": "subjective",
            },
            ensure_ascii=False,
        ),
        offered=_situations(),
        model="test-open-world",
    )

    assert draft.situation_token == "situation-token-cafe-cat"
    assert "猫" in draft.moment
    assert draft.moment_scope == "subjective"


def test_open_world_draft_cannot_invent_identity_location_or_extra_authority_fields() -> None:
    with pytest.raises(ValueError, match="unknown situation"):
        parse_open_world_event_draft(
            raw=json.dumps(
                {
                    "decision": "select",
                    "situation_token": "invented-place-and-person",
                    "moment": "这里有一个从未出现过的人。",
                    "moment_scope": "subjective",
                }
            ),
            offered=_situations(),
            model="test-open-world",
        )

    with pytest.raises(ValueError, match="exactly"):
        parse_open_world_event_draft(
            raw=json.dumps(
                {
                    "decision": "select",
                    "situation_token": "situation-token-cafe-cat",
                    "moment": "一个小插曲。",
                    "moment_scope": "subjective",
                    "location_ref": "location:invented",
                }
            ),
            offered=_situations(),
            model="test-open-world",
        )


def test_open_world_no_op_is_a_first_class_model_decision() -> None:
    draft = parse_open_world_event_draft(
        raw='{"decision":"no_op"}', offered=_situations(), model="test-open-world"
    )
    assert draft.decision == "no_op"
    assert draft.situation_token is None


def test_open_world_selected_moment_must_be_explicitly_subjective() -> None:
    with pytest.raises(ValueError, match="moment_scope"):
        parse_open_world_event_draft(
            raw=json.dumps(
                {
                    "decision": "select",
                    "situation_token": "situation-token-cafe-cat",
                    "moment": "她看见了一个外部事实。",
                },
                ensure_ascii=False,
            ),
            offered=_situations(),
            model="test-open-world",
        )


class _ChoosingEventModel:
    model = "test-open-world-model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.4):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        situations = json.loads(messages[-1]["content"])["situations"]
        return json.dumps(
            {
                "decision": "select",
                "situation_token": next(
                    item["token"] for item in situations if item["event_kind"] == "npc_friction"
                ),
                "moment": "她和林因为一件小事拌了两句嘴，后来都停下来重新看了看对方。",
                "moment_scope": "subjective",
            },
            ensure_ascii=False,
        )


class _DecliningEventModel:
    model = "test-open-world-declining-model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.4):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return '{"decision":"no_op"}'


def _active_plan_ledger() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    at = NOW - timedelta(minutes=1)
    commit(
        ledger,
        [
            event(
                "open-world-clock",
                "ClockAdvanced",
                {
                    "logical_time_from": (at - timedelta(minutes=1)).isoformat(),
                    "logical_time_to": at.isoformat(),
                },
                at=at,
            )
        ],
    )
    commit(
        ledger,
        [
            event(
                "open-world-npc-observation",
                "OperatorObservationRecorded",
                {"observation_id": "operator:open-world-npc", "observation_hash": "b" * 64},
                at=at,
            ),
            event(
                "open-world-npc",
                "NpcRegistered",
                {
                    **mutation(
                        "open-world-npc", expected_revision=0,
                        evidence_refs=[EvidenceRef(
                            ref_id="operator:open-world-npc",
                            evidence_type="operator_observation",
                            claim_purpose="current_fact",
                            immutable_hash="b" * 64,
                        ).model_dump(mode="json")],
                    ),
                    "npc": NpcProjection(
                        npc_id="lin", entity_revision=1, stable_identity_ref="identity:lin",
                        privacy_class="personal",
                    ).model_dump(mode="json"),
                },
                at=at,
            ),
        ],
    )
    plan = PlanStateProjection(
        plan_id="plan:open-world",
        activity_id="activity:open-world",
        entity_revision=1,
        activity_kind="social:coffee",
        evidence_refs=(EvidenceRef(
            ref_id="open-world-clock",
            evidence_type="committed_world_event",
            claim_purpose="future_plan",
            source_world_revision=1,
            immutable_hash=ledger.project().committed_world_event_refs[0].payload_hash,
        ),),
        status="planned",
        importance_bp=3_000,
        scheduled_window=DueWindow(opens_at=at, closes_at=NOW + timedelta(minutes=30)),
        participant_refs=("npc:lin",),
        location_ref="location:cafe",
        owner_actor_ref="actor:companion",
        privacy_class="personal",
    )
    commit(
        ledger,
        [
            event(
                "open-world-plan",
                "ActivityPlanned",
                {**mutation("open-world-plan", expected_revision=0, evidence_refs=[plan.evidence_refs[0].model_dump(mode="json")]), "plan": plan.model_dump(mode="json")},
                at=at,
            ),
            event(
                "open-world-start",
                "ActivityStarted",
                {**mutation("open-world-start", expected_revision=1, evidence_refs=[plan.evidence_refs[0].model_dump(mode="json")]), "plan_id": plan.plan_id, "transitioned_at": at.isoformat(), "reason_ref": "activity:started"},
                at=at,
            ),
        ],
    )
    return ledger


@pytest.mark.asyncio
async def test_open_world_event_is_accepted_into_occurrence_and_replayed_without_recalling_model() -> None:
    ledger = _active_plan_ledger()
    from companion_daemon.world_v2.life_content_store import InMemoryImmutableLifeContentStore

    model = _ChoosingEventModel()
    runtime = OpenWorldEventRuntime(
        ledger=ledger,
        content_store=InMemoryImmutableLifeContentStore(),
        model=model,
        situation_source=ActivePlanSituationSource(owner_actor_ref="actor:companion"),
        owner_actor_ref="actor:companion",
    )

    first = await runtime.advance_once(
        wake_event_ref="open-world-start", trace_id="trace:open-world", correlation_id="corr:open-world"
    )
    assert first.status == "committed"
    assert model.calls == 1
    occurrence = ledger.project().world_occurrences[-1]
    assert occurrence.status == "active"
    assert occurrence.location_ref == "location:cafe"
    assert occurrence.participant_refs == ("actor:companion", "npc:lin")
    assert first.proposal_id in ledger.project().proposal_ids

    second = await runtime.advance_once(
        wake_event_ref="open-world-start", trace_id="trace:open-world", correlation_id="corr:open-world"
    )
    assert second.status == "recovered"
    assert model.calls == 1
    assert len(ledger.project().world_occurrences) == 1


@pytest.mark.asyncio
async def test_open_world_no_op_is_durable_and_replayed_without_recalling_model() -> None:
    ledger = _active_plan_ledger()
    from companion_daemon.world_v2.life_content_store import InMemoryImmutableLifeContentStore

    model = _DecliningEventModel()
    runtime = OpenWorldEventRuntime(
        ledger=ledger,
        content_store=InMemoryImmutableLifeContentStore(),
        model=model,
        situation_source=ActivePlanSituationSource(owner_actor_ref="actor:companion"),
        owner_actor_ref="actor:companion",
    )

    first = await runtime.advance_once(
        wake_event_ref="open-world-start", trace_id="trace:open-world", correlation_id="corr:open-world"
    )
    second = await runtime.advance_once(
        wake_event_ref="open-world-start", trace_id="trace:open-world", correlation_id="corr:open-world"
    )

    assert first.status == "no_op"
    assert second.reason_code == "open_world_event.model_declined_recovered"
    assert model.calls == 1
    assert len(ledger.project().world_occurrences) == 0
    assert second.proposal_id in ledger.project().proposal_ids
