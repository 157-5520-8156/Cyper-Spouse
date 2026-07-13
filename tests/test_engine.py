import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import (
    CompanionEngine,
    _afterthought_repeats_recent,
    afterthought_prompt,
    relative_chat_time_hint,
    seed_user,
)
from companion_daemon.image_generation import GeneratedImage
from companion_daemon.image_requests import detect_image_request
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MessageAttachment, MoodState
from companion_daemon.multimodal_analysis import AttachmentInsight
from companion_daemon.stickers import StickerCatalog, Sticker
from companion_daemon.character import load_character
from companion_daemon.budget import BudgetGate
from companion_daemon.time import utc_now
from companion_daemon.models import LifeRuntimeState
from companion_daemon.world import WorldKernel

TEST_PROMPT = "你是凛，用户的赛博女友。"


def test_afterthought_rejects_rephrased_repeat() -> None:
    assert _afterthought_repeats_recent(
        "哦对，刚才那句是复习间隙顺手敲的。",
        ["[qq][刚刚] 她: 哦对，你刚说要考试来着。那我不打扰你啦，好好考。"],
    )


def test_afterthought_prompt_keeps_speaker_ownership_and_does_not_invent_a_reply() -> None:
    prompt = afterthought_prompt(
        "quick_continue",
        ["[qq][刚刚] 你: 你是不是在跟别人聊天", "[qq][刚刚] 她: 没有啊，我刚在发呆。"],
    )

    assert "不能假装用户在这之后又说了一句" in prompt
    assert "我信你" in prompt


@pytest.mark.asyncio
async def test_handle_message_updates_mood_and_replies(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )

    assert reply.canonical_user_id == "geoff"
    assert reply.mood == "miss_you"
    assert "我在呢" in reply.text


@pytest.mark.asyncio
async def test_reply_has_a_delivered_turn_trace_with_its_behavioral_contract(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天真的有点撑不住")
    )

    assert reply is not None
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["id"] == reply.turn_trace_id
    assert trace["appraisal"] == "user_vulnerable"
    assert trace["status"] == "delivered"
    assert "优先接住情绪" in trace["observable_reason"]
    assert trace["output_text"] == reply.text
    prompt_text = "\n".join(item["content"] for item in engine.model.calls[-1])
    assert "回合授权（daemon 决定，必须遵守）" in prompt_text


@pytest.mark.asyncio
async def test_skipped_reply_still_leaves_an_observed_turn_trace(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一下"),
        skip_reply=True,
        mark_unread=True,
    )

    assert reply is None
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["direction"] == "incoming_skip"
    assert trace["status"] == "observed"
    assert store.get_mood_state("geoff").has_unread is True


@pytest.mark.asyncio
async def test_world_enabled_reply_records_input_action_and_delivery_settlement(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        TEST_PROMPT,
        world_kernel=world,
        world_id=world_id,
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你在吗", message_id="world-input-1")
    )

    assert reply is not None and reply.world_action_id == f"outgoing:{reply.delivery_id}"
    event_types = [event.event_type for event in world.events(world_id)]
    assert "UserMessageObserved" in event_types
    assert "ActionScheduled" in event_types
    assert "ActionSettled" in event_types
    assert any(
        action["kind"] == "model_call" and action["status"] == "delivered"
        for action in world.snapshot(world_id)["actions"].values()
    )
    assert world.snapshot(world_id)["communication"]["attention"] == "seen"


@pytest.mark.asyncio
async def test_world_reply_prompt_exposes_source_ids_and_current_scene(tmp_path: Path) -> None:
    class GroundedModel:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls.append(messages)
            return (
                '{"reply_text":"在图书馆和范予安核对了读书会的书单。",'
                '"mentioned_event_ids":["outcome:2026-07-11:morning_study"],'
                '"proposed_action_ids":[],"claims":['
                '{"source_id":"outcome:2026-07-11:morning_study",'
                '"text":"在图书馆和范予安核对了读书会的书单。"}]}'
            )

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    logical_start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(world_id, logical_start + timedelta(hours=3, minutes=30), expected_revision=world.revision(world_id))
    model = GroundedModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, world_kernel=world, world_id=world_id)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="上午做了什么？", message_id="grounded-recall")
    )

    assert reply is not None
    assert "范予安" in reply.text
    prompt = "\n".join(item["content"] for item in model.calls[-1])
    assert "outcome:2026-07-11:morning_study" in prompt
    assert "当前场景" in prompt
    assert "逻辑时间" in prompt


@pytest.mark.asyncio
async def test_world_mode_question_thread_is_opened_only_after_delivery_and_closed_by_user_turn(tmp_path: Path) -> None:
    class QuestionModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return '{"reply_text":"你今天还好吗？","mentioned_event_ids":[],"proposed_action_ids":[]}'

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, QuestionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="在吗", message_id="thread-open")
    )
    assert reply is not None
    threads = world.snapshot(world_id)["conversation_threads"]
    assert len(threads) == 1
    thread_id = next(iter(threads))
    assert threads[thread_id]["status"] == "open"

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天很好，因为事情都做完了", message_id="thread-answer")
    )
    assert world.snapshot(world_id)["conversation_threads"][thread_id]["status"] == "answered"


@pytest.mark.asyncio
async def test_world_mode_turn_confirms_durable_user_fact_in_world_ledger(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)
    monkeypatch.setattr(store, "upsert_memory", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy memory")))

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我喜欢桂花乌龙", message_id="fact-1")
    )

    assert any("桂花乌龙" in str(item["value"]) for item in world.snapshot(world_id)["facts"].values())


@pytest.mark.asyncio
async def test_world_mode_typing_transitions_do_not_touch_legacy_mood(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)
    message = IncomingMessage(platform="qq", platform_user_id="geoff", text="你在吗", message_id="typing-1")

    await engine.handle_message(message)
    monkeypatch.setattr(store, "save_mood_state", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy mood")))
    engine.begin_world_typing(message)
    assert world.snapshot(world_id)["communication"]["typing"] == "started"
    engine.stop_world_typing(message, reason="reply_sent")
    assert world.snapshot(world_id)["communication"]["typing"] == "idle"


@pytest.mark.asyncio
async def test_low_energy_is_advisory_and_does_not_hard_veto_a_reply(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {"type": "change_need", "world_id": world_id, "need": "energy", "delta": -40},
        expected_revision=world.revision(world_id),
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="晚点聊", message_id="policy-defer")
    )

    snapshot = world.snapshot(world_id)
    assert reply is not None
    assert reply.world_action_id is not None
    assert snapshot["actions"][reply.world_action_id]["status"] == "delivered"
    assert not any(
        item["kind"] == "reply_later" and item["status"] == "scheduled"
        for item in snapshot["actions"].values()
    )


@pytest.mark.asyncio
async def test_duplicate_world_message_returns_the_original_reply_without_a_second_model_call(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, world_kernel=world, world_id=world_id)
    message = IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        text="你在吗",
        message_id="duplicate-world-message",
    )

    first = await engine.handle_message(message)
    calls_after_first = len(model.calls)
    second = await engine.handle_message(message)

    assert first is not None
    assert second is None
    assert len(model.calls) == calls_after_first
    assert sum(event.event_type == "UserMessageObserved" for event in world.events(world_id)) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_world_message_has_one_turn_owner_and_one_outgoing_action(tmp_path: Path) -> None:
    class SlowModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            await asyncio.sleep(0.03)
            return '{"reply_text":"嗯，你说。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    model = SlowModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, world_kernel=world, world_id=world_id)
    message = IncomingMessage(
        platform="qq", platform_user_id="geoff", text="你在吗", message_id="concurrent-duplicate"
    )

    results = await asyncio.gather(engine.handle_message(message), engine.handle_message(message))

    assert sum(reply is not None for reply in results) == 1
    assert model.calls == 1
    actions = world.snapshot(world_id)["actions"].values()
    assert sum(action["kind"] == "outgoing_message" for action in actions) == 1
    assert sum(event.event_type == "TurnProcessingClaimed" for event in world.events(world_id)) == 1


