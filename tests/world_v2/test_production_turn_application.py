from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelOutput,
    ModelRoute,
    RouteRequest,
)
from companion_daemon.world_v2.chat_model_deliberation_adapter import ChatModelDeliberationAdapter
from companion_daemon.world_v2.appraisal_chat_model_adapter import AppraisalDraftDeliberationAdapter
from companion_daemon.world_v2.affect_chat_model_adapter import AffectDraftDeliberationAdapter
from companion_daemon.world_v2.memory_retrieval import MemoryRetrievalCompiler
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.activity_plan_runtime import (
    ActivityPlanCommand,
    ActivityPlanTransitionCommand,
)
from companion_daemon.world_v2.image_evidence_contract import ImageEvidenceV1
from companion_daemon.world_v2.image_evidence_runtime import ImageEvidenceDeclarationCommand
from companion_daemon.world_v2.event_ecology_media import EcologyPolicy
from companion_daemon.world_v2.life_ecology_runtime import LifeEcologyRunResult
from companion_daemon.world_v2.production_turn_application import LifeEcologyComposition
from companion_daemon.world_v2.schemas import ClockObservation, ProjectionCursor
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.world_turn_runtime import InboundTurn


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert (platform, platform_user_id) == ("test", "user.1")
        return "user:user.1", "user:user.1"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _InvalidModel:
    async def propose(self, _request: ModelInput) -> ModelOutput:
        return ModelOutput(model_id="test-main", model_version="test.1", raw_proposal={})


class _InvalidQuick:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        return ModelOutput(model_id="test-quick", model_version="test.1", raw_proposal={})


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("invalid proposal must not create an external dispatch")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _DraftChatModel:
    model = "test-flash"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "response_text": "嗯，我刚刚有点飘走了。你继续说，我在听。",
                "stance": "acknowledge_briefly",
                "brief_rationale": "Own the missed connection without adding a world claim.",
                "confidence": 7200,
            },
            ensure_ascii=False,
        )


class _NoOpMediaSelectionModel:
    model = "test-media-selection"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.2
        return '{"decision":"no_op"}'


class _CapturingDraftChatModel(_DraftChatModel):
    """Keep the exact model request so a production turn can assert Context."""

    def __init__(self) -> None:
        self.requests: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        self.requests.append(messages)
        return await super().complete(messages, temperature=temperature)


class _DeliveredTransport:
    provider = "platform:test"

    def __init__(self) -> None:
        self.bodies: list[str] = []

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.bodies.append(request.body)
        return PlatformDispatchReceipt(
            provider_receipt_id="receipt:production-application:1",
            provider_ref="message:production-application:1",
            status="delivered",
            received_at=NOW,
            raw_payload_hash="sha256:" + "a" * 64,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _NoChangeAppraisalChat:
    model = "test-appraiser"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "appraise": False,
                "brief_rationale": "The ordinary message does not warrant a durable relational interpretation.",
                "behavior_tendency": "observe",
                "stance": "wait",
                "display_strategy": "withhold",
                "confidence": 3000,
            }
        )


class _AppraisingChat:
    model = "test-appraiser"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "appraise": True,
                "brief_rationale": "The user may be disappointed, but the interpretation remains fallible.",
                "behavior_tendency": "hold_space",
                "stance": "attend",
                "display_strategy": "withhold",
                "confidence": 7600,
                "meanings": [{"meaning": "disappointment", "confidence": 7600}],
                "attribution": "user",
                "severity": 6000,
            }
        )


class _OpenAffectChat:
    model = "test-affect"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "affect": "open",
                "brief_rationale": "The accepted appraisal warrants a bounded residual hurt episode.",
                "behavior_tendency": "hold_space",
                "stance": "care_despite_hurt",
                "display_strategy": "partial_disclosure",
                "confidence": 7200,
                "components": [{"dimension": "hurt", "intensity_bp": 4200}],
            }
        )


class _FactChat:
    model = "test-fact"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.1
        return json.dumps(
            {
                "retain": True,
                "predicate_code": "preference.likes",
                "value": "乌龙茶",
                "privacy_class": "personal",
                "confidence": 8600,
                "rationale": "The user stated an enduring preference.",
            },
            ensure_ascii=False,
        )


class _MemoryChat:
    model = "test-memory"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.15
        return json.dumps(
            {
                "retain": True,
                "cue_kind": "future_utility",
                "retention_rationales": ["future_utility"],
                "salience": {
                    "autobiographical_relevance_bp": 6200,
                    "relationship_relevance_bp": 1800,
                    "emotional_residue_bp": 0,
                    "unfinished_business_bp": 0,
                    "recurrence_bp": 1200,
                    "novelty_bp": 2800,
                    "future_utility_bp": 7600,
                    "world_continuity_bp": 1000,
                },
            },
            ensure_ascii=False,
        )


