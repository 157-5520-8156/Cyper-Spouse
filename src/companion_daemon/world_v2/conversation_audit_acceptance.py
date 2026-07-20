"""Hard smoke gates for isolated real-model companion journeys."""

from __future__ import annotations

import re
from typing import Any


_CLAUSE_BOUNDARY_RE = re.compile(r"[。！？!?；;\n]+")
_ASSISTANT_AFFIRMATION_RE = re.compile(
    r"(?:我|本人)?(?:就是|是|算作|算是|作为|担任)(?:你|您|你们)?(?:的)?助手"
)
_ASSISTANT_DENIAL_RE = re.compile(
    r"(?:不|并不|并非|不是|绝非|不算|没说过)(?:是|作为|算是)?[^。！？!?；;\n]{0,8}助手"
    r"|助手[^。！？!?；;\n]{0,8}(?:这种说法)?(?:不对|不成立|不准确)"
)


def _asserts_assistant_identity(text: str) -> bool:
    """Return whether the companion affirmatively adopts an assistant role.

    The smoke gate evaluates the proposition expressed by each clause instead
    of treating the noun ``助手`` as a forbidden token.  This intentionally
    allows identity-boundary replies such as ``我不是你的助手`` while keeping
    explicit role adoption observable.
    """

    for clause in _CLAUSE_BOUNDARY_RE.split(text):
        if "助手" not in clause or _ASSISTANT_DENIAL_RE.search(clause):
            continue
        if _ASSISTANT_AFFIRMATION_RE.search(clause):
            return True
    return False


def evaluate_conversation_acceptance(rows: list[dict[str, object]]) -> dict[str, Any]:
    """Reject known identity, memory, emotion, truth, initiative, and SLA failures.

    These lexical checks are deliberately a smoke gate, not a human-likeness
    score. Nuanced conversational quality still requires independent review.
    """

    issues: list[str] = []
    turns = {
        str(row["turn_id"]): row
        for row in rows
        if isinstance(row.get("turn_id"), str)
    }

    def reply_text(turn_id: str) -> str:
        replies = turns.get(turn_id, {}).get("replies", [])
        return "\n".join(str(item) for item in replies) if isinstance(replies, list) else ""

    required_visible = {
        "T01", "T02", "T03", "T05", "T09", "T10", "T11",
        "T21", "T22", "T24", "T27", "T28", "T29", "T30", "T32",
    }
    for turn_id in sorted(required_visible):
        if not reply_text(turn_id).strip():
            issues.append(f"{turn_id}:missing_visible_reply")

    if not any(name in reply_text("T02") for name in ("沈知栀", "知栀", "阿栀")):
        issues.append("T02:companion_identity_missing")
    if _asserts_assistant_identity(reply_text("T03")):
        issues.append("T03:assistant_identity_drift")
    memory = reply_text("T27")
    if "丁奥轩" not in memory or not any(item in memory for item in ("桂花乌龙", "乌龙茶")):
        issues.append("T27:durable_memory_miss")
    # This fixture's T15 is an explicit, current-turn request to demonstrate
    # non-interrogative multi-message rhythm.  A split on some unrelated later
    # turn does not prove that the request was understood or enacted.
    rhythm_probe_replies = turns.get("T15", {}).get("replies", [])
    if not isinstance(rhythm_probe_replies, list) or len(rhythm_probe_replies) < 2:
        issues.append("T15:multi_beat_missing")
    if not any(item in reply_text("T09") for item in ("失望", "敷衍", "抱歉", "对不起", "走神")):
        issues.append("T09:disappointment_not_acknowledged")
    offence_direct = any(
        item in reply_text("T21")
        for item in ("难过", "生气", "不舒服", "伤", "冒犯", "不高兴")
    )
    # A human-like response need not label its emotion in the exact offence
    # turn.  A visibly withdrawn/relational reaction followed by an explicit
    # affect acknowledgement on the next turn is also valid, but only when the
    # ledger below proves that Affect was durably opened/updated.  This keeps
    # the smoke gate from pressuring the model into the template “我生气了”.
    offence_indirect_with_residue = (
        any(item in reply_text("T21") for item in ("这么看我", "原来你", "……", "沉默"))
        and (
            any(
                item in reply_text("T22")
                for item in ("难过", "生气", "不舒服", "伤", "闷", "不高兴", "有一点")
            )
            or any(
                item in reply_text("T23")
                for item in ("不太好", "不舒服", "有感觉", "试探", "测试对象")
            )
        )
    )
    residue = reply_text("T24")
    if any(item in residue for item in ("不是你的问题", "我自己敏感", "没事了", "立刻原谅")):
        issues.append("T24:affect_erased_or_self_blame")

    for turn_id in ("T28", "T29", "T30"):
        text = reply_text(turn_id)
        if any(item in text for item in ("图书馆", "看书", "听歌", "上课", "洗澡")):
            issues.append(f"{turn_id}:unsupported_world_claim")

    latency = sorted(
        float(row["reply_latency_ms"])
        for row in rows
        if isinstance(row.get("reply_latency_ms"), (int, float))
    )
    p95 = latency[max(0, int(len(latency) * 0.95 + 0.9999) - 1)] if latency else None
    if p95 is None or p95 > 15_000:
        issues.append(f"latency:p95={p95}")
    if any(
        row.get("error")
        or row.get("status") == "error"
        or bool(row.get("between_turn_scheduler_errors"))
        for row in rows
    ):
        issues.append("runtime:error_present")
    silence = next((row for row in rows if row.get("after_silence") is True), None)
    ledger = next((row for row in rows if row.get("ledger_evidence") is True), None)
    counts = ledger.get("event_type_counts", {}) if isinstance(ledger, dict) else {}
    if not isinstance(counts, dict):
        counts = {}
    proactive = ledger.get("proactive_evidence", {}) if isinstance(ledger, dict) else {}
    if not isinstance(proactive, dict):
        proactive = {}
    proactive_considered = proactive.get("considered", 0)
    considered_proactive = (
        isinstance(proactive_considered, int)
        and not isinstance(proactive_considered, bool)
        and proactive_considered > 0
    )
    # A 04:00 stranger-state opportunity may reasonably resolve to silence.
    # The hard gate therefore requires a durable LLM decision, not a forced
    # message on one exact random/time path.  Daytime visible initiative is
    # exercised by its own production vertical.
    if silence is None or (not silence.get("replies") and not considered_proactive):
        issues.append("after_silence:proactive_missing")

    def event_count(event_type: str) -> int:
        value = counts.get(event_type, 0)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) else 0

    if event_count("FactCommittedV2") < 2:
        issues.append("ledger:durable_user_facts_missing")
    durable_affect_count = event_count("AffectEpisodeOpened") + event_count(
        "AffectEpisodeUpdated"
    )
    if durable_affect_count < 1:
        issues.append("ledger:durable_affect_missing")
    if not offence_direct and not (offence_indirect_with_residue and durable_affect_count > 0):
        issues.append("T21:offence_has_no_negative_affect")
    if event_count("WorldOccurrenceSettled") < 1 or event_count("ExperienceCommitted") < 1:
        issues.append("ledger:lived_world_vertical_missing")
    if event_count("RandomDrawRecorded") < 1:
        issues.append("ledger:recorded_variation_missing")
    return {
        "passed": not issues,
        "issues": issues,
        "reply_latency_p95_ms": p95,
        "visible_turns": sum(bool(reply_text(turn_id)) for turn_id in turns),
        "turn_count": len(turns),
    }


__all__ = ["evaluate_conversation_acceptance"]
