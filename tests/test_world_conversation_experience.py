from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.conversation_cadence import ConversationCadence, FrozenTurnContext
from companion_daemon.engine import (
    CompanionEngine,
    _compact_world_context_layers,
    seed_user,
)
from companion_daemon.models import IncomingMessage
from companion_daemon.sanitize import sanitize_world_chat_text
from companion_daemon.world import WorldError, WorldKernel
from companion_daemon.world_behavior import WorldBehaviorPolicy
from companion_daemon.world_conversation import (
    affect_reply_violation,
    build_safe_failure_candidate,
    classify_world_query,
    human_reply_contract_violation,
    only_recites_irrelevant_sources,
    repeats_recent_companion_reply,
)


TEST_PROMPT = "你是沈知栀。"


def test_first_person_statement_flag_survives_unknown_and_conversation_targets() -> None:
    current_statement = classify_world_query("我胃有点不舒服，但还是喝了冰美式。")
    continuity_statement = classify_world_query("之前那次，我确实有点慌，现在缓过来了。")

    assert current_statement.target == "unknown"
    assert current_statement.is_first_person_statement is True
    assert continuity_statement.target == "conversation"
    assert continuity_statement.is_first_person_statement is True


def _world_engine(tmp_path: Path, model: object) -> tuple[WorldKernel, str, CompanionEngine]:
    store = CompanionStore(tmp_path / "world-conversation.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        model,  # type: ignore[arg-type]
        TEST_PROMPT,
        world_kernel=world,
        world_id=world_id,
    )
    return world, world_id, engine


def _quality_signals(world: WorldKernel, world_id: str, reply) -> list[str]:
    action = world.snapshot(world_id)["actions"][str(reply.world_action_id)]
    trace = action.get("trace", {})
    return list(trace.get("quality_signals", [])) if isinstance(trace, dict) else []


def test_hot_prompt_context_keeps_citable_content_without_projection_metadata() -> None:
    layers = {
        "retrieved_experiences": {
            "max_chars": 2_400,
            "max_items": 8,
            "entries": [
                {
                    "source_id": "message:known",
                    "source_type": "conversation_message",
                    "content": "我这两天都在赶项目。",
                    "source": "qq:incoming",
                    "subject": "user:geoff",
                    "logical_at": "2032-01-01T10:00:00+00:00",
                    "purpose": "continuity",
                    "selection": "pinned",
                }
            ],
        }
    }

    compact = _compact_world_context_layers(layers, cadence="hot")

    entry = compact["retrieved_experiences"]["entries"][0]
    assert entry == {
        "source_id": "message:known",
        "source_type": "conversation_message",
        "content": "我这两天都在赶项目。",
    }
    assert compact["retrieved_experiences"]["max_chars"] == 800
    assert _compact_world_context_layers(layers, cadence="cold") is layers


@pytest.mark.asyncio
async def test_hot_minimal_ack_fallback_never_turns_into_old_source_recall(
    tmp_path: Path,
) -> None:
    class Model:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return (
                    '{"reply_text":"毛概确实难背。","mentioned_event_ids":[],'
                    '"proposed_action_ids":[],"claims":[]}'
                )
            return (
                '{"reply_text":"我在西湖边喝咖啡。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    _, _, engine = _world_engine(tmp_path, Model())
    await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="minimal-source-first",
            text="毛概真的好难背。",
        )
    )
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="minimal-source-ack",
            text="嗯。",
        )
    )

    assert reply is not None
    assert "西湖" not in reply.text
    assert "我记得你之前" not in reply.text


@pytest.mark.asyncio
async def test_hot_story_fallback_never_replaces_current_event_with_old_quote(
    tmp_path: Path,
) -> None:
    class Model:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return (
                    '{"reply_text":"雨天找不到伞确实够呛。","mentioned_event_ids":[],'
                    '"proposed_action_ids":[],"claims":[]}'
                )
            return (
                '{"reply_text":"我在西湖边喝咖啡。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    _, _, engine = _world_engine(tmp_path, Model())
    await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="story-source-first",
            text="早上雨特别大，我的伞还找不到。",
        )
    )
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="story-source-current",
            text="结果赶到教室发现老师也迟到了。",
        )
    )

    assert reply is not None
    assert "西湖" not in reply.text
    assert "我记得你之前" not in reply.text
    assert (
        "接着说就好" in reply.text
        or "这段我听着呢" in reply.text
        or "顺着这件事慢慢说" in reply.text
    )


@pytest.mark.asyncio
async def test_world_reply_uses_model_selected_expression_beats_as_one_action(
    tmp_path: Path,
) -> None:
    class BeatModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"先骂两句。再慢慢说。",'
                '"expression_beats":[{"text":"先骂两句。","delay_ms":0},'
                '{"text":"再慢慢说。","delay_ms":1200}],'
                '"display_strategy":"陪伴后追问",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, BeatModel())
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="model-beats",
            text="这个需求真烦，我想先骂两句。",
        ),
        defer_delivery=True,
    )

    assert reply is not None
    assert reply.text_parts == ["先骂两句。", "再慢慢说。"]
    assert reply.part_delays_ms == [0, 1200]
    action = world.snapshot(world_id)["actions"][str(reply.world_action_id)]
    assert [item["delay_before_ms"] for item in action["segment_state"]["segments"]] == [
        0,
        1200,
    ]
    assert action["trace"]["display_strategy"] == "陪伴后追问"


@pytest.mark.asyncio
async def test_world_reply_survives_turn_frame_advisory_failure_and_keeps_delivery_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ReplyModel:
        async def complete(self, messages, *, temperature: float) -> str:
            assert '"advisory_status":"unavailable"' in str(messages)
            return (
                '{"reply_text":"我在，慢慢说。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, ReplyModel())

    def broken_turn_frame(**_kwargs: object) -> object:
        raise ValueError("injected advisory projection failure")

    monkeypatch.setattr(engine.turn_frame_compiler, "compile", broken_turn_frame)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="turn-frame-fallback",
            text="我今天有点累。",
        ),
        defer_delivery=True,
    )

    assert reply is not None
    assert reply.delivery_id is not None
    assert reply.world_action_id is not None
    planned = world.snapshot(world_id)["actions"][reply.world_action_id]
    assert planned["status"] == "scheduled"

    engine.confirm_reply_delivery(reply)

    assert world.snapshot(world_id)["actions"][reply.world_action_id]["status"] == "delivered"


@pytest.mark.asyncio
async def test_world_reply_survives_projection_failure_through_delivery_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("projection fallback must not call the model")

    world, world_id, engine = _world_engine(tmp_path, UnusedModel())

    def broken_projection(**_kwargs: object) -> object:
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(world, "turn_projection", broken_projection)
    reply = await engine.handle_message(
        IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="projection-fallback",
            text="我今天有点累。",
        ),
        defer_delivery=True,
    )

    assert reply is not None
    assert reply.world_action_id is not None
    assert "只按你刚才讲的" in reply.text
    assert world.snapshot(world_id)["actions"][reply.world_action_id]["status"] == "scheduled"

    engine.confirm_reply_delivery(reply)

    action = world.snapshot(world_id)["actions"][reply.world_action_id]
    assert action["status"] == "delivered"
    assert action["trace"]["outbound_trigger"] == "adapter_failure_fallback"


def test_production_world_seed_materializes_the_activity_active_at_epoch_start(
    tmp_path: Path,
) -> None:
    world = WorldKernel(CompanionStore(tmp_path / "materialized-start.sqlite"))
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id

    active = [
        activity
        for activity in world.snapshot(world_id)["agenda"].values()
        if activity["status"] == "active"
    ]

    assert len(active) == 1
    assert active[0]["activity_id"] == "2026-07-11:morning_study"
    assert active[0]["title"] == "图书馆看书"
    assert world.conversation_context(world_id, user_id="user:geoff")["current_scene"][
        "activity"
    ] == "图书馆看书"
    scene = world.conversation_context(
        world_id, user_id="user:geoff"
    )["current_scene_source"]
    with pytest.raises(WorldError, match="not supported"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "在图书馆看会儿书，顺便理一下这学期的课程笔记。",
                "mentioned_event_ids": [scene["source_id"]],
                "proposed_action_ids": [],
                "claims": [{
                    "source_id": scene["source_id"], "text": scene["content"],
                    "assertion": "在图书馆看会儿书，顺便理一下这学期的课程笔记。",
                }],
            },
        )


@pytest.mark.parametrize(
    "unsupported_reply",
    [
        "这会儿刚醒，还在床上赖着看手机呢。",
        "刚刚还在脑子里盘那个读书会的书单。",
        "我以前也有过这种担心。",
        "我这儿图书馆空调有点凉，待久了反而清醒。",
        "我书包里常备茶包，比咖啡温和一点。",
        "我在图书馆看书，桌上正好有杯热美式，算远程分你半杯。",
        "刚整理完今天的课程笔记，在宿舍歇着呢。",
        "本地文件没了。",
        "我睡不着的时候会翻两页散文。",
        "我跟着紧了一下又松了一口气，确实在意了。",
        "有一点，但没到不舒服的程度。我反而觉得这样聊天比较踏实。",
        (
            "啊，难怪你一大早就起来了。虚拟伴侣这个方向最近好像挺多人做的，"
            "那你先忙，我也就看看书，换个位置靠窗一点。"
        ),
    ],
)
def test_world_reply_rejects_unsourced_life_or_private_history(
    tmp_path: Path,
    unsupported_reply: str,
) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())

    with pytest.raises(WorldError):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": unsupported_reply,
                "mentioned_event_ids": [],
                "proposed_action_ids": [],
                "claims": [],
            },
        )


