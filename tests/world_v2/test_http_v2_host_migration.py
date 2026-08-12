from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
import inspect
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

import companion_daemon.app as app_module
import companion_daemon.world_v2.semantic_chat_composition as semantic_chat_composition
from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.action_pump import ActionPumpResult
from companion_daemon.world_v2.http_capture_host import (
    HttpCaptureTransport,
    HttpCaptureResult,
    HttpV2CaptureHost,
    build_http_v2_capture_host,
)
from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.platform_host import PlatformScheduledDrainResult
from companion_daemon.world_v2.platform_action_executor import (
    MediaProviderDispatchRequest,
    PlatformDispatchReceipt,
    PlatformDispatchRequest,
)
from companion_daemon.world_v2.production_turn_application import (
    MediaPreviewDeployment,
    MediaSelectionAcceptanceComposition,
)
from companion_daemon.world_v2.schemas import ProviderMediaGrantBinding


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _ready_character_interior_health(
    *,
    parallel_conflicts: int = 0,
) -> dict[str, object]:
    return {
        "status": "ready",
        "installed": True,
        "semantic_author_count": 1,
        "legacy_interface_invocations": 0,
        "parallel_character_author_conflicts": parallel_conflicts,
        "dual_write_conflicts": 0,
        "topology_issues": [],
        "topology_evidence": {
            "duplicate_purpose_owner_count": 0,
            "legacy_compatibility_route_installed": False,
            "semantic_author_ids": ["character-semantic-author:test"],
        },
    }


def _post_with_world_v2_readiness_retry(client: TestClient, payload: dict[str, object]):
    """Retry a bounded 503 without changing the message id or payload.

    A configured ASGI app may still be building its immutable capture on the
    first request.  The route deliberately performs no ledger write in that
    state, so the safe client behavior is to retry the exact idempotent
    ingress rather than keep an HTTP connection open behind cold replay.
    """

    response = None
    for _ in range(80):
        response = client.post("/messages", json=payload)
        if response.status_code != 503:
            return response
        time.sleep(0.1)
    assert response is not None
    return response


class _DurableMediaTransport:
    """Composition fake; no image call is possible without an authorized Action."""

    provider = "media:durable-test"

    async def send(self, request: MediaProviderDispatchRequest) -> PlatformDispatchReceipt:
        raise AssertionError(f"unexpected provider call for {request.action_id}")

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        return None

    async def lookup_execution_result(
        self, *, action_id: str, idempotency_key: str, request_fingerprint: str
    ) -> None:
        return None


