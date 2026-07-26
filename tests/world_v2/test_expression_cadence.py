from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.action_due_wake import ActionDueWake
from companion_daemon.world_v2.action_pump import ActionPump
from companion_daemon.world_v2.expression_cadence import (
    CADENCE_POLICY_VERSION,
    CadenceDraw,
    cadence_windows,
    record_cadence_draws,
)
from companion_daemon.world_v2.expression_draft import (
    ExpressionDraftCapabilities,
    materialize_expression_draft,
)


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


def _request(*, draw_refs: tuple[str, ...] = ()) -> ModelInput:
    return ModelInput(
        call_id="model-call:cadence:1",
        attempt_id="attempt:cadence:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="event:observation:cadence:1",
        evaluated_world_revision=4,
        model_content_json=json.dumps({"logical_time": NOW.isoformat()}),
        recorded_draw_refs=draw_refs,
        trigger_message=TriggerMessage(
            event_ref="event:observation:cadence:1",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:cadence:1",
            source_world_revision=4,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:10001",
            platform_message_id="qq-message-cadence-1",
            text="你慢慢说。",
        ),
    )


def _draw(position: int, fraction_ppm: int) -> CadenceDraw:
    return CadenceDraw(
        draw_ref=f"draw:cadence:{position}",
        beat_position=position,
        fraction_ppm=fraction_ppm,
        policy_version=CADENCE_POLICY_VERSION,
    )


@pytest.mark.parametrize(
    ("profile", "minimum", "maximum"),
    [
        ("rapid", 0.35, 1.1),
        ("conversational", 0.8, 2.5),
        ("hesitant", 2.0, 7.0),
        ("escalating", 0.45, 5.0),
    ],
)
def test_each_recorded_cadence_profile_is_deterministic_and_bounded(
    profile: str, minimum: float, maximum: float
) -> None:
    draws = (_draw(2, 0), _draw(3, 500_000), _draw(4, 1_000_000))

    first = cadence_windows(origin=NOW, profile=profile, beat_count=4, draws=draws)
    replay = cadence_windows(origin=NOW, profile=profile, beat_count=4, draws=draws)

    assert replay == first
    assert first[0] is None
    gaps = [
        (first[index][0] - (NOW if index == 1 else first[index - 1][0])).total_seconds()
        for index in range(1, 4)
    ]
    assert all(minimum <= gap <= maximum for gap in gaps)


def test_on_materialization_records_absolute_due_per_beat_and_shadow_changes_no_action_timing() -> None:
    draws = (_draw(2, 0), _draw(3, 1_000_000))
    value = {
        "timing_choice": "now",
        "cadence": "conversational",
        "beats": [
            {"modality": "text", "role": "opening", "text": "先说第一件。"},
            {"modality": "text", "role": "substantive", "text": "第二件更重要。"},
            {"modality": "text", "role": "afterthought", "text": "还有一个补充。"},
        ],
        "stance": "warm",
        "brief_rationale": "Three distinct semantic beats.",
    }
    on = materialize_expression_draft(
        value=value,
        request=_request(draw_refs=tuple(item.draw_ref for item in draws)),
        capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-on.1",
            modalities=("text",),
            recorded_cadence_mode="on",
        ),
        cadence_draws=draws,
    )
    shadow = materialize_expression_draft(
        value=value,
        request=_request(draw_refs=tuple(item.draw_ref for item in draws)),
        capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-shadow.1",
            modalities=("text",),
            recorded_cadence_mode="shadow",
        ),
        cadence_draws=draws,
    )

    assert on.action_intents[0].due_window is None
    assert on.action_intents[1].due_window[0] == NOW + timedelta(seconds=0.8)
    assert on.action_intents[2].due_window[0] == NOW + timedelta(seconds=3.3)
    assert all(item.due_window is None for item in shadow.action_intents)
    assert [item.payload_hash for item in shadow.action_intents] == [
        item.payload_hash for item in on.action_intents
    ]


@pytest.mark.parametrize("beat_count", range(2, 9))
def test_two_through_eight_beats_each_materialize_one_independent_action(
    beat_count: int,
) -> None:
    draws = tuple(
        CadenceDraw(
            draw_ref="draw:cadence:shared-vector",
            beat_position=position,
            fraction_ppm=500_000,
        )
        for position in range(2, beat_count + 1)
    )
    proposal = materialize_expression_draft(
        value={
            "timing_choice": "now",
            "cadence": "rapid",
            "beats": [
                {
                    "modality": "text",
                    "role": "opening" if position == 1 else "substantive",
                    "text": f"beat-{position}",
                }
                for position in range(1, beat_count + 1)
            ],
            "stance": "engaged",
            "brief_rationale": "Each beat has its own conversational job.",
        },
        request=_request(draw_refs=("draw:cadence:shared-vector",)),
        capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-counts.1",
            modalities=("text",),
            recorded_cadence_mode="on",
        ),
        cadence_draws=draws,
    )

    assert len(proposal.action_intents) == beat_count
    assert len({item.payload_ref for item in proposal.action_intents}) == beat_count
    assert proposal.action_intents[0].due_window is None
    assert all(item.due_window is not None for item in proposal.action_intents[1:])


