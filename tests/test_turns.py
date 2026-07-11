from companion_daemon.context_orchestrator import build_context_package
from companion_daemon.emotion_state import InteractionEvent
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.turns import TurnPlan, build_turn_plan
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
import pytest


def test_turn_plan_exposes_the_behavioral_contract_for_a_vulnerable_message() -> None:
    message = IncomingMessage(platform="qq", platform_user_id="geoff", text="我今天真的有点撑不住")
    package = build_context_package(message, MoodState(mood="worried"), [], [])
    event = InteractionEvent(
        kind="user_vulnerable",
        intensity=3,
        user_intent="vulnerable_sharing",
        private_note="用户在示弱。",
        reply_style_hint="温柔、具体、少说教，先接住情绪。",
    )

    plan = build_turn_plan(
        event=event,
        context_package=package,
        allowed_facts=["用户事实/所在地: 成都"],
        subtext=None,
    )

    assert isinstance(plan, TurnPlan)
    assert plan.appraisal == "user_vulnerable"
    assert plan.expression_policy == package.reply_policy
    assert plan.allowed_facts == ("用户事实/所在地: 成都",)
    assert plan.observable_reason == "用户在示弱，优先接住情绪。"


def test_turn_plan_keeps_subtext_as_a_short_lived_constraint_not_a_fact() -> None:
    message = IncomingMessage(platform="qq", platform_user_id="geoff", text="嗯")
    package = build_context_package(message, MoodState(mood="sulking", security=30), [], [])
    event = InteractionEvent("ordinary_message", 1, "ordinary_chat", "", "短句、自然，别像客服。")

    plan = build_turn_plan(
        event=event,
        context_package=package,
        allowed_facts=[],
        subtext="想被认真对待，但嘴上会硬一点。",
    )

    assert plan.short_lived_constraint == "想被认真对待，但嘴上会硬一点。"
    assert plan.short_lived_constraint not in plan.allowed_facts
    assert "回合授权" in plan.prompt_block()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_appraisal", "expected_reason"),
    [
        ("我今天真的有点撑不住", "user_vulnerable", "优先接住情绪"),
        ("滚，别烦我", "boundary_violation", "保持短而清楚"),
        ("对不起，刚刚那样说不对", "repair_attempt", "允许缓和"),
        ("我先忙一会儿", "availability_drop", "收住主动性"),
        ("我回来了", "return_after_gap", "自然接上当前话题"),
        ("你觉得怎么样？", "curiosity_invited", "普通推进"),
        ("今天下雨了", "ordinary_message", "普通推进"),
        ("宝宝你在吗", "premature_intimacy", "普通推进"),
    ],
)
async def test_turn_replay_records_a_visible_behavioral_reason(
    tmp_path, text: str, expected_appraisal: str, expected_reason: str
) -> None:
    store = CompanionStore(tmp_path / "test.sqlite")
    seed_user(store)
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。")

    reply = await engine.handle_message(IncomingMessage(platform="qq", platform_user_id="geoff", text=text))

    assert reply is not None
    trace = store.recent_turn_traces("geoff")[-1]
    assert trace["appraisal"] == expected_appraisal
    assert expected_reason in trace["observable_reason"]
    assert trace["status"] == "delivered"
