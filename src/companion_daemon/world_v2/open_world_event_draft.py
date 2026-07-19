"""Bounded model choice for one temporary lived-world event.

The model is allowed to choose among authority-built situations and to write a
small subjective moment.  It never supplies a participant id, location ref,
event id, hash, time window, privacy level, or ledger mutation.
"""

from __future__ import annotations

import json
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .schema_core import FrozenModel, PrivacyClass


class OpenWorldEventModel(Protocol):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.4) -> str: ...


class OpenWorldEventSituation(FrozenModel):
    """One source-bound situation offered to the model."""

    token: str = Field(min_length=1, max_length=256)
    event_kind: Literal[
        "noticed_small_thing",
        "npc_friction",
        "unexpected_help",
        "pleasant_surprise",
        "minor_setback",
        "private_reflection",
    ]
    safe_summary: str = Field(min_length=1, max_length=240)
    participant_tokens: tuple[str, ...] = ()
    location_token: str = Field(min_length=1, max_length=256)
    privacy: PrivacyClass
    duration_minutes: int = Field(ge=5, le=60)

    @model_validator(mode="after")
    def participant_tokens_are_unique(self) -> "OpenWorldEventSituation":
        if len(self.participant_tokens) != len(set(self.participant_tokens)):
            raise ValueError("open-world situation participant tokens must be unique")
        return self


class OpenWorldEventDraft(FrozenModel):
    decision: Literal["select", "no_op"]
    situation_token: str | None = Field(default=None, max_length=256)
    moment: str | None = Field(default=None, min_length=1, max_length=720)
    # This is deliberately a required declaration for selected moments.  The
    # prose is a character's subjective impression, not external evidence.
    moment_scope: Literal["subjective"] | None = None
    model: str = Field(min_length=1, max_length=256)
    raw_output: str = Field(min_length=1)

    @model_validator(mode="after")
    def selected_shape_is_complete(self) -> "OpenWorldEventDraft":
        if self.decision == "select" and (
            self.situation_token is None or self.moment is None or self.moment_scope != "subjective"
        ):
            raise ValueError("selected open-world event needs a subjective situation and moment")
        if self.decision == "no_op" and (
            self.situation_token is not None or self.moment is not None or self.moment_scope is not None
        ):
            raise ValueError("open-world no_op cannot carry a situation or moment")
        return self


def parse_open_world_event_draft(
    *, raw: str, offered: tuple[OpenWorldEventSituation, ...], model: str
) -> OpenWorldEventDraft:
    if not offered:
        raise ValueError("open-world event requires at least one offered situation")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("open-world model did not return one valid JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("open-world model output must be an object")
    decision = value.get("decision")
    if decision == "no_op":
        if set(value) != {"decision"}:
            raise ValueError("open-world no_op must contain exactly decision")
        return OpenWorldEventDraft(decision="no_op", model=model, raw_output=raw)
    if decision != "select":
        raise ValueError("open-world decision must be select or no_op")
    if set(value) != {"decision", "situation_token", "moment", "moment_scope"}:
        raise ValueError(
            "open-world select must contain exactly decision, situation_token, moment, moment_scope"
        )
    token = value.get("situation_token")
    moment = value.get("moment")
    if value.get("moment_scope") != "subjective":
        raise ValueError("open-world moment_scope must be subjective")
    if not isinstance(token, str) or token not in {item.token for item in offered}:
        raise ValueError("unknown situation token")
    if not isinstance(moment, str) or not moment.strip():
        raise ValueError("open-world moment must be non-empty text")
    if "\n" in moment or "\r" in moment:
        raise ValueError("open-world moment must be one short visible paragraph")
    return OpenWorldEventDraft(
        decision="select", situation_token=token, moment=moment.strip(), moment_scope="subjective",
        model=model, raw_output=raw
    )


def _reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise ValueError("open-world model output has duplicate keys")
        value[key] = item
    return value


__all__ = [
    "OpenWorldEventDraft",
    "OpenWorldEventModel",
    "OpenWorldEventSituation",
    "parse_open_world_event_draft",
]
