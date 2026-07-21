from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
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


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


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
        recipient_id="10001", canonical_user_id="geoff",
        ingress_store=SQLiteQQIngressStore(tmp_path / "scheduler-ingress.sqlite"),
        ingress_now=lambda: clock["now"], ingress_sleep=_advance_ingress_window,
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
                message_id="concurrent-message", recipient_id="10001", text="你在吗？",
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


def _visible(delivery: "_Delivery") -> list[tuple[str, str]]:
    """Delivered expression content, excluding best-effort presence pulses.

    The host now emits one non-authoritative ``typing:composing`` pulse when a
    text turn starts; it is provider presence metadata, not a delivered beat.
    """

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


class _OneExpressionModel:
    model = "fixture:one-expression"

    def __init__(self, beat: dict[str, str]) -> None:
        self.beat = beat
        self.calls = 0

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del messages, temperature
        self.calls += 1
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [self.beat],
                "stance": "acknowledge_briefly",
                "brief_rationale": "The model selected one available expression form.",
            }
        )


class _SilentExpressionModel:
    model = "fixture:silent-expression"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del messages, temperature
        self.calls += 1
        return json.dumps({
            "timing_choice": "silent",
            "beats": [],
            "stance": "defer",
            "brief_rationale": "A reaction is available, but I choose not to use it.",
        })


