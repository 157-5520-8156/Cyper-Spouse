from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
    RoutedChatModelDeliberationAdapter,
)
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute


def _request() -> ModelInput:
    return ModelInput(
        call_id="call:1",
        attempt_id="attempt:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:1",
        evaluated_world_revision=3,
        model_content_json='{"capsule":"authoritative"}',
    )


class _Model:
    model = "deepseek-v4-flash"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.calls.append((messages, temperature))
        return self._reply


@pytest.mark.asyncio
async def test_adapter_keeps_chat_model_output_inert_and_binds_request_to_prompt() -> None:
    model = _Model('{"proposal_id":"proposal:1"}')
    adapter = ChatModelDeliberationAdapter(model=model)

    output = await adapter.propose(_request())

    assert output.model_id == "deepseek-v4-flash"
    assert output.raw_proposal == {"proposal_id": "proposal:1"}
    messages, temperature = model.calls[0]
    assert temperature == 0.7
    assert "MinimalProposal" in messages[0]["content"]
    supplied = json.loads(messages[1]["content"])
    assert supplied["request"]["trigger_ref"] == "trigger:1"
    assert supplied["request"]["evaluated_world_revision"] == 3


@pytest.mark.asyncio
async def test_quick_recovery_uses_lower_temperature_and_accepts_fenced_json() -> None:
    model = _Model("```json\n{\"proposal_id\":\"proposal:quick\"}\n```")
    adapter = ChatModelDeliberationAdapter(model=model, temperature=1.1)

    output = await adapter.recover(_request(), "main_timeout")

    assert output.raw_proposal == {"proposal_id": "proposal:quick"}
    messages, temperature = model.calls[0]
    assert temperature == 0.25
    assert "main attempt failed" in messages[0]["content"].lower()
    assert json.loads(messages[1]["content"])["quick_recovery_failure"] == "main_timeout"


@pytest.mark.asyncio
async def test_adapter_rejects_non_object_or_malformed_model_output() -> None:
    for reply in ("not json", "[]", "```json\n{}"):
        adapter = ChatModelDeliberationAdapter(model=_Model(reply))
        with pytest.raises(ValueError, match="JSON"):
            await adapter.propose(_request())


@pytest.mark.asyncio
async def test_routed_adapter_uses_thinking_only_for_the_explicit_thinking_route() -> None:
    flash = _Model('{"proposal_id":"proposal:flash"}')
    thinking = _Model('{"proposal_id":"proposal:thinking"}')
    adapter = RoutedChatModelDeliberationAdapter(
        flash_model=flash, thinking_model=thinking, temperature=0.8
    )

    flash_output = await adapter.propose(_request())
    thinking_output = await adapter.propose(
        _request().model_copy(
            update={"route": ModelRoute(tier="thinking", reason_code="ambiguity", router_version="test.1")}
        )
    )
    quick_output = await adapter.recover(_request(), "main_timeout")

    assert flash_output.raw_proposal == {"proposal_id": "proposal:flash"}
    assert thinking_output.raw_proposal == {"proposal_id": "proposal:thinking"}
    assert quick_output.raw_proposal == {"proposal_id": "proposal:flash"}
    assert len(flash.calls) == 2
    assert len(thinking.calls) == 1


@pytest.mark.asyncio
async def test_routed_adapter_fails_closed_when_thinking_was_selected_without_a_thinking_model() -> None:
    adapter = RoutedChatModelDeliberationAdapter(flash_model=_Model("{}"))
    thinking_request = _request().model_copy(
        update={"route": ModelRoute(tier="thinking", reason_code="ambiguity", router_version="test.1")}
    )

    with pytest.raises(RuntimeError, match="not configured"):
        await adapter.propose(thinking_request)
