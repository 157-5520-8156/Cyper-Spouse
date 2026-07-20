"""Normalize an explicitly mutual future continuation into an internal wait.

This module records interaction semantics; it does not schedule or send a
message.  A later response-gap candidate still passes through relationship,
availability, social-initiative, and RandomAuthority policy.
"""

from __future__ import annotations

import re


_GENERIC_FAREWELL = re.compile(r"(?:晚安|拜拜|再见|改天见|明天见|下次见|先走了|先走啦)")
_RESUME_TIME = r"(?:晚点|等会儿?|一会儿?|稍后|回头|忙完|处理完|做完|回来|有空)"
_CONVERSATION = r"(?:聊|说|继续|接着|找你|回你)"
_COUNTERPART_RESUME = re.compile(
    rf"{_RESUME_TIME}.{{0,10}}{_CONVERSATION}"
    rf"|{_CONVERSATION}.{{0,5}}{_RESUME_TIME}"
    r"|(?:忙完|处理完|做完).{0,8}(?:回来|再).{0,6}(?:聊|说)"
)
_COMPANION_ACCEPTS_RESUME = re.compile(
    r"等你.{0,12}(?:回来|忙完|处理完|做完|有空)"
    rf"|{_RESUME_TIME}.{{0,10}}(?:再|继续|接着).{{0,5}}(?:聊|说)"
    rf"|(?:再|继续|接着).{{0,5}}(?:聊|说).{{0,8}}{_RESUME_TIME}"
    rf"|(?:好|行|嗯|可以).{{0,6}}{_RESUME_TIME}.{{0,6}}(?:聊|说)"
)


def establishes_mutual_future_continuation(
    *, trigger_text: str, visible_texts: tuple[str, ...]
) -> bool:
    """Whether both sides explicitly leave this conversation open for later."""

    trigger = trigger_text.strip()
    if not trigger or not visible_texts:
        return False
    # A conventional goodbye is not an interaction bid merely because it
    # names another day.  A concrete resume phrase can still win when present.
    if _GENERIC_FAREWELL.search(trigger) and not _COUNTERPART_RESUME.search(trigger):
        return False
    if not _COUNTERPART_RESUME.search(trigger):
        return False
    return any(_COMPANION_ACCEPTS_RESUME.search(text) for text in visible_texts)


def normalize_future_continuation_expectation(
    *, trigger_text: str, visible_texts: tuple[str, ...], draft: dict[str, object]
) -> dict[str, object]:
    """Add one conservative expectation when the model omitted an obvious one."""

    if draft.get("response_expectation") is not None:
        return draft
    if draft.get("timing_choice", "now") != "now":
        return draft
    if not establishes_mutual_future_continuation(
        trigger_text=trigger_text, visible_texts=visible_texts
    ):
        return draft
    normalized = dict(draft)
    normalized["response_expectation"] = {
        "hoped_response": "对方忙完后按自己的节奏回来继续聊天",
        "pressure_bp": 900,
        "importance_bp": 3_500,
        "wait_seconds": 1_800,
        "expires_after_seconds": 43_200,
    }
    return normalized


__all__ = [
    "establishes_mutual_future_continuation",
    "normalize_future_continuation_expectation",
]
