from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from companion_daemon.config import Settings
from companion_daemon.world_v2.interactive_turn_budget import InteractiveTurnBudgetPolicy
from companion_daemon.world_v2.private_self_expression_audit import (
    ModelResultAttemptAudit,
    NaturalnessReadinessAudit,
    PreconversationLifeEcologyAudit,
    PrivateSelfCausalChainAudit,
    PrivateSelfExpressionAuditEvaluator,
    PrivateSelfExpressionAuditSummary,
    assess_naturalness_readiness,
    classify_model_attempt_lane,
    load_private_self_expression_scenario,
)
from companion_daemon.world_v2.proposal_audit_schemas import (
    ModelResultAuditProjection,
    RecordedModelResultAudit,
    canonical_json,
    model_audit_json,
    sha256,
)
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host
from companion_daemon.world_v2.qq_c2c_host import qq_c2c_world_id
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
FIXTURE = Path(__file__).with_name("fixtures") / "private_self_expression_targeted.json"


@pytest.mark.parametrize(
    "outcome",
    (
        "winner",
        "returned",
        "invalid",
        "timeout",
        "exception",
        "hedge_cancelled",
        "hedge_lost",
        "budget_exhausted",
    ),
)
def test_model_result_attempt_audit_accepts_every_recorded_projection_outcome(
    outcome: str,
) -> None:
    attempt = ModelResultAttemptAudit(
        model_result_ref="model-result:attempt-outcome",
        attempt_lane="expression",
        status="candidate_returned" if outcome == "returned" else "proposal_validated",
        request_hash="a" * 64,
        response_hash="b" * 64,
        attempt_index=0,
        attempt_count=1,
        slot="primary",
        outcome=outcome,
    )

    assert attempt.outcome == outcome


def test_naturalness_readiness_keeps_zero_preheat_reliability_only() -> None:
    projection = SimpleNamespace(
        experiences=(),
        memory_candidates=(),
        character_core=object(),
        appraisals=(),
        affect_episodes=(),
        relationship_states=(),
        private_impressions=(),
        plans=(),
        commitments=(),
        threads=(),
        trigger_processes=(
            SimpleNamespace(
                process_kind="interaction_appraisal",
                source_evidence_ref="observation:one",
                state="terminal",
            ),
        ),
    )

    readiness = assess_naturalness_readiness(
        projection=projection,
        immutable_replay_audit={
            "turns": [
                {"turn_id": "turn:one", "observation_id": "observation:one"},
                {"turn_id": "turn:two", "observation_id": "observation:two"},
            ],
        },
        requested_preconversation_units=0,
    )

    assert isinstance(readiness, NaturalnessReadinessAudit)
    assert readiness.assessment == "reliability_only"
    assert readiness.production_behavior_gate is False
    assert readiness.source_bound_self_material.status == "unavailable"
    assert readiness.source_bound_self_material.committed_experience_count == 0
    assert readiness.source_bound_self_material.active_memory_candidate_count == 0
    assert readiness.inner_life_snapshot.status == "available"
    assert (
        readiness.inner_life_snapshot.evidence_basis
        == "immutable_projection_inputs_not_provider_delivery"
    )
    assert readiness.prior_interaction_appraisal.status == "settled"
    assert readiness.prior_interaction_appraisal.eligible_observation_count == 1
    assert readiness.prior_interaction_appraisal.terminal_count == 1
    assert "zero_preconversation_life_ecology" in readiness.reason_codes


def test_naturalness_readiness_reports_source_material_but_pending_prior_appraisal() -> None:
    projection = SimpleNamespace(
        experiences=(
            SimpleNamespace(
                authority_contract_version="experience.1",
                status="committed",
                values=SimpleNamespace(source_bindings=(object(),)),
            ),
        ),
        memory_candidates=(
            SimpleNamespace(
                values=SimpleNamespace(status="active", source_bindings=(object(),)),
            ),
        ),
        character_core=None,
        appraisals=(),
        affect_episodes=(),
        relationship_states=(),
        private_impressions=(),
        plans=(),
        commitments=(),
        threads=(),
        trigger_processes=(
            SimpleNamespace(
                process_kind="interaction_appraisal",
                source_evidence_ref="observation:one",
                state="claimed",
            ),
        ),
    )

    readiness = assess_naturalness_readiness(
        projection=projection,
        immutable_replay_audit={
            "turns": [
                {"turn_id": "turn:one", "observation_id": "observation:one"},
                {"turn_id": "turn:two", "observation_id": "observation:two"},
            ],
        },
        requested_preconversation_units=1,
    )

    assert readiness.assessment == "not_ready_for_naturalness_observation"
    assert readiness.source_bound_self_material.status == "available"
    assert readiness.source_bound_self_material.committed_experience_count == 1
    assert readiness.source_bound_self_material.active_memory_candidate_count == 1
    assert readiness.inner_life_snapshot.status == "available"
    assert readiness.prior_interaction_appraisal.status == "pending"
    assert readiness.prior_interaction_appraisal.pending_count == 1
    assert readiness.reason_codes == ("prior_interaction_appraisal_pending",)


