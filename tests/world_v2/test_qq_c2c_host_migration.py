from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
import inspect
import json
import logging
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.action_due_wake import ActionDueWake
import companion_daemon.world_v2.qq_c2c_host as qq_c2c_host_module
from companion_daemon.world_v2.qq_c2c_host import (
    QQC2CDrainResult,
    QQC2CHost,
    QQC2CIdentityResolver,
    build_qq_c2c_host,
    qq_c2c_target,
)
from companion_daemon.world_v2.platform_action_executor import (
    MediaProviderDispatchRequest,
    PlatformDispatchReceipt,
)
from companion_daemon.world_v2.action_pump import ActionPumpResult
from companion_daemon.world_v2.interactive_turn_budget import (
    InteractiveTurnBudgetPolicy,
)
from companion_daemon.world_v2.deliberation import ModelUsageProvenance
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.production_turn_application import (
    MediaPreviewDeployment,
    MediaSelectionAcceptanceComposition,
)
from companion_daemon.world_v2.qq_c2c_onebot_app import create_qq_c2c_onebot_app
from companion_daemon.world_v2.qq_c2c_onebot_app import (
    QQC2CSchedulerDiagnostics,
    _scheduler_loop,
)
from companion_daemon.world_v2.qq_ingress_policy import QQIngressFragment
from companion_daemon.world_v2.qq_ingress_policy import SQLiteQQIngressStore
from companion_daemon.world_v2.schemas import ProviderMediaGrantBinding
from companion_daemon.world_v2.random_authority import RandomAuthority
from companion_daemon.world_v2.social_initiative import (
    SocialInitiativeContextPolicy,
    SocialInitiativePolicy,
    social_initiative_attempt_id,
)
from companion_daemon.world_v2.system_notice import (
    SYSTEM_NOTICE_TEXT,
    SQLiteSystemNoticeDispatcher,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


class _NamedNoCallModel:
    def __init__(self, model: str) -> None:
        self.model = model
        self.semantic_authority_id = f"semantic-authority:test:{model.casefold()}"

    async def complete(
        self,
        _messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        raise AssertionError(f"unexpected composition-only model call: {self.model}")


class _NamedStrictInventoryNoCallModel(_NamedNoCallModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-inventory.5"


class _NamedStrictCoverageNoCallModel(_NamedNoCallModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-coverage.5"


class _NamedStrictFullReviewNoCallModel(_NamedNoCallModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in {
            "report-relative-entailment-adjudication.3",
            "source-closure-review.7",
        }


@pytest.mark.asyncio
async def test_qq_composition_wires_independent_proactive_source_authority(
    tmp_path: Path,
) -> None:
    author = _NamedNoCallModel("qq-proactive-author")
    reviewer = _NamedStrictCoverageNoCallModel("qq-independent-source-reviewer")
    life_reviewer = _NamedNoCallModel("qq-isolated-life-source-reviewer")
    inventory = _NamedStrictInventoryNoCallModel("qq-candidate-inventory")
    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-proactive-source-authority.sqlite"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=author,
        source_closure_model=reviewer,
        life_source_closure_model=life_reviewer,
        candidate_external_proposition_inventory_model=inventory,
        use_configured_recall_embedding=False,
    )
    try:
        interior = host._host._application._turns._runtime._character_interior  # noqa: SLF001
        runtime = interior._background_driver._proactive  # noqa: SLF001
        adapter = runtime._turn._deliberation._main  # noqa: SLF001

        assert adapter._identity_frame is not None  # noqa: SLF001
        assert adapter._source_closure_reviewer is reviewer  # noqa: SLF001
        assert adapter._inventory_model is inventory  # noqa: SLF001
        development = (  # noqa: SLF001
            host._host._application._life_ecology._life_development_followup
        )
        assert development._world_author.authority_origin is author  # noqa: SLF001
        assert (  # noqa: SLF001
            development._world_author_source_rewriter.authority_origin is author
        )
        assert (  # noqa: SLF001
            development._source_closure_reviewer.authority_origin is life_reviewer
        )
        assert development._source_closure_reviewer_is_independent is True  # noqa: SLF001
        assert host.proactive_source_authority_health()["status"] == "ready"
        assert host.life_source_authority_health()["status"] == ("operational_unqualified")
    finally:
        await host.aclose()


def test_qq_c2c_production_builder_has_no_quick_reaction_injection() -> None:
    """Visible quick reactions cannot be enabled through the QQ composition API."""

    assert "quick_reaction_model" not in inspect.signature(build_qq_c2c_host).parameters
    assert (
        "_test_only_expression_episode_mode"
        not in inspect.signature(build_qq_c2c_host).parameters
    )


@pytest.mark.asyncio
async def test_qq_c2c_production_builder_rejects_bypassed_visible_episode_setting(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "qq-visible-expression-episode.sqlite",
    ).model_copy(update={"world_v2_expression_episode_mode": "on"})
    host = None
    try:
        with pytest.raises(
            ValueError,
            match="production QQ expression episode mode must be off, shadow, or stream",
        ):
            host = build_qq_c2c_host(
                settings=settings,
                recipient_id="10001",
                bootstrap_at=NOW,
                model=FakeCompanionModel(),
                use_configured_recall_embedding=False,
            )
    finally:
        if host is not None:
            await host.aclose()


def test_claimed_action_due_timer_waits_for_lease_instead_of_past_not_before() -> None:
    projection = SimpleNamespace(
        actions=(
            SimpleNamespace(
                state="claimed",
                not_before=NOW - timedelta(seconds=30),
                claim_lease=SimpleNamespace(expires_at=NOW + timedelta(minutes=2)),
            ),
        )
    )

    assert ActionDueWake.nearest_due(projection) == NOW + timedelta(minutes=2)


@pytest.mark.asyncio
async def test_action_due_timer_rebuilds_after_transient_wake_failure() -> None:
    action = SimpleNamespace(
        state="scheduled",
        not_before=NOW,
        claim_lease=None,
    )
    attempts = 0

    async def wake() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient")
        action.state = "delivered"

    async def immediate_sleep(_: float) -> None:
        await asyncio.sleep(0)

    timer = ActionDueWake(
        project=lambda: SimpleNamespace(actions=(action,)),
        wake=wake,
        now=lambda: NOW,
        sleep=immediate_sleep,
        coalesce_seconds=0,
    )
    try:
        await timer.refresh()
        for _ in range(10):
            await asyncio.sleep(0)
            if attempts == 2:
                break
        assert attempts == 2
        assert timer.diagnostics()["failure_count"] == 1
    finally:
        await timer.aclose()


@pytest.mark.asyncio
async def test_action_due_timer_rebuilds_after_initial_projection_failure() -> None:
    reads = 0

    def project() -> SimpleNamespace:
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("transient projection failure")
        return SimpleNamespace(actions=())

    async def immediate_sleep(_: float) -> None:
        await asyncio.sleep(0)

    timer = ActionDueWake(
        project=project,
        wake=lambda: asyncio.sleep(0),
        now=lambda: NOW,
        sleep=immediate_sleep,
    )
    try:
        assert await timer.refresh() is None
        for _ in range(10):
            await asyncio.sleep(0)
            if reads == 2:
                break
        assert reads == 2
        assert timer.diagnostics()["failure_count"] == 1
    finally:
        await timer.aclose()


@pytest.mark.asyncio
async def test_stale_action_timer_does_not_jump_to_new_future_due() -> None:
    class Host:
        def __init__(self) -> None:
            self.tick_calls = 0
            self.drain_calls = 0

        async def action_due_projection(self) -> SimpleNamespace:
            return SimpleNamespace(
                actions=(
                    SimpleNamespace(
                        state="claimed",
                        not_before=NOW - timedelta(minutes=1),
                        claim_lease=SimpleNamespace(expires_at=NOW + timedelta(minutes=2)),
                    ),
                )
            )

        async def current_logical_time(self) -> datetime:
            return NOW

        async def tick(self, _: object) -> None:
            self.tick_calls += 1

        async def drain_actions_once(self) -> None:
            self.drain_calls += 1

        def close(self) -> None:
            return None

    platform = Host()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_now=lambda: NOW,
    )
    try:
        await host._wake_due_actions()  # noqa: SLF001
        assert platform.tick_calls == 0
        assert platform.drain_calls == 0
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cognition_seconds", "dispatch_delay_seconds"),
    ((13.0, 0.0), (11.95, 0.1)),
)
@pytest.mark.asyncio
async def test_qq_visible_reply_still_reaches_delivery_after_cognition_exhausts_turn_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cognition_seconds: float,
    dispatch_delay_seconds: float,
) -> None:
    monotonic = {"now": 0.0}
    wall = {"now": NOW}
    delivered: list[str] = []
    visible_reply_count = 0

    class _BudgetExhaustingHost:
        async def inbound(self, _inbound):  # type: ignore[no-untyped-def]
            monotonic["now"] += cognition_seconds
            wall["now"] += timedelta(seconds=cognition_seconds)
            return SimpleNamespace(
                status="action_authorized",
                authorized_action_ids=("action:visible-reply",),
                scheduled_action_ids=(),
            )

        async def drain_action(self, action_id: str):  # type: ignore[no-untyped-def]
            await asyncio.sleep(dispatch_delay_seconds)
            delivered.append(action_id)
            return ActionPumpResult(
                action_id=action_id,
                action_kind="reply",
                status="settled",
                provider_status="delivered",
            )

        def close(self) -> None:
            return None

    async def advance(seconds: float) -> None:
        monotonic["now"] += seconds
        wall["now"] += timedelta(seconds=seconds)

    def record_visible_reply() -> None:
        nonlocal visible_reply_count
        visible_reply_count += 1

    monkeypatch.setattr(
        "companion_daemon.world_v2.qq_c2c_host.record_visible_reply",
        record_visible_reply,
    )
    policy = InteractiveTurnBudgetPolicy(
        total_seconds=12.0,
        hedge_after_seconds=2.5,
        acceptance_dispatch_reserve_seconds=1.2,
        clock=lambda: monotonic["now"],
        sleep=advance,
        wall_clock=lambda: wall["now"],
    )
    host = QQC2CHost(
        host=_BudgetExhaustingHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "exhausted-visible-reply.sqlite"),
        ingress_now=lambda: wall["now"],
        ingress_sleep=advance,
        interactive_turn_budget_policy=policy,
    )
    try:
        result = await host.inbound_text(
            message_id="slow-visible-reply",
            recipient_id="10001",
            text="你还在吗？",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    assert delivered == ["action:visible-reply"]
    assert visible_reply_count == 1


@pytest.mark.asyncio
async def test_qq_user_perceived_reply_log_uses_observation_clock_not_virtual_pacing_clock(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    pacing_clock = {"now": NOW}
    observation_clock_ns = {"now": 1_000_000_000}

    class _Host:
        async def inbound(self, _inbound):  # type: ignore[no-untyped-def]
            # A real model/provider wait advances the observation clock while
            # the fast audit's pacing clock intentionally stays frozen.
            observation_clock_ns["now"] += 7_000_000_000
            return SimpleNamespace(
                status="action_authorized",
                authorized_action_ids=(
                    "action:first-visible-reply",
                    "action:later-beat",
                ),
                scheduled_action_ids=(),
            )

        async def drain_action(self, action_id: str):  # type: ignore[no-untyped-def]
            observation_clock_ns["now"] += 2_000_000_000
            return ActionPumpResult(
                action_id=action_id,
                action_kind="reply",
                status="settled",
                provider_status="delivered",
            )

        def close(self) -> None:
            return None

    async def skip_pacing(seconds: float) -> None:
        pacing_clock["now"] += timedelta(seconds=max(0.0, seconds))

    host = QQC2CHost(
        host=_Host(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "observed-latency.sqlite"),
        ingress_now=lambda: pacing_clock["now"],
        ingress_sleep=skip_pacing,
        observation_clock_ns=lambda: observation_clock_ns["now"],
    )
    caplog.set_level(logging.WARNING)
    try:
        result = await host.inbound_text(
            message_id="real-observation-clock",
            recipient_id="10001",
            text="在吗",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    latency_messages = [
        record.getMessage()
        for record in caplog.records
        if "user_perceived_reply_ms=" in record.getMessage()
    ]
    assert len(latency_messages) == 1
    assert "user_perceived_reply_ms=9000.0" in latency_messages[0]
    assert "measurement_clock=monotonic" in latency_messages[0]


@pytest.mark.asyncio
async def test_qq_inbound_drains_every_immediately_due_beat_from_its_expression_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drained: list[str] = []
    visible_reply_count = 0
    clock = {"now": NOW}

    class _MultiBeatHost:
        async def inbound(self, _inbound):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="action_authorized",
                authorized_action_ids=(
                    "action:typing",
                    "action:text:opening",
                    "action:text:substantive",
                ),
                scheduled_action_ids=(),
            )

        async def drain_action(self, action_id: str) -> ActionPumpResult:
            drained.append(action_id)
            return ActionPumpResult(
                action_id=action_id,
                action_kind=("typing" if action_id == "action:typing" else "reply"),
                status="settled",
                provider_status="delivered",
            )

        def close(self) -> None:
            return None

    async def advance(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    def record_visible_reply() -> None:
        nonlocal visible_reply_count
        visible_reply_count += 1

    monkeypatch.setattr(
        "companion_daemon.world_v2.qq_c2c_host.record_visible_reply",
        record_visible_reply,
    )
    host = QQC2CHost(
        host=_MultiBeatHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "multi-beat-inline-drain.sqlite"),
        ingress_now=lambda: clock["now"],
        ingress_sleep=advance,
    )
    try:
        result = await host.inbound_text(
            message_id="multi-beat-inline-drain",
            recipient_id="10001",
            text="想到什么就连着说。",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    assert drained == [
        "action:typing",
        "action:text:opening",
        "action:text:substantive",
    ]
    assert visible_reply_count == 1


@pytest.mark.asyncio
async def test_qq_authorized_action_without_provider_acceptance_is_not_counted_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    visible_reply_count = 0
    clock = {"now": NOW}

    class _UndeliveredHost:
        async def inbound(self, _inbound):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="action_authorized",
                authorized_action_ids=("action:not-yet-visible",),
                scheduled_action_ids=(),
            )

        async def drain_action(self, _action_id):  # type: ignore[no-untyped-def]
            return None

        def close(self) -> None:
            return None

    async def advance(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    def record_visible_reply() -> None:
        nonlocal visible_reply_count
        visible_reply_count += 1

    monkeypatch.setattr(
        "companion_daemon.world_v2.qq_c2c_host.record_visible_reply",
        record_visible_reply,
    )
    host = QQC2CHost(
        host=_UndeliveredHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "not-yet-visible.sqlite"),
        ingress_now=lambda: clock["now"],
        ingress_sleep=advance,
    )
    try:
        result = await host.inbound_text(
            message_id="authorized-but-not-delivered",
            recipient_id="10001",
            text="你还在吗？",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    assert visible_reply_count == 0


@pytest.mark.asyncio
async def test_qq_provider_ack_is_recorded_but_not_claimed_as_user_visible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    visible_reply_count = 0
    dispatch_ack_count = 0
    clock = {"now": NOW}

    class _AckOnlyHost:
        async def inbound(self, _inbound):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="action_authorized",
                authorized_action_ids=("action:ack-only",),
                scheduled_action_ids=(),
            )

        async def drain_action(self, action_id: str) -> ActionPumpResult:
            return ActionPumpResult(
                action_id=action_id,
                action_kind="reply",
                status="settled",
                provider_status="provider_accepted",
            )

        def close(self) -> None:
            return None

    def record_visible_reply() -> None:
        nonlocal visible_reply_count
        visible_reply_count += 1

    def record_dispatch_ack() -> None:
        nonlocal dispatch_ack_count
        dispatch_ack_count += 1

    async def advance(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    monkeypatch.setattr(
        "companion_daemon.world_v2.qq_c2c_host.record_visible_reply",
        record_visible_reply,
    )
    monkeypatch.setattr(
        "companion_daemon.world_v2.qq_c2c_host.record_dispatch_ack",
        record_dispatch_ack,
        raising=False,
    )
    host = QQC2CHost(
        host=_AckOnlyHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "ack-is-not-visible.sqlite"),
        ingress_now=lambda: clock["now"],
        ingress_sleep=advance,
        observation_clock_ns=lambda: 9_000_000_000,
    )
    caplog.set_level(logging.WARNING)
    try:
        result = await host.inbound_text(
            message_id="ack-is-not-visible",
            recipient_id="10001",
            text="看得到吗？",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    assert dispatch_ack_count == 1
    assert visible_reply_count == 0
    assert not any("user_perceived_reply_ms=" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_qq_verified_scheduled_delivery_records_visibility_without_fake_latency(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    visible_reply_count = 0

    class _VerifiedDeliveryHost:
        async def drain_actions_once(self) -> ActionPumpResult:
            # This is the result ActionPump returns after a provider ACK is
            # upgraded by get_msg (or equivalent strong delivery evidence).
            return ActionPumpResult(
                action_id="action:verified-later",
                action_kind="reply",
                status="settled",
                provider_status="delivered",
            )

        def close(self) -> None:
            return None

    def record_visible_reply() -> None:
        nonlocal visible_reply_count
        visible_reply_count += 1

    monkeypatch.setattr(
        "companion_daemon.world_v2.qq_c2c_host.record_visible_reply",
        record_visible_reply,
    )
    host = QQC2CHost(
        host=_VerifiedDeliveryHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_now=lambda: NOW,
    )
    caplog.set_level(logging.WARNING)
    try:
        result = await host._drain_scheduled_action_once()  # noqa: SLF001
    finally:
        await host.aclose()

    assert result.provider_status == "delivered"
    assert visible_reply_count == 1
    assert not any("user_perceived_reply_ms=" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_qq_scheduler_does_not_hold_ingress_lock_during_slow_background_and_rebases_tick(
    tmp_path: Path,
) -> None:
    entered_background = asyncio.Event()
    release_background = asyncio.Event()
    clock = {"now": NOW}

    class _ConcurrentHost:
        def __init__(self) -> None:
            self.background_calls = 0
            self.ticks = []

        async def inbound(self, _inbound):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="observed_only", authorized_action_ids=(), scheduled_action_ids=()
            )

        async def drain_action(self, _action_id):  # type: ignore[no-untyped-def]
            return None

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.background_calls += 1
            if self.background_calls == 1:
                entered_background.set()
                await release_background.wait()
            return None

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return NOW

        async def tick(self, tick):  # type: ignore[no-untyped-def]
            self.ticks.append(tick)
            return SimpleNamespace(status="observed_only")

        async def drain_scheduled_work(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(action_statuses=(), background_statuses=())

        def close(self) -> None:
            return None

    async def _advance_ingress_window(_delay: float) -> None:
        clock["now"] += timedelta(seconds=1)

    platform = _ConcurrentHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "scheduler-ingress.sqlite"),
        ingress_now=lambda: clock["now"],
        ingress_sleep=_advance_ingress_window,
    )
    requested_boundary = NOW + timedelta(hours=1)
    scheduler = asyncio.create_task(
        host.scheduler_once(
            observed_at=requested_boundary, max_action_units=1, max_background_units=1
        )
    )
    try:
        await asyncio.wait_for(entered_background.wait(), timeout=1)
        inbound = await asyncio.wait_for(
            host.inbound_text(
                message_id="concurrent-message",
                recipient_id="10001",
                text="你在吗？",
                observed_at=NOW + timedelta(seconds=1),
            ),
            timeout=1,
        )
        assert inbound.status == "observed_only"
        clock["now"] = NOW + timedelta(seconds=5)
        release_background.set()
        await asyncio.wait_for(scheduler, timeout=1)
    finally:
        release_background.set()
        if not scheduler.done():
            scheduler.cancel()
        await host.aclose()

    assert platform.ticks[0].logical_time_to == requested_boundary + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_qq_scheduler_zero_background_budget_does_not_force_cognition(
    tmp_path: Path,
) -> None:
    class _IngressOnlyHost:
        def __init__(self) -> None:
            self.background_calls = 0
            self.scheduled_kwargs: dict[str, object] | None = None

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return NOW

        async def tick(self, _tick):  # type: ignore[no-untyped-def]
            return SimpleNamespace(status="observed_only")

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.background_calls += 1
            raise AssertionError("zero background budget must not enter cognition")

        async def drain_scheduled_work(self, **kwargs):  # type: ignore[no-untyped-def]
            self.scheduled_kwargs = kwargs
            return SimpleNamespace(action_statuses=(), background_statuses=())

        def close(self) -> None:
            return None

    platform = _IngressOnlyHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "scheduler-zero-background.sqlite"),
        ingress_now=lambda: NOW,
    )
    try:
        result = await host.scheduler_once(
            observed_at=NOW + timedelta(seconds=1),
            max_action_units=0,
            max_background_units=0,
        )
    finally:
        await host.aclose()

    assert result.action_statuses == ()
    assert result.background_statuses == ()
    assert platform.background_calls == 0
    assert platform.scheduled_kwargs is not None
    assert platform.scheduled_kwargs["max_background_units"] == 0


@pytest.mark.asyncio
async def test_qq_scheduler_isolates_direct_post_tick_background_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry wake must not let the direct preflight bypass host isolation."""

    retry_due = NOW + timedelta(minutes=10)

    class _FailingPostTickHost:
        def __init__(self) -> None:
            self.logical_time = NOW
            self.scheduled_kwargs: dict[str, object] | None = None
            self.background_calls = 0

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return self.logical_time

        async def action_due_projection(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(logical_time=self.logical_time, actions=())

        async def tick(self, tick):  # type: ignore[no-untyped-def]
            self.logical_time = tick.logical_time_to
            return SimpleNamespace(status="observed_only", authorized_action_ids=())

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.background_calls += 1
            raise ValueError("historical background trigger")

        async def drain_scheduled_work(self, **kwargs):  # type: ignore[no-untyped-def]
            self.scheduled_kwargs = kwargs
            return SimpleNamespace(action_statuses=(), background_statuses=())

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        qq_c2c_host_module,
        "next_expression_retry_due",
        lambda _projection: retry_due,
    )
    monkeypatch.setattr(
        qq_c2c_host_module,
        "next_proactive_retry_due",
        lambda _projection: None,
        raising=False,
    )
    platform = _FailingPostTickHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "post-tick-background-failure.sqlite"),
        ingress_now=lambda: NOW,
        idle_heartbeat_seconds=3_600,
    )
    try:
        drained = await host.scheduler_once(
            observed_at=NOW + timedelta(minutes=11),
            max_action_units=0,
            max_background_units=1,
        )
    finally:
        await host.aclose()

    assert platform.background_calls == 1
    assert drained.background_statuses == ("technical_failure:valueerror",)
    assert platform.scheduled_kwargs is not None
    assert platform.scheduled_kwargs["max_background_units"] == 0


@pytest.mark.asyncio
async def test_qq_scheduler_isolates_direct_pre_tick_background_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-tick bounded unit has the same technical-failure boundary."""

    class _FailingPreTickHost:
        def __init__(self) -> None:
            self.logical_time = NOW
            self.scheduled_kwargs: dict[str, object] | None = None
            self.background_calls = 0

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return self.logical_time

        async def action_due_projection(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(logical_time=self.logical_time, actions=())

        async def tick(self, tick):  # type: ignore[no-untyped-def]
            self.logical_time = tick.logical_time_to
            return SimpleNamespace(status="observed_only", authorized_action_ids=())

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.background_calls += 1
            raise ValueError("historical background trigger")

        async def drain_scheduled_work(self, **kwargs):  # type: ignore[no-untyped-def]
            self.scheduled_kwargs = kwargs
            return SimpleNamespace(action_statuses=(), background_statuses=())

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        qq_c2c_host_module,
        "next_expression_retry_due",
        lambda _projection: None,
    )
    monkeypatch.setattr(
        qq_c2c_host_module,
        "next_proactive_retry_due",
        lambda _projection: None,
        raising=False,
    )
    platform = _FailingPreTickHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "pre-tick-background-failure.sqlite"),
        ingress_now=lambda: NOW,
        idle_heartbeat_seconds=3_600,
    )
    try:
        drained = await host.scheduler_once(
            observed_at=NOW + timedelta(seconds=1),
            max_action_units=0,
            max_background_units=1,
        )
    finally:
        await host.aclose()

    assert platform.background_calls == 1
    assert drained.background_statuses == ("technical_failure:valueerror",)
    assert platform.scheduled_kwargs is not None
    assert platform.scheduled_kwargs["max_background_units"] == 0


