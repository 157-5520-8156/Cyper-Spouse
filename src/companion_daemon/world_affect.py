"""Deterministic, event-sourced affect rules for the virtual world."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import pow
from typing import Any

from companion_daemon.world_affinity import personality_affect_baseline
from companion_daemon.world_interaction_rules import HARMFUL_INTERACTION_APPRAISALS


AFFECT_KEYS = (
    "hurt", "anger", "sadness", "loneliness", "anxiety", "resentment", "warmth", "joy"
)
RULE_VERSION = "world-affect-v1"
DEFAULT_AFFECT_PROFILE: dict[str, object] = {
    "version": "affect-profile-v1",
    "negative_half_life_hours": 18,
    "positive_half_life_hours": 10,
    "warmth_half_life_hours": 6,
    "repair_evidence_required": 2,
    "spillover_leakage_cap": 25,
    "resentment_half_life_gain_hours": 2,
    "resentment_intensity_gain": 3,
}


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
    repair_evidence_count: int = 0
    core_affect: dict[str, object] | None = None
    active_episodes: tuple[dict[str, object], ...] = ()
    profile: dict[str, object] | None = None


_EFFECTS: dict[str, dict[str, int]] = {
    "reply_discomfort": {"hurt": 4, "anger": 2},
    "boundary_violation": {"hurt": 18, "anger": 12, "sadness": 4, "anxiety": 4, "resentment": 8, "joy": -4},
    "sexual_boundary_violation": {"hurt": 23, "anger": 25, "sadness": 8, "anxiety": 14, "resentment": 16, "warmth": -8, "joy": -8},
    "dehumanization": {"hurt": 16, "anger": 15, "sadness": 7, "anxiety": 5, "resentment": 11, "warmth": -6, "joy": -5},
    "coercion": {"hurt": 14, "anger": 20, "sadness": 4, "anxiety": 9, "resentment": 13, "warmth": -5, "joy": -5},
    "control_pressure": {"hurt": 10, "anger": 16, "anxiety": 3, "resentment": 8, "joy": -3},
    "repair_attempt": {"hurt": -6, "anger": -8, "sadness": -3, "anxiety": -2, "resentment": -5, "warmth": 4},
    "repair_perfunctory": {"hurt": -1, "anger": -1},
    "repair_specific": {"hurt": -9, "anger": -9, "sadness": -4, "anxiety": -3, "resentment": -6, "warmth": 3},
    "boundary_respected": {"hurt": -3, "anger": -2, "anxiety": -2, "resentment": -2, "warmth": 1},
    "repair_restitution": {"hurt": -12, "anger": -12, "sadness": -6, "anxiety": -4, "resentment": -8, "warmth": 6},
    "repeated_violation": {"hurt": 24, "anger": 18, "sadness": 7, "anxiety": 7, "resentment": 14, "joy": -6},
    "warmth_received": {"warmth": 5, "joy": 3, "hurt": -1},
    "user_vulnerable": {"warmth": 4, "joy": 1},
    "availability_drop": {"warmth": -1},
    "return_after_gap": {"sadness": -2, "loneliness": -2, "anxiety": -2, "warmth": 2},
    "conversation_thread_expired": {"sadness": 4, "loneliness": 3, "anxiety": 5, "resentment": 2},
    "npc_conflict": {"hurt": 10, "anger": 14, "sadness": 3, "anxiety": 6, "resentment": 6, "warmth": -4, "joy": -2},
    "social_warmth": {"warmth": 7, "joy": 4, "loneliness": -3, "anxiety": -1},
    "family_connection": {"warmth": 9, "joy": 4, "loneliness": -5, "anxiety": -2},
    "goal_progress": {"warmth": 2, "joy": 5, "anxiety": -3},
    "goal_completed": {"warmth": 5, "joy": 12, "anxiety": -7, "sadness": -2},
    "creative_satisfaction": {"warmth": 3, "joy": 7, "anxiety": -3},
    "restorative_solitude": {"anger": -3, "sadness": -2, "anxiety": -5, "resentment": -1},
    "goal_strain": {"anger": 3, "sadness": 2, "anxiety": 7, "joy": -2},
}

_BEHAVIOR: dict[str, tuple[str, str, str]] = {
    "reply_discomfort": ("guarded", "guarded", "the current message caused discomfort"),
    "boundary_violation": ("guarded", "guarded", "boundary was crossed"),
    "sexual_boundary_violation": ("guarded", "guarded", "a sexual or privacy boundary was crossed"),
    "dehumanization": ("guarded", "guarded", "the character was dehumanized or objectified"),
    "coercion": ("guarded", "guarded", "the interaction attempted to coerce obedience"),
    "control_pressure": ("guarded", "guarded", "control pressure was felt"),
    "repair_attempt": ("softening", "soft", "repair was heard but not yet complete"),
    "repair_perfunctory": ("guarded", "neutral", "a bare apology was heard but needs evidence"),
    "repair_specific": ("softening", "soft", "a specific apology began an observation period"),
    "boundary_respected": ("softening", "soft", "a later action respected the stated boundary"),
    "repair_restitution": ("softening", "soft", "corrective action began an observation period"),
    "repeated_violation": ("guarded", "guarded", "a boundary was crossed again during repair"),
    "warmth_received": ("warm", "smile", "warmth was received"),
    "user_vulnerable": ("caring", "worry", "the user needs care"),
    "availability_drop": ("patient", "neutral", "the user is temporarily busy"),
    "return_after_gap": ("open", "soft", "the user returned"),
    "conversation_thread_expired": ("patient", "neutral", "an open question went unanswered"),
    "npc_conflict": ("guarded", "guarded", "an NPC interaction caused friction"),
    "social_warmth": ("warm", "smile", "a settled social interaction felt warm"),
    "family_connection": ("warm", "soft", "settled family contact felt grounding"),
    "goal_progress": ("steady", "soft", "a meaningful goal moved forward"),
    "goal_completed": ("proud", "smile", "a meaningful goal was completed"),
    "creative_satisfaction": ("absorbed", "smile", "creative work felt satisfying"),
    "restorative_solitude": ("calm", "neutral", "quiet time helped the character settle"),
    "goal_strain": ("tense", "guarded", "goal pressure remained after the activity"),
}

_DECAY_PER_HOUR = {
    "hurt": 2, "anger": 3, "sadness": 1, "loneliness": 1,
    "anxiety": 2, "resentment": 1, "warmth": 1, "joy": 2,
}


def initial_affect(
    logical_at: str,
    *,
    protagonist: object = None,
    profile: object = None,
) -> dict[str, object]:
    baseline = personality_affect_baseline(protagonist)
    affect_profile = _profile(profile)
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
        core_affect=_core_affect(baseline, baseline, ()),
        active_episodes=(),
        repair_evidence_count=0,
        profile=affect_profile,
    )


def apply_appraisal(
    current: dict[str, object],
    appraisal: str,
    logical_at: str,
    *,
    source_reference: str = "",
    intensity: int | None = None,
    target: str = "general",
    appraisal_dimensions: dict[str, object] | None = None,
    relationship_residue: dict[str, object] | None = None,
) -> AffectOutcome:
    repair_observation_seconds = int(current.get("repair_observation_seconds") or 0)
    violation_count = int(current.get("violation_count") or 0)
    repair_streak = int(current.get("repair_streak") or 0)
    repair_quality = str(current.get("repair_quality") or "")
    repair_evidence_count = int(current.get("repair_evidence_count") or 0)
    profile = _profile(current.get("profile"))
    if appraisal in {"boundary_violation", "control_pressure"} and (
        repair_observation_seconds > 0 or repair_streak > 0
    ):
        appraisal = "repeated_violation"
    vector = _vector(current)
    baseline = _baseline(current)
    has_affect_effect = appraisal in _EFFECTS
    for key, delta in _EFFECTS.get(appraisal, {}).items():
        scaled = delta
        if intensity is not None:
            raw_intensity = int(intensity)
            if 1 <= raw_intensity <= 4:
                scaled = int(round(delta * {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.25}[raw_intensity]))
            else:
                bounded_intensity = max(1, min(100, raw_intensity))
                scaled = int(round(delta * (50 + bounded_intensity) / 100))
        vector[key] = _clamp(vector[key] + scaled)
    raw_episodes = current.get("active_episodes", ())
    episodes = (
        tuple(
            dict(item)
            for item in raw_episodes
            if isinstance(item, dict) and item.get("status") != "resolved"
        )
        if isinstance(raw_episodes, (list, tuple))
        else ()
    )
    if has_affect_effect:
        episodes = (
            *episodes,
            _new_episode(
                appraisal=appraisal,
                logical_at=logical_at,
                source_reference=source_reference,
                target=target,
                intensity=intensity,
                dimensions=appraisal_dimensions or {},
                relationship_residue=relationship_residue or {},
                profile=profile,
            ),
        )[-16:]
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
    if appraisal in HARMFUL_INTERACTION_APPRAISALS:
        violation_count += 1
        repair_streak = 0
        repair_quality = ""
        repair_observation_seconds = 0
        repair_evidence_count = 0
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
        repair_evidence_count = 0
        behavior_tendency = "repair_observing"
    elif appraisal == "repair_restitution":
        repair_quality = "restitution"
        repair_streak += 2
        repair_observation_seconds = 12 * 3600
        repair_evidence_count = 0
        behavior_tendency = "repair_observing"
    elif repair_observation_seconds > 0 and appraisal == "boundary_respected":
        repair_evidence_count = min(6, repair_evidence_count + 1)
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
        source_appraisal=(
            appraisal
            if has_affect_effect
            else str(current.get("source_appraisal") or appraisal)
        ),
        reason=reason,
        decay_remainder_seconds=int(current.get("decay_remainder_seconds") or 0),
        decay_anchor_at=logical_at,
        source_reference=(
            source_reference
            if has_affect_effect
            else str(current.get("source_reference") or source_reference)
        ),
        repair_quality=repair_quality,
        repair_observation_seconds=repair_observation_seconds,
        repair_streak=repair_streak,
        violation_count=violation_count,
        core_affect=_core_affect(vector, baseline, episodes),
        active_episodes=episodes,
        repair_evidence_count=repair_evidence_count,
        profile=profile,
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
    repair_evidence_count = int(current.get("repair_evidence_count") or 0)
    profile = _profile(current.get("profile"))
    current_repair_quality = str(current.get("repair_quality") or "")
    if current_repair_quality and repair_evidence_count < int(
        profile["repair_evidence_required"]
    ):
        repair_observation_seconds = max(3600, repair_observation_seconds)
    repair_quality = current_repair_quality if repair_observation_seconds else ""
    episodes = _decay_episodes(current, elapsed_seconds, logical_at)
    if (
        current_repair_quality
        and repair_evidence_count >= int(profile["repair_evidence_required"])
        and negative_total < 8
    ):
        episodes = tuple(
            episode
            for episode in episodes
            if not (
                int(episode.get("valence") or 0) < 0
                and str(episode.get("target") or "") == "companion"
            )
        )
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
        repair_evidence_count=repair_evidence_count,
        core_affect=_core_affect(vector, baseline, episodes),
        active_episodes=episodes,
        profile=profile,
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
        "repair_evidence_count": outcome.repair_evidence_count,
        "core_affect": dict(outcome.core_affect or {}),
        "active_episodes": [dict(item) for item in outcome.active_episodes],
        "profile": dict(outcome.profile or DEFAULT_AFFECT_PROFILE),
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
        return "情绪还没过去，正在消化：不主动追问，不把普通忙碌解释成背叛。"
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
        "sexual_boundary_violation": 28,
        "dehumanization": 20,
        "coercion": 22,
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
        "npc_conflict": 14,
        "social_warmth": 6,
        "family_connection": 7,
        "goal_progress": 5,
        "goal_completed": 10,
        "creative_satisfaction": 7,
        "restorative_solitude": -5,
        "goal_strain": 8,
    }.get(appraisal, -1)


def _clamp(value: int) -> int:
    return max(0, min(100, value))


def _negative_total(vector: dict[str, int], baseline: dict[str, int]) -> int:
    return sum(
        max(0, vector[key] - baseline[key])
        for key in ("hurt", "anger", "sadness", "loneliness", "anxiety", "resentment")
    )


def _new_episode(
    *,
    appraisal: str,
    logical_at: str,
    source_reference: str,
    target: str,
    intensity: int | None,
    dimensions: dict[str, object],
    relationship_residue: dict[str, object],
    profile: dict[str, object],
) -> dict[str, object]:
    raw_intensity = 50 if intensity is None else int(intensity)
    episode_intensity = (
        {1: 30, 2: 50, 3: 75, 4: 95}[raw_intensity]
        if 1 <= raw_intensity <= 4
        else max(1, min(100, raw_intensity))
    )
    episode_intensity = min(
        100,
        episode_intensity
        + min(10, max(0, -int(dimensions.get("norm_compatibility", 0))) // 20)
        + min(8, max(0, -int(dimensions.get("power_delta", 0))) // 20),
    )
    effect = _EFFECTS.get(appraisal, {})
    positive = sum(max(0, effect.get(key, 0)) for key in ("warmth", "joy"))
    negative = sum(
        max(0, effect.get(key, 0))
        for key in ("hurt", "anger", "sadness", "loneliness", "anxiety", "resentment")
    )
    valence = 1 if positive > negative else -1 if negative > positive else 0
    half_life = int(
        profile[
            "negative_half_life_hours"
            if appraisal in HARMFUL_INTERACTION_APPRAISALS
            else "positive_half_life_hours"
        ]
    )
    residue_vector = relationship_residue.get("vector", {})
    residue_vector = residue_vector if isinstance(residue_vector, dict) else {}
    learned_resentment = max(0, int(residue_vector.get("resentment") or 0))
    if appraisal in HARMFUL_INTERACTION_APPRAISALS:
        half_life += min(
            12,
            learned_resentment
            * int(profile["resentment_half_life_gain_hours"]),
        )
        episode_intensity = min(
            100,
            episode_intensity
            + min(15, learned_resentment * int(profile["resentment_intensity_gain"])),
        )
    if appraisal in {"warmth_received", "social_warmth", "goal_progress"}:
        half_life = int(profile["warmth_half_life_hours"])
    basis = f"{source_reference}|{appraisal}|{logical_at}|{target}"
    return {
        "episode_id": f"affect:{sha256(basis.encode()).hexdigest()[:16]}",
        "source_reference": source_reference,
        "appraisal": appraisal,
        "target": target,
        "started_at": logical_at,
        "updated_at": logical_at,
        "intensity": episode_intensity,
        "valence": valence,
        "half_life_hours": half_life,
        "status": "active",
        "certainty": int(dimensions.get("certainty", 100)),
        "controllability": int(dimensions.get("controllability", 50)),
        "power_delta": int(dimensions.get("power_delta", 0)),
        "confidence": float(dimensions.get("confidence", 1.0)),
        "profile_version": str(profile["version"]),
    }


def _decay_episodes(
    current: dict[str, object], elapsed_seconds: int, logical_at: str
) -> tuple[dict[str, object], ...]:
    elapsed_hours = max(0.0, float(elapsed_seconds) / 3600.0)
    decayed: list[dict[str, object]] = []
    raw_episodes = current.get("active_episodes", ())
    if not isinstance(raw_episodes, (list, tuple)):
        return ()
    for raw in raw_episodes:
        if not isinstance(raw, dict) or raw.get("status") == "resolved":
            continue
        half_life = max(1.0, float(raw.get("half_life_hours") or 12))
        intensity = int(round(int(raw.get("intensity") or 0) * pow(0.5, elapsed_hours / half_life)))
        if intensity < 8:
            continue
        decayed.append(
            {
                **raw,
                "intensity": intensity,
                "updated_at": logical_at,
                "status": "regulated" if elapsed_seconds > 0 else str(raw.get("status") or "active"),
            }
        )
    return tuple(decayed[-16:])


def _core_affect(
    vector: dict[str, int],
    baseline: dict[str, int],
    episodes: tuple[dict[str, object], ...],
) -> dict[str, object]:
    positive = sum(max(0, vector[key] - baseline[key]) for key in ("warmth", "joy"))
    negative = _negative_total(vector, baseline)
    valence = max(-100, min(100, (positive - negative) * 2))
    arousal = max(
        0,
        min(
            100,
            int(
                (
                    vector["anger"]
                    + vector["anxiety"]
                    + vector["joy"]
                    + vector["hurt"] // 2
                )
                / 2
            ),
        ),
    )
    dominance = max(
        -100,
        min(
            100,
            vector["anger"] - vector["anxiety"] - vector["hurt"] // 2,
        ),
    )
    episode_valences = {int(item.get("valence") or 0) for item in episodes}
    return {
        "valence": valence,
        "arousal": arousal,
        "dominance": dominance,
        "mixed": -1 in episode_valences and 1 in episode_valences,
        "rule_version": "core-affect-v1",
    }


def _profile(value: object) -> dict[str, object]:
    result = dict(DEFAULT_AFFECT_PROFILE)
    if isinstance(value, dict):
        for key, default in DEFAULT_AFFECT_PROFILE.items():
            if key not in value:
                continue
            if key == "version":
                result[key] = str(value[key])[:80]
            elif isinstance(default, int):
                result[key] = max(1, min(168, int(value[key])))
    return result