class _WrappedExpressionModel:
    model = "fixture:wrapped-expression"

    async def complete(self, _messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps({
            "expression_draft": {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我在，刚看到。"}],
                "stance": "acknowledge_briefly",
                "brief_rationale": "Reply to the current message.",
            }
        }, ensure_ascii=False)


class _IdentityAwareModel:
    model = "fixture:identity-aware"

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        system = messages[0]["content"]
        grounded = "沈知栀" in system and "不是助手" in system and "geoff" in system.lower()
        text = "我是沈知栀，你是 Geoff。" if grounded else "我是你的 AI 助手小 Geoff。"
        return json.dumps({
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "answer_without_world_claims",
            "brief_rationale": "Answer from the supplied stable identity.",
        }, ensure_ascii=False)


class _SelectingLifeEcologyModel:
    """Select available life authority and drive it to completion."""

    model = "test-qq-life-ecology"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        capsule = json.loads(messages[-1]["content"])
        if "candidate" in capsule:
            return json.dumps({
                "decision": "select",
                "candidate_token": capsule["candidate"]["token"],
            })
        openings = capsule.get("openings", [])
        if not openings:
            return '{"decision":"no_op"}'
        # planned => start/abandon; active => pause/complete/abandon.
        selected = openings[1] if len(openings) >= 3 else openings[0]
        return json.dumps({
            "decision": "select",
            "opening_token": selected["opening_token"],
        })


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
        return json.dumps({
            "timing_choice": "later",
            "beats": [{"modality": "text", "text": "晚点我来找你。"}],
            "delay_seconds": 60, "expires_after_seconds": 600,
            "stance": "defer", "brief_rationale": "稍后接续", "confidence": 7200,
        }, ensure_ascii=False)


@pytest.mark.asyncio
async def test_qq_production_composition_ticks_life_from_plan_through_experience(
    tmp_path: Path,
) -> None:
    """The actual QQ host installs and advances the complete life vertical."""

    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-life-vertical.sqlite"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_SelectingLifeEcologyModel(),
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
        settings=Settings(database_path=tmp_path / "qq-shared-later.sqlite", PRIMARY_USER_ID="geoff"),
        recipient_id="10001", bootstrap_at=NOW, model=model,
        advisory_model=FakeCompanionModel(), delivery=delivery,
    )
    try:
        result = await host.inbound_text(
            message_id="qq-later-1", recipient_id="10001", text="你先忙吧", observed_at=NOW,
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert result.status == "deferred" and result.action_id is None
    assert _visible(delivery) == []
    assert model.calls == 1
    assert len(projection.actions) == len(projection.commitments) == 1
    assert projection.actions[0].kind == "followup"


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
    assert observation["coalescing_metadata"]["source_event_ids"] == [
        "onebot-message-1"
    ]
    assert observation["coalescing_metadata"]["policy_version"] == (
        "world-v2-qq-ingress-matrix.1"
    )

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
    assert drained.action_statuses
    assert any("unknown" in status for status in drained.action_statuses), drained


@pytest.mark.asyncio
async def test_qq_c2c_host_turns_a_wrapped_flash_draft_into_a_delivered_action(
    tmp_path: Path,
) -> None:
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(database_path=tmp_path / "qq-wrapped-flash.sqlite", PRIMARY_USER_ID="geoff"),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_WrappedExpressionModel(),
        advisory_model=FakeCompanionModel(),
        delivery=delivery,
    )
    try:
        result = await host.inbound_text(
            message_id="wrapped-flash-1",
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
        advisory_model=FakeCompanionModel(),
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
    assert observation["attachment_refs"] == [
        "qq-attachment:image:sha256:" + "a" * 64
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("beat", "expected"),
    (
        ({"modality": "reaction", "reaction_id": "like"}, "reaction:onebot-expression-1:like"),
        ({"modality": "sticker", "sticker_id": "qq-face:14"}, "sticker:qq-face:14"),
        ({"modality": "typing"}, "typing:composing"),
    ),
)
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
        advisory_model=FakeCompanionModel(),
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
async def test_napcat_main_model_can_refuse_every_available_expression_without_action(
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
        advisory_model=FakeCompanionModel(),
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
    assert _visible(delivery) == [] and projection.actions == ()
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
        selection_model=_SelectionModel(),
        planner=_NoCallMediaPlanner(),
        acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(
                grant_id="grant:qq-onebot-preview", grant_revision=1,
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


def test_qq_health_reports_a_running_scheduler_even_when_the_world_is_starved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=Settings(
            database_path=tmp_path / "qq-onebot-starved.sqlite",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
        ),
        use_fake_model=True,
        scheduler_interval_seconds=3_600,
    )

    async def _healthy_but_no_world_work(**_kwargs: object) -> QQC2CDrainResult:
        return QQC2CDrainResult(action_statuses=(), background_statuses=())

    monkeypatch.setattr(
        app.state.qq_c2c_host, "scheduler_once", _healthy_but_no_world_work
    )
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    scheduler = response.json()["scheduler"]
    assert scheduler["status"] == "running"
    assert scheduler["initiative"] == {
        "last_status": None,
        "last_reason": None,
        "pending_opportunity_count": 0,
        "pending_process_count": 0,
        "pending_action_count": 0,
        "spontaneous_candidate_due": False,
    }
    assert scheduler["world_activity"] == {
        "life_event_count": 0,
        "occurrence_count": 0,
        "experience_count": 0,
        "starved": True,
    }


@pytest.mark.asyncio
async def test_qq_c2c_scheduler_diagnostics_record_real_pass_progress() -> None:
    completed = asyncio.Event()

    class _Host:
        async def scheduler_once(self, **_kwargs: object) -> None:
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
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_qq_health_does_not_call_a_due_spontaneous_candidate_an_opportunity_without_a_draw(
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
        await host.inbound_text(
            message_id="qq-health-message",
            recipient_id="10001",
            text="我先去忙一会儿。",
            observed_at=NOW,
        )
        await host.tick(
            tick_id="tick:qq-health:idle",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=31),
            observed_at=NOW + timedelta(minutes=31),
            reason="health_projection_test",
        )

        diagnostics = await host.world_health_diagnostics()
    finally:
        await host.aclose()

    assert diagnostics["spontaneous_candidate_due"] is True
    assert diagnostics["pending_proactive_opportunity_count"] == 0
    assert diagnostics["pending_proactive_process_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selected", "expected_opportunities"),
    (("act", 1), ("hold", 0)),
)
async def test_qq_health_counts_only_a_recorded_act_draw_as_spontaneous_opportunity(
    tmp_path: Path, selected: str, expected_opportunities: int,
) -> None:
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / f"qq-health-{selected}-draw.sqlite"
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=FakeCompanionModel(),
        delivery=_Delivery(),
    )
    try:
        await host.inbound_text(
            message_id=f"qq-health-{selected}-message",
            recipient_id="10001",
            text="我先离开一下。",
            observed_at=NOW,
        )
        logical_time = NOW + timedelta(minutes=31)
        await host.tick(
            tick_id=f"tick:qq-health:{selected}",
            logical_time_from=NOW,
            logical_time_to=logical_time,
            observed_at=logical_time,
            reason="health_projection_draw_test",
        )
        ledger = host._host._application._ledger  # type: ignore[attr-defined]
        projection = ledger.project()
        source = next(
            item
            for item in projection.committed_world_event_refs
            if item.event_type == "ObservationRecorded"
            and item.world_revision == projection.message_observations[-1].world_revision
        )
        profile = SocialInitiativeContextPolicy(
            policy=SocialInitiativePolicy()
        ).compile(projection=projection, logical_time=logical_time)
        RandomAuthority(ledger=ledger, source="test:health-random").draw(
            attempt_id=social_initiative_attempt_id(
                source_event_ref=source.event_id,
                profile=profile,
            ),
            candidate_refs=("act", "hold"),
            candidate_weights={
                "act": int(selected == "act"),
                "hold": int(selected == "hold"),
            },
            weight_policy_version=SocialInitiativeContextPolicy.version,
            catalog_version="social-initiative-act-hold.1",
            logical_time=logical_time,
            seed_instant=source.logical_time,
            actor="system:social-initiative",
            trace_id=f"trace:health-{selected}",
            correlation_id=f"correlation:health-{selected}",
        )

        diagnostics = await host.world_health_diagnostics()
    finally:
        await host.aclose()

    assert diagnostics["spontaneous_candidate_due"] is True
    assert (
        diagnostics["pending_proactive_opportunity_count"]
        == expected_opportunities
    )


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

    assert napcat_cli.resolve_cli_world_v2_c2c_selection(
        settings=Settings(
            QQ_ADAPTER="napcat",
            NAPCAT_ALLOWED_PRIVATE_USER_IDS="10001",
            NAPCAT_ALLOW_GROUP_MESSAGES="false",
        ),
        requested=None,
    ) is True


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
        napcat_cli.resolve_cli_world_v2_c2c_selection(settings=settings, requested=None)
        is expected
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
