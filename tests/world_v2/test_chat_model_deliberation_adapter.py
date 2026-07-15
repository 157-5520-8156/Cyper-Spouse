from __future__ import annotations

import json
from hashlib import sha256

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
    RoutedChatModelDeliberationAdapter,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    ModelUsageProvenance,
    TriggerMessage,
)


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


class _MeteredModel(_Model):
    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        self.calls.append((messages, temperature))
        material = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 12,
            "output_tokens": 3,
            "thinking_tokens": 0,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": "fake-provider",
            "provider_usage_ref": "usage:fake:1",
        }
        digest = sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self._reply, ModelUsageProvenance(**material, provider_usage_hash=digest)


@pytest.mark.asyncio
async def test_adapter_keeps_chat_model_output_inert_and_binds_request_to_prompt() -> None:
    model = _Model('{"proposal_id":"proposal:1"}')
    adapter = ChatModelDeliberationAdapter(model=model)

    output = await adapter.propose(_request())

    assert output.model_id == "deepseek-v4-flash"
    assert output.raw_proposal == {"proposal_id": "proposal:1"}
    messages, temperature = model.calls[0]
    assert temperature == 0.7
    assert "ReplyDraft" in messages[0]["content"]
    supplied = json.loads(messages[1]["content"])
    assert supplied["request"]["trigger_ref"] == "trigger:1"
    assert supplied["request"]["evaluated_world_revision"] == 3


@pytest.mark.asyncio
async def test_adapter_composes_provider_usage_with_the_same_completion() -> None:
    adapter = ChatModelDeliberationAdapter(model=_MeteredModel('{"proposal_id":"proposal:metered"}'))

    output = await adapter.propose(_request())

    assert output.input_tokens == 12
    assert output.output_tokens == 3
    assert output.usage is not None
    assert output.usage.route_class == "chat"
    assert output.usage.token_provenance == "provider_reported"


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


@pytest.mark.asyncio
async def test_adapter_materializes_a_verified_reply_draft_into_a_hash_bound_minimal_proposal() -> None:
    text = "我刚刚确实有点飘走了。"
    model = _Model(
        json.dumps(
            {
                "response_text": text,
                "stance": "acknowledge_briefly",
                "brief_rationale": "Acknowledge the missed connection without inventing facts.",
                "confidence": 7300,
            },
            ensure_ascii=False,
        )
    )
    request = _request().model_copy(
        update={
            "trigger_message": TriggerMessage(
                event_ref="event:observation:1",
                event_payload_hash="sha256:" + "a" * 64,
                observation_ref="observation:1",
                source_world_revision=3,
                actor="user:primary",
                channel="test",
                reply_target="user:primary",
                text="你刚刚没接住我。",
            )
        }
    )
    adapter = ChatModelDeliberationAdapter(model=model)

    output = await adapter.propose(request)

    assert output.raw_proposal["trigger_ref"] == "trigger:1"
    assert output.raw_proposal["response_text"] == text
    assert output.raw_proposal["action_intents"][0]["target"] == "user:primary"
    assert output.raw_proposal["action_intents"][0]["payload_hash"] == "sha256:" + sha256(
        text.encode("utf-8")
    ).hexdigest()
    assert output.raw_proposal["evidence_refs"][0]["ref_id"] == "observation:1"


@pytest.mark.asyncio
async def test_adapter_rejects_a_reply_draft_without_a_verified_current_message() -> None:
    adapter = ChatModelDeliberationAdapter(
        model=_Model(
            '{"response_text":"hi","stance":"plain","brief_rationale":"ordinary response"}'
        )
    )

    with pytest.raises(ValueError, match="verified current message"):
        await adapter.propose(_request())
