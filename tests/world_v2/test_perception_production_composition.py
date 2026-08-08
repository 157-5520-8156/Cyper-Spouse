from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from world_v2_application import (
    build_sqlite_world_v2_test_application,
    compose_fixture_character_interior,
)

from companion_daemon.world_v2.deliberation import ModelInput, ModelOutput, ModelRoute, RouteRequest
from companion_daemon.world_v2.perception_input_source import PerceptionInputDescriptor
from companion_daemon.world_v2.perception_result_context import PerceptionResultContent
from companion_daemon.world_v2.private_turn_state import PrivateTurnState
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
)
from companion_daemon.world_v2.proposal_envelope import DecisionProposal

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
        proposal = DecisionProposal(
            proposal_id="proposal:fixture:no-change:" + _request.call_id[-24:],
            trigger_ref=_request.trigger_ref,
            evaluated_world_revision=_request.evaluated_world_revision,
            evidence_refs=(),
            proposed_changes=(),
            action_intents=(),
            confidence=5_000,
            brief_rationale="Fixture cognition chose no visible effect.",
            behavior_tendency="observe",
            stance="silent",
            display_strategy="withhold",
            timing_choice="silent",
            private_turn_state=PrivateTurnState(
                inner_state_summary="这条只有附件，我还没有看到里面是什么；此刻不想凭空回应。",
                attended_source_refs=(),
            ),
        )
        return ModelOutput(
            model_id="test",
            model_version="test.1",
            raw_proposal=proposal.model_dump(mode="json"),
        )


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

    def dispatched_count_since(self, cutoff: datetime) -> int:
        del cutoff
        return 0

    def has_result_for_input(self, *, input_hash: str) -> bool:
        del input_hash
        return False


class _PerceptionPurposeFaculty:
    """Test-only character Faculty; production always uses the structured role."""

    name = "fixture-qq-attachment-perception"
    purposes = ("qq_attachment_perception",)

    def __init__(self, *, select: bool) -> None:
        self.select = select

    @staticmethod
    def _author_lineage(request) -> dict[str, object]:  # type: ignore[no-untyped-def]
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

    async def experience(self, request):  # pragma: no cover - consider-only
        return {
            "status": "no_change",
            "summary": "fixture no change",
            "author_lineage": self._author_lineage(request),
        }

    async def consider(self, request):
        if not self.select:
            return {
                "status": "silent",
                "summary": "fixture character chose not to inspect this time",
                "author_lineage": self._author_lineage(request),
            }
        manifest = request.capability_manifest
        assert manifest is not None
        return {
            "status": "decision",
            "summary": "fixture character chose an offered attachment",
            "author_lineage": self._author_lineage(request),
            "decision": {
                "contract": "character-interior-purpose-decision.1",
                "purpose": "qq_attachment_perception",
                "source_refs": list(manifest.source_refs),
                "capability_ref": manifest.capability_ref,
                "capability_payload_hash": manifest.payload_hash,
                "payload": {
                    "contract": ("character-interior-qq-attachment-perception-decision.1"),
                    "selected_token": manifest.payload["offered_tokens"][0],
                },
            },
        }


class _TechnicalPerceptionPurposeFaculty(_PerceptionPurposeFaculty):
    name = "fixture-technical-qq-attachment-perception"

    def __init__(self) -> None:
        super().__init__(select=False)

    async def consider(self, request):
        del request
        raise RuntimeError("fixture provider outage")


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

    def dispatched_count_since(self, cutoff: datetime) -> int:
        del cutoff
        return 0

    def has_result_for_input(self, *, input_hash: str) -> bool:
        del input_hash
        return False