@pytest.mark.asyncio
async def test_qq_scheduler_dispatches_retry_actions_before_more_background(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class _RetryActionHost:
        def __init__(self) -> None:
            self.background_calls = 0

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return NOW

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.background_calls += 1
            events.append(f"background:{self.background_calls}")
            if self.background_calls > 1:
                raise AssertionError(
                    "an authorized retry Action must dispatch before more background work"
                )
            return SimpleNamespace(
                status="action_authorized",
                authorized_action_ids=("action:retry:1", "action:retry:2"),
            )

        async def drain_action(self, action_id):  # type: ignore[no-untyped-def]
            events.append(f"dispatch:{action_id}")
            return SimpleNamespace(status="settled")

        async def tick(self, tick):  # type: ignore[no-untyped-def]
            events.append(f"tick:{tick.logical_time_to.isoformat()}")
            return SimpleNamespace(status="observed_only")

        async def drain_scheduled_work(self, **_kwargs):  # type: ignore[no-untyped-def]
            events.append("scheduled-work")
            return SimpleNamespace(action_statuses=(), background_statuses=())

        def close(self) -> None:
            return None

    platform = _RetryActionHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "retry-action-priority.sqlite"),
    )
    try:
        drained = await host.scheduler_once(
            observed_at=NOW + timedelta(seconds=1),
            max_action_units=2,
            max_background_units=2,
        )
    finally:
        await host.aclose()

    assert events[0] == "background:1"
    assert events[1].startswith("tick:")
    assert events[2:4] == [
        "dispatch:action:retry:1",
        "dispatch:action:retry:2",
    ]
    assert drained.action_statuses == ("settled", "settled")
    assert platform.background_calls == 1


@pytest.mark.asyncio
async def test_qq_scheduler_advances_all_exact_due_boundaries_before_expression_retry(
    tmp_path: Path,
) -> None:
    action_due_times = (
        NOW + timedelta(minutes=2),
        NOW + timedelta(minutes=5),
    )
    retry_due = NOW + timedelta(minutes=10)
    attempt_id = "attempt:expression:one"
    observation_id = "observation:one"
    observation_event_id = "event:observation:one"
    payload_hash = "a" * 64

    class _MultipleBoundaryHost:
        def __init__(self) -> None:
            self.logical_time = NOW
            self.tick_targets: list[datetime] = []
            self.background_logical_times: list[datetime] = []

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return self.logical_time

        async def action_due_projection(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                logical_time=self.logical_time,
                actions=tuple(
                    SimpleNamespace(
                        state="scheduled",
                        not_before=due,
                        claim_lease=None,
                    )
                    for due in action_due_times
                ),
                trigger_processes=(
                    SimpleNamespace(
                        trigger_id="trigger:expression:one",
                        process_kind="expression_episode",
                        state="claimed",
                        source_evidence_ref=observation_id,
                        attempt_ids=(attempt_id,),
                        claim_lease=SimpleNamespace(
                            attempt_id=attempt_id,
                            acquired_at=NOW,
                            expires_at=retry_due,
                        ),
                    ),
                ),
                message_observations=(
                    SimpleNamespace(
                        observation_id=observation_id,
                        world_revision=1,
                        event_payload_hash=payload_hash,
                    ),
                ),
                committed_world_event_refs=(
                    SimpleNamespace(
                        event_id=observation_event_id,
                        event_type="ObservationRecorded",
                        world_revision=1,
                        payload_hash=payload_hash,
                    ),
                ),
                model_result_audits=(
                    SimpleNamespace(
                        trigger_ref=observation_event_id,
                        attempt_id=attempt_id,
                        deliberation_result_id="deliberation:expression:one",
                        proposal_hash=None,
                        attempt_index=1,
                        attempt_count=2,
                    ),
                ),
                proposal_audits=(),
                minimal_reply_manifests=(),
                expression_plan_manifests=(),
            )

        async def tick(self, tick):  # type: ignore[no-untyped-def]
            self.tick_targets.append(tick.logical_time_to)
            self.logical_time = tick.logical_time_to
            return SimpleNamespace(status="observed_only", authorized_action_ids=())

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.background_logical_times.append(self.logical_time)
            if self.logical_time < retry_due:
                raise AssertionError(
                    "the expression-retry reserve was consumed at an earlier Action boundary"
                )
            return SimpleNamespace(
                status="observed_only",
                work_status="expression-retry",
                authorized_action_ids=(),
            )

        async def drain_scheduled_work(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(action_statuses=(), background_statuses=())

        def close(self) -> None:
            return None

    platform = _MultipleBoundaryHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "multiple-due-boundaries.sqlite"),
        ingress_now=lambda: NOW,
        idle_heartbeat_seconds=600,
    )
    try:
        drained = await host.scheduler_once(
            observed_at=NOW + timedelta(minutes=11),
            max_action_units=0,
            max_background_units=1,
        )
    finally:
        await host.aclose()

    assert platform.tick_targets == [*action_due_times, retry_due]
    assert platform.background_logical_times == [retry_due]
    assert drained.background_statuses == ("expression-retry",)