@pytest.mark.asyncio
async def test_invalid_world_reply_locally_redacts_before_using_a_second_model_call(tmp_path: Path) -> None:
    class RepairModel:
        def __init__(self) -> None:
            self.calls = []

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls.append(messages)
            if len(self.calls) == 1:
                return '{"reply_text":"刚从图书馆回来。你呢？","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            return '{"reply_text":"嗯，你说。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    model = RepairModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, world_kernel=world, world_id=world_id)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你好呀", message_id="repair-invalid")
    )

    assert reply is not None
    assert reply.text == "你呢？"
    assert len(model.calls) == 1
    assert any(
        action["kind"] == "model_call" and action["status"] == "delivered"
        for action in world.snapshot(world_id)["actions"].values()
    )


@pytest.mark.asyncio
async def test_misquoted_current_scene_is_salvaged_as_exact_grounded_text(tmp_path: Path) -> None:
    class SceneMentionModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"刚醒，还赖在床上。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    model = SceneMentionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, world_kernel=world, world_id=world_id)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="现在在做什么？", message_id="scene-salvage")
    )

    assert reply is not None
    assert reply.text == "现在在华东师范大学，正在图书馆看书。"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_boundary_pressure_is_advisory_and_does_not_hard_veto_a_reply(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {"type": "change_need", "world_id": world_id, "need": "boundary", "delta": 80},
        expected_revision=world.revision(world_id),
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你现在就必须发给我", message_id="policy-dnd")
    )

    assert reply is not None
    assert reply.world_action_id is not None
    assert world.snapshot(world_id)["actions"][reply.world_action_id]["status"] == "delivered"


@pytest.mark.asyncio
async def test_world_image_request_uses_generation_and_delivery_actions(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id,
        image_generator=FakeImageGenerator(), image_output_dir=tmp_path / "images",
    )
    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你好", message_id="media-register")
    )
    for index in range(1, 55):
        world.submit(
            {
                "type": "appraise_turn",
                "world_id": world_id,
                "appraisal": "warmth_received",
                "intent_id": f"media-warmth:{index}",
                "message_id": f"media-warmth:{index}",
                "user_id": "user:geoff",
                "idempotency_key": f"media-warmth:{index}",
            },
            expected_revision=world.revision(world_id),
        )
        if world.snapshot(world_id)["relationships"]["user:geoff"]["stage"] == "close_friend":
            break
    assert world.snapshot(world_id)["relationships"]["user:geoff"]["stage"] == "close_friend"

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="能发一张自拍吗", message_id="media-request")
    )

    assert reply is not None
    assert reply.image_path and Path(reply.image_path).exists()
    assert reply.media_action_id
    media = next(iter(world.snapshot(world_id)["media"].values()))
    assert media["status"] == "generated"
    engine.confirm_media_delivery(reply)
    assert world.snapshot(world_id)["media"][media["request_id"]]["status"] == "shared"


@pytest.mark.asyncio
async def test_world_image_can_send_after_text_via_background_adapter(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id,
        image_generator=FakeImageGenerator(), image_output_dir=tmp_path / "images",
    )
    user_id = engine._ensure_world_user("geoff")
    for index in range(1, 55):
        world.submit(
            {
                "type": "appraise_turn", "world_id": world_id,
                "appraisal": "warmth_received", "intent_id": f"async-warmth:{index}",
                "message_id": f"async-warmth:{index}", "user_id": user_id,
                "idempotency_key": f"async-warmth:{index}",
            },
            expected_revision=world.revision(world_id),
        )
        if world.snapshot(world_id)["relationships"][user_id]["stage"] == "close_friend":
            break
    sent: list[Path] = []

    async def deliver(_incoming: IncomingMessage, path: Path) -> bool:
        sent.append(path)
        return True

    engine.set_media_delivery_handler(deliver)
    image_path, action_id, reason = await engine._maybe_generate_world_image(
        user_id=user_id,
        message=IncomingMessage(
            platform="qq", platform_user_id="geoff", text="能发一张自拍吗", message_id="async-media"
        ),
    )

    assert (image_path, action_id, reason) == (None, None, "media_generation_pending")
    for _ in range(30):
        if sent:
            break
        await asyncio.sleep(0.01)
    assert len(sent) == 1 and sent[0].exists()
    media = next(iter(world.snapshot(world_id)["media"].values()))
    assert media["status"] == "shared"
    await engine.aclose()


