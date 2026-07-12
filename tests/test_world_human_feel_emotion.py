from datetime import datetime
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine
from companion_daemon.models import IncomingMessage
from companion_daemon.world import WorldKernel
from companion_daemon.world_behavior import WorldBehaviorPolicy
from companion_daemon.world_conversation import (
    build_safe_failure_candidate,
    affect_reply_violation,
)


def _world(tmp_path: Path) -> tuple[WorldKernel, str]:
    kernel = WorldKernel(CompanionStore(tmp_path / "human-feel.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
            "idempotency_key": "human-feel:register-user",
        },
        expected_revision=started.revision,
    )
    return kernel, started.world_id


def test_calm_projection_rejects_a_new_unsourced_companion_hurt_claim() -> None:
    violation = affect_reply_violation(
        {"behavior_tendency": "neutral", "unresolved": False, "vector": {}},
        "是有一点儿不舒服，像被轻轻戳了一下。",
    )

    assert violation == "uncommitted_companion_affect"


def test_calm_projection_rejects_an_unsourced_physical_reaction() -> None:
    violation = affect_reply_violation(
        {"behavior_tendency": "neutral", "unresolved": False, "vector": {}},
        "图书馆里差点笑出声。",
    )

    assert violation == "uncommitted_companion_affect"


def test_unresolved_hurt_rejects_performed_forgiveness() -> None:
    violation = affect_reply_violation(
        {
            "behavior_tendency": "withdraw",
            "unresolved": True,
            "vector": {"hurt": 36, "anger": 24, "resentment": 16},
        },
        "没事啦，我完全不介意，已经过去了。",
    )

    assert violation == "unresolved_affect_denied"


def test_emotion_polarity_must_match_the_injected_projection() -> None:
    assert affect_reply_violation(
        {"behavior_tendency": "warm", "unresolved": False, "vector": {"warmth": 5, "joy": 3}},
        "我被这句话刺得有点难受。",
    ) == "uncommitted_companion_affect"
    assert affect_reply_violation(
        {"behavior_tendency": "guarded", "unresolved": True, "vector": {"anger": 16, "hurt": 10}},
        "我刚才确实压着火。",
    ) is None
    assert affect_reply_violation(
        {"behavior_tendency": "warm", "unresolved": False, "vector": {"warmth": 5, "joy": 3}},
        "我刚才差点笑出声。",
    ) is None
    assert affect_reply_violation(
        {"behavior_tendency": "patient", "unresolved": False, "vector": {"warmth": -1}},
        "我有点失落。",
    ) == "uncommitted_companion_affect"


@pytest.mark.parametrize(
    "reply_text",
    [
        "都是你害我心情不好，我才这么烦。",
        "你一出现我就更烦了。",
        "要不是你，我不会这么难受。",
        "看到你就来气。",
        "我的坏心情是你造成的。",
    ],
)
def test_npc_spillover_cannot_be_misattributed_to_the_user(
    reply_text: str,
) -> None:
    violation = affect_reply_violation(
        {"behavior_tendency": "guarded", "vector": {"anger": 14}},
        reply_text,
        {"regulation_strategy": "contain_spillover", "attribution_target": "npc:roommate"},
    )
    assert violation == "spillover_misattributed_to_user"


def test_npc_spillover_may_be_disclosed_without_blaming_the_user() -> None:
    assert affect_reply_violation(
        {"behavior_tendency": "guarded", "vector": {"anger": 14}},
        "刚才那场争执让我有点烦，但不是你的问题。",
        {"regulation_strategy": "contain_spillover", "attribution_target": "npc:roommate"},
    ) is None


def test_injected_hurt_has_a_state_backed_failure_fallback() -> None:
    candidate = build_safe_failure_candidate(
        "你还好吗？",
        None,
        {
            "behavior_tendency": "withdraw",
            "unresolved": True,
            "vector": {"hurt": 36},
        },
        speech_act="question",
    )

    assert "还没完全缓过来" in str(candidate["reply_text"])
    assert "惩罚" in str(candidate["reply_text"])