def _config() -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:production-turn-application",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:production-turn-application",
    )


def _life_ecology_config() -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:life-ecology-production",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:life-ecology-production",
        life_ecology=LifeEcologyComposition.production_v1(),
    )


@pytest.mark.asyncio
async def test_production_life_ecology_profile_claims_one_clock_wake_without_writing_life_facts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "life-ecology.sqlite"
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_life_ecology_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        now=NOW,
    )
    try:
        await app.tick(
            tick_id="life-ecology:1",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:life-ecology",
            causation_id="scheduler:life-ecology",
            correlation_id="correlation:life-ecology",
            reason="test",
        )
        first = await app.advance_life_ecology_once(
            wake_event_ref="event:trigger:clock:life-ecology:1",
            trace_id="trace:life-ecology",
            correlation_id="correlation:life-ecology",
        )
        second = await app.advance_life_ecology_once(
            wake_event_ref="event:trigger:clock:life-ecology:1",
            trace_id="trace:life-ecology",
            correlation_id="correlation:life-ecology",
        )
        assert isinstance(first, LifeEcologyRunResult)
        assert first.status == "joined_existing"
        assert first.reason_code == "life_ecology.run_completed"
        assert second.status == "joined_existing"
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_media_selection_is_explicitly_proposal_only_and_noops_without_candidates(
    tmp_path: Path,
) -> None:
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "media-selection.sqlite",
        config=_life_ecology_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        media_selection_model=_NoOpMediaSelectionModel(),
        now=NOW,
    )
    try:
        selected_at = NOW + timedelta(minutes=1)
        await app.tick(
            tick_id="media-selection:1",
            logical_time_from=NOW,
            logical_time_to=selected_at,
            observed_at=selected_at,
            trace_id="trace:media-selection:clock",
            causation_id="scheduler:media-selection",
            correlation_id="correlation:media-selection",
            reason="test",
        )
        result = await app.drain_media_selection_once(
            logical_time=selected_at,
            trace_id="trace:media-selection",
            correlation_id="correlation:media-selection",
        )
        assert result is not None
        assert result.status == "no_op"
        assert result.reason_code == "media_selection.no_available_candidates"
        expiry = await app.expire_media_candidates_once(
            logical_time=selected_at,
            trace_id="trace:media-expiry",
            correlation_id="correlation:media-selection",
        )
        assert expiry.status == "idle"
        assert app._ledger.project().proposal_ids == ()  # type: ignore[attr-defined]
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_application_bootstraps_sqlite_once_and_exposes_only_turn_operations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "world-v2.sqlite"
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        now=NOW,
    )
    try:
        assert await app.drain_background_once() is None
        assert (await app.drain_media_planning_once()).status == "idle"
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message.1",
                text="今天有点累。",
                observed_at=NOW,
                trace_id="trace:production-turn-application",
            )
        )
        assert outcome.status == "observed_only"
        assert await app.drain_actions_once() is not None
    finally:
        app.close()

    # Rebuilding must reuse the same ledger and not seed a second world or
    # budget account.  The host does not need (and is not given) ledger writes.
    rebuilt = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        now=NOW,
    )
    rebuilt.close()
    ledger = SQLiteWorldLedger(path=path, world_id=_config().world_id)
    try:
        evidence = ledger.export_replay_evidence()
        event_types = [item.event.event_type for item in evidence.events]
        assert event_types.count("WorldStarted") == 1
        assert event_types.count("BudgetAccountConfigured") == 1
        assert ledger.project().budget_accounts[0].account_id == "account:world-v2:chat"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_production_application_advances_clock_without_exposing_ledger_writes(
    tmp_path: Path,
) -> None:
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-clock.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        now=NOW,
    )
    try:
        outcome = await app.advance(
            ClockObservation(
                schema_version="world-v2.1",
                tick_id="clock:production-application:1",
                world_id=_config().world_id,
                logical_time=NOW,
                created_at=NOW,
                trace_id="trace:production-clock",
                causation_id="cause:production-clock",
                correlation_id="correlation:production-clock",
                logical_time_from=NOW,
                logical_time_to=NOW.replace(minute=1),
                reason="scheduler_tick",
            )
        )
    finally:
        app.close()

    assert outcome.status == "observed_only"