@pytest.mark.asyncio
async def test_world_media_outbox_is_recovered_after_engine_restart(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {"type": "register_user", "world_id": world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=world.revision(world_id),
    )
    for index in range(1, 55):
        world.submit(
            {
                "type": "appraise_turn", "world_id": world_id,
                "appraisal": "warmth_received", "intent_id": f"restart-warmth:{index}",
                "message_id": f"restart-warmth:{index}", "user_id": "user:geoff",
                "idempotency_key": f"restart-warmth:{index}",
            },
            expected_revision=world.revision(world_id),
        )
        if world.snapshot(world_id)["relationships"]["user:geoff"]["stage"] == "close_friend":
            break
    source_text = "能发一张自拍吗"
    request = detect_image_request(source_text)
    request_id = "media:" + sha256(
        f"user:geoff|restart-media|{request.type}|{request.directive}".encode("utf-8")
    ).hexdigest()[:20]
    world.submit(
        {
            "type": "request_media", "world_id": world_id, "request_id": request_id,
            "user_id": "user:geoff", "media_kind": "selfie", "topic": "窗边自拍",
            "reason": "world_relationship_allows_selfie",
            "delivery_context": {
                "platform": "qq", "platform_user_id": "geoff", "text": source_text, "message_id": "restart-media",
            },
        },
        expected_revision=world.revision(world_id),
    )
    engine = CompanionEngine(
        store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id,
        image_generator=FakeImageGenerator(), image_output_dir=tmp_path / "images",
    )
    delivered: list[Path] = []

    async def deliver(_incoming: IncomingMessage, path: Path) -> bool:
        delivered.append(path)
        return True

    engine.set_media_delivery_handler(deliver)
    assert engine.recover_pending_media() == 1
    for _ in range(30):
        if delivered:
            break
        await asyncio.sleep(0.01)
    assert delivered and delivered[0].exists()
    assert world.snapshot(world_id)["media"][request_id]["status"] == "shared"
    await engine.aclose()


@pytest.mark.asyncio
async def test_world_sticker_selection_and_delivery_are_world_actions(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    stickers = StickerCatalog(
        stickers=[Sticker(id="comfort", category="comfort", mood="calm", intent="comfort", path=Path("assets/stickers/comfort.png"))]
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, stickers=stickers, world_kernel=world, world_id=world_id)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天有点撑不住", message_id="sticker-world")
    )

    assert reply is not None
    assert reply.sticker_path == "assets/stickers/comfort.png"
    assert reply.sticker_action_id == "sticker-delivery:sticker-world"
    engine.confirm_sticker_delivery(reply)
    assert world.snapshot(world_id)["stickers"][reply.sticker_action_id]["status"] == "shared"


def test_world_mode_debug_snapshot_uses_only_world_projection(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)
    monkeypatch.setattr(
        "companion_daemon.engine.advance_life_runtime",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy runtime read")),
    )

    snapshot = engine.debug_snapshot("geoff")

    assert snapshot["state"]["world_id"] == world_id
    assert snapshot["dashboard"]["scene"]["location"] == "desk"
    assert snapshot["recent_social_tasks"] == []


@pytest.mark.asyncio
async def test_world_enabled_turn_does_not_write_legacy_life_or_social_tables(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)
    with store.connect() as conn:
        before = {table: conn.execute(f"select count(*) from {table}").fetchone()[0] for table in ("life_runtime", "life_runtime_events", "calendar_events", "social_tasks")}
    await engine.handle_message(IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿", message_id="world-isolation"))
    with store.connect() as conn:
        after = {table: conn.execute(f"select count(*) from {table}").fetchone()[0] for table in before}
    assert after == before


@pytest.mark.asyncio
async def test_world_mode_store_guard_rejects_legacy_writes_but_allows_world_turn(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    store.enable_world_mode()
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你在吗", message_id="guard-1")
    )

    assert reply is not None
    with pytest.raises(RuntimeError, match="forbids legacy behaviour write"):
        store.save_mood_state("geoff", MoodState())

    # A second process opening the same SQLite file must not recreate a legacy
    # write bypass merely because its in-memory flag starts false.
    restarted_store = CompanionStore(tmp_path / "test.sqlite")
    with pytest.raises(RuntimeError, match="forbids legacy behaviour write"):
        restarted_store.save_mood_state("geoff", MoodState())
    with pytest.raises(RuntimeError, match="forbids legacy behaviour write"):
        restarted_store.cancel_social_task(1)
    with pytest.raises(RuntimeError, match="forbids legacy behaviour write"):
        restarted_store.save_calendar_week(
            "geoff", week_start="2026-07-06", theme="旧日历", summary="不能旁路", source="test"
        )


@pytest.mark.asyncio
async def test_world_mode_does_not_call_legacy_behavior_writers(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)

    def legacy_write(*args, **kwargs):
        raise AssertionError("world mode must not mutate legacy behaviour state")

    for name in (
        "save_mood_state",
        "save_incoming",
        "record_interaction_event",
        "upsert_memory",
        "record_fact_observation",
        "create_social_task",
        "save_life_runtime",
    ):
        monkeypatch.setattr(store, name, legacy_write)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天有点累", message_id="world-only-1")
    )

    assert reply is not None
    assert world.snapshot(world_id)["last_appraisal"]["appraisal"] == "user_vulnerable"


@pytest.mark.asyncio
async def test_world_mode_proactive_uses_only_world_action(tmp_path: Path, monkeypatch) -> None:
    class WorldProactiveModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return '{"private_thought":"想轻轻问候。","should_send":true,"platform":"qq","message_type":"text","message":"今天还顺利吗？","cooldown_minutes":45}'

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {"type": "register_user", "world_id": world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=world.revision(world_id),
    )
    for index in range(4):
        world.submit(
            {
                "type": "appraise_turn", "world_id": world_id,
                "appraisal": "warmth_received", "intent_id": f"proactive-warmth:{index}",
                "message_id": f"proactive-warmth:{index}", "user_id": "user:geoff",
                "idempotency_key": f"proactive-warmth:{index}",
            },
            expected_revision=world.revision(world_id),
        )
    engine = CompanionEngine(store, WorldProactiveModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)

    def legacy_write(*args, **kwargs):
        raise AssertionError("world mode proactive must not use legacy writers")

    for name in ("save_mood_state", "save_proactive_event", "create_social_task", "save_life_runtime"):
        monkeypatch.setattr(store, name, legacy_write)

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is True
    assert decision.world_action_id is not None
    assert world.snapshot(world_id)["last_deliberation"]["stance"] == "initiate"
    engine.confirm_proactive_delivery(decision)
    assert world.snapshot(world_id)["actions"][decision.world_action_id]["status"] == "delivered"


@pytest.mark.asyncio
async def test_world_proactive_keeps_soft_quality_signals_without_serial_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SoftSignalModel:
        calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"private_thought":"想轻轻说一句。","should_send":true,'
                '"platform":"qq","message_type":"text",'
                '"message":"宝宝，我永远爱你；想到你，我心里挺温暖的。",'
                '"cooldown_minutes":45}'
            )

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {"type": "register_user", "world_id": world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=world.revision(world_id),
    )
    original_decide = world.character_deliberation.decide

    def choose_initiate(*args, **kwargs):
        decision = original_decide(*args, **kwargs)
        return replace(
            decision,
            chosen_stance="initiate",
            selection=replace(decision.selection, chosen_stance="initiate"),
        )

    monkeypatch.setattr(world.character_deliberation, "decide", choose_initiate)
    model = SoftSignalModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, world_kernel=world, world_id=world_id)

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    assert decision.world_action_id is None
    assert model.calls == 1
    assert "关系阶段" in decision.private_thought


@pytest.mark.asyncio
async def test_world_proactive_blocks_unsupported_external_capability_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnsupportedCapabilityModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"private_thought":"想替他解决。","should_send":true,'
                '"platform":"qq","message_type":"text",'
                '"message":"我已经替你点好了。","cooldown_minutes":45}'
            )

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {"type": "register_user", "world_id": world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=world.revision(world_id),
    )
    original_decide = world.character_deliberation.decide

    def choose_initiate(*args, **kwargs):
        decision = original_decide(*args, **kwargs)
        return replace(
            decision,
            chosen_stance="initiate",
            selection=replace(decision.selection, chosen_stance="initiate"),
        )

    monkeypatch.setattr(world.character_deliberation, "decide", choose_initiate)
    engine = CompanionEngine(
        store, UnsupportedCapabilityModel(), TEST_PROMPT, world_kernel=world, world_id=world_id
    )

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    assert decision.message is None
    assert "硬约束" in decision.private_thought
    assert not any(
        action["kind"] == "outgoing_message"
        for action in world.snapshot(world_id)["actions"].values()
    )


@pytest.mark.asyncio
async def test_world_mode_withheld_proactive_is_a_reviewable_world_decision(tmp_path: Path) -> None:
    class WithholdingModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return '{"private_thought":"他可能正在忙，先不打扰。","should_send":false}'

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {"type": "register_user", "world_id": world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=world.revision(world_id),
    )
    for index in range(4):
        world.submit(
            {
                "type": "appraise_turn", "world_id": world_id,
                "appraisal": "warmth_received", "intent_id": f"withheld-warmth:{index}",
                "message_id": f"withheld-warmth:{index}", "user_id": "user:geoff",
                "idempotency_key": f"withheld-warmth:{index}",
            },
            expected_revision=world.revision(world_id),
        )
    engine = CompanionEngine(store, WithholdingModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    deferred = list(world.snapshot(world_id)["decisions"].values())
    assert deferred and deferred[0]["kind"] == "withheld_impulse"
    assert world.snapshot(world_id)["actions"][deferred[0]["action_id"]]["status"] == "scheduled"


@pytest.mark.asyncio
async def test_world_proactive_stance_can_defer_before_model_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ModelMustNotRun:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("a deferred proactive stance must not generate prose")

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {"type": "register_user", "world_id": world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=world.revision(world_id),
    )
    original = world.character_deliberation.decide

    def choose_defer(*args, **kwargs):
        decision = original(*args, **kwargs)
        return replace(
            decision,
            chosen_stance="defer",
            display_strategy="delay_without_false_promise",
            selection=replace(decision.selection, chosen_stance="defer"),
        )

    monkeypatch.setattr(world.character_deliberation, "decide", choose_defer)
    engine = CompanionEngine(
        store, ModelMustNotRun(), TEST_PROMPT, world_kernel=world, world_id=world_id
    )

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    snapshot = world.snapshot(world_id)
    assert snapshot["last_deliberation"]["stance"] == "defer"
    assert any(
        item["kind"] == "proactive_defer" and item["status"] == "deferred"
        for item in snapshot["decisions"].values()
    )
    assert not any(
        item["kind"] == "outgoing_message" for item in snapshot["actions"].values()
    )


@pytest.mark.asyncio
async def test_concurrent_world_proactive_ticks_claim_generation_once(tmp_path: Path) -> None:
    class BlockingModel:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return (
                '{"private_thought":"想轻轻问候。","should_send":true,'
                '"platform":"qq","message_type":"text","message":"今天还顺利吗？"}'
            )

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    world.submit(
        {"type": "register_user", "world_id": world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=world.revision(world_id),
    )
    model = BlockingModel()
    engine = CompanionEngine(
        store, model, TEST_PROMPT, world_kernel=world, world_id=world_id
    )

    first = asyncio.create_task(engine.proactive_tick("geoff"))
    await model.started.wait()
    second = asyncio.create_task(engine.proactive_tick("geoff"))
    await asyncio.sleep(0)
    model.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert model.calls == 1
    assert sorted([first_result.should_send, second_result.should_send]) == [False, True]
    assert sum(
        action["kind"] == "outgoing_message"
        for action in world.snapshot(world_id)["actions"].values()
    ) == 1


@pytest.mark.asyncio
async def test_world_proactive_treats_an_open_conversation_thread_as_costly_soft_pressure(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)
    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="在吗", message_id="proactive-thread-input")
    )
    # FakeCompanionModel's normal reply is not a question; create one delivered
    # world question explicitly to make the gate independent of model wording.
    logical_now = engine._world_logical_now()
    delivery_id, _, _ = world.queue_outgoing_action(
        canonical_user_id="geoff", platform="qq", text="你还好吗？", kind="reply",
        expires_at=logical_now + timedelta(hours=12),
        trace={
            "world_id": world_id, "appraisal": "ordinary_message", "expression_policy": "test",
            "allowed_facts": [], "observable_reason": "test",
            "conversation_thread": {
                "thread_id": "proactive-thread", "user_id": "user:geoff", "question": "你还好吗？",
                "expires_at": (logical_now + timedelta(hours=12)).isoformat(),
            },
        },
    )
    world.settle_outgoing_action(delivery_id, delivered=True)

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    proactive_prompt = "\n".join(item["content"] for item in engine.model.calls[-1])
    assert "当前主动软压力: open_conversation_thread" in proactive_prompt
    assert "越过代价: 20; strike: 1" in proactive_prompt


