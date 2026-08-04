from __future__ import annotations

import pytest

from companion_daemon.world_v2.structured_completion import complete_json_object


class _JsonCapableModel:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def complete(self, messages, *, temperature=0.2):
        del messages, temperature
        self.calls.append("plain")
        return "plain"

    async def complete_json(self, messages, *, temperature=0.2):
        del messages, temperature
        self.calls.append("json")
        return '{}'


class _PlainModel:
    async def complete(self, messages, *, temperature=0.2):
        del messages, temperature
        return '{}'


@pytest.mark.asyncio
async def test_structured_completion_prefers_provider_json_object_transport() -> None:
    model = _JsonCapableModel()

    result = await complete_json_object(model, [], temperature=0.4)

    assert result == '{}'
    assert model.calls == ["json"]


@pytest.mark.asyncio
async def test_structured_completion_keeps_plain_protocol_compatible() -> None:
    assert await complete_json_object(_PlainModel(), [], temperature=0.4) == '{}'
