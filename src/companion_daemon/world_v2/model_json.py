"""Tolerant extraction of one JSON object from a chat-model text answer.

Chat providers frequently wrap a requested JSON object in Markdown code
fences or add a short lead-in sentence.  Authority never comes from this
module: it only recovers the bytes that are then strictly validated by the
caller's closed schema, so a lenient extraction cannot widen what a model is
allowed to say.
"""

from __future__ import annotations


def extract_json_object_text(raw: str) -> str:
    """Return the most plausible single-JSON-object substring of ``raw``.

    The input is returned unchanged when it already looks like a bare JSON
    object.  Otherwise the first balanced ``{...}`` block is extracted,
    which covers code fences, chatty prefixes, and trailing explanations.
    Callers still run ``json.loads`` plus strict schema validation on the
    result; when no object exists the original text is returned so the
    caller's error path reports the real payload.
    """

    text = raw.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    if start < 0:
        return raw
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return raw


__all__ = ["extract_json_object_text"]