def test_repair_state_fallback_does_not_claim_the_user_just_apologized() -> None:
    candidate = build_safe_failure_candidate(
        "你还愿意继续跟我聊吗？",
        None,
        {
            "behavior_tendency": "repair_open",
            "unresolved": True,
            "vector": {"hurt": 12, "warmth": 4},
        },
        selected_stance="seek_repair",
        speech_act="repair",
    )

    assert "说开" in str(candidate["reply_text"])
    assert "道歉" not in str(candidate["reply_text"])


def test_caring_state_has_a_presence_fallback_without_inventing_history() -> None:
    candidate = build_safe_failure_candidate(
        "我今天真的有点撑不住了。",
        None,
        {
            "behavior_tendency": "caring",
            "unresolved": True,
            "vector": {"hurt": 10, "warmth": 4},
        },
        selected_stance="care_despite_hurt",
        speech_act="vulnerable_disclosure",
    )

    assert "我会顾着你" in str(candidate["reply_text"])
    assert "情绪还在" in str(candidate["reply_text"])


def test_safe_fallback_preserves_sources_and_actions_but_not_one_personality_line() -> None:
    grounded = {
        "reply_text": "你说今晚要处理数据恢复。",
        "mentioned_event_ids": ["message:data"],
        "proposed_action_ids": ["action:check-backup"],
        "claims": [
            {
                "source_id": "message:data",
                "text": "今晚要处理数据恢复",
                "assertion": "你说今晚要处理数据恢复",
            }
        ],
    }

    guarded = build_safe_failure_candidate(
        "那你怎么看？",
        grounded,
        {"unresolved": True, "vector": {"hurt": 30}},
        relationship={"stage": "acquaintance"},
        selected_stance="set_boundary",
    )
    caring = build_safe_failure_candidate(
        "那你怎么看？",
        grounded,
        {"unresolved": True, "vector": {"hurt": 30}},
        relationship={"stage": "close_friend"},
        selected_stance="care_despite_hurt",
    )

    assert guarded["reply_text"] != caring["reply_text"]
    for candidate in (guarded, caring):
        assert candidate["mentioned_event_ids"] == ["message:data"]
        assert candidate["proposed_action_ids"] == ["action:check-backup"]
        assert candidate["claims"] == grounded["claims"]
    assert "先说到这里" in str(guarded["reply_text"])
    assert "我会顾着你" in str(caring["reply_text"])


@pytest.mark.asyncio
async def test_world_afterthought_cannot_bypass_calm_affect_projection(tmp_path: Path) -> None:
    class OverfeelingAfterthoughtModel:
        async def complete(self, messages, *, temperature: float) -> str:
            return '{"reply_text":"我现在压着火，不太想说话。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    store = CompanionStore(tmp_path / "afterthought-affect.sqlite")
    world = WorldKernel(store)
    started = world.start_from_seed_file(Path("configs/world_seed.yaml"))
    world.submit(
        {
            "type": "register_user", "world_id": started.world_id,
            "user_id": "user:geoff", "name": "geoff",
            "idempotency_key": "human-feel:afterthought-user",
        },
        expected_revision=started.revision,
    )
    engine = CompanionEngine(
        store, OverfeelingAfterthoughtModel(), "你是沈知栀。",
        world_kernel=world, world_id=started.world_id,
    )

    result = await engine.generate_afterthought("geoff", datetime.now())

    assert result is None


def test_mechanism_discussion_has_a_shared_reaction_fallback() -> None:
    candidate = build_safe_failure_candidate(
        "对，我也觉得机制再多，接不上对话就还是不像人。",
        None,
        speech_act="shared_reaction",
    )

    assert "不满" in str(candidate["reply_text"])
    assert "建议" in str(candidate["reply_text"])