@pytest.mark.asyncio
async def test_qq_scheduler_advances_exactly_to_proactive_technical_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_due = NOW + timedelta(minutes=10)

    class _ProactiveRetryHost:
        def __init__(self) -> None:
            self.logical_time = NOW
            self.tick_reasons: list[tuple[datetime, str]] = []
            self.background_logical_times: list[datetime] = []

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return self.logical_time

        async def action_due_projection(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(logical_time=self.logical_time, actions=())

        async def tick(self, tick):  # type: ignore[no-untyped-def]
            self.tick_reasons.append((tick.logical_time_to, tick.reason))
            self.logical_time = tick.logical_time_to
            return SimpleNamespace(status="observed_only", authorized_action_ids=())

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.background_logical_times.append(self.logical_time)
            if self.logical_time < retry_due:
                raise AssertionError("proactive retry work ran before its durable deadline")
            return SimpleNamespace(
                status="failed_safe",
                work_status="proactive-technical-retry",
                authorized_action_ids=(),
            )

        async def drain_scheduled_work(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(action_statuses=(), background_statuses=())

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        qq_c2c_host_module,
        "next_expression_retry_due",
        lambda _projection: None,
    )
    monkeypatch.setattr(
        qq_c2c_host_module,
        "next_proactive_retry_due",
        lambda _projection: retry_due,
        raising=False,
    )
    platform = _ProactiveRetryHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "proactive-retry-wake.sqlite"),
        ingress_now=lambda: NOW,
        idle_heartbeat_seconds=3_600,
    )
    try:
        drained = await host.scheduler_once(
            observed_at=NOW + timedelta(minutes=11),
            max_action_units=0,
            max_background_units=1,
        )
    finally:
        await host.aclose()

    assert platform.tick_reasons == [(retry_due, "qq_c2c_proactive_retry_wake")]
    assert platform.background_logical_times == [retry_due]
    assert drained.background_statuses == ("proactive-technical-retry",)


@pytest.mark.asyncio
async def test_qq_scheduler_preserves_retry_unit_when_slow_background_crosses_due(
    tmp_path: Path,
) -> None:
    retry_due = NOW + timedelta(minutes=10)
    observed_at = retry_due - timedelta(seconds=1)
    attempt_id = "attempt:expression:slow-crossing"
    observation_id = "observation:slow-crossing"
    observation_event_id = "event:observation:slow-crossing"
    payload_hash = "b" * 64
    wall_clock = {"now": NOW}

    class _SlowCrossingHost:
        def __init__(self) -> None:
            self.logical_time = NOW
            self.background_logical_times: list[datetime] = []
            self.tick_targets: list[datetime] = []

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return self.logical_time

        async def action_due_projection(self):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                logical_time=self.logical_time,
                actions=(),
                trigger_processes=(
                    SimpleNamespace(
                        trigger_id="trigger:expression:slow-crossing",
                        process_kind="expression_episode",
                        state="claimed",
                        source_evidence_ref=observation_id,
                        attempt_ids=(attempt_id,),
                        claim_lease=SimpleNamespace(
                            attempt_id=attempt_id,
                            acquired_at=NOW,
                            expires_at=retry_due,
                        ),
                    ),
                ),
                message_observations=(
                    SimpleNamespace(
                        observation_id=observation_id,
                        world_revision=1,
                        event_payload_hash=payload_hash,
                    ),
                ),
                committed_world_event_refs=(
                    SimpleNamespace(
                        event_id=observation_event_id,
                        event_type="ObservationRecorded",
                        world_revision=1,
                        payload_hash=payload_hash,
                    ),
                ),
                model_result_audits=(
                    SimpleNamespace(
                        trigger_ref=observation_event_id,
                        attempt_id=attempt_id,
                        proposal_hash=None,
                        attempt_index=1,
                        attempt_count=2,
                        deliberation_result_id=("deliberation:expression:slow-crossing"),
                    ),
                ),
                proposal_audits=(),
                minimal_reply_manifests=(),
                expression_plan_manifests=(),
            )

        async def tick(self, tick):  # type: ignore[no-untyped-def]
            self.tick_targets.append(tick.logical_time_to)
            self.logical_time = tick.logical_time_to
            return SimpleNamespace(status="observed_only", authorized_action_ids=())

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            self.background_logical_times.append(self.logical_time)
            if len(self.background_logical_times) == 1:
                wall_clock["now"] += timedelta(seconds=2)
                return SimpleNamespace(
                    status="observed_only",
                    work_status="unrelated-background",
                    authorized_action_ids=(),
                )
            if self.logical_time < retry_due:
                raise AssertionError("slow unrelated work consumed the retry's reserved budget")
            return SimpleNamespace(
                status="observed_only",
                work_status="expression-retry",
                authorized_action_ids=(),
            )

        async def drain_scheduled_work(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(action_statuses=(), background_statuses=())

        def close(self) -> None:
            return None

    platform = _SlowCrossingHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "slow-crossing-retry.sqlite"),
        ingress_now=lambda: wall_clock["now"],
        idle_heartbeat_seconds=600,
    )
    try:
        drained = await host.scheduler_once(
            observed_at=observed_at,
            max_action_units=0,
            max_background_units=2,
        )
    finally:
        await host.aclose()

    assert platform.tick_targets == [retry_due]
    assert platform.background_logical_times == [NOW, retry_due]
    assert drained.background_statuses == (
        "unrelated-background",
        "expression-retry",
    )


@pytest.mark.asyncio
async def test_qq_scheduler_persists_only_one_idle_heartbeat_per_ten_minutes(
    tmp_path: Path,
) -> None:
    class _HeartbeatHost:
        def __init__(self) -> None:
            self.logical_time = NOW
            self.ticks = []

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return self.logical_time

        async def tick(self, tick):  # type: ignore[no-untyped-def]
            self.ticks.append(tick)
            self.logical_time = tick.logical_time_to
            return SimpleNamespace(status="observed_only")

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            return None

        async def drain_scheduled_work(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(action_statuses=(), background_statuses=())

        def close(self) -> None:
            return None

    platform = _HeartbeatHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "idle-heartbeat.sqlite"),
        ingress_now=lambda: NOW,
        idle_heartbeat_seconds=600,
    )
    try:
        for seconds in (30, 60, 300, 599):
            await host.scheduler_once(
                observed_at=NOW + timedelta(seconds=seconds),
                max_action_units=0,
                max_background_units=0,
            )
        assert platform.ticks == []

        await host.scheduler_once(
            observed_at=NOW + timedelta(seconds=600),
            max_action_units=0,
            max_background_units=0,
        )
        await host.scheduler_once(
            observed_at=NOW + timedelta(seconds=630),
            max_action_units=0,
            max_background_units=0,
        )
    finally:
        await host.aclose()

    assert len(platform.ticks) == 1
    assert platform.ticks[0].logical_time_to == NOW + timedelta(seconds=600)


@pytest.mark.asyncio
async def test_qq_inbound_advances_stale_world_clock_without_waiting_for_heartbeat(
    tmp_path: Path,
) -> None:
    clock = {"now": NOW + timedelta(minutes=3)}

    class _InboundClockHost:
        def __init__(self) -> None:
            self.logical_time = NOW
            self.ticks = []
            self.inbounds = []

        async def current_logical_time(self):  # type: ignore[no-untyped-def]
            return self.logical_time

        async def tick(self, tick):  # type: ignore[no-untyped-def]
            self.ticks.append(tick)
            self.logical_time = tick.logical_time_to
            return SimpleNamespace(status="observed_only")

        async def inbound(self, inbound):  # type: ignore[no-untyped-def]
            self.inbounds.append(inbound)
            return SimpleNamespace(
                status="observed_only", authorized_action_ids=(), scheduled_action_ids=()
            )

        def close(self) -> None:
            return None

    async def advance(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    platform = _InboundClockHost()
    host = QQC2CHost(
        host=platform,  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "inbound-clock.sqlite"),
        ingress_now=lambda: clock["now"],
        ingress_sleep=advance,
        idle_heartbeat_seconds=600,
    )
    try:
        result = await host.inbound_text(
            message_id="message:clock-now",
            recipient_id="10001",
            text="现在发生的事",
            observed_at=clock["now"],
        )
    finally:
        await host.aclose()

    assert result.status == "observed_only"
    assert len(platform.ticks) == 1
    assert platform.ticks[0].reason == "qq_c2c_inbound"
    assert platform.ticks[0].logical_time_to == platform.inbounds[0].observed_at


def _visible(delivery: "_Delivery") -> list[tuple[str, str]]:
    """Delivered visible content, excluding a model-selected typing beat."""

    return [item for item in delivery.sent if item[1] != "typing:composing"]


class _Delivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {"status": "ok", "data": {"message_id": f"qq-{len(self.sent)}"}}

    async def send_reaction(
        self, recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        self.sent.append((recipient_id, f"reaction:{message_id}:{reaction_id}"))
        return {"status": "ok", "data": {"message_id": f"reaction-{len(self.sent)}"}}

    async def send_sticker(self, recipient_id: str, *, sticker_id: str) -> dict[str, object]:
        self.sent.append((recipient_id, f"sticker:{sticker_id}"))
        return {"status": "ok", "data": {"message_id": f"sticker-{len(self.sent)}"}}

    async def send_typing(self, recipient_id: str, *, state: str) -> dict[str, object]:
        self.sent.append((recipient_id, f"typing:{state}"))
        return {"status": "ok", "data": {"message_id": f"typing-{len(self.sent)}"}}


@pytest.mark.asyncio
async def test_qq_host_sends_distinct_system_notice_for_terminal_turn_failure(
    tmp_path: Path,
) -> None:
    class _TechnicalFailureHost:
        async def inbound(self, _inbound):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                status="deferred",
                authorized_action_ids=(),
                scheduled_action_ids=(),
                deferred_refs=("expression_episode.technical_retry_pending",),
                terminal_errors=(),
            )

        def close(self) -> None:
            return None

    clock = {"now": NOW}

    async def advance(seconds: float) -> None:
        clock["now"] += timedelta(seconds=seconds)

    delivery = _Delivery()
    notice = SQLiteSystemNoticeDispatcher(
        path=str(tmp_path / "system-notice-host.sqlite"),
        world_id="world:test",
        delivery=delivery,
        now=lambda: clock["now"],
    )
    host = QQC2CHost(
        host=_TechnicalFailureHost(),  # type: ignore[arg-type]
        recipient_id="10001",
        canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "system-notice-host.sqlite"),
        ingress_now=lambda: clock["now"],
        ingress_sleep=advance,
        system_notice_dispatcher=notice,
    )
    try:
        result = await host.inbound_text(
            message_id="message:technical-failure",
            recipient_id="10001",
            text="你还在吗？",
            observed_at=clock["now"],
        )
    finally:
        await host.aclose()

    assert result.status == "deferred"
    assert delivery.sent == [("10001", SYSTEM_NOTICE_TEXT)]


class _CanonicalStreamingFixture:
    async def complete_json_stream_with_usage(
        self,
        messages,  # type: ignore[no-untyped-def]
        *,
        temperature=0.8,  # type: ignore[no-untyped-def]
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ):
        raw = await self.complete(messages, temperature=temperature)  # type: ignore[attr-defined]
        if on_text_delta is not None:
            on_text_delta(raw)
        material = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 10,
            "output_tokens": 10,
            "thinking_tokens": 0,
            "token_provenance": "estimated",
            "transport": "fake",
            "provider": "fixture",
            "provider_usage_ref": "usage:fixture:canonical-stream",
        }
        import hashlib

        usage_hash = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return raw, ModelUsageProvenance(**material, provider_usage_hash=usage_hash)


class _OneExpressionModel(_CanonicalStreamingFixture):
    model = "fixture:one-expression"

    def __init__(self, beat: dict[str, str]) -> None:
        self.beat = beat
        self.calls = 0
        self.prompt_kinds: list[str] = []

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        joined = "\n".join(message["content"] for message in messages)
        combined = (
            "appraisal_draft and expression_draft" in joined
            and "COMBINED OUTPUT ENVELOPE" in joined
        )
        self.prompt_kinds.append("combined" if combined else "expression")
        expression = {
            "private_turn_state": {
                "inner_state_summary": "I want to use the available expression form.",
                "attended_source_refs": [],
            },
            "timing_choice": "now",
            "beats": [self.beat],
            "cadence": "conversational",
            "stance": "acknowledge_briefly",
            "brief_rationale": "The model selected one available expression form.",
            "confidence": 7000,
        }
        if not combined:
            return json.dumps(expression)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed for this fixture.",
                    "behavior_tendency": "observe",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 3000,
                },
                "expression_draft": expression,
            }
        )


class _StreamingExpressionModel(_OneExpressionModel):
    model = "fixture:one-streaming-expression"
    reports_exact_request_emission = True

    def __init__(self, *, leading_typing: bool = False) -> None:
        super().__init__({"modality": "text", "text": "unused"})
        self.leading_typing = leading_typing
        self.stream_calls = 0
        self.tail_release = asyncio.Event()
        self.tail_releases = [self.tail_release]
        self.cancelled_stream_ordinals: list[int] = []

    async def complete_json_stream_with_usage(
        self,
        messages,  # type: ignore[no-untyped-def]
        *,
        temperature=0.8,  # type: ignore[no-untyped-def]
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ):
        del temperature
        self.stream_calls += 1
        ordinal = self.stream_calls
        tail_release = self.tail_release
        if ordinal > 1:
            tail_release = asyncio.Event()
            self.tail_releases.append(tail_release)
        self.calls += 1
        joined = "\n".join(message["content"] for message in messages)
        combined = (
            "appraisal_draft" in joined
            and "character-interior-events.1" in joined
        )
        self.prompt_kinds.append("combined_stream" if combined else "expression_stream")
        first = {
            "type": "head",
            "private_turn_state": {
                "inner_state_summary": "I want to answer in two separate bubbles.",
                "attended_source_refs": [],
            },
            "timing_choice": "now",
            "beat": {
                "modality": "text",
                "text": "第一条先发。" if ordinal == 1 else f"第{ordinal}轮先发。",
            },
            "cadence": "conversational",
            "stance": "two_bubble_reply",
            "brief_rationale": "I chose two separate messages.",
            "confidence": 7000,
            "world_claims": [],
        }
        if self.leading_typing:
            first["leading_typing_beat"] = {"modality": "typing"}
        raw = json.dumps(
            {
                "protocol": "character-interior-events.1",
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed for this fixture.",
                    "behavior_tendency": "observe",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 3000,
                },
                "events": [
                    first,
                    {
                        "type": "beat",
                        "beat": {
                            "modality": "text",
                            "text": ("第二条再跟上。" if ordinal == 1 else f"第{ordinal}轮尾条。"),
                        },
                        "world_claims": [],
                    },
                    {"type": "end"},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        boundary = raw.index(',{"type":"beat"')
        if on_text_delta is not None:
            on_text_delta(raw[:boundary])
            try:
                await tail_release.wait()
            except asyncio.CancelledError:
                self.cancelled_stream_ordinals.append(ordinal)
                raise
            on_text_delta(raw[boundary:])
        material = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 10,
            "output_tokens": 10,
            "thinking_tokens": 0,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": "fixture",
            "provider_usage_ref": "usage:fixture:stream",
        }
        import hashlib

        usage_hash = hashlib.sha256(
            json.dumps(
                material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return raw, ModelUsageProvenance(
            **material,
            provider_usage_hash=usage_hash,
        )


class _SilentExpressionModel(_CanonicalStreamingFixture):
    model = "fixture:silent-expression"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        joined = "\n".join(message["content"] for message in messages)
        combined = (
            "appraisal_draft and expression_draft" in joined
            and "COMBINED OUTPUT ENVELOPE" in joined
        )
        expression = {
            "private_turn_state": {
                "inner_state_summary": "I do not want to add a visible reply this turn.",
                "attended_source_refs": [],
            },
            "timing_choice": "silent",
            "beats": [],
            "cadence": "conversational",
            "stance": "defer",
            "brief_rationale": "A reaction is available, but I choose not to use it.",
            "confidence": 7000,
        }
        if not combined:
            return json.dumps(expression)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed for this fixture.",
                    "behavior_tendency": "observe",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 3000,
                },
                "expression_draft": expression,
            }
        )


