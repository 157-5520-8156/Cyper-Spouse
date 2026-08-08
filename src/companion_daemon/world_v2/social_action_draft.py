"""Historical inert social-action draft contract.

The model chooses inside a small *possibility space*; it does not receive or
return ledger authority.  IDs, hashes, targets, due windows and budgets are
derived from the pinned request by this adapter and by Acceptance.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


class SocialActionDraft(FrozenModel):
    """Non-authoritative linguistic/temporal suggestion returned by a model."""

    choice: Literal["reply_now", "defer", "no_reply"]
    response_text: str | None = Field(default=None, min_length=1, max_length=4_096)
    delay_seconds: int | None = Field(default=None, ge=1, le=86_400)
    expires_after_seconds: int | None = Field(default=None, ge=2, le=172_800)
    brief_rationale: str = Field(min_length=1, max_length=240)
    confidence: int = Field(default=5_000, ge=0, le=10_000)

    @model_validator(mode="after")
    def choice_has_only_its_linguistic_fields(self) -> "SocialActionDraft":
        if self.choice == "no_reply":
            if any(value is not None for value in (self.response_text, self.delay_seconds, self.expires_after_seconds)):
                raise ValueError("no_reply cannot smuggle payload or scheduling authority")
            return self
        if self.response_text is None:
            raise ValueError("a visible social action requires response_text")
        if self.choice == "reply_now":
            if self.delay_seconds is not None or self.expires_after_seconds is not None:
                raise ValueError("reply_now cannot select a delayed window")
            return self
        if self.delay_seconds is None or self.expires_after_seconds is None:
            raise ValueError("defer requires a bounded relative window")
        if self.expires_after_seconds <= self.delay_seconds:
            raise ValueError("defer expiry must follow its opening delay")
        return self


__all__ = ["SocialActionDraft"]
