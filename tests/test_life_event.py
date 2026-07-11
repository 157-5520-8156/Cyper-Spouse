from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import companion_daemon.life_event as life_event_module
from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.life_event import LifeEvent, LifeEventGenerator, parse_life_event, run


def _record_shareable_event(store: CompanionStore) -> int:
    now = datetime.now(UTC)
    return store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="看书时翻到一段有点好笑的注释，停下来发了会儿呆。",
        started_at=now,
        ends_at=now,
        status="completed",
        source="life_runtime:incidental:test",
    )


def test_unshared_life_events_exclude_legacy_model_authored_sources(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = datetime.now(UTC)
    store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="图书馆遇到一本奇怪的书名。",
        started_at=now,
        ends_at=now,
        status="completed",
        source="life_event:spontaneous_recall",
    )
    expected_id = _record_shareable_event(store)

    rows = store.unshared_private_life_events("geoff")

    assert [row["id"] for row in rows] == [expected_id]


def test_parse_life_event_json() -> None:
    event = parse_life_event(
        '{"topic":"食堂","messages":["刚刚吃到一个好甜的南瓜。","我认真怀疑阿姨今天心情很好。"],"sticker_category":"happy","memory_mode":"planned_today"}'
    )

    assert event.topic == "食堂"
    assert len(event.messages) == 2
    assert event.sticker_category == "happy"
    assert event.memory_mode == "planned_today"


@pytest.mark.asyncio
async def test_life_event_generator_requires_a_previously_recorded_event() -> None:
    generator = LifeEventGenerator(model=object())

    event = await generator.generate(
        mood="calm",
        relationship_stage="friend",
        relationship_status="关系状态：朋友",
        lived_event=None,
    )

    assert event is None


@pytest.mark.asyncio
async def test_life_event_generator_reuses_the_event_ledger_verbatim() -> None:
    generator = LifeEventGenerator(model=object())
    source = "看书时翻到一段有点好笑的注释，停下来发了会儿呆。"

    event = await generator.generate(
        mood="happy",
        relationship_stage="friend",
        relationship_status="关系状态：朋友",
        lived_event=source,
    )

    assert event is not None
    assert source in "".join(event.messages)
    assert "图书馆" not in "".join(event.messages)
    assert "书名" not in "".join(event.messages)


def test_parse_life_event_supports_spontaneous_recall() -> None:
    event = parse_life_event(
        '{"topic":"午饭小插曲","messages":["我刚突然想起来，中午那份饭里有根头发。"],"memory_mode":"spontaneous_recall"}'
    )

    assert event.memory_mode == "spontaneous_recall"


def test_parse_life_event_rewrites_unrealistic_local_invitation() -> None:
    event = parse_life_event(
        '{"topic":"猫咖","messages":["学校后门新开了一家猫咖。","你要不要去？我明天下午没课"],"sticker_category":"happy"}'
    )

    assert event.messages == ["学校后门新开了一家猫咖。", "下次拍给你看。我明天下午没课"]


@pytest.mark.asyncio
async def test_life_event_generator_has_no_model_authored_fact_path() -> None:
    event = await life_event_module.LifeEventGenerator(model=object()).generate(
        mood="calm",
        relationship_stage="friend",
        relationship_status="关系状态：朋友",
        lived_event="路上风有点大，路边的宣传单被吹得到处跑。",
    )

    assert event is not None
    assert event.messages == ["路上风有点大，路边的宣传单被吹得到处跑。刚想起这件小事，想跟你说一下。"]


