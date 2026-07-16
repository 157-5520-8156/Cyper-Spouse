"""Bounded model choice for an already-opened P1 media candidate set.

The model sees short, non-authoritative labels and opaque tokens only.  It may
decline or name one offered token; it cannot form a snapshot, alter privacy,
or authorize a provider action.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .schema_core import FrozenModel


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class MediaSelectionDraftModel(Protocol):
    model: str

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


class MediaCandidateChoice(FrozenModel):
    token: str = Field(min_length=1, max_length=512)
    safe_summary: str = Field(min_length=1, max_length=480)
    advisory: dict[str, object] = Field(default_factory=dict)


class MediaSelectionCapsule(FrozenModel):
    candidates: tuple[MediaCandidateChoice, ...] = Field(default=(), max_length=32)
    draw_suggestion: dict[str, object] | None = None

    @model_validator(mode="after")
    def tokens_are_unique(self) -> "MediaSelectionCapsule":
        if len({item.token for item in self.candidates}) != len(self.candidates):
            raise ValueError("media selection candidate tokens must be unique")
        return self


class MediaSelectionDraft(FrozenModel):
    decision: Literal["no_op", "select"]
    token: str | None = None
    model: str | None = None
    raw_output_hash: str | None = None
    normalized_output_hash: str | None = None


class MediaSelectionDraftAdapter:
    """A narrow model seam; callers map its token back to a ledger candidate."""

    def __init__(self, *, model: MediaSelectionDraftModel, temperature: float = 0.2) -> None:
        if not model.model or not 0 <= temperature <= 2:
            raise ValueError("media selection draft model configuration is invalid")
        self._model, self._temperature = model, temperature

    async def deliberate(self, *, capsule: MediaSelectionCapsule) -> MediaSelectionDraft:
        if not capsule.candidates:
            return MediaSelectionDraft(decision="no_op")
        raw = await self._model.complete(
            [
                {"role": "system", "content": (
                    "Choose one offered candidate token or decline. Return exactly JSON: "
                    '{"decision":"no_op"} or {"decision":"select","token":"offered"}. '
                    "A token is not permission to create, send, or describe an image."
                )},
                {"role": "user", "content": json.dumps({
                    "candidates": [item.model_dump() for item in capsule.candidates],
                    "draw_suggestion": capsule.draw_suggestion,
                }, ensure_ascii=False)},
            ],
            temperature=self._temperature,
        )
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("media selection model did not return JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("media selection model did not return an object")
        if value == {"decision": "no_op"}:
            normalized = '{"decision":"no_op"}'
            return MediaSelectionDraft(decision="no_op", model=self._model.model, raw_output_hash=_hash(raw), normalized_output_hash=_hash(normalized))
        token = value.get("token")
        offered = {item.token for item in capsule.candidates}
        if set(value) != {"decision", "token"} or value.get("decision") != "select" or token not in offered:
            raise ValueError("media selection model chose an unknown candidate")
        normalized = json.dumps({"decision": "select", "token": token}, separators=(",", ":"))
        return MediaSelectionDraft(decision="select", token=token, model=self._model.model, raw_output_hash=_hash(raw), normalized_output_hash=_hash(normalized))


__all__ = ["MediaCandidateChoice", "MediaSelectionCapsule", "MediaSelectionDraft", "MediaSelectionDraftAdapter", "MediaSelectionDraftModel"]
