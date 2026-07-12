"""Deterministic, event-sourced affect rules for the virtual world."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from companion_daemon.world_affinity import personality_affect_baseline


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
    repair_quality: str = ""
    repair_observation_seconds: int = 0
    repair_streak: int = 0
    violation_count: int = 0


_EFFECTS: dict[str, dict[str, int]] = {
    "reply_discomfort": {"hurt": 4, "anger": 2},
    "boundary_violation": {"hurt": 18, "anger": 12, "sadness": 4, "anxiety": 4, "resentment": 8, "joy": -4},
    "control_pressure": {"hurt": 10, "anger": 16, "anxiety": 3, "resentment": 8, "joy": -3},
    "repair_attempt": {"hurt": -6, "anger": -8, "sadness": -3, "anxiety": -2, "resentment": -5, "warmth": 4},
    "repair_perfunctory": {"hurt": -1, "anger": -1},
    "repair_specific": {"hurt": -9, "anger": -9, "sadness": -4, "anxiety": -3, "resentment": -6, "warmth": 3},
    "repair_restitution": {"hurt": -12, "anger": -12, "sadness": -6, "anxiety": -4, "resentment": -8, "warmth": 6},
    "repeated_violation": {"hurt": 24, "anger": 18, "sadness": 7, "anxiety": 7, "resentment": 14, "joy": -6},
    "warmth_received": {"warmth": 5, "joy": 3, "hurt": -1},
    "user_vulnerable": {"warmth": 4, "joy": 1},
    "availability_drop": {"warmth": -1},
    "return_after_gap": {"sadness": -2, "loneliness": -2, "anxiety": -2, "warmth": 2},
    "conversation_thread_expired": {"sadness": 4, "loneliness": 3, "anxiety": 5, "resentment": 2},
}

_BEHAVIOR: dict[str, tuple[str, str, str]] = {
    "reply_discomfort": ("guarded", "guarded", "the current message caused discomfort"),
    "boundary_violation": ("guarded", "guarded", "boundary was crossed"),
    "control_pressure": ("guarded", "guarded", "control pressure was felt"),
    "repair_attempt": ("softening", "soft", "repair was heard but not yet complete"),
    "repair_perfunctory": ("guarded", "neutral", "a bare apology was heard but needs evidence"),
    "repair_specific": ("softening", "soft", "a specific apology began an observation period"),
    "repair_restitution": ("softening", "soft", "corrective action began an observation period"),
    "repeated_violation": ("guarded", "guarded", "a boundary was crossed again during repair"),
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


def initial_affect(logical_at: str, *, protagonist: object = None) -> dict[str, object]:
    baseline = personality_affect_baseline(protagonist)
    return _state(
        vector=dict(baseline),
        personality_baseline=baseline,
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
    repair_observation_seconds = int(current.get("repair_observation_seconds") or 0)
    violation_count = int(current.get("violation_count") or 0)
    repair_streak = int(current.get("repair_streak") or 0)
    repair_quality = str(current.get("repair_quality") or "")
    if appraisal in {"boundary_violation", "control_pressure"} and (
        repair_observation_seconds > 0 or repair_streak > 0
    ):
        appraisal = "repeated_violation"
    vector = _vector(current)
    baseline = _baseline(current)
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
    if appraisal in {"repair_attempt", "repair_perfunctory", "repair_specific", "repair_restitution"}:
        behavior_tendency = "repair_open"
    elif appraisal == "user_vulnerable":
        # A hurt person can still choose to care when the other person is in
        # genuine distress; this does not erase the hurt vector.
        behavior_tendency = "caring"
    elif vector["hurt"] - baseline["hurt"] >= 30 or vector["resentment"] - baseline["resentment"] >= 20:
        behavior_tendency = "withdraw"
    elif _negative_total(vector, baseline) >= 8 and str(current.get("behavior_tendency") or "") in {
        "withdraw", "guarded", "patient", "repair_open"
    }:
        # An ordinary turn must not make an unresolved feeling disappear merely
        # because it did not add a new appraisal effect.
        behavior_tendency = str(current["behavior_tendency"])
    else:
        behavior_tendency = mode
    unresolved = (
        _negative_total(vector, baseline) >= 8
    )
    if appraisal in {"boundary_violation", "control_pressure", "repeated_violation"}:
        violation_count += 1
        repair_streak = 0
        repair_quality = ""
        repair_observation_seconds = 0
    elif appraisal == "repair_perfunctory":
        repair_quality = "perfunctory"
        repair_streak = 0
        repair_observation_seconds = max(repair_observation_seconds, 36 * 3600)
        behavior_tendency = "repair_observing"
    elif appraisal == "repair_attempt":
        # Compatibility for old event ledgers. New observations classify a
        # concrete quality before they enter the world.
        repair_quality = ""
    elif appraisal == "repair_specific":
        repair_quality = "specific"
        repair_streak += 1
        repair_observation_seconds = 24 * 3600
        behavior_tendency = "repair_observing"
    elif appraisal == "repair_restitution":
        repair_quality = "restitution"
        repair_streak += 2
        repair_observation_seconds = 12 * 3600
        behavior_tendency = "repair_observing"
    if repair_observation_seconds > 0:
        unresolved = True
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
        repair_quality=repair_quality,
        repair_observation_seconds=repair_observation_seconds,
        repair_streak=repair_streak,
        violation_count=violation_count,
    )


def decay_affect(current: dict[str, object], elapsed_seconds: int, logical_at: str) -> AffectOutcome:
    vector = _vector(current)
    baseline = _baseline(current)
    accumulated_seconds = max(0, int(elapsed_seconds)) + int(current.get("decay_remainder_seconds") or 0)
    hours, remainder_seconds = divmod(accumulated_seconds, 3600)
    for key, rate in _DECAY_PER_HOUR.items():
        target = baseline[key]
        if vector[key] > target:
            vector[key] = max(target, vector[key] - rate * hours)
        elif vector[key] < target:
            vector[key] = min(target, vector[key] + rate * hours)
    negative_total = _negative_total(vector, baseline)
    unresolved = negative_total >= 8
    repair_observation_seconds = max(
        0, int(current.get("repair_observation_seconds") or 0) - max(0, int(elapsed_seconds))
    )
    repair_quality = str(current.get("repair_quality") or "") if repair_observation_seconds else ""
    if repair_observation_seconds:
        unresolved = True
        behavior_tendency, mode, expression = "repair_observing", "softening", "soft"
    elif unresolved:
        behavior_tendency = (
            "withdraw"
            if vector["hurt"] - baseline["hurt"] >= 30
            or vector["resentment"] - baseline["resentment"] >= 20
            else "patient"
        )
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
        repair_quality=repair_quality,
        repair_observation_seconds=repair_observation_seconds,
        repair_streak=int(current.get("repair_streak") or 0),
        violation_count=int(current.get("violation_count") or 0),
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
        "repair_quality": outcome.repair_quality,
        "repair_observation_seconds": outcome.repair_observation_seconds,
        "repair_streak": outcome.repair_streak,
        "violation_count": outcome.violation_count,
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
    if behavior == "repair_observing":
        return "修复正在观察期：承认对方的具体行动，但让信任按后续一致行为逐步恢复。"
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
    if behavior in {"guarded", "repair_open", "repair_observing"} or mode == "guarded":
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


def _baseline(current: dict[str, object]) -> dict[str, int]:
    raw = current.get("personality_baseline")
    return {
        key: _clamp(int(raw.get(key, 0))) if isinstance(raw, dict) else 0
        for key in AFFECT_KEYS
    }


def _charge_delta(appraisal: str) -> int:
    return {
        "boundary_violation": 16,
        "control_pressure": 11,
        "repair_attempt": -5,
        "repair_perfunctory": -1,
        "repair_specific": -4,
        "repair_restitution": -7,
        "repeated_violation": 22,
        "warmth_received": 5,
        "user_vulnerable": 7,
        "availability_drop": -2,
        "return_after_gap": 3,
        "conversation_thread_expired": 3,
    }.get(appraisal, -1)


def _clamp(value: int) -> int:
    return max(0, min(100, value))


def _negative_total(vector: dict[str, int], baseline: dict[str, int]) -> int:
    return sum(
        max(0, vector[key] - baseline[key])
        for key in ("hurt", "anger", "sadness", "loneliness", "anxiety", "resentment")
    )
