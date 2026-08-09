from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

import pytest

from companion_daemon.config import Settings
from companion_daemon.delayed_trigger_catalog import load_delayed_trigger_catalog
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


_CATALOG = Path("configs/delayed_trigger_qualification.v1.yaml")


def _host_scenario(
    scenario_id: str,
    nodeid: str,
    *,
    mechanism_ids: tuple[str, ...],
) -> None:
    evidence = load_delayed_trigger_catalog(_CATALOG).host_scenario(scenario_id)
    assert evidence.test_nodeid == nodeid
    assert evidence.mechanism_ids == mechanism_ids
    assert evidence.qualification_scope == "public_host_scripted_typed_role_lifecycle"
    assert {
        "real_provider_author_transport",
        "production_stream_expression_episode",
        "character_autonomy",
        "onebot_provider_callback_normalization",
        "24_hour_soak",
    } <= set(evidence.excluded_scope)


SCENARIO_EVIDENCE_SCOPE = {
    "qualification_layer": "public_host_scenario_evidence",
    "mechanisms": ("affect.decay", "relationship.silence_aftermath"),
    "public_seams": (
        "QQC2CHost.inbound_text",
        "QQC2CHost.tick",
        "QQC2CHost.drain",
        "QQC2CHost.export_replay_evidence",
        "QQC2CHost.aclose",
    ),
    "semantic_author_scope": "scripted_typed_role_contract_only",
    "excluded_scope": "real_provider_quality_and_autonomy",
}