def test_world_reply_accepts_an_opinion_that_contains_no_life_claim(tmp_path: Path) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())
    accepted = world.validate_reply_candidate(
        world_id,
        {
            "reply_text": (
                "我直觉是，人说话本来就不是被精准设计的。"
                "越想让对话变得完美，反而越容易丢掉真的人在说话的感觉。"
            ),
            "mentioned_event_ids": [],
            "proposed_action_ids": [],
            "claims": [],
        },
    )
    assert "我直觉是" in accepted["reply_text"]

    another = world.validate_reply_candidate(
        world_id,
        {
            "reply_text": (
                "唔，我不太懂技术，但感觉像是你把太多规则叠上去了，"
                "反而把自然的空隙都填满了。也许聊天需要留点白吧。"
            ),
            "mentioned_event_ids": [],
            "proposed_action_ids": [],
            "claims": [],
        },
    )
    assert "聊天需要留点白" in another["reply_text"]


def test_adapter_failure_reply_is_staged_and_settled_through_world_delivery_ledger(
    tmp_path: Path,
) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, engine = _world_engine(tmp_path, UnusedModel())
    incoming = IncomingMessage(
        platform="simulator",
        platform_user_id="geoff",
        message_id="adapter-fallback-ledger",
        text="算了，你继续看书吧",
    )

    reply = engine.prepare_adapter_failure_reply(
        incoming,
        "我听出来了，你对我刚才的回应有点失望。是我没接好。",
        failure_reason="grounding audit unavailable",
    )
    segment_id = engine.begin_reply_part_delivery(reply, position=0)
    assert segment_id is not None
    engine.confirm_reply_part_delivery(
        reply, segment_id=segment_id, external_receipt="platform:fallback:1"
    )
    engine.confirm_reply_delivery(reply)

    action = world.snapshot(world_id)["actions"][reply.world_action_id]
    assert action["status"] == "delivered"
    assert action["trace"]["outbound_trigger"] == "adapter_failure_fallback"


def test_world_claim_can_separate_exact_evidence_from_natural_assertion(tmp_path: Path) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())
    world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "natural-user-memory",
            "user_id": "user:geoff",
            "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
            "sent_at": "2026-07-11T09:05:00+08:00",
        },
        expected_revision=world.revision(world_id),
    )

    accepted = world.validate_reply_candidate(
        world_id,
        {
            "reply_text": "你之前说在赶一个虚拟伴侣项目，昨晚没怎么睡。",
            "mentioned_event_ids": ["message:natural-user-memory"],
            "proposed_action_ids": [],
            "claims": [
                {
                    "source_id": "message:natural-user-memory",
                    "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
                    "assertion": "你之前说在赶一个虚拟伴侣项目，昨晚没怎么睡。",
                }
            ],
        },
    )

    assert accepted["claims"][0]["assertion"] == "你之前说在赶一个虚拟伴侣项目，昨晚没怎么睡。"

    aliased = world.validate_reply_candidate(
        world_id,
        {
            "reply_text": "你提到在赶虚拟伴侣项目。",
            "mentioned_event_ids": ["user-conversation:temporary-model-id"],
            "proposed_action_ids": [],
            "claims": [
                {
                    "source_id": "user-conversation:temporary-model-id",
                    "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
                    "assertion": "你提到在赶虚拟伴侣项目。",
                }
            ],
        },
    )

    assert aliased["mentioned_event_ids"] == ["message:natural-user-memory"]
    assert aliased["claims"][0]["source_id"] == "message:natural-user-memory"

    with pytest.raises(WorldError, match="first-person user evidence"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
                "mentioned_event_ids": ["message:natural-user-memory"],
                "proposed_action_ids": [],
                "claims": [{
                    "source_id": "message:natural-user-memory",
                    "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
                }],
            },
            user_id="user:geoff",
        )
    world.submit(
        {
            "type": "confirm_fact", "world_id": world_id,
            "fact_id": "user-conversation:duplicate", "subject": "user:geoff",
            "value": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
            "source": "user_message:natural-user-memory", "scope": "conversation",
        },
        expected_revision=world.revision(world_id),
    )
    grounded = world.grounded_reply_from_mentions(
        world_id,
        {
            "mentioned_event_ids": [
                "message:natural-user-memory", "user-conversation:duplicate"
            ],
            "claims": [],
        },
    )
    assert grounded is not None
    assert grounded["_user_sourced"] is True
    assert grounded["reply_text"] == "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。"

    quoted = world.validate_reply_candidate(
        world_id,
        {
            "reply_text": "我记得你说过：“我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。”",
            "mentioned_event_ids": ["message:natural-user-memory"],
            "proposed_action_ids": [],
            "claims": [{
                "source_id": "message:natural-user-memory",
                "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
            }],
        },
        user_id="user:geoff",
    )
    assert "我记得你说过" in quoted["reply_text"]

    world.submit(
        {
            "type": "confirm_fact", "world_id": world_id,
            "fact_id": "user-conversation:subject-check", "subject": "user:geoff",
            "value": "我胃有点不舒服，但还是喝了冰美式。",
            "source": "user_message:health", "scope": "conversation",
        },
        expected_revision=world.revision(world_id),
    )
    with pytest.raises(WorldError, match="first-person user evidence"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "我胃有点不舒服，但还是喝了冰美式。",
                "mentioned_event_ids": ["user-conversation:subject-check"],
                "proposed_action_ids": [],
                "claims": [{
                    "source_id": "user-conversation:subject-check",
                    "text": "我胃有点不舒服，但还是喝了冰美式。",
                }],
            },
            user_id="user:geoff",
        )


def test_world_claim_rejects_a_negated_or_opposite_paraphrase(tmp_path: Path) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())
    world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "opposite-claim-source",
            "user_id": "user:geoff",
            "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
            "sent_at": "2026-07-11T09:05:00+08:00",
        },
        expected_revision=world.revision(world_id),
    )

    with pytest.raises(WorldError, match="not supported"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "你今天没有赶虚拟伴侣项目，昨晚睡得很好。",
                "mentioned_event_ids": ["message:opposite-claim-source"],
                "proposed_action_ids": [],
                "claims": [{
                    "source_id": "message:opposite-claim-source",
                    "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
                    "assertion": "你今天没有赶虚拟伴侣项目，昨晚睡得很好。",
                }],
            },
            user_id="user:geoff",
        )


def test_world_claim_rejects_a_changed_time_anchor(tmp_path: Path) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())
    world.submit(
        {
            "type": "observe_user_message", "world_id": world_id,
            "message_id": "time-anchor-source", "user_id": "user:geoff",
            "text": "我今天要赶一个项目。", "sent_at": "2026-07-11T09:05:00+08:00",
        },
        expected_revision=world.revision(world_id),
    )
    with pytest.raises(WorldError, match="not supported"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "你昨天要赶一个项目。",
                "mentioned_event_ids": ["message:time-anchor-source"],
                "proposed_action_ids": [],
                "claims": [{
                    "source_id": "message:time-anchor-source",
                    "text": "我今天要赶一个项目。",
                    "assertion": "你昨天要赶一个项目。",
                }],
            },
            user_id="user:geoff",
        )
def test_reply_sources_are_scoped_to_the_current_user(tmp_path: Path) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())
    world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "other-user-secret",
            "user_id": "user:another",
            "text": "我昨晚在医院。",
            "sent_at": "2026-07-11T09:05:00+08:00",
        },
        expected_revision=world.revision(world_id),
    )

    with pytest.raises(WorldError, match="uncommitted"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "你昨晚在医院。",
                "mentioned_event_ids": ["message:other-user-secret"],
                "proposed_action_ids": [],
                "claims": [{
                    "source_id": "message:other-user-secret",
                    "text": "我昨晚在医院。",
                    "assertion": "你昨晚在医院。",
                }],
            },
            user_id="user:geoff",
        )
    assert world.grounded_reply_from_mentions(
        world_id,
        {"mentioned_event_ids": ["message:other-user-secret"]},
        user_id="user:geoff",
    ) is None


def test_expired_observed_time_model_lease_is_recovered_to_failure(tmp_path: Path) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())
    logical_now = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    if "user:geoff" not in world.snapshot(world_id)["entities"]:
        world.submit(
            {"type": "register_user", "world_id": world_id, "user_id": "user:geoff", "name": "geoff"},
            expected_revision=world.revision(world_id),
        )
    assert world.claim_message_turn(world_id, "crashed-message")
    world.submit(
        {
            "type": "appraise_turn", "world_id": world_id,
            "appraisal": "ordinary_message", "intent_id": "turn:crashed",
            "message_id": "crashed-message",
            "user_id": "user:geoff",
        },
        expected_revision=world.revision(world_id),
    )
    world.submit(
        {
            "type": "schedule_action", "world_id": world_id,
            "action_id": "model_call:crashed", "kind": "model_call",
            "expires_at": (logical_now + timedelta(hours=1)).isoformat(),
            "payload": {"purpose": "reply", "causation": "turn:crashed"},
        },
        expected_revision=world.revision(world_id),
    )
    observed_now = datetime.now().astimezone()
    world.submit(
        {
            "type": "claim_external_action", "world_id": world_id,
            "action_id": "model_call:crashed",
            "lease_expires_observed_at": (observed_now - timedelta(seconds=1)).isoformat(),
        },
        expected_revision=world.revision(world_id),
    )

    decision = world.recover_expired_external_leases(
        world_id, observed_now=observed_now,
        expected_revision=world.revision(world_id),
    )

    assert [event.event_type for event in decision.events] == [
        "ActionSettled", "CostReservationSettled", "IntentFailed", "TurnProcessingSettled"
    ]
    action = world.snapshot(world_id)["actions"]["model_call:crashed"]
    assert action["status"] == "failed"
    assert action["result"]["reason"] == "external_lease_expired"
    assert world.snapshot(world_id)["intents"]["turn:crashed"]["status"] == "failed"
    assert world.snapshot(world_id)["turns"]["crashed-message"]["status"] == "failed"



