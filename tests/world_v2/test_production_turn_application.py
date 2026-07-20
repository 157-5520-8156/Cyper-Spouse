from __future__ import annotations

from dataclasses import replace
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
from companion_daemon.world_v2.relationship_draft_deliberation_adapter import (
    RelationshipDraftDeliberationAdapter,
)
from companion_daemon.world_v2.memory_retrieval import MemoryRetrievalCompiler
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.production_turn_application import (
    MediaContinuationComposition,
    MediaSelectionAcceptanceComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.activity_plan_runtime import (
    ActivityPlanCommand,
    ActivityPlanTransitionCommand,
)
from companion_daemon.world_v2.image_evidence_contract import (
    CharacterMediaEvidenceV1,
    ImageEvidenceV1,
)
from companion_daemon.world_v2.image_evidence_runtime import ImageEvidenceDeclarationCommand
from companion_daemon.world_v2.private_image_evidence_contract import RecipientScopedImageEvidenceV1
from companion_daemon.world_v2.private_image_evidence_runtime import (
    RecipientScopedImageEvidenceDeclarationCommand,
)
from companion_daemon.world_v2.appearance_state import (
    AppearanceStateRecordCommand,
    VisibleAppearanceAttribute,
)
from companion_daemon.world_v2.event_ecology_media import EcologyPolicy
from companion_daemon.world_v2.life_ecology_runtime import LifeEcologyRunResult
from companion_daemon.world_v2.media_v2 import MediaNotRenderable, MediaPlanningResult
from companion_daemon.world_v2.production_turn_application import LifeEcologyComposition
from companion_daemon.world_v2.schemas import ClockObservation, ProjectionCursor, ProviderMediaGrantBinding
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.visual_fact import (
    VisualFactContentV1,
    VisualFactRecordCommand,
    VisualObjectEvidenceV1,
)
from companion_daemon.world_v2.world_turn_runtime import InboundTurn


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert (platform, platform_user_id) == ("test", "user.1")
        return "user:user.1", "user:user.1"


class _ConversationTargetIdentities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert (platform, platform_user_id) == ("test", "user.1")
        return "user:user.1", "conversation:test:c2c:user.1"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _InvalidModel:
    async def propose(self, _request: ModelInput) -> ModelOutput:
        return ModelOutput(model_id="test-main", model_version="test.1", raw_proposal={})


class _InvalidQuick:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        return ModelOutput(model_id="test-quick", model_version="test.1", raw_proposal={})


class _AuthorityShapedLifeModel:
    model = "test-authority-shaped-life"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        capsule = json.loads(messages[-1]["content"])
        openings = capsule.get("openings", [])
        for phrase in (
            "outside interruption", "previously paused", "private shared",
            "user-influenced", "replacement plan",
        ):
            selected = next(
                (
                    item for item in openings
                    if phrase in str(item.get("safe_summary", ""))
                ),
                None,
            )
            if selected is not None:
                return json.dumps({
                    "decision": "select",
                    "opening_token": selected["opening_token"],
                })
        return '{"decision":"no_op"}'


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("invalid proposal must not create an external dispatch")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _MediaTransport:
    provider = "provider:test-media"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("composition test must not dispatch media")

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


class _TwoBeatChatModel:
    model = "test-flash-two-beat"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {"modality": "text", "text": "第一句。"},
                    {"modality": "text", "text": "第二句。"},
                ],
                "stance": "continue_naturally",
                "brief_rationale": "A thought is naturally split across two messages.",
            },
            ensure_ascii=False,
        )


class _LaterDraftChatModel:
    model = "test-flash-later"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return json.dumps({
            "timing_choice": "later",
            "beats": [{"modality": "text", "text": "那就先这样，等你忙完再聊。"}],
            "delay_seconds": 60,
            "expires_after_seconds": 600,
            "stance": "defer",
            "brief_rationale": "当前活动结束后再自然接续",
            "confidence": 7200,
        }, ensure_ascii=False)


class _NeverProactiveModel:
    model = "test-proactive-must-remain-idle"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        raise AssertionError("a materialized deferred Thread must not authorize a duplicate send")


class _TimingDraftChatModel:
    model = "test-flash-timing"

    def __init__(self, choice: str) -> None:
        self.choice = choice
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        value = {
            "timing_choice": self.choice,
            "beats": [] if self.choice == "silent" else [
                {"modality": "text", "text": "我在。"}
            ],
            "stance": "answer_without_world_claims",
            "brief_rationale": "同一轮选择表达时机",
            "confidence": 7000,
        }
        return json.dumps(value, ensure_ascii=False)


class _SequenceTimingDraftChatModel:
    model = "test-flash-timing-sequence"

    def __init__(self, choices: tuple[str, ...]) -> None:
        self.choices = choices
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        choice = self.choices[self.calls]
        self.calls += 1
        value: dict[str, object] = {
            "timing_choice": choice,
            "beats": [] if choice == "silent" else [
                {"modality": "text", "text": f"第{self.calls}次稍后接着说。"}
            ],
            "stance": "defer" if choice == "later" else "answer_without_world_claims",
            "brief_rationale": "测试同一主草案的时机选择",
            "confidence": 7000,
        }
        if choice == "later":
            value.update(delay_seconds=60, expires_after_seconds=600)
        return json.dumps(value, ensure_ascii=False)


class _CancelReviewer:
    async def review(self, **_kwargs):  # type: ignore[no-untyped-def]
        return "cancel"


class _NoOpMediaSelectionModel:
    model = "test-media-selection"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.2
        return '{"decision":"no_op"}'


class _SelectingMediaSelectionModel:
    """Select only an opaque token offered by the production selection seam."""

    model = "test-media-selection"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.2
        self.calls += 1
        capsule = json.loads(messages[-1]["content"])
        return json.dumps(
            {"decision": "select", "token": capsule["candidates"][0]["token"]}
        )


class _DurableNotRenderablePlanner:
    """A restart-safe planner double keyed by the immutable request id."""

    def __init__(self) -> None:
        self.results: dict[str, MediaPlanningResult] = {}
        self.plan_calls = 0

    async def lookup(self, *, planning_request_id: str) -> MediaPlanningResult | None:
        return self.results.get(planning_request_id)

    async def plan(self, *, opportunity, planning_request_id: str) -> MediaPlanningResult:  # type: ignore[no-untyped-def]
        self.plan_calls += 1
        result = MediaPlanningResult(not_renderable=MediaNotRenderable(
            opportunity_id=opportunity.opportunity_id,
            planning_request_id=planning_request_id,
            event_snapshot_hash=opportunity.event_snapshot_hash,
            reason_code="fixture_no_renderer",
            planner_version="test-durable-planner.1",
        ))
        self.results[planning_request_id] = result
        return result


class _CapturingDraftChatModel(_DraftChatModel):
    """Keep the exact model request so a production turn can assert Context."""

    def __init__(self) -> None:
        self.requests: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        self.requests.append(messages)
        return await super().complete(messages, temperature=temperature)


class _DeliveredTransport:
    provider = "platform:test"

    def __init__(self, *, received_at: datetime = NOW) -> None:
        self.bodies: list[str] = []
        self.received_at = received_at

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.bodies.append(request.body)
        sequence = len(self.bodies)
        return PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:production-application:{sequence}",
            provider_ref=f"message:production-application:{sequence}",
            status="delivered",
            received_at=self.received_at,
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