def test_legacy_character_recall_audit_fields_populate_explicit_pull_names() -> None:
    causal = PrivateSelfCausalChainAudit(
        private_state_recorded=True,
        character_recall_selected=True,
        final_private_state_recorded_after_character_recall=True,
        source_bound_claim_recorded=False,
        visible_action_authorized=True,
        terminal_receipt_recorded=True,
    )
    summary = PrivateSelfExpressionAuditSummary(
        turn_count=1,
        effect_accepted_turn_count=1,
        model_silent_turn_count=0,
        character_recall_turn_count=1,
        turns_with_surface_question_marks=0,
        surface_question_mark_count=0,
        terminal_delivery_turn_count=1,
        technical_failure_turn_count=0,
        unclassified_attempt_turn_count=0,
    )

    assert causal.prefetch_presented is False
    assert causal.character_pull_selected is True
    assert summary.prefetch_presented_turn_count == 0
    assert summary.character_pull_selected_turn_count == 1
    assert (
        summary.surface_question_counting_policy
        == "surface_question_marks_only_descriptive"
    )


def _preconversation_life_ecology_report() -> dict[str, object]:
    return {
        "contract": "private-self-expression-preconversation-life-ecology.2",
        "requested_units": 1,
        "unit_seconds": 600,
        "world_started_at": NOW - timedelta(minutes=10),
        "conversation_started_at": NOW,
        "tick_statuses": ("observed_only",),
        "tick_statuses_deprecated": True,
        "tick_statuses_semantics": "legacy_clock_status_only",
        "units": (
            {
                "ordinal": 1,
                "logical_time_from": NOW - timedelta(minutes=10),
                "logical_time_to": NOW,
                "clock_status": "observed_only",
                "ecology_status": "technical_failure",
                "ecology_reason_code": "life_development.model_timeout",
                "ecology_runtime_outcome_ref": (
                    "life-ecology:technical_failure.life_development.model_timeout"
                ),
                "ecology_trigger_id": "trigger:life:1",
                "ecology_completion_event_ref": "event:life:complete:1",
                "ledger_sequence_before": 4,
                "ledger_sequence_after": 7,
            },
        ),
        "ecology_status_counts": {
            "accepted": 0,
            "cooldown": 0,
            "no_op": 0,
            "not_observed": 0,
            "technical_failure": 1,
            "unknown": 0,
        },
        "ecology_reason_code_counts": {"life_development.model_timeout": 1},
        "ledger_sequence_before": 4,
        "ledger_sequence_after": 7,
        "new_event_type_counts": {
            "ClockAdvanced": 1,
            "TriggerProcessCompleted": 1,
            "TriggerProcessOpened": 1,
        },
        "experience_count_before": 0,
        "experience_count_after": 0,
        "plan_count_before": 0,
        "plan_count_after": 0,
        "memory_candidate_count_before": 0,
        "memory_candidate_count_after": 0,
    }


def test_preconversation_life_ecology_report_schema_keeps_clock_and_work_distinct() -> None:
    report = PreconversationLifeEcologyAudit.model_validate(_preconversation_life_ecology_report())

    assert report.tick_statuses == ("observed_only",)
    assert report.units[0].clock_status == "observed_only"
    assert report.units[0].ecology_status == "technical_failure"
    assert report.ecology_status_counts.technical_failure == 1

    invalid = _preconversation_life_ecology_report()
    invalid["ecology_status_counts"] = {
        **invalid["ecology_status_counts"],  # type: ignore[arg-type]
        "technical_failure": 0,
    }
    with pytest.raises(ValidationError, match="ecology status counts"):
        PreconversationLifeEcologyAudit.model_validate(invalid)


def test_attempt_lane_does_not_guess_unknown_or_legacy_foreground_identity() -> None:
    assert (
        classify_model_attempt_lane(
            attempt_id="attempt:expression-episode:current",
            expression_authority_present=True,
        )
        == "expression"
    )
    assert (
        classify_model_attempt_lane(
            attempt_id="attempt:pinned-turn:background",
            expression_authority_present=True,
        )
        == "background"
    )
    assert (
        classify_model_attempt_lane(
            attempt_id="attempt:pinned-turn:legacy-or-background",
            expression_authority_present=False,
        )
        == "unknown"
    )
    assert (
        classify_model_attempt_lane(
            attempt_id="attempt:future-expression:new-authority",
            expression_authority_present=True,
        )
        == "unknown"
    )


class _Delivery:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        del recipient_id
        self.sent.append(text)
        return {
            "status": "ok",
            "data": {"message_id": f"private-self-audit-{len(self.sent)}"},
        }

    async def send_reaction(
        self,
        recipient_id: str,
        *,
        message_id: str,
        reaction_id: str,
    ) -> dict[str, object]:
        del recipient_id, message_id, reaction_id
        return {"status": "ok", "data": {"message_id": "private-self-audit-reaction"}}

    async def send_sticker(
        self,
        recipient_id: str,
        *,
        sticker_id: str,
    ) -> dict[str, object]:
        del recipient_id, sticker_id
        return {"status": "ok", "data": {"message_id": "private-self-audit-sticker"}}

    async def send_typing(
        self,
        recipient_id: str,
        *,
        state: str,
    ) -> dict[str, object]:
        del recipient_id, state
        return {"status": "ok", "data": {"message_id": "private-self-audit-typing"}}

    async def get_message(
        self,
        recipient_id: str,
        *,
        message_id: str,
    ) -> dict[str, object]:
        del recipient_id
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": message_id, "message": self.sent[-1]},
        }


