from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.npc_ecology import NpcEcology
from companion_daemon.world_v2.occurrence_content_coordinator import (
    OccurrenceContentCoordinator,
)
from test_life_projection import WORLD_ID, commit, event, seed_through_proposal


class _Model:
    def __init__(self, payload: dict[str, object]) -> None:
        self.model = "test-npc-role"
        self.payload = payload
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(self.payload, ensure_ascii=False)


def _actor(decision: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "decision": decision,
        "npc_ref": "npc:lin",
        "impulse_summary": "她忙完后想起下午一起泡茶时没有聊完的事。",
        "inner_state_summary": "实习作品集还有点乱，她既想自己理清，也愿意再见面聊聊。",
        "source_refs": ["clock-life"],
        "relationship_to_protagonist": {
            "trust_bp": 5200,
            "closeness_bp": 4300,
            "respect_bp": 5100,
            "reliability_bp": 4800,
            "mutuality_bp": 3900,
            "repair_confidence_bp": 5000,
            "friction_bp": 300,
            "tension_bp": 700,
        },
        "current_goal_summaries": ["把实习作品集整理出一个敢拿给别人看的版本。"],
    }
    if decision == "propose":
        payload["proposal"] = {
            "timing": "now",
            "premise": "林在厨房忙完后，想顺势提起作品集。",
            "participant_refs": ["npc:lin"],
            "location_ref": "room:kitchen",
            "duration_minutes": 25,
            "visibility": "personal",
        }
    return payload


def _world() -> dict[str, object]:
    return {
        "decision": "accept",
        "outcomes": [
            {"text": "林对着页面理了一阵，改清楚了最卡的一处。", "privacy": "personal"},
            {"text": "刚说到一半林临时有事，只约好之后再接着看。", "privacy": "personal"},
        ],
    }


def _world_plan() -> dict[str, object]:
    return {
        "decision": "accept",
        "outcomes": [],
    }


def _runtime(actor_payload: dict[str, object], world_payload: dict[str, object]):
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=AcceptedLedgerBatchIssuer(),
    )
    seed_through_proposal(ledger, event_visibility="personal")
    store = InMemoryImmutableLifeContentStore()
    descriptor = "林，角色在当前生活里认识的人。"
    store.put_if_absent(
        StoredLifeContent(
            content_ref=ledger.project().npcs[0].stable_identity_ref,
            content_kind="provisional_npc_introduction",
            content_payload_hash=life_content_payload_hash(descriptor),
            text=descriptor,
        )
    )
    actor = _Model(actor_payload)
    world = _Model(world_payload)
    runtime = NpcEcology(
        ledger=ledger,
        content_store=store,
        occurrence_content=OccurrenceContentCoordinator(ledger=ledger, store=store),
        actor_model=actor,
        world_author=world,
        protagonist_actor_ref="actor:companion",
    )
    return ledger, store, actor, world, runtime


@pytest.mark.asyncio
async def test_npc_no_op_still_advances_private_state_effect_once() -> None:
    ledger, store, actor, world, runtime = _runtime(_actor("no_op"), {"decision": "no_op"})

    first = await runtime.advance_once(
        wake_event_ref="clock-life", trace_id="trace", correlation_id="correlation"
    )
    second = await runtime.advance_once(
        wake_event_ref="clock-life", trace_id="trace", correlation_id="correlation"
    )

    assert first.status == "state_advanced"
    assert second.status in {"already_considered", "not_due"}
    assert len(actor.calls) == 1
    assert world.calls == []
    actor_context = json.loads(actor.calls[0][1]["content"])
    assert actor_context["authority"]["selected_npc_ref"] == "npc:lin"
    assert "identities" not in actor_context["public_world"]
    assert "protagonist_affect_context_json" not in actor.calls[0][1]["content"]
    state = ledger.project().npcs[0].subjective_state
    assert state is not None
    assert state.relationship_to_subject.closeness_bp == 4300
    assert store.read_exact(content_ref=state.inner_state_content_ref).text.startswith("实习作品集")


@pytest.mark.asyncio
async def test_npc_impulse_is_separately_adjudicated_and_enters_event_machine() -> None:
    ledger, _store, actor, world, runtime = _runtime(_actor("propose"), _world())

    result = await runtime.advance_once(
        wake_event_ref="clock-life", trace_id="trace", correlation_id="correlation"
    )

    assert result.status == "occurrence_committed"
    assert len(actor.calls) == 1
    assert len(world.calls) == 1
    occurrence = next(
        item
        for item in ledger.project().world_occurrences
        if item.occurrence_id == result.occurrence_id
    )
    assert occurrence.status == "active"
    assert occurrence.participant_refs == ("npc:lin",)
    assert len(occurrence.candidate_outcomes) == 2
    assert ledger.project().npcs[0].subjective_state is not None


