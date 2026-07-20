"""The QQ perception factory fails safe; the real pieces compose end to end.

The end-to-end case is the production analogue of
``test_perception_production_composition``: it swaps the test fakes for the
real deployment implementations (attachment archive input source, decision
adapter, durable vision transport) and fakes only the chat/vision providers.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import httpx
import pytest

from companion_daemon.config import Settings
from companion_daemon.world_v2.deliberation import ModelInput, ModelOutput, ModelRoute, RouteRequest
from companion_daemon.world_v2.perception_authority_provisioning import (
    PerceptionAuthorityProvisioner,
)
from companion_daemon.world_v2.perception_decision_adapter import QQPerceptionDecisionModel
from companion_daemon.world_v2.perception_vision_transport import (
    SQLiteDurableVisionPerceptionTransport,
)
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.qq_attachment_archive import QQAttachmentArchive
from companion_daemon.world_v2.qq_perception_deployment import (
    build_qq_perception_deployment,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
WORLD_ID = "world:qq-perception-deployment"
IMAGE_REF = "qq-attachment:image:sha256:" + "a" * 64
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"qq-perception-e2e-png"
VISION_TEXT = "照片里是一只窗台上的橘猫，午后的光线，看起来很松弛。"


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str):
        return "user:primary", "user:primary"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _NoChangeModel:
    def __init__(self) -> None:
        self.requests: list[ModelInput] = []

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.requests.append(request)
        return ModelOutput(model_id="test", model_version="test.1", raw_proposal={})


class _Quick:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        return ModelOutput(model_id="test", model_version="test.1", raw_proposal={})


class _Platform:
    provider = "platform:test"

    async def send(self, _request):
        raise AssertionError("perception e2e must not send a visible reply")

    async def lookup(self, **_kwargs):
        return None


class _LookDecision:
    def __init__(self, raw: str = '{"look": true, "attachment_index": 0, "reason": "想看"}') -> None:
        self.raw = raw
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.8) -> str:
        self.calls += 1
        return self.raw


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_path": tmp_path / "qq-perception.sqlite",
        "DEEPSEEK_API_KEY": "test-deepseek",
        "OPENAI_API_KEY": "test-openai",
        "PERCEPTION_BUDGET_LIMIT": 12,
        "ATTACHMENT_CACHE_PATH": tmp_path / "attachments",
        "PRIMARY_USER_ID": "geoff",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_factory_disables_without_prerequisites(tmp_path: Path) -> None:
    for overrides in (
        {"PERCEPTION_BUDGET_LIMIT": 0},
        {"OPENAI_API_KEY": None},
        {"DEEPSEEK_API_KEY": None},
        {},  # credentials fine, but no provisioned enforcement chain
    ):
        assert (
            build_qq_perception_deployment(
                settings=_settings(tmp_path, **overrides),
                world_id=WORLD_ID,
                api_url="http://127.0.0.1:3000",
            )
            is None
        )


@pytest.mark.asyncio
async def test_backfill_archives_attachment_bytes_even_for_deduplicated_events() -> None:
    from companion_daemon.world_v2.qq_history_backfill import (
        backfill_missed_private_messages,
    )

    archived: list[str] = []

    async def archive_event(event) -> None:
        archived.append(str(event["message_id"]))

    class _Host:
        def submission_state(self, source_event_id: str) -> str | None:
            return "committed"  # already ingested; bytes may still be missing

        async def inbound_fragment(self, fragment):  # pragma: no cover
            raise AssertionError("deduplicated events must not replay a turn")

    async def fetch_history() -> list[dict[str, object]]:
        return [
            {
                "message_id": "hist-1",
                "message_type": "private",
                "sender": {"user_id": "10001"},
                "time": NOW.timestamp(),
                "message": [
                    {"type": "image", "data": {"file": "x.jpg", "url": "https://u.invalid/x"}}
                ],
            },
            {
                "message_id": "hist-2",
                "message_type": "private",
                "sender": {"user_id": "10001"},
                "time": NOW.timestamp(),
                "message": [{"type": "text", "data": {"text": "纯文本"}}],
            },
        ]

    report = await backfill_missed_private_messages(
        host=_Host(),
        fetch_history=fetch_history,
        recipient_id="10001",
        now=NOW + timedelta(minutes=5),
        archive_event=archive_event,
    )
    assert report.deduplicated == 2
    assert archived == ["hist-1"]


async def _provisioned_world(path: Path, config: WorldV2TurnApplicationConfig) -> None:
    class _NoModel:
        async def propose(self, _request):  # pragma: no cover
            raise AssertionError("bootstrap does not deliberate")

    app = build_sqlite_world_v2_turn_application(
        path=path, config=config, identities=_Identities(), router=_Router(),
        main_model=_NoModel(), quick_recovery=_Quick(), transport=_Platform(), now=NOW,
    )
    try:
        await app.tick(
            tick_id="perception-e2e:1", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1), trace_id="trace:perception-e2e",
            causation_id="cause:perception-e2e",
            correlation_id="correlation:perception-e2e", reason="test",
        )
    finally:
        app.close()
    ledger = SQLiteWorldLedger(path=path, world_id=config.world_id)
    try:
        PerceptionAuthorityProvisioner(
            ledger=ledger, signing_key_hex="11" * 32, subject_ref="user:primary",
        ).ensure()
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_factory_composes_when_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    settings = _settings(tmp_path)
    config = WorldV2TurnApplicationConfig(
        world_id=WORLD_ID,
        companion_actor_ref="agent:companion",
        reply_target="user:primary",
        action_pump_owner="pump:qq-perception",
    )
    await _provisioned_world(Path(settings.database_path), config)
    bundle = build_qq_perception_deployment(
        settings=settings, world_id=WORLD_ID, api_url="http://127.0.0.1:3000"
    )
    assert bundle is not None
    try:
        assert bundle.budget_limit == 12
        assert bundle.transport.provider == "openai:vision"
        assert bundle.archiver.archive is bundle.input_source
        assert bundle.input_source.root == Path(settings.attachment_cache_path) / "qq-c2c-v2"
    finally:
        bundle.close()


@pytest.mark.asyncio
async def test_real_pieces_compose_into_next_turn_context_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    path = tmp_path / "perception-e2e.sqlite"
    config = WorldV2TurnApplicationConfig(
        world_id="world:perception-production-e2e",
        companion_actor_ref="agent:companion",
        reply_target="user:primary",
        action_pump_owner="pump:perception-e2e",
        perception_budget_limit=12,
    )
    await _provisioned_world(path, config)

    archive = QQAttachmentArchive(tmp_path / "attachments")
    archive.store(IMAGE_REF, PNG_BYTES)

    vision_calls = {"count": 0}

    def vision_handler(request: httpx.Request) -> httpx.Response:
        vision_calls["count"] += 1
        body = json.loads(request.content.decode())
        image_url = body["messages"][1]["content"][1]["image_url"]["url"]
        assert image_url == (
            "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode()
        )
        return httpx.Response(
            200,
            json={"id": "chatcmpl-e2e", "choices": [{"message": {"content": VISION_TEXT}}]},
        )

    transport = SQLiteDurableVisionPerceptionTransport(
        path,
        api_key="test-openai",
        base_url="https://api.openai.example/v1",
        model="gpt-4o-mini",
        transport=httpx.MockTransport(vision_handler),
    )
    decision = _LookDecision()
    adapter = QQPerceptionDecisionModel(
        model=decision,
        input_source=archive,
        dispatch_evidence=transport,
        budget_account_id="account:world-v2:perception",
        budget_limit=12,
        daily_limit=12,
        local_timezone="Asia/Shanghai",
    )
    main_model = _NoChangeModel()
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=main_model,
        quick_recovery=_Quick(),
        transport=_Platform(),
        perception_model=adapter,
        perception_input_source=archive,
        perception_transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.inbound(
            platform="test",
            platform_user_id="primary",
            platform_message_id="attachment:e2e",
            text="给你看张照片",
            observed_at=NOW + timedelta(minutes=2),
            trace_id="trace:attachment:e2e",
            attachment_refs=(IMAGE_REF,),
        )
        assert outcome.status == "observed_only"

        actions: tuple = ()
        for _ in range(8):
            await app.drain_background_once()
            actions = tuple(
                item
                for item in app._ledger.project().actions  # noqa: SLF001
                if item.layer == "perception_tool"
            )
            if actions:
                break
        assert len(actions) == 1
        assert actions[0].payload_ref == IMAGE_REF
        assert decision.calls == 1

        settled = await app.drain_actions_once()
        assert settled is not None and settled.status == "settled"
        assert vision_calls["count"] == 1

        for _ in range(8):
            await app.drain_background_once()
            result_processes = tuple(
                item
                for item in app._ledger.project().trigger_processes  # noqa: SLF001
                if item.process_kind == "perception_result_deliberation"
            )
            if result_processes and result_processes[0].state == "terminal":
                break
        assert result_processes[0].state == "terminal"

        await app.inbound(
            platform="test",
            platform_user_id="primary",
            platform_message_id="text:after",
            text="你看到了吗？",
            observed_at=NOW + timedelta(minutes=3),
            trace_id="trace:text:after",
        )
        context = json.loads(main_model.requests[-1].model_content_json)
        item = context["slices"]["perception_results"]["items"][0]["value"]
        assert item["text"] == VISION_TEXT
        assert item["epistemic_status"] == "provider_observation_not_world_fact"

        # Re-sending the exact same bytes is deduplicated by the decision
        # adapter: a new trigger opens, terminates as no-change, and the
        # provider is never called a second time.
        await app.inbound(
            platform="test",
            platform_user_id="primary",
            platform_message_id="attachment:repeat",
            text=None,
            observed_at=NOW + timedelta(minutes=4),
            trace_id="trace:attachment:repeat",
            attachment_refs=(IMAGE_REF,),
        )
        for _ in range(8):
            drained = await app.drain_background_once()
            if drained is None:
                break
        projection = app._ledger.project()  # noqa: SLF001
        perception_actions = tuple(
            item for item in projection.actions if item.layer == "perception_tool"
        )
        assert len(perception_actions) == 1
        assert vision_calls["count"] == 1
        assert decision.calls == 1
        open_perception = tuple(
            item
            for item in projection.trigger_processes
            if item.process_kind == "perception_deliberation" and item.state != "terminal"
        )
        assert open_perception == ()
    finally:
        app.close()
        transport.close()
