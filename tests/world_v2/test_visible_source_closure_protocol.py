from __future__ import annotations

import json

import httpx
import pytest

from companion_daemon.llm import DeepSeekChatModel
from companion_daemon.world_v2.character_interior.inbound_wire import (
    _metered_review_call,
    _ProviderSubcallAuditCapture,
)
from companion_daemon.world_v2.structured_source_review_model import (
    StrictOutputCapabilityEvidence,
)
from companion_daemon.world_v2.visible_source_closure_protocol import (
    VISIBLE_SOURCE_CLOSURE_CONTRACT,
    parse_visible_source_closure,
    visible_source_closure_messages,
    visible_source_closure_schema,
)
from companion_daemon.world_v2.visible_source_review_model import (
    VisibleSourceReviewModel,
    audited_visible_source_verdict_capability_evidence,
    visible_source_verdict_schema_digest,
)


def _wire(*decisions: dict[str, object]) -> str:
    return json.dumps(
        {
            "contract": "visible-beat-source-verdict.1",
            "decisions": list(decisions),
        },
        ensure_ascii=False,
    )


def _decision(
    *,
    beat_index: int = 0,
    verdict: str,
    role: str,
    subject_role: str = "companion",
    refs: list[int] | None = None,
) -> dict[str, object]:
    return {
        "beat_index": beat_index,
        "verdict": verdict,
        "semantic_role": role,
        "subject_role": subject_role,
        "source_ref_indexes": refs or [],
    }


def test_visible_source_verdict_schema_is_deepseek_strict_subset() -> None:
    schema = visible_source_closure_schema()
    encoded = json.dumps(schema, sort_keys=True)

    assert schema["additionalProperties"] is False
    assert "$defs" not in encoded
    assert "minLength" not in encoded
    assert "maxLength" not in encoded
    assert "minItems" not in encoded
    assert "maxItems" not in encoded
    assert schema["properties"]["contract"]["enum"] == [
        "visible-beat-source-verdict.1"
    ]


def test_visible_source_verdict_request_carries_one_compact_evidence_table() -> None:
    messages = visible_source_closure_messages(
        visible_beats=("我今天去了公园。",),
        world_claims=(),
        source_references=(
            {
                "source_ref_index": 0,
                "source_ref": "world-event:1",
                "kind": "settled_world_event",
                "subject_role": "companion",
                "evidence_text": "她今天去了公园。",
            },
        ),
    )

    packet = json.loads(messages[1]["content"])
    assert "source_evidence" not in packet
    assert packet["source_references"][0]["source_ref"] == "world-event:1"
    assert packet["output_contract"] == {
        "contract": "visible-beat-source-verdict.1",
        "authority": "correlated_source_guard_not_character_author",
    }
    assert "我今天淋雨了" in messages[0]["content"]
    assert "我有点担心你，你现在发烧了" in messages[0]["content"]


def test_visible_source_verdict_requires_one_ordered_decision_per_beat() -> None:
    raw = _wire(
        _decision(
            beat_index=1,
            verdict="source_free",
            role="private_state",
        )
    )

    with pytest.raises(ValueError, match="each visible Beat exactly once"):
        parse_visible_source_closure(
            raw,
            visible_beats=("第一条", "第二条"),
            source_ref_kinds=(),
        )


def test_visible_source_verdict_derives_whole_beat_locator_host_side() -> None:
    text = "我有点担心你。"
    proof = parse_visible_source_closure(
        _wire(
            _decision(
                verdict="source_free",
                role="private_state",
            )
        ),
        visible_beats=(text,),
        source_ref_kinds=(),
    )

    segment = proof.segments[0]
    assert segment.locator.model_dump() == {
        "beat_index": 0,
        "char_start": 0,
        "char_end": len(text),
        "text": text,
    }
    assert segment.decision == "source_free"


def test_visible_source_verdict_accepts_character_commitment_as_source_free() -> None:
    proof = parse_visible_source_closure(
        _wire(
            _decision(
                verdict="source_free",
                role="commitment",
            )
        ),
        visible_beats=("这件事我不会跟别人说。",),
        source_ref_kinds=(),
    )

    assert proof.segments[0].semantic_role == "nonassertive_content"
    assert proof.segments[0].source_relation == "not_external_proposition"


