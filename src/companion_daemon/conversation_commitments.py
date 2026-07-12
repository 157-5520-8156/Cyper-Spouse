"""Pure lifecycle and waiting rules for world conversation commitments.

Conversation threads are the common world representation for questions and
proactive follow-ups.  This module deliberately depends on logical time passed
by the caller; it never reads a wall clock or mutates a projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Mapping, cast


ConversationKind = Literal[
    "question",
    "comfort",
    "promise",
    "contradiction",
    "life_share",
    "reply_reconsider",
    "pulse",
]
WaitingPhase = Literal[
    "not_due",
    "anticipating",
    "holding_back",
    "confused",
    "mildly_hurt",
    "letting_go",
    "revisit_later",
]

RULE_VERSION = "conversation-commitments-v1"

KINDS: tuple[ConversationKind, ...] = (
    "question",
    "comfort",
    "promise",
    "contradiction",
    "life_share",
    "reply_reconsider",
    "pulse",
)

_KIND_ALIASES: dict[str, ConversationKind] = {
    "comfort_followup": "comfort",
    "promise_followup": "promise",
    "contradiction_followup": "contradiction",
    "life_share_followup": "life_share",
    "conversation_pulse": "pulse",
}

_EXPECTED_RESPONSE_HOURS: dict[ConversationKind, float] = {
    "question": 4.0,
    "comfort": 6.0,
    "promise": 8.0,
    "contradiction": 5.0,
    "life_share": 6.0,
    "reply_reconsider": 3.0,
    "pulse": 2.0,
}

_RELATIONSHIP_MULTIPLIER = {
    "stranger": 0.75,
    "acquaintance": 0.90,
    "friend": 1.0,
    "close_friend": 1.15,
    "ambiguous": 1.25,
    "lover": 1.35,
}


class ConversationCommitmentError(ValueError):
    """A proposed conversation commitment has no safe replayable lifecycle."""


@dataclass(frozen=True)
class ConversationThread:
    thread_id: str
    kind: ConversationKind
    user_id: str
    origin: dict[str, str]
    reason: str
    due_at: datetime
    expires_at: datetime
    cancel_conditions: tuple[str, ...]
    owner: str
    status: str = "open"
    terminal_state: str | None = None
    waiting_phase: WaitingPhase = "not_due"
    waiting_changed_at: datetime | None = None
    rule_version: str = RULE_VERSION

    def as_payload(self) -> dict[str, object]:
        """Return the complete JSON-safe projection/event payload."""
        return {
            "thread_id": self.thread_id,
            "kind": self.kind,
            "user_id": self.user_id,
            "origin": dict(self.origin),
            "reason": self.reason,
            "due_at": self.due_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "cancel_conditions": list(self.cancel_conditions),
            "owner": self.owner,
            "status": self.status,
            "terminal_state": self.terminal_state,
            "waiting_phase": self.waiting_phase,
            "waiting_changed_at": (
                self.waiting_changed_at.isoformat() if self.waiting_changed_at else None
            ),
            "rule_version": self.rule_version,
        }


@dataclass(frozen=True)
class WaitingResponse:
    phase: WaitingPhase
    reason: str
    expression_policy: str
    relationship_deltas: dict[str, int]
    next_review_at: datetime | None
    rule_version: str = RULE_VERSION


def normalize_kind(value: str) -> ConversationKind:
    """Translate legacy task names into the one thread-kind vocabulary."""
    normalized = value.strip().casefold()
    normalized = _KIND_ALIASES.get(normalized, cast(ConversationKind, normalized))
    if normalized not in KINDS:
        raise ConversationCommitmentError(f"unsupported conversation kind: {value}")
    return cast(ConversationKind, normalized)


def create_conversation_thread(
    *,
    thread_id: str,
    kind: str,
    user_id: str,
    origin: Mapping[str, object],
    reason: str,
    due_at: datetime,
    expires_at: datetime,
    cancel_conditions: tuple[str, ...] | list[str],
    owner: str,
) -> ConversationThread:
    """Validate and create one fully-owned, expiring conversation thread."""
    normalized_id = thread_id.strip()
    normalized_user = user_id.strip()
    normalized_reason = reason.strip()
    normalized_owner = owner.strip()
    origin_kind = str(origin.get("kind") or "").strip()
    origin_reference = str(origin.get("reference") or "").strip()
    conditions = tuple(dict.fromkeys(str(item).strip() for item in cancel_conditions if str(item).strip()))
    if not normalized_id:
        raise ConversationCommitmentError("thread_id is required")
    if not normalized_user:
        raise ConversationCommitmentError("user_id is required")
    if not normalized_reason or len(normalized_reason) > 240:
        raise ConversationCommitmentError("reason is required and must be at most 240 characters")
    if not normalized_owner:
        raise ConversationCommitmentError("owner is required")
    if not origin_kind or not origin_reference:
        raise ConversationCommitmentError("origin kind and reference are required")
    if due_at.tzinfo is None or expires_at.tzinfo is None:
        raise ConversationCommitmentError("due_at and expires_at must be timezone-aware")
    if expires_at <= due_at:
        raise ConversationCommitmentError("expires_at must be later than due_at")
    if not conditions:
        raise ConversationCommitmentError("at least one cancel condition is required")
    return ConversationThread(
        thread_id=normalized_id,
        kind=normalize_kind(kind),
        user_id=normalized_user,
        origin={"kind": origin_kind, "reference": origin_reference},
        reason=normalized_reason,
        due_at=due_at,
        expires_at=expires_at,
        cancel_conditions=conditions,
        owner=normalized_owner,
    )


def evaluate_waiting_response(
    thread: ConversationThread | Mapping[str, object],
    *,
    relationship: Mapping[str, object],
    logical_at: datetime,
) -> WaitingResponse:
    """Evaluate one progressive response-waiting phase from replayable inputs.

    The curve intentionally changes with thread kind, relationship stage and
    reliability.  It cannot produce relational grievance for strangers.
    """
    if logical_at.tzinfo is None:
        raise ConversationCommitmentError("logical_at must be timezone-aware")
    kind, due_at = _waiting_inputs(thread)
    if logical_at < due_at:
        return WaitingResponse(
            "not_due",
            "commitment_not_due",
            "这件事还没到需要等待回应的时间。",
            {},
            due_at,
        )

    stage = str(relationship.get("stage") or "stranger")
    if stage not in _RELATIONSHIP_MULTIPLIER:
        stage = "stranger"
    reliability = max(-100, min(100, int(relationship.get("reliability") or 0)))
    expected_hours = _EXPECTED_RESPONSE_HOURS[kind] * _RELATIONSHIP_MULTIPLIER[stage]
    if reliability >= 60:
        expected_hours *= 0.85
    elif reliability <= -40:
        expected_hours *= 0.65
    elapsed_hours = max(0.0, (logical_at - due_at).total_seconds() / 3600.0)
    progress = elapsed_hours / max(0.25, expected_hours)
    expected = timedelta(hours=expected_hours)

    if progress < 0.25:
        return WaitingResponse(
            "anticipating",
            "ordinary_early_expectation",
            "只是自然期待，不追问，也不把短暂沉默解释成态度。",
            {},
            due_at + expected * 0.25,
        )
    if progress < 1.0:
        return WaitingResponse(
            "holding_back",
            "giving_user_space",
            "先把注意力收回自己的生活，给对方留出回应空间。",
            {},
            due_at + expected,
        )

    if stage == "stranger":
        if progress < 2.5:
            return WaitingResponse(
                "holding_back",
                "stranger_preserves_distance",
                "刚认识时保持分寸，不因等待而增加亲密含义。",
                {},
                due_at + expected * 2.5,
            )
        return WaitingResponse(
            "letting_go",
            "stranger_releases_unanswered_outbound",
            "收住这次等待，不索取解释，也不使用亲密关系式委屈。",
            {},
            None,
        )

    if kind == "pulse" and progress >= 2.5:
        return WaitingResponse(
            "letting_go",
            "low_stakes_pulse_released",
            "余韵没有得到回应就自然放下，不把它升级成关系问题。",
            {},
            None,
        )
    if stage == "acquaintance":
        if progress < 2.0:
            return WaitingResponse(
                "confused",
                "acquaintance_response_uncertain",
                "可以觉得有点意外，但保留多种解释，不表现占有或委屈。",
                {},
                due_at + expected * 2,
            )
        return WaitingResponse(
            "letting_go",
            "acquaintance_releases_unanswered_outbound",
            "先放下，不追着确认关系意义。",
            {},
            None,
        )

    if progress < 1.8:
        reason = (
            "reliable_response_pattern_interrupted"
            if reliability >= 60
            else "response_timing_uncertain"
        )
        return WaitingResponse(
            "confused",
            reason,
            "允许短暂困惑，但不读心，也不急着追加消息。",
            {},
            due_at + expected * 1.8,
        )

    close = stage in {"close_friend", "ambiguous", "lover"}
    emotionally_salient = kind in {"question", "comfort", "promise", "contradiction"}
    if close and emotionally_salient and progress < 3.5:
        reason = (
            "close_relationship_reliable_promise_unanswered"
            if kind == "promise" and reliability >= 60
            else "close_relationship_salient_thread_unanswered"
        )
        return WaitingResponse(
            "mildly_hurt",
            reason,
            "可以承认一点失落，但不指控、不惩罚，也不把沉默解释成拒绝。",
            {"reliability": -1},
            due_at + expected * 3.5,
        )

    if kind in {"comfort", "promise", "contradiction"}:
        return WaitingResponse(
            "revisit_later",
            "salient_thread_deferred_for_later",
            "把这件事留作以后自然再提，不立即追发。",
            {"reliability": -1} if close else {},
            None,
        )
    return WaitingResponse(
        "letting_go",
        "unanswered_outbound_released",
        "放下这次等待，让后续互动重新从当下开始。",
        {},
        None,
    )


def _waiting_inputs(
    thread: ConversationThread | Mapping[str, object],
) -> tuple[ConversationKind, datetime]:
    if isinstance(thread, ConversationThread):
        return thread.kind, thread.due_at
    kind = normalize_kind(str(thread.get("kind") or "question"))
    due_raw = thread.get("due_at") or thread.get("opened_at")
    if isinstance(due_raw, datetime):
        due_at = due_raw
    else:
        try:
            due_at = datetime.fromisoformat(str(due_raw))
        except (TypeError, ValueError) as exc:
            raise ConversationCommitmentError("thread due_at must be an ISO datetime") from exc
    if due_at.tzinfo is None:
        raise ConversationCommitmentError("thread due_at must be timezone-aware")
    return kind, due_at
