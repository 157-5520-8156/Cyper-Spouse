"""The QQ perception factory fails safe; CharacterInterior composes end to end.

The end-to-end case is the production analogue of
``test_perception_production_composition``: it swaps the test fakes for the
real attachment archive and durable vision transport while injecting one
fixture CharacterInterior purpose faculty. No separate perception author is
constructed.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from world_v2_application import (
    build_sqlite_world_v2_test_application,
    compose_fixture_character_interior,
)

from companion_daemon.config import Settings
from companion_daemon.world_v2.deliberation import ModelInput, ModelOutput, ModelRoute, RouteRequest
from companion_daemon.world_v2.perception_authority_provisioning import (
    PerceptionAuthorityProvisioner,
)
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


class _Platform:
    provider = "platform:test"

    async def send(self, _request):
        raise AssertionError("perception e2e must not send a visible reply")

    async def lookup(self, **_kwargs):
        return None


class _PerceptionFaculty:
    name = "fixture-qq-attachment-perception"
    purposes = ("qq_attachment_perception",)

    def __init__(self) -> None:
        self.calls = 0

    @staticmethod
    def _author_lineage(request) -> dict[str, object]:
        request_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        return {
            "model_id": "fixture-perception-character",
            "model_version": "fixture-perception-character.1",
            "model_call_id": f"model-call:fixture-perception:{request_hash}",
            "request_hash": f"sha256:{request_hash}",
            "response_hash": "sha256:"
            + hashlib.sha256(f"fixture-response:{request_hash}".encode()).hexdigest(),
            "attempt_ordinal": request.correction_ordinal,
            "parent_model_call_id": None,
        }

    async def experience(self, request):  # pragma: no cover - consider-only purpose
        return {
            "status": "no_change",
            "summary": "fixture did not experience a capability",
            "author_lineage": self._author_lineage(request),
        }

    async def consider(self, request):
        self.calls += 1
        manifest = request.capability_manifest
        assert manifest is not None
        token = manifest.payload["offered_tokens"][0]
        return {
            "status": "decision",
            "summary": "fixture character chose one offered attachment",
            "author_lineage": self._author_lineage(request),
            "decision": {
                "contract": "character-interior-purpose-decision.1",
                "purpose": "qq_attachment_perception",
                "source_refs": list(manifest.source_refs),
                "capability_ref": manifest.capability_ref,
                "capability_payload_hash": manifest.payload_hash,
                "payload": {
                    "contract": (
                        "character-interior-qq-attachment-perception-decision.1"
                    ),
                    "selected_token": token,
                },
            },
        }


class _PerceptionResultExperienceFaculty:
    name = "fixture-perception-result-experience"
    purposes = ("world_stimulus_appraisal",)

    def __init__(self) -> None:
        self.calls = 0

    async def experience(self, request):
        self.calls += 1
        manifest = request.capability_manifest
        assert manifest is not None
        result = {
            "contract": "character-interior-world-stimulus-appraisal-result.1",
            "decision": "no_change",
            "brief_rationale": "她看见了结果，但没有形成新的稳定变化。",
            "behavior_tendency": "照常继续",
            "stance": "保留不确定性",
            "display_strategy": "withhold",
            "confidence": 7200,
            "meaning_candidates": None,
            "attribution": None,
            "severity": None,
            "expiry": None,
            "affect_transition": None,
            "relationship_signal": None,
            "aspiration_transition": None,
        }
        return {
            "status": "no_change",
            "summary": "fixture character privately considered the provider result",
            "attended_source_refs": manifest.source_refs,
            "proposals": (
                {
                    "contract": "character-interior-typed-proposal.1",
                    "proposal_type": "world_stimulus_appraisal_result",
                    "purpose": "world_stimulus_appraisal",
                    "source_refs": list(manifest.source_refs),
                    "capability_ref": manifest.capability_ref,
                    "capability_payload_hash": manifest.payload_hash,
                    "payload": result,
                },
            ),
            "author_lineage": _PerceptionFaculty._author_lineage(request),
        }

    async def consider(self, request):  # pragma: no cover - experience-only faculty
        raise AssertionError(f"unexpected consider request: {request.purpose}")


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

    app = build_sqlite_world_v2_test_application(
        path=path,
        config=config,
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=_NoModel(),
        ),
        transport=_Platform(),
        now=NOW,
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
        settings=_settings(tmp_path, DEEPSEEK_API_KEY=None),
        world_id=WORLD_ID,
        api_url="http://127.0.0.1:3000",
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
    decision = _PerceptionFaculty()
    result_experience = _PerceptionResultExperienceFaculty()
    main_model = _NoChangeModel()
    interior = compose_fixture_character_interior(
        inbound_author=main_model,
        purpose_faculties=(decision, result_experience),
    )
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=config,
        identities=_Identities(),
        router=_Router(),
        character_interior=interior,
        transport=_Platform(),
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
        assert outcome.status == "deferred"

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
        assert result_experience.calls == 1

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
        assert result_experience.calls == 1
        open_perception = tuple(
            item
            for item in projection.trigger_processes
            if item.process_kind == "perception_deliberation" and item.state != "terminal"
        )
        assert open_perception == ()
    finally:
        app.close()
        transport.close()
