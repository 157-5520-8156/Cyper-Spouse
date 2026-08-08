from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json
from pathlib import Path
import re

import pytest

from companion_daemon.config import Settings
from companion_daemon.delayed_trigger_catalog import load_delayed_trigger_catalog
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


_CATALOG = Path("configs/delayed_trigger_qualification.v1.yaml")
_SCENARIO_ID = "life.activity-lifecycle-public-host.1"
_AFTERMATH_SCENARIO_ID = "life.aftermath-outcome-public-host.1"


class _PlanWorldAuthor:
    model = "fixture:life-activity-public-world-author"
    semantic_authority_id = "semantic-authority:fixture:life-activity-public-world-author"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        del temperature
        self.calls += 1
        joined = "\n".join(message.get("content", "") for message in messages)
        matches = re.findall(r'"query_ref"\s*:\s*"([^"]+)"', joined)
        wake_ref = next(
            (item for item in reversed(matches) if item.startswith("event:trigger:clock:")),
            None,
        )
        if wake_ref is None:
            raise AssertionError("public life author did not receive a clock wake ref")
        return json.dumps(
            {
                "decision": "propose",
                "authored_subject_ref": "agent:companion",
                "causal_authority": "character_choice",
                "outcome_resolution_authority": "world_contingency",
                "premise_scope": "external_opportunity",
                "premise": "楼下临时有一场可以自由参加的小放映。",
                "premise_claim_refs": ["local:claim:public-screening"],
                "claim_declarations": [
                    {
                        "claim_id": "local:claim:public-screening",
                        "summary": "楼下出现一场可以自由参加的小放映。",
                        "scope": "novel_world_generation",
                        "subject_scope": "world_environment",
                        "source_refs": [],
                    }
                ],
                "timing": {"mode": "now", "duration_minutes": 30},
                "anchor_refs": [wake_ref],
                "location_ref": None,
                "entity_refs": [],
                "privacy_class": "shareable",
                "outcomes": [
                    {
                        "experienced_by_ref": "agent:companion",
                        "text": "放映平静地结束了。",
                        "privacy_class": "shareable",
                        "relative_plausibility_weight": 1,
                        "claim_refs": ["local:claim:public-screening"],
                        "provisional_npcs": [],
                        "dynamic_life_direction": None,
                    },
                    {
                        "experienced_by_ref": "agent:companion",
                        "text": "中途下了一点小雨，放映提前结束。",
                        "privacy_class": "shareable",
                        "relative_plausibility_weight": 1,
                        "claim_refs": ["local:claim:public-screening"],
                        "provisional_npcs": [],
                        "dynamic_life_direction": None,
                    },
                ],
            },
            ensure_ascii=False,
        )


class _CharacterChoiceWorldAuthor(_PlanWorldAuthor):
    """Use the same public plan source with model-owned outcome settlement."""

    model = "fixture:life-aftermath-character-choice-world-author"
    semantic_authority_id = "semantic-authority:fixture:life-aftermath-character-choice-world-author"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        raw = await super().complete(messages, temperature=temperature)
        payload = json.loads(raw)
        payload["causal_authority"] = "character_choice"
        payload["outcome_resolution_authority"] = "character_choice"
        return json.dumps(payload, ensure_ascii=False)


class _LifeSourceReviewer:
    model = "fixture:life-activity-source-reviewer"
    semantic_authority_id = "semantic-authority:fixture:life-activity-source-reviewer"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        del temperature
        self.calls += 1
        if messages and "focused novel-origin critic" in messages[0].get("content", ""):
            return json.dumps(
                {
                    "decision": "supported",
                    "unsupported_claims": [],
                    "unsupported_provisional_npcs": [],
                    "unsupported_outcome_prerequisites": [],
                    "undeclared_premise_fragments": [],
                    "reason": "No prior history or imported prerequisite is present.",
                }
            )
        return json.dumps(
            {
                "decision": "supported",
                "unsupported_claim_ids": [],
                "undeclared_fact_fragments": [],
                "typed_location_conflicts": [],
                "reason": "The bounded fixture proposal is source-closed.",
            }
        )


