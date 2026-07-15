"""Offline executable evidence for the frozen World v2 scenario corpus.

This runner is deliberately narrower than the human-likeness evaluator.  It
uses a fixed fake chat model and a deterministic fake provider, invokes only
``WorldV2TurnApplication`` for ingress/delivery, then exports replay evidence.
It can prove that the 120 frozen fixtures exercise the v2 authority chain; it
cannot prove a real model or a person finds the output human-like.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Literal

from companion_daemon.llm import FakeCompanionModel

from .activity_plan_runtime import ActivityPlanCommand
from .chat_model_deliberation_adapter import RoutedChatModelDeliberationAdapter
from .deliberation import ModelInput, ModelOutput, ModelRoute, RouteRequest
from .platform_action_executor import PlatformDispatchReceipt, PlatformDispatchRequest
from .production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from .occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OutcomeCandidateContent,
)
from .proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from .replay_evaluator import ReplayEvaluator
from .room_projection import RoomProjectionMaterializer
from .scenario_corpus import (
    SCENARIO_CORPUS_VERSION,
    TEST_ECONOMY_PROFILE_VERSION,
    ScenarioCase,
    ScenarioFault,
    verify_frozen_scenario_corpus,
)
from .simulator_adapters import SimulatorIdentityResolver
from .schemas import DueWindow, EvidenceRef, OutcomeObservation, WorldOccurrenceProjection


class ScenarioVerificationError(AssertionError):
    """A frozen scenario did not exercise its declared authority predicates."""


# Filled after the complete, fixed fake suite has been run.  A change to this
# value is a new offline mechanism baseline, not evidence of a human-likeness
# improvement.
FROZEN_OFFLINE_SUITE_MANIFEST_HASH = "f4e4439f702023b7b95f728c9cfe09730f4bcf7794404e82f85f714e679e5fd7"


class _FixedScenarioRouter:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(
            tier="flash",
            reason_code="phase8_fixed_fake_route",
            router_version="world-v2-scenario-runner.1",
        )


class _FixedScenarioTransport:
    """A fixed receipt provider with a deliberately small fault surface."""

    provider = "scenario:fixed-provider"

    def __init__(self, *, received_at: datetime, fault: ScenarioFault) -> None:
        self._received_at = received_at
        self._fault = fault
        self._receipts: dict[str, PlatformDispatchReceipt] = {}
        self.bodies: list[str] = []

    async def send(self, request: PlatformDispatchRequest) -> PlatformDispatchReceipt:
        existing = self._receipts.get(request.idempotency_key)
        if existing is not None:
            return existing
        status: Literal["delivered", "failed", "unknown"] = (
            "failed"
            if self._fault == "provider_failed"
            else "unknown"
            if self._fault == "provider_unknown"
            else "delivered"
        )
        identity = hashlib.sha256(request.fingerprint.encode("utf-8")).hexdigest()
        receipt = PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:scenario:{identity}",
            provider_ref=f"message:scenario:{identity}",
            status=status,
            error_class=(
                "simulated_provider_timeout"
                if status == "failed"
                else "simulated_provider_unknown"
                if status == "unknown"
                else None
            ),
            received_at=self._received_at,
            raw_payload_hash="sha256:" + hashlib.sha256(request.body.encode("utf-8")).hexdigest(),
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        self._receipts[request.idempotency_key] = receipt
        self.bodies.append(request.body)
        return receipt

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        receipt = self._receipts.get(idempotency_key)
        if receipt is not None and receipt.request_fingerprint != request_fingerprint:
            raise ScenarioVerificationError("provider lookup fingerprint mismatch")
        return receipt


class _FixedOutcomeModel:
    """One deterministic outcome decision for the seeded Phase-8 chain.

    This is deliberately a deliberation adapter, not a ledger fixture: the
    app still pins its audit, compiles the typed proposal, and atomically
    accepts settlement before the subsequent NPC appraisal can begin.
    """

    def __init__(self, *, scenario_turn_id: str) -> None:
        self._scenario_turn_id = scenario_turn_id
        self.calls = 0

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.calls += 1
        source = request.trigger_evidence[0]
        occurrence_id = f"occurrence:phase8:{self._scenario_turn_id}"
        proposal = DecisionProposal(
            proposal_id=f"proposal:phase8:{self._scenario_turn_id}:outcome",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(source,),
            proposed_changes=(
                TypedChange(
                    change_id=f"change:phase8:{self._scenario_turn_id}:outcome",
                    kind="outcome_settlement",
                    target_id=occurrence_id,
                    transition="settle",
                    expected_entity_revision=3,
                    evidence_refs=(source.ref_id,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="outcome_settlement.v1",
                        value={
                            "outcome_proposal_id": f"model-hint:phase8:{self._scenario_turn_id}:outcome",
                            "candidate_result_ref": f"candidate:phase8:{self._scenario_turn_id}:settled",
                            "result_id": f"result:phase8:{self._scenario_turn_id}:settled",
                            "entity_id": occurrence_id,
                            "entity_revision": 3,
                            "observations": [
                                {
                                    "ref_id": f"observation:phase8:{self._scenario_turn_id}:settled",
                                    "source_world_revision": source.source_world_revision,
                                    "immutable_hash": source.immutable_hash,
                                }
                            ],
                            "result_payload": {
                                "object_ref": f"payload:phase8:{self._scenario_turn_id}:settled",
                                "schema_version": "outcome-result.1",
                                "payload_hash": "sha256:" + "e" * 64,
                            },
                        },
                    ),
                ),
            ),
            action_intents=(),
            confidence=8400,
            brief_rationale="The frozen sidecar candidate matches the observed result.",
            behavior_tendency="continue_life",
            stance="settle_verified_outcome",
            display_strategy="withhold",
        )
        return ModelOutput(
            model_id="phase8-fixed-outcome",
            model_version="v1",
            raw_proposal=proposal.model_dump(mode="json"),
        )

    async def recover(self, request: ModelInput, _failure: str) -> ModelOutput:
        return await self.propose(request)


class _FixedNpcAppraisalModel:
    """Deterministic, source-bound appraisal for the settled seeded outcome."""

    def __init__(self, *, scenario_turn_id: str) -> None:
        self._scenario_turn_id = scenario_turn_id
        self.calls = 0

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.calls += 1
        source = request.trigger_evidence[0]
        proposal = DecisionProposal(
            proposal_id=f"proposal:phase8:{self._scenario_turn_id}:npc-appraisal",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(source,),
            proposed_changes=(
                TypedChange(
                    change_id=f"change:phase8:{self._scenario_turn_id}:npc-appraisal",
                    kind="appraisal_transition",
                    target_id=f"appraisal:phase8:{self._scenario_turn_id}:settled-outcome",
                    transition="activate",
                    expected_entity_revision=0,
                    evidence_refs=(source.ref_id,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="appraisal_transition.v1",
                        value={
                            "appraisal_id": f"appraisal:phase8:{self._scenario_turn_id}:settled-outcome",
                            "meaning_candidates": [
                                {"meaning": "goal_progress", "confidence": 7000},
                                {"meaning": "care", "confidence": 3000},
                            ],
                            "attribution": "situation",
                            "severity": 4300,
                            "confidence": 7000,
                            "expiry": None,
                        },
                    ),
                ),
            ),
            action_intents=(),
            confidence=7000,
            brief_rationale="The settled private occurrence has bounded subjective significance.",
            behavior_tendency="reflect",
            stance="attend",
            display_strategy="withhold",
        )
        return ModelOutput(
            model_id="phase8-fixed-npc-appraisal",
            model_version="v1",
            raw_proposal=proposal.model_dump(mode="json"),
        )

    async def recover(self, request: ModelInput, _failure: str) -> ModelOutput:
        return await self.propose(request)


class _FixedAffectModel:
    """Deterministic Affect proposal consuming the accepted NPC appraisal."""

    def __init__(self, *, scenario_turn_id: str) -> None:
        self._scenario_turn_id = scenario_turn_id
        self.calls = 0

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.calls += 1
        source = request.trigger_evidence[0]
        proposal = DecisionProposal(
            proposal_id=f"proposal:phase8:{self._scenario_turn_id}:affect",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(source,),
            proposed_changes=(
                TypedChange(
                    change_id=f"change:phase8:{self._scenario_turn_id}:affect",
                    kind="affect_transition",
                    target_id=f"affect:phase8:{self._scenario_turn_id}:settled-outcome",
                    transition="open",
                    expected_entity_revision=0,
                    evidence_refs=(source.ref_id,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="affect_transition.v1",
                        value={
                            "episode_id": f"affect:phase8:{self._scenario_turn_id}:settled-outcome",
                            "appraisal_change_refs": [
                                f"change:phase8:{self._scenario_turn_id}:npc-appraisal"
                            ],
                            "component_deltas": [{"name": "warmth", "value": 3300}],
                            "decay_config": {
                                "object_ref": "policy:decay:standard",
                                "schema_version": "affect-decay.1",
                                "payload_hash": "sha256:" + "c" * 64,
                            },
                            "residue_config": {
                                "object_ref": "policy:residue:standard",
                                "schema_version": "affect-residue.1",
                                "payload_hash": "sha256:" + "b" * 64,
                            },
                        },
                    ),
                ),
            ),
            action_intents=(),
            confidence=7100,
            brief_rationale="The accepted appraisal warrants a bounded warm residual.",
            affect_decision="propose",
            behavior_tendency="hold_space",
            stance="quietly_warm",
            display_strategy="withhold",
        )
        return ModelOutput(
            model_id="phase8-fixed-affect",
            model_version="v1",
            raw_proposal=proposal.model_dump(mode="json"),
        )

    async def recover(self, request: ModelInput, _failure: str) -> ModelOutput:
        return await self.propose(request)


class _FixedDelayedExpressionModel:
    """One two-beat DecisionProposal, then an ordinary reply after interruption.

    It is intentionally a model adapter input/output only.  The app still
    audits, accepts and schedules the two beats; the fixture never writes
    Actions, ExpressionPlans or receipts itself.
    """

    def __init__(self, *, scenario_turn_id: str) -> None:
        self._scenario_turn_id = scenario_turn_id
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) != 1:
            return json.dumps(
                {
                    "response_text": "我记得。刚才没有立刻接下去，是想先把前一段话说完整。",
                    "stance": "acknowledge_briefly",
                    "brief_rationale": "The delayed continuation was explicitly reconsidered after the new message.",
                    "confidence": 6100,
                },
                ensure_ascii=False,
            )
        request = json.loads(messages[-1]["content"])["request"]
        trigger = request["trigger_message"]
        source = ProposalEvidenceRef.model_validate(request["trigger_evidence"][0])
        base = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
        first = "我先把你这句接住。"
        second = "等我把手头这点收完，再认真回到刚刚的话。"
        first_hash = "sha256:" + hashlib.sha256(first.encode("utf-8")).hexdigest()
        second_hash = "sha256:" + hashlib.sha256(second.encode("utf-8")).hexdigest()
        suffix = self._scenario_turn_id
        change_id = f"change:phase8:{suffix}:delayed-expression"
        plan_id = f"plan:phase8:{suffix}:delayed-expression"
        first_beat = f"beat:phase8:{suffix}:immediate"
        second_beat = f"beat:phase8:{suffix}:delayed"
        proposal = DecisionProposal(
            proposal_id=f"proposal:phase8:{suffix}:delayed-expression",
            trigger_ref=request["trigger_ref"],
            evaluated_world_revision=request["evaluated_world_revision"],
            evidence_refs=(source,),
            proposed_changes=(
                TypedChange(
                    change_id=change_id,
                    kind="expression_plan_transition",
                    target_id=plan_id,
                    transition="accept",
                    evidence_refs=(source.ref_id,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="expression_plan_transition.v1",
                        value={
                            "plan_id": plan_id,
                            "overall_intent": "use one short acknowledgement, then a deliberate delayed continuation",
                            "ordering_policy": "dependencies",
                            "terminal_policy": "settle_after_terminal_beats",
                            "beat_drafts": [
                                {
                                    "beat_id": first_beat,
                                    "inline_text": first,
                                    "materialized_payload_ref": f"payload:phase8:{suffix}:immediate",
                                    "payload_hash": first_hash,
                                    "content_type": "text/plain",
                                    "dependency_beat_ids": [],
                                    "delay_window": None,
                                    "cancel_policy": "cancel-before-dispatch",
                                    "reconsider_policy": "reconsider-on-new-observation",
                                    "merge_policy": "never",
                                },
                                {
                                    "beat_id": second_beat,
                                    "inline_text": second,
                                    "materialized_payload_ref": f"payload:phase8:{suffix}:delayed",
                                    "payload_hash": second_hash,
                                    "content_type": "text/plain",
                                    "dependency_beat_ids": [first_beat],
                                    "delay_window": {
                                        "not_before": (base + timedelta(minutes=2)).isoformat(),
                                        "expires_at": (base + timedelta(minutes=10)).isoformat(),
                                    },
                                    "cancel_policy": "cancel-before-dispatch",
                                    "reconsider_policy": "reconsider-on-new-observation",
                                    "merge_policy": "merge-if-reconsidered",
                                },
                            ],
                        },
                    ),
                ),
            ),
            action_intents=(
                ProposalActionIntent(
                    intent_id=f"intent:phase8:{suffix}:immediate",
                    kind="reply",
                    layer="external_action",
                    target=trigger["reply_target"],
                    payload_ref=f"payload:phase8:{suffix}:immediate",
                    payload_hash=first_hash,
                    causal_change_id=change_id,
                    beat_ref=first_beat,
                ),
                ProposalActionIntent(
                    intent_id=f"intent:phase8:{suffix}:delayed",
                    kind="followup",
                    layer="external_action",
                    target=trigger["reply_target"],
                    payload_ref=f"payload:phase8:{suffix}:delayed",
                    payload_hash=second_hash,
                    causal_change_id=change_id,
                    beat_ref=second_beat,
                    dependencies=(f"intent:phase8:{suffix}:immediate",),
                    due_window=(base + timedelta(minutes=2), base + timedelta(minutes=10)),
                ),
            ),
            confidence=7800,
            brief_rationale="The delayed beat is separate because a new user message can still change whether it should be sent.",
            drives=("continue_conversation",),
            behavior_tendency="engage",
            stance="paced",
            display_strategy="two_beats",
        )
        return json.dumps(proposal.model_dump(mode="json"), ensure_ascii=False)


class _ContinueReconsideration:
    """Fixed semantic review used only to prove the app-owned gate path."""

    async def review(self, **_kwargs) -> str:
        return "continue"

@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    scenario_turn_id: str
    scenario_family: str
    emotional_gold: bool
    fault: ScenarioFault
    world_id: str
    output_hash: str | None
    event_types: tuple[str, ...]
    terminal_action_states: tuple[str, ...]
    replay_hash: str
    replay_passed: bool
    model_calls: int
    observation_count: int
    trigger_kinds: tuple[str, ...]
    room_view_hash: str
    # These fields distinguish the ordinary chat-only controls from the one
    # durable outcome/NPC/Affect continuation.  They are frozen in the suite
    # manifest so a later runner cannot silently stop running the background
    # consumers while preserving its user-facing output hash.
    restarted_after_seed: bool
    background_work_statuses: tuple[str, ...]
    background_model_calls: int
    verification_errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.verification_errors

    def manifest_row(self) -> dict[str, object]:
        return {
            "scenario_turn_id": self.scenario_turn_id,
            "scenario_family": self.scenario_family,
            "emotional_gold": self.emotional_gold,
            "fault": self.fault,
            "world_id": self.world_id,
            "output_hash": self.output_hash,
            "event_types": self.event_types,
            "terminal_action_states": self.terminal_action_states,
            "replay_hash": self.replay_hash,
            "replay_passed": self.replay_passed,
            "model_calls": self.model_calls,
            "observation_count": self.observation_count,
            "trigger_kinds": self.trigger_kinds,
            "room_view_hash": self.room_view_hash,
            "restarted_after_seed": self.restarted_after_seed,
            "background_work_statuses": self.background_work_statuses,
            "background_model_calls": self.background_model_calls,
            "verification_errors": self.verification_errors,
        }


@dataclass(frozen=True, slots=True)
class ScenarioSuiteResult:
    corpus_version: str
    economy_profile_version: str
    runs: tuple[ScenarioRunResult, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.runs)

    @property
    def manifest_hash(self) -> str:
        payload = {
            "corpus_version": self.corpus_version,
            "economy_profile_version": self.economy_profile_version,
            "runs": [item.manifest_row() for item in self.runs],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def export_manifest(self) -> dict[str, object]:
        return {
            "kind": "world-v2-offline-scenario-run.1",
            "corpus_version": self.corpus_version,
            "economy_profile_version": self.economy_profile_version,
            "runner_limitations": (
                "fixed fake model/provider only; this is not a human or model blind evaluation"
            ),
            "passed": self.passed,
            "manifest_hash": self.manifest_hash,
            "runs": [item.manifest_row() for item in self.runs],
        }


class ScenarioRunner:
    """Run a frozen scenario exclusively through the public v2 app seam."""

    def __init__(self, *, workdir: str | Path) -> None:
        self._workdir = Path(workdir)
        self._workdir.mkdir(parents=True, exist_ok=True)

    async def run_case(self, case: ScenarioCase) -> ScenarioRunResult:
        now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
        world_id = f"world:phase8-scenario:{case.entry.scenario_turn_id}"
        database_path = self._workdir / f"{case.entry.scenario_turn_id}.sqlite"
        if database_path.exists():
            database_path.unlink()
        model = (
            _FixedDelayedExpressionModel(scenario_turn_id=case.entry.scenario_turn_id)
            if case.execution == "seeded_expression_delay"
            else FakeCompanionModel()
        )
        adapter = RoutedChatModelDeliberationAdapter(
            flash_model=model,
            flash_model_id="phase8-fixed-fake-flash",
        )
        transport = _FixedScenarioTransport(received_at=now, fault=case.fault)
        outcome_model = (
            _FixedOutcomeModel(scenario_turn_id=case.entry.scenario_turn_id)
            if case.execution == "seeded_world_outcome_affect"
            else None
        )
        appraisal_model = (
            _FixedNpcAppraisalModel(scenario_turn_id=case.entry.scenario_turn_id)
            if case.execution == "seeded_world_outcome_affect"
            else None
        )
        affect_model = (
            _FixedAffectModel(scenario_turn_id=case.entry.scenario_turn_id)
            if case.execution == "seeded_world_outcome_affect"
            else None
        )

        def build_application():
            return build_sqlite_world_v2_turn_application(
                path=database_path,
                config=WorldV2TurnApplicationConfig(
                    world_id=world_id,
                    companion_actor_ref="agent:companion",
                    reply_target="user:scenario",
                    action_pump_owner="pump:phase8-scenario",
                ),
                identities=SimulatorIdentityResolver(canonical_user_id="scenario"),
                router=_FixedScenarioRouter(),
                main_model=adapter,
                quick_recovery=adapter,
                outcome_model=outcome_model,
                appraisal_model=appraisal_model,
                affect_model=affect_model,
                expression_reconsideration_reviewer=(
                    _ContinueReconsideration()
                    if case.execution == "seeded_expression_delay"
                    else None
                ),
                transport=transport,
                now=now,
            )

        app = build_application()
        restarted_after_seed = False
        background_work_statuses: tuple[str, ...] = ()
        try:
            if case.execution in {"seeded_world_outcome", "seeded_world_outcome_affect"}:
                await self._seed_world_outcome(app=app, case=case, now=now)
            if case.execution == "seeded_world_outcome_affect":
                # The fixture intentionally crashes only after the source
                # observation/trigger were durable.  The restarted app must
                # perform outcome settlement, its NPC appraisal continuation,
                # then Affect before the next visible user turn is compiled.
                app.close()
                restarted_after_seed = True
                app = build_application()
                statuses: list[str] = []
                for _ in range(3):
                    work = await app.drain_background_once()
                    if work is None or getattr(work, "work_status", None) is None:
                        raise ScenarioVerificationError("seeded outcome continuation did not run")
                    statuses.append(work.work_status)
                background_work_statuses = tuple(statuses)
            # A seeded occurrence advances logical time before its follow-up
            # chat.  The inbound observation must not claim to arrive before
            # that committed authority; otherwise an interaction-appraisal
            # lease would correctly reject the backwards clock.
            inbound_observed_at = (
                now + timedelta(minutes=2)
                if case.execution in {"seeded_world_outcome", "seeded_world_outcome_affect"}
                else now
            )
            for index, turn in enumerate(case.turns, start=1):
                inbound = dict(
                    platform="simulator",
                    platform_user_id="scenario",
                    platform_message_id=f"{case.entry.scenario_turn_id}:{turn.step_id}",
                    text=turn.text,
                    # The scripted sequence is causal order, not a claim that
                    # logical time advanced.  Advancing time has to travel
                    # through ``app.tick`` with its clock authority.
                    observed_at=inbound_observed_at,
                    trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:{turn.step_id}",
                    coalescing_metadata={
                        "scenario_family": case.entry.scenario_family,
                        "scenario_step": turn.step_id,
                    },
                )
                await app.inbound(**inbound)
                if case.fault == "duplicate_ingress" and index == 1:
                    await app.inbound(**inbound)
                if case.execution == "seeded_activity_plan" and index == 1:
                    await app.plan_activity(
                        ActivityPlanCommand(
                            command_id=f"command:phase8:{case.entry.scenario_turn_id}:activity",
                            world_id=world_id,
                            source_observation_id=(
                                "observation:simulator:scenario:"
                                f"{case.entry.scenario_turn_id}:{turn.step_id}"
                            ),
                            plan_id=f"plan:phase8:{case.entry.scenario_turn_id}:museum",
                            activity_id=f"activity:phase8:{case.entry.scenario_turn_id}:museum",
                            activity_kind="museum_visit",
                            importance_bp=4800,
                            location_ref="place:phase8:museum",
                            participant_refs=("agent:companion",),
                            scheduled_window=DueWindow(
                                opens_at=now + timedelta(days=1),
                                closes_at=now + timedelta(days=1, hours=4),
                            ),
                        ),
                        logical_time=inbound_observed_at,
                        created_at=inbound_observed_at,
                        trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:activity-plan",
                        causation_id=f"observation:simulator:scenario:{case.entry.scenario_turn_id}:{turn.step_id}",
                        correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}:activity-plan",
                    )
                if case.execution == "seeded_expression_delay" and index == 1:
                    immediate = await app.drain_actions_once()
                    delayed = await app.drain_actions_once()
                    if getattr(immediate, "status", None) != "settled" or getattr(delayed, "status", None) != "not_due":
                        raise ScenarioVerificationError("delayed expression fixture did not schedule an undelivered beat")
                # An interruption deliberately arrives before the old action is
                # dispatched.  Every other scripted turn reaches the same
                # application-owned ActionPump before the next ingress.
                if case.execution not in {"interruption", "seeded_expression_delay"}:
                    await app.drain_actions_once()
            if case.execution == "seeded_expression_delay":
                reconsideration = await app.drain_background_once()
                if getattr(reconsideration, "status", None) != "continued":
                    raise ScenarioVerificationError("delayed expression beat was not explicitly reconsidered")
                due = now + timedelta(minutes=3)
                await app.tick(
                    tick_id=f"{case.entry.scenario_turn_id}:delayed-beat-due",
                    logical_time_from=now,
                    logical_time_to=due,
                    observed_at=due,
                    trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:delayed-beat-due",
                    causation_id=f"scheduler:phase8:{case.entry.scenario_turn_id}:delayed-beat-due",
                    correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}:delayed-beat",
                    reason="phase8_seeded_delayed_expression_due",
                )
                for _ in range(3):
                    await app.drain_actions_once()
            if case.fault == "restart_before_dispatch":
                app.close()
                app = build_application()
            if case.execution == "interruption":
                await app.drain_actions_once()
            evidence = app.export_replay_evidence()
        finally:
            app.close()

        event_types = tuple(item.event.event_type for item in evidence.events)
        projection = evidence.projection
        action_states = tuple(item.state for item in projection.actions)
        observation_count = sum(item == "ObservationRecorded" for item in event_types)
        trigger_kinds = tuple(sorted({item.process_kind for item in projection.trigger_processes}))
        room_view_json = RoomProjectionMaterializer.materialize(projection).model_dump_json()
        replay = ReplayEvaluator().evaluate(evidence=evidence)
        background_model_calls = sum(
            item.calls for item in (outcome_model, appraisal_model, affect_model) if item is not None
        )
        next_context_has_outcome_affect = self._next_context_has_outcome_affect(
            model=model, case=case
        )
        errors = self._verify(
            case=case,
            event_types=event_types,
            action_states=action_states,
            replay_passed=replay.passed,
            model_calls=len(model.calls),
            observation_count=observation_count,
            trigger_kinds=trigger_kinds,
            room_view_json=room_view_json,
            restarted_after_seed=restarted_after_seed,
            background_work_statuses=background_work_statuses,
            background_model_calls=background_model_calls,
            next_context_has_outcome_affect=next_context_has_outcome_affect,
        )
        output_hash = (
            hashlib.sha256(transport.bodies[-1].encode("utf-8")).hexdigest()
            if transport.bodies
            else None
        )
        return ScenarioRunResult(
            scenario_turn_id=case.entry.scenario_turn_id,
            scenario_family=case.entry.scenario_family,
            emotional_gold=case.entry.emotional_gold,
            fault=case.fault,
            world_id=world_id,
            output_hash=output_hash,
            event_types=event_types,
            terminal_action_states=action_states,
            replay_hash=projection.semantic_hash,
            replay_passed=replay.passed,
            model_calls=len(model.calls),
            observation_count=observation_count,
            trigger_kinds=trigger_kinds,
            room_view_hash=hashlib.sha256(room_view_json.encode("utf-8")).hexdigest(),
            restarted_after_seed=restarted_after_seed,
            background_work_statuses=background_work_statuses,
            background_model_calls=background_model_calls,
            verification_errors=errors,
        )

    @staticmethod
    def _next_context_has_outcome_affect(*, model: FakeCompanionModel, case: ScenarioCase) -> bool:
        """Prove the next reply consumed this exact outcome and Affect episode.

        Merely checking that both slices are non-empty would allow an unrelated
        interaction appraisal or settled occurrence to mask a broken causal
        continuation.  The fixture must bind all three source identifiers
        from the seeded life event into the next app-owned capsule.
        """

        if case.execution != "seeded_world_outcome_affect":
            return True
        if not model.calls:
            return False
        try:
            request = json.loads(model.calls[-1][1]["content"])["request"]
            context = json.loads(request["model_content_json"])
            slices = context["slices"]
            world_life = slices["world_life"]["items"]
            affect = slices["affect_episodes"]["items"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return False
        occurrence_id = f"occurrence:phase8:{case.entry.scenario_turn_id}"
        result_id = f"result:phase8:{case.entry.scenario_turn_id}:settled"
        appraisal_change_id = f"change:phase8:{case.entry.scenario_turn_id}:npc-appraisal"
        has_settled_outcome = any(
            item.get("value", {}).get("occurrence_id") == occurrence_id
            and item.get("value", {}).get("result_id") == result_id
            for item in world_life
            if isinstance(item, dict)
        )
        # The compiler assigns the accepted episode a deterministic compiled
        # id, so the stable source identity is the NPC appraisal change that
        # every component must retain rather than the model's provisional id.
        has_causal_affect = any(
            appraisal_ref.get("accepted_change_id") == appraisal_change_id
            for item in affect
            if isinstance(item, dict)
            for component in item.get("value", {}).get("components", ())
            if isinstance(component, dict)
            for appraisal_ref in component.get("appraisal_refs", ())
            if isinstance(appraisal_ref, dict)
        )
        return has_settled_outcome and has_causal_affect

    @staticmethod
    async def _seed_world_outcome(
        *, app, case: ScenarioCase, now: datetime
    ) -> None:
        """Seed one durable, private occurrence through application commands.

        This intentionally stops at the open outcome deliberation trigger.
        Outcome → NPC appraisal → Affect is separately exercised with
        deliberation models by the production recovery suite; this corpus must
        not pretend that a chat-only fake model settled a world outcome.
        """

        world_id = f"world:phase8-scenario:{case.entry.scenario_turn_id}"
        first_tick = now + timedelta(minutes=1)
        second_tick = now + timedelta(minutes=2)
        occurrence_id = f"occurrence:phase8:{case.entry.scenario_turn_id}"
        candidate_ref = f"candidate:phase8:{case.entry.scenario_turn_id}:settled"
        result_id = f"result:phase8:{case.entry.scenario_turn_id}:settled"
        await app.tick(
            tick_id=f"{case.entry.scenario_turn_id}:seed",
            logical_time_from=now,
            logical_time_to=first_tick,
            observed_at=first_tick,
            trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:seed",
            causation_id=f"scheduler:phase8:{case.entry.scenario_turn_id}:seed",
            correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}",
            reason="phase8_seeded_outcome",
        )
        candidate = OutcomeCandidateContent(
            candidate_result_ref=candidate_ref,
            result_id=result_id,
            result_payload_ref=f"payload:phase8:{case.entry.scenario_turn_id}:settled",
            result_payload_hash="sha256:" + "e" * 64,
            privacy_class="private",
            content_ref=f"content:phase8:{case.entry.scenario_turn_id}:settled",
            text="这件小事有了一个可验证的结果，但仍然只属于角色自己的生活。",
        )
        await app.commit_occurrence(
            OccurrenceContentCommitRequest(
                world_id=world_id,
                occurrence=WorldOccurrenceProjection(
                    occurrence_id=occurrence_id,
                    entity_revision=1,
                    trigger_ref=f"trigger:phase8:{case.entry.scenario_turn_id}",
                    participant_refs=("agent:companion",),
                    location_ref="room:scenario-private",
                    time_window=DueWindow(
                        opens_at=first_tick, closes_at=first_tick + timedelta(minutes=10)
                    ),
                    candidate_outcome_refs=(candidate_ref,),
                    visibility="private",
                    status="committed",
                ),
                candidate_contents=(candidate,),
                change_id=f"change:phase8:{case.entry.scenario_turn_id}:occurrence",
                transition_id=f"transition:phase8:{case.entry.scenario_turn_id}:occurrence",
                evidence_refs=(
                    EvidenceRef(
                        ref_id=f"clock:{first_tick.isoformat()}",
                        evidence_type="clock_observation",
                        claim_purpose="current_fact",
                    ),
                ),
                logical_time=first_tick,
                created_at=first_tick,
                actor="system:phase8-scenario",
                source="phase8-scenario",
                trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:occurrence",
                causation_id=f"cause:phase8:{case.entry.scenario_turn_id}:occurrence",
                correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}",
            )
        )
        await app.tick(
            tick_id=f"{case.entry.scenario_turn_id}:activate",
            logical_time_from=first_tick,
            logical_time_to=second_tick,
            observed_at=second_tick,
            trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:activate",
            causation_id=f"scheduler:phase8:{case.entry.scenario_turn_id}:activate",
            correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}",
            reason="phase8_activate_seeded_outcome",
        )
        observation = OutcomeObservation(
            schema_version="world-v2.1",
            observation_id=f"observation:phase8:{case.entry.scenario_turn_id}:settled",
            world_id=world_id,
            logical_time=second_tick,
            created_at=second_tick,
            trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:outcome",
            causation_id=f"sensor:phase8:{case.entry.scenario_turn_id}",
            correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}",
            occurrence_id=occurrence_id,
            source_kind="committed_world_event",
            source_refs=(f"event:trigger:clock:{case.entry.scenario_turn_id}:activate",),
            observed_payload_ref=f"sensor:phase8:{case.entry.scenario_turn_id}:settled",
            observed_payload_hash="a" * 64,
            observed_at=second_tick,
            confidence_bp=9200,
        )
        recorded = await app.record_outcome_observation(observation)
        if await app.record_outcome_observation(observation) != recorded:
            raise ScenarioVerificationError("outcome observation ingress was not effect-once")

    async def run_frozen_suite(self, *, limit: int | None = None) -> ScenarioSuiteResult:
        cases = verify_frozen_scenario_corpus()
        if limit is not None:
            if limit < 1:
                raise ValueError("scenario limit must be positive")
            cases = cases[:limit]
        runs_list: list[ScenarioRunResult] = []
        for case in cases:
            runs_list.append(await self.run_case(case))
        runs = tuple(runs_list)
        suite = ScenarioSuiteResult(
            corpus_version=SCENARIO_CORPUS_VERSION,
            economy_profile_version=TEST_ECONOMY_PROFILE_VERSION,
            runs=runs,
        )
        if limit is None and suite.manifest_hash != FROZEN_OFFLINE_SUITE_MANIFEST_HASH:
            raise ScenarioVerificationError(
                "offline scenario manifest drifted; establish a new versioned mechanism baseline"
            )
        return suite

    @staticmethod
    def _verify(
        *,
        case: ScenarioCase,
        event_types: tuple[str, ...],
        action_states: tuple[str, ...],
        replay_passed: bool,
        model_calls: int,
        observation_count: int,
        trigger_kinds: tuple[str, ...],
        room_view_json: str,
        restarted_after_seed: bool,
        background_work_statuses: tuple[str, ...],
        background_model_calls: int,
        next_context_has_outcome_affect: bool,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        required = {
            "ObservationRecorded",
            "ActionAuthorized",
            "ExternalObservationRecorded",
            "ExternalObservationProcessed",
        }
        required.update(case.required_event_types)
        missing = sorted(required.difference(event_types))
        if missing:
            errors.append("missing_required_events:" + ",".join(missing))
        expected_terminal = {
            "provider_failed": "failed",
            "provider_unknown": "unknown",
        }.get(case.fault, "delivered")
        if case.execution == "interruption":
            # The fixture's first action belongs to the pre-interruption
            # ingress and the second to the interrupting ingress.  A mere
            # unordered "one delivered, one authorized" check would accept
            # the exact regression this scenario is meant to catch: sending
            # stale content, then stranding the new response.
            expected_interruption_states = ("authorized", "delivered")
            if action_states != expected_interruption_states:
                errors.append("interruption_old_action_was_not_gated")
        elif case.execution == "seeded_expression_delay":
            # First ingress creates two materialized beats; the interruption
            # creates one fresh reply.  Every one must settle, proving that
            # the old delayed beat was gated/reviewed rather than lost or
            # emitted before the Logical Clock made it due.
            if action_states != ("delivered", "delivered", "delivered"):
                errors.append("delayed_expression_lifecycle_incomplete")
        elif action_states != (expected_terminal,) * len(case.turns):
            errors.append("terminal_action_state_mismatch")
        required_terminal_event = {
            "provider_failed": "ActionFailed",
            "provider_unknown": "ActionUnknown",
        }.get(case.fault)
        if required_terminal_event is not None and required_terminal_event not in event_types:
            errors.append("fault_terminal_event_missing")
        if not replay_passed:
            errors.append("replay_evaluator_failed")
        # test-economy-v1: regular chat has exactly one main fake call; this
        # runner deliberately does not configure background audit models.
        if model_calls != len(case.turns):
            errors.append("test_economy_model_call_budget_exceeded")
        expected_observations = len(case.turns)
        if observation_count != expected_observations:
            errors.append("ingress_idempotency_failed")
        forbidden = sorted(set(case.forbidden_event_types).intersection(event_types))
        if forbidden:
            errors.append("forbidden_events_present:" + ",".join(forbidden))
        missing_triggers = sorted(set(case.required_trigger_kinds).difference(trigger_kinds))
        if missing_triggers:
            errors.append("missing_required_triggers:" + ",".join(missing_triggers))
        leaked_values = tuple(
            value for value in case.forbidden_room_view_values if value in room_view_json
        )
        if leaked_values:
            errors.append("room_projection_redaction_failed:" + ",".join(leaked_values))
        if case.execution == "seeded_world_outcome_affect":
            if not restarted_after_seed:
                errors.append("outcome_chain_did_not_restart_after_seed")
            if background_work_statuses != ("accepted", "accepted", "accepted"):
                errors.append("outcome_npc_affect_chain_incomplete")
            if background_model_calls != 3:
                errors.append("outcome_npc_affect_model_call_budget_exceeded")
            if not next_context_has_outcome_affect:
                errors.append("next_reply_did_not_consume_outcome_affect_context")
        return tuple(errors)


def run_frozen_suite_sync(*, workdir: str | Path, limit: int | None = None) -> ScenarioSuiteResult:
    """CLI-safe synchronous wrapper; never performs network/model calls."""

    return asyncio.run(ScenarioRunner(workdir=workdir).run_frozen_suite(limit=limit))


__all__ = [
    "ScenarioRunResult",
    "ScenarioRunner",
    "ScenarioSuiteResult",
    "ScenarioVerificationError",
    "FROZEN_OFFLINE_SUITE_MANIFEST_HASH",
    "run_frozen_suite_sync",
]
