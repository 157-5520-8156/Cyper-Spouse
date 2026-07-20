"""Deployment-owned attachment bytes for optional perception Actions."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field

from .schema_core import FrozenModel
from .schemas import Action


class PerceptionInputDescriptor(FrozenModel):
    """Immutable content identity resolved before Acceptance."""

    attachment_ref: str = Field(min_length=1, max_length=512)
    analysis_kind: Literal["vision", "transcription"]
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class PerceptionInputSource(Protocol):
    """Durable source capable of describing and later reopening exact bytes.

    ``describe`` is side-effect free and runs before Acceptance. ``resolve``
    runs only after ActionPump repeats capability/consent/privacy checks.
    Implementations must survive process restart for every accepted Action.
    """

    def describe(
        self, *, attachment_ref: str, analysis_kind: Literal["vision", "transcription"]
    ) -> PerceptionInputDescriptor: ...

    async def resolve(self, action: Action) -> tuple[str, str, str]: ...


__all__ = ["PerceptionInputDescriptor", "PerceptionInputSource"]