class _NamedNoCallModel:
    def __init__(self, model: str) -> None:
        self.model = model
        self.semantic_authority_id = f"semantic-authority:test:{model.casefold()}"

    async def complete(
        self,
        _messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        raise AssertionError(f"unexpected composition-only model call: {self.model}")


class _NamedCompactReviewNoCallModel(_NamedNoCallModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "visible-beat-source-verdict.1"


class _NoCallMediaPlanner:
    async def lookup(self, *, planning_request_id: str):  # type: ignore[no-untyped-def]
        del planning_request_id
        return None

    async def plan(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("a deployment seam must not plan without an accepted candidate")


def _inbound_character_result(
    expression_draft: dict[str, object],
    *,
    private_self: str,
) -> str:
    """Return the complete production CharacterInterior inbound wire.

    HTTP host tests exercise the strict production composition, where omission
    must not let adapter defaults choose appraisal, Affect, relationship,
    expectation, cadence, or visible-expression semantics for the character.
    These fixtures therefore make the role's no-change choices explicit while
    leaving each individual test free to select its own timing and beats.
    """

    expression = {
        "private_turn_state": {
            "inner_state_summary": private_self,
            "attended_source_refs": [],
        },
        "cadence": "conversational",
        "response_expectation": None,
        "response_expectation_assessment": None,
        "world_claims": [],
        **expression_draft,
    }
    return json.dumps(
        {
            "appraisal_draft": {
                "appraise": False,
                "affect": "no_change",
                "brief_rationale": (
                    "她把这句当作当前对话来接，没有形成需要持久化的新解释。"
                ),
                "behavior_tendency": "按自己的当下判断回应",
                "stance": "自然在场",
                "display_strategy": "直接表达",
                "confidence": 7_000,
            },
            "expression_draft": expression,
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_http_composition_wires_compact_source_guard_without_inventory(
    tmp_path: Path,
) -> None:
    author = _NamedNoCallModel("http-proactive-author")
    reviewer = _NamedCompactReviewNoCallModel("http-independent-source-reviewer")
    life_reviewer = _NamedNoCallModel("http-isolated-life-source-reviewer")
    settings = Settings(
        database_path=tmp_path / "http-proactive-source-authority.sqlite"
    )
    host = build_http_v2_capture_host(
        settings=settings,
        bootstrap_at=NOW,
        model=author,
        source_closure_model=reviewer,
        life_source_closure_model=life_reviewer,
    )
    try:
        interior_health = host.character_interior_health()
        source_health = host.proactive_source_authority_health()
        interior = host._semantic_chat.character_interior  # noqa: SLF001
        identity = interior._production_identity_frame  # noqa: SLF001

        assert settings.world_v2_expression_episode_mode == "stream"
        assert (  # noqa: SLF001
            host._host._application._turns._runtime._pinned_turn.expression_episode_mode
            == settings.world_v2_expression_episode_mode
        )
        assert interior_health["status"] == "ready"
        assert interior_health["primary_author_model"] == author.model
        assert interior_health["semantic_author_count"] == 1
        assert interior_health["primary_author_faculty"] == "structured-character-role"
        primary_route = interior_health["primary_author_route"]
        assert primary_route["name"] == "structured-character-role"
        assert primary_route["version"] == "structured-character-role.1"
        assert primary_route["model_id"] == author.model
        assert interior_health["legacy_interface_invocations"] == 0
        assert interior_health["parallel_character_author_conflicts"] == 0
        assert interior_health["dual_write_conflicts"] == 0
        assert "inbound_turn" in interior_health["purpose_faculties"]
        topology = interior_health["topology_evidence"]
        assert topology["duplicate_purpose_owner_count"] == 0
        assert topology["legacy_compatibility_route_installed"] is False
        assert topology["legacy_compatibility_route_names"] == []
        assert identity is host._semantic_chat.identity_frame  # noqa: SLF001
        assert identity.companion_name == "沈知栀"
        assert source_health["author_model"] == author.model
        assert source_health["reviewer_model"] == reviewer.model
        assert source_health["candidate_inventory_model"] is None
        assert source_health["visible_review_strategy"] == "visible_beat_verdict"
        assert source_health["active_source_review_protocol"] == (
            "visible_beat_source_verdict.1"
        )
        assert source_health["selective_source_review"]["enabled"] is True
        assert source_health["candidate_review_capabilities"]["ordinary"] == {
            "inventory_v5": False,
            "coverage_v5": False,
            "roles_independent": False,
        }
        development = (  # noqa: SLF001
            host._host._application._life_ecology._life_development_followup
        )
        assert development._world_author.authority_origin is author  # noqa: SLF001
        assert (  # noqa: SLF001
            development._world_author_source_rewriter.authority_origin is author
        )
        assert (  # noqa: SLF001
            development._source_closure_reviewer.authority_origin is life_reviewer
        )
        assert development._source_closure_reviewer_is_independent is True  # noqa: SLF001
        assert source_health["status"] == "ready"
        assert host.life_source_authority_health()["status"] == (
            "operational_unqualified"
        )
    finally:
        await host.aclose()


class _CognitiveHostModel:
    """One model serving reply plus the narrow background cognitive prompts."""

    model = "test-cognitive-host"
    supports_required_tool_choice = True

    def __init__(self) -> None:
        self.reply_requests: list[list[dict[str, str]]] = []

    async def complete_json(
        self,
        messages,
        *,
        temperature: float = 0.2,
        tools=None,
        tool_choice=None,
    ):  # type: ignore[no-untyped-def]
        if tools is None:
            return await self.complete(messages, temperature=temperature)
        del temperature
        assert tools and len(tools) == 1
        tool_name = tools[0]["function"]["name"]
        assert tool_name in {
            "character_role_fact_memory_retention_v1",
            "character_role_experience_memory_retention_v1",
        }
        assert tool_choice == {
            "type": "function",
            "function": {"name": tool_name},
        }
        # Keep one fixture semantic author.  The same role-shaped response is
        # parsed by StructuredCharacterRoleFaculty after the transport gate.
        return await self.complete(messages, temperature=0.15)

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        prompt = messages[0]["content"]
        if "Assess one verified user message" in prompt:
            return (
                '{"retain":true,"predicate_code":"preference.likes","value":"乌龙茶",'
                '"privacy_class":"personal","confidence":8600,"rationale":"explicit preference"}'
            )
        if "retrieval memory" in prompt:
            return (
                '{"retain":true,"cue_kind":"future_utility",'
                '"retention_rationales":["future_utility"],"salience":'
                '{"autobiographical_relevance_bp":6200,"relationship_relevance_bp":1800,'
                '"emotional_residue_bp":0,"unfinished_business_bp":0,"recurrence_bp":1200,'
                '"novelty_bp":2800,"future_utility_bp":7600,"world_continuity_bp":1000}}'
            )
        if "sole semantic author" in prompt:
            request = json.loads(messages[-1]["content"])
            inner_turn = request["inner_turn"]
            if inner_turn["purpose"] == "fact_memory_retention":
                return json.dumps(
                    {
                        "status": "decision",
                        "summary": (
                            "她觉得这项明确偏好以后能帮助自己自然接续对话，想保留下来。"
                        ),
                        "attended_source_refs": [],
                        "decision": {
                            "source_refs": inner_turn["subject_source_refs"],
                            "payload": {
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
                        },
                        "recall_query": None,
                        "proposals": [],
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "status": "no_change",
                    "summary": "她看过这次机会，没有形成需要落实的新选择。",
                    "attended_source_refs": [],
                    "decision": None,
                    "recall_query": None,
                    "proposals": [],
                },
                ensure_ascii=False,
            )
        if "COMBINED OUTPUT ENVELOPE" in prompt:
            self.reply_requests.append(messages)
            return _inbound_character_result(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "记下了。"}],
                    "stance": "acknowledge_briefly",
                    "brief_rationale": "她想简短接住这项明确偏好。",
                    "confidence": 7_200,
                },
                private_self="她注意到对方在分享自己的偏好，想先自然接住。",
            )
        return (
            '{"response_text":"记下了。","stance":"acknowledge_briefly",'
            '"brief_rationale":"test","confidence":7200}'
        )


class _LaterHostModel:
    model = "test-http-later"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return _inbound_character_result(
            {
                "timing_choice": "later",
                "beats": [{"modality": "text", "text": "等我忙完回来。"}],
                "delay_seconds": 60,
                "expires_after_seconds": 600,
                "stance": "defer",
                "brief_rationale": "稍后接续",
                "confidence": 7200,
            },
            private_self="她现在不想草率接话，决定明确约一个稍后的接续。",
        )


class _TwoBeatHostModel:
    model = "test-http-two-beat"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return _inbound_character_result(
            {
                "timing_choice": "now",
                "beats": [
                    {"modality": "text", "text": "等等。"},
                    {"modality": "text", "text": "我想认真听你讲。"},
                ],
                "stance": "engage",
                "brief_rationale": "Use two natural beats selected in the one draft.",
                "confidence": 7600,
            },
            private_self="她察觉对方想认真说件事，愿意停下来听完整。",
        )


class _UnavailableHttpExpressionModel:
    model = "test-http-unavailable-expression"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        return _inbound_character_result(
            {
                "timing_choice": "now",
                "beats": [{"modality": "reaction", "reaction_id": "like"}],
                "stance": "acknowledge_briefly",
                "brief_rationale": "Attempt an unavailable HTTP modality.",
                "confidence": 7_000,
            },
            private_self="她此刻只想用一个轻量反应接住这句话。",
        )


class _BlockingHttpBackgroundModel:
    model = "test-http-blocking-background"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.started.set()
        await self.release.wait()
        system = str(messages[0]["content"])
        if "already verified user Fact" in system or "Assess one verified user message" in system:
            return '{"retain":false}'
        if "immediate inner appraisal" in system:
            return '{"appraise":false,"affect":"no_change"}'
        return '{"decision":"no_change"}'


@pytest.mark.asyncio
async def test_http_production_profile_fails_closed_when_model_selects_unavailable_reaction(
    tmp_path: Path,
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-v2-no-reaction.sqlite"),
        bootstrap_at=NOW,
        model=_UnavailableHttpExpressionModel(),
        world_support_model=FakeCompanionModel(),
    )
    try:
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-no-reaction",
            text="给这句话点个表情。",
            observed_at=NOW,
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert result.status == "deferred"
    assert result.action_id is None and result.text is None
    assert projection.actions == ()


@pytest.mark.asyncio
async def test_http_multi_beat_expression_reaches_terminal_receipts_from_one_main_call(
    tmp_path: Path,
) -> None:
    model = _TwoBeatHostModel()
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "http-v2-two-beat.sqlite",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
    )
    try:
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-two-beat",
            text="我有件事想和你说。",
            observed_at=NOW,
        )
        await host.drain(max_action_units=4, max_background_units=0)
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    assert result.text == "等等。"
    assert model.calls == 1
    assert [item.state for item in projection.expression_beats[-2:]] == ["settled", "settled"]
    assert projection.expression_plans[-1].state == "completed"
    assert [item.state for item in projection.actions[-2:]] == ["delivered", "delivered"]


@pytest.mark.asyncio
async def test_http_shared_reply_audit_reaches_deferred_followup_with_one_main_call(
    tmp_path: Path,
) -> None:
    model = _LaterHostModel()
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "http-v2-shared-later.sqlite",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
    )
    try:
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-later",
            text="你先忙吧",
            observed_at=NOW,
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert result.status == "deferred" and result.action_id is None
    assert model.calls == 1
    assert len(projection.actions) == len(projection.commitments) == 1
    assert projection.actions[0].kind == "followup"


