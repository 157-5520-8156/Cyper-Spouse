from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.config import Settings
from companion_daemon.delayed_trigger_catalog import load_delayed_trigger_catalog
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.proactive_action import proactive_technical_retry_states
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