@pytest.mark.asyncio
async def test_world_proactive_can_deliberately_override_open_thread_soft_pressure(tmp_path: Path) -> None:
    class StubbornButBoundedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"private_thought":"我知道刚问过，但还是想把这点在意说出来。",'
                '"should_send":true,"platform":"qq","message_type":"text",'
                '"message":"刚才那句你不用急着答，但我还是有点想知道。",'
                '"cooldown_minutes":45}'
            )

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store, StubbornButBoundedModel(), TEST_PROMPT, world_kernel=world, world_id=world_id
    )
    await engine.handle_message(
        IncomingMessage(
            platform="qq", platform_user_id="geoff", text="在吗", message_id="override-thread-input"
        )
    )
    logical_now = engine._world_logical_now()
    delivery_id, _, _ = world.queue_outgoing_action(
        canonical_user_id="geoff", platform="qq", text="你还好吗？", kind="reply",
        expires_at=logical_now + timedelta(hours=12),
        trace={
            "world_id": world_id, "appraisal": "ordinary_message", "expression_policy": "test",
            "allowed_facts": [], "observable_reason": "test",
            "conversation_thread": {
                "thread_id": "override-thread", "user_id": "user:geoff", "question": "你还好吗？",
                "expires_at": (logical_now + timedelta(hours=12)).isoformat(),
            },
        },
    )
    world.settle_outgoing_action(delivery_id, delivered=True)

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is True
    assert decision.world_action_id is not None
    snapshot = world.snapshot(world_id)
    assert snapshot["actions"][decision.world_action_id]["status"] == "scheduled"
    overrides = [
        event for event in world.events(world_id) if event.event_type == "OutboundSoftGateOverridden"
    ]
    assert overrides
    override = overrides[-1].payload["override"]
    assert override["cost"] == 20
    assert override["strike"] == 1
    assert "open_conversation_thread" in str(override["reason"])


@pytest.mark.asyncio
async def test_world_remain_silent_stance_is_advisory_and_does_not_veto_a_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ModelStillRuns:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return '{"reply_text":"我听到了。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    original = world.character_deliberation.decide

    def choose_silence(*args, **kwargs):
        decision = original(*args, **kwargs)
        return replace(
            decision,
            chosen_stance="remain_silent",
            display_strategy="withhold_expression_without_fabricating_agreement",
            selection=replace(decision.selection, chosen_stance="remain_silent"),
        )

    monkeypatch.setattr(world.character_deliberation, "decide", choose_silence)
    model = ModelStillRuns()
    engine = CompanionEngine(store, model, TEST_PROMPT, world_kernel=world, world_id=world_id)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="我说完了。",
            message_id="deliberate-silence",
        )
    )

    assert reply is not None
    assert model.calls == 1
    snapshot = world.snapshot(world_id)
    assert snapshot["last_deliberation"]["stance"] == "remain_silent"
    assert not any(item["kind"] == "deliberate_silence" for item in snapshot["decisions"].values())
    assert any(
        item["kind"] == "outgoing_message"
        for item in snapshot["actions"].values()
    )


@pytest.mark.asyncio
async def test_world_proactive_feedback_becomes_a_versioned_turn_consequence(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)
    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你好", message_id="feedback-register")
    )
    delivery_id, _, _ = world.queue_outgoing_action(
        canonical_user_id="geoff", platform="qq", text="今天还顺利吗？", kind="proactive",
        expires_at=engine._world_logical_now() + timedelta(hours=12),
            trace={
                "world_id": world_id, "direction": "proactive", "appraisal": "checkin",
                "expression_policy": "test", "allowed_facts": [], "observable_reason": "test",
                "outbound_override": {
                    "reason": "test models a deliberate check-in despite the recent reply",
                    "cost": 10,
                    "strike": 1,
                },
            },
    )
    world.settle_outgoing_action(delivery_id, delivered=True)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="谢谢你，我在", message_id="feedback-warm")
    )

    snapshot = world.snapshot(world_id)
    assert snapshot["last_appraisal"]["appraisal"] == "warmth_received"
    assert snapshot["last_appraisal"]["rule_version"] == "world-interaction-v2"
    assert snapshot["relationships"]["user:geoff"]["closeness"] >= 4


def test_world_mode_delayed_reply_is_a_cancellable_world_action(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)

    def legacy_write(*args, **kwargs):
        raise AssertionError("world mode must not create social_tasks")

    monkeypatch.setattr(store, "create_social_task", legacy_write)
    message = IncomingMessage(platform="qq", platform_user_id="geoff", text="晚点说", message_id="delay-1")
    action_id = engine.create_read_later_task(message, defer_minutes=5, reason="busy")

    assert isinstance(action_id, str)
    assert world.snapshot(world_id)["actions"][action_id]["status"] == "scheduled"
    engine.cancel_deferred_reply_task(action_id)
    assert world.snapshot(world_id)["actions"][action_id]["status"] == "cancelled"