class _CombinedCharacterInteriorModel(_CanonicalStreamingFixture):
    model = "fixture:combined-character-interior"

    async def complete(self, _messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed for this fixture.",
                    "behavior_tendency": "observe",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 3000,
                },
                "expression_draft": {
                    "private_turn_state": {
                        "inner_state_summary": "I want to answer the current message directly.",
                        "attended_source_refs": [],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我在，刚看到。"}],
                    "cadence": "conversational",
                    "stance": "acknowledge_briefly",
                    "brief_rationale": "Reply to the current message.",
                    "confidence": 7000,
                }
            },
            ensure_ascii=False,
        )


class _DurableExpressionRetryModel:
    """Script one failed interactive attempt followed by a legal later retry."""

    def __init__(
        self,
        *,
        model: str,
        retry_state: dict[str, bool],
        initial_delay_seconds: float = 0.0,
    ) -> None:
        self.model = model
        self.retry_state = retry_state
        self.initial_delay_seconds = initial_delay_seconds
        self.prompts: list[str] = []
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        joined = "\n".join(message["content"] for message in messages)
        self.prompts.append(joined)
        is_combined = (
            "appraisal_draft and expression_draft" in joined
            and "COMBINED OUTPUT ENVELOPE" in joined
        )
        is_expression = (
            "Return one raw JSON ExpressionDraft" in joined
            or "raw JSON ExpressionDraft only" in joined
        )
        if not is_combined and not is_expression:
            return await self._fallback.complete(messages, temperature=temperature)
        if (
            self.initial_delay_seconds
            and not self.retry_state["ready"]
            and "failed the private-turn-state causal contract" not in joined
        ):
            await asyncio.sleep(self.initial_delay_seconds)
        expression = {
            "private_turn_state": {
                "inner_state_summary": "I want to answer the message that is still waiting.",
                "attended_source_refs": (
                    [] if self.retry_state["ready"] else ["context:not-in-the-pinned-turn"]
                ),
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "这次接住了。"}],
            "cadence": "conversational",
            "stance": "answer_directly",
            "brief_rationale": "Answer the still-unanswered observation.",
            "confidence": 7000,
            "world_claims": [],
        }
        if not is_combined:
            return json.dumps(expression, ensure_ascii=False)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed for this fixture.",
                    "behavior_tendency": "observe",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 3000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _IdentityAwareModel(_CanonicalStreamingFixture):
    model = "fixture:identity-aware"

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        system = messages[0]["content"]
        grounded = (
            "沈知栀" in system and "not as a task assistant" in system and "geoff" in system.lower()
        )
        text = "我是沈知栀，你是 Geoff。" if grounded else "我是你的 AI 助手小 Geoff。"
        expression = {
            "private_turn_state": {
                "inner_state_summary": "The stable identity is salient for this answer.",
                "attended_source_refs": [],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "cadence": "conversational",
            "stance": "answer_without_world_claims",
            "brief_rationale": "Answer from the supplied stable identity.",
            "confidence": 7000,
        }
        if "COMBINED OUTPUT ENVELOPE" not in system:
            return json.dumps(expression, ensure_ascii=False)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "Identity recall does not require a durable appraisal.",
                    "behavior_tendency": "observe",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 3000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _SelectingLifeEcologyModel:
    """Select available life authority and drive it to completion."""

    model = "test-qq-life-ecology"
    semantic_authority_id = "semantic-authority:test:qq-life-ecology-author"
    supports_required_tool_choice = True

    async def complete_json(
        self,
        messages,
        *,
        temperature: float = 0.2,
        tools=None,
        tool_choice=None,
    ):  # type: ignore[no-untyped-def]
        purpose = json.loads(messages[-1]["content"]).get("inner_turn", {}).get("purpose")
        if purpose == "activity_lifecycle_choice":
            assert tools and len(tools) == 1
            assert tools[0]["function"]["name"] == (
                "character_role_activity_lifecycle_choice_v1"
            )
            assert tool_choice == {
                "type": "function",
                "function": {"name": "character_role_activity_lifecycle_choice_v1"},
            }
        elif purpose == "life_development_choice":
            assert tools and len(tools) == 1
            assert tools[0]["function"]["name"] == (
                "character_role_life_development_choice_v1"
            )
            assert tool_choice == {
                "type": "function",
                "function": {"name": "character_role_life_development_choice_v1"},
            }
        else:
            assert tools is None
            assert tool_choice is None
        return await self.complete(messages, temperature=temperature)

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        system = messages[0]["content"]
        if "retrieval memory" in system:
            return '{"retain":false}'
        capsule = json.loads(messages[-1]["content"])
        if "You are the World Author" in system:
            anchor = capsule["capability_manifest"]["anchor_refs"][0]
            owner_actor_ref = capsule["authored_subject"]["owner_actor_ref"]
            return json.dumps(
                {
                    "decision": "propose",
                    "authored_subject_ref": owner_actor_ref,
                    "causal_authority": "character_choice",
                    "outcome_resolution_authority": "world_contingency",
                    "premise_scope": "external_opportunity",
                    "premise": "今天出现了一段可以自由安排的散步时间。",
                    "premise_claim_refs": ["local:claim:walk"],
                    "claim_declarations": [
                        {
                            "claim_id": "local:claim:walk",
                            "summary": "当前有一段可自由安排的散步时间。",
                            "scope": "novel_world_generation",
                            "subject_scope": "world_environment",
                            "source_refs": [],
                        }
                    ],
                    "timing": {"mode": "now", "duration_minutes": 5},
                    "anchor_refs": [anchor],
                    "location_ref": None,
                    "entity_refs": [],
                    "privacy_class": "shareable",
                    "outcomes": [
                        {
                            "experienced_by_ref": owner_actor_ref,
                            "text": "散步平静结束了。",
                            "privacy_class": "shareable",
                            "relative_plausibility_weight": 1,
                            "claim_refs": ["local:claim:walk"],
                            "provisional_npcs": [],
                            "dynamic_life_direction": None,
                        },
                        {
                            "experienced_by_ref": owner_actor_ref,
                            "text": "走到一半下了小雨，于是提前回来了。",
                            "privacy_class": "shareable",
                            "relative_plausibility_weight": 1,
                            "claim_refs": ["local:claim:walk"],
                            "provisional_npcs": [],
                            "dynamic_life_direction": None,
                        },
                    ],
                },
                ensure_ascii=False,
            )
        if "You are the Character Model" in system:
            return json.dumps(
                {
                    "decision": "accept",
                    "intention_summary": "我想出去走一小圈。",
                    "importance_bp": 3500,
                    "participant_refs": [],
                },
                ensure_ascii=False,
            )
        if capsule.get("inner_turn", {}).get("purpose") == "life_development_choice":
            source_refs = capsule["capability_manifest"]["source_refs"]
            return json.dumps(
                {
                    "status": "decision",
                    "summary": "我想出去走一小圈。",
                    "attended_source_refs": source_refs,
                    "decision": {
                        "source_refs": source_refs,
                        "payload": {
                            "completion": {
                                "decision": "accept",
                                "intention_summary": "我想出去走一小圈。",
                                "importance_bp": 3500,
                                "participant_refs": [],
                            }
                        },
                    },
                    "proposals": [],
                },
                ensure_ascii=False,
            )
        if capsule.get("inner_turn", {}).get("purpose") == "activity_lifecycle_choice":
            source_refs = capsule["capability_manifest"]["source_refs"]
            openings = capsule["capability_manifest"]["payload"]["openings"]
            selected = next(
                (
                    item
                    for item in openings
                    if item["safe_summary"].startswith("finish ")
                ),
                openings[0],
            )
            return json.dumps(
                {
                    "status": "decision",
                    "summary": "我愿意推进当前这段生活安排。",
                    "attended_source_refs": source_refs,
                    "decision": {
                        "source_refs": source_refs,
                        "payload": {
                            "decision": "select",
                            "selected_token": selected["opening_token"],
                        },
                    },
                    "proposals": [],
                },
                ensure_ascii=False,
            )
        if "candidate" in capsule:
            return json.dumps(
                {
                    "decision": "select",
                    "candidate_token": capsule["candidate"]["token"],
                }
            )
        openings = capsule.get("openings", [])
        if not openings:
            return '{"decision":"no_op"}'
        selected = next(
            (item for item in openings if item["safe_summary"].startswith("finish ")),
            openings[0],
        )
        return json.dumps(
            {
                "decision": "select",
                "opening_token": selected["opening_token"],
            }
        )


class _SupportingLifeSourceReviewer:
    """Independent fixture authority for the production life vertical."""

    model = "test-qq-life-source-reviewer"
    semantic_authority_id = "semantic-authority:test:qq-life-source-reviewer"

    async def complete(self, messages, *, temperature: float = 0.0):  # type: ignore[no-untyped-def]
        del temperature
        system = messages[0]["content"]
        if "focused novel-origin critic" in system:
            return json.dumps(
                {
                    "decision": "supported",
                    "unsupported_claims": [],
                    "unsupported_provisional_npcs": [],
                    "unsupported_outcome_prerequisites": [],
                    "undeclared_premise_fragments": [],
                    "reason": "The proposal introduces no prior history or imported prerequisite.",
                }
            )
        return json.dumps(
            {
                "decision": "supported",
                "unsupported_claim_ids": [],
                "undeclared_fact_fragments": [],
                "undeclared_fact_paths": [],
                "typed_location_conflicts": [],
                "reason": "The proposal-scoped novel facts are declared and source-closed.",
            }
        )


class _DurableMediaTransport:
    provider = "media:durable-test"

    async def send(self, request: MediaProviderDispatchRequest) -> PlatformDispatchReceipt:
        raise AssertionError(f"unexpected provider call for {request.action_id}")

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        return None

    async def lookup_execution_result(
        self, *, action_id: str, idempotency_key: str, request_fingerprint: str
    ) -> None:
        return None


class _NoCallMediaPlanner:
    async def lookup(self, *, planning_request_id: str):  # type: ignore[no-untyped-def]
        del planning_request_id
        return None

    async def plan(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("entry construction must not plan without an accepted candidate")


class _SelectionModel(FakeCompanionModel):
    model = "test-onebot-media-selection"


class _LaterQQModel:
    model = "test-qq-later"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable emotional change is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "present",
                    "display_strategy": "model_owned",
                    "confidence": 6000,
                },
                "expression_draft": {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "I want to defer this reply and return to it shortly."
                        ),
                        "attended_source_refs": [],
                    },
                    "timing_choice": "later",
                    "beats": [{"modality": "text", "text": "晚点我来找你。"}],
                    "cadence": "conversational",
                    "delay_seconds": 60,
                    "expires_after_seconds": 600,
                    "stance": "defer",
                    "brief_rationale": "稍后接续",
                    "confidence": 7200,
                },
            },
            ensure_ascii=False,
        )


@pytest.mark.asyncio
async def test_qq_production_composition_ticks_life_from_plan_through_experience(
    tmp_path: Path,
) -> None:
    """The actual QQ host installs and advances the complete life vertical."""

    conversation_reviewer = _SupportingLifeSourceReviewer()
    life_reviewer = _SupportingLifeSourceReviewer()
    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-life-vertical.sqlite"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_SelectingLifeEcologyModel(),
        source_closure_model=conversation_reviewer,
        life_source_closure_model=life_reviewer,
        delivery=_Delivery(),
    )
    previous = NOW
    try:
        for phase, at in (
            ("plan", NOW + timedelta(minutes=1)),
            ("start", NOW + timedelta(minutes=2)),
        ):
            await host.tick(
                tick_id=f"tick:qq-life:{phase}",
                logical_time_from=previous,
                logical_time_to=at,
                observed_at=at,
                reason="qq_production_life_vertical_test",
            )
            previous = at

        # Ordinary completion tracks the accepted schedule window, so the
        # settling wake happens only after the started plan's window closes.
        started = host._host._application._ledger.project().plans[0]  # type: ignore[attr-defined]
        assert started.status == "active"
        assert started.scheduled_window is not None
        settle_at = started.scheduled_window.closes_at + timedelta(seconds=30)
        await host.tick(
            tick_id="tick:qq-life:settle",
            logical_time_from=previous,
            logical_time_to=settle_at,
            observed_at=settle_at,
            reason="qq_production_life_vertical_test",
        )

        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    completed = [item for item in projection.plans if item.status == "completed"]
    assert len(completed) == 1
    assert completed[0].plan_id == started.plan_id
    assert len(projection.world_occurrences) == 1
    assert projection.world_occurrences[0].status == "settled"
    assert len(projection.experiences) == 1


