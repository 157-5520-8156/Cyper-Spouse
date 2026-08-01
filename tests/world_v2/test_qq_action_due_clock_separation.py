from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.action_pump import ActionPumpResult
from companion_daemon.world_v2.qq_c2c_host import QQC2CHost, build_qq_c2c_host
from companion_daemon.world_v2.qq_ingress_policy import SQLiteQQIngressStore
from companion_daemon.world_v2.recent_dialogue import RecentDialogueCompiler


@pytest.mark.asyncio
async def test_ingress_pacing_clock_cannot_fast_forward_future_action_due_wake(
    tmp_path: Path,
) -> None:
    """Skipping bubble pacing must not also skip a model-owned ``later`` wait."""

    started_at = datetime.now(UTC).replace(microsecond=0)
    due_at = started_at + timedelta(hours=8)
    pacing_clock = {"now": started_at}

    class _FutureActionHost:
        def __init__(self) -> None:
            self.logical_time = started_at
            self.action_state: str | None = None
            self.tick_targets: list[datetime] = []
            self.delivered: list[str] = []

        async def action_due_projection(self) -> SimpleNamespace:
            actions = ()
            if self.action_state is not None:
                actions = (
                    SimpleNamespace(
                        action_id="action:later",
                        state=self.action_state,
                        not_before=due_at,
                        claim_lease=None,
                    ),
                )
            return SimpleNamespace(actions=actions)

        async def current_logical_time(self) -> datetime:
            return self.logical_time

        async def inbound(self, _inbound: object) -> SimpleNamespace:
            self.action_state = "authorized"
            return SimpleNamespace(
                status="deferred",
                authorized_action_ids=(),
                scheduled_action_ids=("action:later",),
            )

        async def drain_action(self, action_id: str) -> ActionPumpResult:
            assert action_id == "action:later"
            if self.logical_time < due_at:
                self.action_state = "scheduled"
                return ActionPumpResult(action_id=action_id, status="not_due")
            self.action_state = "delivered"
            self.delivered.append(action_id)
            return ActionPumpResult(
                action_id=action_id,
                status="settled",
                provider_status="delivered",
            )

        async def drain_actions_once(self) -> ActionPumpResult:
            return await self.drain_action("action:later")

        async def tick(self, tick: object) -> SimpleNamespace:
            target = getattr(tick, "logical_time_to")
            self.tick_targets.append(target)
            self.logical_time = target
            return SimpleNamespace(status="observed_only")

        def close(self) -> None:
            return None

    async def skip_pacing(seconds: float) -> None:
        pacing_clock["now"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    platform = _FutureActionHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "clock-separation.sqlite"),
        ingress_now=lambda: pacing_clock["now"],
        ingress_sleep=skip_pacing,
    )
    try:
        outcome = await host.inbound_text(
            message_id="message:later",
            recipient_id="10001",
            text="晚点再说也行",
            observed_at=started_at,
        )
        for _ in range(10):
            await asyncio.sleep(0)

        assert outcome.status == "deferred"
        assert platform.tick_targets == []
        assert platform.delivered == []
        assert pacing_clock["now"] < due_at
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_action_due_wake_uses_scheduler_clock_and_dispatches_effect_once(
    tmp_path: Path,
) -> None:
    started_at = datetime.now(UTC).replace(microsecond=0)
    due_at = started_at + timedelta(hours=8)
    pacing_clock = {"now": started_at}
    scheduler_clock = {"now": started_at}

    class _FutureActionHost:
        def __init__(self) -> None:
            self.logical_time = started_at
            self.action_state: str | None = None
            self.tick_targets: list[datetime] = []
            self.delivered: list[str] = []

        async def action_due_projection(self) -> SimpleNamespace:
            actions = ()
            if self.action_state is not None:
                actions = (
                    SimpleNamespace(
                        action_id="action:later",
                        state=self.action_state,
                        not_before=due_at,
                        claim_lease=None,
                    ),
                )
            return SimpleNamespace(actions=actions)

        async def current_logical_time(self) -> datetime:
            return self.logical_time

        async def inbound(self, _inbound: object) -> SimpleNamespace:
            self.action_state = "authorized"
            return SimpleNamespace(
                status="deferred",
                authorized_action_ids=(),
                scheduled_action_ids=("action:later",),
            )

        async def drain_action(self, action_id: str) -> ActionPumpResult:
            assert action_id == "action:later"
            if self.logical_time < due_at:
                self.action_state = "scheduled"
                return ActionPumpResult(action_id=action_id, status="not_due")
            if self.action_state != "delivered":
                self.action_state = "delivered"
                self.delivered.append(action_id)
            return ActionPumpResult(
                action_id=action_id,
                status="settled",
                provider_status="delivered",
            )

        async def drain_actions_once(self) -> ActionPumpResult:
            if self.action_state == "delivered":
                return ActionPumpResult(status="idle")
            return await self.drain_action("action:later")

        async def tick(self, tick: object) -> SimpleNamespace:
            target = getattr(tick, "logical_time_to")
            self.tick_targets.append(target)
            self.logical_time = target
            return SimpleNamespace(status="observed_only")

        def close(self) -> None:
            return None

    async def skip_pacing(seconds: float) -> None:
        pacing_clock["now"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    platform = _FutureActionHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "exact-due.sqlite"),
        ingress_now=lambda: pacing_clock["now"],
        ingress_sleep=skip_pacing,
        action_due_now=lambda: scheduler_clock["now"],
    )
    try:
        await host.inbound_text(
            message_id="message:later",
            recipient_id="10001",
            text="晚点再说也行",
            observed_at=started_at,
        )
        scheduler_clock["now"] = due_at

        await host._wake_due_actions()  # noqa: SLF001
        await host._wake_due_actions()  # noqa: SLF001

        assert platform.tick_targets == [due_at]
        assert platform.delivered == ["action:later"]
        assert pacing_clock["now"] < due_at
    finally:
        await host.aclose()


