from companion_daemon.models import MoodState
from companion_daemon.relationship_events import detect_key_relationship_event
from companion_daemon.repair_curve import (
    SeriousRepairKeyEvent,
    apply_repair_curve,
    classify_repair_quality,
    serious_repair_key_event,
)
from companion_daemon.models import IncomingMessage


def test_classify_repair_quality_distinguishes_serious_and_perfunctory() -> None:
    assert classify_repair_quality("我认真道歉，以后我会注意") == "serious"
    assert classify_repair_quality("对不起") == "perfunctory"
    assert classify_repair_quality("嗯好的") is None


def test_serious_repair_applies_once_with_merged_curve() -> None:
    hurt = MoodState(
        mood="hurt",
        trust=30,
        patience=40,
        security=30,
        emotional_charge=30,
        last_interaction_event="repair_attempt",
    )

    repaired = apply_repair_curve(hurt, message_text="我认真道歉，以后我会注意")

    assert repaired.mood == "calm"
    assert repaired.trust == 34
    assert repaired.patience == 46
    assert repaired.security == 35
    assert repaired.emotional_charge == 18


def test_serious_repair_key_event_is_memory_only() -> None:
    state = MoodState(last_interaction_event="repair_attempt")

    event = serious_repair_key_event(state, "我认真道歉，以后我会注意")

    assert isinstance(event, SeriousRepairKeyEvent)
    assert serious_repair_key_event(MoodState(), "我认真道歉，以后我会注意") is None


def test_is_repair_message_recognizes_serious_apology_without_sorry() -> None:
    from companion_daemon.repair_curve import is_repair_message

    assert is_repair_message("我认真道歉，以后我会注意")
    assert is_repair_message("对不起")
    assert not is_repair_message("今天天气不错")


def test_detect_key_relationship_event_no_longer_duplicates_serious_repair() -> None:
    message = IncomingMessage(platform="qq", platform_user_id="geoff", text="我认真道歉，以后我会注意")

    assert detect_key_relationship_event(message) is None