@pytest.mark.parametrize("role", ["private_state", "commitment"])
def test_visible_source_verdict_rejects_source_free_actor_swap(role: str) -> None:
    with pytest.raises(ValueError, match="cannot change actor"):
        parse_visible_source_closure(
            _wire(
                _decision(
                    verdict="source_free",
                    role=role,
                    subject_role="counterpart",
                )
            ),
            visible_beats=("我现在有点担心。",),
            source_ref_kinds=(),
        )


def test_visible_source_verdict_rejects_external_beat_marked_source_free() -> None:
    raw = _wire(
        _decision(
            verdict="source_free",
            role="external_proposition",
        )
    )

    with pytest.raises(ValueError, match="cannot be source-free"):
        parse_visible_source_closure(
            raw,
            visible_beats=("我去了公园。",),
            source_ref_kinds=(),
        )


def test_visible_source_verdict_rejects_closed_beat_without_source() -> None:
    raw = _wire(
        _decision(
            verdict="closed",
            role="external_proposition",
        )
    )

    with pytest.raises(ValueError, match="requires at least one"):
        parse_visible_source_closure(
            raw,
            visible_beats=("我去了公园。",),
            source_ref_kinds=(),
        )


def test_visible_source_verdict_accepts_exact_current_report() -> None:
    proof = parse_visible_source_closure(
        _wire(
            _decision(
                verdict="closed",
                role="external_proposition",
                subject_role="counterpart",
                refs=[0],
            )
        ),
        visible_beats=("你刚才淋雨了。",),
        source_ref_kinds=("current_counterpart_report",),
        source_ref_subject_roles=("counterpart",),
    )

    assert proof.segments[0].decision == "closed"
    assert (
        proof.segments[0].source_relation
        == "exact_current_report_discourse_coverage"
    )


def test_visible_source_verdict_rejects_cross_actor_source_binding() -> None:
    raw = _wire(
        _decision(
            verdict="closed",
            role="external_proposition",
            subject_role="companion",
            refs=[0],
        )
    )

    with pytest.raises(ValueError, match="source actor does not match"):
        parse_visible_source_closure(
            raw,
            visible_beats=("我今天淋雨了。",),
            source_ref_kinds=("current_counterpart_report",),
            source_ref_subject_roles=("counterpart",),
        )


@pytest.mark.parametrize("subject_role", ["general", "none"])
def test_visible_source_verdict_rejects_closed_actor_evasion(subject_role: str) -> None:
    with pytest.raises(ValueError, match="source actor|identify its source actor"):
        parse_visible_source_closure(
            _wire(
                _decision(
                    verdict="closed",
                    role="external_proposition",
                    subject_role=subject_role,
                    refs=[0],
                )
            ),
            visible_beats=("她今天淋雨了。",),
            source_ref_kinds=("current_counterpart_report",),
            source_ref_subject_roles=("counterpart",),
        )


def test_visible_source_verdict_rejects_partial_refs_on_unclosed_mixed_beat() -> None:
    raw = _wire(
        _decision(
            verdict="unclosed",
            role="mixed",
            subject_role="mixed",
            refs=[0],
        )
    )

    with pytest.raises(ValueError, match="partial source authority"):
        parse_visible_source_closure(
            raw,
            visible_beats=("我有点担心你，你现在发烧了。",),
            source_ref_kinds=("current_counterpart_report",),
        )


def test_visible_source_verdict_closes_mixed_private_and_sourced_counterpart_fact() -> None:
    proof = parse_visible_source_closure(
        _wire(
            _decision(
                verdict="closed",
                role="mixed",
                subject_role="counterpart",
                refs=[0],
            )
        ),
        visible_beats=("我有点担心你，你现在发烧了。",),
        source_ref_kinds=("current_counterpart_report",),
        source_ref_subject_roles=("counterpart",),
    )

    assert proof.segments[0].decision == "closed"
    assert proof.segments[0].semantic_role == "embedded_external_proposition"


