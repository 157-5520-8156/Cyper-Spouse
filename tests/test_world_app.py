from pathlib import Path
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
import pytest

import companion_daemon.app as app_module
from companion_daemon.db import CompanionStore
from companion_daemon.config import Settings
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world import ConcurrencyConflict, WorldKernel
from companion_daemon.models import IncomingMessage


def test_world_enablement_and_trusted_delivery_settlement(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=kernel, world_id=started.world_id)
    monkeypatch.setattr(app_module, "engine", engine)
    client = TestClient(app_module.archive_app)

    audit = client.get("/world-runtime/enablement")
    assert audit.status_code == 200
    assert audit.json()["enabled"] is True

    delivery_id, _, _ = kernel.queue_outgoing_action(
        canonical_user_id="geoff", platform="qq", text="测试。", kind="reply",
        expires_at=__import__("datetime").datetime.fromisoformat("2026-07-12T09:00:00+08:00"),
        trace={"world_id": started.world_id, "appraisal": "test", "expression_policy": "test", "allowed_facts": [], "short_lived_constraint": None, "observable_reason": "test"},
    )
    kernel.begin_outgoing_action(delivery_id, expected_revision=kernel.revision(started.world_id))
    kernel.mark_outgoing_unknown(delivery_id, reason="restart", expected_revision=kernel.revision(started.world_id))
    # A platform adapter, not a browser endpoint, owns receipt-based
    # settlement.  The receipt is preserved with the world action.
    kernel.settle_outgoing_action(delivery_id, delivered=True, external_receipt="qq:42")
    assert kernel.snapshot(started.world_id)["actions"][f"outgoing:{delivery_id}"]["status"] == "delivered"
    assert client.post(f"/world/{started.world_id}/deliveries/reconcile", json={}).status_code == 404
    blocked = client.post(
        f"/world/{started.world_id}/commands",
        json={
            "expected_revision": kernel.revision(started.world_id),
            "command": {
                "type": "record_external_result", "action_id": f"outgoing:{delivery_id}",
                "result": {"kind": "delivery", "status": "delivered"},
            },
        },
    )
    assert blocked.status_code == 403
    assert "settle external results" in blocked.json()["detail"]