def test_world_afterthought_confirmation_does_not_write_legacy_mood(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)

    monkeypatch.setattr(store, "save_mood_state", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy mood")))
    delivery_id = engine.queue_afterthought_delivery("geoff", "qq", "哦对，补一句。")
    engine.confirm_afterthought_delivery("geoff", "qq", "哦对，补一句。", delivery_id=delivery_id)

    action_id = world.action_id_for_delivery(world_id, delivery_id)
    assert action_id is not None
    assert world.snapshot(world_id)["actions"][action_id]["status"] == "delivered"


def test_world_conversation_pulse_is_a_cancellable_world_action(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    store.enable_world_mode()
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)
    monkeypatch.setattr(store, "create_social_task", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy task")))

    action_id = engine.schedule_conversation_pulse(
        canonical_user_id="geoff", platform="qq", platform_user_id="openid", reply_sent_at=utc_now(),
        mode="quick_continue", delay_seconds=5, remaining=[],
    )

    assert isinstance(action_id, str)
    assert engine.conversation_pulse_is_active(action_id) is True
    engine.cancel_conversation_pulse(action_id)
    assert engine.conversation_pulse_is_active(action_id) is False


@pytest.mark.asyncio
async def test_new_world_turn_cancels_pending_conversation_pulse(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)
    action_id = engine.schedule_conversation_pulse(
        canonical_user_id="geoff", platform="qq", platform_user_id="openid", reply_sent_at=utc_now(),
        mode="quick_continue", delay_seconds=5, remaining=[],
    )

    await engine.handle_message(IncomingMessage(platform="qq", platform_user_id="geoff", text="我回来啦", message_id="cancel-pulse"))

    assert world.snapshot(world_id)["actions"][action_id]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_new_world_turn_supersedes_persisted_delayed_reply(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, world_kernel=world, world_id=world_id)
    delayed = engine.create_deferred_reply_task(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="晚点说", message_id="old-delay"),
        defer_minutes=5,
        reason="busy",
    )

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我回来了", message_id="new-turn")
    )

    assert world.snapshot(world_id)["actions"][delayed]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_failed_reply_marks_the_same_turn_trace_failed(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你在吗"),
        defer_delivery=True,
    )

    assert reply is not None
    commit = engine.fail_reply_delivery(reply, "network failed")
    assert commit is not None and commit.status == "failed"
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["id"] == reply.turn_trace_id
    assert trace["status"] == "failed"