@pytest.mark.asyncio
async def test_qq_shared_reply_audit_reaches_deferred_followup_with_one_main_call(
    tmp_path: Path,
) -> None:
    model = _LaterQQModel()
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "qq-shared-later.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
    )
    try:
        result = await host.inbound_text(
            message_id="qq-later-1",
            recipient_id="10001",
            text="你先忙吧",
            observed_at=NOW,
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert result.status == "deferred" and result.action_id is None
    assert _visible(delivery) == []
    # This fixture isolates the deferred-plan contract from the independently
    # covered two-slot shadow race.
    assert model.calls == 1
    assert len(projection.actions) == len(projection.commitments) == 1
    assert projection.actions[0].kind == "followup"


@pytest.mark.asyncio
async def test_qq_stream_mode_sends_two_units_from_one_role_author_request(
    tmp_path: Path,
) -> None:
    model = _StreamingExpressionModel()
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "qq-unit-stream.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="stream",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    inbound_task: asyncio.Task[object] | None = None
    try:
        inbound_task = asyncio.create_task(
            host.inbound_text(
                message_id="qq-stream-1",
                recipient_id="10001",
                text="你分两条回我试试",
                observed_at=NOW,
            )
        )
        for _ in range(200):
            if _visible(delivery):
                break
            await asyncio.sleep(0.01)
        assert _visible(delivery) == [("10001", "第一条先发。")]
        model.tail_release.set()
        result = await asyncio.wait_for(inbound_task, timeout=2)
        await host.drain(max_action_units=8, max_background_units=0)
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
        stream_audits = [
            item for item in projection.model_result_audits if item.parent_model_call_id is not None
        ]
        physical_audits = [
            json.loads(item.audit_json)
            for item in projection.model_result_audits
            if json.loads(item.audit_json)["route"]["router_version"] == "physical-provider-audit.1"
        ]
        pending_tails = (
            host._host._application._turns._runtime._pinned_turn._deliberation._episode_tail_tasks
        )  # type: ignore[attr-defined]
        rebuilt = host._host._application._ledger.rebuild()  # type: ignore[attr-defined]
        latency_segments = {sample.segment for sample in host.latency_samples()}
    finally:
        model.tail_release.set()
        if inbound_task is not None and not inbound_task.done():
            inbound_task.cancel()
        await asyncio.gather(
            *(task for task in (inbound_task,) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()

    assert result.status == "action_authorized"
    assert model.stream_calls == 1
    assert model.prompt_kinds.count("combined_stream") == 1
    assert "expression_stream" not in model.prompt_kinds
    assert len(stream_audits) == 2
    assert len({item.model_call_id for item in stream_audits}) == 2
    assert len({item.parent_model_call_id for item in stream_audits}) == 1
    assert len(physical_audits) == 1
    assert physical_audits[0]["status"] == "provider_completed"
    assert physical_audits[0]["response_hash"] is not None
    assert physical_audits[0]["usage"]["token_provenance"] == "provider_reported"
    assert rebuilt.semantic_hash == projection.semantic_hash
    assert pending_tails == {}
    assert "ingress_to_first_expression_frame" in latency_segments
    assert "ingress_to_first_candidate_validated" in latency_segments
    assert _visible(delivery) == [
        ("10001", "第一条先发。"),
        ("10001", "第二条再跟上。"),
    ]


@pytest.mark.asyncio
async def test_qq_stream_mode_dispatches_model_selected_typing_before_visible_text(
    tmp_path: Path,
) -> None:
    model = _StreamingExpressionModel(leading_typing=True)
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "qq-unit-stream-typing.sqlite",
            PRIMARY_USER_ID="geoff",
            QQ_ADAPTER="napcat",
            WORLD_V2_EXPRESSION_EPISODE_MODE="stream",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    inbound_task: asyncio.Task[object] | None = None
    try:
        inbound_task = asyncio.create_task(
            host.inbound_text(
                message_id="qq-stream-typing-1",
                recipient_id="10001",
                text="你分两条回我试试",
                observed_at=NOW,
            )
        )
        for _ in range(200):
            if len(delivery.sent) >= 2:
                break
            await asyncio.sleep(0.01)
        assert delivery.sent == [
            ("10001", "typing:composing"),
            ("10001", "第一条先发。"),
        ]
        model.tail_release.set()
        result = await asyncio.wait_for(inbound_task, timeout=2)
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        model.tail_release.set()
        if inbound_task is not None and not inbound_task.done():
            inbound_task.cancel()
        await asyncio.gather(
            *(task for task in (inbound_task,) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()

    assert result.status == "action_authorized"
    assert [item.kind for item in projection.actions[:2]] == ["typing", "reply"]
    assert [item.state for item in projection.actions[:2]] == [
        "provider_accepted",
        "provider_accepted",
    ]


@pytest.mark.asyncio
async def test_new_user_message_supersedes_an_unfinished_stream_tail(
    tmp_path: Path,
) -> None:
    model = _StreamingExpressionModel()
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "qq-unit-stream-interrupt.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="stream",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    first: asyncio.Task[object] | None = None
    second: asyncio.Task[object] | None = None
    try:
        first = asyncio.create_task(
            host.inbound_text(
                message_id="qq-stream-interrupt-1",
                recipient_id="10001",
                text="第一轮先说一半",
                observed_at=NOW,
            )
        )
        for _ in range(300):
            if _visible(delivery):
                break
            await asyncio.sleep(0.01)
        assert _visible(delivery) == [("10001", "第一条先发。")]

        second = asyncio.create_task(
            host.inbound_text(
                message_id="qq-stream-interrupt-2",
                recipient_id="10001",
                text="等等，我补充一句",
                observed_at=NOW + timedelta(seconds=1),
            )
        )
        for _ in range(400):
            if 1 in model.cancelled_stream_ordinals:
                break
            await asyncio.sleep(0.01)
        assert 1 in model.cancelled_stream_ordinals, (
            model.calls,
            model.prompt_kinds,
            host._host._application._turns.expression_episode_diagnostics(),  # type: ignore[attr-defined]
            host._host._application._turns._runtime._pinned_turn._deliberation._episode_tail_tasks,  # type: ignore[attr-defined]
        )
        if len(model.tail_releases) > 1:
            model.tail_releases[1].set()
        for _ in range(300):
            projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
            if len(projection.message_observations) >= 2:
                break
            await asyncio.sleep(0.01)
        assert len(projection.message_observations) == 2
    finally:
        for release in model.tail_releases:
            release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()

    visible = _visible(delivery)
    assert ("10001", "第二条再跟上。") not in visible


@pytest.mark.asyncio
async def test_qq_c2c_host_runs_text_ingress_and_restart_recovery_without_a_legacy_sender(
    tmp_path: Path,
) -> None:
    database = tmp_path / "qq-c2c-v2.sqlite"
    first_delivery = _Delivery()
    first = build_qq_c2c_host(
        settings=Settings(database_path=database, PRIMARY_USER_ID="geoff"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        delivery=first_delivery,
    )
    try:
        result = await first.inbound_text(
            message_id="onebot-message-1",
            recipient_id="10001",
            text="我今天有点累。",
            observed_at=NOW,
        )
        duplicate = await first.inbound_text(
            message_id="onebot-message-1",
            recipient_id="10001",
            text="我今天有点累。",
            observed_at=NOW,
        )
    finally:
        await first.aclose()

    assert result.status == "action_authorized"
    assert result.action_id is not None
    assert duplicate.action_id == result.action_id
    assert len(_visible(first_delivery)) == 1

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT event_json FROM world_v2_events "
            "WHERE json_extract(event_json, '$.event_type')='ObservationRecorded'"
        ).fetchall()
    assert len(rows) == 1
    observation = json.loads(json.loads(rows[0][0])["payload_json"])
    assert observation["source_event_id"].startswith("qq:10001:qq-coalesced:")
    assert observation["coalescing_metadata"]["source_event_ids"] == ["onebot-message-1"]
    assert observation["coalescing_metadata"]["policy_version"] == ("world-v2-qq-ingress-matrix.2")

    # OneBot only acknowledged acceptance.  A fresh process cannot prove the
    # terminal send, so it recovers to unknown rather than emitting a duplicate.
    second_delivery = _Delivery()
    restarted = build_qq_c2c_host(
        settings=Settings(database_path=database, PRIMARY_USER_ID="geoff"),
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(seconds=1),
        model=FakeCompanionModel(),
        delivery=second_delivery,
    )
    try:
        drained = await restarted.scheduler_once(
            observed_at=NOW + timedelta(seconds=121),
            max_action_units=3,
            max_background_units=2,
        )
    finally:
        await restarted.aclose()

    assert _visible(second_delivery) == []
    # The always-on exact-due timer may recover the action before the explicit
    # scheduler pass.  If the scheduler wins the race it must still report the
    # same unknown terminal result.
    if drained.action_statuses:
        assert any("unknown" in status for status in drained.action_statuses), drained


@pytest.mark.asyncio
async def test_qq_restart_scheduler_retries_a_deferred_expression_failure_once(
    tmp_path: Path,
) -> None:
    """A validation outage must not turn one accepted Observation into permanent silence."""

    database = tmp_path / "qq-expression-technical-retry.sqlite"
    retry_state = {"ready": False}
    primary = _DurableExpressionRetryModel(
        model="fixture:expression-retry-primary",
        retry_state=retry_state,
        initial_delay_seconds=0.05,
    )
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=3.5,
        hedge_after_seconds=0.01,
        acceptance_dispatch_reserve_seconds=0.3,
    )
    first_delivery = _Delivery()
    first = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=database,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=primary,
        world_support_model=FakeCompanionModel(),
        delivery=first_delivery,
        interactive_turn_budget_policy=budget,
        use_configured_recall_embedding=False,
    )
    try:
        failed = await first.inbound_text(
            message_id="expression-technical-retry-1",
            recipient_id="10001",
            text="你能接着说吗？",
            observed_at=NOW,
        )
        waiting_before_restart = await first.world_health_diagnostics()
    finally:
        await first.aclose()

    assert failed.status == "deferred"
    assert _visible(first_delivery) == [("10001", SYSTEM_NOTICE_TEXT)]
    assert any(
        "failed the private-turn-state causal contract" in prompt for prompt in primary.prompts
    )
    waiting_retry = waiting_before_restart["mechanisms"]["expression_retry"]
    assert waiting_before_restart["expression_retry"] == waiting_retry
    assert len(waiting_retry["pending_source_observation_refs"]) == 1
    assert len(waiting_retry["pending_trigger_ids"]) == 1
    assert waiting_retry == {
        "state": "waiting",
        "pending_count": 1,
        "waiting_count": 1,
        "due_count": 0,
        "overdue_count": 0,
        "earliest_due_at": (NOW + timedelta(minutes=10)).isoformat(),
        "max_attempt_ordinal": 1,
        "consecutive_technical_failures": 1,
        "pending_source_observation_refs": waiting_retry["pending_source_observation_refs"],
        "pending_trigger_ids": waiting_retry["pending_trigger_ids"],
        "locators_truncated": False,
        "warning": False,
        "warning_reasons": [],
    }

    retry_state["ready"] = True
    retry_delivery = _Delivery()
    restarted = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            database_path=database,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
        ),
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(minutes=10, seconds=1),
        model=primary,
        world_support_model=FakeCompanionModel(),
        delivery=retry_delivery,
        interactive_turn_budget_policy=budget,
        use_configured_recall_embedding=False,
    )
    try:
        waiting_after_restart = await restarted.world_health_diagnostics()
        assert (
            waiting_after_restart["mechanisms"]["expression_retry"]
            == waiting_before_restart["mechanisms"]["expression_retry"]
        )
        overdue_at = NOW + timedelta(minutes=12, seconds=1)
        await restarted.tick(
            tick_id="tick:expression-technical-retry-overdue",
            logical_time_from=NOW,
            logical_time_to=overdue_at,
            observed_at=overdue_at,
            reason="expression_retry_health_test",
            run_life_ecology=False,
        )
        overdue = await restarted.world_health_diagnostics()
        assert overdue["mechanisms"]["expression_retry"] == {
            **waiting_before_restart["mechanisms"]["expression_retry"],
            "state": "due",
            "waiting_count": 0,
            "due_count": 1,
            "overdue_count": 1,
            "warning": True,
            "warning_reasons": ["expression_retry_overdue"],
        }
        await restarted.scheduler_once(
            observed_at=overdue_at,
            max_action_units=8,
            max_background_units=1,
        )
        assert _visible(retry_delivery) == [("10001", "这次接住了。")]
        recovered = await restarted.world_health_diagnostics()
        assert recovered["mechanisms"]["expression_retry"] == {
            "state": "idle",
            "pending_count": 0,
            "waiting_count": 0,
            "due_count": 0,
            "overdue_count": 0,
            "earliest_due_at": None,
            "max_attempt_ordinal": 0,
            "consecutive_technical_failures": 0,
            "pending_source_observation_refs": [],
            "pending_trigger_ids": [],
            "locators_truncated": False,
            "warning": False,
            "warning_reasons": [],
        }
        await restarted.scheduler_once(
            observed_at=NOW + timedelta(minutes=20, seconds=2),
            max_action_units=8,
            # This regression is about the superseded visible-expression
            # recovery lane.  Background appraisal may legitimately use the
            # combined cognition contract and therefore contains the same
            # ExpressionDraft marker without authorizing a visible retry.
            max_background_units=0,
        )
    finally:
        await restarted.aclose()

    assert _visible(retry_delivery) == [("10001", "这次接住了。")]


@pytest.mark.asyncio
async def test_restart_waits_for_foreign_reclaimed_attempt_that_crashed_before_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "qq-expression-reclaim-crash.sqlite"
    retry_state = {"ready": False}
    primary = _DurableExpressionRetryModel(
        model="fixture:reclaim-crash-primary",
        retry_state=retry_state,
        initial_delay_seconds=0.05,
    )
    settings = Settings(
        database_path=database,
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
    )
    first = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=NOW,
        model=primary,
        world_support_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    try:
        failed = await first.inbound_text(
            message_id="reclaim-crash-1",
            recipient_id="10001",
            text="失败后重试也可能刚好重启。",
            observed_at=NOW,
        )
    finally:
        await first.aclose()
    assert failed.status == "deferred"

    retry_state["ready"] = True
    crashed = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(minutes=10, seconds=1),
        model=primary,
        world_support_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = crashed._host._application._turns._runtime  # noqa: SLF001

    async def crash_before_retry_model(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("fault injection after reclaim")

    monkeypatch.setattr(
        runtime._pinned_turn,  # noqa: SLF001
        "audit_observation",
        crash_before_retry_model,
    )
    try:
        crashed_result = await crashed.scheduler_once(
            observed_at=NOW + timedelta(minutes=10, seconds=1),
            max_action_units=8,
            max_background_units=1,
        )
        assert crashed_result.background_statuses == ("technical_failure:runtimeerror",)
    finally:
        await crashed.aclose()

    delivered = _Delivery()
    resumed = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(minutes=10, seconds=2),
        model=primary,
        world_support_model=FakeCompanionModel(),
        delivery=delivered,
        use_configured_recall_embedding=False,
    )
    try:
        await resumed.scheduler_once(
            observed_at=NOW + timedelta(minutes=10, seconds=2),
            max_action_units=8,
            max_background_units=1,
        )
        waiting = await resumed._host.action_due_projection()  # noqa: SLF001
        waiting_episode = next(
            item for item in waiting.trigger_processes if item.process_kind == "expression_episode"
        )
        assert waiting_episode.state == "claimed"
        assert len(waiting_episode.attempt_ids) == 2
        assert waiting_episode.claim_lease is not None
        assert _visible(delivered) == []

        # This is a different Runtime instance. With no durable ModelResult
        # from the reclaimed attempt, it cannot infer a crash and borrow the
        # live generation claim before the exact lease deadline.
        await resumed.scheduler_once(
            observed_at=waiting_episode.claim_lease.expires_at,
            max_action_units=8,
            max_background_units=8,
        )
        projection = await resumed._host.action_due_projection()  # noqa: SLF001
    finally:
        await resumed.aclose()

    episode = next(
        item for item in projection.trigger_processes if item.process_kind == "expression_episode"
    )
    assert len(episode.attempt_ids) == 3
    assert episode.state == "terminal"
    assert _visible(delivered) == [("10001", "这次接住了。")]


@pytest.mark.asyncio
async def test_newer_qq_inbound_supersedes_older_technical_expression_retry_after_restart(
    tmp_path: Path,
) -> None:
    """Recovery must never answer an old failed turn after a newer turn succeeds."""

    database = tmp_path / "qq-expression-retry-superseded.sqlite"
    retry_state = {"ready": False}
    primary = _DurableExpressionRetryModel(
        model="fixture:expression-supersession-primary",
        retry_state=retry_state,
        initial_delay_seconds=0.05,
    )
    budget = InteractiveTurnBudgetPolicy(
        total_seconds=3.5,
        hedge_after_seconds=0.01,
        acceptance_dispatch_reserve_seconds=0.3,
    )
    first_delivery = _Delivery()
    first = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=primary,
        world_support_model=FakeCompanionModel(),
        delivery=first_delivery,
        interactive_turn_budget_policy=budget,
        use_configured_recall_embedding=False,
    )
    try:
        failed = await first.inbound_text(
            message_id="expression-superseded-old",
            recipient_id="10001",
            text="这条没接住也不用晚点补了。",
            observed_at=NOW,
        )
        assert failed.status == "deferred"
        assert _visible(first_delivery) == [("10001", SYSTEM_NOTICE_TEXT)]

        retry_state["ready"] = True
        newer = await first.inbound_text(
            message_id="expression-superseded-new",
            recipient_id="10001",
            text="这是新的消息，回这条就好。",
            observed_at=NOW + timedelta(seconds=1),
        )
    finally:
        await first.aclose()

    assert newer.status == "action_authorized"
    assert _visible(first_delivery) == [
        ("10001", SYSTEM_NOTICE_TEXT),
        ("10001", "这次接住了。"),
    ]

    def expression_prompt_count() -> int:
        return sum(
            "Return one raw JSON ExpressionDraft" in prompt
            or "raw JSON ExpressionDraft only" in prompt
            or (
                "appraisal_draft and expression_draft" in prompt
                and "COMBINED OUTPUT ENVELOPE" in prompt
            )
            for prompt in primary.prompts
        )

    calls_after_newer_reply = expression_prompt_count()
    restarted_delivery = _Delivery()
    restarted = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(minutes=10, seconds=1),
        model=primary,
        world_support_model=FakeCompanionModel(),
        delivery=restarted_delivery,
        interactive_turn_budget_policy=budget,
        use_configured_recall_embedding=False,
    )
    try:
        # Catch up the newer reply's provider-acceptance recovery deadline
        # first.  The scheduler advances exact due boundaries in order rather
        # than jumping past them on restart.
        await restarted.scheduler_once(
            observed_at=NOW + timedelta(minutes=10, seconds=1),
            max_action_units=8,
            max_background_units=0,
        )
        await restarted.scheduler_once(
            observed_at=NOW + timedelta(minutes=10, seconds=2),
            max_action_units=8,
            max_background_units=1,
        )
        projection = await restarted._host.action_due_projection()  # noqa: SLF001
        old_observation = projection.message_observations[0]
        old_episode = next(
            item
            for item in projection.trigger_processes
            if item.process_kind == "expression_episode"
            and item.source_evidence_ref == old_observation.observation_id
        )
        assert old_episode.state == "terminal"
        assert old_episode.runtime_outcome_ref == "expression-episode:superseded-by-newer-inbound"
        assert expression_prompt_count() == calls_after_newer_reply
        assert _visible(restarted_delivery) == []

        await restarted.scheduler_once(
            observed_at=NOW + timedelta(minutes=20, seconds=2),
            max_action_units=8,
            max_background_units=1,
        )
    finally:
        await restarted.aclose()

    assert _visible(restarted_delivery) == []