class _AuditClock:
    def __init__(self) -> None:
        self.current = NOW

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)


class _RecallEmbedding:
    version = "private-self-audit-recall.1"
    dimensions = 2
    dense_match_threshold_bp = 4_000

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            (1.0, 0.0)
            if any(cue in text for cue in ("学校门口", "摊贩", "争了半天"))
            else (0.0, 1.0)
            for text in texts
        )


class _BackgroundModel:
    model = "fixture:private-self-audit-background"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        del temperature
        system = messages[0]["content"]
        user = messages[1]["content"]
        if "Audit only factual source closure" in system:
            review = json.loads(user)
            assert review["output_contract"]["contract"] == "source-closure-review.7"
            return '{"ci":[],"v":[],"p":[]}'
        if "Classify fallible semantic interpretations" in system:
            return '{"classifications":[]}'
        if "Assess one verified user message" in system:
            text = str(json.loads(user).get("text", ""))
            if "学校门口那个摊贩" in text:
                return json.dumps(
                    {
                        "retain": True,
                        "predicate_code": "situation.recent",
                        "value": "学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天",
                        "privacy_class": "personal",
                        "confidence": 9_000,
                        "rationale": "The user explicitly described a recent upsetting event.",
                    },
                    ensure_ascii=False,
                )
            return '{"retain":false}'
        if "already verified user Fact" in system:
            return json.dumps(
                {
                    "retain": True,
                    "cue_kind": "emotional_residue",
                    "retention_rationales": ["emotional_salience", "future_utility"],
                    "salience": {
                        "autobiographical_relevance_bp": 1_000,
                        "relationship_relevance_bp": 5_000,
                        "emotional_residue_bp": 8_000,
                        "unfinished_business_bp": 3_000,
                        "recurrence_bp": 1_000,
                        "novelty_bp": 4_000,
                        "future_utility_bp": 6_000,
                        "world_continuity_bp": 2_000,
                    },
                },
                ensure_ascii=False,
            )
        return '{"decision":"no_change"}'


def _recall_material(messages: list[dict[str, str]]) -> dict[str, object] | None:
    if len(messages) >= 4:
        content = messages[-1]["content"]
        marker = "\n{"
        marker_index = content.rfind(marker)
        if marker_index >= 0:
            return json.loads(content[marker_index + 1 :])

    # Production CharacterInterior performs the bounded pull outside the
    # provider transcript, then invokes the same author with the result in the
    # source-bound selective-memory facet.  Normalize that production shape to
    # the older fixture helper's tiny audit view.
    supplied = json.loads(messages[1]["content"])
    snapshot = supplied.get("inner_life_snapshot")
    if not isinstance(snapshot, dict):
        return None
    materials = snapshot.get("materials")
    if not isinstance(materials, dict):
        return None
    selected = materials.get("selected_recall")
    if not isinstance(selected, dict):
        return None
    selected_content = selected.get("content")
    if not isinstance(selected_content, dict):
        return None
    items = selected_content.get("items")
    if not isinstance(items, list) or not items:
        return None
    candidates: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source_ref = item.get("source_ref")
        if not isinstance(source_ref, str) or not source_ref:
            continue
        candidates.append({**item, "source_refs": [source_ref]})
    if not candidates:
        return None
    return {"character_chosen_recall": {"candidates": candidates}}


def _first_hit_source_ref(material: dict[str, object]) -> str:
    chosen = material["character_chosen_recall"]
    assert isinstance(chosen, dict)
    hits = chosen["candidates"]
    assert isinstance(hits, list) and hits
    first = hits[0]
    assert isinstance(first, dict)
    refs = first["source_refs"]
    assert isinstance(refs, list) and refs
    return str(refs[0])