class _ImmediateEmotionChat:
    model = "test-immediate-emotion"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return json.dumps(
            {
                "appraise": True,
                "affect": "open",
                "brief_rationale": "The insult creates an immediate but suppressible wound.",
                "behavior_tendency": "set_boundary",
                "stance": "attend_with_distance",
                "display_strategy": "restrained_boundary",
                "confidence": 8400,
                "meanings": [
                    {"meaning": "boundary_violation", "confidence": 8200},
                    {"meaning": "misunderstanding", "confidence": 1800},
                ],
                "attribution": "user",
                "severity": 7800,
                "components": [
                    {"dimension": "hurt", "intensity_bp": 6200},
                    {"dimension": "anger", "intensity_bp": 4100},
                ],
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


class _NeverCalledAffectChat:
    model = "test-background-affect-must-not-run"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        raise AssertionError("same-turn affect must reuse the appraisal model result")


class _RelationshipSignalChat:
    """Suggest one bounded first relationship signal; never names a subject."""

    model = "test-relationship"

    def __init__(self) -> None:
        self.messages: list[object] = []

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.2
        self.messages.append(messages)
        return json.dumps(
            {
                "decision": "signal",
                "signal_code": "reliability_follow_through",
                "confidence_bp": 7400,
                "persistence": "durable",
                "rationale_code": "accepted_reliability_evidence",
                "suggested_deltas": {
                    "trust_bp": 240,
                    "closeness_bp": 40,
                    "respect_bp": 160,
                    "reliability_bp": 260,
                    "mutuality_bp": 20,
                    "repair_confidence_bp": 0,
                },
            }
        )


class _RelationshipStrongSignalChat(_RelationshipSignalChat):
    """Fixture-only evidence whose bounded accumulation crosses each stage gate."""

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.2
        self.messages.append(messages)
        return json.dumps(
            {
                "decision": "signal",
                "signal_code": "reliability_follow_through",
                "confidence_bp": 7400,
                "persistence": "durable",
                "rationale_code": "accepted_reliability_evidence",
                "suggested_deltas": {
                    "trust_bp": 500,
                    "closeness_bp": 500,
                    "respect_bp": 500,
                    "reliability_bp": 500,
                    "mutuality_bp": 500,
                    "repair_confidence_bp": 500,
                },
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


def test_media_continuation_composition_bootstraps_separate_accounts_and_restarts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "world-v2-media-continuation-composition.sqlite"
    continuation = MediaContinuationComposition(
        render_grant=ProviderMediaGrantBinding(grant_id="grant:render", grant_revision=1),
        render_account_id="account:media-render", render_window_id="window:media-render",
        render_account_limit=7, render_amount_limit=2,
        inspection_grant=ProviderMediaGrantBinding(
            grant_id="grant:inspection", grant_revision=1
        ),
        inspection_account_id="account:media-inspection",
        inspection_window_id="window:media-inspection",
        inspection_account_limit=5, inspection_amount_limit=1,
    )
    config = WorldV2TurnApplicationConfig(
        world_id="world:media-continuation-composition",
        companion_actor_ref="agent:companion", reply_target="user:user.1",
        action_pump_owner="pump:media-continuation-composition",
        media_continuation=continuation,
    )
    for _restart in range(2):
        app = build_sqlite_world_v2_turn_application(
            path=path, config=config, identities=_Identities(), router=_Router(),
            main_model=_InvalidModel(), quick_recovery=_InvalidQuick(),
            transport=_Transport(), media_transport=_MediaTransport(), now=NOW,
        )
        try:
            projection = app._ledger.project()  # noqa: SLF001 - composition assertion
            accounts = {item.account_id: item for item in projection.budget_accounts}
            assert accounts[continuation.render_account_id].limit == 7
            assert accounts[continuation.inspection_account_id].limit == 5
            assert app._media_continuation_worker is not None  # noqa: SLF001
        finally:
            app.close()


def _life_ecology_config() -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:life-ecology-production",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:life-ecology-production",
        life_ecology=LifeEcologyComposition.production_v1(),
    )


def test_media_selection_acceptance_configuration_bootstraps_its_image_account(tmp_path: Path) -> None:
    config = WorldV2TurnApplicationConfig(
        world_id="world:media-selection-acceptance-config",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:media-selection-acceptance-config",
        event_ecology_policy=EcologyPolicy(),
        media_selection_acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(grant_id="grant:media", grant_revision=1),
            account_id="account:media", account_window_id="window:media", account_limit=5,
            amount_limit=1,
        ),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "media-selection-acceptance-config.sqlite", config=config,
        identities=_Identities(), router=_Router(), main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(), transport=_Transport(), now=NOW,
    )
    try:
        account = next(item for item in app._ledger.project().budget_accounts if item.account_id == "account:media")  # type: ignore[attr-defined]
        assert account.category == "image"
        assert account.limit == 5
    finally:
        app.close()

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
async def test_production_authority_shaped_life_openings_cover_shared_interruption_and_repair(
    tmp_path: Path,
) -> None:
    config = WorldV2TurnApplicationConfig(
        world_id="world:authority-shaped-life",
        companion_actor_ref="agent:companion", reply_target="user:user.1",
        action_pump_owner="pump:authority-shaped-life",
        life_ecology=LifeEcologyComposition.production_v1(),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "authority-shaped-life.sqlite", config=config,
        identities=_Identities(), router=_Router(), main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(), transport=_Transport(),
        activity_lifecycle_model=_AuthorityShapedLifeModel(), now=NOW,
    )
    try:
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id="message:shared-plan",
            text="我们晚点一起安静看一会儿书。", observed_at=NOW,
            trace_id="trace:authority-shaped:plan-source",
        ))
        await app.plan_activity(ActivityPlanCommand(
            command_id="command:shared-plan", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:shared-plan",
            plan_id="plan:shared-private", activity_id="activity:shared-private",
            activity_kind="shared.quiet_reading", importance_bp=5_000,
            participant_refs=("user:user.1",), privacy_class="private",
            policy_refs=(
                "matrix:domain:family_roommate_friend",
                "matrix:social:shared_private",
                "matrix:source:user_influence",
            ),
        ), logical_time=NOW, created_at=NOW,
            trace_id="trace:authority-shaped:plan",
            causation_id="cause:authority-shaped:plan",
            correlation_id="correlation:authority-shaped")

        await app.tick(
            tick_id="authority-shaped:start", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:authority-shaped:start", causation_id="scheduler:test",
            correlation_id="correlation:authority-shaped", reason="test",
        )
        assert app._ledger.project().plans[0].status == "active"  # noqa: SLF001

        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id="message:interrupt",
            text="等等，我突然想先说件事。", observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:authority-shaped:interrupt-source",
        ))
        await app.tick(
            tick_id="authority-shaped:pause",
            logical_time_from=NOW + timedelta(minutes=1),
            logical_time_to=NOW + timedelta(minutes=2),
            observed_at=NOW + timedelta(minutes=2),
            trace_id="trace:authority-shaped:pause", causation_id="scheduler:test",
            correlation_id="correlation:authority-shaped", reason="test",
        )
        assert app._ledger.project().plans[0].status == "paused"  # noqa: SLF001

        await app.tick(
            tick_id="authority-shaped:resume",
            logical_time_from=NOW + timedelta(minutes=2),
            logical_time_to=NOW + timedelta(minutes=3),
            observed_at=NOW + timedelta(minutes=3),
            trace_id="trace:authority-shaped:resume", causation_id="scheduler:test",
            correlation_id="correlation:authority-shaped", reason="test",
        )
        projection = app._ledger.project()  # noqa: SLF001
        assert projection.plans[0].status == "active"
        events = app._ledger.export_replay_evidence().events  # noqa: SLF001
        effects = [
            item.event.payload() for item in events
            if item.event.event_type in {"ActivityStarted", "ActivityPaused", "ActivityResumed"}
        ]
        assert "matrix:social:shared_private" in effects[0]["policy_refs"]
        assert "matrix:source:user_influence" in effects[0]["policy_refs"]
        assert "matrix:source:interruption" in effects[1]["policy_refs"]
        assert effects[1]["evidence_refs"][-1]["evidence_type"] == "observed_message"
        assert "matrix:deviation:repair" in effects[2]["policy_refs"]
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_user_observation_replacement_becomes_optional_change_plan_opening(
    tmp_path: Path,
) -> None:
    config = WorldV2TurnApplicationConfig(
        world_id="world:authority-shaped-replacement",
        companion_actor_ref="agent:companion", reply_target="user:user.1",
        action_pump_owner="pump:authority-shaped-replacement",
        life_ecology=LifeEcologyComposition.production_v1(),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "authority-shaped-replacement.sqlite", config=config,
        identities=_Identities(), router=_Router(), main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(), transport=_Transport(),
        activity_lifecycle_model=_AuthorityShapedLifeModel(), now=NOW,
    )
    try:
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id="message:original-plan", text="晚点读会儿书。",
            observed_at=NOW, trace_id="trace:replacement:original-source",
        ))
        await app.plan_activity(ActivityPlanCommand(
            command_id="command:original-plan", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:original-plan",
            plan_id="plan:original", activity_id="activity:original",
            activity_kind="study.reading", importance_bp=4_000,
            privacy_class="personal",
        ), logical_time=NOW, created_at=NOW, trace_id="trace:replacement:original",
            causation_id="cause:replacement:original",
            correlation_id="correlation:replacement")
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id="message:replacement-plan",
            text="改主意了，我们先出去走走。", observed_at=NOW,
            trace_id="trace:replacement:new-source",
        ))
        await app.replace_activity(ActivityPlanCommand(
            command_id="command:replacement-plan", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:replacement-plan",
            plan_id="plan:replacement", activity_id="activity:replacement",
            activity_kind="commute.short_walk", importance_bp=5_000,
            participant_refs=("user:user.1",), privacy_class="personal",
            supersedes_plan_id="plan:original",
            policy_refs=(
                "matrix:deviation:change_plan",
                "matrix:social:user_relayed",
                "matrix:source:user_influence",
            ),
        ), predecessor_plan_id="plan:original", logical_time=NOW, created_at=NOW,
            trace_id="trace:replacement:new", causation_id="cause:replacement:new",
            correlation_id="correlation:replacement")

        await app.tick(
            tick_id="replacement:start", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:replacement:start", causation_id="scheduler:test",
            correlation_id="correlation:replacement", reason="test",
        )

        projection = app._ledger.project()  # noqa: SLF001
        by_id = {item.plan_id: item for item in projection.plans}
        assert by_id["plan:original"].status == "abandoned"
        assert by_id["plan:replacement"].status == "active"
        events = app._ledger.export_replay_evidence().events  # noqa: SLF001
        started = next(
            item.event for item in events
            if item.event.event_type == "ActivityStarted"
        )
        assert "matrix:deviation:change_plan" in started.payload()["policy_refs"]
        assert "matrix:source:user_influence" in started.payload()["policy_refs"]
        assert started.payload()["evidence_refs"][-1]["evidence_type"] == "observed_message"
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
        preview = await app.drain_media_preview_once(
            trace_id="trace:media-preview-unavailable",
            correlation_id="correlation:media-preview-unavailable",
        )
        assert (preview.status, preview.reason_code) == (
            "blocked", "media_preview.conductor_unavailable",
        )
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
                    character_media=CharacterMediaEvidenceV1(
                        character_ref="agent:companion",
                        present=True,
                        capture_capabilities=("character_front_camera",),
                    ),
                ),
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:declared-ecology:evidence",
            correlation_id="correlation:declared-ecology",
        )
        result = await app.drain_media_ecology_once(
            wake_event_ref=declaration.event_ids[-1], logical_time=NOW,
            trace_id="trace:declared-ecology:worker", correlation_id="correlation:declared-ecology",
        )
        character_candidates = await app.drain_character_media_candidates_once(
            wake_event_ref=declaration.event_ids[-1], logical_time=NOW,
            trace_id="trace:declared-ecology:character-worker",
            correlation_id="correlation:declared-ecology",
        )
    finally:
        app.close()

    assert result is not None and result.status == "created"
    assert len(result.candidate_ids) == 1
    assert len(character_candidates) == 1