@pytest.mark.asyncio
async def test_restart_recovers_an_observation_crash_before_reply_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "qq-expression-observation-crash.sqlite"
    model = _OneExpressionModel({"modality": "text", "text": "刚才中断了，但这句我接回来了。"})
    first = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = first._host._application._turns._runtime  # noqa: SLF001

    async def crash_before_model(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("fault injection after ObservationRecorded")

    monkeypatch.setattr(runtime._pinned_turn, "audit_observation", crash_before_model)  # noqa: SLF001
    try:
        with pytest.raises(RuntimeError, match="fault injection"):
            await first._host._application.inbound(  # noqa: SLF001
                platform="qq",
                platform_user_id="10001",
                platform_message_id="observation-crash-1",
                text="这句不能因为重启丢掉。",
                observed_at=NOW,
                trace_id="trace:observation-crash-1",
            )
    finally:
        await first.aclose()

    assert model.calls == 0
    delivery = _Delivery()
    restarted = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(seconds=1),
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    try:
        await restarted.scheduler_once(
            observed_at=NOW + timedelta(seconds=1),
            max_action_units=8,
            max_background_units=1,
        )
        # A new Runtime cannot know that the previous process died rather
        # than remaining inside its provider call.  The durable claim
        # therefore protects the original invocation until lease expiry;
        # recovery becomes immediate work at that exact boundary.
        assert model.calls == 0
        assert _visible(delivery) == []
        await restarted.scheduler_once(
            observed_at=NOW + timedelta(seconds=121),
            max_action_units=8,
            # One recovered CharacterInterior unit owns appraisal, affect and
            # expression at the same pinned cursor.
            max_background_units=2,
        )
    finally:
        await restarted.aclose()

    # Recovery invokes the one canonical CharacterInterior author once.  Its
    # audited combined result settles same-turn inner state and expression;
    # the retired Appraisal-then-Expression side path must not reappear.
    assert model.calls == 1
    assert model.prompt_kinds == ["combined"]
    assert _visible(delivery) == [("10001", "刚才中断了，但这句我接回来了。")]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ("now", "silent", "later"))
@pytest.mark.asyncio
async def test_restart_continues_exact_durable_reply_proposal_without_regeneration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    choice: str,
) -> None:
    database = tmp_path / f"qq-expression-proposal-crash-{choice}.sqlite"
    model = (
        _SilentExpressionModel()
        if choice == "silent"
        else _LaterQQModel()
        if choice == "later"
        else _OneExpressionModel({"modality": "text", "text": "这条只生成一次。"})
    )
    first = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = first._host._application._turns._runtime  # noqa: SLF001

    if choice == "now":

        async def crash_after_proposal(**_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("fault injection before ActionAuthorized")

        monkeypatch.setattr(
            runtime,
            "_commit_visible_acceptance",
            crash_after_proposal,
        )
    elif choice == "later":

        async def crash_after_proposal(_observation_id):  # type: ignore[no-untyped-def]
            raise RuntimeError("fault injection before deferred ActionAuthorized")

        monkeypatch.setattr(
            runtime._social_action_worker,  # noqa: SLF001
            "run_observation",
            crash_after_proposal,
        )
    else:

        async def crash_after_proposal(**_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("fault injection before model-silent completion")

        monkeypatch.setattr(
            runtime,
            "_complete_expression_episode",
            crash_after_proposal,
        )

    try:
        with pytest.raises(RuntimeError, match="fault injection"):
            await first._host._application.inbound(  # noqa: SLF001
                platform="qq",
                platform_user_id="10001",
                platform_message_id=f"proposal-crash-{choice}",
                text="请保留已经做出的决定。",
                observed_at=NOW,
                trace_id=f"trace:proposal-crash-{choice}",
            )
    finally:
        await first.aclose()

    assert model.calls == 1
    delivery = _Delivery()
    restarted = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(seconds=1),
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    try:
        await restarted.scheduler_once(
            observed_at=NOW + timedelta(seconds=1),
            max_action_units=8,
            max_background_units=1,
        )
        if choice == "later":
            await restarted.scheduler_once(
                observed_at=NOW + timedelta(seconds=61),
                max_action_units=8,
                max_background_units=0,
            )
        projection = await restarted._host.action_due_projection()  # noqa: SLF001
    finally:
        await restarted.aclose()

    assert model.calls == 1
    episode = next(
        item for item in projection.trigger_processes if item.process_kind == "expression_episode"
    )
    assert episode.state == "terminal"
    if choice == "silent":
        assert _visible(delivery) == []
        assert episode.runtime_outcome_ref == "expression-episode:model-silent"
    elif choice == "later":
        assert _visible(delivery) == [("10001", "晚点我来找你。")]
    else:
        assert _visible(delivery) == [("10001", "这条只生成一次。")]


@pytest.mark.asyncio
async def test_qq_c2c_host_turns_a_combined_inner_turn_into_a_delivered_action(
    tmp_path: Path,
) -> None:
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "qq-combined-inner-turn.sqlite", PRIMARY_USER_ID="geoff"
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_CombinedCharacterInteriorModel(),
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
    )
    runtime = host._host._application._turns._runtime  # noqa: SLF001
    assert not hasattr(runtime, "_quick_reaction_worker")
    try:
        result = await host.inbound_text(
            message_id="combined-inner-turn-1",
            recipient_id="10001",
            text="你好？",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    assert result.action_id is not None
    assert _visible(delivery) == [("10001", "我在，刚看到。")]


@pytest.mark.asyncio
async def test_qq_c2c_host_supplies_companion_and_user_identity_to_the_reply_model(
    tmp_path: Path,
) -> None:
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-identity.sqlite", PRIMARY_USER_ID="geoff"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_IdentityAwareModel(),
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
    )
    try:
        result = await host.inbound_text(
            message_id="identity-1",
            recipient_id="10001",
            text="你是谁？我是谁？",
            observed_at=NOW,
        )
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    assert _visible(delivery) == [("10001", "我是沈知栀，你是 Geoff。")]


@pytest.mark.asyncio
async def test_qq_c2c_host_accepts_pure_attachment_without_fabricating_text(
    tmp_path: Path,
) -> None:
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "qq-pure-attachment.sqlite",
            PRIMARY_USER_ID="geoff",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        delivery=delivery,
    )
    try:
        result = await host.inbound_fragment(
            QQIngressFragment(
                source_event_id="onebot-pure-image-1",
                recipient_id="10001",
                observed_at=NOW,
                content_shape="attachment",
                attachment_refs=("qq-attachment:image:sha256:" + "a" * 64,),
            )
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
        observation_ref = projection.message_observations[-1]
        event_ref = next(
            item
            for item in projection.committed_world_event_refs
            if item.world_revision == observation_ref.world_revision
            and item.event_type == "ObservationRecorded"
        )
        event, _commit = host._host._application._ledger.lookup_event_commit(  # type: ignore[attr-defined]
            event_ref.event_id
        )
    finally:
        await host.aclose()

    assert result.status in {"observed_only", "action_authorized"}
    observation = json.loads(event.payload_json)
    assert observation["text"] is None
    assert observation["attachment_refs"] == ["qq-attachment:image:sha256:" + "a" * 64]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("beat", "expected"),
    (
        ({"modality": "reaction", "reaction_id": "like"}, "reaction:onebot-expression-1:like"),
        ({"modality": "sticker", "sticker_id": "qq-face:14"}, "sticker:qq-face:14"),
    ),
)
@pytest.mark.asyncio
async def test_napcat_expression_is_selected_by_the_single_main_model_and_reaches_delivery(
    tmp_path: Path, beat: dict[str, str], expected: str
) -> None:
    model = _OneExpressionModel(beat)
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / f"qq-expression-{beat['modality']}.sqlite",
            QQ_ADAPTER="napcat",
            PRIMARY_USER_ID="geoff",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
    )
    try:
        result = await host.inbound_text(
            message_id="onebot-expression-1",
            recipient_id="10001",
            text="终于做完了。",
            observed_at=NOW,
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    assert model.calls == 1
    assert delivery.sent[-1] == ("10001", expected)
    assert len([item for item in delivery.sent if item[1] != "typing:composing"]) <= 1
    # NapCat's synchronous response proves provider acceptance, not terminal
    # delivery.  A dependent next beat must wait for a later terminal receipt;
    # the generic lifecycle tests cover that exact receipt-driven transition.
    assert projection.actions[-1].state == "provider_accepted"
    assert projection.expression_beats[-1].state == "authorized"
    assert projection.expression_plans[-1].state == "authorized"


@pytest.mark.asyncio
async def test_napcat_typing_only_choice_cannot_become_a_silent_expression_plan(
    tmp_path: Path,
) -> None:
    model = _OneExpressionModel({"modality": "typing"})
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "qq-expression-typing-only.sqlite",
            QQ_ADAPTER="napcat",
            PRIMARY_USER_ID="geoff",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
    )
    try:
        result = await host.inbound_text(
            message_id="onebot-expression-typing-only-1",
            recipient_id="10001",
            text="终于做完了。",
            observed_at=NOW,
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert result.status == "deferred"
    assert projection.actions == ()
    assert projection.expression_plans == ()
    assert delivery.sent == [("10001", SYSTEM_NOTICE_TEXT)]


@pytest.mark.asyncio
async def test_napcat_main_model_can_choose_silence_without_host_owned_typing(
    tmp_path: Path,
) -> None:
    model = _SilentExpressionModel()
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "qq-expression-silent.sqlite",
            QQ_ADAPTER="napcat",
            PRIMARY_USER_ID="geoff",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        world_support_model=FakeCompanionModel(),
        delivery=delivery,
    )
    try:
        result = await host.inbound_text(
            message_id="onebot-expression-silent-1",
            recipient_id="10001",
            text="你也可以点个表情，但不必回应。",
            observed_at=NOW,
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert result.status == "observed_only" and result.action_id is None
    assert model.calls == 1
    assert delivery.sent == [] and projection.actions == ()
    assert projection.proposal_audits[-1].proposal_id.startswith("proposal:expression:")


@pytest.mark.asyncio
async def test_qq_c2c_host_rejects_an_unconfigured_user_before_it_can_enter_the_world(
    tmp_path: Path,
) -> None:
    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-c2c-v2-user.sqlite"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        delivery=_Delivery(),
    )
    try:
        with pytest.raises(ValueError, match="not configured"):
            await host.inbound_text(
                message_id="onebot-message-foreign",
                recipient_id="20002",
                text="不应被映射到默认用户",
                observed_at=NOW,
            )
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_qq_c2c_host_composes_only_an_explicit_durable_media_transport(
    tmp_path: Path,
) -> None:
    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-c2c-v2-media.sqlite"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        delivery=_Delivery(),
        media_transport=_DurableMediaTransport(),
    )
    try:
        assert host._host._application._media_execution_worker is not None
    finally:
        await host.aclose()