class _RoleModel:
    model = "fixture:private-self-audit-role"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        self.calls.append(messages)
        system = messages[0]["content"]
        raw_user = json.loads(messages[1]["content"])
        inner_turn = raw_user.get("inner_turn")
        if isinstance(inner_turn, dict) and inner_turn.get("purpose") == "fact_memory_retention":
            capability = raw_user.get("capability_manifest")
            assert isinstance(capability, dict)
            source_refs = capability.get("source_refs")
            assert isinstance(source_refs, list) and source_refs
            return json.dumps(
                {
                    "status": "decision",
                    "summary": "这段具体经历对以后理解她很有用，我愿意把它留下来。",
                    "attended_source_refs": [],
                    "decision": {
                        "source_refs": source_refs,
                        "payload": {
                            "retain": True,
                            "cue_kind": "emotional_residue",
                            "retention_rationales": [
                                "emotional_salience",
                                "future_utility",
                            ],
                            "salience": {
                                "autobiographical_relevance_bp": 1_000,
                                "relationship_relevance_bp": 5_000,
                                "emotional_residue_bp": 8_000,
                                "unfinished_business_bp": 3_000,
                                "recurrence_bp": 1_000,
                                "novelty_bp": 4_000,
                                "future_utility_bp": 6_000,
                                "world_continuity_bp": 2_000,
                            },
                        },
                    },
                    "recall_query": None,
                    "proposals": [],
                },
                ensure_ascii=False,
            )
        user_material = raw_user
        trigger = user_material["current_trigger_message"]
        assert isinstance(trigger, dict)
        text = str(trigger["text"])
        observation_ref = str(trigger["observation_ref"])
        combined = "appraisal_draft" in system and "expression_draft" in system
        private_state = {
            "contract": "private-turn-state.1",
            "inner_state_summary": "我先按自己此刻真正注意到的东西决定要不要回应。",
            "attended_source_refs": [observation_ref],
        }

        if text.startswith("刚刚那句听着有点像客服"):
            recalled = _recall_material(messages)
            if recalled is None:
                return json.dumps(
                    {
                        "private_turn_state": {
                            "contract": "private-turn-state.1",
                            "inner_state_summary": (
                                "这句让我想起她最早说的摊贩争执；"
                                "我想先把那段原话找回来，再决定自己想说什么。"
                            ),
                            "attended_source_refs": [observation_ref],
                        },
                        "recall_request": {
                            "query_text": "学校门口摊贩争了半天",
                            "memory_kinds": ["episodic", "semantic"],
                            "limit": 4,
                        },
                    },
                    ensure_ascii=False,
                )
            recalled_ref = _first_hit_source_ref(recalled)
            boundaries = user_material["expression_hard_boundaries"]
            assert isinstance(boundaries, dict)
            source_refs = boundaries["world_claim_source_refs"]
            assert isinstance(source_refs, dict)
            stable_refs = source_refs["stable_identity"]
            assert isinstance(stable_refs, list) and stable_refs
            stable_ref = str(stable_refs[0])
            expression = {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": (
                        "重新看到她先前只是想吐槽，我不想再用问题把话题拽走；"
                        "我更想带一点自己的东西陪她待在这里。"
                    ),
                    "attended_source_refs": [recalled_ref],
                },
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [
                    {
                        "modality": "text",
                        "text": "我一直挺喜欢桂花乌龙。听你这么说，我先陪你骂两句，不急着追问。",
                    }
                ],
                "stance": "self_present_and_companionable",
                "brief_rationale": "I recalled her original intent and chose a grounded self-share.",
                "confidence": 8_200,
                "world_claims": [
                    {
                        "claim_text": "我一直喜欢桂花乌龙",
                        "scope": "stable_identity",
                        "source_refs": [stable_ref],
                    }
                ],
            }
        else:
            expression = {
                "private_turn_state": private_state,
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [{"modality": "text", "text": "嗯，我在听。"}],
                "stance": "present_without_interrogating",
                "brief_rationale": "I chose a small acknowledgement without forcing continuation.",
                "confidence": 7_000,
                "world_claims": [],
            }

        if not combined:
            return json.dumps(expression, ensure_ascii=False)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "affect": "no_change",
                    "brief_rationale": "No durable affect transition is required for this fixture.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_directed",
                    "display_strategy": "model_owned",
                    "confidence": 7_000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _ExpressionInvalidAppraisalValidRoleModel:
    model = "fixture:private-self-audit-expression-invalid-appraisal-valid"

    def __init__(self) -> None:
        self.call_count = 0

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del messages, temperature
        self.call_count += 1
        # Permanently invalid envelope: envelope repair (2026-08-08) must
        # still fail closed so the failure-audit privacy assertions hold.
        return '{"not":"an-expression-draft"}'


class _ExpressionValidAppraisalInvalidRoleModel:
    model = "fixture:private-self-audit-expression-valid-appraisal-invalid"

    def __init__(self) -> None:
        self.call_count = 0
        self._valid_expression = _RoleModel()

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.call_count += 1
        if self.call_count == 1:
            return await self._valid_expression.complete(
                messages,
                temperature=temperature,
            )
        return '{"not":"a-background-appraisal"}'


class _AppraisalInvalidBackgroundModel(_BackgroundModel):
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        system = messages[0]["content"]
        if "appraisal_draft" in system and "expression_draft" in system:
            return '{"not":"a-background-appraisal"}'
        return await super().complete(messages, temperature=temperature)


