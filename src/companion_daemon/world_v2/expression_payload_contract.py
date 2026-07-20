"""Provider-neutral immutable payload contract for visible expression Actions."""

from __future__ import annotations

import json


QQ_REACTION_OPTIONS: tuple[tuple[str, str], ...] = (
    ("heart", "heart"),
    ("haha", "laughing"),
    ("wow", "surprised"),
    ("sad", "sad"),
    ("fire", "enthusiastic"),
    ("like", "approval"),
    ("star", "appreciation"),
    ("bolt", "struck-or-startled"),
)

QQ_STICKER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("qq-face:1", "pout"),
    ("qq-face:5", "tears"),
    ("qq-face:6", "bashful"),
    ("qq-face:13", "surprised"),
    ("qq-face:14", "smile"),
    ("qq-face:21", "cute"),
    ("qq-face:66", "heart"),
    ("qq-face:76", "thumbs-up"),
    ("qq-face:179", "doge"),
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def validate_materialized_expression_payload(
    *,
    action_kind: str,
    content_type: str,
    body: str,
    expected_provider_message_id: str | None,
) -> None:
    """Reverse-check provider-sensitive bytes at the Acceptance seam."""

    if action_kind in {"reply", "followup", "proactive_message"}:
        if content_type != "text/plain":
            raise ValueError("text expression content type is invalid")
        return
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError("expression payload is not canonical JSON") from exc
    if not isinstance(value, dict) or _canonical_json(value) != body:
        raise ValueError("expression payload is not canonical JSON")
    if action_kind == "reaction":
        if value != {
            "provider_message_id": expected_provider_message_id,
            "reaction_id": value.get("reaction_id"),
            "version": "expression-reaction.1",
        }:
            raise ValueError("reaction payload does not bind the source message")
        if value.get("reaction_id") not in {item[0] for item in QQ_REACTION_OPTIONS}:
            raise ValueError("reaction payload token is not installed")
        return
    if action_kind == "sticker":
        if value != {
            "sticker_id": value.get("sticker_id"),
            "version": "expression-sticker.1",
        } or value.get("sticker_id") not in {item[0] for item in QQ_STICKER_OPTIONS}:
            raise ValueError("sticker payload token is not installed")
        return
    if action_kind == "typing":
        if value != {"state": "composing", "version": "expression-typing.1"}:
            raise ValueError("typing payload is invalid")
        return
    raise ValueError("expression payload action kind is not installed")


__all__ = [
    "QQ_REACTION_OPTIONS",
    "QQ_STICKER_OPTIONS",
    "validate_materialized_expression_payload",
]
