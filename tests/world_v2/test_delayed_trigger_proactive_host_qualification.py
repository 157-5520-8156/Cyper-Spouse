from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.config import Settings
from companion_daemon.delayed_trigger_catalog import load_delayed_trigger_catalog
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.proactive_action import proactive_technical_retry_states
from companion_daemon.world_v2.social_initiative import post_silent_prior_trigger_id
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


NOW = datetime.now(UTC).replace(microsecond=0)
_CATALOG = Path("configs/delayed_trigger_qualification.v1.yaml")


def _host_scenario(
    scenario_id: str, nodeid: str, *, mechanism_ids: tuple[str, ...], qualification_scope: str
):
    evidence = load_delayed_trigger_catalog(_CATALOG).host_scenario(scenario_id)
    assert evidence.test_nodeid == nodeid
    assert evidence.mechanism_ids == mechanism_ids
    assert evidence.qualification_scope == qualification_scope
    assert {
        "real_provider_author_transport",
        "production_stream_expression_episode",
        "character_autonomy",
        "onebot_provider_callback_normalization",
        "24_hour_soak",
    } <= set(evidence.excluded_scope)
    return evidence


class _DeliveredQQ:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {
            "status": "ok",
            "data": {"message_id": f"qq-public-host-{len(self.sent)}"},
        }

    async def send_reaction(
        self, recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        self.sent.append((recipient_id, f"reaction:{message_id}:{reaction_id}"))
        return {
            "status": "ok",
            "data": {"message_id": f"qq-public-host-{len(self.sent)}"},
        }

    async def send_sticker(self, recipient_id: str, *, sticker_id: str) -> dict[str, object]:
        self.sent.append((recipient_id, f"sticker:{sticker_id}"))
        return {
            "status": "ok",
            "data": {"message_id": f"qq-public-host-{len(self.sent)}"},
        }

    async def send_typing(self, recipient_id: str, *, state: str) -> dict[str, object]:
        self.sent.append((recipient_id, f"typing:{state}"))
        return {
            "status": "ok",
            "data": {"message_id": f"qq-public-host-{len(self.sent)}"},
        }


class _ProactiveRoleScript:
    model = "fixture:public-host-proactive-qualification"
    supports_required_tool_choice = True

    def __init__(self, proactive_replies: tuple[dict[str, object] | str, ...]) -> None:
        self._proactive_replies = list(proactive_replies)
        self.proactive_calls = 0
        self.proactive_source_kinds: list[str] = []
        self._ordinary = FakeCompanionModel()

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        return await self._next(messages, temperature=temperature)

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> str:
        if tools is not None:
            assert len(tools) == 1
            assert tool_choice == {
                "type": "function",
                "function": {"name": "character_role_proactive_contact_v1"},
            }
        return await self._next(messages, temperature=temperature)

    async def _next(self, messages: list[dict[str, str]], *, temperature: float) -> str:
        joined = "\n".join(message["content"] for message in messages)
        if "proactive_contact" not in joined or "impulse_summary" not in joined:
            return await self._ordinary.complete(messages, temperature=temperature)
        self.proactive_calls += 1
        for source_kind in (
            "post_silent",
            "ambient_presence",
            "spontaneous_contact",
            "situation_change",
        ):
            if f'"source_kind":"{source_kind}"' in joined:
                self.proactive_source_kinds.append(source_kind)
                break
        reply = self._proactive_replies.pop(0)
        if isinstance(reply, str):
            return reply
        return json.dumps(
            {
                "status": "decision",
                "summary": "此刻想到了对方，但仍由我决定是否联系。",
                "attended_source_refs": [],
                "decision": {
                    "source_refs": [self._capability_source_ref(messages)],
                    "payload": reply,
                },
                "recall_query": None,
                "proposals": [],
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _capability_source_ref(messages: list[dict[str, str]]) -> str:
        def find(value: object) -> str | None:
            if isinstance(value, dict):
                manifest = value.get("capability_manifest")
                if isinstance(manifest, dict):
                    refs = manifest.get("source_refs")
                    if isinstance(refs, list) and refs and isinstance(refs[0], str):
                        return refs[0]
                for nested in value.values():
                    found = find(nested)
                    if found is not None:
                        return found
            elif isinstance(value, list):
                for nested in value:
                    found = find(nested)
                    if found is not None:
                        return found
            return None

        for message in messages:
            try:
                decoded = json.loads(message["content"])
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
            found = find(decoded)
            if found is not None:
                return found
        raise AssertionError("proactive role request omitted capability source refs")


def _silent() -> dict[str, object]:
    return {
        "timing_choice": "silent",
        "cadence": "conversational",
        "beats": [],
        "stance": "quietly_content",
        "brief_rationale": "此刻没有想主动说出口的话。",
        "impulse_summary": "念头停在心里，没有形成表达冲动。",
        "confidence": 7_000,
        "world_claims": [],
    }


def _proactive_action_count(host) -> int:  # type: ignore[no-untyped-def]
    return sum(
        action.kind == "proactive_message"
        for action in host.export_replay_evidence().projection.actions
    )


def _proactive_terminal_outcomes(host) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    return tuple(
        str(process.runtime_outcome_ref)
        for process in host.export_replay_evidence().projection.trigger_processes
        if process.process_kind == "proactive_action_deliberation" and process.state == "terminal"
    )


@pytest.mark.asyncio
async def test_public_host_event_driven_silence_is_effect_once(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "proactive.event-driven-silent-effect-once.1",
        request.node.nodeid,
        mechanism_ids=("proactive.event_driven",),
        qualification_scope="public_host_proactive_silent_lifecycle",
    )
    model = _ProactiveRoleScript((_silent(),))
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "proactive-public-host.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=_DeliveredQQ(),
        use_configured_recall_embedding=False,
    )
    # The production stranger cadence is a deterministic draw inside the
    # six-to-eight-hour band. Crossing the upper edge qualifies every legal
    # draw without installing a test-only scheduler policy.
    due = NOW + timedelta(hours=8, seconds=1)
    try:
        await host.inbound_text(
            message_id="message:public-host-proactive-source",
            recipient_id="10001",
            text="我先去忙一会儿。",
            observed_at=NOW,
        )
        await host.tick(
            tick_id="tick:public-host-proactive:first",
            logical_time_from=NOW,
            logical_time_to=due,
            observed_at=due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)

        assert model.proactive_calls == 1
        assert _proactive_action_count(host) == 0
        assert _proactive_terminal_outcomes(host) == ("proactive:silent",)

        await host.tick(
            tick_id="tick:public-host-proactive:first",
            logical_time_from=NOW,
            logical_time_to=due,
            observed_at=due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)

        assert model.proactive_calls == 1
        assert _proactive_action_count(host) == 0
        assert _proactive_terminal_outcomes(host) == ("proactive:silent",)
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_public_host_ambient_consideration_survives_restart_and_is_effect_once(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "proactive.ambient-restart-effect-once.1",
        request.node.nodeid,
        mechanism_ids=("proactive.ambient",),
        qualification_scope="public_host_proactive_ambient_lifecycle",
    )
    model = _ProactiveRoleScript((_silent(),))
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "proactive-public-host-ambient.sqlite",
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
    )
    delivery = _DeliveredQQ()
    due = NOW + timedelta(hours=12, seconds=1)
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    try:
        await host.inbound_text(
            message_id="message:public-host-ambient-source",
            recipient_id="10001",
            text="我先去忙一会儿。",
            observed_at=NOW,
        )
        await host.tick(
            tick_id="tick:public-host-ambient:due",
            logical_time_from=NOW,
            logical_time_to=due,
            observed_at=due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)

        assert model.proactive_calls == 1
        assert _proactive_action_count(host) == 0
        assert _proactive_terminal_outcomes(host) == ("proactive:silent",)
        projection = host.export_replay_evidence().projection
        process = next(
            item
            for item in projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        source_ref = next(
            item
            for item in projection.committed_world_event_refs
            if item.event_id == process.source_evidence_ref
        )
        assert source_ref.event_type == "ClockAdvanced"
    finally:
        await host.aclose()

    restarted = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=due,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    restarted_evidence = None
    restarted_processes: tuple[tuple[object, ...], ...] = ()
    try:
        restarted_tick_to = due + timedelta(seconds=1)
        await restarted.tick(
            tick_id="tick:public-host-ambient:restart",
            logical_time_from=due,
            logical_time_to=restarted_tick_to,
            observed_at=restarted_tick_to,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await restarted.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 1
        assert _proactive_action_count(restarted) == 0
        assert _proactive_terminal_outcomes(restarted) == ("proactive:silent",)
        restarted_evidence = restarted.export_replay_evidence()
        restarted_processes = tuple(
            (
                process.trigger_id,
                process.trigger_ref,
                process.source_evidence_ref,
                process.runtime_outcome_ref,
                process.state,
            )
            for process in restarted_evidence.projection.trigger_processes
            if process.process_kind == "proactive_action_deliberation"
        )
    finally:
        await restarted.aclose()

    assert restarted_evidence is not None
    cold = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=due + timedelta(seconds=1),
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    try:
        cold_evidence = cold.export_replay_evidence()
        assert (
            restarted_evidence.projection.semantic_hash
            == restarted_evidence.replay.semantic_hash
            == cold_evidence.projection.semantic_hash
            == cold_evidence.replay.semantic_hash
        )
        cold_processes = tuple(
            (
                process.trigger_id,
                process.trigger_ref,
                process.source_evidence_ref,
                process.runtime_outcome_ref,
                process.state,
            )
            for process in cold_evidence.projection.trigger_processes
            if process.process_kind == "proactive_action_deliberation"
        )
        assert cold_processes == restarted_processes
    finally:
        await cold.aclose()


@pytest.mark.asyncio
async def test_public_host_post_silent_reconsideration_is_effect_once(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "proactive.post-silent-reconsideration-effect-once.1",
        request.node.nodeid,
        mechanism_ids=("proactive.post_silent",),
        qualification_scope="public_host_proactive_post_silent_lifecycle",
    )
    model = _ProactiveRoleScript((_silent(), _silent()))
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "proactive-public-host-post-silent.sqlite",
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
    )
    delivery = _DeliveredQQ()
    first_due = NOW + timedelta(hours=12, seconds=1)
    second_due = first_due + timedelta(hours=8, seconds=1)

    def build(bootstrap_at: datetime):
        return build_qq_c2c_host(
            settings=settings,
            recipient_id="10001",
            bootstrap_at=bootstrap_at,
            model=model,
            world_support_model=FakeCompanionModel(),
            delivery=delivery,
            use_configured_recall_embedding=False,
        )

    host = build(NOW)
    try:
        await host.inbound_text(
            message_id="message:public-host-post-silent-source",
            recipient_id="10001",
            text="我先去忙一会儿。",
            observed_at=NOW,
        )
        await host.tick(
            tick_id="tick:public-host-post-silent:first",
            logical_time_from=NOW,
            logical_time_to=first_due,
            observed_at=first_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 1
        assert model.proactive_source_kinds == ["ambient_presence"]
        first = host.export_replay_evidence()
        first_processes = tuple(
            item
            for item in first.projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        assert len(first_processes) == 1
        assert first_processes[0].runtime_outcome_ref == "proactive:silent"

        # Rebuild after the first role-owned silence, before its next draw is
        # due. Recovery must retain the post-silent marker rather than opening
        # an ambient sibling from the same Clock event.
        await host.aclose()
        host = build(first_due)
        await host.drain(max_action_units=0, max_background_units=0)
        before_post_silent = host.export_replay_evidence()
        before_processes = tuple(
            item
            for item in before_post_silent.projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        assert before_processes == first_processes
        assert model.proactive_calls == 1

        await host.tick(
            tick_id="tick:public-host-post-silent:second",
            logical_time_from=first_due,
            logical_time_to=second_due,
            observed_at=second_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 2
        assert model.proactive_source_kinds == ["ambient_presence", "post_silent"]
        second = host.export_replay_evidence()
        second_processes = tuple(
            item
            for item in second.projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        assert len(second_processes) == 2
        assert len({item.trigger_id for item in second_processes}) == 2
        assert post_silent_prior_trigger_id(
            second_processes[-1].trigger_ref.removeprefix("proactive-consideration:")
        ) == first_processes[0].trigger_id
        post_silent_source = next(
            item
            for item in second.projection.committed_world_event_refs
            if item.event_id == second_processes[-1].source_evidence_ref
        )
        assert post_silent_source.event_type == "ClockAdvanced"
        completion_events = tuple(
            item.event
            for item in second.events
            if item.event.event_type == "TriggerProcessCompleted"
            and item.event.payload().get("runtime_outcome_ref") == "proactive:silent"
        )
        assert len(completion_events) == 2
        assert {
            event.payload().get("trigger_id") for event in completion_events
        } == {item.trigger_id for item in second_processes}
        completion_by_trigger = {
            event.payload().get("trigger_id"): event for event in completion_events
        }
        assert all(
            completion_by_trigger[item.trigger_id].causation_id == item.source_evidence_ref
            for item in second_processes
        )

        await host.tick(
            tick_id="tick:public-host-post-silent:second",
            logical_time_from=first_due,
            logical_time_to=second_due,
            observed_at=second_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        repeated = host.export_replay_evidence()
        assert model.proactive_calls == 2
        assert repeated.cursor == second.cursor
        assert repeated.projection.semantic_hash == second.projection.semantic_hash

        await host.aclose()
        host = build(second_due)
        await host.drain(max_action_units=0, max_background_units=0)
        cold = host.export_replay_evidence()
        assert model.proactive_calls == 2
        assert cold.cursor == repeated.cursor
        assert cold.projection.semantic_hash == repeated.projection.semantic_hash
        assert cold.replay.semantic_hash == repeated.replay.semantic_hash
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_public_host_post_silent_failure_retry_preserves_identity(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "proactive.post-silent-technical-retry-restart.1",
        request.node.nodeid,
        mechanism_ids=("proactive.post_silent", "proactive.technical_retry"),
        qualification_scope="public_host_post_silent_technical_retry_lifecycle",
    )
    # The post-silent attempt receives one malformed result and one malformed
    # same-role correction.  Only the later retry receives a valid silent
    # decision; no local fallback is allowed to speak for the role.
    model = _ProactiveRoleScript((_silent(), "{}", "{}", _silent()))
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "proactive-public-host-post-silent-retry.sqlite",
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
    )
    delivery = _DeliveredQQ()
    first_due = NOW + timedelta(hours=12, seconds=1)
    post_silent_due = first_due + timedelta(hours=8, seconds=1)
    retry_due = post_silent_due + timedelta(minutes=10)

    def build(bootstrap_at: datetime):
        return build_qq_c2c_host(
            settings=settings,
            recipient_id="10001",
            bootstrap_at=bootstrap_at,
            model=model,
            world_support_model=FakeCompanionModel(),
            delivery=delivery,
            use_configured_recall_embedding=False,
        )

    host = build(NOW)
    try:
        await host.inbound_text(
            message_id="message:public-host-post-silent-retry-source",
            recipient_id="10001",
            text="我先去忙一会儿。",
            observed_at=NOW,
        )
        await host.tick(
            tick_id="tick:public-host-post-silent-retry:first",
            logical_time_from=NOW,
            logical_time_to=first_due,
            observed_at=first_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 1
        assert model.proactive_source_kinds == ["ambient_presence"]
        first_projection = host.export_replay_evidence().projection
        first_process = next(
            item
            for item in first_projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
    finally:
        await host.aclose()

    host = build(first_due)
    try:
        await host.drain(max_action_units=0, max_background_units=0)
        await host.tick(
            tick_id="tick:public-host-post-silent-retry:post-silent",
            logical_time_from=first_due,
            logical_time_to=post_silent_due,
            observed_at=post_silent_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 3
        assert model.proactive_source_kinds == [
            "ambient_presence",
            "post_silent",
            "post_silent",
        ]
        failed_projection = host.export_replay_evidence().projection
        failed_process = next(
            item
            for item in failed_projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
            and str(item.runtime_outcome_ref).startswith("proactive:deliberation-failed:")
        )
        retry_state = proactive_technical_retry_states(failed_projection)
        assert len(retry_state) == 1
        retry_state = retry_state[0]
        assert retry_state.retry_ordinal == 1
        assert retry_state.next_retry_at == retry_due
        assert retry_state.trigger_ref == failed_process.trigger_ref
        assert post_silent_prior_trigger_id(
            failed_process.trigger_ref.removeprefix("proactive-consideration:")
        ) == first_process.trigger_id

        await host.aclose()
        host = build(post_silent_due)
        await host.tick(
            tick_id="tick:public-host-post-silent-retry:retry",
            logical_time_from=post_silent_due,
            logical_time_to=retry_due,
            observed_at=retry_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 4
        assert model.proactive_source_kinds[-1] == "post_silent"
        recovered = host.export_replay_evidence()
        recovered_processes = tuple(
            item
            for item in recovered.projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        assert len(recovered_processes) == 3
        assert sum(
            str(item.runtime_outcome_ref).startswith("proactive:deliberation-failed:")
            for item in recovered_processes
        ) == 1
        assert sum(item.runtime_outcome_ref == "proactive:silent" for item in recovered_processes) == 2
        assert proactive_technical_retry_states(recovered.projection) == ()
        assert _proactive_action_count(host) == 0

        cursor = recovered.cursor
        await host.tick(
            tick_id="tick:public-host-post-silent-retry:retry",
            logical_time_from=post_silent_due,
            logical_time_to=retry_due,
            observed_at=retry_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        repeated = host.export_replay_evidence()
        assert model.proactive_calls == 4
        assert repeated.cursor == cursor
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_public_host_technical_retry_survives_restart_and_is_effect_once(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "proactive.technical-retry-restart-effect-once.1",
        request.node.nodeid,
        mechanism_ids=("proactive.technical_retry",),
        qualification_scope="public_host_proactive_technical_retry_lifecycle",
    )
    model = _ProactiveRoleScript(("{}", "{}", _silent()))
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "proactive-public-host-retry.sqlite",
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
    )
    delivery = _DeliveredQQ()
    initial_due = NOW + timedelta(hours=8, seconds=1)
    retry_due = initial_due + timedelta(minutes=10)
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    try:
        await host.inbound_text(
            message_id="message:public-host-retry-source",
            recipient_id="10001",
            text="我先去忙一会儿。",
            observed_at=NOW,
        )
        await host.tick(
            tick_id="tick:public-host-retry:initial",
            logical_time_from=NOW,
            logical_time_to=initial_due,
            observed_at=initial_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)

        # One semantic failure consists of the initial malformed physical call
        # and the same-role correction call. It must not masquerade as silence.
        assert model.proactive_calls == 2
        assert _proactive_action_count(host) == 0
        failed_outcomes = _proactive_terminal_outcomes(host)
        assert len(failed_outcomes) == 1
        assert failed_outcomes[0].startswith("proactive:deliberation-failed:")
        failed_projection = host.export_replay_evidence().projection
        failed_process = next(
            item
            for item in failed_projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        retry_states = proactive_technical_retry_states(failed_projection)
        assert len(retry_states) == 1
        retry_state = retry_states[0]
        assert retry_state.retry_ordinal == 1
        assert retry_state.consecutive_technical_failures == 1
        assert retry_state.trigger_ref == failed_process.trigger_ref
        assert retry_state.source_evidence_ref == failed_process.source_evidence_ref
        assert retry_state.next_retry_at == retry_due
    finally:
        await host.aclose()

    restarted = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=initial_due,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    try:
        before_retry = retry_due - timedelta(seconds=1)
        await restarted.tick(
            tick_id="tick:public-host-retry:before-due",
            logical_time_from=initial_due,
            logical_time_to=before_retry,
            observed_at=before_retry,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await restarted.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 2

        await restarted.tick(
            tick_id="tick:public-host-retry:ordinal-1",
            logical_time_from=before_retry,
            logical_time_to=retry_due,
            observed_at=retry_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await restarted.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 3
        assert _proactive_action_count(restarted) == 0
        recovered_outcomes = _proactive_terminal_outcomes(restarted)
        assert len(recovered_outcomes) == 2
        assert (
            sum(
                outcome.startswith("proactive:deliberation-failed:")
                for outcome in recovered_outcomes
            )
            == 1
        )
        assert recovered_outcomes.count("proactive:silent") == 1
        recovered_projection = restarted.export_replay_evidence().projection
        recovered_processes = tuple(
            item
            for item in recovered_projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        assert len(recovered_processes) == 2
        assert len({item.trigger_id for item in recovered_processes}) == 2
        assert {item.trigger_ref for item in recovered_processes} == {retry_state.trigger_ref}
        assert {item.source_evidence_ref for item in recovered_processes} == {
            retry_state.source_evidence_ref
        }
        assert proactive_technical_retry_states(recovered_projection) == ()

        await restarted.tick(
            tick_id="tick:public-host-retry:ordinal-1",
            logical_time_from=before_retry,
            logical_time_to=retry_due,
            observed_at=retry_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await restarted.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 3
        assert _proactive_action_count(restarted) == 0
        assert _proactive_terminal_outcomes(restarted) == recovered_outcomes
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
async def test_public_host_new_inbound_supersedes_old_technical_retry(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "proactive.technical-retry-superseded-by-inbound.1",
        request.node.nodeid,
        mechanism_ids=("proactive.technical_retry",),
        qualification_scope="public_host_proactive_retry_supersession",
    )
    model = _ProactiveRoleScript(("{}", "{}"))
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "proactive-public-host-superseded.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=_DeliveredQQ(),
        use_configured_recall_embedding=False,
    )
    initial_due = NOW + timedelta(hours=8, seconds=1)
    superseded_at = initial_due + timedelta(minutes=1)
    old_retry_due = initial_due + timedelta(minutes=10)
    try:
        await host.inbound_text(
            message_id="message:public-host-superseded-source",
            recipient_id="10001",
            text="我先去忙一会儿。",
            observed_at=NOW,
        )
        await host.tick(
            tick_id="tick:public-host-superseded:initial",
            logical_time_from=NOW,
            logical_time_to=initial_due,
            observed_at=initial_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        assert model.proactive_calls == 2
        failed_outcomes = _proactive_terminal_outcomes(host)
        assert len(failed_outcomes) == 1
        assert failed_outcomes[0].startswith("proactive:deliberation-failed:")
        failed_projection = host.export_replay_evidence().projection
        failed_processes = tuple(
            item
            for item in failed_projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        assert len(failed_processes) == 1
        failed_process = failed_processes[0]
        source_observation = failed_projection.message_observations[0]
        source_event = next(
            item
            for item in failed_projection.committed_world_event_refs
            if item.event_type == "ObservationRecorded"
            and item.world_revision == source_observation.world_revision
        )
        assert failed_process.source_evidence_ref == source_event.event_id
        assert proactive_technical_retry_states(failed_projection)[0].retry_ordinal == 1

        await host.inbound_text(
            message_id="message:public-host-new-context",
            recipient_id="10001",
            text="我回来了，刚才又发生了一件事。",
            observed_at=superseded_at,
        )
        await host.drain(max_action_units=8, max_background_units=16)
        await host.tick(
            tick_id="tick:public-host-superseded:old-retry-due",
            logical_time_from=superseded_at,
            logical_time_to=old_retry_due,
            observed_at=old_retry_due,
            reason="virtual_public_host_qualification",
            run_life_ecology=False,
        )
        await host.drain(max_action_units=8, max_background_units=16)

        assert model.proactive_calls == 2
        assert _proactive_action_count(host) == 0
        assert _proactive_terminal_outcomes(host) == failed_outcomes
        projection = host.export_replay_evidence().projection
        proactive_processes = tuple(
            item
            for item in projection.trigger_processes
            if item.process_kind == "proactive_action_deliberation"
        )
        assert proactive_processes == failed_processes
        assert len(projection.message_observations) == 2
        latest_observation = projection.message_observations[-1]
        assert latest_observation.actor == "user:geoff"
        assert latest_observation.channel == "qq"
        failed_result_ref = failed_outcomes[0].removeprefix("proactive:deliberation-failed:")
        failed_audit = next(
            item
            for item in projection.model_result_audits
            if item.model_result_ref == failed_result_ref
        )
        # Retry authority is derived from immutable public evidence: a newer
        # inbound Observation revision invalidates the older failed attempt.
        # No synthetic "superseded" business event is needed merely to cancel
        # a timer that had not opened its next TriggerProcess yet.
        assert latest_observation.world_revision > failed_audit.evaluated_world_revision
        assert proactive_technical_retry_states(projection) == ()
    finally:
        await host.aclose()