def test_operator_can_reconcile_unknown_delivery_once_with_audited_external_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    store = CompanionStore(tmp_path / "world-reconciliation.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    runtime = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是知栀。",
        world_kernel=kernel,
        world_id=started.world_id,
    )
    monkeypatch.setattr(app_module, "engine", runtime)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="operator-secret"),
    )
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="发送后进程崩溃的消息。",
        kind="reply",
        expires_at=datetime.fromisoformat("2026-07-12T09:00:00+08:00"),
        trace={
            "world_id": started.world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "short_lived_constraint": None,
            "observable_reason": "test",
        },
    )
    kernel.begin_outgoing_action(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    kernel.mark_outgoing_unknown(
        delivery_id,
        reason="process crashed after dispatch",
        expected_revision=kernel.revision(started.world_id),
    )
    client = TestClient(app_module.archive_app)
    request = {
        "expected_revision": kernel.revision(started.world_id),
        "status": "delivered",
        "evidence_kind": "operator_verification",
        "external_receipt": "qq-message:late-42",
        "reviewer_id": "ops-geoff",
        "review_note": "运维人员已在 QQ 端核对到该消息。",
        "segment_id": kernel.snapshot(started.world_id)["actions"][action_id][
            "segment_state"
        ]["segments"][0]["segment_id"],
    }

    response = client.post(
        f"/world/{started.world_id}/deliveries/{delivery_id}/reconcile",
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json=request,
    )

    assert response.status_code == 200
    assert response.json()["reconciled"] is True
    action = kernel.snapshot(started.world_id)["actions"][action_id]
    assert action["status"] == "delivered"
    assert action["result"]["external_receipt"] == "qq-message:late-42"
    assert action["result"]["reconciliation_evidence"] == {
        "kind": "operator_verification",
        "source": "operator_reconciliation",
        "reference": "qq-message:late-42",
        "reviewer_id": "ops-geoff",
        "review_note": "运维人员已在 QQ 端核对到该消息。",
    }
    event_count = len(kernel.export_ledger(started.world_id))

    duplicate = client.post(
        f"/world/{started.world_id}/deliveries/{delivery_id}/reconcile",
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json=request,
    )

    assert duplicate.status_code == 200
    assert duplicate.json()["reconciled"] is False
    assert len(kernel.export_ledger(started.world_id)) == event_count

    conflicting_duplicate = client.post(
        f"/world/{started.world_id}/deliveries/{delivery_id}/reconcile",
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json={**request, "external_receipt": "qq-message:conflicting-late-42"},
    )
    assert conflicting_duplicate.status_code == 409

    with pytest.raises(ConcurrencyConflict, match="already reconciled as delivered"):
        kernel.settle_outgoing_action(
            delivery_id,
            delivered=False,
            reason="conflicting late callback",
            external_receipt="qq-message:conflict-42",
            reconciliation_evidence={
                "kind": "platform_receipt",
                "reference": "qq-message:conflict-42",
                "reviewer_id": "ops-geoff",
                "review_note": "迟到的冲突回调。",
            },
        )

    partial_delivery, _, partial_action = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="第一段。第二段。第三段。",
        text_parts=["第一段。", "第二段。", "第三段。"],
        kind="reply",
        expires_at=datetime.fromisoformat("2026-07-12T10:00:00+08:00"),
        trace={
            "world_id": started.world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "observable_reason": "test",
        },
    )
    first = kernel.claim_outgoing_segment(
        partial_delivery, expected_revision=kernel.revision(started.world_id)
    )
    assert first
    kernel.settle_outgoing_segment(
        partial_delivery,
        first.segment_id,
        delivered=True,
        expected_revision=kernel.revision(started.world_id),
    )
    uncertain = kernel.claim_outgoing_segment(
        partial_delivery, expected_revision=kernel.revision(started.world_id)
    )
    assert uncertain
    kernel.mark_outgoing_segment_unknown(
        partial_delivery,
        uncertain.segment_id,
        reason="connection lost after segment dispatch",
        expected_revision=kernel.revision(started.world_id),
    )
    segment_request = {
        **request,
        "expected_revision": kernel.revision(started.world_id),
        "external_receipt": "qq-segment:late-43",
        "segment_id": uncertain.segment_id,
        "cancel_remaining": True,
        "cancel_remaining_reason": "用户已在后续消息中转入新话题，不能再补发第三段。",
    }

    segment_response = client.post(
        f"/world/{started.world_id}/deliveries/{partial_delivery}/reconcile",
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json=segment_request,
    )

    assert segment_response.status_code == 200
    assert segment_response.json()["action_status"] == "cancelled"
    partial = kernel.snapshot(started.world_id)["actions"][partial_action]
    assert [item["status"] for item in partial["segment_state"]["segments"]] == [
        "delivered",
        "delivered",
        "cancelled",
    ]
    assert [
        row["text"] for row in store.recent_messages("geoff") if row["direction"] == "out"
    ][-2:] == ["第一段。", "第二段。"]
    reconciled_event = next(
        event
        for event in reversed(kernel.events(started.world_id))
        if event.event_type == "ActionSegmentSettled"
    )
    assert reconciled_event.payload["result"]["reconciliation_evidence"][
        "reviewer_id"
    ] == "ops-geoff"

    failed_delivery, _, failed_action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="未确认段。未发送段。",
        text_parts=["未确认段。", "未发送段。"],
        kind="reply",
        expires_at=datetime.fromisoformat("2026-07-12T10:00:00+08:00"),
        trace={
            "world_id": started.world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "observable_reason": "test",
        },
    )
    failed_segment = kernel.claim_outgoing_segment(
        failed_delivery, expected_revision=kernel.revision(started.world_id)
    )
    assert failed_segment
    kernel.mark_outgoing_segment_unknown(
        failed_delivery,
        failed_segment.segment_id,
        reason="connection lost",
        expected_revision=kernel.revision(started.world_id),
    )
    failed_request = {
        **request,
        "expected_revision": kernel.revision(started.world_id),
        "status": "failed",
        "failure_reason": "platform confirmed rejection",
        "external_receipt": "qq-segment-rejected:44",
        "segment_id": failed_segment.segment_id,
    }

    failed_response = client.post(
        f"/world/{started.world_id}/deliveries/{failed_delivery}/reconcile",
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json=failed_request,
    )

    assert failed_response.status_code == 200
    failed_action = kernel.snapshot(started.world_id)["actions"][failed_action_id]
    assert failed_action["status"] == "failed"
    assert [item["status"] for item in failed_action["segment_state"]["segments"]] == [
        "cancelled",
        "cancelled",
    ]
    assert not any(
        row["text"] in {"未确认段。", "未发送段。"}
        for row in store.recent_messages("geoff")
    )