class _EightHourLaterModel:
    model = "fixture:qq-eight-hour-later"

    async def complete(
        self,
        _messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        return json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": "我想等忙完以后再认真接这句话。",
                    "attended_source_refs": [],
                },
                "timing_choice": "later",
                "beats": [{"modality": "text", "text": "我忙完来找你。"}],
                "cadence": "conversational",
                "delay_seconds": 28_800,
                "expires_after_seconds": 43_200,
                "stance": "defer",
                "brief_rationale": "I chose to return later.",
                "confidence": 7_200,
            },
            ensure_ascii=False,
        )


class _LaterThenNowModel(_EightHourLaterModel):
    model = "fixture:qq-later-then-now"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.calls += 1
        if self.calls == 1:
            return await super().complete(messages, temperature=temperature)
        return json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": "她又发来一句，我现在想直接接住。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我在，接着说。"}],
                "cadence": "conversational",
                "stance": "present",
                "brief_rationale": "I chose to answer the newer message now.",
                "confidence": 7_500,
            },
            ensure_ascii=False,
        )


class _VerifiedDelivery:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.messages: dict[str, str] = {}

    async def send_text(self, _recipient_id: str, text: str) -> dict[str, object]:
        message_id = f"message:{len(self.texts) + 1}"
        self.texts.append(text)
        self.messages[message_id] = text
        return {"status": "ok", "data": {"message_id": message_id}}

    async def send_reaction(
        self,
        _recipient_id: str,
        *,
        message_id: str,
        reaction_id: str,
    ) -> dict[str, object]:
        del message_id, reaction_id
        return {"status": "failed"}

    async def send_sticker(
        self,
        _recipient_id: str,
        *,
        sticker_id: str,
    ) -> dict[str, object]:
        del sticker_id
        return {"status": "failed"}

    async def send_typing(
        self,
        _recipient_id: str,
        *,
        state: str,
    ) -> dict[str, object]:
        del state
        return {"status": "ok", "data": {"message_id": "typing"}}

    async def get_message(
        self,
        _recipient_id: str,
        *,
        message_id: str,
    ) -> dict[str, object]:
        return {
            "status": "ok",
            "retcode": 0,
            "data": {
                "message_id": message_id,
                "message": self.messages[message_id],
            },
        }


