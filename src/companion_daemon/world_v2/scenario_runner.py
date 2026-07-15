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

from .chat_model_deliberation_adapter import RoutedChatModelDeliberationAdapter
from .deliberation import ModelRoute, RouteRequest
from .platform_action_executor import PlatformDispatchReceipt, PlatformDispatchRequest
from .production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from .occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OutcomeCandidateContent,
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
FROZEN_OFFLINE_SUITE_MANIFEST_HASH = "da4780e374ddbcf36eef7f4fa98d538098fc93b06f893f15cbdacd60c091f969"


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
        model = FakeCompanionModel()
        adapter = RoutedChatModelDeliberationAdapter(
            flash_model=model,
            flash_model_id="phase8-fixed-fake-flash",
        )
        transport = _FixedScenarioTransport(received_at=now, fault=case.fault)

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
                transport=transport,
                now=now,
            )

        app = build_application()
        try:
            if case.execution == "seeded_world_outcome":
                await self._seed_world_outcome(app=app, case=case, now=now)
            for index, turn in enumerate(case.turns, start=1):
                inbound = dict(
                    platform="simulator",
                    platform_user_id="scenario",
                    platform_message_id=f"{case.entry.scenario_turn_id}:{turn.step_id}",
                    text=turn.text,
                    # The scripted sequence is causal order, not a claim that
                    # logical time advanced.  Advancing time has to travel
                    # through ``app.tick`` with its clock authority.
                    observed_at=now,
                    trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:{turn.step_id}",
                    coalescing_metadata={
                        "scenario_family": case.entry.scenario_family,
                        "scenario_step": turn.step_id,
                    },
                )
                await app.inbound(**inbound)
                if case.fault == "duplicate_ingress" and index == 1:
                    await app.inbound(**inbound)
                # An interruption deliberately arrives before the old action is
                # dispatched.  Every other scripted turn reaches the same
                # application-owned ActionPump before the next ingress.
                if case.execution != "interruption":
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
        errors = self._verify(
            case=case,
            event_types=event_types,
            action_states=action_states,
            replay_passed=replay.passed,
            model_calls=len(model.calls),
            observation_count=observation_count,
            trigger_kinds=trigger_kinds,
            room_view_json=room_view_json,
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
            verification_errors=errors,
        )

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
