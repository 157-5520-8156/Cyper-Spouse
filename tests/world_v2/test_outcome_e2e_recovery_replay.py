"""Production outcome vertical: durable restart, causal follow-ups and replay.

This fixture deliberately uses the platform-neutral production composition
root.  It proves that a sidecar-backed occurrence can survive a process stop
between observation and background work, then flows through outcome settlement
and the next visible Character-Interior Context without relying on QQ.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from world_v2_application import (
    build_sqlite_world_v2_test_application,
    compose_fixture_character_interior,
    compose_fixture_character_purpose,
)

from companion_daemon.world_v2.character_interior.inbound_wire import _ExpressionDraftWire
from companion_daemon.world_v2.deliberation import ModelRoute, RouteRequest
from companion_daemon.world_v2.expression_draft import (
    PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from companion_daemon.world_v2.occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OutcomeCandidateContent,
)
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
)
from companion_daemon.world_v2.replay_evaluator import ReplayEvaluator
from companion_daemon.world_v2.schemas import (
    ClockObservation,
    DueWindow,
    EvidenceRef,
    OutcomeObservation,
    WorldOccurrenceProjection,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.world_turn_runtime import InboundTurn

WORLD_ID = "world:outcome-e2e-recovery"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
TICK_ONE = NOW + timedelta(minutes=1)
TICK_TWO = NOW + timedelta(minutes=2)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert (platform, platform_user_id) == ("test", "user.1")
        return "user:user.1", "user:user.1"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _DeliveredTransport:
    provider = "platform:test"

    async def send(self, request):  # type: ignore[no-untyped-def]
        return PlatformDispatchReceipt(
            provider_receipt_id="receipt:outcome-e2e",
            provider_ref="message:outcome-e2e",
            status="delivered",
            received_at=TICK_TWO,
            raw_payload_hash="sha256:" + "d" * 64,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _CapturingReplyChat:
    model = "test-reply"

    def __init__(self) -> None:
        self.requests: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        assert temperature in {0.25, 0.7}
        self.requests.append(messages)
        return json.dumps(
            {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我记得刚落定的泡茶结果，想直接接住这个问题。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "beats": [
                    {"modality": "text", "text": "嗯，我把刚才那件事记着。你接着说。"}
                ],
                "stance": "acknowledge_briefly",
                "brief_rationale": "Reply without asserting any unobserved event.",
                "confidence": 7000,
                "world_claims": [],
            },
            ensure_ascii=False,
        )


class _OutcomeModel:
    model = "test-outcome-character"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        assert temperature == 0.8
        self.calls += 1
        request = json.loads(messages[-1]["content"])
        source_ref = request["capability_manifest"]["source_refs"][0]
        return json.dumps(
            {
                "status": "decision",
                "summary": "The observed tea result now feels settled.",
                "attended_source_refs": [source_ref],
                "decision": {
                    "source_refs": [source_ref],
                    "payload": {
                        "selected_token": "candidate:e2e:tea-ready",
                        "character_life_direction": None,
                    },
                },
                "recall_query": None,
                "proposals": [],
            }
        )


def _config() -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id=WORLD_ID,
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:e2e",
        character_memory_enabled=False,
    )


def _clock(*, tick_id: str, start: datetime, end: datetime) -> ClockObservation:
    return ClockObservation(
        schema_version="world-v2.1",
        tick_id=tick_id,
        world_id=WORLD_ID,
        logical_time=start,
        created_at=end,
        trace_id=f"trace:{tick_id}",
        causation_id=f"scheduler:{tick_id}",
        correlation_id="correlation:e2e",
        logical_time_from=start,
        logical_time_to=end,
        reason="test_clock",
    )


def _occurrence_request() -> OccurrenceContentCommitRequest:
    candidate = OutcomeCandidateContent(
        candidate_result_ref="candidate:e2e:tea-ready",
        result_id="result:e2e:tea-ready",
        result_payload_ref="payload:e2e:tea-ready",
        result_payload_hash="sha256:" + "e" * 64,
        privacy_class="private",
        content_ref="content:e2e:outcome:tea-ready",
        text="水开后，茶叶慢慢舒展开来，杯子被放在窗边晾着。",
    )
    return OccurrenceContentCommitRequest(
        world_id=WORLD_ID,
        occurrence=WorldOccurrenceProjection(
            occurrence_id="occurrence:e2e:tea",
            entity_revision=1,
            trigger_ref="trigger:e2e:tea",
            participant_refs=("agent:companion",),
            location_ref="room:kitchen",
            time_window=DueWindow(opens_at=TICK_ONE, closes_at=TICK_ONE + timedelta(minutes=10)),
            candidate_outcome_refs=(candidate.candidate_result_ref,),
            visibility="private",
            status="committed",
        ),
        candidate_contents=(candidate,),
        change_id="change:e2e:occurrence",
        transition_id="transition:e2e:occurrence",
        evidence_refs=(
            EvidenceRef(
                ref_id=f"clock:{TICK_ONE.isoformat()}",
                evidence_type="clock_observation",
                claim_purpose="current_fact",
            ),
        ),
        logical_time=TICK_ONE,
        created_at=TICK_ONE,
        actor="system:e2e-occurrence",
        source="test",
        trace_id="trace:e2e-occurrence",
        causation_id="cause:e2e-occurrence",
        correlation_id="correlation:e2e",
    )


@pytest.mark.asyncio
async def test_outcome_restart_recovers_world_result_and_next_turn_context(tmp_path: Path) -> None:
    """Sidecar candidate → outcome → unified next-turn Context.

    The first app deliberately exits after durable observation/trigger creation.
    The second process must reuse those records, make exactly one call per
    outcome model, and retain a replay-stable causal chain.
    """

    path = tmp_path / "outcome-e2e.sqlite"
    reply_chat = _CapturingReplyChat()
    outcome_model = _OutcomeModel()
    reply_model = _ExpressionDraftWire(
        model=reply_chat,
        expression_capabilities=PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
    )

    first = build_sqlite_world_v2_test_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=reply_model,
            purpose_faculties=(
                compose_fixture_character_purpose(
                    purpose="outcome_selection",
                    provider=outcome_model,
                ),
            ),
        ),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        await first.advance(_clock(tick_id="e2e:seed", start=NOW, end=TICK_ONE))
        await first.commit_occurrence(_occurrence_request())
        await first.advance(_clock(tick_id="e2e:activate", start=TICK_ONE, end=TICK_TWO))
        command = OutcomeObservation(
            schema_version="world-v2.1",
            observation_id="observation:e2e:tea-ready",
            world_id=WORLD_ID,
            logical_time=TICK_TWO,
            created_at=TICK_TWO,
            trace_id="trace:e2e:outcome-observation",
            causation_id="sensor:e2e:tea",
            correlation_id="correlation:e2e",
            occurrence_id="occurrence:e2e:tea",
            source_kind="committed_world_event",
            source_refs=("event:trigger:clock:e2e:activate",),
            observed_payload_ref="sensor-payload:e2e:tea-ready",
            observed_payload_hash="a" * 64,
            observed_at=TICK_TWO,
            confidence_bp=9200,
        )
        recorded = await first.record_outcome_observation(command)
        # Replayed ingress joins the existing trigger rather than opening a
        # second observation or outcome worker opportunity.
        assert await first.record_outcome_observation(command) == recorded
    finally:
        first.close()

    # Simulate a cold restart after the durable trigger, before any model work.
    restarted = build_sqlite_world_v2_test_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=reply_model,
            purpose_faculties=(
                compose_fixture_character_purpose(
                    purpose="outcome_selection",
                    provider=outcome_model,
                ),
            ),
        ),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await restarted.drain_background_once()
        visible = await restarted.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:e2e:next-turn",
                text="刚才那杯茶后来怎么样了？",
                observed_at=TICK_TWO,
                trace_id="trace:e2e:next-turn",
            )
        )
    finally:
        restarted.close()

    assert outcome is not None and outcome.work_status == "accepted"
    assert outcome_model.calls == 1
    assert visible.status == "action_authorized"
    provider_packet = json.loads(reply_chat.requests[-1][1]["content"])
    next_request = provider_packet["request"]
    context = json.loads(next_request["model_content_json"])
    assert "slices" not in context
    recent_experiences = provider_packet["inner_life_snapshot"]["materials"][
        "recent_self_experiences"
    ]["items"]
    settled = next(
        item for item in recent_experiences if item.get("occurrence_id") == "occurrence:e2e:tea"
    )
    assert settled["result_id"] == "result:e2e:tea-ready"

    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    try:
        evidence = ledger.export_replay_evidence()
        assert evidence.projection.semantic_hash == evidence.replay.semantic_hash
        assert ReplayEvaluator().evaluate(evidence=evidence).passed
        occurrence = next(
            item
            for item in evidence.projection.world_occurrences
            if item.occurrence_id == "occurrence:e2e:tea"
        )
        assert occurrence.status == "settled"
        kinds = [item.event.event_type for item in evidence.events]
        assert kinds.count("OutcomeObservationRecorded") == 1
        assert kinds.count("WorldOccurrenceSettled") == 1
    finally:
        ledger.close()
