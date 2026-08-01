from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.action_pump import TerminalPreDispatchFailure
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_payload_reader import (
    LedgerAuthorizedPayloadReader,
)
from companion_daemon.world_v2.platform_action_executor import (
    PlatformActionExecutor,
    PlatformDispatchReceipt,
    ProviderMediaActionExecutor,
    ResolvedActionPayload,
)
from companion_daemon.world_v2.platform_host import WorldV2PlatformHost
from companion_daemon.world_v2.qq_c2c_transport import QQC2CPlatformTransport
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    BudgetReservation,
    ClockObservation,
    WorldEvent,
)


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
WORLD = "world:action-preflight-failure"
TEXT = "我在。"


def _action() -> Action:
    return Action(
        schema_version="world-v2.1",
        action_id="action:reply:preflight",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:action-preflight",
        causation_id="acceptance:reply:preflight",
        correlation_id="conversation:preflight",
        kind="reply",
        layer="external_action",
        intent_ref="intent:reply:preflight",
        actor="agent:companion",
        target="user:primary",
        payload_ref="payload:reply:preflight",
        payload_hash="sha256:" + hashlib.sha256(TEXT.encode("utf-8")).hexdigest(),
        idempotency_key="action-preflight:reply:1",
        budget_reservation_id="reservation:reply:preflight",
        state="authorized",
        recovery_policy="effect_once",
    )


def _event(event_type: str, payload: dict[str, object], suffix: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:action-preflight:{suffix}",
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="test",
        source="test",
        trace_id="trace:action-preflight",
        causation_id="test",
        correlation_id="conversation:preflight",
        idempotency_key=f"action-preflight:{suffix}",
        payload=payload,
    )


def _ready_ledger() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    action = _action()
    account = BudgetAccount(
        account_id="account:chat",
        category="chat",
        window_id="test",
        limit=100,
    )
    reservation = BudgetReservation(
        reservation_id=action.budget_reservation_id,
        account_id=account.account_id,
        action_id=action.action_id,
        category="chat",
        amount_limit=10,
    )
    ledger.commit(
        (
            _event(
                "BudgetAccountConfigured",
                {"account": account.model_dump(mode="json")},
                "account",
            ),
            _event(
                "BudgetReserved",
                {"reservation": reservation.model_dump(mode="json")},
                "reservation",
            ),
            _event(
                "ActionAuthorized",
                {"action": action.model_dump(mode="json")},
                "action",
            ),
        ),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return ledger


class _MismatchedPayloads:
    async def resolve(self, action: Action) -> ResolvedActionPayload:
        return ResolvedActionPayload(
            payload_ref=action.payload_ref,
            payload_hash=action.payload_hash,
            content_type="text/plain",
            body="这些不是已授权的字节。",
        )


class _ProgrammingErrorPayloads:
    async def resolve(self, _action: Action) -> ResolvedActionPayload:
        raise ValueError("unclassified payload-reader programming error")


class _StaticPayloads:
    def __init__(self, *, body: str, content_type: str) -> None:
        self._body = body
        self._content_type = content_type

    async def resolve(self, action: Action) -> ResolvedActionPayload:
        return ResolvedActionPayload(
            payload_ref=action.payload_ref,
            payload_hash=action.payload_hash,
            content_type=self._content_type,
            body=self._body,
        )


class _Transport:
    provider = "transport:preflight-test"

    def __init__(self) -> None:
        self.send_calls = 0
        self.lookup_calls = 0

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.send_calls += 1
        return PlatformDispatchReceipt(
            provider_receipt_id="receipt:should-not-send",
            provider_ref="provider-ref:should-not-send",
            status="delivered",
            received_at=NOW,
            raw_payload_hash="sha256:should-not-send",
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )

    async def lookup(self, *, idempotency_key: str, request_fingerprint: str):
        self.lookup_calls += 1
        return PlatformDispatchReceipt(
            provider_receipt_id="receipt:should-not-lookup",
            provider_ref="provider-ref:should-not-lookup",
            status="delivered",
            received_at=NOW,
            raw_payload_hash="sha256:should-not-lookup",
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
        )


def _runtime(
    *,
    ledger: WorldLedger,
    payloads: object,
    transport: _Transport,
) -> WorldRuntime:
    return WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=PlatformActionExecutor(
            payloads=payloads,  # type: ignore[arg-type]
            transport=transport,
        ),
        action_pump_owner="pump:preflight",
    )


