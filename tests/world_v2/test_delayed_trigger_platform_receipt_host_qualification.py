from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Literal

import pytest

from companion_daemon.config import Settings
from companion_daemon.delayed_trigger_catalog import load_delayed_trigger_catalog
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.character_interior.turn_store import (
    open_sqlite_character_interior_turn_store,
)
from companion_daemon.world_v2.expression_draft import qq_expression_capabilities
from companion_daemon.world_v2.platform_host import (
    PlatformClockTick,
    PlatformInbound,
    PlatformReceipt,
    WorldV2PlatformHost,
)
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.qq_c2c_host import (
    QQC2CIdentityResolver,
    qq_c2c_target,
    qq_c2c_world_id,
)
from companion_daemon.world_v2.qq_c2c_transport import QQC2CPlatformTransport
from companion_daemon.world_v2.replay_evidence import ReplayEvidence
from companion_daemon.world_v2.semantic_chat_composition import (
    build_semantic_chat_composition,
)


RECEIPT_HOST_QUALIFICATION = {
    "qualification_layer": "public_host_scenario",
    "scenario_id": "platform-receipt-provider-accepted-terminal-restart.1",
    "mechanisms": ("action.authorized_due:text", "expression.deferred_reply"),
    "public_seams": (
        "WorldV2PlatformHost.inbound",
        "WorldV2PlatformHost.tick",
        "WorldV2PlatformHost.drain_scheduled_work",
        "WorldV2PlatformHost.receipt",
        "WorldV2PlatformHost.export_replay_evidence",
        "WorldV2PlatformHost.aclose",
    ),
    "dispatch_transport": "QQC2CPlatformTransport.delivery",
    "receipt_scope": "normalized_platform_receipt_acceptance_only",
    "excluded_scope": "production_provider_callback_normalization",
}

_CATALOG = Path("configs/delayed_trigger_qualification.v1.yaml")


def _host_scenario(
    scenario_id: str, nodeid: str, *, qualification_scope: str
):
    evidence = load_delayed_trigger_catalog(_CATALOG).host_scenario(scenario_id)
    assert evidence.test_nodeid == nodeid
    assert evidence.mechanism_ids == (
        "action.authorized_due",
        "expression.deferred_reply",
        "conversation.commitment_due",
    )
    assert evidence.qualification_scope == qualification_scope
    assert "production_provider_callback_normalization" in evidence.excluded_scope
    assert {
        "real_provider_author_transport",
        "production_stream_expression_episode",
        "character_autonomy",
        "onebot_provider_callback_normalization",
        "24_hour_soak",
    } <= set(evidence.excluded_scope)
    return evidence


def test_receipt_host_qualification_declaration_is_narrow_and_machine_readable() -> None:
    evidence = load_delayed_trigger_catalog(_CATALOG).host_scenario(
        RECEIPT_HOST_QUALIFICATION["scenario_id"]
    )
    assert RECEIPT_HOST_QUALIFICATION == {
        "qualification_layer": "public_host_scenario",
        "scenario_id": "platform-receipt-provider-accepted-terminal-restart.1",
        "mechanisms": ("action.authorized_due:text", "expression.deferred_reply"),
        "public_seams": (
            "WorldV2PlatformHost.inbound",
            "WorldV2PlatformHost.tick",
            "WorldV2PlatformHost.drain_scheduled_work",
            "WorldV2PlatformHost.receipt",
            "WorldV2PlatformHost.export_replay_evidence",
            "WorldV2PlatformHost.aclose",
        ),
        "dispatch_transport": "QQC2CPlatformTransport.delivery",
        "receipt_scope": "normalized_platform_receipt_acceptance_only",
        "excluded_scope": "production_provider_callback_normalization",
    }
    assert evidence.qualification_scope == RECEIPT_HOST_QUALIFICATION["receipt_scope"]
    assert evidence.test_nodeid == (
        "tests/world_v2/test_delayed_trigger_platform_receipt_host_qualification.py::"
        "test_public_host_receipt_settles_terminal_effect_once_and_cold_replays"
    )