class _PerceptionResultExperienceFaculty:
    """The protagonist privately experiences one accepted provider result."""

    name = "fixture-perception-result-experience"
    purposes = ("world_stimulus_appraisal",)

    def __init__(self, *, activate: bool = True) -> None:
        self.activate = activate
        self.requests = []

    @staticmethod
    def _author_lineage(request) -> dict[str, object]:  # type: ignore[no-untyped-def]
        request_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        return {
            "model_id": "fixture-perception-result-character",
            "model_version": "fixture-perception-result-character.1",
            "model_call_id": f"model-call:fixture-perception-result:{request_hash}",
            "request_hash": f"sha256:{request_hash}",
            "response_hash": "sha256:"
            + hashlib.sha256(f"fixture-result:{request_hash}".encode()).hexdigest(),
            "attempt_ordinal": request.correction_ordinal,
            "parent_model_call_id": None,
        }

    async def experience(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        manifest = request.capability_manifest
        assert manifest is not None
        result = {
            "contract": "character-interior-world-stimulus-appraisal-result.1",
            "decision": "activate" if self.activate else "no_change",
            "brief_rationale": "她按自己看到的内容形成了这一刻的感受。",
            "behavior_tendency": "先按自己的理解消化",
            "stance": "保留不确定性",
            "display_strategy": "withhold",
            "confidence": 7200,
            "meaning_candidates": (
                [{"meaning": "uncertainty", "confidence": 10_000}]
                if self.activate
                else None
            ),
            "attribution": "user" if self.activate else None,
            "severity": 3600 if self.activate else None,
            "expiry": None,
            "affect_transition": (
                {
                    "operation": "open",
                    "component_targets": [
                        {"dimension": "joy", "target_intensity_bp": 4200}
                    ],
                }
                if self.activate
                else None
            ),
            "relationship_signal": None,
            "aspiration_transition": None,
        }
        return {
            "status": "transition" if self.activate else "no_change",
            "summary": "她确实看见并在心里处理了这份结果。",
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
            "author_lineage": self._author_lineage(request),
        }

    async def consider(self, request):  # pragma: no cover - experience-only
        raise AssertionError(f"unexpected consider request: {request.purpose}")


def _config() -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:perception-production-composition",
        companion_actor_ref="agent:companion",
        reply_target="user:primary",
        action_pump_owner="pump:perception-production",
        character_memory_enabled=False,
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


def test_production_has_no_perception_result_noop_or_parallel_deliberator() -> None:
    root = Path(__file__).resolve().parents[2]
    composition = (
        root / "src/companion_daemon/world_v2/production_turn_application.py"
    ).read_text(encoding="utf-8")
    runtime = (root / "src/companion_daemon/world_v2/runtime.py").read_text(
        encoding="utf-8"
    )

    assert "NoopPerceptionResultDeliberator" not in composition
    assert "perception_result_deliberator" not in composition
    assert "perception_result_deliberator" not in runtime
    assert not (
        root / "src/companion_daemon/world_v2/perception_result_trigger_runtime.py"
    ).exists()


def test_perception_production_composition_requires_all_explicit_dependencies(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be explicitly injected together"):
        build_sqlite_world_v2_test_application(
            path=tmp_path / "partial.sqlite",
            config=_config(),
            identities=_Identities(),
            router=_Router(),
            character_interior=compose_fixture_character_interior(
                inbound_author=_NoChangeModel(),
            ),
            transport=_Platform(),
            perception_input_source=_Inputs(),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_attachment_opens_optional_perception_worker_but_text_does_not(
    tmp_path: Path,
) -> None:
    path = tmp_path / "reachable.sqlite"
    app = build_sqlite_world_v2_test_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=_NoChangeModel(),
            purpose_faculties=(_PerceptionPurposeFaculty(select=False),),
        ),
        transport=_Platform(),
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
    rebuilt = build_sqlite_world_v2_test_application(
        path=path,
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=_NoChangeModel(),
            purpose_faculties=(_PerceptionPurposeFaculty(select=False),),
        ),
        transport=_Platform(),
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
async def test_character_interior_technical_failure_stays_retryable(
    tmp_path: Path,
) -> None:
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "retryable.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=_NoChangeModel(),
            purpose_faculties=(_TechnicalPerceptionPurposeFaculty(),),
        ),
        transport=_Platform(),
        perception_input_source=_Inputs(),
        perception_transport=_PerceptionProvider(),
        now=NOW,
    )
    try:
        await app.inbound(
            platform="test",
            platform_user_id="primary",
            platform_message_id="attachment:technical",
            text=None,
            observed_at=NOW,
            trace_id="trace:attachment:technical",
            attachment_refs=("attachment:image:opaque:technical",),
        )
        first = await app.drain_background_once()
        assert first is not None and first.work_status == "technical_failure"
        process = next(
            item
            for item in app._ledger.project().trigger_processes  # noqa: SLF001
            if item.process_kind == "perception_deliberation"
        )
        assert process.state == "claimed" and len(process.attempt_ids) == 1

        projection = app._ledger.project()  # noqa: SLF001
        await app.tick(
            tick_id="perception-technical-retry:clock",
            logical_time_from=projection.logical_time or NOW,
            logical_time_to=NOW + timedelta(minutes=3),
            observed_at=NOW + timedelta(minutes=3),
            trace_id="trace:perception-technical-retry:clock",
            causation_id="cause:perception-technical-retry:clock",
            correlation_id="correlation:perception-technical-retry:clock",
            reason="test_retry_lease",
        )
        for _ in range(8):
            retried = await app.drain_background_once()
            process = next(
                item
                for item in app._ledger.project().trigger_processes  # noqa: SLF001
                if item.process_kind == "perception_deliberation"
            )
            if len(process.attempt_ids) == 2:
                break
        assert retried is not None and retried.work_status == "technical_failure"
        assert process.state == "claimed" and len(process.attempt_ids) == 2
    finally:
        app.close()