def _receipt_error_class(ledger: WorldLedger) -> str | None:
    receipts = ledger.project().execution_receipts
    assert len(receipts) == 1
    return receipts[0].error_class


@pytest.mark.asyncio
async def test_scheduler_fresh_dispatch_settles_declared_payload_failure_and_then_idles() -> None:
    ledger = _ready_ledger()
    transport = _Transport()
    runtime = _runtime(
        ledger=ledger,
        payloads=_MismatchedPayloads(),
        transport=transport,
    )

    failed = await runtime.drain_actions_once()
    idle = await runtime.drain_actions_once()

    assert failed is not None
    assert failed.status == "settled"
    assert failed.provider_status == "failed"
    assert idle is not None and idle.status == "idle"
    assert transport.send_calls == 0
    assert transport.lookup_calls == 0
    projection = ledger.project()
    assert projection.actions[0].state == "failed"
    assert projection.budget_reservations[0].state == "settled"
    assert _receipt_error_class(ledger) == "local_preflight_payload_hash_mismatch"


@pytest.mark.asyncio
async def test_targeted_fast_dispatch_settles_declared_payload_failure_without_provider_call() -> None:
    ledger = _ready_ledger()
    transport = _Transport()
    runtime = _runtime(
        ledger=ledger,
        payloads=_MismatchedPayloads(),
        transport=transport,
    )

    failed = await runtime.drain_action(_action().action_id)

    assert failed is not None
    assert failed.status == "settled"
    assert failed.provider_status == "failed"
    assert transport.send_calls == 0
    assert transport.lookup_calls == 0
    assert ledger.project().actions[0].state == "failed"
    assert _receipt_error_class(ledger) == "local_preflight_payload_hash_mismatch"


@pytest.mark.asyncio
async def test_production_payload_reader_declares_missing_authorization_proof() -> None:
    ledger = _ready_ledger()
    transport = _Transport()
    runtime = _runtime(
        ledger=ledger,
        payloads=LedgerAuthorizedPayloadReader(ledger=ledger),
        transport=transport,
    )

    failed = await runtime.drain_actions_once()
    idle = await runtime.drain_actions_once()

    assert failed is not None and failed.provider_status == "failed"
    assert idle is not None and idle.status == "idle"
    assert transport.send_calls == 0
    assert transport.lookup_calls == 0
    assert ledger.project().actions[0].state == "failed"
    assert _receipt_error_class(ledger) == "local_preflight_payload_unavailable"


def _mark_dispatch_started(ledger: WorldLedger) -> None:
    action = ledger.project().actions[0]
    lease = {
        "owner_id": "pump:dead",
        "attempt_id": "attempt:dead",
        "acquired_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=2)).isoformat(),
    }
    for event in (
        _event("ActionScheduled", {"action_id": action.action_id}, "scheduled"),
        _event(
            "ActionClaimed",
            {"action_id": action.action_id, "claim_lease": lease},
            "claimed",
        ),
        _event(
            "ActionDispatchStarted",
            {
                "action_id": action.action_id,
                "owner_id": lease["owner_id"],
                "attempt_id": lease["attempt_id"],
                "started_at": NOW.isoformat(),
            },
            "dispatch-started",
        ),
    ):
        projection = ledger.project()
        ledger.commit(
            (event,),
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )


