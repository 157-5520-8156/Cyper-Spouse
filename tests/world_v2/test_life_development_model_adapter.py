from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.life_development_model_adapter import (
    RoleBoundLifeDevelopmentModelAdapter,
    life_development_reviewer_is_independent,
)


class _JsonCapableModel:
    model = "provider-model"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]], float]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        self.calls.append(("complete", messages, temperature))
        return "plain"

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        self.calls.append(("complete_json", messages, temperature))
        return '{"decision":"no_op"}'


class _RoutedModel(_JsonCapableModel):
    def __init__(self) -> None:
        super().__init__()
        self.routed = _JsonCapableModel()
        self.route_calls = 0

    def wire_reselection_route(self) -> _JsonCapableModel:
        self.route_calls += 1
        return self.routed


@pytest.mark.asyncio
async def test_role_bound_life_adapter_prefers_json_completion_without_rewriting_messages() -> None:
    provider = _JsonCapableModel()
    adapter = RoleBoundLifeDevelopmentModelAdapter(
        model=provider,
        role="world_author",
    )
    messages = [
        {"role": "system", "content": "world-author role"},
        {"role": "user", "content": '{"output_contract":{"contract":"test.1"}}'},
    ]

    result = await adapter.complete(messages, temperature=0.6)

    assert result == '{"decision":"no_op"}'
    assert provider.calls == [("complete_json", messages, 0.6)]
    assert provider.calls[0][1] is messages


@pytest.mark.asyncio
async def test_role_bound_life_adapter_preserves_role_on_wire_reselection_route() -> None:
    provider = _RoutedModel()
    adapter = RoleBoundLifeDevelopmentModelAdapter(
        model=provider,
        role="world_author_source_reviewer",
    )
    messages = [{"role": "user", "content": "{}"}]

    routed = adapter.wire_reselection_route()
    result = await routed.complete(messages, temperature=0.0)

    assert provider.route_calls == 1
    assert result == '{"decision":"no_op"}'
    assert provider.routed.calls == [("complete_json", messages, 0.0)]
    assert routed.model.endswith("/life-development/world_author_source_reviewer")


def test_life_development_independence_uses_checkpoint_authority_not_route_alias() -> None:
    author = SimpleNamespace(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
    )
    aliased_self_reviewer = SimpleNamespace(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="deepseek/deepseek-v4-flash",
    )
    independent_reviewer = SimpleNamespace(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="qwen/qwen-plus",
    )
    undeclared_reviewer = SimpleNamespace(model="private-route-alias")

    assert not life_development_reviewer_is_independent(
        author=author,
        reviewer=aliased_self_reviewer,
    )
    assert life_development_reviewer_is_independent(
        author=author,
        reviewer=independent_reviewer,
    )
    assert not life_development_reviewer_is_independent(
        author=author,
        reviewer=undeclared_reviewer,
    )