@pytest.mark.asyncio
async def test_targeted_fixture_audit_reads_private_self_recall_expression_and_receipt(
    tmp_path: Path,
) -> None:
    scenario = load_private_self_expression_scenario(FIXTURE)
    assert scenario.scenario_id == "private-self-expression-targeted-real-audit.v1"
    assert [turn.turn_id for turn in scenario.turns] == [
        "T01",
        "T02",
        "T03",
        "T04",
        "T05",
        "T06",
        "T07",
        "T09",
    ]
    delivery = _Delivery()
    ingress_clock = _AuditClock()
    action_clock = _AuditClock()
    role_model = _RoleModel()
    database_path = tmp_path / "private-self-expression-audit.sqlite"
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=database_path,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=role_model,
        world_support_model=_BackgroundModel(),
        delivery=delivery,
        ingress_now=ingress_clock.now,
        ingress_sleep=ingress_clock.sleep,
        # This fixture contains only immediate replies and explicitly audits
        # the terminal QQ verification receipt.  Its scheduler clock is
        # therefore virtualized separately from ingress pacing; ``later``
        # behavior has its own regression coverage and must never inherit
        # this fast-forward seam.
        action_due_now=action_clock.now,
        action_due_sleep=action_clock.sleep,
        # Action settlement fast-forwards its own virtual scheduler. Ingress
        # coalescing and the foreground wall clock share a different clock, so
        # Action pacing can never manufacture queue age for the next message.
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            wall_clock=ingress_clock.now,
        ),
        semantic_recall_embedding=_RecallEmbedding(),
    )
    try:
        for turn in scenario.turns:
            observed_at = NOW + timedelta(minutes=turn.at_minutes)
            ingress_clock.current = observed_at
            action_clock.current = observed_at
            outcome = await host.inbound_text(
                message_id=scenario.source_event_id(turn),
                recipient_id="10001",
                text=turn.text,
                observed_at=observed_at,
            )
            await host.drain(max_action_units=8, max_background_units=0)
            assert outcome.status == "action_authorized"
            if turn.turn_id == "T01":
                await host.drain(max_action_units=0, max_background_units=24)
        live_evidence = host.export_replay_evidence()
    finally:
        await host.aclose()
    cold_ledger = SQLiteWorldLedger(
        path=database_path,
        world_id=qq_c2c_world_id("geoff"),
    )
    try:
        evidence = cold_ledger.export_replay_evidence()
    finally:
        cold_ledger.close()

    assert live_evidence.cursor == evidence.cursor
    assert evidence.projection == evidence.replay

    report = PrivateSelfExpressionAuditEvaluator().evaluate(
        evidence=evidence,
        scenario=scenario,
    )
    final_turn = next(turn for turn in report.turns if turn.turn_id == "T09")

    assert final_turn.proposal_selection == "effect_accepted"
    assert final_turn.proposal_event is not None
    assert final_turn.proposal_event.event_ref.startswith("event:")
    assert final_turn.proposal_event.event_envelope_hash
    assert final_turn.model_result_event is not None
    assert final_turn.model_result_attempts
    selected_authors = tuple(
        attempt
        for attempt in final_turn.model_result_attempts
        if attempt.selected_proposal_author
    )
    assert len(selected_authors) == 1
    selected_author = selected_authors[0]
    assert selected_author.status == "proposal_validated"
    assert selected_author.failure_code is None
    assert selected_author.ledger_event is not None
    # A valid CharacterInterior recall choice is an internal phase of the same
    # semantic author turn.  It must not be misreported as an invalid primary
    # proposal merely because the role chose to retrieve before expressing.
    assert all(
        attempt.status != "main_invalid"
        for attempt in final_turn.model_result_attempts
    )
    assert final_turn.private_turn_state is not None
    assert final_turn.private_turn_state.inner_state_summary.startswith("重新看到她先前")
    assert final_turn.causal_chain.private_state_recorded
    # Parallel prefetch remains opportunistic.  The authoritative requirement
    # is the character-selected pull below; a slow/empty prefetch must neither
    # block nor become a second semantic author path.
    assert final_turn.causal_chain.character_pull_selected
    assert final_turn.causal_chain.character_recall_selected
    assert final_turn.causal_chain.final_private_state_recorded_after_character_recall
    assert final_turn.recall_traces[0].mode == "character_pull"
    assert final_turn.recall_traces[0].hit_source_refs
    assert final_turn.timing_choice == "now"
    assert final_turn.world_claims[0].scope == "stable_identity"
    assert final_turn.world_claims[0].source_refs
    assert final_turn.beats[0].text is not None
    assert "桂花乌龙" in final_turn.beats[0].text
    assert final_turn.surface_question_mark_count == 0
    assert [action.state for action in final_turn.actions] == ["delivered"]
    assert final_turn.actions[0].ledger_event is not None
    assert [receipt.observed_state for receipt in final_turn.receipts] == [
        "provider_accepted",
        "delivered",
    ]
    assert all(receipt.ledger_event is not None for receipt in final_turn.receipts)
    assert not final_turn.receipts[0].is_terminal
    assert final_turn.receipts[-1].is_terminal
    assert final_turn.causal_chain.visible_action_authorized
    assert final_turn.causal_chain.terminal_receipt_recorded
    prefetch_turns = [
        turn
        for turn in report.turns
        if any(
            trace.mode == "prefetch" and trace.hit_count > 0
            for trace in turn.recall_traces
        )
    ]
    assert prefetch_turns
    assert all(turn.causal_chain.prefetch_presented for turn in prefetch_turns)
    empty_prefetch_turns = [
        turn
        for turn in report.turns
        if any(
            trace.mode == "prefetch" and trace.hit_count == 0
            for trace in turn.recall_traces
        )
        and not any(
            trace.mode == "prefetch" and trace.hit_count > 0
            for trace in turn.recall_traces
        )
    ]
    assert all(
        not turn.causal_chain.prefetch_presented for turn in empty_prefetch_turns
    )
    assert report.summary.prefetch_presented_turn_count == len(prefetch_turns)
    assert report.summary.character_pull_selected_turn_count == 1
    assert report.summary.character_recall_turn_count == 1
    assert report.summary.turns_with_surface_question_marks == 0
    assert report.summary.reporting_policy == "descriptive_only_not_an_acceptance_rule"

    # audit.5 binds every automatic prefetch presentation to its actual model
    # call.  Rebuild an equivalent audit.4 replay shape to prove the evaluator
    # still reads ledgers written before the ordered presentation contract.
    legacy_model_audits = []
    for item in evidence.replay.model_result_audits:
        audit = RecordedModelResultAudit.model_validate_json(item.audit_json)
        if not audit.presented_prefetch_traces:
            legacy_model_audits.append(item)
            continue
        presentation = next(
            (
                candidate
                for candidate in audit.presented_prefetch_traces
                if candidate.trace.hits
            ),
            audit.presented_prefetch_traces[0],
        )
        legacy_audit = audit.model_copy(
            update={
                "prefetch_trace": presentation.trace,
                "presented_prefetch_traces": (),
            }
        )
        legacy_json = model_audit_json(legacy_audit)
        legacy_model_audits.append(
            item.model_copy(
                update={
                    "audit_contract": "model-result-audit.4",
                    "audit_json": legacy_json,
                    "audit_hash": sha256(legacy_json),
                }
            )
        )
    legacy_evidence = replace(
        evidence,
        replay=evidence.replay.model_copy(
            update={"model_result_audits": tuple(legacy_model_audits)}
        ),
    )
    legacy_report = PrivateSelfExpressionAuditEvaluator().evaluate(
        evidence=legacy_evidence,
        scenario=scenario,
    )

    assert legacy_report.summary.prefetch_presented_turn_count > 0
    assert any(
        trace.mode == "prefetch" and trace.hit_count > 0
        for turn in legacy_report.turns
        for trace in turn.recall_traces
    )


