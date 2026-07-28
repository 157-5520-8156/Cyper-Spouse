"""Composition-only role identity for the open-life model provider."""

from __future__ import annotations

from typing import Literal, Protocol


class LifeDevelopmentCompletionModel(Protocol):
    model: str

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str: ...


class RoleBoundLifeDevelopmentModelAdapter:
    """Give one provider two explicit semantic-authority identities."""

    def __init__(
        self,
        *,
        model: LifeDevelopmentCompletionModel,
        role: Literal["world_author", "character_model"],
    ) -> None:
        model_id = str(getattr(model, "model", "")).strip() or type(model).__name__
        self.model = f"{model_id}/life-development/{role}"
        self._model = model

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        return await self._model.complete(messages, temperature=temperature)


__all__ = [
    "LifeDevelopmentCompletionModel",
    "RoleBoundLifeDevelopmentModelAdapter",
]