def _observation_ref(messages: list[dict[str, str]]) -> str:
    for message in messages:
        try:
            material = json.loads(message["content"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        if not isinstance(material, dict):
            continue
        trigger = material.get("current_trigger_message")
        if isinstance(trigger, dict) and isinstance(trigger.get("observation_ref"), str):
            return trigger["observation_ref"]
    raise AssertionError("same-role fixture did not receive a pinned observation")


def _expression_response(
    messages: list[dict[str, str]], expression: dict[str, object]
) -> str:
    system = messages[0]["content"]
    if "appraisal_draft" not in system or "expression_draft" not in system:
        return json.dumps(expression, ensure_ascii=False)
    return json.dumps(
        {
            "appraisal_draft": {
                "appraise": False,
                "affect": "no_change",
                "brief_rationale": "No durable appraisal is needed for this fixture.",
                "behavior_tendency": "choose_own_response",
                "stance": "self_directed",
                "display_strategy": "model_owned",
                "confidence": 7_000,
            },
            "expression_draft": expression,
        },
        ensure_ascii=False,
    )


class _SameRoleLaterModel:
    model = "fixture:platform-receipt-host-qualification"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        observation_ref = _observation_ref(messages)
        return _expression_response(
            messages,
            {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我想忙完以后再认真接住这句话。",
                    "attended_source_refs": [observation_ref],
                },
                "timing_choice": "later",
                "beats": [{"modality": "text", "text": "我忙完来找你。"}],
                "cadence": "conversational",
                "delay_seconds": 60,
                "expires_after_seconds": 600,
                "stance": "defer",
                "brief_rationale": "I chose to return later.",
                "confidence": 7_200,
                "world_claims": [],
            },
        )


class _ProductionDeliveryInterceptor:
    """The real QQ transport calls this provider-shaped delivery boundary."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.message_ids: list[str] = []
        self.lookup_status: Literal["delivered", "missing"] = "delivered"
        self.lookup_calls: list[tuple[str, str]] = []

    async def send_text(self, _recipient_id: str, text: str) -> dict[str, object]:
        message_id = f"message:receipt-qualification:{len(self.texts) + 1}"
        self.texts.append(text)
        self.message_ids.append(message_id)
        return {"status": "ok", "data": {"message_id": message_id}}

    async def send_reaction(
        self, _recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        del message_id, reaction_id
        return {"status": "failed"}

    async def send_sticker(
        self, _recipient_id: str, *, sticker_id: str
    ) -> dict[str, object]:
        del sticker_id
        return {"status": "failed"}

    async def send_typing(
        self, _recipient_id: str, *, state: str
    ) -> dict[str, object]:
        del state
        return {"status": "ok", "data": {"message_id": "typing"}}

    async def get_message(
        self, recipient_id: str, *, message_id: str
    ) -> dict[str, object]:
        self.lookup_calls.append((recipient_id, message_id))
        if self.lookup_status == "missing":
            return {"status": "failed", "retcode": 1404, "data": {}}
        return {"status": "ok", "retcode": 0, "data": {"message_id": message_id}}


@dataclass(frozen=True, slots=True)
class _DispatchedActionEvidence:
    action_id: str
    idempotency_key: str
    provider_ack_ref: str
    lease_expires_at: datetime
    expression_plan_id: str
    expression_beat_id: str
    commitment_id: str


def _assert_unknown_deferred_reply_closed(
    evidence: ReplayEvidence,
    *,
    dispatched: _DispatchedActionEvidence,
) -> None:
    commitment = next(
        item
        for item in evidence.projection.commitments
        if item.commitment_id == dispatched.commitment_id
    )
    plan = next(
        item
        for item in evidence.projection.expression_plans
        if item.plan_id == dispatched.expression_plan_id
    )
    beat = next(
        item
        for item in evidence.projection.expression_beats
        if item.beat_id == dispatched.expression_beat_id
    )
    assert commitment.values.status == "released"
    assert commitment.values.settlement_reason_code == "precondition_failed"
    assert beat.state == "settled"
    assert beat.history[-1].terminal_action_state == "unknown"
    assert plan.state == "terminated"
    assert plan.history[-1].terminal_disposition == "unknown"


class _PublicReceiptHarness:
    def __init__(self, *, tmp_path: Path, database_name: str) -> None:
        self.started_at = datetime.now(UTC).replace(microsecond=0)
        self.scheduler_clock = {"now": self.started_at}
        self.delivery = _ProductionDeliveryInterceptor()
        self.settings = Settings(
            database_path=tmp_path / database_name,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        )

    def build(self) -> WorldV2PlatformHost:
        """Compose the production application and QQ transport, then expose only its host."""

        capabilities = qq_expression_capabilities(self.settings.qq_adapter)
        turn_store = open_sqlite_character_interior_turn_store(
            path=self.settings.database_path,
            world_id=qq_c2c_world_id(self.settings.primary_user_id),
        )
        semantic = build_semantic_chat_composition(
            settings=self.settings,
            flash_model=_SameRoleLaterModel(),
            world_support_model=FakeCompanionModel(),
            model_id_prefix="platform-receipt-qualification",
            expression_capabilities=capabilities,
            character_interior_turn_store=turn_store,
            character_interior_turn_owner_id="qualification:platform-receipt",
        )
        transport = QQC2CPlatformTransport(
            delivery=self.delivery,
            recipients_by_target={qq_c2c_target("10001"): "10001"},
            now=lambda: self.scheduler_clock["now"],
        )
        application = build_sqlite_world_v2_turn_application(
            path=self.settings.database_path,
            config=WorldV2TurnApplicationConfig(
                world_id=qq_c2c_world_id(self.settings.primary_user_id),
                companion_actor_ref="agent:companion",
                counterpart_actor_ref=f"user:{self.settings.primary_user_id}",
                reply_target=qq_c2c_target("10001"),
                action_pump_owner="pump:platform-receipt-qualification",
                local_timezone=self.settings.local_timezone,
                trace_environment="real_transport",
                expression_action_kinds=capabilities.action_kinds,
                expression_capabilities=capabilities,
                life_ecology=LifeEcologyComposition.production_v1(),
            ),
            identities=QQC2CIdentityResolver(
                recipient_id="10001", canonical_user_id=self.settings.primary_user_id
            ),
            router=semantic.router,
            character_interior=semantic.character_interior,
            transport=transport,
            fact_model=semantic.world_support_model,
            proactive_source_closure_model=semantic.proactive_source_closure_model,
            proactive_candidate_external_proposition_inventory_model=(
                semantic.candidate_external_proposition_inventory_model
            ),
            npc_actor_model=semantic.world_support_model,
            now=self.started_at,
        )
        return WorldV2PlatformHost(application=application)

    async def due_and_dispatch(
        self, host: WorldV2PlatformHost
    ) -> _DispatchedActionEvidence:
        inbound = await host.inbound(
            PlatformInbound(
                platform="qq",
                platform_user_id="10001",
                platform_message_id="message:receipt-host-qualification",
                text="你先忙吧",
                observed_at=self.started_at,
                trace_id="trace:receipt-host-inbound",
            )
        )
        assert inbound.status == "deferred"
        created = host.export_replay_evidence()
        action = next(item for item in created.projection.actions if item.kind == "followup")
        assert action.not_before is not None
        self.scheduler_clock["now"] = action.not_before
        await host.tick(
            PlatformClockTick(
                tick_id="tick:receipt-host-due",
                logical_time_from=created.projection.logical_time,
                logical_time_to=action.not_before,
                observed_at=action.not_before,
                trace_id="trace:receipt-host-tick",
                causation_id="scheduler:receipt-host-due",
                correlation_id="conversation:receipt-host",
                reason="receipt_host_qualification_due",
                run_life_ecology=False,
            )
        )
        await host.drain_scheduled_work(
            max_action_units=2,
            max_background_units=0,
            media_preview_trace_id="trace:receipt-host-drain",
            media_preview_correlation_id="conversation:receipt-host",
        )
        dispatched = host.export_replay_evidence()
        action = next(item for item in dispatched.projection.actions if item.action_id == action.action_id)
        provider_ack = next(
            item
            for item in dispatched.projection.execution_receipts
            if item.action_id == action.action_id and item.observed_state == "provider_accepted"
        )
        assert action.state == "provider_accepted"
        assert action.claim_lease is not None
        assert action.expression_plan_id is not None
        assert action.expression_beat_id is not None
        commitment = next(
            item
            for item in dispatched.projection.commitments
            if item.values.fulfillment_contract.expected_action_id == action.action_id
        )
        assert self.delivery.texts == ["我忙完来找你。"]
        assert self.delivery.message_ids
        return _DispatchedActionEvidence(
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider_ack_ref=provider_ack.provider_ref,
            lease_expires_at=action.claim_lease.expires_at,
            expression_plan_id=action.expression_plan_id,
            expression_beat_id=action.expression_beat_id,
            commitment_id=commitment.commitment_id,
        )

    @staticmethod
    def normalized_receipt(
        *,
        action_id: str,
        idempotency_key: str,
        provider_ack_ref: str,
        status: Literal["delivered", "unknown"],
        observed_at: datetime,
        source_event_id: str,
    ) -> PlatformReceipt:
        provider_ref = f"{provider_ack_ref}:verified"
        callback_payload = json.dumps(
            {
                "source_event_id": source_event_id,
                "status": status,
                "provider_ref": provider_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return PlatformReceipt(
            source="qq:c2c",
            source_event_id=source_event_id,
            action_id=action_id,
            idempotency_key=idempotency_key,
            status=status,
            provider_ref=provider_ref,
            observed_at=observed_at,
            trace_id=f"trace:{source_event_id}",
            causation_id=action_id,
            correlation_id="conversation:receipt-host",
            raw_payload_hash="sha256:"
            + hashlib.sha256(callback_payload.encode()).hexdigest(),
            kind="execution_receipt",
        )


@pytest.mark.asyncio
async def test_public_host_receipt_settles_terminal_effect_once_and_cold_replays(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "platform-receipt-provider-accepted-terminal-restart.1",
        request.node.nodeid,
        qualification_scope="normalized_platform_receipt_acceptance_only",
    )
    harness = _PublicReceiptHarness(tmp_path=tmp_path, database_name="terminal.sqlite")
    first = harness.build()
    try:
        dispatched = await harness.due_and_dispatch(first)
        accepted = first.export_replay_evidence()
        delivered_at = accepted.projection.logical_time + timedelta(seconds=1)
        await first.tick(
            PlatformClockTick(
                tick_id="tick:receipt-host-terminal",
                logical_time_from=accepted.projection.logical_time,
                logical_time_to=delivered_at,
                observed_at=delivered_at,
                trace_id="trace:receipt-host-terminal",
                causation_id="scheduler:receipt-host-terminal",
                correlation_id="conversation:receipt-host",
                reason="receipt_host_qualification_terminal",
                run_life_ecology=False,
            )
        )
        receipt = harness.normalized_receipt(
            action_id=dispatched.action_id,
            idempotency_key=dispatched.idempotency_key,
            provider_ack_ref=dispatched.provider_ack_ref,
            status="delivered",
            observed_at=delivered_at,
            source_event_id="receipt:terminal:1",
        )
        first_outcome = await first.receipt(receipt)
        before_duplicate = first.export_replay_evidence()
        duplicate_outcome = await first.receipt(receipt)
        terminal = first.export_replay_evidence()
        action = next(
            item
            for item in terminal.projection.actions
            if item.action_id == dispatched.action_id
        )
        commitment = next(
            item
            for item in terminal.projection.commitments
            if item.commitment_id == dispatched.commitment_id
        )
        plan = next(
            item
            for item in terminal.projection.expression_plans
            if item.plan_id == dispatched.expression_plan_id
        )
        beat = next(
            item
            for item in terminal.projection.expression_beats
            if item.beat_id == dispatched.expression_beat_id
        )
        assert first_outcome.status == duplicate_outcome.status == "action_executed"
        assert action.state == "delivered"
        assert commitment.values.status == "fulfilled"
        assert plan.state == "completed"
        assert beat.state == "settled"
        assert len(
            [
                item
                for item in terminal.projection.execution_receipts
                if item.action_id == dispatched.action_id
            ]
        ) == 2
        assert terminal.cursor == before_duplicate.cursor
        assert terminal.projection.semantic_hash == before_duplicate.projection.semantic_hash
        assert len(terminal.events) == len(before_duplicate.events)
        assert harness.delivery.texts == ["我忙完来找你。"]
    finally:
        await first.aclose()

    cold = harness.build()
    try:
        replayed = cold.export_replay_evidence()
        replayed_action = next(
            item
            for item in replayed.projection.actions
            if item.action_id == dispatched.action_id
        )
        assert replayed_action.state == "delivered"
        assert replayed.cursor == terminal.cursor
        assert (
            terminal.projection.semantic_hash
            == terminal.replay.semantic_hash
            == replayed.projection.semantic_hash
            == replayed.replay.semantic_hash
        )
        await cold.drain_scheduled_work(
            max_action_units=2,
            max_background_units=0,
            media_preview_trace_id="trace:receipt-host-cold-drain",
            media_preview_correlation_id="conversation:receipt-host",
        )
        after_cold_drain = cold.export_replay_evidence()
        assert after_cold_drain.cursor == replayed.cursor
        assert after_cold_drain.projection.semantic_hash == replayed.projection.semantic_hash
        assert len(after_cold_drain.events) == len(replayed.events)
        assert harness.delivery.texts == ["我忙完来找你。"]
    finally:
        await cold.aclose()


@pytest.mark.asyncio
async def test_public_host_receipt_preserves_unknown_and_records_late_terminal_reconciliation(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    _host_scenario(
        "platform-receipt-unknown-late-conflict-restart.1",
        request.node.nodeid,
        qualification_scope="normalized_platform_receipt_unknown_conflict_only",
    )
    harness = _PublicReceiptHarness(tmp_path=tmp_path, database_name="unknown.sqlite")
    first = harness.build()
    try:
        dispatched = await harness.due_and_dispatch(first)
    finally:
        await first.aclose()

    harness.delivery.lookup_status = "missing"
    restarted = harness.build()
    try:
        before_recovery = restarted.export_replay_evidence()
        expires_at = dispatched.lease_expires_at
        harness.scheduler_clock["now"] = expires_at
        await restarted.tick(
            PlatformClockTick(
                tick_id="tick:receipt-host-missing-terminal",
                logical_time_from=before_recovery.projection.logical_time,
                logical_time_to=expires_at,
                observed_at=expires_at,
                trace_id="trace:receipt-host-missing-terminal",
                causation_id="scheduler:receipt-host-missing-terminal",
                correlation_id="conversation:receipt-host",
                reason="receipt_host_qualification_missing_terminal",
                run_life_ecology=False,
            )
        )
        await restarted.drain_scheduled_work(
            max_action_units=2,
            max_background_units=0,
            media_preview_trace_id="trace:receipt-host-missing-drain",
            media_preview_correlation_id="conversation:receipt-host",
        )
        unknown = restarted.export_replay_evidence()
        unknown_action = next(
            item
            for item in unknown.projection.actions
            if item.action_id == dispatched.action_id
        )
        assert unknown_action.state == "unknown"
        _assert_unknown_deferred_reply_closed(unknown, dispatched=dispatched)
        assert harness.delivery.lookup_calls == [
            ("10001", harness.delivery.message_ids[0])
        ]
        assert harness.delivery.texts == ["我忙完来找你。"]

        late_at = expires_at + timedelta(seconds=1)
        await restarted.tick(
            PlatformClockTick(
                tick_id="tick:receipt-host-late-terminal",
                logical_time_from=unknown.projection.logical_time,
                logical_time_to=late_at,
                observed_at=late_at,
                trace_id="trace:receipt-host-late-terminal",
                causation_id="scheduler:receipt-host-late-terminal",
                correlation_id="conversation:receipt-host",
                reason="receipt_host_qualification_late_terminal",
                run_life_ecology=False,
            )
        )
        late_receipt = harness.normalized_receipt(
            action_id=dispatched.action_id,
            idempotency_key=dispatched.idempotency_key,
            provider_ack_ref=dispatched.provider_ack_ref,
            status="delivered",
            observed_at=late_at,
            source_event_id="receipt:late-delivered-after-unknown",
        )
        late = await restarted.receipt(late_receipt)
        reconciled = restarted.export_replay_evidence()
        reconciled_action = next(
            item
            for item in reconciled.projection.actions
            if item.action_id == dispatched.action_id
        )
        assert late.status == "deferred"
        assert reconciled_action.state == "unknown"
        _assert_unknown_deferred_reply_closed(reconciled, dispatched=dispatched)
        assert reconciled.projection.reconciliations[-1].reason == "terminal_conflict"
        assert harness.delivery.texts == ["我忙完来找你。"]

        duplicate_late = await restarted.receipt(late_receipt)
        after_duplicate = restarted.export_replay_evidence()
        assert duplicate_late.status == "deferred"
        assert after_duplicate.cursor == reconciled.cursor
        assert after_duplicate.projection.semantic_hash == reconciled.projection.semantic_hash
        assert len(after_duplicate.events) == len(reconciled.events)

        await restarted.drain_scheduled_work(
            max_action_units=2,
            max_background_units=0,
            media_preview_trace_id="trace:receipt-host-conflict-drain",
            media_preview_correlation_id="conversation:receipt-host",
        )
        final = restarted.export_replay_evidence()
        assert final.cursor == reconciled.cursor
        assert final.projection.semantic_hash == reconciled.projection.semantic_hash
        assert len(final.events) == len(reconciled.events)
    finally:
        await restarted.aclose()

    cold = harness.build()
    try:
        replayed = cold.export_replay_evidence()
        replayed_action = next(
            item
            for item in replayed.projection.actions
            if item.action_id == dispatched.action_id
        )
        replayed_reconciliation = next(
            item
            for item in replayed.projection.reconciliations
            if item.result_id == reconciled.projection.reconciliations[-1].result_id
        )
        assert replayed_action.state == "unknown"
        _assert_unknown_deferred_reply_closed(replayed, dispatched=dispatched)
        assert replayed_reconciliation.reason == "terminal_conflict"
        assert replayed.cursor == final.cursor
        assert (
            final.projection.semantic_hash
            == final.replay.semantic_hash
            == replayed.projection.semantic_hash
            == replayed.replay.semantic_hash
        )
        await cold.drain_scheduled_work(
            max_action_units=2,
            max_background_units=0,
            media_preview_trace_id="trace:receipt-host-conflict-cold-drain",
            media_preview_correlation_id="conversation:receipt-host",
        )
        after_cold_drain = cold.export_replay_evidence()
        assert after_cold_drain.cursor == replayed.cursor
        assert after_cold_drain.projection.semantic_hash == replayed.projection.semantic_hash
        assert len(after_cold_drain.events) == len(replayed.events)
        assert harness.delivery.texts == ["我忙完来找你。"]
    finally:
        await cold.aclose()