@pytest.mark.asyncio
async def test_missing_proposal_keeps_observation_bound_failure_audit_without_private_text(
    tmp_path: Path,
) -> None:
    scenario = load_private_self_expression_scenario(FIXTURE)
    first = scenario.turns[0]
    clock = _AuditClock()
    role_model = _ExpressionInvalidAppraisalValidRoleModel()
    database_path = tmp_path / "private-self-expression-failure-audit.sqlite"
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=database_path,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=role_model,
        world_support_model=_BackgroundModel(),
        delivery=_Delivery(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            wall_clock=clock.now,
        ),
    )
    try:
        outcome = await host.inbound_text(
            message_id=scenario.source_event_id(first),
            recipient_id="10001",
            text=first.text,
            observed_at=NOW,
        )
        await host.drain(max_action_units=0, max_background_units=24)
        live_evidence = host.export_replay_evidence()
    finally:
        await host.aclose()
    cold_ledger = SQLiteWorldLedger(
        path=database_path,
        world_id=qq_c2c_world_id("geoff"),
    )
    try:
        evidence = cold_ledger.export_replay_evidence()
    finally:
        cold_ledger.close()

    assert outcome.status == "deferred"
    assert live_evidence.cursor == evidence.cursor
    assert evidence.projection == evidence.replay
    report = PrivateSelfExpressionAuditEvaluator().evaluate(
        evidence=evidence,
        scenario=scenario,
    )
    turn = report.turns[0]

    assert turn.proposal_selection == "missing"
    assert turn.observations == ("model_result_failure_recorded",)
    assert turn.model_result_attempts
    assert {attempt.attempt_lane for attempt in turn.model_result_attempts} == {
        "expression",
    }
    assert all(
        attempt.status != "proposal_validated"
        for attempt in turn.model_result_attempts
        if attempt.attempt_lane == "expression"
    )
    expression_attempts = tuple(
        attempt for attempt in turn.model_result_attempts if attempt.attempt_lane == "expression"
    )
    assert all(attempt.failure_code for attempt in expression_attempts)
    assert all(attempt.failure_stage is not None for attempt in expression_attempts)
    assert all(attempt.failure_class is not None for attempt in expression_attempts)
    assert all(len(attempt.request_hash) == 64 for attempt in turn.model_result_attempts)
    assert all(
        attempt.ledger_event is not None
        and attempt.ledger_event.event_type == "ModelResultRecorded"
        for attempt in turn.model_result_attempts
    )
    assert first.text not in json.dumps(
        [attempt.model_dump(mode="json") for attempt in turn.model_result_attempts],
        ensure_ascii=False,
    )
    assert report.summary.technical_failure_turn_count == 1
    assert (
        report.summary.source_review_technical_failure_turn_count
        + report.summary.candidate_validation_exhausted_turn_count
        + report.summary.other_expression_failure_turn_count
        == report.summary.technical_failure_turn_count
    )

    legacy_model_audits = tuple(
        item
        if not RecordedModelResultAudit.model_validate_json(item.audit_json).attempt_id.startswith(
            "attempt:expression-episode:"
        )
        else item.model_copy(
            update={
                "attempt_id": (legacy_attempt_id := "attempt:pinned-turn:legacy-foreground"),
                "audit_json": model_audit_json(
                    RecordedModelResultAudit.model_validate_json(item.audit_json).model_copy(
                        update={"attempt_id": legacy_attempt_id}
                    )
                ),
            }
        )
        for item in evidence.replay.model_result_audits
    )
    legacy_evidence = replace(
        evidence,
        replay=evidence.replay.model_copy(update={"model_result_audits": legacy_model_audits}),
    )

    legacy_report = PrivateSelfExpressionAuditEvaluator().evaluate(
        evidence=legacy_evidence,
        scenario=scenario,
    )
    legacy_turn = legacy_report.turns[0]

    assert {attempt.attempt_lane for attempt in legacy_turn.model_result_attempts} == {"unknown"}
    assert legacy_turn.observations == (
        "accepted_expression_proposal_missing",
        "model_result_attempt_lane_unknown",
    )
    assert legacy_report.summary.technical_failure_turn_count == 0
    assert legacy_report.summary.unclassified_attempt_turn_count == 1