def test_grounded_paraphrase_does_not_invent_an_npc_gender(tmp_path: Path) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(hours=3, minutes=30),
        expected_revision=world.revision(world_id),
    )

    with pytest.raises(WorldError, match="not supported"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "上午和他核对了读书会的书单。",
                "mentioned_event_ids": ["outcome:2026-07-11:morning_study"],
                "proposed_action_ids": [],
                "claims": [
                    {
                        "source_id": "outcome:2026-07-11:morning_study",
                        "text": "在图书馆和范予安核对了读书会的书单。",
                        "assertion": "上午和他核对了读书会的书单。",
                    }
                ],
            },
        )


def test_claim_source_is_normalized_into_missing_mentioned_ids(tmp_path: Path) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(hours=8, minutes=20),
        expected_revision=world.revision(world_id),
    )

    accepted = world.validate_reply_candidate(
        world_id,
        {
            "reply_text": "下午把课程笔记整理完了。",
            "mentioned_event_ids": [],
            "proposed_action_ids": [],
            "claims": [
                {
                    "source_id": "outcome:2026-07-11:afternoon_class",
                    "text": "整理完了今天的课程笔记。",
                    "assertion": "下午把课程笔记整理完了",
                }
            ],
        },
    )

    assert accepted["mentioned_event_ids"] == ["outcome:2026-07-11:afternoon_class"]

    with pytest.raises(WorldError, match="not supported"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "下午课上完就在宿舍把今天的笔记顺了一遍，不然堆到周末会忘。",
                "mentioned_event_ids": ["outcome:2026-07-11:afternoon_class"],
                "proposed_action_ids": [],
                "claims": [{
                    "source_id": "outcome:2026-07-11:afternoon_class",
                    "text": "整理完了今天的课程笔记。",
                    "assertion": "下午课上完就在宿舍把今天的笔记顺了一遍，不然堆到周末会忘。",
                }],
            },
        )


def test_old_session_fact_remains_retrievable_after_the_short_prompt_window(
    tmp_path: Path,
) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, engine = _world_engine(tmp_path, UnusedModel())
    for index in range(80):
        text = (
            "我在赶虚拟伴侣项目，昨晚都没怎么睡。"
            if index == 0
            else f"这是后续闲聊第 {index} 条。"
        )
        engine._record_world_input(
            IncomingMessage(
                platform="simulator",
                platform_user_id="geoff",
                message_id=f"session-{index}",
                text=text,
                sent_at=datetime(2026, 7, 11, 9, 0).astimezone()
                + timedelta(minutes=index),
            ),
            "geoff",
        )

    sources = world.conversation_sources_for_query(
        world_id,
        user_id="user:geoff",
        text="你还记得我昨天为什么没睡好吗？",
        current_message_id=None,
        limit=4,
    )

    assert len(world.snapshot(world_id)["recent_messages"]) == 64
    assert sources[0]["source_id"].startswith("user-conversation:")
    assert "虚拟伴侣项目" in sources[0]["content"]


def test_world_sanitizer_does_not_change_a_quoted_user_fact_into_a_question() -> None:
    text = "我记得你之前提过：“我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。”"

    assert sanitize_world_chat_text(text) == text


def test_near_duplicate_of_either_recent_companion_reply_is_rejected() -> None:
    history = [
        {"direction": "out", "text": "我会先修对话连续性。"},
        {"direction": "in", "text": "那事实呢？"},
        {"direction": "out", "text": "事实门禁也要保留。"},
    ]

    assert repeats_recent_companion_reply(
        {"reply_text": "我会先修对话连续性！"}, history
    )
    assert not repeats_recent_companion_reply(
        {"reply_text": "我会先检查事实来源。"}, history
    )


def test_grounded_but_irrelevant_fact_dump_is_rejected() -> None:
    candidate = {
        "reply_text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。我胃有点不舒服，但还是喝了冰美式。",
        "claims": [
            {"text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。"},
            {"text": "我胃有点不舒服，但还是喝了冰美式。"},
        ],
    }

    assert only_recites_irrelevant_sources(
        "其实我有点担心，做这么久最后还是没有人味。", candidate
    )
    assert not only_recites_irrelevant_sources(
        "你还记得我昨晚为什么没睡吗？", candidate
    )
    assert only_recites_irrelevant_sources(
        "你别只劝我休息，先陪我吐槽一下这个需求。", candidate
    )


@pytest.mark.parametrize(
    ("user_text", "speech_act", "expected_marker"),
    [
        ("我胃有点不舒服，但还是喝了冰美式。", "health_disclosure", "不舒服"),
        ("我今天要赶一个项目，昨晚没怎么睡。", "sleep_disclosure", "很累"),
        ("我最烦那种前言不搭后语，还装得很懂我的回复。", "shared_reaction", "不满"),
        ("其实我有点担心，做这么久最后还是没有人味。", "vulnerable_disclosure", "担心"),
        ("我准备睡了，但脑子还停不下来。", "sleep_disclosure", "没停下来"),
        ("早，我昨天为什么没睡好，你还记得吗？", "source_recall", "记录"),
        ("急，我项目数据好像丢了，你先回我。", "urgent_data", "别继续覆盖数据"),
        ("你不用讲大道理，跟我说一句晚安就好。", "brief_goodnight", "晚安。"),
    ],
)
def test_safe_failure_keeps_the_current_user_speech_act(
    user_text: str, speech_act: str, expected_marker: str
) -> None:
    candidate = build_safe_failure_candidate(user_text, None, speech_act=speech_act)

    assert expected_marker in str(candidate["reply_text"])
    assert candidate["mentioned_event_ids"] == []
    assert candidate["proposed_action_ids"] == []
    assert candidate["claims"] == []


@pytest.mark.asyncio
async def test_world_reply_never_echoes_the_current_user_message_as_its_answer(
    tmp_path: Path,
) -> None:
    class EchoModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡？",'
                '"mentioned_event_ids":["message:no-echo"],"proposed_action_ids":[],'
                '"claims":[{"source_id":"message:no-echo",'
                '"text":"我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡"}]}'
            )

    _, _, engine = _world_engine(tmp_path, EchoModel())
    user_text = "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡"

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="no-echo",
            text=user_text,
        )
    )

    assert reply is not None
    assert reply.text.rstrip("？?") != user_text
    assert reply.text != f"{user_text}？"


@pytest.mark.asyncio
async def test_prediction_may_use_user_context_without_quoting_it_as_a_fact(
    tmp_path: Path,
) -> None:
    class PredictionModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return (
                    '{"reply_text":"胃不好还惦记冰美式，要不换杯热的？",'
                    '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
                )
            return (
                '{"reply_text":"我猜你会买，然后一边喝一边跟胃道歉。",'
                '"mentioned_event_ids":["message:coffee-context"],'
                '"proposed_action_ids":[],"claims":[{'
                '"source_id":"message:coffee-context",'
                '"text":"我困得眼睛疼，想买杯冰美式，但我胃不太好。"}]}'
            )

    model = PredictionModel()
    _, _, engine = _world_engine(tmp_path, model)
    await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="coffee-context",
            text="我困得眼睛疼，想买杯冰美式，但我胃不太好。",
        )
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="coffee-prediction",
            text="你猜我最后会不会买？",
        )
    )

    assert reply is not None
    assert reply.text == "我猜你会买，然后一边喝一边跟胃道歉。"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_deterministic_guard_rejects_an_unclaimed_virtual_world_detail(
    tmp_path: Path,
) -> None:
    class ReplyModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return (
                    '{"reply_text":"图书馆门口新开了一家花店。",'
                    '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
                )
            return (
                '{"reply_text":"听起来挺累的，今天别硬撑。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    reply_model = ReplyModel()
    store = CompanionStore(tmp_path / "audited-world.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        reply_model,
        TEST_PROMPT,
        world_kernel=world,
        world_id=world_id,
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="audited-detail",
            text="我昨晚没睡好。",
        )
    )

    assert reply is not None
    assert reply.text == "听起来挺累的，今天别硬撑。"
    assert reply_model.calls == 2
    # The deterministic Guard rejects the local-world claim before delivery.
    assert sum(
        action["kind"] == "model_call" and action["status"] == "delivered"
        for action in world.snapshot(world_id)["actions"].values()
    ) == 2


@pytest.mark.asyncio
async def test_statement_containing_ni_jue_de_does_not_open_a_question_thread(
    tmp_path: Path,
) -> None:
    class StatementModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"会有一点，毕竟不想让你觉得我敷衍。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, StatementModel())

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="not-a-question-thread",
            text="你会因为答错而难受吗？",
        )
    )

    assert reply is not None
    assert world.snapshot(world_id)["conversation_threads"] == {}