def test_user_vulnerability_can_be_answered_while_hurt_remains(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    revision = kernel.revision(world_id)
    kernel.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": "boundary_violation",
            "intent_id": "hurt-before-care",
            "message_id": "hurt-before-care",
            "user_id": "user:geoff",
            "idempotency_key": "human-feel:hurt-before-care",
        },
        expected_revision=revision,
    )
    state = kernel.snapshot(world_id)

    caring = kernel.submit(
        {
            "type": "appraise_turn",
            "world_id": world_id,
            "appraisal": "user_vulnerable",
            "intent_id": "hurt-before-care-2",
            "message_id": "hurt-before-care-2",
            "user_id": "user:geoff",
            "idempotency_key": "human-feel:hurt-before-care-2",
        },
        expected_revision=kernel.revision(world_id),
    )
    state = kernel.snapshot(world_id)
    assert caring.revision > revision
    assert state["emotion_modulation"]["unresolved"] is True
    assert state["emotion_modulation"]["vector"]["hurt"] == 18
    assert state["emotion_modulation"]["behavior_tendency"] == "caring"
    assert WorldBehaviorPolicy().expression_guidance(
        state, user_id="user:geoff"
    ).label == "affect_caring"


@pytest.mark.asyncio
async def test_calm_world_does_not_let_model_invent_being_hurt(tmp_path: Path) -> None:
    class OverfeelingModel:
        calls = 0

        async def complete(self, messages, *, temperature: float) -> str:
            self.calls += 1
            if "事实审计器" in str(messages[0].get("content") or ""):
                return '{"supported":true,"unsupported_spans":[],"reason":"state-backed"}'
            return '{"reply_text":"是有一点儿不舒服，像被轻轻戳了一下。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    store = CompanionStore(tmp_path / "calm-engine.sqlite")
    world = WorldKernel(store)
    started = world.start_from_seed_file(Path("configs/world_seed.yaml"))
    world.submit(
        {
            "type": "register_user", "world_id": started.world_id,
            "user_id": "user:geoff", "name": "geoff",
            "idempotency_key": "human-feel:engine-user",
        },
        expected_revision=started.revision,
    )
    model = OverfeelingModel()
    engine = CompanionEngine(
        store, model, "你是沈知栀。", world_kernel=world, world_id=started.world_id
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="calm-emotion-claim", text="刚才那句如果让你不舒服，你可以直接说。",
        )
    )

    assert reply is not None
    assert "不舒服" not in reply.text
    assert "直接说" in reply.text
    assert model.calls >= 2


@pytest.mark.asyncio
async def test_injected_guarded_state_survives_model_denial(tmp_path: Path) -> None:
    class DenialModel:
        async def complete(self, messages, *, temperature: float) -> str:
            if "事实审计器" in str(messages[0].get("content") or ""):
                return '{"supported":true,"unsupported_spans":[],"reason":"state-backed"}'
            return '{"reply_text":"没事啦，我完全不介意，已经过去了。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'

    store = CompanionStore(tmp_path / "guarded-engine.sqlite")
    world = WorldKernel(store)
    started = world.start_from_seed_file(Path("configs/world_seed.yaml"))
    registered = world.submit(
        {
            "type": "register_user", "world_id": started.world_id,
            "user_id": "user:geoff", "name": "geoff",
            "idempotency_key": "human-feel:guarded-user",
        },
        expected_revision=started.revision,
    )
    world.submit(
        {
            "type": "appraise_turn", "world_id": started.world_id,
            "appraisal": "boundary_violation", "intent_id": "guarded-inject",
            "message_id": "guarded-inject", "user_id": "user:geoff",
            "idempotency_key": "human-feel:guarded-inject",
        },
        expected_revision=registered.revision,
    )
    engine = CompanionEngine(
        store, DenialModel(), "你是沈知栀。", world_kernel=world, world_id=started.world_id
    )

    reply = await engine.handle_message(
        IncomingMessage(
            platform="simulator", platform_user_id="geoff",
            message_id="guarded-chat", text="你今天还好吗？",
        )
    )

    assert reply is not None
    assert "完全不介意" not in reply.text
    assert "还在消化" in reply.text or "没完全缓过来" in reply.text