@pytest.mark.asyncio
async def test_nested_reviewer_winner_does_not_hide_failed_expression_author_chain(
    tmp_path: Path,
) -> None:
    scenario = load_private_self_expression_scenario(FIXTURE)
    first = scenario.turns[0]
    scenario = scenario.model_copy(update={"turns": (first,)})
    clock = _AuditClock()
    database_path = tmp_path / "private-self-expression-nested-reviewer.sqlite"
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=database_path,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_ExpressionInvalidAppraisalValidRoleModel(),
        world_support_model=_BackgroundModel(),
        delivery=_Delivery(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            wall_clock=clock.now,
        ),
    )
    try:
        outcome = await host.inbound_text(
            message_id=scenario.source_event_id(first),
            recipient_id="10001",
            text=first.text,
            observed_at=NOW,
        )
        evidence = host.export_replay_evidence()
    finally:
        await host.aclose()

    expression_template = next(
        item
        for item in evidence.replay.model_result_audits
        if RecordedModelResultAudit.model_validate_json(
            item.audit_json
        ).attempt_id.startswith("attempt:expression-episode:")
    )
    attempt_id = RecordedModelResultAudit.model_validate_json(
        expression_template.audit_json
    ).attempt_id
    primary_call_id = "model-call:audit-regression:primary"

    def recorded_attempt(
        *,
        label: str,
        model_call_id: str,
        parent_model_call_id: str | None,
        route_reason_code: str,
        status: str,
        failure_code: str | None,
        slot: str,
        result: str,
        has_response: bool,
    ) -> RecordedModelResultAudit:
        response_hash = sha256(f"{label}:response") if has_response else None
        model_result_ref = "model-result:" + sha256(
            canonical_json(
                {
                    "model_call_id": model_call_id,
                    "response_hash": response_hash,
                }
            )
        )
        return RecordedModelResultAudit.model_validate(
            {
                "model_call_id": model_call_id,
                "parent_model_call_id": parent_model_call_id,
                "model_result_ref": model_result_ref,
                "attempt_id": attempt_id,
                "route": {
                    "tier": "flash",
                    "reason_code": route_reason_code,
                    "router_version": "audit-regression.1",
                },
                "model_id": f"fixture:{label}" if has_response else None,
                "model_version": "2026-07" if has_response else None,
                "attempted_model_id": None if has_response else f"fixture:{label}",
                "attempted_model_version": None if has_response else "2026-07",
                "request_hash": sha256(f"{label}:request"),
                "response_hash": response_hash,
                "status": status,
                "failure_code": failure_code,
                "slot": slot,
                "outcome": result,
            }
        )

    def projected_attempt(
        audit: RecordedModelResultAudit,
        *,
        attempt_index: int,
        attempt_count: int,
    ) -> ModelResultAuditProjection:
        audit_json = model_audit_json(audit)
        return ModelResultAuditProjection.model_validate(
            {
                **expression_template.model_dump(mode="json"),
                "audit_contract": "model-result-audit.3",
                "model_result_ref": audit.model_result_ref,
                "deliberation_result_id": f"deliberation:{audit.model_call_id}",
                "proposal_hash": None,
                "model_call_id": audit.model_call_id,
                "parent_model_call_id": audit.parent_model_call_id,
                "attempt_id": audit.attempt_id,
                "attempt_index": attempt_index,
                "attempt_count": attempt_count,
                "audit_json": audit_json,
                "audit_hash": sha256(audit_json),
                "event_ref": f"event:ModelResultRecorded:{audit.model_call_id}",
                "event_payload_hash": sha256(f"{audit.model_call_id}:event"),
            }
        )

    primary = recorded_attempt(
        label="primary",
        model_call_id=primary_call_id,
        parent_model_call_id=None,
        route_reason_code="author_candidate.primary_initial.validation_rejected",
        status="main_invalid",
        failure_code="primary_invalid",
        slot="primary",
        result="invalid",
        has_response=True,
    )
    reviewer = recorded_attempt(
        label="source-reviewer",
        model_call_id="model-call:audit-regression:source-reviewer",
        parent_model_call_id=primary_call_id,
        route_reason_code="validation.source_review",
        status="proposal_validated",
        failure_code=None,
        slot="primary",
        result="winner",
        has_response=True,
    )
    reselection = recorded_attempt(
        label="validation-reselection",
        model_call_id="model-call:audit-regression:validation-reselection",
        parent_model_call_id=primary_call_id,
        route_reason_code="validation.validation_reselection",
        status="main_timeout",
        failure_code="source_review_timeout",
        slot="primary",
        result="timeout",
        has_response=False,
    )
    correction = recorded_attempt(
        label="correction",
        model_call_id="model-call:audit-regression:correction",
        parent_model_call_id=None,
        route_reason_code="ordinary_compute",
        status="main_timeout",
        failure_code="authored_subcall_timeout",
        slot="corrective",
        result="timeout",
        has_response=False,
    )
    backup = recorded_attempt(
        label="backup",
        model_call_id="model-call:audit-regression:backup",
        parent_model_call_id=None,
        route_reason_code="ordinary_compute",
        status="recovery_failed",
        failure_code="backup_timeout",
        slot="backup",
        result="timeout",
        has_response=False,
    )
    synthetic_expression_attempts = (
        projected_attempt(primary, attempt_index=0, attempt_count=1),
        projected_attempt(reviewer, attempt_index=0, attempt_count=1),
        projected_attempt(reselection, attempt_index=0, attempt_count=1),
        projected_attempt(correction, attempt_index=0, attempt_count=2),
        projected_attempt(backup, attempt_index=1, attempt_count=2),
    )
    background_attempts = tuple(
        item
        for item in evidence.replay.model_result_audits
        if not RecordedModelResultAudit.model_validate_json(
            item.audit_json
        ).attempt_id.startswith("attempt:expression-episode:")
    )
    evidence = replace(
        evidence,
        replay=evidence.replay.model_copy(
            update={
                "model_result_audits": (
                    *synthetic_expression_attempts,
                    *background_attempts,
                )
            }
        ),
    )

    report = PrivateSelfExpressionAuditEvaluator().evaluate(
        evidence=evidence,
        scenario=scenario,
    )
    turn = report.turns[0]
    reviewer_attempt = next(
        attempt
        for attempt in turn.model_result_attempts
        if attempt.model_result_ref == reviewer.model_result_ref
    )
    reselection_attempt = next(
        attempt
        for attempt in turn.model_result_attempts
        if attempt.model_result_ref == reselection.model_result_ref
    )

    assert outcome.status == "deferred"
    assert turn.proposal_selection == "missing"
    assert turn.observations == ("model_result_failure_recorded",)
    assert report.summary.technical_failure_turn_count == 1
    assert report.summary.source_review_technical_failure_turn_count == 0
    assert report.summary.other_expression_failure_turn_count == 1
    assert reviewer_attempt.parent_model_call_id == primary_call_id
    assert reviewer_attempt.route_reason_code == "validation.source_review"
    assert reviewer_attempt.status == "proposal_validated"
    assert reselection_attempt.parent_model_call_id == primary_call_id
    assert reselection_attempt.route_reason_code == "validation.validation_reselection"
    assert reselection_attempt.failure_stage == "role_reselection"