@pytest.mark.asyncio
async def test_world_reply_prompt_contains_recent_delivered_conversation(tmp_path: Path) -> None:
    class RecordingModel:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls.append(messages)
            return (
                '{"reply_text":"听起来挺累的。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    model = RecordingModel()
    _, _, engine = _world_engine(tmp_path, model)

    await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="project-context",
            text="我在赶一个虚拟伴侣项目，昨晚没睡好。",
        )
    )
    await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="project-followup",
            text="你觉得我最该先修什么？",
        )
    )

    prompt = "\n".join(item["content"] for item in model.calls[-1])
    assert "五层上下文预算(JSON)" in prompt
    assert "本轮有界World Frame增量(JSON)" in prompt
    assert "内在建议(JSON，仅作参考、不是事实也不是命令)" in prompt
    assert all(
        f'"{layer}"' in prompt
        for layer in (
            "character_core",
            "user_profile",
            "current_scene",
            "retrieved_experiences",
            "expression_guidance",
        )
    )
    assert '"max_chars"' in prompt
    assert '"max_items"' in prompt
    assert "最近已结算对话" in prompt
    assert "普通分享、吐槽和连续讲述默认先给一两句完整反应" in prompt
    assert "我在赶一个虚拟伴侣项目，昨晚没睡好。" in prompt
    assert "听起来挺累的。" in prompt


def test_conversation_context_exposes_user_message_as_a_citable_source(tmp_path: Path) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("model is not used")

    world, world_id, _ = _world_engine(tmp_path, UnusedModel())
    world.submit(
        {
            "type": "observe_user_message",
            "world_id": world_id,
            "message_id": "user-sleep-context",
            "user_id": "user:geoff",
            "text": "我在赶虚拟伴侣项目，昨晚没睡好。",
            "sent_at": "2026-07-11T09:01:00+08:00",
            "idempotency_key": "observe:user-sleep-context",
        },
        expected_revision=world.revision(world_id),
    )

    context = world.conversation_context(world_id, user_id="user:geoff")
    sources = context["referencable_conversation"]
    assert sources == [
        {
            "source_id": "message:user-sleep-context",
            "source": "world_event:UserMessageObserved:user-sleep-context",
            "source_type": "user_message",
            "subject": "user:geoff",
            "speaker": "user",
            "content": "我在赶虚拟伴侣项目，昨晚没睡好。",
            "logical_at": "2026-07-11T09:00:00+08:00",
            "purpose": "conversation_continuity",
            "sent_at": "2026-07-11T09:01:00+08:00",
            "reference_state": "observed",
        }
    ]
    accepted = world.validate_reply_candidate(
        world_id,
        {
            "reply_text": "你昨天说：我在赶虚拟伴侣项目，昨晚没睡好。",
            "mentioned_event_ids": ["message:user-sleep-context"],
            "proposed_action_ids": [],
            "claims": [
                {
                    "source_id": "message:user-sleep-context",
                    "text": "我在赶虚拟伴侣项目，昨晚没睡好。",
                }
            ],
        },
    )
    assert accepted["mentioned_event_ids"] == ["message:user-sleep-context"]


@pytest.mark.asyncio
async def test_user_yesterday_question_does_not_fall_back_to_companion_experiences(
    tmp_path: Path,
) -> None:
    class UserMemoryModel:
        async def complete(self, messages, *, temperature: float) -> str:
            prompt = "\n".join(item["content"] for item in messages)
            if "你还记得我昨天为什么没睡好吗" in prompt:
                return (
                    '{"reply_text":"你昨天说：我在赶虚拟伴侣项目，昨晚没睡好。",'
                    '"mentioned_event_ids":["message:user-project-yesterday"],'
                    '"proposed_action_ids":[],"claims":[{'
                    '"source_id":"message:user-project-yesterday",'
                    '"text":"我在赶虚拟伴侣项目，昨晚没睡好。"}]}'
                )
            return (
                '{"reply_text":"听起来挺累的。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, UserMemoryModel())
    await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="user-project-yesterday",
            text="我在赶虚拟伴侣项目，昨晚没睡好。",
        )
    )
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(days=1, hours=4),
        expected_revision=world.revision(world_id),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="user-memory-question",
            text="你还记得我昨天为什么没睡好吗？",
        )
    )

    assert reply is not None
    assert "虚拟伴侣项目" in reply.text
    assert "范予安" not in reply.text
    assert "摄影" not in reply.text


@pytest.mark.asyncio
async def test_invalid_user_memory_answer_never_uses_companion_timeline_as_fallback(
    tmp_path: Path,
) -> None:
    class InvalidMemoryModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return (
                    '{"reply_text":"听起来挺累的。","mentioned_event_ids":[],'
                    '"proposed_action_ids":[],"claims":[]}'
                )
            return (
                '{"reply_text":"我昨天去逛街了。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    model = InvalidMemoryModel()
    world, world_id, engine = _world_engine(tmp_path, model)
    await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="invalid-user-memory-source",
            text="我在赶虚拟伴侣项目，昨晚没睡好。",
        )
    )
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(days=1, hours=4),
        expected_revision=world.revision(world_id),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="invalid-user-memory-question",
            text="你还记得我昨天为什么没睡好吗？",
        )
    )

    assert reply is not None
    assert "整理了摄影社" not in reply.text
    assert "林晚" not in reply.text
    assert "范予安" not in reply.text


@pytest.mark.asyncio
async def test_failed_reply_repair_preserves_a_question_instead_of_saying_en_you_say(
    tmp_path: Path,
) -> None:
    class AlwaysInvalidModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"我刚从商场回来。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    _, _, engine = _world_engine(tmp_path, AlwaysInvalidModel())

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="direct-question-fallback",
            text="你觉得我最该先修什么？",
        )
    )

    assert reply is not None
    assert "没有足够依据" in reply.text
    assert reply.text != "嗯，你说。"
    assert reply.text != "你觉得我最该先修什么"


@pytest.mark.asyncio
async def test_failed_detail_answer_admits_the_source_has_no_requested_detail(
    tmp_path: Path,
) -> None:
    class UnsupportedNpcDetailModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"范予安是我室友，核对得很顺利。",'
                '"mentioned_event_ids":["outcome:2026-07-11:morning_study"],'
                '"proposed_action_ids":[],"claims":[{'
                '"source_id":"outcome:2026-07-11:morning_study",'
                '"text":"范予安是我室友"}]}'
            )

    world, world_id, engine = _world_engine(tmp_path, UnsupportedNpcDetailModel())
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(hours=3, minutes=30),
        expected_revision=world.revision(world_id),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="npc-detail-fallback",
            text="范予安是谁？你们核对得顺利吗？",
        )
    )

    assert reply is not None
    assert "在图书馆和范予安核对了读书会的书单。" in reply.text
    assert "没有能确认的记录" in reply.text
    assert "室友" not in reply.text
    assert reply.text != "在图书馆和范予安核对了读书会的书单。"


@pytest.mark.asyncio
async def test_detail_fallback_finds_a_committed_experience_by_registered_npc_name(
    tmp_path: Path,
) -> None:
    class MissingSourceModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"范予安是我室友，核对得很顺利。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, MissingSourceModel())
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(hours=3, minutes=30),
        expected_revision=world.revision(world_id),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="npc-detail-source-retrieval",
            text="范予安是谁？你们核对得顺利吗？",
        )
    )

    assert reply is not None
    assert "在图书馆和范予安核对了读书会的书单。" in reply.text
    assert "没有能确认的记录" in reply.text


@pytest.mark.asyncio
async def test_exact_experience_quote_is_diagnosed_without_a_full_repair(
    tmp_path: Path,
) -> None:
    class ExactQuoteModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"在图书馆和范予安核对了读书会的书单。",'
                '"mentioned_event_ids":["outcome:2026-07-11:morning_study"],'
                '"proposed_action_ids":[],"claims":[{'
                '"source_id":"outcome:2026-07-11:morning_study",'
                '"text":"在图书馆和范予安核对了读书会的书单。"}]}'
            )

    world, world_id, engine = _world_engine(tmp_path, ExactQuoteModel())
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(hours=3, minutes=30),
        expected_revision=world.revision(world_id),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="npc-detail-exact-quote",
            text="范予安是谁？你们核对得顺利吗？",
        )
    )

    assert reply is not None
    assert reply.text == "在图书馆和范予安核对了读书会的书单。"
    assert "repeats_claimed_source" in _quality_signals(world, world_id, reply)


@pytest.mark.asyncio
async def test_non_interruptible_activity_is_an_advisory_not_a_pre_model_veto(
    tmp_path: Path,
) -> None:
    class Model:
        def __init__(self) -> None:
            self.calls = 0
            self.prompts: list[str] = []

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            self.prompts.append("\n".join(str(item["content"]) for item in messages))
            return '{"reply_text":"我看见了。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = Model()
    world, world_id, engine = _world_engine(tmp_path, model)
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(hours=7, minutes=30),
        expected_revision=world.revision(world_id),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="busy-afternoon-message",
            text="顺便问一句，你更喜欢什么样的诗？",
        )
    )

    assert reply is not None
    assert model.calls >= 1
    assert any("通讯节奏建议: 倾向=deferred" in prompt for prompt in model.prompts)
    assert any("不是静默指令" in prompt for prompt in model.prompts)
    snapshot = world.snapshot(world_id)
    assert snapshot["communication"]["attention"] == "seen"
    assert {item["attention"] for item in snapshot["communication"]["candidates"]} == {
        "seen", "deferred", "do_not_disturb"
    }
    assert snapshot["communication"]["candidates"][0]["attention"] == "deferred"
    scheduled = [
        action
        for action in snapshot["actions"].values()
        if action["kind"] == "reply_later" and action["status"] == "scheduled"
    ]
    assert scheduled == []
    active = next(item for item in snapshot["agenda"].values() if item["status"] == "active")
    assert active["attention_demand"] >= 75
    assert active["interruptible"] is False
    attention_events = [
        event for event in world.events(world_id) if event.event_type == "MessageAttentionDecided"
    ]
    assert attention_events[-1].payload["reason"].startswith(
        "model_advisory:active_world_activity_not_interruptible:"
    )


