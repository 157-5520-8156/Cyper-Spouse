from companion_daemon.world_v2.no_world_evidence_recovery import (
    classify_world_probe_intent,
    recover_without_world_evidence,
)


def test_current_probe_recovery_keeps_a_real_conversational_presence() -> None:
    text = recover_without_world_evidence(
        trigger_text="你现在在干什么？",
        source_ref="observation:test:current-presence",
        recent_visible_texts=(),
    )

    assert classify_world_probe_intent("你现在在干什么？") == "current"
    assert text
    assert "没有数据" not in text
    assert any(marker in text for marker in ("回你", "注意力", "听你", "和你说话"))


def test_current_probe_recovery_avoids_repeating_recent_presence_phrase() -> None:
    first = recover_without_world_evidence(
        trigger_text="你现在在干什么？",
        source_ref="observation:test:current-presence",
        recent_visible_texts=(),
    )
    second = recover_without_world_evidence(
        trigger_text="你现在在干什么？",
        source_ref="observation:test:current-presence",
        recent_visible_texts=(first,),
    )

    assert second != first

