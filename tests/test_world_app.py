from pathlib import Path
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

import companion_daemon.app as app_module
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world import WorldKernel
from companion_daemon.models import IncomingMessage


def test_world_enablement_and_trusted_delivery_settlement(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=kernel, world_id=started.world_id)
    monkeypatch.setattr(app_module, "engine", engine)
    client = TestClient(app_module.app)

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


def test_world_console_reads_active_world_and_submits_clock_commands(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "world-console.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=kernel, world_id=started.world_id)
    monkeypatch.setattr(app_module, "engine", engine)
    client = TestClient(app_module.app)

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

    response = TestClient(app_module.app).get("/world-runtime/overview")

    assert response.status_code == 200
    assert response.json() == {"enabled": False}


def test_http_message_endpoint_represents_world_defer_without_server_error(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "deferred-world.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    logical_now = datetime.fromisoformat(str(kernel.snapshot(started.world_id)["clock"]["logical_at"]))
    planned = kernel.submit(
        {
            "type": "plan_activity",
            "world_id": started.world_id,
            "activity_id": "busy-http",
            "entity_id": "zhizhi",
            "title": "整理资料",
            "starts_at": logical_now.isoformat(),
            "ends_at": (logical_now + timedelta(hours=2)).isoformat(),
        },
        expected_revision=started.revision,
    )
    kernel.advance(started.world_id, logical_now, expected_revision=planned.revision)
    kernel.submit(
        {"type": "change_need", "world_id": started.world_id, "need": "energy", "delta": -50},
        expected_revision=kernel.revision(started.world_id),
    )
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=kernel, world_id=started.world_id)
    monkeypatch.setattr(app_module, "engine", engine)

    response = TestClient(app_module.app, raise_server_exceptions=False).post(
        "/messages",
        json=IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            text="晚点聊",
            message_id="http-defer",
        ).model_dump(mode="json"),
    )

    assert response.status_code == 202
    assert response.json() == {"status": "no_immediate_reply", "message_id": "http-defer"}


def test_world_console_keeps_unresolved_activity_visible_after_long_history(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "long-world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    start = datetime.fromisoformat(str(kernel.snapshot(started.world_id)["clock"]["logical_at"]))
    revision = started.revision
    for index in range(13):
        begins = start + timedelta(hours=index * 2)
        planned = kernel.submit(
            {
                "type": "plan_activity", "world_id": started.world_id,
                "activity_id": f"history-{index}", "entity_id": "zhizhi", "title": "历史活动",
                "starts_at": begins.isoformat(), "ends_at": (begins + timedelta(hours=1)).isoformat(),
            },
            expected_revision=revision,
        )
        revision = planned.revision
    advanced_to = start + timedelta(hours=27)
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