@pytest.mark.asyncio
async def test_production_ecology_runs_only_after_a_durable_life_wake_and_only_opens_preview_opportunities(
    tmp_path: Path,
) -> None:
    config = WorldV2TurnApplicationConfig(
        world_id="world:production-ecology",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:production-ecology",
        event_ecology_policy=EcologyPolicy(
            max_candidates_per_drain=1,
            direct_preview_compatibility=True,
        ),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-ecology.sqlite",
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        now=NOW,
    )
    observation_id = "observation:test:user.1:message:ecology"
    try:
        # Normal chat is not an ecology wake, even with an enabled policy.
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="message:ecology",
            text="我想晚点去公园散步。", observed_at=NOW, trace_id="trace:ecology:inbound",
        ))
        plan = await app.plan_activity(
            ActivityPlanCommand(
                command_id="command:ecology:plan", world_id=config.world_id,
                source_observation_id=observation_id, plan_id="plan:ecology",
                activity_id="activity:ecology", activity_kind="walk", importance_bp=4_000,
                location_ref="location:park", participant_refs=("agent:companion",),
                privacy_class="shareable",
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:ecology:plan",
            causation_id="cause:ecology:plan", correlation_id="correlation:ecology",
        )
        started = await app.transition_activity(
            ActivityPlanTransitionCommand(
                command_id="command:ecology:start", world_id=config.world_id,
                source_observation_id=observation_id, plan_id="plan:ecology", operation="start",
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:ecology:start",
            causation_id=plan.event_ids[-1], correlation_id="correlation:ecology",
        )
        result = await app.drain_media_ecology_once(
            wake_event_ref=started.event_ids[-1], logical_time=NOW,
            trace_id="trace:ecology:worker", correlation_id="correlation:ecology",
        )
        # A repeat joins the immutable candidate; no media planning Action is
        # created by this worker and no image can be sent from this seam.
        replay = await app.drain_media_ecology_once(
            wake_event_ref=started.event_ids[-1], logical_time=NOW,
            trace_id="trace:ecology:worker", correlation_id="correlation:ecology",
        )
    finally:
        app.close()

    # Even an explicit legacy compatibility switch cannot turn a bare life
    # event into a picture candidate.  Production-capable ecology waits for a
    # separately accepted, source-bound visual declaration.
    assert result is not None and result.status == "idle"
    assert replay is not None and replay.status == "idle"
    ledger = SQLiteWorldLedger(path=tmp_path / "world-v2-ecology.sqlite", world_id=config.world_id)
    try:
        projection = ledger.project()
        assert len(projection.photo_candidates) == 0
        assert len(projection.media_opportunities) == 0
        assert not any(action.kind == "media_planning" for action in projection.actions)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_production_life_event_declaration_opens_one_source_bound_candidate(
    tmp_path: Path,
) -> None:
    config = WorldV2TurnApplicationConfig(
        world_id="world:production-declared-ecology",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:production-declared-ecology",
        event_ecology_policy=EcologyPolicy(max_candidates_per_drain=1),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-declared-ecology.sqlite",
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        now=NOW,
    )
    try:
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="message:declared-ecology",
            text="我晚点想去公园散散步。", observed_at=NOW, trace_id="trace:declared-ecology:inbound",
        ))
        plan = await app.plan_activity(
            ActivityPlanCommand(
                command_id="command:declared-ecology:plan", world_id=config.world_id,
                source_observation_id="observation:test:user.1:message:declared-ecology",
                plan_id="plan:declared-ecology", activity_id="activity:declared-ecology",
                activity_kind="walk", importance_bp=4_000, location_ref="location:park",
                participant_refs=("agent:companion",), privacy_class="shareable",
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:declared-ecology:plan",
            causation_id="cause:declared-ecology:plan", correlation_id="correlation:declared-ecology",
        )
        started = await app.transition_activity(
            ActivityPlanTransitionCommand(
                command_id="command:declared-ecology:start", world_id=config.world_id,
                source_observation_id="observation:test:user.1:message:declared-ecology",
                plan_id="plan:declared-ecology", operation="start",
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:declared-ecology:start",
            causation_id=plan.event_ids[-1], correlation_id="correlation:declared-ecology",
        )
        declaration = await app.declare_image_evidence(
            ImageEvidenceDeclarationCommand(
                command_id="command:declared-ecology:evidence",
                source_event_ref=started.event_ids[-1],
                image_evidence=ImageEvidenceV1(
                    visibility="shareable",
                    activity={
                        "evidence_visibility": "shareable", "id": "activity:declared-ecology",
                        "kind": "walk", "description": "傍晚在公园散步", "phase": "active",
                    },
                ),
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:declared-ecology:evidence",
            correlation_id="correlation:declared-ecology",
        )
        result = await app.drain_media_ecology_once(
            wake_event_ref=declaration.event_ids[-1], logical_time=NOW,
            trace_id="trace:declared-ecology:worker", correlation_id="correlation:declared-ecology",
        )
    finally:
        app.close()

    assert result is not None and result.status == "created"
    assert len(result.candidate_ids) == 1


@pytest.mark.asyncio
async def test_production_application_materializes_a_chat_draft_and_settles_one_platform_reply(
    tmp_path: Path,
) -> None:
    transport = _DeliveredTransport()
    model = ChatModelDeliberationAdapter(model=_DraftChatModel())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-delivery.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=model,
        quick_recovery=model,
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:delivery",
                text="你刚刚没接住我。",
                observed_at=NOW,
                trace_id="trace:production-delivery",
            )
        )
        delivery = await app.drain_actions_once()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert delivery is not None and delivery.status == "settled"
    assert transport.bodies == ["嗯，我刚刚有点飘走了。你继续说，我在听。"]


@pytest.mark.asyncio
async def test_production_application_drains_appraisal_after_the_visible_reply_lane(tmp_path: Path) -> None:
    transport = _DeliveredTransport()
    reply_model = ChatModelDeliberationAdapter(model=_DraftChatModel())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-background-appraisal.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=_NoChangeAppraisalChat()),
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:background-appraisal",
                text="今天就是有点累。",
                observed_at=NOW,
                trace_id="trace:production-background-appraisal",
            )
        )
        background = await app.drain_background_once()
        await app.drain_actions_once()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert background is not None
    assert background.status == "processed"
    assert background.work_status == "no_change"


