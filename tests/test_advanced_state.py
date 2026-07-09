from datetime import UTC, datetime, timedelta

from companion_daemon.inner_subtext import infer_inner_subtext
from companion_daemon.models import MoodState
from companion_daemon.proactive_waiting import apply_waiting_after_proactive
from companion_daemon.relationship import key_event_bonus, stage_for_scores
from companion_daemon.repair_curve import apply_repair_curve
from companion_daemon.reply_segments import split_reply_text
from companion_daemon.tone_inertia import build_tone_inertia


def test_key_event_bonus_can_advance_relationship_effective_count() -> None:
    base = stage_for_scores(18, 25, 10)
    advanced = stage_for_scores(18, 25, 10 + key_event_bonus(["用户记得她的小事"]))

    assert base == "acquaintance"
    assert advanced == "friend"


def test_repair_curve_distinguishes_serious_from_perfunctory() -> None:
    hurt = MoodState(
        mood="hurt",
        trust=30,
        security=30,
        emotional_charge=30,
        last_interaction_event="repair_attempt",
    )

    serious = apply_repair_curve(hurt, message_text="我认真道歉，以后我会注意")
    perfunctory = apply_repair_curve(hurt, message_text="对不起")

    assert serious.security > hurt.security
    assert serious.emotional_charge < hurt.emotional_charge
    assert perfunctory.security < hurt.security


def test_waiting_after_proactive_changes_once_per_stage() -> None:
    last_sent = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    state = MoodState(initiative=30, emotional_charge=5)

    first = apply_waiting_after_proactive(state, last_sent_iso=last_sent, incoming_since=0)
    second = apply_waiting_after_proactive(first, last_sent_iso=last_sent, incoming_since=0)

    assert first.initiative > state.initiative
    assert first.emotional_charge > state.emotional_charge
    assert second == first


def test_tone_inertia_preserves_recent_reserve() -> None:
    inertia = build_tone_inertia(
        MoodState(mood="guarded"),
        ["[qq] 她: 我不太喜欢这样。"],
    )

    assert inertia.label == "reserved"
    assert "不要突然热情" in inertia.prompt_line


def test_inner_subtext_can_represent_proud_hurt() -> None:
    subtext = infer_inner_subtext(MoodState(mood="sulking", security=35))

    assert subtext
    assert subtext.label == "wants_repair_but_proud"


def test_split_reply_text_keeps_reserved_replies_single() -> None:
    text = "我知道了。你先忙吧。我晚点再说。刚刚其实还想补一句，但又觉得现在说好像有点打扰你。"

    reserved = split_reply_text(text, MoodState(mood="guarded"))
    soft = split_reply_text(text, MoodState(mood="happy"))

    assert reserved == [text]
    assert len(soft) > 1
