"""Mechanical identities for generic, role-authored interaction acts.

The helpers in this module only canonicalize already-authored coordinates.  They
do not classify an act kind, infer an object, or decide a transition.  Keeping
the formulas public lets candidate compilation, domain materialization, and
ledger replay verify the same bytes independently.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
import unicodedata

from pydantic import BaseModel

from .schema_core import canonicalize_json_value


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return value


def interaction_act_canonical_hash(value: object) -> str:
    encoded = json.dumps(
        canonicalize_json_value(_json_value(value)),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_interaction_act_source_text(value: str) -> str:
    """Normalize Unicode representation while preserving selected boundaries."""

    if type(value) is not str:
        raise TypeError("interaction act source text must be a string")
    return unicodedata.normalize("NFC", value)


def interaction_act_overlapping_occurrence_count(
    *, source_text: str, selected_text: str
) -> int:
    """Count exact occurrences, including overlaps, without interpreting text."""

    if type(source_text) is not str or type(selected_text) is not str:
        raise TypeError("interaction act exact text coordinates must be strings")
    if not selected_text:
        raise ValueError("interaction act selected text must not be empty")
    count = 0
    offset = 0
    while True:
        found = source_text.find(selected_text, offset)
        if found < 0:
            return count
        count += 1
        offset = found + 1


def interaction_act_conversation_ref(
    *,
    world_id: str,
    channel: str,
    participant_refs: tuple[str, ...],
) -> str:
    """Derive a stable hard identity without reading conversational semantics."""

    if not world_id or not channel or not 2 <= len(participant_refs) <= 9:
        raise ValueError("interaction act conversation coordinates are incomplete")
    if any(not item for item in participant_refs) or len(participant_refs) != len(
        set(participant_refs)
    ):
        raise ValueError("interaction act conversation participants are invalid")
    digest = interaction_act_canonical_hash(
        {
            "contract": "interaction-act-conversation-ref.1",
            "world_id": world_id,
            "channel": channel,
            "participant_refs": sorted(participant_refs),
        }
    )
    return f"conversation:interaction-act:sha256:{digest}"


def interaction_act_object_ref(
    *,
    conversation_ref: str,
    object_label: str,
    opening_source_event_ref: str,
    opening_source_payload_hash: str,
) -> str:
    if not all(
        (
            conversation_ref,
            object_label,
            opening_source_event_ref,
            opening_source_payload_hash,
        )
    ):
        raise ValueError("interaction act object coordinates are incomplete")
    digest = interaction_act_canonical_hash(
        {
            "conversation_ref": conversation_ref,
            "object_label": object_label,
            "opening_source_event_ref": opening_source_event_ref,
            "opening_source_payload_hash": opening_source_payload_hash,
        }
    )
    return f"interaction-object:sha256:{digest}"


def interaction_act_role_output_hash(role_output: BaseModel | Mapping[str, Any]) -> str:
    return interaction_act_canonical_hash(role_output)


def interaction_act_id(
    *,
    world_id: str,
    conversation_ref: str,
    source_ref: BaseModel | Mapping[str, Any],
    role_output_hash: str,
) -> str:
    if not world_id or not conversation_ref or not role_output_hash:
        raise ValueError("interaction act identity coordinates are incomplete")
    digest = interaction_act_canonical_hash(
        {
            "world_id": world_id,
            "conversation_ref": conversation_ref,
            "source_ref": _json_value(source_ref),
            "role_output_hash": role_output_hash,
        }
    )
    return f"interaction-act:sha256:{digest}"


def interaction_act_transition_id(
    *,
    interaction_act_ref: str,
    operation: str,
    source_ref: BaseModel | Mapping[str, Any],
    role_output_hash: str,
) -> str:
    if not interaction_act_ref or not operation or not role_output_hash:
        raise ValueError("interaction act transition coordinates are incomplete")
    digest = interaction_act_canonical_hash(
        {
            "interaction_act_id": interaction_act_ref,
            "operation": operation,
            "source_ref": _json_value(source_ref),
            "role_output_hash": role_output_hash,
        }
    )
    return f"interaction-act-transition:sha256:{digest}"


__all__ = [
    "interaction_act_canonical_hash",
    "interaction_act_conversation_ref",
    "interaction_act_id",
    "interaction_act_object_ref",
    "interaction_act_overlapping_occurrence_count",
    "interaction_act_role_output_hash",
    "interaction_act_transition_id",
    "normalize_interaction_act_source_text",
]
