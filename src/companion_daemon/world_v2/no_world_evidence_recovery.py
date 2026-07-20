"""Natural, claim-free recovery for questions lacking world evidence.

The recovery is local and deterministic: no second model round trip, no world
mutation, and no assertion about whether an event did or did not occur.  Its
variation key is an immutable observation ref, while accepted recent dialogue
prevents an immediately repeated visible sentence.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal


WorldProbeIntent = Literal["current", "recent", "setting", "general"]

_SETTING = re.compile(r"(?:设定|角色|人设|背景|剧本)")
_CURRENT = re.compile(r"(?:现在|此刻|这会儿|在干嘛|在干什么|干啥|忙啥|做啥|正在做)")
_RECENT = re.compile(r"(?:今天|今晚|最近|刚才|刚刚|发生|经历|印象深)")
_EVIDENCE_QUESTION = re.compile(
    r"(?:在干嘛|在干什么|干啥|忙啥|做啥|正在做|做什么|做了什么|干什么|忙什么|过得怎么样|发生|发生了什么|经历|印象深|实际|现实|亲历|设定|角色|人设)"
)
_QUESTION_SHAPE = re.compile(
    r"(?:吗|什么|啥|哪|干嘛|怎么样|有没有|还是|呢|问的是|告诉我|说说|how|what|didyou|areyou)", re.I
)
_NEGATED_EVIDENCE_PROBE = re.compile(
    r"(?:不是在问|并非在问|不在问|没在问|不是要问|并非要问).{0,12}"
    r"(?:在干嘛|在干什么|干啥|忙啥|做啥|正在做|做什么|做了什么|发生|经历)",
    re.S,
)

# Every candidate stays claim-free: it may describe the current exchange or
# the companion's own recall/attention (inner experience), but never asserts
# that something did or did not happen offline.  The phrasing is deliberately
# conversational — an epistemic boundary should sound like a person choosing
# honesty, not like a database reporting a missing row.
_STRATEGIES: dict[WorldProbeIntent, tuple[str, ...]] = {
    "setting": (
        "设定是设定，真发生过的事是另一回事，我不想混着讲。",
        "背景可以聊，但把它当成亲身经历讲出来就是在骗你了。",
        "人设能说明我是什么样的人，可补不出一件真事，这段我不编。",
        "我可以跟你聊设定，但不会把它冒充成我的经历。",
    ),
    "current": (
        # These are deliberately about the current exchange, not a fabricated
        # offline activity.  A reply is itself a real, local presence: the
        # companion has paused to receive this message and is answering it.
        # Avoiding "没有数据" here matters because an empty activity slice is
        # an epistemic boundary, not an absence of personhood or attention.
        "刚把手上的事放一放，先回你。",
        "这会儿我先把注意力放在你这边。",
        "手上的事先暂停一下，我在听你说。",
        "你这条进来了，我先和你说话。",
    ),
    "recent": (
        "让我想想……一下子还真没想起有什么能拿出来讲的，想起来我再主动跟你说？",
        "唔，这会儿翻了翻记忆没翻到合适的，不想现编一个糊弄你。",
        "我想了下，没什么现成能讲的。你先说你的，我边听边想。",
        "一时想不起来，硬编一件又没意思，回头想到了跟你讲。",
    ),
    "general": (
        "这个我还真答不上来，就不瞎编了。",
        "说不好诶，我不想拿一个听起来合理的说法糊弄你。",
        "这我真不知道，你要不给我点提示？",
        "答不上来……我宁可承认这个，也不想顺口编一个。",
    ),
}


def classify_world_probe_intent(text: str) -> WorldProbeIntent:
    """Classify only the epistemic shape of a probe, never a reply behavior."""

    if _SETTING.search(text):
        return "setting"
    if _CURRENT.search(text):
        return "current"
    if _RECENT.search(text):
        return "recent"
    return "general"


def is_companion_world_evidence_probe(text: str | None) -> bool:
    """Recognize a request for the companion's actual lived-world evidence.

    This is only an epistemic category.  It does not prescribe whether a
    normal model should answer, joke, defer, or redirect when evidence exists.
    """

    if not text or _NEGATED_EVIDENCE_PROBE.search(text):
        return False
    if not _EVIDENCE_QUESTION.search(text):
        return False
    has_time_or_setting = bool(
        _SETTING.search(text) or _CURRENT.search(text) or _RECENT.search(text)
    )
    return has_time_or_setting and bool(_QUESTION_SHAPE.search(text))


def recover_without_world_evidence(
    *, trigger_text: str, source_ref: str, recent_visible_texts: tuple[str, ...]
) -> str:
    """Choose a bounded claim-free expression and avoid recent verbatim reuse."""

    intent = classify_world_probe_intent(trigger_text)
    candidates = _STRATEGIES[intent]
    offset = int.from_bytes(
        hashlib.sha256(
            f"no-world-evidence-recovery.1\0{source_ref}\0{intent}".encode("utf-8")
        ).digest()[:4],
        "big",
    ) % len(candidates)
    recent = set(recent_visible_texts)
    for step in range(len(candidates)):
        candidate = candidates[(offset + step) % len(candidates)]
        if candidate not in recent:
            return candidate
    # The dialogue compiler retains at most four companion turns and every
    # intent owns four alternatives, so this is only a defensive fallback.
    return candidates[offset]


def claim_free_reply_already_given(
    *, trigger_text: str, recent_visible_texts: tuple[str, ...]
) -> bool:
    """Whether every claim-free variant for this probe intent was already said.

    Consecutive probes deliberately receive varied, non-repeating lines (see
    :func:`recover_without_world_evidence`).  Only once the whole pool is
    spent would the next recovery parrot a sentence verbatim — and within one
    ongoing exchange the honest answer then is no answer at all: silence is a
    first-class expression in this world, and the person already heard her.
    """

    candidates = _STRATEGIES[classify_world_probe_intent(trigger_text)]
    recent = set(recent_visible_texts)
    return all(candidate in recent for candidate in candidates)


def recent_companion_texts(context: object) -> tuple[str, ...]:
    """Read only provider-visible companion text from a verified Context view."""

    if not isinstance(context, dict) or not isinstance(context.get("slices"), dict):
        return ()
    lane = context["slices"].get("recent_dialogue")
    if not isinstance(lane, dict) or lane.get("availability") != "available":
        return ()
    items = lane.get("items")
    if not isinstance(items, list):
        return ()
    texts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        speaker = value.get("speaker")
        text = value.get("text")
        if speaker in {"companion", "assistant"} and isinstance(text, str) and text:
            texts.append(text)
    return tuple(texts)


__all__ = [
    "claim_free_reply_already_given",
    "classify_world_probe_intent",
    "is_companion_world_evidence_probe",
    "recent_companion_texts",
    "recover_without_world_evidence",
]