def test_reconciliation_without_authority_or_evidence_leaves_delivery_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    store = CompanionStore(tmp_path / "world-reconciliation-guard.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    monkeypatch.setattr(
        app_module,
        "engine",
        CompanionEngine(
            store,
            FakeCompanionModel(),
            "你是知栀。",
            world_kernel=kernel,
            world_id=started.world_id,
        ),
    )
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="operator-secret"),
    )
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="这条没有回执。",
        kind="reply",
        expires_at=datetime.fromisoformat("2026-07-12T09:00:00+08:00"),
        trace={
            "world_id": started.world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "short_lived_constraint": None,
            "observable_reason": "test",
        },
    )
    kernel.begin_outgoing_action(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    kernel.mark_outgoing_unknown(
        delivery_id,
        reason="receipt unavailable",
        expected_revision=kernel.revision(started.world_id),
    )
    client = TestClient(app_module.archive_app)
    endpoint = f"/world/{started.world_id}/deliveries/{delivery_id}/reconcile"
    body = {
        "expected_revision": kernel.revision(started.world_id),
        "status": "failed",
        "evidence_kind": "platform_receipt",
        "external_receipt": "qq-rejection:42",
        "reviewer_id": "ops-geoff",
        "review_note": "QQ 返回了明确的拒绝回执。",
        "failure_reason": "platform rejected the message",
    }

    unauthorized = client.post(
        endpoint,
        headers={"X-Delivery-Reconciliation-Token": "wrong-secret"},
        json=body,
    )
    missing_evidence = client.post(
        endpoint,
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json={key: value for key, value in body.items() if key != "external_receipt"},
    )
    stale_revision = client.post(
        endpoint,
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json={**body, "expected_revision": body["expected_revision"] - 1},
    )
    unaudited_cancellation = client.post(
        endpoint,
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json={**body, "cancel_remaining": True},
    )

    assert unauthorized.status_code == 403
    assert missing_evidence.status_code == 422
    assert stale_revision.status_code == 409
    assert unaudited_cancellation.status_code == 400
    assert kernel.snapshot(started.world_id)["actions"][action_id]["status"] == "unknown"

    reconciled = client.post(
        endpoint,
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json=body,
    )

    assert reconciled.status_code == 200
    assert kernel.snapshot(started.world_id)["actions"][action_id]["status"] == "failed"


def test_operator_reconciliation_never_dispatches_a_following_planned_segment(
    tmp_path: Path, monkeypatch
) -> None:
    store = CompanionStore(tmp_path / "world-operator-no-followup.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    runtime = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是知栀。",
        world_kernel=kernel,
        world_id=started.world_id,
    )
    monkeypatch.setattr(app_module, "engine", runtime)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(DELIVERY_RECONCILIATION_TOKEN="operator-secret"),
    )
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="第一段。第二段。第三段。",
        text_parts=["第一段。", "第二段。", "第三段。"],
        kind="reply",
        expires_at=datetime.fromisoformat("2026-07-12T09:00:00+08:00"),
        trace={
            "world_id": started.world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "short_lived_constraint": None,
            "observable_reason": "test",
        },
    )
    first = kernel.claim_outgoing_segment(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    assert first
    kernel.settle_outgoing_segment(
        delivery_id,
        first.segment_id,
        delivered=True,
        external_receipt="qq:first",
        expected_revision=kernel.revision(started.world_id),
    )
    uncertain = kernel.claim_outgoing_segment(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    assert uncertain
    kernel.mark_outgoing_segment_unknown(
        delivery_id,
        uncertain.segment_id,
        reason="process stopped after QQ accepted the segment",
        expected_revision=kernel.revision(started.world_id),
    )

    response = TestClient(app_module.archive_app).post(
        f"/world/{started.world_id}/deliveries/{delivery_id}/reconcile",
        headers={"X-Delivery-Reconciliation-Token": "operator-secret"},
        json={
            "expected_revision": kernel.revision(started.world_id),
            "status": "delivered",
            "evidence_kind": "operator_verification",
            "external_receipt": "qq:second-late",
            "reviewer_id": "ops-geoff",
            "review_note": "已人工核对第二段在 QQ 中可见。",
            "segment_id": uncertain.segment_id,
            "cancel_remaining": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["action_status"] == "scheduled"
    action = kernel.snapshot(started.world_id)["actions"][action_id]
    assert [part["status"] for part in action["segment_state"]["segments"]] == [
        "delivered",
        "delivered",
        "planned",
    ]
    assert [
        row["text"] for row in store.recent_messages("geoff") if row["direction"] == "out"
    ][-2:] == ["第一段。", "第二段。"]


def test_world_console_reads_active_world_and_submits_clock_commands(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "world-console.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=kernel, world_id=started.world_id)
    monkeypatch.setattr(app_module, "engine", engine)
    client = TestClient(app_module.archive_app)

    page = client.get("/world-console")
    assert page.status_code == 200
    assert "世界控制台" in page.text
    assert "world-runtime/overview" in page.text
    assert "set_clock_mode" in page.text

    overview = client.get("/world-runtime/overview")
    assert overview.status_code == 200
    body = overview.json()
    assert body["enabled"] is True
    assert body["world_id"] == started.world_id
    assert body["revision"] == started.revision
    assert body["clock"]["mode"] == "paused"
    assert isinstance(body["goals"], list)
    assert isinstance(body["timeline"], list)

    clock = client.post(
        f"/world/{started.world_id}/commands",
        json={"expected_revision": body["revision"], "command": {"type": "set_clock_mode", "mode": "accelerated", "rate": 4}},
    )
    assert clock.status_code == 200
    assert client.get("/world-runtime/overview").json()["clock"] == {"mode": "accelerated", "rate": 4, "logical_at": body["clock"]["logical_at"]}


def test_world_console_reports_disabled_runtime(monkeypatch, tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "no-world.sqlite")
    seed_user(store)
    monkeypatch.setattr(app_module, "engine", CompanionEngine(store, FakeCompanionModel(), "你是知栀。"))

    response = TestClient(app_module.archive_app).get("/world-runtime/overview")

    assert response.status_code == 200
    assert response.json() == {"enabled": False}


def test_http_message_endpoint_uses_world_v2_capture_without_archive_engine(
    monkeypatch,
) -> None:
    class Capture:
        async def respond(self, **_kwargs):
            return type(
                "Result",
                (),
                {
                    "status": "action_authorized",
                    "action_id": "action:v2:http-defer",
                    "text": "晚点见。",
                    "canonical_user_id": "geoff",
                },
            )()

    class ArchiveEngine:
        def __getattr__(self, name):
            raise AssertionError(f"World v2 HTTP reached archive Engine: {name}")

    monkeypatch.setattr(app_module, "http_v2_capture", Capture())
    monkeypatch.setattr(app_module, "engine", ArchiveEngine())

    response = TestClient(app_module.app).post(
        "/messages",
        json=IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            text="晚点聊",
            message_id="http-defer",
        ).model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["world_action_id"] == "action:v2:http-defer"


def test_http_message_endpoint_returns_world_v2_capture_text(monkeypatch) -> None:
    class Capture:
        async def respond(self, **_kwargs):
            return type(
                "Result",
                (),
                {
                    "status": "action_authorized",
                    "action_id": "action:v2:http-text",
                    "text": "我在。",
                    "canonical_user_id": "geoff",
                },
            )()

    monkeypatch.setattr(app_module, "http_v2_capture", Capture())

    response = TestClient(app_module.app).post(
        "/messages",
        json=IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            text="我今天有点撑不住",
            message_id="http-sticker",
        ).model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["text"] == "我在。"
    assert response.json()["world_action_id"] == "action:v2:http-text"


def test_world_console_keeps_unresolved_activity_visible_after_long_history(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "long-world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    epoch = datetime.fromisoformat(str(kernel.snapshot(started.world_id)["clock"]["logical_at"]))
    settled = kernel.advance(
        started.world_id,
        epoch + timedelta(hours=3, minutes=30),
        expected_revision=started.revision,
    )
    start = epoch + timedelta(hours=3, minutes=30)
    revision = settled.revision
    for index in range(13):
        begins = start + timedelta(minutes=index * 40)
        planned = kernel.submit(
            {
                "type": "plan_activity", "world_id": started.world_id,
                "activity_id": f"history-{index}", "entity_id": "zhizhi", "title": "历史活动",
                "starts_at": begins.isoformat(), "ends_at": (begins + timedelta(minutes=20)).isoformat(),
            },
            expected_revision=revision,
        )
        revision = planned.revision
    advanced_to = start + timedelta(hours=10)
    advanced = kernel.advance(started.world_id, advanced_to, expected_revision=revision)
    current = kernel.submit(
        {
            "type": "plan_activity", "world_id": started.world_id,
            "activity_id": "current", "entity_id": "zhizhi", "title": "当前活动",
            "starts_at": advanced_to.isoformat(), "ends_at": (advanced_to + timedelta(hours=1)).isoformat(),
        },
        expected_revision=advanced.revision,
    )
    kernel.advance(started.world_id, advanced_to, expected_revision=current.revision)

    agenda = kernel.dashboard_overview(started.world_id)["agenda"]

    current_item = next(item for item in agenda if item["activity_id"] == "current")
    assert current_item["status"] in {"active", "planned"}


def test_qq_webhook_observes_through_turn_seam_without_staging_reply(
    tmp_path: Path, monkeypatch
) -> None:
    store = CompanionStore(tmp_path / "qq-webhook-observe.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    runtime = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是知栀。",
        world_kernel=kernel,
        world_id=started.world_id,
    )
    monkeypatch.setattr(app_module, "engine", runtime)
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(QQ_VERIFY_SIGNATURES=False),
    )

    response = TestClient(app_module.archive_app).post(
        "/qq/webhook",
        json={
            "op": 0,
            "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": "qq-webhook-observe-1",
                "content": "我先嗯一声。",
                "author": {"user_openid": "geoff"},
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {"op": 12}
    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["turns"]["qq:geoff:qq-webhook-observe-1"]["status"] == "deferred"
    assert not any(
        action.get("trace", {}).get("input_message_id")
        == "qq:geoff:qq-webhook-observe-1"
        for action in snapshot["actions"].values()
        if isinstance(action, dict)
    )
