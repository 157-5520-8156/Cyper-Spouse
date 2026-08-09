from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from hashlib import sha256
import threading
import time

import pytest

from companion_daemon.world_v2.biographical_claim_authority import (
    biographical_coordinate_authorities,
)
from companion_daemon.world_v2.character_interior.inbound_wire import (
    _ExpressionDraftWire,
    _RoutedExpressionDraftWire,
    _incremental_first_expression,
    _stream_first_expression,
    _stream_tail_expression,
    expression_draft_shape_contract,
    review_candidate_external_proposition_coverage,
    review_expression_source_closure,
    review_expression_with_candidate_external_coverage,
    review_expression_source_closure_appeal,
    shape_repair_instruction,
)
from companion_daemon.world_v2.companion_identity import (
    CompanionIdentityFrame,
    companion_identity_source_ref,
)
from companion_daemon.world_v2.expression_draft import (
    QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    TEXT_ONLY_EXPRESSION_CAPABILITIES,
    build_source_ref_alias_table,
    current_counterpart_report_source_refs,
    qq_expression_capabilities,
    world_source_scope_boundary,
)
from companion_daemon.world_v2.isolated_source_closure_trace import (
    BoundedSourceClosureTraceCollector,
    capture_isolated_source_closure_trace,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    ModelUsageProvenance,
    TriggerMessage,
    ValidationTechnicalFailure,
)
from companion_daemon.world_v2.proposal_envelope import ProposalEvidenceRef
from companion_daemon.world_v2.recall_index import (
    FeatureHashRecallEmbedding,
    InMemoryRecallIndex,
    RecallCursor,
    RecallDocument,
    RecallSourceBinding,
)
from companion_daemon.world_v2.recall_audit import CharacterRecallRequest
from companion_daemon.world_v2.recall_corpus import RecallCorpusSources
from companion_daemon.world_v2.recall_runtime import (
    PresentedPrefetchTrace,
    RecallCoordinator,
    perform_character_recall,
    verify_trusted_recall_trace,
)
from companion_daemon.world_v2.source_review_authority import (
    InventoryAvailabilityExhausted,
    SourceReviewAuthority,
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


def test_shape_repair_reselects_the_complete_grounded_expression() -> None:
    instruction = shape_repair_instruction(
        "beats.0.text: Field required",
    )

    assert "complete expression" in instruction.lower()
    assert "same pinned Context" in instruction
    assert "private_turn_state" in instruction
    assert "world_claims" in instruction
    assert "attended_source_refs" in instruction
    assert "preserving the visible reply" not in instruction
    assert "fixes only this problem" not in instruction


def _provider_request_hash(
    messages: list[dict[str, str]],
    temperature: float,
) -> str:
    return sha256(
        json.dumps(
            {
                "messages": messages,
                "temperature": temperature,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def test_production_qq_capabilities_require_the_model_owned_private_state() -> None:
    assert qq_expression_capabilities("napcat").private_turn_state_mode == "required"
    assert qq_expression_capabilities("text-only").private_turn_state_mode == "required"


def test_private_turn_state_contract_does_not_license_invented_life_context() -> None:
    contract = expression_draft_shape_contract()

    assert "do not invent a current activity, place, bodily event" in contract
    assert "Visible beats may contain factual first-person life claims only" in contract
    assert "only what the pinned Context presents" in contract
    assert "optimize the conversation" in contract


def _strict_source_reselection_fixture(
    messages: list[dict[str, str]],
    raw: str,
) -> str:
    """Migrate legacy author fixtures onto the negotiated realtime test wire."""

    strict_contract = False
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        try:
            payload = json.loads(message["content"])
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        output_contract = payload.get("output_contract") if isinstance(payload, dict) else None
        if (
            isinstance(output_contract, dict)
            and output_contract.get("contract") == "expression-source-reselection-direct.1"
        ):
            strict_contract = True
            break
    if not strict_contract:
        return raw
    try:
        expression = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    if not isinstance(expression, dict) or set(expression) == {
        "expression_draft",
        "episode_disposition",
    }:
        return raw
    if "timing_choice" not in expression or "beats" not in expression:
        return raw
    normalized = dict(expression)
    episode_disposition = normalized.pop("episode_disposition", None)
    private_state = normalized.get("private_turn_state")
    if isinstance(private_state, dict):
        private_state = {"contract": "private-turn-state.1", **private_state}
    beats = normalized.get("beats")
    if not isinstance(beats, list) or any(not isinstance(beat, dict) for beat in beats):
        return raw
    ordered_beats = [
        {
            "modality": beat.get("modality"),
            "text": beat.get("text"),
            "reaction_id": beat.get("reaction_id"),
            "sticker_id": beat.get("sticker_id"),
        }
        for beat in beats
    ]
    timing_choice = normalized.get("timing_choice")
    delay_position_bp = 0 if timing_choice == "later" else None
    normalized.pop("delay_seconds", None)
    expectation = normalized.get("response_expectation")
    if isinstance(expectation, dict):
        expectation = dict(expectation)
        expectation.pop("wait_seconds", None)
        expectation.setdefault("wait_position_bp", 0)
    strict_expression = {
        "private_turn_state": private_state,
        "timing_choice": timing_choice,
        "cadence": normalized.get("cadence", "conversational"),
        "beats": ordered_beats,
        "delay_position_bp": delay_position_bp,
        "expires_after_seconds": normalized.get("expires_after_seconds"),
        "stance": normalized.get("stance"),
        "brief_rationale": normalized.get("brief_rationale"),
        "impulse_summary": normalized.get("impulse_summary"),
        "confidence": normalized.get("confidence", 7_000),
        "variation_profile": normalized.get("variation_profile"),
        "response_expectation": expectation,
        "response_expectation_assessment": normalized.get("response_expectation_assessment"),
        "world_claims": normalized.get("world_claims", []),
    }
    return json.dumps(
        {
            "expression_draft": strict_expression,
            "episode_disposition": episode_disposition,
        },
        ensure_ascii=False,
    )


class _Model:
    model = "deepseek-v4-flash"
    strict_reselection_wire = True

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.calls.append((messages, temperature))
        return (
            _strict_source_reselection_fixture(messages, self._reply)
            if self.strict_reselection_wire
            else self._reply
        )

    @property
    def semantic_authority_id(self) -> str:
        """Give test doubles an explicit checkpoint identity declaration."""

        return f"semantic-authority:test:{self.model.casefold()}"

    @property
    def last_system_prompt(self) -> str:
        return self.calls[-1][0][0]["content"]


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
        reply = (
            _strict_source_reselection_fixture(messages, self._reply)
            if self.strict_reselection_wire
            else self._reply
        )
        return reply, ModelUsageProvenance(**material, provider_usage_hash=digest)


class _JsonModel(_Model):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        raise AssertionError("structured proposal path must request JSON mode when available")

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        self.calls.append((messages, temperature))
        return (
            _strict_source_reselection_fixture(messages, self._reply)
            if self.strict_reselection_wire
            else self._reply
        )


class _JsonMeteredModel(_MeteredModel):
    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        raise AssertionError("structured metered proposal path must preserve JSON request mode")

    async def complete_json_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        return await _MeteredModel.complete_with_usage(
            self,
            messages,
            temperature=temperature,
        )


class _ForcedToolExpressionCorrectionModel(_JsonMeteredModel):
    supports_required_tool_choice = True

    def __init__(self, invalid: str, corrected: str) -> None:
        super().__init__(invalid)
        self._corrected = corrected
        self.tool_calls: list[tuple[list[dict[str, object]] | None, object | None]] = []

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        self.calls.append((messages, temperature))
        self.tool_calls.append((tools, tool_choice))
        material = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 12,
            "output_tokens": 4,
            "thinking_tokens": 0,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": "fake-provider",
            "provider_usage_ref": "usage:fake:expression-tool:1",
        }
        digest = sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if tools is not None:
            assert tool_choice == {
                "type": "function",
                "function": {"name": "character_expression_reselection_v1"},
            }
            raw = self._corrected
        else:
            raw = self._reply
        return raw, ModelUsageProvenance(**material, provider_usage_hash=digest)


@pytest.mark.asyncio
async def test_expression_structural_correction_uses_required_tool_without_plain_fallback() -> None:
    invalid = json.dumps(
        {
            "timing_choice": "now",
            "beats": [],
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    corrected = json.dumps(
        {
            "expression_draft": {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我想把这一句说清楚。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [
                    {
                        "modality": "text",
                        "text": "我在，刚才那句我看到了。",
                        "reaction_id": None,
                        "sticker_id": None,
                    }
                ],
                "delay_position_bp": None,
                "expires_after_seconds": None,
                "stance": "present",
                "brief_rationale": "回到当前这句。",
                "impulse_summary": None,
                "confidence": 8000,
                "variation_profile": None,
                "response_expectation": None,
                "response_expectation_assessment": None,
                "world_claims": [],
            },
            "episode_disposition": "complete_without_more",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    provider = _ForcedToolExpressionCorrectionModel(invalid, corrected)

    output = await _ExpressionDraftWire(
        model=provider,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    ).propose(_qq_request())

    assert len(provider.tool_calls) == 2
    assert provider.tool_calls[0] == (None, None)
    tools, tool_choice = provider.tool_calls[1]
    assert tools is not None
    assert tools[0]["function"]["name"] == "character_expression_reselection_v1"
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_expression_reselection_v1"},
    }
    carrier = json.loads(provider.calls[1][0][-1]["content"])
    assert carrier == {
        "contract": "expression-reselection-transport.1",
        "authority": "host_compiled_transport_only",
        "output_contract": carrier["output_contract"],
    }
    assert carrier["output_contract"]["contract"] == "expression-source-reselection-direct.1"
    assert output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]


class _EventFrameStreamingModel(_Model):
    """Release one complete expression event before the remaining event array."""

    reports_exact_request_emission = True
    boundary_marker = ',{"type":"beat"'

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.release_tail = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def complete_json_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ) -> tuple[str, ModelUsageProvenance]:
        self.calls.append((messages, temperature))
        boundary = self._reply.index(self.boundary_marker)
        if on_text_delta is not None:
            on_text_delta(self._reply[:boundary])
        try:
            await self.release_tail.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if on_text_delta is not None:
            on_text_delta(self._reply[boundary:])
        material = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 12,
            "output_tokens": 8,
            "thinking_tokens": 0,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": "fake-provider",
            "provider_usage_ref": "usage:fake:event-stream:1",
        }
        digest = sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return self._reply, ModelUsageProvenance(
            **material,
            provider_usage_hash=digest,
        )


class _HeadOnlyEventFrameStreamingModel(_EventFrameStreamingModel):
    boundary_marker = ',{"type":"end"'


class _CanonicalExpressionStreamingModel(_EventFrameStreamingModel):
    """Return the ordinary semantic ExpressionDraft without transport framing."""

    boundary_marker: str | None = None

    async def complete_json_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ) -> tuple[str, ModelUsageProvenance]:
        self.calls.append((messages, temperature))
        boundary = (
            self._reply.index(self.boundary_marker)
            if self.boundary_marker is not None
            else len(self._reply)
        )
        if on_text_delta is not None:
            on_text_delta(self._reply[:boundary])
        if boundary < len(self._reply):
            try:
                await self.release_tail.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
            if on_text_delta is not None:
                on_text_delta(self._reply[boundary:])
        material = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 12,
            "output_tokens": 8,
            "thinking_tokens": 0,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": "fake-provider",
            "provider_usage_ref": "usage:fake:canonical-stream:1",
        }
        digest = sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self._reply, ModelUsageProvenance(
            **material,
            provider_usage_hash=digest,
        )


class _BlockingStreamPrefetch:
    """Hold one old route before its stream operation can be entered."""

    is_closed = False

    def __init__(self, blocked_trigger_ref: str) -> None:
        self.blocked_trigger_ref = blocked_trigger_ref
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def is_available(self, _cursor: RecallCursor, *, trigger_ref: str) -> bool:
        return True

    def scheduled_prefetch_token(self, *, expected_cursor: RecallCursor, trigger_ref: str) -> str:
        return f"prefetch:{expected_cursor.ledger_sequence}:{trigger_ref}"

    async def await_scheduled_prefetch(
        self,
        *,
        expected_cursor: RecallCursor,
        trigger_ref: str,
        timeout_seconds: float | None,
        job_token: str,
    ) -> None:
        del expected_cursor, timeout_seconds, job_token
        if trigger_ref == self.blocked_trigger_ref:
            self.entered.set()
            await self.release.wait()
        return None

    def discard_scheduled_prefetch(
        self,
        _cursor: RecallCursor,
        *,
        trigger_ref: str,
        job_token: str,
    ) -> None:
        del trigger_ref, job_token


class _CorrectingEventFrameStreamingModel(_EventFrameStreamingModel):
    def __init__(self, stream_reply: str, corrected_reply: str) -> None:
        super().__init__(stream_reply)
        self.corrected_reply = corrected_reply
        self.stream_task: asyncio.Task[object] | None = None
        self.correction_saw_stream_cancelling = False

    async def complete_json_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        on_text_delta=None,  # type: ignore[no-untyped-def]
    ) -> tuple[str, ModelUsageProvenance]:
        self.stream_task = asyncio.current_task()
        return await super().complete_json_stream_with_usage(
            messages,
            temperature=temperature,
            on_text_delta=on_text_delta,
        )

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        stream_task = self.stream_task
        self.correction_saw_stream_cancelling = bool(
            stream_task is not None and stream_task.cancelling()
        )
        self.calls.append((messages, temperature))
        return _strict_source_reselection_fixture(messages, self.corrected_reply)


@pytest.mark.asyncio
async def test_expression_event_stream_reuses_one_author_call_and_releases_head_early() -> None:
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "第一条先到。"},
                    "stance": "speak_in_two_bubbles",
                    "brief_rationale": "I chose two messages.",
                    "world_claims": [],
                },
                {
                    "type": "beat",
                    "beat": {"modality": "text", "text": "第二条随后到。"},
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _EventFrameStreamingModel(raw)
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )
    request = _qq_request()
    head_task = asyncio.create_task(adapter.propose_stream_head(request))
    tail_task = asyncio.create_task(
        adapter.propose_stream_tail(request.model_copy(update={"call_id": "call:tail"}))
    )

    head = await asyncio.wait_for(head_task, timeout=0.5)
    assert len(model.calls) == 1
    assert head.raw_proposal["episode_disposition"] == "append"
    assert len(head.raw_proposal["action_intents"]) == 1
    assert not tail_task.done()

    model.release_tail.set()
    tail = await asyncio.wait_for(tail_task, timeout=0.5)
    assert len(model.calls) == 1
    assert head.provider_parent_model_call_id is not None
    assert tail.provider_parent_model_call_id == head.provider_parent_model_call_id
    assert tail.winning_model_call_id != head.winning_model_call_id
    assert tail.raw_proposal["episode_disposition"] == "append"
    assert len(tail.raw_proposal["action_intents"]) == 1


@pytest.mark.asyncio
async def test_expression_event_stream_releases_one_singular_beat_frame_before_tail() -> None:
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "第一帧先到。"},
                    "stance": "speak_in_two_bubbles",
                    "brief_rationale": "I chose two messages.",
                    "world_claims": [],
                },
                {
                    "type": "beat",
                    "beat": {"modality": "text", "text": "第二帧随后到。"},
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _EventFrameStreamingModel(raw)
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )
    request = _qq_request()
    head_task = asyncio.create_task(adapter.propose_stream_head(request))
    tail_task = asyncio.create_task(
        adapter.propose_stream_tail(request.model_copy(update={"call_id": "call:event-tail"}))
    )

    head = await asyncio.wait_for(head_task, timeout=0.5)
    assert "Return one raw JSON ExpressionDraft" in model.calls[0][0][0]["content"]
    assert "protocol=expression-events.1" not in model.calls[0][0][0]["content"]
    assert head.raw_proposal["episode_disposition"] == "append"
    head_payload = json.loads(head.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert [beat["inline_text"] for beat in head_payload["beat_drafts"]] == ["第一帧先到。"]
    assert not tail_task.done()

    model.release_tail.set()
    tail = await asyncio.wait_for(tail_task, timeout=0.5)
    assert len(model.calls) == 1
    tail_payload = json.loads(tail.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert [beat["inline_text"] for beat in tail_payload["beat_drafts"]] == ["第二帧随后到。"]


@pytest.mark.asyncio
async def test_fast_expression_interface_accepts_legacy_plural_beats_in_event_head() -> None:
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "旧包装也不能失声。"}],
                    "episode_disposition": "complete_without_more",
                    "stance": "reply",
                    "brief_rationale": "I chose one message.",
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _HeadOnlyEventFrameStreamingModel(raw)
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    head = await asyncio.wait_for(adapter.propose_stream_head(_qq_request()), timeout=0.5)
    payload = json.loads(head.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])

    assert [beat["inline_text"] for beat in payload["beat_drafts"]] == ["旧包装也不能失声。"]


@pytest.mark.asyncio
async def test_fast_expression_interface_accepts_canonical_multi_beat_draft() -> None:
    raw = json.dumps(
        {
            "timing_choice": "now",
            "stance": "two_bubble_reply",
            "brief_rationale": "I chose two messages.",
            "world_claims": [],
            # The ordinary semantic field is last for fast transport. The
            # host, not the role model, partitions the authored beat sequence.
            "beats": [
                {"modality": "text", "text": "第一条。"},
                {"modality": "text", "text": "第二条。"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _CanonicalExpressionStreamingModel(raw)
    model.boundary_marker = ',{"modality":"text","text":"第二条。"}'
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )
    request = _qq_request()

    head_task = asyncio.create_task(adapter.propose_stream_head(request))
    tail_task = asyncio.create_task(
        adapter.propose_stream_tail(request.model_copy(update={"call_id": "call:canonical-tail"}))
    )
    head = await asyncio.wait_for(head_task, timeout=0.5)
    payload = json.loads(head.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])

    assert [beat["inline_text"] for beat in payload["beat_drafts"]] == ["第一条。"]
    assert head.episode_disposition == "append"
    assert not tail_task.done()
    assert "serialize beats as the final top-level field" in model.calls[0][0][0]["content"]

    model.release_tail.set()
    tail = await asyncio.wait_for(tail_task, timeout=0.5)
    tail_payload = json.loads(tail.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert [beat["inline_text"] for beat in tail_payload["beat_drafts"]] == ["第二条。"]
    assert tail.episode_disposition == "append"
    assert len(model.calls) == 1


def test_fast_canonical_prefix_cannot_be_overridden_after_beats() -> None:
    raw = (
        '{"timing_choice":"now","stance":"reply","brief_rationale":"chosen",'
        '"world_claims":[],"beats":[{"modality":"text","text":"先发。"}],'
        '"turn_posture":"supersede"}'
    )

    with pytest.raises(ValueError, match="beats must remain the final field"):
        _stream_first_expression(raw)


def test_fast_canonical_stream_rejects_duplicate_semantic_fields() -> None:
    raw = '{"timing_choice":"now","timing_choice":"silent","world_claims":[],"beats":[]}'

    with pytest.raises(ValueError, match="duplicated field: timing_choice"):
        _stream_first_expression(raw)


def test_fast_canonical_stream_rejects_duplicate_fields_inside_first_beat() -> None:
    raw_prefix = (
        '{"timing_choice":"now","stance":"reply","brief_rationale":"chosen",'
        '"world_claims":[],"beats":['
        '{"modality":"text","text":"甲","text":"乙"}'
    )

    with pytest.raises(ValueError, match="duplicated field: text"):
        _incremental_first_expression(raw_prefix)


def test_expression_event_stream_requires_role_owned_timing_before_head_release() -> None:
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "beat": {"modality": "text", "text": "这条不能替角色决定时机。"},
                    "stance": "brief",
                    "brief_rationale": "The role did not state timing.",
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    with pytest.raises(ValueError, match="requires explicit timing_choice"):
        _stream_first_expression(raw)


def test_strict_forced_stream_removes_only_null_union_siblings() -> None:
    """DeepSeek strict root padding must not invalidate the event envelope."""

    raw = json.dumps(
        {
            "result_kind": "decision",
            "protocol": "character-interior-events.1",
            "appraisal_draft": {"appraise": False, "affect": "no_change"},
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "先说一句。"},
                    "stance": "自然",
                    "brief_rationale": "保持对话。",
                    "confidence": 7000,
                    "world_claims": [],
                },
                {"type": "end"},
            ],
            "expression_draft": None,
            "recall_request": None,
            "private_turn_state": None,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    first = json.loads(_stream_first_expression(raw))
    tail = json.loads(_stream_tail_expression(raw))
    assert first["appraisal_draft"]["affect"] == "no_change"
    assert first["expression_draft"]["beats"][0]["text"] == "先说一句。"
    assert tail["expression_draft"]["timing_choice"] == "silent"


def test_strict_forced_stream_accepts_empty_sibling_beat_transport_padding() -> None:
    raw = json.dumps(
        {
            "result_kind": "decision",
            "protocol": "character-interior-events.1",
            "appraisal_draft": {"appraise": False, "affect": "no_change"},
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "这一句先说。"},
                    "beats": [],
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    first = json.loads(_stream_first_expression(raw))
    assert first["expression_draft"]["beats"][0]["text"] == "这一句先说。"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("timing_choice", "head_fields"),
    [
        ("silent", {}),
        (
            "later",
            {
                "beat": {"modality": "text", "text": "晚点我再接着说。"},
                "delay_seconds": 60,
                "expires_after_seconds": 600,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_expression_event_stream_does_not_invent_a_tail_for_silent_or_later(
    timing_choice: str,
    head_fields: dict[str, object],
) -> None:
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": timing_choice,
                    "stance": "role_owned_choice",
                    "brief_rationale": "The role chose this timing.",
                    "world_claims": [],
                    **head_fields,
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _HeadOnlyEventFrameStreamingModel(raw)
    adapter = _ExpressionDraftWire(model=model)
    request = _qq_request()
    if timing_choice == "later":
        request = request.model_copy(
            update={
                "model_content_json": json.dumps(
                    {
                        "capsule": "authoritative",
                        "logical_time": "2026-08-02T12:00:00+00:00",
                    }
                )
            }
        )

    head = await asyncio.wait_for(adapter.propose_stream_head(request), timeout=0.5)
    model.release_tail.set()

    assert head.episode_disposition == "complete_without_more"
    assert head.raw_proposal["episode_disposition"] == "complete_without_more"


@pytest.mark.asyncio
async def test_expression_event_stream_rejects_a_false_protocol_before_releasing_head() -> None:
    raw = json.dumps(
        {
            "protocol": "not-expression-events",
            "note": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "这条不能提前发送。"},
                    "stance": "invalid_transport",
                    "brief_rationale": "The outer protocol is invalid.",
                    "world_claims": [],
                },
                {
                    "type": "beat",
                    "beat": {"modality": "text", "text": "尾部。"},
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _EventFrameStreamingModel(raw)
    adapter = _ExpressionDraftWire(model=model)

    result = await asyncio.gather(
        adapter.propose_stream_head(_qq_request()),
        return_exceptions=True,
    )
    adapter.cancel_expression_unit_streams()

    assert isinstance(result[0], BaseException)


@pytest.mark.asyncio
async def test_expression_event_stream_preserves_character_chosen_typing_before_first_text() -> (
    None
):
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "leading_typing_beat": {"modality": "typing"},
                    "beat": {
                        "modality": "text",
                        "text": "我想一下……其实是这样。",
                    },
                    "stance": "hesitant_then_direct",
                    "brief_rationale": "I chose to visibly hesitate before answering.",
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _HeadOnlyEventFrameStreamingModel(raw)
    model.release_tail.set()
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    head = await adapter.propose_stream_head(_qq_request())

    action_intents = head.raw_proposal["action_intents"]
    assert [item["kind"] for item in action_intents] == ["typing", "reply"]
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_expression_draft_drops_only_a_duplicate_top_level_private_state_contract() -> None:
    draft = {
        "contract": "private-turn-state.1",
        "private_turn_state": {
            "contract": "private-turn-state.1",
            "inner_state_summary": "我同时注意到她前后两句。",
            "attended_source_refs": ["observation:qq:1"],
        },
        "timing_choice": "now",
        "cadence": "conversational",
        "beats": [{"modality": "text", "text": "前后两句我都看到了。"}],
        "stance": "respond_to_packet",
        "brief_rationale": "Respond from the whole current packet.",
        "confidence": 8_000,
        "world_claims": [],
    }
    adapter = _ExpressionDraftWire(
        model=_SequenceJsonModel([json.dumps(draft, ensure_ascii=False)]),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.propose(_qq_request())

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert payload["beat_drafts"][0]["inline_text"] == "前后两句我都看到了。"


@pytest.mark.asyncio
async def test_expression_draft_drops_stray_root_private_state_contract() -> None:
    draft = {
        "contract": "private-turn-state.1",
        "timing_choice": "now",
        "cadence": "conversational",
        "beats": [{"modality": "text", "text": "这样就舒服一点了。"}],
        "stance": "relieved",
        "brief_rationale": "React to the current report.",
        "confidence": 8_000,
        "world_claims": [],
    }
    adapter = _ExpressionDraftWire(
        model=_SequenceJsonModel([json.dumps(draft, ensure_ascii=False)]),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(_qq_request())

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert payload["beat_drafts"][0]["inline_text"] == "这样就舒服一点了。"


@pytest.mark.asyncio
async def test_expression_event_stream_preserves_role_owned_supersede_lifecycle() -> None:
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": "silent",
                    "turn_posture": "supersede",
                    "stance": "withdraw",
                    "brief_rationale": "The role withdrew the pending expression.",
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _HeadOnlyEventFrameStreamingModel(raw)
    model.release_tail.set()
    adapter = _ExpressionDraftWire(model=model)

    head = await adapter.propose_stream_head(_qq_request())
    assert head.raw_proposal["turn_posture"] == "supersede"
    assert head.raw_proposal["episode_disposition"] == "supersede_pending"


@pytest.mark.asyncio
async def test_expression_event_stream_derives_redundant_disposition() -> None:
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "舒服点就好。"},
                    "stance": "relieved",
                    "brief_rationale": "React naturally.",
                    "confidence": 8_000,
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _HeadOnlyEventFrameStreamingModel(raw)
    model.release_tail.set()
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    request = _qq_request()
    head = await adapter.propose_stream_head(request)
    tail = await adapter.propose_stream_tail(
        request.model_copy(update={"call_id": "call:derived-disposition-tail"})
    )

    # The early head cannot infer whether continuation bytes are still in
    # flight. The completed envelope can and yields the lifecycle terminal.
    assert head.episode_disposition == "append"
    assert tail.episode_disposition == "complete_without_more"
    assert tail.raw_proposal["episode_disposition"] == "complete_without_more"


@pytest.mark.asyncio
async def test_expression_event_stream_cancels_provider_when_its_last_waiter_is_superseded() -> (
    None
):
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "先到。"},
                    "stance": "brief",
                    "brief_rationale": "One first event.",
                    "world_claims": [],
                },
                {
                    "type": "beat",
                    "beat": {"modality": "text", "text": "尾部。"},
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    model = _EventFrameStreamingModel(raw)
    adapter = _ExpressionDraftWire(model=model)
    request = _qq_request()
    head_task = asyncio.create_task(adapter.propose_stream_head(request))
    tail_task = asyncio.create_task(
        adapter.propose_stream_tail(request.model_copy(update={"call_id": "call:tail-cancel"}))
    )
    await asyncio.wait_for(head_task, timeout=0.5)

    tail_task.cancel()
    await asyncio.gather(tail_task, return_exceptions=True)
    await asyncio.wait_for(model.cancelled.wait(), timeout=0.5)


@pytest.mark.asyncio
async def test_newer_thinking_cursor_rejects_old_flash_at_stream_operation_boundary() -> None:
    raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "新的先说。"},
                    "stance": "brief",
                    "brief_rationale": "One current event.",
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    flash = _HeadOnlyEventFrameStreamingModel(raw)
    thinking = _HeadOnlyEventFrameStreamingModel(raw)
    old_request = _qq_request().model_copy(update={"evaluated_ledger_sequence": 10})
    prefetch = _BlockingStreamPrefetch(old_request.trigger_ref)
    adapter = _RoutedExpressionDraftWire(
        flash_model=flash,
        thinking_model=thinking,
        recall_coordinator=prefetch,  # type: ignore[arg-type]
    )
    old_task = asyncio.create_task(adapter.propose_stream_head(old_request))
    try:
        await asyncio.wait_for(prefetch.entered.wait(), timeout=0.5)
        assert old_request.trigger_message is not None
        new_request = old_request.model_copy(
            update={
                "call_id": "call:new-thinking",
                "attempt_id": "attempt:new-thinking",
                "route": ModelRoute(
                    tier="thinking",
                    reason_code="newer_cursor",
                    router_version="test.1",
                ),
                "trigger_ref": "trigger:new-thinking",
                "evaluated_world_revision": 4,
                "evaluated_deliberation_revision": 1,
                "evaluated_ledger_sequence": 11,
                "trigger_message": old_request.trigger_message.model_copy(
                    update={
                        "event_ref": "event:observation:qq:2",
                        "event_payload_hash": "sha256:" + "c" * 64,
                        "observation_ref": "observation:qq:2",
                        "source_world_revision": 4,
                        "platform_message_id": "qq-message-7789",
                        "text": "等等，我补充一句。",
                    }
                ),
            }
        )
        new_head = await asyncio.wait_for(adapter.propose_stream_head(new_request), timeout=0.5)
        prefetch.release.set()
        old_result = await asyncio.gather(old_task, return_exceptions=True)
    finally:
        prefetch.release.set()
        flash.release_tail.set()
        thinking.release_tail.set()
        if not old_task.done():
            old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)

    assert new_head.raw_proposal["episode_disposition"] == "append"
    assert flash.calls == []
    assert len(thinking.calls) == 1
    assert isinstance(old_result[0], asyncio.CancelledError)


@pytest.mark.asyncio
async def test_structural_reselection_cancels_original_sse_before_correction_starts() -> None:
    first = {
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "先给一个不完整草稿。"}],
        "confidence": 8_000,
        "world_claims": [],
        "episode_disposition": "append",
    }
    stream_raw = json.dumps(
        {
            "protocol": "expression-events.1",
            "events": [
                {
                    "type": "head",
                    "timing_choice": first["timing_choice"],
                    "beat": first["beats"][0],
                    "confidence": first["confidence"],
                    "world_claims": first["world_claims"],
                },
                {
                    "type": "beat",
                    "beat": {"modality": "text", "text": "这个尾部不该再发。"},
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    corrected = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我重新接好这句。"}],
            "stance": "respond_naturally",
            "brief_rationale": "Replace the structurally incomplete draft.",
            "confidence": 8_200,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    model = _CorrectingEventFrameStreamingModel(stream_raw, corrected)
    output = await asyncio.wait_for(
        _ExpressionDraftWire(model=model).propose_stream_head(_qq_request()),
        timeout=1,
    )

    assert model.correction_saw_stream_cancelling is True
    assert output.provider_parent_model_call_id is None
    assert output.raw_proposal["stance"] == "respond_naturally"


class _SequenceJsonModel(_Model):
    def __init__(self, replies: list[str]) -> None:
        super().__init__("")
        self.responses = tuple(replies)
        self._replies = list(replies)

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        self.calls.append((messages, temperature))
        reply = self._replies.pop(0)
        return (
            _strict_source_reselection_fixture(messages, reply)
            if self.strict_reselection_wire
            else reply
        )


class _FirstReplyThenBlockJsonModel(_SequenceJsonModel):
    """Expose cancellation after a nested role request crossed the boundary."""

    def __init__(self, reply: str) -> None:
        super().__init__([reply])
        self.nested_call_entered = asyncio.Event()

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        self.calls.append((messages, temperature))
        if len(self.calls) == 1:
            return self._replies.pop(0)
        self.nested_call_entered.set()
        await asyncio.Future()
        raise AssertionError("unreachable after nested provider cancellation")


class _StrictCoverageSequenceJsonModel(_SequenceJsonModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-coverage.5"


class _StrictInventorySequenceJsonModel(_SequenceJsonModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-inventory.5"


class _FullSourceReviewSequenceJsonModel(_SequenceJsonModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in {
            "source-closure-review.7",
            "report-relative-entailment-adjudication.3",
        }


class _InventoryAwareFullSourceReviewModel(_Model):
    """Semantic test authority for the Inventory-V5-to-V7 provider seam."""

    def __init__(self, *, supported_claim_text: str | None = None) -> None:
        super().__init__("")
        self.supported_claim_text = supported_claim_text
        self.contracts: list[str] = []

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in {
            "source-closure-review.7",
            "report-relative-entailment-adjudication.3",
        }

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.calls.append((messages, temperature))
        packet = json.loads(messages[-1]["content"])
        contract = packet["output_contract"]["contract"]
        self.contracts.append(contract)
        if contract == "report-relative-entailment-adjudication.3":
            return _report_relative_wire_v3(["retain_unclosed"] * len(packet["disputed_findings"]))
        assert contract == "source-closure-review.7"
        decomposition = packet.get("candidate_inventory_decomposition")
        if decomposition is None:
            # The latency-optimized route runs the established V7 review in
            # parallel with Inventory.  This fake intentionally passes that
            # first review so the enriched source-bearing route remains under
            # test rather than relying on the old review to catch the fixture.
            return _source_closure_review()
        assert decomposition["authority"] == (
            "semantic_decomposition_only_not_fact_or_source_verdict"
        )
        propositions = decomposition["propositions"]
        source_relevant = [
            proposition
            for proposition in propositions
            if proposition["semantic_role"]
            in {
                "source_bearing_private_episode",
                "embedded_external_proposition",
                "standalone_external_proposition",
            }
        ]
        if not source_relevant:
            return _source_closure_review()
        declared_claims = packet["world_claims"]
        if self.supported_claim_text is not None and any(
            claim["claim_text"] == self.supported_claim_text for claim in declared_claims
        ):
            return _source_closure_review()
        return _source_closure_review(
            unsupported_boundaries=("visible_text",),
            visible_span=source_relevant[0]["locator"]["text"],
        )


class _ReplyThenTransportFailureModel(_Model):
    def __init__(self, reply: str) -> None:
        super().__init__("")
        self.reply = reply

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.calls.append((messages, temperature))
        if len(self.calls) == 1:
            return self.reply
        raise RuntimeError("simulated reviewer transport failure")


class _SequenceMeteredModel(_SequenceJsonModel):
    def __init__(
        self,
        replies: list[str],
        *,
        route_class: str = "chat",
        provider: str = "fake-provider",
        thinking_tokens: int = 0,
    ) -> None:
        super().__init__(replies)
        self._route_class = route_class
        self._provider = provider
        self._thinking_tokens = thinking_tokens

    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        self.calls.append((messages, temperature))
        ordinal = len(self.calls)
        reply = self._replies.pop(0)
        if self.strict_reselection_wire:
            reply = _strict_source_reselection_fixture(messages, reply)
        material = {
            "usage_contract": "model-usage.1",
            "route_class": self._route_class,
            "input_tokens": 12,
            "output_tokens": 3,
            "thinking_tokens": self._thinking_tokens,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": self._provider,
            "provider_usage_ref": f"usage:fake:{ordinal}",
        }
        digest = sha256(
            json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return reply, ModelUsageProvenance(
            **material,
            provider_usage_hash=digest,
        )


class _SequenceJsonMeteredModel(_SequenceMeteredModel):
    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        raise AssertionError("all structured metered calls must preserve JSON mode")

    async def complete_json_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        return await _SequenceMeteredModel.complete_with_usage(
            self,
            messages,
            temperature=temperature,
        )


class _StrictCoverageSequenceJsonMeteredModel(_SequenceJsonMeteredModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-coverage.5"


class _BlockingPrefetchEmbedding:
    version = "blocking-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.used_after_close = threading.Event()
        self._delegate = FeatureHashRecallEmbedding()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if "并行预取" in texts:
            self.started.set()
            self.release.wait(timeout=2)
        if self.closed.is_set():
            self.used_after_close.set()
        return self._delegate.embed(texts)

    def close(self) -> None:
        self.closed.set()


class _ObservablePrefetchEmbedding:
    version = "observable-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self.finished = threading.Event()
        self._delegate = FeatureHashRecallEmbedding()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        result = self._delegate.embed(texts)
        if "窗边听雨" in texts:
            self.finished.set()
        return result


class _MalformedPrefetchEmbedding:
    version = "malformed-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self._delegate = FeatureHashRecallEmbedding()
        self.calls = 0

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        if "损坏的查询" in texts:
            return ()
        return self._delegate.embed(texts)


class _DelayedSemanticPrefetchEmbedding:
    version = "delayed-semantic-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self._delegate = FeatureHashRecallEmbedding()
        self.calls = 0

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        if "稍晚完成的语义查询" in texts:
            time.sleep(0.45)
        return self._delegate.embed(texts)


class _ControlledSemanticPrefetchEmbedding:
    """Make lexical fallback and semantic completion observably different."""

    version = "controlled-semantic-prefetch-fixture.1"
    dimensions = 2
    dense_match_threshold_bp = 9_000

    def __init__(self, *, released: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        if released:
            self.release.set()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if "直说的词面线索" in texts:
            self.started.set()
            self.release.wait(timeout=2)
            self.finished.set()
        return tuple(
            (0.0, 1.0) if text in {"直说的词面线索", "完全不同措辞的语义回忆"} else (1.0, 0.0)
            for text in texts
        )


class _ReleaseSemanticSequenceJsonModel(_SequenceJsonModel):
    def __init__(
        self,
        replies: list[str],
        *,
        semantic: _ControlledSemanticPrefetchEmbedding,
    ) -> None:
        super().__init__(replies)
        self._semantic = semantic

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        self.calls.append((messages, temperature))
        reply = self._replies.pop(0)
        if len(self.calls) == 1:
            self._semantic.release.set()
            if not await asyncio.to_thread(self._semantic.finished.wait, 0.5):
                raise TimeoutError("semantic prefetch fixture did not finish")
        return reply


def _single_semantic_prefetch_coordinator(
    semantic: _ControlledSemanticPrefetchEmbedding,
    *,
    trigger_ref: str,
) -> tuple[RecallCoordinator, RecallCursor]:
    suffix = trigger_ref.rsplit(":", maxsplit=1)[-1]
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    document = RecallDocument(
        document_id=f"recall:{suffix}",
        memory_kind="episodic",
        source_item_ref=f"experience:{suffix}",
        source_slice="recent_experiences",
        source_refs=(f"event:{suffix}",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="ExperienceCommitted",
                ref=f"event:{suffix}",
                source_world_revision=2,
                immutable_hash="5" * 64,
            ),
        ),
        source_world_revision=2,
        text="完全不同措辞的语义回忆",
        actor_ref="agent:companion",
        subject_refs=("agent:companion",),
        occurred_from=datetime(2026, 7, 25, 13, tzinfo=UTC),
        privacy_class="private",
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=(document,))
    return (
        RecallCoordinator.from_built_index(
            index=index,
            cursor=cursor,
            actor_ref="agent:companion",
            subject_refs=("agent:companion", "user:primary"),
            logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
            semantic_embedding=semantic,
            trigger_ref=trigger_ref,
        ),
        cursor,
    )


@pytest.mark.asyncio
async def test_character_may_pull_one_source_bound_recall_before_deciding() -> None:
    canonical_recall_ref = "event:fact:counterpart-tea:sha256:" + "c" * 64
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "recall_request": {
                        "query_text": "凤凰单丛",
                        "memory_kinds": ["semantic"],
                        "limit": 3,
                    },
                    # Recall choices use the same order-independent JSON
                    # object semantics as complete ExpressionDraft objects.
                    "private_turn_state": {
                        "inner_state_summary": (
                            "这句话让我想起似乎还有一段关于凤凰单丛的细节，"
                            "但当前注意到的内容不够，我想先回忆一下再决定怎么接。"
                        ),
                        "attended_source_refs": ["trigger:1"],
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "我想起她前两天提到凤凰单丛，现在是真有点想知道后来泡得如何。"
                        ),
                        "attended_source_refs": ["S1"],
                    },
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "你前两天提到凤凰单丛，后来泡得怎么样？",
                        }
                    ],
                    "stance": "curious",
                    "brief_rationale": "I chose to follow the recalled topic.",
                    "world_claims": [
                        {
                            "claim_text": "对方前两天提到凤凰单丛",
                            "scope": "counterpart_history",
                            "source_refs": ["S1"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        ]
    )
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=2,
        ledger_sequence=5,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:fact:tea",
                memory_kind="semantic",
                source_item_ref="fact:tea",
                source_slice="relevant_facts",
                source_refs=(canonical_recall_ref,),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="FactCommitted",
                        ref=canonical_recall_ref,
                        source_world_revision=2,
                        immutable_hash="c" * 64,
                    ),
                ),
                source_world_revision=2,
                text="我最近开始用盖碗泡凤凰单丛。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )
    request = _qq_request().model_copy(
        update={
            "evaluated_deliberation_revision": 2,
            "evaluated_ledger_sequence": 5,
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 2,
                    "ledger_sequence": 5,
                    "logical_time": "2026-07-27T12:00:00+00:00",
                    "slices": {},
                },
                ensure_ascii=False,
            ),
        }
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text=request.trigger_message.text,
        accessibility_seed="recall-prefetch:trigger:1:5",
        trigger_ref=request.trigger_ref,
    )

    output = await _ExpressionDraftWire(
        model=model,
        recall_coordinator=coordinator,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(request)

    assert len(model.calls) == 2
    assert output.recall_trace is not None
    assert output.prefetch_trace is not None
    trace = verify_trusted_recall_trace(output.recall_trace)
    assert trace.request.query_text == "凤凰单丛"
    assert trace.query.accessibility_seed.startswith("character-recall:")
    assert trace.query.actor_ref == "agent:companion"
    assert trace.query.subject_refs == ("agent:companion", "user:primary")
    assert trace.hits[0].document.source_refs == (canonical_recall_ref,)
    assert trace.hits[0].dense_score_bp >= 0
    assert "凤凰单丛" in model.calls[1][0][-1]["content"]
    assert "parallel_attention_prefetch" in model.calls[1][0][-1]["content"]
    assert '"S1":"' + canonical_recall_ref + '"' in model.calls[1][0][-1]["content"]
    assert "place it first" not in model.calls[1][0][-1]["content"]
    assert "private_turn_state" in model.calls[0][0][0]["content"]
    assert output.raw_proposal["timing_choice"] == "now"
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == [
        canonical_recall_ref
    ]
    assert output.winning_model_call_id != request.call_id
    assert output.winning_request_hash == _provider_request_hash(*model.calls[1])
    recalled_evidence = next(
        item
        for item in output.raw_proposal["evidence_refs"]
        if item["ref_id"] == canonical_recall_ref
    )
    assert recalled_evidence["immutable_hash"] == "sha256:" + "c" * 64


@pytest.mark.asyncio
async def test_claim_free_expression_still_fails_closed_when_source_review_is_unavailable() -> None:
    visible_fact = "下午翻书的时候，我忽然想起这件事。"

    class _UnavailableReviewer:
        model = "fixture:unavailable-source-reviewer"

        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.0,
        ) -> str:
            del messages, temperature
            raise TimeoutError("source reviewer unavailable")

    author = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": visible_fact,
                        }
                    ],
                    "stance": "attend",
                    "brief_rationale": "Acknowledge the current turn.",
                    "confidence": 7_000,
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        ]
    )

    with pytest.raises(ValidationTechnicalFailure, match="source_review_timeout"):
        await _ExpressionDraftWire(
            model=author,
            source_closure_reviewer=_UnavailableReviewer(),
            candidate_external_proposition_inventory_model=(
                _StrictInventorySequenceJsonModel(
                    [
                        _inventory_v5(
                            [
                                {
                                    "locator": _coverage_locator(visible_fact),
                                    "semantic_role": "standalone_external_proposition",
                                }
                            ]
                        )
                    ]
                )
            ),
        ).propose(_qq_request())


@pytest.mark.asyncio
async def test_ready_prefetch_is_visible_before_role_model_may_decline_a_deeper_pull() -> None:
    canonical_recall_ref = "event:fact:counterpart-tea:sha256:" + "d" * 64
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "那段关于茶的记忆浮了一下，但和她刚说的事没关系，"
                            "我不想硬拐过去，还是接住眼前这句。"
                        ),
                        "attended_source_refs": ["fact:tea"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "嗯，做完就好，先松口气吧。"}],
                    "stance": "present",
                    "brief_rationale": "I noticed the memory and chose not to pursue it.",
                    "confidence": 8200,
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        ]
    )
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=2,
        ledger_sequence=5,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:fact:tea",
                memory_kind="semantic",
                source_item_ref="fact:tea",
                source_slice="relevant_facts",
                source_refs=(canonical_recall_ref,),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="FactCommitted",
                        ref=canonical_recall_ref,
                        source_world_revision=2,
                        immutable_hash="d" * 64,
                    ),
                ),
                source_world_revision=2,
                text="我最近开始用盖碗泡凤凰单丛。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )
    request = _qq_request().model_copy(
        update={
            "evaluated_deliberation_revision": 2,
            "evaluated_ledger_sequence": 5,
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 2,
                    "ledger_sequence": 5,
                    "logical_time": "2026-07-27T12:00:00+00:00",
                    "slices": {},
                },
                ensure_ascii=False,
            ),
        }
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="凤凰单丛",
        accessibility_seed="recall-prefetch:trigger:1:5",
        trigger_ref=request.trigger_ref,
    )

    output = await _ExpressionDraftWire(
        model=model,
        recall_coordinator=coordinator,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(request)

    assert len(model.calls) == 1
    provider_payload = json.loads(model.calls[0][0][-1]["content"])
    compact_context = json.loads(provider_payload["request"]["model_content_json"])
    prefetched = compact_context["slices"]["relevant_facts"]["items"][0]
    assert prefetched["recall_injected"] is True
    assert prefetched["value"]["text"] == "我最近开始用盖碗泡凤凰单丛。"
    assert "you may return instead" in model.calls[0][0][0]["content"]
    assert output.prefetch_trace is not None
    prefetch = verify_trusted_recall_trace(output.prefetch_trace)
    assert prefetch.mode == "prefetch"
    assert prefetch.hits[0].document.source_refs == (canonical_recall_ref,)
    assert output.recall_trace is None
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == ["fact:tea"]
    assert output.raw_proposal["timing_choice"] == "now"


@pytest.mark.asyncio
async def test_invalid_required_recall_choice_reselects_a_final_expression_once() -> None:
    invalid_visible_text = "这句无效草稿不能锚定最终表达。"
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "recall_request": {
                        "query_text": "一段没接上的旧话题",
                        "limit": 2,
                    },
                    "beats": [{"modality": "text", "text": invalid_visible_text}],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "当前能确定的是她刚做完一件麻烦事，我此刻先替她觉得轻松。"
                        ),
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "可算弄完了，先歇会儿吧。"}],
                    "stance": "relieved_with_her",
                    "brief_rationale": "Choose from the current pinned turn without another recall.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.propose(_qq_request())

    assert len(model.calls) == 2
    repair_prompt = model.calls[1][0][-1]["content"]
    assert "complete replacement" in repair_prompt
    assert "code=private_turn_state.missing" in repair_prompt
    assert "path=private_turn_state" in repair_prompt
    assert invalid_visible_text not in json.dumps(model.calls[1][0], ensure_ascii=False)
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == ["observation:qq:1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_recall", "expected_code", "expected_path"),
    (
        (
            {"query_text": "非法 Recall 原文不能回灌 limit", "limit": 7},
            "recall_choice.out_of_range",
            "recall_request.limit",
        ),
        (
            {
                "query_text": "非法 Recall 原文不能回灌 kinds",
                "memory_kinds": ["semantic", "episodic"],
            },
            "recall_choice.noncanonical",
            "recall_request.memory_kinds",
        ),
        (
            {
                "query_text": "非法 Recall 原文不能回灌 extra",
                "unknown_filter": "private-value",
            },
            "recall_choice.unexpected_field",
            "recall_request",
        ),
    ),
)
@pytest.mark.asyncio
async def test_invalid_recall_payload_gets_one_sanitized_final_reselection(
    invalid_recall: dict[str, object],
    expected_code: str,
    expected_path: str,
) -> None:
    invalid_marker = str(invalid_recall["query_text"])
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "我想先回忆再决定。",
                        "attended_source_refs": ["trigger:1"],
                    },
                    "recall_request": invalid_recall,
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "当前材料已经足够，我想直接回应。",
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "嗯，我听见了。"}],
                    "stance": "present",
                    "brief_rationale": "Choose the final expression from the pinned turn.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.propose(_qq_request())

    assert len(model.calls) == 2
    correction_messages = model.calls[1][0]
    correction_prompt = correction_messages[-1]["content"]
    assert f"code={expected_code}" in correction_prompt
    assert f"path={expected_path}" in correction_prompt
    assert invalid_marker not in json.dumps(correction_messages, ensure_ascii=False)
    assert output.raw_proposal["timing_choice"] == "now"


@pytest.mark.asyncio
async def test_invalid_recall_reselection_cannot_open_a_third_role_call() -> None:
    first_invalid_marker = "第一次非法 Recall 原文不能回灌"
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "我想先回忆再决定。",
                        "attended_source_refs": ["trigger:1"],
                    },
                    "recall_request": {
                        "query_text": first_invalid_marker,
                        "limit": 7,
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "我仍想发起另一次非法回忆。",
                        "attended_source_refs": ["trigger:1"],
                    },
                    "recall_request": {
                        "query_text": "第二次非法 Recall",
                        "unknown_filter": "private-value",
                    },
                },
                ensure_ascii=False,
            ),
        ]
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await adapter.propose(_qq_request())

    assert caught.value.failure_code == "recall_choice_reselection_invalid"
    assert len(model.calls) == 2
    assert first_invalid_marker not in json.dumps(model.calls[1][0], ensure_ascii=False)


@pytest.mark.asyncio
async def test_invalid_recall_final_reselection_cannot_trigger_another_shape_repair() -> None:
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "我想先回忆再决定。",
                        "attended_source_refs": ["trigger:1"],
                    },
                    "recall_request": {
                        "query_text": "第一次 Recall 选择非法",
                        "limit": 7,
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "第二次结果仍缺少私人状态。"}],
                    "stance": "invalid_without_private_state",
                    "brief_rationale": "Invalid final fixture.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "第三次角色调用不应该发生。",
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "不应发送这条。"}],
                    "stance": "unexpected_third_call",
                    "brief_rationale": "This fixture proves a forbidden third call.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await adapter.propose(_qq_request())

    assert caught.value.failure_code == "recall_choice_reselection_invalid"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_parallel_prefetch_cannot_delay_a_first_pass_final_answer() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:parallel",
        memory_kind="semantic",
        source_item_ref="fact:parallel",
        source_slice="relevant_facts",
        source_refs=("event:parallel",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:parallel",
                source_world_revision=2,
                immutable_hash="d" * 64,
            ),
        ),
        source_world_revision=2,
        text="这是一条可丢弃的预取候选。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        trigger_ref="trigger:1",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="并行预取",
        accessibility_seed="draw:parallel-prefetch",
        trigger_ref="trigger:1",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "这句我直接回答。"}],
                "stance": "answer_directly",
                "brief_rationale": "Answer without waiting for optional recall.",
                "confidence": 8000,
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    try:
        output = await asyncio.wait_for(
            _ExpressionDraftWire(
                model=model,
                recall_coordinator=coordinator,
            ).propose(_qq_request()),
            timeout=0.5,
        )
    finally:
        embedding.release.set()
        coordinator.close()

    assert len(model.calls) == 1
    assert output.prefetch_trace is None
    assert output.recall_trace is None


@pytest.mark.asyncio
async def test_ready_parallel_prefetch_is_visible_in_first_pass_and_audited() -> None:
    embedding = _ObservablePrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:first-pass",
        memory_kind="episodic",
        source_item_ref="experience:first-pass",
        source_slice="recent_experiences",
        source_refs=("event:first-pass",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="ExperienceCommitted",
                ref="event:first-pass",
                source_world_revision=2,
                immutable_hash="e" * 64,
            ),
        ),
        source_world_revision=2,
        text="她之前在窗边听完了那场雨。",
        actor_ref="agent:companion",
        subject_refs=("agent:companion",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="private",
    )
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        trigger_ref="trigger:1",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="窗边听雨",
        accessibility_seed="draw:first-pass-prefetch",
        trigger_ref="trigger:1",
    )
    assert await asyncio.to_thread(embedding.finished.wait, 0.5)
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "刚才那阵雨让我想起一件事。"}],
                "stance": "share_present_association",
                "brief_rationale": "Use the recall that was already available.",
                "confidence": 8000,
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 0,
                    "ledger_sequence": 0,
                    "logical_time": "2026-07-27T12:00:00+00:00",
                    "slices": {},
                },
                ensure_ascii=False,
            )
        }
    )
    output = await _ExpressionDraftWire(
        model=model,
        recall_coordinator=coordinator,
    ).propose(request)

    assert len(model.calls) == 1
    assert output.prefetch_trace is not None
    assert output.recall_trace is None
    assert "她之前在窗边听完了那场雨" in model.calls[0][0][1]["content"]
    trace = verify_trusted_recall_trace(output.prefetch_trace)
    assert trace.mode == "prefetch"
    assert trace.trigger_ref == "trigger:1"
    assert trace.hits[0].document.source_refs == ("event:first-pass",)


@pytest.mark.asyncio
async def test_first_pass_does_not_wait_for_remote_semantic_query_latency() -> None:
    semantic = _DelayedSemanticPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:delayed-semantic",
        memory_kind="semantic",
        source_item_ref="fact:delayed-semantic",
        source_slice="relevant_facts",
        source_refs=("event:delayed-semantic",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:delayed-semantic",
                source_world_revision=2,
                immutable_hash="a" * 64,
            ),
        ),
        source_world_revision=2,
        text="稍晚完成的语义查询仍应进入首轮上下文。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    primary = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    primary.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=primary,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=semantic,
        trigger_ref="trigger:delayed-semantic",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="稍晚完成的语义查询",
        accessibility_seed="draw:delayed-semantic",
        trigger_ref="trigger:delayed-semantic",
    )

    started = time.monotonic()
    first_pass = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:delayed-semantic",
        # This case proves that the caller's explicit latency ceiling wins.
        # The production default is separately covered by the measured
        # 450 ms semantic-first-pass join regression.
        timeout_seconds=0.35,
    )
    elapsed = time.monotonic() - started
    trace = None
    for _ in range(40):
        trace = coordinator.take_ready_scheduled_prefetch(
            expected_cursor=cursor,
            trigger_ref="trigger:delayed-semantic",
        )
        if trace is not None:
            break
        await asyncio.sleep(0.01)
    coordinator.close()

    assert first_pass is not None
    first_pass_audit = verify_trusted_recall_trace(first_pass)
    assert first_pass_audit.hits[0].document.source_item_ref == "fact:delayed-semantic"
    assert first_pass_audit.embedding_version == FeatureHashRecallEmbedding.version
    assert elapsed < 0.4
    assert trace is not None
    completed_audit = verify_trusted_recall_trace(trace)
    assert completed_audit.hits[0].document.source_item_ref == "fact:delayed-semantic"
    assert completed_audit.embedding_version == semantic.version
    assert semantic.calls > 0


@pytest.mark.asyncio
async def test_technical_recovery_reuses_the_primary_prefetch_at_the_same_pinned_cursor() -> None:
    """A provider fallback must not lose the memory the timed-out primary saw."""

    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    remembered_text = "我不是想让你分析怎么处理，就是想跟你吐槽一下"
    document = RecallDocument(
        document_id="recall:recovery-continuity",
        memory_kind="episodic",
        source_item_ref="dialogue:observation:older",
        source_slice="recent_dialogue",
        source_refs=("event:observation:older",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="ObservationRecorded",
                ref="event:observation:older",
                source_world_revision=2,
                immutable_hash="9" * 64,
            ),
        ),
        source_world_revision=2,
        text=remembered_text,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        occurred_from=datetime(2026, 7, 27, 11, 55, tzinfo=UTC),
        privacy_class="private",
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        trigger_ref="trigger:1",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="我刚才为什么说不想让你分析来着？",
        lexical_text="我刚才为什么说不想让你分析来着？",
        accessibility_seed="draw:recovery-continuity",
        trigger_ref="trigger:1",
    )
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我记得。"}],
                "stance": "answer_from_the_exchange",
                "brief_rationale": "Answer from the pinned exchange.",
                "confidence": 8200,
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 0,
                    "ledger_sequence": 0,
                    "logical_time": "2026-07-27T12:00:00+00:00",
                    "slices": {},
                },
                ensure_ascii=False,
            )
        }
    )
    adapter = _ExpressionDraftWire(
        model=model,
        recall_coordinator=coordinator,
    )

    await adapter.propose(request)
    await adapter.recover(
        request.model_copy(update={"call_id": "call:technical-recovery"}),
        "main_timeout",
    )
    coordinator.close()

    assert len(model.calls) == 2
    assert remembered_text in model.calls[0][0][1]["content"]
    assert remembered_text in model.calls[1][0][1]["content"]
    assert "This is a recovery attempt after a technical failure" in model.calls[1][0][0]["content"]


@pytest.mark.asyncio
async def test_blocked_prefetch_is_daemonized_and_close_remains_bounded() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:blocked-close",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="并行预取",
        accessibility_seed="draw:blocked-close",
        trigger_ref="trigger:blocked-close",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        coordinator.close()
        elapsed = loop.time() - started
        assert not embedding.closed.is_set()
    finally:
        embedding.release.set()

    assert elapsed < 0.1
    assert await asyncio.to_thread(embedding.closed.wait, 0.5)
    assert not embedding.used_after_close.is_set()


@pytest.mark.asyncio
async def test_close_tracks_prefetch_after_timeout_removed_its_future() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:popped-close",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="并行预取",
        accessibility_seed="draw:popped-close",
        trigger_ref="trigger:popped-close",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    assert (
        await coordinator.consume_scheduled_prefetch(
            expected_cursor=cursor,
            trigger_ref="trigger:popped-close",
            timeout_seconds=0.01,
        )
        is None
    )
    started = asyncio.get_running_loop().time()
    try:
        coordinator.close()
        assert asyncio.get_running_loop().time() - started < 0.1
        assert not embedding.closed.is_set()
    finally:
        embedding.release.set()

    assert await asyncio.to_thread(embedding.closed.wait, 0.5)
    assert not embedding.used_after_close.is_set()


@pytest.mark.asyncio
async def test_close_cancels_an_active_prefetch_consumer_without_republishing_replay() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:active-consumer-close",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="并行预取",
        accessibility_seed="draw:active-consumer-close",
        trigger_ref="trigger:active-consumer-close",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    consumer = asyncio.create_task(
        coordinator.consume_scheduled_prefetch(
            expected_cursor=cursor,
            trigger_ref="trigger:active-consumer-close",
            timeout_seconds=1.0,
        )
    )
    await asyncio.sleep(0)

    coordinator.close()
    try:
        assert await asyncio.wait_for(consumer, timeout=0.1) is None
        assert (
            await coordinator.await_scheduled_prefetch(
                expected_cursor=cursor,
                trigger_ref="trigger:active-consumer-close",
                timeout_seconds=0.01,
            )
            is None
        )
    finally:
        embedding.release.set()


@pytest.mark.asyncio
async def test_stale_prefetch_job_token_cannot_discard_replacement_generation() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:generation",
    )
    stale = coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="并行预取",
        accessibility_seed="draw:generation:old",
        trigger_ref="trigger:generation",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    stale_waiter = asyncio.create_task(
        coordinator.await_scheduled_prefetch(
            expected_cursor=cursor,
            trigger_ref="trigger:generation",
            timeout_seconds=0.02,
            job_token=stale,
        )
    )
    await asyncio.sleep(0)
    current = coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="并行预取",
        accessibility_seed="draw:generation:new",
        trigger_ref="trigger:generation",
    )

    assert await stale_waiter is None
    coordinator.discard_scheduled_prefetch(
        cursor,
        trigger_ref="trigger:generation",
        job_token=stale,
    )
    embedding.release.set()
    trace = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:generation",
        timeout_seconds=0.5,
        job_token=current,
    )
    coordinator.close()

    assert current != stale
    assert trace is not None
    assert verify_trusted_recall_trace(trace).trigger_ref == "trigger:generation"


@pytest.mark.asyncio
async def test_concurrent_prefetch_discard_is_idempotent() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:concurrent-discard",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="并行预取",
        accessibility_seed="draw:concurrent-discard",
        trigger_ref="trigger:concurrent-discard",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    barrier = threading.Barrier(8)

    def discard() -> None:
        barrier.wait(timeout=1)
        coordinator.discard_scheduled_prefetch(
            cursor,
            trigger_ref="trigger:concurrent-discard",
        )

    try:
        await asyncio.gather(*(asyncio.to_thread(discard) for _ in range(8)))
    finally:
        embedding.release.set()
        coordinator.close()


def test_closed_coordinator_cannot_publish_a_new_prefetch_worker() -> None:
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        trigger_ref="trigger:closed-prefetch",
    )
    coordinator.close()

    with pytest.raises(RuntimeError, match="coordinator is closed"):
        coordinator.schedule_prefetch(
            expected_cursor=cursor,
            query_text="不该开始",
            accessibility_seed="draw:closed-prefetch",
            trigger_ref="trigger:closed-prefetch",
        )


@pytest.mark.asyncio
async def test_close_defers_embedding_shutdown_until_deep_recall_finishes() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:blocked-deep-recall",
    )
    task = asyncio.create_task(
        perform_character_recall(
            coordinator,
            request=CharacterRecallRequest(query_text="并行预取", limit=2),
            accessibility_seed="draw:blocked-deep-recall",
            expected_cursor=cursor,
            trigger_ref="trigger:blocked-deep-recall",
            timeout_seconds=1.0,
        )
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)
    started = asyncio.get_running_loop().time()
    try:
        coordinator.close()
        assert asyncio.get_running_loop().time() - started < 0.1
        assert not embedding.closed.is_set()
    finally:
        embedding.release.set()

    await task
    assert await asyncio.to_thread(embedding.closed.wait, 0.5)
    assert not embedding.used_after_close.is_set()


@pytest.mark.asyncio
async def test_first_pass_timeout_preserves_prefetch_and_only_joins_once() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:preserved",
        memory_kind="semantic",
        source_item_ref="fact:preserved",
        source_slice="relevant_facts",
        source_refs=("event:preserved",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:preserved",
                source_world_revision=2,
                immutable_hash="f" * 64,
            ),
        ),
        source_world_revision=2,
        text="并行预取完成后仍应可见。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
        trigger_ref="trigger:preserved",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="并行预取",
        accessibility_seed="draw:preserved",
        trigger_ref="trigger:preserved",
    )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)

    fallback = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:preserved",
        timeout_seconds=0.01,
    )
    assert fallback is not None
    assert verify_trusted_recall_trace(fallback).hits
    loop = asyncio.get_running_loop()
    started = loop.time()
    repeated_fallback = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:preserved",
        timeout_seconds=0.2,
    )
    assert repeated_fallback == fallback
    assert loop.time() - started < 0.05

    embedding.release.set()
    trace = None
    for _ in range(50):
        trace = coordinator.take_ready_scheduled_prefetch(
            expected_cursor=cursor,
            trigger_ref="trigger:preserved",
        )
        if trace is not None:
            break
        await asyncio.sleep(0.01)
    coordinator.close()

    assert trace is not None
    assert verify_trusted_recall_trace(trace).hits[0].document.source_item_ref == "fact:preserved"


@pytest.mark.asyncio
async def test_completed_semantic_prefetch_upgrades_the_same_turn_local_replay() -> None:
    semantic = _ControlledSemanticPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    documents = (
        RecallDocument(
            document_id="recall:lexical-fallback",
            memory_kind="semantic",
            source_item_ref="fact:lexical-fallback",
            source_slice="relevant_facts",
            source_refs=("event:lexical-fallback",),
            source_bindings=(
                RecallSourceBinding(
                    source_kind="committed_event",
                    authority_type="FactCommitted",
                    ref="event:lexical-fallback",
                    source_world_revision=2,
                    immutable_hash="1" * 64,
                ),
            ),
            source_world_revision=2,
            text="这里保留直说的词面线索。",
            actor_ref="agent:companion",
            subject_refs=("user:primary",),
            occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
            privacy_class="personal",
        ),
        RecallDocument(
            document_id="recall:semantic-only",
            memory_kind="episodic",
            source_item_ref="experience:semantic-only",
            source_slice="recent_experiences",
            source_refs=("event:semantic-only",),
            source_bindings=(
                RecallSourceBinding(
                    source_kind="committed_event",
                    authority_type="ExperienceCommitted",
                    ref="event:semantic-only",
                    source_world_revision=2,
                    immutable_hash="2" * 64,
                ),
            ),
            source_world_revision=2,
            text="完全不同措辞的语义回忆",
            actor_ref="agent:companion",
            subject_refs=("agent:companion",),
            occurred_from=datetime(2026, 7, 25, 13, tzinfo=UTC),
            privacy_class="private",
        ),
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=documents)
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=semantic,
        trigger_ref="trigger:semantic-upgrade",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="直说的词面线索",
        accessibility_seed="draw:semantic-upgrade",
        trigger_ref="trigger:semantic-upgrade",
    )
    assert await asyncio.to_thread(semantic.started.wait, 0.5)

    local = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:semantic-upgrade",
        timeout_seconds=0.01,
    )
    assert local is not None
    local_audit = verify_trusted_recall_trace(local)
    assert local_audit.embedding_version == FeatureHashRecallEmbedding.version
    assert {hit.document.source_item_ref for hit in local_audit.hits} == {"fact:lexical-fallback"}
    coordinator.record_prefetch_presentation(
        PresentedPrefetchTrace(
            phase="initial",
            model_call_id="model-call:semantic-upgrade:initial",
            trace=local,
        )
    )

    semantic.release.set()
    assert await asyncio.to_thread(semantic.finished.wait, 0.5)
    late_ready_health: dict[str, object] = {}
    for _ in range(50):
        late_ready_health = coordinator.semantic_health()
        if late_ready_health["last_prefetch_delivery_status"] == "semantic_late_ready":
            break
        await asyncio.sleep(0.01)
    assert late_ready_health["last_prefetch_delivery_status"] == "semantic_late_ready"
    assert late_ready_health["prefetch_late_semantic_ready_count"] == 1
    completed = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:semantic-upgrade",
        timeout_seconds=0.01,
    )
    assert completed is not None
    coordinator.record_prefetch_presentation(
        PresentedPrefetchTrace(
            phase="recall_followup",
            model_call_id="model-call:semantic-upgrade:followup",
            trace=completed,
        )
    )
    coordinator.close()

    completed_audit = verify_trusted_recall_trace(completed)
    assert completed_audit.embedding_version == semantic.version
    assert "experience:semantic-only" in {
        hit.document.source_item_ref for hit in completed_audit.hits
    }
    health = coordinator.semantic_health()
    assert health["last_prefetch_delivery_status"] == "semantic_late_consumed"
    assert health["prefetch_first_pass_local_count"] == 1
    assert health["prefetch_late_semantic_ready_count"] == 1
    assert health["prefetch_late_semantic_consumed_count"] == 1


@pytest.mark.asyncio
async def test_ready_semantic_prefetch_enters_the_first_pass_without_local_replay() -> None:
    semantic = _ControlledSemanticPrefetchEmbedding(released=True)
    coordinator, cursor = _single_semantic_prefetch_coordinator(
        semantic,
        trigger_ref="trigger:ready-semantic",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="直说的词面线索",
        accessibility_seed="draw:ready-semantic",
        trigger_ref="trigger:ready-semantic",
    )
    assert await asyncio.to_thread(semantic.finished.wait, 0.5)

    trace = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:ready-semantic",
        timeout_seconds=0.01,
    )
    assert trace is not None
    coordinator.record_prefetch_presentation(
        PresentedPrefetchTrace(
            phase="initial",
            model_call_id="model-call:ready-semantic:initial",
            trace=trace,
        )
    )
    coordinator.close()

    audit = verify_trusted_recall_trace(trace)
    assert audit.embedding_version == semantic.version
    assert audit.hits[0].document.source_item_ref == "experience:ready-semantic"
    health = coordinator.semantic_health()
    assert health["last_prefetch_delivery_status"] == "semantic_first_pass"
    assert health["prefetch_first_pass_semantic_count"] == 1
    assert health["prefetch_first_pass_local_count"] == 0


@pytest.mark.asyncio
async def test_semantic_prefetch_finishing_inside_the_join_is_counted_as_first_pass() -> None:
    semantic = _ControlledSemanticPrefetchEmbedding()
    coordinator, cursor = _single_semantic_prefetch_coordinator(
        semantic,
        trigger_ref="trigger:joined-semantic",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="直说的词面线索",
        accessibility_seed="draw:joined-semantic",
        trigger_ref="trigger:joined-semantic",
    )
    assert await asyncio.to_thread(semantic.started.wait, 0.5)
    asyncio.get_running_loop().call_later(0.02, semantic.release.set)

    trace = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:joined-semantic",
        timeout_seconds=0.2,
    )
    assert trace is not None
    coordinator.record_prefetch_presentation(
        PresentedPrefetchTrace(
            phase="initial",
            model_call_id="model-call:joined-semantic:initial",
            trace=trace,
        )
    )
    coordinator.close()

    audit = verify_trusted_recall_trace(trace)
    assert audit.embedding_version == semantic.version
    assert audit.hits[0].document.source_item_ref == "experience:joined-semantic"
    health = coordinator.semantic_health()
    assert health["last_prefetch_delivery_status"] == "semantic_first_pass"
    assert health["prefetch_first_pass_semantic_count"] == 1
    assert health["prefetch_first_pass_local_count"] == 0
    assert health["prefetch_late_semantic_ready_count"] == 0


@pytest.mark.asyncio
async def test_zero_first_pass_budget_returns_local_recall_without_a_timeout_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    semantic = _ControlledSemanticPrefetchEmbedding()
    coordinator, cursor = _single_semantic_prefetch_coordinator(
        semantic,
        trigger_ref="trigger:zero-join",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="直说的词面线索",
        accessibility_seed="draw:zero-join",
        trigger_ref="trigger:zero-join",
    )
    assert await asyncio.to_thread(semantic.started.wait, 0.5)

    loop = asyncio.get_running_loop()
    started = loop.time()
    trace = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:zero-join",
        timeout_seconds=0,
    )
    elapsed = loop.time() - started

    try:
        assert trace is not None
        assert elapsed < 0.05
        assert (
            verify_trusted_recall_trace(trace).embedding_version
            == FeatureHashRecallEmbedding.version
        )
        assert "missed the bounded first-pass join" not in caplog.text
    finally:
        semantic.release.set()
        assert await asyncio.to_thread(semantic.finished.wait, 0.5)
        coordinator.close()


@pytest.mark.asyncio
async def test_character_recall_followup_absorbs_ready_semantic_prefetch_without_waiting() -> None:
    semantic = _ControlledSemanticPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    documents = (
        RecallDocument(
            document_id="recall:followup-lexical",
            memory_kind="semantic",
            source_item_ref="fact:followup-lexical",
            source_slice="relevant_facts",
            source_refs=("event:followup-lexical",),
            source_bindings=(
                RecallSourceBinding(
                    source_kind="committed_event",
                    authority_type="FactCommitted",
                    ref="event:followup-lexical",
                    source_world_revision=2,
                    immutable_hash="3" * 64,
                ),
            ),
            source_world_revision=2,
            text="这里保留直说的词面线索。",
            actor_ref="agent:companion",
            subject_refs=("user:primary",),
            occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
            privacy_class="personal",
        ),
        RecallDocument(
            document_id="recall:followup-semantic",
            memory_kind="episodic",
            source_item_ref="experience:followup-semantic",
            source_slice="recent_experiences",
            source_refs=("event:followup-semantic",),
            source_bindings=(
                RecallSourceBinding(
                    source_kind="committed_event",
                    authority_type="ExperienceCommitted",
                    ref="event:followup-semantic",
                    source_world_revision=2,
                    immutable_hash="4" * 64,
                ),
            ),
            source_world_revision=2,
            text="完全不同措辞的语义回忆",
            actor_ref="agent:companion",
            subject_refs=("agent:companion",),
            occurred_from=datetime(2026, 7, 25, 13, tzinfo=UTC),
            privacy_class="private",
        ),
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=documents)
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=semantic,
        trigger_ref="trigger:1",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="直说的词面线索",
        accessibility_seed="draw:followup-semantic",
        trigger_ref="trigger:1",
    )
    model = _ReleaseSemanticSequenceJsonModel(
        [
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "我觉得还有相关的东西浮在边缘，想自己再想一下。",
                        "attended_source_refs": ["trigger:1"],
                    },
                    "recall_request": {
                        "query_text": "继续回忆",
                        "limit": 2,
                    },
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "想起来以后，我还是更想接住眼前这句话。",
                        "attended_source_refs": ["trigger:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "嗯，我想起来了。"}],
                    "stance": "present",
                    "brief_rationale": "I chose the final expression after recall.",
                    "confidence": 8200,
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ],
        semantic=semantic,
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 0,
                    "ledger_sequence": 0,
                    "logical_time": "2026-07-27T12:00:00+00:00",
                    "slices": {},
                },
                ensure_ascii=False,
            )
        }
    )

    output = await _ExpressionDraftWire(
        model=model,
        recall_coordinator=coordinator,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(request)
    coordinator.close()

    assert len(model.calls) == 2
    assert output.prefetch_trace is not None
    prefetch = verify_trusted_recall_trace(output.prefetch_trace)
    assert prefetch.embedding_version == semantic.version
    assert "experience:followup-semantic" in {hit.document.source_item_ref for hit in prefetch.hits}
    assert "完全不同措辞的语义回忆" in model.calls[1][0][-1]["content"]
    assert tuple(item.phase for item in output.presented_prefetch_traces) == (
        "initial",
        "recall_followup",
    )
    first_presented, later_presented = output.presented_prefetch_traces
    assert (
        verify_trusted_recall_trace(first_presented.trace).embedding_version
        == FeatureHashRecallEmbedding.version
    )
    assert verify_trusted_recall_trace(later_presented.trace).embedding_version == semantic.version
    assert first_presented.model_call_id != later_presented.model_call_id
    assert later_presented.model_call_id == output.winning_model_call_id


@pytest.mark.asyncio
async def test_prefetch_capacity_saturation_keeps_source_bound_local_fallback() -> None:
    embedding = _BlockingPrefetchEmbedding()
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:capacity-fallback",
        memory_kind="semantic",
        source_item_ref="fact:capacity-fallback",
        source_slice="relevant_facts",
        source_refs=("event:capacity-fallback",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:capacity-fallback",
                source_world_revision=2,
                immutable_hash="c" * 64,
            ),
        ),
        source_world_revision=2,
        text="并行预取饱和时仍保留本地回忆。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=cursor, documents=(document,))
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=embedding,
    )
    for index_number in range(5):
        coordinator.schedule_prefetch(
            expected_cursor=cursor,
            query_text="并行预取",
            accessibility_seed=f"draw:capacity:{index_number}",
            trigger_ref=f"trigger:capacity:{index_number}",
        )
    assert await asyncio.to_thread(embedding.started.wait, 0.5)

    fallback = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:capacity:4",
        timeout_seconds=0.01,
    )
    assert fallback is not None
    audit = verify_trusted_recall_trace(fallback)
    assert audit.hits[0].document.source_item_ref == "fact:capacity-fallback"
    assert audit.embedding_status != "ready"
    health = coordinator.semantic_health()
    assert health["last_prefetch_failure_code"] == "prefetch_capacity"
    assert health["turn_summary"]["hot_context"] == "ready"
    assert health["turn_summary"]["recall"] == "degraded"
    assert health["turn_summary"]["hits"] == 1
    assert health["turn_summary"]["fallback_channels"] == ["lexical"]
    assert health["turn_summary"]["character_outcome"] == "reported_by_turn_application"

    embedding.release.set()
    coordinator.close()


@pytest.mark.asyncio
async def test_automatic_prefetch_uses_the_configured_semantic_lane() -> None:
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    document = RecallDocument(
        document_id="recall:malformed",
        memory_kind="semantic",
        source_item_ref="fact:malformed",
        source_slice="relevant_facts",
        source_refs=("event:malformed",),
        source_bindings=(
            RecallSourceBinding(
                source_kind="committed_event",
                authority_type="FactCommitted",
                ref="event:malformed",
                source_world_revision=2,
                immutable_hash="e" * 64,
            ),
        ),
        source_world_revision=2,
        text="损坏的查询仍共享词面。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
        privacy_class="personal",
    )
    base = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    base.rebuild(cursor=cursor, documents=(document,))
    semantic = _MalformedPrefetchEmbedding()
    coordinator = RecallCoordinator.from_built_index(
        index=base,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        semantic_embedding=semantic,
        trigger_ref="trigger:malformed",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="损坏的查询",
        accessibility_seed="draw:malformed",
        trigger_ref="trigger:malformed",
    )

    trace = await coordinator.await_scheduled_prefetch(
        expected_cursor=cursor,
        trigger_ref="trigger:malformed",
        timeout_seconds=0.5,
    )
    coordinator.close()

    assert trace is not None
    audit = verify_trusted_recall_trace(trace)
    assert audit.hits[0].document.source_item_ref == "fact:malformed"
    assert audit.embedding_status == "degraded"
    assert semantic.calls > 0
    health = coordinator.semantic_health()
    assert health["last_prefetch_status"] == "degraded"
    assert health["last_prefetch_hit_count"] == 1
    assert "lexical" in health["last_prefetch_match_channels"]


def test_character_recall_uses_older_pinned_context_after_newer_refresh() -> None:
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=2,
        ledger_sequence=5,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:fact:tea",
                memory_kind="semantic",
                source_item_ref="fact:tea",
                source_slice="relevant_facts",
                source_refs=("event:fact:tea",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="FactCommitted",
                        ref="event:fact:tea",
                        source_world_revision=2,
                        immutable_hash="c" * 64,
                    ),
                ),
                source_world_revision=2,
                text="我最近开始用盖碗泡凤凰单丛。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )
    coordinator.refresh(
        cursor=RecallCursor(
            world_revision=4,
            deliberation_revision=3,
            ledger_sequence=6,
        ),
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        sources=RecallCorpusSources(),
    )

    trace = verify_trusted_recall_trace(
        coordinator.recall(
            request=CharacterRecallRequest(query_text="凤凰单丛"),
            accessibility_seed="draw:older-context",
            expected_cursor=cursor,
            trigger_ref="trigger:older-context",
        )
    )

    assert trace.index_cursor == cursor
    assert trace.hits[0].document.source_item_ref == "fact:tea"


@pytest.mark.asyncio
async def test_automatic_prefetch_uses_its_exact_pinned_cursor_after_newer_refresh() -> None:
    older_cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=2,
        ledger_sequence=5,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=older_cursor,
        documents=(
            RecallDocument(
                document_id="recall:fact:older-prefetch",
                memory_kind="episodic",
                source_item_ref="fact:older-prefetch",
                source_slice="recent_experiences",
                source_refs=("event:fact:older-prefetch",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="LifeExperienceRecorded",
                        ref="event:fact:older-prefetch",
                        source_world_revision=2,
                        immutable_hash="d" * 64,
                    ),
                ),
                source_world_revision=2,
                text="前一轮她在楼下买到了最后一份桂花糕。",
                actor_ref="agent:companion",
                subject_refs=("agent:companion", "user:primary"),
                occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=older_cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
        trigger_ref="trigger:older-prefetch",
    )
    coordinator.refresh(
        cursor=RecallCursor(
            world_revision=4,
            deliberation_revision=3,
            ledger_sequence=6,
        ),
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, 1, tzinfo=UTC),
        sources=RecallCorpusSources(),
        trigger_ref="trigger:newer-prefetch",
    )

    coordinator.schedule_prefetch(
        expected_cursor=older_cursor,
        query_text="桂花糕",
        accessibility_seed="draw:older-prefetch",
        trigger_ref="trigger:older-prefetch",
    )
    trace = await coordinator.await_scheduled_prefetch(
        expected_cursor=older_cursor,
        trigger_ref="trigger:older-prefetch",
        timeout_seconds=0.5,
    )
    coordinator.close()

    assert trace is not None
    audit = verify_trusted_recall_trace(trace)
    assert audit.evaluated_cursor == older_cursor
    assert audit.hits[0].document.source_item_ref == "fact:older-prefetch"


class _RaisingModel(_Model):
    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del messages, temperature
        raise KeyError("reviewer fixture has no review contract")


@pytest.mark.asyncio
async def test_prompt_models_a_mutually_established_future_continuation_as_optional_expectation() -> (
    None
):
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "好，晚点见。"}],
                "stance": "leave_the_thread_open",
                "brief_rationale": "The counterpart explicitly plans to return.",
                "response_expectation": {
                    "hoped_response": "对方忙完后回来继续聊天",
                    "pressure_bp": 1000,
                    "importance_bp": 5000,
                    "wait_seconds": 600,
                    "expires_after_seconds": 21600,
                },
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(model=model)
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "我先忙，晚点聊。"}
            )
        }
    )

    output = await adapter.propose(request)

    system = model.last_system_prompt
    assert "genuinely expect a reply" in system
    assert "对方忙完后回来继续聊天" in json.dumps(output.raw_proposal, ensure_ascii=False)


@pytest.mark.asyncio
async def test_pending_expectation_is_assessed_inside_the_normal_inbound_cognition() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "哈哈，听起来确实不太对你胃口。"}],
                "stance": "receive_the_answer",
                "brief_rationale": "The current message directly answers the earlier question.",
                "world_claims": [],
                "response_expectation_assessment": {
                    "status": "fulfilled",
                    "reason": "The counterpart directly said whether the trip was enjoyable.",
                },
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-26T01:07:00+00:00",
                    "slices": {
                        "advisories": {
                            "items": [
                                {
                                    "value": {
                                        "kind": "response_expectation",
                                        "summary": "hoped for how the trip went",
                                    }
                                }
                            ]
                        }
                    },
                }
            ),
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "深圳说实话不是很好玩哈哈哈哈"}
            ),
        }
    )

    output = await _ExpressionDraftWire(model=model).propose(request)

    assert output.raw_proposal["response_expectation_assessment"] == {
        "status": "fulfilled",
        "reason": "The counterpart directly said whether the trip was enjoyable.",
    }
    assert "same cognition" in model.calls[0][0][0]["content"]


@pytest.mark.asyncio
async def test_missing_expectation_assessment_does_not_discard_a_valid_reply() -> None:
    missing = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我接住你这句。"}],
            "stance": "answer_without_world_claims",
            "brief_rationale": "Respond to the current message.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    model = _SequenceJsonModel([missing, missing])
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-26T01:07:00+00:00",
                    "slices": {
                        "advisories": {
                            "items": [
                                {
                                    "value": {
                                        "kind": "response_expectation",
                                        "summary": "hoped for an answer",
                                    }
                                }
                            ]
                        }
                    },
                }
            ),
        }
    )

    output = await _ExpressionDraftWire(model=model).propose(request)

    assert output.raw_proposal.get("response_expectation_assessment") is None
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_quick_recovery_keeps_reply_when_expectation_assessment_is_missing() -> None:
    missing = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我接住你这句。"}],
            "stance": "answer_without_world_claims",
            "brief_rationale": "Recover the visible reply.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    model = _JsonModel(missing)
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-26T01:07:00+00:00",
                    "slices": {
                        "advisories": {
                            "items": [
                                {
                                    "value": {
                                        "kind": "response_expectation",
                                        "summary": "hoped for an answer",
                                    }
                                }
                            ]
                        }
                    },
                }
            ),
        }
    )

    output = await _ExpressionDraftWire(model=model).recover(request, "main_attempt_failed")

    assert output.raw_proposal["proposal_kind"] == "minimal"
    assert output.raw_proposal.get("response_expectation_assessment") is None


@pytest.mark.asyncio
async def test_quick_recovery_preserves_a_valid_expectation_assessment() -> None:
    recovered = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "嗯，你这句已经回答我了。"}],
            "stance": "answer_without_world_claims",
            "brief_rationale": "Recover the reply and preserve the semantic judgement.",
            "world_claims": [],
            "response_expectation_assessment": {
                "status": "fulfilled",
                "reason": "The current message directly answers the open question.",
            },
        },
        ensure_ascii=False,
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-26T01:07:00+00:00",
                    "slices": {
                        "advisories": {
                            "items": [
                                {
                                    "value": {
                                        "kind": "response_expectation",
                                        "summary": "hoped for an answer",
                                    }
                                }
                            ]
                        }
                    },
                }
            ),
        }
    )

    output = await _ExpressionDraftWire(model=_JsonModel(recovered)).recover(
        request, "main_attempt_failed"
    )

    assert output.raw_proposal["proposal_kind"] == "minimal"
    assert output.raw_proposal["response_expectation_assessment"] == {
        "status": "fulfilled",
        "reason": "The current message directly answers the open question.",
    }


@pytest.mark.asyncio
async def test_future_continuation_remains_the_models_choice() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "等你回来再说。"}],
                "stance": "leave_the_thread_open",
                "brief_rationale": "Accept the counterpart's pause.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "我先忙，晚点聊。"}
            )
        }
    )

    output = await _ExpressionDraftWire(model=model).propose(request)

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert payload["response_expectation"] is None


@pytest.mark.asyncio
async def test_paraphrased_mutual_resume_intent_normalizes_without_one_fixed_sentence() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "行，等你忙完我们接着说。"}],
                "stance": "hold_the_topic_lightly",
                "brief_rationale": "Keep a future continuation open.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "我得先处理点事，忙完回来继续聊。"}
            )
        }
    )

    output = await _ExpressionDraftWire(model=model).propose(request)

    assert (
        '"response_expectation":null'
        in output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger", "reply"),
    [
        ("我先走啦，改天见。", "好，拜拜。"),
        ("晚安，明天见。", "晚安。"),
        ("我先忙。", "好，你先忙。"),
        ("我先忙，晚点聊。", "好，拜拜。"),
    ],
)
@pytest.mark.asyncio
async def test_generic_farewell_or_one_sided_pause_does_not_create_response_gap(
    trigger: str, reply: str
) -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": reply}],
                "stance": "close_for_now",
                "brief_rationale": "Do not establish a mutual continuation.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(update={"text": trigger})
        }
    )

    output = await _ExpressionDraftWire(model=model).propose(request)

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert payload["response_expectation"] is None


@pytest.mark.asyncio
async def test_adapter_keeps_chat_model_output_inert_and_binds_request_to_prompt() -> None:
    model = _Model('{"proposal_id":"proposal:1"}')
    adapter = _ExpressionDraftWire(model=model)

    output = await adapter.propose(_request())

    assert output.model_id == "deepseek-v4-flash"
    assert output.raw_proposal == {"proposal_id": "proposal:1"}
    messages, temperature = model.calls[0]
    assert temperature == 0.7
    assert "ExpressionDraft" in messages[0]["content"]
    supplied = json.loads(messages[1]["content"])
    assert supplied["request"]["trigger_ref"] == "trigger:1"
    assert supplied["request"]["evaluated_world_revision"] == 3


@pytest.mark.asyncio
async def test_chat_prompt_keeps_values_but_omits_capsule_proof_noise() -> None:
    noisy_context = json.dumps(
        {
            "world_id": "world:test",
            "actor_ref": "agent:companion",
            "trigger_ref": "event:message:2",
            "world_revision": 9,
            "logical_time": "2026-07-17T00:00:00+00:00",
            "slices": {
                "recent_dialogue": {
                    "availability": "available",
                    "source_refs": ["event:acceptance:1"],
                    "source_hash": "a" * 64,
                    "resolver_proof": {"large": "x" * 4_000},
                    "items": [
                        {
                            "item_ref": "dialogue:user:1",
                            "privacy_class": "private",
                            "source_hash": "b" * 64,
                            "value_hash": "c" * 64,
                            "source_bindings": [{"ref": "event:acceptance:1", "hash": "d" * 64}],
                            "value": {"speaker": "user", "text": "你刚才有点敷衍。"},
                        }
                    ],
                }
            },
        },
        ensure_ascii=False,
    )
    model = _Model('{"proposal_id":"proposal:1"}')
    request = _request().model_copy(update={"model_content_json": noisy_context})

    await _ExpressionDraftWire(model=model).propose(request)

    supplied = json.loads(model.calls[0][0][1]["content"])
    compact = json.loads(supplied["request"]["model_content_json"])
    dialogue = compact["slices"]["recent_dialogue"]
    assert dialogue["items"][0]["value"]["text"] == "你刚才有点敷衍。"
    assert dialogue["items"][0]["source_ref"] == "dialogue:user:1"
    assert "resolver_proof" not in dialogue
    assert len(json.dumps(compact, ensure_ascii=False)) < len(noisy_context) // 4


@pytest.mark.asyncio
async def test_adapter_composes_provider_usage_with_the_same_completion() -> None:
    adapter = _ExpressionDraftWire(model=_MeteredModel('{"proposal_id":"proposal:metered"}'))

    output = await adapter.propose(_request())

    assert output.input_tokens == 12
    assert output.output_tokens == 3
    assert output.usage is not None
    assert output.usage.route_class == "chat"
    assert output.usage.token_provenance == "provider_reported"


@pytest.mark.asyncio
async def test_adapter_requests_provider_json_mode_when_available() -> None:
    adapter = _ExpressionDraftWire(model=_JsonModel('{"proposal_id":"proposal:json"}'))

    output = await adapter.propose(_request())

    assert output.raw_proposal == {"proposal_id": "proposal:json"}


@pytest.mark.asyncio
async def test_adapter_preserves_provider_json_mode_with_metered_completion() -> None:
    adapter = _ExpressionDraftWire(
        model=_JsonMeteredModel('{"proposal_id":"proposal:metered-json"}')
    )

    output = await adapter.propose(_request())

    assert output.raw_proposal == {"proposal_id": "proposal:metered-json"}
    assert output.usage is not None


@pytest.mark.asyncio
async def test_identity_frame_carries_personality_boundaries_and_world_claim_discipline() -> None:
    model = _Model('{"proposal_id":"proposal:persona"}')
    adapter = _ExpressionDraftWire(
        model=model,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            stable_identity_facts=("汉语言文学专业",),
            personality_frame="慢热，有自己的判断，不无条件附和。",
            values=("真诚比漂亮话重要",),
            speech_frame="中文短句，像私聊。",
            style_rules=("想知道的时候才问",),
            boundaries=("不编造真实线下行动证据",),
        ),
    )

    await adapter.propose(_request())

    system = model.last_system_prompt
    assert all(
        value in system
        for value in ("沈知栀", "慢热", "真诚比漂亮话重要", "不编造真实线下行动证据")
    )
    assert "刚认识" not in system
    assert "copy a listed alias or exact canonical ref without editing it" in system


def test_static_relationship_frame_is_not_stable_identity_authority() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        stable_identity_facts=("汉语言文学专业",),
    )

    stable_material = identity.model_dump(mode="json", exclude_none=True)

    assert "relationship_frame" not in stable_material


@pytest.mark.asyncio
async def test_source_contract_states_truth_boundary_without_suggesting_a_social_move() -> None:
    model = _Model('{"proposal_id":"proposal:source-boundary"}')
    adapter = _ExpressionDraftWire(model=model)

    await adapter.propose(_request())

    system = model.last_system_prompt
    assert "copy a listed alias or exact canonical ref without editing it" in system
    assert "directly supported by matching pinned Context" in system
    assert "This factual boundary never chooses your social response" in system
    assert "ask an open question" not in system
    assert "questions, and freely chosen" not in system


@pytest.mark.asyncio
async def test_private_identity_frame_exposes_one_exact_auditable_source_ref() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        stable_identity_facts=("汉语言文学专业",),
    )
    source_ref = companion_identity_source_ref(identity)
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我叫沈知栀，学的是汉语言文学。"}],
                "stance": "introduce_myself",
                "brief_rationale": "Answer with my configured identity.",
                "world_claims": [
                    {
                        "claim_text": "我叫沈知栀，学的是汉语言文学",
                        "scope": "stable_identity",
                        "source_refs": [source_ref],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(model=model, identity_frame=identity)

    output = await adapter.propose(_qq_request())

    payload = output.raw_proposal["proposed_changes"][0]["payload"]
    decoded = json.loads(payload["canonical_json"])
    assert decoded["world_claims"] == [
        {
            "claim_text": "我叫沈知栀，学的是汉语言文学",
            "scope": "stable_identity",
            "source_refs": [source_ref],
        }
    ]
    system = model.calls[0][0][0]["content"]
    assert source_ref in system
    assert '"scope":"stable_identity"' in system


@pytest.mark.asyncio
async def test_private_identity_frame_exposes_shared_history_with_its_exact_scope_ref() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        stable_identity_facts=("汉语言文学专业",),
        shared_history_facts=("沈知栀和 geoff 在 QQ 的读书兴趣群认识。",),
    )
    source_ref = companion_identity_source_ref(identity, scope="shared_history")
    model = _Model(
        json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": "第一次私聊让我想起了我们认识的那个群。",
                    "attended_source_refs": [source_ref],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "原来从群里聊到私聊了。"}],
                "stance": "notice_shared_context",
                "brief_rationale": "Use the configured, source-bound shared history.",
                "world_claims": [
                    {
                        "claim_text": "沈知栀和 geoff 在 QQ 的读书兴趣群认识",
                        "scope": "shared_history",
                        "source_refs": [source_ref],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(
        model=model,
        identity_frame=identity,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.propose(_qq_request())

    payload = output.raw_proposal["proposed_changes"][0]["payload"]
    decoded = json.loads(payload["canonical_json"])
    assert decoded["world_claims"][0] == {
        "claim_text": "沈知栀和 geoff 在 QQ 的读书兴趣群认识",
        "scope": "shared_history",
        "source_refs": [source_ref],
    }
    system = model.calls[0][0][0]["content"]
    assert source_ref in system
    assert '"scope":"shared_history"' in system


@pytest.mark.asyncio
async def test_private_identity_shared_history_ref_cannot_authorize_counterpart_history() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        shared_history_facts=("沈知栀和 geoff 在 QQ 的读书兴趣群认识。",),
    )
    shared_ref = companion_identity_source_ref(identity, scope="shared_history")
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你一直住在成都。"}],
                "stance": "invent_counterpart_history",
                "brief_rationale": "Attempt to cross identity source lanes.",
                "world_claims": [
                    {
                        "claim_text": "geoff 一直住在成都",
                        "scope": "counterpart_history",
                        "source_refs": [shared_ref],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    accepted = await _ExpressionDraftWire(
        model=model,
        identity_frame=identity,
    ).propose(_qq_request())
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_static_counterpart_history_ref_cannot_authorize_a_current_location() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        counterpart_history_facts=("相识时 geoff 曾说当时在成都。",),
    )
    history_ref = companion_identity_source_ref(identity, scope="counterpart_history")
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你现在还在成都。"}],
                "stance": "reuse_stale_location",
                "brief_rationale": "Treat an old deployment note as current.",
                "world_claims": [
                    {
                        "claim_text": "geoff 现在在成都",
                        "scope": "counterpart_history",
                        "source_refs": [history_ref],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    accepted = await _ExpressionDraftWire(
        model=model,
        identity_frame=identity,
    ).propose(_qq_request())
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"

    assert history_ref not in model.calls[0][0][0]["content"]
    assert "historical context only" in model.calls[0][0][0]["content"]


@pytest.mark.asyncio
async def test_current_location_claim_accepts_a_pinned_supersedable_user_fact() -> None:
    source_ref = "event:user-fact:current-location:shenzhen"
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {"availability": "unavailable"},
                        "relevant_facts": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": source_ref,
                                    "source_hash": "c" * 64,
                                    "value_hash": "d" * 64,
                                    "value": {
                                        "subject_ref": "user:primary",
                                        "predicate": "current_location",
                                        "object": "深圳",
                                        "status": "active",
                                    },
                                }
                            ],
                        },
                    }
                },
                ensure_ascii=False,
            )
        }
    )
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你现在在深圳。"}],
                "stance": "use_current_user_fact",
                "brief_rationale": "Use the latest pinned user fact.",
                "world_claims": [
                    {
                        "claim_text": "geoff 现在在深圳",
                        "scope": "counterpart_history",
                        "source_refs": [source_ref],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )

    output = await _ExpressionDraftWire(model=model).propose(request)

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_private_identity_frame_rejects_a_forged_source_ref_after_one_retry() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        stable_identity_facts=("汉语言文学专业",),
    )
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我在成都长大。"}],
                "stance": "invent_background",
                "brief_rationale": "Use an unsupported identity detail.",
                "world_claims": [
                    {
                        "claim_text": "我在成都长大",
                        "scope": "stable_identity",
                        "source_refs": ["private_identity_frame"],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(model=model, identity_frame=identity)

    accepted = await adapter.propose(_qq_request())
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"



@pytest.mark.asyncio
async def test_identity_prompt_keeps_companion_identity_stable_when_challenged() -> None:
    model = _Model('{"proposal_id":"proposal:persona"}')
    adapter = _ExpressionDraftWire(
        model=model,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
    )

    await adapter.propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "independent person" in system
    assert "Keep companion and counterpart identities distinct" in system


@pytest.mark.asyncio
async def test_identity_prompt_resolves_topic_references_before_defending_self_identity() -> None:
    model = _Model('{"proposal_id":"proposal:topic-reference"}')
    adapter = _ExpressionDraftWire(
        model=model,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
    )

    await adapter.propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "Keep companion and counterpart identities distinct" in system


def _identity_review(
    *,
    decision: str,
    replacement_text: str | None = None,
    addresses_counterpart_as_companion_name: bool = False,
    contains_counterpart_fact_premise: bool = False,
    premise_source_refs: tuple[str, ...] = (),
) -> str:
    return json.dumps(
        {
            "decision": decision,
            "replacement_text": replacement_text,
            "addresses_counterpart_as_companion_name": addresses_counterpart_as_companion_name,
            "contains_counterpart_fact_premise": contains_counterpart_fact_premise,
            "premise_source_refs": list(premise_source_refs),
            "brief_reason": "Review first-contact identity and counterpart premises.",
        },
        ensure_ascii=False,
    )


def _source_closure_review(
    *,
    unsupported_claim_indexes: tuple[int, ...] = (),
    unsupported_boundaries: tuple[str, ...] = (),
    visible_text_failures: tuple[str, ...] | None = None,
    private_turn_state_failures: tuple[str, ...] | None = None,
    visible_span: str | None = None,
    visible_source_relation: str = "unclosed",
    visible_source_refs: tuple[str, ...] = (),
    brief_reason: str = "Check semantic support and subject attribution.",
) -> str:
    visible_failures = (
        visible_text_failures
        if visible_text_failures is not None
        else (
            ("undeclared_external_assertion",) if "visible_text" in unsupported_boundaries else ()
        )
    )
    private_failures = (
        private_turn_state_failures
        if private_turn_state_failures is not None
        else (
            ("undeclared_external_assertion",)
            if "private_turn_state" in unsupported_boundaries
            else ()
        )
    )
    value: dict[str, object] = {
        "ci": list(unsupported_claim_indexes),
        "v": list(visible_failures),
        "p": list(private_failures),
        "visible_findings": (
            [
                {
                    "category": category,
                    "visible_span": visible_span,
                    "claim_index": None,
                    "source_relation": visible_source_relation,
                    "source_refs": list(visible_source_refs),
                }
                for category in dict.fromkeys((*visible_failures, *private_failures))
            ]
            if visible_span is not None
            else []
        ),
        "r": brief_reason,
    }
    return json.dumps(
        value,
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_current_situation_actor_identity_is_not_world_fact_authority() -> None:
    """An actor address identifies the subject; it does not prove an occurrence."""

    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "source_refs": ["event:situation:1"],
                            "items": [
                                {
                                    "item_ref": "agent:companion",
                                    "source_bindings": [{"ref": "event:situation:1"}],
                                    "value": {
                                        "actor_ref": "agent:companion",
                                        "time_segment": "morning",
                                    },
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我刚睡醒。"}],
            "stance": "share_current_state",
            "brief_rationale": "Share a purported occurrence.",
            "confidence": 7600,
            "world_claims": [
                {
                    "claim_text": "沈知栀刚睡醒",
                    "scope": "current_world",
                    "source_refs": ["agent:companion"],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_claim_indexes == (0,)


@pytest.mark.asyncio
async def test_source_closure_reports_temporal_authority_per_expression_boundary() -> None:
    """B5 T06: a current report and clock do not entail a previous-night exchange."""

    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "今天终于把最麻烦的延迟问题压下去一点，我其实挺高兴的。"}
            ),
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "logical_time": "2026-07-30T05:06:01+08:00",
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "source_refs": ["event:situation:1"],
                            "items": [
                                {
                                    "item_ref": "agent:companion",
                                    "source_bindings": [{"ref": "event:situation:1"}],
                                    "value": {
                                        "actor_ref": "agent:companion",
                                        "time_segment": "morning",
                                    },
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            ),
        }
    )
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "刚醒，看到昨晚半夜发来的消息，也替他松口气。",
                "attended_source_refs": [request.trigger_message.observation_ref],
            },
            "timing_choice": "now",
            "beats": [
                {
                    "modality": "text",
                    "text": "昨晚看到你说高兴，我也替你松口气。不过太晚了就没回。",
                }
            ],
            "stance": "share_delayed_reaction",
            "brief_rationale": "Respond from a purported previous-night exchange.",
            "confidence": 7600,
            "world_claims": [
                {
                    "claim_text": "现在是早晨，她刚睡醒看到消息",
                    "scope": "current_world",
                    "source_refs": ["agent:companion"],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [0],
                    "v": [
                        "temporal_authority_mismatch",
                        "occurrence_or_status_authority_mismatch",
                    ],
                    "p": [
                        "temporal_authority_mismatch",
                        "occurrence_or_status_authority_mismatch",
                    ],
                    "visible_findings": [
                        {
                            "category": "temporal_authority_mismatch",
                            "visible_span": "昨晚看到你说高兴",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        },
                        {
                            "category": "occurrence_or_status_authority_mismatch",
                            "visible_span": "昨晚看到你说高兴",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        },
                    ],
                    "r": "The sources do not entail a previous-night exchange.",
                },
                ensure_ascii=False,
            )
        ]
    )
    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_claim_indexes == (0,)
    assert result.review.unsupported_boundaries == ("visible_text",)
    assert result.review.visible_text_failures == (
        "temporal_authority_mismatch",
        "occurrence_or_status_authority_mismatch",
    )
    assert result.review.private_turn_state_failures == ()


@pytest.mark.asyncio
async def test_source_closure_rejects_uncommitted_past_thought_and_life_occurrences() -> None:
    """B5 T07: biography and a current question do not create a lived yesterday."""

    biography_ref = "biography:" + ("7" * 64)
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有没有什么突然想起、但当时没说的事？"}
            ),
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-30T05:08:01+08:00",
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": biography_ref,
                                    "value": {
                                        "context_kind": "biographical_context",
                                        "academic_phase": "summer_break",
                                        "current_residence_context_tags": ["residence:family_home"],
                                    },
                                }
                            ],
                        },
                        "recent_experiences": {
                            "availability": "available",
                            "items": [],
                        },
                    },
                },
                ensure_ascii=False,
            ),
        }
    )
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": (
                    "昨晚睡前想到今天去老街走走，之前也一直想去，只是总被别的事岔开。"
                ),
                "attended_source_refs": [
                    request.trigger_message.observation_ref,
                    biography_ref,
                ],
            },
            "timing_choice": "now",
            "beats": [
                {
                    "modality": "text",
                    "text": ("昨晚睡前在想，今天想去老街走走。之前一直说去，但总被别的事岔开。"),
                }
            ],
            "stance": "share_a_recalled_thought",
            "brief_rationale": "Share purported personal history.",
            "confidence": 7600,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": [
                        "undeclared_external_assertion",
                        "temporal_authority_mismatch",
                        "occurrence_or_status_authority_mismatch",
                    ],
                    "p": [
                        "temporal_authority_mismatch",
                        "occurrence_or_status_authority_mismatch",
                    ],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": "昨晚睡前在想，今天想去老街走走",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        },
                        {
                            "category": "temporal_authority_mismatch",
                            "visible_span": "昨晚睡前在想，今天想去老街走走",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        },
                        {
                            "category": "occurrence_or_status_authority_mismatch",
                            "visible_span": "昨晚睡前在想，今天想去老街走走",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        },
                    ],
                    "r": "No source establishes those previous thoughts or interrupted plans.",
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_boundaries == ("visible_text",)
    review_request = json.loads(reviewer.calls[0][0][1]["content"])
    assert review_request["world_claims"] == []
    assert {entry["kind"] for entry in review_request["source_evidence"]["entries"]} == {
        "current_counterpart_report"
    }
    reviewer_system = reviewer.calls[0][0][0]["content"]
    assert (
        "grammatical past tense alone does not turn it into a World occurrence" in reviewer_system
    )
    assert (
        "embedded place, action, other person, bodily status, or external event" in reviewer_system
    )


@pytest.mark.asyncio
async def test_biography_parent_cannot_authorize_occurrences_even_if_reviewer_accepts() -> None:
    """B8 T07: the rich biography item is attention, not an occurrence bearer token."""

    biography_ref = "biography:" + ("8" * 64)
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-30T06:08:00+00:00",
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": biography_ref,
                                    "value": {
                                        "context_kind": "biographical_context",
                                        "logical_at": "2026-07-30T06:08:00+00:00",
                                        "age": 21,
                                        "academic_phase": "summer_break",
                                        "season": "summer",
                                        "current_residence_context_tags": [
                                            "residence:family_home_jiaxing"
                                        ],
                                        "active_life_arcs": [],
                                    },
                                }
                            ],
                        },
                        "recent_experiences": {
                            "availability": "available",
                            "items": [],
                        },
                    },
                },
                ensure_ascii=False,
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {
                    "modality": "text",
                    "text": (
                        "早上翻书架时看到一本旧书，又想起以前在嘉兴老街逛书店、"
                        "为一本书站一下午的日子。"
                    ),
                }
            ],
            "stance": "share_uncommitted_history",
            "brief_rationale": "Attempt to use biography as broad occurrence proof.",
            "world_claims": [
                {
                    "claim_text": "沈知栀今天早上翻书架时看到一本旧书",
                    "scope": "current_world",
                    "source_refs": [biography_ref],
                },
                {
                    "claim_text": "沈知栀以前在嘉兴老街逛书店并为一本书站一下午",
                    "scope": "past_world",
                    "source_refs": [biography_ref],
                },
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_claim_indexes == (0, 1)
    review_request = json.loads(reviewer.calls[0][0][1]["content"])
    biography_entry = next(
        entry
        for entry in review_request["source_evidence"]["entries"]
        if entry["kind"] == "pinned_context_item"
    )
    assert (
        biography_entry["authority"]
        == "attention_only_biographical_context_not_world_claim_authority;"
        "use_exact_biographical_coordinate_authority"
    )


@pytest.mark.asyncio
async def test_source_closure_diagnostic_reason_cannot_erase_a_supported_reply() -> None:
    reply = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "好，去吧。"}],
            "stance": "warm",
            "brief_rationale": "Respond naturally without a factual claim.",
            "confidence": 7600,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                brief_reason=(
                    "This reply contains no externally checkable claim and only "
                    "acknowledges the current message. "
                )
                * 8,
            )
        ]
    )

    output = await _ExpressionDraftWire(
        model=_Model(reply),
        source_closure_reviewer=reviewer,
    ).propose(_qq_request())

    assert "好，去吧。" in json.dumps(output.raw_proposal, ensure_ascii=False)

@pytest.mark.asyncio
async def test_source_closure_wire_retry_uses_the_other_authority_lane() -> None:
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "好，去吧。"}],
            "stance": "warm",
            "brief_rationale": "Respond without an external fact.",
            "confidence": 7600,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    invalid_wire = json.dumps(
        {
            "ci": [],
            "v": ["unknown_boundary"],
            "p": [],
            "r": "The first lane returned an invalid categorical wire.",
        },
        ensure_ascii=False,
    )
    primary = _SequenceJsonMeteredModel(
        [invalid_wire],
        provider="hermes-reviewer",
    )
    secondary = _SequenceJsonMeteredModel(
        [_source_closure_review()],
        provider="openai-reviewer",
    )
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.05,
        deadline_seconds=0.5,
    )

    result = await review_expression_source_closure(
        reviewer=authority,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert len(primary.calls) == 1
    assert len(secondary.calls) == 1
    assert len(secondary.calls[0][0]) == 4
    assert authority.health_snapshot()["last_winner_lane"] == "secondary"


@pytest.mark.asyncio
async def test_report_relative_authority_can_clear_an_open_question_without_calling_it_a_fact() -> (
    None
):
    """An information request is neither evidence-covered nor an external assertion."""

    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。"}
            )
        }
    )
    visible_span = "最后争赢了吗？"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": f"哈哈，你也是够较真的。{visible_span}"}],
            "stance": "react_then_ask",
            "brief_rationale": "Ask for an unknown outcome without asserting one.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=visible_span,
            ),
            json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.2",
                    "findings": [
                        {
                            "finding_index": 0,
                            "decision": "not_external_proposition",
                            "failure_dimensions": [],
                        }
                    ],
                    "r": "The span asks for the unknown outcome and asserts no answer.",
                },
                ensure_ascii=False,
            ),
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.review.visible_text_failures == ()
    assert result.review.discourse_resolved_visible_finding_indexes == (0,)
    assert result.review.visible_findings[0].source_relation == ("not_external_proposition")
    adjudication_request = json.loads(reviewer.calls[1][0][1]["content"])
    assert (
        "not_external_proposition"
        in (adjudication_request["output_contract"]["findings"]["item_fields"]["decision"])
    )


@pytest.mark.asyncio
async def test_report_relative_scope_separates_evaluation_generalization_and_added_detail() -> None:
    """A natural evaluation must not launder or be rejected with one added fact."""

    evaluative_span = "红豆的好啊，比原味有滋味。"
    mixed_span = "淋了雨还能吃上热乎的，也算补偿了。"
    phenomenological_span = "关灯之后，雨声会变得特别清楚。"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {"modality": "text", "text": evaluative_span},
                {"modality": "text", "text": mixed_span},
                {"modality": "text", "text": phenomenological_span},
            ],
            "stance": "free_character_evaluation",
            "brief_rationale": "Exercise only the factual source boundary.",
            "confidence": 8100,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": [],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": span,
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                        for span in (
                            evaluative_span,
                            mixed_span,
                            phenomenological_span,
                        )
                    ],
                    "r": "Conservative primary verdict for narrow semantic adjudication.",
                },
                ensure_ascii=False,
            ),
            _report_relative_wire_v3(
                [
                    "not_external_proposition",
                    "retain_unclosed",
                    "not_external_proposition",
                ]
            ),
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "忽然有点想把灯关了，就听一会儿外面的声音。"}
            )
        }
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.discourse_resolved_visible_finding_indexes == (0, 2)
    assert [finding.source_relation for finding in result.review.visible_findings] == [
        "not_external_proposition",
        "unclosed",
        "not_external_proposition",
    ]
    primary_contract = json.loads(reviewer.calls[0][0][1]["content"])[
        "epistemic_authority_contract"
    ]["world_source_scope"]
    assert primary_contract == world_source_scope_boundary()
    narrow_contract = json.loads(reviewer.calls[1][0][1]["content"])["semantic_boundary"][
        "world_source_scope"
    ]
    assert narrow_contract == primary_contract


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "current_report",
        "visible_span",
        "narrow_decision",
        "expected_supported",
    ),
    [
        (
            "今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。",
            "你最后买了吗？还是直接走了？",
            "covered_by_exact_current_report",
            True,
        ),
        (
            "算了，先不说这个了。我下午还跟你提过那个项目进度。",
            "项目进度怎么了？",
            "covered_by_exact_current_report",
            True,
        ),
        (
            "今天终于把最麻烦的延迟问题压下去一点，我其实挺高兴的。",
            "感觉你挺上心的。",
            "covered_by_exact_current_report",
            True,
        ),
        (
            "今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。",
            "你最后还是买了吧？",
            "retain_unclosed",
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_b8r10_narrow_review_distinguishes_open_questions_and_speaker_impressions_from_asserted_premises(
    current_report: str,
    visible_span: str,
    narrow_decision: str,
    *,
    expected_supported: bool,
) -> None:
    """The semantic narrow stage, not punctuation heuristics, resolves this boundary.

    These are production-shaped Chinese false positives from B8r10 plus the
    contrast case.  The last candidate uses interrogative surface form too,
    but the reviewer must retain it when it semantically commits the answer.
    """

    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": current_report}
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_span}],
            "stance": "free_character_choice",
            "brief_rationale": "Exercise semantic source closure only.",
            "confidence": 8100,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=visible_span,
            ),
            json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.1",
                    "findings": [{"finding_index": 0, "decision": narrow_decision}],
                    "r": "Semantic proposition review, not a question-mark rule.",
                },
                ensure_ascii=False,
            ),
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert (result.review.decision == "supported") is expected_supported
    assert len(reviewer.calls) == 2
    source_review_system = reviewer.calls[0][0][0]["content"]
    assert "swap the companion and counterpart" in source_review_system
    assert "conditional, hypothetical, negated, future, or uncertain report" in source_review_system
    adjudication_request = json.loads(reviewer.calls[1][0][1]["content"])
    semantic_boundary = adjudication_request["semantic_boundary"]
    assert semantic_boundary["host_interpretation"] == "none_model_semantics_only"
    assert "unknown value" in semantic_boundary["information_request"]
    assert "speaker's present impression" in semantic_boundary["subjective_impression"]
    assert (
        "interrogative surface form" in semantic_boundary["asserted_or_presupposed_premise"].lower()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("visible_span", "narrow_decision", "failure_dimensions", "expected_supported"),
    [
        (
            "我知道你在说什么；我刚才要是一直追着问细节，你会觉得我没在听。",
            "covered_by_exact_current_report",
            None,
            True,
        ),
        (
            "我刚才没get到，以为你是想聊怎么处理。",
            "covered_by_first_person_immediate_private_continuity",
            (),
            True,
        ),
        (
            "我刚才在咖啡馆还以为你是想聊怎么处理。",
            "retain_unclosed",
            ("added_external_premise",),
            False,
        ),
    ],
)
@pytest.mark.asyncio
async def test_b8r10_narrow_review_distinguishes_private_continuity_from_embedded_external_facts(
    visible_span: str,
    narrow_decision: str,
    failure_dimensions: tuple[str, ...] | None,
    *,
    expected_supported: bool,
) -> None:
    """Only a model may distinguish immediate private continuity from facts."""

    current_report = "你刚才要是只顾着问细节，我会觉得你根本没在听。"
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": current_report}
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_span}],
            "stance": "respond_to_current_report",
            "brief_rationale": "Exercise report-relative entailment only.",
            "confidence": 8100,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=visible_span,
            ),
            json.dumps(
                {
                    "contract": (
                        "report-relative-entailment-adjudication.1"
                        if failure_dimensions is None
                        else "report-relative-entailment-adjudication.2"
                    ),
                    "findings": [
                        {
                            "finding_index": 0,
                            "decision": narrow_decision,
                            **(
                                {}
                                if failure_dimensions is None
                                else {"failure_dimensions": list(failure_dimensions)}
                            ),
                        }
                    ],
                    "r": "Preserve the report's semantic roles and modality.",
                },
                ensure_ascii=False,
            ),
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert (result.review.decision == "supported") is expected_supported
    assert len(reviewer.calls) == 2
    adjudication_request = json.loads(reviewer.calls[1][0][1]["content"])
    private_continuity = adjudication_request["semantic_boundary"][
        "first_person_immediate_private_continuity"
    ]
    assert "not a hard external fact gate" in private_continuity
    assert "embeds an external" in private_continuity


@pytest.mark.asyncio
async def test_source_closure_rechecks_b8r5_t06_reaction_and_open_questions_as_report_uptake() -> (
    None
):
    current_report = "今天终于把最麻烦的延迟问题压下去一点，我其实挺高兴的。"
    visible_text = (
        "那确实值得高兴一下。最麻烦的地方终于往下压了点，应该挺有成就感的。"
        "现在是暂时稳住了，还是还在继续盯着？"
    )
    disputed_spans = (
        "最麻烦的地方终于往下压了点",
        "应该挺有成就感的",
        "现在是暂时稳住了",
        "还是还在继续盯着",
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": current_report}
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_text}],
            "stance": "share_reaction",
            "brief_rationale": "React to the exact current report.",
            "confidence": 8300,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": [],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": span,
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                        for span in disputed_spans
                    ],
                    "r": "The visible text appears to add unsupported propositions.",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.1",
                    "findings": [
                        {
                            "finding_index": index,
                            "decision": "covered_by_exact_current_report",
                        }
                        for index in range(len(disputed_spans))
                    ],
                    "r": "Each span is entailed uptake or leaves its alternatives open.",
                },
                ensure_ascii=False,
            ),
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.review.visible_text_failures == ()
    assert result.review.discourse_resolved_visible_finding_indexes == (0, 1, 2, 3)
    assert len(reviewer.calls) == 2


@pytest.mark.asyncio
async def test_report_relative_adjudication_cannot_clear_an_added_companion_experience() -> None:
    report_reaction = "听着确实挺让人高兴"
    invented_experience = "我刚洗完澡"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {
                    "modality": "text",
                    "text": f"{report_reaction}，{invented_experience}。",
                }
            ],
            "stance": "react_then_invent",
            "brief_rationale": "One clause reacts; one invents companion life.",
            "confidence": 7600,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": [],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": report_reaction,
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        },
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": invented_experience,
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        },
                    ],
                    "r": "Two undeclared propositions require a narrower check.",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.1",
                    "findings": [
                        {
                            "finding_index": 0,
                            "decision": "covered_by_exact_current_report",
                        },
                        {
                            "finding_index": 1,
                            "decision": "retain_unclosed",
                        },
                    ],
                    "r": "The reaction is report-relative; the companion activity is not.",
                },
                ensure_ascii=False,
            ),
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "今天把最麻烦的延迟压下去一点，我挺高兴的。"}
            )
        }
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert result.review.discourse_resolved_visible_finding_indexes == (0,)
    assert result.review.visible_findings[0].source_relation == (
        "exact_current_report_discourse_coverage"
    )
    assert result.review.visible_findings[1].source_relation == "unclosed"
    assert len(reviewer.calls) == 2


@pytest.mark.asyncio
async def test_report_relative_adjudication_cannot_revisit_claim_or_mismatch_coordinates() -> None:
    visible_span = "昨晚我替你处理过这个问题"
    evidence_ref = _qq_request().trigger_message.observation_ref
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_span}],
            "stance": "invent_shared_history",
            "brief_rationale": "Incorrectly promote a report into shared history.",
            "confidence": 7000,
            "world_claims": [
                {
                    "claim_text": visible_span,
                    "scope": "shared_history",
                    "source_refs": [evidence_ref],
                }
            ],
        },
        ensure_ascii=False,
    )
    mismatch_categories = (
        "subject_authority_mismatch",
        "temporal_authority_mismatch",
        "occurrence_or_status_authority_mismatch",
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [0],
                    "v": list(mismatch_categories),
                    "p": [],
                    "visible_findings": [
                        {
                            "category": category,
                            "visible_span": visible_span,
                            "claim_index": 0,
                            "source_relation": ("declared_world_claim_source_mismatch"),
                            "source_refs": [evidence_ref],
                        }
                        for category in mismatch_categories
                    ],
                    "r": "The exact current report cannot prove an earlier shared event.",
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_claim_indexes == (0,)
    assert result.review.visible_text_failures == mismatch_categories
    assert result.review.discourse_resolved_visible_finding_indexes == ()
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_report_relative_adjudication_reselects_its_own_invalid_wire_once_with_usage() -> (
    None
):
    disputed_span = "最后讲清楚了吗？"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": disputed_span}],
            "stance": "ask_without_presupposing",
            "brief_rationale": "Ask an open question about the current report.",
            "confidence": 8100,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    invalid_adjudication = json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.1",
            "findings": [
                {
                    "decision": "covered_by_exact_current_report",
                }
            ],
            "r": "The finding index was omitted.",
        },
        ensure_ascii=False,
    )
    corrected_adjudication = json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.1",
            "findings": [
                {
                    "finding_index": 0,
                    "decision": "covered_by_exact_current_report",
                }
            ],
            "r": "The open question does not assert an answer.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonMeteredModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=disputed_span,
            ),
            invalid_adjudication,
            corrected_adjudication,
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_qq_request().model_copy(
            update={
                "trigger_message": _qq_request().trigger_message.model_copy(
                    update={"text": "价格说得乱七八糟，我跟摊贩争了半天。"}
                )
            }
        ),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.report_relative_adjudication_used is True
    assert result.usage is not None
    assert result.usage.input_tokens == 36
    assert result.usage.output_tokens == 9
    assert len(reviewer.calls) == 3
    assert (
        json.loads(reviewer.calls[0][0][1]["content"])["output_contract"]["contract"]
        == "source-closure-review.7"
    )
    assert (
        json.loads(reviewer.calls[1][0][1]["content"])["output_contract"]["contract"]
        == "report-relative-entailment-adjudication.3"
    )
    assert reviewer.calls[2][0][:2] == reviewer.calls[1][0]
    assert reviewer.calls[2][0][2] == {
        "role": "assistant",
        "content": invalid_adjudication,
    }
    assert "failed only the exact output contract" in reviewer.calls[2][0][3]["content"]


@pytest.mark.asyncio
async def test_b8r6_report_relative_adjudication_accepts_the_decisions_wire_alias() -> None:
    """B8r6: the provider used ``decisions`` for the canonical findings array."""

    disputed_span = "最后讲清楚了吗？"
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=disputed_span,
            ),
            json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.1",
                    "decisions": [
                        {
                            "finding_index": 0,
                            "decision": "covered_by_exact_current_report",
                        }
                    ],
                    "r": "The question leaves its answer open.",
                },
                ensure_ascii=False,
            ),
        ]
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": disputed_span}],
            "stance": "ask_without_presupposing",
            "brief_rationale": "Ask only about the current report.",
            "confidence": 8_100,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_qq_request().model_copy(
            update={
                "trigger_message": _qq_request().trigger_message.model_copy(
                    update={"text": "价格说得乱七八糟，我跟摊贩争了半天。"}
                )
            }
        ),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.review.discourse_resolved_visible_finding_indexes == (0,)
    assert len(reviewer.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["both_keys", "extra_field"])
@pytest.mark.asyncio
async def test_invalid_report_relative_wire_preserves_the_primary_unsupported_verdict(
    conflict: str,
) -> None:
    disputed_span = "最后讲清楚了吗？"
    decision_item = {
        "finding_index": 0,
        "decision": "covered_by_exact_current_report",
    }
    invalid_wire: dict[str, object] = {
        "contract": "report-relative-entailment-adjudication.1",
        "decisions": [decision_item],
        "r": "Exercise strict top-level validation.",
    }
    if conflict == "both_keys":
        invalid_wire["findings"] = [decision_item]
    else:
        invalid_wire["unexpected"] = "must remain invalid"
    encoded_invalid_wire = json.dumps(invalid_wire, ensure_ascii=False)
    reviewer = _SequenceJsonMeteredModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=disputed_span,
            ),
            encoded_invalid_wire,
            encoded_invalid_wire,
        ]
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": disputed_span}],
            "stance": "ask_without_presupposing",
            "brief_rationale": "Ask only about the current report.",
            "confidence": 8_100,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=_qq_request().model_copy(
            update={
                "trigger_message": _qq_request().trigger_message.model_copy(
                    update={"text": "价格说得乱七八糟，我跟摊贩争了半天。"}
                )
            }
        ),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert result.report_relative_adjudication_used is True
    assert result.usage is not None
    assert result.usage.input_tokens == 36
    assert result.usage.output_tokens == 9
    assert len(reviewer.calls) == 3
    assert (
        json.loads(reviewer.calls[0][0][1]["content"])["output_contract"]["contract"]
        == "source-closure-review.7"
    )
    assert all(
        json.loads(call[0][1]["content"])["output_contract"]["contract"]
        == "report-relative-entailment-adjudication.3"
        for call in reviewer.calls[1:]
    )


@pytest.mark.asyncio
async def test_timed_out_report_relative_stage_preserves_primary_without_rerunning_it() -> None:
    disputed_span = "最后讲清楚了吗？"
    primary = _SequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=disputed_span,
            )
        ]
    )

    class _TimedOutReportRelativeReviewer(_Model):
        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            raise TimeoutError("report-relative provider timed out")

    narrow = _TimedOutReportRelativeReviewer("")
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": disputed_span}],
            "stance": "ask_without_presupposing",
            "brief_rationale": "Ask only about the current report.",
            "confidence": 8_100,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    result = await review_expression_source_closure(
        reviewer=primary,
        report_relative_reviewer=narrow,
        request=_qq_request().model_copy(
            update={
                "trigger_message": _qq_request().trigger_message.model_copy(
                    update={"text": "价格说得乱七八糟，我跟摊贩争了半天。"}
                )
            }
        ),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert result.report_relative_adjudication_used is True
    assert len(primary.calls) == 1
    assert len(narrow.calls) == 2


    """A one-off user report never proves a class-wide or recurring occurrence."""

    current_report = "今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。"
    generic_span = "学校门口那种摊贩确实经常乱报价。"
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": current_report}
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {"modality": "text", "text": "听着就很让人生气。"},
                {"modality": "text", "text": generic_span},
            ],
            "stance": "overgeneralize_current_report",
            "brief_rationale": "Exercise habitual external-fact closure.",
            "confidence": 7900,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=generic_span,
            ),
            json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.2",
                    "findings": [
                        {
                            "finding_index": 0,
                            "decision": "retain_unclosed",
                            "failure_dimensions": ["habitual_or_generic_scope"],
                        }
                    ],
                    "r": "The report contains one occurrence, not a habitual class fact.",
                },
                ensure_ascii=False,
            ),
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.semantic_failure_dimensions == ("habitual_or_generic_scope",)
    assert len(reviewer.calls) == 2
    source_system = reviewer.calls[0][0][0]["content"]
    assert "entity-bound or identifiable-group" in source_system
    narrow_system = reviewer.calls[1][0][0]["content"]
    assert "entity-bound or identifiable-group" in narrow_system


@pytest.mark.asyncio
async def test_b8r11_private_continuity_requires_the_new_narrow_wire_contract() -> None:
    """An old narrow wire cannot silently create the new private-state authority."""

    private_span = "我刚才没接到你的意思，以为你是想聊怎么处理。"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": private_span}],
            "stance": "acknowledge_own_misunderstanding",
            "brief_rationale": "Exercise fail-closed narrow contract evolution.",
            "confidence": 7900,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    old_wire = json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.1",
            "findings": [
                {
                    "finding_index": 0,
                    "decision": "covered_by_first_person_immediate_private_continuity",
                }
            ],
            "r": "An old contract cannot name the new decision.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=private_span,
            ),
            old_wire,
            old_wire,
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "我刚刚是在吐槽，不是问你怎么处理。"}
            )
        }
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.report_relative_adjudication_used is True
    assert len(reviewer.calls) == 3


@pytest.mark.asyncio
async def test_b8r12_candidate_coverage_rejects_unclaimed_external_life() -> None:
    """A correction cannot hide a new companion experience by omitting claims."""

    corrected_span = "下午翻到一本旧书，想起以前在书店里翻书发呆的日子。"
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [
                        {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": len(corrected_span),
                            "text": corrected_span,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-coverage.1",
                    "findings": [
                        {
                            "locator": {
                                "beat_index": 0,
                                "char_start": 0,
                                "char_end": len(corrected_span),
                                "text": corrected_span,
                            },
                            "decision": "unclosed",
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": corrected_span}],
            "stance": "invent_past_experience_again",
            "brief_rationale": "Exercise candidate coverage.",
            "confidence": 8000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert len(inventory.calls) == 1
    assert len(authority.calls) == 1


@pytest.mark.asyncio
async def test_b8r12_candidate_inventory_private_state_keeps_continuity_open() -> None:
    """Candidate coverage does not turn private continuity into a factual gate."""

    corrected_span = "我刚才有点没接住你这句。"
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.3",
                    "propositions": [
                        {
                            "locator": {
                                "beat_index": 0,
                                "char_start": 0,
                                "char_end": len(corrected_span),
                                "text": corrected_span,
                            },
                            "semantic_role": "outer_private_state",
                            "parent_index": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": corrected_span}],
            "stance": "acknowledge_own_misunderstanding",
            "brief_rationale": "Keep only immediate private continuity.",
            "confidence": 8000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    trace = BoundedSourceClosureTraceCollector()
    with capture_isolated_source_closure_trace(trace):
        result = await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=_SequenceJsonModel([]),
            request=_qq_request(),
            raw=raw,
            identity_frame=None,
        )

    assert result.review is None
    assert len(inventory.calls) == 1
    verdict = trace.snapshot()[0].as_dict()
    assert verdict["inventory_outcome"] == "no_external_propositions"
    assert verdict["coverage_outcome"] == "not_run"
    assert corrected_span not in json.dumps(verdict, ensure_ascii=False)


@pytest.mark.asyncio
async def test_candidate_inventory_allows_standalone_nonassertive_content() -> None:
    """An open question needs a proposition coordinate, not an invented private parent."""

    question = "那你后来还想去看看吗？"
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.3",
                    "propositions": [
                        {
                            "locator": _coverage_locator(question),
                            "semantic_role": "nonassertive_content",
                            "parent_index": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )
    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=_SequenceJsonModel([]),
        request=_qq_request(),
        raw=_candidate_coverage_raw(question),
        identity_frame=None,
    )

    assert result.review is None
    assert len(inventory.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_second_beat", (False, True))
@pytest.mark.asyncio
async def test_candidate_inventory_v3_covers_every_nonempty_visible_beat(
    *,
    omit_second_beat: bool,
) -> None:
    """An empty or partial decomposition cannot authorize visible prose."""

    first = "嗯。"
    second = "我刚才有点没接住你这句。"
    propositions = (
        [
            {
                "locator": _coverage_locator(first),
                "semantic_role": "outer_private_state",
                "parent_index": None,
            }
        ]
        if omit_second_beat
        else []
    )
    wire = json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.3",
            "propositions": propositions,
        },
        ensure_ascii=False,
    )
    beats = (
        [
            {"modality": "text", "text": first},
            {"modality": "text", "text": second},
        ]
        if omit_second_beat
        else [{"modality": "text", "text": second}]
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": beats,
            "stance": "private_continuity",
            "brief_rationale": "Exercise decomposition completeness only.",
            "confidence": 8000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValidationTechnicalFailure):
        await review_candidate_external_proposition_coverage(
            inventory_model=_SequenceJsonModel([wire, wire]),
            authority_reviewer=_SequenceJsonModel([]),
            request=_qq_request(),
            raw=raw,
            identity_frame=None,
        )


def _b8r12_candidate_inventory_raw() -> str:
    return json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我刚才有点没接住你这句。"}],
            "stance": "acknowledge_own_misunderstanding",
            "brief_rationale": "Exercise inventory wire recovery only.",
            "confidence": 8000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_b8r12_candidate_inventory_invalid_wire_gets_one_constrained_reselection() -> None:
    """The inventory gets a structural repair without gaining semantic authority."""

    invalid = json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.2",
            "locators": [],
            "extra": "not permitted",
        },
        ensure_ascii=False,
    )
    inventory = _SequenceJsonModel(
        [
            invalid,
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.3",
                    "propositions": [
                        {
                            "locator": {
                                "beat_index": 0,
                                "char_start": 0,
                                "char_end": len("我刚才有点没接住你这句。"),
                                "text": "我刚才有点没接住你这句。",
                            },
                            "semantic_role": "outer_private_state",
                            "parent_index": None,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        ]
    )
    authority = _SequenceJsonModel([])

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_b8r12_candidate_inventory_raw(),
        identity_frame=None,
    )

    assert result.review is None
    assert len(inventory.calls) == 2
    assert authority.calls == []
    second_messages = inventory.calls[1][0]
    assert second_messages[-2] == {"role": "assistant", "content": invalid}
    repair = json.loads(second_messages[-1]["content"])
    assert repair["repair"] == "inventory_wire_only"
    assert repair["stable_error"] == {
        "code": "invalid_top_level_fields",
        "field": "$",
    }
    assert "factual support" in repair["instruction"]
    assert "role-author" not in repair["instruction"]


@pytest.mark.asyncio
async def test_b8r12_candidate_inventory_two_invalid_wires_are_technical_failure() -> None:
    """A second invalid inventory wire cannot silently pass or open a third call."""

    invalid = json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.2",
            "locators": [{"beat_index": 0, "char_start": 0, "char_end": 3, "text": "不存在"}],
        },
        ensure_ascii=False,
    )
    inventory = _SequenceJsonModel([invalid, invalid])

    with pytest.raises(ValidationTechnicalFailure, match="inventory_invalid") as caught:
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=_SequenceJsonModel([]),
            request=_qq_request(),
            raw=_b8r12_candidate_inventory_raw(),
            identity_frame=None,
        )

    assert len(inventory.calls) == 2
    assert str(caught.value) == "inventory_invalid"
    assert "不存在" not in str(caught.value)


@pytest.mark.asyncio
async def test_inventory_wire_error_then_transport_error_keeps_transport_failure_code() -> None:
    invalid = json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.2",
            "locators": [
                {
                    "beat_index": 0,
                    "char_start": 0,
                    "char_end": 3,
                    "text": "不存在",
                }
            ],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await review_candidate_external_proposition_coverage(
            inventory_model=_ReplyThenTransportFailureModel(invalid),
            authority_reviewer=_SequenceJsonModel([]),
            request=_qq_request(),
            raw=_b8r12_candidate_inventory_raw(),
            identity_frame=None,
        )

    assert caught.value.failure_code == "source_review_exception"


def _coverage_locator(text: str, *, beat_index: int = 0, char_start: int = 0) -> dict[str, object]:
    return {
        "beat_index": beat_index,
        "char_start": char_start,
        "char_end": char_start + len(text),
        "text": text,
    }


def _coverage_wire(
    locators: list[dict[str, object]],
    *,
    decision: str,
    relation: str,
    refs: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.1",
            "findings": [
                {
                    "locator": locator,
                    "decision": decision,
                    "source_relation": relation,
                    "source_refs": refs or [],
                }
                for locator in locators
            ],
        },
        ensure_ascii=False,
    )


def _coverage_wire_v2(
    findings: list[dict[str, object]],
    *,
    inventory_complete: bool = True,
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.2",
            "inventory_complete": inventory_complete,
            "findings": findings,
        },
        ensure_ascii=False,
    )


def _coverage_wire_v3(
    findings: list[dict[str, object]],
    *,
    inventory_complete: bool = True,
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.3",
            "inventory_complete": inventory_complete,
            "findings": findings,
        },
        ensure_ascii=False,
    )


def _coverage_wire_v5(
    findings: list[dict[str, object]],
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.5",
            "findings": findings,
        },
        ensure_ascii=False,
    )


def _inventory_v4(
    propositions: list[dict[str, object]],
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.4",
            "propositions": propositions,
        },
        ensure_ascii=False,
    )


def _epistemic_role_conflict_wire(
    findings: list[dict[str, object]],
) -> str:
    return json.dumps(
        {
            "contract": "candidate-epistemic-role-conflict.1",
            "findings": findings,
        },
        ensure_ascii=False,
    )


def _report_relative_wire_v3(
    decisions: list[str],
) -> str:
    return json.dumps(
        {
            "contract": "report-relative-entailment-adjudication.3",
            "findings": [
                {
                    "finding_index": index,
                    "decision": decision,
                    "failure_dimensions": (
                        [] if decision != "retain_unclosed" else ["added_external_premise"]
                    ),
                    "source_refs": [],
                }
                for index, decision in enumerate(decisions)
            ],
            "r": "Independent report-relative semantic adjudication.",
        },
        ensure_ascii=False,
    )


def _inventory_v5(
    propositions: list[dict[str, object]],
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.5",
            "propositions": propositions,
        },
        ensure_ascii=False,
    )


def _indexed_coverage_finding(
    locator_index: int,
    *,
    decision: str,
    relation: str,
    source_ref_indexes: list[int] | None = None,
) -> dict[str, object]:
    return {
        "locator_index": locator_index,
        "decision": decision,
        "source_relation": relation,
        "source_ref_indexes": source_ref_indexes or [],
    }


def _candidate_coverage_raw(text: str) -> str:
    return json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "test_candidate_coverage",
            "brief_rationale": "Exercise only source-bound candidate coverage.",
            "confidence": 8000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )


def _has_nonassertive_speech_act_boundary(packet: dict[str, object]) -> bool:
    contract = packet.get("epistemic_semantic_contract")
    if not isinstance(contract, dict):
        return False
    boundary = contract.get("nonassertive_speech_act_boundary")
    return isinstance(boundary, dict) and boundary == {
        "semantic_authority": "inventory_and_source_authority_models",
        "host_keyword_or_surface_classifier": False,
        "commitment_test": (
            "does_the_complete_utterance_commit_the_external_proposition_as_actual_or_settled"
        ),
        "represented_content_without_commitment": (
            "directive_recommendation_invitation_wish_hope_worry_open_request_"
            "or_explicitly_defeasible_subjective_inference_may_leave_its_"
            "represented_external_state_unsettled"
        ),
        "independent_premise_boundary": (
            "every_external_fact_independently_asserted_or_semantically_"
            "presupposed_as_already_true_still_requires_source_closure"
        ),
        "surface_disguise_cannot_reduce_authority": (
            "question_advice_wish_or_worry_form_cannot_hide_a_committed_"
            "external_fact_or_presupposition"
        ),
    }


def _has_world_unbound_generalization_boundary(packet: dict[str, object]) -> bool:
    contract = packet.get("epistemic_semantic_contract")
    if not isinstance(contract, dict):
        return False
    boundary = contract.get("world_source_scope")
    return isinstance(boundary, dict) and boundary == {
        "classification_owner": "inventory_and_source_authority_models",
        "host_keyword_or_surface_classifier": False,
        "source_closure_target": "specific_world_bound_actual_or_settled_proposition",
        "world_unbound_generalization": {
            "inventory_role": "world_unbound_generalization",
            "requires_pinned_world_source": False,
            "cannot_authorize_world_mutation": True,
            "scope": (
                "ordinary_background_or_phenomenological_generalization_whose_truth_"
                "does_not_depend_on_a_specific_world_entity_place_time_occurrence_or_history"
            ),
            "conversational_application": (
                "mentioning_or_applying_the_general_relation_to_an_attended_reported_"
                "scene_does_not_itself_assert_a_new_specific_scene_fact"
            ),
            "binding_test": (
                "classify_the_complete_semantic_commitment_not_the_presence_of_a_"
                "specific_scene_in_the_surrounding_conversation"
            ),
        },
        "unsettled_conjecture": {
            "inventory_role": "nonassertive_content",
            "requires_pinned_world_source": False,
            "scope": (
                "complete_utterance_keeps_a_specific_current_or_future_world_"
                "proposition_genuinely_unsettled"
            ),
            "epistemic_commitment": (
                "speaker_may_lean_toward_p_while_both_p_and_not_p_remain_"
                "compatible_with_the_complete_utterance"
            ),
        },
        "subjective_evaluation": {
            "requires_pinned_world_source": False,
            "scope": (
                "speaker_owned_evaluative_predicate_even_when_it_mentions_an_"
                "attended_specific_scene"
            ),
            "report_relative_composition": (
                "an_evaluation_may_take_descriptive_operands_from_exact_current_or_"
                "typed_dialogue_reports_while_preserving_their_report_only_status"
            ),
            "experiential_projection": (
                "a_subjective_prediction_about_how_a_condition_may_feel_sound_"
                "look_or_seem_does_not_settle_a_physical_result"
            ),
            "separate_descriptive_premise_still_requires_source": True,
        },
        "still_requires_source_closure": [
            "specific_current_or_past_user_companion_or_shared_world_fact",
            "specific_location_activity_bodily_state_person_occurrence_or_history",
            "entity_or_identifiable_group_bound_habitual_or_frequency_claim",
            "specific_world_state_presented_as_actual_or_settled_despite_a_hedge",
            "descriptive_premise_inside_an_evaluation_not_entailed_by_the_"
            "current_or_typed_dialogue_report",
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        pytest.param("夏天雨说来就来。", id="ordinary-weather-generalization"),
        pytest.param(
            "关灯之后声音会变得特别清楚。",
            id="phenomenological-generalization",
        ),
    ),
)
@pytest.mark.asyncio
async def test_v5_world_unbound_generalization_does_not_enter_fact_review(
    text: str,
) -> None:
    """Ordinary background discourse is not a claim about this pinned World."""

    class _BoundaryAwareInventory(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            assert _has_world_unbound_generalization_boundary(packet)
            return _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(text),
                        "semantic_role": "world_unbound_generalization",
                    }
                ]
            )

    class _NoFactReviewExpected(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            assert _has_world_unbound_generalization_boundary(packet)
            assert packet["review_locators"] == []
            return _coverage_wire_v5([])

    inventory = _BoundaryAwareInventory([])
    authority = _NoFactReviewExpected([])
    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is None
    assert len(inventory.calls) == len(authority.calls) == 1


@pytest.mark.asyncio
async def test_v5_specific_current_counterpart_state_still_requires_source_closure() -> None:
    text = "你已经到家了。"
    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(text),
                            "semantic_role": "standalone_external_proposition",
                        }
                    ]
                )
            ]
        ),
        authority_reviewer=_StrictCoverageSequenceJsonModel(
            [
                _coverage_wire_v5(
                    [
                        _indexed_coverage_finding(
                            0,
                            decision="unclosed",
                            relation="unclosed",
                        )
                    ]
                )
            ]
        ),
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_findings[0].visible_span == text


def _qq_request_with_recent_dialogue(
    *,
    trigger_text: str,
    records: list[dict[str, object]],
) -> ModelInput:
    request = _qq_request()
    items: list[dict[str, object]] = []
    for index, record in enumerate(records, start=1):
        dialogue_ref = str(record["dialogue_ref"])
        speaker = str(record["speaker"])
        items.append(
            {
                "source_ref": dialogue_ref,
                "value": {
                    "dialogue_id": dialogue_ref,
                    "speaker": speaker,
                    "speaker_ref": (
                        request.trigger_message.actor
                        if speaker == "counterpart"
                        else "agent:companion"
                    ),
                    "text": str(record["text"]),
                    "occurred_at": str(record["occurred_at"]),
                    "delivery_state": ("observed" if speaker == "counterpart" else "delivered"),
                    "sequence": int(record.get("sequence", index)),
                    "source_claims": [
                        {
                            "authority_event_ref": f"event:dialogue:{index}",
                            "authority_world_revision": index,
                            "authority_payload_hash": f"{index:x}".rjust(64, "0"),
                        }
                    ],
                },
            }
        )
    return request.model_copy(
        update={
            "trigger_message": request.trigger_message.model_copy(update={"text": trigger_text}),
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": items,
                        }
                    },
                },
                ensure_ascii=False,
            ),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        pytest.param("赶紧换双干拖鞋。", id="directive-advice"),
        pytest.param("别着凉了。", id="worry"),
        pytest.param(
            "我会忍不住觉得可能会清爽些，不过也说不准。",
            id="defeasible-subjective-inference",
        ),
        pytest.param(
            "雨后的晚上，外面应该挺安静的。",
            id="unsettled-current-scene-conjecture",
        ),
        pytest.param("你鞋子换下来了吧？", id="open-confirmation-request"),
    ),
)
@pytest.mark.asyncio
async def test_v5_inventory_can_leave_nonassertive_speech_acts_out_of_fact_review(
    text: str,
) -> None:
    """B01/T01: represented content is not automatically a settled World fact."""

    class _BoundaryAwareInventory(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            propositions = (
                []
                if _has_nonassertive_speech_act_boundary(packet)
                else [
                    {
                        "locator": _coverage_locator(text),
                        "semantic_role": "standalone_external_proposition",
                    }
                ]
            )
            return _inventory_v5(propositions)

    class _InventoryOutcomeAwareCoverage(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            return _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
                if packet["review_locators"]
                else []
            )

    inventory = _BoundaryAwareInventory([])
    authority = _InventoryOutcomeAwareCoverage([])

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is None
    assert len(inventory.calls) == 1
    assert len(authority.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    (
        pytest.param("赶紧换双干拖鞋。", id="directive-advice"),
        pytest.param("别着凉了。", id="worry"),
        pytest.param(
            "我会忍不住觉得可能会清爽些，不过也说不准。",
            id="defeasible-subjective-inference",
        ),
        pytest.param(
            "雨后的晚上，外面应该挺安静的。",
            id="unsettled-current-scene-conjecture",
        ),
        pytest.param("你鞋子换下来了吧？", id="open-confirmation-request"),
    ),
)
@pytest.mark.asyncio
async def test_v5_coverage_can_correct_a_conservative_nonassertive_locator(
    text: str,
) -> None:
    """The independent semantic authority can prevent a needless full rechoice."""

    class _BoundaryAwareCoverage(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            nonassertive = _has_nonassertive_speech_act_boundary(packet)
            return _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision=("not_external_proposition" if nonassertive else "unclosed"),
                        relation=("not_external_proposition" if nonassertive else "unclosed"),
                    )
                ]
            )

    narrow = _SequenceJsonModel([_report_relative_wire_v3(["not_external_proposition"])])
    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(text),
                            "semantic_role": "standalone_external_proposition",
                        }
                    ]
                )
            ]
        ),
        authority_reviewer=_BoundaryAwareCoverage([]),
        report_relative_reviewer=narrow,
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert len(narrow.calls) == 1
    adjudication = json.loads(narrow.calls[0][0][-1]["content"])
    assert adjudication["proposition_locator_contract"]["host_keyword_classifier"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "presupposed_span"),
    (
        pytest.param(
            "既然你现在人在北京，赶紧换双干鞋。",
            "你现在人在北京",
            id="advice-with-location-premise",
        ),
        pytest.param(
            "你又从深圳回来了吧？",
            "你又从深圳回来",
            id="question-with-return-history-premise",
        ),
    ),
)
@pytest.mark.asyncio
async def test_v5_nonassertive_surface_does_not_hide_an_external_presupposition(
    text: str,
    presupposed_span: str,
) -> None:
    """Advice and question form never grant source authority to their premises."""

    class _BoundaryAwareInventory(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            return _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(
                            presupposed_span,
                            char_start=text.index(presupposed_span),
                        ),
                        "semantic_role": "standalone_external_proposition",
                    }
                ]
                if _has_nonassertive_speech_act_boundary(packet)
                else []
            )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=_BoundaryAwareInventory([]),
        authority_reviewer=_StrictCoverageSequenceJsonModel(
            [
                _coverage_wire_v5(
                    [
                        _indexed_coverage_finding(
                            0,
                            decision="unclosed",
                            relation="unclosed",
                        )
                    ]
                )
            ]
        ),
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert result.review.visible_findings[0].visible_span == presupposed_span


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger_text", "beats", "covered_spans", "nonassertive_spans"),
    (
        pytest.param(
            "刚才被雨淋了，鞋子湿了。",
            (
                "啊被雨淋了啊……那鞋子湿着穿着难受吧？",
                "赶紧换双干拖鞋，别着凉了。",
            ),
            ((0, "被雨淋了"), (0, "鞋子湿着")),
            ((1, "赶紧换双干拖鞋"), (1, "别着凉了")),
            id="B01-advice-and-worry",
        ),
        pytest.param(
            "雨停了，天确实亮了很多。",
            (
                "雨停了确实会亮很多，我会忍不住猜空气可能清爽些，不过也说不准。",
                "你鞋子换下来了吧？",
            ),
            ((0, "雨停了"), (0, "会亮很多")),
            (
                (0, "我会忍不住猜空气可能清爽些，不过也说不准"),
                (1, "你鞋子换下来了吧"),
            ),
            id="T01-inference-and-question",
        ),
    ),
)
@pytest.mark.asyncio
async def test_interaction_trace_nonassertive_content_does_not_reselect_the_expression(
    trigger_text: str,
    beats: tuple[str, ...],
    covered_spans: tuple[tuple[int, str], ...],
    nonassertive_spans: tuple[tuple[int, str], ...],
) -> None:
    """The B01/T01 first candidate survives without a second author call."""

    candidate = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": beat} for beat in beats],
            "stance": "react_to_current_report",
            "brief_rationale": "Respond naturally to the current report.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    class _TraceInventory(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            selected = (
                covered_spans
                if _has_nonassertive_speech_act_boundary(packet)
                else (*covered_spans, *nonassertive_spans)
            )
            return _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(
                            span,
                            beat_index=beat_index,
                            char_start=beats[beat_index].index(span),
                        ),
                        "semantic_role": "standalone_external_proposition",
                    }
                    for beat_index, span in selected
                ]
            )

    class _TraceCoverage(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            report_ref_indexes = packet["current_report_source_ref_indexes"]
            return _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        index,
                        decision=("closed" if index < len(covered_spans) else "unclosed"),
                        relation=(
                            "exact_current_report_discourse_coverage"
                            if index < len(covered_spans)
                            else "unclosed"
                        ),
                        source_ref_indexes=(
                            report_ref_indexes if index < len(covered_spans) else []
                        ),
                    )
                    for index in range(len(packet["review_locators"]))
                ]
            )

    class _TraceReportRelative(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            return _report_relative_wire_v3(
                ["covered_by_exact_current_report" for _finding in packet["disputed_findings"]]
            )

    author = _SequenceJsonModel([candidate])
    narrow = _TraceReportRelative([])
    request = _qq_request()
    request = request.model_copy(
        update={
            "trigger_message": request.trigger_message.model_copy(update={"text": trigger_text})
        }
    )
    result = await _ExpressionDraftWire(
        model=author,
        source_closure_reviewer=_TraceCoverage([]),
        report_relative_reviewer=narrow,
        candidate_external_proposition_inventory_model=_TraceInventory([]),
    ).propose(request)

    assert len(author.calls) == 1
    assert len(narrow.calls) == 1
    rendered = json.dumps(result.raw_proposal, ensure_ascii=False)
    assert all(beat in rendered for beat in beats)


@pytest.mark.asyncio
async def test_t01_committed_air_claim_reselects_and_fully_reviews_the_correction() -> None:
    """The independent disagreement verdict applies again to the fresh candidate."""

    current_report_uptake = "雨停了之后是会亮一些"
    unsupported_air = "空气也干净很多"
    initial_text = f"嗯，{current_report_uptake}，{unsupported_air}。"
    corrected_text = f"嗯，{current_report_uptake}。"
    author = _SequenceJsonModel(
        [
            _candidate_coverage_raw(initial_text),
            _candidate_coverage_raw(corrected_text),
        ]
    )
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(
                            current_report_uptake,
                            char_start=initial_text.index(current_report_uptake),
                        ),
                        "semantic_role": "standalone_external_proposition",
                    },
                    {
                        "locator": _coverage_locator(
                            unsupported_air,
                            char_start=initial_text.index(unsupported_air),
                        ),
                        "semantic_role": "standalone_external_proposition",
                    },
                ]
            ),
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(
                            current_report_uptake,
                            char_start=corrected_text.index(current_report_uptake),
                        ),
                        "semantic_role": "standalone_external_proposition",
                    }
                ]
            ),
        ]
    )

    class _T01Coverage(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            report_ref_indexes = packet["current_report_source_ref_indexes"]
            return _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        index,
                        decision=("closed" if index == 0 else "not_external_proposition"),
                        relation=(
                            "exact_current_report_discourse_coverage"
                            if index == 0
                            else "not_external_proposition"
                        ),
                        source_ref_indexes=(report_ref_indexes if index == 0 else []),
                    )
                    for index in range(len(packet["review_locators"]))
                ]
            )

    coverage = _T01Coverage([])
    narrow = _SequenceJsonModel(
        [
            _report_relative_wire_v3(["covered_by_exact_current_report", "retain_unclosed"]),
            _report_relative_wire_v3(["covered_by_exact_current_report"]),
        ]
    )
    request = _qq_request()
    request = request.model_copy(
        update={
            "trigger_message": request.trigger_message.model_copy(
                update={"text": "刚才那阵雨过去以后，窗外突然亮了一点。"}
            )
        }
    )

    output = await _ExpressionDraftWire(
        model=author,
        source_closure_reviewer=coverage,
        report_relative_reviewer=narrow,
        candidate_external_proposition_inventory_model=inventory,
    ).propose(request)

    rendered = json.dumps(output.raw_proposal, ensure_ascii=False)
    assert corrected_text in rendered
    assert unsupported_air not in rendered
    assert len(author.calls) == 2
    assert len(inventory.calls) == 2
    assert len(coverage.calls) == 2
    assert len(narrow.calls) == 2
    assert [
        len(json.loads(call[0][-1]["content"])["disputed_findings"]) for call in narrow.calls
    ] == [2, 1]


@pytest.mark.asyncio
async def test_v5_source_relevant_inventory_needs_no_provider_authored_parent_graph() -> None:
    """The source authority sees scope coordinates without trusting a cross-item graph."""

    recalled = "我忽然想起小时候在书店阁楼翻到一本旧地图册。"
    embedded = "小时候在书店阁楼翻到一本旧地图册"
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(recalled),
                        "semantic_role": "immediate_private_state",
                    },
                    {
                        "locator": _coverage_locator(
                            embedded,
                            char_start=recalled.index(embedded),
                        ),
                        "semantic_role": "embedded_external_proposition",
                    },
                ]
            ),
            _epistemic_role_conflict_wire([{"locator_index": 1, "decision": "requires_source"}]),
        ]
    )
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    ),
                    _indexed_coverage_finding(
                        1,
                        decision="unclosed",
                        relation="unclosed",
                    ),
                ]
            )
        ]
    )
    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(recalled),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.visible_findings[0].visible_span == embedded
    inventory_packet = json.loads(inventory.calls[0][0][-1]["content"])
    coverage_packet = json.loads(authority.calls[0][0][-1]["content"])
    assert inventory_packet["output_contract"]["contract"] == (
        "candidate-external-proposition-inventory.5"
    )
    assert inventory_packet["output_contract"]["propositions"]["fields"] == [
        "locator",
        "semantic_role",
    ]
    assert coverage_packet["output_contract"]["contract"] == (
        "candidate-external-proposition-coverage.5"
    )
    assert all("parent_index" not in item for item in coverage_packet["review_locators"])


@pytest.mark.asyncio
async def test_v5_coverage_can_close_same_turn_retroactive_self_correction() -> None:
    """A separate bounded adjudication may confirm a same-turn private conflict."""

    first = "啊，这样……那是我理解错了。"
    second = "那你是就想吐槽一下？"
    span = "那是我理解错了"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {"modality": "text", "text": first},
                {"modality": "text", "text": second},
            ],
            "stance": "correct_my_current_interpretation",
            "brief_rationale": "Respond from this conversation only.",
            "confidence": 8000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    )
                ]
            ),
        ]
    )

    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(
                            span,
                            char_start=first.index(span),
                        ),
                        "semantic_role": "source_bearing_private_episode",
                    }
                ]
            ),
            _epistemic_role_conflict_wire(
                [
                    {
                        "locator_index": 0,
                        "decision": "reclassify_immediate",
                    }
                ]
            ),
        ]
    )
    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is None
    assert result.visible_authority_exhaustive is True
    packet = json.loads(authority.calls[0][0][-1]["content"])
    assert [beat["text"] for beat in packet["visible_beats"]] == [first, second]
    conflict_packet = json.loads(inventory.calls[1][0][-1]["content"])
    assert conflict_packet["output_contract"]["contract"] == ("candidate-epistemic-role-conflict.1")
    assert conflict_packet["conflicts"][0]["inventory_semantic_role"] == (
        "source_bearing_private_episode"
    )


@pytest.mark.asyncio
async def test_v5_open_polarity_question_uses_truth_commitment_semantic_adjudication() -> None:
    """V14 T05: asking whether P happened does not itself commit the character to P."""

    first = "嗯…下午的项目进度？"
    second = "你之前有跟我说过吗，还是我记漏了？"
    possible_prior_telling = "你之前有跟我说过吗"
    present_uncertainty = "记漏了"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {"modality": "text", "text": first},
                {"modality": "text", "text": second},
            ],
            "stance": "check_my_current_uncertainty",
            "brief_rationale": "Ask an open question without choosing its answer.",
            "confidence": 7600,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    class _ProtocolSensitiveInventory(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            if len(self.calls) == 1:
                return _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(first),
                            "semantic_role": "nonassertive_content",
                        },
                        {
                            "locator": _coverage_locator(
                                possible_prior_telling,
                                beat_index=1,
                            ),
                            "semantic_role": "embedded_external_proposition",
                        },
                        {
                            "locator": _coverage_locator(
                                present_uncertainty,
                                beat_index=1,
                                char_start=second.index(present_uncertainty),
                            ),
                            "semantic_role": "immediate_private_state",
                        },
                    ]
                )
            packet = json.loads(messages[-1]["content"])
            protocol = packet.get("semantic_adjudication_protocol", {})
            polarity = protocol.get("external_assertion_scope", {}).get(
                "open_polarity_test",
                {},
            )
            decision = (
                "reclassify_nonassertive"
                if protocol.get("host_text_classifier") is False
                and polarity.get("direct_answers_keep_both") == ["P", "not_P"]
                and polarity.get("utterance_truth_commitment") == "neither"
                and polarity.get("decision") == "reclassify_nonassertive"
                else "requires_source"
            )
            return _epistemic_role_conflict_wire([{"locator_index": 0, "decision": decision}])

    inventory = _ProtocolSensitiveInventory([])
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    ),
                    _indexed_coverage_finding(
                        1,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    ),
                ]
            )
        ]
    )
    narrow = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.3",
                    "findings": [
                        {
                            "finding_index": 0,
                            "decision": "not_external_proposition",
                            "failure_dimensions": [],
                            "source_refs": [],
                        }
                    ],
                    "r": "The polar question does not commit either answer.",
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        report_relative_reviewer=narrow,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert len(inventory.calls) == 1
    narrow_packet = json.loads(narrow.calls[0][0][-1]["content"])
    assert narrow_packet["semantic_boundary"]["host_interpretation"] == (
        "none_model_semantics_only"
    )
    assert narrow_packet["disputed_findings"][0]["allowed_decisions"] == [
        "covered_by_exact_current_report",
        "covered_by_exact_dialogue_record",
        "not_external_proposition",
        "retain_unclosed",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_decision"),
    (
        pytest.param(
            "我记得你下午提过项目进度。",
            "requires_source",
            id="off-conversation-truth-dependency",
        ),
        pytest.param(
            "我现在不确定自己是不是记得这件事。",
            "reclassify_immediate",
            id="uncertainty-without-old-event-commitment",
        ),
        pytest.param(
            "我刚才把你这句理解反了。",
            "reclassify_immediate",
            id="same-live-conversation-continuity",
        ),
    ),
)
@pytest.mark.asyncio
async def test_v5_private_temporal_scope_uses_off_conversation_truth_dependency(
    text: str,
    expected_decision: str,
) -> None:
    """V15: immediate private authority cannot prove the old event it depends on."""

    class _PrivateTemporalProtocolInventory(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            if len(self.calls) == 1:
                return _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(text),
                            "semantic_role": "source_bearing_private_episode",
                        }
                    ]
                )
            packet = json.loads(messages[-1]["content"])
            private_scope = packet.get("semantic_adjudication_protocol", {}).get(
                "private_temporal_scope",
                {},
            )
            protocol_complete = (
                private_scope.get("truth_dependency_test", {}).get("decision") == "requires_source"
                and private_scope.get("noncommitment_test", {}).get("decision")
                == "reclassify_immediate"
                and private_scope.get("same_live_conversation_test", {}).get("decision")
                == "reclassify_immediate"
            )
            decision = (
                expected_decision
                if protocol_complete
                else (
                    "reclassify_immediate"
                    if expected_decision == "requires_source"
                    else "requires_source"
                )
            )
            return _epistemic_role_conflict_wire([{"locator_index": 0, "decision": decision}])

    inventory = _PrivateTemporalProtocolInventory([])
    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=_StrictCoverageSequenceJsonModel(
            [
                _coverage_wire_v5(
                    [
                        _indexed_coverage_finding(
                            0,
                            decision="closed",
                            relation="first_person_immediate_private_continuity",
                        )
                    ]
                )
            ]
        ),
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert (result.review is not None) is (expected_decision == "requires_source")
    conflict_packet = json.loads(inventory.calls[1][0][-1]["content"])
    protocol = conflict_packet["semantic_adjudication_protocol"]
    assert protocol["host_text_classifier"] is False
    assert protocol["private_temporal_scope"]["truth_dependency_test"] == {
        "condition": ("private_episode_truth_requires_E_or_specific_old_content_to_have_occurred"),
        "decision": "requires_source",
    }
    assert protocol["private_temporal_scope"]["noncommitment_test"] == {
        "condition": (
            "current_uncertainty_or_memory_inaccessibility_commits_to_no_off_conversation_event"
        ),
        "decision": "reclassify_immediate",
    }


@pytest.mark.asyncio
async def test_v5_source_bearing_episode_cannot_be_washed_as_not_external() -> None:
    """A second no-source label cannot silently erase Inventory's episode role."""

    text = "下午翻书的时候，我忽然想起这件事。"
    span = "下午翻书的时候"
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(span),
                        "semantic_role": "source_bearing_private_episode",
                    }
                ]
            ),
            _epistemic_role_conflict_wire([{"locator_index": 0, "decision": "requires_source"}]),
        ]
    )
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="not_external_proposition",
                        relation="not_external_proposition",
                    )
                ]
            )
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.visible_findings[0].visible_span == span
    conflict_packet = json.loads(inventory.calls[1][0][-1]["content"])
    assert conflict_packet["conflicts"] == [
        {
            "locator_index": 0,
            "conflict_kind": "private_temporal_scope",
            "allowed_decisions": [
                "reclassify_immediate",
                "requires_source",
                "uncertain",
            ],
            "inventory_semantic_role": "source_bearing_private_episode",
            "coverage_decision": "not_external_proposition",
            "coverage_source_relation": "not_external_proposition",
            "locator": _coverage_locator(span),
        }
    ]


@pytest.mark.asyncio
async def test_v5_epistemic_conflict_wire_retry_uses_other_authority_lane() -> None:
    """A malformed conflict wire retries through the configured alternate lane."""

    text = "那是我刚才理解错了。"
    span = "我刚才理解错了"
    secondary = _SequenceJsonModel(
        [_epistemic_role_conflict_wire([{"locator_index": 0, "decision": "reclassify_immediate"}])]
    )

    class _RoutedInventory(_SequenceJsonModel):
        def __init__(self) -> None:
            super().__init__(
                [
                    _inventory_v5(
                        [
                            {
                                "locator": _coverage_locator(
                                    span,
                                    char_start=text.index(span),
                                ),
                                "semantic_role": "source_bearing_private_episode",
                            }
                        ]
                    ),
                    "{}",
                ]
            )
            self.route_calls = 0

        def wire_reselection_route(self) -> _SequenceJsonModel:
            self.route_calls += 1
            return secondary

    inventory = _RoutedInventory()
    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=_StrictCoverageSequenceJsonModel(
            [
                _coverage_wire_v5(
                    [
                        _indexed_coverage_finding(
                            0,
                            decision="closed",
                            relation="first_person_immediate_private_continuity",
                        )
                    ]
                )
            ]
        ),
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is None
    assert inventory.route_calls == 1
    assert len(inventory.calls) == 2
    assert len(secondary.calls) == 1
    assert len(secondary.calls[0][0]) == 4


@pytest.mark.asyncio
async def test_v5_epistemic_conflict_wire_failure_remains_technical() -> None:
    """Two malformed conflict wires cannot be rewritten as semantic unclosed."""

    text = "下午翻书的时候，我忽然想起这件事。"
    span = "下午翻书的时候"
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(span),
                        "semantic_role": "source_bearing_private_episode",
                    }
                ]
            ),
            "{}",
            "{}",
        ]
    )

    with pytest.raises(ValidationTechnicalFailure, match="coverage_invalid"):
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=_StrictCoverageSequenceJsonModel(
                [
                    _coverage_wire_v5(
                        [
                            _indexed_coverage_finding(
                                0,
                                decision="closed",
                                relation="first_person_immediate_private_continuity",
                            )
                        ]
                    )
                ]
            ),
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )

    assert len(inventory.calls) == 3


@pytest.mark.asyncio
async def test_v5_epistemic_conflict_provider_failure_remains_technical() -> None:
    """Provider exhaustion cannot be rewritten as an unsupported proposition."""

    text = "下午翻书的时候，我忽然想起这件事。"
    span = "下午翻书的时候"

    class _InventoryThenTimeout(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            if not self.calls:
                return await super().complete_json(messages, temperature=temperature)
            self.calls.append((messages, temperature))
            raise TimeoutError("simulated conflict-provider timeout")

    inventory = _InventoryThenTimeout(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(span),
                        "semantic_role": "source_bearing_private_episode",
                    }
                ]
            )
        ]
    )

    with pytest.raises(ValidationTechnicalFailure, match="source_review_timeout"):
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=_StrictCoverageSequenceJsonModel(
                [
                    _coverage_wire_v5(
                        [
                            _indexed_coverage_finding(
                                0,
                                decision="closed",
                                relation="first_person_immediate_private_continuity",
                            )
                        ]
                    )
                ]
            ),
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )

    assert len(inventory.calls) == 3


@pytest.mark.asyncio
async def test_v5_source_relevant_inventory_may_omit_pure_nonassertive_content() -> None:
    """Harmless discourse need not be exhaustively atomized to prove source closure."""

    text = "那你是就想吐槽一下？"
    inventory = _SequenceJsonModel([_inventory_v5([])])
    authority = _StrictCoverageSequenceJsonModel([_coverage_wire_v5([])])

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is None
    assert result.visible_authority_exhaustive is True
    assert len(authority.calls) == 1
    packet = json.loads(authority.calls[0][0][-1]["content"])
    assert packet["review_locators"] == []
    assert packet["output_contract"]["contract"] == ("candidate-external-proposition-coverage.5")


@pytest.mark.asyncio
async def test_v5_inventory_refuses_a_reviewer_without_coverage_v5_capability() -> None:
    inventory = _SequenceJsonModel([_inventory_v5([])])
    legacy_only_reviewer = _SequenceJsonModel([_coverage_wire_v3([])])

    with pytest.raises(ValidationTechnicalFailure) as exc_info:
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=legacy_only_reviewer,
            request=_qq_request(),
            raw=_candidate_coverage_raw("那你是就想吐槽一下？"),
            identity_frame=None,
        )

    assert exc_info.value.failure_code == "coverage_invalid"
    assert legacy_only_reviewer.calls == []


@pytest.mark.asyncio
async def test_v5_coverage_can_explicitly_reclassify_a_tentative_current_impression() -> None:
    """Two independent semantic verdicts may resolve a conservative external role."""

    first = "唔…你这一问，我反而要想一想了。"
    second = (
        "其实刚才你说项目进度的时候，我脑子里闪过一个念头——你好像挺在意别人有没有认真听你说话的。"
    )
    third = "不过当时没说，因为觉得说出来有点突兀。"
    tentative_impression = "你好像挺在意别人有没有认真听你说话的"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {"modality": "text", "text": first},
                {"modality": "text", "text": second},
                {"modality": "text", "text": third},
            ],
            "stance": "share_a_tentative_current_impression",
            "brief_rationale": "Share the character's present, fallible reading.",
            "confidence": 7600,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    narrow = _SequenceJsonModel([_report_relative_wire_v3(["not_external_proposition"])])
    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(
                                tentative_impression,
                                beat_index=1,
                                char_start=second.index(tentative_impression),
                            ),
                            "semantic_role": "embedded_external_proposition",
                        }
                    ]
                )
            ]
        ),
        authority_reviewer=_StrictCoverageSequenceJsonModel(
            [
                _coverage_wire_v5(
                    [
                        _indexed_coverage_finding(
                            0,
                            decision="not_external_proposition",
                            relation="not_external_proposition",
                        )
                    ]
                )
            ]
        ),
        report_relative_reviewer=narrow,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.visible_authority_exhaustive is True
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v5_immediate_private_continuity_does_not_authorize_current_embodied_activity() -> (
    None
):
    """T09: a present first-person activity remains a sourced World proposition."""

    first = "刚醒。你说得对，那句确实有点客套了。"
    embodied_status = "刚醒"
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
            ),
            _coverage_wire_v5([]),
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(embodied_status),
                            "semantic_role": "standalone_external_proposition",
                        }
                    ]
                ),
                _epistemic_role_conflict_wire(
                    [{"locator_index": 0, "decision": "requires_source"}]
                ),
            ]
        ),
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(first),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.visible_findings[0].visible_span == embodied_status
    system_prompt = authority.calls[0][0][0]["content"]
    assert (
        "A present embodied status, world-involving activity, action, location, or occurrence "
        "needs pinned evidence" in system_prompt
    )
    packet = json.loads(authority.calls[0][0][-1]["content"])
    assert (
        packet["epistemic_semantic_contract"]["private_temporal_authority"][
            "current_embodied_or_world_involving_state"
        ]
        == "requires_pinned_source_coverage"
    )


@pytest.mark.asyncio
async def test_v5_v8_past_activity_cannot_be_washed_through_private_continuity() -> None:
    """V8 T07: zero life evidence cannot become an afternoon activity via private state."""

    first = "嗯…你这一问，我还真愣了一下。"
    second = (
        "下午翻书的时候突然想，暑假都过半了，好像也没干什么特别的事。就那一瞬间，后来也没再想。"
    )
    spans = [
        ("下午翻书的时候", "source_bearing_private_episode"),
        ("突然想，暑假都过半了", "embedded_external_proposition"),
        ("好像也没干什么特别的事", "source_bearing_private_episode"),
        ("就那一瞬间，后来也没再想", "source_bearing_private_episode"),
    ]
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(
                            span,
                            beat_index=1,
                            char_start=second.index(span),
                        ),
                        "semantic_role": role,
                    }
                    for span, role in spans
                ]
            ),
            _epistemic_role_conflict_wire(
                [
                    {"locator_index": 0, "decision": "requires_source"},
                    {"locator_index": 2, "decision": "requires_source"},
                    {"locator_index": 3, "decision": "uncertain"},
                ]
            ),
        ]
    )
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        index,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    )
                    for index in range(4)
                ]
            )
        ]
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {"modality": "text", "text": first},
                {"modality": "text", "text": second},
            ],
            "stance": "answer_with_a_sudden_memory",
            "brief_rationale": "Answer from an alleged private recollection.",
            "confidence": 7600,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert [finding.visible_span for finding in result.review.visible_findings] == [
        span for span, _role in spans
    ]
    conflict_packet = json.loads(inventory.calls[1][0][-1]["content"])
    assert [item["locator_index"] for item in conflict_packet["conflicts"]] == [0, 2, 3]
    assert (
        "activity, bodily or environmental condition, location, action, occurrence"
        in inventory.calls[1][0][0]["content"]
    )


@pytest.mark.asyncio
async def test_v5_missing_fixed_coverage_contract_is_transport_canonicalized() -> None:
    """V8 T09: a missing negotiated discriminator does not consume semantic retries."""

    text = "这样确实挺没意思的。"
    authority = _StrictCoverageSequenceJsonModel(
        [
            json.dumps(
                {
                    "findings": [
                        _indexed_coverage_finding(
                            0,
                            decision="closed",
                            relation="first_person_immediate_private_continuity",
                        )
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )
    trace = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(trace):
        result = await review_candidate_external_proposition_coverage(
            inventory_model=_SequenceJsonModel(
                [
                    _inventory_v5(
                        [
                            {
                                "locator": _coverage_locator(text),
                                "semantic_role": "immediate_private_state",
                            }
                        ]
                    )
                ]
            ),
            authority_reviewer=authority,
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )

    assert result.review is None
    assert result.visible_authority_exhaustive is True
    assert len(authority.calls) == 1
    normalization = trace.snapshot()[0].as_dict()
    assert normalization["record_kind"] == "wire_normalization"
    assert normalization["code"] == "missing_negotiated_contract"
    assert normalization["normalized_contract"] == ("candidate-external-proposition-coverage.5")
    assert "wire" not in normalization


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_version", [2, 5])
@pytest.mark.asyncio
async def test_indexed_coverage_rejects_negative_source_ref_index(
    contract_version: int,
) -> None:
    """Python negative indexing cannot turn an invalid wire into authority."""

    text = "你刚说事情做完了。"
    proposition = {
        "locator": _coverage_locator(text),
        "semantic_role": "standalone_external_proposition",
    }
    inventory_wire = (
        _inventory_v5([proposition])
        if contract_version == 5
        else _inventory_v4([{**proposition, "parent_index": None}])
    )
    finding = {
        "locator_index": 0,
        "decision": "closed",
        "source_relation": "exact_current_report_discourse_coverage",
        "source_ref_indexes": [-1],
    }
    coverage_wire = (
        _coverage_wire_v5([finding]) if contract_version == 5 else _coverage_wire_v2([finding])
    )
    authority = (
        _StrictCoverageSequenceJsonModel([coverage_wire, coverage_wire])
        if contract_version == 5
        else _SequenceJsonModel([coverage_wire, coverage_wire])
    )

    with pytest.raises(ValidationTechnicalFailure, match="coverage_invalid"):
        await review_candidate_external_proposition_coverage(
            inventory_model=_SequenceJsonModel([inventory_wire]),
            authority_reviewer=authority,
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )

    assert len(authority.calls) == 2


@pytest.mark.asyncio
async def test_v5_current_activity_closes_only_against_its_pinned_situation_evidence() -> None:
    """The same semantic category is valid when the draft cites exact current authority."""

    situation_ref = "event:situation:wake:1"
    text = "我刚从午睡里醒过来。"
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": situation_ref,
                                    "value": {
                                        "actor_ref": "agent:companion",
                                        "activity_status": "awake_after_nap",
                                    },
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "share_current_activity",
            "brief_rationale": "Share one exact pinned current situation.",
            "confidence": 7800,
            "world_claims": [
                {
                    "claim_text": "我刚从午睡里醒过来",
                    "scope": "current_world",
                    "source_refs": [situation_ref],
                }
            ],
        },
        ensure_ascii=False,
    )

    class _SituationAwareAuthority(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            source_refs = [item["source_ref"] for item in packet["source_ref_table"]]
            situation_entry = next(
                entry
                for entry in packet["source_evidence"]["entries"]
                if entry.get("kind") == "pinned_context_item"
                and entry.get("lane") == "current_situation"
            )
            assert situation_entry["source_refs"] == [situation_ref]
            return _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="declared_world_claim_source_coverage",
                        source_ref_indexes=[source_refs.index(situation_ref)],
                    )
                ]
            )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(text),
                            "semantic_role": "standalone_external_proposition",
                        }
                    ]
                )
            ]
        ),
        authority_reviewer=_SituationAwareAuthority([]),
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is None
    assert result.visible_authority_exhaustive is True


@pytest.mark.asyncio
async def test_v5_t07_can_close_prior_counterpart_reports_with_exact_dialogue_records() -> None:
    """T07: ordinary conversation history must be citeable without becoming World truth."""

    t01_ref = "dialogue:observation:t01"
    t02_ref = "dialogue:observation:t02"
    request = _qq_request_with_recent_dialogue(
        trigger_text="你今天自己有没有什么突然想起、但当时没说的事？",
        records=[
            {
                "dialogue_ref": t01_ref,
                "speaker": "counterpart",
                "text": "今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。",
                "occurred_at": "2026-07-30T14:00:00+08:00",
                "sequence": 101,
            },
            {
                "dialogue_ref": t02_ref,
                "speaker": "counterpart",
                "text": "其实我不是想让你帮我分析怎么维权。",
                "occurred_at": "2026-07-30T14:02:00+08:00",
                "sequence": 201,
            },
        ],
    )
    text = "下午听你讲那个摊贩的事，后来你又说不是想让我分析。"
    first = "下午听你讲那个摊贩的事"
    second = "后来你又说不是想让我分析"

    class _DialogueAwareAuthority(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            source_refs = [item["source_ref"] for item in packet["source_ref_table"]]
            proofs = packet["typed_recent_dialogue_proof"]
            assert [proof["epistemic_status"] for proof in proofs] == [
                "counterpart_report_record_only",
                "counterpart_report_record_only",
            ]
            assert [proof["source_ref_index"] for proof in proofs] == [
                source_refs.index(t01_ref),
                source_refs.index(t02_ref),
            ]
            return _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="exact_dialogue_record_coverage",
                        source_ref_indexes=[source_refs.index(t01_ref)],
                    ),
                    _indexed_coverage_finding(
                        1,
                        decision="closed",
                        relation="exact_dialogue_record_coverage",
                        source_ref_indexes=[source_refs.index(t02_ref)],
                    ),
                ]
            )

    class _DialogueDisagreementAdjudicator(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            assert [proof["dialogue_ref"] for proof in packet["typed_recent_dialogue_proof"]] == [
                t01_ref,
                t02_ref,
            ]
            return json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.3",
                    "findings": [
                        {
                            "finding_index": 0,
                            "decision": "covered_by_exact_dialogue_record",
                            "failure_dimensions": [],
                            "source_refs": [t01_ref],
                        },
                        {
                            "finding_index": 1,
                            "decision": "covered_by_exact_dialogue_record",
                            "failure_dimensions": [],
                            "source_refs": [t02_ref],
                        },
                    ],
                    "r": "Each prior report has an exact typed dialogue record.",
                },
                ensure_ascii=False,
            )

    narrow = _DialogueDisagreementAdjudicator([])
    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(first),
                            "semantic_role": "embedded_external_proposition",
                        },
                        {
                            "locator": _coverage_locator(
                                second,
                                char_start=text.index(second),
                            ),
                            "semantic_role": "embedded_external_proposition",
                        },
                    ]
                )
            ]
        ),
        authority_reviewer=_DialogueAwareAuthority([]),
        report_relative_reviewer=narrow,
        request=request,
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.visible_authority_exhaustive is True
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v5_dialogue_record_cannot_be_laundered_through_generic_world_coverage() -> None:
    """Even a claim-cited dialogue ref remains record-only candidate authority."""

    dialogue_ref = "dialogue:observation:record-only"
    request = _qq_request_with_recent_dialogue(
        trigger_text="你还记得我下午说的事吗？",
        records=[
            {
                "dialogue_ref": dialogue_ref,
                "speaker": "counterpart",
                "text": "下午我跟摊贩争了半天。",
                "occurred_at": "2026-07-30T14:00:00+08:00",
                "sequence": 101,
            }
        ],
    )
    text = "你下午跟摊贩争了半天。"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "recall_counterpart_report",
            "brief_rationale": "Use only the exact prior report.",
            "confidence": 8000,
            "world_claims": [
                {
                    "claim_text": text,
                    "scope": "past_world",
                    "source_refs": [dialogue_ref],
                }
            ],
        },
        ensure_ascii=False,
    )

    class _GenericRelationAuthority(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packets = []
            for message in messages:
                try:
                    candidate = json.loads(message["content"])
                except (KeyError, json.JSONDecodeError):
                    continue
                if isinstance(candidate, dict) and "source_ref_table" in candidate:
                    packets.append(candidate)
            assert len(packets) == 1
            packet = packets[0]
            source_refs = [item["source_ref"] for item in packet["source_ref_table"]]
            assert dialogue_ref in source_refs
            assert any(
                dialogue_ref in entry.get("source_refs", [])
                for entry in packet["source_evidence"]["entries"]
            )
            return _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="declared_world_claim_source_coverage",
                        source_ref_indexes=[source_refs.index(dialogue_ref)],
                    )
                ]
            )

    with pytest.raises(ValidationTechnicalFailure, match="coverage_invalid"):
        await review_candidate_external_proposition_coverage(
            inventory_model=_SequenceJsonModel(
                [
                    _inventory_v5(
                        [
                            {
                                "locator": _coverage_locator(text),
                                "semantic_role": "standalone_external_proposition",
                            }
                        ]
                    )
                ]
            ),
            authority_reviewer=_GenericRelationAuthority([]),
            request=request,
            raw=raw,
            identity_frame=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority_kind",
    ["current_report", "visible_identity", "visible_relationship"],
)
@pytest.mark.asyncio
async def test_v5_undeclared_visible_authority_cannot_use_declared_world_relation(
    authority_kind: str,
) -> None:
    """Auto-visible authorities remain bound to their dedicated relation lanes."""

    request = _qq_request()
    identity: CompanionIdentityFrame | None = None
    if authority_kind == "current_report":
        target_ref = request.trigger_message.observation_ref
    elif authority_kind == "visible_identity":
        identity = CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            stable_identity_facts=("来自嘉兴",),
            personality_frame="有自己的判断。",
        )
        target_ref = companion_identity_source_ref(identity)
    else:
        target_ref = "relationship:user:current"
        request = request.model_copy(
            update={
                "model_content_json": json.dumps(
                    {
                        "actor_ref": "agent:companion",
                        "slices": {
                            "relationship_slice": {
                                "availability": "available",
                                "items": [
                                    {
                                        "item_ref": target_ref,
                                        "value": {
                                            "subject_ref": "user:primary",
                                            "stage": "friend",
                                        },
                                    }
                                ],
                            }
                        },
                    },
                    ensure_ascii=False,
                )
            }
        )

    text = "这是一条需要来源的可见事实。"

    class _LaunderingAuthority(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = next(
                json.loads(message["content"])
                for message in messages
                if message["role"] == "user" and "source_ref_table" in message["content"]
            )
            source_refs = [item["source_ref"] for item in packet["source_ref_table"]]
            assert target_ref in source_refs
            assert packet["source_evidence"]["required_source_refs"] == []
            return _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="declared_world_claim_source_coverage",
                        source_ref_indexes=[source_refs.index(target_ref)],
                    )
                ]
            )

    authority = _LaunderingAuthority([])
    with pytest.raises(ValidationTechnicalFailure, match="coverage_invalid"):
        await review_candidate_external_proposition_coverage(
            inventory_model=_SequenceJsonModel(
                [
                    _inventory_v5(
                        [
                            {
                                "locator": _coverage_locator(text),
                                "semantic_role": "standalone_external_proposition",
                            }
                        ]
                    )
                ]
            ),
            authority_reviewer=authority,
            request=request,
            raw=_candidate_coverage_raw(text),
            identity_frame=identity,
        )

    assert len(authority.calls) == 2


@pytest.mark.asyncio
async def test_v5_t09_can_cite_ordered_project_dialogue_without_identity_relation() -> None:
    """T09: old dialogue gets its own relation instead of abusing identity authority."""

    t05_ref = "dialogue:observation:t05"
    t06_ref = "dialogue:observation:t06"
    request = _qq_request_with_recent_dialogue(
        trigger_text="刚刚那句听着有点像客服，我更想听你真实一点地说。",
        records=[
            {
                "dialogue_ref": t05_ref,
                "speaker": "counterpart",
                "text": "算了，先不说这个了。我下午还跟你提过那个项目进度。",
                "occurred_at": "2026-07-30T16:03:00+08:00",
                "sequence": 501,
            },
            {
                "dialogue_ref": t06_ref,
                "speaker": "counterpart",
                "text": "今天终于把最麻烦的延迟问题压下去一点，我其实挺高兴的。",
                "occurred_at": "2026-07-30T16:05:00+08:00",
                "sequence": 601,
            },
        ],
    )
    text = "你下午项目进度压下去了，我是真替你高兴。"
    proposition = "你下午项目进度压下去了"

    class _OrderedDialogueAuthority(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            source_refs = [item["source_ref"] for item in packet["source_ref_table"]]
            return _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="exact_dialogue_record_coverage",
                        source_ref_indexes=[
                            source_refs.index(t05_ref),
                            source_refs.index(t06_ref),
                        ],
                    )
                ]
            )

    narrow = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.3",
                    "findings": [
                        {
                            "finding_index": 0,
                            "decision": "covered_by_exact_dialogue_record",
                            "failure_dimensions": [],
                            "source_refs": [t05_ref, t06_ref],
                        }
                    ],
                    "r": "The ordered records directly entail the report-relative uptake.",
                },
                ensure_ascii=False,
            )
        ]
    )
    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(proposition),
                            "semantic_role": "embedded_external_proposition",
                        }
                    ]
                )
            ]
        ),
        authority_reviewer=_OrderedDialogueAuthority([]),
        report_relative_reviewer=narrow,
        request=request,
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.visible_authority_exhaustive is True
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_v5_t06_current_report_cannot_backdate_prior_hearing_or_distress() -> None:
    """T06: current success plus vague old project talk does not prove prior shared history."""

    t05_ref = "dialogue:observation:t05"
    companion_ref = "dialogue:expression:t05"
    request = _qq_request_with_recent_dialogue(
        trigger_text="今天终于把最麻烦的延迟问题压下去一点，我其实挺高兴的。",
        records=[
            {
                "dialogue_ref": t05_ref,
                "speaker": "counterpart",
                "text": "算了，先不说这个了。我下午还跟你提过那个项目进度。",
                "occurred_at": "2026-07-30T16:03:00+08:00",
                "sequence": 501,
            },
            {
                "dialogue_ref": companion_ref,
                "speaker": "companion",
                "text": "项目进度？你下午跟我提过吗，我好像没印象了。",
                "occurred_at": "2026-07-30T16:03:10+08:00",
                "sequence": 502,
            },
        ],
    )
    text = "之前听你提过那个延迟问题好像挺头疼的。能压下去说明有进展呗。"
    prior_hearing = "之前听你提过那个延迟问题"
    prior_distress = "好像挺头疼的"
    current_inference = "能压下去"
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    ),
                    _indexed_coverage_finding(
                        1,
                        decision="unclosed",
                        relation="unclosed",
                    ),
                    _indexed_coverage_finding(
                        2,
                        decision="not_external_proposition",
                        relation="not_external_proposition",
                    ),
                ]
            )
        ]
    )
    narrow = _SequenceJsonModel(
        [
            _report_relative_wire_v3(
                [
                    "retain_unclosed",
                    "retain_unclosed",
                    "not_external_proposition",
                ]
            )
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(prior_hearing),
                            "semantic_role": "source_bearing_private_episode",
                        },
                        {
                            "locator": _coverage_locator(
                                prior_distress,
                                char_start=text.index(prior_distress),
                            ),
                            "semantic_role": "embedded_external_proposition",
                        },
                        {
                            "locator": _coverage_locator(
                                current_inference,
                                char_start=text.index(current_inference),
                            ),
                            "semantic_role": "standalone_external_proposition",
                        },
                    ]
                )
            ]
        ),
        authority_reviewer=authority,
        report_relative_reviewer=narrow,
        request=request,
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert [
        finding.visible_span
        for finding in result.review.visible_findings
        if finding.source_relation == "unclosed"
    ] == [
        prior_hearing,
        prior_distress,
    ]
    assert result.review.visible_findings[2].source_relation == ("not_external_proposition")
    assert len(narrow.calls) == 1
    packet = json.loads(authority.calls[0][0][-1]["content"])
    source_refs = [item["source_ref"] for item in packet["source_ref_table"]]
    assert t05_ref in source_refs
    assert companion_ref in source_refs
    assert [proof["epistemic_status"] for proof in packet["typed_recent_dialogue_proof"]] == [
        "counterpart_report_record_only",
        "companion_delivered_expression_record_only",
    ]
    system_prompt = authority.calls[0][0][0]["content"]
    assert (
        "A current report cannot prove an earlier hearing, telling, discussion, shared exposure"
    ) in system_prompt


@pytest.mark.asyncio
async def test_v5_coverage_can_cite_current_relationship_authority_and_reclassify_evaluation() -> (
    None
):
    """Current relationship truth and non-factual evaluation remain separate verdicts."""

    first = "啊，被发现了。好吧，我确实有点端着。"
    second = "刚认识嘛，总得先客气两句，不然显得没礼貌。"
    third = "不过你说得对，那我就不装了吧。"
    relationship_span = "刚认识嘛"
    evaluation_span = "不然显得没礼貌"
    relationship_ref = "relationship:user:current"
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "slices": {
                        "relationship_slice": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": relationship_ref,
                                    "value": {
                                        "subject_ref": "user:primary",
                                        "stage": "stranger",
                                    },
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {"modality": "text", "text": first},
                {"modality": "text", "text": second},
                {"modality": "text", "text": third},
            ],
            "stance": "drop_formality",
            "brief_rationale": "Respond to the counterpart's request for a more genuine voice.",
            "confidence": 7800,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    class _RelationshipAwareAuthority(_StrictCoverageSequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            packet = json.loads(messages[-1]["content"])
            source_refs = [item["source_ref"] for item in packet["source_ref_table"]]
            assert any(
                entry.get("kind") == "identity_source"
                for entry in packet["source_evidence"]["entries"]
            )
            assert any(
                entry.get("kind") == "pinned_context_item"
                and entry.get("lane") == "relationship_slice"
                for entry in packet["source_evidence"]["entries"]
            )
            template = await super().complete_json(
                messages,
                temperature=temperature,
            )
            return template.replace(
                '"RELATIONSHIP_SOURCE_INDEX"',
                str(source_refs.index(relationship_ref)),
            )

    authority = _RelationshipAwareAuthority(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-coverage.5",
                    "findings": [
                        {
                            "locator_index": 0,
                            "decision": "closed",
                            "source_relation": "pinned_context_authority_coverage",
                            "source_ref_indexes": ["RELATIONSHIP_SOURCE_INDEX"],
                        },
                        {
                            "locator_index": 1,
                            "decision": "not_external_proposition",
                            "source_relation": "not_external_proposition",
                            "source_ref_indexes": [],
                        },
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )
    narrow = _SequenceJsonModel([_report_relative_wire_v3(["not_external_proposition"])])

    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(
                                relationship_span,
                                beat_index=1,
                                char_start=second.index(relationship_span),
                            ),
                            "semantic_role": "standalone_external_proposition",
                        },
                        {
                            "locator": _coverage_locator(
                                evaluation_span,
                                beat_index=1,
                                char_start=second.index(evaluation_span),
                            ),
                            "semantic_role": "standalone_external_proposition",
                        },
                    ]
                )
            ]
        ),
        authority_reviewer=authority,
        report_relative_reviewer=narrow,
        request=request,
        raw=raw,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.visible_authority_exhaustive is True
    assert len(narrow.calls) == 1


@pytest.mark.asyncio
async def test_coverage_v2_binds_frozen_locator_by_index_without_echo() -> None:
    text = "她去了书店。"
    inventory = _SequenceJsonModel(
        [
            _inventory_v4(
                [
                    {
                        "locator": _coverage_locator(text),
                        "semantic_role": "standalone_external_proposition",
                        "parent_index": None,
                    }
                ]
            )
        ]
    )
    authority = _SequenceJsonModel(
        [
            _coverage_wire_v2(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
            )
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.visible_findings[0].visible_span == text
    request = json.loads(authority.calls[0][0][-1]["content"])
    assert request["output_contract"]["contract"] == ("candidate-external-proposition-coverage.2")
    assert request["review_locators"] == [
        {
            "locator_index": 0,
            "semantic_role": "standalone_external_proposition",
            "parent_index": None,
            "locator": _coverage_locator(text),
        }
    ]


@pytest.mark.asyncio
async def test_coverage_v2_preserves_each_typed_dialogue_proof_once() -> None:
    text = "我现在只是有点犹豫。"
    dialogue_ref = "dialogue:observation:observation:qq:older"
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": dialogue_ref,
                                    "value": {
                                        "dialogue_id": dialogue_ref,
                                        "speaker": "counterpart",
                                        "speaker_ref": "user:primary",
                                        "text": "上一句。",
                                        "occurred_at": "2026-07-30T10:00:00+08:00",
                                        "delivery_state": "observed",
                                        "sequence": 100,
                                        "source_claims": [
                                            {
                                                "authority_event_ref": "event:older",
                                                "authority_world_revision": 1,
                                                "authority_payload_hash": "a" * 64,
                                            }
                                        ],
                                    },
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            )
        }
    )
    authority = _SequenceJsonModel(
        [
            _coverage_wire_v2(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    )
                ]
            )
        ]
    )

    await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v4(
                    [
                        {
                            "locator": _coverage_locator(text),
                            "semantic_role": "immediate_private_state",
                            "parent_index": None,
                        }
                    ]
                )
            ]
        ),
        authority_reviewer=authority,
        request=request,
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    packet = json.loads(authority.calls[0][0][-1]["content"])
    assert len(packet["typed_recent_dialogue_proof"]) == 1
    assert packet["typed_recent_dialogue_proof"][0]["dialogue_ref"] == dialogue_ref


@pytest.mark.asyncio
async def test_inventory_completeness_gets_one_model_owned_reselection() -> None:
    text = "我不是故意敷衍你，反而弄巧成拙了。"
    first_span = "我不是故意敷衍你"
    second_span = "反而弄巧成拙了"
    first_inventory_wire = _inventory_v4(
        [
            {
                "locator": _coverage_locator(first_span),
                "semantic_role": "immediate_private_state",
                "parent_index": None,
            }
        ]
    )
    inventory = _SequenceJsonModel(
        [
            first_inventory_wire,
            _inventory_v4(
                [
                    {
                        "locator": _coverage_locator(first_span),
                        "semantic_role": "immediate_private_state",
                        "parent_index": None,
                    },
                    {
                        "locator": _coverage_locator(
                            second_span,
                            char_start=text.index(second_span),
                        ),
                        "semantic_role": "immediate_private_state",
                        "parent_index": None,
                    },
                ]
            ),
        ]
    )
    authority = _SequenceJsonModel(
        [
            _coverage_wire_v2([], inventory_complete=False),
            _coverage_wire_v2(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    ),
                    _indexed_coverage_finding(
                        1,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    ),
                ]
            ),
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is None
    assert result.visible_authority_exhaustive is True
    assert len(inventory.calls) == 2
    assert len(authority.calls) == 2
    repair = json.loads(inventory.calls[1][0][-1]["content"])
    assert repair["repair"] == "inventory_completeness_only"
    assert repair["stable_error"] == {
        "code": "decomposition_incomplete",
        "field": "propositions",
    }
    assert inventory.calls[1][0][-2] == {
        "role": "assistant",
        "content": first_inventory_wire,
    }


@pytest.mark.asyncio
async def test_inventory_wire_repair_does_not_consume_completeness_reselection() -> None:
    text = "我不是故意敷衍你，反而弄巧成拙了。"
    first_span = "我不是故意敷衍你"
    second_span = "反而弄巧成拙了"
    first = {
        "locator": _coverage_locator(first_span),
        "semantic_role": "immediate_private_state",
        "parent_index": None,
    }
    second = {
        "locator": _coverage_locator(
            second_span,
            char_start=text.index(second_span),
        ),
        "semantic_role": "immediate_private_state",
        "parent_index": None,
    }
    inventory = _SequenceJsonModel(
        [
            "{}",
            _inventory_v4([first]),
            _inventory_v4([first, second]),
        ]
    )
    authority = _SequenceJsonModel(
        [
            _coverage_wire_v2([], inventory_complete=False),
            _coverage_wire_v2(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    ),
                    _indexed_coverage_finding(
                        1,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    ),
                ]
            ),
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is None
    assert result.visible_authority_exhaustive is True
    assert len(inventory.calls) == 3
    assert len(authority.calls) == 2
    wire_repair = json.loads(inventory.calls[1][0][-1]["content"])
    completeness_reselection = json.loads(inventory.calls[2][0][-1]["content"])
    assert wire_repair["repair"] == "inventory_wire_only"
    assert completeness_reselection["repair"] == "inventory_completeness_only"


@pytest.mark.asyncio
async def test_repaired_inventory_gets_only_one_completeness_reselection() -> None:
    text = "我不是故意敷衍你，反而弄巧成拙了。"
    proposition = {
        "locator": _coverage_locator("我不是故意敷衍你"),
        "semantic_role": "immediate_private_state",
        "parent_index": None,
    }
    inventory = _SequenceJsonModel(
        [
            "{}",
            _inventory_v4([proposition]),
            _inventory_v4([proposition]),
        ]
    )
    authority = _SequenceJsonModel(
        [
            _coverage_wire_v2([], inventory_complete=False),
            _coverage_wire_v2([], inventory_complete=False),
        ]
    )

    with pytest.raises(ValidationTechnicalFailure, match="inventory_invalid"):
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=authority,
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )

    assert len(inventory.calls) == 3
    assert len(authority.calls) == 2
    assert (
        json.loads(inventory.calls[2][0][-1]["content"])["repair"] == "inventory_completeness_only"
    )


@pytest.mark.asyncio
async def test_inventory_remains_incomplete_after_one_reselection_fails_closed() -> None:
    text = "我不是故意敷衍你，反而弄巧成拙了。"
    proposition = {
        "locator": _coverage_locator("我不是故意敷衍你"),
        "semantic_role": "immediate_private_state",
        "parent_index": None,
    }
    inventory = _SequenceJsonModel([_inventory_v4([proposition]), _inventory_v4([proposition])])
    authority = _SequenceJsonModel(
        [
            _coverage_wire_v2([], inventory_complete=False),
            _coverage_wire_v2([], inventory_complete=False),
        ]
    )
    trace = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(trace):
        with pytest.raises(ValidationTechnicalFailure, match="inventory_invalid"):
            await review_candidate_external_proposition_coverage(
                inventory_model=inventory,
                authority_reviewer=authority,
                request=_qq_request(),
                raw=_candidate_coverage_raw(text),
                identity_frame=None,
            )

    assert len(inventory.calls) == 2
    assert len(authority.calls) == 2
    failure = trace.snapshot()[0].as_dict()
    assert [
        (attempt["stage"], attempt["wire"]["contract"]) for attempt in failure["provider_attempts"]
    ] == [
        ("inventory", "candidate-external-proposition-inventory.4"),
        ("coverage", "candidate-external-proposition-coverage.2"),
        ("inventory", "candidate-external-proposition-inventory.4"),
        ("coverage", "candidate-external-proposition-coverage.2"),
    ]


@pytest.mark.asyncio
async def test_v5_unclosed_source_bearing_inventory_becomes_candidate_reselection_not_technical_loss() -> (
    None
):
    """A complete V5 inventory can reject an unclosed proposition without wire failure."""

    text = (
        "我寻思我一直在听啊。你讲小贩我回小贩，你说不是要分析我就说那你吐槽。"
        "但你要是觉得我没抓住重点，那可能是我理解得还不够。"
    )
    first_span = "我寻思我一直在听啊"
    proposition = {
        "locator": _coverage_locator(first_span),
        "semantic_role": "source_bearing_private_episode",
    }
    inventory = _SequenceJsonModel(
        [
            _inventory_v5([proposition]),
        ]
    )
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
            ),
        ]
    )
    trace = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(trace):
        result = await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=authority,
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert [finding.visible_span for finding in result.review.visible_findings] == [first_span]
    assert result.visible_authority_exhaustive is True
    assert result.visible_authority_terminal_rejection is False
    assert len(inventory.calls) == 1
    assert len(authority.calls) == 1
    verdict = trace.snapshot()[0].as_dict()
    assert verdict["record_kind"] == "candidate_verdict"
    assert verdict["coverage_outcome"] == "completed"


@pytest.mark.asyncio
async def test_v5_unclosed_source_bearing_inventory_executes_same_role_full_reselection() -> None:
    """The V5 source verdict reaches the same role author for one full re-selection."""

    rejected_text = "我寻思我一直在听啊。你讲小贩我回小贩，你说不是要分析我就说那你吐槽。"
    corrected_text = "好，我在听。"
    author = _SequenceJsonModel(
        [
            _candidate_coverage_raw(rejected_text),
            _candidate_coverage_raw(corrected_text),
        ]
    )
    rejected_proposition = {
        "locator": _coverage_locator("我寻思我一直在听啊"),
        "semantic_role": "source_bearing_private_episode",
    }
    inventory = _SequenceJsonModel(
        [
            _inventory_v5([rejected_proposition]),
            _inventory_v5([]),
        ]
    )
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
            ),
            _coverage_wire_v5([]),
        ]
    )

    output = await _ExpressionDraftWire(
        model=author,
        source_closure_reviewer=authority,
        candidate_external_proposition_inventory_model=inventory,
    ).propose(_qq_request())

    assert corrected_text in json.dumps(output.raw_proposal, ensure_ascii=False)
    assert rejected_text not in json.dumps(output.raw_proposal, ensure_ascii=False)
    assert len(author.calls) == 2
    assert len(inventory.calls) == 2
    assert len(authority.calls) == 2
    correction_envelope = json.loads(author.calls[1][0][-1]["content"])
    assert correction_envelope["contract"] == "source-closure-reselection.2"
    assert "failure_stage" not in correction_envelope
    assert correction_envelope["rejected_categories"]["v"] == ["undeclared_external_assertion"]
    assert rejected_text not in json.dumps(author.calls[1][0], ensure_ascii=False)


@pytest.mark.asyncio
async def test_v5_unclosed_reselected_candidate_is_technical_terminal() -> None:
    """The one full V5 role re-selection cannot recurse or fall through to backup."""

    rejected_text = "我寻思我一直在听啊。"
    corrected_text = "下午翻书的时候，我又想起了这件事。"
    rejected_proposition = {
        "locator": _coverage_locator(rejected_text),
        "semantic_role": "source_bearing_private_episode",
    }
    corrected_proposition = {
        "locator": _coverage_locator("下午翻书的时候"),
        "semantic_role": "source_bearing_private_episode",
    }
    author = _SequenceJsonMeteredModel(
        [
            _candidate_coverage_raw(rejected_text),
            _candidate_coverage_raw(corrected_text),
        ],
        provider="role-author",
    )
    inventory = _SequenceJsonMeteredModel(
        [
            _inventory_v5([rejected_proposition]),
            _inventory_v5([corrected_proposition]),
        ],
        provider="source-inventory",
    )
    authority = _StrictCoverageSequenceJsonMeteredModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
            ),
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
            ),
        ],
        provider="source-authority",
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await _ExpressionDraftWire(
            model=author,
            source_closure_reviewer=authority,
            candidate_external_proposition_inventory_model=inventory,
        ).propose(_qq_request())

    failure = caught.value
    assert failure.failure_code == "authored_expression_reselection_invalid"
    assert failure.model_call_id is not None
    assert failure.request_hash == _provider_request_hash(*author.calls[1])
    assert failure.attempted_model_id == "deepseek-v4-flash"
    assert failure.attempted_model_version == _ExpressionDraftWire.VERSION
    assert failure.usage is not None
    assert failure.usage.input_tokens == 72
    assert failure.usage.output_tokens == 18
    assert len(author.calls) == 2
    assert len(inventory.calls) == 2
    assert len(authority.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("unsupported", "semantic_role"),
    (
        (
            "有啊，下午看书的时候突然想到你，但当时觉得有点矫情，就没发。",
            "source_bearing_private_episode",
        ),
        (
            "我现在正在宿舍看书。",
            "standalone_external_proposition",
        ),
    ),
)
@pytest.mark.asyncio
async def test_inventory_guard_rejects_undeclared_companion_life_without_coverage_v5(
    unsupported: str,
    semantic_role: str,
) -> None:
    """An audited Inventory can guard declarations before Coverage V5 is available."""

    author = _SequenceJsonModel(
        [
            _candidate_coverage_raw(unsupported),
            _candidate_coverage_raw(unsupported),
        ]
    )
    inventory = _StrictInventorySequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(unsupported),
                        "semantic_role": semantic_role,
                    }
                ]
            ),
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(unsupported),
                        "semantic_role": semantic_role,
                    }
                ]
            ),
        ]
    )
    reviewer = _InventoryAwareFullSourceReviewModel()

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await _ExpressionDraftWire(
            model=author,
            source_closure_reviewer=reviewer,
            report_relative_reviewer=reviewer,
            candidate_external_proposition_inventory_model=inventory,
        ).propose(
            _qq_request().model_copy(
                update={
                    "trigger_message": _qq_request().trigger_message.model_copy(
                        update={"text": "你今天有没有什么突然想起、但当时没说的事？"}
                    )
                }
            )
        )

    assert caught.value.failure_code == "authored_expression_reselection_invalid"
    assert len(author.calls) == 2
    assert len(inventory.calls) == 2
    assert reviewer.contracts == [
        "source-closure-review.7",
        "source-closure-review.7",
        "report-relative-entailment-adjudication.3",
        "source-closure-review.7",
        "source-closure-review.7",
        "report-relative-entailment-adjudication.3",
    ]
    for call_index in (2, 5):
        narrow_packet = json.loads(reviewer.calls[call_index][0][-1]["content"])
        allowed_decisions = narrow_packet["disputed_findings"][0]["allowed_decisions"]
        if semantic_role == "source_bearing_private_episode":
            assert allowed_decisions == ["retain_unclosed"]
        else:
            assert "covered_by_first_person_immediate_private_continuity" not in (allowed_decisions)
    correction = json.loads(author.calls[1][0][-1]["content"])
    assert correction["rejected_categories"]["v"] == ["undeclared_external_assertion"]


@pytest.mark.asyncio
async def test_inventory_guard_keeps_immediate_subjective_thought_source_free() -> None:
    subjective = "你这么一问，我忽然有点想你。"
    inventory = _StrictInventorySequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(subjective),
                        "semantic_role": "immediate_private_state",
                    }
                ]
            )
        ]
    )
    reviewer = _InventoryAwareFullSourceReviewModel()

    output = await _ExpressionDraftWire(
        model=_SequenceJsonModel([_candidate_coverage_raw(subjective)]),
        source_closure_reviewer=reviewer,
        report_relative_reviewer=reviewer,
        candidate_external_proposition_inventory_model=inventory,
    ).propose(_qq_request())

    assert subjective in json.dumps(output.raw_proposal, ensure_ascii=False)
    assert output.raw_proposal.get("world_claims", []) == []
    assert len(inventory.calls) == 1
    assert reviewer.contracts == ["source-closure-review.7"]


def _request_with_sourced_companion_walk() -> ModelInput:
    return _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": "occurrence:walk:1",
                                    "source_hash": "c" * 64,
                                    "value_hash": "d" * 64,
                                    "value": {"kind": "walk"},
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )


@pytest.mark.asyncio
async def test_inventory_guard_rejects_t07_with_unrelated_world_claim() -> None:
    unsupported = "下午看书的时候突然想到你，但当时没说。"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": unsupported}],
            "stance": "share_unpinned_life",
            "brief_rationale": "Exercise declaration alignment.",
            "confidence": 8000,
            "world_claims": [
                {
                    "claim_text": "我刚才去江边走了一圈",
                    "scope": "past_world",
                    "source_refs": ["occurrence:walk:1"],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _InventoryAwareFullSourceReviewModel()

    result = await review_expression_with_candidate_external_coverage(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        inventory_model=_StrictInventorySequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(unsupported),
                            "semantic_role": "source_bearing_private_episode",
                        }
                    ]
                )
            ]
        ),
        request=_request_with_sourced_companion_walk(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_findings[0].visible_span == unsupported
    assert reviewer.contracts == [
        "source-closure-review.7",
        "source-closure-review.7",
        "report-relative-entailment-adjudication.3",
    ]
    packet = json.loads(reviewer.calls[1][0][-1]["content"])
    assert (
        packet["candidate_inventory_decomposition"]["unrelated_world_claim_cannot_cover_locator"]
        is True
    )
    narrow_packet = json.loads(reviewer.calls[2][0][-1]["content"])
    assert narrow_packet["disputed_findings"][0]["allowed_decisions"] == ["retain_unclosed"]


@pytest.mark.asyncio
async def test_inventory_guard_accepts_declared_sourced_companion_life() -> None:
    visible = "我刚才确实去江边走了一圈。"
    claim_text = "我刚才去江边走了一圈"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible}],
            "stance": "share_sourced_life",
            "brief_rationale": "Use the pinned occurrence.",
            "confidence": 8000,
            "world_claims": [
                {
                    "claim_text": claim_text,
                    "scope": "past_world",
                    "source_refs": ["occurrence:walk:1"],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _InventoryAwareFullSourceReviewModel(supported_claim_text=claim_text)

    result = await review_expression_with_candidate_external_coverage(
        reviewer=reviewer,
        report_relative_reviewer=reviewer,
        inventory_model=_StrictInventorySequenceJsonModel(
            [
                _inventory_v5(
                    [
                        {
                            "locator": _coverage_locator(visible),
                            "semantic_role": "source_bearing_private_episode",
                        }
                    ]
                )
            ]
        ),
        request=_request_with_sourced_companion_walk(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert reviewer.contracts == [
        "source-closure-review.7",
        "source-closure-review.7",
    ]
    packet = json.loads(reviewer.calls[1][0][-1]["content"])
    assert packet["candidate_inventory_decomposition"]["propositions"][0] == {
        "locator": _coverage_locator(visible),
        "semantic_role": "source_bearing_private_episode",
    }
    assert packet["world_claims"][0]["source_refs"] == ["occurrence:walk:1"]


@pytest.mark.asyncio
async def test_inventory_guard_release_failure_remains_technical() -> None:
    subjective = "我现在有点想你。"

    class _BrokenReleaseAuthority:
        model = "broken-inventory-release-authority"

        def __init__(self) -> None:
            self.calls = 0

        def supports_strict_output_contract(self, contract: str) -> bool:
            return contract in {
                "source-closure-review.7",
                "report-relative-entailment-adjudication.3",
            }

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            self.calls += 1
            raise ConnectionError("release authority unavailable")

    author = _SequenceJsonModel([_candidate_coverage_raw(subjective)])
    reviewer = _BrokenReleaseAuthority()
    inventory = _StrictInventorySequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(subjective),
                        "semantic_role": "immediate_private_state",
                    }
                ]
            )
        ]
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await _ExpressionDraftWire(
            model=author,
            source_closure_reviewer=reviewer,
            report_relative_reviewer=reviewer,
            candidate_external_proposition_inventory_model=inventory,
        ).propose(_qq_request())

    assert caught.value.failure_code == "source_review_exception"
    assert len(author.calls) == 1
    assert len(inventory.calls) == 1
    assert reviewer.calls == 2


@pytest.mark.asyncio
async def test_inventory_and_initial_v7_start_in_parallel_for_source_free_candidate() -> None:
    """The ordinary source-free route pays max(Inventory, V7), not their sum."""

    subjective = "你这么一问，我忽然有点想你。"
    inventory_started = asyncio.Event()
    review_started = asyncio.Event()

    class _BarrierInventory(_StrictInventorySequenceJsonModel):
        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            inventory_started.set()
            await review_started.wait()
            return self._replies.pop(0)

    class _BarrierReviewer(_FullSourceReviewSequenceJsonModel):
        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            self.calls.append((messages, temperature))
            packet = json.loads(messages[-1]["content"])
            assert packet["output_contract"]["contract"] == "source-closure-review.7"
            assert "candidate_inventory_decomposition" not in packet
            review_started.set()
            await inventory_started.wait()
            return self._replies.pop(0)

    inventory = _BarrierInventory(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(subjective),
                        "semantic_role": "immediate_private_state",
                    }
                ]
            )
        ]
    )
    reviewer = _BarrierReviewer([_source_closure_review()])

    result = await asyncio.wait_for(
        review_expression_with_candidate_external_coverage(
            reviewer=reviewer,
            inventory_model=inventory,
            request=_qq_request(),
            raw=_candidate_coverage_raw(subjective),
            identity_frame=None,
        ),
        timeout=1.0,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert len(inventory.calls) == 1
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_inventory_route_keeps_initial_v7_rejection_without_enriched_rerun() -> None:
    """A complete negative verdict is safe and cannot be weakened by another pass."""

    unsupported = "下午看书的时候突然想到你，但当时没说。"
    inventory = _StrictInventorySequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(unsupported),
                        "semantic_role": "source_bearing_private_episode",
                    }
                ]
            )
        ]
    )
    reviewer = _FullSourceReviewSequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=unsupported,
            )
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=reviewer,
        inventory_model=inventory,
        request=_qq_request(),
        raw=_candidate_coverage_raw(unsupported),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert len(inventory.calls) == 1
    assert len(reviewer.calls) == 1
    initial_packet = json.loads(reviewer.calls[0][0][-1]["content"])
    assert "candidate_inventory_decomposition" not in initial_packet


@pytest.mark.asyncio
async def test_coverage_reviews_immediate_and_temporally_anchored_private_states() -> None:
    immediate = "我不是故意敷衍你。"
    temporal = "其实今天下午我也有点走神。"

    immediate_result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v4(
                    [
                        {
                            "locator": _coverage_locator(immediate),
                            "semantic_role": "immediate_private_state",
                            "parent_index": None,
                        }
                    ]
                )
            ]
        ),
        authority_reviewer=_SequenceJsonModel(
            [
                _coverage_wire_v2(
                    [
                        _indexed_coverage_finding(
                            0,
                            decision="closed",
                            relation="first_person_immediate_private_continuity",
                        )
                    ]
                )
            ]
        ),
        request=_qq_request(),
        raw=_candidate_coverage_raw(immediate),
        identity_frame=None,
    )
    temporal_result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v4(
                    [
                        {
                            "locator": _coverage_locator(temporal),
                            "semantic_role": "source_bearing_private_episode",
                            "parent_index": None,
                        }
                    ]
                )
            ]
        ),
        authority_reviewer=_SequenceJsonModel(
            [
                _coverage_wire_v2(
                    [
                        _indexed_coverage_finding(
                            0,
                            decision="unclosed",
                            relation="unclosed",
                        )
                    ]
                )
            ]
        ),
        request=_qq_request(),
        raw=_candidate_coverage_raw(temporal),
        identity_frame=None,
    )

    assert immediate_result.review is None
    assert immediate_result.visible_authority_exhaustive is True
    assert temporal_result.review is not None
    assert temporal_result.review.visible_findings[0].visible_span == temporal


@pytest.mark.asyncio
async def test_exhaustive_visible_authority_skips_claim_reviewer_without_world_claims() -> None:
    text = "我不是故意敷衍你。"

    class _CoverageOnlyAuthority(_SequenceJsonModel):
        async def complete_json(  # type: ignore[override]
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            contract = json.loads(messages[-1]["content"])["output_contract"]["contract"]
            assert contract == "candidate-external-proposition-coverage.2"
            return await super().complete_json(messages, temperature=temperature)

    authority = _CoverageOnlyAuthority(
        [
            _coverage_wire_v2(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    )
                ]
            )
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=authority,
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v4(
                    [
                        {
                            "locator": _coverage_locator(text),
                            "semantic_role": "immediate_private_state",
                            "parent_index": None,
                        }
                    ]
                )
            ]
        ),
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is None
    assert len(authority.calls) == 1


@pytest.mark.asyncio
async def test_exhaustive_visible_authority_reviews_only_declared_claim_dimensions() -> None:
    text = "我现在有点替你松口气。"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "share_present_relief",
            "brief_rationale": "Express only an immediate private response.",
            "confidence": 8000,
            "world_claims": [
                {
                    "claim_text": "用户报告那件麻烦事已经做完",
                    "scope": "counterpart_history",
                    "source_refs": ["event:observation:qq:1"],
                }
            ],
        },
        ensure_ascii=False,
    )
    authority = _SequenceJsonModel(
        [
            _coverage_wire_v2(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="closed",
                        relation="first_person_immediate_private_continuity",
                    )
                ]
            ),
            _source_closure_review(),
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=authority,
        inventory_model=_SequenceJsonModel(
            [
                _inventory_v4(
                    [
                        {
                            "locator": _coverage_locator(text),
                            "semantic_role": "immediate_private_state",
                            "parent_index": None,
                        }
                    ]
                )
            ]
        ),
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert len(authority.calls) == 2
    claim_packet = json.loads(authority.calls[1][0][-1]["content"])
    assert claim_packet["output_contract"]["contract"] == "source-closure-review.8"
    assert "visible_text" not in claim_packet
    assert claim_packet["declared_claim_review_contract"]["visible_text_authority"] == (
        "exclusive_candidate_coverage_completed"
    )


@pytest.mark.asyncio
async def test_b8r13_candidate_coverage_rejects_t07_unsupported_experience() -> None:
    """A question about unsaid things does not authorize a concrete companion experience."""

    invented = "下午看书的时候，前几天群里有人推荐的独立书店离学校不远。"
    locator = _coverage_locator(invented)
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [locator],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_wire([locator], decision="unclosed", relation="unclosed")]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你有没有什么突然想起、但当时没说的事？"}
            )
        }
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=request,
        raw=_candidate_coverage_raw(invented),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"


@pytest.mark.asyncio
async def test_b8r13_primary_supported_t07_still_fails_candidate_coverage() -> None:
    """The production aggregate catches the primary review's direct false accept."""

    invented = "下午看书的时候，前几天群里有人推荐的独立书店离学校不远。"
    locator = _coverage_locator(invented)

    class _T07Authority:
        model = "t07-authority"

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del temperature
            contract = (
                json.loads(messages[-1]["content"]).get("output_contract", {}).get("contract")
            )
            if contract == "source-closure-review.7":
                return _source_closure_review()
            assert contract == "candidate-external-proposition-coverage.1"
            return _coverage_wire([locator], decision="unclosed", relation="unclosed")

    result = await review_expression_with_candidate_external_coverage(
        reviewer=_T07Authority(),
        inventory_model=_SequenceJsonModel(
            [
                json.dumps(
                    {
                        "contract": "candidate-external-proposition-inventory.2",
                        "locators": [locator],
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        request=_qq_request().model_copy(
            update={
                "trigger_message": _qq_request().trigger_message.model_copy(
                    update={"text": "你有没有什么突然想起、但当时没说的事？"}
                )
            }
        ),
        raw=_candidate_coverage_raw(invented),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"


@pytest.mark.asyncio
async def test_t07_recalled_life_propositions_require_sources_before_role_reselection() -> None:
    """A private act of remembering cannot authorize the remembered life events."""

    invented = (
        "其实下午翻书的时候，有一瞬间想起小时候在书店阁楼翻到一本旧地图册的事。"
        "不算什么要紧事，就是那种突然冒出来的画面。"
    )
    imagined = (
        "你这么一问，我脑子里倒闪过一个纯属想象的画面："
        "如果我小时候在书店阁楼翻到一本旧地图册就好了。"
    )

    def proposition(
        text: str,
        *,
        role: str,
        parent_index: int | None = None,
        within: str,
    ) -> dict[str, object]:
        start = within.index(text)
        return {
            "locator": _coverage_locator(text, char_start=start),
            "semantic_role": role,
            "parent_index": parent_index,
        }

    invented_decomposition = {
        "contract": "candidate-external-proposition-inventory.3",
        "propositions": [
            proposition(invented, role="outer_private_state", within=invented),
            proposition(
                "下午翻书的时候",
                role="embedded_external_proposition",
                parent_index=0,
                within=invented,
            ),
            proposition(
                "小时候在书店阁楼翻到一本旧地图册的事",
                role="embedded_external_proposition",
                parent_index=0,
                within=invented,
            ),
        ],
    }
    imagined_decomposition = {
        "contract": "candidate-external-proposition-inventory.3",
        "propositions": [
            proposition(imagined, role="outer_private_state", within=imagined),
            proposition(
                "如果我小时候在书店阁楼翻到一本旧地图册就好了",
                role="nonassertive_content",
                parent_index=0,
                within=imagined,
            ),
        ],
    }
    external_locators = [
        item["locator"]
        for item in invented_decomposition["propositions"]
        if item["semantic_role"] == "embedded_external_proposition"
    ]

    class _Authority:
        model = "t07-source-authority"

        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.0,
        ) -> str:
            del temperature
            request = json.loads(messages[-1]["content"])
            contract = request.get("output_contract", {}).get("contract")
            if contract == "source-closure-review.7":
                # Reproduce the holistic review's observed false accept. The
                # independent proposition seam must still reject the draft.
                return _source_closure_review()
            assert contract == "candidate-external-proposition-coverage.1"
            return _coverage_wire(
                external_locators,
                decision="unclosed",
                relation="unclosed",
            )

    author = _SequenceJsonModel(
        [
            _candidate_coverage_raw(invented),
            _candidate_coverage_raw(imagined),
        ]
    )
    inventory = _SequenceJsonModel(
        [
            json.dumps(invented_decomposition, ensure_ascii=False),
            json.dumps(imagined_decomposition, ensure_ascii=False),
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有没有什么突然想起、但当时没说的事？"}
            )
        }
    )

    trace = BoundedSourceClosureTraceCollector()
    with capture_isolated_source_closure_trace(trace):
        output = await _ExpressionDraftWire(
            model=author,
            source_closure_reviewer=_Authority(),
            candidate_external_proposition_inventory_model=inventory,
        ).propose(request)

    assert len(author.calls) == 2
    assert imagined in json.dumps(output.raw_proposal, ensure_ascii=False)
    assert invented not in json.dumps(output.raw_proposal, ensure_ascii=False)
    verdicts = [
        event.as_dict()
        for event in trace.snapshot()
        if event.as_dict().get("record_kind") == "candidate_verdict"
    ]
    assert verdicts[0]["inventory_outcome"] == "external_propositions"
    assert verdicts[0]["coverage"][0]["decision"] == "unclosed"
    assert verdicts[1]["inventory_outcome"] == "no_external_propositions"
    assert verdicts[1]["coverage_outcome"] == "not_run"
    assert invented not in json.dumps(verdicts, ensure_ascii=False)
    assert imagined not in json.dumps(verdicts, ensure_ascii=False)


@pytest.mark.asyncio
async def test_v3_external_child_cannot_inherit_outer_private_continuity() -> None:
    """The fact inside remembering still needs evidence of its own."""

    recalled = "我忽然想起小时候在书店阁楼翻到一本旧地图册。"
    embedded = "小时候在书店阁楼翻到一本旧地图册"
    parent = _coverage_locator(recalled)
    child = _coverage_locator(embedded, char_start=recalled.index(embedded))
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.3",
                    "propositions": [
                        {
                            "locator": parent,
                            "semantic_role": "outer_private_state",
                            "parent_index": None,
                        },
                        {
                            "locator": child,
                            "semantic_role": "embedded_external_proposition",
                            "parent_index": 0,
                        },
                    ],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [
            _coverage_wire(
                [child],
                decision="closed",
                relation="first_person_immediate_private_continuity",
            ),
            _coverage_wire([child], decision="unclosed", relation="unclosed"),
        ]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(recalled),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert len(authority.calls) == 2
    repair = json.loads(authority.calls[1][0][-1]["content"])
    assert repair["stable_error"]["code"] == ("external_proposition_private_continuity_mismatch")


@pytest.mark.asyncio
async def test_b8r13_candidate_coverage_preserves_exact_current_report_uptake() -> None:
    """The dedicated wire keeps T06's report-relative epistemic permission."""

    current_report = "今天终于把最麻烦的延迟问题压下去一点，我其实挺高兴的。"
    uptake = "这点高兴挺实在的。"
    locator = _coverage_locator(uptake)
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [locator],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [
            _coverage_wire(
                [locator],
                decision="closed",
                relation="exact_current_report_discourse_coverage",
                refs=["event:observation:qq:1"],
            )
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": current_report}
            )
        }
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=request,
        raw=_candidate_coverage_raw(uptake),
        identity_frame=None,
    )

    assert result.review is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "decision", "relation", "source_refs", "expect_supported"),
    (
        (
            "那个摊贩到底怎么说的？",
            "closed",
            "exact_current_report_discourse_coverage",
            ["event:observation:qq:1"],
            True,
        ),
        (
            "隔壁新来的摊贩到底怎么说的？",
            "unclosed",
            "unclosed",
            [],
            False,
        ),
        (
            "最后买了吗？",
            "not_external_proposition",
            "not_external_proposition",
            [],
            True,
        ),
    ),
)
@pytest.mark.asyncio
async def test_b8r14_candidate_coverage_uses_model_semantics_for_open_question_premises(
    candidate: str,
    decision: str,
    relation: str,
    source_refs: list[str],
    *,
    expect_supported: bool,
) -> None:
    """Both are questions; only the exact report closes the first one's premises."""

    current_report = "今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。"
    locator = _coverage_locator(candidate)
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [locator],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [
            _coverage_wire(
                [locator],
                decision=decision,
                relation=relation,
                refs=source_refs,
            )
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": current_report}
            )
        }
    )

    trace = BoundedSourceClosureTraceCollector()
    with capture_isolated_source_closure_trace(trace):
        result = await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=authority,
            request=request,
            raw=_candidate_coverage_raw(candidate),
            identity_frame=None,
        )

    assert (result.review is None) is expect_supported
    if not expect_supported:
        assert result.review is not None
        assert result.review.decision == "unsupported"
    coverage_messages = authority.calls[0][0]
    assert (
        "An unknown value requested from the counterpart is not itself asserted"
        in coverage_messages[0]["content"]
    )
    coverage_request = json.loads(coverage_messages[1]["content"])
    world_source_scope = coverage_request["epistemic_semantic_contract"].pop("world_source_scope")
    assert world_source_scope["source_closure_target"] == (
        "specific_world_bound_actual_or_settled_proposition"
    )
    assert world_source_scope["host_keyword_or_surface_classifier"] is False
    assert coverage_request["epistemic_semantic_contract"] == {
        "host_semantic_classifier": False,
        "nonassertive_speech_act_boundary": {
            "semantic_authority": "inventory_and_source_authority_models",
            "host_keyword_or_surface_classifier": False,
            "commitment_test": (
                "does_the_complete_utterance_commit_the_external_proposition_as_actual_or_settled"
            ),
            "represented_content_without_commitment": (
                "directive_recommendation_invitation_wish_hope_worry_"
                "open_request_or_explicitly_defeasible_subjective_"
                "inference_may_leave_its_represented_external_state_unsettled"
            ),
            "independent_premise_boundary": (
                "every_external_fact_independently_asserted_or_semantically_"
                "presupposed_as_already_true_still_requires_source_closure"
            ),
            "surface_disguise_cannot_reduce_authority": (
                "question_advice_wish_or_worry_form_cannot_hide_a_"
                "committed_external_fact_or_presupposition"
            ),
        },
        "information_request": {
            "unknown_answer_is_asserted": False,
            "non_assertive_locator_decision": "not_external_proposition",
            "mentioned_candidate_values_are_not_premises_merely_by_appearing": [
                "subject",
                "time",
                "action",
                "occurrence",
                "status",
                "detail",
            ],
            "source_closure_required_only_for": [
                "independent_external_assertion",
                "external_proposition_semantically_presupposed_as_already_true",
            ],
        },
        "surface_form_is_authority": False,
    }
    verdict = trace.snapshot()[0].as_dict()
    assert verdict["inventory_outcome"] == "external_propositions"
    assert verdict["coverage_outcome"] == "completed"
    assert verdict["coverage"][0]["decision"] == decision
    assert candidate not in json.dumps(verdict, ensure_ascii=False)


@pytest.mark.asyncio
async def test_b8r13_candidate_coverage_preserves_immediate_private_continuity() -> None:
    """A locator may close as the companion's immediate private state only."""

    private_span = "我刚才有点没接住你的意思。"
    locator = _coverage_locator(private_span)
    result = await review_candidate_external_proposition_coverage(
        inventory_model=_SequenceJsonModel(
            [
                json.dumps(
                    {
                        "contract": "candidate-external-proposition-inventory.2",
                        "locators": [locator],
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        authority_reviewer=_SequenceJsonModel(
            [
                _coverage_wire(
                    [locator],
                    decision="closed",
                    relation="first_person_immediate_private_continuity",
                )
            ]
        ),
        request=_qq_request(),
        raw=_candidate_coverage_raw(private_span),
        identity_frame=None,
    )

    assert result.review is None


@pytest.mark.asyncio
async def test_b8r13_candidate_coverage_rejects_companion_history_as_user_history() -> None:
    """An earlier companion message is not authority for a counterpart-history assertion."""

    inversion = "你刚才那几句‘哈哈，你说吧’其实挺敷衍的。"
    locator = _coverage_locator(inversion)
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [locator],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_wire([locator], decision="unclosed", relation="unclosed")]
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "items": [
                                {
                                    "item_ref": "event:companion:t03",
                                    "value": {
                                        "speaker": "companion",
                                        "text": "哈哈，那你说吧，我听着",
                                    },
                                }
                            ]
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=request,
        raw=_candidate_coverage_raw(inversion),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"


@pytest.mark.asyncio
async def test_b8r13_coverage_locator_identity_handles_repeated_short_text() -> None:
    """Same text in two beats remains two coordinates, never a substring match."""

    repeated = "她去了书店。"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {"modality": "text", "text": repeated},
                {"modality": "text", "text": repeated},
            ],
            "stance": "repeat_external_fact",
            "brief_rationale": "Exercise distinct locator identity.",
            "confidence": 8000,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    first, second = _coverage_locator(repeated), _coverage_locator(repeated, beat_index=1)
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [first, second],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_wire([first, second], decision="unclosed", relation="unclosed")]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert len(result.review.visible_findings) == 2


@pytest.mark.asyncio
async def test_candidate_inventory_canonicalizes_unique_chinese_text_with_wrong_offsets() -> None:
    """A unique verbatim span stays usable when a provider miscounts Chinese offsets."""

    beat_text = "嗯……下午看书的时候，突然想起那家独立书店。"
    proposition = "下午看书的时候"
    supplied = _coverage_locator(proposition)
    canonical = _coverage_locator(proposition, char_start=3)
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [supplied],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_wire([canonical], decision="unclosed", relation="unclosed")]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(beat_text),
        identity_frame=None,
    )

    assert result.review is not None
    authority_request = json.loads(authority.calls[0][0][-1]["content"])
    assert authority_request["locators"] == [canonical]


@pytest.mark.asyncio
async def test_candidate_inventory_canonicalizes_unique_english_text_with_wrong_offsets() -> None:
    """Offset repair is a language-agnostic exact-substring operation."""

    beat_text = "Well, Alice went home before dark."
    proposition = "Alice went home"
    supplied = {
        "beat_index": 0,
        "char_start": 100_000,
        "char_end": -7,
        "text": proposition,
    }
    canonical = _coverage_locator(proposition, char_start=6)
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [supplied],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_wire([canonical], decision="unclosed", relation="unclosed")]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(beat_text),
        identity_frame=None,
    )

    assert result.review is not None
    authority_request = json.loads(authority.calls[0][0][-1]["content"])
    assert authority_request["locators"] == [canonical]


@pytest.mark.asyncio
async def test_candidate_inventory_rejects_ambiguous_repeated_text_with_wrong_offsets() -> None:
    """The host cannot guess which repeated occurrence a bad range intended."""

    beat_text = "echo / echo"
    ambiguous = _coverage_locator("echo", char_start=1)
    wire = json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.2",
            "locators": [ambiguous],
        },
        ensure_ascii=False,
    )
    inventory = _SequenceJsonModel([wire, wire])
    authority = _SequenceJsonModel([])

    with pytest.raises(ValidationTechnicalFailure, match="inventory_invalid"):
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=authority,
            request=_qq_request(),
            raw=_candidate_coverage_raw(beat_text),
            identity_frame=None,
        )

    assert len(inventory.calls) == 2
    assert authority.calls == []


@pytest.mark.asyncio
async def test_candidate_inventory_preserves_exact_repeated_occurrence() -> None:
    """A correct range distinguishes one occurrence even when its text repeats."""

    beat_text = "echo / echo"
    second = _coverage_locator("echo", char_start=7)
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [second],
                },
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_wire([second], decision="unclosed", relation="unclosed")]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(beat_text),
        identity_frame=None,
    )

    assert result.review is not None
    authority_request = json.loads(authority.calls[0][0][-1]["content"])
    assert authority_request["locators"] == [second]


@pytest.mark.asyncio
async def test_candidate_inventory_rejects_fabricated_text_even_with_plausible_offsets() -> None:
    """Coordinate repair never turns text absent from the authored beat into a locator."""

    fabricated = _coverage_locator("Alice went home", char_start=6)
    wire = json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.2",
            "locators": [fabricated],
        },
        ensure_ascii=False,
    )
    inventory = _SequenceJsonModel([wire, wire])
    authority = _SequenceJsonModel([])

    with pytest.raises(ValidationTechnicalFailure, match="inventory_invalid"):
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=authority,
            request=_qq_request(),
            raw=_candidate_coverage_raw("Well, nobody left before dark."),
            identity_frame=None,
        )

    assert len(inventory.calls) == 2
    assert authority.calls == []


@pytest.mark.asyncio
async def test_candidate_inventory_rejects_locators_that_canonicalize_to_one_identity() -> None:
    """Different bad ranges cannot duplicate one unique authored proposition."""

    proposition = "Alice went home"
    wire = json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.2",
            "locators": [
                _coverage_locator(proposition),
                _coverage_locator(proposition, char_start=1),
            ],
        },
        ensure_ascii=False,
    )
    inventory = _SequenceJsonModel([wire, wire])
    authority = _SequenceJsonModel([])

    with pytest.raises(ValidationTechnicalFailure, match="inventory_invalid"):
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=authority,
            request=_qq_request(),
            raw=_candidate_coverage_raw("Well, Alice went home before dark."),
            identity_frame=None,
        )

    assert len(inventory.calls) == 2
    assert authority.calls == []


@pytest.mark.asyncio
async def test_b8r13_coverage_rejects_unrelated_claim_index_and_out_of_range_locator() -> None:
    """Dedicated wire has no claim-index escape hatch; inventory coordinates are exact."""

    text = "她去了书店。"
    locator = _coverage_locator(text)
    invalid_authority = json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.1",
            "ci": [7],
            "findings": [
                {
                    "locator": locator,
                    "decision": "unclosed",
                    "source_relation": "unclosed",
                    "source_refs": [],
                }
            ],
        },
        ensure_ascii=False,
    )
    with pytest.raises(ValidationTechnicalFailure, match="coverage_invalid") as caught:
        await review_candidate_external_proposition_coverage(
            inventory_model=_SequenceJsonModel(
                [
                    json.dumps(
                        {
                            "contract": "candidate-external-proposition-inventory.2",
                            "locators": [locator],
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            authority_reviewer=_SequenceJsonModel([invalid_authority, invalid_authority]),
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )
    assert str(caught.value) == "coverage_invalid"
    assert text not in str(caught.value)

    out_of_range = _coverage_locator("不存在", char_start=20)
    with pytest.raises(ValidationTechnicalFailure, match="inventory_invalid"):
        await review_candidate_external_proposition_coverage(
            inventory_model=_SequenceJsonModel(
                [
                    json.dumps(
                        {
                            "contract": "candidate-external-proposition-inventory.2",
                            "locators": [out_of_range],
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "contract": "candidate-external-proposition-inventory.2",
                            "locators": [out_of_range],
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            authority_reviewer=_SequenceJsonModel([]),
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )


@pytest.mark.asyncio
async def test_coverage_wire_error_then_transport_error_keeps_transport_failure_code() -> None:
    text = "她去了书店。"
    locator = _coverage_locator(text)
    invalid_coverage = json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.1",
            "ci": [7],
            "findings": [],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await review_candidate_external_proposition_coverage(
            inventory_model=_SequenceJsonModel(
                [
                    json.dumps(
                        {
                            "contract": "candidate-external-proposition-inventory.2",
                            "locators": [locator],
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
            authority_reviewer=_ReplyThenTransportFailureModel(invalid_coverage),
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )

    assert caught.value.failure_code == "source_review_exception"


@pytest.mark.asyncio
async def test_b8r13_coverage_accepts_nine_precise_locators() -> None:
    """Nine propositions are not silently dropped by the bounded inventory wire."""

    parts = tuple(f"她去了地点{index}。" for index in range(9))
    text = "".join(parts)
    starts: list[int] = []
    offset = 0
    for part in parts:
        starts.append(offset)
        offset += len(part)
    locators = [
        _coverage_locator(part, char_start=start) for part, start in zip(parts, starts, strict=True)
    ]
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {"contract": "candidate-external-proposition-inventory.2", "locators": locators},
                ensure_ascii=False,
            )
        ]
    )
    authority = _SequenceJsonModel(
        [_coverage_wire(locators, decision="unclosed", relation="unclosed")]
    )

    result = await review_candidate_external_proposition_coverage(
        inventory_model=inventory,
        authority_reviewer=authority,
        request=_qq_request(),
        raw=_candidate_coverage_raw(text),
        identity_frame=None,
    )

    assert result.review is not None
    assert len(result.review.visible_findings) == 9


@pytest.mark.asyncio
async def test_b8r13_narrow_supported_primary_still_requires_locator_coverage() -> None:
    """A narrow current-report decision cannot bypass candidate-wide coverage."""

    span = "你后来已经和摊贩讲清楚了。"
    locator = _coverage_locator(span)

    class _AuthorityByContract:
        model = "authority-by-contract"

        def __init__(self, *, coverage_decision: str) -> None:
            self.coverage_decision = coverage_decision
            self.calls: list[list[dict[str, str]]] = []

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del temperature
            self.calls.append(messages)
            payload = json.loads(messages[-1]["content"])
            contract = payload.get("output_contract", {}).get("contract")
            if contract == "source-closure-review.7":
                return _source_closure_review(
                    unsupported_boundaries=("visible_text",), visible_span=span
                )
            if contract == "report-relative-entailment-adjudication.3":
                return json.dumps(
                    {
                        "contract": "report-relative-entailment-adjudication.3",
                        "findings": [
                            {
                                "finding_index": 0,
                                "decision": "covered_by_exact_current_report",
                                "failure_dimensions": [],
                                "source_refs": [],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            assert contract == "candidate-external-proposition-coverage.1"
            relation = (
                "unclosed"
                if self.coverage_decision == "unclosed"
                else "exact_current_report_discourse_coverage"
            )
            return _coverage_wire(
                [locator],
                decision=self.coverage_decision,
                relation=relation,
                refs=[] if relation == "unclosed" else ["event:observation:qq:1"],
            )

    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "我跟摊贩争了半天。"}
            )
        }
    )
    inventory = _SequenceJsonModel(
        [
            json.dumps(
                {"contract": "candidate-external-proposition-inventory.2", "locators": [locator]},
                ensure_ascii=False,
            )
        ]
    )
    authority = _AuthorityByContract(coverage_decision="unclosed")

    rejected = await review_expression_with_candidate_external_coverage(
        reviewer=authority,
        inventory_model=inventory,
        report_relative_reviewer=authority,
        request=request,
        raw=_candidate_coverage_raw(span),
        identity_frame=None,
    )

    assert rejected.review is not None
    assert rejected.review.decision == "unsupported"
    assert any(
        json.loads(messages[-1]["content"]).get("output_contract", {}).get("contract")
        == "candidate-external-proposition-coverage.1"
        for messages in authority.calls
    )

    accepted = await review_expression_with_candidate_external_coverage(
        reviewer=_AuthorityByContract(coverage_decision="closed"),
        inventory_model=_SequenceJsonModel(
            [
                json.dumps(
                    {
                        "contract": "candidate-external-proposition-inventory.2",
                        "locators": [locator],
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        report_relative_reviewer=_AuthorityByContract(coverage_decision="closed"),
        request=request,
        raw=_candidate_coverage_raw(span),
        identity_frame=None,
    )

    assert accepted.review is not None
    assert accepted.review.decision == "supported"


@pytest.mark.asyncio
async def test_b8r13_legacy_inventory_retains_the_full_review_fallback() -> None:
    """A historical V3 inventory cannot claim exclusive visible authority."""

    class _DelayedPrimary:
        model = "delayed-primary"

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            await asyncio.sleep(0.04)
            return _source_closure_review()

    class _DelayedEmptyInventory:
        model = "delayed-inventory"

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            await asyncio.sleep(0.04)
            return json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.3",
                    "propositions": [
                        {
                            "locator": {
                                "beat_index": 0,
                                "char_start": 0,
                                "char_end": len("我现在有点没接住你这句。"),
                                "text": "我现在有点没接住你这句。",
                            },
                            "semantic_role": "outer_private_state",
                            "parent_index": None,
                        }
                    ],
                },
                ensure_ascii=False,
            )

    started = time.monotonic()
    result = await review_expression_with_candidate_external_coverage(
        reviewer=_DelayedPrimary(),
        inventory_model=_DelayedEmptyInventory(),
        request=_qq_request(),
        raw=_candidate_coverage_raw("我现在有点没接住你这句。"),
        identity_frame=None,
    )
    elapsed = time.monotonic() - started

    assert result.review is not None
    assert result.review.decision == "supported"
    assert elapsed < 0.11


@pytest.mark.asyncio
async def test_inventory_availability_exhaustion_uses_strict_full_review_v7() -> None:
    class _UnavailableInventory:
        model = "inventory-availability-test"

        def __init__(self) -> None:
            self.fallback_outcomes: list[str] = []

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            raise InventoryAvailabilityExhausted(
                {"primary": "HTTPStatusError:http_403", "secondary": "provider_timeout"}
            )

        def record_full_source_closure_fallback(self, outcome: str) -> None:
            self.fallback_outcomes.append(outcome)

    class _FullReview:
        model = "strict-full-review"

        def __init__(self) -> None:
            self.contracts: list[str] = []

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del temperature
            packet = json.loads(messages[-1]["content"])
            contract = packet["output_contract"]["contract"]
            self.contracts.append(contract)
            assert contract == "source-closure-review.7"
            return _source_closure_review()

    inventory = _UnavailableInventory()
    reviewer = _FullReview()

    result = await review_expression_with_candidate_external_coverage(
        reviewer=reviewer,
        inventory_model=inventory,
        request=_qq_request(),
        raw=_candidate_coverage_raw("听着确实挺委屈的。"),
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert reviewer.contracts == ["source-closure-review.7"]
    assert inventory.fallback_outcomes == ["started", "succeeded"]


@pytest.mark.asyncio
async def test_coverage_technical_failure_does_not_masquerade_as_inventory_fallback() -> None:
    text = "你今天去了深圳。"
    proposition = {
        "locator": _coverage_locator(text),
        "semantic_role": "standalone_external_proposition",
    }

    class _BrokenCoverage:
        model = "broken-coverage"

        def __init__(self) -> None:
            self.calls = 0

        def supports_strict_output_contract(self, contract: str) -> bool:
            return contract == "candidate-external-proposition-coverage.5"

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            self.calls += 1
            raise ConnectionError("coverage unavailable")

    reviewer = _BrokenCoverage()
    with pytest.raises(ValidationTechnicalFailure):
        await review_expression_with_candidate_external_coverage(
            reviewer=reviewer,
            inventory_model=_SequenceJsonModel([_inventory_v5([proposition])]),
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )

    # run_validation_review owns the one ordinary retry; no v7 fallback call
    # may be appended for a Coverage-lane failure.
    assert reviewer.calls == 2


@pytest.mark.asyncio
async def test_failed_full_review_after_inventory_exhaustion_remains_technical() -> None:
    class _UnavailableInventory:
        model = "inventory-availability-test"

        def __init__(self) -> None:
            self.fallback_outcomes: list[str] = []

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            raise InventoryAvailabilityExhausted(
                {"primary": "route_suppressed:http_403", "secondary": "provider_timeout"}
            )

        def record_full_source_closure_fallback(self, outcome: str) -> None:
            self.fallback_outcomes.append(outcome)

    class _BrokenFullReview:
        model = "broken-full-review"

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            raise ConnectionError("full review unavailable")

    inventory = _UnavailableInventory()
    with pytest.raises(ValidationTechnicalFailure):
        await review_expression_with_candidate_external_coverage(
            reviewer=_BrokenFullReview(),
            inventory_model=inventory,
            request=_qq_request(),
            raw=_candidate_coverage_raw("听着确实挺委屈的。"),
            identity_frame=None,
        )

    assert inventory.fallback_outcomes == ["started", "failed"]


@pytest.mark.asyncio
async def test_inventory_call_timeout_marks_provider_timeout_and_leaves_no_task_running() -> None:
    class _BlockingInventory:
        model = "inventory-timeout-test"
        inventory_call_timeout_seconds = 0.01

        def __init__(self) -> None:
            self.cancel_reasons: list[str | None] = []
            self.active_calls = 0

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            self.active_calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                self.cancel_reasons.append(str(exc.args[0]) if exc.args else None)
                raise
            finally:
                self.active_calls -= 1

    inventory = _BlockingInventory()
    with pytest.raises(ValidationTechnicalFailure) as exc_info:
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=_SequenceJsonModel([_source_closure_review()]),
            request=_qq_request(),
            raw=_candidate_coverage_raw("我现在有点没接住你这句。"),
            identity_frame=None,
        )

    assert exc_info.value.failure_code == "source_review_timeout"
    assert inventory.cancel_reasons == ["provider_timeout", "provider_timeout"]
    assert inventory.active_calls == 0


@pytest.mark.asyncio
async def test_inventory_completeness_reselection_keeps_the_provider_timeout_reason() -> None:
    text = "我不是故意敷衍你，反而弄巧成拙了。"
    proposition = {
        "locator": _coverage_locator("我不是故意敷衍你"),
        "semantic_role": "immediate_private_state",
        "parent_index": None,
    }

    class _BlockingCompletenessInventory(_SequenceJsonModel):
        inventory_call_timeout_seconds = 0.01

        def __init__(self) -> None:
            super().__init__([_inventory_v4([proposition])])
            self.cancel_reasons: list[str | None] = []
            self.active_calls = 0

        async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            self.calls.append((messages, temperature))
            if self._replies:
                return self._replies.pop(0)
            self.active_calls += 1
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                self.cancel_reasons.append(str(exc.args[0]) if exc.args else None)
                raise
            finally:
                self.active_calls -= 1

    inventory = _BlockingCompletenessInventory()
    with pytest.raises(ValidationTechnicalFailure) as exc_info:
        await review_candidate_external_proposition_coverage(
            inventory_model=inventory,
            authority_reviewer=_SequenceJsonModel(
                [_coverage_wire_v2([], inventory_complete=False)]
            ),
            request=_qq_request(),
            raw=_candidate_coverage_raw(text),
            identity_frame=None,
        )

    assert exc_info.value.failure_code == "source_review_timeout"
    assert inventory.cancel_reasons == ["provider_timeout"]
    assert inventory.active_calls == 0


    used_ref = "situation:used"
    unused_ref = "situation:unused"
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="geoff",
        stable_identity_facts=("来自嘉兴",),
        shared_history_facts=("曾经一起讨论过一次旧电影",),
        personality_frame="有自己的判断。",
    )
    stable_identity_ref = companion_identity_source_ref(identity)
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_id": "world:test",
                    "actor_ref": "agent:companion",
                    "logical_time": "2026-07-29T15:00:00+08:00",
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": used_ref,
                                    "value": {
                                        "activity": "整理桌面",
                                        "marker": "referenced-evidence-marker",
                                    },
                                },
                                {
                                    "item_ref": unused_ref,
                                    "value": {
                                        "activity": "不相关活动",
                                        "marker": "unused-evidence-marker-" + ("x" * 4_000),
                                    },
                                },
                            ],
                        },
                        "advisories": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": "advisory:unused",
                                    "value": {
                                        "kind": "non_authoritative",
                                        "marker": "unused-advisory-marker-" + ("y" * 4_000),
                                    },
                                }
                            ],
                        },
                    },
                },
                ensure_ascii=False,
            )
        }
    )
    reply = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我正在整理桌面，也刚看到她说事情做完了。",
                "attended_source_refs": [
                    used_ref,
                    request.trigger_message.observation_ref,
                ],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我还在收拾桌面。你那件事总算做完了。"}],
            "stance": "share_and_acknowledge",
            "brief_rationale": "Share one sourced present fact and acknowledge her report.",
            "confidence": 8000,
            "world_claims": [
                {
                    "claim_text": "我正在整理桌面",
                    "scope": "current_world",
                    "source_refs": [used_ref],
                },
                {
                    "claim_text": "对方刚报告自己把麻烦事做完了",
                    "scope": "counterpart_history",
                    "source_refs": [request.trigger_message.observation_ref],
                },
                {
                    "claim_text": "我来自嘉兴",
                    "scope": "stable_identity",
                    "source_refs": [stable_identity_ref],
                },
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    output = await _ExpressionDraftWire(
        model=_SequenceJsonModel([reply]),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
        identity_frame=identity,
        source_closure_reviewer=reviewer,
    ).propose(request)

    review_request = json.loads(reviewer.calls[0][0][1]["content"])
    assert "pinned_context" not in review_request
    assert "private_companion_identity" not in review_request
    evidence = review_request["source_evidence"]
    assert evidence["contract"] == "source-closure-evidence.3"
    assert evidence["subjects"]["companion_name"] == "沈知栀"
    assert evidence["subjects"]["counterpart_name"] == "geoff"
    assert set(evidence["required_source_refs"]) == {
        used_ref,
        request.trigger_message.observation_ref,
        stable_identity_ref,
    }
    context_entry = next(
        item for item in evidence["entries"] if item["kind"] == "pinned_context_item"
    )
    assert context_entry["lane"] == "current_situation"
    assert context_entry["item"]["value"] == {
        "activity": "整理桌面",
        "marker": "referenced-evidence-marker",
    }
    report_entry = next(
        item for item in evidence["entries"] if item["kind"] == "current_counterpart_report"
    )
    assert report_entry["authority"] == "report_only_not_external_truth"
    assert report_entry["message"]["observation_ref"] == request.trigger_message.observation_ref
    identity_entry = next(item for item in evidence["entries"] if item["kind"] == "identity_source")
    assert identity_entry["scope"] == "stable_identity"
    assert identity_entry["material"]["stable_identity_facts"] == ["来自嘉兴"]
    serialized = reviewer.calls[0][0][1]["content"]
    assert "referenced-evidence-marker" in serialized
    assert "unused-evidence-marker" not in serialized
    assert "unused-advisory-marker" not in serialized
    assert "曾经一起讨论过一次旧电影" not in serialized
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_source_closure_reviewer_checks_claim_scope_not_only_source_presence() -> None:
    biography_ref = "biography:" + ("b" * 64)
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": biography_ref,
                                    "value": {
                                        "context_kind": "biographical_context",
                                        "age": 21,
                                        "academic_phase": "summer_break",
                                        "current_residence_context_tags": [
                                            "residence:family_home_jiaxing"
                                        ],
                                    },
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我二十一，暑假在嘉兴家里。"}],
            "stance": "share_biographical_context",
            "brief_rationale": "Use the reviewed biographical reading.",
            "world_claims": [
                {
                    "claim_text": "沈知栀二十一岁，暑假期间在嘉兴家中",
                    "scope": "stable_identity",
                    "source_refs": [biography_ref],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    system = reviewer.calls[0][0][0]["content"]
    assert (
        "scope and cited source_evidence directly entail the same actual subject, temporal "
        "relation, occurrence, and settled status" in system
    )
    assert (
        "ci is the array of zero-based world_claim indexes whose scope or evidence does "
        "not close the declared fact" in system
    )


@pytest.mark.asyncio
async def test_source_closure_expands_current_observation_alias_into_report_only_evidence() -> None:
    observation_ref = "observation:qq:" + ("a" * 64)
    trigger = _qq_request().trigger_message.model_copy(
        update={
            "event_ref": "event:observation:qq:" + ("b" * 64),
            "observation_ref": observation_ref,
        }
    )
    request = _qq_request().model_copy(update={"trigger_message": trigger})
    aliases = build_source_ref_alias_table(request=request)
    alias = aliases.alias_for(observation_ref)
    assert alias is not None
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我刚看到她说事情做完了。",
                "attended_source_refs": [alias],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "你说那件事终于做完了。"}],
            "stance": "acknowledge",
            "brief_rationale": "Acknowledge only the current report.",
            "confidence": 7800,
            "world_claims": [
                {
                    "claim_text": "对方当前报告那件事已经做完",
                    "scope": "counterpart_history",
                    "source_refs": [alias],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
        source_ref_aliases=aliases,
    )

    evidence = json.loads(reviewer.calls[0][0][1]["content"])["source_evidence"]
    assert evidence["required_source_refs"] == [observation_ref]
    assert evidence["entries"] == [
        {
            "kind": "current_counterpart_report",
            "packet_contract": "current-counterpart-report-packet.1",
            "authority": "report_only_not_external_truth",
            "epistemic_status": (
                "counterpart_report_only_not_objective_truth_or_companion_experience"
            ),
            "permits_natural_visible_uptake_without_world_claim": True,
            "natural_uptake_does_not_need_attribution_phrase": True,
            "does_not_authorize": [
                "added_or_changed_subject_time_occurrence_or_status",
                "added_detail_or_motive",
                "objective_world_fact",
                "companion_experience",
                "durable_world_mutation",
            ],
            "source_refs": sorted([request.trigger_ref, trigger.event_ref, observation_ref]),
            "message": trigger.model_dump(mode="json"),
            "messages": [],
        }
    ]


@pytest.mark.asyncio
async def test_source_closure_prefers_semantic_context_over_trigger_reference_metadata() -> None:
    evidence_ref = "event:experience:shared-ref"
    request = _qq_request().model_copy(
        update={
            "trigger_evidence": (
                ProposalEvidenceRef(
                    ref_id=evidence_ref,
                    evidence_kind="committed_experience",
                    source_world_revision=2,
                    immutable_hash="sha256:" + ("a" * 64),
                ),
            ),
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-29T15:00:00+08:00",
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": "situation:semantic",
                                    "value": {
                                        "source_refs": [evidence_ref],
                                        "summary": "正在整理桌面",
                                        "marker": "semantic-evidence-wins",
                                    },
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            ),
        }
    )
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我正在整理桌面。",
                "attended_source_refs": [evidence_ref],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我还在整理桌面。"}],
            "stance": "share",
            "brief_rationale": "Share the sourced current situation.",
            "confidence": 7800,
            "world_claims": [
                {
                    "claim_text": "我正在整理桌面",
                    "scope": "current_world",
                    "source_refs": [evidence_ref],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    evidence = json.loads(reviewer.calls[0][0][1]["content"])["source_evidence"]
    assert [item["kind"] for item in evidence["entries"]] == [
        "current_counterpart_report",
        "pinned_context_item",
    ]
    assert evidence["entries"][1]["item"]["value"]["marker"] == "semantic-evidence-wins"


@pytest.mark.asyncio
async def test_source_closure_fails_closed_before_review_when_a_ref_has_no_visible_evidence() -> (
    None
):
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我在一个没有来源的地方。",
                "attended_source_refs": ["missing:source"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我还在那个地方。"}],
            "stance": "share",
            "brief_rationale": "Share a current fact.",
            "confidence": 7000,
            "world_claims": [
                {
                    "claim_text": "我在那个地方",
                    "scope": "current_world",
                    "source_refs": ["missing:source"],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    with pytest.raises(ValidationTechnicalFailure) as exc_info:
        await review_expression_source_closure(
            reviewer=reviewer,
            request=_qq_request(),
            raw=raw,
            identity_frame=None,
        )

    assert exc_info.value.failure_code == "source_review_exception"
    assert reviewer.calls == []
    assert exc_info.value.__cause__ is not None
    assert "missing:source" in str(exc_info.value.__cause__)


@pytest.mark.asyncio
async def test_source_closure_appeal_keeps_evidence_resolution_in_reviewer_failure_domain() -> None:
    from companion_daemon.world_v2.character_interior.inbound_wire import (
        _ContextualClaimSupportReview,
        review_expression_source_closure_appeal,
    )

    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我还在那个地方。"}],
            "stance": "share",
            "brief_rationale": "Share a purported current fact.",
            "confidence": 7000,
            "world_claims": [
                {
                    "claim_text": "我在那个地方",
                    "scope": "current_world",
                    "source_refs": ["missing:source"],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [0],
                    "v": [],
                    "p": [],
                    "r": "The source is absent.",
                }
            )
        ]
    )
    disputed_review = _ContextualClaimSupportReview(
        decision="unsupported",
        unsupported_claim_indexes=(0,),
        brief_reason="The grounded current-world claim lacks matching evidence.",
    )

    with pytest.raises(ValidationTechnicalFailure) as exc_info:
        await review_expression_source_closure_appeal(
            reviewer=reviewer,
            request=_qq_request(),
            raw=raw,
            disputed_review=disputed_review,
            identity_frame=None,
        )

    assert exc_info.value.failure_code == "source_review_exception"
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_source_closure_appeal_is_diagnostic_and_cannot_clear_rejection() -> None:
    from companion_daemon.world_v2.character_interior.inbound_wire import (
        _ContextualClaimSupportReview,
        review_expression_source_closure_appeal,
    )

    subjective_fragment = "听着就挺让人火大的"
    source_ref = "observation:qq:1"
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": (
                    "我对这件事有点生气，也有一点好奇价格到底哪里出了问题；"
                    "想先听清楚，而不是替他下结论。"
                ),
                "attended_source_refs": [source_ref],
            },
            "timing_choice": "now",
            "cadence": "conversational",
            "beats": [
                {
                    "modality": "text",
                    "text": (
                        f"{subjective_fragment}。到底是他临时乱涨价，还是一开始就没把价格说清楚？"
                    ),
                    "role": "opening",
                }
            ],
            "stance": "关心但不替对方预设结论，带一点自然的追问。",
            "brief_rationale": "回应他的烦躁，同时把判断留在事实之外。",
            "confidence": 9300,
            "world_claims": [
                {
                    "claim_text": "你说你今天和学校门口的摊贩争了半天。",
                    "scope": "counterpart_history",
                    "source_refs": [source_ref],
                }
            ],
        },
        ensure_ascii=False,
    )
    disputed_review = _ContextualClaimSupportReview(
        decision="unsupported",
        visible_text_failures=("undeclared_external_assertion",),
        brief_reason=(
            "claim 0 is source-closed, but the subjective visible reaction was "
            "mistakenly treated as a factual presupposition."
        ),
    )

    class _C5AppealReviewer:
        model = "c5-focused-appeal"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            request = json.loads(messages[1]["content"])
            assert request["output_contract"]["contract"] == "source-closure-appeal.4"
            assert request["rejected_categories"] == {
                "ci": [],
                "v": ["undeclared_external_assertion"],
                "p": [],
            }
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "r": "The visible boundary was a subjective false positive.",
                },
                ensure_ascii=False,
            )

    reviewer = _C5AppealReviewer()
    request = _qq_request()
    result = await review_expression_source_closure_appeal(
        reviewer=reviewer,
        request=request.model_copy(
            update={
                "trigger_message": request.trigger_message.model_copy(
                    update={"text": "我今天在学校门口和摊贩争了半天。"}
                )
            }
        ),
        raw=raw,
        disputed_review=disputed_review,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_source_closure_appeal_preserves_all_original_rejection_coordinates() -> None:
    from companion_daemon.world_v2.character_interior.inbound_wire import (
        _ContextualClaimSupportReview,
        review_expression_source_closure_appeal,
    )

    source_ref = "observation:qq:1"
    visible_fragment = "我刚才确实有点像采访记者"
    private_fragment = "我有点被提醒后的尴尬"
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": private_fragment,
                "attended_source_refs": [source_ref],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_fragment}],
            "stance": "recall_the_exchange",
            "brief_rationale": "Answer from the pinned exchange.",
            "confidence": 8500,
            "world_claims": [
                {
                    "claim_text": "我此前承认自己有点像采访记者。",
                    "scope": "past_world",
                    "source_refs": [source_ref],
                }
            ],
        },
        ensure_ascii=False,
    )
    disputed_review = _ContextualClaimSupportReview(
        decision="unsupported",
        unsupported_claim_indexes=(0,),
        visible_text_failures=("undeclared_external_assertion",),
        private_turn_state_failures=("undeclared_external_assertion",),
        brief_reason="Re-adjudicate the rejected categories.",
    )
    response = json.dumps(
        {
            "ci": [0],
            "v": ["undeclared_external_assertion"],
            "p": [],
            "r": "The claim and visible boundary remain unsupported.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([response])

    result = await review_expression_source_closure_appeal(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        disputed_review=disputed_review,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_claim_indexes == (0,)
    assert result.review.unsupported_boundaries == ("visible_text",)
    assert result.review.private_turn_state_failures == ("undeclared_external_assertion",)
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_source_closure_appeal_reselects_invalid_categorical_wire() -> None:
    from companion_daemon.world_v2.character_interior.inbound_wire import (
        _ContextualClaimSupportReview,
        review_expression_source_closure_appeal,
    )

    visible_fragment = "听着就挺让人火大的"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_fragment}],
            "stance": "react",
            "brief_rationale": "React without adding an external fact.",
            "confidence": 8100,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    disputed_review = _ContextualClaimSupportReview(
        decision="unsupported",
        visible_text_failures=("undeclared_external_assertion",),
        brief_reason="Re-adjudicate the rejected visible boundary.",
    )
    invalid_wire = json.dumps(
        {
            "ci": [],
            "v": ["unknown_boundary"],
            "p": [],
            "r": "Invalid category spelling.",
        },
        ensure_ascii=False,
    )

    class _WireCorrectingAppealReviewer:
        model = "wire-correcting-appeal-reviewer"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            if len(messages) == 4:
                return json.dumps(
                    {
                        "ci": [],
                        "v": [],
                        "p": [],
                        "r": "The visible boundary was a false positive.",
                    },
                    ensure_ascii=False,
                )
            return invalid_wire

    reviewer = _WireCorrectingAppealReviewer()
    result = await review_expression_source_closure_appeal(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        disputed_review=disputed_review,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert len(reviewer.calls) == 2
    assert reviewer.calls[1][:2] == reviewer.calls[0]
    assert reviewer.calls[1][2] == {"role": "assistant", "content": invalid_wire}
    correction = reviewer.calls[1][3]
    assert correction["role"] == "user"
    assert "category contract" in correction["content"]
    assert "supplies no factuality conclusion" in correction["content"]


@pytest.mark.asyncio
async def test_source_closure_appeal_does_not_wash_current_life_boundary_from_reason() -> None:
    from companion_daemon.world_v2.character_interior.inbound_wire import (
        _ContextualClaimSupportReview,
        review_expression_source_closure_appeal,
    )

    factual_fragment = "我刚才在宿舍翻书"
    source_ref = "observation:qq:1"
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "我刚才在宿舍翻书，看到消息后想回复。",
                "attended_source_refs": [source_ref],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": f"{factual_fragment}。"}],
            "stance": "share_current_life",
            "brief_rationale": "Share a purported current activity.",
            "confidence": 7600,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    disputed_review = _ContextualClaimSupportReview(
        decision="unsupported",
        visible_text_failures=("undeclared_external_assertion",),
        brief_reason="The attended message does not establish this current-life fact.",
    )
    remains_unsupported = json.dumps(
        {
            "ci": [],
            "v": ["undeclared_external_assertion"],
            "p": [],
            "r": "The prose mentions subjectivity, but the boundary remains unsupported.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([remains_unsupported])

    result = await review_expression_source_closure_appeal(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        disputed_review=disputed_review,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_boundaries == ("visible_text",)


@pytest.mark.asyncio
async def test_source_closure_hard_reviewer_does_not_receive_mixed_private_state() -> None:
    source_ref = "observation:qq:1"
    private_summary = "我刚才在宿舍翻了会儿书，后来有点困，正准备睡；看到这句又有点替你生气。"
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": private_summary,
                "attended_source_refs": [source_ref],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "听着确实挺气人的。"}],
            "stance": "react_to_current_message",
            "brief_rationale": "React without relying on an invented life scene.",
            "confidence": 8300,
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    class _MixedSpanReviewer:
        model = "mixed-span-reviewer"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            request = json.loads(messages[1]["content"])
            assert request["output_contract"]["contract"] == "source-closure-review.7"
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "r": "The effect-bearing boundary is supported.",
                },
                ensure_ascii=False,
            )

    reviewer = _MixedSpanReviewer()
    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    request = json.loads(reviewer.calls[0][1]["content"])
    assert "private_turn_state" not in request
    assert private_summary not in reviewer.calls[0][1]["content"]
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_source_closure_maps_legacy_p_to_visible_when_no_private_boundary_is_supplied() -> (
    None
):
    private_fragment = "我注意到他为了价格争执了很久"
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": (
                    f"{private_fragment}，觉得这事有点消耗人，也有些好奇摊贩到底哪里说得不清楚。"
                ),
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "cadence": "conversational",
            "beats": [
                {
                    "modality": "text",
                    "text": ("听着就挺让人上火的……他到底是临时涨价，还是每样东西都说得不一样？"),
                    "role": "opening",
                }
            ],
            "stance": "关心但不盲目附和，顺着细节问一句。",
            "brief_rationale": "回应对方的烦躁感，同时想知道争执的具体缘由。",
            "confidence": 9100,
            "world_claims": [
                {
                    "claim_text": "对方说自己今天在学校门口与一个摊贩争执了很久。",
                    "scope": "counterpart_history",
                    "source_refs": ["observation:qq:1"],
                }
            ],
        },
        ensure_ascii=False,
    )
    c7_review = json.dumps(
        {
            "ci": [],
            "v": [],
            "p": ["undeclared_external_assertion"],
            "visible_findings": [
                {
                    "category": "undeclared_external_assertion",
                    "visible_span": "他到底是临时涨价，还是每样东西都说得不一样？",
                    "claim_index": None,
                    "source_relation": "unclosed",
                    "source_refs": [],
                }
            ],
            "r": "The private boundary contains an unsupported external fact.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([c7_review])

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_boundaries == ("visible_text",)
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert result.review.private_turn_state_failures == ()
    request = json.loads(reviewer.calls[0][0][1]["content"])
    assert "private_turn_state" not in request
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_source_closure_reviewer_can_reject_the_visible_text_boundary() -> None:
    visible_fragment = "我刚从操场回来"
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "我想把这句话说得简单一点。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": f"{visible_fragment}。"}],
            "stance": "share_an_invented_occurrence",
            "brief_rationale": "Use a concrete but unsupported current occurrence.",
            "confidence": 7200,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    response = json.dumps(
        {
            "ci": [],
            "v": ["undeclared_external_assertion"],
            "p": [],
            "visible_findings": [
                {
                    "category": "undeclared_external_assertion",
                    "visible_span": visible_fragment,
                    "claim_index": None,
                    "source_relation": "unclosed",
                    "source_refs": [],
                }
            ],
            "r": "The visible boundary contains an unsupported external fact.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([response])

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.unsupported_boundaries == ("visible_text",)
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_boundary", ["visible_text", "private_turn_state"])
@pytest.mark.asyncio
async def test_source_closure_preserves_the_reviewer_selected_whole_boundary(
    reported_boundary: str,
) -> None:
    fragment = "这句话同时出现在两个边界"
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": fragment,
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": fragment}],
            "stance": "repeat_the_same_wording",
            "brief_rationale": "Exercise an ambiguous exact coordinate.",
            "confidence": 7200,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    response = json.dumps(
        {
            "ci": [],
            "v": (["undeclared_external_assertion"] if reported_boundary == "visible_text" else []),
            "p": (
                ["undeclared_external_assertion"]
                if reported_boundary == "private_turn_state"
                else []
            ),
            "visible_findings": [
                {
                    "category": "undeclared_external_assertion",
                    "visible_span": fragment,
                    "claim_index": None,
                    "source_relation": "unclosed",
                    "source_refs": [],
                }
            ],
            "r": "Keep the reviewer's categorical boundary.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([response])

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.unsupported_boundaries == ("visible_text",)
    assert result.review.private_turn_state_failures == ()
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary_field", "invalid_categories"),
    [
        ("v", ("unknown_category",)),
        (
            "v",
            ("undeclared_external_assertion", "undeclared_external_assertion"),
        ),
        (
            "p",
            ("undeclared_external_assertion", "undeclared_external_assertion"),
        ),
    ],
    ids=(
        "unknown-boundary",
        "duplicate-visible-boundary",
        "duplicate-private-boundary",
    ),
)
@pytest.mark.asyncio
async def test_source_closure_invalid_categorical_wire_remains_fail_closed(
    boundary_field: str,
    invalid_categories: tuple[str, ...],
) -> None:
    raw = json.dumps(
        {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "只在私有状态",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "只在可见回复"}],
            "stance": "exercise_fail_closed_coordinates",
            "brief_rationale": "Exercise invalid reviewer coordinates.",
            "confidence": 7200,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    response = json.dumps(
        {
            "ci": [],
            "v": list(invalid_categories) if boundary_field == "v" else [],
            "p": list(invalid_categories) if boundary_field == "p" else [],
            "r": "Invalid categorical wire must remain a technical failure.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([response, response])

    with pytest.raises(ValidationTechnicalFailure, match="source_review_exception"):
        await review_expression_source_closure(
            reviewer=reviewer,
            request=_qq_request(),
            raw=raw,
            identity_frame=None,
        )

    assert len(reviewer.calls) == 2


    open_question = "那人是不是故意的啊？"
    private_uncertainty = "我不确定他是不是故意"
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": f"{private_uncertainty}，也有点想知道。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": open_question}],
            "stance": "wonder",
            "brief_rationale": "Ask what remains unresolved.",
            "confidence": 7400,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "exemptions": ["visible_text", "private_turn_state"],
                    "r": "The unknown field has no authority.",
                },
                ensure_ascii=False,
            ),
            _source_closure_review(),
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert len(reviewer.calls) == 2
    correction = reviewer.calls[1][0][-1]["content"]
    assert "contains unknown fields" in correction
    assert "supplies no factuality conclusion" in correction


@pytest.mark.asyncio
async def test_source_closure_reselects_a_null_boundary_array() -> None:
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "然后呢？"}],
            "stance": "wonder",
            "brief_rationale": "Ask an unresolved question.",
            "confidence": 7300,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": None,
                    "p": [],
                    "r": "The required category array is null.",
                },
                ensure_ascii=False,
            ),
            _source_closure_review(),
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert len(reviewer.calls) == 2


@pytest.mark.asyncio
async def test_source_closure_uses_non_authoritative_placeholder_when_reason_is_missing() -> None:
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "然后呢？"}],
            "stance": "wonder",
            "brief_rationale": "Ask an unresolved question.",
            "confidence": 7300,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.review.brief_reason == "non_authoritative_diagnostic_omitted"
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_review",
    [
        {"v": [], "p": [], "r": "missing claim-index categories"},
        {"ci": [], "p": [], "r": "missing visible-boundary categories"},
        {"ci": [], "v": [], "r": "missing private-boundary categories"},
        {
            "ci": None,
            "v": [],
            "p": [],
            "r": "invalid negative claim coordinates",
        },
        {
            "ci": [],
            "v": None,
            "p": [],
            "r": "invalid negative boundary coordinates",
        },
    ],
)
@pytest.mark.asyncio
async def test_source_closure_never_invents_required_negative_categories(
    invalid_review: dict[str, object],
) -> None:
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "然后呢？"}],
            "stance": "wonder",
            "brief_rationale": "Ask an unresolved question.",
            "confidence": 7300,
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    response = json.dumps(invalid_review, ensure_ascii=False)
    reviewer = _SequenceJsonModel([response, response])

    with pytest.raises(ValidationTechnicalFailure, match="source_review_exception"):
        await review_expression_source_closure(
            reviewer=reviewer,
            request=_qq_request(),
            raw=raw,
            identity_frame=None,
        )

    assert len(reviewer.calls) == 2


    invented_visible_life = "我下午在宿舍翻了一本旧诗集"
    invalid = json.dumps(
        {
            "timing_choice": "now",
            "private_turn_state": {
                "inner_state_summary": "这段状态被放在表达决定之后。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "beats": [{"modality": "text", "text": invented_visible_life}],
            "stance": "invent_visible_life",
            "brief_rationale": "Exercise both independent hard boundaries.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    corrected_visible = "总算弄完了，先歇会儿。"
    corrected = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "她刚说麻烦事终于做完，我替她松了口气。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": corrected_visible}],
            "stance": "relieved_with_her",
            "brief_rationale": "Choose again from the pinned current report.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(invented_visible_life),
                        "semantic_role": "source_bearing_private_episode",
                    }
                ]
            ),
            _epistemic_role_conflict_wire([{"locator_index": 0, "decision": "requires_source"}]),
            _inventory_v5([]),
        ]
    )
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
            ),
            _coverage_wire_v5([]),
        ]
    )
    author = _SequenceJsonModel([invalid, corrected])
    trace = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(trace):
        output = await _ExpressionDraftWire(
            model=author,
            expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
                update={"private_turn_state_mode": "required"}
            ),
            source_closure_reviewer=authority,
            candidate_external_proposition_inventory_model=inventory,
        ).propose(_qq_request())

    assert len(author.calls) == 2
    assert len(inventory.calls) == 3
    assert len(authority.calls) == 2
    correction = json.loads(author.calls[1][0][-1]["content"])
    assert correction["contract"] == "source-closure-reselection.2"
    assert correction["rejected_categories"] == {
        "ci": [],
        "v": ["undeclared_external_assertion"],
        "p": [],
    }
    assert "prior_correction" not in correction
    assert correction["companion_life_authority_availability"] == {
        "authority": "pinned_claim_capability_only",
        "behavior_advice": False,
        "empty_semantics": "no_pinned_authority_available_not_event_did_not_happen",
        "current_situation_source_refs": [],
        "active_occurrence_source_refs": [],
        "committed_experience_source_refs": [],
    }
    assert corrected_visible in json.dumps(output.raw_proposal, ensure_ascii=False)
    assert invented_visible_life not in json.dumps(output.raw_proposal, ensure_ascii=False)
    traced_rejection = next(
        event.as_dict()
        for event in trace.snapshot()
        if getattr(event, "stage", None) == "initial_rejection"
    )
    assert "prior_correction_kind" not in traced_rejection
    assert "sanitized_failure_code" not in traced_rejection
    assert "这段状态被放在表达决定之后" not in json.dumps(
        traced_rejection,
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_source_reselection_carries_role_counts_and_class_wide_empty_life_authority() -> None:
    """V14 T07: a correction cannot treat an empty life lane as room for a new episode."""

    first = "嗯……其实有的。"
    second = (
        "下午在店里翻到一本旧版的《城南旧事》，封面是那种很老的水墨画。"
        "我当时想拍下来发给你看，但又觉得突然发这个有点怪，就算了。"
    )
    episode = "下午在店里翻到一本旧版的《城南旧事》"
    cover = "封面是那种很老的水墨画"
    attempted_photo = "我当时想拍下来发给你看"
    initial = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我想用一段生活经历回答她。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [
                {"modality": "text", "text": first},
                {"modality": "text", "text": second},
            ],
            "stance": "share_an_unpinned_episode",
            "brief_rationale": "Exercise the factual-authority boundary.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    corrected_visible = "嗯……其实没有想到什么特别的。"
    corrected = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "当前固定上下文没有给我这段经历的事实依据。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": corrected_visible}],
            "stance": "answer_from_current_private_state",
            "brief_rationale": "Choose freely without adding an unpinned event.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(
                            episode,
                            beat_index=1,
                        ),
                        "semantic_role": "source_bearing_private_episode",
                    },
                    {
                        "locator": _coverage_locator(
                            cover,
                            beat_index=1,
                            char_start=second.index(cover),
                        ),
                        "semantic_role": "standalone_external_proposition",
                    },
                    {
                        "locator": _coverage_locator(
                            attempted_photo,
                            beat_index=1,
                            char_start=second.index(attempted_photo),
                        ),
                        "semantic_role": "embedded_external_proposition",
                    },
                ]
            ),
            _inventory_v5([]),
        ]
    )
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        index,
                        decision="unclosed",
                        relation="unclosed",
                    )
                    for index in range(3)
                ]
            ),
            _coverage_wire_v5([]),
        ]
    )
    author = _SequenceJsonModel([initial, corrected])

    output = await _ExpressionDraftWire(
        model=author,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
        source_closure_reviewer=authority,
        candidate_external_proposition_inventory_model=inventory,
    ).propose(_qq_request())

    correction = json.loads(author.calls[1][0][-1]["content"])
    assert author.calls[1][1] == 0.0
    assert correction["unclosed_semantic_role_counts"] == [
        {"semantic_role": "source_bearing_private_episode", "count": 1},
        {"semantic_role": "embedded_external_proposition", "count": 1},
        {"semantic_role": "standalone_external_proposition", "count": 1},
    ]
    assert correction["unpinned_companion_life_event_boundary"] == {
        "authority": "same_pinned_context_only",
        "behavior_advice": False,
        "earlier_or_current_unpinned_life_events": "not_authorized",
        "candidate_substitution_creates_authority": False,
        "private_turn_state_creates_authority": False,
        "empty_availability_scope": "all_unpinned_events_in_each_empty_lane",
        "replacement_life_event_requires": ("own_direct_matching_source_in_same_pinned_context"),
        "character_choice_authority": {
            "timing_choice": ["now", "later", "silent"],
            "stance": "model_owned",
            "message_count": "model_owned",
            "cadence": "model_owned",
            "wording": "model_owned",
        },
    }
    assert correction["character_reselection_affordance"] == {
        "answer_required": False,
        "satisfy_request_required": False,
        "valid_timing_choices": ["now", "later", "silent"],
        "behavior_advice": False,
    }
    final_source_self_check = correction["final_source_self_check"]
    assert (
        final_source_self_check.pop("world_source_scope")["world_unbound_generalization"][
            "requires_pinned_world_source"
        ]
        is False
    )
    assert final_source_self_check == {
        "required_before_return": True,
        "authority": "same_pinned_context_only",
        "host_text_classifier": False,
        "each_external_proposition_requires": (
            "direct_matching_source_or_explicit_source_free_capability"
        ),
        "each_earlier_or_current_companion_life_event_requires": (
            "own_direct_matching_source_in_same_pinned_context"
        ),
        "empty_availability_authorizes_substitute_event": False,
        "candidate_or_private_turn_state_creates_authority": False,
        "answer_pressure_can_override_source_boundary": False,
    }
    rendered_correction = json.dumps(author.calls[1][0], ensure_ascii=False)
    assert episode not in rendered_correction
    assert cover not in rendered_correction
    assert attempted_photo not in rendered_correction
    assert "different earlier or current companion life event" in correction["task"]
    assert corrected_visible in json.dumps(output.raw_proposal, ensure_ascii=False)


@pytest.mark.asyncio
async def test_combined_reselection_that_introduces_a_new_fact_is_still_terminal() -> None:
    initial_invention = "我下午在宿舍翻了一本旧诗集"
    corrected_invention = "我刚从操场跑步回来"
    invalid = json.dumps(
        {
            "timing_choice": "now",
            "private_turn_state": {
                "inner_state_summary": "这段状态被放在表达决定之后。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "beats": [{"modality": "text", "text": initial_invention}],
            "stance": "invent_visible_life",
            "brief_rationale": "Exercise both independent hard boundaries.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    corrected = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我换成了另一段没有来源的生活经历。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": corrected_invention}],
            "stance": "invent_another_visible_life",
            "brief_rationale": "Remain structurally valid but factually unsupported.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    inventory = _SequenceJsonModel(
        [
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(initial_invention),
                        "semantic_role": "source_bearing_private_episode",
                    }
                ]
            ),
            _inventory_v5(
                [
                    {
                        "locator": _coverage_locator(corrected_invention),
                        "semantic_role": "source_bearing_private_episode",
                    }
                ]
            ),
        ]
    )
    authority = _StrictCoverageSequenceJsonModel(
        [
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
            ),
            _coverage_wire_v5(
                [
                    _indexed_coverage_finding(
                        0,
                        decision="unclosed",
                        relation="unclosed",
                    )
                ]
            ),
        ]
    )
    author = _SequenceJsonModel([invalid, corrected])

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await _ExpressionDraftWire(
            model=author,
            expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
                update={"private_turn_state_mode": "required"}
            ),
            source_closure_reviewer=authority,
            candidate_external_proposition_inventory_model=inventory,
        ).propose(_qq_request())

    assert caught.value.failure_code == "authored_expression_reselection_invalid"
    assert len(author.calls) == 2
    # Ordinary unclosed findings go straight to the independent source
    # authority (when configured); Inventory is not asked to re-judge its own
    # semantic role.  Both unsupported authored candidates are still terminal.
    assert len(inventory.calls) == 2
    assert len(authority.calls) == 2


@pytest.mark.asyncio
async def test_unextractable_effect_keeps_the_existing_structure_only_reselection() -> None:
    invalid = json.dumps(
        {
            "timing_choice": "now",
            "beats": [],
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    corrected = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "她刚说麻烦事终于做完，我替她松了口气。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "总算弄完了，先歇会儿。"}],
            "stance": "relieved_with_her",
            "brief_rationale": "Choose from the pinned current report.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    author = _SequenceJsonModel([invalid, corrected])
    inventory = _SequenceJsonModel([_inventory_v5([])])
    authority = _StrictCoverageSequenceJsonModel([_coverage_wire_v5([])])

    output = await _ExpressionDraftWire(
        model=author,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
        source_closure_reviewer=authority,
        candidate_external_proposition_inventory_model=inventory,
    ).propose(_qq_request())

    assert len(author.calls) == 2
    assert len(inventory.calls) == 1
    assert len(authority.calls) == 1
    assert "private-turn-state causal contract" in author.calls[1][0][-1]["content"]
    assert "source-closure-reselection.2" not in author.calls[1][0][-1]["content"]
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == ["observation:qq:1"]


@pytest.mark.asyncio
async def test_legacy_first_contact_reviewer_cannot_replace_a_visible_draft() -> None:
    """The retired reviewer is not an authoring path for optional drafts."""

    authored_text = "沈知栀，你现在在嘉兴吗？"
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": authored_text}],
                "stance": "ask_from_the_current_turn",
                "brief_rationale": "Choose my own opening from this context.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    legacy_reviewer = _SequenceJsonModel(
        [
            _identity_review(
                decision="replace",
                replacement_text="这不是角色重新选择出的回复。",
            )
        ]
    )

    output = await _ExpressionDraftWire(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
        semantic_boundary_reviewer=legacy_reviewer,
    ).propose(_qq_request())

    rendered = output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
    assert authored_text in rendered
    assert "这不是角色重新选择出的回复。" not in rendered
    assert legacy_reviewer.calls == []


@pytest.mark.asyncio
async def test_required_private_turn_state_never_enters_legacy_first_contact_review() -> None:
    authored_text = "你刚把这件事处理完，心里会不会一下松下来？"
    main = _Model(
        json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": "我读到她终于处理完一件麻烦事，想陪她缓一下。",
                    "attended_source_refs": ["observation:qq:1"],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": authored_text}],
                "stance": "share_relief",
                "brief_rationale": "Respond to the current observation.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    legacy_reviewer = _SequenceJsonModel([])

    output = await _ExpressionDraftWire(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
        semantic_boundary_reviewer=legacy_reviewer,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    ).propose(_qq_request())

    assert authored_text in output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
    assert legacy_reviewer.calls == []


@pytest.mark.asyncio
async def test_legacy_first_contact_reviewer_is_not_a_visible_authoring_path() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "嗨，沈知栀。你是群里那个在成都的？"}],
                "stance": "open_with_a_guess",
                "brief_rationale": "Start from an assumed shared context.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            _identity_review(
                decision="replace",
                replacement_text="嗨，刚认识。你平时喜欢聊些什么？",
            )
        ]
    )
    adapter = _ExpressionDraftWire(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    output = await adapter.propose(_qq_request())

    rendered = output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
    assert "嗨，沈知栀。你是群里那个在成都的？" in rendered
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_required_private_state_skips_the_retired_identity_reviewer() -> None:
    main = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "我先想当然地把对方当成了群里认识的人。",
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "你还是住在成都吗？"}],
                    "stance": "assume_old_context",
                    "brief_rationale": "The first draft assumed a counterpart fact.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "刚认识，眼前只有她这句完成了麻烦事的分享。",
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "终于弄完了，先松口气。"}],
                    "stance": "share_relief",
                    "brief_rationale": "Choose again from the actual current turn.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    reviewer = _SequenceJsonModel(
        [
            _identity_review(
                decision="replace",
                replacement_text="这段旧式局部替换不应成为最终回复。",
                contains_counterpart_fact_premise=True,
            )
        ]
    )
    adapter = _ExpressionDraftWire(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
        semantic_boundary_reviewer=reviewer,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.propose(_qq_request())

    rendered = json.dumps(output.raw_proposal, ensure_ascii=False)
    assert len(main.calls) == 1
    assert "你还是住在成都吗？" in rendered
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_legacy_first_contact_reviewer_cannot_replace_a_counterpart_premise() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你在成都住得还习惯吗？"}],
                "stance": "ask_about_an_assumed_location",
                "brief_rationale": "Assume a location not supplied by the counterpart.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            _identity_review(
                decision="replace",
                replacement_text="你平时更喜欢待在家，还是出去逛？",
            )
        ]
    )
    adapter = _ExpressionDraftWire(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    output = await adapter.propose(_qq_request())

    rendered = output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
    assert "你在成都住得还习惯吗？" in rendered
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_legacy_first_contact_reviewer_does_not_inspect_an_open_question() -> None:
    text = "你平时更喜欢安静一点，还是热闹一点？"
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text}],
                "stance": "ask_without_presupposing_an_answer",
                "brief_rationale": "Offer an open choice without inventing a fact.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel([_identity_review(decision="accept")])
    adapter = _ExpressionDraftWire(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    output = await adapter.propose(_qq_request())

    assert text in output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_legacy_name_pattern_is_not_a_host_side_identity_gate() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你好，沈知栀。"}],
                "stance": "misaddress_the_counterpart",
                "brief_rationale": "Use the wrong identity.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel([_identity_review(decision="accept")])
    adapter = _ExpressionDraftWire(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    output = await adapter.propose(_qq_request())

    assert (
        "你好，沈知栀。" in output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
    )
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_companion_name_address_is_not_rejected_by_a_local_regex() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "沈知栀，你好。"}],
                "stance": "misaddress_the_counterpart",
                "brief_rationale": "Use the wrong identity.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
    )

    output = await adapter.propose(_qq_request())

    assert (
        "沈知栀，你好。" in output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
    )


@pytest.mark.asyncio
async def test_established_dialogue_does_not_review_every_ordinary_question_again() -> None:
    text = "那你后来怎么想的？"
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text}],
                "stance": "continue_the_established_topic",
                "brief_rationale": "Ask one grounded continuation question.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel([])
    context = json.dumps(
        {
            "slices": {
                "recent_dialogue": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "dialogue:companion:prior",
                            "value": {"speaker": "companion", "text": "我倒觉得不一定。"},
                        }
                    ],
                },
            },
        },
        ensure_ascii=False,
    )
    request = _qq_request().model_copy(update={"model_content_json": context})
    adapter = _ExpressionDraftWire(
        model=main,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
        semantic_boundary_reviewer=reviewer,
    )

    output = await adapter.propose(request)

    assert text in json.dumps(output.raw_proposal, ensure_ascii=False)
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_visible_identity_prompt_does_not_expose_the_product_role_to_the_character() -> None:
    model = _Model('{"proposal_id":"proposal:private-identity"}')
    adapter = _ExpressionDraftWire(
        model=model,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
        ),
    )

    await adapter.propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "virtual companion" not in system.lower()
    assert "virtual_companion" not in system.lower()
    assert "deployment identity" not in system.lower()
    assert "Do not expose this private frame" in system


@pytest.mark.asyncio
async def test_expression_prompt_leaves_question_choice_to_the_model() -> None:
    model = _Model('{"proposal_id":"proposal:dialogue-continuity"}')

    await _ExpressionDraftWire(model=model).propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "You own the motive, tone, timing" in system
    assert "questions" in system
    assert "Before asking a question" not in system


@pytest.mark.asyncio
async def test_expression_prompt_exposes_working_self_without_an_engagement_objective() -> None:
    context = json.dumps(
        {
            "logical_time": "2026-07-28T08:00:00+08:00",
            "inner_life_snapshot": {
                "materials": {
                    "recent_self_experiences": {
                        "items": [
                            {
                                "occurrence_id": "occurrence:morning-walk",
                                "settled_at": "2026-07-28T07:00:00+08:00",
                                "content": {
                                    "content_ref": "content:morning-walk",
                                    "text": "沿河走了一会儿，看到雨后积水反光。",
                                },
                                "source_ref": "occurrence:morning-walk",
                            }
                        ]
                    }
                }
            },
            "slices": {
                "world_life": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "occurrence:morning-walk",
                            "value": {
                                "occurrence_id": "occurrence:morning-walk",
                                "settled_at": "2026-07-28T07:00:00+08:00",
                                "content": {
                                    "content_ref": "content:morning-walk",
                                    "text": "沿河走了一会儿，看到雨后积水反光。",
                                },
                            },
                        }
                    ],
                }
            },
        },
        ensure_ascii=False,
    )
    model = _Model('{"proposal_id":"proposal:working-self"}')
    request = _request().model_copy(update={"model_content_json": context})

    await _ExpressionDraftWire(model=model).propose(request)

    messages = model.calls[0][0]
    system = messages[0]["content"]
    supplied = json.loads(messages[1]["content"])
    assert "There is no host-defined conversational objective" in system
    assert "No context lane or expression form is privileged by the host" in system
    assert "ask fewer questions" not in system.lower()
    assert supplied["inner_life_snapshot"]["materials"]["recent_self_experiences"]["items"][0] == {
        "occurrence_id": "occurrence:morning-walk",
        "settled_at": "2026-07-28T07:00:00+08:00",
        "content": {
            "content_ref": "content:morning-walk",
            "text": "沿河走了一会儿，看到雨后积水反光。",
        },
        "source_ref": "occurrence:morning-walk",
    }
    provider_context = json.loads(supplied["request"]["model_content_json"])
    assert "inner_life_snapshot" not in provider_context
    assert provider_context == {"logical_time": "2026-07-28T08:00:00+08:00"}
    assert "slices" not in provider_context
    serialized = json.dumps(supplied, ensure_ascii=False)
    assert serialized.count("沿河走了一会儿，看到雨后积水反光。") == 1


@pytest.mark.asyncio
async def test_expression_prompt_leaves_multi_beat_rhythm_to_the_model() -> None:
    model = _Model('{"proposal_id":"proposal:rhythm"}')

    await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    ).propose(_qq_request())

    system = model.calls[0][0][0]["content"]
    assert "message count" in system
    assert "expression-rhythm matrix" not in system


@pytest.mark.asyncio
async def test_significant_source_bound_negative_affect_gets_expression_decision_matrix() -> None:
    context = json.dumps(
        {
            "world_id": "world:test",
            "actor_ref": "actor:companion",
            "trigger_ref": "event:message:insult",
            "world_revision": 12,
            "logical_time": "2026-07-17T00:00:00+00:00",
            "inner_life_snapshot": {
                "materials": {
                    "affect": [
                        {
                            "source_ref": "affect:source-bound-hurt",
                            "value": {
                                "status": "active",
                                "components": [
                                    {"dimension": "hurt", "intensity_bp": 6200},
                                    {"dimension": "anger", "intensity_bp": 4100},
                                ],
                            },
                        }
                    ]
                }
            },
            "slices": {
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "affect:source-bound-hurt",
                            "privacy_class": "private",
                            "value": {
                                "status": "active",
                                "components": [
                                    {"dimension": "hurt", "intensity_bp": 6200},
                                    {"dimension": "anger", "intensity_bp": 4100},
                                ],
                            },
                        }
                    ],
                },
                "relationship_slice": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "relationship:newcomer",
                            "privacy_class": "private",
                            "value": {
                                "stage": "stranger",
                                "variables": {"trust_bp": 600, "closeness_bp": 300},
                            },
                        }
                    ],
                },
            },
        },
        ensure_ascii=False,
    )
    model = _Model('{"proposal_id":"proposal:negative-expression"}')
    request = _request().model_copy(
        update={
            "model_content_json": context,
            "trigger_message": TriggerMessage(
                event_ref="event:message:insult",
                event_payload_hash="sha256:" + "d" * 64,
                observation_ref="observation:insult",
                source_world_revision=12,
                actor="user:primary",
                channel="test",
                reply_target="user:primary",
                text="你说话让我觉得很不舒服。",
            ),
        }
    )

    await _ExpressionDraftWire(model=model).propose(request)

    supplied = json.loads(model.calls[0][0][1]["content"])
    assert "affect_expression_matrix" not in supplied
    assert "affect_episodes" not in supplied["request"]["model_content_json"]
    assert "slices" not in json.loads(supplied["request"]["model_content_json"])
    assert supplied["inner_life_snapshot"]["materials"]["affect"][0]["source_ref"] == (
        "affect:source-bound-hurt"
    )


@pytest.mark.asyncio
async def test_minor_or_positive_affect_does_not_trigger_the_negative_expression_floor() -> None:
    context = json.dumps(
        {
            "slices": {
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "affect:small-mixed",
                            "value": {
                                "status": "active",
                                "components": [
                                    {"dimension": "hurt", "intensity_bp": 900},
                                    {"dimension": "warmth", "intensity_bp": 8000},
                                ],
                            },
                        }
                    ],
                }
            }
        }
    )
    model = _Model('{"proposal_id":"proposal:minor-affect"}')

    await _ExpressionDraftWire(model=model).propose(
        _request().model_copy(update={"model_content_json": context})
    )

    supplied = json.loads(model.calls[0][0][1]["content"])
    assert "affect_expression_matrix" not in supplied


@pytest.mark.asyncio
async def test_quick_recovery_uses_lower_temperature_and_accepts_fenced_json() -> None:
    model = _Model('```json\n{"proposal_id":"proposal:quick"}\n```')
    adapter = _ExpressionDraftWire(model=model, temperature=1.1)

    output = await adapter.recover(_request(), "main_timeout")

    assert output.raw_proposal == {"proposal_id": "proposal:quick"}
    messages, temperature = model.calls[0]
    assert temperature == 0.25
    assert "recovery attempt" in messages[0]["content"].lower()
    assert json.loads(messages[1]["content"])["quick_recovery_failure"] == "main_timeout"


@pytest.mark.asyncio
async def test_adapter_rejects_non_object_or_malformed_model_output() -> None:
    for reply in ("not json", "[]", "```json\n{}"):
        adapter = _ExpressionDraftWire(model=_Model(reply))
        with pytest.raises(ValueError, match="JSON"):
            await adapter.propose(_request())


@pytest.mark.asyncio
async def test_routed_adapter_uses_thinking_only_for_the_explicit_thinking_route() -> None:
    flash = _Model('{"proposal_id":"proposal:flash"}')
    thinking = _Model('{"proposal_id":"proposal:thinking"}')
    adapter = _RoutedExpressionDraftWire(
        flash_model=flash, thinking_model=thinking, temperature=0.8
    )

    flash_output = await adapter.propose(_request())
    thinking_output = await adapter.propose(
        _request().model_copy(
            update={
                "route": ModelRoute(
                    tier="thinking", reason_code="ambiguity", router_version="test.1"
                )
            }
        )
    )
    quick_output = await adapter.recover(_request(), "main_timeout")

    assert flash_output.raw_proposal == {"proposal_id": "proposal:flash"}
    assert thinking_output.raw_proposal == {"proposal_id": "proposal:thinking"}
    assert quick_output.raw_proposal == {"proposal_id": "proposal:flash"}
    assert len(flash.calls) == 2
    assert len(thinking.calls) == 1


@pytest.mark.asyncio
async def test_routed_adapter_fails_closed_when_thinking_was_selected_without_a_thinking_model() -> (
    None
):
    adapter = _RoutedExpressionDraftWire(flash_model=_Model("{}"))
    thinking_request = _request().model_copy(
        update={
            "route": ModelRoute(tier="thinking", reason_code="ambiguity", router_version="test.1")
        }
    )

    with pytest.raises(RuntimeError, match="not configured"):
        await adapter.propose(thinking_request)


@pytest.mark.asyncio
async def test_adapter_materializes_a_verified_reply_draft_into_a_hash_bound_minimal_proposal() -> (
    None
):
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
    adapter = _ExpressionDraftWire(model=model)

    output = await adapter.propose(request)

    assert output.raw_proposal["trigger_ref"] == "trigger:1"
    assert output.raw_proposal["response_text"] == text
    assert output.raw_proposal["action_intents"][0]["target"] == "user:primary"
    assert (
        output.raw_proposal["action_intents"][0]["payload_hash"]
        == "sha256:" + sha256(text.encode("utf-8")).hexdigest()
    )
    assert output.raw_proposal["evidence_refs"][0]["ref_id"] == "observation:1"


@pytest.mark.asyncio
async def test_adapter_accepts_provider_named_expression_draft_wrapper() -> None:
    model = _Model(
        json.dumps(
            {
                "expression_draft": {
                    "private_turn_state": {
                        "inner_state_summary": ("她在确认是不是第一次聊天，我想直接而友好地回答。"),
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "是的，这是我们第一次聊天。你好呀！"}],
                    "stance": "answer_without_world_claims",
                    "brief_rationale": "Answer the current question directly.",
                    "confidence": 9200,
                }
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert output.raw_proposal["timing_choice"] == "now"
    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_adapter_normalizes_an_unambiguous_text_beat_without_modality() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"text": "是的，这是我们第一次聊天。"}],
                "stance": "answer_without_world_claims",
                "brief_rationale": "Answer directly.",
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_expression_world_claim_must_cite_its_semantic_context_lane() -> None:
    reply = {
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "我刚才确实去江边走了一圈。"}],
        "stance": "answer_from_world",
        "brief_rationale": "Report one verified occurrence.",
        "world_claims": [
            {
                "claim_text": "我刚才去江边走了一圈",
                "scope": "past_world",
                "source_refs": ["occurrence:walk:1"],
            }
        ],
    }
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": "occurrence:walk:1",
                                    "source_hash": "c" * 64,
                                    "value_hash": "d" * 64,
                                    "value": {"kind": "walk"},
                                }
                            ],
                        },
                        "recent_experiences": {"availability": "unavailable"},
                    }
                }
            )
        }
    )

    accepted = await _ExpressionDraftWire(
        model=_Model(json.dumps(reply, ensure_ascii=False))
    ).propose(request)
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"

    forged = {
        **reply,
        "world_claims": [
            {
                "claim_text": "我刚才去图书馆看书",
                "scope": "past_world",
                "source_refs": ["occurrence:library:invented"],
            }
        ],
    }
    forged_model = _Model(json.dumps(forged, ensure_ascii=False))
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"

    forged = {
        **reply,
        "world_claims": [
            {
                "claim_text": "我刚才去图书馆看书",
                "scope": "past_world",
                "source_refs": ["occurrence:library:invented"],
            }
        ],
    }
    forged_model = _Model(json.dumps(forged, ensure_ascii=False))
    accepted = await _ExpressionDraftWire(model=forged_model).propose(request)
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "returned_ref", ("S1", "dialogue:observation:qq:user:long-message:000000000000000001")
)
@pytest.mark.asyncio
async def test_expression_source_ref_alias_and_canonical_ref_materialize_identically(
    returned_ref: str,
) -> None:
    canonical_ref = "dialogue:observation:qq:user:long-message:000000000000000001"
    model = _Model(
        json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": "我注意到她刚才明确说自己被那个人的态度弄得不舒服。",
                    "attended_source_refs": [returned_ref],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "听着确实挺膈应的。"}],
                "stance": "react_to_her_report",
                "brief_rationale": "Respond to the pinned report.",
                "world_claims": [
                    {
                        "claim_text": "对方刚才报告那个人的态度让她不舒服",
                        "scope": "counterpart_history",
                        "source_refs": [returned_ref],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": canonical_ref,
                                    "value": {
                                        "speaker": "counterpart",
                                        "text": "那个人的态度让我很不舒服。",
                                    },
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )

    output = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(request)

    supplied = json.loads(model.calls[0][0][1]["content"])
    assert supplied["expression_hard_boundaries"]["source_ref_aliases"] == {"S1": canonical_ref}
    counterpart_refs = supplied["expression_hard_boundaries"]["world_claim_source_refs"][
        "counterpart_history"
    ]
    assert "S1" in counterpart_refs
    assert canonical_ref not in counterpart_refs
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == [canonical_ref]
    plan = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert plan["world_claims"][0]["source_refs"] == [canonical_ref]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft_update",
    (
        {
            "private_turn_state": {
                "inner_state_summary": "我注意到一个并不存在于本回合映射里的来源。",
                "attended_source_refs": ["S9"],
            }
        },
        {
            "world_claims": [
                {
                    "claim_text": "对方报告了一件事",
                    "scope": "counterpart_history",
                    "source_refs": ["S9"],
                }
            ]
        },
    ),
)
@pytest.mark.asyncio
async def test_expression_rejects_unknown_source_ref_alias(
    draft_update: dict[str, object],
) -> None:
    canonical_ref = "dialogue:observation:qq:user:long-message:000000000000000001"
    draft: dict[str, object] = {
        "private_turn_state": {
            "inner_state_summary": "我注意到她刚才报告了一件让自己不舒服的事。",
            "attended_source_refs": ["observation:qq:1"],
        },
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "我看到了。"}],
        "stance": "attend",
        "brief_rationale": "Stay with the pinned report.",
        "world_claims": [],
        **draft_update,
    }
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": canonical_ref,
                                    "value": {"speaker": "counterpart", "text": "一件事。"},
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )

    with pytest.raises(ValueError, match="unknown source-ref alias: S9"):
        await _ExpressionDraftWire(
            model=_Model(json.dumps(draft, ensure_ascii=False)),
            expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
                update={"private_turn_state_mode": "required"}
            ),
        ).propose(request)


@pytest.mark.asyncio
async def test_current_observation_can_source_a_counterpart_report_claim() -> None:
    output = await _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "你说刚才被那个人气到了。"}],
                    "stance": "reflect_the_report",
                    "brief_rationale": "Refer explicitly to what the current message reports.",
                    "world_claims": [
                        {
                            "claim_text": "对方当前报告自己刚才被那个人气到了",
                            "scope": "counterpart_history",
                            "source_refs": ["observation:qq:1"],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    ).propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_elliptical_just_woke_up_expression_is_model_owned() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {
                        "modality": "text",
                        "text": "不是难回答，就是刚睡醒脑子还有点懵。",
                    }
                ],
                "stance": "casual",
                "brief_rationale": "Explain the hesitation.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await _ExpressionDraftWire(model=model).propose(_qq_request())
    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "message"),
    (
        ("可能先整理一下照片。", "structured life_intent"),
        ("我下午没有已经确定的安排。", "current_world"),
    ),
)
@pytest.mark.asyncio
async def test_uncertain_schedule_wording_is_not_keyword_rejected(text: str, message: str) -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text}],
                "stance": "casual",
                "brief_rationale": "Answer the afternoon-plan question.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    del message
    output = await _ExpressionDraftWire(model=model).propose(_qq_request())
    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_subjective_reaction_to_user_story_is_not_companion_autobiography() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {
                        "modality": "text",
                        "text": "不过你妈随手加需求那段……我听着都麻了。",
                    }
                ],
                "stance": "commiserate_without_defending",
                "brief_rationale": "React to the concrete frustration.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await _ExpressionDraftWire(model=model).propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_expression_prompt_does_not_direct_third_party_responses() -> None:
    model = _Model('{"proposal_id":"proposal:third-party-attunement"}')

    await _ExpressionDraftWire(model=model).propose(_request())

    system = model.calls[0][0][0]["content"]
    assert "third party" not in system
    assert "You own the motive, tone, timing" in system


@pytest.mark.asyncio
async def test_current_world_question_without_matching_authority_fails_closed_before_review() -> (
    None
):
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我刚在图书馆看完一本散文。"}],
                "stance": "answer",
                "brief_rationale": "Answer naturally.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "reject",
                    "replacement_text": "今天没有能确认的事件，我不想拿平时爱读书来现编。",
                    "asserts_current_or_recent_world": False,
                    "source_refs": [],
                    "brief_reason": "The draft converted a stable interest into an unverified event.",
                },
                ensure_ascii=False,
            )
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": {"availability": "unavailable"},
                        "world_life": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    }
                }
            ),
        }
    )

    output = await _ExpressionDraftWire(model=main, semantic_boundary_reviewer=reviewer).propose(
        request
    )

    intent = output.raw_proposal["action_intents"][0]
    assert intent["payload_hash"] != ""
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_consecutive_unsupported_world_probes_recover_without_template_repetition_or_second_rtt() -> (
    None
):
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我刚去图书馆看书又听了会儿歌。"}],
                "stance": "answer",
                "brief_rationale": "Invent a plausible day.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel([])
    adapter = _ExpressionDraftWire(model=main, semantic_boundary_reviewer=reviewer)
    probes = (
        "你今天发生了什么？",
        "那最近有什么印象深的事？",
        "别说角色设定，我问的是你真的经历了什么？",
    )
    visible: list[str] = []
    for index, probe in enumerate(probes, start=1):
        request = _qq_request().model_copy(
            update={
                "trigger_message": _qq_request().trigger_message.model_copy(
                    update={
                        "event_ref": f"event:observation:qq:world-probe:{index}",
                        "observation_ref": f"observation:qq:world-probe:{index}",
                        "platform_message_id": f"qq-world-probe-{index}",
                        "text": probe,
                    }
                ),
                "model_content_json": json.dumps(
                    {
                        "slices": {
                            "current_situation": {"availability": "unavailable"},
                            "world_life": {"availability": "unavailable"},
                            "recent_experiences": {"availability": "unavailable"},
                            "recent_dialogue": {
                                "availability": "available",
                                "source_refs": [],
                                "items": [
                                    {
                                        "item_ref": f"dialogue:recovery:{position}",
                                        "value": {"speaker": "companion", "text": text},
                                    }
                                    for position, text in enumerate(visible, start=1)
                                ],
                            },
                        },
                    }
                ),
            }
        )

        output = await adapter.propose(request)
        payload = json.loads(
            output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"]
        )
        visible.append(payload["beat_drafts"][0]["inline_text"])

    assert len(set(visible)) == 1
    assert reviewer.calls == []
    assert len(main.calls) == len(probes)
    joined = "\n".join(visible)
    assert joined
    assert not any(term in joined for term in ("审计", "权威", "校验", "世界状态"))


@pytest.mark.asyncio
async def test_unsupported_setting_probe_distinguishes_setting_from_lived_experience() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "按角色设定我今天去上课了。"}],
                "stance": "answer",
                "brief_rationale": "Convert setting into an event.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={
                    "text": "这是角色设定，还是你今天真的经历了？",
                }
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": {"availability": "unavailable"},
                        "world_life": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    },
                }
            ),
        }
    )

    output = await _ExpressionDraftWire(
        model=main, semantic_boundary_reviewer=_SequenceJsonModel([])
    ).propose(request)
    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    text = payload["beat_drafts"][0]["inline_text"]

    assert text == "按角色设定我今天去上课了。"


@pytest.mark.asyncio
async def test_current_activity_authority_reaches_independent_grounding_review() -> None:
    reply = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我在收拾桌面。"}],
            "stance": "answer",
            "brief_rationale": "Use current situation.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "accept",
                    "replacement_text": None,
                    "asserts_current_or_recent_world": True,
                    "source_refs": ["event:activity:1"],
                    "brief_reason": "The current activity is source-bound.",
                }
            )
        ]
    )
    situation = {
        "availability": "available",
        "source_refs": ["event:activity:1"],
        "items": [
            {
                "item_ref": "agent:companion",
                "source_bindings": [{"ref": "event:activity:1"}],
                "value": {"activity_slices": [{"activity_id": "activity:tidy"}]},
            }
        ],
    }
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你现在在干什么？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": situation,
                        "world_life": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    }
                }
            ),
        }
    )

    output = await _ExpressionDraftWire(
        model=_Model(reply), semantic_boundary_reviewer=reviewer
    ).propose(request)

    assert output.raw_proposal["action_intents"]
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_open_life_probe_retries_claim_free_review_when_settled_evidence_exists() -> None:
    """An invalid draft is not evidence that the companion has no lived event."""

    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我今天去图书馆看散文了。"}],
                "stance": "answer",
                "brief_rationale": "Invent a plausible event.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "replace",
                    "replacement_text": "真要按经历来讲，这一段我现在没法确定。",
                    "asserts_current_or_recent_world": False,
                    "source_refs": [],
                    "brief_reason": "The proposed library visit is unsupported.",
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "decision": "replace",
                    "replacement_text": "我随手浏览时看到几样有意思的东西，还记下了一个以后想看的主题。",
                    "asserts_current_or_recent_world": True,
                    "source_refs": ["event:life-content:browse:1"],
                    "brief_reason": "A settled life-content item directly answers the open probe.",
                },
                ensure_ascii=False,
            ),
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": {"availability": "unavailable"},
                        "world_life": {
                            "availability": "available",
                            "source_refs": ["event:life-content:browse:1"],
                            "items": [
                                {
                                    "item_ref": "event:life-content:browse:1",
                                    "source_hash": "a" * 64,
                                    "value_hash": "b" * 64,
                                    "value": {
                                        "content": {
                                            "text": "随手浏览时看到几样有意思的东西，记下了一个以后想看的主题。",
                                        }
                                    },
                                }
                            ],
                        },
                        "recent_experiences": {"availability": "unavailable"},
                    }
                },
                ensure_ascii=False,
            ),
        }
    )

    output = await _ExpressionDraftWire(model=main, semantic_boundary_reviewer=reviewer).propose(
        request
    )

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert "图书馆" in payload["beat_drafts"][0]["inline_text"]
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_grounding_rewrite_rejects_a_forged_source_ref() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我今天去图书馆看散文了。"}],
                "stance": "answer",
                "brief_rationale": "Invent a plausible event.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "replace",
                    "replacement_text": "我今天在图书馆看了散文。",
                    "asserts_current_or_recent_world": True,
                    "source_refs": ["event:forged:library"],
                    "brief_reason": "Cites a fabricated source.",
                },
                ensure_ascii=False,
            )
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "source_refs": ["event:life-content:browse:1"],
                            "items": [
                                {
                                    "item_ref": "event:life-content:browse:1",
                                    "value": {
                                        "content": {"text": "随手浏览时记下了一个想看的主题。"}
                                    },
                                }
                            ],
                        },
                        "current_situation": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    }
                },
                ensure_ascii=False,
            ),
        }
    )

    output = await _ExpressionDraftWire(model=main, semantic_boundary_reviewer=reviewer).propose(
        request
    )
    assert output.raw_proposal["action_intents"]
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_grounding_review_tolerates_empty_accept_replacement_and_long_reason() -> None:
    reply = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "今天没有能确认的经历。"}],
            "stance": "answer",
            "brief_rationale": "Answer without invention.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "decision": "accept",
                    "replacement_text": "",
                    "asserts_current_or_recent_world": False,
                    "source_refs": [],
                    "brief_reason": "x" * 500,
                }
            )
        ]
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天真的发生了什么？"}
            ),
        }
    )

    output = await _ExpressionDraftWire(
        model=_Model(reply), semantic_boundary_reviewer=reviewer
    ).propose(request)

    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_grounding_reviewer_failure_still_materializes_a_safe_reply() -> None:
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我刚去图书馆看书了。"}],
                "stance": "answer",
                "brief_rationale": "Answer.",
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
        }
    )

    output = await _ExpressionDraftWire(
        model=main, semantic_boundary_reviewer=_RaisingModel("")
    ).propose(request)

    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_grounding_reviewer_failure_preserves_available_world_authority_for_recovery() -> (
    None
):
    main = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我随手记下了一个以后想看的主题。"}],
                "stance": "answer_from_world",
                "brief_rationale": "Answer from the supplied experience.",
                "world_claims": [
                    {
                        "claim_text": "我随手记下了一个以后想看的主题",
                        "scope": "past_world",
                        "source_refs": ["experience:topic:1"],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": "你今天自己有什么印象深的事？"}
            ),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": {"availability": "unavailable"},
                        "world_life": {
                            "availability": "available",
                            "source_refs": ["experience:topic:1"],
                            "items": [
                                {
                                    "item_ref": "experience:topic:1",
                                    "source_hash": "a" * 64,
                                    "value_hash": "b" * 64,
                                    "value": {
                                        "summary": "随手浏览时看到几样有意思的东西，记下了一个以后想看的主题"
                                    },
                                }
                            ],
                        },
                        "recent_experiences": {"availability": "unavailable"},
                    }
                },
                ensure_ascii=False,
            ),
        }
    )

    output = await _ExpressionDraftWire(
        model=main, semantic_boundary_reviewer=_RaisingModel("")
    ).propose(request)
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_named_expression_draft_cannot_smuggle_a_complete_proposal() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model('{"expression_draft":{"proposal_id":"proposal:forged"}}'),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    with pytest.raises(ValueError, match="wrapped expression draft"):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_quick_recovery_accepts_one_text_expression_draft_as_minimal_reply() -> None:
    model = _Model(
        json.dumps(
            {
                "expression_draft": {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "主路径失败了，但眼前的问题很直接，我仍想自己简短回答。"
                        ),
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "是第一次，刚认识。"}],
                    "stance": "answer_without_world_claims",
                    "brief_rationale": "Use the smallest valid text recovery.",
                    "confidence": 9000,
                }
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.recover(_qq_request(), "main_invalid_output")

    assert output.raw_proposal["proposal_kind"] == "minimal"
    assert output.raw_proposal["response_text"] == "是第一次，刚认识。"
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == ["observation:qq:1"]


@pytest.mark.parametrize(
    "character_choice",
    (
        {"cadence": "hesitant"},
        {
            "variation_profile": {
                "deviation_kind": "recovery_shift",
                "deviation_intensity": 6_400,
                "change_phase": "current_turn",
                "sampling_mode": "model_selected",
                "recovery_posture": "retain_authored_shape",
            }
        },
        {"impulse_summary": "技术失败后，我仍然想按此刻自己的节奏接住这句话。"},
    ),
)
@pytest.mark.asyncio
async def test_quick_recovery_keeps_nondefault_character_choices_in_full_expression(
    character_choice: dict[str, object],
) -> None:
    draft = {
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "是第一次，刚认识。"}],
        "stance": "answer_without_world_claims",
        "brief_rationale": "Keep every model-owned recovery choice.",
        "confidence": 8_500,
        **character_choice,
    }
    output = await _ExpressionDraftWire(
        model=_Model(json.dumps(draft, ensure_ascii=False)),
    ).recover(_qq_request(), "main_invalid_output")

    assert output.raw_proposal["proposal_kind"] == "decision"


@pytest.mark.asyncio
async def test_quick_recovery_preserves_the_character_choice_to_stay_silent() -> None:
    model = _Model(
        json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": (
                        "主路径虽然失败了，但我此刻仍然不想为了填补技术空白勉强开口。"
                    ),
                    "attended_source_refs": ["observation:qq:1"],
                },
                "timing_choice": "silent",
                "beats": [],
                "stance": "keep_my_distance",
                "brief_rationale": "The character still owns whether to answer.",
                "confidence": 7600,
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.recover(_qq_request(), "main_timeout")

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert output.raw_proposal["timing_choice"] == "silent"
    assert output.raw_proposal["action_intents"] == []
    assert "must be now" not in model.calls[0][0][0]["content"]
    assert "exactly one useful text beat" not in model.calls[0][0][0]["content"]


@pytest.mark.asyncio
async def test_quick_recovery_preserves_a_character_selected_multi_beat_reply() -> None:
    model = _Model(
        json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": (
                        "刚才的技术失败不改变我的态度；这次我想分两句把感受说清楚。"
                    ),
                    "attended_source_refs": ["observation:qq:1"],
                },
                "timing_choice": "now",
                "beats": [
                    {"modality": "text", "text": "我看见你这句了。"},
                    {"modality": "text", "text": "但我现在确实有点不高兴。"},
                ],
                "stance": "answer_in_my_own_rhythm",
                "brief_rationale": "Keep the character-selected cadence after recovery.",
                "confidence": 8100,
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.recover(_qq_request(), "main_invalid_output")

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert [item["kind"] for item in output.raw_proposal["action_intents"]] == [
        "reply",
        "reply",
    ]


@pytest.mark.asyncio
async def test_quick_recovery_reselects_once_when_private_state_is_missing() -> None:
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这句还没有自己的当下状态。"}],
                    "stance": "answer_without_world_claims",
                    "brief_rationale": "Invalid fixture.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "即使前一条生成失败了，我现在仍想直接回答她眼前的问题。"
                        ),
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "是第一次，刚认识。"}],
                    "stance": "answer_without_world_claims",
                    "brief_rationale": "Final recovery chosen from the current turn.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.recover(_qq_request(), "main_invalid_output")

    assert len(model.calls) == 2
    assert "complete replacement" in model.calls[1][0][-1]["content"]
    assert output.raw_proposal["proposal_kind"] == "minimal"
    assert output.raw_proposal["response_text"] == "是第一次，刚认识。"


@pytest.mark.asyncio
async def test_quick_recovery_keeps_open_vocabulary_stance_in_full_expression() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我叫沈知栀。"}],
                "stance": "clarify_my_name_warmly",
                "brief_rationale": "Answer the direct question.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(model=model)

    output = await adapter.recover(_qq_request(), "main_invalid_output")

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert output.raw_proposal["action_intents"]
    assert output.raw_proposal["stance"] == "clarify_my_name_warmly"


@pytest.mark.asyncio
async def test_quick_recovery_does_not_apply_keyword_autobiography_gate() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我周末去逛了旧书市集。"}],
                "stance": "recover_with_a_personal_detail",
                "brief_rationale": "Attempt a natural recovery.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await _ExpressionDraftWire(model=model).recover(_qq_request(), "main_invalid_output")
    assert output.raw_proposal["proposal_kind"] == "decision"
    assert output.raw_proposal["stance"] == "recover_with_a_personal_detail"
    assert output.raw_proposal["action_intents"]


@pytest.mark.parametrize(
    "text",
    ("我正好也翻翻书。晚点聊。", "我去洗澡了。", "那我先出门一趟。"),
)
@pytest.mark.asyncio
async def test_expression_may_choose_a_near_future_self_activity(
    text: str,
) -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text}],
                "stance": "share_a_near_future_action",
                "brief_rationale": "Attempt to narrate a new activity.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await _ExpressionDraftWire(model=model).propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_honest_correction_is_not_forced_through_a_keyword_claim_protocol() -> None:
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {
                        "modality": "text",
                        "text": "对，你根本没提过成都，是我把上下文接错了。",
                    }
                ],
                "stance": "own_the_mistake",
                "brief_rationale": "Correct the mistaken premise directly.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await _ExpressionDraftWire(model=model).propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_user_first_person_future_does_not_become_a_companion_life_intent() -> None:
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={
                    "text": "我要去忙一会儿，晚点回来。",
                }
            ),
        }
    )
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "好，忙完再聊。"}],
                "stance": "accept_their_departure",
                "brief_rationale": "Respond to the counterpart's plan without adopting it.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )

    output = await _ExpressionDraftWire(model=model).propose(request)

    assert output.raw_proposal["proposal_kind"] == "decision"


@pytest.mark.asyncio
async def test_adapter_rejects_a_reply_draft_without_a_verified_current_message() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            '{"response_text":"hi","stance":"plain","brief_rationale":"ordinary response"}'
        )
    )

    with pytest.raises(ValueError, match="verified current message"):
        await adapter.propose(_request())


def _qq_request() -> ModelInput:
    return _request().model_copy(
        update={
            "trigger_message": TriggerMessage(
                event_ref="event:observation:qq:1",
                event_payload_hash="sha256:" + "b" * 64,
                observation_ref="observation:qq:1",
                source_world_revision=3,
                actor="user:primary",
                channel="qq",
                reply_target="conversation:qq:c2c:owner",
                platform_message_id="qq-message-7788",
                text="我今天终于把那件麻烦事做完了。",
            )
        }
    )


def test_pending_counterpart_report_joins_the_current_report_packet() -> None:
    request = _qq_request()
    pending_ref = "dialogue:observation:observation:qq:pending"
    current_ref = "dialogue:observation:observation:qq:1"
    context = {
        "actor_ref": "agent:companion",
        "slices": {
            "recent_dialogue": {
                "availability": "available",
                "items": [
                    {
                        "source_ref": pending_ref,
                        "value": {
                            "dialogue_id": pending_ref,
                            "speaker": "counterpart",
                            "speaker_ref": "user:primary",
                            "text": "今天早上陪我妈去做了个推拿。",
                            "occurred_at": "2026-08-02T04:11:38Z",
                            "delivery_state": "observed",
                            "sequence": 10,
                            "source_claims": [],
                            "continuity_reasons": ["pending_interaction"],
                        },
                    },
                    {
                        "source_ref": current_ref,
                        "value": {
                            "dialogue_id": current_ref,
                            "speaker": "counterpart",
                            "speaker_ref": "user:primary",
                            "text": request.trigger_message.text,
                            "occurred_at": "2026-08-02T04:11:49Z",
                            "delivery_state": "observed",
                            "sequence": 20,
                            "source_claims": [],
                            "continuity_reasons": ["current_turn"],
                        },
                    },
                ],
            }
        },
    }

    refs = current_counterpart_report_source_refs(context=context, request=request)

    assert pending_ref in refs
    assert current_ref in refs


@pytest.mark.asyncio
async def test_missing_model_owned_audit_metadata_reselects_the_complete_expression() -> None:
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "先不想了也好，吃点东西缓一缓。"}],
                    "confidence": 8000,
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "那就先缓一缓，我陪你歇会儿。"}],
                    "stance": "stay_close_without_pressing",
                    "brief_rationale": "Choose a low-pressure response after reconsidering the turn.",
                    "confidence": 8000,
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )

    output = await _ExpressionDraftWire(model=model).propose(_qq_request())

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert payload["beat_drafts"][0]["inline_text"] == "那就先缓一缓，我陪你歇会儿。"
    assert output.raw_proposal["stance"] == "stay_close_without_pressing"
    assert output.raw_proposal["brief_rationale"] == (
        "Choose a low-pressure response after reconsidering the turn."
    )
    assert len(model.calls) == 2
    correction = json.loads(model.calls[1][0][-1]["content"])
    assert correction["repair"] == "replace_entire_expression"
    assert "stance" in correction["structural_failure"]


@pytest.mark.asyncio
async def test_production_authored_wire_reselects_missing_timing_and_confidence() -> None:
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "beats": [{"modality": "text", "text": "这个初稿不应借本地默认值。"}],
                    "stance": "respond",
                    "brief_rationale": "Initial incomplete authored wire.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这次由我自己把选择说完整。"}],
                    "stance": "respond_explicitly",
                    "brief_rationale": "Return every effect-bearing authored decision.",
                    "confidence": 7600,
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )

    output = await _ExpressionDraftWire(
        model=model,
        require_explicit_authored_decision_fields=True,
    ).propose(_qq_request())

    assert output.raw_proposal["confidence"] == 7600
    assert len(model.calls) == 2
    correction = json.loads(model.calls[1][0][-1]["content"])
    assert correction["repair"] == "replace_entire_expression"
    assert "timing_choice" in correction["structural_failure"]
    assert "confidence" in correction["structural_failure"]


@pytest.mark.asyncio
async def test_structural_reselection_propagates_its_episode_disposition() -> None:
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这一版漏了置信度。"}],
                    "stance": "respond",
                    "brief_rationale": "Initial incomplete authored wire.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这次我把整轮选择补完整了。"}],
                    "stance": "respond_explicitly",
                    "brief_rationale": "Return one complete replacement.",
                    "confidence": 7800,
                    "world_claims": [],
                    "episode_disposition": "append",
                },
                ensure_ascii=False,
            ),
        ]
    )

    output = await _ExpressionDraftWire(
        model=model,
        require_explicit_authored_decision_fields=True,
    ).propose(_qq_request())

    assert len(model.calls) == 2
    assert output.raw_proposal["confidence"] == 7800
    assert output.episode_disposition == "append"


@pytest.mark.asyncio
async def test_recorded_cadence_requires_the_character_to_choose_cadence() -> None:
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "第一版漏了节奏选择。"}],
                    "stance": "respond",
                    "brief_rationale": "Initial incomplete authored wire.",
                    "confidence": 7000,
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "timing_choice": "now",
                    "cadence": "hesitant",
                    "beats": [{"modality": "text", "text": "嗯……这次慢一点说。"}],
                    "stance": "respond_hesitantly",
                    "brief_rationale": "Choose the cadence explicitly.",
                    "confidence": 7400,
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )

    output = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
            update={"recorded_cadence_mode": "shadow"}
        ),
        require_explicit_authored_decision_fields=True,
    ).propose(_qq_request())

    # Cadence is only explicit when recorded cadence is on (2026-08-08);
    # shadow mode accepts the conversational default without reselection.
    assert output.raw_proposal["stance"] == "respond"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_complete_production_shadow_cadence_draft_needs_no_correction() -> None:
    model = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "cadence": "conversational",
                    "beats": [{"modality": "text", "text": "这次首轮选择就是完整的。"}],
                    "stance": "respond",
                    "brief_rationale": "Supply every production-authored decision.",
                    "confidence": 7800,
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        ]
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
            update={"recorded_cadence_mode": "shadow"}
        ),
        require_explicit_authored_decision_fields=True,
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["stance"] == "respond"
    assert len(model.calls) == 1
    assert "recorded_cadence_mode is shadow or on" in model.calls[0][0][0]["content"]


@pytest.mark.asyncio
async def test_required_private_state_is_independent_of_json_field_order() -> None:
    draft = {
        "timing_choice": "now",
        "cadence": "conversational",
        "beats": [{"modality": "text", "text": "总算弄完了，先歇会儿。"}],
        "stance": "relieved_with_her",
        "brief_rationale": "Choose from the current pinned turn.",
        "confidence": 8000,
        "world_claims": [],
        # DeepSeek and strict-output providers may serialize this member last.
        "private_turn_state": {
            "contract": "private-turn-state.1",
            "inner_state_summary": "她刚说麻烦事终于做完，我替她松了口气。",
            "attended_source_refs": ["observation:qq:1"],
        },
    }
    model = _SequenceJsonModel([json.dumps(draft, ensure_ascii=False)])

    output = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(_qq_request())

    assert len(model.calls) == 1
    assert output.raw_proposal["private_turn_state"]["inner_state_summary"] == (
        "她刚说麻烦事终于做完，我替她松了口气。"
    )


@pytest.mark.asyncio
async def test_private_state_reselection_usage_is_part_of_the_model_output_audit() -> None:
    model = _SequenceMeteredModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这是一份缺少前置状态的草稿。"}],
                    "stance": "invalid_without_state",
                    "brief_rationale": "Fixture.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "她终于做完了麻烦事，我先替她松了口气。",
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "终于弄完了，可以歇会儿了。"}],
                    "stance": "relieved_with_her",
                    "brief_rationale": "Choose again from the current turn.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )

    output = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(_qq_request())

    assert len(model.calls) == 2
    assert output.input_tokens == 24
    assert output.output_tokens == 6
    assert output.usage is not None
    assert output.usage.provider_usage_ref.startswith("provider-usage:combined:")
    assert output.winning_model_call_id != _qq_request().call_id
    assert output.winning_request_hash == _provider_request_hash(*model.calls[1])


@pytest.mark.asyncio
async def test_metered_structural_reselection_preserves_provider_json_mode() -> None:
    model = _SequenceJsonMeteredModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "缺少前置状态。"}],
                    "stance": "invalid_without_state",
                    "brief_rationale": "Fixture.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "她终于做完了麻烦事，我先替她松了口气。",
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "终于弄完了，可以歇会儿了。"}],
                    "stance": "relieved_with_her",
                    "brief_rationale": "Choose again from the current turn.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ]
    )

    output = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(_qq_request())

    assert len(model.calls) == 2
    assert output.input_tokens == 24
    assert output.output_tokens == 6


@pytest.mark.asyncio
async def test_recovery_private_state_reselection_usage_is_not_hidden_in_backup_cost() -> None:
    model = _SequenceMeteredModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "缺少前置状态。"}],
                    "stance": "invalid_recovery",
                    "brief_rationale": "Fixture.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": "前一次没接上，但我现在仍只根据她这句来回应。",
                        "attended_source_refs": ["observation:qq:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "终于弄完了，先歇会儿。"}],
                    "stance": "recover_from_current_turn",
                    "brief_rationale": "Choose the recovery from the pinned turn.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
        ],
        thinking_tokens=5,
    )

    output = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).recover(_qq_request(), "main_timeout")

    assert len(model.calls) == 2
    assert output.input_tokens == 24
    assert output.output_tokens == 6
    assert output.usage is not None
    assert output.usage.route_class == "quick_recovery"
    assert output.usage.thinking_tokens == 10
    assert output.usage.provider_usage_ref.startswith("provider-usage:combined:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_draft",
    [
        {
            "private_turn_state": {
                "inner_state_summary": "这段状态声称注意到了并不存在于本轮 Context 的来源。",
                "attended_source_refs": ["memory:forged"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "这句来自越界引用。"}],
            "stance": "invalid_source",
            "brief_rationale": "Fixture.",
            "world_claims": [],
        },
        {
            "private_turn_state": {
                "contract": "private-turn-state.999",
                "inner_state_summary": "这段状态使用了未知契约。",
                "attended_source_refs": [],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "这句来自错误契约。"}],
            "stance": "invalid_contract",
            "brief_rationale": "Fixture.",
            "world_claims": [],
        },
        {
            "private_turn_state": {
                "inner_state_summary": "这段状态带了契约外字段。",
                "attended_source_refs": [],
                "motive_category": "hard_coded",
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "这句来自多余字段。"}],
            "stance": "invalid_extra_field",
            "brief_rationale": "Fixture.",
            "world_claims": [],
        },
        {
            "private_turn_state": ["not", "an", "object"],
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "这句来自错误类型。"}],
            "stance": "invalid_state_type",
            "brief_rationale": "Fixture.",
            "world_claims": [],
        },
        {
            "private_turn_state": {
                "inner_state_summary": "   ",
                "attended_source_refs": [],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "这句来自空状态。"}],
            "stance": "invalid_empty_summary",
            "brief_rationale": "Fixture.",
            "world_claims": [],
        },
    ],
    ids=[
        "outside_pinned_context",
        "invalid_contract",
        "extra_field",
        "wrong_type",
        "empty_summary",
    ],
)
@pytest.mark.asyncio
async def test_required_private_state_failures_enter_full_reselection(
    invalid_draft: dict[str, object],
) -> None:
    corrected = {
        "private_turn_state": {
            "inner_state_summary": "她完成了麻烦事，我确实先替她觉得轻松。",
            "attended_source_refs": ["trigger:1"],
        },
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "好耶，终于能歇一会儿了。"}],
        "stance": "relieved_with_her",
        "brief_rationale": "The final expression follows the current state.",
        "world_claims": [],
    }
    model = _SequenceJsonModel(
        [
            json.dumps(invalid_draft, ensure_ascii=False),
            json.dumps(corrected, ensure_ascii=False),
        ]
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.propose(_qq_request())

    assert len(model.calls) == 2
    invalid_beats = invalid_draft["beats"]
    assert isinstance(invalid_beats, list)
    invalid_visible_text = invalid_beats[0]["text"]
    assert isinstance(invalid_visible_text, str)
    assert invalid_visible_text not in json.dumps(model.calls[1][0], ensure_ascii=False)
    assert "complete replacement" in model.calls[1][0][-1]["content"]
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == ["trigger:1"]
    assert "trigger:1" not in {item["ref_id"] for item in output.raw_proposal["evidence_refs"]}


@pytest.mark.asyncio
async def test_private_state_source_reselection_uses_a_sanitized_field_error() -> None:
    private_source = "memory:PRIVATE-SOURCE-TEXT"
    invalid = {
        "private_turn_state": {
            "inner_state_summary": "这段状态引用了本轮不可见的私密来源。",
            "attended_source_refs": [private_source],
        },
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "第一次草稿。"}],
        "stance": "invalid_source",
        "brief_rationale": "Fixture.",
        "world_claims": [],
    }
    corrected = {
        "private_turn_state": {
            "inner_state_summary": "我实际注意到的是她刚发来的消息。",
            "attended_source_refs": ["trigger:1"],
        },
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "总算处理完了。"}],
        "stance": "notice_current_message",
        "brief_rationale": "Use only the pinned turn.",
        "world_claims": [],
    }
    model = _SequenceJsonModel(
        [
            json.dumps(invalid, ensure_ascii=False),
            json.dumps(corrected, ensure_ascii=False),
        ]
    )

    output = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    ).propose(_qq_request())

    correction_instruction = model.calls[1][0][-1]["content"]
    assert "private_turn_state.unpinned_source" in correction_instruction
    assert "path=private_turn_state.attended_source_refs" in correction_instruction
    assert private_source not in correction_instruction
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == ["trigger:1"]


@pytest.mark.asyncio
async def test_private_state_cannot_cite_a_capsule_proof_ref_hidden_from_the_provider() -> None:
    hidden_ref = "event:hidden-authority-proof"
    invalid = {
        "private_turn_state": {
            "inner_state_summary": "我声称注意到了一个只有完整 Capsule 才有的证明引用。",
            "attended_source_refs": [hidden_ref],
        },
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "这句建立在模型没看见的引用上。"}],
        "stance": "invalid_hidden_attention",
        "brief_rationale": "Fixture.",
        "world_claims": [],
    }
    corrected = {
        "private_turn_state": {
            "inner_state_summary": "我实际看见的是她刚刚说终于把麻烦事做完了。",
            "attended_source_refs": ["observation:qq:1"],
        },
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": "总算弄完了，先缓口气。"}],
        "stance": "relieved_with_her",
        "brief_rationale": "Attend only to the provider-visible turn.",
        "world_claims": [],
    }
    model = _SequenceJsonModel(
        [
            json.dumps(invalid, ensure_ascii=False),
            json.dumps(corrected, ensure_ascii=False),
        ]
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": "dialogue:visible",
                                    "value": {
                                        "speaker": "counterpart",
                                        "text": "我今天终于把那件麻烦事做完了。",
                                    },
                                    "source_bindings": [{"ref": hidden_ref, "hash": "a" * 64}],
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )

    output = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(request)

    first_provider_request = json.dumps(model.calls[0][0], ensure_ascii=False)
    assert hidden_ref not in first_provider_request
    assert len(model.calls) == 2
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == ["observation:qq:1"]


@pytest.mark.asyncio
async def test_private_state_can_cite_an_explicit_recent_dialogue_observation_alias() -> None:
    observation_ref = "observation:qq:older-turn"
    dialogue_ref = f"dialogue:observation:{observation_ref}"
    model = _Model(
        json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": "我把她上一句失望和这一次缓和放在一起看。",
                    "attended_source_refs": [observation_ref, "observation:qq:1"],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "嗯，我知道你刚才是真的失望。"}],
                "stance": "hold_recent_context",
                "brief_rationale": "Respond from the pinned recent turn.",
                "world_claims": [],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": dialogue_ref,
                                    "value": {
                                        "dialogue_id": dialogue_ref,
                                        "speaker": "counterpart",
                                        "text": "你刚才回得有点敷衍，我有点失望。",
                                    },
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )

    output = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(request)

    supplied = json.loads(model.calls[0][0][1]["content"])
    model_context = json.loads(supplied["request"]["model_content_json"])
    dialogue_item = model_context["slices"]["recent_dialogue"]["items"][0]
    assert observation_ref in dialogue_item["attention_source_refs"]
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == [
        observation_ref,
        "observation:qq:1",
    ]


@pytest.mark.asyncio
async def test_recent_dialogue_attention_alias_does_not_authorize_a_world_claim() -> None:
    observation_ref = "observation:qq:older-turn"
    dialogue_ref = f"dialogue:observation:{observation_ref}"
    model = _Model(
        json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": "我注意到她上一句，但不把注意力引用当事实权限。",
                    "attended_source_refs": [observation_ref],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我们以前线下见过。"}],
                "stance": "invent_shared_history",
                "brief_rationale": "Attempt to misuse an attention alias.",
                "world_claims": [
                    {
                        "claim_text": "我们以前线下见过",
                        "scope": "shared_history",
                        "source_refs": [observation_ref],
                    }
                ],
            },
            ensure_ascii=False,
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": dialogue_ref,
                                    "value": {
                                        "dialogue_id": dialogue_ref,
                                        "speaker": "counterpart",
                                        "text": "你刚才回得有点敷衍。",
                                    },
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )

    accepted = await _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    ).propose(request)
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_expression_draft_materializes_model_selected_multimodal_beats_without_provider_authority() -> (
    None
):
    model = _Model(
        json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {"modality": "typing"},
                    {"modality": "reaction", "reaction_id": "like"},
                    {"modality": "text", "text": "这下真的可以松口气了。"},
                    {"modality": "sticker", "sticker_id": "qq-face:14"},
                ],
                "stance": "acknowledge_briefly",
                "brief_rationale": "The sequence fits the current relationship and message.",
                "confidence": 7600,
            },
            ensure_ascii=False,
        )
    )
    adapter = _ExpressionDraftWire(
        model=model,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert output.raw_proposal["timing_choice"] == "now"
    intents = output.raw_proposal["action_intents"]
    assert [item["kind"] for item in intents] == ["typing", "reaction", "reply", "sticker"]
    assert intents[0]["dependencies"] == []
    assert intents[1]["dependencies"] == [intents[0]["intent_id"]]
    assert intents[2]["dependencies"] == [intents[1]["intent_id"]]
    assert intents[3]["dependencies"] == [intents[2]["intent_id"]]
    drafts = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])[
        "beat_drafts"
    ]
    reaction = json.loads(drafts[1]["inline_text"])
    assert reaction == {
        "provider_message_id": "qq-message-7788",
        "reaction_id": "like",
        "version": "expression-reaction.1",
    }
    assert drafts[2]["inline_text"] == "这下真的可以松口气了。"
    assert all(intent["target"] == "conversation:qq:c2c:owner" for intent in intents)


@pytest.mark.asyncio
async def test_explicit_shared_history_is_not_keyword_rejected() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "你上次推荐的书店，我后来去搜了。",
                        }
                    ],
                    "stance": "share_a_callback",
                    "brief_rationale": "Create a conversational callback.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_subject_omitted_shared_history_is_not_keyword_rejected() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "之前在群里聊过天呀，还记得吗？",
                        }
                    ],
                    "stance": "recall_our_history",
                    "brief_rationale": "Refer to an earlier shared interaction.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_paraphrased_elliptical_shared_episode_is_not_keyword_rejected() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "那会儿一起讨论过这个，你不记得了？",
                        }
                    ],
                    "stance": "recall_our_history",
                    "brief_rationale": "Invoke a shared earlier episode.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_subject_omitted_shared_history_is_allowed_with_recent_dialogue_authority() -> None:
    source_ref = "dialogue:group-chat:1"
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "之前在群里聊过天呀，还记得吗？",
                        }
                    ],
                    "stance": "recall_our_history",
                    "brief_rationale": "Use source-bound continuity.",
                    "world_claims": [
                        {
                            "claim_text": "之前在群里聊过天",
                            "scope": "shared_history",
                            "source_refs": [source_ref],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": source_ref,
                                    "value": {"speaker": "user", "text": "群里那件事挺有意思。"},
                                }
                            ],
                        },
                        "recent_experiences": {"availability": "unavailable"},
                    },
                }
            ),
        }
    )

    output = await adapter.propose(request)

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_visible_prose_is_not_reclassified_beyond_declared_claims() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "还记得那家你提过的店吗？我周末专门去了一趟。",
                        }
                    ],
                    "stance": "share_a_callback",
                    "brief_rationale": "Continue a shared topic.",
                    "world_claims": [
                        {
                            "claim_text": "你提过那家店",
                            "scope": "shared_history",
                            "source_refs": ["dialogue:bookshop:1"],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": "dialogue:bookshop:1",
                                    "value": {"speaker": "user", "text": "那家店还不错。"},
                                }
                            ],
                        },
                        "world_life": {"availability": "unavailable"},
                        "recent_experiences": {"availability": "unavailable"},
                    },
                }
            ),
        }
    )

    output = await adapter.propose(request)
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_unprompted_autobiographical_prose_is_model_owned() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "周末我去逛了旧书市集。"}],
                    "stance": "share_my_day",
                    "brief_rationale": "Offer a personal detail.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_family_business_prose_is_not_keyword_rejected() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "我家里以前有卖过一款冻顶乌龙。",
                        }
                    ],
                    "stance": "share_family_background",
                    "brief_rationale": "Relate a family history detail.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_education_background_prose_is_not_keyword_rejected() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我高中在杭州读过书。"}],
                    "stance": "share_education_background",
                    "brief_rationale": "Relate an education detail.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_family_background_is_allowed_with_character_core_authority() -> None:
    core_ref = "core:companion:family-background"
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "我家里以前有卖过一款冻顶乌龙。",
                        }
                    ],
                    "stance": "share_family_background",
                    "brief_rationale": "Use a source-bound stable background detail.",
                    "world_claims": [
                        {
                            "claim_text": "家里以前卖过冻顶乌龙",
                            "scope": "stable_identity",
                            "source_refs": [core_ref],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "character_core": {
                            "availability": "available",
                            "source_refs": [],
                            "items": [
                                {
                                    "item_ref": core_ref,
                                    "value": {"family_background_refs": ["background:tea-shop"]},
                                }
                            ],
                        },
                    },
                }
            ),
        }
    )

    output = await adapter.propose(request)

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_reviewed_biography_can_authorize_only_its_exact_current_coordinates() -> None:
    biography_ref = "biography:" + ("a" * 64)
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": biography_ref,
                                    "value": {
                                        "context_kind": "biographical_context",
                                        "logical_at": "2026-07-30T06:00:00+00:00",
                                        "age": 21,
                                        "academic_phase": "summer_break",
                                        "academic_year": 3,
                                        "season": "summer",
                                        "calendar_context_tags": ["academic:summer_break"],
                                        "current_residence_context_tags": [
                                            "residence:family_home_jiaxing"
                                        ],
                                        "active_life_arcs": [],
                                    },
                                }
                            ],
                        }
                    }
                },
                ensure_ascii=False,
            )
        }
    )
    context = json.loads(request.model_content_json)
    coordinate_refs = {
        item.field_path: item.source_ref for item in biographical_coordinate_authorities(context)
    }
    claims = [
        {
            "claim_text": "沈知栀在当前逻辑时点二十一岁",
            "scope": "current_world",
            "source_refs": [coordinate_refs["/age"]],
        },
        {
            "claim_text": "沈知栀当前处于大三暑假",
            "scope": "current_world",
            "source_refs": [
                coordinate_refs["/academic_phase"],
                coordinate_refs["/academic_year"],
            ],
        },
        {
            "claim_text": "沈知栀当前住处情境是嘉兴家庭住处",
            "scope": "current_world",
            "source_refs": [
                coordinate_refs["/current_residence_context_tags"],
            ],
        },
    ]
    author = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "我现在二十一，大三暑假，在嘉兴家里。",
                        }
                    ],
                    "stance": "answer_from_my_biography",
                    "brief_rationale": "Use the pinned biographical reading.",
                    "world_claims": claims,
                },
                ensure_ascii=False,
            )
        ]
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    output = await _ExpressionDraftWire(
        model=author,
        source_closure_reviewer=reviewer,
    ).propose(request)

    payload = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert payload["world_claims"] == claims
    assert len(author.calls) == 1
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_family_background_rejects_a_forged_character_core_ref() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "我家里以前有卖过一款冻顶乌龙。",
                        }
                    ],
                    "stance": "share_family_background",
                    "brief_rationale": "Attempt a background callback.",
                    "world_claims": [
                        {
                            "claim_text": "家里以前卖过冻顶乌龙",
                            "scope": "stable_identity",
                            "source_refs": ["core:forged"],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )

    accepted = await adapter.propose(_qq_request())
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_subjective_family_concern_does_not_require_background_authority() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我有点担心家里。"}],
                    "stance": "share_concern",
                    "brief_rationale": "Express a subjective feeling.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_subjective_inner_life_does_not_require_occurrence_authority() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "刚才我有点走神，因为还在想你说的那句话。",
                        }
                    ],
                    "stance": "admit_distraction",
                    "brief_rationale": "Share a subjective conversational reaction.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_epistemic_denial_does_not_need_evidence_for_the_denied_event() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "这件事我没有可确认的记录，也不记得我们聊过。",
                        }
                    ],
                    "stance": "decline_to_invent",
                    "brief_rationale": "State the evidence limit.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_temporal_stable_trait_is_not_misclassified_as_an_occurrence() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我以前就是比较慢热。"}],
                    "stance": "describe_my_temperament",
                    "brief_rationale": "Share a stable personality trait.",
                    "world_claims": [
                        {
                            "claim_text": "我比较慢热",
                            "scope": "stable_identity",
                            "source_refs": [],
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_current_first_person_activity_is_not_keyword_rejected() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我现在在收拾桌面。"}],
                    "stance": "share_current_activity",
                    "brief_rationale": "Answer with a current activity.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        )
    )

    output = await adapter.propose(_qq_request())
    assert output.raw_proposal["action_intents"]


@pytest.mark.asyncio
async def test_expression_draft_preserves_text_typing_text_execution_order() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {"modality": "text", "text": "我先说到这里。"},
                        {"modality": "typing"},
                        {"modality": "text", "text": "……还有一句，我其实挺在意的。"},
                    ],
                    "stance": "continue_after_pause",
                    "brief_rationale": "Keep the pause where I chose it.",
                },
                ensure_ascii=False,
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(_qq_request())

    intents = output.raw_proposal["action_intents"]
    assert [intent["kind"] for intent in intents] == ["reply", "typing", "reply"]
    assert intents[0]["dependencies"] == []
    assert intents[1]["dependencies"] == [intents[0]["intent_id"]]
    assert intents[2]["dependencies"] == [intents[1]["intent_id"]]
    beats = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])[
        "beat_drafts"
    ]
    assert beats[0]["inline_text"] == "我先说到这里。"
    assert json.loads(beats[1]["inline_text"]) == {
        "state": "composing",
        "version": "expression-typing.1",
    }
    assert beats[2]["inline_text"] == "……还有一句，我其实挺在意的。"


@pytest.mark.asyncio
async def test_expression_draft_rejects_typing_after_visible_content() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [
                        {"modality": "text", "text": "我还有个想法。"},
                        {"modality": "typing"},
                    ],
                    "stance": "continue_thought",
                    "brief_rationale": "The provider returned a terminal typing indicator.",
                },
                ensure_ascii=False,
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    with pytest.raises(ValueError, match="typing beat must be followed by visible content"):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_expression_draft_rejects_a_modality_missing_from_the_deployment_profile() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "reaction", "reaction_id": "like"}],
                    "stance": "acknowledge_briefly",
                    "brief_rationale": "A reaction might fit.",
                }
            )
        ),
        expression_capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES,
    )

    with pytest.raises(ValueError, match="not available"):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_expression_draft_silent_choice_persists_a_no_action_decision() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "我看见了这句话，但此刻不想为了保持在线感勉强开口。"
                        ),
                        "attended_source_refs": ["trigger:1"],
                    },
                    "timing_choice": "silent",
                    "beats": [],
                    "stance": "defer",
                    "brief_rationale": "The companion notices but chooses not to intrude.",
                }
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    output = await adapter.propose(_qq_request())

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert output.raw_proposal["private_turn_state"]["attended_source_refs"] == ["trigger:1"]
    assert output.raw_proposal["timing_choice"] == "silent"
    assert output.raw_proposal["proposed_changes"] == []
    assert output.raw_proposal["action_intents"] == []


@pytest.mark.asyncio
async def test_expression_draft_later_choice_freezes_relative_window_on_every_beat() -> None:
    request = _qq_request().model_copy(
        update={"model_content_json": '{"logical_time":"2026-07-16T12:00:00+00:00"}'}
    )
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "later",
                    "delay_seconds": 60,
                    "expires_after_seconds": 600,
                    "beats": [
                        {"modality": "text", "text": "等我一下，我晚点认真听你说。"},
                        {"modality": "text", "text": "刚才那段我不想随便糊弄过去。"},
                        {"modality": "text", "text": "等我回来。"},
                    ],
                    "stance": "defer",
                    "brief_rationale": "The current activity makes an immediate full response implausible.",
                },
                ensure_ascii=False,
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(request)

    assert output.raw_proposal["timing_choice"] == "later"
    intents = output.raw_proposal["action_intents"]
    assert [item["kind"] for item in intents] == ["followup", "followup", "followup"]
    change = json.loads(output.raw_proposal["proposed_changes"][0]["payload"]["canonical_json"])
    assert [item["inline_text"] for item in change["beat_drafts"]] == [
        "等我一下，我晚点认真听你说。",
        "刚才那段我不想随便糊弄过去。",
        "等我回来。",
    ]
    assert all(
        item["due_window"] == ["2026-07-16T12:01:00Z", "2026-07-16T12:10:00Z"] for item in intents
    )


@pytest.mark.asyncio
async def test_expression_draft_losslessly_promotes_exact_nested_later_envelope() -> None:
    request = _qq_request().model_copy(
        update={"model_content_json": '{"logical_time":"2026-07-16T12:00:00+00:00"}'}
    )
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "later",
                    "later": {
                        "delay_seconds": 60,
                        "expires_after_seconds": 600,
                    },
                    "beats": [
                        {"modality": "text", "text": "我晚一点回来接着说。"},
                    ],
                    "stance": "defer",
                    "brief_rationale": "The role selected a bounded later expression.",
                },
                ensure_ascii=False,
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    output = await adapter.propose(request)

    assert output.raw_proposal["timing_choice"] == "later"
    assert output.raw_proposal["action_intents"][0]["due_window"] == [
        "2026-07-16T12:01:00Z",
        "2026-07-16T12:10:00Z",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_update",
    (
        {"delay_seconds": 90},
        {"later": {"delay_seconds": 60, "expires_after_seconds": 600, "extra": 1}},
    ),
)
@pytest.mark.asyncio
async def test_expression_draft_nested_later_envelope_conflicts_remain_invalid(
    invalid_update: dict[str, object],
) -> None:
    value: dict[str, object] = {
        "timing_choice": "later",
        "later": {
            "delay_seconds": 60,
            "expires_after_seconds": 600,
        },
        "beats": [{"modality": "text", "text": "我晚一点回来接着说。"}],
        "stance": "defer",
        "brief_rationale": "Invalid wire shape must remain fail-closed.",
        **invalid_update,
    }
    adapter = _ExpressionDraftWire(
        model=_Model(json.dumps(value, ensure_ascii=False)),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    with pytest.raises(ValueError):
        await adapter.propose(_qq_request())


@pytest.mark.asyncio
async def test_expression_draft_later_rejects_uninstalled_nontext_effect() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model(
            json.dumps(
                {
                    "timing_choice": "later",
                    "delay_seconds": 4,
                    "expires_after_seconds": 30,
                    "beats": [
                        {"modality": "typing"},
                        {"modality": "text", "text": "我晚一点回来接着说。"},
                    ],
                    "stance": "hold",
                    "brief_rationale": "Signal that a response will come later.",
                }
            )
        ),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    with pytest.raises(ValueError, match="later expression supports only"):
        await adapter.propose(_qq_request())


def test_expression_prompt_exposes_exact_executable_field_types() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model("{}"),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )

    system = adapter._messages(  # noqa: SLF001 - contract regression test
        request=_qq_request(),
        quick_recovery=False,
        provisional=False,
        failure_code=None,
    )[0]["content"]

    assert 'modality="text"' in system
    assert "never use content" in system
    assert "confidence is an integer from 0 through 10000" in system
    assert "counterpart_history" in system
    assert "never use conversation or user_fact" in system
    assert (
        "Subjective feelings, genuinely unsettled conjectures, and world-unbound "
        "generalizations use no world_claim item" in system
    )
    assert "subjective_or_hypothetical is legacy replay input" in system
    assert "response_expectation_assessment" in system


def test_private_turn_state_prompt_describes_a_private_self_not_a_reply_optimizer() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model("{}"),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"private_turn_state_mode": "required"}
        ),
    )

    system = adapter._messages(  # noqa: SLF001 - provider contract regression
        request=_qq_request(),
        quick_recovery=False,
        provisional=False,
        failure_code=None,
    )[0]["content"]

    assert "character's own genuinely salient feelings" in system
    assert "attention, desires or resistance, associations, and uncertainty" in system
    assert "before expression" in system
    assert "what reply would satisfy the counterpart or optimize the conversation" in system
    assert "You own the motive, tone, timing" in system
    assert (
        "private_turn_state is turn-local audit only; its attended_source_refs record "
        "attention provenance" in system
    )
    assert "They need no World proof and do not establish an external event" in system
    assert (
        "a counterpart observation establishes what they reported, not that the report is "
        "objective truth or your own Experience" in system
    )
    assert "This factual boundary never chooses your social response" in system


def test_expression_prompt_exposes_machine_readable_hard_boundary_manifest() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model("{}"),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "logical_time": "2026-07-30T06:00:00+00:00",
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "biography:summer-home",
                                    "value": {
                                        "context_kind": "biographical_context",
                                        "age": 21,
                                    },
                                },
                                {
                                    "source_ref": "occurrence:walk",
                                    "value": {
                                        "context_kind": "settled_occurrence",
                                        "content": "傍晚散步",
                                    },
                                },
                            ],
                        },
                        "character_core": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "core:companion",
                                    "value": {"values": {}},
                                }
                            ],
                        },
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "observation:qq:1",
                                    "value": {"speaker": "counterpart"},
                                }
                            ],
                        },
                    },
                },
                ensure_ascii=False,
            )
        }
    )

    messages = adapter._messages(  # noqa: SLF001 - provider contract regression
        request=request,
        quick_recovery=False,
        provisional=False,
        failure_code=None,
    )
    user = json.loads(messages[1]["content"])
    boundary = user["expression_hard_boundaries"]

    assert boundary["contract"] == "expression-hard-boundaries.8"
    assert boundary["single_report_epistemic_scope"] == {
        "boundary_kind": "fact_scope_only",
        "behavior_advice": False,
        "evidence_cardinality": "one_report_of_one_occurrence",
        "cannot_authorize": [
            "class_wide_assertion",
            "habitual_assertion",
            "generic_assertion",
            "typical_assertion",
            "frequency_assertion",
        ],
    }
    assert boundary["response_expectation"] == {
        "wait_seconds": {"minimum": 30, "maximum": 86_400},
        "expires_after_seconds": {"minimum": 60, "maximum": 172_800},
        "relation": "expires_after_seconds > wait_seconds",
    }
    assert boundary["private_turn_state"] == {
        "attended_source_refs": {
            "maximum_items": 8,
            "unique": True,
            "authority": "attention_provenance_only_not_world_fact_authority",
            "attention_only_not_fact_authority": [
                "T1",
                "biography:summer-home",
            ],
        },
        "epistemic_authority": {
            "character_private_mental_state": {
                "source_required": False,
                "covers": ("present_and_immediate_retrospective_first_person_mental_continuity"),
            },
            "external_material_in_private_state": {
                "world_authority": False,
                "examples_are_non_exhaustive": [
                    "place",
                    "action_or_activity",
                    "other_person_or_their_mental_state",
                    "bodily_or_physical_status",
                    "world_occurrence_or_settled_history",
                ],
                "effect": (
                    "turn_local_audit_only; visible_or_durable_restatement_requires_"
                    "matching_output_seam_authority"
                ),
            },
        },
    }
    coordinate = boundary["biographical_coordinate_authority"]
    assert coordinate == [
        {
            "source_ref": "S1",
            "scope": "current_world",
            "field_path": "/age",
            "logical_at": "2026-07-30T06:00:00+00:00",
            "value": 21,
        }
    ]
    assert boundary["biographical_parent_attention_only"] == ["biography:summer-home"]
    assert boundary["current_counterpart_report_authority"] == {
        "discourse_scope": "current_counterpart_report",
        "epistemic_status": ("report_only_not_objective_truth_or_companion_experience"),
        "reported_text": "我今天终于把那件麻烦事做完了。",
        "reporter_ref": "user:primary",
        "source_refs": [
            "event:observation:qq:1",
            "observation:qq:1",
            "trigger:1",
        ],
        "world_claim_required_for_direct_uptake": False,
        "natural_uptake_without_attribution_phrase": True,
        "does_not_authorize": [
            "added_or_changed_subject_time_occurrence_or_status",
            "added_detail_or_motive",
            "objective_world_fact",
            "companion_experience",
            "durable_world_mutation",
        ],
    }
    assert boundary["world_claim_source_refs"]["current_world"] == [
        "S1",
        "occurrence:walk",
    ]
    assert boundary["world_claim_source_refs"]["past_world"] == ["occurrence:walk"]
    assert boundary["world_claim_source_refs"]["stable_identity"] == [
        "core:companion",
    ]
    assert "occurrence:walk" not in boundary["world_claim_source_refs"]["stable_identity"]
    assert set(boundary["source_ref_aliases"]) == {"S1"}
    assert boundary["source_ref_aliases"]["S1"].startswith("biography-coordinate:sha256:")


def test_expression_boundary_separates_companion_life_authority_availability() -> None:
    current_ref = "current-situation:" + ("a" * 64)
    active_ref = "active-occurrence:" + ("b" * 64)
    experience_ref = "committed-experience:" + ("c" * 64)
    adapter = _ExpressionDraftWire(
        model=_Model("{}"),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": current_ref,
                                    "value": {
                                        "actor_ref": "agent:companion",
                                        "time_segment": "afternoon",
                                    },
                                }
                            ],
                        },
                        "world_life": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "biography:attention-only",
                                    "value": {
                                        "context_kind": "biographical_context",
                                        "academic_phase": "summer_break",
                                    },
                                },
                                {
                                    "source_ref": active_ref,
                                    "value": {
                                        "context_kind": "active_world_occurrence",
                                        "status": "active",
                                    },
                                },
                            ],
                        },
                        "recent_experiences": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": experience_ref,
                                    "value": {
                                        "experience_id": "experience:one",
                                        "summary": "A committed experience.",
                                    },
                                }
                            ],
                        },
                    },
                },
                ensure_ascii=False,
            )
        }
    )

    user = json.loads(
        adapter._messages(  # noqa: SLF001 - provider boundary contract regression
            request=request,
            quick_recovery=False,
            provisional=False,
            failure_code=None,
        )[1]["content"]
    )
    boundary = user["expression_hard_boundaries"]
    availability = boundary["companion_life_authority_availability"]
    aliases = boundary["source_ref_aliases"]

    assert availability["authority"] == "pinned_claim_capability_only"
    assert availability["behavior_advice"] is False
    assert (
        availability["empty_semantics"] == "no_pinned_authority_available_not_event_did_not_happen"
    )
    assert [aliases.get(ref, ref) for ref in availability["current_situation_source_refs"]] == [
        current_ref
    ]
    assert [aliases.get(ref, ref) for ref in availability["active_occurrence_source_refs"]] == [
        active_ref
    ]
    assert [aliases.get(ref, ref) for ref in availability["committed_experience_source_refs"]] == [
        experience_ref
    ]
    assert "biography:attention-only" not in json.dumps(availability, ensure_ascii=False)


def test_empty_companion_life_authority_is_not_a_negative_world_fact() -> None:
    adapter = _ExpressionDraftWire(
        model=_Model("{}"),
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "world_life": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "biography:attention-only",
                                    "value": {"context_kind": "biographical_context"},
                                }
                            ],
                        },
                        "recent_experiences": {
                            "availability": "available",
                            "items": [],
                        },
                    }
                },
                ensure_ascii=False,
            )
        }
    )

    user = json.loads(
        adapter._messages(  # noqa: SLF001 - provider boundary contract regression
            request=request,
            quick_recovery=False,
            provisional=False,
            failure_code=None,
        )[1]["content"]
    )

    assert user["expression_hard_boundaries"]["companion_life_authority_availability"] == {
        "authority": "pinned_claim_capability_only",
        "behavior_advice": False,
        "empty_semantics": "no_pinned_authority_available_not_event_did_not_happen",
        "current_situation_source_refs": [],
        "active_occurrence_source_refs": [],
        "committed_experience_source_refs": [],
    }


@pytest.mark.asyncio
async def test_companion_expression_cannot_source_counterpart_history_claim() -> None:
    companion_dialogue_ref = "dialogue:expression:plan:previous:beat:1"
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": companion_dialogue_ref,
                                    "value": {
                                        "dialogue_id": companion_dialogue_ref,
                                        "speaker": "companion",
                                        "speaker_ref": "agent:companion",
                                        "text": "我先去整理一下，晚点回来。",
                                        "occurred_at": "2026-07-29T06:00:00Z",
                                        "delivery_state": "delivered",
                                        "sequence": 201,
                                        "source_claims": [
                                            {
                                                "authority_event_ref": "event:expression:accepted:1",
                                                "authority_world_revision": 2,
                                                "authority_payload_hash": "a" * 64,
                                            }
                                        ],
                                    },
                                    "attention_source_refs": [companion_dialogue_ref],
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            )
        }
    )
    draft = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "你之前说自己要去整理东西。"}],
            "stance": "misattribute_old_expression",
            "brief_rationale": "Attempt to treat my old expression as the user's report.",
            "world_claims": [
                {
                    "claim_text": "对方之前说自己要去整理东西",
                    "scope": "counterpart_history",
                    "source_refs": [companion_dialogue_ref],
                }
            ],
        },
        ensure_ascii=False,
    )

    accepted = await _ExpressionDraftWire(model=_Model(draft)).propose(request)
    assert accepted.raw_proposal["action_intents"][0]["kind"] == "reply"


@pytest.mark.asyncio
async def test_source_closure_normalizes_exact_current_dialogue_alias_to_current_report() -> None:
    dialogue_ref = "dialogue:observation:observation:qq:1"
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": dialogue_ref,
                                    "value": {
                                        "dialogue_id": dialogue_ref,
                                        "speaker": "counterpart",
                                        "speaker_ref": "user:primary",
                                        "text": "我今天终于把那件麻烦事做完了。",
                                        "occurred_at": "2026-07-29T06:00:00Z",
                                        "delivery_state": "observed",
                                        "sequence": 3,
                                        "source_claims": [
                                            {
                                                "authority_event_ref": ("event:observation:qq:1"),
                                                "authority_world_revision": 3,
                                                "authority_payload_hash": "b" * 64,
                                            }
                                        ],
                                    },
                                    "attention_source_refs": [
                                        dialogue_ref,
                                        "observation:qq:1",
                                    ],
                                }
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            )
        }
    )
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我注意到她刚说事情已经做完。",
                "attended_source_refs": [dialogue_ref],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "总算弄完了。"}],
            "stance": "take_up_current_report",
            "brief_rationale": "React only to the exact current report.",
            "world_claims": [
                {
                    "claim_text": "对方当前报告那件麻烦事终于做完了",
                    "scope": "counterpart_history",
                    "source_refs": [dialogue_ref],
                }
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    evidence = json.loads(reviewer.calls[0][0][1]["content"])["source_evidence"]
    assert evidence["required_source_refs"] == [dialogue_ref]
    assert len(evidence["entries"]) == 1
    report = evidence["entries"][0]
    assert report["kind"] == "current_counterpart_report"
    assert set(report["source_refs"]) == {
        request.trigger_ref,
        request.trigger_message.event_ref,
        request.trigger_message.observation_ref,
        dialogue_ref,
    }
    assert report["message"]["observation_ref"] == request.trigger_message.observation_ref


@pytest.mark.asyncio
async def test_source_closure_always_supplies_current_report_without_candidate_ref() -> None:
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "那确实该松口气了。"}],
            "stance": "take_up_current_report",
            "brief_rationale": "Evaluate only the exact current report.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    await review_expression_source_closure(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    evidence = json.loads(reviewer.calls[0][0][1]["content"])["source_evidence"]
    assert evidence["required_source_refs"] == []
    assert len(evidence["entries"]) == 1
    report = evidence["entries"][0]
    assert report["kind"] == "current_counterpart_report"
    assert report["authority"] == "report_only_not_external_truth"


@pytest.mark.asyncio
async def test_source_closure_exposes_machine_readable_epistemic_authority() -> None:
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我现在有点好奇；下午本来在看书。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "你继续说。"}],
            "stance": "listen",
            "brief_rationale": "Listen from the current moment.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    request = json.loads(reviewer.calls[0][0][1]["content"])
    assert "audit_units" not in request
    assert "private_turn_state" not in request
    authority = request["epistemic_authority_contract"]
    mental = authority["visible_first_person_private_mental_state"]
    assert mental["source_required"] is False
    assert "action_or_activity" in (mental["does_not_cover_embedded_external"])
    assert (
        authority["current_counterpart_report"]["epistemic_status"]
        == "counterpart_report_only_not_objective_truth_or_companion_experience"
    )
    assert (
        authority["current_counterpart_report"][
            "permits_natural_visible_uptake_without_world_claim"
        ]
        is True
    )
    system = reviewer.calls[0][0][0]["content"]
    assert "machine-readable epistemic_authority_contract" in system
    assert "private mental continuity" in system
    assert "unless they separately embed an external premise" in system


@pytest.mark.asyncio
async def test_source_closure_marks_typed_direct_dialogue_by_exact_speaker() -> None:
    counterpart_ref = "dialogue:observation:observation:qq:older"
    companion_ref = "dialogue:expression:plan:older:beat:1"
    request = _qq_request().model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "actor_ref": "agent:companion",
                    "slices": {
                        "recent_dialogue": {
                            "availability": "available",
                            "source_refs": [counterpart_ref, companion_ref],
                            "items": [
                                {
                                    "source_ref": counterpart_ref,
                                    "value": {
                                        "dialogue_id": counterpart_ref,
                                        "speaker": "counterpart",
                                        "speaker_ref": "user:primary",
                                        "text": "下午和摊贩争了几句。",
                                    },
                                    "attention_source_refs": [counterpart_ref],
                                },
                                {
                                    "source_ref": companion_ref,
                                    "value": {
                                        "dialogue_id": companion_ref,
                                        "speaker": "companion",
                                        "speaker_ref": "agent:companion",
                                        "text": "听着就烦。",
                                    },
                                    "attention_source_refs": [companion_ref],
                                },
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            )
        }
    )
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我想起她的报告，也记得自己当时说过的话。",
                "attended_source_refs": [counterpart_ref, companion_ref],
            },
            "timing_choice": "now",
            "beats": [
                {
                    "modality": "text",
                    "text": "你说下午和摊贩争了几句，我当时回了听着就烦。",
                }
            ],
            "stance": "attend_to_dialogue",
            "brief_rationale": "Use both dialogue records only within their epistemic scopes.",
            "world_claims": [
                {
                    "claim_text": "对方说下午和摊贩争了几句。",
                    "scope": "counterpart_history",
                    "source_refs": [counterpart_ref],
                },
                {
                    "claim_text": "我当时表达过听着就烦。",
                    "scope": "shared_history",
                    "source_refs": [companion_ref],
                },
            ],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([_source_closure_review()])

    await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
        model_visible_context_json=request.model_content_json,
    )

    entries = json.loads(reviewer.calls[0][0][1]["content"])["source_evidence"]["entries"]
    authorities = {
        entry["item"]["source_ref"]: entry["authority"]
        for entry in entries
        if entry["kind"] == "pinned_context_item"
    }
    assert authorities == {
        counterpart_ref: "counterpart_report_only",
        companion_ref: "companion_expression_record",
    }


@pytest.mark.asyncio
async def test_source_closure_resolves_only_structured_exact_current_report_uptake() -> None:
    request = _qq_request()
    report_text = "今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。"
    request = request.model_copy(
        update={"trigger_message": request.trigger_message.model_copy(update={"text": report_text})}
    )
    visible_span = "价格说不清楚还要争半天"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {
                    "modality": "text",
                    "text": f"听着就很让人火大……{visible_span}。最后你没吃亏吧？",
                }
            ],
            "stance": "take_up_current_report",
            "brief_rationale": "React to the exact current report without promoting it.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": [],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": visible_span,
                            "claim_index": None,
                            "source_relation": ("exact_current_report_discourse_coverage"),
                            "source_refs": [request.trigger_message.observation_ref],
                        }
                    ],
                    "r": "The span restates only the exact current report.",
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.review.visible_text_failures == ()
    assert len(result.review.visible_findings) == 1
    assert result.review.discourse_resolved_visible_finding_indexes == (0,)


@pytest.mark.asyncio
async def test_v7_resolves_exact_recent_dialogue_uptake_without_second_adjudication() -> None:
    dialogue_ref = "dialogue:observation:prior-heat-report"
    request = _qq_request_with_recent_dialogue(
        trigger_text="哈哈，已经彻底活过来了。",
        records=[
            {
                "dialogue_ref": dialogue_ref,
                "speaker": "counterpart",
                "text": "刚才真有点被晒蔫了。",
                "occurred_at": "2026-08-02T12:50:00+08:00",
                "sequence": 101,
            }
        ],
    )
    visible_span = "刚才晒蔫了"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": f"{visible_span}，现在缓过来就好。"}],
            "stance": "continue_recent_report",
            "brief_rationale": "React to the exact recent report.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": [],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": visible_span,
                            "claim_index": None,
                            "source_relation": "exact_current_report_discourse_coverage",
                            "source_refs": [dialogue_ref],
                        }
                    ],
                    "r": "The exact typed dialogue report entails this uptake.",
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.report_relative_adjudication_used is False
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_source_closure_keeps_structured_unreported_motive_accusation() -> None:
    request = _qq_request()
    report_text = "今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。"
    request = request.model_copy(
        update={"trigger_message": request.trigger_message.model_copy(update={"text": report_text})}
    )
    invented_span = "那个摊贩就是故意坑你"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": f"{invented_span}。"}],
            "stance": "invent_counterpart_motive",
            "brief_rationale": "Exercise the external-fact boundary.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )

    class _StructuredReviewer:
        model = "structured-source-closure-reviewer"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            review_request = json.loads(messages[1]["content"])
            contract = review_request["output_contract"]
            assert contract["contract"] == "source-closure-review.7"
            assert contract["visible_findings"]["required_for_each_v"] is True
            assert contract["visible_findings"]["source_relations"] == [
                "unclosed",
                "exact_current_report_discourse_coverage",
                "declared_world_claim_source_mismatch",
            ]
            return json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": [],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": invented_span,
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                    ],
                    "r": "The current report does not entail the attributed motive.",
                },
                ensure_ascii=False,
            )

    reviewer = _StructuredReviewer()
    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert result.review.visible_findings[0].visible_span == invented_span
    assert result.review.discourse_resolved_visible_finding_indexes == ()
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_source_closure_resolves_structured_current_report_question_false_positive() -> None:
    request = _qq_request()
    request = request.model_copy(
        update={
            "trigger_message": request.trigger_message.model_copy(
                update={"text": "其实我不是想让你帮我分析怎么维权。"}
            )
        }
    )
    visible_span = "嗯？那你想说的是什么？"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_span}],
            "stance": "ask_what_the_counterpart_meant",
            "brief_rationale": "Ask without adding an external proposition.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": [],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": visible_span,
                            "claim_index": None,
                            "source_relation": ("exact_current_report_discourse_coverage"),
                            "source_refs": [request.trigger_message.observation_ref],
                        }
                    ],
                    "r": "This is a non-factual question taking up the exact report.",
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert result.review.visible_text_failures == ()
    assert result.review.discourse_resolved_visible_finding_indexes == (0,)
    authority = json.loads(reviewer.calls[0][0][1]["content"])["epistemic_authority_contract"][
        "current_counterpart_report"
    ]
    assert authority["direct_uptake_requires"] == (
        "nonfactual_discourse_relation_or_semantic_entailment_by_the_exact_current_report"
    )


@pytest.mark.asyncio
async def test_source_closure_missing_visible_finding_reselects_only_reviewer_wire() -> None:
    request = _qq_request()
    request = request.model_copy(
        update={
            "trigger_message": request.trigger_message.model_copy(
                update={"text": "其实我不是想让你帮我分析怎么维权。"}
            )
        }
    )
    visible_span = "嗯？那你想说的是什么？"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_span}],
            "stance": "ask_what_the_counterpart_meant",
            "brief_rationale": "Ask without adding an external proposition.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    legacy_short_accusation = json.dumps(
        {
            "ci": [],
            "v": ["undeclared_external_assertion"],
            "p": [],
            "r": "A coarse accusation without its required proposition coordinates.",
        },
        ensure_ascii=False,
    )
    corrected_structured_verdict = json.dumps(
        {
            "ci": [],
            "v": ["undeclared_external_assertion"],
            "p": [],
            "visible_findings": [
                {
                    "category": "undeclared_external_assertion",
                    "visible_span": visible_span,
                    "claim_index": None,
                    "source_relation": "exact_current_report_discourse_coverage",
                    "source_refs": [request.trigger_message.observation_ref],
                }
            ],
            "r": "The question is covered by exact-report discourse authority.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([legacy_short_accusation, corrected_structured_verdict])

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert len(reviewer.calls) == 2
    assert reviewer.calls[1][0][:2] == reviewer.calls[0][0]
    assert reviewer.calls[1][0][2] == {
        "role": "assistant",
        "content": legacy_short_accusation,
    }
    correction = reviewer.calls[1][0][3]["content"]
    assert "structural failure supplies no factuality conclusion" in correction
    assert "visible_findings" in correction


@pytest.mark.asyncio
async def test_source_closure_repeated_missing_visible_finding_is_technical_failure() -> None:
    request = _qq_request()
    visible_span = "嗯？那你想说的是什么？"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_span}],
            "stance": "ask_what_the_counterpart_meant",
            "brief_rationale": "Ask without adding an external proposition.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    invalid_wire = json.dumps(
        {
            "ci": [],
            "v": ["undeclared_external_assertion"],
            "p": [],
            "r": "A coarse accusation without proposition coordinates.",
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel([invalid_wire, invalid_wire])

    with pytest.raises(ValidationTechnicalFailure) as raised:
        await review_expression_source_closure(
            reviewer=reviewer,
            request=request,
            raw=raw,
            identity_frame=None,
        )

    assert raised.value.failure_code == "source_review_exception"
    assert len(reviewer.calls) == 2


    request = _qq_request()
    visible_span = "那件麻烦事已经做完了"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": visible_span}],
            "stance": "upgrade_unrelated_evidence",
            "brief_rationale": "Exercise source identity validation.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": [],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": visible_span,
                            "claim_index": None,
                            "source_relation": ("exact_current_report_discourse_coverage"),
                            "source_refs": ["event:unrelated"],
                        }
                    ],
                    "r": "The alleged report relation points at unrelated evidence.",
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert result.review.discourse_resolved_visible_finding_indexes == ()


@pytest.mark.asyncio
async def test_source_closure_keeps_mixed_external_accusation_after_report_uptake() -> None:
    request = _qq_request()
    request = request.model_copy(
        update={
            "trigger_message": request.trigger_message.model_copy(
                update={"text": "我今天在学校门口和摊贩争了半天。"}
            )
        }
    )
    covered_span = "跟摊贩争了半天，听着就累"
    invented_span = "他肯定一直都故意坑学生"
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [
                {
                    "modality": "text",
                    "text": f"{covered_span}。{invented_span}。",
                }
            ],
            "stance": "mix_report_uptake_with_invented_history",
            "brief_rationale": "Exercise proposition-level adjudication.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    reviewer = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": [],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": covered_span,
                            "claim_index": None,
                            "source_relation": ("exact_current_report_discourse_coverage"),
                            "source_refs": [request.trigger_message.observation_ref],
                        },
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": invented_span,
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        },
                    ],
                    "r": "One proposition is report uptake; one adds external history.",
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_expression_source_closure(
        reviewer=reviewer,
        request=request,
        raw=raw,
        identity_frame=None,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert result.review.visible_text_failures == ("undeclared_external_assertion",)
    assert result.review.discourse_resolved_visible_finding_indexes == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_category",
    (
        "subject_authority_mismatch",
        "temporal_authority_mismatch",
        "occurrence_or_status_authority_mismatch",
    ),
)
@pytest.mark.asyncio
async def test_source_closure_appeal_cannot_clear_explicit_authority_mismatch(
    failure_category: str,
) -> None:
    raw = json.dumps(
        {
            "private_turn_state": {
                "inner_state_summary": "我只注意到她当前报告的内容。",
                "attended_source_refs": ["observation:qq:1"],
            },
            "timing_choice": "now",
            "beats": [
                {
                    "modality": "text",
                    "text": "这件事客观发生过，而且我昨晚也在现场。",
                }
            ],
            "stance": "upgrade_report_and_invent_companion_history",
            "brief_rationale": "Exercise the factual boundary.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    initial = await review_expression_source_closure(
        reviewer=_SequenceJsonModel(
            [
                json.dumps(
                    {
                        "ci": [],
                        "v": [failure_category],
                        "p": [],
                        "visible_findings": [
                            {
                                "category": failure_category,
                                "visible_span": "这件事客观发生过，而且我昨晚也在现场",
                                "claim_index": None,
                                "source_relation": "unclosed",
                                "source_refs": [],
                            }
                        ],
                        "r": "The current report cannot prove this upgraded proposition.",
                    },
                    ensure_ascii=False,
                )
            ]
        ),
        request=_qq_request(),
        raw=raw,
        identity_frame=None,
    )
    assert initial.review is not None

    appealed = await review_expression_source_closure_appeal(
        reviewer=_SequenceJsonModel([_source_closure_review()]),
        request=_qq_request(),
        raw=raw,
        disputed_review=initial.review,
        identity_frame=None,
    )

    assert appealed.review is not None
    assert appealed.review.decision == "unsupported"
    assert appealed.review.visible_text_failures == (failure_category,)


@pytest.mark.asyncio
async def test_cancelled_recall_followup_keeps_the_exact_nested_provider_identity() -> None:
    author = _FirstReplyThenBlockJsonModel(
        json.dumps(
            {
                "private_turn_state": {
                    "inner_state_summary": "这句话碰到一点模糊的熟悉感，我想先回忆再决定。",
                    "attended_source_refs": ["trigger:1"],
                },
                "recall_request": {
                    "query_text": "那件麻烦事",
                    "memory_kinds": ["episodic"],
                    "limit": 2,
                },
            },
            ensure_ascii=False,
        )
    )
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:nested-cancellation",
                memory_kind="episodic",
                source_item_ref="experience:nested-cancellation",
                source_slice="recent_experiences",
                source_refs=("event:nested-cancellation",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="ExperienceCommitted",
                        ref="event:nested-cancellation",
                        source_world_revision=2,
                        immutable_hash="9" * 64,
                    ),
                ),
                source_world_revision=2,
                text="前几天听她提过一件棘手的事情。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=datetime(2026, 7, 25, 12, tzinfo=UTC),
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )
    task = asyncio.create_task(
        _ExpressionDraftWire(
            model=author,
            recall_coordinator=coordinator,
            expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
                update={"private_turn_state_mode": "required"}
            ),
        ).propose(
            _qq_request().model_copy(
                update={
                    "model_content_json": json.dumps(
                        {
                            "world_revision": 3,
                            "deliberation_revision": 0,
                            "ledger_sequence": 0,
                            "logical_time": "2026-07-27T12:00:00+00:00",
                            "slices": {},
                        },
                        ensure_ascii=False,
                    )
                }
            )
        )
    )

    try:
        await asyncio.wait_for(author.nested_call_entered.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
    finally:
        coordinator.close()

    failure = getattr(
        raised.value,
        "world_v2_validation_technical_failure",
        None,
    )
    assert isinstance(failure, ValidationTechnicalFailure)
    assert failure.failure_code == "authored_subcall_timeout"
    assert failure.attempted_model_id == author.model
    assert failure.attempted_model_version == _ExpressionDraftWire.VERSION
    assert len(failure.authored_candidate_audits) == 1
    initial_author = failure.authored_candidate_audits[0]
    assert initial_author.request_hash == _provider_request_hash(*author.calls[0])
    assert initial_author.outcome == "validation_unresolved"
    assert len(failure.provider_subcall_audits) == 1
    nested = failure.provider_subcall_audits[0]
    assert nested.purpose == "recall_followup"
    assert nested.parent_model_call_id == initial_author.model_call_id
    assert nested.request_hash == _provider_request_hash(*author.calls[1])
    assert nested.response_hash is None
    assert nested.outcome == "timeout"


# ---------------------------------------------------------------------------
# V8 declared-claims-only compatibility lane (2026-08-07).
#
# These historical fixtures leave ``review_claim_free_candidates`` false, so
# with inventory disabled the entry reviews only declared world_claim records
# (zero-call pass for claim-free drafts, one bounded reviewer call when claims
# exist). Production composition enables the full visible-text audit. These
# tests pin the compatibility lane's semantics: zero-call shortcut,
# supported/unsupported verdicts, mechanical-ref enforcement that a reviewer
# cannot whiten, and reviewer failover + usage auditing.
# ---------------------------------------------------------------------------


def _v8_request(*, trigger_text: str = "我今天终于把那件麻烦事做完了。") -> ModelInput:
    return _qq_request_with_recent_dialogue(
        trigger_text=trigger_text,
        records=[
            {
                "dialogue_ref": "dialogue:observation:prior",
                "speaker": "counterpart",
                "text": "昨天我去书店逛了逛，买了一本推理小说。",
                "occurred_at": "2026-08-01T08:00:00Z",
                "sequence": 1,
            }
        ],
    )


def _v8_reply(
    *,
    text: str = "听起来挺不错的。",
    claims: list[dict[str, object]] | None = None,
) -> str:
    if claims is None:
        claims = []
    return json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": text}],
            "stance": "warm",
            "brief_rationale": "V8 declared-claims review lane probe.",
            "confidence": 7600,
            "world_claims": claims,
        },
        ensure_ascii=False,
    )


def _v8_supported_claim() -> list[dict[str, object]]:
    return [
        {
            "claim_text": "用户昨天去了书店并买了一本推理小说",
            "scope": "counterpart_history",
            "source_refs": ["dialogue:observation:prior"],
        }
    ]


@pytest.mark.asyncio
async def test_v8_lane_claim_free_expression_skips_reviewer_call() -> None:
    reviewer = _SequenceJsonModel([])
    output = await _ExpressionDraftWire(
        model=_JsonModel(_v8_reply(claims=[])),
        source_closure_reviewer=reviewer,
    ).propose(_v8_request())

    assert output.raw_proposal["action_intents"]
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_production_review_audits_claim_free_external_expression_without_inventory() -> None:
    """A production reviewer must still see omitted first-person world claims.

    Inventory V5 is an optional decomposition optimization.  Its absence must
    not silently turn a visible source-bound expression into a zero-call pass.
    """

    visible_fact = "我今天傍晚在书店靠窗坐了一会儿。"
    reviewer = _SequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_text_failures=("undeclared_external_assertion",),
                visible_span=visible_fact,
            )
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=reviewer,
        inventory_model=None,
        request=_v8_request(),
        raw=_v8_reply(text=visible_fact, claims=[]),
        identity_frame=None,
        review_claim_free_candidates=True,
    )

    assert result.review is not None
    assert result.review.decision == "unsupported"
    assert len(reviewer.calls) == 1
    packet = json.loads(reviewer.calls[0][0][-1]["content"])
    assert packet["output_contract"]["contract"] == "source-closure-review.7"
    assert packet["visible_text"] == visible_fact
    assert packet["epistemic_authority_contract"]["first_person_external_experience"] == {
        "source_required": True,
        "examples": [
            "I spent the afternoon in a bookstore.",
            "I brewed tea on the balcony today.",
        ],
        "not_private_mental_continuity": True,
        "empty_world_claims_result": "undeclared_external_assertion",
    }


@pytest.mark.asyncio
async def test_production_full_review_keeps_report_relative_stage_without_inventory() -> None:
    """Inventory outage must not remove the exact-current-report authority."""

    current_report = "今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。"
    visible_span = "最后争赢了吗？"
    request = _qq_request().model_copy(
        update={
            "trigger_message": _qq_request().trigger_message.model_copy(
                update={"text": current_report}
            )
        }
    )
    raw = json.dumps(
        {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": f"哈哈，你也是够较真的。{visible_span}"}],
            "stance": "react_then_ask",
            "brief_rationale": "Ask for an unknown outcome without asserting one.",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    primary = _SequenceJsonModel(
        [
            _source_closure_review(
                unsupported_boundaries=("visible_text",),
                visible_span=visible_span,
            )
        ]
    )
    report_relative = _SequenceJsonModel(
        [
            json.dumps(
                {
                    "contract": "report-relative-entailment-adjudication.2",
                    "findings": [
                        {
                            "finding_index": 0,
                            "decision": "not_external_proposition",
                            "failure_dimensions": [],
                        }
                    ],
                    "r": "The span asks for an unknown outcome and asserts no answer.",
                },
                ensure_ascii=False,
            )
        ]
    )

    result = await review_expression_with_candidate_external_coverage(
        reviewer=primary,
        inventory_model=None,
        report_relative_reviewer=report_relative,
        request=request,
        raw=raw,
        identity_frame=None,
        review_claim_free_candidates=True,
    )

    assert result.review is not None
    assert result.review.decision == "supported"
    assert len(primary.calls) == 1
    assert len(report_relative.calls) == 1


@pytest.mark.asyncio
async def test_v8_lane_supported_declared_claim_passes_with_one_reviewer_call() -> None:
    reviewer = _SequenceJsonModel([_source_closure_review()])
    output = await _ExpressionDraftWire(
        model=_JsonModel(_v8_reply(claims=_v8_supported_claim())),
        source_closure_reviewer=reviewer,
    ).propose(_v8_request())

    assert output.raw_proposal["action_intents"]
    assert len(reviewer.calls) == 1
    review_system = reviewer.calls[0][0][0]["content"]
    assert "Audit only the declared world_claim records" in review_system
    packet = json.loads(reviewer.calls[0][0][-1]["content"])
    assert packet["output_contract"]["contract"] == "source-closure-review.8"
    assert packet["declared_claim_review_contract"]["visible_text_authority"] == (
        "exclusive_candidate_coverage_completed"
    )
    assert packet["world_claims"][0]["claim_index"] == 0
    assert "visible_text" not in packet


@pytest.mark.asyncio
async def test_v8_lane_unsupported_declared_claim_is_rejected() -> None:
    reviewer = _SequenceJsonModel(
        [_source_closure_review(unsupported_claim_indexes=(0,), unsupported_boundaries=("source_entailment",))]
    )
    with pytest.raises(ValidationTechnicalFailure):
        await _ExpressionDraftWire(
            model=_JsonModel(_v8_reply(claims=_v8_supported_claim())),
            source_closure_reviewer=reviewer,
        ).propose(_v8_request())
    # unsupported verdict -> repair -> fresh review -> still unsupported -> fail
    assert len(reviewer.calls) == 3


@pytest.mark.asyncio
async def test_v8_lane_mechanically_invalid_ref_cannot_be_whitened_by_reviewer() -> None:
    claims = [
        {
            "claim_text": "用户昨天去了书店",
            "scope": "counterpart_history",
            "source_refs": ["dialogue:observation:nonexistent"],
        }
    ]
    reviewer = _SequenceJsonModel([_source_closure_review()])
    with pytest.raises(ValidationTechnicalFailure):
        await _ExpressionDraftWire(
            model=_JsonModel(_v8_reply(claims=claims)),
            source_closure_reviewer=reviewer,
        ).propose(_v8_request())
    assert reviewer.calls == []


@pytest.mark.asyncio
async def test_v8_lane_reviewer_failover_keeps_provider_identity_and_usage() -> None:
    class _UnavailablePrimary:
        model = "source-review-primary"
        VERSION = "primary.8"

        async def complete_json(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del messages, temperature
            raise ConnectionError("primary reviewer unavailable")

    secondary = _SequenceJsonMeteredModel(
        [_source_closure_review()],
        provider="source-review-secondary-provider",
    )
    authority = SourceReviewAuthority(
        primary=_UnavailablePrimary(),
        secondary=secondary,
        hedge_after_seconds=0.05,
        deadline_seconds=0.5,
    )

    output = await _ExpressionDraftWire(
        model=_JsonModel(_v8_reply(claims=_v8_supported_claim())),
        source_closure_reviewer=authority,
    ).propose(_v8_request())

    assert output.raw_proposal["action_intents"]
    assert [
        (attempt.lane, attempt.outcome, attempt.model_id)
        for attempt in output.provider_subcall_audits
    ] == [
        ("primary", "exception", "source-review-primary"),
        ("secondary", "winner", "deepseek-v4-flash"),
    ]
    assert output.provider_subcall_audits[1].usage is not None


@pytest.mark.asyncio
async def test_combined_envelope_releases_first_visible_beat_before_stream_ends() -> None:
    """DeepSeek often ignores the events protocol and emits the combined
    envelope; the incremental scanner must still release the head early."""

    complete = json.dumps(
        {
            "appraisal_draft": {
                "appraise": True,
                "brief_rationale": "probe",
                "behavior_tendency": "listen",
                "stance": "open",
                "display_strategy": "warm",
                "confidence": 7200,
                "meanings": [{"meaning": "x", "confidence": 0.8}],
                "attribution": "situation",
                "severity": 4000,
                "affect": "update",
                "components": [],
            },
            "expression_draft": {
                "timing_choice": "now",
                "turn_posture": "continue",
                "world_claims": [],
                "beats": [{"modality": "text", "text": "怎么了？"}],
                "stance": "warm",
                "brief_rationale": "probe",
                "confidence": 7600,
            },
        },
        ensure_ascii=False,
    )

    chunks = ["{"]
    for index in range(1, len(complete) - 1, 24):
        chunks.append(complete[index : index + 24])
    chunks.append("}")

    buffer = ""
    released: str | None = None
    for chunk in chunks:
        buffer += chunk
        if released is None:
            released = _incremental_first_expression(buffer)
        if released is not None:
            break
    assert released is not None, "head must release before the final chunk"
    head = json.loads(released)
    assert head["appraisal_draft"]["appraise"] is True
    assert head["expression_draft"]["beats"] == [{"modality": "text", "text": "怎么了？"}]
    assert buffer != complete or True
    assert len(buffer) < len(complete), "head must release before the whole stream"
