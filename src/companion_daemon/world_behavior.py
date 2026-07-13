"""Deterministic, world-only behaviour rules for online companion actions.

The module is deliberately pure: callers supply a world projection and turn
text, then persist its decision through :class:`WorldKernel`.  It never reads
wall clock time, MoodState, social tasks, or a model response.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import ceil
from typing import Any, Literal

from companion_daemon.conversation_cadence import ConversationCadence
from companion_daemon.outbound_policy import (
    OutboundAllowance,
    OutboundKind,
    OutboundPolicy,
    OutboundProjection,
    OutboundRequest,
    RecentOutbound,
    evaluate_outbound,
)
from companion_daemon.repair_curve import is_repair_message
from companion_daemon.world_affect import affect_guidance
from companion_daemon.world_relationship import STAGES, relationship_stage_instruction

CommunicationAttention = Literal["seen", "deferred", "do_not_disturb"]


@dataclass(frozen=True)
class CommunicationDecision:
    attention: CommunicationAttention
    reason: str
    defer_minutes: int | None = None
    candidates: tuple["AttentionCandidate", ...] = ()


@dataclass(frozen=True)
class AttentionCandidate:
    attention: CommunicationAttention
    score: int
    reason: str
    defer_minutes: int | None = None


@dataclass(frozen=True)
class OutreachConstraint:
    allowed: bool
    reason: str
    requires_deliberation: bool = False
    override_cost: int = 0
    override_strike: int = 0


@dataclass(frozen=True)
class ExpressionGuidance:
    label: str
    prompt_line: str


class WorldBehaviorPolicy:
    """Hide world communication and outreach policy behind two pure methods."""

    RULE_VERSION = "world-behavior-v1"
    _URGENT_MARKERS = ("急", "紧急", "救命", "出事", "医院", "危险", "现在就", "立刻")
    _VULNERABLE_MARKERS = ("崩溃", "难受", "撑不住", "想哭", "好难过", "很痛苦", "不想活", "我害怕", "害怕")
    _PRESSURE_MARKERS = ("必须", "立刻", "马上", "现在就", "快点", "不发", "证明", "听话")

    def communication_decision(
        self,
        state: dict[str, Any],
        *,
        text: str,
        resumed_action: bool = False,
        user_id: str | None = None,
        cadence: ConversationCadence | None = None,
    ) -> CommunicationDecision:
        """Rank attention options from explicit, replayable world facts."""
        needs = _mapping(state.get("needs"))
        boundary = int(needs.get("boundary", 0))
        security = int(needs.get("security", 50))
        energy = int(needs.get("energy", 70))
        scores: dict[CommunicationAttention, int] = {
            "seen": 50 + max(0, int(needs.get("attention", 50)) - 50) // 5,
            "deferred": 18,
            "do_not_disturb": 0,
        }
        reasons: dict[CommunicationAttention, str] = {
            "seen": "world_available",
            "deferred": "brief_attention_hold",
            "do_not_disturb": "boundary_reserve",
        }
        defer_minutes = 5
        boundary_pressure = boundary >= 75 and self._is_pressure(text)
        if boundary_pressure:
            scores["do_not_disturb"] += 180
            reasons["do_not_disturb"] = "boundary_high_under_pressure"
        if resumed_action or (self._is_urgent(text) and not boundary_pressure):
            scores["seen"] += 220
            reasons["seen"] = "resumed_or_urgent_turn"
        elif self._is_vulnerable(text):
            scores["seen"] += 200
            reasons["seen"] = "user_vulnerable_turn"
        modulation = _mapping(state.get("emotion_modulation"))
        affect_vector = _mapping(modulation.get("vector"))
        if (
            str(modulation.get("behavior_tendency") or "") == "withdraw"
            and int(affect_vector.get("hurt", 0)) >= 30
            and not is_repair_message(text)
        ):
            hurt = int(affect_vector.get("hurt", 0))
            scores["deferred"] += 70 + hurt // 3 + max(0, 35 - energy)
            reasons["deferred"] = "unresolved_hurt"
            defer_minutes = max(
                defer_minutes,
                min(30, 6 + hurt // 6 + max(0, 40 - energy) // 4),
            )
        active = next(
            (
                _mapping(item)
                for item in _mapping(state.get("agenda")).values()
                if _mapping(item).get("status") == "active"
            ),
            None,
        )
        if active:
            attention_demand = int(active.get("attention_demand", 35))
            interruptible = bool(active.get("interruptible", True))
            if attention_demand >= 90 or (attention_demand >= 70 and not interruptible):
                phase, remaining_minutes = _activity_phase(state, active)
                scores["deferred"] += 120 + attention_demand // 4
                reasons["deferred"] = f"active_world_activity_not_interruptible:{phase}"
                defer_minutes = max(1, min(20, remaining_minutes))
        if active and energy <= 35:
            scores["deferred"] += 65 + (35 - energy)
            if not reasons["deferred"].startswith("active_world_activity_not_interruptible"):
                reasons["deferred"] = "active_world_activity_low_energy"
                defer_minutes = max(defer_minutes, min(30, 20 + max(0, 30 - energy) // 2))
        if security <= 20 and boundary >= 55:
            scores["deferred"] += 70 + (20 - security)
            reasons["deferred"] = "security_low_boundary_recovery"
            defer_minutes = max(defer_minutes, min(25, 15 + (20 - security) // 3))
        relationship = _mapping(_mapping(state.get("relationships")).get(user_id))
        scores["seen"] += max(-10, min(10, int(relationship.get("trust", 0)) // 5))
        heat = str(getattr(cadence, "heat", "cold"))
        if heat == "hot":
            scores["seen"] += 35
            if reasons["seen"] == "world_available":
                reasons["seen"] = "hot_conversation_continuation"
        elif heat == "warm":
            scores["seen"] += 15
            if reasons["seen"] == "world_available":
                reasons["seen"] = "warm_conversation_continuation"
        ordered = tuple(
            sorted(
                (
                    AttentionCandidate(
                        attention=attention,
                        score=score,
                        reason=reasons[attention],
                        defer_minutes=defer_minutes if attention == "deferred" else None,
                    )
                    for attention, score in scores.items()
                ),
                key=lambda item: (-item.score, {"seen": 0, "deferred": 1, "do_not_disturb": 2}[item.attention]),
            )
        )
        selected = ordered[0]
        return CommunicationDecision(
            selected.attention,
            selected.reason,
            selected.defer_minutes,
            ordered,
        )

    def outreach_constraint(self, state: dict[str, Any], *, user_id: str) -> OutreachConstraint:
        """Prevent new initiative when a world commitment already owns the turn."""
        for raw in _mapping(state.get("conversation_threads")).values():
            thread = _mapping(raw)
            if thread.get("status") == "open" and thread.get("user_id") == user_id:
                return OutreachConstraint(True, "open_conversation_thread", True, 20, 1)
        for raw in _mapping(state.get("actions")).values():
            action = _mapping(raw)
            if action.get("kind") == "outgoing_message" and action.get("status") in {"scheduled", "sending", "unknown"}:
                return OutreachConstraint(False, "outgoing_action_unresolved")
        relationship = _mapping(_mapping(state.get("relationships")).get(user_id))
        relationship_stage = str(relationship.get("stage") or "stranger")
        if not relationship or relationship_stage == "stranger":
            return OutreachConstraint(True, "relationship_stage_stranger", True, 30, 1)
        modulation = _mapping(state.get("emotion_modulation"))
        vector = _mapping(modulation.get("vector"))
        behavior_tendency = str(modulation.get("behavior_tendency") or "neutral")
        if bool(modulation.get("unresolved")) and (
            int(vector.get("hurt", 0)) >= 20
            or behavior_tendency in {"withdraw", "guarded", "patient"}
        ):
            return OutreachConstraint(True, "unresolved_negative_affect", True, 35, 1)
        needs = _mapping(state.get("needs"))
        if int(needs.get("boundary", 0)) >= 55:
            return OutreachConstraint(True, "boundary_high", True, 45, 2)
        if int(needs.get("security", 50)) <= 25:
            return OutreachConstraint(True, "security_low", True, 40, 2)
        return OutreachConstraint(True, "world_allows_outreach")

    def outbound_allowance(
        self,
        state: dict[str, Any],
        *,
        request_id: str,
        message_kind: str,
        trigger: str,
        text: str | None,
        topic_key: str | None = None,
        policy: OutboundPolicy = OutboundPolicy(),
    ) -> tuple[OutboundRequest, OutboundProjection, OutboundAllowance]:
        """Evaluate the shared outbound budget from only replayable World state."""
        now = datetime.fromisoformat(str(_mapping(state.get("clock")).get("logical_at")))
        request = OutboundRequest(
            request_id=request_id,
            kind=_outbound_kind(message_kind),
            trigger=trigger,
            text=text,
            now=now,
            topic_key=topic_key,
        )
        projection = outbound_projection(state)
        return request, projection, evaluate_outbound(request, projection, policy)

    def expression_guidance(
        self, state: dict[str, Any], *, user_id: str | None = None
    ) -> ExpressionGuidance:
        """Derive a short-lived expression guide without writing private prose."""
        needs = _mapping(state.get("needs"))
        modulation = _mapping(state.get("emotion_modulation"))
        relationship = _mapping(_mapping(state.get("relationships")).get(user_id))
        relationship_stage = str(relationship.get("stage") or "stranger")
        if relationship_stage not in STAGES:
            relationship_stage = "stranger"
        mode = str(modulation.get("mode") or "calm")
        behavior_tendency = str(modulation.get("behavior_tendency") or "neutral")
        if behavior_tendency in {"caring", "repair_open", "repair_observing"}:
            return ExpressionGuidance(
                f"affect_{behavior_tendency}",
                affect_guidance(modulation),
            )
        core_affect = _mapping(modulation.get("core_affect"))
        active_episodes = modulation.get("active_episodes")
        episode_targets = {
            str(item.get("target") or "")
            for item in active_episodes
            if isinstance(item, dict)
        } if isinstance(active_episodes, list) else set()
        # A display plan is an audit record for one past Action.  Derive any
        # current mixed/spillover posture from this revision's affect instead
        # of letting a previous recipient or revision leak into a new reply.
        if bool(core_affect.get("mixed")):
            return ExpressionGuidance(
                "affect_mixed",
                "有混合情绪的余韵；自然表达当前这一刻，不把矛盾简化成单一态度。",
            )
        if (
            bool(modulation.get("unresolved"))
            and episode_targets
            and user_id not in episode_targets
            and any(target.startswith(("npc:", "goal:")) for target in episode_targets)
        ):
            return ExpressionGuidance(
                "affect_spillover",
                "把世界里的情绪留在分寸内，不迁怒眼前的用户，也不假装毫无余韵。",
            )
        if behavior_tendency in {"withdraw", "patient"}:
            return ExpressionGuidance(
                f"affect_{behavior_tendency}",
                affect_guidance(modulation),
            )
        if int(needs.get("boundary", 0)) >= 55 or mode == "guarded":
            return ExpressionGuidance("guarded", "表达简短、清楚，不讨好；只说愿意说的部分。")
        if relationship_stage in {"stranger", "acquaintance"}:
            return ExpressionGuidance("slow_warm", relationship_stage_instruction(relationship_stage))
        if relationship_stage == "friend":
            return ExpressionGuidance("friend", relationship_stage_instruction(relationship_stage))
        if relationship_stage == "close_friend":
            return ExpressionGuidance("close_friend", relationship_stage_instruction(relationship_stage))
        if relationship_stage == "ambiguous":
            return ExpressionGuidance("ambiguous", relationship_stage_instruction(relationship_stage))
        if relationship_stage == "lover":
            return ExpressionGuidance("lover", relationship_stage_instruction(relationship_stage))
        if mode == "caring":
            return ExpressionGuidance("caring", "先接住对方情绪，语气温和，不把关心变成追问。")
        if mode in {"warm", "open", "softening"}:
            return ExpressionGuidance("warm", "可以自然亲近，但不夸张、不补写未发生的共同经历。")
        if int(needs.get("energy", 70)) <= 30:
            return ExpressionGuidance("low_energy", "用较短句回应，允许保留一点安静感，不解释成冷漠。")
        return ExpressionGuidance("neutral", "自然接话，避免模板化表态和没有依据的心理独白。")

    @classmethod
    def _is_urgent(cls, text: str) -> bool:
        return any(marker in text for marker in cls._URGENT_MARKERS)

    @classmethod
    def _is_pressure(cls, text: str) -> bool:
        return any(marker in text for marker in cls._PRESSURE_MARKERS)

    @classmethod
    def _is_vulnerable(cls, text: str) -> bool:
        return any(marker in text for marker in cls._VULNERABLE_MARKERS)


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def outbound_projection(state: dict[str, Any]) -> OutboundProjection:
    """Build the policy read model from settled messages and active World Actions."""
    recent: list[RecentOutbound] = []
    unanswered = 0
    for raw in reversed(list(state.get("recent_messages") or [])):
        message = _mapping(raw)
        if str(message.get("direction") or "") == "in":
            break
        if str(message.get("direction") or "") != "out":
            continue
        unanswered += 1
    messages = list(state.get("recent_messages") or [])
    for raw in messages:
        message = _mapping(raw)
        if str(message.get("direction") or "") != "out":
            continue
        occurred_at = str(message.get("logical_at") or message.get("sent_at") or "")
        if not occurred_at:
            continue
        direction = str(message.get("outgoing_direction") or "")
        recent.append(
            RecentOutbound(
                request_id=str(message.get("source_action_id") or message.get("message_id") or ""),
                kind=_outbound_kind(direction),
                trigger=str(message.get("outbound_trigger") or direction or "outbound"),
                text=str(message.get("text") or "") or None,
                topic_key=str(message.get("topic_key") or "") or None,
                occurred_at=datetime.fromisoformat(occurred_at),
            )
        )
    actions = _mapping(state.get("actions"))
    sending = sorted(
        (
            str(action_id),
            _mapping(raw).get("lease_expires_observed_at"),
        )
        for action_id, raw in actions.items()
        if _mapping(raw).get("status") == "sending"
    )
    lock_owner = sending[0][0] if sending else None
    lock_expiry = datetime.fromisoformat(str(sending[0][1])) if sending and sending[0][1] else None
    trigger_last: dict[str, datetime] = {}
    for item in recent:
        previous = trigger_last.get(item.trigger)
        if previous is None or item.occurred_at > previous:
            trigger_last[item.trigger] = item.occurred_at
    return OutboundProjection(
        last_outbound_at=recent[-1].occurred_at if recent else None,
        trigger_last_outbound_at=trigger_last,
        unanswered_outbound_count=unanswered,
        generation_lock_owner=lock_owner,
        generation_lock_expires_at=lock_expiry,
        recent_outbounds=tuple(recent),
    )


def _outbound_kind(message_kind: str) -> OutboundKind:
    normalized = message_kind.strip().casefold()
    if normalized == "reply" or normalized == "incoming_reply":
        return OutboundKind.REPLY
    if normalized in {"life_event", "life_share"}:
        return OutboundKind.LIFE_SHARE
    if "followup" in normalized or "follow_up" in normalized:
        return OutboundKind.FOLLOWUP
    if normalized in {"media", "image", "media_delivery", "selfie"}:
        return OutboundKind.MEDIA
    if normalized in {"reaction", "sticker", "sticker_delivery"}:
        return OutboundKind.REACTION
    if normalized.startswith("tool"):
        return OutboundKind.TOOL
    return OutboundKind.PULSE


def _activity_phase(
    state: dict[str, Any], active: dict[str, Any]
) -> tuple[str, int]:
    now = datetime.fromisoformat(str(_mapping(state.get("clock")).get("logical_at")))
    starts_at = datetime.fromisoformat(str(active.get("starts_at")))
    ends_at = datetime.fromisoformat(str(active.get("ends_at")))
    duration = max(1.0, (ends_at - starts_at).total_seconds())
    progress = max(0.0, min(1.0, (now - starts_at).total_seconds() / duration))
    phase = "early" if progress < 0.33 else "middle" if progress < 0.8 else "ending"
    remaining = max(1, ceil((ends_at - now).total_seconds() / 60))
    return phase, remaining
