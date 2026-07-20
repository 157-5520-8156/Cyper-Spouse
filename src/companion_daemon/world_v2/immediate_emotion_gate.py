"""Semantic same-turn emotion scheduling gate with a hard latency budget.

This gate answers exactly one scheduling question: is this inbound message
emotionally material enough to pay for the synchronous inner-appraisal lane
*before* the visible reply is compiled?  It never interprets the emotion
itself — the appraisal model still owns meaning, intensity, attribution,
suppression and persistence.

The durable ``interaction_appraisal`` trigger is opened unconditionally at
ingress (see ``WorldRuntime.ingest``), independent of this gate.  A gate
failure therefore only affects *when* the appraisal runs (same turn vs. the
background trigger drain), never *whether* it runs.  That is what makes the
fail-open-to-keywords policy below safe.

Decision order (lowest average latency first):

1. Keyword cue table hit -> immediately ``True`` with zero model calls.
2. Keyword miss -> ask the local small model (Qwen-class checkpoint on the
   OpenAI-compatible loopback endpoint) with a strict ``asyncio.timeout``.
3. Model timeout / transport failure / invalid output -> fall back to the
   keyword verdict, which at this point is ``False``.

Worst-case added latency on the reply-critical path is therefore bounded by
``timeout_seconds`` (default 2.5s) and only paid for keyword-miss turns; a
keyword hit and a disabled gate both add ~0.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Sequence

from .chat_model_deliberation_adapter import ChatCompletionModel
from .model_json import extract_json_object_text

# The gate sits on the visible-reply critical path.  2.5s matches the other
# bounded recovery lanes in this package (_RECOVERY_MODEL_TIMEOUT_SECONDS)
# and is the hard upper bound this gate may add to one turn.
# The gate sits on the visible reply path for every keyword-miss message, so
# its worst case is a direct tax on responsiveness.  A healthy local 1.7B
# model answers this one-boolean classification well under a second; when it
# cannot, waiting longer buys nothing the keyword fallback does not provide.
_DEFAULT_TIMEOUT_SECONDS = 1.5
# Bounded prompt material: the judgment only needs the current message plus a
# tiny slice of her own recent words as contrast (to expose sudden coldness /
# withdrawal), never the full context capsule.
_MAX_MESSAGE_CHARS = 600
_MAX_CONTEXT_ITEMS = 2
_MAX_CONTEXT_ITEM_CHARS = 120

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是中文聊天的情绪信号初筛器，只做调度判断，不解读情绪本身。"
    "判断这条用户消息是否携带需要在回复前立即处理的关系/情绪信号。"
    "包括但不限于：明确的负面情绪；以及不带情绪词的冷暴力、阴阳怪气、敷衍、"
    "突然的冷淡疏远、赌气式简短回应、被忽视后的试探、道歉或关系修复。"
    "普通的日常分享、提问、闲聊输出 false。"
    '只输出一个 JSON 对象：{"immediate": true} 或 {"immediate": false}，'
    "禁止 Markdown、解释和任何其他字段。"
)


class SemanticImmediateEmotionGate:
    """Bounded local-model classifier for the same-turn emotion decision.

    ``assess`` returns ``True``/``False`` only for a well-formed model verdict
    and ``None`` for every failure mode (timeout, transport error, garbage
    output).  Mapping ``None`` back to the keyword verdict belongs to the
    caller via ``resolve_immediate_emotion_gate`` so failure semantics stay in
    one auditable place.
    """

    def __init__(
        self,
        *,
        model: ChatCompletionModel,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("semantic emotion gate timeout must be positive")
        self._model = model
        self._timeout_seconds = timeout_seconds

    async def assess(
        self,
        *,
        text: str,
        recent_companion_texts: Sequence[str] = (),
    ) -> bool | None:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                raw = await self._model.complete(
                    self._messages(
                        text=text, recent_companion_texts=recent_companion_texts
                    ),
                    temperature=0.0,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "semantic immediate-emotion gate unavailable: %s: %s",
                type(exc).__name__,
                str(exc)[:240],
            )
            return None
        verdict = _parse_verdict(raw)
        if verdict is None:
            logger.warning(
                "semantic immediate-emotion gate returned an invalid verdict; falling back"
            )
        return verdict

    @staticmethod
    def _messages(
        *, text: str, recent_companion_texts: Sequence[str]
    ) -> list[dict[str, str]]:
        parts: list[str] = []
        contrast = [
            item.strip()[:_MAX_CONTEXT_ITEM_CHARS]
            for item in recent_companion_texts
            if isinstance(item, str) and item.strip()
        ][-_MAX_CONTEXT_ITEMS:]
        if contrast:
            parts.append("她自己最近说过（仅作语气对照，不要评判这些）：")
            parts.extend(f"- {item}" for item in contrast)
        parts.append(f"只判断这条当前用户消息：\n{text.strip()[:_MAX_MESSAGE_CHARS]}")
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)},
        ]


def _parse_verdict(raw: object) -> bool | None:
    """Strictly extract ``{"immediate": bool}``; anything else is a failure."""

    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(extract_json_object_text(raw))
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    verdict = value.get("immediate")
    if not isinstance(verdict, bool):
        return None
    return verdict


async def resolve_immediate_emotion_gate(
    *,
    keyword_hit: bool,
    text: str | None,
    gate: SemanticImmediateEmotionGate | None,
    recent_companion_texts: Sequence[str] = (),
) -> bool:
    """Combine the keyword table and the semantic gate into one decision.

    A keyword hit is authoritative and free: it never spends a model call.
    Only a keyword miss consults the semantic gate, and every gate failure
    falls back to the keyword verdict (``False`` here), so the worst case is
    exactly the pre-semantic behavior plus the gate's bounded timeout.
    """

    if keyword_hit:
        return True
    if gate is None or not isinstance(text, str) or not text.strip():
        return False
    verdict = await gate.assess(
        text=text, recent_companion_texts=recent_companion_texts
    )
    if verdict is None:
        return False
    return verdict


__all__ = [
    "SemanticImmediateEmotionGate",
    "resolve_immediate_emotion_gate",
]
