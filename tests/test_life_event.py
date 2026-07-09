from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import companion_daemon.life_event as life_event_module
from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.life_event import LifeEvent, parse_life_event, run


def test_parse_life_event_json() -> None:
    event = parse_life_event(
        '{"topic":"食堂","messages":["刚刚吃到一个好甜的南瓜。","我认真怀疑阿姨今天心情很好。"],"sticker_category":"happy"}'
    )

    assert event.topic == "食堂"
    assert len(event.messages) == 2
    assert event.sticker_category == "happy"


@pytest.mark.asyncio
async def test_life_event_send_records_outgoing_and_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.map_account("qq", "openid", "geoff")

    class FakeEngine:
        def __init__(self):
            self.store = store

    class FakeGenerator:
        def __init__(self, model):
            self.model = model

        async def generate(self, *, mood: str, relationship_stage: str, relationship_status: str) -> LifeEvent:
            return LifeEvent(topic="午饭", messages=["刚刚吃了南瓜。", "有点甜。"])

    class FakeQQClient:
        sent: list[str] = []

        def __init__(self, *args, **kwargs):
            return None

        async def send_c2c_text(self, openid: str, message: str, *, is_wakeup: bool) -> None:
            self.sent.append(message)

    monkeypatch.setattr(life_event_module, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(life_event_module, "LifeEventGenerator", FakeGenerator)
    monkeypatch.setattr(life_event_module, "QQOfficialClient", FakeQQClient)
    monkeypatch.setattr(
        life_event_module,
        "get_settings",
        lambda: SimpleNamespace(
            deepseek_api_key=None,
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-chat",
            monthly_budget_cny=80,
            daily_budget_cny=3,
            soft_daily_budget_cny=2,
            monthly_image_limit=20,
            monthly_vision_limit=120,
            monthly_audio_limit=60,
            openai_api_key=None,
            qq_bot_app_id="app",
            qq_bot_secret="secret",
        ),
    )

    sent = await run(user_id="geoff", send=True, sandbox=True, generate_image=False, image_kind="life")

    assert sent is True
    assert FakeQQClient.sent == ["刚刚吃了南瓜。", "有点甜。"]
    assert [row["text"] for row in store.recent_messages("geoff", limit=2)] == ["刚刚吃了南瓜。", "有点甜。"]
    assert any(row["kind"] == "life_event" and "午饭" in row["content"] for row in store.memories("geoff", limit=10))
    assert store.usage_count("life_event", "month", datetime.now(UTC)) == 1


@pytest.mark.asyncio
async def test_life_event_respects_budget_before_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.record_usage("vision", 2.0)

    class FakeEngine:
        def __init__(self):
            self.store = store

    class FakeGenerator:
        called = False

        def __init__(self, model):
            self.model = model

        async def generate(self, *, mood: str, relationship_stage: str, relationship_status: str) -> LifeEvent:
            type(self).called = True
            return LifeEvent(topic="午饭", messages=["刚刚吃了南瓜。"])

    monkeypatch.setattr(life_event_module, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(life_event_module, "LifeEventGenerator", FakeGenerator)
    monkeypatch.setattr(
        life_event_module,
        "get_settings",
        lambda: SimpleNamespace(
            deepseek_api_key=None,
            deepseek_base_url="https://api.deepseek.com",
            deepseek_model="deepseek-chat",
            monthly_budget_cny=80,
            daily_budget_cny=3,
            soft_daily_budget_cny=2,
            monthly_image_limit=20,
            monthly_vision_limit=120,
            monthly_audio_limit=60,
            openai_api_key=None,
            qq_bot_app_id="app",
            qq_bot_secret="secret",
        ),
    )

    sent = await run(user_id="geoff", send=False, sandbox=True, generate_image=False, image_kind="life")

    assert sent is False
    assert FakeGenerator.called is False
