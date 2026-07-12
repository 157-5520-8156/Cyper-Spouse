"""Bounded, non-CoT envelope for one model-led companion turn.

The schema deliberately accepts the former ``WorldReplyJSON`` shape.  Optional
fields add delivery rhythm and a display choice without promoting free-form
reasoning to world truth or making older adapters incompatible.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from companion_daemon.world import WorldError


@dataclass(frozen=True)
class ExpressionBeat:
    """A visible segment whose text must exactly compose the reply."""

    text: str
    delay_ms: int = 0


@dataclass(frozen=True)
class PrivateImpressionProposal:
    """A fallible inner reading that still needs the World commit policy."""

    kind: str
    summary: str
    confidence: float


@dataclass(frozen=True)
class MindProposal:
    """A bounded model proposal; factual authority remains outside this type."""

    candidate: dict[str, object]
    expression_beats: tuple[ExpressionBeat, ...] = ()
    display_strategy: str | None = None
    private_impression: PrivateImpressionProposal | None = None


def parse_mind_proposal(raw: str) -> MindProposal:
    """Parse a compatible reply envelope and locally discard malformed extras.

    The reply candidate keeps the existing four-field contract.  Optional
    beats are accepted only when their concatenated text equals the candidate,
    so a malformed choreography can never alter or partially send prose.
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorldError("world reply must be JSON") from exc
    if not isinstance(payload, dict):
        raise WorldError("world reply must be a JSON object")
    candidate = {
        "reply_text": str(payload.get("reply_text") or "").strip(),
        "mentioned_event_ids": payload.get("mentioned_event_ids", []),
        "proposed_action_ids": payload.get("proposed_action_ids", []),
        "claims": payload.get("claims", []),
    }
    return MindProposal(
        candidate=candidate,
        expression_beats=_parse_expression_beats(
            payload.get("expression_beats"), candidate["reply_text"]
        ),
        display_strategy=_parse_display_strategy(payload.get("display_strategy")),
        private_impression=_parse_private_impression(payload.get("private_impression")),
    )


def _parse_expression_beats(raw: object, reply_text: object) -> tuple[ExpressionBeat, ...]:
    if not isinstance(raw, list) or not raw or len(raw) > 3:
        return ()
    beats: list[ExpressionBeat] = []
    for item in raw:
        if not isinstance(item, dict):
            return ()
        text = str(item.get("text") or "").strip()
        delay = item.get("delay_ms", 0)
        if not text or len(text) > 360 or type(delay) is not int or not 0 <= delay <= 20_000:
            return ()
        beats.append(ExpressionBeat(text=text, delay_ms=delay))
    if "".join(item.text for item in beats) != str(reply_text):
        return ()
    if beats[0].delay_ms != 0:
        return ()
    return tuple(beats)


def _parse_display_strategy(raw: object) -> str | None:
    value = str(raw or "").strip()
    if not value or len(value) > 80:
        return None
    return value


def _parse_private_impression(raw: object) -> PrivateImpressionProposal | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    confidence = raw.get("confidence")
    if (
        kind
        not in {"possible_disappointment", "possible_confusion"}
        or not summary
        or len(summary) > 240
        or "\n" in summary
        or type(confidence) not in {float, int}
        or not 0.0 < float(confidence) <= 1.0
    ):
        return None
    return PrivateImpressionProposal(kind, summary, float(confidence))