@pytest.mark.asyncio
async def test_http_capture_host_runs_one_v2_ingress_action_tick_and_duplicate_without_legacy_write(
    tmp_path: Path,
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )
    try:
        first = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-v2:1",
            text="我今天有点累。",
            observed_at=NOW,
            coalescing_metadata={"channel_id": "http-local"},
        )
        duplicate = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-v2:1",
            text="我今天有点累。",
            observed_at=NOW,
            coalescing_metadata={"channel_id": "http-local"},
        )
        tick_status = await host.tick(
            tick_id="tick:http-v2:1",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:http-v2:tick:1",
            causation_id="scheduler:http-v2:1",
            correlation_id="clock:http-v2:1",
            reason="test_scheduler",
        )
        drained = await host.drain(max_action_units=2, max_background_units=2)
    finally:
        await host.aclose()

    assert first.status == "action_authorized"
    assert first.action_id is not None
    assert first.text
    assert duplicate.action_id == first.action_id
    assert duplicate.text == first.text
    assert tick_status == "observed_only"
    assert isinstance(drained.action_statuses, tuple)
    assert isinstance(drained.background_statuses, tuple)


@pytest.mark.asyncio
async def test_http_regular_drain_does_not_hold_the_inbound_lock(tmp_path: Path) -> None:
    background = _BlockingHttpBackgroundModel()
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "http-v2-background-nonblocking.sqlite",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        bootstrap_at=NOW,
        model=_TwoBeatHostModel(),
        world_support_model=background,
    )
    drain_task: asyncio.Task[object] | None = None
    try:
        first = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-background-one",
            text="你好",
            observed_at=NOW,
        )
        drain_task = asyncio.create_task(host.drain(max_action_units=0, max_background_units=1))
        await asyncio.wait_for(background.started.wait(), timeout=2)
        started = asyncio.get_running_loop().time()
        second = await asyncio.wait_for(
            host.respond(
                platform="simulator",
                platform_user_id="geoff",
                platform_message_id="message:http-background-two",
                text="还在吗？",
                observed_at=NOW + timedelta(minutes=1),
            ),
            timeout=2,
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert first.status == second.status == "action_authorized"
        assert first.text == second.text == "等等。"
        assert elapsed < 2
        assert not drain_task.done()
    finally:
        background.release.set()
        if drain_task is not None:
            await asyncio.wait_for(drain_task, timeout=5)
        await host.aclose()


@pytest.mark.asyncio
async def test_http_capture_returns_at_first_visible_body_while_joining_settlement() -> None:
    transport = HttpCaptureTransport()

    class _SlowSettlementHost:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.settled = False

        async def inbound(self, _message):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="action_authorized",
                authorized_action_ids=("action:early-body",),
                scheduled_action_ids=(),
            )

        async def drain_action(self, action_id: str) -> ActionPumpResult:
            await transport.send(
                PlatformDispatchRequest(
                    action_id=action_id,
                    kind="reply",
                    target="user:geoff",
                    payload_ref="payload:early-body",
                    payload_hash="hash:early-body",
                    content_type="text/plain",
                    body="先把看见的这句给你。",
                    idempotency_key="idempotency:early-body",
                )
            )
            await self.release.wait()
            self.settled = True
            return ActionPumpResult(action_id=action_id, status="settled")

        def close(self) -> None:
            return None

    target = _SlowSettlementHost()
    host = HttpV2CaptureHost(host=target, transport=transport, primary_user_id="geoff")  # type: ignore[arg-type]
    response_task = asyncio.create_task(
        host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:early-body",
            text="这轮可以先返回吗？",
            observed_at=NOW,
        )
    )
    try:
        result = await asyncio.wait_for(response_task, timeout=1.0)
        assert result.text == "先把看见的这句给你。"
        assert target.settled is False
        target.release.set()
        await host.aclose()
    finally:
        if not response_task.done():
            response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)

    assert target.settled is True


@pytest.mark.asyncio
async def test_http_builder_installs_only_a_complete_media_preview_deployment(
    tmp_path: Path,
) -> None:
    deployment = MediaPreviewDeployment(
        planner=_NoCallMediaPlanner(),
        acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(
                grant_id="grant:http-preview",
                grant_revision=1,
            ),
            account_id="account:http-preview",
            account_window_id="window:http-preview",
            account_limit=3,
            amount_limit=1,
        ),
    )
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-v2-media-preview.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        media_preview=deployment,
    )
    try:
        application = host._host._application  # type: ignore[attr-defined]
        assert application._media_preview_conductor is not None  # type: ignore[attr-defined]
        result = await host.drain(max_action_units=1, max_background_units=1)
    finally:
        await host.aclose()

    assert not any(item.startswith("media-preview:") for item in result.background_statuses)


def test_real_http_asgi_factory_carries_complete_media_deployment_to_conductor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable_model(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider unavailable in composition test")

    monkeypatch.setattr(semantic_chat_composition.DeepSeekChatModel, "complete", unavailable_model)
    deployment = MediaPreviewDeployment(
        planner=_NoCallMediaPlanner(),
        acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(
                grant_id="grant:http-asgi-preview",
                grant_revision=1,
            ),
            account_id="account:http-asgi-preview",
            account_window_id="window:http-asgi-preview",
            account_limit=3,
            amount_limit=1,
        ),
    )
    configured = app_module.create_http_asgi_app(
        settings=Settings(
            database_path=tmp_path / "http-v2-asgi-media.sqlite",
            DEEPSEEK_API_KEY="composition-test-key",
        ),
        media_preview=deployment,
        media_transport=_DurableMediaTransport(),
    )

    with TestClient(configured) as client:
        response = _post_with_world_v2_readiness_retry(
            client,
            {
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:asgi-media",
                "text": "今天先聊聊天。",
                "sent_at": NOW.isoformat(),
            },
        )
        capture = configured.state.http_v2_capture
        application = capture._host._application  # type: ignore[attr-defined]
        assert application._media_preview_conductor is not None  # type: ignore[attr-defined]
        assert application._media_execution_worker is not None  # type: ignore[attr-defined]

    # The real entry constructs its configured provider model, but provider
    # failure is a legal observed-only outcome.  Media wiring must not depend
    # on fabricating a fixture reply merely to return HTTP 200.
    assert response.status_code in {200, 202}