@pytest.mark.asyncio
async def test_npc_cannot_bind_protagonist_without_character_decision() -> None:
    actor_payload = _actor("propose")
    actor_payload["proposal"] = {
        **actor_payload["proposal"],
        "participant_refs": ["actor:companion", "npc:lin"],
    }
    ledger, _store, actor, world, runtime = _runtime(actor_payload, _world())

    result = await runtime.advance_once(
        wake_event_ref="clock-life", trace_id="trace", correlation_id="correlation"
    )

    assert result.status == "technical_failure"
    assert result.reason_code == "npc_ecology.actor_invalid_after_repair"
    assert len(actor.calls) == 2
    assert "actor_participant_authority_failed" in actor.calls[1][-1]["content"]
    assert world.calls == []
    assert not any(
        "actor:companion" in item.participant_refs
        for item in ledger.project().world_occurrences
        if item.occurrence_id.startswith("occurrence:npc-ecology:")
    )


@pytest.mark.asyncio
async def test_world_author_gets_one_exact_reselection_then_technical_failure() -> None:
    bad_actor = _actor("propose")
    bad_actor["proposal"] = {
        **bad_actor["proposal"],
        "location_ref": "location:invented",
    }
    ledger, _store, _actor_model, world_model, runtime = _runtime(bad_actor, _world())

    result = await runtime.advance_once(
        wake_event_ref="clock-life", trace_id="trace", correlation_id="correlation"
    )

    assert result.status == "technical_failure"
    assert result.reason_code == "npc_ecology.actor_invalid_after_repair"
    assert len(_actor_model.calls) == 2
    assert "actor_location_closure_failed" in _actor_model.calls[1][-1]["content"]
    assert world_model.calls == []
    assert not any(
        item.occurrence_id.startswith("occurrence:npc-ecology:")
        for item in ledger.project().world_occurrences
    )

    retry = await runtime.advance_once(
        wake_event_ref="clock-life",
        trace_id="trace:retry",
        correlation_id="correlation:retry",
    )
    assert retry.status == "technical_failure"
    assert len(_actor_model.calls) == 4
    assert world_model.calls == []


@pytest.mark.asyncio
async def test_npc_can_form_its_own_future_plan_without_binding_protagonist() -> None:
    actor_payload = _actor("propose")
    actor_payload["proposal"] = {
        "timing": "later",
        "premise": "林想周末留一段完整时间整理作品集。",
        "participant_refs": ["npc:lin"],
        "location_ref": "room:kitchen",
        "duration_minutes": 90,
        "visibility": "personal",
        "activity_kind": "整理实习作品集",
        "scheduled_start_after_minutes": 180,
        "importance_bp": 7200,
    }
    ledger, _store, actor, world, runtime = _runtime(actor_payload, _world_plan())

    result = await runtime.advance_once(
        wake_event_ref="clock-life", trace_id="trace", correlation_id="correlation"
    )

    assert result.status == "plan_committed"
    assert len(actor.calls) == 1
    assert len(world.calls) == 1
    plan = ledger.project().plans[-1]
    assert plan.owner_actor_ref == "npc:lin"
    assert plan.participant_refs == ("npc:lin",)
    assert plan.activity_kind == "整理实习作品集"
    assert plan.status == "planned"

    due = plan.scheduled_window.opens_at
    commit(
        ledger,
        [
            event(
                "clock-plan-due",
                "ClockAdvanced",
                {
                    "logical_time_from": ledger.project().logical_time.isoformat(),
                    "logical_time_to": due.isoformat(),
                },
                at=due,
            )
        ],
    )
    actor.payload["source_refs"] = ["clock-plan-due"]
    actor.payload["proposal"] = {
        "timing": "now",
        "premise": "林按计划开始整理作品集。",
        "participant_refs": ["npc:lin"],
        "location_ref": "room:kitchen",
        "duration_minutes": 90,
        "visibility": "personal",
    }
    world.payload = _world()

    due_result = await runtime.advance_once(
        wake_event_ref="clock-plan-due",
        trace_id="trace:due",
        correlation_id="correlation:due",
    )

    assert due_result.status == "occurrence_committed"
    projected_plan = next(item for item in ledger.project().plans if item.plan_id == plan.plan_id)
    assert projected_plan.status == "active"
    occurrence = next(
        item
        for item in ledger.project().world_occurrences
        if item.occurrence_id == due_result.occurrence_id
    )
    assert occurrence.trigger_ref == plan.plan_id
    assert occurrence.precondition_refs == (f"plan:{plan.plan_id}",)
