from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.deliberation import ModelInput, ModelOutput, ModelRoute, RouteRequest
from companion_daemon.world_v2.perception_input_source import PerceptionInputDescriptor
from companion_daemon.world_v2.perception_proposal_compiler import perception_input_ref
from companion_daemon.world_v2.perception_result_context import PerceptionResultContent
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    TypedChange,
)
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str):
        return "user:primary", "user:primary"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _NoChangeModel:
    def __init__(self) -> None:
        self.requests: list[ModelInput] = []

    async def propose(self, _request: ModelInput) -> ModelOutput:
        self.requests.append(_request)
        return ModelOutput(model_id="test", model_version="test.1", raw_proposal={})


class _Quick:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        return ModelOutput(model_id="test", model_version="test.1", raw_proposal={})


class _Platform:
    provider = "platform:test"

    async def send(self, _request):
        raise AssertionError("no-change test must not send a reply")

    async def lookup(self, **_kwargs):
        return None


class _Inputs:
    body = "durable-image-bytes"
    digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()

    def describe(self, *, attachment_ref: str, analysis_kind: str):
        return PerceptionInputDescriptor(
            attachment_ref=attachment_ref,
            analysis_kind=analysis_kind,
            content_hash=self.digest,
        )

    async def resolve(self, action):
        return action.payload_ref, action.payload_hash, self.body


class _PerceptionProvider:
    provider = "perception:test"

    async def analyze(self, **_kwargs):
        raise AssertionError("no-change model must not invoke perception provider")

    async def lookup(self, **_kwargs):
        return None

    def read_exact(self, *, result_ref: str):
        return None


class _SelectingPerceptionModel:
    async def propose(self, request: ModelInput) -> ModelOutput:
        trigger = request.trigger_message
        assert trigger is not None
        assert trigger.text is None
        assert trigger.attachment_media_types == ("image",)
        attachment_ref = trigger.attachment_refs[0]
        proposal_id = "proposal:perception:production-e2e"
        change_id = "change:perception:production-e2e"
        change = TypedChange(
            change_id=change_id,
            kind="perception_request",
            target_id="perception:vision",
            transition="request",
            evidence_refs=(trigger.observation_ref,),
            payload=CanonicalTypedPayload.from_value(
                payload_schema="perception_request.v1",
                value={
                    "analysis_kind": "vision",
                    "attachment_ref": attachment_ref,
                    "content_privacy_class": "private",
                    "budget_account_id": "account:world-v2:perception",
                    "budget_limit": 5,
                },
            ),
        )
        proposal = DecisionProposal(
            proposal_id=proposal_id,
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=request.trigger_evidence,
            proposed_changes=(change,),
            action_intents=(
                ProposalActionIntent(
                    intent_id="intent:perception:production-e2e",
                    kind="vision",
                    layer="perception_tool",
                    target="perception:vision",
                    payload_ref=perception_input_ref(
                        proposal_id=proposal_id, change_id=change_id
                    ),
                    payload_hash="sha256:"
                    + hashlib.sha256(attachment_ref.encode()).hexdigest(),
                    causal_change_id=change_id,
                ),
            ),
            confidence=8200,
            brief_rationale="Inspect the selected image if deployment authority allows it.",
            behavior_tendency="inspect_if_authorized",
            stance="curious",
            display_strategy="private",
        )
        return ModelOutput(
            model_id="test-perception",
            model_version="test.1",
            raw_proposal=proposal.model_dump(mode="json"),
        )


class _DurablePerceptionProvider:
    provider = "perception:test"
    body = '{"description":"a cat on a windowsill"}'
    result_ref = "perception-result:production-e2e"
    result_hash = "sha256:" + hashlib.sha256(body.encode()).hexdigest()

    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, **_kwargs):
        self.calls += 1
        return self.result_ref, self.result_hash, "provider:result:1", 2, NOW

    async def lookup(self, **_kwargs):
        return self.result_ref, self.result_hash, "provider:result:1", 2, NOW

    def read_exact(self, *, result_ref: str):
        if result_ref != self.result_ref:
            return None
        return PerceptionResultContent(
            result_ref=result_ref, result_hash=self.result_hash, text=self.body
        )