def test_first_messages_returns_retryable_not_ready_without_ingress_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold capture never holds ingress behind replay or writes early data."""

    class _SlowCapture:
        def __init__(self) -> None:
            self.responded: list[str] = []

        async def respond(self, **kwargs):  # type: ignore[no-untyped-def]
            self.responded.append(str(kwargs["platform_message_id"]))
            return HttpCaptureResult(
                status="replied",
                action_id=None,
                text="已接住。",
                canonical_user_id="geoff",
                mood="calm",
            )

        async def aclose(self) -> None:
            return None

    capture = _SlowCapture()

    def delayed_capture(*, asgi_app, bootstrap_at=None):  # type: ignore[no-untyped-def]
        del bootstrap_at
        time.sleep(0.15)
        asgi_app.state.http_v2_capture = capture
        return capture

    monkeypatch.setattr(app_module, "_http_v2_capture", delayed_capture)
    monkeypatch.setattr(app_module, "_HTTP_V2_MESSAGE_READY_WAIT_SECONDS", 0.01)
    configured = app_module.create_http_asgi_app(
        settings=Settings(database_path=tmp_path / "http-v2-readiness.sqlite")
    )
    payload = {
        "platform": "simulator",
        "platform_user_id": "geoff",
        "message_id": "message:http-v2:readiness",
        "text": "冷启动时不要丢掉我。",
        "sent_at": NOW.isoformat(),
    }

    with TestClient(configured) as client:
        first = client.post("/messages", json=payload)
        assert first.status_code == 503
        assert first.headers["retry-after"] == "1"
        # The capture is still warming and therefore no ingress callback (and
        # consequently no ledger ObservationRecorded event) has happened.
        assert capture.responded == []

        deadline = time.monotonic() + 2
        while configured.state.http_v2_capture is None and time.monotonic() < deadline:
            time.sleep(0.01)
        second = client.post("/messages", json=payload)

    assert second.status_code == 200
    assert second.json()["text"] == "已接住。"
    assert capture.responded == ["message:http-v2:readiness"]


def test_health_starts_one_warmup_and_reports_sanitized_sticky_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build_calls = 0

    def invalid_capture(*, asgi_app, bootstrap_at=None):  # type: ignore[no-untyped-def]
        nonlocal build_calls
        del asgi_app, bootstrap_at
        build_calls += 1
        raise LedgerIntegrityError("private ledger path and semantic hash details")

    monkeypatch.setattr(app_module, "_http_v2_capture", invalid_capture)
    configured = app_module.create_http_asgi_app(
        settings=Settings(database_path=tmp_path / "http-v2-invalid-ledger.sqlite")
    )

    with TestClient(configured) as client:
        first = client.get("/health")
        deadline = time.monotonic() + 2
        current = first
        while (
            current.json().get("world_v2_capture", {}).get("status") != "failed"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            current = client.get("/health")

        assert current.status_code == 200
        assert current.json()["status"] == "degraded"
        assert current.json()["world_v2_capture"] == {
            "status": "failed",
            "failure_code": "ledger_integrity_error",
        }
        assert "private ledger path" not in current.text

        repeated = [client.get("/health").json() for _ in range(3)]

    assert build_calls == 1
    assert all(item == current.json() for item in repeated)


def test_explicit_message_retries_failed_health_warmup_and_restores_ready_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _RecoveredCapture:
        async def respond(self, **kwargs):  # type: ignore[no-untyped-def]
            return HttpCaptureResult(
                status="replied",
                action_id=None,
                text=f"接住 {kwargs['platform_message_id']}",
                canonical_user_id="geoff",
                mood="calm",
            )

        def proactive_source_authority_health(self) -> dict[str, object]:
            return {"status": "ready"}

        def character_interior_health(self) -> dict[str, object]:
            return _ready_character_interior_health()

        async def aclose(self) -> None:
            return None

    recovered = _RecoveredCapture()
    build_calls = 0

    def fail_then_recover(*, asgi_app, bootstrap_at=None):  # type: ignore[no-untyped-def]
        nonlocal build_calls
        del bootstrap_at
        build_calls += 1
        if build_calls == 1:
            raise LedgerIntegrityError("must remain internal")
        asgi_app.state.http_v2_capture = recovered
        return recovered

    monkeypatch.setattr(app_module, "_http_v2_capture", fail_then_recover)
    configured = app_module.create_http_asgi_app(
        settings=Settings(database_path=tmp_path / "http-v2-recover.sqlite")
    )

    with TestClient(configured) as client:
        client.get("/health")
        deadline = time.monotonic() + 2
        health = client.get("/health")
        while (
            health.json().get("world_v2_capture", {}).get("status") != "failed"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            health = client.get("/health")
        assert health.json()["status"] == "degraded"

        response = client.post(
            "/messages",
            json={
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:recover",
                "text": "再试一次。",
                "sent_at": NOW.isoformat(),
            },
        )
        ready = client.get("/health")

    assert response.status_code == 200
    assert response.json()["text"] == "接住 message:http-v2:recover"
    assert build_calls == 2
    assert ready.json()["status"] == "ok"
    assert ready.json()["world_v2_capture"] == {"status": "ready"}
    assert ready.json()["character_interior"]["status"] == "ready"
    assert ready.json()["proactive_source_authority"] == {"status": "ready"}
    assert ready.json()["life_source_authority"]["status"] == "unavailable"


def test_health_reports_running_warmup_without_starting_another_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ReadyCapture:
        async def aclose(self) -> None:
            return None

    build_started = threading.Event()
    release_build = threading.Event()
    build_calls = 0

    def blocked_capture(*, asgi_app, bootstrap_at=None):  # type: ignore[no-untyped-def]
        nonlocal build_calls
        del bootstrap_at
        build_calls += 1
        build_started.set()
        assert release_build.wait(timeout=2)
        capture = _ReadyCapture()
        asgi_app.state.http_v2_capture = capture
        return capture

    monkeypatch.setattr(app_module, "_http_v2_capture", blocked_capture)
    configured = app_module.create_http_asgi_app(
        settings=Settings(database_path=tmp_path / "http-v2-running.sqlite")
    )

    with TestClient(configured) as client:
        first = client.get("/health")
        assert build_started.wait(timeout=2)
        second = client.get("/health")
        assert first.json()["status"] == "degraded"
        assert second.json()["status"] == "degraded"
        assert first.json()["world_v2_capture"] == {"status": "warming"}
        assert second.json()["world_v2_capture"] == {"status": "warming"}
        assert first.json()["character_interior"] == {
            "status": "unavailable",
            "installed": False,
            "semantic_author_count": 0,
            "primary_author_model": None,
            "primary_author_route": None,
            "legacy_interface_invocations": 0,
            "parallel_character_author_conflicts": 0,
            "dual_write_conflicts": 0,
            "metrics_available": False,
            "warning": True,
            "warning_reasons": ["character_interior.composition_unavailable"],
        }
        assert build_calls == 1
        release_build.set()

        deadline = time.monotonic() + 2
        while configured.state.http_v2_capture is None and time.monotonic() < deadline:
            time.sleep(0.01)

    assert build_calls == 1


def test_health_degrades_when_character_interior_reports_parallel_author_conflict(
    tmp_path: Path,
) -> None:
    class _ConflictedCapture:
        def character_interior_health(self) -> dict[str, object]:
            return _ready_character_interior_health(parallel_conflicts=1)

        def proactive_source_authority_health(self) -> dict[str, object]:
            return {"status": "ready"}

        async def aclose(self) -> None:
            return None

    configured = app_module.create_http_asgi_app(
        settings=Settings(database_path=tmp_path / "http-v2-conflicted.sqlite")
    )
    configured.state.http_v2_capture = _ConflictedCapture()

    with TestClient(configured) as client:
        current = client.get("/health")

    assert current.status_code == 200
    assert current.json()["status"] == "degraded"
    assert current.json()["world_v2_capture"] == {"status": "ready"}
    assert current.json()["character_interior"][
        "parallel_character_author_conflicts"
    ] == 1


@pytest.mark.asyncio
async def test_http_capture_warmup_is_single_flight_and_timeout_does_not_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = app_module.create_http_asgi_app(
        settings=Settings(database_path=tmp_path / "http-v2-single-flight.sqlite")
    )
    capture = object()
    build_calls = 0

    def delayed_capture(*, asgi_app, bootstrap_at=None):  # type: ignore[no-untyped-def]
        nonlocal build_calls
        del bootstrap_at
        build_calls += 1
        time.sleep(0.1)
        asgi_app.state.http_v2_capture = capture
        return capture

    monkeypatch.setattr(app_module, "_http_v2_capture", delayed_capture)
    results = await asyncio.gather(
        app_module._http_v2_capture_async(
            asgi_app=configured,
            wait_timeout_seconds=0.01,
        ),
        app_module._http_v2_capture_async(
            asgi_app=configured,
            wait_timeout_seconds=0.01,
        ),
        return_exceptions=True,
    )

    assert all(isinstance(item, app_module.HttpV2NotReady) for item in results)
    await asyncio.sleep(0.15)
    assert build_calls == 1
    assert configured.state.http_v2_capture is capture


def test_real_http_asgi_factory_defaults_media_to_unavailable_and_rejects_partial_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable_model(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider unavailable in composition test")

    monkeypatch.setattr(semantic_chat_composition.DeepSeekChatModel, "complete", unavailable_model)

    class _UnrelatedGlobalCapture:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    unrelated = _UnrelatedGlobalCapture()
    monkeypatch.setattr(app_module, "http_v2_capture", unrelated)
    configured = app_module.create_http_asgi_app(
        settings=Settings(
            database_path=tmp_path / "http-v2-asgi-default.sqlite",
            DELIVERY_RECONCILIATION_TOKEN="isolated-http-token",
            DEEPSEEK_API_KEY="composition-test-key",
        ),
    )
    with TestClient(configured) as client:
        response = _post_with_world_v2_readiness_retry(
            client,
            {
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:asgi-default",
                "text": "默认不应启用媒体。",
                "sent_at": NOW.isoformat(),
            },
        )
        application = configured.state.http_v2_capture._host._application  # type: ignore[attr-defined]
        assert application._media_preview_conductor is None  # type: ignore[attr-defined]
        assert application._media_execution_worker is None  # type: ignore[attr-defined]
        drained = client.post(
            "/internal/world-v2/drain",
            headers={"X-World-V2-Internal-Token": "isolated-http-token"},
            json={"max_action_units": 0, "max_background_units": 0},
        )
        asset = client.get("/assets/dashboard/rooms/scene-registry.json")
    assert response.status_code in {200, 202}
    assert drained.status_code == 200
    assert asset.status_code == 200
    assert unrelated.closed is False

    with pytest.raises(ValueError, match="must be supplied together"):
        app_module.create_http_asgi_app(
            settings=Settings(database_path=tmp_path / "http-v2-asgi-partial.sqlite"),
            media_transport=_DurableMediaTransport(),
        )


@pytest.mark.asyncio
async def test_http_default_composition_retains_a_fact_and_retrieval_memory_off_reply_path(
    tmp_path: Path,
) -> None:
    model = _CognitiveHostModel()
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "http-v2-cognitive.sqlite",
            WORLD_V2_RECALL_SEMANTIC_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        bootstrap_at=NOW,
        model=model,
    )
    try:
        await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-v2:memory",
            text="我最喜欢喝乌龙茶。",
            observed_at=NOW,
        )
        drained = await host.drain(max_action_units=2, max_background_units=12)
        next_time = NOW + timedelta(seconds=1)
        await host.tick(
            tick_id="tick:http-v2:memory-consumer",
            logical_time_from=NOW,
            logical_time_to=next_time,
            observed_at=next_time,
            trace_id="trace:http-v2:memory-consumer:tick",
            causation_id="scheduler:http-v2:memory-consumer",
            correlation_id="clock:http-v2:memory-consumer",
            reason="memory-consumer-fixture",
        )
        await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:http-v2:memory-consumer",
            text="你还记得我刚才说的乌龙茶吗？",
            observed_at=next_time,
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert any("accepted" in status for status in drained.background_statuses)
    assert len(projection.facts) == 1
    assert projection.facts[0].values.predicate_code == "preference.likes"
    assert projection.facts[0].values.value_hash
    assert len(projection.memory_candidates) == 1
    assert projection.memory_candidates[0].values.status == "active"
    next_turn = json.loads(model.reply_requests[-1][1]["content"])
    snapshot = next_turn["inner_life_snapshot"]
    assert snapshot["faculties"]["selective_memory"]["availability"] == "available"
    memories = snapshot["materials"]["remembered_material"]
    assert memories[0]["source_excerpts"][0]["text"] == "我最喜欢喝乌龙茶。"
    # The canonical InnerLifeSnapshot replaces the old independently arranged
    # Capsule slices in the provider request; both must never be sent in
    # parallel as competing versions of the character's current interior.
    compact_request_context = json.loads(
        next_turn["request"]["model_content_json"]
    )
    assert "slices" not in compact_request_context
    assert "inner_life_snapshot" not in compact_request_context


def test_http_capture_host_composes_only_an_explicit_durable_media_transport(
    tmp_path: Path,
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-v2-media.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        media_transport=_DurableMediaTransport(),
    )
    try:
        # The worker exists only when composition receives a recovery-capable
        # provider; it is not constructed from the HTTP capture transport or
        # the legacy image-machine bridge.
        assert host._host._application._media_execution_worker is not None
    finally:
        host._host.close()


def test_http_messages_route_uses_the_injected_v2_capture_host_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "http-route-v2.sqlite",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="scheduler-secret"),
    )
    try:
        client = TestClient(app_module.app)
        response = client.post(
            "/messages",
            json={
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:route",
                "text": "你在吗？",
                "sent_at": NOW.isoformat(),
            },
        )
        tick = client.post(
            "/internal/world-v2/tick",
            headers={"X-World-V2-Internal-Token": "scheduler-secret"},
            json={
                "tick_id": "tick:http-v2:route",
                "logical_time_from": NOW.isoformat(),
                "logical_time_to": (NOW + timedelta(minutes=1)).isoformat(),
                "observed_at": (NOW + timedelta(minutes=1)).isoformat(),
                "trace_id": "trace:http-v2:route-tick",
                "causation_id": "scheduler:http-v2:route",
                "correlation_id": "clock:http-v2:route",
                "reason": "test_scheduler",
            },
        )
        drain = client.post(
            "/internal/world-v2/drain",
            headers={"X-World-V2-Internal-Token": "scheduler-secret"},
            json={"max_action_units": 2, "max_background_units": 2},
        )
        denied = client.post("/internal/world-v2/drain", json={})
    finally:
        asyncio.run(host.aclose())

    assert response.status_code == 200
    assert response.json()["world_action_id"].startswith("action:expression-plan:")
    assert response.json()["text"]
    assert tick.json() == {"status": "observed_only", "tick_id": "tick:http-v2:route"}
    assert drain.status_code == 200
    assert set(drain.json()) == {"action_statuses", "background_statuses"}
    assert denied.status_code == 403


def test_http_attachment_evidence_changes_reused_message_identity_into_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=tmp_path / "http-attachment-v2.sqlite",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    try:
        client = TestClient(app_module.app)
        first = client.post(
            "/messages",
            json={
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:attachment",
                "text": "看看这张图",
                "attachments": [{"kind": "image", "url": "https://example.test/a.png"}],
                "sent_at": NOW.isoformat(),
            },
        )
        changed = client.post(
            "/messages",
            json={
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:attachment",
                "text": "看看这张图",
                "attachments": [{"kind": "image", "url": "https://example.test/b.png"}],
                "sent_at": NOW.isoformat(),
            },
        )
    finally:
        asyncio.run(host.aclose())

    assert first.status_code == 200
    assert changed.status_code == 409
    assert "different content" in changed.json()["detail"]


def test_http_accepts_pure_attachment_without_fabricating_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "http-pure-attachment-v2.sqlite"
    host = build_http_v2_capture_host(
        settings=Settings(
            database_path=database,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    try:
        client = TestClient(app_module.app)
        response = client.post(
            "/messages",
            json={
                "platform": "simulator",
                "platform_user_id": "geoff",
                "message_id": "message:http-v2:pure-attachment",
                "text": "",
                "attachments": [{"kind": "image", "url": "https://example.test/pure.png"}],
                "sent_at": NOW.isoformat(),
            },
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
        observation_ref = projection.message_observations[-1]
        event_ref = next(
            item
            for item in projection.committed_world_event_refs
            if item.world_revision == observation_ref.world_revision
            and item.event_type == "ObservationRecorded"
        )
        event, _commit = host._host._application._ledger.lookup_event_commit(  # type: ignore[attr-defined]
            event_ref.event_id
        )
    finally:
        asyncio.run(host.aclose())

    assert response.status_code in {200, 202}
    observation = json.loads(event.payload_json)
    assert observation["text"] is None
    assert observation["attachment_refs"][0].startswith("attachment:http:image:sha256:")


def test_http_dashboard_room_route_is_operator_gated_and_returns_only_the_v2_public_dto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the HTTP route as a black box, not the projection adapter directly."""

    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-dashboard-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"v2 dashboard route touched legacy Engine attribute {name!r}")

    assert not hasattr(app_module, "engine")
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-operator-secret"),
    )
    try:
        client = TestClient(app_module.app)
        denied = client.get("/internal/world-v2/dashboard-room")
        response = client.get(
            "/internal/world-v2/dashboard-room",
            headers={"X-World-V2-Internal-Token": "dashboard-operator-secret"},
        )
    finally:
        asyncio.run(host.aclose())

    assert denied.status_code == 403
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"schema_version", "cursor", "projection_hash", "route"}
    assert payload["schema_version"] == "world-v2-dashboard-room.1"
    assert set(payload["cursor"]) == {"world_revision", "ledger_sequence"}
    assert set(payload["route"]) == {"scene_id", "action_id", "availability"}
    assert payload["route"] == {
        "scene_id": "unavailable",
        "action_id": "idle",
        "availability": "unavailable",
    }
    wire = str(payload)
    for forbidden in (
        "world_id",
        "semantic_hash",
        "affect",
        "participant",
        "media",
        "debug",
        "operator",
    ):
        assert forbidden not in wire


