"""One immutable policy source for affect prompt, validation and fallback."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
import json
import re
from typing import Mapping


@dataclass(frozen=True)
class ReplyCandidate:
    reply_text: str
    mentioned_event_ids: tuple[str, ...] = ()
    proposed_action_ids: tuple[str, ...] = ()
    claims: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class ReplySkeleton:
    reply_text: str
    mentioned_event_ids: tuple[str, ...] = ()
    proposed_action_ids: tuple[str, ...] = ()
    claims: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True)
class ReplyTurnContext:
    speech_act: str = "statement"
    variant_key: str = ""


@dataclass(frozen=True)
class ExpressionResolution:
    reply_text: str
    mentioned_event_ids: tuple[str, ...]
    proposed_action_ids: tuple[str, ...]
    claims: tuple[Mapping[str, object], ...]
    used_fallback: bool
    violation: str | None
    plan_hash: str


@dataclass(frozen=True)
class ExpressionPolicySpec:
    primary_appraisal: str
    secondary_appraisal: str
    mixed: bool
    unresolved: bool
    regulation_strategy: str
    attribution_target: str
    leakage: int
    directness: int
    negative_total: int
    negative_peak: int
    positive_total: int
    anger: int
    sadness_support: int
    sadness_peak: int
    behavior_tendency: str
    current_appraisal: str
    rule_version: str = "expression-policy-v1"

    def compile_prompt(self) -> str:
        if self.regulation_strategy == "contain_spillover":
            return (
                "世界里的情绪可以轻微影响节奏和耐心，但明确归因于原事件；"
                "不要把它算到用户头上，也不要无故设用户边界。"
            )
        if self.regulation_strategy == "boundary_expression":
            mixed = "若仍有温暖，允许关心与防御并存；" if self.mixed else ""
            repair = (
                "可以听见修复，但不得把道歉直接等同于原谅；"
                if self.current_appraisal == "repair_attempt"
                else ""
            )
            return (
                "保留受伤或生气，只针对有证据的行为；"
                f"{mixed}{repair}不要假装已经没事。"
            )
        if self.mixed:
            return "表达主要感受时保留次要感受的余韵；不要把混合情绪压成单一标签。"
        return "按当前有来源的感受自然回应，不额外补写情绪原因。"

    def validate(self, reply_text: str) -> str | None:
        if self.regulation_strategy == "contain_spillover" and _blames_user_for_spillover(reply_text):
            return "spillover_misattributed_to_user"
        explicitly_preserves_negative = bool(
            re.search(
                r"(?:没事|没关系|过去了)[^。！？]{0,8}(?:不等于|但|可是|并不)"
                r"[^。！？]{0,12}(?:生气|介意|不舒服|难过|没感觉)",
                reply_text,
            )
        )
        if (
            self.unresolved
            and self.negative_total > 0
            and any(marker in reply_text for marker in ("没事", "没关系", "完全不介意", "已经过去", "一点都不"))
            and not explicitly_preserves_negative
        ):
            return "unresolved_affect_denied"

        negative_claim = re.search(
            r"(?:我|我这会儿|刚才|是有|[^。！？]{0,8}里)[^。！？]{0,10}"
            r"(?:不舒服|介意|生气|难过|委屈|不高兴|烦|压着火|闷着|不想理|"
            r"失落|孤独|心里发紧|被[^。！？]{0,8}(?:硌|刺|伤|戳))",
            reply_text,
        )
        positive_claim = re.search(
            r"(?:我|我这会儿|刚才|是有|[^。！？]{0,8}里)[^。！？]{0,10}"
            r"(?:开心|高兴|踏实|安心|温暖|放松|笑出声|笑了|松了口气)",
            reply_text,
        )
        anger_claim = re.search(
            r"(?:我|我这会儿|刚才)[^。！？]{0,8}(?:压着火|气得|很生气|闷着|不想理)",
            reply_text,
        )
        sadness_claim = re.search(
            r"(?:我|我这会儿|刚才)[^。！？]{0,8}(?:失落|孤独|心里发紧|很难过)",
            reply_text,
        )
        supports_negative = self.negative_peak >= 4 or self.behavior_tendency in {
            "withdraw", "guarded"
        } or (self.unresolved and self.behavior_tendency in {"patient", "repair_open"})
        supports_positive = self.positive_total >= 3 or self.behavior_tendency in {
            "caring", "warm", "open"
        }
        supports_anger = self.anger >= 8 or self.behavior_tendency in {"withdraw", "guarded"}
        supports_sadness = self.sadness_peak >= 3 or (
            self.unresolved and self.behavior_tendency in {"patient", "repair_open"}
        )
        if negative_claim and not supports_negative:
            return "uncommitted_companion_affect"
        if positive_claim and not supports_positive:
            return "uncommitted_companion_affect"
        if anger_claim and not supports_anger:
            return "uncommitted_companion_affect"
        if sadness_claim and not supports_sadness:
            return "uncommitted_companion_affect"
        return None

    def fallback(self, safe_seed: ReplySkeleton, turn: ReplyTurnContext) -> ReplyCandidate:
        base = safe_seed.reply_text.strip()
        keep_provenance = bool(base) and self.validate(base) is None
        if self.regulation_strategy == "contain_spillover":
            suffix = "我这点情绪来自刚才那件事，不是你的错。"
        elif self.unresolved and self.mixed:
            suffix = "我愿意听你把话说完；在意还在，但这件事还没有完全过去。"
        elif self.unresolved and self.current_appraisal == "repair_attempt":
            suffix = "我听见你的修复了，但这件事还没有完全过去，我需要看后续。"
        elif self.unresolved:
            suffix = "这件事还没有完全过去；我会按现在能承受的节奏说。"
        else:
            suffix = ""
        if keep_provenance:
            text = " ".join(part for part in (base, suffix) if part)
        else:
            text = suffix or "我听到了，先按现在能确认的部分回应。"
        # Fallback varies only discourse rhythm.  It never changes source, claim,
        # or Action fields from the already-grounded skeleton.
        if turn.variant_key and turn.speech_act == "repair" and not text.endswith("。"):
            text += "。"
        candidate = ReplyCandidate(
            text,
            safe_seed.mentioned_event_ids if keep_provenance else (),
            safe_seed.proposed_action_ids if keep_provenance else (),
            safe_seed.claims if keep_provenance else (),
        )
        if self.validate(candidate.reply_text) is not None:
            candidate = ReplyCandidate(
                "我听到了，先按现在能确认的部分回应。",
                (),
                (),
                (),
            )
        return candidate


@dataclass(frozen=True)
class ExpressionPlan:
    policy_spec: ExpressionPolicySpec
    plan_hash: str
    revision: int = 0
    user_id: str = ""
    intent_id: str = ""

    @property
    def prompt_fragment(self) -> str:
        return self.policy_spec.compile_prompt()

    def validate(self, reply_text: str) -> str | None:
        return self.policy_spec.validate(reply_text)

    def resolve(
        self,
        proposed: ReplyCandidate,
        *,
        safe_seed: ReplySkeleton,
        turn: ReplyTurnContext,
    ) -> ExpressionResolution:
        violation = self.validate(proposed.reply_text)
        accepted = proposed if violation is None else self.policy_spec.fallback(safe_seed, turn)
        return ExpressionResolution(
            accepted.reply_text,
            accepted.mentioned_event_ids,
            accepted.proposed_action_ids,
            accepted.claims,
            violation is not None,
            violation,
            self.plan_hash,
        )


def compile_expression_plan(
    affect: Mapping[str, object],
    relationship: Mapping[str, object],
    needs: Mapping[str, object],
    *,
    current_appraisal: str,
    revision: int = 0,
    user_id: str = "",
    intent_id: str = "",
) -> ExpressionPlan:
    raw_episodes = affect.get("active_episodes", ())
    episodes = [
        item for item in raw_episodes
        if isinstance(item, dict) and item.get("status") != "resolved"
    ] if isinstance(raw_episodes, (list, tuple)) else []
    episodes.sort(
        key=lambda item: _expression_relevance(
            item,
            episodes=episodes,
            current_appraisal=current_appraisal,
            user_id=user_id,
            intent_id=intent_id,
        ),
        reverse=True,
    )
    primary = episodes[0] if episodes else {}
    primary_valence = int(primary.get("valence") or 0)
    secondary = next(
        (item for item in episodes[1:] if int(item.get("valence") or 0) != primary_valence),
        {},
    )
    mixed = bool(primary and secondary)
    target = str(primary.get("target") or "general")
    profile = affect.get("profile", {})
    profile = profile if isinstance(profile, dict) else {}
    if target.startswith(("npc:", "goal:")) or target == "world":
        regulation = "contain_spillover"
        leakage = min(
            int(profile.get("spillover_leakage_cap") or 25),
            int(primary.get("intensity") or 0) // 4,
        )
    elif primary_valence < 0 and target == "companion":
        regulation = "boundary_expression"
        leakage = min(80, 35 + int(primary.get("intensity") or 0) // 3)
    elif mixed:
        regulation = "integrate_mixed_affect"
        leakage = 35
    else:
        regulation = "natural_expression"
        leakage = min(45, int(primary.get("intensity") or 0) // 2)
    emotion_program = primary.get("emotion_program")
    emotion_program = emotion_program if isinstance(emotion_program, dict) else {}
    process_effects = emotion_program.get("process_effects")
    process_effects = process_effects if isinstance(process_effects, dict) else {}
    display_multiplier = max(
        0.0, min(1.0, float(process_effects.get("display_multiplier") or 1.0))
    )
    leakage = int(round(leakage * display_multiplier))

    vector = affect.get("vector")
    vector = vector if isinstance(vector, dict) else {}
    negative_total = sum(int(vector.get(key, 0) or 0) for key in (
        "hurt", "anger", "sadness", "loneliness", "anxiety", "resentment",
        "shame", "guilt", "jealousy",
    ))
    negative_peak = max((int(vector.get(key, 0) or 0) for key in (
        "hurt", "anger", "sadness", "loneliness", "anxiety", "resentment",
        "shame", "guilt", "jealousy",
    )), default=0)
    positive_total = sum(int(vector.get(key, 0) or 0) for key in ("warmth", "joy"))
    stage = str(relationship.get("stage") or "stranger")
    directness = 45 + min(30, int(needs.get("boundary") or 0) // 4)
    if stage in {"close_friend", "ambiguous", "lover"}:
        directness += 10
    spec = ExpressionPolicySpec(
        primary_appraisal=str(primary.get("appraisal") or current_appraisal),
        secondary_appraisal=str(secondary.get("appraisal") or ""),
        mixed=mixed,
        unresolved=bool(affect.get("unresolved")),
        regulation_strategy=regulation,
        attribution_target=target,
        leakage=max(0, min(100, leakage)),
        directness=max(0, min(100, directness)),
        negative_total=negative_total,
        negative_peak=negative_peak,
        positive_total=positive_total,
        anger=int(vector.get("anger", 0) or 0),
        sadness_support=sum(int(vector.get(key, 0) or 0) for key in (
            "sadness", "loneliness", "anxiety", "hurt"
        )),
        sadness_peak=max((int(vector.get(key, 0) or 0) for key in (
            "sadness", "loneliness", "anxiety", "hurt"
        )), default=0),
        behavior_tendency=str(affect.get("behavior_tendency") or "neutral"),
        current_appraisal=current_appraisal,
    )
    canonical = json.dumps(
        {
            "policy_spec": asdict(spec),
            "revision": revision,
            "user_id": user_id,
            "intent_id": intent_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ExpressionPlan(
        spec,
        sha256(canonical.encode()).hexdigest(),
        revision,
        user_id,
        intent_id,
    )


def policy_spec_from_projection(
    affect: Mapping[str, object],
    display_plan: Mapping[str, object] | None = None,
) -> ExpressionPolicySpec:
    """Compatibility compiler for callers that only have the old display payload."""
    display = display_plan or {}
    plan = compile_expression_plan(affect, {}, {}, current_appraisal="ordinary_message")
    spec = plan.policy_spec
    if not display:
        return spec
    values = asdict(spec)
    for key in (
        "primary_appraisal", "secondary_appraisal", "mixed", "regulation_strategy",
        "attribution_target", "leakage", "directness",
    ):
        if key in display:
            values[key] = display[key]
    return ExpressionPolicySpec(**values)


def _blames_user_for_spillover(reply_text: str) -> bool:
    return bool(re.search(
        r"(?:"
        r"(?:都是|全是|还不是|要不是|因为|怪)[^。！？]{0,4}你[^。！？]{0,14}(?:烦|生气|难受|来气|心情不好)|"
        r"(?:你(?:一出现|一来|一说话|让我|把我|害我|气得我)|看到你|见到你)[^。！？]{0,14}(?:更?烦|生气|难受|来气|心情不好)|"
        r"(?:坏心情|心情不好)[^。！？]{0,12}(?:你造成|因为你|怪你)|被你气)",
        reply_text,
    ))


def _expression_relevance(
    episode: Mapping[str, object],
    *,
    episodes: list[dict[str, object]],
    current_appraisal: str,
    user_id: str,
    intent_id: str,
) -> float:
    """Rank expression candidates without changing authoritative episode state."""
    intensity = max(0, int(episode.get("intensity") or 0))
    program = episode.get("emotion_program")
    program = program if isinstance(program, Mapping) else {}
    effects = program.get("process_effects")
    effects = effects if isinstance(effects, Mapping) else {}
    accessibility = max(
        0.0, min(1.0, float(effects.get("display_multiplier") or 1.0))
    )
    score = intensity * accessibility
    appraisal = str(episode.get("appraisal") or "")
    if appraisal and appraisal == current_appraisal:
        score += 100
    target = str(episode.get("target") or "")
    if target == "companion":
        score += 35
    elif user_id and target in {user_id, f"user:{user_id}"}:
        score += 60
    elif target.startswith(("npc:", "goal:")) or target == "world":
        score -= 10
    source_reference = str(episode.get("source_reference") or "")
    if intent_id and (
        str(episode.get("intent_id") or "") == intent_id
        or intent_id in source_reference
    ):
        score += 120
    updated = _episode_time(episode)
    newest = max((_episode_time(item) for item in episodes), default=None)
    if updated is not None and newest is not None:
        age_hours = max(0.0, (newest - updated).total_seconds() / 3600.0)
        score += 30 * pow(0.5, age_hours / 24.0)
    if str(episode.get("status") or "active") == "active":
        score += 5
    return score


def _episode_time(episode: Mapping[str, object]) -> datetime | None:
    raw = str(episode.get("updated_at") or episode.get("started_at") or "")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None
