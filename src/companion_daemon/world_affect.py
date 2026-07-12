"""Deterministic, event-sourced affect rules for the virtual world."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


AFFECT_KEYS = (
    "hurt", "anger", "sadness", "loneliness", "anxiety", "resentment", "warmth", "joy"
)
RULE_VERSION = "world-affect-v1"


@dataclass(frozen=True)
class AffectOutcome:
    vector: dict[str, int]
    mode: str
    expression: str
    charge: int
    behavior_tendency: str
    unresolved: bool
    source_appraisal: str
    reason: str
    decay_remainder_seconds: int = 0
    decay_anchor_at: str = ""
    decay_elapsed_seconds: int = 0
    source_reference: str = ""


_EFFECTS: dict[str, dict[str, int]] = {
    "boundary_violation": {"hurt": 18, "anger": 12, "sadness": 4, "anxiety": 4, "resentment": 8, "joy": -4},
    "control_pressure": {"hurt": 10, "anger": 16, "anxiety": 3, "resentment": 8, "joy": -3},
    "repair_attempt": {"hurt": -6, "anger": -8, "sadness": -3, "anxiety": -2, "resentment": -5, "warmth": 4},
    "warmth_received": {"warmth": 5, "joy": 3, "hurt": -1},
    "user_vulnerable": {"warmth": 4, "joy": 1},
    "availability_drop": {"warmth": -1},
    "return_after_gap": {"sadness": -2, "loneliness": -2, "anxiety": -2, "warmth": 2},
    "conversation_thread_expired": {"sadness": 4, "loneliness": 3, "anxiety": 5, "resentment": 2},
}

_BEHAVIOR: dict[str, tuple[str, str, str]] = {
    "boundary_violation": ("guarded", "guarded", "boundary was crossed"),
    "control_pressure": ("guarded", "guarded", "control pressure was felt"),
    "repair_attempt": ("softening", "soft", "repair was heard but not yet complete"),
    "warmth_received": ("warm", "smile", "warmth was received"),
    "user_vulnerable": ("caring", "worry", "the user needs care"),
    "availability_drop": ("patient", "neutral", "the user is temporarily busy"),
    "return_after_gap": ("open", "soft", "the user returned"),
    "conversation_thread_expired": ("patient", "neutral", "an open question went unanswered"),
}

_DECAY_PER_HOUR = {
    "hurt": 2, "anger": 3, "sadness": 1, "loneliness": 1,
    "anxiety": 2, "resentment": 1, "warmth": 1, "joy": 2,
}


def initial_affect(logical_at: str) -> dict[str, object]:
    return _state(
        vector={key: 0 for key in AFFECT_KEYS},
        mode="calm",
        expression="neutral",
        charge=0,
        behavior_tendency="neutral",
        unresolved=False,
        source_appraisal="world_started",
        source_event="world_started",
        last_changed_at=logical_at,
        decay_remainder_seconds=0,
        decay_anchor_at=logical_at,
    )


def apply_appraisal(
    current: dict[str, object],
    appraisal: str,
    logical_at: str,
    *,
    source_reference: str = "",
) -> AffectOutcome:
    vector = _vector(current)
    for key, delta in _EFFECTS.get(appraisal, {}).items():
        vector[key] = _clamp(vector[key] + delta)
    mode, expression, reason = _BEHAVIOR.get(
        appraisal,
        (
            str(current.get("mode") or "calm"),
            str(current.get("expression") or "neutral"),
            str(current.get("reason") or "ordinary interaction"),
        ),
    )
    if appraisal == "repair_attempt":
        behavior_tendency = "repair_open"
    elif appraisal == "user_vulnerable":
        # A hurt person can still choose to care when the other person is in
        # genuine distress; this does not erase the hurt vector.
        behavior_tendency = "caring"
    elif vector["hurt"] >= 30 or vector["resentment"] >= 20:
        behavior_tendency = "withdraw"
    elif _negative_total(vector) >= 8 and str(current.get("behavior_tendency") or "") in {
        "withdraw", "guarded", "patient", "repair_open"
    }:
        # An ordinary turn must not make an unresolved feeling disappear merely
        # because it did not add a new appraisal effect.
        behavior_tendency = str(current["behavior_tendency"])
    else:
        behavior_tendency = mode
    unresolved = (
        sum(vector[key] for key in ("hurt", "anger", "sadness", "loneliness", "anxiety", "resentment"))
        >= 8
    )
    charge = max(0, min(100, int(current.get("charge") or 0) + _charge_delta(appraisal)))
    return AffectOutcome(
        vector=vector,
        mode=mode,
        expression=expression,
        charge=charge,
        behavior_tendency=behavior_tendency,
        unresolved=unresolved,
        source_appraisal=appraisal,
        reason=reason,
        decay_remainder_seconds=int(current.get("decay_remainder_seconds") or 0),
        decay_anchor_at=logical_at,
        source_reference=source_reference,
    )


def decay_affect(current: dict[str, object], elapsed_seconds: int, logical_at: str) -> AffectOutcome:
    vector = _vector(current)
    accumulated_seconds = max(0, int(elapsed_seconds)) + int(current.get("decay_remainder_seconds") or 0)
    hours, remainder_seconds = divmod(accumulated_seconds, 3600)
    for key, rate in _DECAY_PER_HOUR.items():
        vector[key] = _clamp(vector[key] - rate * hours)
    negative_total = _negative_total(vector)
    unresolved = negative_total >= 8
    if unresolved:
        behavior_tendency = "withdraw" if vector["hurt"] >= 30 or vector["resentment"] >= 20 else "patient"
        mode, expression = ("guarded", "guarded") if behavior_tendency == "withdraw" else ("patient", "neutral")
    else:
        behavior_tendency, mode, expression = "neutral", "calm", "neutral"
    return AffectOutcome(
        vector=vector,
        mode=mode,
        expression=expression,
        charge=max(0, int(current.get("charge") or 0) - hours * 2),
        behavior_tendency=behavior_tendency,
        unresolved=unresolved,
        source_appraisal=str(current.get("source_appraisal") or "logical_time_decay"),
        reason="logical time decayed affect",
        decay_remainder_seconds=remainder_seconds,
        decay_anchor_at=logical_at,
        decay_elapsed_seconds=max(0, int(elapsed_seconds)),
        source_reference=str(current.get("source_reference") or ""),
    )


def outcome_payload(outcome: AffectOutcome, *, logical_at: str, event_type: str) -> dict[str, object]:
    payload = {
        "vector": dict(outcome.vector),
        "mode": outcome.mode,
        "expression": outcome.expression,
        "charge": outcome.charge,
        "behavior_tendency": outcome.behavior_tendency,
        "unresolved": outcome.unresolved,
        "source_appraisal": outcome.source_appraisal,
        "reason": outcome.reason,
        "last_changed_at": logical_at,
        "source_event": event_type,
        "event_type": event_type,
        "rule_version": RULE_VERSION,
        "source_reference": outcome.source_reference,
    }
    if event_type == "AffectDecayed":
        payload.update(
            {
                "elapsed_seconds": int(outcome.decay_elapsed_seconds),
                "decay_remainder_seconds": int(outcome.decay_remainder_seconds),
                "decay_anchor_at": outcome.decay_anchor_at or logical_at,
            }
        )
    else:
        payload.update(
            {
                "decay_remainder_seconds": int(outcome.decay_remainder_seconds),
                "decay_anchor_at": outcome.decay_anchor_at or logical_at,
            }
        )
    return payload


def affect_guidance(state: dict[str, Any]) -> str:
    behavior = str(state.get("behavior_tendency") or "neutral")
    unresolved = bool(state.get("unresolved"))
    if behavior == "withdraw":
        return "情绪还没过去：可以短一点、慢一点，说明不舒服，但不辱骂、不威胁，也不用冷处理惩罚对方。"
    if behavior == "guarded":
        return "保持边界：明确指出不舒服的地方，不辱骂、不威胁，也不假装没事。"
    if behavior == "patient" and unresolved:
        return "正在消化情绪：不主动追问，不把普通忙碌解释成背叛。"
    if behavior == "repair_open":
        return "听见了修复：可以缓和，但不要把残留情绪瞬间清零。"
    if behavior == "caring":
        return "先接住对方，不把关心变成控制。"
    return "自然表达，不凭空添加情绪原因。"


def public_mood(state: dict[str, object]) -> str:
    """Map the world affect projection to the existing transport mood enum.

    The world remains the source of truth; this is only an adapter for clients
    that still use the legacy ``CompanionReply.mood`` field and reply timing.
    """
    behavior = str(state.get("behavior_tendency") or "neutral")
    mode = str(state.get("mode") or "calm")
    expression = str(state.get("expression") or "neutral")
    if behavior == "withdraw":
        return "sulking"
    if behavior in {"guarded", "repair_open"} or mode == "guarded":
        return "guarded"
    if behavior == "patient" and bool(state.get("unresolved")):
        return "hurt"
    if behavior == "caring" or expression == "worry":
        return "worried"
    if behavior in {"warm", "open"} or expression == "smile":
        return "happy"
    return "calm"


def _state(**values: object) -> dict[str, object]:
    return values


def _vector(current: dict[str, object]) -> dict[str, int]:
    raw = current.get("vector")
    return {
        key: _clamp(int(raw.get(key, 0))) if isinstance(raw, dict) else 0
        for key in AFFECT_KEYS
    }


def _charge_delta(appraisal: str) -> int:
    return {
        "boundary_violation": 16,
        "control_pressure": 11,
        "repair_attempt": -5,
        "warmth_received": 5,
        "user_vulnerable": 7,
        "availability_drop": -2,
        "return_after_gap": 3,
        "conversation_thread_expired": 3,
    }.get(appraisal, -1)


def _clamp(value: int) -> int:
    return max(0, min(100, value))


def _negative_total(vector: dict[str, int]) -> int:
    return sum(vector[key] for key in ("hurt", "anger", "sadness", "loneliness", "anxiety", "resentment"))