def test_outbox_and_trace_are_created_as_one_auditable_unit(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.db")
    store.resolve_user("qq", "user")

    delivery_id, trace_id = store.queue_outgoing_with_turn_trace(
        "geoff",
        "qq",
        "我晚点再认真回你。",
        kind="reply",
        appraisal="availability_drop",
        expression_policy="简短说明并收住。",
        allowed_facts=[],
        short_lived_constraint=None,
        observable_reason="当前不适合展开。",
    )

    assert store.outbox_message(delivery_id)["status"] == "planned"
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["id"] == trace_id
    assert trace["delivery_id"] == delivery_id
    assert trace["status"] == "planned"


def test_afterthought_delivery_has_the_same_auditable_commit_path(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    delivery_id = engine.queue_afterthought_delivery("geoff", "qq", "哦对，还想补一句。")
    engine.confirm_afterthought_delivery("geoff", "qq", "哦对，还想补一句。", delivery_id=delivery_id)

    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["direction"] == "afterthought"
    assert trace["delivery_id"] == delivery_id
    assert trace["status"] == "delivered"


def test_cancelled_afterthought_closes_its_outbox_and_trace(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    delivery_id = engine.queue_afterthought_delivery("geoff", "qq", "等等，我补一句。")
    engine.fail_afterthought_delivery(delivery_id, "cancelled by newer user turn")

    assert store.outbox_message(delivery_id)["status"] == "failed"
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["status"] == "failed"
    assert trace["failure_reason"] == "cancelled by newer user turn"


def test_read_now_attention_trace_can_be_resolved_by_a_later_task(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "companion.db")
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    message = IncomingMessage(platform="qq", platform_user_id="user", text="在吗")
    store.resolve_user("qq", "user")

    decision = engine.phone_attention_decision(message)
    assert decision.read_now is True
    task_id = engine.create_read_later_task(
        message,
        defer_minutes=2,
        reason="busy_after_read",
        turn_trace_id=decision.turn_trace_id,
    )
    engine.complete_deferred_reply_task(task_id)

    task = store.recent_social_tasks("geoff")[-1]
    trace = store.recent_turn_traces("geoff")[-1]
    assert task["origin_turn_trace_id"] == decision.turn_trace_id
    assert task["reason_code"] == "attention_read_later"
    assert task["resolution"] == "completed"
    assert trace["status"] == "resolved"


@pytest.mark.asyncio
async def test_delivery_trace_cannot_be_changed_to_failed_after_delivery(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(IncomingMessage(platform="qq", platform_user_id="geoff", text="在吗"))

    assert reply is not None
    engine.fail_reply_delivery(reply, "late failure callback")
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["status"] == "delivered"
    assert trace["failure_reason"] is None


@pytest.mark.asyncio
async def test_character_examples_are_not_replayed_as_fake_chat_history(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(
        store,
        model,
        load_character("configs/character.yaml").system_prompt(),
        character_profile=load_character("configs/character.yaml"),
    )

    await engine.handle_message(IncomingMessage(platform="qq", platform_user_id="geoff", text="你好"))

    prompt = model.calls[-1]
    assert not any(message["role"] == "user" and message["content"] == "你叫什么？" for message in prompt)


def test_self_fact_ledger_uses_canonical_facts_not_freeform_background(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    character = load_character("configs/character.yaml")
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        character.system_prompt(),
        character_profile=character,
    )

    facts = engine._self_fact_lines("geoff")

    assert any("没有可验证的宠物饲养经历" in fact for fact in facts)
    assert not any("成长背景" in fact for fact in facts)
    assert not any("书店门口" in fact for fact in facts)


def test_self_fact_ledger_excludes_legacy_model_authored_life_events(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    now = utc_now()
    store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="图书馆遇到一本奇怪的书名。",
        started_at=now,
        ends_at=now,
        status="completed",
        source="life_event:spontaneous_recall",
    )
    store.record_life_event(
        "geoff",
        kind="private_life_event",
        content="看书时翻到一段有点好笑的注释。",
        started_at=now,
        ends_at=now,
        status="completed",
        source="life_runtime:incidental:test",
    )

    facts = engine._self_fact_lines("geoff")

    assert any("有点好笑的注释" in fact for fact in facts)
    assert not any("奇怪的书名" in fact for fact in facts)


def test_engine_wakes_for_the_next_message_after_persisting_an_unread_state(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    now = utc_now()
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
                activity="正在专注看书，手机放在包里",
                activity_kind="study",
            attention_demand=88,
            interruptible=False,
            started_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(minutes=35),
            phone_attention="away",
            updated_at=now,
        ),
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    first = engine.phone_attention_decision(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我到啦")
    )
    second = engine.phone_attention_decision(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我再说一句")
    )

    assert first.read_now is False
    assert store.get_mood_state("geoff").has_unread is True
    first_trace = store.recent_turn_traces("geoff")[-2]
    assert first_trace["direction"] == "attention"
    assert first_trace["status"] == "planned"
    assert second.read_now is True


def test_cancelling_delayed_reply_closes_its_attention_trace(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    message = IncomingMessage(platform="qq", platform_user_id="geoff", text="我晚点再说")
    now = utc_now()
    store.save_life_runtime(
        "geoff",
        LifeRuntimeState(
            activity="正在专注看书，手机放在包里",
            activity_kind="study",
            attention_demand=88,
            interruptible=False,
            started_at=now - timedelta(minutes=5),
            ends_at=now + timedelta(minutes=30),
            updated_at=now,
        ),
    )
    decision = engine.phone_attention_decision(message)

    assert decision.read_now is False

    task_id = engine.create_deferred_reply_task(
        message, defer_minutes=5, reason="unread_during_study", turn_trace_id=decision.turn_trace_id
    )
    engine.cancel_deferred_reply_task(task_id)

    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["id"] == decision.turn_trace_id
    assert trace["status"] == "cancelled"


@pytest.mark.asyncio
async def test_normal_reply_runs_the_shared_output_safety_cleanup(tmp_path: Path) -> None:
    class LocationConfusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return "（手机震了一下）成都这边食堂倒没这么卷。"

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, LocationConfusedModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你们那边食堂怎么样")
    )

    assert reply is not None
    assert reply.text == "上海这边食堂倒没这么卷。"


@pytest.mark.asyncio
async def test_rewrite_uses_fact_ledger_to_remove_ungrounded_history(tmp_path: Path) -> None:
    class FactCheckingModel:
        def __init__(self):
            self.calls: list[list[dict[str, str]]] = []

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls.append(messages)
            if "她的消息：" in messages[0]["content"]:
                return "没有呢，不过 B 站吸猫倒是真的很快乐。"
            return "没有呢，小时候养过几条金鱼，最后都……你懂的。后来就只敢养植物了。不过 B 站吸猫倒是真的很快乐。"

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FactCheckingModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, rewrite_model=model)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你养过别的宠物吗"),
    )

    assert reply is not None
    assert "金鱼" not in reply.text
    assert "后来就只敢养植物" not in reply.text
    assert "B 站吸猫" in reply.text
    rewrite_prompt = model.calls[-1][0]["content"]
    assert "事实账本" in rewrite_prompt
    assert "可用知栀事实" in rewrite_prompt


@pytest.mark.asyncio
async def test_deferred_reply_only_enters_history_after_delivery_confirmation(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿"),
        defer_delivery=True,
    )

    assert reply.delivery_id is not None
    assert store.outbox_message(reply.delivery_id)["status"] == "planned"
    assert not any(row["direction"] == "out" for row in store.recent_messages("geoff"))

    engine.fail_reply_delivery(reply, "network failed")

    assert store.outbox_message(reply.delivery_id)["status"] == "failed"
    assert not any(row["direction"] == "out" for row in store.recent_messages("geoff"))


@pytest.mark.asyncio
async def test_failed_deferred_reply_creates_reconsider_task_without_writing_history(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    original = IncomingMessage(platform="qq", platform_user_id="geoff", text="你刚刚还没回我那个")
    task_id = engine.create_deferred_reply_task(original, defer_minutes=5, reason="unread_during_study")

    reply = await engine.handle_message(original, defer_delivery=True, context_hint="刚才因为在忙，隔了一会儿才回来。")
    engine.fail_reply_delivery(reply, "network failed", source_task_id=task_id)

    tasks = store.recent_social_tasks("geoff")
    assert store.outbox_message(reply.delivery_id)["status"] == "failed"
    assert not any(row["direction"] == "out" for row in store.recent_messages("geoff"))
    assert any(task["id"] == task_id and task["kind"] == "reply_later" and task["status"] == "cancelled" for task in tasks)
    retry = [task for task in tasks if task["kind"] == "reply_reconsider"]
    assert retry and retry[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_serious_apology_uses_single_repair_curve_and_still_records_key_event(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            mood="hurt",
            trust=30,
            security=30,
            emotional_charge=30,
            patience=40,
        ),
    )
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我认真道歉，以后我会注意")
    )

    state = store.get_mood_state("geoff")
    assert state.mood == "calm"
    assert state.security > 30
    assert state.patience > 40
    assert state.emotional_charge < 30
    assert state.last_interaction_event == "repair_attempt"
    assert any(
        row["kind"] == "key_relationship_event" and "认真修复" in row["content"]
        for row in store.memories("geoff")
    )
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "关键事件" in prompt_text


@pytest.mark.asyncio
async def test_key_relationship_event_reaches_the_reply_prompt(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你还记得那天说的桂花乌龙吗")
    )

    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "关键事件" in prompt_text
    assert any(row["kind"] == "key_relationship_event" for row in store.memories("geoff"))


def test_failed_proactive_delivery_creates_reconsider_task(tmp_path: Path) -> None:
    from companion_daemon.models import ProactiveDecision

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    decision = ProactiveDecision(
        canonical_user_id="geoff",
        private_thought="想跟他说一句",
        should_send=True,
        platform="qq",
        message_type="text",
        message="刚路过操场，突然想到你。",
        delivery_id=store.queue_outgoing("geoff", "qq", "刚路过操场，突然想到你。", kind="proactive"),
    )

    engine.fail_proactive_delivery(decision, "network down")

    assert store.outbox_message(decision.delivery_id)["status"] == "failed"
    tasks = store.recent_social_tasks("geoff")
    reconsider = [task for task in tasks if task["kind"] == "reply_reconsider"]
    assert reconsider and reconsider[0]["status"] == "pending"
    assert "主动消息" in reconsider[0]["reason"]
    assert not any(row["direction"] == "out" for row in store.recent_messages("geoff"))


def test_refresh_waiting_state_advances_waiting_psychology(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    # A proactive delivery four hours ago with no incoming reply since.
    with store.connect() as conn:
        conn.execute(
            "insert into proactive_delivery (canonical_user_id, platform, sent_at) values (?, ?, ?)",
            ("geoff", "qq", (utc_now() - timedelta(hours=4)).isoformat()),
        )

    state = engine.refresh_waiting_state("geoff")

    assert state.unresolved_emotion == "主动消息没等到回应，她会把分享欲收住，先回到自己的节奏。"
    assert store.get_mood_state("geoff").unresolved_emotion == state.unresolved_emotion


def test_long_unanswered_outgoing_streak_blocks_new_proactive_turns(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    store.save_outgoing("geoff", "qq", "刚刚想到一件小事。")
    store.save_outgoing("geoff", "qq", "再补一句。")
    with store.connect() as conn:
        conn.execute(
            "update messages set sent_at = ? where canonical_user_id = ? and direction = 'out'",
            ((utc_now() - timedelta(hours=2)).isoformat(), "geoff"),
        )

    assert engine.outreach_block_reason("geoff")


def test_waiting_clock_uses_first_outgoing_after_last_user_turn(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_incoming("geoff", IncomingMessage(platform="qq", platform_user_id="geoff", text="晚安"))
    store.save_outgoing("geoff", "qq", "晚安。")
    store.save_outgoing("geoff", "qq", "刚刚又想到一点。")
    with store.connect() as conn:
        conn.execute(
            "update messages set sent_at = ? where canonical_user_id = ? and direction = 'in'",
            ((utc_now() - timedelta(hours=14)).isoformat(), "geoff"),
        )
        rows = conn.execute(
            "select id from messages where canonical_user_id = ? and direction = 'out' order by id",
            ("geoff",),
        ).fetchall()
        conn.execute(
            "update messages set sent_at = ? where id = ?",
            ((utc_now() - timedelta(hours=13)).isoformat(), rows[0]["id"]),
        )
        conn.execute(
            "update messages set sent_at = ? where id = ?",
            ((utc_now() - timedelta(minutes=5)).isoformat(), rows[1]["id"]),
        )

    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    state = engine.refresh_waiting_state("geoff")

    assert state.initiative <= 8
    assert "不再追着补话" in (state.unresolved_emotion or "")


def test_debug_snapshot_includes_social_task_visibility(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    store.create_social_task(
        "geoff",
        kind="reply_later",
        platform="qq",
        platform_user_id="2759284998",
        payload={"text": "稍后回"},
        reason="unread_during_class",
        due_at=utc_now(),
        expires_at=utc_now() + timedelta(hours=1),
    )

    snapshot = engine.debug_snapshot("geoff")

    assert snapshot["recent_social_tasks"][0]["reason"] == "unread_during_class"
    assert snapshot["dashboard"]["active_task_count"] == 1
    assert snapshot["dashboard"]["next_plan"]
    assert snapshot["dashboard"]["scene"]["location"]
    assert snapshot["dashboard"]["scene"]["action"]


@pytest.mark.asyncio
async def test_rudeness_updates_persistent_impression_and_current_reply_policy(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="你算什么，闭嘴")
    )

    state = store.get_mood_state("geoff")
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert state.perceived_respect < 50
    assert state.mood == "hurt"
    assert "明确表示不舒服" in prompt_text
    assert "最近感到不被尊重" in prompt_text


@pytest.mark.asyncio
async def test_skip_reply_can_avoid_unread_for_pure_ack(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="嗯嗯"),
        skip_reply=True,
        mark_unread=False,
    )

    assert reply is None
    assert store.get_mood_state("geoff").has_unread is False


@pytest.mark.asyncio
async def test_skip_reply_can_mark_unread_for_deferred_message(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我刚刚想了很久，" * 8),
        skip_reply=True,
        mark_unread=True,
    )

    assert reply is None
    assert store.get_mood_state("geoff").has_unread is True


@pytest.mark.asyncio
async def test_handle_message_injects_human_rhythm_context(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )

    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "生活节律" in prompt_text
    assert "上下文编排" in prompt_text
    assert "当前用户意图" in prompt_text
    assert "像手机私聊" in prompt_text
    assert any(row["kind"] == "life_continuity" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_reply_prompt_does_not_duplicate_current_user_message_in_recent_history(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_outgoing("geoff", "qq", "我刚从图书馆出来。")
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    unique_text = "单轮上下文排重测试XYZ"
    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text=unique_text)
    )

    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    recent_block = next(message["content"] for message in model.calls[-1] if message["content"].startswith("最近聊天:"))
    assert unique_text not in recent_block
    assert prompt_text.count(unique_text) >= 1
    assert "图书馆" in recent_block


def test_recent_lines_sanitize_previous_bad_outgoing(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    store.save_outgoing(
        "geoff",
        "qq",
        "成都理工啊，那你们学校后门是不是有条街全是串串和冰粉？我有个高中同学在那读土木，她跟我提过。",
    )

    recent = engine._recent_lines("geoff")

    assert "高中同学" not in "\n".join(recent)


def test_recent_lines_include_local_recency_hint(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)
    store.save_outgoing("geoff", "qq", "我刚从图书馆出来。")

    recent = engine._recent_lines("geoff")

    assert "[qq][" in recent[-1]
    assert "] 她:" in recent[-1]


def test_relative_chat_time_hint_uses_local_overnight_labels() -> None:
    now = datetime(2026, 7, 10, 10, 34, tzinfo=UTC)

    assert relative_chat_time_hint("2026-07-10T10:30:00+00:00", now=now) == "刚刚"
    assert relative_chat_time_hint("2026-07-10T09:50:00+00:00", now=now) == "刚才"
    assert relative_chat_time_hint("2026-07-09T19:40:00+00:00", now=now) == "今天凌晨"
    assert relative_chat_time_hint("2026-07-08T19:40:00+00:00", now=now) == "昨晚"


def test_debug_snapshot_exposes_daemon_context_and_prompt(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_outgoing("geoff", "qq", "我刚从图书馆出来。")
    store.upsert_memory(
        "geoff",
        kind="life_fact",
        content="用户人在成都",
        source="test",
        confidence=0.8,
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    snapshot = engine.debug_snapshot("geoff", preview_text="你在干嘛")

    assert snapshot["canonical_user_id"] == "geoff"
    assert "state" in snapshot
    assert any("图书馆" in line for line in snapshot["recent"])
    assert not any("成都" in line for line in snapshot["memories"])
    assert "context_package" in snapshot
    prompt_text = "\n".join(message["content"] for message in snapshot["preview_prompt"])
    assert "最近聊天" in prompt_text
    assert "上下文编排" in prompt_text
    assert "你在干嘛" in prompt_text


@pytest.mark.asyncio
async def test_handle_message_relaxes_after_warm_proactive_response(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            mood="miss_you",
            security=35,
            initiative=45,
            emotional_charge=18,
            last_platform="qq",
        ),
    )
    store.record_proactive_delivery("geoff", "qq")
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我在呀，刚刚忙完")
    )

    state = store.get_mood_state("geoff")
    assert state.security > 35
    assert state.initiative < 45
    assert state.emotional_charge < 18
    assert any(row["kind"] == "proactive_response" for row in store.memories("geoff"))
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "主动反馈" not in prompt_text
    assert "本轮回复策略" in prompt_text


@pytest.mark.asyncio
async def test_life_share_response_uses_the_same_feedback_loop_as_proactive_message(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store, initial_state=MoodState(mood="miss_you", security=35, initiative=45))
    store.record_proactive_delivery("geoff", "qq:life_event")
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="哈哈，听起来也太离谱了")
    )

    state = store.get_mood_state("geoff")
    assert state.security > 35
    assert state.perceived_responsiveness > 50
    assert any(row["kind"] == "proactive_response" for row in store.memories("geoff"))


def test_confirm_life_event_delivery_feeds_back_into_life_runtime(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store, initial_state=MoodState(initiative=45, emotional_charge=12))
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    engine.confirm_life_event_delivery("geoff")

    state = store.get_mood_state("geoff")
    assert store.last_initiated_delivery("geoff", "qq")
    assert state.initiative < 45
    assert store.get_life_runtime("geoff") is not None


@pytest.mark.asyncio
async def test_user_event_can_change_current_life_context(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我现在真的好难受")
    )

    runtime = store.get_life_runtime("geoff")
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert runtime.user_event_effect
    assert "用户事件余波" in prompt_text


@pytest.mark.asyncio
async def test_handle_message_notices_skipped_own_question(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_outgoing("geoff", "qq", "你刚刚是不是在忙？")
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我回来了")
    )

    state = store.get_mood_state("geoff")
    assert state.security < 45
    assert any(row["kind"] == "own_question_skipped" for row in store.memories("geoff"))
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "没有回答她刚刚问的问题" not in prompt_text
    assert "保留一点情绪" in prompt_text


@pytest.mark.asyncio
async def test_platform_switch_context_is_reported(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    store.map_account("wechat", "wechat-geoff", "geoff")
    await engine.handle_message(
        IncomingMessage(platform="wechat", platform_user_id="wechat-geoff", text="等我一下")
    )
    store.map_account("qq", "qq-geoff", "geoff")
    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="qq-geoff", text="我回来了")
    )

    assert reply.platform_context == "刚刚在 wechat 聊，现在切到了 qq。"


@pytest.mark.asyncio
async def test_user_self_image_claim_records_visual_anchor(tmp_path: Path) -> None:
    class FakeAnalyzer:
        async def analyze(self, attachment: MessageAttachment) -> AttachmentInsight:
            return AttachmentInsight("image", "图片内容：一张室内自拍，人物穿深色上衣。", 0.82)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, multimodal_analyzer=FakeAnalyzer())

    await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="这是我刚拍的自拍",
            attachments=[MessageAttachment(kind="image", url="https://example.test/me.jpg")],
        )
    )

    memories = store.memories("geoff", limit=20)
    assert any(row["kind"] == "user_visual_anchor" and "室内自拍" in row["content"] for row in memories)
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "视觉身份" in prompt_text


