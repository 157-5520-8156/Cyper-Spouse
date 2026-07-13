import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

import companion_daemon.app as app_module
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel


def test_dashboard_serves_local_control_panel() -> None:
    client = TestClient(app_module.app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "知栀的小屋" in response.text
    assert "为什么是这个动作" in response.text
    assert 'id="roomCanvas"' in response.text
    assert "loadRoomBundle" in response.text
    assert "/assets/dashboard/rooms/zhizhi-home/runtime/room.bundle.json" in response.text
    assert '/dashboard-static/room/runtime.js' in response.text
    assert '/dashboard-static/room/editor.js' in response.text
    assert "DashboardRoomRuntime.load" in response.text
    assert "roomRuntime.setActor" in response.text
    assert "roomRuntime.activatePreview" in response.text
    assert "roomRuntime.preloadArtDraft" in response.text
    assert "get('freeze') === '1'" in response.text
    assert "get('view') === 'canvas'" in response.text
    assert "applyPreviewMode" in response.text
    assert "用户列表读取失败" in response.text
    assert "The visual home remains usable" in response.text
    assert "状态同步失败 · 可稍后重试" in response.text
    assert "/debug/users" in response.text
    assert "世界审计" in response.text
    assert "/world-runtime/enablement" in response.text


def test_dashboard_room_runtime_is_served_as_an_independent_module() -> None:
    client = TestClient(app_module.app)

    response = client.get("/dashboard-static/room/runtime.js")

    assert response.status_code == 200
    assert "class DashboardRoomRuntime" in response.text
    assert "static async load" in response.text
    assert "setActor(scene)" in response.text
    assert "activatePreview(params)" in response.text
    assert "start()" in response.text
    assert "interactionDepth.relativeTo" in response.text
    assert "小屋巡回行走 · 不写入 daemon" in response.text
    assert "动作巡检 · ${spot} · 不写入 daemon" in response.text

    editor = client.get("/dashboard-static/room/editor.js")
    assert editor.status_code == 200
    assert "class DashboardRoomEditor" in editor.text
    assert "manifestSnippet()" in editor.text
    assert "pointerMove(event)" in editor.text
    assert "data-toggle=\"walkable\"" in editor.text
    assert "data-toggle=\"footprints\"" in editor.text
    assert "data-toggle=\"approaches\"" in editor.text
    assert "data-field=\"inventory\"" in editor.text
    assert "data-field=\"layer\"" in editor.text
    assert "object.provenance.method" in editor.text
    assert "data-action=\"hidden\"" in editor.text
    assert "data-action=\"solo\"" in editor.text
    assert "data-field=\"audit-status\"" in editor.text


def test_dashboard_visual_baseline_manifest_matches_captured_files() -> None:
    root = Path(__file__).resolve().parents[1]
    baseline_dir = root / "docs/visual-baselines/dashboard-room"
    manifest = json.loads((baseline_dir / "baseline.json").read_text())
    bundle = json.loads(
        (root / "assets/dashboard/rooms/zhizhi-home/runtime/room.bundle.json").read_text()
    )

    capture_names = {item["name"] for item in manifest["captures"]}
    expected_audits = {
        f"{item['id']}-{side}"
        for item in bundle["objects"]
        for side in ("behind", "front")
    }
    assert capture_names == {"tour-start", *expected_audits}
    for capture in manifest["captures"]:
        path = baseline_dir / capture["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == capture["sha256"]
        assert Image.open(path).format == "JPEG"


def test_debug_state_and_memory_controls_reject_direct_mutation(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是沈知栀。")
    monkeypatch.setattr(app_module, "engine", engine)
    client = TestClient(app_module.app)

    users = client.get("/debug/users").json()
    assert users["users"] == ["geoff"]

    state_response = client.post(
        "/debug/geoff/state",
        json={"updates": {"mood": "curious", "trust": 42, "unknown": "ignored"}},
    )
    assert state_response.status_code == 409
    assert "forbids direct state mutation" in state_response.json()["detail"]

    add_response = client.post(
        "/debug/geoff/memories",
        json={
            "kind": "favorite_thing",
            "content": "用户喜欢桂花乌龙",
            "confidence": 0.8,
        },
    )
    assert add_response.status_code == 409
    assert "forbids direct memory mutation" in add_response.json()["detail"]

    delete_response = client.delete(
        "/debug/geoff/memories",
        params={"kind": "favorite_thing", "content": "用户喜欢桂花乌龙"},
    )
    assert delete_response.status_code == 409
    assert "forbids direct memory mutation" in delete_response.json()["detail"]