def test_http_public_room_route_is_read_only_v2_dto_without_engine_or_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Godot's public route has no archive fallback or write-on-read escape hatch."""

    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-public-room-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"public v2 room route touched legacy Engine attribute {name!r}")

    assert not hasattr(app_module, "engine")
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    try:
        response = TestClient(app_module.app).get("/world-v2/room")
    finally:
        asyncio.run(host.aclose())

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"schema_version", "cursor", "projection_hash", "route"}
    assert payload["schema_version"] == "world-v2-dashboard-room.1"
    assert set(payload["cursor"]) == {"world_revision", "ledger_sequence"}
    assert all(isinstance(value, int) and value >= 0 for value in payload["cursor"].values())
    assert payload["route"] == {
        "scene_id": "unavailable",
        "action_id": "idle",
        "availability": "unavailable",
    }
    assert len(payload["projection_hash"]) == 64


def test_http_dashboard_public_route_is_operator_gated_cacheable_and_never_reads_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = build_http_v2_capture_host(
        settings=Settings(database_path=tmp_path / "http-dashboard-public-v2.sqlite"),
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
    )

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"public dashboard route touched legacy Engine {name!r}")

    assert not hasattr(app_module, "engine")
    monkeypatch.setattr(app_module, "http_v2_capture", host)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-public-secret"),
    )
    try:
        client = TestClient(app_module.app)
        denied = client.get("/world-v2/dashboard")
        response = client.get(
            "/world-v2/dashboard",
            headers={"X-World-V2-Internal-Token": "dashboard-public-secret"},
        )
        not_modified = client.get(
            "/world-v2/dashboard",
            headers={
                "X-World-V2-Internal-Token": "dashboard-public-secret",
                "If-None-Match": response.headers["etag"],
            },
        )
    finally:
        asyncio.run(host.aclose())

    assert denied.status_code == 403
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"] == f'"{response.json()["projection_hash"]}"'
    assert not_modified.status_code == 304
    payload = response.json()
    assert set(payload) == {
        "schema_version",
        "cursor",
        "projection_hash",
        "room",
        "now",
        "agenda",
        "notices",
        "freshness",
    }
    assert payload["schema_version"] == "world-v2-dashboard.1"
    assert set(payload["cursor"]) == {"world_revision", "ledger_sequence"}
    assert set(payload["room"]) == {"scene_id", "action_id", "availability"}
    assert set(payload["now"]) == {"activity_id", "activity_label", "availability"}
    assert payload["agenda"] == []
    assert payload["notices"] == []
    assert set(payload["freshness"]) == {"observed_at", "stale_after_seconds"}
    wire = str(payload)
    for forbidden in (
        "world_id",
        "semantic_hash",
        "affect",
        "participant",
        "media",
        "debug",
        "operator",
        "plan_id",
    ):
        assert forbidden not in wire