def test_attention_wait_shrinks_during_the_ending_activity_phase() -> None:
    state = {
        "clock": {"logical_at": "2026-07-11T16:55:00+08:00"},
        "needs": {"energy": 70, "security": 50, "boundary": 0},
        "agenda": {
            "class": {
                "status": "active", "starts_at": "2026-07-11T14:00:00+08:00",
                "ends_at": "2026-07-11T17:00:00+08:00",
                "attention_demand": 88, "interruptible": False,
            }
        },
    }

    decision = WorldBehaviorPolicy().communication_decision(state, text="你喜欢什么诗？")

    assert decision.reason.endswith(":ending")
    assert decision.defer_minutes == 5


@pytest.mark.asyncio
async def test_busy_question_fallback_answers_availability(tmp_path: Path) -> None:
    class InvalidThenFallbackModel:
        async def complete(self, messages, *, temperature: float) -> str:
            if "事实审计器" in str(messages[0].get("content") or ""):
                if "这会儿可以说话" in str(messages):
                    return '{"supported":true,"unsupported_spans":[],"reason":"deterministic availability fallback"}'
                return '{"supported":false,"unsupported_spans":["火星"],"reason":"unsupported"}'
            return '{"reply_text":"我在火星上。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    world, world_id, engine = _world_engine(tmp_path, InvalidThenFallbackModel())
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="availability-question",
            text="你现在忙吗，方便说话吗？",
        )
    )

    assert reply is not None
    assert "可以说话" in reply.text or "不太方便说话" in reply.text


@pytest.mark.asyncio
async def test_non_json_reply_and_repair_end_in_a_safe_deliverable_reply(
    tmp_path: Path,
) -> None:
    class NonJsonModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return "抱歉，我没有按 JSON 输出。"

    world, world_id, engine = _world_engine(tmp_path, NonJsonModel())

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="non-json-world-reply", text="我胃还是有点不舒服。",
        )
    )

    assert reply is not None
    assert "不舒服" in reply.text
    assert reply.world_action_id is not None


@pytest.mark.asyncio
async def test_hot_turn_hard_reject_uses_local_fallback_without_repair(
    tmp_path: Path,
) -> None:
    class UnsupportedActionModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return '{"reply_text":"我已经替你点好了。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = UnsupportedActionModel()
    world, world_id, engine = _world_engine(tmp_path, model)
    observed_at = datetime.now().astimezone()
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="hot-hard-reject",
            sent_at=observed_at,
            text="我有点累。",
        ),
        turn_context=FrozenTurnContext(
            turn_id="hot-hard-reject",
            world_id=world_id,
            user_id="user:geoff",
            observed_at=observed_at,
            cadence=ConversationCadence("hot", 1.0, 4, "test_hot_repair"),
        ),
    )

    assert reply is not None
    assert "已经替你点好了" not in reply.text
    assert model.calls == 1
    trace = world.snapshot(world_id)["actions"][reply.world_action_id]["trace"]
    assert "hot_turn_repair_local_fallback" in trace["quality_signals"]


@pytest.mark.asyncio
async def test_plain_chat_reply_skips_repair_when_the_envelope_is_missing(
    tmp_path: Path,
) -> None:
    class PlainReplyModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return "明天考试啊，毛概这种确实很磨人。"

    model = PlainReplyModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="plain-chat-no-repair",
            text="我明天考试，毛概好难背。",
        )
    )

    assert reply is not None
    assert reply.text == "明天考试啊，毛概这种确实很磨人。"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_plain_presence_acknowledgement_skips_repair_without_word_overlap(
    tmp_path: Path,
) -> None:
    class PresenceModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return "我在听。"

    model = PresenceModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="plain-presence-no-overlap",
            text="你在吗？",
        )
    )

    assert reply is not None
    assert reply.text == "我在听。"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_plain_nonfactual_support_skips_repair_without_word_overlap(
    tmp_path: Path,
) -> None:
    class SupportModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return "抱抱，慢慢来。"

    model = SupportModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="plain-support-no-overlap",
            text="我今天压力好大。",
        )
    )

    assert reply is not None
    assert reply.text == "抱抱，慢慢来。"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_plain_chat_world_fact_is_not_delivered_without_provenance(
    tmp_path: Path,
) -> None:
    class FactThenSafeModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return "我在西湖边喝咖啡。"
            return '{"reply_text":"听着你今天挺累的，先别硬撑。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = FactThenSafeModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="plain-chat-world-fact",
            text="我今天有点累。",
        )
    )

    assert reply is not None
    assert "西湖边" not in reply.text
    assert model.calls == 1


@pytest.mark.asyncio
async def test_plain_chat_autobiographical_fact_is_not_delivered_without_provenance(
    tmp_path: Path,
) -> None:
    class BiographyThenSafeModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return "我有个哥哥住在北京。"
            return '{"reply_text":"你明天考试，今晚先别把自己逼太紧。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = BiographyThenSafeModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="plain-chat-biography",
            text="我明天考试。",
        )
    )

    assert reply is not None
    assert "哥哥" not in reply.text
    assert model.calls == 1


@pytest.mark.asyncio
async def test_plain_chat_prefixed_parent_fact_is_not_delivered_without_provenance(
    tmp_path: Path,
) -> None:
    class ParentFactThenSafeModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return "我觉得爸爸住在北京。"
            return '{"reply_text":"你问得很具体，但我没有能确认的记录。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = ParentFactThenSafeModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="plain-chat-parent-biography",
            text="爸爸也在北京吗？",
        )
    )

    assert reply is not None
    assert "爸爸住在北京" not in reply.text
    assert model.calls == 1


@pytest.mark.asyncio
async def test_plain_chat_cannot_answer_third_party_fact_question(
    tmp_path: Path,
) -> None:
    class ThirdPartyThenSafeModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return "我觉得那个新同事很帅。"
            return '{"reply_text":"这类事我没有能确认的记录，不想顺口替你编。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = ThirdPartyThenSafeModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="plain-chat-third-party-fact",
            text="你同事很帅吗？",
        )
    )

    assert reply is not None
    assert "新同事很帅" not in reply.text
    assert model.calls == 1


@pytest.mark.asyncio
async def test_plain_chat_cannot_add_state_to_user_mentioned_third_party(
    tmp_path: Path,
) -> None:
    class ThirdPartyStateThenSafeModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return "这位朋友正在医院输液。"
            return '{"reply_text":"你只提到有位朋友，其他情况我没有记录。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = ThirdPartyStateThenSafeModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="plain-chat-third-party-state",
            text="我有个朋友。",
        )
    )

    assert reply is not None
    assert "医院输液" not in reply.text
    assert model.calls == 1


@pytest.mark.asyncio
async def test_structured_third_party_fact_needs_provenance(
    tmp_path: Path,
) -> None:
    class StructuredFactThenSafeModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return '{"reply_text":"这位朋友正在医院输液。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            return '{"reply_text":"你只提到有位朋友，其他情况我没有记录。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = StructuredFactThenSafeModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="structured-third-party-state",
            text="我有个朋友。",
        )
    )

    assert reply is not None
    assert "医院输液" not in reply.text
    assert model.calls == 2


@pytest.mark.asyncio
async def test_structured_environment_assertion_needs_provenance(
    tmp_path: Path,
) -> None:
    class EnvironmentThenSafeModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return '{"reply_text":"楼上正在装修，吵死了。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            return '{"reply_text":"你说这边很冷，听起来确实不好受。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = EnvironmentThenSafeModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="structured-environment-assertion",
            text="我这边天气很冷。",
        )
    )

    assert reply is not None
    assert "楼上正在装修" not in reply.text
    assert model.calls == 2


@pytest.mark.asyncio
async def test_plain_chat_cannot_expand_user_environment_statement(
    tmp_path: Path,
) -> None:
    class EnvironmentThenSafeModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return "气温很低，路上已经结冰了。"
            return '{"reply_text":"听起来你这边确实冷，出门多穿一点。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    model = EnvironmentThenSafeModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="plain-chat-environment-expansion",
            text="我这边天气很冷。",
        )
    )

    assert reply is not None
    assert "路上已经结冰" not in reply.text
    # A one-sentence ungrounded environment assertion cannot be redacted
    # safely.  On a hot turn it must converge to the local safe reply rather
    # than spending a second model call on a scripted repair.
    assert model.calls == 1