@pytest.mark.asyncio
async def test_production_application_records_a_sparse_source_bound_appearance_state(
    tmp_path: Path,
) -> None:
    config = WorldV2TurnApplicationConfig(
        world_id="world:production-appearance-state",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:production-appearance-state",
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-production-appearance.sqlite",
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
            platform="test", platform_user_id="user.1", platform_message_id="message:appearance",
            text="我去公园走走。", observed_at=NOW, trace_id="trace:appearance:inbound",
        ))
        plan = await app.plan_activity(
            ActivityPlanCommand(
                command_id="command:appearance:plan", world_id=config.world_id,
                source_observation_id="observation:test:user.1:message:appearance",
                plan_id="plan:appearance", activity_id="activity:appearance", activity_kind="walk",
                importance_bp=4_000, location_ref="location:park", participant_refs=("agent:companion",),
                privacy_class="shareable",
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:appearance:plan",
            causation_id="cause:appearance:plan", correlation_id="correlation:appearance",
        )
        started = await app.transition_activity(
            ActivityPlanTransitionCommand(
                command_id="command:appearance:start", world_id=config.world_id,
                source_observation_id="observation:test:user.1:message:appearance",
                plan_id="plan:appearance", operation="start",
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:appearance:start",
            causation_id=plan.event_ids[-1], correlation_id="correlation:appearance",
        )
        recorded = await app.record_appearance_state(
            AppearanceStateRecordCommand(
                command_id="command:appearance:record", source_event_ref=started.event_ids[-1],
                subject_ref="agent:companion", visibility="shareable",
                visible_attributes=(
                    VisibleAppearanceAttribute(
                        aspect="outfit", description="深色运动外套",
                    ),
                ),
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:appearance:record",
            correlation_id="correlation:appearance",
        )
        projection = app._ledger.project()  # type: ignore[attr-defined]
    finally:
        app.close()

    assert recorded.event_ids
    assert projection.appearance_states[0].source_event_ref == started.event_ids[-1]
    assert projection.appearance_states[0].visible_attributes[0].description == "深色运动外套"


@pytest.mark.asyncio
async def test_production_p1_selection_acceptance_commits_the_source_bound_planning_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed production seams preserve P1's proposal/acceptance split.

    This deliberately uses the real SQLite composition, evidence compiler,
    candidate ecology, selector and acceptance runtime.  Provider-grant
    verification itself belongs to its domain-authorization contract tests;
    patching it here lets this test isolate the production wiring of the
    four-effect accepted batch.
    """

    config = WorldV2TurnApplicationConfig(
        world_id="world:production-p1-selection-acceptance",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:production-p1-selection-acceptance",
        event_ecology_policy=EcologyPolicy(max_candidates_per_drain=1),
        media_selection_acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(grant_id="grant:production-media", grant_revision=1),
            account_id="account:production-media",
            account_window_id="window:production-media",
            account_limit=5,
            amount_limit=1,
        ),
    )
    path = tmp_path / "world-v2-production-p1-selection.sqlite"
    selector = _SelectingMediaSelectionModel()
    planner = _DurableNotRenderablePlanner()
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(),
        transport=_Transport(),
        media_selection_model=selector,
        media_planner=planner,
        now=NOW,
    )
    try:
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="message:production-p1",
            text="傍晚我想去公园走走。", observed_at=NOW, trace_id="trace:production-p1:inbound",
        ))
        plan = await app.plan_activity(
            ActivityPlanCommand(
                command_id="command:production-p1:plan", world_id=config.world_id,
                source_observation_id="observation:test:user.1:message:production-p1",
                plan_id="plan:production-p1", activity_id="activity:production-p1",
                activity_kind="walk", importance_bp=4_000, location_ref="location:park",
                participant_refs=("agent:companion",), privacy_class="shareable",
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:production-p1:plan",
            causation_id="cause:production-p1:plan", correlation_id="correlation:production-p1",
        )
        started = await app.transition_activity(
            ActivityPlanTransitionCommand(
                command_id="command:production-p1:start", world_id=config.world_id,
                source_observation_id="observation:test:user.1:message:production-p1",
                plan_id="plan:production-p1", operation="start",
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:production-p1:start",
            causation_id=plan.event_ids[-1], correlation_id="correlation:production-p1",
        )
        declaration = await app.declare_image_evidence(
            ImageEvidenceDeclarationCommand(
                command_id="command:production-p1:evidence", source_event_ref=started.event_ids[-1],
                image_evidence=ImageEvidenceV1(
                    visibility="shareable",
                    activity={
                        "evidence_visibility": "shareable", "id": "activity:production-p1",
                        "kind": "walk", "description": "傍晚在公园散步", "phase": "active",
                    },
                ),
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:production-p1:evidence",
            correlation_id="correlation:production-p1",
        )
        ecology = await app.drain_media_ecology_once(
            wake_event_ref=declaration.event_ids[-1], logical_time=NOW,
            trace_id="trace:production-p1:ecology", correlation_id="correlation:production-p1",
        )
        assert ecology is not None and ecology.status == "created"
        selection = await app.drain_media_selection_once(
            logical_time=NOW, trace_id="trace:production-p1:selection",
            correlation_id="correlation:production-p1",
        )
        assert selection is not None and selection.status == "proposed"
        assert selection.proposal_event_ref is not None
        monkeypatch.setattr(
            "companion_daemon.world_v2.reducers.require_provider_media_grant",
            lambda **_kwargs: object(),
        )
        accepted = await app.accept_media_selection_once(
            proposal_event_ref=selection.proposal_event_ref, logical_time=NOW,
            trace_id="trace:production-p1:acceptance", correlation_id="correlation:production-p1",
        )
        projection = app._ledger.project()  # type: ignore[attr-defined]
    finally:
        app.close()

    assert accepted is not None
    assert len(accepted.event_ids) == 4
    assert [candidate.status for candidate in projection.photo_candidates] == ["selected"]
    assert len(projection.media_opportunities) == 1
    assert len(projection.budget_reservations) == 1
    assert len(projection.actions) == 1
    assert projection.actions[0].kind == "media_planning"
    assert projection.actions[0].budget_reservation_id == projection.budget_reservations[0].reservation_id

    # Simulate a crash after the four-effect Acceptance batch and before the
    # dedicated planning worker runs.  The next conductor pass must recover
    # the existing Action before asking the selector about another candidate.
    monkeypatch.setattr(
        "companion_daemon.world_v2.media_planning_worker.require_provider_media_grant",
        lambda **_kwargs: object(),
    )
    rebuilt = build_sqlite_world_v2_turn_application(
        path=path, config=config, identities=_Identities(), router=_Router(),
        main_model=_InvalidModel(), quick_recovery=_InvalidQuick(), transport=_Transport(),
        media_selection_model=selector, media_planner=planner, now=NOW,
    )
    try:
        resumed = await rebuilt.drain_media_preview_once(
            trace_id="trace:production-p1:planning-recovery",
            correlation_id="correlation:production-p1",
        )
        resumed_projection = rebuilt._ledger.project()  # type: ignore[attr-defined]
    finally:
        rebuilt.close()

    assert resumed.status == "not_renderable"
    assert resumed.selection is None
    assert planner.plan_calls == 1
    assert selector.calls == 1
    assert resumed_projection.actions[0].state == "delivered"


@pytest.mark.asyncio
async def test_media_preview_conductor_recovers_head_proposal_after_sqlite_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash between Proposal and Acceptance must not strand the candidate."""

    path = tmp_path / "world-v2-media-preview-proposal-recovery.sqlite"
    config = WorldV2TurnApplicationConfig(
        world_id="world:media-preview-proposal-recovery",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:media-preview-proposal-recovery",
        event_ecology_policy=EcologyPolicy(max_candidates_per_drain=1),
        media_selection_acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(
                grant_id="grant:media-preview-recovery", grant_revision=1,
            ),
            account_id="account:media-preview-recovery",
            account_window_id="window:media-preview-recovery",
            account_limit=5,
            amount_limit=1,
        ),
    )
    selector = _SelectingMediaSelectionModel()
    planner = _DurableNotRenderablePlanner()

    app = build_sqlite_world_v2_turn_application(
        path=path, config=config, identities=_Identities(), router=_Router(),
        main_model=_InvalidModel(), quick_recovery=_InvalidQuick(), transport=_Transport(),
        media_selection_model=selector, media_planner=planner, now=NOW,
    )
    try:
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id="message:media-preview-recovery",
            text="今晚去公园走走。", observed_at=NOW,
            trace_id="trace:media-preview-recovery:inbound",
        ))
        plan = await app.plan_activity(ActivityPlanCommand(
            command_id="command:media-preview-recovery:plan", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:media-preview-recovery",
            plan_id="plan:media-preview-recovery", activity_id="activity:media-preview-recovery",
            activity_kind="walk", importance_bp=4_000, location_ref="location:park",
            participant_refs=("agent:companion",), privacy_class="shareable",
        ), logical_time=NOW, created_at=NOW, trace_id="trace:media-preview-recovery:plan",
            causation_id="cause:media-preview-recovery:plan",
            correlation_id="correlation:media-preview-recovery")
        started = await app.transition_activity(ActivityPlanTransitionCommand(
            command_id="command:media-preview-recovery:start", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:media-preview-recovery",
            plan_id="plan:media-preview-recovery", operation="start",
        ), logical_time=NOW, created_at=NOW, trace_id="trace:media-preview-recovery:start",
            causation_id=plan.event_ids[-1], correlation_id="correlation:media-preview-recovery")
        declaration = await app.declare_image_evidence(ImageEvidenceDeclarationCommand(
            command_id="command:media-preview-recovery:evidence",
            source_event_ref=started.event_ids[-1],
            image_evidence=ImageEvidenceV1(
                visibility="shareable",
                activity={
                    "evidence_visibility": "shareable",
                    "id": "activity:media-preview-recovery",
                    "kind": "walk",
                    "description": "傍晚在公园散步",
                    "phase": "active",
                },
            ),
        ), logical_time=NOW, created_at=NOW,
            trace_id="trace:media-preview-recovery:evidence",
            correlation_id="correlation:media-preview-recovery")
        ecology = await app.drain_media_ecology_once(
            wake_event_ref=declaration.event_ids[-1], logical_time=NOW,
            trace_id="trace:media-preview-recovery:ecology",
            correlation_id="correlation:media-preview-recovery",
        )
        assert ecology is not None and ecology.status == "created"
        proposed = await app.drain_media_selection_once(
            logical_time=NOW, trace_id="trace:media-preview-recovery:selection",
            correlation_id="correlation:media-preview-recovery",
        )
        assert proposed is not None and proposed.status == "proposed"
        proposal_event_ref = proposed.proposal_event_ref
    finally:
        app.close()

    # This fixture isolates orchestration/restart.  Enforcement-grant
    # signature provisioning has separate authority tests and remains a
    # deployment seam; neither patch changes candidate or snapshot bytes.
    monkeypatch.setattr(
        "companion_daemon.world_v2.reducers.require_provider_media_grant",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "companion_daemon.world_v2.media_planning_worker.require_provider_media_grant",
        lambda **_kwargs: object(),
    )
    rebuilt = build_sqlite_world_v2_turn_application(
        path=path, config=config, identities=_Identities(), router=_Router(),
        main_model=_InvalidModel(), quick_recovery=_InvalidQuick(), transport=_Transport(),
        media_selection_model=selector, media_planner=planner, now=NOW,
    )
    try:
        result = await rebuilt.drain_media_preview_once(
            trace_id="trace:media-preview-recovery:resume",
            correlation_id="correlation:media-preview-recovery",
        )
        projection = rebuilt._ledger.project()  # type: ignore[attr-defined]
    finally:
        rebuilt.close()

    assert result.status == "not_renderable"
    assert result.selection is not None
    assert result.selection.reason_code == "media_selection.recovered_pending_proposal"
    assert result.selection.proposal_event_ref == proposal_event_ref
    assert selector.calls == 1
    assert planner.plan_calls == 1
    assert len(projection.proposal_ids) == 1
    assert len(projection.media_opportunities) == 1
    assert len(projection.actions) == 1
    assert projection.actions[0].state == "delivered"
    assert projection.media_unrenderable_opportunity_ids == (
        projection.media_opportunities[0].opportunity_id,
    )


