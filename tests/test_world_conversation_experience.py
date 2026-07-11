from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.models import IncomingMessage
from companion_daemon.sanitize import sanitize_world_chat_text
from companion_daemon.world import WorldError, WorldKernel
from companion_daemon.world_behavior import WorldBehaviorPolicy
from companion_daemon.world_conversation import (
    build_safe_failure_candidate,
    only_recites_irrelevant_sources,
    repeats_recent_companion_reply,
)


TEST_PROMPT = "你是沈知栀。"


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
        "ActionSettled", "IntentFailed", "TurnProcessingSettled"
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
    ("user_text", "expected"),
    [
        ("我胃有点不舒服，但还是喝了冰美式。", "听着就挺难受的。先别硬撑，缓一会儿。"),
        ("我今天要赶一个项目，昨晚没怎么睡。", "听着强度不小。先顾眼前最要紧的，别一直硬扛。"),
        ("其实我有点担心，做这么久最后还是没有人味。", "我明白你在担心什么。先别急着给它判死刑，我们一点点看。"),
        ("我准备睡了，但脑子还停不下来。", "那就先别逼自己马上睡着。慢慢缓一会儿，我陪你安静一下。"),
        ("急，我项目数据好像丢了，你先回我。", "在，我先回你。先别继续覆盖数据，告诉我你最后一次确认它还在是什么时候。"),
        ("你不用讲大道理，跟我说一句晚安就好。", "晚安。"),
    ],
)
def test_safe_failure_keeps_the_current_user_speech_act(
    user_text: str, expected: str
) -> None:
    assert build_safe_failure_candidate(user_text, None)["reply_text"] == expected


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
async def test_independent_grounding_audit_rejects_an_unclaimed_virtual_world_detail(
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

    class AuditModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if self.calls == 1:
                return (
                    '{"supported":false,"unsupported_spans":["图书馆门口新开了一家花店"],'
                    '"reason":"授权来源没有该现实环境事实"}'
                )
            return '{"supported":true,"unsupported_spans":[],"reason":"仅包含建议"}'

    reply_model = ReplyModel()
    audit_model = AuditModel()
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
        world_grounding_audit_model=audit_model,
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
    assert audit_model.calls == 2
    assert sum(
        action["kind"] == "model_call" and action["status"] == "delivered"
        for action in world.snapshot(world_id)["actions"].values()
    ) == 4


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
    assert "最近已结算对话" in prompt
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
            "source_type": "user_message",
            "speaker": "user",
            "content": "我在赶虚拟伴侣项目，昨晚没睡好。",
            "logical_at": "2026-07-11T09:00:00+08:00",
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
    assert reply.text == "这个我现在没有把握，不想随口糊弄你。"
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
async def test_exact_experience_quote_is_not_accepted_as_an_answer_to_a_detail_question(
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
    assert "没有能确认的记录" in reply.text
    assert reply.text != "在图书馆和范予安核对了读书会的书单。"


@pytest.mark.asyncio
async def test_non_interruptible_high_attention_activity_defers_an_ordinary_message(
    tmp_path: Path,
) -> None:
    class UnusedModel:
        async def complete(self, messages, *, temperature: float) -> str:
            raise AssertionError("a deferred turn must not call the model")

    world, world_id, engine = _world_engine(tmp_path, UnusedModel())
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

    assert reply is None
    snapshot = world.snapshot(world_id)
    assert snapshot["communication"]["attention"] == "deferred"
    scheduled = [
        action
        for action in snapshot["actions"].values()
        if action["kind"] == "reply_later" and action["status"] == "scheduled"
    ]
    assert len(scheduled) == 1
    active = next(item for item in snapshot["agenda"].values() if item["status"] == "active")
    assert active["attention_demand"] >= 75
    assert active["interruptible"] is False
    assert "active_world_activity_not_interruptible:" in snapshot["communication"]["reason"]
    assert scheduled[0]["payload"]["due_at"] <= active["ends_at"]


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
    class InvalidThenAuditModel:
        async def complete(self, messages, *, temperature: float) -> str:
            if "事实审计器" in str(messages[0].get("content") or ""):
                return '{"supported":false,"unsupported_spans":["火星"],"reason":"unsupported"}'
            return '{"reply_text":"我在火星上。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    world, world_id, engine = _world_engine(tmp_path, InvalidThenAuditModel())
    engine.world_grounding_audit_model = engine.model
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
    assert reply.text == "听着就挺难受的。先别硬撑，缓一会儿。"


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