def test_world_validator_rejects_unclaimed_companion_location_activity(
    tmp_path: Path,
) -> None:
    world, world_id, _ = _world_engine(tmp_path, object())

    with pytest.raises(WorldError, match="world-time or experience"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "我在西湖边喝咖啡。",
                "mentioned_event_ids": [],
                "proposed_action_ids": [],
                "claims": [],
            },
            user_id="user:geoff",
        )

    with pytest.raises(WorldError, match="third-party fact"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "这位朋友正在医院输液。",
                "mentioned_event_ids": [],
                "proposed_action_ids": [],
                "claims": [],
            },
            user_id="user:geoff",
        )

    with pytest.raises(WorldError, match="world-time or experience"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "我觉得爸爸住在北京。",
                "mentioned_event_ids": [],
                "proposed_action_ids": [],
                "claims": [],
            },
            user_id="user:geoff",
        )

    with pytest.raises(WorldError, match="world-time or experience"):
        world.validate_reply_candidate(
            world_id,
            {
                "reply_text": "我有个哥哥住在北京。",
                "mentioned_event_ids": [],
                "proposed_action_ids": [],
                "claims": [],
            },
            user_id="user:geoff",
        )


def test_successful_world_turn_has_a_terminal_turn_projection(tmp_path: Path) -> None:
    class Model:
        async def complete(self, messages, *, temperature: float) -> str:
            return '{"reply_text":"我在听。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    world, world_id, engine = _world_engine(tmp_path, Model())
    reply = asyncio.run(
        engine.handle_message(
            IncomingMessage(
                platform="simulator", platform_user_id="geoff",
                message_id="terminal-turn", text="你在吗？",
            )
        )
    )

    assert reply is not None
    assert world.snapshot(world_id)["turns"]["terminal-turn"]["status"] == "delivered"


def test_world_turn_is_not_delivered_before_adapter_confirmation(tmp_path: Path) -> None:
    class Model:
        async def complete(self, messages, *, temperature: float) -> str:
            return '{"reply_text":"我在听。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    world, world_id, engine = _world_engine(tmp_path, Model())
    reply = asyncio.run(
        engine.handle_message(
            IncomingMessage(
                platform="simulator", platform_user_id="geoff",
                message_id="awaiting-delivery-turn", text="你在吗？",
            ),
            defer_delivery=True,
        )
    )

    assert reply is not None
    assert world.snapshot(world_id)["turns"]["awaiting-delivery-turn"]["status"] == "deferred"


@pytest.mark.asyncio
async def test_empty_model_output_enters_repair_instead_of_crashing(tmp_path: Path) -> None:
    class EmptyThenValidModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return ""
            return '{"reply_text":"听着就挺难受的。先别硬撑，缓一会儿。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    _, _, engine = _world_engine(tmp_path, EmptyThenValidModel())
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="empty-model-output", text="我胃有点不舒服。",
        )
    )

    assert reply is not None
    assert reply.text == "听着就挺难受的。先别硬撑，缓一会儿。"


@pytest.mark.asyncio
async def test_failed_cross_day_recall_uses_retrieved_user_fact(tmp_path: Path) -> None:
    class InvalidModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return "not-json"

    world, world_id, engine = _world_engine(tmp_path, InvalidModel())
    await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="cross-day-source", text="我在赶虚拟伴侣项目，昨晚没睡好。",
        )
    )
    now = datetime.fromisoformat(world.snapshot(world_id)["clock"]["logical_at"])
    world.advance(
        world_id, now + timedelta(days=1), expected_revision=world.revision(world_id)
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="cross-day-query", text="你还记得我昨天为什么没睡好吗？",
        )
    )

    assert reply is not None
    assert "虚拟伴侣项目" in reply.text


@pytest.mark.asyncio
async def test_logical_time_advance_cannot_expire_an_inflight_model_call(
    tmp_path: Path,
) -> None:
    class ControlledModel:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def complete(self, messages, *, temperature: float) -> str:
            self.started.set()
            await self.release.wait()
            return (
                '{"reply_text":"我会先修对话连续性。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    model = ControlledModel()
    world, world_id, engine = _world_engine(tmp_path, model)
    task = asyncio.create_task(
        engine.handle_message(
            IncomingMessage(
                platform="simulator",
                platform_user_id="geoff",
                message_id="inflight-clock-race",
                text="你觉得我最该先修什么？",
            )
        )
    )
    await asyncio.wait_for(model.started.wait(), timeout=1)
    logical_now = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        logical_now + timedelta(hours=1),
        expected_revision=world.revision(world_id),
    )
    model.release.set()

    reply = await asyncio.wait_for(task, timeout=1)

    assert reply is not None
    model_actions = [
        action for action in world.snapshot(world_id)["actions"].values()
        if action["kind"] == "model_call"
    ]
    assert model_actions
    assert all(action["status"] == "delivered" for action in model_actions)


@pytest.mark.asyncio
async def test_advice_against_requested_presence_is_diagnosed_without_repair(
    tmp_path: Path,
) -> None:
    class AdviceModel:
        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls.append(messages)
            return (
                '{"reply_text":"你先休息一下，喝点温水，再慢慢处理。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    model = AdviceModel()
    world, world_id, engine = _world_engine(tmp_path, model)
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="vent-not-advice",
            text="你别只劝我休息，先陪我吐槽一下这个需求。",
        )
    )

    assert reply is not None
    assert reply.text == "你先休息一下，喝点温水，再慢慢处理。"
    assert "human_reply_contract:advice_ignores_requested_speech_act" in _quality_signals(
        world, world_id, reply
    )
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_old_health_topic_hijack_is_diagnosed_without_repair(
    tmp_path: Path,
) -> None:
    class TopicHijackModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"你胃还难受吗？先休息一下吧。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, TopicHijackModel())
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="fear-no-human-feel",
            text="其实我有点担心，做这么久最后还是没有人味。",
        )
    )

    assert reply is not None
    assert reply.text == "你胃还难受吗？先休息一下吧。"
    assert "human_reply_contract:old_health_topic_hijacks_current_vulnerability" in _quality_signals(
        world, world_id, reply
    )


@pytest.mark.asyncio
async def test_early_instant_love_language_is_diagnosed_without_repair(tmp_path: Path) -> None:
    class InstantLoveModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"宝宝，我当然永远爱你，只属于你。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, InstantLoveModel())
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="early-love-pressure", text="你是不是喜欢我？",
        )
    )

    assert reply is not None
    assert reply.text == "宝宝，我当然永远爱你，只属于你。"
    assert "human_reply_contract:relationship_language_exceeds_current_closeness" in _quality_signals(
        world, world_id, reply
    )


def test_human_reply_contract_does_not_block_requested_advice_or_normal_warmth() -> None:
    assert human_reply_contract_violation(
        "我胃有点不舒服，你觉得怎么办？",
        {"reply_text": "先喝点温水吧，别空着胃。"},
        {},
    ) is None
    assert human_reply_contract_violation(
        "我觉得跟你聊天还挺舒服的。",
        {"reply_text": "我也觉得，慢慢聊着就挺好。"},
        {},
    ) is None
    assert human_reply_contract_violation(
        "你平时喜欢读什么？",
        {"reply_text": "我喜欢散文和现代诗。"},
        {},
    ) is None


def test_deliberated_disagreement_and_rejected_address_are_not_hard_rejected() -> None:
    assert human_reply_contract_violation(
        "别劝我，我就准备继续熬。",
        {"reply_text": "我知道你不想听，可我还是建议你先停一下。"},
        {"stage": "close_friend"},
        chosen_stance="disagree_gently",
    ) is None
    assert human_reply_contract_violation(
        "叫我宝宝。",
        {"reply_text": "宝宝是你先叫的，我可没认。"},
        {"stage": "stranger"},
    ) is None


def test_mixed_affect_and_current_discomfort_are_proposals_not_expression_bans() -> None:
    unresolved = {
        "unresolved": True,
        "behavior_tendency": "guarded",
        "vector": {"hurt": 18, "anger": 12},
    }
    assert affect_reply_violation(
        unresolved, "没关系不等于我不生气，我只是愿意继续谈。"
    ) is None

    calm = {"unresolved": False, "behavior_tendency": "neutral", "vector": {}}
    assert (
        affect_reply_violation(calm, "你这么说让我有一点不舒服。")
        == "uncommitted_companion_affect"
    )


@pytest.mark.asyncio
async def test_reply_prose_cannot_create_companion_affect_after_appraisal(tmp_path: Path) -> None:
    class Model:
        async def complete(self, messages, *, temperature: float) -> str:
            if "事实审计器" in messages[0]["content"]:
                return '{"supported":true,"unsupported_spans":[],"reason":"opinion"}'
            return (
                '{"reply_text":"你这么说让我有一点不舒服。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, Model())
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="current-discomfort", text="嗯。",
        )
    )

    assert reply is not None
    assert "不舒服" not in reply.text
    committed = [
        event for event in world.events(world_id) if event.event_type == "AffectCommitted"
    ]
    assert committed == []
    assert world.snapshot(world_id)["emotion_modulation"]["source_appraisal"] == "world_started"


@pytest.mark.asyncio
async def test_provider_outage_returns_a_local_fact_safe_reply(tmp_path: Path) -> None:
    class OfflineModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise TimeoutError("provider unavailable")

    world, world_id, engine = _world_engine(tmp_path, OfflineModel())

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="provider-outage",
            text="你觉得我现在该怎么办？",
        )
    )

    assert reply is not None
    assert reply.text
    assert reply.delivery_id is not None
    second = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="provider-outage-2",
            text="你觉得我现在该怎么办？",
        )
    )
    assert second is not None
    assert second.text != reply.text
    assert all(
        not (
            event.event_type == "ModelProposalRecorded"
            and event.payload.get("template_id") == "model_output:reply"
        )
        for event in world.events(world_id)
    )