def _config() -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:perception-production-composition",
        companion_actor_ref="agent:companion",
        reply_target="user:primary",
        action_pump_owner="pump:perception-production",
        perception_budget_limit=5,
    )


def _copy_perception_authority(monkeypatch, *, ledger) -> None:
    """Install signed enforcement fixtures into the production SQLite ledger."""

    from perception_test_support import perception_authorized_ledger

    fixture, _binding = perception_authorized_ledger(
        monkeypatch,
        world_id=ledger.world_id,
        now=NOW,
        actor="agent:companion",
        subject="user:primary",
        analysis_kind="vision",
    )
    for reference in fixture.project().committed_world_event_refs:
        if reference.event_type == "WorldStarted":
            continue
        event, _commit = fixture.lookup_event_commit(reference.event_id)
        head = ledger.project()
        ledger.commit(
            (event,),
            expected_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
        )


def test_perception_production_composition_requires_all_explicit_dependencies(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be explicitly injected together"):
        build_sqlite_world_v2_turn_application(
            path=tmp_path / "partial.sqlite",
            config=_config(),
            identities=_Identities(),
            router=_Router(),
            main_model=_NoChangeModel(),
            quick_recovery=_Quick(),
            transport=_Platform(),
            perception_model=_NoChangeModel(),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_attachment_opens_optional_perception_worker_but_text_does_not(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reachable.sqlite"
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_NoChangeModel(),
        quick_recovery=_Quick(),
        transport=_Platform(),
        perception_model=_NoChangeModel(),
        perception_input_source=_Inputs(),
        perception_transport=_PerceptionProvider(),
        now=NOW,
    )
    try:
        await app.inbound(
            platform="test",
            platform_user_id="primary",
            platform_message_id="attachment:1",
            text=None,
            observed_at=NOW,
            trace_id="trace:attachment:1",
            attachment_refs=("attachment:image:opaque:1",),
        )
        opened = tuple(
            item
            for item in app._ledger.project().trigger_processes  # noqa: SLF001
            if item.process_kind == "perception_deliberation"
        )
        assert len(opened) == 1 and opened[0].state == "open"
        drained = await app.drain_background_once()
        assert drained is not None
        assert drained.status == "processed"
        assert drained.work_status == "no_change"

        await app.inbound(
            platform="test",
            platform_user_id="primary",
            platform_message_id="text:1",
            text="只是普通文本",
            observed_at=NOW,
            trace_id="trace:text:1",
        )
        after = tuple(
            item
            for item in app._ledger.project().trigger_processes  # noqa: SLF001
            if item.process_kind == "perception_deliberation"
        )
        assert len(after) == 1 and after[0].state == "terminal"
    finally:
        app.close()

    # The terminal decision and budget configuration are durable; rebuilding
    # the complete opt-in composition does not reopen or duplicate the trigger.
    rebuilt = build_sqlite_world_v2_turn_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_NoChangeModel(),
        quick_recovery=_Quick(),
        transport=_Platform(),
        perception_model=_NoChangeModel(),
        perception_input_source=_Inputs(),
        perception_transport=_PerceptionProvider(),
        now=NOW,
    )
    try:
        triggers = tuple(
            item
            for item in rebuilt._ledger.project().trigger_processes  # noqa: SLF001
            if item.process_kind == "perception_deliberation"
        )
        assert len(triggers) == 1 and triggers[0].state == "terminal"
    finally:
        rebuilt.close()


@pytest.mark.asyncio
async def test_selected_attachment_without_enforcement_authority_fails_closed(
    tmp_path: Path,
) -> None:
    provider = _DurablePerceptionProvider()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "missing-perception-authority.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=_NoChangeModel(),
        quick_recovery=_Quick(),
        transport=_Platform(),
        perception_model=_SelectingPerceptionModel(),
        perception_input_source=_Inputs(),
        perception_transport=provider,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="test",
            platform_user_id="primary",
            platform_message_id="attachment:no-auth",
            text=None,
            observed_at=NOW,
            trace_id="trace:attachment:no-auth",
            attachment_refs=("attachment:image:opaque:no-auth",),
        )
        drained = await app.drain_background_once()
        assert drained is not None and drained.work_status == "rejected"
        assert tuple(
            item for item in app._ledger.project().actions if item.layer == "perception_tool"  # noqa: SLF001
        ) == ()
        assert provider.calls == 0
    finally:
        app.close()


