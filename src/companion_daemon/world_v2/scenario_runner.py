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
from datetime import UTC, datetime
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
from .replay_evaluator import ReplayEvaluator
from .scenario_corpus import (
    SCENARIO_CORPUS_VERSION,
    TEST_ECONOMY_PROFILE_VERSION,
    ScenarioCase,
    verify_frozen_scenario_corpus,
)
from .simulator_adapters import SimulatorIdentityResolver


class ScenarioVerificationError(AssertionError):
    """A frozen scenario did not exercise its declared authority predicates."""


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

    def __init__(self, *, received_at: datetime, fault: str) -> None:
        self._received_at = received_at
        self._fault = fault
        self._receipts: dict[str, PlatformDispatchReceipt] = {}
        self.bodies: list[str] = []

    async def send(self, request: PlatformDispatchRequest) -> PlatformDispatchReceipt:
        existing = self._receipts.get(request.idempotency_key)
        if existing is not None:
            return existing
        status: Literal["delivered", "failed"] = (
            "failed" if self._fault == "provider_failed" else "delivered"
        )
        identity = hashlib.sha256(request.fingerprint.encode("utf-8")).hexdigest()
        receipt = PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:scenario:{identity}",
            provider_ref=f"message:scenario:{identity}",
            status=status,
            error_class="simulated_provider_timeout" if status == "failed" else None,
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
    fault: str
    world_id: str
    output_hash: str | None
    event_types: tuple[str, ...]
    terminal_action_states: tuple[str, ...]
    replay_hash: str
    replay_passed: bool
    model_calls: int
    duplicate_observation_count: int
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
            "duplicate_observation_count": self.duplicate_observation_count,
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
        app = build_sqlite_world_v2_turn_application(
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
        try:
            inbound = dict(
                platform="simulator",
                platform_user_id="scenario",
                platform_message_id=case.entry.scenario_turn_id,
                text=case.user_text,
                observed_at=now,
                trace_id=f"trace:phase8:{case.entry.scenario_turn_id}",
                coalescing_metadata={"scenario_family": case.entry.scenario_family},
            )
            await app.inbound(**inbound)
            if case.fault == "duplicate_ingress":
                await app.inbound(**inbound)
            await app.drain_actions_once()
            evidence = app.export_replay_evidence()
        finally:
            app.close()

        event_types = tuple(item.event.event_type for item in evidence.events)
        projection = evidence.projection
        action_states = tuple(item.state for item in projection.actions)
        observation_count = sum(item == "ObservationRecorded" for item in event_types)
        replay = ReplayEvaluator().evaluate(evidence=evidence)
        errors = self._verify(
            case=case,
            event_types=event_types,
            action_states=action_states,
            replay_passed=replay.passed,
            model_calls=len(model.calls),
            observation_count=observation_count,
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
            duplicate_observation_count=observation_count,
            verification_errors=errors,
        )

    async def run_frozen_suite(self, *, limit: int | None = None) -> ScenarioSuiteResult:
        cases = verify_frozen_scenario_corpus()
        if limit is not None:
            if limit < 1:
                raise ValueError("scenario limit must be positive")
            cases = cases[:limit]
        runs = tuple([await self.run_case(case) for case in cases])
        return ScenarioSuiteResult(
            corpus_version=SCENARIO_CORPUS_VERSION,
            economy_profile_version=TEST_ECONOMY_PROFILE_VERSION,
            runs=runs,
        )

    @staticmethod
    def _verify(
        *,
        case: ScenarioCase,
        event_types: tuple[str, ...],
        action_states: tuple[str, ...],
        replay_passed: bool,
        model_calls: int,
        observation_count: int,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        required = {
            "ObservationRecorded",
            "ActionAuthorized",
            "ExternalObservationRecorded",
            "ExternalObservationProcessed",
        }
        missing = sorted(required.difference(event_types))
        if missing:
            errors.append("missing_required_events:" + ",".join(missing))
        expected_terminal = "failed" if case.fault == "provider_failed" else "delivered"
        if action_states != (expected_terminal,):
            errors.append("terminal_action_state_mismatch")
        if not replay_passed:
            errors.append("replay_evaluator_failed")
        # test-economy-v1: regular chat has exactly one main fake call; this
        # runner deliberately does not configure background audit models.
        if model_calls != 1:
            errors.append("test_economy_model_call_budget_exceeded")
        expected_observations = 1
        if observation_count != expected_observations:
            errors.append("ingress_idempotency_failed")
        return tuple(errors)


def run_frozen_suite_sync(*, workdir: str | Path, limit: int | None = None) -> ScenarioSuiteResult:
    """CLI-safe synchronous wrapper; never performs network/model calls."""

    return asyncio.run(ScenarioRunner(workdir=workdir).run_frozen_suite(limit=limit))


__all__ = [
    "ScenarioRunResult",
    "ScenarioRunner",
    "ScenarioSuiteResult",
    "ScenarioVerificationError",
    "run_frozen_suite_sync",
]
