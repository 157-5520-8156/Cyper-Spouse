"""Neutral asynchronous chat-completion transport contract.

This protocol grants no character semantics, world authority, or fallback
policy. Modules may depend on it without importing a private CharacterInterior
wire implementation.
"""

from __future__ import annotations

from typing import Protocol


class ChatCompletionModel(Protocol):
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str: ...


__all__ = ["ChatCompletionModel"]