@pytest.mark.asyncio
async def test_life_event_send_records_outgoing_and_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.map_account("qq", "openid", "geoff")
    _record_shareable_event(store)

    class FakeEngine:
        def __init__(self):
            self.store = store

    class FakeGenerator:
        def __init__(self, model):
            self.model = model

        async def generate(self, *, mood: str, relationship_stage: str, relationship_status: str, life_context: str | None = None, lived_event: str | None = None) -> LifeEvent:
            return LifeEvent(topic="午饭", messages=["刚刚吃了南瓜。", "有点甜。"], memory_mode="spontaneous_recall")

    class FakeDelivery:
        sent: list[str] = []

        def __init__(self, *args, **kwargs):
            return None

        def proactive_recipient_id(self) -> None:
            return None

        async def send_text(self, openid: str, message: str) -> None:
            self.sent.append(message)

    monkeypatch.setattr(life_event_module, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(life_event_module, "LifeEventGenerator", FakeGenerator)
    monkeypatch.setattr(life_event_module, "QQDelivery", FakeDelivery)
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
    assert FakeDelivery.sent == ["刚刚吃了南瓜。", "有点甜。"]
    assert [row["text"] for row in store.recent_messages("geoff", limit=2)] == ["刚刚吃了南瓜。", "有点甜。"]
    assert store.outbox_message(1)["kind"] == "life_event"
    assert store.outbox_message(1)["status"] == "delivered"
    assert store.outbox_message(2)["status"] == "delivered"
    memories = store.memories("geoff", limit=10)
    assert any(row["kind"] == "private_life_event" and row["source"] == "life_runtime:incidental:test" for row in store.recent_life_events("geoff", limit=10))
    assert any(row["kind"] == "life_event" and "午饭" in row["content"] for row in memories)
    assert any(
        row["kind"] == "private_life_event" and row["status"] == "completed"
        for row in store.recent_life_events("geoff", limit=10)
    )
    assert store.memory_by_source("geoff", kind="private_life_event", source="life_event:spontaneous_recall") is None
    assert store.unshared_private_life_events("geoff") == []
    assert store.usage_count("life_event", "month", datetime.now(UTC)) == 0


@pytest.mark.asyncio
async def test_life_event_text_send_failure_does_not_record_lived_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.map_account("qq", "openid", "geoff")
    _record_shareable_event(store)

    class FakeEngine:
        def __init__(self):
            self.store = store

    class FakeGenerator:
        def __init__(self, model):
            self.model = model

        async def generate(self, *, mood: str, relationship_stage: str, relationship_status: str, life_context: str | None = None, lived_event: str | None = None) -> LifeEvent:
            return LifeEvent(topic="午饭", messages=["刚刚吃了南瓜。", "有点甜。"])

    class FailingDelivery:
        def __init__(self, *args, **kwargs):
            return None

        def proactive_recipient_id(self) -> None:
            return None

        async def send_text(self, openid: str, message: str) -> None:
            raise RuntimeError("qq 400")

    monkeypatch.setattr(life_event_module, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(life_event_module, "LifeEventGenerator", FakeGenerator)
    monkeypatch.setattr(life_event_module, "QQDelivery", FailingDelivery)
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

    assert sent is False
    memories = store.memories("geoff", limit=10)
    assert any(row["kind"] == "private_life_event" and row["source"] == "life_runtime:incidental:test" for row in store.recent_life_events("geoff", limit=10))
    assert any(
        row["kind"] == "private_life_event" and row["status"] == "completed"
        for row in store.recent_life_events("geoff", limit=10)
    )
    assert not any(row["kind"] == "life_event" for row in memories)
    assert any(row["kind"] == "life_event_send_failed" and "午饭" in row["content"] for row in memories)
    assert any(row["source"] == "life_runtime:incidental:test" for row in store.unshared_private_life_events("geoff"))
    assert store.last_proactive_delivery("geoff", "qq:life_event") is None
    assert store.recent_messages("geoff", limit=5) == []
    assert store.outbox_message(1)["kind"] == "life_event"
    assert store.outbox_message(1)["status"] == "failed"


@pytest.mark.asyncio
async def test_life_event_partial_send_still_counts_as_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.map_account("qq", "openid", "geoff")
    _record_shareable_event(store)

    class FakeEngine:
        def __init__(self):
            self.store = store

    class FakeGenerator:
        def __init__(self, model):
            self.model = model

        async def generate(self, *, mood: str, relationship_stage: str, relationship_status: str, life_context: str | None = None, lived_event: str | None = None) -> LifeEvent:
            return LifeEvent(topic="午饭", messages=["刚刚吃了南瓜。", "有点甜。"])

    class PartialDelivery:
        def __init__(self, *args, **kwargs):
            return None

        def proactive_recipient_id(self) -> None:
            return None

        async def send_text(self, openid: str, message: str) -> None:
            if message == "有点甜。":
                raise RuntimeError("qq 400")

    monkeypatch.setattr(life_event_module, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(life_event_module, "LifeEventGenerator", FakeGenerator)
    monkeypatch.setattr(life_event_module, "QQDelivery", PartialDelivery)
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

    assert sent is False
    # The first message reached the user, so the event may not be re-shared later.
    assert store.unshared_private_life_events("geoff") == []
    assert store.last_proactive_delivery("geoff", "qq:life_event") is not None
    assert store.outbox_message(1)["status"] == "delivered"
    assert store.outbox_message(2)["status"] == "failed"
    memories = store.memories("geoff", limit=10)
    shared = [row for row in memories if row["kind"] == "life_event"]
    assert shared and "刚刚吃了南瓜。" in shared[0]["content"]
    assert "有点甜。" not in shared[0]["content"]


@pytest.mark.asyncio
async def test_life_event_dry_run_does_not_record_private_life(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)

    class FakeEngine:
        def __init__(self):
            self.store = store

    class FakeGenerator:
        def __init__(self, model):
            self.model = model

        async def generate(self, *, mood: str, relationship_stage: str, relationship_status: str, life_context: str | None = None, lived_event: str | None = None) -> LifeEvent:
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
    assert not any(row["kind"] == "private_life_event" for row in store.memories("geoff", limit=10))


@pytest.mark.asyncio
async def test_life_event_without_ledger_source_never_calls_the_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.map_account("qq", "openid", "geoff")

    class FakeEngine:
        def __init__(self):
            self.store = store

    class FailingGenerator:
        def __init__(self, model):
            raise AssertionError("a model must not invent a life event without a ledger source")

    monkeypatch.setattr(life_event_module, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(life_event_module, "LifeEventGenerator", FailingGenerator)

    sent = await run(user_id="geoff", send=True, sandbox=True, generate_image=False, image_kind="life")

    assert sent is False
    assert store.recent_messages("geoff", limit=5) == []
    assert not any(
        str(row["source"]).startswith("life_event:")
        for row in store.recent_life_events("geoff", limit=10)
    )


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

        async def generate(self, *, mood: str, relationship_stage: str, relationship_status: str, life_context: str | None = None, lived_event: str | None = None) -> LifeEvent:
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