@pytest.mark.asyncio
async def test_cold_recovery_terminates_preflight_poison_as_unknown_without_provider_call() -> None:
    ledger = _ready_ledger()
    _mark_dispatch_started(ledger)
    transport = _Transport()
    runtime = _runtime(
        ledger=ledger,
        payloads=_MismatchedPayloads(),
        transport=transport,
    )
    await runtime.advance(
        ClockObservation(
            schema_version="world-v2.1",
            tick_id="tick:preflight-recovery",
            world_id=WORLD,
            logical_time=NOW + timedelta(minutes=3),
            created_at=NOW + timedelta(minutes=3),
            trace_id="trace:preflight-recovery",
            causation_id="scheduler:preflight-recovery",
            correlation_id="conversation:preflight",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=3),
            reason="expire dispatch recovery lease",
        )
    )

    recovered = await runtime.drain_actions_once()
    idle = await runtime.drain_actions_once()

    assert recovered is not None
    assert recovered.status == "settled"
    assert recovered.provider_status == "unknown"
    assert idle is not None and idle.status == "idle"
    assert transport.send_calls == 0
    assert transport.lookup_calls == 0
    assert ledger.project().actions[0].state == "unknown"
    assert _receipt_error_class(ledger) == "local_preflight_payload_hash_mismatch"


@pytest.mark.asyncio
async def test_unclassified_executor_value_error_remains_observable() -> None:
    ledger = _ready_ledger()
    transport = _Transport()
    runtime = _runtime(
        ledger=ledger,
        payloads=_ProgrammingErrorPayloads(),
        transport=transport,
    )

    with pytest.raises(ValueError, match="programming error"):
        await runtime.drain_actions_once()

    assert transport.send_calls == 0
    assert transport.lookup_calls == 0
    assert ledger.project().actions[0].state == "dispatch_started"
    assert ledger.project().execution_receipts == ()


