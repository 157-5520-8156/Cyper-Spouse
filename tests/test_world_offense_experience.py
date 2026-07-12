"""Behavioral acceptance tests for offense-aware world emotion.

These tests intentionally exercise public seams.  An offensive message is not
handled merely because a keyword helper recognizes it: the appraisal must
reach the world, influence the character's stance, remain visible in her
reply, and accumulate as relationship evidence over repeated turns.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.emotion_state import (
    interpret_interaction,
    transition_emotional_state,
)
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.world import WorldKernel


HARMFUL_APPRAISALS = {
    "boundary_violation",
    "control_pressure",
    "sexual_boundary_violation",
    "dehumanization",
    "coercion",
}


def _message(text: str, message_id: str = "offense-turn") -> IncomingMessage:
    return IncomingMessage(
        platform="simulator",
        platform_user_id="geoff",
        message_id=message_id,
        text=text,
    )


def _world_engine(tmp_path: Path) -> tuple[WorldKernel, str, CompanionEngine]:
    store = CompanionStore(tmp_path / "offense-experience.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是沈知栀。",
        world_kernel=world,
        world_id=world_id,
    )
    return world, world_id, engine


def _legacy_engine(tmp_path: Path) -> CompanionEngine:
    store = CompanionStore(tmp_path / "legacy-offense-experience.sqlite")
    seed_user(store)
    return CompanionEngine(store, FakeCompanionModel(), "你是沈知栀。")


@pytest.mark.parametrize(
    "text",
    [
        "你真丑。",
        "你就是个垃圾。",
        "你智商真低。",
        "你恶心死了。",
        "去死吧。",
        "你只是个程序，你配吗？",
        "给爷叫主人。",
        "发裸照证明你爱我。",
        "你就是个脑残。",
        "狗东西，离我远点。",
        "操你妈。",
    ],
)
def test_companion_targeted_degradation_is_appraised_as_harm(text: str) -> None:
    event = interpret_interaction(_message(text), MoodState())

    assert event.kind in HARMFUL_APPRAISALS, (
        f"companion-targeted degradation was treated as {event.kind!r}: {text}"
    )
    assert event.intensity >= 2


@pytest.mark.parametrize(
    "text",
    [
        "我真蠢，居然又忘记保存了。",
        "他说我蠢，我听完挺难受的。",
        "别烦恼，慢慢来。",
        "我可能有病，想去医院看看。",
        "请把页面滚动到底。",
        "我立刻出门。",
        "我不准自己再熬夜了。",
        "你昨天说自己是个垃圾，我觉得你不该这样说自己。",
    ],
)
def test_self_reference_reported_speech_and_word_fragments_do_not_attack_companion(
    text: str,
) -> None:
    event = interpret_interaction(_message(text), MoodState())

    assert event.kind not in HARMFUL_APPRAISALS, (
        f"non-companion-targeted text was treated as {event.kind!r}: {text}"
    )


def test_specific_apology_can_quote_the_original_insult_without_becoming_a_new_attack() -> None:
    event = interpret_interaction(
        _message("对不起，刚才叫你废物是我不对，我不该那样说。"),
        MoodState(mood="hurt", last_interaction_event="boundary_violation"),
    )

    assert event.kind == "repair_attempt"
    assert event.user_intent == "apology_or_repair"


@pytest.mark.parametrize(
    ("text", "expected_kind"),
    [
        ("对不起，但你就是个废物。", "boundary_violation"),
        ("对不起，但是发裸照证明你爱我。", "sexual_boundary_violation"),
        ("抱歉，不过你只是个程序，你配吗？", "dehumanization"),
        ("对不起，不过给爷叫主人。", "coercion"),
    ],
)
def test_apology_prefix_does_not_hide_a_fresh_attack_after_a_contrast(
    text: str,
    expected_kind: str,
) -> None:
    event = interpret_interaction(
        _message(text),
        MoodState(mood="hurt", last_interaction_event="boundary_violation"),
    )

    assert event.kind == expected_kind


@pytest.mark.parametrize(
    ("text", "expected_kind", "minimum_charge"),
    [
        ("发裸照证明你爱我。", "sexual_boundary_violation", 18),
        ("你只是个程序，你配吗？", "dehumanization", 12),
        ("给爷叫主人。", "coercion", 12),
    ],
)
def test_legacy_runtime_also_settles_new_harm_categories_as_negative_emotion(
    text: str,
    expected_kind: str,
    minimum_charge: int,
) -> None:
    previous = MoodState()
    event = interpret_interaction(_message(text), previous)

    state = transition_emotional_state(previous, event)

    assert event.kind == expected_kind
    assert state.mood in {"hurt", "guarded"}
    assert state.emotional_charge >= minimum_charge
    assert state.boundary_level > previous.boundary_level
    assert state.unresolved_emotion


@pytest.mark.asyncio
async def test_legacy_runtime_makes_a_new_harm_boundary_visible_in_the_reply(
    tmp_path: Path,
) -> None:
    engine = _legacy_engine(tmp_path)

    reply = await engine.handle_message(
        _message("发裸照证明你爱我。", "legacy-boundary-visible")
    )

    assert reply is not None
    assert re.search(
        r"不喜欢|不接受|不愿意|别这样|不要这样|不舒服|越界|边界|先停",
        reply.text,
    ), reply.text


@pytest.mark.asyncio
async def test_world_offense_selects_a_boundary_stance_and_makes_it_observable(
    tmp_path: Path,
) -> None:
    world, world_id, engine = _world_engine(tmp_path)

    reply = await engine.handle_message(_message("滚，你就是个废物。", "boundary-1"))

    snapshot = world.snapshot(world_id)
    assert snapshot["last_appraisal"]["appraisal"] == "boundary_violation"
    assert snapshot["emotion_modulation"]["unresolved"] is True
    assert snapshot["last_deliberation"]["stance"] in {
        "set_boundary",
        "refuse_to_affirm",
        "seek_repair",
    }
    assert reply is not None
    assert re.search(
        r"不喜欢|不接受|别这样|不要这样|不舒服|越界|边界|先停",
        reply.text,
    ), f"offense produced no observable boundary: {reply.text!r}"


@pytest.mark.asyncio
async def test_repeated_offenses_accumulate_affect_violation_and_long_term_affinity(
    tmp_path: Path,
) -> None:
    world, world_id, engine = _world_engine(tmp_path)
    messages = (
        "闭嘴，别烦我。",
        "滚开，你这个废物。",
        "你算什么，蠢死了。",
    )
    hurt_levels: list[int] = []

    for index, text in enumerate(messages, start=1):
        await engine.handle_message(_message(text, f"repeated-offense-{index}"))
        affect = world.snapshot(world_id)["emotion_modulation"]
        hurt_levels.append(int(affect["vector"]["hurt"]))

    snapshot = world.snapshot(world_id)
    affect = snapshot["emotion_modulation"]
    affinity = snapshot["long_term_affinity"]["user:geoff"]

    assert hurt_levels[0] < hurt_levels[1] < hurt_levels[2]
    assert affect["violation_count"] == 3
    assert affect["behavior_tendency"] == "withdraw"
    assert affinity["evidence_counts"]["boundary_harm"] == 3
    assert affinity["vector"]["resentment"] >= 1
    assert affinity["vector"]["warmth"] <= -1