@pytest.mark.asyncio
async def test_selected_attachment_without_enforcement_authority_fails_closed(
    tmp_path: Path,
) -> None:
    provider = _DurablePerceptionProvider()
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "missing-perception-authority.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=_NoChangeModel(),
            purpose_faculties=(_PerceptionPurposeFaculty(select=True),),
        ),
        transport=_Platform(),
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
        assert (
            tuple(
                item
                for item in app._ledger.project().actions
                if item.layer == "perception_tool"  # noqa: SLF001
            )
            == ()
        )
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
    result_experience = _PerceptionResultExperienceFaculty()

    def dependencies() -> dict[str, object]:
        return {
            "identities": _Identities(),
            "router": _Router(),
            "character_interior": compose_fixture_character_interior(
                inbound_author=main_model,
                purpose_faculties=(
                    _PerceptionPurposeFaculty(select=True),
                    result_experience,
                ),
            ),
            "transport": _Platform(),
            "perception_input_source": _Inputs(),
            "perception_transport": provider,
            "now": NOW,
        }

    app = build_sqlite_world_v2_test_application(path=path, config=_config(), **dependencies())
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
    rebuilt = build_sqlite_world_v2_test_application(path=path, config=_config(), **dependencies())
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
        assert len(result_experience.requests) == 1
        request = result_experience.requests[0]
        assert request.purpose == "world_stimulus_appraisal"
        manifest = request.capability_manifest
        assert manifest is not None
        assert manifest.payload["process_kind"] == "perception_result_deliberation"
        assert manifest.payload["stimulus_kind"] == "attended_perception_result"
        perceived = manifest.payload["perception_result"]
        assert perceived["text"] == _DurablePerceptionProvider.body
        assert perceived["result_hash"] == _DurablePerceptionProvider.result_hash
        assert perceived["epistemic_status"] == "provider_observation_not_world_fact"
        assert len(rebuilt._ledger.project().appraisals) == 1  # noqa: SLF001
        assert len(rebuilt._ledger.project().affect_episodes) == 1  # noqa: SLF001
        assert any(
            item.trigger_ref == result_processes[0].source_evidence_ref
            and item.proposal_id.startswith(
                "proposal:character-interior-world-stimulus:"
            )
            for item in rebuilt._ledger.project().proposal_audits  # noqa: SLF001
        )
        await rebuilt.drain_background_once()
        assert len(result_experience.requests) == 1
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
        assert (
            len(tuple(item for item in projection.actions if item.layer == "perception_tool")) == 1
        )
    finally:
        rebuilt.close()


def test_forged_perception_result_content_is_rejected() -> None:
    with pytest.raises(ValueError, match="does not match result_hash"):
        PerceptionResultContent(
            result_ref="result:forged",
            result_hash="sha256:" + "0" * 64,
            text="model claims it saw content that the provider did not return",
        )