def _json_objects(messages: list[dict[str, str]]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for message in messages:
        try:
            value = json.loads(message["content"])
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _find_capability(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        manifest = value.get("capability_manifest")
        if isinstance(manifest, dict):
            return manifest
        for child in value.values():
            found = _find_capability(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_capability(child)
            if found is not None:
                return found
    return None


def _observation_ref(messages: list[dict[str, str]]) -> str | None:
    for value in _json_objects(messages):
        trigger = value.get("current_trigger_message")
        if isinstance(trigger, dict) and isinstance(trigger.get("observation_ref"), str):
            return trigger["observation_ref"]
    return None


class _QualifiedRoleModel:
    model = "fixture:affect-silence-public-host"
    supports_required_tool_choice = True

    def __init__(self, *, inbound_affect: bool, silence_choice: str = "no_change") -> None:
        self.inbound_affect = inbound_affect
        self.silence_choice = silence_choice
        self.silence_calls = 0

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        manifest = next(
            (
                found
                for value in _json_objects(messages)
                if (found := _find_capability(value)) is not None
            ),
            None,
        )
        payload = manifest.get("payload") if isinstance(manifest, dict) else None
        if isinstance(payload, dict) and payload.get("process_kind") == "silence_appraisal":
            self.silence_calls += 1
            if self.silence_choice == "technical_failure" and self.silence_calls == 1:
                raise ConnectionError("scripted provider unavailable")
            source = payload["source_event"]["event_id"]
            proposal: dict[str, object] = {
                "proposal_type": "world_stimulus_appraisal_result",
                "decision": "activate" if self.silence_choice == "open_affect" else "no_change",
                "brief_rationale": "她按自己的感受理解这段没有回应的时间。",
                "behavior_tendency": "先在心里消化",
                "stance": "self_directed",
                "display_strategy": "withhold",
                "confidence": 7200,
            }
            if self.silence_choice == "open_affect":
                proposal.update(
                    {
                        "meaning_candidates": [
                            {"meaning": "这段安静让她有一点失落", "confidence": 7200}
                        ],
                        "attribution": "situation",
                        "severity": 4200,
                        "expiry": None,
                        "affect_transition": {
                            "operation": "open",
                            "component_targets": [
                                {"dimension": "sadness", "target_intensity_bp": 4200}
                            ],
                        },
                    }
                )
            return json.dumps(
                {
                    "status": (
                        "transition" if self.silence_choice == "open_affect" else "no_change"
                    ),
                    "summary": "她形成了自己的判断。",
                    "attended_source_refs": [source],
                    "decision": None,
                    "recall_query": None,
                    "proposals": [proposal],
                },
                ensure_ascii=False,
            )

        combined = "COMBINED OUTPUT ENVELOPE" in messages[0]["content"]
        observation_ref = _observation_ref(messages) if combined else None
        expression = {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "我想按自己的感受回应这一刻。",
                "attended_source_refs": [observation_ref] if observation_ref is not None else [],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我收到啦。"}],
            "stance": "self_directed",
            "brief_rationale": "This is the role's own typed choice.",
            "confidence": 7600,
            "world_claims": [],
        }
        if not combined:
            return json.dumps(expression, ensure_ascii=False)
        appraisal: dict[str, object] = {
            "appraise": self.inbound_affect,
            "affect": "open" if self.inbound_affect else "no_change",
            "brief_rationale": "这句话确实触动了她。" if self.inbound_affect else "没有持久变化。",
            "behavior_tendency": "sit_with_feeling",
            "stance": "self_directed",
            "display_strategy": "withhold",
            "confidence": 8200,
        }
        if self.inbound_affect:
            appraisal.update(
                {
                    "meanings": [{"meaning": "这句话让她感到受伤", "confidence": 8200}],
                    "attribution": "user",
                    "severity": 6200,
                    "components": [{"dimension": "hurt", "target_intensity_bp": 6200}],
                }
            )
        return json.dumps(
            {"AppraisalDraft": appraisal, "ExpressionDraft": expression},
            ensure_ascii=False,
        )

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: object | None = None,
        tool_choice: object | None = None,
    ) -> str:
        if tools is not None:
            assert tool_choice == {
                "type": "function",
                "function": {"name": "character_role_world_stimulus_appraisal_v1"},
            }
        return await self.complete(messages, temperature=temperature)


class _ProductionDeliveryInterceptor:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.messages: dict[str, str] = {}
        self.lookups: list[str] = []

    async def send_text(self, _recipient_id: str, text: str) -> dict[str, object]:
        message_id = f"message:affect-silence:{len(self.texts) + 1}"
        self.texts.append(text)
        self.messages[message_id] = text
        return {"status": "ok", "data": {"message_id": message_id}}

    async def send_reaction(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "failed"}

    async def send_sticker(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "failed"}

    async def send_typing(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"status": "ok", "data": {"message_id": "typing"}}

    async def get_message(
        self, _recipient_id: str, *, message_id: str
    ) -> dict[str, object]:
        self.lookups.append(message_id)
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": message_id, "message": self.messages[message_id]},
        }


def _event_count(evidence: object, event_type: str) -> int:
    return sum(item.event.event_type == event_type for item in evidence.events)


def _host_builder(
    tmp_path: Path,
    *,
    started_at: datetime,
    clock: dict[str, datetime],
    role: _QualifiedRoleModel,
    delivery: _ProductionDeliveryInterceptor,
    name: str,
):
    settings = Settings(
        database_path=tmp_path / f"{name}.sqlite",
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
    )

    async def skip_pacing(seconds: float) -> None:
        clock["pacing"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    def build():
        return build_qq_c2c_host(
            settings=settings,
            recipient_id="10001",
            bootstrap_at=started_at,
            model=role,
            world_support_model=FakeCompanionModel(),
            delivery=delivery,
            ingress_now=lambda: clock["pacing"],
            ingress_sleep=skip_pacing,
            action_due_now=lambda: clock["scheduler"],
        )

    return build


async def _tick(host: object, *, at: datetime, tick_id: str) -> None:
    evidence = host.export_replay_evidence()
    await host.tick(
        tick_id=tick_id,
        logical_time_from=evidence.projection.logical_time,
        logical_time_to=at,
        observed_at=at,
        reason=f"host_qualification:{tick_id}",
        run_life_ecology=False,
    )


async def _qualify_affect_decay(tmp_path: Path) -> dict[str, object]:
    started = datetime.now(UTC).replace(microsecond=0)
    clock = {"scheduler": started, "pacing": started}
    role = _QualifiedRoleModel(inbound_affect=True)
    delivery = _ProductionDeliveryInterceptor()
    build = _host_builder(
        tmp_path, started_at=started, clock=clock, role=role, delivery=delivery, name="affect"
    )
    first = build()
    try:
        await first.inbound_text(
            message_id="message:affect-open",
            recipient_id="10001",
            text="你一点都不在乎我。",
            observed_at=started,
        )
        opened = first.export_replay_evidence()
        assert len(opened.projection.affect_episodes) == 1, (
            tuple(item.event.event_type for item in opened.events),
            tuple(json.loads(item.audit_json) for item in opened.projection.model_result_audits),
        )
        component = opened.projection.affect_episodes[0].components[0]
        due = component.decay_not_before
        original_intensity = component.intensity_bp
    finally:
        await first.aclose()

    second = build()
    try:
        rebuilt = second.export_replay_evidence()
        assert rebuilt.projection.semantic_hash == opened.projection.semantic_hash
        before = due - timedelta(microseconds=1)
        await _tick(second, at=before, tick_id="affect-before")
        pre = second.export_replay_evidence()
        assert _event_count(pre, "AffectEpisodeDecayed") == 0
        await _tick(second, at=due, tick_id="affect-exact")
        exact = second.export_replay_evidence()
        assert _event_count(exact, "AffectEpisodeDecayed") == 0
        materialized = due + timedelta(seconds=1)
        await _tick(second, at=materialized, tick_id="affect-first-materialized")
        decayed = second.export_replay_evidence()
        assert _event_count(decayed, "AffectEpisodeDecayed") == 1
        assert decayed.projection.affect_episodes[0].components[0].intensity_bp < original_intensity
        cursor = decayed.cursor
        semantic_hash = decayed.projection.semantic_hash
        await second.tick(
            tick_id="affect-first-materialized",
            logical_time_from=exact.projection.logical_time,
            logical_time_to=materialized,
            observed_at=materialized,
            reason="host_qualification:affect-first-materialized",
            run_life_ecology=False,
        )
        duplicate = second.export_replay_evidence()
        assert duplicate.cursor == cursor
        assert duplicate.projection.semantic_hash == semantic_hash
    finally:
        await second.aclose()

    cold = build()
    try:
        replayed = cold.export_replay_evidence()
    finally:
        await cold.aclose()
    assert replayed.cursor == decayed.cursor
    assert replayed.projection.semantic_hash == decayed.projection.semantic_hash
    assert replayed.replay.semantic_hash == decayed.projection.semantic_hash
    return {
        "scenario_id": "affect.decay",
        "qualification": SCENARIO_EVIDENCE_SCOPE,
        "decay_event_count": _event_count(replayed, "AffectEpisodeDecayed"),
        "intensity_decreased": (
            replayed.projection.affect_episodes[0].components[0].intensity_bp
            < original_intensity
        ),
        "cold_replay_hash_matches": replayed.replay.semantic_hash == semantic_hash,
    }


async def _prepare_delivered_reply(
    *,
    build: object,
    clock: dict[str, datetime],
    delivery: _ProductionDeliveryInterceptor,
    started: datetime,
) -> tuple[object, datetime, int, int]:
    first = build()
    try:
        outcome = await first.inbound_text(
            message_id="message:silence-anchor",
            recipient_id="10001",
            text="早呀。",
            observed_at=started,
        )
        assert outcome.status == "action_authorized"
        await first.drain(max_action_units=8, max_background_units=0)
        accepted = first.export_replay_evidence()
        action = next(item for item in accepted.projection.actions if item.kind == "reply")
        assert action.state == "provider_accepted"
        assert action.claim_lease is not None
        reconcile_at = action.claim_lease.expires_at
        action_id = action.action_id
    finally:
        await first.aclose()

    second = build()
    try:
        clock["scheduler"] = reconcile_at
        await _tick(second, at=reconcile_at, tick_id="silence-anchor-reconcile")
        await second.drain(max_action_units=8, max_background_units=0)
        delivered = second.export_replay_evidence()
        terminal = next(
            item
            for item in delivered.projection.execution_receipts
            if item.action_id == action_id and item.is_terminal
        )
        assert terminal.observed_state == "delivered"
        assert delivery.texts == ["我收到啦。"]
        assert len(delivery.lookups) == 1
        assert terminal.provider_ref.endswith(f":{delivery.lookups[0]}:verified")
        # Clear unrelated ordinary inbound jobs only through the public scheduler seam.
        await second.drain(max_action_units=0, max_background_units=32)
        settled = second.export_replay_evidence()
        anchor = next(
            item
            for item in settled.projection.execution_receipts
            if item.action_id == action_id and item.is_terminal
        )
        appraisal_count = len(settled.projection.appraisals)
        affect_count = len(settled.projection.affect_episodes)
    finally:
        await second.aclose()
    return settled, anchor.received_at, appraisal_count, affect_count


async def _qualify_silence_aftermath(
    tmp_path: Path, *, role_choice: str
) -> dict[str, object]:
    started = datetime.now(UTC).replace(microsecond=0)
    clock = {"scheduler": started, "pacing": started}
    role = _QualifiedRoleModel(inbound_affect=False, silence_choice=role_choice)
    delivery = _ProductionDeliveryInterceptor()
    build = _host_builder(
        tmp_path,
        started_at=started,
        clock=clock,
        role=role,
        delivery=delivery,
        name=f"silence-{role_choice}",
    )
    settled, anchor, appraisal_before, affect_before = await _prepare_delivered_reply(
        build=build, clock=clock, delivery=delivery, started=started
    )
    due = anchor + timedelta(seconds=3_600)

    third = build()
    try:
        rebuilt = third.export_replay_evidence()
        assert rebuilt.cursor == settled.cursor
        assert rebuilt.projection.semantic_hash == settled.projection.semantic_hash
        before = due - timedelta(microseconds=1)
        clock["scheduler"] = before
        await _tick(third, at=before, tick_id=f"silence-{role_choice}-before")
        await third.drain(max_action_units=0, max_background_units=8)
        pre = third.export_replay_evidence()
        assert role.silence_calls == 0
        assert not any(
            item.process_kind == "silence_appraisal" for item in pre.projection.trigger_processes
        )

        clock["scheduler"] = due
        await _tick(third, at=due, tick_id=f"silence-{role_choice}-exact")
        await third.drain(max_action_units=0, max_background_units=8)
        decided = third.export_replay_evidence()
        processes = [
            item
            for item in decided.projection.trigger_processes
            if item.process_kind == "silence_appraisal"
        ]
        assert len(processes) == 1 and processes[0].state == "terminal"
        assert role.silence_calls == 1
        if role_choice == "no_change":
            assert len(decided.projection.appraisals) == appraisal_before
            assert len(decided.projection.affect_episodes) == affect_before
        else:
            assert len(decided.projection.appraisals) == appraisal_before + 1
            assert len(decided.projection.affect_episodes) == affect_before + 1
        cursor = decided.cursor
        semantic_hash = decided.projection.semantic_hash
        await third.drain(max_action_units=0, max_background_units=8)
        duplicate = third.export_replay_evidence()
        assert duplicate.cursor == cursor
        assert duplicate.projection.semantic_hash == semantic_hash
        assert role.silence_calls == 1
    finally:
        await third.aclose()

    cold = build()
    try:
        replayed = cold.export_replay_evidence()
    finally:
        await cold.aclose()
    assert replayed.cursor == cursor
    assert replayed.projection.semantic_hash == semantic_hash
    assert replayed.replay.semantic_hash == semantic_hash
    return {
        "scenario_id": "relationship.silence_aftermath",
        "qualification": SCENARIO_EVIDENCE_SCOPE,
        "trigger_terminal": True,
        "role_choice": role_choice,
        "role_calls": role.silence_calls,
        "cold_replay_hash_matches": True,
    }


async def _qualify_silence_technical_failure(tmp_path: Path) -> dict[str, object]:
    started = datetime.now(UTC).replace(microsecond=0)
    clock = {"scheduler": started, "pacing": started}
    role = _QualifiedRoleModel(inbound_affect=False, silence_choice="technical_failure")
    delivery = _ProductionDeliveryInterceptor()
    build = _host_builder(
        tmp_path,
        started_at=started,
        clock=clock,
        role=role,
        delivery=delivery,
        name="silence-technical-failure",
    )
    settled, anchor, appraisal_before, affect_before = await _prepare_delivered_reply(
        build=build, clock=clock, delivery=delivery, started=started
    )
    due = anchor + timedelta(seconds=3_600)
    host = build()
    try:
        clock["scheduler"] = due
        await _tick(host, at=due, tick_id="silence-technical-failure-exact")
        await host.drain(max_action_units=0, max_background_units=8)
        failed = host.export_replay_evidence()
    finally:
        await host.aclose()
    processes = [
        item
        for item in failed.projection.trigger_processes
        if item.process_kind == "silence_appraisal"
    ]
    assert len(processes) == 1
    assert len(failed.projection.appraisals) == appraisal_before
    assert len(failed.projection.affect_episodes) == affect_before
    assert role.silence_calls >= 1
    assert processes[0].state == "claimed"
    assert not any(
        item.trigger_ref == processes[0].source_evidence_ref
        for item in failed.projection.proposal_audits
    )
    assert processes[0].claim_lease is not None
    retry_at = processes[0].claim_lease.expires_at + timedelta(microseconds=1)
    failed_cursor = failed.cursor
    failed_hash = failed.projection.semantic_hash

    restarted = build()
    try:
        before_retry = restarted.export_replay_evidence()
        assert before_retry.cursor == failed_cursor
        assert before_retry.projection.semantic_hash == failed_hash
        clock["scheduler"] = retry_at
        await _tick(restarted, at=retry_at, tick_id="silence-technical-retry-after-lease")
        await restarted.drain(max_action_units=0, max_background_units=16)
        recovered = restarted.export_replay_evidence()
        recovered_process = next(
            item
            for item in recovered.projection.trigger_processes
            if item.process_kind == "silence_appraisal"
        )
        assert recovered_process.state == "terminal"
        assert role.silence_calls == 2
        assert len(recovered.projection.appraisals) == appraisal_before
        assert len(recovered.projection.affect_episodes) == affect_before
        assert any(
            item.trigger_ref == recovered_process.source_evidence_ref
            for item in recovered.projection.proposal_audits
        )
        recovered_cursor = recovered.cursor
        recovered_hash = recovered.projection.semantic_hash
        await restarted.drain(max_action_units=0, max_background_units=16)
        duplicate = restarted.export_replay_evidence()
        assert duplicate.cursor == recovered_cursor
        assert duplicate.projection.semantic_hash == recovered_hash
        assert role.silence_calls == 2
    finally:
        await restarted.aclose()

    cold = build()
    try:
        replayed = cold.export_replay_evidence()
    finally:
        await cold.aclose()
    assert replayed.cursor == recovered_cursor
    assert replayed.projection.semantic_hash == recovered_hash
    assert replayed.replay.semantic_hash == recovered_hash
    return {
        "scenario_id": "relationship.silence_aftermath:technical_failure",
        "trigger_terminal": True,
        "role_calls": role.silence_calls,
        "retry_choice": "no_change",
        "accepted_appraisal_delta": len(failed.projection.appraisals) - appraisal_before,
        "accepted_affect_delta": len(failed.projection.affect_episodes) - affect_before,
        "recorded_as_no_change": processes[0].state == "terminal",
        "cold_replay_hash_matches": replayed.replay.semantic_hash == recovered_hash,
    }


@pytest.mark.asyncio
async def test_public_host_affect_decay_obeys_boundary_restart_and_effect_once(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    _host_scenario(
        "affect.decay-boundary-restart-effect-once.1",
        request.node.nodeid,
        mechanism_ids=("affect.decay",),
    )
    evidence = await _qualify_affect_decay(tmp_path)

    assert evidence["scenario_id"] == "affect.decay"
    assert evidence["decay_event_count"] == 1
    assert evidence["intensity_decreased"] is True
    assert evidence["cold_replay_hash_matches"] is True
    assert evidence["qualification"]["semantic_author_scope"] == (
        "scripted_typed_role_contract_only"
    )
    assert evidence["qualification"]["excluded_scope"] == (
        "real_provider_quality_and_autonomy"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role_choice", ["no_change", "open_affect"])
async def test_public_host_silence_aftermath_is_role_owned_and_effect_once(
    tmp_path: Path,
    role_choice: str,
    request: pytest.FixtureRequest,
) -> None:
    _host_scenario(
        f"relationship.silence-aftermath-{role_choice}-effect-once.1",
        request.node.nodeid,
        mechanism_ids=("relationship.silence_aftermath",),
    )
    evidence = await _qualify_silence_aftermath(tmp_path, role_choice=role_choice)

    assert evidence["scenario_id"] == "relationship.silence_aftermath"
    assert evidence["trigger_terminal"] is True
    assert evidence["role_choice"] == role_choice
    assert evidence["role_calls"] == 1
    assert evidence["cold_replay_hash_matches"] is True
    assert evidence["qualification"]["semantic_author_scope"] == (
        "scripted_typed_role_contract_only"
    )
    assert evidence["qualification"]["excluded_scope"] == (
        "real_provider_quality_and_autonomy"
    )


@pytest.mark.asyncio
async def test_public_host_silence_technical_failure_is_not_role_no_change(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    _host_scenario(
        "relationship.silence-aftermath-technical-failure-lease-recovery.1",
        request.node.nodeid,
        mechanism_ids=("relationship.silence_aftermath",),
    )
    evidence = await _qualify_silence_technical_failure(tmp_path)

    assert evidence["scenario_id"] == "relationship.silence_aftermath:technical_failure"
    assert evidence["trigger_terminal"] is True
    assert evidence["role_calls"] == 2
    assert evidence["retry_choice"] == "no_change"
    assert evidence["accepted_appraisal_delta"] == 0
    assert evidence["accepted_affect_delta"] == 0
    assert evidence["recorded_as_no_change"] is False
    assert evidence["cold_replay_hash_matches"] is True