@pytest.mark.asyncio
async def test_accepted_unified_expression_never_opens_background_appraisal_author(
    tmp_path: Path,
) -> None:
    scenario = load_private_self_expression_scenario(FIXTURE)
    first = scenario.turns[0]
    scenario = scenario.model_copy(update={"turns": (first,)})
    clock = _AuditClock()
    role_model = _ExpressionValidAppraisalInvalidRoleModel()
    database_path = tmp_path / "private-self-expression-background-failure.sqlite"
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=database_path,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=role_model,
        world_support_model=_AppraisalInvalidBackgroundModel(),
        delivery=_Delivery(),
        ingress_now=clock.now,
        ingress_sleep=clock.sleep,
        interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
            wall_clock=clock.now,
        ),
    )
    try:
        outcome = await host.inbound_text(
            message_id=scenario.source_event_id(first),
            recipient_id="10001",
            text=first.text,
            observed_at=NOW,
        )
        await host.drain(max_action_units=0, max_background_units=24)
        evidence = host.export_replay_evidence()
    finally:
        await host.aclose()

    report = PrivateSelfExpressionAuditEvaluator().evaluate(
        evidence=evidence,
        scenario=scenario,
    )
    turn = report.turns[0]
    expression_attempts = tuple(
        attempt for attempt in turn.model_result_attempts if attempt.attempt_lane == "expression"
    )
    background_attempts = tuple(
        attempt for attempt in turn.model_result_attempts if attempt.attempt_lane == "background"
    )
    assert outcome.status == "action_authorized"
    assert turn.proposal_selection == "effect_accepted"
    assert any(attempt.status == "proposal_validated" for attempt in expression_attempts)
    assert background_attempts == ()
    assert len(role_model._valid_expression.calls) == 1  # noqa: SLF001
    assert report.summary.technical_failure_turn_count == 0