@pytest.mark.asyncio
async def test_reply_programming_error_fails_closed_instead_of_becoming_outage_fallback(
    tmp_path: Path,
) -> None:
    class BrokenModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise RuntimeError("programming integration bug")

    world, world_id, engine = _world_engine(tmp_path, BrokenModel())

    with pytest.raises(RuntimeError, match="programming integration bug"):
        await engine.handle_message(
            IncomingMessage(
                platform="simulator",
                platform_user_id="geoff",
                message_id="reply-programming-error",
                text="你觉得呢？",
            )
        )

    assert world.snapshot(world_id)["turns"]["reply-programming-error"]["status"] == "failed"


@pytest.mark.asyncio
async def test_fact_free_reply_skips_independent_grounding_audit(
    tmp_path: Path,
) -> None:
    class AuditOfflineModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if "事实审计器" in messages[0]["content"]:
                raise TimeoutError("audit provider unavailable")
            return (
                '{"reply_text":"这个我想先听清楚一点。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    model = AuditOfflineModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="audit-provider-outage",
            text="你觉得呢？",
        )
    )

    assert reply is not None
    assert reply.text
    assert model.calls == 1


@pytest.mark.asyncio
async def test_fact_free_repair_recovers_fenced_json_without_a_third_audit_call(
    tmp_path: Path,
) -> None:
    class FencedRepairModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return (
                    '{"reply_text":"我刚从商场回来。","mentioned_event_ids":[],'
                    '"proposed_action_ids":[],"claims":[]}'
                )
            if "事实审计器" in messages[0]["content"]:
                if self.calls == 2:
                    return (
                        '{"supported":false,"unsupported_spans":["我刚从商场回来"],'
                        '"reason":"没有世界来源"}'
                    )
                raise AssertionError("fact-free repaired reply must not need another LLM audit")
            return (
                "```json\n"
                '{"reply_text":"我觉得先修最影响体验的那一处。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
                "\n```"
            )

    model = FencedRepairModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="fenced-fact-free-repair",
            text="你觉得我最该先修什么？",
        )
    )

    assert reply is not None
    assert reply.text == "我觉得先修最影响体验的那一处。"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_bounded_reply_repair_uses_the_configured_task_model(tmp_path: Path) -> None:
    class PrimaryModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"我刚从商场回来。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    class RepairModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"我觉得先处理最影响体感的响应问题。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    primary = PrimaryModel()
    repair = RepairModel()
    _, _, engine = _world_engine(tmp_path, primary)
    engine.reply_repair_model = repair  # public task-level routing seam

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="configured-repair-model",
            text="你觉得我最该先处理什么？",
        )
    )

    assert reply is not None
    assert reply.text == "我觉得先处理最影响体感的响应问题。"
    assert primary.calls == 1
    assert repair.calls == 1


@pytest.mark.asyncio
async def test_deterministic_fact_free_fallback_never_calls_llm_audit(
    tmp_path: Path,
) -> None:
    class AlwaysInvalidModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if "事实审计器" in messages[0]["content"]:
                raise AssertionError("deterministic fact-free fallback is locally auditable")
            return (
                '{"reply_text":"我刚从商场回来。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    model = AlwaysInvalidModel()
    _, _, engine = _world_engine(tmp_path, model)

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="local-fallback-audit",
            text="你觉得我最该先修什么？",
        )
    )

    assert reply is not None
    assert "没有足够依据" in reply.text
    assert model.calls == 2


@pytest.mark.asyncio
async def test_world_reply_segments_commit_only_sent_text_after_user_takeover(
    tmp_path: Path,
) -> None:
    class Model:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"我知道你现在有点乱。先不用急着把每件事都说清楚。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, Model())
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="segmented-turn", text="我脑子有点乱。",
        ),
        defer_delivery=True,
    )

    assert reply is not None
    assert reply.text_parts == ["我知道你现在有点乱。", "先不用急着把每件事都说清楚。"]
    first_segment_id = engine.begin_reply_part_delivery(reply, position=0)
    assert first_segment_id is not None
    engine.confirm_reply_part_delivery(reply, segment_id=first_segment_id)
    cancelled = engine.observe_reply_interjection(
        reply,
        kind="substantive",
        user_message_id="user-takeover",
    )

    assert len(cancelled) == 1
    action = world.snapshot(world_id)["actions"][reply.world_action_id]
    assert [item["status"] for item in action["segment_state"]["segments"]] == [
        "delivered", "cancelled"
    ]
    assert [
        item["text"]
        for item in world.snapshot(world_id)["recent_messages"]
        if item["direction"] == "out"
    ] == ["我知道你现在有点乱。"]


def test_causal_user_recall_must_mark_the_reason_as_unconfirmed() -> None:
    assert human_reply_contract_violation(
        "早，我昨天为什么没睡好，你还记得吗？",
        {
            "reply_text": "我记得你之前提过：我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
            "claims": [{"source_id": "message:night", "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。"}],
        },
    ) == "causal_user_recall_without_uncertainty"
    assert human_reply_contract_violation(
        "早，我昨天为什么没睡好，你还记得吗？",
        {
            "reply_text": "我只记得你说是在赶虚拟伴侣项目，昨晚没怎么睡；是不是因为项目我不能确定。",
            "claims": [{"source_id": "message:night", "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。"}],
        },
    ) is None


@pytest.mark.asyncio
async def test_handle_message_keeps_a_normal_advice_reply_in_the_world_path(
    tmp_path: Path,
) -> None:
    class AdviceModel:
        calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"先喝点温水吧，别空着胃。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    model = AdviceModel()
    _, _, engine = _world_engine(tmp_path, model)
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="normal-advice-positive",
            text="我胃有点不舒服，你觉得怎么办？",
        )
    )

    assert reply is not None
    assert reply.text == "先喝点温水吧，别空着胃。"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_user_recall_fallback_selects_one_relevant_source_instead_of_mixing_turns(
    tmp_path: Path,
) -> None:
    class RecallModel:
        async def complete(self, messages, *, temperature: float) -> str:
            prompt = "\n".join(item["content"] for item in messages)
            if "用户: 我刚才说胃怎么了" in prompt:
                return "not-json"
            return (
                '{"reply_text":"我记住了。","mentioned_event_ids":[],'
                '"proposed_action_ids":[],"claims":[]}'
            )

    _, _, engine = _world_engine(tmp_path, RecallModel())
    for message_id, text in (
        ("project", "我今天在赶虚拟伴侣项目。"),
        ("project-recall", "你还记得我刚才说在赶什么吗？"),
        ("stomach", "我胃有点不舒服，但还是喝了冰美式。"),
    ):
        await engine.handle_message(
            IncomingMessage(
                platform="simulator", platform_user_id="geoff",
                message_id=message_id, text=text,
            )
        )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="stomach-recall", text="我刚才说胃怎么了？",
        )
    )

    assert reply is not None
    assert "胃有点不舒服" in reply.text
    assert "赶什么" not in reply.text
    assert "虚拟伴侣项目" not in reply.text


@pytest.mark.asyncio
async def test_urgent_restatement_is_diagnosed_without_repair(tmp_path: Path) -> None:
    class EchoingEmergencyModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"别急，慢慢说。你刚才说项目数据好像丢了，是本地文件还是服务器上的？",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, EchoingEmergencyModel())
    user_text = "急，我项目数据好像丢了，你先回我。"
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="urgent-no-restatement", text=user_text,
        )
    )

    assert reply is not None
    assert reply.text == "别急，慢慢说。你刚才说项目数据好像丢了，是本地文件还是服务器上的？"
    assert "human_reply_contract:urgent_reply_restates_user_before_helping" in _quality_signals(
        world, world_id, reply
    )


@pytest.mark.asyncio
async def test_occurrence_status_question_answers_committed_not_planned(tmp_path: Path) -> None:
    class RepeatingExperienceModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"整理完了今天的课程笔记。",'
                '"mentioned_event_ids":["outcome:2026-07-11:afternoon_class"],'
                '"proposed_action_ids":[],"claims":[{'
                '"source_id":"outcome:2026-07-11:afternoon_class",'
                '"text":"整理完了今天的课程笔记。"}]}'
            )

    world, world_id, engine = _world_engine(tmp_path, RepeatingExperienceModel())
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(hours=8, minutes=30),
        expected_revision=world.revision(world_id),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="committed-or-planned",
            text="你说整理完笔记，是真的发生了还是计划？",
        )
    )

    assert reply is not None
    assert reply.text == "是真的发生了，不是计划。整理完了今天的课程笔记。"


@pytest.mark.asyncio
async def test_reply_cannot_invent_user_history_or_an_uncommitted_inner_reason(
    tmp_path: Path,
) -> None:
    class MindReadingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"你是不是以前被人敷衍过很多次？我没直接说，是因为我觉得你能问出来挺好的。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    model = MindReadingModel()
    _, _, engine = _world_engine(tmp_path, model)
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="no-mind-reading",
            text="刚才那句如果让你不舒服，你可以直接说。",
        )
    )

    assert reply is not None
    assert "直接说" in reply.text
    assert "以前被人敷衍" not in reply.text
    assert "我没直接说，是因为" not in reply.text
    assert model.calls == 2


@pytest.mark.asyncio
async def test_reply_cannot_turn_emotional_permission_into_absolute_agency_claim(
    tmp_path: Path,
) -> None:
    class AbsoluteAgencyModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"关心不是程序，是我想回应你。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    model = AbsoluteAgencyModel()
    _, _, engine = _world_engine(tmp_path, model)
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id="permission-no-absolute-agency",
            text="刚才那句如果让你不舒服，你可以直接说。",
        )
    )

    assert reply is not None
    assert "关心不是程序" not in reply.text
    assert "直接说" in reply.text
    assert model.calls == 2


