from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import companion_daemon.life_event as life_event_module
from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.life_event import LifeEvent, LifeEventGenerator, parse_life_event, run
from companion_daemon.world import WorldKernel


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
async def test_legacy_life_event_send_is_retired_without_delivery_or_store_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.map_account("qq", "openid", "geoff")
    _record_shareable_event(store)

    class FakeEngine:
        def __init__(self):
            self.store = store

    class FakeDelivery:
        def __init__(self, *args, **kwargs):
            raise AssertionError("retired legacy path must not construct a delivery adapter")

    monkeypatch.setattr(life_event_module, "build_companion_engine", lambda: FakeEngine())
    monkeypatch.setattr(life_event_module, "QQDelivery", FakeDelivery)
    monkeypatch.setattr(life_event_module, "get_settings", lambda: SimpleNamespace())

    sent = await run(user_id="geoff", send=True, sandbox=True, generate_image=False, image_kind="life")

    assert sent is False
    assert store.outbox_message(1) is None
    assert store.recent_messages("geoff", limit=5) == []
    assert store.last_proactive_delivery("geoff", "qq:life_event") is None
    assert store.unshared_private_life_events("geoff")


@pytest.mark.asyncio
async def test_world_life_event_shares_only_committed_world_experience(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.map_account("qq", "openid", "geoff")
    world = WorldKernel(store)
    started = world.start_from_seed_file(Path("configs/world_seed.yaml"))
    world.advance(
        started.world_id,
        datetime(2026, 7, 11, 12, 30, tzinfo=datetime.fromisoformat("2026-07-11T09:00:00+08:00").tzinfo),
        expected_revision=started.revision,
    )

    class FakeEngine:
        def __init__(self):
            self.world_kernel = world
            self.world_id = started.world_id
            self.store = store

        def _submit_world_with_retry(self, command):
            return world.submit(command, expected_revision=world.revision(started.world_id))

    class FakeDelivery:
        def __init__(self, *args, **kwargs):
            pass

        def proactive_recipient_id(self):
            return None

        async def send_text(self, recipient_id, text):
            assert recipient_id == "openid"
            return {"message_id": "life-share-1"}

    monkeypatch.setattr(life_event_module, "build_companion_engine", FakeEngine)
    monkeypatch.setattr(life_event_module, "QQDelivery", FakeDelivery)
    monkeypatch.setattr(life_event_module, "get_settings", lambda: SimpleNamespace())

    sent = await run(user_id="geoff", send=True, sandbox=True, generate_image=False, image_kind="life")

    assert sent is True
    assert any(item.get("shared") for item in world.snapshot(started.world_id)["experiences"].values())


def test_world_life_share_migrates_a_scheduled_pre_segment_action(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.map_account("qq", "openid", "geoff")
    world = WorldKernel(store)
    started = world.start_from_seed_file(Path("configs/world_seed.yaml"))
    world.advance(
        started.world_id,
        datetime(2026, 7, 11, 12, 30, tzinfo=datetime.fromisoformat("2026-07-11T09:00:00+08:00").tzinfo),
        expected_revision=started.revision,
    )
    selected = world.schedule_life_share_delivery(
        world_id=started.world_id,
        canonical_user_id="geoff",
        platform="qq",
        expires_at=datetime(2026, 7, 11, 16, 30, tzinfo=datetime.fromisoformat("2026-07-11T09:00:00+08:00").tzinfo),
        expected_revision=world.revision(started.world_id),
    )
    assert selected is not None

    # Emulate a persisted action created before ActionSegmentsPlanned existed.
    with store.connect() as conn:
        revision, state = world._load_state(conn, started.world_id)
        del state["actions"][selected.action_id]["segment_state"]
        world._write_projection(conn, started.world_id, revision, state)

    migrated = world.schedule_life_share_delivery(
        world_id=started.world_id,
        canonical_user_id="geoff",
        platform="qq",
        expires_at=datetime(2026, 7, 11, 16, 30, tzinfo=datetime.fromisoformat("2026-07-11T09:00:00+08:00").tzinfo),
        expected_revision=selected.revision,
    )

    assert migrated is not None
    assert migrated.delivery_id == selected.delivery_id
    assert migrated.revision > selected.revision
    action = world.snapshot(started.world_id)["actions"][selected.action_id]
    assert [segment["text"] for segment in action["segment_state"]["segments"]] == [selected.text]
    assert [segment["status"] for segment in action["segment_state"]["segments"]] == ["planned"]
    migrated_events = [
        event for event in world.events(started.world_id)
        if event.event_type == "ActionSegmentsPlanned" and event.source == "schema_migration"
    ]
    assert len(migrated_events) == 1

    repeated = world.schedule_life_share_delivery(
        world_id=started.world_id,
        canonical_user_id="geoff",
        platform="qq",
        expires_at=datetime(2026, 7, 11, 16, 30, tzinfo=datetime.fromisoformat("2026-07-11T09:00:00+08:00").tzinfo),
        expected_revision=migrated.revision,
    )
    assert repeated is not None and repeated.revision == migrated.revision
    assert len([event for event in world.events(started.world_id) if event.source == "schema_migration"]) == 1


@pytest.mark.asyncio
async def test_world_life_event_without_durable_receipt_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.map_account("qq", "openid", "geoff")
    world = WorldKernel(store)
    started = world.start_from_seed_file(Path("configs/world_seed.yaml"))
    world.advance(
        started.world_id,
        datetime(2026, 7, 11, 12, 30, tzinfo=datetime.fromisoformat("2026-07-11T09:00:00+08:00").tzinfo),
        expected_revision=started.revision,
    )

    class FakeEngine:
        def __init__(self):
            self.world_kernel = world
            self.world_id = started.world_id
            self.store = store

    class FakeDelivery:
        def __init__(self, *args, **kwargs):
            pass

        def proactive_recipient_id(self):
            return None

        async def send_text(self, recipient_id, text):
            assert recipient_id == "openid"
            return {"status": "ok"}

    monkeypatch.setattr(life_event_module, "build_companion_engine", FakeEngine)
    monkeypatch.setattr(life_event_module, "QQDelivery", FakeDelivery)
    monkeypatch.setattr(life_event_module, "get_settings", lambda: SimpleNamespace())

    sent = await run(user_id="geoff", send=True, sandbox=True, generate_image=False, image_kind="life")

    assert sent is False
    snapshot = world.snapshot(started.world_id)
    life_share = next(
        action for action in snapshot["actions"].values()
        if action.get("trace", {}).get("life_share")
    )
    assert life_share["status"] == "unknown"
    assert not any(item.get("shared") for item in snapshot["experiences"].values())


@pytest.mark.asyncio
async def test_world_life_event_explicit_adapter_failure_is_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.map_account("qq", "openid", "geoff")
    world = WorldKernel(store)
    started = world.start_from_seed_file(Path("configs/world_seed.yaml"))
    world.advance(
        started.world_id,
        datetime(2026, 7, 11, 12, 30, tzinfo=datetime.fromisoformat("2026-07-11T09:00:00+08:00").tzinfo),
        expected_revision=started.revision,
    )

    class FakeEngine:
        def __init__(self):
            self.world_kernel = world
            self.world_id = started.world_id
            self.store = store

    class FakeDelivery:
        def __init__(self, *args, **kwargs):
            pass

        def proactive_recipient_id(self):
            return None

        async def send_text(self, recipient_id, text):
            assert recipient_id == "openid"
            return {"status": "failed", "retcode": 100}

    monkeypatch.setattr(life_event_module, "build_companion_engine", FakeEngine)
    monkeypatch.setattr(life_event_module, "QQDelivery", FakeDelivery)
    monkeypatch.setattr(life_event_module, "get_settings", lambda: SimpleNamespace())

    sent = await run(user_id="geoff", send=True, sandbox=True, generate_image=False, image_kind="life")

    assert sent is False
    snapshot = world.snapshot(started.world_id)
    life_share = next(
        action for action in snapshot["actions"].values()
        if action.get("trace", {}).get("life_share")
    )
    assert life_share["status"] == "failed"
    assert not any(item.get("shared") for item in snapshot["experiences"].values())


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
async def test_life_event_closes_engine_on_early_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "close-early.sqlite")
    seed_user(store)

    class FakeEngine:
        def __init__(self) -> None:
            self.store = store
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    engine = FakeEngine()
    monkeypatch.setattr(life_event_module, "build_companion_engine", lambda: engine)

    assert await run(
        user_id="geoff",
        send=False,
        sandbox=True,
        generate_image=False,
        image_kind="life",
    ) is False
    assert engine.closed is True


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