@pytest.mark.asyncio
async def test_production_application_accepts_a_fact_outside_the_visible_reply_lane(
    tmp_path: Path,
) -> None:
    path = tmp_path / "world-v2-background-fact.sqlite"
    reply_model = ChatModelDeliberationAdapter(model=_DraftChatModel())
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        fact_model=_FactChat(),
        memory_model=_MemoryChat(),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:background-fact",
                text="我最近很喜欢喝乌龙茶。",
                observed_at=NOW,
                trace_id="trace:production-background-fact",
            )
        )
        background = await app.drain_background_once()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert background is not None
    assert background.work_status == "accepted"
    ledger = SQLiteWorldLedger(path=path, world_id=_config().world_id)
    try:
        projection = ledger.project()
        retrieval = MemoryRetrievalCompiler(ledger=ledger).compile(
            cursor=ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            ),
            candidates=projection.memory_candidates,
            viewer_privacy_ceiling="private",
            projection=projection,
        )
    finally:
        ledger.close()
    assert len(projection.facts) == 1
    assert projection.facts[0].values.predicate_code == "preference.likes"
    assert len(projection.memory_candidates) == 1
    assert projection.memory_candidates[0].values.status == "active"
    assert retrieval.items[0].source_excerpts[0].text == "我最近很喜欢喝乌龙茶。"


@pytest.mark.asyncio
async def test_production_application_exposes_accepted_fact_memory_to_the_next_turn(
    tmp_path: Path,
) -> None:
    """The next deliberation must see source text, not only a candidate identifier."""

    chat = _CapturingDraftChatModel()
    reply_model = ChatModelDeliberationAdapter(model=chat)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-next-turn-fact-memory.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        fact_model=_FactChat(),
        memory_model=_MemoryChat(),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        first = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:next-turn-fact-source",
                text="我最近很喜欢喝乌龙茶。",
                observed_at=NOW,
                trace_id="trace:next-turn-fact-source",
            )
        )
        background = await app.drain_background_once()
        second = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:next-turn-memory-consumer",
                text="你还记得我刚才说的偏好吗？",
                observed_at=NOW,
                trace_id="trace:next-turn-memory-consumer",
            )
        )
    finally:
        app.close()

    assert first.status == "action_authorized"
    assert background is not None and background.work_status == "accepted"
    assert second.status == "action_authorized"
    assert len(chat.requests) == 2

    next_request = json.loads(chat.requests[1][1]["content"])["request"]
    context = json.loads(next_request["model_content_json"])
    memories = context["slices"]["active_memory_candidates"]
    assert len(memories["items"]) == 1
    memory = memories["items"][0]["value"]
    assert memory["source_excerpts"][0]["text"] == "我最近很喜欢喝乌龙茶。"


@pytest.mark.asyncio
async def test_production_application_carries_accepted_appraisal_into_an_affect_episode(
    tmp_path: Path,
) -> None:
    path = tmp_path / "world-v2-affect.sqlite"
    reply_model = ChatModelDeliberationAdapter(model=_DraftChatModel())
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=_AppraisingChat()),
        affect_model=AffectDraftDeliberationAdapter(model=_OpenAffectChat()),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:affect",
                text="你刚才的回复让我有点失望。",
                observed_at=NOW,
                trace_id="trace:production-affect",
            )
        )
        appraisal = await app.drain_background_once()
        affect = await app.drain_background_once()
    finally:
        app.close()

    assert appraisal is not None and appraisal.work_status == "accepted"
    assert affect is not None and affect.work_status == "accepted"
    ledger = SQLiteWorldLedger(path=path, world_id=_config().world_id)
    try:
        episode = ledger.project().affect_episodes
    finally:
        ledger.close()
    assert len(episode) == 1
    assert episode[0].components[0].dimension == "hurt"