class _CharacterModel(FakeCompanionModel):
    model = "fixture:life-activity-character"

    def __init__(self) -> None:
        super().__init__()
        self.outcome_offered_tokens: tuple[str, ...] = ()
        self.outcome_selected_tokens: list[str] = []

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> str:
        del tools, tool_choice
        return await self.complete(messages, temperature=temperature)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.calls.append(messages)
        try:
            request = json.loads(messages[-1]["content"])
            inner_turn = request["inner_turn"]
            purpose = inner_turn["purpose"]
            source_refs = request["capability_manifest"]["source_refs"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return await super().complete(messages, temperature=temperature)
        if purpose == "life_development_choice":
            payload = {
                "completion": {
                    "decision": "accept",
                    "intention_summary": "我想去看看。",
                    "importance_bp": 4_300,
                    "participant_refs": [],
                }
            }
        elif purpose == "activity_lifecycle_choice":
            openings = request["capability_manifest"]["payload"]["openings"]
            payload = {
                "decision": "select",
                "selected_token": openings[0]["opening_token"],
            }
        elif purpose == "outcome_selection":
            offered_tokens = request["capability_manifest"]["payload"]["offered_tokens"]
            self.outcome_offered_tokens = tuple(offered_tokens)
            selected_token = offered_tokens[-1]
            self.outcome_selected_tokens.append(selected_token)
            payload = {
                "selected_token": selected_token,
                "character_life_direction": None,
            }
        else:
            return await super().complete(messages, temperature=temperature)
        return json.dumps(
            {
                "status": "decision",
                "summary": "我自己做了这个选择。",
                "attended_source_refs": source_refs,
                "decision": {
                    "source_refs": source_refs,
                    "payload": payload,
                },
                "recall_query": None,
                "proposals": [],
            },
            ensure_ascii=False,
        )


class _Delivery:
    async def send_text(self, _recipient_id: str, _text: str) -> dict[str, object]:
        return {"status": "ok", "data": {"message_id": "life-activity-text"}}

    async def send_reaction(
        self,
        _recipient_id: str,
        *,
        message_id: str,
        reaction_id: str,
    ) -> dict[str, object]:
        del message_id, reaction_id
        return {"status": "failed"}

    async def send_sticker(
        self,
        _recipient_id: str,
        *,
        sticker_id: str,
    ) -> dict[str, object]:
        del sticker_id
        return {"status": "failed"}

    async def send_typing(
        self,
        _recipient_id: str,
        *,
        state: str,
    ) -> dict[str, object]:
        del state
        return {"status": "ok", "data": {"message_id": "life-activity-typing"}}

    async def get_message(
        self,
        _recipient_id: str,
        *,
        message_id: str,
    ) -> dict[str, object]:
        return {"status": "ok", "retcode": 0, "data": {"message_id": message_id}}


def _host_scenario(nodeid: str, *, scenario_id: str = _SCENARIO_ID) -> None:
    evidence = load_delayed_trigger_catalog(_CATALOG).host_scenario(scenario_id)
    assert evidence.test_nodeid == nodeid
    assert evidence.mechanism_ids == (
        ("life.activity_lifecycle",)
        if scenario_id == _SCENARIO_ID
        else ("life.aftermath_outcome",)
    )


@pytest.mark.asyncio
async def test_public_host_activity_lifecycle_is_role_owned_and_effect_once(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    _host_scenario(request.node.nodeid)
    started_at = datetime.now(UTC).replace(microsecond=0)
    scheduler_clock = {"now": started_at}
    database = tmp_path / "life-activity-host-qualification.sqlite"
    settings = Settings(
        _env_file=None,
        database_path=database,
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        WORLD_V2_LIFE_SOURCE_REVIEW_ENABLED=False,
    )

    async def skip_pacing(seconds: float) -> None:
        scheduler_clock["now"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    world_author = _PlanWorldAuthor()
    character_model = _CharacterModel()
    source_reviewer = _LifeSourceReviewer()

    def build():
        return build_qq_c2c_host(
            settings=settings,
            recipient_id="10001",
            bootstrap_at=started_at,
            model=character_model,
            world_support_model=world_author,
            life_source_closure_model=source_reviewer,
            delivery=_Delivery(),
            ingress_now=lambda: scheduler_clock["now"],
            ingress_sleep=skip_pacing,
            action_due_now=lambda: scheduler_clock["now"],
            use_configured_recall_embedding=False,
        )

    host = build()
    try:
        first_due = started_at + timedelta(minutes=10)
        await host.tick(
            tick_id="life-activity-public-plan",
            logical_time_from=started_at,
            logical_time_to=first_due,
            observed_at=first_due,
            reason="life_activity_public_plan",
            run_life_ecology=True,
        )
        first = host.export_replay_evidence()
        assert len(first.projection.plans) == 1
        assert world_author.calls == 1

        second_due = first_due + timedelta(minutes=1)
        scheduler_clock["now"] = second_due
        await host.tick(
            tick_id="life-activity-public-choice",
            logical_time_from=first_due,
            logical_time_to=second_due,
            observed_at=second_due,
            reason="life_activity_public_choice",
            run_life_ecology=True,
        )
        chosen = host.export_replay_evidence()
        assert len(chosen.projection.world_occurrences) == 1
        occurrence = chosen.projection.world_occurrences[0]
        assert occurrence.status == "active"
        assert len(character_model.calls) >= 2
        assert source_reviewer.calls >= 1
        proposal_events = tuple(
            item for item in chosen.events if item.event.event_type == "ActivityLifecycleProposalRecorded"
        )
        acceptance_events = tuple(
            item for item in chosen.events if item.event.event_type == "AcceptanceRecorded"
        )
        effect_events = tuple(
            item for item in chosen.events if item.event.event_type == "ActivityStarted"
        )
        activation_events = tuple(
            item for item in chosen.events if item.event.event_type == "WorldOccurrenceActivated"
        )
        assert len(proposal_events) == len(acceptance_events) == len(effect_events) == 1
        assert len(activation_events) == 1
        proposal = proposal_events[0].event.payload()
        acceptance = acceptance_events[0].event.payload()
        effect = effect_events[0].event.payload()
        activation = activation_events[0].event.payload()
        assert acceptance["proposal_id"] == proposal["proposal_id"]
        assert effect["activity_lifecycle_proposal_id"] == proposal["proposal_id"]
        assert effect["plan_id"] == proposal["plan_id"]
        assert effect["accepted_change_hash"] == proposal["proposed_change_hash"]
        assert occurrence.occurrence_id == activation["occurrence_id"]
        before_repeat = chosen
        character_calls_before_repeat = len(character_model.calls)
        effect_event_types = {
            "ActivityLifecycleProposalRecorded",
            "AcceptanceRecorded",
            "ActivityStarted",
            "WorldOccurrenceActivated",
        }
        effect_event_ids_before = tuple(
            item.event.event_id
            for item in before_repeat.events
            if item.event.event_type in effect_event_types
        )
        await host.tick(
            tick_id="life-activity-public-choice",
            logical_time_from=first_due,
            logical_time_to=second_due,
            observed_at=second_due,
            reason="life_activity_public_choice",
            run_life_ecology=True,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        repeated = host.export_replay_evidence()
        assert tuple(
            item.event.event_id
            for item in repeated.events
            if item.event.event_type in effect_event_types
        ) == effect_event_ids_before
        assert len(character_model.calls) == character_calls_before_repeat
        await host.aclose()
        host = build()
        await host.drain(max_action_units=8, max_background_units=16)
        cold = host.export_replay_evidence()
        assert cold.cursor == repeated.cursor
        assert cold.projection.semantic_hash == repeated.projection.semantic_hash
        assert len(cold.events) == len(repeated.events)
        assert tuple(
            item.event.event_id
            for item in cold.events
            if item.event.event_type in effect_event_types
        ) == effect_event_ids_before
        assert len(character_model.calls) == character_calls_before_repeat
        health = await host.world_health_diagnostics()
        assert health["mechanisms"]["life_ecology"]["schedule"]["last_outcome_ref"] == (
            "life-ecology:aftermath_occurrence_opened"
        )
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_public_host_aftermath_outcome_is_role_owned_and_effect_once(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    _host_scenario(request.node.nodeid, scenario_id=_AFTERMATH_SCENARIO_ID)
    started_at = datetime.now(UTC).replace(microsecond=0)
    scheduler_clock = {"now": started_at}
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "life-aftermath-outcome-host-qualification.sqlite",
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        WORLD_V2_LIFE_SOURCE_REVIEW_ENABLED=False,
    )

    async def skip_pacing(seconds: float) -> None:
        scheduler_clock["now"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    world_author = _CharacterChoiceWorldAuthor()
    character_model = _CharacterModel()
    source_reviewer = _LifeSourceReviewer()

    def build():
        return build_qq_c2c_host(
            settings=settings,
            recipient_id="10001",
            bootstrap_at=started_at,
            model=character_model,
            world_support_model=world_author,
            life_source_closure_model=source_reviewer,
            delivery=_Delivery(),
            ingress_now=lambda: scheduler_clock["now"],
            ingress_sleep=skip_pacing,
            action_due_now=lambda: scheduler_clock["now"],
            use_configured_recall_embedding=False,
        )

    host = build()
    try:
        plan_due = started_at + timedelta(minutes=10)
        await host.tick(
            tick_id="life-aftermath-public-plan",
            logical_time_from=started_at,
            logical_time_to=plan_due,
            observed_at=plan_due,
            reason="life_aftermath_public_plan",
            run_life_ecology=True,
        )
        choice_due = plan_due + timedelta(minutes=1)
        scheduler_clock["now"] = choice_due
        await host.tick(
            tick_id="life-aftermath-public-choice",
            logical_time_from=plan_due,
            logical_time_to=choice_due,
            observed_at=choice_due,
            reason="life_aftermath_public_choice",
            run_life_ecology=True,
        )
        active = host.export_replay_evidence()
        assert len(active.projection.world_occurrences) == 1
        assert active.projection.world_occurrences[0].status == "active"

        settle_due = choice_due + timedelta(minutes=1)
        scheduler_clock["now"] = settle_due
        await host.tick(
            tick_id="life-aftermath-public-settle",
            logical_time_from=choice_due,
            logical_time_to=settle_due,
            observed_at=settle_due,
            reason="life_aftermath_public_settle",
            run_life_ecology=True,
        )
        settled = host.export_replay_evidence()
        assert settled.projection.world_occurrences[0].status == "settled"

        observation_events = tuple(
            item for item in settled.events if item.event.event_type == "OutcomeObservationRecorded"
        )
        proposal_events = tuple(
            item for item in settled.events if item.event.event_type == "OutcomeProposalRecorded"
        )
        settlement_events = tuple(
            item for item in settled.events if item.event.event_type == "WorldOccurrenceSettled"
        )
        assert len(observation_events) == len(proposal_events) == 1
        observation = observation_events[0].event.payload()
        proposal = proposal_events[0].event.payload()
        acceptance_events = tuple(
            item
            for item in settled.events
            if item.event.event_type == "AcceptanceRecorded"
            and item.event.payload().get("proposal_id") == proposal["outcome_proposal_id"]
        )
        assert len(acceptance_events) == len(settlement_events) == 1
        acceptance = acceptance_events[0].event.payload()
        settlement = settlement_events[0].event.payload()
        assert len(character_model.outcome_offered_tokens) >= 2
        assert character_model.outcome_selected_tokens == [
            character_model.outcome_offered_tokens[-1]
        ]
        assert character_model.outcome_selected_tokens[0] != character_model.outcome_offered_tokens[0]
        assert proposal["decision_authority"] == "character_model"
        assert proposal["decision_model"] == character_model.model
        assert proposal["candidate_result_ref"] == character_model.outcome_selected_tokens[0]
        assert proposal["occurrence_id"] == active.projection.world_occurrences[0].occurrence_id
        assert proposal["decision_model_result_ref"]
        assert proposal["decision_model_result_event_ref"]
        assert proposal["decision_audit_proposal_event_ref"]
        assert any(
            item.event.event_id == proposal["decision_model_result_event_ref"]
            and item.event.event_type == "ModelResultRecorded"
            for item in settled.events
        )
        assert any(
            item.event.event_id == proposal["decision_audit_proposal_event_ref"]
            and item.event.event_type == "ProposalRecorded"
            for item in settled.events
        )
        assert observation["observation"]["occurrence_id"] == proposal["occurrence_id"]
        assert proposal_events[0].event.causation_id == proposal["decision_audit_proposal_event_ref"]
        assert acceptance["proposal_id"] == proposal["outcome_proposal_id"]
        assert acceptance["accepted_change_hash"] == proposal["proposed_change_hash"]
        assert settlement["occurrence_id"] == proposal["occurrence_id"]
        assert settlement["outcome_proposal_id"] == proposal["outcome_proposal_id"]
        assert settlement["candidate_result_ref"] == proposal["candidate_result_ref"]
        assert settlement["accepted_change_hash"] == proposal["proposed_change_hash"]
        assert settlement_events[0].event.causation_id == acceptance_events[0].event.event_id

        def calls_for_purpose(purpose: str) -> int:
            count = 0
            for messages in character_model.calls:
                try:
                    request = json.loads(messages[-1]["content"])
                    if request.get("inner_turn", {}).get("purpose") == purpose:
                        count += 1
                except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                    continue
            return count

        assert calls_for_purpose("outcome_selection") == 1
        assert source_reviewer.calls >= 1
        outcome_event_types = {
            "OutcomeObservationRecorded",
            "OutcomeProposalRecorded",
            "AcceptanceRecorded",
            "WorldOccurrenceSettled",
        }
        outcome_event_ids = tuple(
            item.event.event_id
            for item in settled.events
            if item.event.event_type in outcome_event_types
        )
        outcome_calls_before_repeat = calls_for_purpose("outcome_selection")
        await host.tick(
            tick_id="life-aftermath-public-settle",
            logical_time_from=choice_due,
            logical_time_to=settle_due,
            observed_at=settle_due,
            reason="life_aftermath_public_settle",
            run_life_ecology=True,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        repeated = host.export_replay_evidence()
        assert tuple(
            item.event.event_id
            for item in repeated.events
            if item.event.event_type in outcome_event_types
        ) == outcome_event_ids
        assert repeated.projection.semantic_hash == repeated.replay.semantic_hash
        assert calls_for_purpose("outcome_selection") == outcome_calls_before_repeat

        await host.aclose()
        host = build()
        await host.drain(max_action_units=8, max_background_units=16)
        cold = host.export_replay_evidence()
        assert cold.cursor == repeated.cursor
        assert cold.projection.semantic_hash == repeated.projection.semantic_hash
        assert cold.projection.semantic_hash == cold.replay.semantic_hash
        assert len(cold.events) == len(repeated.events)
        assert tuple(
            item.event.event_id
            for item in cold.events
            if item.event.event_type in outcome_event_types
        ) == outcome_event_ids
        assert calls_for_purpose("outcome_selection") == outcome_calls_before_repeat
    finally:
        await host.aclose()