class _QQDelivery:
    def __init__(self) -> None:
        self.provider_calls = 0

    async def send_reaction(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        self.provider_calls += 1
        raise AssertionError("invalid reaction payload must not reach QQ")


@pytest.mark.asyncio
async def test_qq_declares_typed_payload_validation_failure_before_provider_call() -> None:
    body = '{"reaction":'
    action = _action().model_copy(
        update={
            "kind": "reaction",
            "target": "conversation:qq:c2c:owner",
            "payload_hash": "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
    )
    delivery = _QQDelivery()
    ledger = WorldLedger.in_memory(world_id=WORLD)
    account = BudgetAccount(
        account_id="account:chat",
        category="chat",
        window_id="test",
        limit=100,
    )
    reservation = BudgetReservation(
        reservation_id=action.budget_reservation_id,
        account_id=account.account_id,
        action_id=action.action_id,
        category="chat",
        amount_limit=10,
    )
    ledger.commit(
        (
            _event(
                "BudgetAccountConfigured",
                {"account": account.model_dump(mode="json")},
                "qq-account",
            ),
            _event(
                "BudgetReserved",
                {"reservation": reservation.model_dump(mode="json")},
                "qq-reservation",
            ),
            _event(
                "ActionAuthorized",
                {"action": action.model_dump(mode="json")},
                "qq-action",
            ),
        ),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=PlatformActionExecutor(
            payloads=_StaticPayloads(
                body=body,
                content_type="application/vnd.world-v2.reaction+json",
            ),
            transport=QQC2CPlatformTransport(
                delivery=delivery,  # type: ignore[arg-type]
                recipients_by_target={action.target: "10001"},
                now=lambda: NOW,
            ),
        ),
        action_pump_owner="pump:preflight",
    )

    failed = await runtime.drain_actions_once()

    assert failed is not None and failed.provider_status == "failed"
    assert delivery.provider_calls == 0
    assert ledger.project().actions[0].state == "failed"
    assert (
        _receipt_error_class(ledger)
        == "local_preflight_payload_semantics_rejected"
    )


@pytest.mark.asyncio
async def test_platform_executor_declares_media_authorization_rejection_without_details() -> None:
    ledger = _ready_ledger()
    transport = _Transport()
    executor = PlatformActionExecutor(
        payloads=_MismatchedPayloads(),
        transport=transport,
    )
    media_delivery = _action().model_copy(
        update={
            "kind": "media_delivery",
            "media_delivery_approval": {
                "approval_id": "approval:missing",
                "approval_revision": 1,
            },
        }
    )

    with pytest.raises(TerminalPreDispatchFailure) as caught:
        await executor.assert_dispatch_authorized(
            action=media_delivery,
            projection=ledger.project(),
        )

    assert caught.value.error_class == "local_preflight_authorization_rejected"
    assert caught.value.provider == transport.provider
    assert "approval:missing" not in str(caught.value)
    assert transport.send_calls == 0
    assert transport.lookup_calls == 0


@pytest.mark.asyncio
async def test_media_executor_declares_stale_grant_without_provider_call() -> None:
    ledger = _ready_ledger()
    transport = _Transport()
    executor = ProviderMediaActionExecutor(
        payloads=_MismatchedPayloads(),
        transport=transport,
    )
    media_action = _action().model_copy(
        update={
            "kind": "media_render",
            "layer": "media_action",
            "target": transport.provider,
            "provider_media_grant": {
                "grant_id": "grant:missing",
                "grant_revision": 1,
            },
        }
    )

    with pytest.raises(TerminalPreDispatchFailure) as caught:
        await executor.assert_dispatch_authorized(
            action=media_action,
            projection=ledger.project(),
        )

    assert caught.value.error_class == "local_preflight_authorization_rejected"
    assert caught.value.provider == transport.provider
    assert "grant:missing" not in str(caught.value)
    assert transport.send_calls == 0
    assert transport.lookup_calls == 0


class _AuthorizationRejectedExecutor:
    provider_calls = 0

    async def assert_dispatch_authorized(self, **_kwargs: object) -> None:
        raise TerminalPreDispatchFailure(
            provider="transport:authorization-test",
            error_class="local_preflight_authorization_rejected",
            message="declared authorization rejection",
        )

    async def dispatch(self, _action: Action):  # type: ignore[no-untyped-def]
        self.provider_calls += 1
        raise AssertionError("authorization failure must stop before dispatch")

    async def lookup_result(self, _action: Action):  # type: ignore[no-untyped-def]
        self.provider_calls += 1
        raise AssertionError("authorization failure must stop before lookup")


@pytest.mark.asyncio
async def test_action_pump_settles_declared_authorization_failure_before_provider_call() -> None:
    ledger = _ready_ledger()
    executor = _AuthorizationRejectedExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="pump:preflight",
    )

    failed = await runtime.drain_actions_once()

    assert failed is not None
    assert failed.status == "settled"
    assert failed.provider_status == "failed"
    assert executor.provider_calls == 0
    assert ledger.project().actions[0].state == "failed"
    assert _receipt_error_class(ledger) == "local_preflight_authorization_rejected"


class _SchedulerApplication:
    def __init__(self, runtime: WorldRuntime) -> None:
        self._runtime = runtime
        self.background_calls = 0

    async def drain_actions_once(self):  # type: ignore[no-untyped-def]
        return await self._runtime.drain_actions_once()

    async def drain_background_once(self):  # type: ignore[no-untyped-def]
        self.background_calls += 1
        return SimpleNamespace(work_status="background:continued")


@pytest.mark.asyncio
async def test_declared_preflight_failure_does_not_abort_scheduler_background() -> None:
    ledger = _ready_ledger()
    transport = _Transport()
    application = _SchedulerApplication(
        _runtime(
            ledger=ledger,
            payloads=_MismatchedPayloads(),
            transport=transport,
        )
    )

    result = await WorldV2PlatformHost(
        application=application  # type: ignore[arg-type]
    ).drain_scheduled_work(
        max_action_units=2,
        max_background_units=1,
        media_preview_trace_id="trace:preflight-scheduler",
        media_preview_correlation_id="conversation:preflight",
    )

    assert result.action_statuses == ("settled",)
    assert result.background_statuses == ("background:continued",)
    assert application.background_calls == 1
    assert transport.send_calls == 0
    assert transport.lookup_calls == 0
    assert ledger.project().actions[0].state == "failed"