def test_cadence_intent_prompt_echo_is_repaired_without_loosening_validation() -> None:
    """Production 07-24 regression: the prompt phrase "cadence intent" made the
    model emit a literal ``cadence_intent`` key, which strict validation
    rejected and every reply turn collapsed into the recovery lane.  Only this
    exact alias is repaired; any other extra key still fails closed."""

    value = {
        "timing_choice": "now",
        "cadence_intent": "conversational",
        "beats": [
            {"modality": "text", "role": "opening", "text": "刚看到你的消息。"},
            {"modality": "text", "role": "substantive", "text": "我在的。"},
        ],
        "stance": "warm",
        "brief_rationale": "Alias repair regression.",
    }
    proposal = materialize_expression_draft(
        value=value,
        request=_request(),
        capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-alias.1",
            modalities=("text",),
            recorded_cadence_mode="shadow",
        ),
    )
    assert len(proposal.action_intents) == 2

    with pytest.raises(Exception, match="unknown_extra"):
        materialize_expression_draft(
            value={**value, "unknown_extra": True},
            request=_request(),
            capabilities=ExpressionDraftCapabilities(
                profile_id="expression:test-alias.1",
                modalities=("text",),
                recorded_cadence_mode="shadow",
            ),
        )


def test_off_mode_keeps_legacy_immediate_multi_beat_behavior_exactly() -> None:
    proposal = materialize_expression_draft(
        value={
            "timing_choice": "now",
            "cadence": "hesitant",
            "beats": [
                {"modality": "text", "role": "opening", "text": "第一条。"},
                {"modality": "text", "role": "substantive", "text": "第二条。"},
            ],
            "stance": "plain",
            "brief_rationale": "Compatibility test.",
        },
        request=_request(),
        capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-off.1",
            modalities=("text",),
            recorded_cadence_mode="off",
        ),
    )

    assert all(item.due_window is None for item in proposal.action_intents)


def test_one_random_authority_record_supplies_all_subsequent_beat_fractions() -> None:
    class Authority:
        calls = 0

        def draw(self, **_kwargs):
            self.calls += 1
            return SimpleNamespace(
                draw_id="draw:cadence:vector:1",
                seed_hash="c" * 64,
                selected_candidate_ref="cadence-vector:17",
            )

    authority = Authority()
    draws = record_cadence_draws(
        authority=authority,
        attempt_id="attempt:cadence-vector:1",
        beat_count=8,
        logical_time=NOW,
        actor="agent:test",
        trace_id="trace:test",
        correlation_id="correlation:test",
    )

    assert authority.calls == 1
    assert tuple(item.beat_position for item in draws) == tuple(range(2, 9))
    assert {item.draw_ref for item in draws} == {"draw:cadence:vector:1"}


def test_fake_clock_reproduces_legacy_same_drain_flush_then_due_one_by_one() -> None:
    legacy = [
        SimpleNamespace(action_id=f"legacy:{position}", not_before=None)
        for position in range(1, 4)
    ]
    paced = [
        SimpleNamespace(action_id="paced:1", not_before=None),
        SimpleNamespace(action_id="paced:2", not_before=NOW + timedelta(seconds=1)),
        SimpleNamespace(action_id="paced:3", not_before=NOW + timedelta(seconds=3)),
    ]

    assert [
        item.action_id
        for item in legacy[:8]
        if ActionPump._is_due(action=item, logical_time=NOW)
    ] == ["legacy:1", "legacy:2", "legacy:3"]
    assert [
        item.action_id
        for item in paced[:8]
        if ActionPump._is_due(action=item, logical_time=NOW)
    ] == ["paced:1"]
    assert [
        item.action_id
        for item in paced[:8]
        if ActionPump._is_due(action=item, logical_time=NOW + timedelta(seconds=1))
    ] == ["paced:1", "paced:2"]
    assert not ActionPump._is_due(
        action=paced[2], logical_time=NOW + timedelta(seconds=1)
    )


class _FakeWallClock:
    def __init__(self) -> None:
        self.value = NOW
        self.sleepers: list[tuple[datetime, asyncio.Future[None]]] = []

    def now(self) -> datetime:
        return self.value

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.value + timedelta(seconds=seconds), future))
        await future

    async def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)
        ready = [item for item in self.sleepers if item[0] <= self.value]
        self.sleepers = [item for item in self.sleepers if item[0] > self.value]
        for _, future in ready:
            if not future.done():
                future.set_result(None)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_lightweight_wake_rebuilds_from_projection_and_fires_due_beats_in_order() -> None:
    clock = _FakeWallClock()
    actions = [
        SimpleNamespace(
            action_id="action:beat:2",
            state="scheduled",
            not_before=NOW + timedelta(seconds=1),
        ),
        SimpleNamespace(
            action_id="action:beat:3",
            state="scheduled",
            not_before=NOW + timedelta(seconds=3),
        ),
    ]
    woke: list[str] = []

    async def wake() -> None:
        due = [
            item
            for item in actions
            if item.state == "scheduled" and item.not_before <= clock.now()
        ]
        assert due
        item = due[0]
        woke.append(item.action_id)
        item.state = "delivered"

    timer = ActionDueWake(
        project=lambda: SimpleNamespace(actions=tuple(actions)),
        wake=wake,
        now=clock.now,
        sleep=clock.sleep,
        coalesce_seconds=0,
    )
    assert await timer.refresh() == NOW + timedelta(seconds=1)
    await asyncio.sleep(0)
    await clock.advance(1)
    assert woke == ["action:beat:2"]
    await asyncio.sleep(0)
    await clock.advance(2)
    assert woke == ["action:beat:2", "action:beat:3"]
    await timer.aclose()

    # A fresh process has no timer memory; projection reconstruction finds no
    # remaining due work and therefore cannot duplicate either provider call.
    restarted = ActionDueWake(
        project=lambda: SimpleNamespace(actions=tuple(actions)),
        wake=wake,
        now=clock.now,
        sleep=clock.sleep,
    )
    assert await restarted.refresh() is None
