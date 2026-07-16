from __future__ import annotations

import hashlib
import json

import pytest

from companion_daemon.world_v2.activity_lifecycle_draft import (
    ActivityLifecycleDraftAdapter,
    ActivityLifecycleDraftCapsule,
    ActivityLifecycleOpening,
    materialize_activity_lifecycle_draft,
)


def _capsule() -> ActivityLifecycleDraftCapsule:
    return ActivityLifecycleDraftCapsule(
        situation_summary="午后，角色有一项尚未开始的日常安排。",
        openings=(
            ActivityLifecycleOpening(
                opening_token="opening:7bf5b65ca5d51fab690613ebc0ea5b1c",
                safe_summary="可以开始一项已安排的日常活动。",
            ),
            ActivityLifecycleOpening(
                opening_token="opening:23df02c833eaf0b96fafd2c55bd848bd",
                safe_summary="也可以暂时放弃一项已安排的日常活动。",
            ),
        ),
    )


def test_materializes_only_a_preoffered_opaque_opening_token_with_auditable_bytes() -> None:
    raw = '{"decision":"select","opening_token":"opening:7bf5b65ca5d51fab690613ebc0ea5b1c"}'

    draft = materialize_activity_lifecycle_draft(raw=raw, capsule=_capsule(), model="fake-flash")

    assert draft.decision == "opening_token"
    assert draft.opening_token == "opening:7bf5b65ca5d51fab690613ebc0ea5b1c"
    assert draft.model == "fake-flash"
    assert draft.raw_output == raw
    assert draft.raw_output_hash == "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    assert draft.normalized_json == raw
    assert draft.normalized_output_hash == "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def test_materializes_the_exact_no_op_shape_without_an_opening_token() -> None:
    draft = materialize_activity_lifecycle_draft(
        raw='{"decision":"no_op"}', capsule=_capsule(), model="fake-thinking"
    )

    assert draft.decision == "no_op"
    assert draft.opening_token is None
    assert draft.normalized_json == '{"decision":"no_op"}'


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "null",
        '{"decision":"select"}',
        '{"decision":"no_op","opening_token":"opening:7bf5b65ca5d51fab690613ebc0ea5b1c"}',
        '{"decision":"select","opening_token":"opening:unknown"}',
        '{"decision":"select","opening_token":"opening:7bf5b65ca5d51fab690613ebc0ea5b1c","operation":"complete"}',
        '{"decision":"select","opening_token":"opening:7bf5b65ca5d51fab690613ebc0ea5b1c","plan_id":"plan:leaked"}',
        '{"decision":"start","opening_token":"opening:7bf5b65ca5d51fab690613ebc0ea5b1c"}',
    ],
)
def test_rejects_malformed_unknown_or_authority_bearing_model_output(raw: str) -> None:
    with pytest.raises(ValueError):
        materialize_activity_lifecycle_draft(raw=raw, capsule=_capsule(), model="fake")


class _FakeModel:
    model = "fake-flash"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str:
        self.calls.append((messages, temperature))
        return self.response


@pytest.mark.asyncio
async def test_adapter_exposes_only_safe_summaries_and_the_preoffered_token_set_to_model() -> None:
    model = _FakeModel('{"decision":"no_op"}')
    adapter = ActivityLifecycleDraftAdapter(model=model, temperature=0.15)

    draft = await adapter.deliberate(capsule=_capsule())

    assert draft.decision == "no_op"
    assert len(model.calls) == 1
    messages, temperature = model.calls[0]
    assert temperature == 0.15
    model_input = json.loads(messages[1]["content"])
    assert model_input == {
        "situation_summary": "午后，角色有一项尚未开始的日常安排。",
        "openings": [
            {
                "opening_token": "opening:7bf5b65ca5d51fab690613ebc0ea5b1c",
                "safe_summary": "可以开始一项已安排的日常活动。",
            },
            {
                "opening_token": "opening:23df02c833eaf0b96fafd2c55bd848bd",
                "safe_summary": "也可以暂时放弃一项已安排的日常活动。",
            },
        ],
    }
    assert "plan_id" not in model_input
    assert "revision" not in model_input
    assert "evidence" not in model_input


@pytest.mark.asyncio
async def test_adapter_does_not_call_a_model_or_offer_a_fallback_when_catalog_is_empty() -> None:
    model = _FakeModel('{"decision":"select","opening_token":"opening:invented"}')
    empty = ActivityLifecycleDraftCapsule(situation_summary="没有可推进的安排。", openings=())

    draft = await ActivityLifecycleDraftAdapter(model=model).deliberate(capsule=empty)

    assert draft.decision == "no_op"
    assert draft.opening_token is None
    assert model.calls == []
