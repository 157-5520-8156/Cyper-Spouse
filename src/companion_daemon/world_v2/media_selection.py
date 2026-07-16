"""Narrow, model-suggestible selection contract for P1 media authorization."""

from __future__ import annotations

from typing import Literal
import hashlib
import json

from pydantic import Field, model_validator

from .schema_core import FrozenModel


class MediaSelection(FrozenModel):
    """A choice label, never an opportunity or image-generation permission."""

    candidate_id: str = Field(min_length=1, max_length=256)
    family: Literal["life_share", "character_media"]
    delivery_mode: Literal["preview", "automatic"] = "preview"
    media_privacy_ceiling: Literal["ordinary", "personal", "intimate"] = "ordinary"
    expression_charge_ceiling: Literal["none", "subtle", "charged", "veiled"] = "none"
    recipient_ref: str | None = Field(default=None, min_length=1, max_length=512)
    private_expression_basis_ref: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def p1_public_preview_shape_is_closed(self) -> "MediaSelection":
        if self.delivery_mode == "automatic":
            raise ValueError("media selection automatic delivery requires a later acceptance lane")
        if self.family == "life_share" and (
            self.recipient_ref is not None
            or self.private_expression_basis_ref is not None
            or self.media_privacy_ceiling != "ordinary"
            or self.expression_charge_ceiling != "none"
        ):
            raise ValueError("life_share selection may not carry private or expressive authority")
        if (self.recipient_ref is None) != (self.private_expression_basis_ref is None):
            raise ValueError("private media selection recipient and basis must bind together")
        return self


def media_selection_hash(selection: MediaSelection) -> str:
    return hashlib.sha256(json.dumps(selection.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


__all__ = ["MediaSelection", "media_selection_hash"]