@pytest.mark.asyncio
async def test_proactive_tick_records_decision(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store, initial_state=MoodState(initiative=60, emotional_charge=15))
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )
    outgoing_before = sum(
        row["direction"] == "out" for row in store.recent_messages("geoff", limit=20)
    )
    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is True
    assert decision.turn_trace_id is not None
    assert decision.platform == "qq"
    assert decision.message
    assert decision.delivery_id is not None
    state = store.get_mood_state("geoff")
    assert store.outbox_message(decision.delivery_id)["status"] == "planned"
    assert sum(row["direction"] == "out" for row in store.recent_messages("geoff", limit=20)) == outgoing_before
    engine.confirm_proactive_delivery(decision)
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["direction"] == "proactive"
    assert trace["status"] == "delivered"
    state = store.get_mood_state("geoff")
    assert state.initiative < 61
    assert state.emotional_charge < 15
    assert store.outbox_message(decision.delivery_id)["status"] == "delivered"


@pytest.mark.asyncio
async def test_proactive_tick_attaches_sticker_path(tmp_path: Path) -> None:
    class StickerModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            if any("Return strict JSON" in message["content"] for message in messages):
                return (
                    '{"private_thought":"想发个表情但先不长篇说话",'
                    '"should_send":true,"platform":"qq","message_type":"sticker",'
                    '"message":null,"sticker_category":null,"cooldown_minutes":30}'
                )
            return await super().complete(messages, temperature=temperature)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    stickers = StickerCatalog(
        stickers=[
            Sticker(
                id="miss_you",
                category="miss_you",
                mood="miss_you",
                intent="reaching_out",
                path=Path("assets/stickers/rin-miss-you.png"),
            )
        ]
    )
    engine = CompanionEngine(store, StickerModel(), TEST_PROMPT, stickers)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿")
    )
    decision = await engine.proactive_tick("geoff")

    assert decision.message_type == "sticker"
    assert decision.sticker_path == "assets/stickers/rin-miss-you.png"