def test_http_dashboard_public_route_never_bootstraps_or_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"cold public dashboard route touched legacy Engine {name!r}")

    def _must_not_compose(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dashboard public GET must not construct a writable World v2 host")

    assert not hasattr(app_module, "engine")
    monkeypatch.setattr(app_module, "http_v2_capture", None)
    monkeypatch.setattr(app_module, "build_http_v2_capture_host", _must_not_compose)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-public-secret"),
    )

    response = TestClient(app_module.app).get(
        "/world-v2/dashboard",
        headers={"X-World-V2-Internal-Token": "dashboard-public-secret"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "World v2 dashboard projection is unavailable until the platform host is initialized"
    )


def test_http_public_room_route_never_bootstraps_or_falls_back_to_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"public room fallback touched legacy Engine {name!r}")

    def _must_not_compose(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("public room GET must not construct a writable World v2 host")

    assert not hasattr(app_module, "engine")
    monkeypatch.setattr(app_module, "http_v2_capture", None)
    monkeypatch.setattr(app_module, "build_http_v2_capture_host", _must_not_compose)

    response = TestClient(app_module.app).get("/world-v2/room")

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "World v2 room projection is unavailable until the platform host is initialized"
    )


def test_http_dashboard_room_route_never_falls_back_to_legacy_when_v2_capture_lacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CaptureWithoutDashboard:
        async def aclose(self) -> None:
            return None

        def dashboard_room(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("World v2 dashboard capture is not configured")

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"dashboard fallback touched legacy Engine {name!r}")

    assert not hasattr(app_module, "engine")
    monkeypatch.setattr(app_module, "http_v2_capture", _CaptureWithoutDashboard())
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-operator-secret"),
    )

    response = TestClient(app_module.app).get(
        "/internal/world-v2/dashboard-room",
        headers={"X-World-V2-Internal-Token": "dashboard-operator-secret"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "World v2 dashboard capture is not configured"


def test_http_dashboard_room_route_does_not_bootstrap_a_cold_v2_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator read must not create WorldStarted or budget events on GET."""

    class _NoLegacyEngine:
        async def aclose(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"cold dashboard route touched legacy Engine {name!r}")

    def _must_not_compose(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dashboard GET must not construct a writable World v2 host")

    assert not hasattr(app_module, "engine")
    monkeypatch.setattr(app_module, "http_v2_capture", None)
    monkeypatch.setattr(app_module, "build_http_v2_capture_host", _must_not_compose)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="dashboard-operator-secret"),
    )

    response = TestClient(app_module.app).get(
        "/internal/world-v2/dashboard-room",
        headers={"X-World-V2-Internal-Token": "dashboard-operator-secret"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "World v2 dashboard capture is unavailable until the platform host is initialized"
    )


@pytest.mark.asyncio
async def test_http_capture_only_drains_the_action_authorized_by_its_own_ingress() -> None:
    class _TargetedHost:
        def __init__(self) -> None:
            self.targeted_action_ids: list[str] = []

        async def inbound(self, _message):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="action_authorized",
                authorized_action_ids=("action:new",),
                scheduled_action_ids=(),
            )

        async def drain_action(self, action_id: str) -> ActionPumpResult:
            self.targeted_action_ids.append(action_id)
            return ActionPumpResult(action_id=action_id, status="settled")

        async def drain_actions_once(self):  # type: ignore[no-untyped-def]
            raise AssertionError("HTTP ingress must not drain an unrelated world Action")

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            return None

        def close(self) -> None:
            return None

    targeted = _TargetedHost()
    host = HttpV2CaptureHost(  # type: ignore[arg-type]
        host=targeted,
        transport=HttpCaptureTransport(),
        primary_user_id="geoff",
    )
    try:
        result = await host.respond(
            platform="simulator",
            platform_user_id="geoff",
            platform_message_id="message:targeted",
            text="只应投递这一轮的 Action",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert targeted.targeted_action_ids == ["action:new"]
    assert result.action_id == "action:new"


@pytest.mark.asyncio
async def test_http_capture_prefers_platform_async_lifecycle() -> None:
    class _AsyncClosingHost:
        def __init__(self) -> None:
            self.async_closed = False
            self.sync_closed = False

        async def aclose(self) -> None:
            await asyncio.sleep(0)
            self.async_closed = True

        def close(self) -> None:
            self.sync_closed = True

    platform = _AsyncClosingHost()
    host = HttpV2CaptureHost(  # type: ignore[arg-type]
        host=platform,
        transport=HttpCaptureTransport(),
        primary_user_id="geoff",
    )

    await host.aclose()

    assert platform.async_closed is True
    assert platform.sync_closed is False


@pytest.mark.asyncio
async def test_http_asgi_lifespan_consumes_capture_shutdown_lease(tmp_path: Path) -> None:
    events: list[str] = []

    class _LeasedCapture:
        closed = False
        quiescent = False

        async def aclose(self) -> None:
            self.closed = True
            events.append("close")

        @property
        def shutdown_pending_task_count(self) -> int:
            return int(self.closed and not self.quiescent)

        async def wait_for_shutdown_quiescence(self) -> None:
            assert self.closed is True
            events.append("wait")
            self.quiescent = True

    configured = app_module.create_http_asgi_app(
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "http-v2-lifespan-lease.sqlite",
        )
    )
    capture = _LeasedCapture()
    configured.state.http_v2_capture = capture

    async with configured.router.lifespan_context(configured):
        pass

    assert events == ["close", "wait"]
    assert capture.shutdown_pending_task_count == 0


@pytest.mark.asyncio
async def test_http_asgi_lifespan_consumes_shutdown_lease_after_server_error(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class _LeasedCapture:
        async def aclose(self) -> None:
            events.append("close")

        async def wait_for_shutdown_quiescence(self) -> None:
            events.append("wait")

    configured = app_module.create_http_asgi_app(
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "http-v2-error-shutdown-lease.sqlite",
        )
    )
    configured.state.http_v2_capture = _LeasedCapture()

    with pytest.raises(RuntimeError, match="server failed"):
        async with configured.router.lifespan_context(configured):
            raise RuntimeError("server failed")

    assert events == ["close", "wait"]


@pytest.mark.asyncio
async def test_http_asgi_lifespan_joins_late_warmup_before_closing_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread_started = threading.Event()
    release_thread = threading.Event()
    cleanup_events: list[str] = []

    class _LateCapture:
        async def aclose(self) -> None:
            cleanup_events.append("close")

        async def wait_for_shutdown_quiescence(self) -> None:
            cleanup_events.append("wait")

    configured = app_module.create_http_asgi_app(
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "http-v2-late-warmup.sqlite",
        )
    )

    def delayed_build(*, asgi_app, bootstrap_at=None):
        del bootstrap_at
        thread_started.set()
        assert release_thread.wait(timeout=2)
        capture = _LateCapture()
        asgi_app.state.http_v2_capture = capture
        return capture

    monkeypatch.setattr(app_module, "_http_v2_capture", delayed_build)

    async def run_lifespan() -> None:
        async with configured.router.lifespan_context(configured):
            app_module._schedule_http_v2_warmup(asgi_app=configured)
            await asyncio.to_thread(thread_started.wait, 2)

    lifecycle = asyncio.create_task(run_lifespan())
    await asyncio.to_thread(thread_started.wait, 2)
    configured.state.http_v2_warmup_task.cancel()
    await asyncio.sleep(0)
    assert lifecycle.done() is False
    assert cleanup_events == []

    release_thread.set()
    await asyncio.wait_for(lifecycle, timeout=2)

    assert cleanup_events == ["close", "wait"]


@pytest.mark.asyncio
async def test_http_close_reports_semantic_shutdown_lease_without_blocking() -> None:
    release = asyncio.Event()

    class _AsyncClosingHost:
        async def aclose(self) -> None:
            return None

    class _SemanticDependencies:
        def __init__(self) -> None:
            self.close_called = False

        async def aclose(self) -> None:
            self.close_called = True

        @property
        def shutdown_pending_task_count(self) -> int:
            return 0 if release.is_set() else int(self.close_called)

        async def wait_for_shutdown_quiescence(self) -> None:
            await release.wait()

    semantic = _SemanticDependencies()
    host = HttpV2CaptureHost(  # type: ignore[arg-type]
        host=_AsyncClosingHost(),
        transport=HttpCaptureTransport(),
        primary_user_id="geoff",
        semantic_chat=semantic,  # type: ignore[arg-type]
    )

    await asyncio.wait_for(host.aclose(), timeout=0.2)

    assert semantic.close_called is True
    assert host.shutdown_pending_task_count == 1
    quiescence = asyncio.create_task(host.wait_for_shutdown_quiescence())
    await asyncio.sleep(0)
    assert quiescence.done() is False

    release.set()
    await asyncio.wait_for(quiescence, timeout=1)
    assert host.shutdown_pending_task_count == 0


@pytest.mark.asyncio
async def test_http_close_defers_semantic_dependencies_while_world_lease_is_open() -> None:
    release = asyncio.Event()

    class _LeasedWorld:
        async def aclose(self) -> None:
            return None

        @property
        def shutdown_pending_task_count(self) -> int:
            return 0 if release.is_set() else 1

        async def wait_for_shutdown_quiescence(self) -> None:
            await release.wait()

    class _SemanticDependencies:
        def __init__(self) -> None:
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    semantic = _SemanticDependencies()
    host = HttpV2CaptureHost(  # type: ignore[arg-type]
        host=_LeasedWorld(),
        transport=HttpCaptureTransport(),
        primary_user_id="geoff",
        semantic_chat=semantic,  # type: ignore[arg-type]
    )

    await asyncio.wait_for(host.aclose(), timeout=0.2)

    assert semantic.closed is False
    assert host.shutdown_pending_task_count == 1

    release.set()
    await asyncio.wait_for(host.wait_for_shutdown_quiescence(), timeout=1)

    assert semantic.closed is True


@pytest.mark.asyncio
async def test_http_capture_duplicate_and_cancelled_close_join_one_owner() -> None:
    world_close_started = asyncio.Event()
    release_world_close = asyncio.Event()

    class _SlowWorld:
        close_calls = 0
        finished = False

        async def aclose(self) -> None:
            self.close_calls += 1
            world_close_started.set()
            await release_world_close.wait()
            self.finished = True

    world = _SlowWorld()
    host = HttpV2CaptureHost(  # type: ignore[arg-type]
        host=world,
        transport=HttpCaptureTransport(),
        primary_user_id="geoff",
    )

    cancelled_waiter = asyncio.create_task(host.aclose())
    await world_close_started.wait()
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    duplicate_waiter = asyncio.create_task(host.aclose())
    quiescence_waiter = asyncio.create_task(host.wait_for_shutdown_quiescence())
    await asyncio.sleep(0)
    assert duplicate_waiter.done() is False
    assert quiescence_waiter.done() is False
    assert world.close_calls == 1

    release_world_close.set()
    await asyncio.gather(duplicate_waiter, quiescence_waiter)

    assert world.finished is True
    assert world.close_calls == 1
    assert host.shutdown_pending_task_count == 0


@pytest.mark.asyncio
async def test_http_scheduler_invokes_the_composition_owned_media_preview_conductor() -> None:
    class _PreviewHost:
        preview_calls = 0

        async def drain_scheduled_work(self, **kwargs):  # type: ignore[no-untyped-def]
            self.preview_calls += 1
            assert kwargs["max_action_units"] == 1
            assert kwargs["max_background_units"] == 1
            return PlatformScheduledDrainResult(
                action_units_used=1,
                background_units_used=1,
                background_statuses=("media-preview:not_renderable",),
            )

        def close(self) -> None:
            return None

    preview_host = _PreviewHost()
    host = HttpV2CaptureHost(  # type: ignore[arg-type]
        host=preview_host, transport=HttpCaptureTransport(), primary_user_id="geoff"
    )
    try:
        result = await host.drain(max_action_units=1, max_background_units=1)
    finally:
        await host.aclose()

    assert preview_host.preview_calls == 1
    assert result.background_statuses == ("media-preview:not_renderable",)


@pytest.mark.asyncio
async def test_http_capture_transport_rejects_same_key_with_a_different_payload() -> None:
    transport = HttpCaptureTransport()
    first = PlatformDispatchRequest(
        action_id="action:http:1",
        kind="reply",
        target="user:geoff",
        payload_ref="payload:http:1",
        payload_hash="sha256:" + "a" * 64,
        content_type="text/plain",
        body="第一版",
        idempotency_key="http:dispatch:1",
    )
    changed = first.model_copy(update={"body": "篡改版"})

    await transport.send(first)
    with pytest.raises(ValueError, match="conflicts with the original payload"):
        await transport.send(changed)


def test_http_migration_blackbox_does_not_grant_the_new_path_legacy_authority() -> None:
    host_path = Path(__file__).parents[2] / "src/companion_daemon/world_v2/http_capture_host.py"
    tree = ast.parse(host_path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(
        module.startswith("companion_daemon.engine")
        or module.startswith("companion_daemon.world")
        or module.startswith("companion_daemon.runtime")
        for module in imported_modules
    )

    route_source = inspect.getsource(app_module.post_message)
    forbidden = ("engine", "CompanionTurn", "QQTurnPresenter", "_handle_world_message")
    assert not any(token in route_source for token in forbidden)