@pytest.mark.asyncio
async def test_explicit_scheduler_clock_keeps_receipt_after_virtual_inbound(
    tmp_path: Path,
) -> None:
    """One injected audit clock orders Observation, receipt and recent dialogue."""

    started_at = datetime.now(UTC).replace(microsecond=0)
    observed_at = started_at + timedelta(minutes=9)
    virtual_clock = {"now": observed_at}
    delivery = _VerifiedDelivery()
    model = _LaterThenNowModel()
    model.calls = 1

    async def skip_pacing(seconds: float) -> None:
        virtual_clock["now"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "virtual-receipt-clock.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=started_at,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=delivery,
        ingress_now=lambda: virtual_clock["now"],
        ingress_sleep=skip_pacing,
        action_due_now=lambda: virtual_clock["now"],
    )
    try:
        outcome = await host.inbound_text(
            message_id="message:virtual-nine-minutes-later",
            recipient_id="10001",
            text="九分钟后我又来啦",
            observed_at=observed_at,
        )
        await host.drain(max_action_units=8, max_background_units=0)
        application = host._host._application  # type: ignore[attr-defined]  # noqa: SLF001
        projection = application._ledger.project()  # noqa: SLF001
        dialogue = RecentDialogueCompiler(
            ledger=application._ledger,  # noqa: SLF001
            expression_payload_store=application._expression_payload_store,  # noqa: SLF001
        ).compile(
            projection=projection,
            actor_ref="agent:companion",
            subject_refs=frozenset({"user:geoff"}),
        )
    finally:
        await host.aclose()

    counterpart = next(item for item in dialogue if item.speaker == "counterpart")
    companion = next(item for item in dialogue if item.speaker == "companion")
    visible_receipts = tuple(
        receipt
        for receipt in projection.execution_receipts
        if receipt.observed_state in {"provider_accepted", "delivered"}
    )
    assert outcome.status == "action_authorized"
    assert projection.execution_receipts
    assert all(
        receipt.received_at >= observed_at
        for receipt in projection.execution_receipts
    )
    assert counterpart.occurred_at == observed_at
    assert visible_receipts
    assert len({receipt.action_id for receipt in visible_receipts}) == 1
    assert companion.occurred_at == min(
        receipt.received_at for receipt in visible_receipts
    )
    assert companion.sequence > counterpart.sequence


@pytest.mark.asyncio
async def test_fast_paced_later_turn_does_not_poison_the_next_inbound_clock(
    tmp_path: Path,
) -> None:
    started_at = datetime.now(UTC).replace(microsecond=0)
    pacing_clock = {"now": started_at}
    delivery = _VerifiedDelivery()
    model = _LaterThenNowModel()

    async def skip_pacing(seconds: float) -> None:
        pacing_clock["now"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "later-then-next-inbound.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=started_at,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=delivery,
        ingress_now=lambda: pacing_clock["now"],
        ingress_sleep=skip_pacing,
    )
    try:
        first = await host.inbound_text(
            message_id="message:later",
            recipient_id="10001",
            text="你先忙吧",
            observed_at=started_at,
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert first.status == "deferred"
        assert delivery.texts == []

        second_observed_at = started_at + timedelta(minutes=2)
        second = await host.inbound_text(
            message_id="message:newer",
            recipient_id="10001",
            text="我再补一句",
            observed_at=second_observed_at,
        )
        await host.drain(max_action_units=8, max_background_units=0)
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]  # noqa: SLF001
    finally:
        await host.aclose()

    assert second.status == "action_authorized"
    assert projection.logical_time == second_observed_at
    assert delivery.texts == ["我在，接着说。"]
    deferred_action = next(item for item in projection.actions if item.kind == "followup")
    assert deferred_action.state in {"authorized", "scheduled", "cancelled"}


