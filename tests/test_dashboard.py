from pathlib import Path

from fastapi.testclient import TestClient

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
    assert "pathfind" in response.text
    assert "applyScene" in response.text
    assert "free-bedroom" in response.text
    assert "sceneDefinitions" in response.text
    assert "activateScene" in response.text
    assert "scene_id" in response.text
    assert "zhizhi-room-isometric-v2.png" in response.text
    assert "zhizhi-iso-walk-v4.png" in response.text
    assert "characterAction" in response.text
    assert "spriteCell" in response.text
    assert "downRight" in response.text
    assert "upLeft" in response.text
    assert "WALK_FRAMES = 4" in response.text
    assert "walkable" in response.text
    assert "footprint" in response.text
    assert "depthKey" in response.text
    assert "directionFor" in response.text
    assert "applyPreviewMode" in response.text
    assert "demo') !== 'walk'" in response.text
    assert "drawPhone" in response.text
    assert "drawSleep" in response.text
    assert "drawInteractionCue" in response.text
    assert "状态同步失败 · 可稍后重试" in response.text
    assert "routeGraph" not in response.text
    assert "/debug/users" in response.text


def test_debug_state_and_memory_controls(tmp_path: Path, monkeypatch) -> None:
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
    ).json()
    assert state_response["state"]["mood"] == "curious"
    assert state_response["state"]["trust"] == 42
    assert state_response["updated"] == ["mood", "trust"]

    add_response = client.post(
        "/debug/geoff/memories",
        json={
            "kind": "favorite_thing",
            "content": "用户喜欢桂花乌龙",
            "confidence": 0.8,
        },
    ).json()
    assert add_response == {"ok": True}
    context = client.get("/debug/geoff/context?preview_text=你好").json()
    assert not any("桂花乌龙" in line for line in context["memories"])
    assert any("桂花乌龙" in row["content"] for row in context["available_memories"])

    delete_response = client.delete(
        "/debug/geoff/memories",
        params={"kind": "favorite_thing", "content": "用户喜欢桂花乌龙"},
    ).json()
    assert delete_response == {"deleted": 1}