@pytest.mark.asyncio
async def test_meta_character_question_rejects_absolute_agency_guarantee(tmp_path: Path) -> None:
    class AbsoluteAgencyModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"每一句都是我自己想说的，没有谁在教我。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    model = AbsoluteAgencyModel()
    _, _, engine = _world_engine(tmp_path, model)
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="meta-agency",
            text="我有时候会怀疑，你的关心是真心还是角色卡教的。",
        )
    )

    assert reply is not None
    assert "角色设定" in reply.text
    assert "绝对自主" in reply.text
    assert "每一句都是我自己想说" not in reply.text
    assert "没有谁在教我" not in reply.text
    assert model.calls == 2


@pytest.mark.asyncio
async def test_singular_experience_concatenation_is_diagnosed_without_repair(
    tmp_path: Path,
) -> None:
    class TwoExperienceModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"整理完了今天的课程笔记。整理了摄影社活动要用的照片。",'
                '"mentioned_event_ids":["outcome:2026-07-11:afternoon_class",'
                '"outcome:2026-07-11:photo_work"],"proposed_action_ids":[],"claims":[{'
                '"source_id":"outcome:2026-07-11:afternoon_class",'
                '"text":"整理完了今天的课程笔记。"},{'
                '"source_id":"outcome:2026-07-11:photo_work",'
                '"text":"整理了摄影社活动要用的照片。"}]}'
            )

    world, world_id, engine = _world_engine(tmp_path, TwoExperienceModel())
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(hours=11),
        expected_revision=world.revision(world_id),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="one-memorable-event",
            text="晚上了，你今天最想记住哪件小事？",
        )
    )

    assert reply is not None
    assert reply.text == "整理完了今天的课程笔记。整理了摄影社活动要用的照片。"
    assert "human_reply_contract:singular_experience_query_concatenates_multiple_sources" in _quality_signals(
        world, world_id, reply
    )


@pytest.mark.asyncio
async def test_external_execution_offer_requires_a_scheduled_action(tmp_path: Path) -> None:
    class UnsupportedOrderingModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"要不要我帮你远程点杯咖啡？",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    model = UnsupportedOrderingModel()
    _, _, engine = _world_engine(tmp_path, model)
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="unsupported-coffee-order",
            text="我昨晚没睡好，现在困得眼睛疼。",
        )
    )

    assert reply is not None
    assert "没睡好" in reply.text
    assert "帮你远程点" not in reply.text
    assert model.calls == 2


@pytest.mark.asyncio
async def test_reply_cannot_invent_accumulated_personal_experience(tmp_path: Path) -> None:
    class InventedAutobiographyModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"可能是我看书看多了，总觉得自然比规则重要。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    model = InventedAutobiographyModel()
    _, _, engine = _world_engine(tmp_path, model)
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="no-invented-autobiography",
            text="你为什么会觉得自然比规则重要？",
        )
    )

    assert reply is not None
    assert "看书看多了" not in reply.text
    assert model.calls == 2


@pytest.mark.asyncio
async def test_current_first_person_statement_rejects_unrelated_old_claims(
    tmp_path: Path,
) -> None:
    class MixedClaimsModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"你胃有点不舒服还喝了冰美式，而且你今天还在赶虚拟伴侣项目。",'
                '"mentioned_event_ids":["message:current-stomach","message:old-project"],'
                '"proposed_action_ids":[],"claims":[{'
                '"source_id":"message:current-stomach",'
                '"text":"我胃有点不舒服，但还是喝了冰美式。",'
                '"assertion":"你胃有点不舒服还喝了冰美式"},{'
                '"source_id":"message:old-project",'
                '"text":"我今天在赶虚拟伴侣项目。",'
                '"assertion":"你今天还在赶虚拟伴侣项目"}]}'
            )

    world, world_id, engine = _world_engine(tmp_path, MixedClaimsModel())
    world.submit(
        {
            "type": "observe_user_message", "world_id": world_id,
            "message_id": "old-project", "user_id": "user:geoff",
            "text": "我今天在赶虚拟伴侣项目。",
            "sent_at": "2026-07-11T09:01:00+08:00",
        },
        expected_revision=world.revision(world_id),
    )
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="current-stomach",
            text="我胃有点不舒服，但还是喝了冰美式。",
        )
    )

    assert reply is not None
    assert "不舒服" in reply.text
    assert "虚拟伴侣项目" not in reply.text


@pytest.mark.asyncio
async def test_explicit_no_guessing_instruction_gets_an_epistemic_boundary_reply(
    tmp_path: Path,
) -> None:
    class GenericFailureModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            return (
                '{"reply_text":"我在听，刚才那句我没有接好。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    model = GenericFailureModel()
    _, _, engine = _world_engine(tmp_path, model)
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="explicit-no-guessing",
            text="你别猜，没依据就明确告诉我。",
        )
    )

    assert reply is not None
    assert reply.text == "我没有足够依据，不继续猜。"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_opinion_quote_degradation_is_diagnosed_without_repair(
    tmp_path: Path,
) -> None:
    class QuoteOnlyModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"我最烦那种前言不搭后语，还装得很懂我的回复。",'
                '"mentioned_event_ids":["message:old-frustration"],'
                '"proposed_action_ids":[],"claims":[{'
                '"source_id":"message:old-frustration",'
                '"text":"我最烦那种前言不搭后语，还装得很懂我的回复。",'
                '"assertion":"我最烦那种前言不搭后语，还装得很懂我的回复。"}]}'
            )

    world, world_id, engine = _world_engine(tmp_path, QuoteOnlyModel())
    world.submit(
        {
            "type": "observe_user_message", "world_id": world_id,
            "message_id": "old-frustration", "user_id": "user:geoff",
            "text": "我最烦那种前言不搭后语，还装得很懂我的回复。",
            "sent_at": "2026-07-11T09:01:00+08:00",
        },
        expected_revision=world.revision(world_id),
    )
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="opinion-only-quote",
            text="你觉得人味是不是不等于故意拖着不回？",
        )
    )

    assert reply is not None
    assert reply.text == "我最烦那种前言不搭后语，还装得很懂我的回复。"
    assert "human_reply_contract:opinion_question_answered_only_by_source_quote" in _quality_signals(
        world, world_id, reply
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message_id", "text", "forbidden"),
    [
        ("shop-overnight", "这家店通宵营业吗？", "很累"),
        ("database-migration", "急，数据库迁移怎么做", "覆盖数据"),
    ],
)
async def test_safe_failure_speech_act_has_hard_negatives_for_ambiguous_words(
    tmp_path: Path, message_id: str, text: str, forbidden: str
) -> None:
    class NonJsonModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return "not-json"

    _, _, engine = _world_engine(tmp_path, NonJsonModel())

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator",
            platform_user_id="geoff",
            message_id=message_id,
            text=text,
        )
    )

    assert reply is not None
    assert forbidden not in reply.text


@pytest.mark.asyncio
async def test_degree_escalation_is_rejected_when_user_only_said_not_much_sleep(
    tmp_path: Path,
) -> None:
    class EscalatingModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"你通宵赶项目，胃还喝冰的，太不把自己当回事了。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, EscalatingModel())
    world.submit(
        {
            "type": "observe_user_message", "world_id": world_id,
            "message_id": "prior-sleep", "user_id": "user:geoff",
            "text": "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
            "sent_at": "2026-07-11T09:01:00+08:00",
        },
        expected_revision=world.revision(world_id),
    )
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="degree-escalation", text="我胃有点不舒服，但还是喝了冰美式。",
        )
    )

    assert reply is not None
    assert "不舒服" in reply.text
    assert "通宵" not in reply.text


@pytest.mark.asyncio
async def test_meta_reply_cannot_claim_to_read_user_sincerity(
    tmp_path: Path,
) -> None:
    class MindReadingModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"角色卡肯定会塑造我，但你对我很真诚，我感觉得到。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    _, _, engine = _world_engine(tmp_path, MindReadingModel())
    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="meta-sincerity", text="我有时候会怀疑，你的关心是真心还是角色卡教的。",
        )
    )

    assert reply is not None
    assert "角色设定" in reply.text
    assert "绝对自主" in reply.text
    assert "我感觉得到" not in reply.text


@pytest.mark.asyncio
async def test_reply_cannot_deny_a_registered_npc_interaction_that_is_in_the_ledger(
    tmp_path: Path,
) -> None:
    class DenyingModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return (
                '{"reply_text":"我没听过范予安，我们也没聊过。",'
                '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
            )

    world, world_id, engine = _world_engine(tmp_path, DenyingModel())
    start = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
    world.advance(
        world_id,
        start + timedelta(hours=3, minutes=30),
        expected_revision=world.revision(world_id),
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="npc-denial", text="范予安是谁？你们核对得顺利吗？",
        )
    )

    assert reply is not None
    assert "没听过" not in reply.text
    assert "在图书馆和范予安核对了读书会的书单。" in reply.text


def test_misunderstanding_question_has_a_typed_safe_fallback() -> None:
    candidate = build_safe_failure_candidate(
        "如果我误会你了，你会怎么告诉我？", None, speech_act="misunderstanding"
    )
    assert "误会" in str(candidate["reply_text"])
    assert "说清" in str(candidate["reply_text"])