@pytest.mark.asyncio
async def test_future_followup_survives_restart_and_settles_once_at_due_boundaries(
    tmp_path: Path,
) -> None:
    """A restart rebuilds the timer without sending before ``not_before``."""

    started_at = datetime.now(UTC).replace(microsecond=0)
    pacing_clock = {"now": started_at}
    scheduler_clock = {"now": started_at}
    delivery = _VerifiedDelivery()
    database = tmp_path / "restart-future-followup.sqlite"
    settings = Settings(
        database_path=database,
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
    )

    async def skip_pacing(seconds: float) -> None:
        pacing_clock["now"] += timedelta(seconds=max(0.0, seconds))
        await asyncio.sleep(0)

    first = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=started_at,
        model=_EightHourLaterModel(),
        advisory_model=FakeCompanionModel(),
        delivery=delivery,
        ingress_now=lambda: pacing_clock["now"],
        ingress_sleep=skip_pacing,
        action_due_now=lambda: scheduler_clock["now"],
    )
    try:
        outcome = await first.inbound_text(
            message_id="message:later-before-restart",
            recipient_id="10001",
            text="你先忙吧",
            observed_at=started_at,
        )
        before_restart = first._host._application._ledger.project()  # type: ignore[attr-defined]  # noqa: SLF001
        action = before_restart.actions[0]

        assert outcome.status == "deferred"
        assert action.not_before == started_at + timedelta(hours=8)
        assert action.state in {"authorized", "scheduled"}
        assert delivery.texts == []
    finally:
        await first.aclose()

    rebuilt = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=started_at,
        model=_EightHourLaterModel(),
        advisory_model=FakeCompanionModel(),
        delivery=delivery,
        ingress_now=lambda: pacing_clock["now"],
        ingress_sleep=skip_pacing,
        action_due_now=lambda: scheduler_clock["now"],
    )
    try:
        scheduler_clock["now"] = action.not_before
        await rebuilt._wake_due_actions()  # noqa: SLF001
        after_send = rebuilt._host._application._ledger.project()  # type: ignore[attr-defined]  # noqa: SLF001
        sent_action = next(
            item for item in after_send.actions if item.action_id == action.action_id
        )

        assert delivery.texts == ["我忙完来找你。"]
        assert sent_action.state == "provider_accepted"
        assert sent_action.claim_lease is not None

        scheduler_clock["now"] = sent_action.claim_lease.expires_at
        await rebuilt._wake_due_actions()  # noqa: SLF001
        await rebuilt._wake_due_actions()  # noqa: SLF001
        application = rebuilt._host._application  # type: ignore[attr-defined]  # noqa: SLF001
        settled = application._ledger.project()  # noqa: SLF001
        dialogue = RecentDialogueCompiler(
            ledger=application._ledger,  # noqa: SLF001
            expression_payload_store=application._expression_payload_store,  # noqa: SLF001
        ).compile(
            projection=settled,
            actor_ref="agent:companion",
            subject_refs=frozenset({"user:geoff"}),
        )
    finally:
        await rebuilt.aclose()

    settled_action = next(item for item in settled.actions if item.action_id == action.action_id)
    companion = next(item for item in dialogue if item.speaker == "companion")
    visible_receipts = tuple(
        receipt
        for receipt in settled.execution_receipts
        if receipt.action_id == action.action_id
        and receipt.observed_state in {"provider_accepted", "delivered"}
    )
    commitment = next(
        item
        for item in settled.commitments
        if item.values.fulfillment_contract.expected_action_id == action.action_id
    )
    assert settled_action.state == "delivered"
    assert companion.occurred_at == min(
        receipt.received_at for receipt in visible_receipts
    )
    assert companion.occurred_at >= action.not_before
    assert commitment.values.status == "fulfilled"
    assert delivery.texts == ["我忙完来找你。"]