@pytest.mark.asyncio
async def test_sqlite_attachment_reaches_provider_and_next_turn_context_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "perception-e2e.sqlite"
    main_model = _NoChangeModel()
    provider = _DurablePerceptionProvider()
    dependencies = dict(
        identities=_Identities(),
        router=_Router(),
        main_model=main_model,
        quick_recovery=_Quick(),
        transport=_Platform(),
        perception_model=_SelectingPerceptionModel(),
        perception_input_source=_Inputs(),
        perception_transport=provider,
        now=NOW,
    )
    app = build_sqlite_world_v2_turn_application(
        path=path, config=_config(), **dependencies
    )
    _copy_perception_authority(monkeypatch, ledger=app._ledger)  # noqa: SLF001
    try:
        outcome = await app.inbound(
            platform="test",
            platform_user_id="primary",
            platform_message_id="attachment:e2e",
            text=None,
            observed_at=NOW,
            trace_id="trace:attachment:e2e",
            attachment_refs=("attachment:image:opaque:e2e",),
        )
        assert outcome.status == "observed_only"

        raced = await asyncio.gather(
            app.drain_background_once(), app.drain_background_once(), return_exceptions=True
        )
        assert not tuple(item for item in raced if isinstance(item, BaseException))
        projection = app._ledger.project()  # noqa: SLF001
        perception_actions = tuple(
            item for item in projection.actions if item.layer == "perception_tool"
        )
        assert len(perception_actions) == 1
        assert perception_actions[0].payload_ref == "attachment:image:opaque:e2e"

        settled = await app.drain_actions_once()
        assert settled is not None and settled.status == "settled"
        assert provider.calls == 1
        assert len(app._ledger.project().perception_results) == 1  # noqa: SLF001
    finally:
        app.close()

    # Restart between provider settlement and result-trigger consumption. The
    # result remains exactly-once and becomes source-bound Context next turn.
    rebuilt = build_sqlite_world_v2_turn_application(
        path=path, config=_config(), **dependencies
    )
    try:
        for _ in range(8):
            await rebuilt.drain_background_once()
            result_processes = tuple(
                item
                for item in rebuilt._ledger.project().trigger_processes  # noqa: SLF001
                if item.process_kind == "perception_result_deliberation"
            )
            if result_processes and result_processes[0].state == "terminal":
                break
        assert result_processes[0].state == "terminal"
        assert (await rebuilt.drain_actions_once()).status == "idle"
        assert provider.calls == 1

        await rebuilt.inbound(
            platform="test",
            platform_user_id="primary",
            platform_message_id="text:after-perception",
            text="你看到了吗？",
            observed_at=NOW,
            trace_id="trace:text:after-perception",
        )
        next_turn = main_model.requests[-1]
        model_context = json.loads(next_turn.model_content_json)
        result_value = model_context["slices"]["perception_results"]["items"][0]["value"]
        assert result_value["text"] == _DurablePerceptionProvider.body
        assert result_value["epistemic_status"] == "provider_observation_not_world_fact"
        projection = rebuilt._ledger.project()  # noqa: SLF001
        assert len(projection.perception_results) == 1
        assert len(
            tuple(item for item in projection.actions if item.layer == "perception_tool")
        ) == 1
    finally:
        rebuilt.close()


def test_forged_perception_result_content_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not match result_hash"):
        PerceptionResultContent(
            result_ref="result:forged",
            result_hash="sha256:" + "0" * 64,
            text="model claims it saw content that the provider did not return",
        )
