"""Phone attention: a pure, model-safe reading of her presence at the phone.

The v1 daemon carried an explicit ``PhoneAttention`` runtime state (away /
notified / glanced / reading / typing / do_not_disturb) that made "she is a
person with her own attention" legible to every reply decision.  World v2
never rebuilt that organ, so production answered 2 a.m. messages with the
same instant presence as a lazy Sunday afternoon.

This module restores the concept exactly where the glossary allows it: a
*projection level* derivation (like ``change_phase_view``) over material that
is already accepted authority — active Plans (her current activity), the
pinned Logical Time localized by the deployment chronology, and active Affect
episodes.  Nothing here writes World truth, schedules anything, or vetoes a
reply; the reading enters deliberation only through the ordinary
Inner-Advisory envelope, where the expression model may weigh it when it
chooses ``timing_choice`` (now / later / silent).

The state vocabulary is deliberately the v1 set minus ``typing`` (an output
state of a turn in flight, not a derivable disposition):

* ``away``           — 手机不在注意范围：睡着了，或深夜没有任何醒着的证据；
* ``notified``       — 专注活动中：通知知道了，但多半要忙完这一段才看；
* ``glanced``        — 在忙别的或白天空档：能瞥一眼，回不回看心情；
* ``reading``        — 正在手机上（或晚间空闲惯性刷手机）：消息即刻可见；
* ``do_not_disturb`` — 情绪性不想理手机：看到了也可能先放着。这是她的
  情绪权利，不是故障。

An accepted *active* Plan is stronger evidence than the clock: a 2 a.m.
``study.essay_writing`` head proves she is awake, so the deep-night bias
yields to it.  Conversely being asleep outranks any mood: an ``away`` sleeper
is not "refusing" the phone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Literal

from .local_chronology import LocalChronology
from .mood_view import active_mood_intensities
from .schema_core import FrozenModel


ATTENTION_VIEW_VERSION = "attention-view.1"

PhoneAttentionState = Literal[
    "away", "notified", "glanced", "reading", "do_not_disturb"
]

# Local-civil hours in which an idle companion is presumed asleep.  This is
# the strong "深夜" bias only; an active non-sleep Plan overrides it.
_DEEP_NIGHT_START_HOUR = 1
_DEEP_NIGHT_END_HOUR = 7

# Idle evening / lunch-break hours lean toward "already on the phone".
_IDLE_PHONE_HOURS = frozenset({12, 13, 18, 19, 20, 21, 22, 23, 0})

# Activity-kind domains, mirroring the reviewed life-seed catalog.  Prefix
# matching keeps a new seed of the same domain classified without touching
# this view.
_SLEEP_PREFIX = "sleep."
_FOCUS_PREFIXES = ("study.",)
# Away-from-phone social engagements where a notification is felt but not
# read: an in-person meetup/outing, or an ongoing voice call.
_FOCUS_SOCIAL_KINDS = frozenset(
    {
        "social.literature_club_meetup",
        "social.exhibition_outing",
        "social.family_call",
    }
)
_PHONE_ADJACENT_KINDS = frozenset({"leisure.digital_browse"})

# Feeling dimensions whose strong presence reads as "不想理手机" — wanting
# space from the conversation rather than from the world.  Approach moods
# (warmth/joy) never produce do_not_disturb.
_WITHDRAWAL_DIMENSIONS = ("anger", "resentment", "hurt")
_WITHDRAWAL_FLOOR_BP = 6_000

_ACTIVITY_LABELS = {
    "study.": "学习",
    "sleep.": "休息",
    "leisure.digital_browse": "刷手机",
    "leisure.": "自己的休闲",
    "social.literature_club_meetup": "文学社的线下活动",
    "social.exhibition_outing": "看展",
    "social.family_call": "和家里通话",
    "social.": "和别人相处",
    "commute.": "路上",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _activity_label(activity_kind: str) -> str:
    exact = _ACTIVITY_LABELS.get(activity_kind)
    if exact is not None:
        return exact
    for prefix, label in _ACTIVITY_LABELS.items():
        if prefix.endswith(".") and activity_kind.startswith(prefix):
            return label
    return "手头的事"


class PhoneAttentionReading(FrozenModel):
    """One sourced disposition toward the phone at the pinned Logical Time.

    ``source_event_refs`` name only committed events (plan authority origins,
    affect episode origins, the current clock transition), so an advisory
    built on it stays ledger-backed.
    """

    state: PhoneAttentionState
    prose: str
    # Why this state was chosen; a bounded diagnostic token, never a rule.
    derivation: Literal[
        "sleep_plan",
        "deep_night_idle",
        "withdrawal_affect",
        "focus_plan",
        "phone_plan",
        "engaged_plan",
        "idle_phone_hours",
        "idle_daytime",
    ]
    local_hour: int
    source_event_refs: tuple[str, ...]


_STATE_LABELS: dict[PhoneAttentionState, str] = {
    "away": "不在手机旁",
    "notified": "专注中",
    "glanced": "偶尔瞥一眼",
    "reading": "正在手机上",
    "do_not_disturb": "不想理手机",
}


def _clock_source_ref(projection) -> str | None:
    """The committed clock event that testifies to the pinned logical time."""

    logical_time = getattr(projection, "logical_time", None)
    if not isinstance(logical_time, datetime):
        return None
    matching = [
        item
        for item in getattr(projection, "clock_transition_history", ())
        if getattr(item, "logical_time_to", None) == logical_time
    ]
    if not matching:
        return None
    latest = max(matching, key=lambda item: getattr(item, "computed_world_revision", 0))
    ref = getattr(latest, "clock_event_ref", None)
    return str(ref) if ref else None


def _active_plan_heads(projection) -> tuple[tuple[str, str], ...]:
    """(activity_kind, accepted_event_ref) for every currently active Plan."""

    heads: list[tuple[str, str]] = []
    for plan in getattr(projection, "plans", ()):
        if getattr(plan, "status", None) != "active":
            continue
        kind = str(getattr(plan, "activity_kind", "") or "")
        origin = getattr(plan, "authority_origin", None)
        ref = str(getattr(origin, "accepted_event_ref", "") or "")
        if kind and ref:
            heads.append((kind, ref))
    return tuple(heads)


def _withdrawal_material(projection) -> tuple[int, tuple[str, ...]]:
    """Strongest active withdrawal feeling and its committed episode refs."""

    intensities = active_mood_intensities(tuple(getattr(projection, "affect_episodes", ())))
    strongest = max(
        (intensities.get(dimension, 0) for dimension in _WITHDRAWAL_DIMENSIONS),
        default=0,
    )
    if strongest < _WITHDRAWAL_FLOOR_BP:
        return 0, ()
    refs: list[str] = []
    for episode in getattr(projection, "affect_episodes", ()):
        if getattr(episode, "status", None) != "active":
            continue
        components = getattr(episode, "components", ())
        if not any(
            str(getattr(component, "dimension", "")) in _WITHDRAWAL_DIMENSIONS
            and int(getattr(component, "intensity_bp", 0)) >= _WITHDRAWAL_FLOOR_BP
            for component in components
        ):
            continue
        origin = getattr(episode, "origin", None)
        ref = str(getattr(origin, "accepted_event_ref", "") or "")
        if ref:
            refs.append(ref)
    return strongest, tuple(dict.fromkeys(refs))


def phone_attention_reading(
    projection, *, chronology: LocalChronology | None = None
) -> PhoneAttentionReading | None:
    """Derive her current phone attention from the pinned projection.

    Pure over accepted material: the same projection and chronology always
    yield the same reading.  Returns ``None`` when the projection has no
    Logical Time (an unstarted world has no "now" to be present in).
    """

    logical_time = getattr(projection, "logical_time", None)
    if not isinstance(logical_time, datetime):
        return None
    local_time = (chronology or LocalChronology()).localize(logical_time)
    assert local_time is not None
    hour = local_time.hour
    deep_night = _DEEP_NIGHT_START_HOUR <= hour < _DEEP_NIGHT_END_HOUR

    heads = _active_plan_heads(projection)
    clock_ref = _clock_source_ref(projection)
    sleep_refs = tuple(ref for kind, ref in heads if kind.startswith(_SLEEP_PREFIX))
    awake_heads = tuple(
        (kind, ref) for kind, ref in heads if not kind.startswith(_SLEEP_PREFIX)
    )

    def reading(
        state: PhoneAttentionState,
        derivation: str,
        prose: str,
        refs: tuple[str, ...],
    ) -> PhoneAttentionReading:
        with_clock = (*refs, clock_ref) if clock_ref else refs
        source_refs = tuple(dict.fromkeys(ref for ref in with_clock if ref))
        return PhoneAttentionReading(
            state=state,
            prose=prose,
            derivation=derivation,  # type: ignore[arg-type]
            local_hour=hour,
            source_event_refs=source_refs,
        )

    # 1. Asleep outranks everything: a sleeper is not choosing anything.
    if sleep_refs:
        if deep_night:
            prose = "深夜，她已经睡下了，手机静音放在一边；消息要等她醒来才会看到。"
        elif hour >= 22 or hour < _DEEP_NIGHT_START_HOUR:
            prose = "她在收尾准备睡了，手机放在床头；今晚大概率不会再认真看消息。"
        else:
            prose = "她正在休息补觉，手机放在一边；消息要等她醒来才会看到。"
        return reading("away", "sleep_plan", prose, sleep_refs)

    # 2. Deep night with no committed evidence of being awake: asleep.
    if deep_night and not awake_heads:
        return reading(
            "away",
            "deep_night_idle",
            "深夜，她多半已经睡着了，手机不在注意范围里；消息要等她早上醒来才会看到。",
            (),
        )

    # 3. An awake companion carrying a strong withdrawal feeling may simply
    #    not want the phone right now.  Her right, not a malfunction.
    withdrawal, withdrawal_refs = _withdrawal_material(projection)
    if withdrawal >= _WITHDRAWAL_FLOOR_BP:
        return reading(
            "do_not_disturb",
            "withdrawal_affect",
            "她现在情绪上不太想理手机，看到通知也可能先放着，等自己缓过来再说。",
            withdrawal_refs,
        )

    # 4. Current activity, from strongest phone-distance to weakest.
    focus_heads = tuple(
        (kind, ref)
        for kind, ref in awake_heads
        if kind.startswith(_FOCUS_PREFIXES) or kind in _FOCUS_SOCIAL_KINDS
    )
    if focus_heads:
        label = _activity_label(focus_heads[0][0])
        return reading(
            "notified",
            "focus_plan",
            f"她正专注在{label}里，手机扣在旁边；通知知道有消息，"
            "但多半要忙完这一段才会点开看。",
            tuple(ref for _kind, ref in focus_heads),
        )
    phone_heads = tuple((kind, ref) for kind, ref in awake_heads if kind in _PHONE_ADJACENT_KINDS)
    if phone_heads:
        return reading(
            "reading",
            "phone_plan",
            "她这会儿正好在刷手机，消息一来就能看到。",
            tuple(ref for _kind, ref in phone_heads),
        )
    if awake_heads:
        label = _activity_label(awake_heads[0][0])
        return reading(
            "glanced",
            "engaged_plan",
            f"她在{label}，手机在旁边，偶尔瞥一眼屏幕；能看到消息，回不回看她当下的节奏。",
            tuple(ref for _kind, ref in awake_heads),
        )

    # 5. No active Plan: an idle gap, disposed by the local hour.
    if hour in _IDLE_PHONE_HOURS:
        return reading(
            "reading",
            "idle_phone_hours",
            "她现在闲着，这个点多半正拿着手机，消息一来就能看到。",
            (),
        )
    return reading(
        "glanced",
        "idle_daytime",
        "她现在没有特别的安排，手机在手边，看到消息不难；回不回、多快回都随她当下的状态。",
        (),
    )


def phone_attention_prose(reading: PhoneAttentionReading) -> str:
    """One bounded, model-safe Chinese line: state label plus situation."""

    return f"【手机注意力：{_STATE_LABELS[reading.state]}】{reading.prose}"[:256]


def phone_attention_advisories(
    projection, *, chronology: LocalChronology | None = None
) -> tuple:
    """Wrap the reading in the ordinary non-authoritative advisory envelope.

    Mirrors ``change_phase_advisories``: the capsule import stays local, the
    advisory is dropped when no committed source event can back it, and the
    envelope carries prose only — never a rule, a schedule, or a veto.
    """

    from .context_capsule import InnerAdvisoryCandidate, InnerAdvisoryProjection

    logical_time = getattr(projection, "logical_time", None)
    if not isinstance(logical_time, datetime):
        return ()
    view = phone_attention_reading(projection, chronology=chronology)
    if view is None or not view.source_event_refs:
        return ()
    candidate_ref = "phone-attention:" + _digest(
        {"state": view.state, "derivation": view.derivation}
    )
    candidate = InnerAdvisoryCandidate(
        candidate_ref=candidate_ref,
        value=phone_attention_prose(view),
        weight_bp=10_000,
        confidence_bp=10_000,
    )
    return (
        InnerAdvisoryProjection(
            advisory_id="advisory:phone-attention:" + _digest(
                {"state": view.state, "derivation": view.derivation, "hour": view.local_hour}
            ),
            kind="phone_attention",
            source_refs=view.source_event_refs,
            candidate_refs=(candidate_ref,),
            candidates=(candidate,),
            # Same eviction rank as the Change Phase texture: under extreme
            # capsule budget pressure this presence reading must be evicted
            # before the continuity floors it sits beside.
            confidence_bp=6_000,
            # Attention is the most transient of the advisory textures; it is
            # anchored to the pinned durable head, not wall-clock time.
            expiry=logical_time + timedelta(hours=2),
            producer_version=ATTENTION_VIEW_VERSION,
        ),
    )


__all__ = [
    "ATTENTION_VIEW_VERSION",
    "PhoneAttentionReading",
    "PhoneAttentionState",
    "phone_attention_advisories",
    "phone_attention_prose",
    "phone_attention_reading",
]
