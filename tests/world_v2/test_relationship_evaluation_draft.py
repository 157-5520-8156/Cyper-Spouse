from __future__ import annotations

import hashlib
import json

import pytest

from companion_daemon.world_v2.relationship_evaluation_draft import (
    RelationshipEvaluationDraftAdapter,
    RelationshipEvaluationDraftCapsule,
    materialize_relationship_evaluation_draft,
)


def _capsule() -> RelationshipEvaluationDraftCapsule:
    return RelationshipEvaluationDraftCapsule(
        accepted_appraisal_summary="对方的说法可能表达了失望，但解释仍是不确定的。",
        relationship_summary="最近的互动有来有往，仍需避免由单次对话过度推断。",
        active_boundary_summaries=("不接受羞辱性称呼。",),
        unconsumed_signal_summaries=("之前一次可靠回应仍未消费。",),
    )


def _signal() -> str:
    return json.dumps(
        {
            "decision": "signal",
            "signal_code": "missed_connection",
            "confidence_bp": 7200,
            "persistence": "session",
            "rationale_code": "accepted_appraisal_residue",
            "suggested_deltas": {
                "trust_bp": -400,
                "closeness_bp": -250,
                "respect_bp": 0,
                "reliability_bp": -350,
                "mutuality_bp": -100,
                "repair_confidence_bp": -200,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_signal_materializes_a_complete_bounded_suggestion_with_audit_bytes() -> None:
    raw = _signal()

    draft = materialize_relationship_evaluation_draft(raw=raw, capsule=_capsule(), model="fake-flash")

    assert draft.decision == "signal"
    assert draft.signal_code == "missed_connection"
    assert draft.suggested_deltas is not None
    assert draft.suggested_deltas.reliability_bp == -350
    assert draft.raw_output_hash == "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
    assert draft.normalized_json == json.dumps(
        json.loads(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert draft.normalized_output_hash == "sha256:" + hashlib.sha256(
        draft.normalized_json.encode()
    ).hexdigest()


def test_no_change_has_no_signal_payload() -> None:
    draft = materialize_relationship_evaluation_draft(
        raw='{"decision":"no_change"}', capsule=_capsule(), model="fake-thinking"
    )

    assert draft.decision == "no_change"
    assert draft.signal_code is None
    assert draft.suggested_deltas is None


@pytest.mark.parametrize(
    "raw",
    [
        '{"decision":"signal"}',
        '{"decision":"no_change","confidence_bp":1}',
        '{"decision":"signal","signal_code":"bad-code","confidence_bp":1,"persistence":"session",'
        '"rationale_code":"reason","suggested_deltas":{"trust_bp":0,"closeness_bp":0,"respect_bp":0,'
        '"reliability_bp":0,"mutuality_bp":0,"repair_confidence_bp":0}}',
        '{"decision":"signal","signal_code":"noticed","confidence_bp":0,"persistence":"session",'
        '"rationale_code":"reason","suggested_deltas":{"trust_bp":0,"closeness_bp":0,"respect_bp":0,'
        '"reliability_bp":0,"mutuality_bp":0,"repair_confidence_bp":0}}',
        '{"decision":"signal","signal_code":"noticed","confidence_bp":1,"persistence":"session",'
        '"rationale_code":"reason","suggested_deltas":{"trust_bp":10001,"closeness_bp":0,"respect_bp":0,'
        '"reliability_bp":0,"mutuality_bp":0,"repair_confidence_bp":0}}',
        '{"decision":"signal","signal_code":"noticed","confidence_bp":1,"persistence":"session",'
        '"rationale_code":"reason","suggested_deltas":{"trust_bp":0,"closeness_bp":0,"respect_bp":0,'
        '"reliability_bp":0,"mutuality_bp":0},"stage":"lover"}',
        '{"decision":"signal","signal_code":"noticed","confidence_bp":1,"persistence":"session",'
        '"rationale_code":"reason","suggested_deltas":{"trust_bp":0,"closeness_bp":0,"respect_bp":0,'
        '"reliability_bp":0,"mutuality_bp":0,"repair_confidence_bp":0},"relationship_id":"relationship:1"}',
        '{"decision":"signal","signal_code":"noticed","confidence_bp":1,"persistence":"session",'
        '"rationale_code":"reason","suggested_deltas":{"trust_bp":0,"closeness_bp":0,"respect_bp":0,'
        '"reliability_bp":0,"mutuality_bp":0,"repair_confidence_bp":0},"evidence_refs":[]}',
    ],
)
def test_rejects_incomplete_out_of_bound_or_authority_bearing_output(raw: str) -> None:
    with pytest.raises(ValueError):
        materialize_relationship_evaluation_draft(raw=raw, capsule=_capsule(), model="fake")


class _Model:
    model = "fake-flash"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.calls.append((messages, temperature))
        return self.response


@pytest.mark.asyncio
async def test_adapter_uses_chat_model_protocol_and_only_exposes_safe_summaries() -> None:
    model = _Model('{"decision":"no_change"}')
    adapter = RelationshipEvaluationDraftAdapter(model=model, temperature=0.15)

    draft = await adapter.deliberate(capsule=_capsule())

    assert draft.decision == "no_change"
    assert len(model.calls) == 1
    messages, temperature = model.calls[0]
    assert temperature == 0.15
    assert "stage" in messages[0]["content"]
    model_input = json.loads(messages[1]["content"])
    assert model_input == _capsule().model_dump(mode="json")
    rendered = messages[1]["content"]
    assert "revision" not in rendered
    assert "evidence" not in rendered
    assert "accepted_event" not in rendered
