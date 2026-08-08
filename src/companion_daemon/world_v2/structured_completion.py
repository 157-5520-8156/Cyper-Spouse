"""Shared provider boundary for model-authored JSON objects.

Semantic ownership remains with the role model.  This adapter only asks a
capable provider to preserve the JSON wire contract; local schema and source
closure validators remain authoritative.
"""

from __future__ import annotations

from typing import Protocol


class TextCompletionModel(Protocol):
    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2
    ) -> str: ...


async def complete_json_object(
    model: TextCompletionModel,
    messages: list[dict[str, str]],
    *,
    temperature: float,
    tools: list[dict[str, object]] | None = None,
    tool_choice: object | None = None,
) -> str:
    """Prefer the provider JSON-object transport, with protocol fallback.

    The fallback keeps local/test and non-DeepSeek providers compatible.  It
    is not a semantic fallback and never invents a role decision.  Supplying
    ``tools`` selects a required provider contract and deliberately disables
    that plain protocol fallback.
    """

    complete_json = getattr(model, "complete_json", None)
    if tools is not None:
        if not callable(complete_json):
            raise TypeError("required-tool JSON completion is unavailable")
        return await complete_json(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
    if callable(complete_json):
        return await complete_json(messages, temperature=temperature)
    return await model.complete(messages, temperature=temperature)