@pytest.mark.asyncio
async def test_production_visual_fact_sidecar_opens_source_bound_object_food_candidate(
    tmp_path: Path,
) -> None:
    """The public composition preserves the sidecar-only object/food seam.

    This is deliberately narrower than P1 selection: P1 already proves the
    candidate-to-planning batch, while this regression proves the trusted
    production writer and ecology compiler use the same immutable payload
    store.  The production profile opens only a candidate; it must not skip
    the selection/acceptance boundary and freeze an opportunity directly.
    No text from a generic Fact or an activity label can stand in for the
    visual evidence.
    """

    config = WorldV2TurnApplicationConfig(
        world_id="world:production-visual-fact",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:production-visual-fact",
        event_ecology_policy=EcologyPolicy(max_candidates_per_drain=1),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-production-visual-fact.sqlite",
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
            platform="test", platform_user_id="user.1", platform_message_id="message:production-visual-fact",
            text="傍晚想吃点热的。", observed_at=NOW, trace_id="trace:production-visual-fact:inbound",
        ))
        plan = await app.plan_activity(ActivityPlanCommand(
            command_id="command:production-visual-fact:plan", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:production-visual-fact",
            plan_id="plan:production-visual-fact", activity_id="activity:production-visual-fact",
            activity_kind="cook", importance_bp=4_000, location_ref="location:home",
            participant_refs=("agent:companion",), privacy_class="shareable",
        ), logical_time=NOW, created_at=NOW, trace_id="trace:production-visual-fact:plan",
            causation_id="cause:production-visual-fact:plan", correlation_id="correlation:production-visual-fact")
        started = await app.transition_activity(ActivityPlanTransitionCommand(
            command_id="command:production-visual-fact:start", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:production-visual-fact",
            plan_id="plan:production-visual-fact", operation="start",
        ), logical_time=NOW, created_at=NOW, trace_id="trace:production-visual-fact:start",
            causation_id=plan.event_ids[-1], correlation_id="correlation:production-visual-fact")
        recorded = await app.record_visual_fact(
            VisualFactRecordCommand(
                command_id="command:production-visual-fact:record",
                source_event_ref=started.event_ids[-1],
                content_ref="sidecar:production-visual-fact:noodles",
                content=VisualFactContentV1(
                    facet="meal.visible_food", subject_ref="activity:production-visual-fact",
                    visibility="shareable", objects=(VisualObjectEvidenceV1(
                        id="object:tomato-noodles", kind="food", description="一碗番茄鸡蛋面",
                        ownership="character", visibility="shareable",
                    ),),
                ),
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:production-visual-fact:record",
            correlation_id="correlation:production-visual-fact",
        )
        ecology = await app.drain_media_ecology_once(
            wake_event_ref=recorded.event_ids[-1], logical_time=NOW,
            trace_id="trace:production-visual-fact:ecology",
            correlation_id="correlation:production-visual-fact",
        )
        assert ecology is not None and ecology.status == "created", ecology
        projection = app._ledger.project()  # type: ignore[attr-defined]
    finally:
        app.close()

    assert projection.photo_candidates[0].ecology_category == "object_or_food"
    assert projection.photo_candidates[0].source_event_refs == (recorded.event_ids[-1],)
    assert projection.media_opportunities == ()
    descriptor = SQLiteWorldLedger(
        path=tmp_path / "world-v2-production-visual-fact.sqlite", world_id=config.world_id
    )
    try:
        stored = descriptor.lookup_event_commit(recorded.event_ids[-1])
        assert stored is not None
        assert "一碗番茄鸡蛋面" not in stored[0].payload_json
    finally:
        descriptor.close()


@pytest.mark.asyncio
async def test_production_p2_character_selection_acceptance_freezes_a_v2_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed SQLite composition accepts an ordinary, fact-bound selfie."""

    config = WorldV2TurnApplicationConfig(
        world_id="world:production-p2-selection-acceptance",
        companion_actor_ref="agent:companion", reply_target="user:user.1",
        action_pump_owner="pump:production-p2-selection-acceptance",
        event_ecology_policy=EcologyPolicy(max_candidates_per_drain=1),
        media_selection_acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(grant_id="grant:production-media", grant_revision=1),
            account_id="account:production-media", account_window_id="window:production-media",
            account_limit=5, amount_limit=1,
        ),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-production-p2-selection.sqlite", config=config,
        identities=_Identities(), router=_Router(), main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(), transport=_Transport(),
        media_selection_model=_SelectingMediaSelectionModel(), now=NOW,
    )
    try:
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="message:production-p2",
            text="今天想去公园散步。", observed_at=NOW, trace_id="trace:production-p2:inbound",
        ))
        plan = await app.plan_activity(ActivityPlanCommand(
            command_id="command:production-p2:plan", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:production-p2",
            plan_id="plan:production-p2", activity_id="activity:production-p2", activity_kind="walk",
            importance_bp=4_000, location_ref="location:park", participant_refs=("agent:companion",),
            privacy_class="shareable",
        ), logical_time=NOW, created_at=NOW, trace_id="trace:production-p2:plan",
            causation_id="cause:production-p2:plan", correlation_id="correlation:production-p2")
        started = await app.transition_activity(ActivityPlanTransitionCommand(
            command_id="command:production-p2:start", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:production-p2",
            plan_id="plan:production-p2", operation="start",
        ), logical_time=NOW, created_at=NOW, trace_id="trace:production-p2:start",
            causation_id=plan.event_ids[-1], correlation_id="correlation:production-p2")
        declaration = await app.declare_image_evidence(ImageEvidenceDeclarationCommand(
            command_id="command:production-p2:evidence", source_event_ref=started.event_ids[-1],
            image_evidence=ImageEvidenceV1(
                visibility="shareable",
                activity={
                    "evidence_visibility": "shareable", "id": "activity:production-p2",
                    "kind": "walk", "description": "公园散步", "phase": "active",
                },
                character_media=CharacterMediaEvidenceV1(
                    character_ref="agent:companion", present=True,
                    capture_capabilities=("character_front_camera",),
                ),
            ),
        ), logical_time=NOW, created_at=NOW, trace_id="trace:production-p2:evidence",
            correlation_id="correlation:production-p2")
        candidates = await app.drain_character_media_candidates_once(
            wake_event_ref=declaration.event_ids[-1], logical_time=NOW,
            trace_id="trace:production-p2:candidates", correlation_id="correlation:production-p2",
        )
        assert len(candidates) == 1
        selection = await app.drain_media_selection_once(
            logical_time=NOW, trace_id="trace:production-p2:selection",
            correlation_id="correlation:production-p2",
        )
        assert selection is not None and selection.status == "proposed"
        assert selection.proposal_event_ref is not None
        monkeypatch.setattr(
            "companion_daemon.world_v2.reducers.require_provider_media_grant", lambda **_kwargs: object()
        )
        accepted = await app.accept_media_selection_once(
            proposal_event_ref=selection.proposal_event_ref, logical_time=NOW,
            trace_id="trace:production-p2:acceptance", correlation_id="correlation:production-p2",
        )
        projection = app._ledger.project()  # type: ignore[attr-defined]
        sidecar = app._media_payload_store  # type: ignore[attr-defined]
        frozen = sidecar.read_exact(payload_ref=projection.media_opportunities[0].event_snapshot_ref)
    finally:
        app.close()

    assert accepted is not None and len(accepted.event_ids) == 4
    assert projection.photo_candidates[0].family == "character_media"
    assert projection.photo_candidates[0].status == "selected"
    opportunity = projection.media_opportunities[0]
    assert opportunity.family == "character_media"
    assert opportunity.candidate_source_event_refs == projection.photo_candidates[0].source_event_refs
    assert frozen is not None and '"world-image-event-snapshot-v2"' in frozen.body


@pytest.mark.asyncio
async def test_production_p3_declaration_opens_only_a_private_character_candidate(
    tmp_path: Path,
) -> None:
    """P3 reaches the production candidate seam without widening P2 evidence."""

    config = WorldV2TurnApplicationConfig(
        world_id="world:production-p3-private-candidate",
        companion_actor_ref="agent:companion", reply_target="user:user.1",
        action_pump_owner="pump:production-p3-private-candidate",
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-production-p3-private-candidate.sqlite", config=config,
        identities=_Identities(), router=_Router(), main_model=_InvalidModel(),
        quick_recovery=_InvalidQuick(), transport=_Transport(), now=NOW,
    )
    try:
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="message:production-p3",
            text="今晚我想自己待一会儿。", observed_at=NOW, trace_id="trace:production-p3:inbound",
        ))
        plan = await app.plan_activity(ActivityPlanCommand(
            command_id="command:production-p3:plan", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:production-p3",
            plan_id="plan:production-p3", activity_id="activity:production-p3", activity_kind="wind_down",
            importance_bp=4_000, location_ref="location:home", participant_refs=("agent:companion",),
            privacy_class="private",
        ), logical_time=NOW, created_at=NOW, trace_id="trace:production-p3:plan",
            causation_id="cause:production-p3:plan", correlation_id="correlation:production-p3")
        started = await app.transition_activity(ActivityPlanTransitionCommand(
            command_id="command:production-p3:start", world_id=config.world_id,
            source_observation_id="observation:test:user.1:message:production-p3",
            plan_id="plan:production-p3", operation="start",
        ), logical_time=NOW, created_at=NOW, trace_id="trace:production-p3:start",
            causation_id=plan.event_ids[-1], correlation_id="correlation:production-p3")
        declaration = await app.declare_recipient_scoped_image_evidence(
            RecipientScopedImageEvidenceDeclarationCommand(
                command_id="command:production-p3:evidence", source_event_ref=started.event_ids[-1],
                recipient_ref="user:user.1",
                image_evidence=RecipientScopedImageEvidenceV1(
                    visibility="private",
                    activity={
                        "evidence_visibility": "private", "id": "activity:production-p3",
                        "kind": "wind_down", "description": "在家放松", "phase": "active",
                    },
                    character_media=CharacterMediaEvidenceV1(
                        character_ref="agent:companion", present=True,
                        capture_capabilities=("character_front_camera",),
                    ),
                ),
            ),
            logical_time=NOW, created_at=NOW, trace_id="trace:production-p3:evidence",
            correlation_id="correlation:production-p3",
        )
        candidates = await app.drain_character_media_candidates_once(
            wake_event_ref=declaration.event_ids[-1], logical_time=NOW,
            trace_id="trace:production-p3:candidates", correlation_id="correlation:production-p3",
        )
        projection = app._ledger.project()  # type: ignore[attr-defined]
    finally:
        app.close()

    assert len(candidates) == 1
    candidate = projection.photo_candidates[0]
    assert candidate.family == "character_media"
    assert candidate.privacy_ceiling == "private"
    assert declaration.event_ids[-1] in candidate.source_event_refs


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
async def test_production_application_delivers_every_ordered_expression_beat(
    tmp_path: Path,
) -> None:
    transport = _DeliveredTransport()
    model = ChatModelDeliberationAdapter(model=_TwoBeatChatModel())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-two-beat-delivery.sqlite",
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
                platform_message_id="message:two-beat-delivery",
                text="别总是一问一答。",
                observed_at=NOW,
                trace_id="trace:production-two-beat-delivery",
            )
        )
        first = await app.drain_actions_once()
        second = await app.drain_actions_once()
        projection = app._ledger.project()  # noqa: SLF001 - public lifecycle evidence
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert first is not None and first.status == "settled"
    assert second is not None and second.status == "settled", {
        "first": first.model_dump(mode="json") if first else None,
        "second": second.model_dump(mode="json") if second else None,
        "actions": [(item.action_id, item.state) for item in projection.actions[-2:]],
    }
    assert transport.bodies == ["第一句。", "第二句。"]
    assert projection.expression_plans[-1].state == "completed"


@pytest.mark.asyncio
async def test_production_shared_reply_audit_reaches_defer_without_second_model_call_and_restarts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "shared-reply-timing.sqlite"
    model = _LaterDraftChatModel()
    proactive = _NeverProactiveModel()
    adapter = ChatModelDeliberationAdapter(model=model)
    transport = _DeliveredTransport(received_at=NOW + timedelta(seconds=60))
    app = build_sqlite_world_v2_turn_application(
        path=path, config=_config(), identities=_Identities(), router=_Router(),
        main_model=adapter, quick_recovery=adapter, transport=transport,
        proactive_model=proactive, now=NOW,
    )
    outcome = await app.respond(InboundTurn(
        platform="test", platform_user_id="user.1", platform_message_id="later-1",
        text="你先忙吧", observed_at=NOW, trace_id="trace:shared-timing",
    ))
    assert outcome.status == "deferred"
    assert outcome.authorized_action_ids == ()
    assert model.calls == 1
    projection = app._ledger.project()  # noqa: SLF001 - production reachability proof
    assert len(projection.threads) == 1
    assert len(projection.commitments) == 1
    assert len(projection.actions) == 1
    assert projection.actions[0].kind == "followup"
    assert projection.actions[0].not_before == NOW + timedelta(seconds=60)
    action_id = projection.actions[0].action_id
    app.close()

    rebuilt_adapter = ChatModelDeliberationAdapter(model=model)
    rebuilt = build_sqlite_world_v2_turn_application(
        path=path, config=_config(), identities=_Identities(), router=_Router(),
        main_model=rebuilt_adapter, quick_recovery=rebuilt_adapter,
        transport=transport, proactive_model=proactive,
        now=NOW + timedelta(seconds=30),
    )
    try:
        replayed = await rebuilt.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="later-1",
            text="你先忙吧", observed_at=NOW, trace_id="trace:shared-timing",
        ))
        assert replayed.status == "deferred"
        assert replayed.authorized_action_ids == ()
        assert await rebuilt.drain_background_once() is None
        assert model.calls == 1
        projection = rebuilt._ledger.project()  # noqa: SLF001
        assert next(item for item in projection.actions if item.action_id == action_id).state in {
            "authorized", "scheduled"
        }
        assert rebuilt._ledger.rebuild() == projection  # noqa: SLF001
        await rebuilt.tick(
            tick_id="shared-reply-due",
            logical_time_from=projection.logical_time or NOW,
            logical_time_to=NOW + timedelta(seconds=60),
            observed_at=NOW + timedelta(seconds=60),
            trace_id="trace:shared-timing:due",
            causation_id=action_id,
            correlation_id="conversation:test:user.1",
            reason="scheduled_followup_due",
        )
        # The still-open Thread is continuity evidence.  Because its exact
        # Commitment and followup Action already exist, Pulse must not turn it
        # into a second model decision or a duplicate message.
        assert await rebuilt.drain_background_once() is None
        assert proactive.calls == 0
        delivered = await rebuilt.drain_actions_once()
        assert delivered is not None and delivered.status == "settled"
        settled = rebuilt._ledger.project()  # noqa: SLF001
        assert next(item for item in settled.actions if item.action_id == action_id).state == "delivered"
        assert next(
            item for item in settled.commitments
            if item.values.fulfillment_contract.expected_action_id == action_id
        ).values.status == "fulfilled"
        assert len(settled.threads) == 1
        assert settled.threads[0].values.status == "open"
        assert any(item.action_id == action_id for item in settled.execution_receipts)
        assert transport.bodies == ["那就先这样，等你忙完再聊。"]
    finally:
        rebuilt.close()


@pytest.mark.parametrize(
    ("choice", "expected_status", "expected_action_count"),
    (("now", "action_authorized", 1), ("silent", "observed_only", 0)),
)
@pytest.mark.asyncio
async def test_production_now_and_silent_are_final_without_a_social_background_unit(
    tmp_path: Path, choice: str, expected_status: str, expected_action_count: int,
) -> None:
    model = _TimingDraftChatModel(choice)
    adapter = ChatModelDeliberationAdapter(model=model)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / f"shared-reply-{choice}.sqlite",
        config=_config(), identities=_Identities(), router=_Router(),
        main_model=adapter, quick_recovery=adapter, transport=_Transport(), now=NOW,
    )
    try:
        outcome = await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id=f"timing-{choice}", text="你在吗？",
            observed_at=NOW, trace_id=f"trace:timing:{choice}",
        ))
        background = await app.drain_background_once()
        projection = app._ledger.project()  # noqa: SLF001
    finally:
        app.close()

    assert outcome.status == expected_status
    assert len(projection.actions) == expected_action_count
    assert model.calls == 1
    assert background is None
    assert not any(
        item.process_kind == "social_action_deliberation"
        for item in projection.trigger_processes
    )


@pytest.mark.asyncio
async def test_production_later_budget_exhaustion_is_terminal_without_partial_effects(
    tmp_path: Path,
) -> None:
    model = _SequenceTimingDraftChatModel(("later", "later"))
    adapter = ChatModelDeliberationAdapter(model=model)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "shared-reply-budget.sqlite",
        config=replace(_config(), chat_budget_limit=10, reply_budget_amount=10),
        identities=_Identities(), router=_Router(), main_model=adapter,
        quick_recovery=adapter, transport=_Transport(), now=NOW,
    )
    try:
        first = await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="later-budget-1",
            text="晚点再说。", observed_at=NOW, trace_id="trace:later-budget:1",
        ))
        second = await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="later-budget-2",
            text="还有一件事也晚点说。", observed_at=NOW,
            trace_id="trace:later-budget:2",
        ))
        projection = app._ledger.project()  # noqa: SLF001
    finally:
        app.close()

    assert first.status == "deferred"
    assert second.status == "failed_safe"
    assert second.terminal_errors == ("social_action.chat_budget_exhausted",)
    assert model.calls == 2
    assert len(projection.actions) == len(projection.commitments) == 1
    terminals = [item for item in projection.trigger_processes
        if item.process_kind == "social_action_deliberation" and item.state == "terminal"]
    assert len(terminals) == 2
    assert {item.runtime_outcome_ref.split(":", 3)[1] for item in terminals} == {
        "accepted_defer", "budget_exhausted"
    }


@pytest.mark.asyncio
async def test_production_user_interjection_cancels_shared_deferred_followup(
    tmp_path: Path,
) -> None:
    model = _SequenceTimingDraftChatModel(("later", "silent"))
    adapter = ChatModelDeliberationAdapter(model=model)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "shared-reply-interjection.sqlite",
        config=_config(), identities=_Identities(), router=_Router(),
        main_model=adapter, quick_recovery=adapter, transport=_Transport(), now=NOW,
        expression_reconsideration_reviewer=_CancelReviewer(),
    )
    try:
        first = await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="later-cancel-1",
            text="晚点再回我。", observed_at=NOW, trace_id="trace:later-cancel:1",
        ))
        accepted_projection = app._ledger.project()  # noqa: SLF001
        accepted_action = accepted_projection.actions[0]
        accepted_commitment = accepted_projection.commitments[0]
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1", platform_message_id="later-cancel-2",
            text="等等，不用再回刚才那句。", observed_at=NOW,
            trace_id="trace:later-cancel:2",
        ))
        cancelled = await app.drain_background_once()
        projection = app._ledger.project()  # noqa: SLF001
    finally:
        app.close()

    assert first.status == "deferred"
    assert cancelled is not None and cancelled.status == "cancelled"
    assert model.calls == 2
    action = next(item for item in projection.actions if item.action_id == accepted_action.action_id)
    commitment = next(item for item in projection.commitments
        if item.commitment_id == accepted_commitment.commitment_id)
    assert action.state == "cancelled"
    assert commitment.values.status == "released"
    assert commitment.values.settlement_reason_code == "user_withdrew"


class _ReconsiderationCancelBackgroundModel:
    """Background channel fixture answering only the reconsideration grammar."""

    model = "test-background-reconsideration"

    def __init__(self) -> None:
        self.review_calls = 0

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        system = messages[0]["content"]
        assert "not-yet-dispatched companion expression" in system, (
            "the background model was consulted by an unexpected lane in this test"
        )
        self.review_calls += 1
        return '{"disposition":"cancel"}'


@pytest.mark.asyncio
async def test_production_composes_reconsideration_reviewer_from_background_model(
    tmp_path: Path,
) -> None:
    """Without an injected reviewer, the background channel must still claim gates.

    Production previously opened durable reconsideration triggers on every
    interjection but composed no reviewer (owner stayed ``None``), so the QQ
    ledger accumulated Opened-only gates and their frozen beats never
    dispatched.  The composition root now assembles the bounded reviewer from
    the same background model that carries proactive cognition.
    """

    model = _SequenceTimingDraftChatModel(("later", "silent"))
    adapter = ChatModelDeliberationAdapter(model=model)
    background = _ReconsiderationCancelBackgroundModel()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "shared-reply-auto-reviewer.sqlite",
        config=_config(), identities=_Identities(), router=_Router(),
        main_model=adapter, quick_recovery=adapter, transport=_Transport(), now=NOW,
        proactive_model=background,
    )
    try:
        first = await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id="auto-reviewer-1",
            text="晚点再回我。", observed_at=NOW, trace_id="trace:auto-reviewer:1",
        ))
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id="auto-reviewer-2",
            text="等等，不用再回刚才那句。", observed_at=NOW,
            trace_id="trace:auto-reviewer:2",
        ))
        cancelled = await app.drain_background_once()
        projection = app._ledger.project()  # noqa: SLF001
    finally:
        app.close()

    assert first.status == "deferred"
    assert cancelled is not None and cancelled.status == "cancelled"
    assert background.review_calls == 1
    gates = [
        item for item in projection.trigger_processes
        if item.process_kind == "expression_reconsideration"
    ]
    assert gates and all(item.state == "terminal" for item in gates)
    assert all(
        item.state == "cancelled"
        for item in projection.actions
        if item.kind == "followup"
    )

@pytest.mark.asyncio
async def test_production_application_resolves_no_change_appraisal_before_the_visible_reply(
    tmp_path: Path,
) -> None:
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
        projection = app._ledger.project()  # noqa: SLF001 - selective-persistence evidence
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert background is not None
    assert background.status == "idle"
    assert background.work_status is None
    assert projection.appraisals == ()
    assert projection.affect_episodes == ()
    assert projection.relationship_states == ()


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
    # A platform delivery address is not the counterpart's domain identity.
    # QQ uses a conversation target here, while Facts and Memories are scoped
    # to the canonical user actor.
    config = replace(
        _config(),
        reply_target="conversation:test:c2c:user.1",
        counterpart_actor_ref="user:user.1",
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "world-v2-next-turn-fact-memory.sqlite",
        config=config,
        identities=_ConversationTargetIdentities(),
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
async def test_next_turn_context_replays_recent_user_and_delivered_companion_text(
    tmp_path: Path,
) -> None:
    """Conversation continuity must survive the accepted delivery boundary."""

    path = tmp_path / "world-v2-recent-dialogue.sqlite"
    first_chat = _CapturingDraftChatModel()
    reply_model = ChatModelDeliberationAdapter(model=first_chat)
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(), identities=_Identities(), router=_Router(),
        main_model=reply_model, quick_recovery=reply_model,
        transport=_DeliveredTransport(), now=NOW,
    )
    try:
        await app.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id="message:dialogue:first",
            text="我今天在路上看到一只特别亲人的橘猫。",
            observed_at=NOW, trace_id="trace:dialogue:first",
        ))
        assert (await app.drain_actions_once()).status == "settled"
    finally:
        app.close()

    second_chat = _CapturingDraftChatModel()
    second_model = ChatModelDeliberationAdapter(model=second_chat)
    restarted = build_sqlite_world_v2_turn_application(
        path=path, config=_config(), identities=_Identities(), router=_Router(),
        main_model=second_model, quick_recovery=second_model,
        transport=_DeliveredTransport(), now=NOW,
    )
    try:
        await restarted.respond(InboundTurn(
            platform="test", platform_user_id="user.1",
            platform_message_id="message:dialogue:second",
            text="你也会这样吗？",
            observed_at=NOW, trace_id="trace:dialogue:second",
        ))
    finally:
        restarted.close()

    request = json.loads(second_chat.requests[0][1]["content"])["request"]
    context = json.loads(request["model_content_json"])
    dialogue = context["slices"]["recent_dialogue"]
    values = [item["value"] for item in dialogue["items"]]
    assert any(
        item["speaker"] == "counterpart"
        and item["text"] == "我今天在路上看到一只特别亲人的橘猫。"
        for item in values
    )
    assert any(
        item["speaker"] == "companion"
        and item["text"] == "嗯，我刚刚有点飘走了。你继续说，我在听。"
        and item["delivery_state"] == "delivered"
        for item in values
    )


@pytest.mark.asyncio
async def test_significant_emotion_is_durable_and_visible_to_the_same_turn_reply(
    tmp_path: Path,
) -> None:
    path = tmp_path / "world-v2-same-turn-emotion.sqlite"
    reply_chat = _CapturingDraftChatModel()
    immediate_chat = _ImmediateEmotionChat()
    reply_model = ChatModelDeliberationAdapter(model=reply_chat)
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=immediate_chat),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:same-turn-emotion",
                text="你就是个没用的机器人，根本不值得认真说话。",
                observed_at=NOW,
                trace_id="trace:same-turn-emotion",
            )
        )
        background = await app.drain_background_once()
        projection = app._ledger.project()  # noqa: SLF001 - production vertical evidence
        observation_refs = [
            (item.event_id, item.world_revision, item.payload_hash)
            for item in projection.committed_world_event_refs
            if item.event_type == "ObservationRecorded"
        ]
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert immediate_chat.calls == 1
    assert background is not None and background.status == "idle"
    assert not any(
        item.process_kind == "interaction_appraisal" and item.state != "terminal"
        for item in projection.trigger_processes
    )
    assert len(projection.appraisals) == len(projection.affect_episodes) == 1
    assert projection.appraisals[0].hypotheses[0].meaning == "boundary_violation"
    assert [item.dimension for item in projection.affect_episodes[0].components] == [
        "hurt",
        "anger",
    ]

    assert len(reply_chat.requests) == 1
    request = json.loads(reply_chat.requests[0][1]["content"])["request"]
    context = json.loads(request["model_content_json"])
    affect_items = context["slices"]["affect_episodes"]["items"]
    expected_evidence_ref = projection.affect_episodes[0].evidence_refs[0].ref_id
    assert "same-turn-emotion" in expected_evidence_ref
    assert affect_items
    observation_event_ref = observation_refs[0][0]
    assert any(
        item["value"]["components"][0]["dimension"] == "hurt"
        and item["value"]["evidence_refs"][0]["ref_id"]
        == observation_event_ref
        # The chat-facing derivative keeps one copyable semantic source
        # token; full source bindings and hashes remain on the immutable
        # Context Capsule rather than competing with affect for attention.
        and item["source_ref"] == projection.affect_episodes[0].episode_id
        for item in affect_items
    )


@pytest.mark.asyncio
async def test_minor_interaction_does_not_create_a_durable_affect_or_reclassify_in_background(
    tmp_path: Path,
) -> None:
    path = tmp_path / "world-v2-minor-same-turn-emotion.sqlite"
    reply_model = ChatModelDeliberationAdapter(model=_DraftChatModel())
    unused_affect_chat = _NeverCalledAffectChat()
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=_NoChangeAppraisalChat()),
        affect_model=AffectDraftDeliberationAdapter(model=unused_affect_chat),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:minor-emotion",
                text="嗯，知道了。",
                observed_at=NOW,
                trace_id="trace:minor-emotion",
            )
        )
        background = await app.drain_background_once()
        projection = app._ledger.project()  # noqa: SLF001 - production vertical evidence
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert unused_affect_chat.calls == 0
    assert projection.appraisals == projection.affect_episodes == ()
    assert not any(
        item.process_kind == "interaction_appraisal" and item.state != "terminal"
        for item in projection.trigger_processes
    )
    assert background is None or background.work_status != "accepted"


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
        appraisal_model=AppraisalDraftDeliberationAdapter(model=_ImmediateEmotionChat()),
        affect_model=AffectDraftDeliberationAdapter(model=_NeverCalledAffectChat()),
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
        background = await app.drain_background_once()
    finally:
        app.close()

    assert background is None or background.work_status is None
    ledger = SQLiteWorldLedger(path=path, world_id=_config().world_id)
    try:
        episode = ledger.project().affect_episodes
    finally:
        ledger.close()
    assert len(episode) == 1
    assert episode[0].components[0].dimension == "hurt"


@pytest.mark.asyncio
async def test_significant_interaction_state_is_consumed_by_the_next_visible_turn(
    tmp_path: Path,
) -> None:
    """A noticed wound must become next-turn context, not a write-only ledger ornament."""

    path = tmp_path / "world-v2-significant-state-next-turn.sqlite"
    chat = _CapturingDraftChatModel()
    reply_model = ChatModelDeliberationAdapter(model=chat)
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=_ImmediateEmotionChat()),
        affect_model=AffectDraftDeliberationAdapter(model=_NeverCalledAffectChat()),
        relationship_model=RelationshipDraftDeliberationAdapter(
            model=_RelationshipSignalChat()
        ),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        first = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:significant-state-source",
                text="你刚才的回复让我挺失望的，感觉你没把我说的事当回事。",
                observed_at=NOW,
                trace_id="trace:significant-state-source",
            )
        )
        jobs = [await app.drain_background_once() for _ in range(4)]
        projection = app._ledger.project()  # noqa: SLF001 - vertical acceptance evidence
        second = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:significant-state-consumer",
                text="算了，继续说吧。",
                observed_at=NOW,
                trace_id="trace:significant-state-consumer",
            )
        )
    finally:
        app.close()

    assert first.status == second.status == "action_authorized"
    assert [job.work_status for job in jobs if job is not None].count("accepted") == 2
    assert projection.appraisals[0].hypotheses[0].meaning == "boundary_violation"
    assert projection.affect_episodes[0].components[0].dimension == "hurt"
    assert projection.relationship_states[0].variables.trust_bp == 240

    request = json.loads(chat.requests[1][1]["content"])["request"]
    context = json.loads(request["model_content_json"])
    affect_items = context["slices"]["affect_episodes"]["items"]
    relationship_items = context["slices"]["relationship_slice"]["items"]
    assert any(
        component["dimension"] == "hurt"
        for item in affect_items
        for component in item["value"]["components"]
    )
    assert any(
        item["value"]["variables"]["trust_bp"] == 240 for item in relationship_items
    ), relationship_items


@pytest.mark.asyncio
async def test_production_application_builds_first_relationship_from_appraisal(
    tmp_path: Path,
) -> None:
    """A first appraisal may form a source-bound state without a pre-existing head."""

    path = tmp_path / "world-v2-first-relationship.sqlite"
    reply_model = ChatModelDeliberationAdapter(model=_DraftChatModel())
    relationship_chat = _RelationshipSignalChat()
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=_AppraisingChat()),
        relationship_model=RelationshipDraftDeliberationAdapter(
            model=relationship_chat
        ),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:first-relationship",
                text="你上次答应我的事真的做到了。",
                observed_at=NOW,
                trace_id="trace:production:first-relationship",
            )
        )
        signal = await app.drain_background_once()
        adjustment = await app.drain_background_once()
        idle = await app.drain_background_once()
    finally:
        app.close()

    assert signal is not None and signal.work_status == "accepted"
    assert adjustment is not None and adjustment.work_status == "accepted"
    assert idle is not None and idle.status == "idle"
    ledger = SQLiteWorldLedger(path=path, world_id=_config().world_id)
    try:
        projection = ledger.project()
    finally:
        ledger.close()
    assert projection.relationship_signals[0].subject_ref == "user:user.1"
    assert projection.relationship_states[0].stage == "stranger"
    assert projection.relationship_states[0].variables.trust_bp == 240


@pytest.mark.asyncio
async def test_production_relationship_accumulation_authorizes_only_bounded_p3_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reach P3 through real app turns, not a hand-written relationship head."""

    path = tmp_path / "world-v2-production-p3-from-relationship.sqlite"
    config = WorldV2TurnApplicationConfig(
        world_id="world:production-p3-from-relationship",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:production-p3-from-relationship",
        event_ecology_policy=EcologyPolicy(max_candidates_per_drain=1),
        media_selection_acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(
                grant_id="grant:production-p3-media", grant_revision=1
            ),
            account_id="account:production-p3-media",
            account_window_id="window:production-p3-media",
            account_limit=5,
            amount_limit=1,
        ),
    )
    reply_model = ChatModelDeliberationAdapter(model=_DraftChatModel())
    relationship_chat = _RelationshipStrongSignalChat()
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=reply_model,
        quick_recovery=reply_model,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=_AppraisingChat()),
        relationship_model=RelationshipDraftDeliberationAdapter(
            model=relationship_chat
        ),
        media_selection_model=_SelectingMediaSelectionModel(),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    current_time = NOW
    try:
        for turn in range(1, 16):
            await app.respond(
                InboundTurn(
                    platform="test",
                    platform_user_id="user.1",
                    platform_message_id=f"message:relationship-p3:{turn}",
                    text="你这次真的把答应我的事情做好了。",
                    observed_at=current_time,
                    trace_id=f"trace:relationship-p3:{turn}",
                )
            )
            # Background scheduling intentionally interleaves user appraisals
            # with source-bound appraisals from settled world effects.  Some
            # of those have no eligible counterpart and correctly finish as
            # ``no_change``.  The production proof therefore follows durable
            # relationship authority, never an assumed three-job queue order.
            for _ in range(24):
                await app.drain_background_once()
                projection = app._ledger.project()  # type: ignore[attr-defined]
                if len(projection.relationship_adjustments) == turn:
                    break
            else:
                pytest.fail(
                    f"turn {turn} did not produce its relationship adjustment: "
                    f"signals={len(projection.relationship_signals)}, "
                    f"adjustments={len(projection.relationship_adjustments)}, "
                    f"active={[(item.process_kind, item.state) for item in projection.trigger_processes if item.state != 'terminal']}"
                )
            assert len(projection.relationship_signals) == turn
            if turn in {4, 9, 14}:
                next_time = current_time + timedelta(days=1)
                await app.tick(
                    tick_id=f"tick:relationship-p3:{turn}",
                    logical_time_from=current_time,
                    logical_time_to=next_time,
                    observed_at=next_time,
                    trace_id=f"trace:relationship-p3:tick:{turn}",
                    causation_id=f"cause:relationship-p3:{turn}",
                    correlation_id=f"correlation:relationship-p3:{turn}",
                    reason="relationship_hysteresis_fixture",
                )
                current_time = next_time

        relationship = app._ledger.project().relationship_states[0]  # type: ignore[attr-defined]
        assert relationship.subject_ref == "user:user.1"
        assert relationship.stage == "close_friend"
        assert len(relationship_chat.messages) == 15

        last_observation = "observation:test:user.1:message:relationship-p3:15"
        plan = await app.plan_activity(
            ActivityPlanCommand(
                command_id="command:relationship-p3:plan",
                world_id=config.world_id,
                source_observation_id=last_observation,
                plan_id="plan:relationship-p3",
                activity_id="activity:relationship-p3",
                activity_kind="wind_down",
                importance_bp=4_000,
                location_ref="location:home",
                participant_refs=("agent:companion",),
                privacy_class="private",
            ),
            logical_time=current_time,
            created_at=current_time,
            trace_id="trace:relationship-p3:plan",
            causation_id="cause:relationship-p3:plan",
            correlation_id="correlation:relationship-p3",
        )
        started = await app.transition_activity(
            ActivityPlanTransitionCommand(
                command_id="command:relationship-p3:start",
                world_id=config.world_id,
                source_observation_id=last_observation,
                plan_id="plan:relationship-p3",
                operation="start",
            ),
            logical_time=current_time,
            created_at=current_time,
            trace_id="trace:relationship-p3:start",
            causation_id=plan.event_ids[-1],
            correlation_id="correlation:relationship-p3",
        )
        declaration = await app.declare_recipient_scoped_image_evidence(
            RecipientScopedImageEvidenceDeclarationCommand(
                command_id="command:relationship-p3:evidence",
                source_event_ref=started.event_ids[-1],
                recipient_ref="user:user.1",
                image_evidence=RecipientScopedImageEvidenceV1(
                    visibility="private",
                    activity={
                        "evidence_visibility": "private",
                        "id": "activity:relationship-p3",
                        "kind": "wind_down",
                        "description": "在家放松",
                        "phase": "active",
                        "private_transition": True,
                    },
                    character_media=CharacterMediaEvidenceV1(
                        character_ref="agent:companion",
                        present=True,
                        capture_capabilities=("character_front_camera",),
                    ),
                ),
            ),
            logical_time=current_time,
            created_at=current_time,
            trace_id="trace:relationship-p3:evidence",
            correlation_id="correlation:relationship-p3",
        )
        candidates = await app.drain_character_media_candidates_once(
            wake_event_ref=declaration.event_ids[-1],
            logical_time=current_time,
            trace_id="trace:relationship-p3:candidates",
            correlation_id="correlation:relationship-p3",
        )
        assert len(candidates) == 1
        selection = await app.drain_media_selection_once(
            logical_time=current_time,
            trace_id="trace:relationship-p3:selection",
            correlation_id="correlation:relationship-p3",
        )
        assert selection is not None and selection.status == "proposed"
        assert selection.proposal_event_ref is not None
        monkeypatch.setattr(
            "companion_daemon.world_v2.reducers.require_provider_media_grant",
            lambda **_kwargs: object(),
        )
        accepted = await app.accept_media_selection_once(
            proposal_event_ref=selection.proposal_event_ref,
            logical_time=current_time,
            trace_id="trace:relationship-p3:acceptance",
            correlation_id="correlation:relationship-p3",
        )
        projection = app._ledger.project()  # type: ignore[attr-defined]
        frozen = app._media_payload_store.read_exact(  # type: ignore[attr-defined]
            payload_ref=projection.media_opportunities[0].event_snapshot_ref
        )
    finally:
        app.close()

    assert accepted is not None and len(accepted.event_ids) == 4
    opportunity = projection.media_opportunities[0]
    assert (
        opportunity.family,
        opportunity.privacy_ceiling,
        opportunity.media_privacy_ceiling,
        opportunity.media_lane,
        opportunity.recipient_ref,
    ) == ("character_media", "private", "intimate", "alluring_life", "user:user.1")
    assert opportunity.private_expression_basis_ref is not None
    assert opportunity.p3_authorization_digest is not None
    assert frozen is not None
    assert '"world-image-event-snapshot-v3"' in frozen.body
    assert '"relationship_media_context"' in frozen.body