def test_real_onebot_entry_carries_complete_media_deployment_and_defaults_unavailable(
    tmp_path: Path,
) -> None:
    deployment = MediaPreviewDeployment(
        planner=_NoCallMediaPlanner(),
        acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(
                grant_id="grant:qq-onebot-preview",
                grant_revision=1,
            ),
            account_id="account:qq-onebot-preview",
            account_window_id="window:qq-onebot-preview",
            account_limit=3,
            amount_limit=1,
        ),
    )
    configured = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            database_path=tmp_path / "qq-onebot-media.sqlite",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        ),
        use_fake_model=True,
        media_preview=deployment,
        media_transport=_DurableMediaTransport(),
    )
    default = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            database_path=tmp_path / "qq-onebot-default.sqlite",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        ),
        use_fake_model=True,
    )
    with TestClient(configured) as client:
        health = client.get("/health")
        assert health.status_code == 200
        scheduler_health = health.json()["scheduler"]
        assert scheduler_health["task_running"] is True
        assert scheduler_health["interval_seconds"] == 15.0
        assert scheduler_health["passes_started"] >= 1
        configured_application = configured.state.qq_c2c_host._host._application  # type: ignore[attr-defined]
        assert configured_application._media_preview_conductor is not None  # type: ignore[attr-defined]
        assert configured_application._media_execution_worker is not None  # type: ignore[attr-defined]
    with TestClient(default) as client:
        assert client.get("/health").status_code == 200
        default_application = default.state.qq_c2c_host._host._application  # type: ignore[attr-defined]
        assert default_application._media_preview_conductor is None  # type: ignore[attr-defined]
        assert default_application._media_execution_worker is None  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="must be supplied together"):
        create_qq_c2c_onebot_app(
            adapter="napcat",
            settings=Settings(
                database_path=tmp_path / "qq-onebot-partial.sqlite",
                NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
            ),
            use_fake_model=True,
            media_transport=_DurableMediaTransport(),
        )


def test_real_onebot_entry_refuses_an_implicit_fake_character_provider(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        RuntimeError,
        match="requires a configured real character provider",
    ):
        create_qq_c2c_onebot_app(
            adapter="napcat",
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY=None,
                OPENAI_API_KEY=None,
                database_path=tmp_path / "qq-onebot-missing-character-provider.sqlite",
                NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
            ),
            use_fake_model=False,
        )


def test_onebot_entry_accepts_explicit_distinct_test_authorities_without_provider_env(
    tmp_path: Path,
) -> None:
    author = _NamedNoCallModel("isolated-explicit-author")
    reviewer = _NamedStrictFullReviewNoCallModel("isolated-explicit-reviewer")
    life_reviewer = _NamedNoCallModel("isolated-explicit-life-reviewer")

    app = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY=None,
            OPENAI_API_KEY=None,
            OPENROUTER_API_KEY=None,
            database_path=tmp_path / "qq-onebot-explicit-authorities.sqlite",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        ),
        _test_only_model=author,
        _test_only_world_support_model=author,
        _test_only_source_closure_model=reviewer,
        _test_only_life_source_closure_model=life_reviewer,
    )

    try:
        health = app.state.qq_c2c_host.proactive_source_authority_health()
        assert health["status"] == "ready"
        assert health["author_model"] == "isolated-explicit-author"
        assert health["reviewer_model"] == "isolated-explicit-reviewer"
        assert app.state.qq_c2c_host.life_source_authority_health()["status"] == (
            "operational_unqualified"
        )
    finally:
        asyncio.run(app.state.qq_c2c_host.aclose())


def test_onebot_test_authority_injection_rejects_non_rr3_v7_reviewer(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="exact RR.3/V7 qualification"):
        create_qq_c2c_onebot_app(
            adapter="napcat",
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY=None,
                OPENAI_API_KEY=None,
                OPENROUTER_API_KEY=None,
                database_path=tmp_path / "qq-onebot-unqualified-test-reviewer.sqlite",
                NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
            ),
            _test_only_model=_NamedNoCallModel("isolated-explicit-author"),
            _test_only_source_closure_model=_NamedStrictCoverageNoCallModel("unqualified-reviewer"),
        )


def test_qq_health_reports_a_running_scheduler_even_when_the_world_is_starved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "qq-onebot-starved.sqlite",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
        ),
        use_fake_model=True,
        scheduler_interval_seconds=3_600,
    )

    async def _healthy_but_no_world_work(**_kwargs: object) -> QQC2CDrainResult:
        return QQC2CDrainResult(action_statuses=(), background_statuses=())

    monkeypatch.setattr(app.state.qq_c2c_host, "scheduler_once", _healthy_but_no_world_work)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    scheduler = response.json()["scheduler"]
    assert scheduler["status"] == "running"
    initiative = scheduler["initiative"]
    reliability = initiative.pop("reliability_24h")
    assert reliability["window_hours"] == 24
    assert reliability["attempt_count"] == 0
    assert reliability["consideration_count"] == 0
    assert reliability["warning"] is False
    assert initiative == {
        "last_status": None,
        "last_reason": None,
        "pending_opportunity_count": 0,
        "pending_process_count": 0,
        "pending_action_count": 0,
        "spontaneous_candidate_due": False,
        "state": "waiting_context",
        "last_considered_at": None,
        "last_model_decision": None,
        "last_decision_reason": None,
        "last_impulse_summary": None,
        "last_grounding_outcome": None,
        "grounding_corrected_count": 0,
        "grounding_rejected_count": 0,
        "stimulus_source_count": 0,
        "stimulus_merge_window_seconds": 600,
        "pending_expectation_count": 0,
        "expectation_status_counts": {},
        "next_consideration_at": None,
        "cadence_reason_codes": [],
        "consecutive_technical_failures": 0,
        "retry_ordinal": 0,
        "last_failure_code": None,
        "warning": False,
        "warning_reasons": [],
    }
    assert scheduler["world_activity"] == {
        "life_event_count": 0,
        "occurrence_count": 0,
        "experience_count": 0,
        "starved": True,
    }
    assert scheduler["mechanisms"]["expression_retry"] == {
        "state": "idle",
        "pending_count": 0,
        "waiting_count": 0,
        "due_count": 0,
        "overdue_count": 0,
        "earliest_due_at": None,
        "max_attempt_ordinal": 0,
        "consecutive_technical_failures": 0,
        "pending_source_observation_refs": [],
        "pending_trigger_ids": [],
        "locators_truncated": False,
        "warning": False,
        "warning_reasons": [],
    }
    assert isinstance(scheduler["recall_semantic"]["enabled"], bool)
    assert scheduler["local_provider_capacity"] == {
        "enabled": False,
        "status": "disabled",
    }
    assert scheduler["external_world_perception"] == {
        "enabled": False,
        "state": "disabled",
        "reason": "mode_off",
    }
    assert scheduler["proactive_source_authority"] == {
        "status": "fact_effects_fail_closed",
        "warning": True,
        "warning_reasons": [
            "proactive_source_authority.independent_reviewer_unavailable",
        ],
        "independent_reviewer": False,
        "fact_effects_available": False,
        "subjective_expression_available": True,
        "author_model": "FakeCompanionModel",
        "reviewer_model": None,
        "candidate_inventory_model": None,
        "requested_candidate_inventory_model": None,
        "inventory_capability_evidence": None,
        "inventory_runtime": {
            "status": "unavailable",
            "successful_calls": 0,
            "failed_calls": 0,
            "last_checked_at": None,
            "last_failure_code": None,
        },
        "inventory_call_timeout_seconds": None,
        "visible_review_strategy": "full_source_review",
        "inventory_qualification_state": "unavailable",
        "active_source_review_protocol": "full_source_review.7",
        "source_review_qualification_transition": ("unavailable -> full_source_review.7"),
        "candidate_review_capabilities": {
            "ordinary": {
                "inventory_v5": False,
                "coverage_v5": False,
                "roles_independent": False,
            },
            "recovery": {
                "inventory_v5": False,
                "coverage_v5": False,
                "roles_independent": False,
            },
            "reselection": {
                "inventory_v5": False,
                "coverage_v5": False,
                "roles_independent": False,
            },
        },
        "inventory_transport": {
            "route_count": 0,
            "routes": [],
            "single_transport": False,
            "provider_count": 0,
            "single_provider": False,
            "capability_evidence": [],
            "attempt_timeout_seconds": None,
            "secondary_reserved_seconds": None,
        },
            "redundancy_state": "unavailable",
            "source_review_authority": None,
            "source_guard_relation": "unavailable",
            "selective_source_review": {
                "enabled": False,
                "runtime": None,
            },
        }
    assert scheduler["life_source_authority"]["status"] == "unavailable"
    assert scheduler["life_source_authority"]["last_transport_winner"] is None
    if scheduler["recall_semantic"]["enabled"]:
        assert scheduler["recall_semantic"]["embedding_version"].startswith("openai-compatible:")


def test_qq_public_health_forwards_expression_diagnostics_without_removing_old_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            _env_file=None,
            database_path=tmp_path / "qq-onebot-expression-health.sqlite",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        ),
        use_fake_model=True,
        scheduler_interval_seconds=3_600,
    )

    async def _healthy_without_world_work(**_kwargs: object) -> QQC2CDrainResult:
        return QQC2CDrainResult(action_statuses=(), background_statuses=())

    monkeypatch.setattr(app.state.qq_c2c_host, "scheduler_once", _healthy_without_world_work)
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    scheduler = response.json()["scheduler"]
    assert scheduler["expression_episode"] == {
        "mode": "stream",
        "active_reply_interface": "fast_stream",
        "reserved_reply_interface": {
            "name": "delayed_attention_complete",
            "status": "disabled",
            "reserved_for": "character_unavailable_or_delayed_attention",
        },
        "turns": 0,
        "candidate_valid": 0,
        "candidate_rejected": 0,
        "full_first": 0,
        "provisional_first": 0,
        "would_send": 0,
        "would_append": 0,
        "would_stop": 0,
        "slot_calls": 0,
        "grounding_rejected": 0,
        "placeholder_rejected": 0,
        "other_rejected": 0,
        "candidate_ms_p50": None,
        "candidate_ms_p95": None,
        "candidate_ms_max": None,
        "full_ms_p50": None,
        "full_ms_p95": None,
        "full_ms_max": None,
    }
    expected_retry = {
        "state": "idle",
        "pending_count": 0,
        "waiting_count": 0,
        "due_count": 0,
        "overdue_count": 0,
        "earliest_due_at": None,
        "max_attempt_ordinal": 0,
        "consecutive_technical_failures": 0,
        "pending_source_observation_refs": [],
        "pending_trigger_ids": [],
        "locators_truncated": False,
        "warning": False,
        "warning_reasons": [],
    }
    assert scheduler["expression_retry"] == expected_retry
    assert scheduler["mechanisms"]["expression_retry"] == expected_retry
    assert scheduler["status"] == "running"
    assert "initiative" in scheduler
    assert "world_activity" in scheduler
    assert "recall_semantic" in scheduler
    interior = scheduler["character_interior"]
    assert interior["semantic_author_count"] == 1
    assert interior["primary_author_model"] != "unknown"
    assert interior["primary_author_route"]["model_id"] == interior["primary_author_model"]
    assert interior["legacy_interface_invocations"] == 0
    assert interior["parallel_character_author_conflicts"] == 0
    assert interior["dual_write_conflicts"] == 0
    reliability = scheduler["reliability"]
    assert isinstance(reliability["dispatch_acks_24h"], int)
    assert isinstance(reliability["visible_replies_24h"], int)
    assert "failsafe_rate_24h" in reliability


@pytest.mark.asyncio
async def test_qq_c2c_scheduler_diagnostics_record_real_pass_progress() -> None:
    completed = asyncio.Event()
    scheduler_kwargs: dict[str, object] = {}

    class _Host:
        async def scheduler_once(self, **kwargs: object) -> None:
            scheduler_kwargs.update(kwargs)
            completed.set()

    diagnostics = QQC2CSchedulerDiagnostics(interval_seconds=60)
    task = asyncio.create_task(
        _scheduler_loop(_Host(), interval_seconds=60, diagnostics=diagnostics)  # type: ignore[arg-type]
    )
    diagnostics.task = task
    try:
        await asyncio.wait_for(completed.wait(), timeout=1)
        await asyncio.sleep(0)
        snapshot = diagnostics.snapshot(now=datetime.now(UTC))
        assert snapshot["status"] == "running"
        assert snapshot["passes_started"] == 1
        assert snapshot["passes_completed"] == 1
        assert snapshot["failures"] == 0
        assert snapshot["last_success_at"] is not None
        assert snapshot["last_duration_ms"] is not None
        assert scheduler_kwargs["max_action_units"] == 8
        assert scheduler_kwargs["max_background_units"] == 1
        assert diagnostics.snapshot(
            now=datetime.now(UTC),
            world={
                "recall_semantic": {
                    "enabled": True,
                    "embedding_status": "degraded",
                    "embedding_failure_code": "budget_exhausted",
                }
            },
        )["recall_semantic"] == {
            "enabled": True,
            "embedding_status": "degraded",
            "embedding_failure_code": "budget_exhausted",
        }
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_qq_c2c_scheduler_diagnostics_surface_returned_background_failure() -> None:
    completed = asyncio.Event()

    class _Host:
        async def scheduler_once(self, **_kwargs: object) -> SimpleNamespace:
            completed.set()
            return SimpleNamespace(background_statuses=("technical_failure:valueerror",))

    diagnostics = QQC2CSchedulerDiagnostics(interval_seconds=60)
    task = asyncio.create_task(
        _scheduler_loop(_Host(), interval_seconds=60, diagnostics=diagnostics)  # type: ignore[arg-type]
    )
    diagnostics.task = task
    try:
        await asyncio.wait_for(completed.wait(), timeout=1)
        await asyncio.sleep(0)
        snapshot = diagnostics.snapshot(now=datetime.now(UTC))
        assert snapshot["status"] == "failing"
        assert snapshot["passes_started"] == 1
        assert snapshot["passes_completed"] == 1
        assert snapshot["failures"] == 1
        assert snapshot["last_success_at"] is None
        assert snapshot["last_error"] == "technical_failure:valueerror"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_qq_scheduler_health_is_failing_when_latest_pass_failed() -> None:
    diagnostics = QQC2CSchedulerDiagnostics(interval_seconds=30)
    task = asyncio.create_task(asyncio.sleep(60))
    diagnostics.task = task
    diagnostics.passes_started = 3
    diagnostics.passes_completed = 3
    diagnostics.failures = 2
    diagnostics.last_success_at = NOW
    diagnostics.last_completed_at = NOW + timedelta(seconds=30)
    diagnostics.last_error = "ValueError"
    try:
        snapshot = diagnostics.snapshot(now=NOW + timedelta(seconds=31))
        assert snapshot["status"] == "failing"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_qq_health_reports_a_due_model_consideration_without_mutating_a_draw(
    tmp_path: Path,
) -> None:
    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-health-initiative.sqlite"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        delivery=_Delivery(),
    )
    try:
        host._host._application._social_initiative_policy = SocialInitiativePolicy(  # type: ignore[attr-defined]  # noqa: SLF001
            spontaneous_idle_seconds=60,
            spontaneous_expiry_seconds=3_600,
            consideration_band_override_seconds=(60, 60),
        )
        await host.inbound_text(
            message_id="qq-health-message",
            recipient_id="10001",
            text="我先去忙一会儿。",
            observed_at=NOW,
        )
        await host.tick(
            tick_id="tick:qq-health:idle",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61),
            reason="health_projection_test",
        )

        diagnostics = await host.world_health_diagnostics()
    finally:
        await host.aclose()

    assert diagnostics["spontaneous_candidate_due"] is True
    assert diagnostics["pending_proactive_opportunity_count"] == 1
    assert diagnostics["pending_proactive_process_count"] == 0
    assert diagnostics["initiative_state"] == "consideration_due"
    assert diagnostics["initiative_next_consideration_at"] is not None
    assert (
        diagnostics["recall_semantic"]["turn_summary"]["character_outcome"] == "action_authorized"
    )


