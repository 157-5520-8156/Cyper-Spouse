from __future__ import annotations

import hashlib

import pytest

from companion_daemon.world_v2.activity_lifecycle_draft import (
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