def test_visible_source_verdict_evidence_is_exact_route_and_schema_bound() -> None:
    evidence = audited_visible_source_verdict_capability_evidence(
        enabled=True,
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
    )

    assert evidence.status == "verified"
    assert evidence.audit_sample_count == 100
    assert evidence.contract_schema_digests == (
        (VISIBLE_SOURCE_CLOSURE_CONTRACT, visible_source_verdict_schema_digest()),
    )
    assert (
        audited_visible_source_verdict_capability_evidence(
            enabled=True,
            base_url="https://example.invalid/v1",
            model="deepseek-v4-flash",
        ).status
        == "unverified"
    )


@pytest.mark.asyncio
async def test_visible_source_review_model_forces_one_tool_without_response_format() -> None:
    captured: dict[str, object] = {}
    result = _wire(
        _decision(
            verdict="source_free",
            role="private_state",
        )
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call.1",
                                    "type": "function",
                                    "function": {
                                        "name": "visible_beat_source_verdict_v1",
                                        "arguments": result,
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 15},
            },
        )

    leaf = DeepSeekChatModel(
        "key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    evidence = StrictOutputCapabilityEvidence.verified(
        evidence_source="test",
        provider="deepseek",
        model="deepseek-v4-flash",
        contracts=(VISIBLE_SOURCE_CLOSURE_CONTRACT,),
        observed_at="2026-08-10",
        contract_schema_digests=(
            (VISIBLE_SOURCE_CLOSURE_CONTRACT, visible_source_verdict_schema_digest()),
        ),
    )
    model = VisibleSourceReviewModel(
        transport_model=leaf,
        strict_output_capability_evidence=evidence,
    )
    try:
        raw, usage = await model.complete_json_with_usage(
            visible_source_closure_messages(
                visible_beats=("我有点担心你。",),
                world_claims=(),
                source_references=(),
            ),
            temperature=0.0,
        )
    finally:
        await model.aclose()

    assert raw == result
    assert usage["input_tokens"] == 40
    assert "response_format" not in captured
    assert captured["thinking"] == {"type": "disabled"}
    assert captured["tool_choice"] == {
        "type": "function",
        "function": {"name": "visible_beat_source_verdict_v1"},
    }
    functions = [tool["function"] for tool in captured["tools"]]
    assert len(functions) == 1
    assert functions[0]["strict"] is True
    assert functions[0]["parameters"] == visible_source_closure_schema()


@pytest.mark.asyncio
async def test_compact_guard_emits_the_exact_durable_request_identity() -> None:
    captured_header: list[str | None] = []
    result = _wire(_decision(verdict="source_free", role="private_state"))

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_header.append(
            request.headers.get("X-Girl-Agent-Request-Identity")
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call.identity",
                                    "type": "function",
                                    "function": {
                                        "name": "visible_beat_source_verdict_v1",
                                        "arguments": result,
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    leaf = DeepSeekChatModel(
        "key",
        "http://127.0.0.1:19876",
        "deepseek-v4-flash",
        thinking_enabled=False,
        transport=httpx.MockTransport(handler),
    )
    leaf._test_only_capture_exact_request_identity = True
    evidence = StrictOutputCapabilityEvidence.verified(
        evidence_source="test",
        provider="deepseek",
        model="deepseek-v4-flash",
        contracts=(VISIBLE_SOURCE_CLOSURE_CONTRACT,),
        observed_at="2026-08-10",
        contract_schema_digests=(
            (VISIBLE_SOURCE_CLOSURE_CONTRACT, visible_source_verdict_schema_digest()),
        ),
    )
    model = VisibleSourceReviewModel(
        transport_model=leaf,
        strict_output_capability_evidence=evidence,
    )
    messages = visible_source_closure_messages(
        visible_beats=("我有点担心你。",),
        world_claims=(),
        source_references=(),
    )
    try:
        with _ProviderSubcallAuditCapture(
            owner_model_call_id="model-call:author",
            owner_request_hash="a" * 64,
            owner_raw="{}",
            owner_model_id="deepseek-v4-flash",
            owner_model_version="character-interior.1",
            purpose="character_author",
        ) as audit_capture:
            await _metered_review_call(
                model,
                messages,
                temperature=0.0,
                audit_purpose="visible_source_closure_proof_v1",
            )
            attempts = audit_capture.finalize()
    finally:
        await model.aclose()

    assert len(attempts) == 1
    assert captured_header == [attempts[0].request_hash]
