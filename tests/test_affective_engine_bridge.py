from companion_daemon.affective_advisory import (
    AffectAdvisory,
    AffectiveAdvisoryEngine,
    ExpressionAffordance,
    SelectedAffordance,
)
from companion_daemon.engine import CompanionEngine
from companion_daemon.models import IncomingMessage
from companion_daemon.turn_frame import TurnFrame


def _frame(
    *,
    text: str,
    message_id: str,
) -> TurnFrame:
    return TurnFrame(
        world_id="zhizhi-v1",
        revision=42,
        state_hash="state",
        user_id="user:geoff",
        input_message_id=message_id,
        recent_messages=(),
        scene={},
        relationship={"stage": "stranger"},
        affect={},
        user_affect={},
        private_impressions=(),
        private_commitments=(),
        facts=(),
        experiences=(),
        open_threads=(),
        open_actions=(),
        capability={"current_text": text},
        dependency_tokens=(),
    )


def _advisory_with_selected(kind: str) -> AffectAdvisory:
    selected = ExpressionAffordance(kind, 1.0, "test")
    return AffectAdvisory(
        readings=(),
        drive_deltas={},
        expression_affordances=(selected,),
        persistence_candidates=(),
        confidence=0,
        evidence_spans=(),
        adapter="test",
        selected_affordance=SelectedAffordance(selected, (selected,), "seed"),
    )


def test_selected_affordance_shapes_expression_choreography() -> None:
    soft = _advisory_with_selected("soft_repair")
    parts, delays = CompanionEngine._apply_affective_expression_choreography(
        "我刚才确实接得急了。你可以慢慢说。",
        ["我刚才确实接得急了。", "你可以慢慢说。"],
        [],
        affective_advisory=soft,
    )

    assert parts == ["我刚才确实接得急了。", "你可以慢慢说。"]
    assert delays == [0, 650]

    withdrawn = _advisory_with_selected("withdraw_slightly")
    parts, delays = CompanionEngine._apply_affective_expression_choreography(
        "我不接受这种说法。先到这里。",
        ["我不接受这种说法。", "先到这里。"],
        [],
        affective_advisory=withdrawn,
    )

    assert parts == ["我不接受这种说法。先到这里。"]
    assert delays == [0]


def test_strong_quote_bound_advisory_can_promote_to_user_affect_but_mild_cannot() -> None:
    strong = _run_advisory("你刚才有点敷衍。", message_id="m:strong")

    promoted = CompanionEngine._material_user_affect_from_advisory(
        message=IncomingMessage(
            platform="qq",
            platform_user_id="geoff",
            message_id="m:strong",
            text="你刚才有点敷衍。",
        ),
        affective_advisory=strong,
        existing_user_affect=None,
    )

    assert promoted is not None
    assert promoted["kind"] == "disappointment"
    assert promoted["intensity"] == 3
    assert promoted["evidence_spans"] == ["敷衍"]

    mild = _run_advisory("算了吧", message_id="m:mild")
    assert (
        CompanionEngine._material_user_affect_from_advisory(
            message=IncomingMessage(
                platform="qq",
                platform_user_id="geoff",
                message_id="m:mild",
                text="算了吧",
            ),
            affective_advisory=mild,
            existing_user_affect=None,
        )
        is None
    )


def _run_advisory(text: str, *, message_id: str) -> AffectAdvisory:
    import asyncio

    return asyncio.run(AffectiveAdvisoryEngine().advise(_frame(text=text, message_id=message_id)))