@pytest.mark.asyncio
async def test_qq_health_reads_a_recorded_delay_draw_as_model_consideration_cadence(
    tmp_path: Path,
) -> None:
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "qq-health-delay-draw.sqlite",
            WORLD_V2_EXPRESSION_EPISODE_MODE="shadow",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        delivery=_Delivery(),
    )
    try:
        policy = SocialInitiativePolicy(
            spontaneous_idle_seconds=60,
            spontaneous_expiry_seconds=3_600,
            consideration_band_override_seconds=(60, 60),
        )
        host._host._application._social_initiative_policy = policy  # type: ignore[attr-defined]  # noqa: SLF001
        await host.inbound_text(
            message_id="qq-health-delay-message",
            recipient_id="10001",
            text="我先离开一下。",
            observed_at=NOW,
        )
        logical_time = NOW + timedelta(seconds=61)
        await host.tick(
            tick_id="tick:qq-health:delay",
            logical_time_from=NOW,
            logical_time_to=logical_time,
            observed_at=logical_time,
            reason="health_projection_draw_test",
        )
        # ``inbound_text`` deliberately leaves non-visible cognition on the
        # owned scheduler lane.  Join it before injecting a direct authority
        # event so this health test does not race a legitimate background CAS.
        await host._join_owned_scheduler_lane_tasks()  # noqa: SLF001
        ledger = host._host._application._ledger  # type: ignore[attr-defined]
        projection = ledger.project()
        source = next(
            item
            for item in projection.committed_world_event_refs
            if item.event_type == "ObservationRecorded"
            and item.world_revision == projection.message_observations[-1].world_revision
        )
        authority = RandomAuthority(ledger=ledger, source="test:health-random")
        for _ in range(8):
            current = ledger.project()
            profile = SocialInitiativeContextPolicy(policy=policy).compile(
                projection=current,
                logical_time=current.logical_time,
            )
            try:
                authority.draw(
                    attempt_id=social_initiative_attempt_id(
                        source_event_ref=source.event_id,
                        profile=profile,
                    ),
                    candidate_refs=("delay:60",),
                    candidate_weights={"delay:60": 1},
                    weight_policy_version=SocialInitiativeContextPolicy.version,
                    catalog_version="social-initiative-delay.1",
                    logical_time=current.logical_time,
                    seed_instant=source.logical_time,
                    actor="system:social-initiative",
                    trace_id="trace:health-delay",
                    correlation_id="correlation:health-delay",
                )
                break
            except ConcurrencyConflict:
                # Direct test injection is outside the production scheduler's
                # normal CAS retry loop. Re-pin both the head and logical time
                # after a legitimate background projection wins the cursor.
                await host._join_owned_scheduler_lane_tasks()  # noqa: SLF001
        else:
            raise AssertionError("health fixture could not obtain a stable ledger cursor")

        diagnostics = await host.world_health_diagnostics()
    finally:
        await host.aclose()

    assert diagnostics["spontaneous_candidate_due"] is True
    assert diagnostics["pending_proactive_opportunity_count"] == 1
    assert diagnostics["initiative_state"] == "consideration_due"
    assert diagnostics["initiative_cadence_reason_codes"]


def test_qq_c2c_identity_is_one_recipient_to_one_explicit_reply_target() -> None:
    resolver = QQC2CIdentityResolver(recipient_id="10001", canonical_user_id="geoff")

    assert resolver.resolve(platform="qq", platform_user_id="10001") == (
        "user:geoff",
        "conversation:qq:c2c:10001",
    )
    assert qq_c2c_target("10001") == "conversation:qq:c2c:10001"

    with pytest.raises(ValueError, match="not configured"):
        resolver.resolve(platform="qq", platform_user_id="20002")


def test_cli_defaults_a_compatible_private_text_deployment_to_world_v2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import companion_daemon.napcat_cli as napcat_cli

    monkeypatch.delenv("WORLD_V2_QQ_C2C_ENABLED", raising=False)
    monkeypatch.delenv("WORLD_V2_QQ_C2C_MODE", raising=False)

    assert (
        napcat_cli.resolve_cli_world_v2_c2c_selection(
            settings=Settings(
                QQ_ADAPTER="napcat",
                NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
                NAPCAT_ALLOW_GROUP_MESSAGES="false",
            ),
            requested=None,
        )
        is True
    )


def test_programmatic_napcat_factory_uses_the_same_compatible_v2_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import companion_daemon.napcat_cli as napcat_cli

    settings = Settings(
        QQ_ADAPTER="napcat",
        NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        NAPCAT_ALLOW_GROUP_MESSAGES="false",
    )
    sentinel = object()
    captured: dict[str, object] = {}

    def _build_v2(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.delenv("WORLD_V2_QQ_C2C_ENABLED", raising=False)
    monkeypatch.delenv("WORLD_V2_QQ_C2C_MODE", raising=False)
    monkeypatch.setattr(napcat_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        "companion_daemon.world_v2.qq_c2c_onebot_app.create_qq_c2c_onebot_app",
        _build_v2,
    )

    result = napcat_cli.create_app(adapter="napcat", use_fake_model=True)

    assert result is sentinel
    assert captured["settings"] is settings


def test_programmatic_napcat_archive_override_is_explicit_and_rejects_v2_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import companion_daemon.napcat_cli as napcat_cli

    monkeypatch.setattr(
        napcat_cli,
        "get_settings",
        lambda: Settings(
            QQ_ADAPTER="napcat",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        ),
    )

    with pytest.raises(ValueError, match="media deployment requires"):
        napcat_cli.create_app(
            adapter="napcat",
            use_fake_model=True,
            world_v2_c2c=False,
            media_preview=object(),  # type: ignore[arg-type]
            media_transport=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("settings", "mode", "expected"),
    (
        (
            Settings(QQ_ADAPTER="napcat", NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001,10002"),
            "auto",
            False,
        ),
        (
            Settings(
                QQ_ADAPTER="napcat",
                NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
                NAPCAT_ALLOW_GROUP_MESSAGES="true",
            ),
            "auto",
            False,
        ),
        (
            Settings(QQ_ADAPTER="napcat", NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001"),
            "archive",
            False,
        ),
    ),
)
def test_cli_migration_gate_archives_unsupported_or_explicitly_archived_qq_shapes(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    mode: str,
    expected: bool,
) -> None:
    import companion_daemon.napcat_cli as napcat_cli

    monkeypatch.delenv("WORLD_V2_QQ_C2C_ENABLED", raising=False)
    monkeypatch.setenv("WORLD_V2_QQ_C2C_MODE", mode)

    assert (
        napcat_cli.resolve_cli_world_v2_c2c_selection(settings=settings, requested=None) is expected
    )


def test_cli_forced_v2_rejects_an_unsupported_qq_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import companion_daemon.napcat_cli as napcat_cli

    monkeypatch.delenv("WORLD_V2_QQ_C2C_ENABLED", raising=False)
    monkeypatch.setenv("WORLD_V2_QQ_C2C_MODE", "v2")

    with pytest.raises(ValueError, match="requires exactly one"):
        napcat_cli.resolve_cli_world_v2_c2c_selection(
            settings=Settings(
                QQ_ADAPTER="napcat",
                NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001,10002",
            ),
            requested=None,
        )


def test_qq_c2c_v2_host_has_no_legacy_chat_or_coalescer_imports() -> None:
    path = Path(__file__).parents[2] / "src/companion_daemon/world_v2/qq_c2c_host.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    forbidden = (
        "companion_daemon.engine",
        "companion_daemon.world",
        "companion_daemon.runtime",
        "companion_daemon.companion_turn",
        "companion_daemon.qq_websocket",
    )
    assert not any(module.startswith(prefix) for module in imports for prefix in forbidden)


def test_qq_fake_composition_keeps_open_life_fact_effects_fail_closed(
    tmp_path: Path,
) -> None:
    host = build_qq_c2c_host(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY=None,
            OPENAI_API_KEY=None,
            database_path=tmp_path / "qq-open-life-wiring.sqlite",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        world_support_model=FakeCompanionModel(),
        delivery=_Delivery(),
    )
    try:
        ecology = host._host._application._life_ecology  # type: ignore[attr-defined]
        development = ecology._life_development_followup  # noqa: SLF001
        assert development is not None
        assert development._world_author.model.endswith(  # noqa: SLF001
            "/life-development/world_author"
        )
        assert not hasattr(development, "_character_model")
        assert development._character_interior is (  # noqa: SLF001
            host._host._application._character_interior  # type: ignore[attr-defined]  # noqa: SLF001
        )
        assert development._source_closure_reviewer is None  # noqa: SLF001
        assert development._source_closure_reviewer_is_independent is False  # noqa: SLF001
        assert (  # noqa: SLF001
            development._world_author_source_rewriter.authority_origin
            is development._world_author.authority_origin
        )
        assert not hasattr(ecology, "_life_author_followup")
        assert not hasattr(ecology, "_future_life_author_followup")
        assert ecology._npc_initiative_followup is not None  # noqa: SLF001
        assert (  # noqa: SLF001
            type(ecology._npc_initiative_followup).__name__ == "NpcEcology"
        )
        assert not hasattr(ecology, "_aspiration_followup")
        assert not hasattr(ecology, "_shared_private_followup")
        assert ecology._open_world_followup is None  # noqa: SLF001
    finally:
        host._host._application.close()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_onebot_lifespan_consumes_host_shutdown_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import companion_daemon.world_v2.qq_c2c_onebot_app as onebot_v2

    events: list[str] = []

    class _LeasedHost:
        closed = False
        quiescent = False

        async def scheduler_once(self, **_kwargs: object) -> None:
            await asyncio.Future()

        async def aclose(self) -> None:
            self.closed = True
            events.append("close")

        @property
        def shutdown_pending_task_count(self) -> int:
            return int(self.closed and not self.quiescent)

        async def wait_for_shutdown_quiescence(self) -> None:
            assert self.closed is True
            events.append("wait")
            self.quiescent = True

    async def _blocked_backfill(**_kwargs: object) -> None:
        await asyncio.Future()

    host = _LeasedHost()
    build_kwargs: dict[str, object] = {}

    def _build_host(**kwargs: object) -> _LeasedHost:
        build_kwargs.update(kwargs)
        return host

    monkeypatch.setattr(onebot_v2, "build_qq_c2c_host", _build_host)
    monkeypatch.setattr(
        onebot_v2,
        "backfill_missed_private_messages",
        _blocked_backfill,
    )
    app = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            _env_file=None,
            QQ_ADAPTER="napcat",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        ),
        use_fake_model=True,
    )

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)

    assert events == ["close", "wait"]
    assert host.shutdown_pending_task_count == 0
    assert build_kwargs["scheduler_interval_seconds"] == 15.0


def test_napcat_v2_branch_never_builds_legacy_engine_and_normalizes_supported_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import companion_daemon.napcat_cli as napcat_cli
    import companion_daemon.world_v2.qq_c2c_onebot_app as onebot_v2

    class _Host:
        inbound_calls: list[dict[str, object]] = []

        async def inbound_fragment(self, fragment):  # type: ignore[no-untyped-def]
            self.inbound_calls.append({"fragment": fragment})
            return type(
                "Result",
                (),
                {
                    "status": "action_authorized",
                    "action_id": "action:v2:1",
                    "canonical_user_id": "geoff",
                },
            )()

        async def scheduler_once(self, **_kwargs: object):
            return None

        async def aclose(self) -> None:
            return None

    host = _Host()
    settings = Settings(
        QQ_ADAPTER="napcat",
        NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        NAPCAT_ACCESS_TOKEN="test-token",
        NAPCAT_ACCEPT_UNAUTHENTICATED_LOCAL_EVENTS="false",
    )
    monkeypatch.setattr(napcat_cli, "get_settings", lambda: settings)
    monkeypatch.setattr(onebot_v2, "build_qq_c2c_host", lambda **_kwargs: host)

    app = napcat_cli.create_app(adapter="napcat", use_fake_model=True, world_v2_c2c=True)
    with TestClient(app) as client:
        text = client.post(
            "/onebot/event",
            headers={"Authorization": "Bearer test-token"},
            json={
                "post_type": "message",
                "message_type": "private",
                "user_id": "10001",
                "message_id": "onebot-text-1",
                "raw_message": "在吗？",
            },
        )
        group = client.post(
            "/onebot/event",
            headers={"Authorization": "Bearer test-token"},
            json={
                "post_type": "message",
                "message_type": "group",
                "group_id": "50001",
                "user_id": "10001",
                "message_id": "onebot-group-1",
                "raw_message": "@你 在吗？",
            },
        )
        sticker = client.post(
            "/onebot/event",
            headers={"Authorization": "Bearer test-token"},
            json={
                "post_type": "message",
                "message_type": "private",
                "user_id": "10001",
                "message_id": "onebot-sticker-1",
                "message": [{"type": "face", "data": {"id": "1"}}],
            },
        )
        oversized = client.post(
            "/onebot/event",
            headers={"Authorization": "Bearer test-token"},
            json={
                "post_type": "message",
                "message_type": "private",
                "user_id": "10001",
                "message_id": "onebot-oversized-1",
                "raw_message": "太" * 12_001,
            },
        )

    assert text.json() == {
        "status": "action_authorized",
        "world_action_id": "action:v2:1",
        "canonical_user_id": "geoff",
    }
    assert group.json() == {"status": "ignored_group_v2_unsupported"}
    assert sticker.json()["status"] == "action_authorized"
    assert oversized.status_code == 400
    assert oversized.json() == {"status": "rejected_invalid_qq_ingress"}
    assert len(host.inbound_calls) == 2
    text_fragment = host.inbound_calls[0]["fragment"]
    sticker_fragment = host.inbound_calls[1]["fragment"]
    assert text_fragment.source_event_id == "onebot-text-1"
    assert text_fragment.recipient_id == "10001"
    assert text_fragment.text == "在吗？"
    assert isinstance(text_fragment.observed_at, datetime)
    assert sticker_fragment.content_shape == "reaction"
    assert sticker_fragment.reaction_refs == ("qq-face:1",)


def test_qq_c2c_onebot_adapter_has_no_legacy_chat_or_coalescer_imports() -> None:
    path = Path(__file__).parents[2] / "src/companion_daemon/world_v2/qq_c2c_onebot_app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    forbidden = (
        "companion_daemon.engine",
        "companion_daemon.world",
        "companion_daemon.runtime",
        "companion_daemon.companion_turn",
        "companion_daemon.qq_websocket",
    )
    assert not any(module.startswith(prefix) for module in imports for prefix in forbidden)