@pytest.mark.asyncio
async def test_proactive_tick_can_attach_self_initiated_image(tmp_path: Path) -> None:
    class ImageModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            if any("Return strict JSON" in message["content"] for message in messages):
                return (
                    '{"private_thought":"路过图书馆窗边，突然想拍给你看",'
                    '"should_send":true,"platform":"qq","message_type":"text_image",'
                    '"message":"刚刚窗边光很好，突然想给你看一下。",'
                    '"sticker_category":null,"cooldown_minutes":45}'
                )
            return await super().complete(messages, temperature=temperature)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="close_friend",
            trust=68,
            intimacy=52,
            security=60,
            initiative=72,
            last_platform="qq",
        ),
    )
    image_generator = FakeImageGenerator()
    engine = CompanionEngine(
        store,
        ImageModel(),
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        image_output_dir=tmp_path / "images",
    )

    decision = await engine.proactive_tick("geoff")

    assert decision.message_type == "text_image"
    assert decision.image_path
    assert Path(decision.image_path).exists()
    assert "virtual-life selfie-style" in image_generator.prompts[0]
    assert any(row["kind"] == "generated_image" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_proactive_tick_records_withheld_impulse(tmp_path: Path) -> None:
    class NoSendModel(FakeCompanionModel):
        async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
            if any("Return strict JSON" in message["content"] for message in messages):
                return (
                    '{"private_thought":"有点想问他后来怎么样，但怕打扰",'
                    '"should_send":false,"platform":null,"message_type":"none",'
                    '"message":null,"sticker_category":null,"cooldown_minutes":30}'
                )
            return await super().complete(messages, temperature=temperature)

    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="friend",
            trust=50,
            intimacy=35,
            initiative=30,
            emotional_charge=4,
        ),
    )
    engine = CompanionEngine(store, NoSendModel(), TEST_PROMPT)
    await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            text="所以你觉得呢？",
            sent_at=utc_now() - timedelta(hours=1),
        )
    )
    before_tick = store.get_mood_state("geoff")

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    state = store.get_mood_state("geoff")
    assert state.initiative > before_tick.initiative
    assert state.emotional_charge > before_tick.emotional_charge
    tasks = store.recent_social_tasks("geoff", limit=10)
    withheld = [task for task in tasks if task["kind"] == "withheld_impulse"]
    assert withheld and withheld[-1]["status"] == "pending"
    assert not any(row["kind"] == "withheld_proactive_impulse" for row in store.memories("geoff"))

    store.defer_social_task(int(withheld[-1]["id"]), due_at=utc_now() - timedelta(minutes=1))
    await engine.proactive_tick("geoff")

    tasks = [task for task in store.recent_social_tasks("geoff", limit=10) if task["kind"] == "withheld_impulse"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "resolved"


@pytest.mark.asyncio
async def test_handle_message_attaches_ordinary_reply_sticker(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    stickers = StickerCatalog(
        stickers=[
            Sticker(
                id="comfort",
                category="comfort",
                mood="calm",
                intent="comfort",
                path=Path("assets/stickers/rin-comfort.png"),
            )
        ]
    )
    engine = CompanionEngine(store, FakeCompanionModel(), TEST_PROMPT, stickers)

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天好累，有点难受")
    )

    assert reply.sticker_path == "assets/stickers/rin-comfort.png"


class FakeImageGenerator:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(
        self,
        prompt: str,
        *,
        output_path: Path,
        size: str = "1024x1024",
    ) -> GeneratedImage:
        self.prompts.append(prompt)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-png")
        return GeneratedImage(output_path, prompt)


@pytest.mark.asyncio
async def test_handle_message_generates_requested_image(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="close_friend",
            trust=70,
            intimacy=55,
            security=62,
        ),
    )
    image_generator = FakeImageGenerator()
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        image_output_dir=tmp_path / "images",
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张水彩风格自拍看看")
    )

    assert reply.image_path
    assert Path(reply.image_path).exists()
    assert reply.sticker_path is None
    assert "Character identity anchor" in image_generator.prompts[0]
    assert any(row["kind"] == "generated_image" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_handle_message_can_defer_early_selfie_request(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    image_generator = FakeImageGenerator()
    model = FakeCompanionModel()
    engine = CompanionEngine(
        store,
        model,
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        image_output_dir=tmp_path / "images",
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张自拍看看")
    )

    assert reply.image_path is None
    assert image_generator.prompts == []
    prompt_text = "\n".join(message["content"] for message in model.calls[-1])
    assert "不要立刻发自拍" in prompt_text
    assert any(row["kind"] == "selfie_deferred" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_auto_image_generation_respects_budget_gate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(
        store,
        initial_state=MoodState(
            relationship_stage="close_friend",
            trust=70,
            intimacy=55,
            security=62,
        ),
    )
    image_generator = FakeImageGenerator()
    budget = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=0.1,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        TEST_PROMPT,
        character_profile=load_character("configs/character.yaml"),
        image_generator=image_generator,
        budget_gate=budget,
        image_output_dir=tmp_path / "images",
    )

    reply = await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="给我发一张自拍看看")
    )

    assert reply.image_path is None
    assert image_generator.prompts == []
    assert any(row["kind"] == "image_request_blocked" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_proactive_tick_respects_budget_gate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    store.save_incoming(
        "geoff",
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我先忙一会儿"),
    )
    store.record_usage("vision", 1.0)
    budget = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=1.0,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, budget_gate=budget)

    decision = await engine.proactive_tick("geoff")

    assert decision.should_send is False
    assert "预算阀门" in decision.private_thought
    assert model.calls == []


@pytest.mark.asyncio
async def test_afterthought_respects_budget_gate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store, initial_state=MoodState(mood="miss_you"))
    store.save_incoming(
        "geoff",
        IncomingMessage(platform="qq", platform_user_id="geoff", text="我刚才心里有点闷"),
    )
    store.save_outgoing("geoff", "qq", "我在听。")
    store.record_usage("vision", 1.0)
    budget = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=1.0,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, budget_gate=budget)

    text = await engine.generate_afterthought("geoff", utc_now())

    assert text is None
    assert model.calls == []
    assert any(row["kind"] == "afterthought_blocked" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_memory_maintenance_respects_budget_gate(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    for index in range(22):
        store.save_incoming(
            "geoff",
            IncomingMessage(platform="qq", platform_user_id="geoff", text=f"消息{index}"),
        )
    for index in range(5):
        store.upsert_memory("geoff", kind="life_fact", content=f"用户事实{index}", source="test", confidence=0.7)
    store.record_usage("vision", 1.0)
    budget = BudgetGate(
        store,
        monthly_budget_cny=80,
        daily_budget_cny=3,
        soft_daily_budget_cny=1.0,
        monthly_image_limit=20,
        monthly_vision_limit=120,
        monthly_audio_limit=60,
    )
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT, budget_gate=budget)

    await engine._maybe_consolidate("geoff", MoodState())

    assert model.calls == []
    assert any(row["kind"] == "memory_maintenance_blocked" for row in store.memories("geoff"))


@pytest.mark.asyncio
async def test_handle_message_records_tool_request_without_executing(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    model = FakeCompanionModel()
    engine = CompanionEngine(store, model, TEST_PROMPT)

    await engine.handle_message(
        IncomingMessage(platform="qq", platform_user_id="geoff", text="帮我打开浏览器看一下这个网页")
    )

    proposals = store.recent_tool_proposals("geoff")
    assert proposals
    assert proposals[-1]["kind"] == "computer_assist"
    sent_prompt = "\n".join(message["content"] for message in model.calls[-1])
    assert "必须先请求用户明确确认" in sent_prompt
