from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import wraps
from hashlib import sha256
import json
import threading
from typing import Any

import httpx
import pytest

from companion_daemon.llm import (
    ModelCapacityBusyError,
    ModelCircuitOpenError,
    ProviderCircuitBreaker,
    mark_model_request_completed,
    mark_model_request_emitted,
)
from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    CompanionIdentityFrame,
    SourceClosureReselectionLane,
    companion_identity_source_ref,
)
from companion_daemon.world_v2.affect_target_bounds import (
    AFFECT_DIMENSIONS,
    AffectTargetDimensionLowerBound,
    AffectTargetLowerBounds,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelOutput,
    ModelRoute,
    ModelUsageProvenance,
    TriggerMessage,
    ValidationTechnicalFailure,
)
from companion_daemon.world_v2.expression_draft import ExpressionDraftCapabilities
from companion_daemon.world_v2.proposal_envelope import (
    DecisionProposal,
    MinimalProposal,
    ProposalEvidenceRef,
    validate_proposal_envelope,
)
from companion_daemon.world_v2.isolated_source_closure_trace import (
    BoundedSourceClosureTraceCollector,
    capture_isolated_source_closure_trace,
)
from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
)
from companion_daemon.world_v2.single_call_inbound_cognition import (
    SingleCallInboundCognition,
)
from companion_daemon.world_v2.recall_index import (
    FeatureHashRecallEmbedding,
    InMemoryRecallIndex,
    RecallCursor,
    RecallDocument,
    RecallSourceBinding,
)
from companion_daemon.world_v2.recall_corpus import RecallCorpusSources
from companion_daemon.world_v2.recall_runtime import (
    RecallCoordinator,
    verify_trusted_recall_trace,
)
from companion_daemon.world_v2.production_turn_application import (
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.production_latency_trace import ProductionLatencyRecorder
from companion_daemon.world_v2.recall_embedding import OpenAICompatibleRecallEmbedding
from companion_daemon.world_v2.world_turn_runtime import InboundTurn
from test_production_turn_application import (
    NOW,
    _DeliveredTransport,
    _Identities,
    _Router,
    _config,
)

_PRIVATE_RECALL_SOURCE_REF = "event:impression:private-recall:sha256:" + "d" * 64
_PAIRED_BACKUP_RECALL_SOURCE_REF = "event:impression:paired-backup:sha256:" + "e" * 64


def _strict_source_reselection_fixture(
    messages: list[dict[str, str]],
    raw: str,
) -> str:
    """Migrate standalone correction fixtures onto the negotiated strict wire."""

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
    normalized.pop("delay_seconds", None)
    expectation = normalized.get("response_expectation")
    if isinstance(expectation, dict):
        expectation = dict(expectation)
        expectation.pop("wait_seconds", None)
        expectation.setdefault("wait_position_bp", 0)
    return json.dumps(
        {
            "expression_draft": {
                "private_turn_state": private_state,
                "timing_choice": timing_choice,
                "cadence": normalized.get("cadence", "conversational"),
                "beats": ordered_beats,
                "delay_position_bp": 0 if timing_choice == "later" else None,
                "expires_after_seconds": normalized.get("expires_after_seconds"),
                "stance": normalized.get("stance"),
                "brief_rationale": normalized.get("brief_rationale"),
                "impulse_summary": normalized.get("impulse_summary"),
                "confidence": normalized.get("confidence", 7_000),
                "variation_profile": normalized.get("variation_profile"),
                "response_expectation": expectation,
                "response_expectation_assessment": normalized.get(
                    "response_expectation_assessment"
                ),
                "world_claims": normalized.get("world_claims", []),
            },
            "episode_disposition": episode_disposition,
        },
        ensure_ascii=False,
    )


class _CombinedProvider:
    model = "combined-flash"
    strict_reselection_wire = True

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Keep paired fixtures honest when the expression pass is standalone."""

        super().__init_subclass__(**kwargs)
        complete = cls.__dict__.get("complete")
        if complete is None:
            return

        @wraps(complete)
        async def prompt_aware_complete(
            self: _CombinedProvider,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            raw = await complete(self, messages, temperature=temperature)
            return self._response_for_prompt(messages, raw)

        cls.complete = prompt_aware_complete  # type: ignore[method-assign]

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def _response_for_prompt(self, messages: list[dict[str, str]], raw: str) -> str:
        if "COMBINED OUTPUT ENVELOPE" in messages[0]["content"]:
            return raw
        try:
            value: Any = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw
        if not isinstance(value, dict):
            return raw
        expression = value.get("expression_draft", value.get("ExpressionDraft"))
        if not isinstance(expression, dict) and "timing_choice" in value:
            expression = value
        if not isinstance(expression, dict):
            return raw
        expression_raw = json.dumps(expression, ensure_ascii=False)
        return (
            _strict_source_reselection_fixture(messages, expression_raw)
            if self.strict_reselection_wire
            else expression_raw
        )

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        raw = json.dumps(
            {
                "AppraisalDraft": {
                    "appraise": True,
                    "affect": "open",
                    "brief_rationale": "The insult creates a real relational wound.",
                    "behavior_tendency": "set_boundary",
                    "stance": "hurt_but_self_possessed",
                    "display_strategy": "restrained_boundary",
                    "confidence": 8500,
                    "meanings": [{"meaning": "boundary_violation", "confidence": 8500}],
                    "attribution": "user",
                    "severity": 7600,
                    "components": [{"dimension": "hurt", "target_intensity_bp": 6200}],
                },
                "ExpressionDraft": {
                    "timing_choice": "now",
                    "beats": [
                        {"modality": "text", "text": "这句话确实有点伤人。"},
                        {"modality": "text", "text": "你可以不认同我，但别这样贬低我。"},
                    ],
                    "stance": "hurt_boundary",
                    "brief_rationale": "Let the accepted hurt shape a restrained boundary.",
                    "confidence": 8200,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )
        return self._response_for_prompt(messages, raw)


class _RecordingModelAdapter:
    """Expose the exact ModelInputs for persisted request-lineage assertions."""

    def __init__(self, target: object) -> None:
        self._target = target
        self.requests: list[ModelInput] = []

    def __getattr__(self, name: str) -> object:
        return getattr(self._target, name)

    async def propose(self, request: ModelInput) -> Any:
        self.requests.append(request)
        return await self._target.propose(request)  # type: ignore[attr-defined,no-any-return]

    async def recover(self, request: ModelInput, failure_code: str) -> Any:
        self.requests.append(request)
        return await self._target.recover(  # type: ignore[attr-defined,no-any-return]
            request,
            failure_code,
        )


def _deliberation_request_hash(request: ModelInput) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(canonical.encode()).hexdigest()


def _provider_request_hash(
    messages: list[dict[str, str]],
    *,
    temperature: float,
) -> str:
    canonical = json.dumps(
        {
            "messages": messages,
            "temperature": temperature,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode()).hexdigest()


class _PrivateTurnStateCombinedProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": True,
                    "affect": "open",
                    "brief_rationale": "The insult lands as a real relational wound.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "hurt_but_self_possessed",
                    "display_strategy": "model_owned",
                    "confidence": 8500,
                    "meanings": [{"meaning": "boundary_violation", "confidence": 8500}],
                    "attribution": "user",
                    "severity": 7600,
                    "components": [{"dimension": "hurt", "target_intensity_bp": 6200}],
                },
                "expression_draft": {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": (
                            "这句话先让我觉得被贬低了；我现在更想守住自己，"
                            "而不是为了让聊天继续去追问。"
                        ),
                        "attended_source_refs": [],
                    },
                    "timing_choice": "now",
                    "beats": [
                        {"modality": "text", "text": "这句话确实有点伤人。"},
                        {"modality": "text", "text": "你可以不认同我，但别这样贬低我。"},
                    ],
                    "stance": "hurt_boundary",
                    "brief_rationale": "Say what I actually want to say.",
                    "confidence": 8200,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _ContextShiftPrivateTurnStateProvider(_PrivateTurnStateCombinedProvider):
    """Return a fresh standalone expression for a later expression request."""

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        if "exactly two keys" in messages[0]["content"]:
            return await super().complete(messages, temperature=temperature)
        del temperature
        self.calls.append(messages)
        accepted_affect_visible = "accepted_affect" in messages[1]["content"]
        return json.dumps(
            {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": (
                        "刚接受的受伤状态现在很清楚；我想按这个当下直接守住边界。"
                        if accepted_affect_visible
                        else "我重新看过当前请求；此刻仍想直接说清自己的边界。"
                    ),
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "这话伤到我了，别这样贬低我。"}],
                "stance": "accepted_hurt_boundary",
                "brief_rationale": "Choose from the newly accepted current state.",
                "confidence": 8300,
                "world_claims": [],
            },
            ensure_ascii=False,
        )


class _PrivateTurnStateRepairProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        expression = (
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "旧回复只是先前的无状态选择。"}],
                "stance": "keep_talking",
                "brief_rationale": "Keep the conversation going.",
                "world_claims": [],
            }
            if len(self.calls) == 1
            else {
                "private_turn_state": {
                    "inner_state_summary": (
                        "我先被这句话刺到了，也更想把界限说清楚，不需要为了延续话题再追问。"
                    ),
                    "attended_source_refs": ["observation:1"],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "这话挺伤人的，我不想装作没事。"}],
                "stance": "hurt_and_direct",
                "brief_rationale": "Express the newly selected response.",
                "world_claims": [],
            }
        )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": True,
                    "affect": "open",
                    "brief_rationale": "The insult changes the immediate emotional situation.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "hurt_but_self_possessed",
                    "display_strategy": "model_owned",
                    "confidence": 8200,
                    "meanings": [{"meaning": "boundary_violation", "confidence": 8500}],
                    "attribution": "user",
                    "severity": 7600,
                    "components": [{"dimension": "hurt", "target_intensity_bp": 6200}],
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _MeteredPrivateTurnStateRepairProvider(_PrivateTurnStateRepairProvider):
    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        reply = await super().complete(messages, temperature=temperature)
        ordinal = len(self.calls)
        material = {
            "usage_contract": "model-usage.1",
            "route_class": "chat",
            "input_tokens": 20,
            "output_tokens": 5,
            "thinking_tokens": 0,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": "paired-fake-provider",
            "provider_usage_ref": f"usage:paired:{ordinal}",
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


class _ExplicitAuthoredFieldsCombinedProvider(_CombinedProvider):
    """Omit authored decisions once, or keep omitting them after reselection."""

    def __init__(
        self,
        *,
        remains_invalid: bool = False,
        correction_episode_disposition: str | None = None,
    ) -> None:
        super().__init__()
        self._remains_invalid = remains_invalid
        self._correction_episode_disposition = correction_episode_disposition

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        self.calls.append(messages)
        expression: dict[str, object] = {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "我想按自己的节奏直接接住这句话。",
                "attended_source_refs": [],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我在听。"}],
            "stance": "present",
            "brief_rationale": "Choose a direct response from the pinned turn.",
            "world_claims": [],
        }
        if len(self.calls) > 1 and not self._remains_invalid:
            expression.update(
                {
                    "cadence": "conversational",
                    "confidence": 8_100,
                }
            )
            if self._correction_episode_disposition is not None:
                expression["episode_disposition"] = self._correction_episode_disposition
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "present",
                    "display_strategy": "model_owned",
                    "confidence": 6_000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _InvalidPrivateStateShapeProvider(_CombinedProvider):
    def __init__(self, invalid_state: object) -> None:
        super().__init__()
        self._invalid_state = invalid_state

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        expression = (
            {
                "private_turn_state": self._invalid_state,
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "这句来自无效状态。"}],
                "stance": "invalid_private_state",
                "brief_rationale": "Fixture.",
                "world_claims": [],
            }
            if len(self.calls) == 1
            else {
                "private_turn_state": {
                    "inner_state_summary": "这句话让我不舒服，我想直接把界限说清楚。",
                    "attended_source_refs": ["observation:1"],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "这话挺伤人的，我不想装作没事。"}],
                "stance": "hurt_and_direct",
                "brief_rationale": "Choose the final expression from the current turn.",
                "world_claims": [],
            }
        )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": True,
                    "affect": "hurt",
                    "brief_rationale": "The message changes the immediate emotional situation.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "hurt_but_self_possessed",
                    "display_strategy": "model_owned",
                    "confidence": 8200,
                    "meanings": [{"meaning": "boundary_violation", "confidence": 8500}],
                    "attribution": "user",
                    "severity": 7600,
                    "components": [{"dimension": "hurt", "target_intensity_bp": 6200}],
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _InvalidPrivateStateRecallChoiceProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) == 1:
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "No durable appraisal is needed.",
                        "behavior_tendency": "choose_own_response",
                        "stance": "self_possessed",
                        "display_strategy": "model_owned",
                        "confidence": 6000,
                        "expression_anchor": "这个 appraisal 外字段也不能进入重选上下文。",
                    },
                    "private_turn_state": {
                        "inner_state_summary": "我想先找一段旧对话再决定怎么回应。",
                        "attended_source_refs": ["observation:1"],
                    },
                    "recall_request": {
                        "query_text": "一段不确定的旧对话",
                        "limit": 2,
                    },
                    "expression_draft": {
                        "beats": [
                            {
                                "modality": "text",
                                "text": "这句无效的旧表达不能进入下一次选择。",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_possessed",
                    "display_strategy": "model_owned",
                    "confidence": 6000,
                },
                "expression_draft": {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "当前这句已经足够让我知道自己不喜欢这种贬低，"
                            "我可以直接说出来，不需要再开一次回忆。"
                        ),
                        "attended_source_refs": ["observation:1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这句我不喜欢，别这样贬低我。"}],
                    "stance": "direct_boundary",
                    "brief_rationale": "Choose from the current turn after the invalid recall choice.",
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _InvalidRecallPayloadCombinedProvider(_CombinedProvider):
    def __init__(self, invalid_recalls: tuple[dict[str, object], ...]) -> None:
        super().__init__()
        self._invalid_recalls = invalid_recalls

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        ordinal = len(self.calls)
        if ordinal <= len(self._invalid_recalls):
            return json.dumps(
                {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": "我想先回忆再决定。",
                        "attended_source_refs": [],
                    },
                    "recall_request": self._invalid_recalls[ordinal - 1],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_possessed",
                    "display_strategy": "model_owned",
                    "confidence": 6000,
                },
                "expression_draft": {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": "当前材料已经足够，我想直接回应。",
                        "attended_source_refs": [],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "嗯，我听见了。"}],
                    "stance": "present",
                    "brief_rationale": "Choose the final expression from the pinned turn.",
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _InvalidRecallThenInvalidFinalCombinedProvider(_CombinedProvider):
    """Expose a third-call bug after the bounded Recall-choice reselection."""

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) == 1:
            return json.dumps(
                {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": "我想先回忆再决定。",
                        "attended_source_refs": [],
                    },
                    "recall_request": {
                        "query_text": "非法 Recall 后不能再发起第三次角色调用",
                        "limit": 7,
                    },
                },
                ensure_ascii=False,
            )
        expression = (
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "第二次结果仍缺少当下私人状态。"}],
                "stance": "invalid_without_private_state",
                "brief_rationale": "Invalid final fixture.",
                "world_claims": [],
            }
            if len(self.calls) == 2
            else {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "第三次调用不应该发生。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "不应发送这条。"}],
                "stance": "unexpected_third_call",
                "brief_rationale": "This fixture proves a forbidden third call.",
                "world_claims": [],
            }
        )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_possessed",
                    "display_strategy": "model_owned",
                    "confidence": 6000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _ReadyPairedPrefetchEmbedding:
    version = "ready-paired-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self.finished = threading.Event()
        self._delegate = FeatureHashRecallEmbedding()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        result = self._delegate.embed(texts)
        if texts == ("机器人",):
            self.finished.set()
        return result


class _EpisodeCombinedProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        if any("provisional first beat" in message["content"] for message in messages):
            del temperature
            self.calls.append(messages)
            return json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这句话有点伤人。"}],
                    "stance": "hurt_boundary",
                    "brief_rationale": "A direct, self-contained first beat.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        return await super().complete(messages, temperature=temperature)


class _ShadowRecallCombinedProvider(_CombinedProvider):
    """Choose Recall on the full lane while a diagnostic shadow runs."""

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) == 1:
            return json.dumps(
                {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": "这句话有点刺，我想先确认自己记得的旧语境。",
                        "attended_source_refs": [],
                    },
                    "recall_request": {
                        "query_text": "之前关于机器人的谈话",
                        "memory_kinds": ["reflective"],
                        "limit": 3,
                    },
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_possessed",
                    "display_strategy": "model_owned",
                    "confidence": 6000,
                },
                "expression_draft": {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": "回看过可用记忆后，我仍想直接说这句话让我不舒服。",
                        "attended_source_refs": [],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这句挺刺的，我不想装作没感觉。"}],
                    "stance": "hurt_and_direct",
                    "brief_rationale": "Choose a direct response after the bounded recall.",
                    "confidence": 7900,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _ShadowPrivateEpisodeProvider(_CombinedProvider):
    """A valid observation-only candidate with no Action authority."""

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我先形成一个可丢弃的影子候选。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "这个影子候选不能变成第二条消息。"}],
                "stance": "shadow_observation",
                "brief_rationale": "Observe one independent provisional candidate.",
                "confidence": 7000,
                "world_claims": [],
            },
            ensure_ascii=False,
        )


class _ShadowPrivateStateRepairProvider(_CombinedProvider):
    """Miss the required state once, then make one complete role reselection."""

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        expression = (
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "旧回复只是先前的无状态选择。"}],
                "stance": "keep_talking",
                "brief_rationale": "Keep the conversation going.",
                "world_claims": [],
            }
            if len(self.calls) == 1
            else {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我被这句话刺到了，想直接把自己的界限说清楚。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "这话挺伤人的，我不想装作没事。"}],
                "stance": "hurt_and_direct",
                "brief_rationale": "Choose the complete expression again.",
                "world_claims": [],
            }
        )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "self_possessed",
                    "display_strategy": "model_owned",
                    "confidence": 6000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _AppendEpisodeProvider:
    model = "append-episode-fixture"

    def __init__(self, disposition: str = "append") -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.disposition = disposition

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        provisional = any("provisional first beat" in item["content"] for item in messages)
        if not provisional:
            await asyncio.sleep(0.05)
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [
                    {
                        "modality": "text",
                        "text": "我先接住你这句话。"
                        if provisional
                        else "还有一点，我也想认真听你接着说。",
                    }
                ],
                "stance": "attentive",
                "brief_rationale": "Each beat has independent semantic value.",
                "world_claims": [],
                **({"episode_disposition": self.disposition} if not provisional else {}),
            },
            ensure_ascii=False,
        )


class _InvalidAppraisalValidExpressionProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": True,
                    # Provider omitted meanings/attribution/severity. State
                    # must fail closed without sacrificing the valid reply.
                    "brief_rationale": "Maybe emotionally meaningful.",
                    "behavior_tendency": "attend",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 5000,
                },
                "expression_draft": {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "你好呀，我是沈知栀。"}],
                    "stance": "warm_introduction",
                    "brief_rationale": "Answer the greeting naturally.",
                    "confidence": 7800,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _BelowBoundThenValidCombinedProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        target = 100 if len(self.calls) == 1 else 4300
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": True,
                    "affect": "open",
                    "brief_rationale": "The current slight still matters.",
                    "behavior_tendency": "pause",
                    "stance": "guarded",
                    "display_strategy": "restrained",
                    "confidence": 7000,
                    "meanings": [{"meaning": "disappointment", "confidence": 7000}],
                    "attribution": "user",
                    "severity": 6000,
                    "components": [
                        {
                            "dimension": "hurt",
                            "target_intensity_bp": target,
                        }
                    ],
                },
                "expression_draft": {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这句我听着有点不舒服。"}],
                    "stance": "guarded",
                    "brief_rationale": "Respond without hiding the reaction.",
                    "confidence": 7600,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _BelowBoundTwiceCombinedProvider(_BelowBoundThenValidCombinedProvider):
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        raw = await super().complete(messages, temperature=temperature)
        value = json.loads(raw)
        value["appraisal_draft"]["components"][0]["target_intensity_bp"] = 100
        return json.dumps(value, ensure_ascii=False)


class _OrdinaryCombinedProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No material emotional shift.",
                    "behavior_tendency": "observe",
                    "stance": "wait",
                    "display_strategy": "withhold",
                    "confidence": 3000,
                },
                "expression_draft": {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我在听。"}],
                    "stance": "attentive",
                    "brief_rationale": "Stay with the current conversation.",
                    "confidence": 7800,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _SubjectMixupCombinedProvider(_CombinedProvider):
    def __init__(self) -> None:
        super().__init__()
        self._replies = (
            "家里那边怎么了？嘉兴最近天气不太好吗？",
            "家里那边怎么了？",
        )

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        text = self._replies[min(len(self.calls) - 1, len(self._replies) - 1)]
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal.",
                    "behavior_tendency": "attend",
                    "stance": "concerned",
                    "display_strategy": "natural",
                    "confidence": 6000,
                },
                "expression_draft": {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": text}],
                    "stance": "concerned",
                    "brief_rationale": "Ask what happened.",
                    "confidence": 7600,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _SourceClosureReviewer:
    model = "local-source-closure"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        reviewed = json.loads(messages[1]["content"])
        if reviewed.get("output_contract", {}).get("contract") == "source-closure-appeal.4":
            self.calls.append(messages)
            return json.dumps(
                {
                    **reviewed["rejected_categories"],
                    "r": "The rejected categories remain unsupported.",
                },
                ensure_ascii=False,
            )
        if "Audit only factual source closure" not in messages[0]["content"]:
            raise ValueError("fixture only implements source-closure review")
        self.calls.append(messages)
        unsupported = "嘉兴" in reviewed["visible_text"]
        return json.dumps(
            {
                "ci": [],
                "v": ["undeclared_external_assertion"] if unsupported else [],
                "p": [],
                "visible_findings": (
                    [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": "嘉兴",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                    ]
                    if unsupported
                    else []
                ),
                "r": (
                    "The companion hometown was used as a counterpart premise."
                    if unsupported
                    else "The corrected question has no factual premise."
                ),
            },
            ensure_ascii=False,
        )


def _requested_output_contract(messages: list[dict[str, str]]) -> str | None:
    """Read the original strict contract through a wire-only retry envelope."""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        try:
            value = json.loads(message["content"])
        except (KeyError, TypeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        output_contract = value.get("output_contract")
        if not isinstance(output_contract, dict):
            continue
        contract = output_contract.get("contract")
        if isinstance(contract, str):
            return contract
    return None


class _TerminalCandidateValidationInventory:
    """Return schema-valid bytes that fail one deterministic inventory boundary."""

    model = "terminal-candidate-inventory"

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        self.calls.append(messages)
        request = json.loads(messages[1]["content"])
        beat = request["visible_beats"][0]
        text = beat["text"]
        proposition = {
            "locator": {
                "beat_index": beat["beat_index"],
                "char_start": 0,
                "char_end": len(text),
                "text": text,
            },
            "semantic_role": "standalone_external_proposition",
            "parent_index": None,
        }
        propositions = (
            [proposition, proposition]
            if self.failure_code == "inventory_invalid"
            else [proposition]
        )
        return json.dumps(
            {
                "contract": "candidate-external-proposition-inventory.3",
                "propositions": propositions,
            },
            ensure_ascii=False,
        )


class _TerminalCandidateValidationReviewer:
    """Accept full review but return a schema-valid invalid coverage relation."""

    model = "terminal-candidate-reviewer"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.contracts: list[str | None] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        self.calls.append(messages)
        contract = _requested_output_contract(messages)
        self.contracts.append(contract)
        if contract == "source-closure-review.7":
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "visible_findings": [],
                    "r": "The full source review found no unsupported proposition.",
                },
                ensure_ascii=False,
            )
        if contract == "candidate-external-proposition-coverage.1":
            request = json.loads(messages[1]["content"])
            return json.dumps(
                {
                    "contract": contract,
                    "findings": [
                        {
                            "locator": locator,
                            "decision": "closed",
                            # Individually valid enum values, but this pair is
                            # deliberately forbidden by the parser.
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                        for locator in request["locators"]
                    ],
                },
                ensure_ascii=False,
            )
        raise AssertionError(f"unexpected source-review contract: {contract}")


class _UnsupportedLifeSourceClosureReviewer:
    model = "independent-source-closure"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.ordinary_calls = 0

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        request = json.loads(messages[1]["content"])
        if request.get("output_contract", {}).get("contract") == "source-closure-appeal.4":
            return json.dumps(
                {
                    **request["rejected_categories"],
                    "r": "The rejected categories remain unsupported.",
                },
                ensure_ascii=False,
            )
        self.ordinary_calls += 1
        if self.ordinary_calls == 1:
            return json.dumps(
                {
                    "ci": [],
                    "v": ["undeclared_external_assertion"],
                    "p": ["undeclared_external_assertion"],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": "刚才在宿舍翻书",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                    ],
                    "r": (
                        "The current observation supports reading the message, not a dorm "
                        "location or an earlier reading activity."
                    ),
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ci": [],
                "v": [],
                "p": [],
                "r": "The replacement adds no unsupported life occurrence.",
            },
            ensure_ascii=False,
        )


class _UnsupportedLifeCombinedProvider(_CombinedProvider):
    def __init__(self) -> None:
        super().__init__()

    @staticmethod
    def _appraisal() -> dict[str, object]:
        return {
            "appraise": False,
            "brief_rationale": "No durable appraisal is needed.",
            "behavior_tendency": "choose_own_response",
            "stance": "present",
            "display_strategy": "model_owned",
            "confidence": 6000,
        }

    @staticmethod
    def _invalid_expression() -> dict[str, object]:
        return {
            "private_turn_state": {
                "inner_state_summary": ("晚上我正在宿舍翻书，看到她这句后想顺手接住她。"),
                "attended_source_refs": ["observation:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "刚才在宿舍翻书，现在看到你了。"}],
            "stance": "share_invented_evening",
            "brief_rationale": "Use an unsupported life scene.",
            "confidence": 7100,
            "world_claims": [],
        }

    @staticmethod
    def _corrected_expression() -> dict[str, object]:
        return {
            "private_turn_state": {
                "inner_state_summary": ("我现在确实看见她这句话，但没有别的生活情境可以当成事实。"),
                "attended_source_refs": ["observation:1"],
            },
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我看到你这句了。"}],
            "stance": "stay_with_the_current_message",
            "brief_rationale": "Choose again from the pinned message only.",
            "confidence": 8200,
            "world_claims": [],
        }

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        expression = (
            self._invalid_expression() if len(self.calls) == 1 else self._corrected_expression()
        )
        if "exactly two keys" in messages[0]["content"]:
            return json.dumps(
                {
                    "appraisal_draft": self._appraisal(),
                    "expression_draft": expression,
                },
                ensure_ascii=False,
            )
        return json.dumps(expression, ensure_ascii=False)


class _DelegatedUnsupportedLifeCombinedProvider(_CombinedProvider):
    """Keep the unsupported draft through the post-appraisal request shift."""

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        self.calls.append(messages)
        try:
            last_user_value = json.loads(messages[-1]["content"])
        except (KeyError, TypeError, json.JSONDecodeError):
            last_user_value = {}
        corrected = (
            isinstance(last_user_value, dict)
            and last_user_value.get("contract") == "source-closure-reselection.2"
        )
        expression = (
            _UnsupportedLifeCombinedProvider._corrected_expression()
            if corrected
            else _UnsupportedLifeCombinedProvider._invalid_expression()
        )
        return json.dumps(
            {
                "appraisal_draft": _UnsupportedLifeCombinedProvider._appraisal(),
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


def _metered_usage(
    *,
    ref: str,
    input_tokens: int,
    output_tokens: int,
) -> ModelUsageProvenance:
    material = {
        "usage_contract": "model-usage.1",
        "route_class": "chat",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thinking_tokens": 0,
        "token_provenance": "provider_reported",
        "transport": "provider_api",
        "provider": "paired-fake-provider",
        "provider_usage_ref": ref,
    }
    digest = sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return ModelUsageProvenance(**material, provider_usage_hash=digest)


class _MeteredOrdinaryCombinedProvider(_OrdinaryCombinedProvider):
    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        raw = await super().complete(messages, temperature=temperature)
        return raw, _metered_usage(
            ref=f"usage:paired-author:{len(self.calls)}",
            input_tokens=20,
            output_tokens=5,
        )


class _JsonMeteredOrdinaryCombinedProvider(_MeteredOrdinaryCombinedProvider):
    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        raise AssertionError("paired structured cognition must preserve JSON mode")

    async def complete_json_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        return await _MeteredOrdinaryCombinedProvider.complete_with_usage(
            self,
            messages,
            temperature=temperature,
        )


class _MeteredSourceClosureReviewer(_SourceClosureReviewer):
    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, ModelUsageProvenance]:
        del temperature
        self.calls.append(messages)
        raw = json.dumps(
            {
                "ci": [],
                "v": [],
                "p": [],
                "r": "The ordinary reply has no factual premise.",
            },
            ensure_ascii=False,
        )
        return raw, _metered_usage(
            ref=f"usage:paired-reviewer:{len(self.calls)}",
            input_tokens=4,
            output_tokens=2,
        )


class _RecallThenCombinedProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) == 1:
            return json.dumps(
                {
                    "recall_request": {
                        "query_text": "之前关于机器人的谈话",
                        "memory_kinds": ["episodic", "semantic"],
                        "limit": 4,
                    }
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "observe",
                    "stance": "self_possessed",
                    "display_strategy": "natural",
                    "confidence": 4000,
                },
                "expression_draft": {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "这句话挺刺的。你具体是在不满我哪一点？",
                        }
                    ],
                    "stance": "self_possessed",
                    "brief_rationale": "I checked memory, then chose a direct question.",
                    "confidence": 7600,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _PrivateStateRecallThenCombinedProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) == 1:
            return json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "这句关于机器人的话刺到了我，也像是连着一段旧对话；"
                            "我想先弄清自己记起的到底是什么。"
                        ),
                        "attended_source_refs": ["observation:1"],
                    },
                    "recall_request": {
                        "query_text": "之前关于机器人的谈话",
                        "memory_kinds": ["reflective"],
                        "limit": 3,
                    },
                },
                ensure_ascii=False,
            )
        if len(self.calls) == 2:
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": True,
                        "affect": "hurt",
                        "brief_rationale": (
                            "The current insult lands in light of the recalled context."
                        ),
                        "behavior_tendency": "choose_own_response",
                        "stance": "hurt_but_self_possessed",
                        "display_strategy": "model_owned",
                        "confidence": 8200,
                        "meanings": [{"meaning": "boundary_violation", "confidence": 8300}],
                        "attribution": "user",
                        "severity": 7200,
                        "components": [{"dimension": "hurt", "target_intensity_bp": 5900}],
                    },
                    "expression_draft": {
                        "timing_choice": "now",
                        "beats": [{"modality": "text", "text": "这句缺了召回后的当下状态。"}],
                        "stance": "invalid_without_final_state",
                        "brief_rationale": "Invalid fixture after recall.",
                        "world_claims": [],
                    },
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": True,
                    "affect": "hurt",
                    "brief_rationale": "The current insult lands in light of the recalled context.",
                    "behavior_tendency": "choose_own_response",
                    "stance": "hurt_but_self_possessed",
                    "display_strategy": "model_owned",
                    "confidence": 8200,
                    "meanings": [{"meaning": "boundary_violation", "confidence": 8300}],
                    "attribution": "user",
                    "severity": 7200,
                    "components": [{"dimension": "hurt", "target_intensity_bp": 5900}],
                },
                "expression_draft": {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "想起那段旧印象后，我更确定自己不想急着给他定性，"
                            "但也不想吞下这句刺人的话。"
                        ),
                        "attended_source_refs": ["S1"],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这句挺刺的，我不想装作没感觉。"}],
                    "stance": "hurt_and_direct",
                    "brief_rationale": "Choose a direct response after recalling the relevant impression.",
                    "confidence": 7900,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


class _AdvancingClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        return self.value

    def advance_ms(self, value: int) -> None:
        self.value += value * 1_000_000


class _TimedCombinedProvider(_OrdinaryCombinedProvider):
    reports_exact_request_emission = True

    def __init__(self, clock: _AdvancingClock) -> None:
        super().__init__()
        self.clock = clock

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.clock.advance_ms(250)
        request_span = mark_model_request_emitted()
        self.clock.advance_ms(5_000)
        try:
            return await super().complete(messages, temperature=temperature)
        finally:
            mark_model_request_completed(request_span)


class _LooseTextCombinedProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) > 1:
            expression = {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你好呀，我是沈知栀。"}],
                "stance": "warm",
                "brief_rationale": "Greet naturally.",
                "confidence": 7000,
                "world_claims": [],
            }
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "Ordinary greeting.",
                        "behavior_tendency": "engage",
                        "stance": "open",
                        "display_strategy": "natural",
                        "confidence": 7000,
                    },
                    "expression_draft": expression,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "Ordinary greeting.",
                    "behavior_tendency": "engage",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 7000,
                },
                # Semantically clear, structurally loose provider output.
                "expression_draft": {
                    "reply": "你好呀，我是沈知栀。",
                    "tone": "warm",
                },
            },
            ensure_ascii=False,
        )


class _LooseMultiMessageCombinedProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) > 1:
            expression = {
                "timing_choice": "now",
                "beats": [
                    {"modality": "text", "text": "先说第一件事。"},
                    {"modality": "text", "text": "还有第二件事。"},
                ],
                "stance": "continue_in_two_beats",
                "brief_rationale": "Two short messages fit the conversational rhythm.",
                "confidence": 7000,
                "world_claims": [],
            }
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "No material emotional shift.",
                        "behavior_tendency": "engage",
                        "stance": "open",
                        "display_strategy": "natural",
                        "confidence": 7000,
                    },
                    "expression_draft": expression,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No material emotional shift.",
                    "behavior_tendency": "engage",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 7000,
                },
                # Common JSON-mode variation: the intended visible beats are
                # an explicit list, but the provider named it ``messages``.
                "expression_draft": {
                    "messages": ["先说第一件事。", "还有第二件事。"],
                    "stance": "continue_in_two_beats",
                    "brief_rationale": "Two short messages fit the conversational rhythm.",
                },
            },
            ensure_ascii=False,
        )


class _LooseExpressionShapeProvider(_CombinedProvider):
    def __init__(self, expression: dict[str, object], *, repair: bool = True) -> None:
        super().__init__()
        self._expression = expression
        self._repair = repair

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) > 1 and self._repair:
            texts: list[str] = []
            for value in self._expression.values():
                if isinstance(value, str):
                    texts = [value]
                    break
                if isinstance(value, list):
                    texts = [
                        str(item.get("text", "")) if isinstance(item, dict) else str(item)
                        for item in value
                    ]
                    break
            expression = {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text} for text in texts if text],
                "stance": "continue",
                "brief_rationale": "Corrected to the exact expression contract.",
                "confidence": 7000,
                "world_claims": [],
            }
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "No material emotional shift.",
                        "behavior_tendency": "engage",
                        "stance": "open",
                        "display_strategy": "natural",
                        "confidence": 7000,
                    },
                    "expression_draft": expression,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No material emotional shift.",
                    "behavior_tendency": "engage",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 7000,
                },
                "expression_draft": self._expression,
            },
            ensure_ascii=False,
        )


class _UnsupportedAutobiographyProvider(_LooseTextCombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "Ordinary question.",
                    "behavior_tendency": "answer",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 6000,
                },
                "expression_draft": {"text": "我刚才一直在看电影。"},
            },
            ensure_ascii=False,
        )


class _TimeoutAfterCombinedProvider(_UnsupportedAutobiographyProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        if not self.calls:
            return await super().complete(messages, temperature=temperature)
        del temperature
        self.calls.append(messages)
        raise TimeoutError("provider main expression timed out")


class _UnsupportedGreetingClaimProvider(_CombinedProvider):
    """Reproduce the production greeting whose invented prefix poisoned the reply."""

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) > 1:
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "A casual greeting does not change affect.",
                        "behavior_tendency": "engage",
                        "stance": "warm",
                        "display_strategy": "natural",
                        "confidence": 6200,
                    },
                    "expression_draft": {
                        "timing_choice": "now",
                        "beats": [
                            {
                                "modality": "text",
                                "text": "午安呀。你今天过得怎么样？",
                            }
                        ],
                        "stance": "warm_greeting",
                        "brief_rationale": "Remove the unsupported activity claim.",
                        "confidence": 7600,
                        "world_claims": [],
                    },
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "A casual greeting does not change affect.",
                    "behavior_tendency": "engage",
                    "stance": "warm",
                    "display_strategy": "natural",
                    "confidence": 6200,
                },
                "expression_draft": {
                    "timing_choice": "now",
                    "beats": [
                        {
                            "modality": "text",
                            "text": "刚忙完社团的事，午安呀。你今天过得怎么样？",
                        }
                    ],
                    "stance": "warm_greeting",
                    "brief_rationale": "Return the greeting and keep the exchange open.",
                    "confidence": 7600,
                    "world_claims": [
                        {
                            "claim_text": "刚忙完社团的事",
                            "scope": "current_world",
                            "source_refs": [
                                "current_situation activity_slices "
                                "social.literature_club_meetup planned"
                            ],
                        }
                    ],
                },
            },
            ensure_ascii=False,
        )


class _AlwaysFailProvider:
    model = "always-fails"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        raise RuntimeError("provider unavailable")


class _FailingProviderWithFallback(_AlwaysFailProvider):
    def __init__(self, fallback: object) -> None:
        super().__init__()
        self.fallback = fallback


class _FailoverAlreadyUsedProvider(_FailingProviderWithFallback):
    """Models a FailoverChatModel whose availability fallback already failed."""

    last_attempt_used_fallback = True


class _QuickExpressionProvider:
    model = "backup-flash"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        expression = {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "我接到了，刚才只是慢了一拍。"}],
            "stance": "acknowledge_briefly",
            "brief_rationale": "Bounded backup response after the main model failed.",
            "confidence": 6000,
            "world_claims": [],
        }
        if "appraisal_draft and expression_draft" in messages[0]["content"]:
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "No emotional transition is required.",
                        "behavior_tendency": "engage",
                        "stance": "present",
                        "display_strategy": "natural",
                        "confidence": 6000,
                    },
                    "expression_draft": expression,
                },
                ensure_ascii=False,
            )
        return json.dumps(expression, ensure_ascii=False)


class _SlowQuickExpressionProvider(_QuickExpressionProvider):
    def __init__(self, *, delay_seconds: float) -> None:
        super().__init__()
        self.delay_seconds = delay_seconds
        self.started = 0
        self.completed = 0

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.started += 1
        await asyncio.sleep(self.delay_seconds)
        raw = await super().complete(messages, temperature=temperature)
        self.completed += 1
        return raw


class _SeparateAppraisalProvider:
    model = "qwen-local"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "appraise": True,
                "brief_rationale": "她把这句话感受到为一次明显的失望和落空。",
                "behavior_tendency": "先消化这份失望",
                "stance": "受伤但仍在意",
                "display_strategy": "有所保留",
                "confidence": 7200,
                "meaning": "disappointment",
                "attribution": "user",
                "severity": 4200,
                "open_affect": True,
                "affect_dimension": "hurt",
                "affect_target_intensity_bp": 2200,
            },
            ensure_ascii=False,
        )


class _BusySeparateAppraisalProvider:
    model = "qwen-local"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del messages, temperature
        self.calls += 1
        raise ModelCapacityBusyError("model provider capacity is in cooldown")


class _SeparateSourceMixupExpressionProvider:
    model = "separate-source-mixup"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        text = (
            "家里那边怎么了？嘉兴最近天气不太好吗？" if len(self.calls) == 1 else "家里那边怎么了？"
        )
        raw = json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": text}],
                "stance": "concerned",
                "brief_rationale": "Ask what happened.",
                "confidence": 7600,
                "world_claims": [],
            },
            ensure_ascii=False,
        )
        return _strict_source_reselection_fixture(messages, raw)


class _ProvisionalBackupProvider:
    model = "independent-provisional"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "我在，先听你说。"}],
                "stance": "present",
                "brief_rationale": "An independently useful first beat.",
                "confidence": 8000,
                "world_claims": [],
            },
            ensure_ascii=False,
        )


class _BlockingShadowObserverProvider(_ProvisionalBackupProvider):
    """One role-identical observer whose transport can remain blocked."""

    model = "same-recovery-checkpoint"

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.started.set()
        await self.release.wait()
        return await super().complete(messages, temperature=temperature)


class _CircuitShadowObserverProvider(_ProvisionalBackupProvider):
    model = "same-recovery-checkpoint"

    def __init__(self) -> None:
        super().__init__()
        self.circuit_breaker = ProviderCircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=30,
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.circuit_breaker.before_call()
        result = await super().complete(messages, temperature=temperature)
        self.circuit_breaker.record_success()
        return result


class _GroundedQuickRecoveryProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) == 1:
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "A memory question need not shift affect.",
                        "behavior_tendency": "answer",
                        "stance": "attentive",
                        "display_strategy": "natural",
                        "confidence": 6000,
                    },
                    "expression_draft": {},
                },
                ensure_ascii=False,
            )
        if len(self.calls) == 2:
            return "{}"
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "记得，你喜欢乌龙茶。"}],
                "stance": "grounded_recall",
                "brief_rationale": "Answer from the verified fact source.",
                "confidence": 7600,
                "world_claims": [
                    {
                        "claim_text": "你喜欢乌龙茶",
                        "scope": "shared_history",
                        "source_refs": ["fact:user:oolong"],
                    }
                ],
            },
            ensure_ascii=False,
        )


def _request(
    *,
    revision: int,
    call: str,
    hurt_minimum_bp: int | None = None,
) -> ModelInput:
    bounds = (
        AffectTargetLowerBounds(
            source_world_revision=revision,
            source_deliberation_revision=0,
            source_ledger_sequence=0,
            bounds=tuple(
                AffectTargetDimensionLowerBound(
                    dimension=dimension,
                    baseline_bp=hurt_minimum_bp if dimension == "hurt" else 0,
                    installed_decay_floor_bp=300,
                    installed_residue_bp=500,
                    minimum_target_intensity_bp=(hurt_minimum_bp if dimension == "hurt" else 500),
                    baseline_calibration_revision=2 if dimension == "hurt" else None,
                    baseline_policy_version=(
                        "affect-baseline-policy.1" if dimension == "hurt" else None
                    ),
                    baseline_basis_hash="d" * 64 if dimension == "hurt" else None,
                )
                for dimension in AFFECT_DIMENSIONS
            ),
        )
        if hurt_minimum_bp is not None
        else None
    )
    return ModelInput(
        call_id=call,
        attempt_id=f"attempt:{call}",
        route=ModelRoute(tier="flash", reason_code="ordinary", router_version="test.1"),
        capsule_id=("a" if revision == 3 else "c") * 64,
        trigger_ref="event:observation:1",
        evaluated_world_revision=revision,
        model_content_json=json.dumps({"world_revision": revision}),
        trigger_evidence=(
            ProposalEvidenceRef(
                ref_id="observation:1",
                evidence_kind="observed_message",
                source_world_revision=3,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:1",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:1",
            source_world_revision=3,
            actor="user:primary",
            channel="qq_c2c",
            reply_target="qq:user:1",
            text="你就是个没用的机器人。",
        ),
        affect_target_bounds=bounds,
    )


@pytest.mark.asyncio
async def test_combined_cognition_binds_model_owned_private_state_without_authorizing_it() -> None:
    provider = _PrivateTurnStateCombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-turn-state.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:private-turn-state")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:private-turn-state-expression"})
    )

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.inner_state_summary.startswith("这句话先让我觉得被贬低")
    assert proposal.private_turn_state.attended_source_refs == ()
    assert all(
        "private_turn_state" not in change.payload.value() for change in proposal.proposed_changes
    )
    system = provider.calls[0][0]["content"]
    assert system.rindex("private_turn_state") < system.rindex("timing_choice")
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_paired_cache_reselects_missing_authored_confidence_and_cadence_once() -> None:
    provider = _ExplicitAuthoredFieldsCombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-explicit-paired-cache.1",
            modalities=("text",),
            private_turn_state_mode="required",
            recorded_cadence_mode="shadow",
        ),
        require_explicit_authored_decision_fields=True,
    )
    request = _request(revision=3, call="call:explicit-paired-cache")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(request)

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 2
    assert proposal.confidence == 8_100
    assert proposal.action_intents
    plan = proposal.proposed_changes[0].payload.value()
    assert plan["cadence_profile"] == "conversational"
    assert plan["recorded_cadence_mode"] == "shadow"
    correction = json.loads(provider.calls[1][-1]["content"])
    assert correction["repair"] == "replace_entire_expression"
    assert correction["structural_failure"].endswith("cadence,confidence")


@pytest.mark.asyncio
async def test_paired_structural_reselection_propagates_its_episode_disposition() -> None:
    provider = _ExplicitAuthoredFieldsCombinedProvider(
        correction_episode_disposition="append"
    )
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-explicit-paired-episode.1",
            modalities=("text",),
            private_turn_state_mode="required",
            recorded_cadence_mode="shadow",
        ),
        require_explicit_authored_decision_fields=True,
    )
    request = _request(revision=3, call="call:explicit-paired-episode")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(request)

    assert expression.episode_disposition == "append"
    assert expression.raw_proposal["episode_disposition"] == "append"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_paired_cache_repeated_authored_field_omission_is_typed_technical_failure() -> None:
    provider = _ExplicitAuthoredFieldsCombinedProvider(remains_invalid=True)
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-explicit-paired-cache-terminal.1",
            modalities=("text",),
            private_turn_state_mode="required",
            recorded_cadence_mode="shadow",
        ),
        require_explicit_authored_decision_fields=True,
    )
    request = _request(revision=3, call="call:explicit-paired-cache-terminal")

    await cognition.appraisal.propose(request)
    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.expression.propose(request)

    assert caught.value.failure_code == "authored_expression_reselection_invalid"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_paired_invalid_correction_episode_disposition_is_typed_terminal() -> None:
    provider = _ExplicitAuthoredFieldsCombinedProvider(
        correction_episode_disposition="wait_forever"
    )
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-explicit-paired-invalid-episode.1",
            modalities=("text",),
            private_turn_state_mode="required",
            recorded_cadence_mode="shadow",
        ),
        require_explicit_authored_decision_fields=True,
    )
    request = _request(revision=3, call="call:explicit-paired-invalid-episode")

    await cognition.appraisal.propose(request)
    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.expression.propose(request)

    assert caught.value.failure_code == "authored_expression_reselection_invalid"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_paired_cached_expression_keeps_its_frozen_source_alias_map() -> None:
    canonical_ref = "dialogue:observation:qq:cached-paired-turn:sha256:" + "a" * 64

    class _AliasedCombinedProvider(_CombinedProvider):
        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "No durable appraisal change is needed.",
                        "behavior_tendency": "choose_own_response",
                        "stance": "attend",
                        "display_strategy": "model_owned",
                        "confidence": 7000,
                    },
                    "expression_draft": {
                        "private_turn_state": {
                            "inner_state_summary": "我注意到她刚才报告自己遇到了一件烦心事。",
                            "attended_source_refs": ["S1"],
                        },
                        "timing_choice": "now",
                        "beats": [{"modality": "text", "text": "听着确实挺烦的。"}],
                        "stance": "attend_to_report",
                        "brief_rationale": "Respond to the pinned report.",
                        "world_claims": [
                            {
                                "claim_text": "对方刚才报告自己遇到了一件烦心事",
                                "scope": "counterpart_history",
                                "source_refs": ["S1"],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )

    provider = _AliasedCombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-paired-alias-cache.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:paired-alias-cache").model_copy(
        update={
            "model_content_json": compact_chat_model_facing_context(
                json.dumps(
                    {
                        "world_revision": 3,
                        "slices": {
                            "recent_dialogue": {
                                "availability": "available",
                                "items": [
                                    {
                                        "item_ref": canonical_ref,
                                        "value": {
                                            "speaker": "counterpart",
                                            "text": "刚遇到了一件烦心事。",
                                        },
                                    }
                                ],
                            }
                        },
                    },
                    ensure_ascii=False,
                )
            )
        }
    )

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(request)

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 1
    assert '"S1":"' + canonical_ref + '"' in provider.calls[0][1]["content"]
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.attended_source_refs == (canonical_ref,)
    plan = proposal.proposed_changes[0].payload.value()
    assert plan["world_claims"][0]["source_refs"] == [canonical_ref]


@pytest.mark.asyncio
async def test_paired_expression_reconsiders_after_authoritative_world_change() -> None:
    provider = _ContextShiftPrivateTurnStateProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-state-context-shift.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    origin = _request(revision=3, call="call:paired-origin")

    await cognition.appraisal.propose(origin)
    expression = await cognition.expression.propose(
        origin.model_copy(
            update={
                "call_id": "call:paired-after-acceptance",
                "capsule_id": "c" * 64,
                "evaluated_world_revision": 4,
                "evaluated_deliberation_revision": 7,
                "evaluated_ledger_sequence": 19,
                "model_content_json": json.dumps(
                    {
                        "world_revision": 4,
                        "accepted_affect": {
                            "dimension": "hurt",
                            "source_ref": "event:appraisal:accepted",
                        },
                    }
                ),
            }
        )
    )

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 2
    assert "accepted_affect" in provider.calls[1][1]["content"]
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.inner_state_summary.startswith("刚接受的受伤状态")


@pytest.mark.asyncio
async def test_paired_expression_reconsiders_when_only_audit_cursor_advances() -> None:
    provider = _ContextShiftPrivateTurnStateProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-state-same-world.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    origin = _request(revision=3, call="call:paired-same-world-origin")

    await cognition.appraisal.propose(origin)
    expression = await cognition.expression.propose(
        origin.model_copy(
            update={
                "call_id": "call:paired-same-world-expression",
                "evaluated_deliberation_revision": 8,
                "evaluated_ledger_sequence": 21,
            }
        )
    )

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 2
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.inner_state_summary.startswith("我重新看过当前请求")


@pytest.mark.asyncio
async def test_combined_cognition_preserves_scoped_private_shared_history_authority() -> None:
    identity = CompanionIdentityFrame(
        companion_name="沈知栀",
        counterpart_name="Geoff",
        shared_history_facts=("沈知栀和 Geoff 在 QQ 的读书兴趣群认识。",),
    )
    source_ref = companion_identity_source_ref(identity, scope="shared_history")

    class _ScopedHistoryProvider(_CombinedProvider):
        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "affect": "no_change",
                        "brief_rationale": "No durable appraisal change is needed.",
                        "behavior_tendency": "choose_own_response",
                        "stance": "recognize_shared_context",
                        "display_strategy": "model_owned",
                        "confidence": 7000,
                        "meanings": [],
                        "attribution": "uncertain",
                        "severity": 0,
                        "components": [],
                    },
                    "expression_draft": {
                        "private_turn_state": {
                            "inner_state_summary": "这次私聊让我想起了我们认识的那个群。",
                            "attended_source_refs": [source_ref],
                        },
                        "timing_choice": "now",
                        "beats": [{"modality": "text", "text": "原来从群里聊到私聊了。"}],
                        "stance": "notice_shared_context",
                        "brief_rationale": "Use the exact scoped identity source.",
                        "world_claims": [
                            {
                                "claim_text": "沈知栀和 Geoff 在 QQ 的读书兴趣群认识",
                                "scope": "shared_history",
                                "source_refs": [source_ref],
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )

    provider = _ScopedHistoryProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        identity_frame=identity,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-shared-history.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:private-shared-history")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:private-shared-history-expression"})
    )

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.attended_source_refs == (source_ref,)
    payload = proposal.proposed_changes[0].payload.value()
    assert payload["world_claims"][0]["scope"] == "shared_history"
    assert payload["world_claims"][0]["source_refs"] == [source_ref]
    assert '"scope":"shared_history"' in provider.calls[0][0]["content"]
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_combined_private_state_failure_reselects_the_complete_expression() -> None:
    provider = _PrivateTurnStateRepairProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-turn-state-repair.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:private-turn-state-repair")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:private-turn-state-repair-expression"})
    )

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 3
    repair_material = json.dumps(provider.calls[1], ensure_ascii=False)
    assert "旧回复只是先前的无状态选择" not in repair_material
    repair_prompt = provider.calls[1][-1]["content"]
    assert "complete replacement" in repair_prompt
    assert "previous visible reply as a constraint" in repair_prompt
    preserved_messages = [
        message["content"] for message in provider.calls[1] if message["role"] == "assistant"
    ]
    assert len(preserved_messages) == 1
    preserved = json.loads(preserved_messages[0])
    assert set(preserved) == {"appraisal_draft"}
    assert preserved_messages[0] == json.dumps(
        preserved,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    assert (
        preserved["appraisal_draft"]["brief_rationale"]
        == "The insult changes the immediate emotional situation."
    )
    assert "code=" in repair_prompt
    assert "path=" in repair_prompt
    assert proposal.private_turn_state is not None
    assert proposal.action_intents
    payload = proposal.proposed_changes[0].payload.value()
    assert payload["beat_drafts"][0]["inline_text"] == "这话挺伤人的，我不想装作没事。"


@pytest.mark.asyncio
async def test_fresh_expression_usage_excludes_discarded_paired_reselection() -> None:
    provider = _MeteredPrivateTurnStateRepairProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-metered-private-turn-state-repair.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:metered-private-state-repair")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:metered-private-state-repair-expression"})
    )

    assert len(provider.calls) == 3
    assert expression.input_tokens == 20
    assert expression.output_tokens == 5
    assert expression.usage is not None
    assert expression.usage.provider_usage_ref == "usage:paired:3"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_state",
    [
        {
            "contract": "private-turn-state.999",
            "inner_state_summary": "未知契约。",
            "attended_source_refs": [],
        },
        {
            "inner_state_summary": "带了契约外字段。",
            "attended_source_refs": [],
            "motive_category": "hard_coded",
        },
        ["not", "an", "object"],
        {
            "inner_state_summary": "   ",
            "attended_source_refs": [],
        },
    ],
    ids=["invalid_contract", "extra_field", "wrong_type", "empty_summary"],
)
async def test_paired_private_state_shape_failures_reselect_the_full_expression(
    invalid_state: object,
) -> None:
    provider = _InvalidPrivateStateShapeProvider(invalid_state)
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-state-shape-repair.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:private-state-shape-repair")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:private-state-shape-expression"})
    )

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 3
    assert "这句来自无效状态" not in json.dumps(provider.calls[1], ensure_ascii=False)
    assert not any(message["role"] == "assistant" for message in provider.calls[1])
    assert "complete replacement" in provider.calls[1][-1]["content"]
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.inner_state_summary.startswith("这句话让我")


@pytest.mark.asyncio
async def test_combined_invalid_private_state_recall_choice_reselects_once() -> None:
    provider = _InvalidPrivateStateRecallChoiceProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-invalid-private-recall.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:invalid-private-recall")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:invalid-private-recall-expression"})
    )

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 3
    repair_material = json.dumps(provider.calls[1], ensure_ascii=False)
    assert "这句无效的旧表达不能进入下一次选择" not in repair_material
    assert "这个 appraisal 外字段也不能进入重选上下文" not in repair_material
    assert "complete replacement" in provider.calls[1][-1]["content"]
    preserved_messages = [
        message["content"] for message in provider.calls[1] if message["role"] == "assistant"
    ]
    assert len(preserved_messages) == 1
    preserved = json.loads(preserved_messages[0])
    assert set(preserved) == {"appraisal_draft"}
    assert preserved_messages[0] == json.dumps(
        preserved,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    repair_prompt = provider.calls[1][-1]["content"]
    assert "code=" in repair_prompt
    assert "path=" in repair_prompt
    assert proposal.private_turn_state is not None
    assert proposal.action_intents


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_recall", "expected_code", "expected_path"),
    (
        (
            {"query_text": "合并非法 Recall 原文 limit", "limit": 7},
            "recall_choice.out_of_range",
            "recall_request.limit",
        ),
        (
            {
                "query_text": "合并非法 Recall 原文 kinds",
                "memory_kinds": ["semantic", "episodic"],
            },
            "recall_choice.noncanonical",
            "recall_request.memory_kinds",
        ),
        (
            {
                "query_text": "合并非法 Recall 原文 extra",
                "unknown_filter": "private-value",
            },
            "recall_choice.unexpected_field",
            "recall_request",
        ),
    ),
)
async def test_combined_invalid_recall_payload_gets_one_sanitized_final_reselection(
    invalid_recall: dict[str, object],
    expected_code: str,
    expected_path: str,
) -> None:
    invalid_marker = str(invalid_recall["query_text"])
    provider = _InvalidRecallPayloadCombinedProvider((invalid_recall,))
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-combined-invalid-recall.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )

    output = await cognition.appraisal.propose(
        _request(revision=3, call="call:combined-invalid-recall")
    )

    assert output.raw_proposal["proposal_kind"] == "decision"
    assert len(provider.calls) == 2
    correction_messages = provider.calls[1]
    correction_prompt = correction_messages[-1]["content"]
    assert f"code={expected_code}" in correction_prompt
    assert f"path={expected_path}" in correction_prompt
    assert invalid_marker not in json.dumps(correction_messages, ensure_ascii=False)
    assert "without requesting another recall" in correction_prompt


@pytest.mark.asyncio
async def test_combined_invalid_recall_reselection_cannot_open_a_third_role_call() -> None:
    first_invalid_marker = "合并第一次非法 Recall 原文不能回灌"
    provider = _InvalidRecallPayloadCombinedProvider(
        (
            {"query_text": first_invalid_marker, "limit": 7},
            {
                "query_text": "合并第二次非法 Recall",
                "unknown_filter": "private-value",
            },
        )
    )
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-combined-invalid-recall-terminal.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.appraisal.propose(
            _request(revision=3, call="call:combined-invalid-recall-terminal")
        )

    assert caught.value.failure_code == "recall_choice_reselection_invalid"
    assert len(provider.calls) == 2
    assert first_invalid_marker not in json.dumps(provider.calls[1], ensure_ascii=False)


@pytest.mark.asyncio
async def test_combined_invalid_recall_final_cannot_trigger_another_shape_repair() -> None:
    provider = _InvalidRecallThenInvalidFinalCombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-combined-invalid-recall-final.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.appraisal.propose(
            _request(revision=3, call="call:combined-invalid-recall-final")
        )

    assert caught.value.failure_code == "recall_choice_reselection_invalid"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_public_combined_invalid_recall_final_stops_before_a_third_role_call(
    tmp_path,
) -> None:
    provider = _InvalidRecallThenInvalidFinalCombinedProvider()
    capabilities = ExpressionDraftCapabilities(
        profile_id="expression:test-public-invalid-recall-final.1",
        modalities=("text",),
        private_turn_state_mode="required",
    )
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=capabilities,
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-public-invalid-recall-final.sqlite",
        config=replace(_config(), expression_capabilities=capabilities),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:public-invalid-recall-final",
                text="我只是想让你听我说。",
                observed_at=NOW,
                trace_id="trace:public-invalid-recall-final",
            )
        )
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "observed_only"
    assert len(provider.calls) == 2
    assert evidence.projection.actions == ()
    audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    failed_audits = [audit for audit in audits if audit["failure_code"] is not None]
    failure_codes = [audit["failure_code"] for audit in failed_audits]
    assert failure_codes.count("recall_choice_reselection_invalid") == 1


@pytest.mark.asyncio
async def test_combined_cognition_repairs_undeclared_subject_mixup_with_character_model() -> None:
    provider = _SubjectMixupCombinedProvider()
    reviewer = _SourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        appraisal_model=reviewer,
        source_closure_model=reviewer,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="已经聊过一阵",
            stable_identity_facts=("来自嘉兴",),
        ),
    )
    request = _request(revision=3, call="call:source-closure")

    await cognition.appraisal.propose(request)
    assert reviewer.calls == []

    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:source-closure-expression"})
    )

    rendered = json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert "家里那边怎么了？" in rendered
    assert "嘉兴" not in rendered
    assert len(provider.calls) == 2
    assert len(reviewer.calls) == 1


@pytest.mark.asyncio
async def test_paired_appraisal_defers_source_review_until_exact_origin_expression_is_authoritative() -> (
    None
):
    class _TransientSourceClosureReviewer:
        model = "transient-source-closure"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            if len(self.calls) == 1:
                raise TimeoutError("review provider timed out")
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "r": "The authored reply adds no external fact.",
                },
                ensure_ascii=False,
            )

    provider = _OrdinaryCombinedProvider()
    reviewer = _TransientSourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
    )

    request = _request(revision=3, call="call:paired-reviewer-recovery")
    appraisal = await cognition.appraisal.propose(request)

    assert appraisal.raw_proposal
    assert len(provider.calls) == 1
    assert reviewer.calls == []

    expression = await cognition.expression.propose(request)

    assert expression.raw_proposal
    assert len(provider.calls) == 1
    assert len(reviewer.calls) == 2
    assert reviewer.calls[0] == reviewer.calls[1]


@pytest.mark.asyncio
async def test_paired_source_closure_gives_private_and_visible_boundaries_to_reselection() -> None:
    provider = _UnsupportedLifeCombinedProvider()
    reviewer = _UnsupportedLifeSourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-paired-source-feedback.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:paired-source-feedback")

    appraisal = await cognition.appraisal.propose(request)

    assert len(provider.calls) == 1
    assert reviewer.calls == []

    trace = BoundedSourceClosureTraceCollector()
    with capture_isolated_source_closure_trace(trace):
        expression = await cognition.expression.propose(request)

    correction_instruction = provider.calls[1][-1]["content"]
    assert all(message["role"] != "assistant" for message in provider.calls[1])
    correction_envelope = json.loads(correction_instruction)
    assert correction_envelope["contract"] == "source-closure-reselection.2"
    assert correction_envelope["authority"] == "categorical_failure_only_not_context_or_evidence"
    assert (
        correction_envelope["output_contract"]["contract"]
        == "expression-source-reselection-direct.1"
    )
    assert set(correction_envelope) == {
        "contract",
        "authority",
        "companion_life_authority_availability",
        "character_reselection_affordance",
        "final_source_self_check",
        "output_contract",
        "rejected_candidate_sha256",
        "rejected_categories",
        "task",
        "unpinned_companion_life_event_boundary",
    }
    assert correction_envelope["rejected_categories"] == {
        "ci": [],
        "v": ["undeclared_external_assertion"],
        "p": [],
    }
    assert correction_envelope["companion_life_authority_availability"] == {
        "authority": "pinned_claim_capability_only",
        "behavior_advice": False,
        "empty_semantics": "no_pinned_authority_available_not_event_did_not_happen",
        "current_situation_source_refs": [],
        "active_occurrence_source_refs": [],
        "committed_experience_source_refs": [],
    }
    rejected_raw = json.dumps(
        provider._invalid_expression(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert (
        correction_envelope["rejected_candidate_sha256"]
        == sha256(rejected_raw.encode()).hexdigest()
    )
    assert "complete replacement" in correction_envelope["task"]
    correction_messages_json = json.dumps(provider.calls[1], ensure_ascii=False)
    assert "刚才在宿舍翻书，现在看到你了。" not in correction_messages_json
    assert "晚上我正在宿舍翻书" not in correction_messages_json
    assert "untrusted_draft_json" not in correction_messages_json
    assert "untrusted_draft_source_ref_aliases" not in correction_messages_json
    assert "reviewer_reason" not in correction_messages_json
    assert "The current observation supports reading the message" not in correction_messages_json

    rendered = json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert "宿舍" not in rendered
    assert "我看到你这句了" in rendered
    assert len(provider.calls) == 2
    assert len(reviewer.calls) == 2
    assert expression.winning_model_call_id != appraisal.winning_model_call_id
    assert expression.winning_request_hash == _provider_request_hash(
        provider.calls[1],
        temperature=0.0,
    )
    traced = trace.snapshot()
    assert [event.stage for event in traced] == ["initial_rejection"]
    assert traced[0].visible_beat_texts == ("刚才在宿舍翻书，现在看到你了。",)
    assert traced[0].ci == ()
    assert traced[0].v == ("undeclared_external_assertion",)
    assert traced[0].p == ()


@pytest.mark.asyncio
async def test_source_trace_follows_single_call_into_post_appraisal_expression_delegate() -> None:
    provider = _DelegatedUnsupportedLifeCombinedProvider()
    reviewer = _UnsupportedLifeSourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-delegated-source-trace.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:delegated-source-trace")
    await cognition.appraisal.propose(request)

    trace = BoundedSourceClosureTraceCollector()
    with capture_isolated_source_closure_trace(trace):
        expression = await cognition.expression.propose(
            request.model_copy(update={"call_id": "call:delegated-source-trace-expression"})
        )

    assert "我看到你这句了" in json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert len(provider.calls) == 3
    assert len(reviewer.calls) == 2
    assert [event.stage for event in trace.snapshot()] == ["initial_rejection"]
    assert trace.snapshot()[0].visible_beat_texts == ("刚才在宿舍翻书，现在看到你了。",)


@pytest.mark.asyncio
async def test_paired_source_trace_distinguishes_invalid_reselection_before_second_review() -> None:
    class _InvalidReselectionProvider(_UnsupportedLifeCombinedProvider):
        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            if len(self.calls) > 1:
                return "{not-valid-json"
            return json.dumps(
                {
                    "appraisal_draft": self._appraisal(),
                    "expression_draft": self._invalid_expression(),
                },
                ensure_ascii=False,
            )

    provider = _InvalidReselectionProvider()
    reviewer = _UnsupportedLifeSourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-invalid-source-reselection-trace.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:invalid-source-reselection-trace")
    await cognition.appraisal.propose(request)

    trace = BoundedSourceClosureTraceCollector()
    with capture_isolated_source_closure_trace(trace):
        with pytest.raises(ValidationTechnicalFailure) as caught:
            await cognition.expression.propose(request)

    assert caught.value.failure_code == "authored_expression_reselection_invalid"
    assert [event.stage for event in trace.snapshot()] == [
        "initial_rejection",
        "pre_final_source_review",
        "reselection_output_invalid_before_review",
    ]
    assert trace.snapshot()[1].as_dict()["record_kind"] == (
        "candidate_materialization_failure"
    )
    assert trace.snapshot()[2].surface_extraction == "unavailable"
    assert trace.snapshot()[2].visible_beat_texts == ()


@pytest.mark.asyncio
async def test_paired_source_closure_private_boundary_reaches_reselection() -> None:
    private_fragment = "我注意到他为了价格争执了很久"
    source_ref = "observation:1"

    class _C7CombinedProvider(_UnsupportedLifeCombinedProvider):
        @staticmethod
        def _invalid_expression() -> dict[str, object]:
            return {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": (
                        f"{private_fragment}，觉得这事有点消耗人，也有些好奇"
                        "摊贩到底哪里说得不清楚。"
                    ),
                    "attended_source_refs": [source_ref],
                },
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [
                    {
                        "modality": "text",
                        "text": (
                            "听着就挺让人上火的……他到底是临时涨价，还是每样东西都说得不一样？"
                        ),
                        "role": "opening",
                    }
                ],
                "stance": "关心但不盲目附和，顺着细节问一句。",
                "brief_rationale": "回应对方的烦躁感，同时想知道争执的具体缘由。",
                "confidence": 9100,
                "world_claims": [
                    {
                        "claim_text": "对方说学校门口的摊贩把价格说得乱七八糟。",
                        "scope": "counterpart_history",
                        "source_refs": [source_ref],
                    }
                ],
            }

        @staticmethod
        def _corrected_expression() -> dict[str, object]:
            return {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我听着有点替他上火，但不想替他补全事情经过。",
                    "attended_source_refs": [source_ref],
                },
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [
                    {
                        "modality": "text",
                        "text": "听着就挺让人上火的。",
                        "role": "opening",
                    }
                ],
                "stance": "先接住情绪，不虚构争执细节。",
                "brief_rationale": "只依据当前消息表达主观反应。",
                "confidence": 8800,
                "world_claims": [],
            }

    class _C7Reviewer:
        model = "c7-source-closure"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            request = json.loads(messages[1]["content"])
            if request.get("output_contract", {}).get("contract") == "source-closure-appeal.4":
                return json.dumps(
                    {
                        **request["rejected_categories"],
                        "r": "The rejected private-state boundary remains unsupported.",
                    },
                    ensure_ascii=False,
                )
            if len(self.calls) == 1:
                return json.dumps(
                    {
                        "ci": [],
                        "v": [],
                        "p": ["undeclared_external_assertion"],
                        "visible_findings": [
                            {
                                "category": "undeclared_external_assertion",
                                "visible_span": "他到底是临时涨价",
                                "claim_index": None,
                                "source_relation": "unclosed",
                                "source_refs": [],
                            }
                        ],
                        "r": "The private-state factual premise is unsupported.",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "r": "The complete replacement contains no unsupported occurrence.",
                },
                ensure_ascii=False,
            )

    provider = _C7CombinedProvider()
    reviewer = _C7Reviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-c7-coordinate-rebinding.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:c7-coordinate-rebinding").model_copy(
        update={
            "trigger_message": _request(
                revision=3,
                call="call:c7-coordinate-rebinding",
            ).trigger_message.model_copy(
                update={"text": ("今天学校门口那个摊贩把价格说得乱七八糟，我跟他争了半天。")}
            )
        }
    )

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(request)

    correction_instruction = provider.calls[1][-1]["content"]
    correction_envelope = json.loads(correction_instruction)
    assert correction_envelope["contract"] == "source-closure-reselection.2"
    assert correction_envelope["authority"] == "categorical_failure_only_not_context_or_evidence"
    assert (
        correction_envelope["output_contract"]["contract"]
        == "expression-source-reselection-direct.1"
    )
    assert set(correction_envelope) == {
        "contract",
        "authority",
        "companion_life_authority_availability",
        "character_reselection_affordance",
        "final_source_self_check",
        "output_contract",
        "rejected_candidate_sha256",
        "rejected_categories",
        "task",
        "unpinned_companion_life_event_boundary",
    }
    assert correction_envelope["rejected_categories"] == {
        "ci": [],
        "v": ["undeclared_external_assertion"],
        "p": [],
    }
    rejected_raw = json.dumps(
        _C7CombinedProvider._invalid_expression(),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert (
        correction_envelope["rejected_candidate_sha256"]
        == sha256(rejected_raw.encode()).hexdigest()
    )
    correction_messages_json = json.dumps(provider.calls[1], ensure_ascii=False)
    assert private_fragment not in correction_messages_json
    assert "untrusted_draft_json" not in correction_messages_json
    assert "untrusted_draft_source_ref_aliases" not in correction_messages_json
    assert "The private-state factual premise is unsupported." not in correction_messages_json
    assert all(message["role"] != "assistant" for message in provider.calls[1])
    assert len(provider.calls) == 2
    assert len(reviewer.calls) == 2
    rendered = json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert private_fragment not in rendered
    assert "听着就挺让人上火的" in rendered


@pytest.mark.asyncio
async def test_paired_corrected_pure_undeclared_false_positive_fails_closed() -> None:
    uncertainty = "具体发生了什么我还不知道"

    class _UncertainCorrectionProvider(_UnsupportedLifeCombinedProvider):
        @staticmethod
        def _corrected_expression() -> dict[str, object]:
            return {
                "private_turn_state": {
                    "inner_state_summary": f"{uncertainty}，我现在愿意先听她说。",
                    "attended_source_refs": ["observation:1"],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你说吧，我听着。"}],
                "stance": "listen",
                "brief_rationale": "Choose again without inventing a background.",
                "confidence": 8000,
                "world_claims": [],
            }

    class _FalsePositiveReviewer:
        model = "paired-source-appeal"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            request = json.loads(messages[1]["content"])
            is_appeal = (
                request.get("output_contract", {}).get("contract") == "source-closure-appeal.4"
            )
            if is_appeal:
                raise AssertionError("production source closure must not call appeal")
            if len(self.calls) == 1:
                return json.dumps(
                    {
                        "ci": [],
                        "v": ["undeclared_external_assertion"],
                        "p": ["undeclared_external_assertion"],
                        "visible_findings": [
                            {
                                "category": "undeclared_external_assertion",
                                "visible_span": "刚才在宿舍翻书",
                                "claim_index": None,
                                "source_relation": "unclosed",
                                "source_refs": [],
                            }
                        ],
                        "r": "The invented life scene has no source.",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": ["undeclared_external_assertion"],
                    "visible_findings": [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": "你说吧，我听着",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                    ],
                    "r": "Mistakenly classified uncertainty as an occurrence.",
                },
                ensure_ascii=False,
            )

    provider = _UncertainCorrectionProvider()
    reviewer = _FalsePositiveReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-paired-source-appeal.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:paired-source-appeal")

    await cognition.appraisal.propose(request)
    trace = BoundedSourceClosureTraceCollector()
    with capture_isolated_source_closure_trace(trace):
        with pytest.raises(ValidationTechnicalFailure) as caught:
            await cognition.expression.propose(request)

    assert caught.value.failure_code == "authored_expression_reselection_invalid"
    assert len(provider.calls) == 2
    assert len(reviewer.calls) == 2
    contracts = [
        json.loads(call[1]["content"])["output_contract"]["contract"] for call in reviewer.calls
    ]
    assert contracts == [
        "source-closure-review.7",
        "source-closure-review.7",
    ]
    assert [event.stage for event in trace.snapshot()] == [
        "initial_rejection",
        "corrected_rejection",
    ]
    assert trace.snapshot()[1].visible_beat_texts == ("你说吧，我听着。",)
    assert trace.snapshot()[1].v == ("undeclared_external_assertion",)
    assert trace.snapshot()[1].p == ()


@pytest.mark.asyncio
async def test_paired_initial_rejection_reselects_without_appeal() -> None:
    subjective_fragment = "听着真让人火大"

    class _SubjectiveCombinedProvider(_UnsupportedLifeCombinedProvider):
        @staticmethod
        def _invalid_expression() -> dict[str, object]:
            return {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我听着有点替他上火，但没有替他补全经过。",
                    "attended_source_refs": ["observation:1"],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": subjective_fragment}],
                "stance": "share_a_subjective_reaction",
                "brief_rationale": "React without adding an occurrence.",
                "confidence": 8_100,
                "world_claims": [],
            }

    class _InitialFalsePositiveReviewer:
        model = "initial-source-appeal"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            request = json.loads(messages[1]["content"])
            if request.get("output_contract", {}).get("contract") == "source-closure-appeal.4":
                raise AssertionError("production source closure must not call appeal")
            return json.dumps(
                {
                    "ci": [],
                    "v": (["undeclared_external_assertion"] if len(self.calls) == 1 else []),
                    "p": [],
                    "visible_findings": (
                        [
                            {
                                "category": "undeclared_external_assertion",
                                "visible_span": subjective_fragment,
                                "claim_index": None,
                                "source_relation": "unclosed",
                                "source_refs": [],
                            }
                        ]
                        if len(self.calls) == 1
                        else []
                    ),
                    "r": (
                        "Mistakenly treated a subjective reaction as an occurrence."
                        if len(self.calls) == 1
                        else "The replacement adds no unsupported occurrence."
                    ),
                },
                ensure_ascii=False,
            )

    provider = _SubjectiveCombinedProvider()
    reviewer = _InitialFalsePositiveReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-initial-source-appeal.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:initial-source-appeal")

    await cognition.appraisal.propose(request)
    trace = BoundedSourceClosureTraceCollector()
    with capture_isolated_source_closure_trace(trace):
        expression = await cognition.expression.propose(request)

    assert len(provider.calls) == 2
    assert len(reviewer.calls) == 2
    rendered = json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert subjective_fragment not in rendered
    assert "我看到你这句了" in rendered
    assert [event.stage for event in trace.snapshot()] == ["initial_rejection"]


@pytest.mark.asyncio
async def test_paired_source_closure_accepts_supported_character_reselection() -> None:
    subjective_fragment = "听着就挺让人火大的"
    source_ref = "observation:1"

    class _C5CombinedProvider(_UnsupportedLifeCombinedProvider):
        @staticmethod
        def _invalid_expression() -> dict[str, object]:
            return {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": (
                        "我注意到他今天和摊贩争执了很久，先是替他觉得烦，"
                        "又有点想知道到底是哪一方把价格说乱了；但现在很晚了，"
                        "不想把话说得太热闹。"
                    ),
                    "attended_source_refs": [source_ref],
                },
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [
                    {
                        "modality": "text",
                        "text": (
                            f"{subjective_fragment}……到底是他临时乱涨价，"
                            "还是一开始就没把价格说清楚？"
                        ),
                        "role": "opening",
                    },
                    {
                        "modality": "text",
                        "text": "不过你能跟他争半天，也蛮有耐心的。",
                        "role": "afterthought",
                    },
                ],
                "stance": "关心但不盲目站队，带一点轻微调侃地追问细节。",
                "brief_rationale": "回应他的情绪，同时保留判断。",
                "confidence": 9300,
                "world_claims": [
                    {
                        "claim_text": "你说今天在学校门口和摊贩争了半天。",
                        "scope": "counterpart_history",
                        "source_refs": [source_ref],
                    }
                ],
            }

        @staticmethod
        def _corrected_expression() -> dict[str, object]:
            return {
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
                            f"{subjective_fragment}。到底是他临时乱涨价，"
                            "还是一开始就没把价格说清楚？"
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
            }

    class _C5Reviewer:
        model = "c5-source-closure"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []
            self.ordinary_calls = 0

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            request = json.loads(messages[1]["content"])
            if request.get("output_contract", {}).get("contract") == "source-closure-appeal.4":
                raise AssertionError("production source closure must not call appeal")
            self.ordinary_calls += 1
            if self.ordinary_calls == 1:
                return json.dumps(
                    {
                        "ci": [],
                        "v": ["undeclared_external_assertion"],
                        "p": ["undeclared_external_assertion"],
                        "visible_findings": [
                            {
                                "category": "undeclared_external_assertion",
                                "visible_span": "他临时乱涨价",
                                "claim_index": None,
                                "source_relation": "unclosed",
                                "source_refs": [],
                            }
                        ],
                        "r": "The initial draft includes unsupported factual premises.",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "r": "The corrected expression is source-closed.",
                },
                ensure_ascii=False,
            )

    provider = _C5CombinedProvider()
    reviewer = _C5Reviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-c5-source-appeal.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:c5-source-appeal").model_copy(
        update={
            "trigger_message": _request(
                revision=3,
                call="call:c5-source-appeal",
            ).trigger_message.model_copy(update={"text": "我今天在学校门口和摊贩争了半天。"})
        }
    )

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(request)

    assert len(provider.calls) == 2
    assert len(reviewer.calls) == 2
    assert subjective_fragment in json.dumps(
        expression.raw_proposal,
        ensure_ascii=False,
    )
    assert expression.raw_proposal["private_turn_state"]["inner_state_summary"].startswith(
        "我对这件事有点生气"
    )


@pytest.mark.asyncio
async def test_paired_source_closure_usage_reaches_the_expression_model_output() -> None:
    provider = _MeteredOrdinaryCombinedProvider()
    reviewer = _MeteredSourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
    )
    request = _request(revision=3, call="call:metered-source-closure")

    appraisal = await cognition.appraisal.propose(request)
    assert appraisal.input_tokens == 20
    assert appraisal.output_tokens == 5
    assert reviewer.calls == []

    expression = await cognition.expression.propose(request)

    assert expression.input_tokens == 24
    assert expression.output_tokens == 7
    assert expression.usage is not None
    assert expression.usage.provider_usage_ref.startswith("provider-usage:combined:")
    assert len(provider.calls) == 1
    assert len(reviewer.calls) == 1
    assert expression.winning_model_call_id == appraisal.winning_model_call_id
    assert expression.winning_request_hash == _provider_request_hash(
        provider.calls[0],
        temperature=0.7,
    )


@pytest.mark.asyncio
async def test_paired_source_closure_usage_is_persisted_in_the_expression_audit(
    tmp_path,
) -> None:
    provider = _MeteredOrdinaryCombinedProvider()
    reviewer = _MeteredSourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "metered-source-closure-audit.sqlite",
        config=replace(_config(), expression_episode_mode="off"),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:metered-source-closure",
                text="我想慢慢说件事。",
                observed_at=NOW,
                trace_id="trace:metered-source-closure",
            )
        )
        projection = app.export_replay_evidence().projection
    finally:
        app.close()

    persisted_usages = tuple(
        payload["usage"]
        for item in projection.model_result_audits
        if (payload := json.loads(item.audit_json)).get("usage") is not None
    )
    assert outcome.status == "action_authorized"
    assert any(
        usage["input_tokens"] == 24
        and usage["output_tokens"] == 7
        and usage["provider_usage_ref"].startswith("provider-usage:combined:")
        for usage in persisted_usages
    )


@pytest.mark.asyncio
async def test_application_waits_for_retried_source_review_before_authorizing_action(
    tmp_path,
) -> None:
    class _BlockingRetryReviewer:
        model = "blocking-retry-reviewer"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []
            self.retry_started = asyncio.Event()
            self.release_retry = asyncio.Event()

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            if len(self.calls) == 1:
                raise TimeoutError("first reviewer attempt failed")
            if len(self.calls) == 2:
                self.retry_started.set()
                await self.release_retry.wait()
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "r": "The authored expression adds no external fact.",
                },
                ensure_ascii=False,
            )

    provider = _OrdinaryCombinedProvider()
    reviewer = _BlockingRetryReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=reviewer,
    )
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "source-review-before-action.sqlite",
        config=replace(_config(), expression_episode_mode="off"),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=transport,
        now=NOW,
    )
    try:
        responding = asyncio.create_task(
            app.respond(
                InboundTurn(
                    platform="test",
                    platform_user_id="user.1",
                    platform_message_id="message:review-before-action",
                    text="我想慢慢说件事。",
                    observed_at=NOW,
                    trace_id="trace:review-before-action",
                )
            )
        )
        await asyncio.wait_for(reviewer.retry_started.wait(), timeout=1)

        assert not responding.done()
        # The visible expression is authored once. Durable Appraisal/Affect
        # now run only when the background scheduler claims their trigger.
        assert len(provider.calls) == 1
        assert transport.bodies == []

        reviewer.release_retry.set()
        outcome = await asyncio.wait_for(responding, timeout=3)

        assert outcome.status == "action_authorized"
        assert len(provider.calls) == 1
        assert len(reviewer.calls) >= 2
    finally:
        reviewer.release_retry.set()
        app.close()


@pytest.mark.asyncio
async def test_paired_metered_cognition_preserves_provider_json_mode() -> None:
    provider = _JsonMeteredOrdinaryCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    request = _request(revision=3, call="call:metered-json")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:metered-json-expression"})
    )

    assert expression.usage is not None
    assert expression.input_tokens == 20
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_combined_prompt_reasserts_final_envelope_when_recall_is_unavailable() -> None:
    provider = _CombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)

    await cognition.appraisal.propose(_request(revision=3, call="call:shape-contract"))

    system = provider.calls[0][0]["content"]
    envelope_at = system.rindex("COMBINED OUTPUT ENVELOPE")
    expression_at = system.index("EXPRESSION DRAFT CONTRACT")
    assert envelope_at > expression_at
    assert '{"appraisal_draft":{...},"expression_draft":{...}}' in system[envelope_at:]
    assert '"recall_request":{...}' not in system[envelope_at:]
    assert "never use content" in system[envelope_at:]
    assert "confidence is an integer from 0 through 10000" in system[envelope_at:]


@pytest.mark.asyncio
async def test_invalid_world_claim_prefix_is_rewritten_by_character_model() -> None:
    provider = _UnsupportedGreetingClaimProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    base = _request(revision=3, call="call:unsupported-greeting")
    request = base.model_copy(
        update={
            "trigger_message": base.trigger_message.model_copy(update={"text": "午安捏"}),
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "logical_time": NOW.isoformat(),
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "items": [
                                {
                                    "item_ref": "situation:planned-club",
                                    "value": {
                                        "activity_slices": [
                                            {
                                                "activity_kind": ("social.literature_club_meetup"),
                                                "status": "planned",
                                            }
                                        ]
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

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:unsupported-greeting-expression"})
    )
    proposal = DecisionProposal.model_validate_json(json.dumps(expression.raw_proposal))
    visible = [
        change.payload.value()["beat_drafts"][0]["inline_text"]
        for change in proposal.proposed_changes
        if change.kind == "expression_plan_transition"
    ]

    assert visible == ["午安呀。你今天过得怎么样？"]
    assert expression.model_version != "local-expression-failsafe.1"
    assert len(provider.calls) == 3


@pytest.mark.asyncio
async def test_invalid_primary_uses_one_contextual_backup_before_expression_acceptance() -> None:
    backup = _OrdinaryCombinedProvider()
    primary = _FailingProviderWithFallback(backup)
    cognition = SingleCallInboundCognition(flash_model=primary)
    request = _request(revision=3, call="call:backup-primary-failure").model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "situation:desk",
                                    "value": {"activity": "整理桌面"},
                                }
                            ],
                        },
                        "relationship_slice": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "relationship:primary",
                                    "value": {"stage": "new_acquaintance"},
                                }
                            ],
                        },
                        "affect_episodes": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "affect:recent-hurt",
                                    "value": {"dimension": "hurt", "target_intensity_bp": 4200},
                                }
                            ],
                        },
                    },
                },
                ensure_ascii=False,
            )
        }
    )

    appraisal = await cognition.appraisal.propose(request)
    expression_request = request.model_copy(update={"call_id": "call:backup-expression"})
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await cognition.expression.propose(expression_request)
    expression = await cognition.expression.recover(expression_request, "main_exception")

    assert appraisal.model_id == "combined-flash"
    assert expression.model_id == "combined-flash"
    assert len(primary.calls) == 2
    assert len(backup.calls) == 2
    assert "整理桌面" in backup.calls[0][1]["content"]
    assert "new_acquaintance" in backup.calls[0][1]["content"]
    assert "recent-hurt" in backup.calls[0][1]["content"]
    assert "我在听" in json.dumps(expression.raw_proposal, ensure_ascii=False)


@pytest.mark.asyncio
async def test_backup_failure_is_scoped_independently_to_appraisal_and_expression() -> None:
    backup = _AlwaysFailProvider()
    primary = _FailingProviderWithFallback(backup)
    cognition = SingleCallInboundCognition(flash_model=primary)
    request = _request(revision=3, call="call:backup-also-fails")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await cognition.appraisal.propose(request)

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(request, "main_exception")

    assert len(primary.calls) == 1
    assert len(backup.calls) == 2


def test_composition_can_disable_an_unreviewable_discovered_recovery_author() -> None:
    backup = _AlwaysFailProvider()
    primary = _FailingProviderWithFallback(backup)

    cognition = SingleCallInboundCognition(
        flash_model=primary,
        discover_recovery_model=False,
    )

    assert cognition._recovery_model is None  # noqa: SLF001
    assert cognition._recovery_expression is None  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_code, expected_inventory_calls, expected_reviewer_contracts",
    [
        (
            "inventory_invalid",
            2,
            [],
        ),
        (
            "coverage_invalid",
            1,
            [
                "candidate-external-proposition-coverage.1",
                "candidate-external-proposition-coverage.1",
            ],
        ),
    ],
)
async def test_backup_candidate_validation_failure_preserves_its_typed_code(
    failure_code: str,
    expected_inventory_calls: int,
    expected_reviewer_contracts: list[str],
) -> None:
    """A terminal truth-boundary failure cannot become another author failure."""

    primary = _AlwaysFailProvider()
    backup = _QuickExpressionProvider()
    inventory = _TerminalCandidateValidationInventory(failure_code)
    reviewer = _TerminalCandidateValidationReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=backup,
        source_closure_model=reviewer,
        recovery_source_closure_model=reviewer,
        candidate_external_proposition_inventory_model=inventory,
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.expression.recover(
            _request(revision=3, call=f"call:backup-{failure_code}"),
            "main_exception",
        )

    assert caught.value.failure_code == failure_code
    assert primary.calls == []
    assert len(backup.calls) == 1
    assert len(inventory.calls) == expected_inventory_calls
    assert sorted(reviewer.contracts) == sorted(expected_reviewer_contracts)


@pytest.mark.asyncio
async def test_provisional_episode_uses_independent_recovery_provider() -> None:
    backup = _ProvisionalBackupProvider()
    primary = _FailingProviderWithFallback(backup)
    cognition = SingleCallInboundCognition(flash_model=primary)

    expression = await cognition.expression.propose_provisional(
        _request(revision=3, call="call:independent-provisional-expression")
    )
    appraisal_lane = await cognition.appraisal.propose_provisional(
        _request(revision=3, call="call:independent-provisional-appraisal")
    )

    assert expression.model_id == "independent-provisional"
    assert appraisal_lane.model_id == "independent-provisional"
    assert not primary.calls
    assert len(backup.calls) == 2


@pytest.mark.asyncio
async def test_shadow_observer_is_never_inferred_from_formal_recovery_provider() -> None:
    formal_recovery = _ProvisionalBackupProvider()
    primary = _FailingProviderWithFallback(formal_recovery)
    cognition = SingleCallInboundCognition(flash_model=primary)
    request = _request(revision=3, call="call:shadow-observer-not-configured")

    assert cognition.expression.shadow_observer_provider_available(request) is False
    with pytest.raises(RuntimeError, match="shadow observer is not configured"):
        await cognition.expression.propose_shadow_observer(request)

    assert formal_recovery.calls == []


def test_shadow_observer_rejects_the_formal_recovery_client_alias() -> None:
    shared = _ProvisionalBackupProvider()

    with pytest.raises(ValueError, match="independent provider client"):
        SingleCallInboundCognition(
            flash_model=_AlwaysFailProvider(),
            recovery_model=shared,
            expression_episode_observer_model=shared,
        )


def test_shadow_observer_rejects_shared_runtime_resources() -> None:
    shared_client = object()
    formal_recovery = _ProvisionalBackupProvider()
    observer = _ProvisionalBackupProvider()
    formal_recovery.client = shared_client
    observer.client = shared_client

    with pytest.raises(ValueError, match="must not share client"):
        SingleCallInboundCognition(
            flash_model=_AlwaysFailProvider(),
            recovery_model=formal_recovery,
            expression_episode_observer_model=observer,
        )


def test_shadow_observer_holds_no_formal_recall_or_review_capability() -> None:
    recovery = _ProvisionalBackupProvider()
    observer = _ProvisionalBackupProvider()
    reviewer = _SourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=_AlwaysFailProvider(),
        recovery_model=recovery,
        expression_episode_observer_model=observer,
        source_closure_model=reviewer,
        candidate_external_proposition_inventory_model=reviewer,
    )

    cognition.expression.install_recall_coordinator(object())  # type: ignore[arg-type]
    observer_adapter = cognition._expression_episode_observer
    recovery_adapter = cognition._recovery_expression

    assert observer_adapter is not None
    assert recovery_adapter is not None
    assert observer_adapter._recall is None
    assert observer_adapter._source_closure_reviewer is None
    assert observer_adapter._report_relative_reviewer is None
    assert observer_adapter._candidate_external_proposition_inventory_model is None
    assert observer_adapter._recovery_contexts is not recovery_adapter._recovery_contexts


@pytest.mark.asyncio
async def test_blocked_shadow_observer_does_not_block_formal_recovery() -> None:
    primary = _AlwaysFailProvider()
    formal_recovery = _ProvisionalBackupProvider()
    formal_recovery.model = "same-recovery-checkpoint"
    observer = _BlockingShadowObserverProvider()
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=formal_recovery,
        expression_episode_observer_model=observer,
    )
    observer_request = _request(
        revision=3,
        call="call:blocked-shadow-observer",
    )
    recovery_request = _request(
        revision=3,
        call="call:formal-recovery-while-shadow-blocked",
    )

    shadow_task = asyncio.create_task(
        cognition.expression.propose_shadow_observer(observer_request)
    )
    await asyncio.wait_for(observer.started.wait(), timeout=0.5)
    try:
        recovered = await asyncio.wait_for(
            cognition.expression.recover(recovery_request, "main_exception"),
            timeout=0.5,
        )
    finally:
        observer.release.set()
    shadow = await asyncio.wait_for(shadow_task, timeout=0.5)

    assert recovered.model_id == "same-recovery-checkpoint"
    assert shadow.model_id == recovered.model_id
    assert len(formal_recovery.calls) == 1
    assert len(observer.calls) == 1


@pytest.mark.asyncio
async def test_blocked_formal_recovery_does_not_block_shadow_observer() -> None:
    formal_recovery = _BlockingShadowObserverProvider()
    observer = _ProvisionalBackupProvider()
    observer.model = "same-recovery-checkpoint"
    cognition = SingleCallInboundCognition(
        flash_model=_AlwaysFailProvider(),
        recovery_model=formal_recovery,
        expression_episode_observer_model=observer,
    )

    recovery_task = asyncio.create_task(
        cognition.expression.recover(
            _request(revision=3, call="call:blocked-formal-recovery"),
            "main_exception",
        )
    )
    await asyncio.wait_for(formal_recovery.started.wait(), timeout=0.5)
    try:
        shadow = await asyncio.wait_for(
            cognition.expression.propose_shadow_observer(
                _request(revision=3, call="call:observer-during-formal-recovery")
            ),
            timeout=0.5,
        )
        assert recovery_task.done() is False
    finally:
        formal_recovery.release.set()
    recovered = await asyncio.wait_for(recovery_task, timeout=0.5)

    assert shadow.model_id == recovered.model_id == "same-recovery-checkpoint"
    assert len(observer.calls) == 1
    assert len(formal_recovery.calls) == 1


@pytest.mark.asyncio
async def test_open_shadow_observer_circuit_does_not_open_formal_recovery() -> None:
    formal_recovery = _ProvisionalBackupProvider()
    formal_recovery.model = "same-recovery-checkpoint"
    observer = _CircuitShadowObserverProvider()
    cognition = SingleCallInboundCognition(
        flash_model=_AlwaysFailProvider(),
        recovery_model=formal_recovery,
        expression_episode_observer_model=observer,
    )
    observer.circuit_breaker.record_failure()

    with pytest.raises(ModelCircuitOpenError):
        await cognition.expression.propose_shadow_observer(
            _request(revision=3, call="call:open-shadow-observer-circuit")
        )
    recovered = await cognition.expression.recover(
        _request(revision=3, call="call:recovery-after-shadow-circuit"),
        "main_exception",
    )

    assert recovered.model_id == "same-recovery-checkpoint"
    assert len(formal_recovery.calls) == 1
    assert observer.calls == []


@pytest.mark.asyncio
async def test_existing_failover_does_not_call_its_fallback_twice() -> None:
    backup = _AlwaysFailProvider()
    primary = _FailoverAlreadyUsedProvider(backup)
    cognition = SingleCallInboundCognition(flash_model=primary)
    request = _request(revision=3, call="call:failover-already-used")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await cognition.appraisal.propose(request)

    recovered = await cognition.appraisal.recover(request, "main_exception")

    assert recovered.model_version == "single-call-inbound-cognition.2"
    assert len(primary.calls) == 1
    assert not backup.calls


@pytest.mark.asyncio
async def test_timeout_recovery_uses_one_contextual_backup_before_local_silence() -> None:
    primary = _AlwaysFailProvider()
    backup = _QuickExpressionProvider()
    cognition = SingleCallInboundCognition(flash_model=primary, recovery_model=backup)
    request = _request(revision=3, call="call:timeout-backup").model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "current_situation": {
                            "availability": "available",
                            "items": [
                                {"source_ref": "situation:desk", "value": {"activity": "整理桌面"}}
                            ],
                        },
                        "relationship_slice": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "relationship:primary",
                                    "value": {"stage": "new_acquaintance"},
                                }
                            ],
                        },
                        "affect_episodes": {
                            "availability": "available",
                            "items": [
                                {"source_ref": "affect:recent-hurt", "value": {"dimension": "hurt"}}
                            ],
                        },
                    }
                },
                ensure_ascii=False,
            )
        }
    )

    output = await cognition.expression.recover(request, "main_timeout")

    assert output.model_id == "backup-flash"
    assert len(backup.calls) == 1
    content = backup.calls[0][1]["content"]
    assert "整理桌面" in content
    assert "new_acquaintance" in content
    assert "recent-hurt" in content


@pytest.mark.asyncio
async def test_public_turn_allows_backup_to_use_the_remaining_technical_recovery_window(
    tmp_path,
) -> None:
    primary = _AlwaysFailProvider()
    backup = _SlowQuickExpressionProvider(delay_seconds=2.7)
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=backup,
    )
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-slow-technical-recovery.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:slow-technical-recovery",
                text="我刚才那件事还没说完。",
                observed_at=NOW,
                trace_id="trace:slow-technical-recovery",
            )
        )
        delivery = await app.drain_actions_once()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert delivery.status == "settled"
    assert primary.calls and len(primary.calls) == 1
    assert backup.started == backup.completed == 1
    assert transport.bodies == ["我接到了，刚才只是慢了一拍。"]


@pytest.mark.asyncio
async def test_public_turn_allows_one_backup_correction_then_fails_closed(
    tmp_path,
) -> None:
    """A recovery candidate gets one correction, never an unbounded retry loop."""

    primary = _LooseExpressionShapeProvider({}, repair=False)
    backup = _LooseExpressionShapeProvider({}, repair=False)
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=backup,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-state-required.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-backup-correction-slot.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:backup-correction-slot",
                text="我只是想让你听我说。",
                observed_at=NOW,
                trace_id="trace:backup-correction-slot",
            )
        )
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    assert outcome.status == "observed_only"
    assert [audit["failure_code"] for audit in audits[-2:]] == [
        "corrective_invalid",
        "backup_invalid",
    ]
    assert len(primary.calls) == 2
    assert len(backup.calls) == 2


@pytest.mark.asyncio
async def test_paired_appraisal_and_fresh_expression_yield_independently_bound_proposals() -> None:
    provider = _CombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="Geoff",
            relationship_frame="刚认识的群友",
        ),
    )

    appraisal_output = await cognition.appraisal.propose(
        _request(revision=3, call="call:appraisal")
    )
    # Acceptance advances World before expression is audited. The later
    # proposal must come from a fresh request, not rebound provider bytes.
    expression_output = await cognition.expression.propose(
        _request(revision=5, call="call:expression")
    )

    appraisal = DecisionProposal.model_validate_json(json.dumps(appraisal_output.raw_proposal))
    expression = DecisionProposal.model_validate_json(json.dumps(expression_output.raw_proposal))
    assert len(provider.calls) == 2
    assert appraisal.proposal_id != expression.proposal_id
    assert (
        appraisal.evidence_refs[0].ref_id == expression.evidence_refs[0].ref_id == "observation:1"
    )
    assert appraisal.proposed_changes[0].kind == "appraisal_transition"
    assert appraisal.proposed_changes[1].kind == "affect_transition"
    assert expression.evaluated_world_revision == 5
    assert len(expression.action_intents) == 2
    assert expression.action_intents[0].kind == "reply"
    assert "appraisal_draft" in provider.calls[0][0]["content"]
    assert "expression_draft" in provider.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_paired_first_pass_sees_ready_prefetch_without_an_extra_model_call() -> None:
    provider = _CombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    embedding = _ReadyPairedPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:paired:first-pass",
                memory_kind="reflective",
                source_item_ref="impression:paired:first-pass",
                source_slice="private_impressions",
                source_refs=("event:impression:paired:first-pass",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="PrivateImpressionAccepted",
                        ref="event:impression:paired:first-pass",
                        source_world_revision=2,
                        immutable_hash="f" * 64,
                    ),
                ),
                source_world_revision=2,
                text="我不想因为一句关于机器人的气话就替对方定性。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=NOW,
                privacy_class="withhold",
                authority="defeasible_interpretation",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        trigger_ref="event:observation:1",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="机器人",
        accessibility_seed="draw:paired:first-pass",
        trigger_ref="event:observation:1",
    )
    assert await asyncio.to_thread(embedding.finished.wait, 0.5)
    cognition.expression.install_recall_coordinator(coordinator)
    base = _request(revision=3, call="call:paired-ready-prefetch")
    request = base.model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 0,
                    "ledger_sequence": 0,
                    "logical_time": NOW.isoformat(),
                    "slices": {
                        # A production Capsule commonly fills this lane before
                        # the parallel attention candidate arrives.  The
                        # candidate actually audited as model-visible must
                        # survive the bounded chat compaction.
                        "private_impressions": {
                            "availability": "available",
                            "source_refs": ["event:old:1", "event:old:2"],
                            "items": [
                                {
                                    "item_ref": "impression:old:1",
                                    "privacy_class": "withhold",
                                    "value": {"reflection_summary": "旧印象一"},
                                },
                                {
                                    "item_ref": "impression:old:2",
                                    "privacy_class": "withhold",
                                    "value": {"reflection_summary": "旧印象二"},
                                },
                            ],
                        }
                    },
                },
                ensure_ascii=False,
            )
        }
    )

    output = await cognition.appraisal.propose(request)

    assert len(provider.calls) == 1
    assert "我不想因为一句关于机器人的气话就替对方定性" in provider.calls[0][1]["content"]
    assert output.prefetch_trace is not None
    assert verify_trusted_recall_trace(output.prefetch_trace).mode == "prefetch"
    assert tuple(item.phase for item in output.presented_prefetch_traces) == ("initial",)
    assert output.presented_prefetch_traces[0].model_call_id == output.winning_model_call_id
    assert (
        output.presented_prefetch_traces[0].trace.audit.result_hash
        == output.prefetch_trace.audit.result_hash
    )


@pytest.mark.asyncio
async def test_paired_required_private_state_can_choose_recall_before_final_expression() -> None:
    provider = _PrivateStateRecallThenCombinedProvider()
    capabilities = ExpressionDraftCapabilities(
        profile_id="expression:test-private-state-recall.1",
        modalities=("text",),
        private_turn_state_mode="required",
    )
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=capabilities,
    )
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:private-state",
                memory_kind="reflective",
                source_item_ref="impression:private-recall",
                source_slice="private_impressions",
                source_refs=(_PRIVATE_RECALL_SOURCE_REF,),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="PrivateImpressionAccepted",
                        ref=_PRIVATE_RECALL_SOURCE_REF,
                        source_world_revision=2,
                        immutable_hash="d" * 64,
                    ),
                ),
                source_world_revision=2,
                text="我不想因为一句关于机器人的气话就急着替对方定性。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=NOW,
                privacy_class="withhold",
                authority="defeasible_interpretation",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        trigger_ref="event:observation:1",
    )
    cognition.expression.install_recall_coordinator(coordinator)
    request = _request(revision=3, call="call:private-state-recall").model_copy(
        update={
            "evaluated_deliberation_revision": 0,
            "evaluated_ledger_sequence": 0,
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 0,
                    "ledger_sequence": 0,
                    "logical_time": NOW.isoformat(),
                    "slices": {},
                },
                ensure_ascii=False,
            ),
        }
    )

    await cognition.appraisal.propose(request)
    expression_cursor = RecallCursor(
        world_revision=5,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    coordinator.refresh(
        cursor=expression_cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        sources=RecallCorpusSources(),
        trigger_ref=request.trigger_ref,
    )
    expression = await cognition.expression.propose(
        request.model_copy(
            update={
                "call_id": "call:private-state-recall-expression",
                "evaluated_world_revision": 5,
                "model_content_json": json.dumps(
                    {
                        "world_revision": 5,
                        "deliberation_revision": 0,
                        "ledger_sequence": 0,
                        "logical_time": NOW.isoformat(),
                        "slices": {},
                    },
                    ensure_ascii=False,
                ),
            }
        )
    )

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 4
    first_system_prompt = provider.calls[0][0]["content"]
    envelope_at = first_system_prompt.rindex("COMBINED OUTPUT ENVELOPE")
    advertised_envelope = first_system_prompt[envelope_at:]
    assert '{"appraisal_draft":{...},"expression_draft":{...}}' in advertised_envelope
    assert (
        '{"private_turn_state":{...},"recall_request":{...}}'
        in advertised_envelope
    )
    assert "contains no appraisal_draft or expression_draft" in advertised_envelope
    assert "no further recall" in provider.calls[1][-1]["content"]
    assert '"S1":"' + _PRIVATE_RECALL_SOURCE_REF + '"' in provider.calls[1][-1]["content"]
    assert "complete replacement" in provider.calls[2][-1]["content"]
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.attended_source_refs == (_PRIVATE_RECALL_SOURCE_REF,)
    assert expression.recall_trace is not None


@pytest.mark.asyncio
async def test_paired_backup_preserves_ready_prefetch_provenance() -> None:
    primary = _AlwaysFailProvider()
    backup = _CombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=backup,
    )
    embedding = _ReadyPairedPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:paired:backup",
                memory_kind="reflective",
                source_item_ref="impression:paired:backup",
                source_slice="private_impressions",
                source_refs=(_PAIRED_BACKUP_RECALL_SOURCE_REF,),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="PrivateImpressionAccepted",
                        ref=_PAIRED_BACKUP_RECALL_SOURCE_REF,
                        source_world_revision=2,
                        immutable_hash="e" * 64,
                    ),
                ),
                source_world_revision=2,
                text="关于机器人的这段回忆必须和备用模型的结果一起留下来源。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=NOW,
                privacy_class="withhold",
                authority="defeasible_interpretation",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        trigger_ref="event:observation:1",
    )
    coordinator.schedule_prefetch(
        expected_cursor=cursor,
        query_text="机器人",
        accessibility_seed="draw:paired:backup",
        trigger_ref="event:observation:1",
    )
    assert await asyncio.to_thread(embedding.finished.wait, 0.5)
    cognition.expression.install_recall_coordinator(coordinator)
    base = _request(revision=3, call="call:paired-prefetch-backup")
    request = base.model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 0,
                    "ledger_sequence": 0,
                    "logical_time": NOW.isoformat(),
                    "slices": {},
                },
                ensure_ascii=False,
            )
        }
    )

    output = await cognition.appraisal.propose(request)

    assert len(primary.calls) == 1
    assert len(backup.calls) == 1
    assert "关于机器人的这段回忆必须和备用模型的结果一起留下来源" in (backup.calls[0][1]["content"])
    assert '"S1":"' + _PAIRED_BACKUP_RECALL_SOURCE_REF + '"' in backup.calls[0][1]["content"]
    assert output.prefetch_trace is not None
    trace = verify_trusted_recall_trace(output.prefetch_trace)
    assert trace.hits[0].document.source_item_ref == "impression:paired:backup"


@pytest.mark.asyncio
async def test_expression_review_recovery_inherits_pinned_prefetch_context() -> None:
    class _RecallThenAnswerProvider:
        model = "recall-then-answer"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            if len(self.calls) == 1:
                return json.dumps(
                    {
                        "recall_request": {
                            "query_text": "机器人那句话关联的旧印象",
                            "memory_kinds": ["reflective"],
                            "limit": 3,
                        }
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "这句话挺刺的。"}],
                    "stance": "answer_after_recall",
                    "brief_rationale": "Choose after seeing the selected memory.",
                    "confidence": 7600,
                    "world_claims": [],
                },
                ensure_ascii=False,
            )

    class _ReviewerFailsOneCandidate:
        model = "reviewer-fails-one-candidate"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            if len(self.calls) <= 2:
                raise RuntimeError("independent reviewer unavailable")
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "r": "The recovery draft adds no unsupported fact.",
                },
                ensure_ascii=False,
            )

    primary = _RecallThenAnswerProvider()
    backup = _QuickExpressionProvider()
    reviewer = _ReviewerFailsOneCandidate()
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=backup,
        source_closure_model=reviewer,
    )
    embedding = _ReadyPairedPrefetchEmbedding()
    cursor = RecallCursor(world_revision=3, deliberation_revision=0, ledger_sequence=0)
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:paired:expression-recovery",
                memory_kind="reflective",
                source_item_ref="impression:paired:expression-recovery",
                source_slice="private_impressions",
                source_refs=(_PAIRED_BACKUP_RECALL_SOURCE_REF,),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="PrivateImpressionAccepted",
                        ref=_PAIRED_BACKUP_RECALL_SOURCE_REF,
                        source_world_revision=2,
                        immutable_hash="e" * 64,
                    ),
                ),
                source_world_revision=2,
                text="这段选择性想起的私人印象必须继续随技术恢复可见。",
                actor_ref="agent:companion",
                subject_refs=("user:primary",),
                occurred_from=NOW,
                privacy_class="withhold",
                authority="defeasible_interpretation",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        trigger_ref="event:observation:1",
    )
    cognition.expression.install_recall_coordinator(coordinator)
    request = _request(
        revision=3,
        call="call:paired-expression-review-recovery",
    ).model_copy(
        update={
            "evaluated_deliberation_revision": 0,
            "evaluated_ledger_sequence": 0,
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "deliberation_revision": 0,
                    "ledger_sequence": 0,
                    "logical_time": NOW.isoformat(),
                    "slices": {},
                },
                ensure_ascii=False,
            ),
        }
    )

    output = await cognition.expression.propose(request)

    assert backup.calls
    assert "这段选择性想起的私人印象必须继续随技术恢复可见" in (backup.calls[0][1]["content"])
    assert output.recall_trace is not None
    trace = verify_trusted_recall_trace(output.recall_trace)
    assert trace.hits[0].document.source_item_ref == ("impression:paired:expression-recovery")


@pytest.mark.asyncio
async def test_local_appraiser_handles_high_signal_text_without_a_keyword_route_override() -> None:
    appraiser = _SeparateAppraisalProvider()
    expression_provider = _QuickExpressionProvider()
    cognition = SingleCallInboundCognition(
        flash_model=expression_provider,
        appraisal_model=appraiser,
    )

    appraisal_request = _request(revision=3, call="call:local-appraisal")
    assert appraisal_request.trigger_message is not None
    appraisal_request = appraisal_request.model_copy(
        update={
            "trigger_message": appraisal_request.trigger_message.model_copy(
                update={"text": "我真的很失望，你刚才完全没认真听。"}
            )
        }
    )
    appraisal_output = await cognition.appraisal.propose(appraisal_request)
    expression_output = await cognition.expression.propose(
        _request(revision=4, call="call:local-expression")
    )

    appraisal = DecisionProposal.model_validate_json(json.dumps(appraisal_output.raw_proposal))
    expression = DecisionProposal.model_validate_json(json.dumps(expression_output.raw_proposal))
    assert appraisal_output.model_id == "qwen-local"
    assert expression_output.model_id == "backup-flash"
    assert len(appraiser.calls) == 1
    assert len(expression_provider.calls) == 1
    assert appraisal.proposed_changes[0].kind == "appraisal_transition"
    assert len(expression.action_intents) == 1


@pytest.mark.asyncio
async def test_busy_local_appraiser_falls_back_without_queueing_or_losing_appraisal() -> None:
    local = _BusySeparateAppraisalProvider()
    remote = _QuickExpressionProvider()
    cognition = SingleCallInboundCognition(
        flash_model=remote,
        appraisal_model=local,
    )

    output = await cognition.appraisal.propose(
        _request(revision=3, call="call:busy-local-appraisal")
    )

    appraisal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert local.calls == 1
    assert len(remote.calls) == 1
    assert output.model_id == "backup-flash"
    assert appraisal.affect_decision == "no_change"


@pytest.mark.asyncio
async def test_separate_appraisal_expression_still_runs_source_closure_review() -> None:
    appraiser = _SeparateAppraisalProvider()
    expression_provider = _SeparateSourceMixupExpressionProvider()
    reviewer = _SourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=expression_provider,
        appraisal_model=appraiser,
        source_closure_model=reviewer,
    )

    await cognition.appraisal.propose(_request(revision=3, call="call:separate-source-appraisal"))
    expression_output = await cognition.expression.propose(
        _request(revision=4, call="call:separate-source-expression")
    )

    rendered = json.dumps(expression_output.raw_proposal, ensure_ascii=False)
    assert "家里那边怎么了？" in rendered
    assert "嘉兴" not in rendered
    assert len(expression_provider.calls) == 2
    assert len(reviewer.calls) == 2


@pytest.mark.asyncio
async def test_invalid_appraisal_fails_closed_without_discarding_valid_expression() -> None:
    provider = _InvalidAppraisalValidExpressionProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)

    appraisal_output = await cognition.appraisal.propose(
        _request(revision=3, call="call:invalid-appraisal")
    )
    expression_output = await cognition.expression.propose(
        _request(revision=5, call="call:valid-expression")
    )

    appraisal = DecisionProposal.model_validate_json(json.dumps(appraisal_output.raw_proposal))
    expression = DecisionProposal.model_validate_json(json.dumps(expression_output.raw_proposal))
    assert len(provider.calls) == 2
    assert appraisal.proposed_changes == ()
    assert appraisal.affect_decision == "no_change"
    assert "invalid" in appraisal.brief_rationale.lower()
    assert len(expression.action_intents) == 1


@pytest.mark.asyncio
async def test_paired_appraisal_reselects_a_target_below_its_pinned_lower_bound() -> None:
    provider = _BelowBoundThenValidCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)

    output = await cognition.appraisal.propose(
        _request(
            revision=3,
            call="call:paired-affect-target-bound",
            hurt_minimum_bp=4200,
        )
    )
    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))

    assert len(provider.calls) == 2
    correction = provider.calls[1][-1]["content"]
    assert "dimension=hurt" in correction
    assert "selected=100" in correction
    assert "minimum=4200" in correction
    assert proposal.proposed_changes[1].payload.value()["component_targets"] == [
        {"dimension": "hurt", "target_intensity_bp": 4300}
    ]


@pytest.mark.asyncio
async def test_paired_appraisal_records_technical_failure_when_reselection_stays_illegal() -> None:
    provider = _BelowBoundTwiceCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.appraisal.propose(
            _request(
                revision=3,
                call="call:paired-affect-target-bound-still-invalid",
                hurt_minimum_bp=4200,
            )
        )

    assert len(provider.calls) == 2
    assert caught.value.failure_code == "affect_target_reselection_invalid"


@pytest.mark.asyncio
async def test_provider_recovery_does_not_infer_affect_from_keywords() -> None:
    cognition = SingleCallInboundCognition(flash_model=_OrdinaryCombinedProvider())
    request = _request(revision=3, call="call:local-appraisal-recovery")
    output = await cognition.appraisal.recover(request, "main_timeout")

    appraisal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert appraisal.proposed_changes == ()
    assert appraisal.affect_decision == "no_change"
    assert "withheld" in appraisal.brief_rationale


@pytest.mark.asyncio
async def test_background_appraisal_preserves_truthful_model_request_lineage(
    tmp_path,
) -> None:
    provider_temperature = 0.7
    provider = _ContextShiftPrivateTurnStateProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        temperature=provider_temperature,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-paired-lineage.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    expression_adapter = _RecordingModelAdapter(cognition.expression)
    appraisal_adapter = _RecordingModelAdapter(cognition.appraisal)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-vertical.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=expression_adapter,
        quick_recovery=expression_adapter,
        appraisal_model=appraisal_adapter,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:single-call-emotion",
                text="你就是个没用的机器人。",
                observed_at=NOW,
                trace_id="trace:single-call-emotion",
            )
        )
        before_background = app.export_replay_evidence()
        for _ in range(8):
            await app.drain_background_once()
            projection = app.export_replay_evidence().projection
            if projection.appraisals and projection.affect_episodes:
                break
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == 2
    assert before_background.projection.appraisals == ()
    assert before_background.projection.affect_episodes == ()
    event_types = [item.event.event_type for item in evidence.events]
    assert event_types.index("ExpressionPlanAccepted") < event_types.index("AppraisalAccepted")
    assert event_types.index("ActionAuthorized") < event_types.index("AffectEpisodeOpened")
    assert len(evidence.projection.appraisals) == len(evidence.projection.affect_episodes) == 1
    outer_requests = (*appraisal_adapter.requests, *expression_adapter.requests)
    recorded = [
        item.event.payload()
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    actual_provider_request_hashes = {
        _provider_request_hash(messages, temperature=provider_temperature)
        for messages in provider.calls
    }
    assert len(recorded) == len(outer_requests) == len(actual_provider_request_hashes) == 2
    assert {
        json.loads(payload["audit_json"])["request_hash"] for payload in recorded
    } == actual_provider_request_hashes
    outer_call_ids = {request.call_id for request in outer_requests}
    for payload in recorded:
        assert payload["model_call_id"] not in outer_call_ids
        assert payload["evaluated_world_revision"] in {
            request.evaluated_world_revision for request in outer_requests
        }
    assert len({payload["model_call_id"] for payload in recorded}) == 2
    assert len({json.loads(payload["audit_json"])["request_hash"] for payload in recorded}) == 2


@pytest.mark.asyncio
async def test_private_turn_state_is_audited_but_never_becomes_expression_authority(
    tmp_path,
) -> None:
    provider = _PrivateTurnStateCombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-state-vertical.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-private-state.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:single-call-private-state",
                text="你就是个没用的机器人。",
                observed_at=NOW,
                trace_id="trace:single-call-private-state",
            )
        )
        evidence = app.export_replay_evidence()
        second_outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:single-call-private-state-next",
                text="换个话题吧，今天先聊点别的。",
                observed_at=NOW,
                trace_id="trace:single-call-private-state-next",
            )
        )
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert second_outcome.status == "action_authorized"
    assert len(provider.calls) == 2
    second_model_input = "\n".join(item["content"] for item in provider.calls[1])
    assert "这句话先让我觉得被贬低了" not in second_model_input
    expression_audits = tuple(
        item
        for item in evidence.projection.proposal_audits
        if '"private_turn_state"' in item.proposal_json
    )
    assert len(expression_audits) == 1
    assert "这句话先让我觉得被贬低" in expression_audits[0].proposal_json
    expression_events = tuple(
        item.event for item in evidence.events if item.event.event_type == "ExpressionPlanAccepted"
    )
    assert len(expression_events) == 1
    assert "private_turn_state" not in expression_events[0].payload_json
    assert sum(item.event.event_type == "ActionAuthorized" for item in evidence.events) == 2


@pytest.mark.asyncio
async def test_paired_cognition_honors_character_recall_and_replays_trace(
    tmp_path,
) -> None:
    provider = _RecallThenCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-recall.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:single-call-recall",
                text="你就是个没用的机器人。",
                observed_at=NOW,
                trace_id="trace:single-call-recall",
            )
        )
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == 2
    assert "bounded read-only recall result" in provider.calls[1][-1]["content"]
    assert "place it first" not in provider.calls[1][-1]["content"]
    recall_audits = tuple(
        item
        for item in evidence.projection.model_result_audits
        if item.audit_contract in {"model-result-audit.4", "model-result-audit.5"}
    )
    assert recall_audits
    assert all('"query_text":"之前关于机器人的谈话"' in item.audit_json for item in recall_audits)
    assert all('"mode":"prefetch"' in item.audit_json for item in recall_audits)
    assert all(
        '"accessibility_seed":"recall-prefetch:' in item.audit_json for item in recall_audits
    )


@pytest.mark.asyncio
async def test_shadow_episode_runs_real_candidate_without_extra_action(tmp_path) -> None:
    provider = _EpisodeCombinedProvider()
    recovery = _EpisodeCombinedProvider()
    observer = _EpisodeCombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        recovery_model=recovery,
        expression_episode_observer_model=observer,
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-episode-shadow.sqlite",
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:episode-shadow",
                text="你就是个没用的机器人。",
                observed_at=NOW,
                trace_id="trace:episode-shadow",
            )
        )
        await asyncio.sleep(0)
        evidence = app.export_replay_evidence()
        diagnostics = await app.world_health_diagnostics()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    # A resumed claimed episode may need one role-model recovery, but never a
    # local authored beat.
    assert len(provider.calls) <= 2
    assert recovery.calls == []
    assert len(observer.calls) == 1
    # The full fixture intentionally contains two beats; shadow adds none.
    assert len(evidence.projection.actions) == 2
    episode = diagnostics["expression_episode"]
    assert episode["mode"] == "shadow"
    assert episode["turns"] == 1
    assert episode["candidate_valid"] == 1
    assert episode["slot_calls"] == 2


@pytest.mark.asyncio
async def test_shadow_mode_without_observer_never_spends_formal_recovery(
    tmp_path,
) -> None:
    provider = _EpisodeCombinedProvider()
    formal_recovery = _EpisodeCombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        recovery_model=formal_recovery,
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-episode-shadow-unconfigured.sqlite",
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:episode-shadow-unconfigured",
                text="你就是个没用的机器人。",
                observed_at=NOW,
                trace_id="trace:episode-shadow-unconfigured",
            )
        )
        await asyncio.sleep(0)
        diagnostics = await app.world_health_diagnostics()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert formal_recovery.calls == []
    assert diagnostics["expression_episode"]["turns"] == 0


@pytest.mark.asyncio
async def test_shadow_episode_cannot_consume_character_recall_or_add_an_action(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _ShadowRecallCombinedProvider()
    shadow = _ShadowPrivateEpisodeProvider()
    capabilities = ExpressionDraftCapabilities(
        profile_id="expression:test-shadow-recall.1",
        modalities=("text",),
        private_turn_state_mode="required",
    )
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        recovery_model=_ShadowPrivateEpisodeProvider(),
        expression_episode_observer_model=shadow,
        expression_capabilities=capabilities,
    )
    appraisal_shadow_calls: list[ModelInput] = []
    original_appraisal_shadow = cognition.appraisal.propose_provisional

    async def track_forbidden_appraisal_shadow(request: ModelInput) -> ModelOutput:
        appraisal_shadow_calls.append(request)
        return await original_appraisal_shadow(request)

    monkeypatch.setattr(
        cognition.appraisal,
        "propose_provisional",
        track_forbidden_appraisal_shadow,
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-shadow-recall.sqlite",
        config=replace(
            _config(),
            expression_episode_mode="shadow",
            expression_capabilities=capabilities,
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:shadow-character-recall",
                text="你就是个没用的机器人。",
                observed_at=NOW,
                trace_id="trace:shadow-character-recall",
            )
        )
        async with asyncio.timeout(1.0):
            while not shadow.calls:
                await asyncio.sleep(0)
        evidence = app.export_replay_evidence()
        diagnostics = await app.world_health_diagnostics()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == 2
    assert "bounded read-only recall result" in provider.calls[1][-1]["content"]
    assert len(shadow.calls) == 1
    assert len(evidence.projection.actions) == 1
    payloads = tuple(
        item.text for item in evidence.projection.stored_message_payloads if item.text is not None
    )
    assert payloads == ("这句挺刺的，我不想装作没感觉。",)
    assert diagnostics["expression_episode"]["turns"] == 1
    assert appraisal_shadow_calls == []


@pytest.mark.asyncio
async def test_shadow_episode_leaves_one_private_state_reselection_to_the_full_lane(
    tmp_path,
) -> None:
    provider = _ShadowPrivateStateRepairProvider()
    shadow = _ShadowPrivateEpisodeProvider()
    capabilities = ExpressionDraftCapabilities(
        profile_id="expression:test-shadow-private-state-reselection.1",
        modalities=("text",),
        private_turn_state_mode="required",
    )
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        recovery_model=_ShadowPrivateEpisodeProvider(),
        expression_episode_observer_model=shadow,
        expression_capabilities=capabilities,
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-shadow-private-state-reselection.sqlite",
        config=replace(
            _config(),
            expression_episode_mode="shadow",
            expression_capabilities=capabilities,
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:shadow-private-state-reselection",
                text="你就是个没用的机器人。",
                observed_at=NOW,
                trace_id="trace:shadow-private-state-reselection",
            )
        )
        async with asyncio.timeout(1.0):
            while not shadow.calls:
                await asyncio.sleep(0)
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == 2
    correction = provider.calls[1][-1]["content"]
    assert "private_turn_state validation failed code=" in correction
    assert "旧回复只是先前的无状态选择" not in "\n".join(
        item["content"] for item in provider.calls[1]
    )
    assert len(shadow.calls) == 1
    assert len(evidence.projection.actions) == 1


@pytest.mark.asyncio
async def test_source_closure_and_isolated_shadow_both_run_without_extra_action(
    tmp_path,
) -> None:
    provider = _SubjectMixupCombinedProvider()
    shadow = _EpisodeCombinedProvider()
    reviewer = _SourceClosureReviewer()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        recovery_model=_EpisodeCombinedProvider(),
        expression_episode_observer_model=shadow,
        source_closure_model=reviewer,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="geoff",
            relationship_frame="刚认识",
            stable_identity_facts=("来自嘉兴",),
        ),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-source-closure-shadow.sqlite",
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:source-closure-shadow",
                text="我最近有点担心家里那边",
                observed_at=NOW,
                trace_id="trace:source-closure-shadow",
            )
        )
        await asyncio.sleep(0)
        projection = app.export_replay_evidence().projection
        diagnostics = await app.world_health_diagnostics()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    payloads = tuple(
        item.text for item in projection.stored_message_payloads if item.text is not None
    )
    assert any("家里那边怎么了？" in item for item in payloads)
    assert all("嘉兴" not in item for item in payloads)
    assert len(provider.calls) == 2
    assert len(shadow.calls) == 1
    assert any("provisional first beat" in message["content"] for message in shadow.calls[0])
    assert all(
        "provisional first beat" not in message["content"]
        for call in provider.calls
        for message in call
    )
    # The persisted path source-reviews the corrected candidate again before
    # authorizing its Action; the isolated observer remains side-effect free.
    assert len(reviewer.calls) == 2
    assert diagnostics["expression_episode"]["turns"] == 1
    assert len(projection.actions) == 1


@pytest.mark.asyncio
async def test_episode_restart_after_audit_authorizes_without_model_recall(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "single-call-episode-crash.sqlite"
    provider = _EpisodeCombinedProvider()
    recovery = _EpisodeCombinedProvider()
    observer = _EpisodeCombinedProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        recovery_model=recovery,
        expression_episode_observer_model=observer,
    )
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    turn = InboundTurn(
        platform="test",
        platform_user_id="user.1",
        platform_message_id="message:episode-crash-after-audit",
        text="你就是个没用的机器人。",
        observed_at=NOW,
        trace_id="trace:episode-crash-after-audit",
    )
    runtime = app._turns._runtime  # noqa: SLF001 - crash-boundary integration proof
    real_accept = runtime._commit_visible_acceptance  # noqa: SLF001
    crashed = False

    async def crash_once(**kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("crash-after-proposal-audit")
        return await real_accept(**kwargs)

    monkeypatch.setattr(runtime, "_commit_visible_acceptance", crash_once)
    try:
        with pytest.raises(RuntimeError, match="crash-after-proposal-audit"):
            await app.respond(turn)
        async with asyncio.timeout(1.0):
            while not observer.calls:
                await asyncio.sleep(0)
        projection_after_crash = app.export_replay_evidence().projection
        episode = next(
            item
            for item in projection_after_crash.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "claimed"
        assert projection_after_crash.proposal_audits
        assert projection_after_crash.actions == ()
        proposal_audit_identity = tuple(
            (item.event_ref, item.proposal_id, item.model_result_ref)
            for item in projection_after_crash.proposal_audits
        )
        model_audit_identity = tuple(
            (
                item.event_ref,
                item.model_call_id,
                item.model_result_ref,
                item.audit_hash,
            )
            for item in projection_after_crash.model_result_audits
        )
    finally:
        app.close()

    restarted_provider = _EpisodeCombinedProvider()
    restarted_recovery = _EpisodeCombinedProvider()
    restarted_observer = _EpisodeCombinedProvider()
    restarted_cognition = SingleCallInboundCognition(
        flash_model=restarted_provider,
        recovery_model=restarted_recovery,
        expression_episode_observer_model=restarted_observer,
    )
    restarted = build_sqlite_world_v2_turn_application(
        path=path,
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        main_model=restarted_cognition.expression,
        quick_recovery=restarted_cognition.expression,
        appraisal_model=restarted_cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await restarted.respond(turn)
        projection = restarted.export_replay_evidence().projection
    finally:
        restarted.close()

    assert outcome.status == "action_authorized"
    assert restarted_provider.calls == []
    assert restarted_recovery.calls == []
    assert restarted_observer.calls == []
    assert (
        tuple(
            (item.event_ref, item.proposal_id, item.model_result_ref)
            for item in projection.proposal_audits
        )
        == proposal_audit_identity
    )
    assert (
        tuple(
            (
                item.event_ref,
                item.model_call_id,
                item.model_result_ref,
                item.audit_hash,
            )
            for item in projection.model_result_audits
        )
        == model_audit_identity
    )
    assert len(projection.expression_plan_manifests) == 1
    assert len(projection.actions) == 2
    assert all(item.text != "这句话有点伤人。" for item in projection.stored_message_payloads)
    episode = next(
        item for item in projection.trigger_processes if item.process_kind == "expression_episode"
    )
    assert episode.state == "terminal"


@pytest.mark.asyncio
async def test_episode_restart_before_author_resumes_claimed_trigger(tmp_path, monkeypatch) -> None:
    provider = _EpisodeCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-episode-crash-before-author.sqlite",
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=None,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    turn = InboundTurn(
        platform="test",
        platform_user_id="user.1",
        platform_message_id="message:episode-crash-before-author",
        text="今天想安静聊两句。",
        observed_at=NOW,
        trace_id="trace:episode-crash-before-author",
    )
    pinned = app._turns._runtime._pinned_turn  # noqa: SLF001
    assert pinned is not None
    real_audit = pinned.audit_observation
    crashed = False

    async def crash_once(**kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("crash-before-author")
        return await real_audit(**kwargs)

    monkeypatch.setattr(pinned, "audit_observation", crash_once)
    try:
        with pytest.raises(RuntimeError, match="crash-before-author"):
            await app.respond(turn)
        assert provider.calls == []
        claimed = app.export_replay_evidence().projection
        episode = next(
            item for item in claimed.trigger_processes if item.process_kind == "expression_episode"
        )
        assert episode.state == "claimed"

        outcome = await app.respond(turn)
        projection = app.export_replay_evidence().projection
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) <= 3
    episode = next(
        item for item in projection.trigger_processes if item.process_kind == "expression_episode"
    )
    # The crash resumes under the original claim and the source-bound
    # expression request now produces a valid independently audited result.
    assert episode.state == "terminal"
    assert len(episode.attempt_ids) == 1


@pytest.mark.asyncio
async def test_on_episode_appends_only_after_provisional_receipt(tmp_path) -> None:
    provider = _AppendEpisodeProvider()
    recovery = _AppendEpisodeProvider()
    observer = _AppendEpisodeProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        recovery_model=recovery,
        expression_episode_observer_model=observer,
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-episode-append.sqlite",
        config=replace(_config(), expression_episode_mode="on"),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=None,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:episode-append",
                text="今天脑子里有很多事。",
                observed_at=NOW,
                trace_id="trace:episode-append",
            )
        )
        delivery = await app.drain_actions_once()
        background_outcomes = []
        for _ in range(16):
            background_outcomes.append(await app.drain_background_once())
        projection = app.export_replay_evidence().projection
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert delivery is not None and delivery.status == "settled"
    assert all(
        getattr(item, "reason_code", None) != "social_action.ambiguous_proposal_authority"
        for item in background_outcomes
    )
    assert len(provider.calls) == 1
    assert len(recovery.calls) == 1
    assert observer.calls == []
    assert len(projection.expression_plan_manifests) == 2
    assert len(projection.actions) == 2
    states = {item.state for item in projection.actions}
    assert "delivered" in states
    assert "authorized" in states
    episode = next(
        item for item in projection.trigger_processes if item.process_kind == "expression_episode"
    )
    assert episode.state == "terminal"
    assert episode.runtime_outcome_ref is not None
    assert ":append:" in episode.runtime_outcome_ref


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["cancel_pending", "supersede_pending"])
async def test_on_episode_cancels_undispatched_provisional_atomically(
    tmp_path, disposition
) -> None:
    provider = _AppendEpisodeProvider(disposition)
    recovery = _AppendEpisodeProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        recovery_model=recovery,
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / f"single-call-episode-{disposition}.sqlite",
        config=replace(_config(), expression_episode_mode="on"),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=None,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id=f"message:episode-{disposition}",
                text="先别急着回，我还没说完。",
                observed_at=NOW,
                trace_id=f"trace:episode-{disposition}",
            )
        )
        projection = app.export_replay_evidence().projection
    finally:
        app.close()

    assert outcome.status == "observed_only"
    assert len(provider.calls) == 1
    assert len(recovery.calls) == 1
    assert projection.actions
    assert {item.state for item in projection.actions} == {"cancelled"}
    assert {
        item.state
        for item in projection.budget_reservations
        if item.action_id in {action.action_id for action in projection.actions}
    } == {"released"}
    assert {item.state for item in projection.expression_plans} == {"terminated"}
    episode = next(
        item for item in projection.trigger_processes if item.process_kind == "expression_episode"
    )
    assert episode.state == "terminal"
    assert disposition in (episode.runtime_outcome_ref or "")


@pytest.mark.asyncio
async def test_invalid_combined_appraisal_still_authorizes_valid_expression_vertical(
    tmp_path,
) -> None:
    provider = _InvalidAppraisalValidExpressionProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-invalid-appraisal.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:invalid-appraisal-valid-expression",
                text="你好，第一次见。",
                observed_at=NOW,
                trace_id="trace:invalid-appraisal-valid-expression",
            )
        )
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == 1
    assert evidence.projection.appraisals == evidence.projection.affect_episodes == ()
    event_types = [item.event.event_type for item in evidence.events]
    assert "ExpressionPlanAccepted" in event_types
    model_statuses = [
        json.loads(item.event.payload()["audit_json"])["status"]
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    assert model_statuses == ["proposal_validated"]


@pytest.mark.asyncio
async def test_affect_acceptance_validation_failure_is_audited_without_losing_expression(
    tmp_path, monkeypatch
) -> None:
    provider = _CombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)

    def reject_invalid_episode_extension(*_args, **_kwargs):
        raise ValueError("new affect component is not a valid episode extension")

    monkeypatch.setattr(
        "companion_daemon.world_v2.affect_acceptance_runtime."
        "AffectAcceptanceRuntime.accept_runtime_owned",
        reject_invalid_episode_extension,
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-invalid-affect-acceptance.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:invalid-affect-valid-expression",
                text="你就是个没用的机器人。",
                observed_at=NOW,
                trace_id="trace:invalid-affect-valid-expression",
            )
        )
        await app.drain_background_once()
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    event_types = [item.event.event_type for item in evidence.events]
    assert "AppraisalAccepted" in event_types
    assert "AffectEpisodeOpened" not in event_types
    assert "AffectEpisodeUpdated" not in event_types
    assert "ExpressionPlanAccepted" in event_types
    rejection_audits = [
        item.event.payload()
        for item in evidence.events
        if item.event.event_type == "AdvisoryAcceptanceRejected"
    ]
    assert len(rejection_audits) == 1
    assert rejection_audits[0]["advisory_kind"] == "appraisal_affect"
    assert rejection_audits[0]["stage"] == "immediate_emotion_acceptance"
    assert rejection_audits[0]["reason_code"] == "advisory_validation_rejected"


@pytest.mark.asyncio
async def test_inbound_expression_uses_one_generation_call_per_turn(tmp_path) -> None:
    provider = _OrdinaryCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-growing-context.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    turns = 8
    try:
        for index in range(turns):
            before = len(provider.calls)
            outcome = await app.respond(
                InboundTurn(
                    platform="test",
                    platform_user_id="user.1",
                    platform_message_id=f"message:growing-context:{index}",
                    text=f"第{index + 1}段分享：" + ("我想慢慢讲一些很细碎的感受。" * 20),
                    observed_at=NOW,
                    trace_id=f"trace:growing-context:{index}",
                )
            )
            assert outcome.status == "action_authorized", index
            assert len(provider.calls) - before == 1
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert len(provider.calls) == turns
    model_audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    # Each visible expression has one immutable provider result. Durable
    # appraisal acceptance may continue in the background.
    assert len(model_audits) == turns
    assert all(item["status"] == "proposal_validated" for item in model_audits)


@pytest.mark.asyncio
async def test_latency_segment_covers_both_real_provider_requests(tmp_path) -> None:
    clock = _AdvancingClock()
    provider = _TimedCombinedProvider(clock)
    cognition = SingleCallInboundCognition(flash_model=provider)
    latency = ProductionLatencyRecorder(clock_ns=clock)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-latency.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
        latency_recorder=latency,
    )
    try:
        outcome = await app.inbound(
            platform="test",
            platform_user_id="user.1",
            platform_message_id="message:timed-combined",
            text="普通的一句话。",
            observed_at=NOW,
            trace_id="trace:timed-combined",
        )
        samples = app.latency_samples()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == 1
    model_samples = [item for item in samples if item.segment == "model_completion"]
    assert len(model_samples) == 1
    assert model_samples[0].duration_ms == 5_000
    entry_samples = [
        item for item in samples if item.segment == "ingress_to_first_role_provider"
    ]
    assert len(entry_samples) == 1
    assert entry_samples[0].duration_ms == 250
    provider_total = [item for item in samples if item.segment == "role_provider_total"]
    assert len(provider_total) == 1
    assert provider_total[0].duration_ms == 5_000
    context_samples = [item for item in samples if item.segment == "context"]
    assert len(context_samples) == 1
    ledger_samples = [item for item in samples if item.segment == "ledger_commit"]
    assert len(ledger_samples) == 1
    assert ledger_samples[0].duration_ms == 0


@pytest.mark.asyncio
async def test_foreground_semantic_embedding_is_in_the_same_turn_provider_timeline(
    tmp_path,
) -> None:
    clock = _AdvancingClock()

    def embedding_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        inputs = payload["input"]
        clock.advance_ms(300)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [1.0, 0.0]}
                    for index, _text in enumerate(inputs)
                ]
            },
        )

    embedding = OpenAICompatibleRecallEmbedding(
        api_key="secret",
        base_url="https://embedding.test/v1",
        model="semantic-fixture",
        dimensions=2,
        transport=httpx.MockTransport(embedding_handler),
    )
    provider = _TimedCombinedProvider(clock)
    cognition = SingleCallInboundCognition(flash_model=provider)
    latency = ProductionLatencyRecorder(clock_ns=clock)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-embedding-latency.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
        latency_recorder=latency,
        semantic_recall_embedding=embedding,
    )
    trace_id = "trace:timed-embedding-and-role"
    try:
        outcome = await app.inbound(
            platform="test",
            platform_user_id="user.1",
            platform_message_id="message:timed-embedding-and-role",
            text="刚才说到的事情你还记得吗？",
            observed_at=NOW,
            trace_id=trace_id,
        )
        trace = latency.get(trace_id)
        assert trace is not None
        calls = trace.role_provider_timing_evidence()["calls"]
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(calls) == 2
    assert all(call["status"] == "completed" for call in calls)
    assert [call["provider_kind"] for call in calls] == ["auxiliary", "role"]
    assert calls[0]["provider_call_id"].startswith(
        "model-call:foreground-context:"
    )
    assert calls[1]["provider_call_id"].startswith("model-call:")
    assert calls[0]["provider_call_id"] != calls[1]["provider_call_id"]
    samples = {sample.segment: sample.duration_ms for sample in latency.samples()}
    assert samples["model_completion"] == 5_000
    assert samples["role_provider_total"] == 5_000
    assert samples["foreground_provider_total"] == 5_300
    assert samples["ingress_to_first_role_provider"] == 550


@pytest.mark.asyncio
async def test_loose_combined_reply_text_is_reselected_by_the_character_model(
    tmp_path,
) -> None:
    provider = _LooseTextCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-loose-text.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:loose-text",
                text="你好，第一次见。",
                observed_at=NOW,
                trace_id="trace:loose-text",
            )
        )
        await app.drain_actions_once()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_loose_combined_messages_are_reselected_with_two_visible_beats() -> None:
    provider = _LooseMultiMessageCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)

    await cognition.appraisal.propose(_request(revision=3, call="call:loose-messages-appraisal"))
    output = await cognition.expression.propose(
        _request(revision=5, call="call:loose-messages-expression")
    )

    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert output.model_id == "combined-flash"
    assert len(provider.calls) == 3
    assert [item.kind for item in proposal.action_intents] == ["reply", "reply"]


@pytest.mark.parametrize(
    "visible_shape",
    (
        {"beats": ["先说第一件事。", "还有第二件事。"]},
        {
            "responses": [
                {"text": "先说第一件事。"},
                {"modality": "text", "text": "还有第二件事。"},
            ]
        },
        {"reply": ["先说第一件事。", "还有第二件事。"]},
    ),
)
@pytest.mark.asyncio
async def test_common_explicit_text_arrays_preserve_all_visible_beats(
    visible_shape: dict[str, object],
) -> None:
    provider = _LooseExpressionShapeProvider(
        {
            **visible_shape,
            "stance": "continue_in_two_beats",
            "brief_rationale": "Two short messages fit the conversational rhythm.",
        }
    )
    cognition = SingleCallInboundCognition(flash_model=provider)

    await cognition.appraisal.propose(_request(revision=3, call="call:text-array-appraisal"))
    output = await cognition.expression.propose(
        _request(revision=5, call="call:text-array-expression")
    )

    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert len(provider.calls) == 3
    assert "Two short messages fit the conversational rhythm." in json.dumps(
        provider.calls[1],
        ensure_ascii=False,
    )
    assert [item.kind for item in proposal.action_intents] == ["reply", "reply"]


@pytest.mark.parametrize(
    "unsafe_shape",
    (
        {"messages": [{"role": "assistant", "text": "不应被抽取。"}]},
        {"beats": [{"text": "不应被抽取。", "tool": "send_message"}]},
        {"reply": "不应被抽取。", "tool_calls": []},
        {"responses": [{"content": {"text": "不应被递归抽取。"}}]},
    ),
)
@pytest.mark.asyncio
async def test_structural_reselection_rejects_roles_tools_and_nested_text(
    unsafe_shape: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _LooseExpressionShapeProvider(unsafe_shape, repair=False)
    cognition = SingleCallInboundCognition(flash_model=provider)
    request = _request(revision=3, call="call:unsafe-expression")

    await cognition.appraisal.propose(request)

    trigger = request.trigger_message
    assert trigger is not None
    assert not cognition.expression.has_precomputed_advisory(
        trigger_ref=request.trigger_ref,
        observation_ref=trigger.observation_ref,
        event_payload_hash=trigger.event_payload_hash,
    )
    assert "failed its exact contract" in caplog.text
    assert "不应被" not in caplog.text


@pytest.mark.asyncio
async def test_loose_unsupported_autobiography_never_reaches_an_action(tmp_path) -> None:
    provider = _UnsupportedAutobiographyProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-unsupported-autobiography.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:unsupported-autobiography",
                text="你刚才在做什么？",
                observed_at=NOW,
                trace_id="trace:unsupported-autobiography",
            )
        )
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "observed_only"
    assert not transport.bodies
    audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    top_level_audits = [
        audit
        for audit in audits
        if audit["route"]["router_version"]
        not in {"provider-subcall-audit.1", "authored-candidate-audit.1"}
    ]
    assert top_level_audits[-1]["status"] == "recovery_failed"


@pytest.mark.asyncio
async def test_model_expression_is_not_replaced_by_a_local_role_template(
    tmp_path,
) -> None:
    provider = _UnsupportedAutobiographyProvider()
    transport = _DeliveredTransport()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        identity_frame=CompanionIdentityFrame(
            companion_name="林乔",
            counterpart_name="Geoff",
            relationship_frame="刚认识、正在互相了解的人",
            not_an_assistant=True,
        ),
    )
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-role-boundary-failsafe.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:role-boundary-failsafe",
                text="你是我的助手吗？",
                observed_at=NOW,
                trace_id="trace:role-boundary-failsafe",
            )
        )
        delivery = await app.drain_actions_once()
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "observed_only"
    assert delivery.status == "idle"
    assert not transport.bodies
    audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    top_level_audits = [
        audit
        for audit in audits
        if audit["route"]["router_version"]
        not in {"provider-subcall-audit.1", "authored-candidate-audit.1"}
    ]
    assert top_level_audits[-1]["status"] == "recovery_failed"


@pytest.mark.asyncio
async def test_model_owned_world_answer_is_not_rewritten_by_a_keyword_gate(
    tmp_path,
) -> None:
    provider = _TimeoutAfterCombinedProvider()
    transport = _DeliveredTransport()
    cognition = SingleCallInboundCognition(flash_model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-world-probe-timeout.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:world-probe-timeout",
                text="不是角色设定里的爱好，我问的是今天真的发生了什么。",
                observed_at=NOW,
                trace_id="trace:world-probe-timeout",
            )
        )
        delivery = await app.drain_actions_once()
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "observed_only"
    assert delivery.status == "idle"
    assert len(provider.calls) >= 2
    assert not transport.bodies
    audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    top_level_audits = [
        audit
        for audit in audits
        if audit["route"]["router_version"]
        not in {"provider-subcall-audit.1", "authored-candidate-audit.1"}
    ]
    assert top_level_audits[-1]["status"] == "recovery_failed"


@pytest.mark.asyncio
async def test_grounded_context_recovery_is_still_owned_by_a_role_model() -> None:
    provider = _GroundedQuickRecoveryProvider()
    recovery = _QuickExpressionProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        recovery_model=recovery,
    )
    base = _request(revision=3, call="call:grounded-appraisal")
    trigger = base.trigger_message.model_copy(
        update={
            "text": "你还记得我喜欢什么吗？",
        }
    )
    context = json.dumps(
        {
            "slices": {
                "relevant_facts": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "fact:user:oolong",
                            "value": {"subject_ref": "user:primary", "value": "喜欢乌龙茶"},
                        }
                    ],
                }
            }
        },
        ensure_ascii=False,
    )
    appraisal_request = base.model_copy(
        update={
            "trigger_message": trigger,
            "model_content_json": context,
        }
    )
    await cognition.appraisal.propose(appraisal_request)
    expression_request = appraisal_request.model_copy(
        update={
            "call_id": "call:grounded-expression",
            "evaluated_world_revision": 5,
        }
    )
    with pytest.raises(ValueError, match="semantic source lane"):
        await cognition.expression.propose(expression_request)
    output = await cognition.expression.recover(
        expression_request,
        "main_invalid_output",
    )
    proposal = MinimalProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert len(proposal.action_intents) == 1
    assert output.model_version != "local-expression-failsafe.1"
    assert len(provider.calls) == 4
    assert len(recovery.calls) == 2


@pytest.mark.asyncio
async def test_ordinary_fact_context_does_not_trigger_local_character_prose() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    request = _request(revision=3, call="call:ordinary-recovery").model_copy(
        update={
            "trigger_message": _request(
                revision=3, call="call:ordinary-trigger"
            ).trigger_message.model_copy(update={"text": "我只是分享一下今天的事。"}),
            "model_content_json": json.dumps(
                {
                    "slices": {
                        "relevant_facts": {
                            "availability": "available",
                            "items": [{"item_ref": "fact:user:oolong", "value": "喜欢乌龙茶"}],
                        }
                    }
                },
                ensure_ascii=False,
            ),
        }
    )

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(request, "main_timeout")
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "text",
    (
        "我只是分享一下今天遇到的一件小事。",
        "所以这是我们第一次聊天吗",
    ),
)
@pytest.mark.asyncio
async def test_generic_local_expression_failure_does_not_author_character_prose(
    text: str,
) -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    base = _request(revision=3, call="call:generic-silent")
    trigger = base.trigger_message.model_copy(
        update={
            "text": text,
        }
    )
    request = base.model_copy(update={"trigger_message": trigger})

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(request, "main_timeout")


@pytest.mark.asyncio
async def test_first_greeting_provider_failure_does_not_invent_a_local_greeting() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="Geoff",
            relationship_frame="刚认识的群友",
        ),
    )
    base = _request(revision=3, call="call:first-greeting-failsafe")
    request = base.model_copy(
        update={
            "trigger_message": base.trigger_message.model_copy(update={"text": "你好，第一次见。"})
        }
    )

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(request, "main_invalid_output")


@pytest.mark.asyncio
async def test_user_fact_provider_failure_does_not_invent_a_local_acknowledgement() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    base = _request(revision=3, call="call:user-fact-failsafe")
    request = base.model_copy(
        update={
            "trigger_message": base.trigger_message.model_copy(
                update={"text": "我叫丁奥轩，英文名 Geoff。"}
            )
        }
    )

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(request, "main_invalid_output")


@pytest.mark.asyncio
async def test_first_greeting_provider_failure_records_technical_silence(tmp_path) -> None:
    provider = _AlwaysFailProvider()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="user:user.1",
            relationship_frame="刚认识的群友",
        ),
    )
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "first-greeting-failsafe.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:first-greeting-failsafe",
                text="你好，第一次见。",
                observed_at=NOW,
                trace_id="trace:first-greeting-failsafe",
            )
        )
        await app.drain_actions_once()
    finally:
        app.close()

    assert outcome.status == "observed_only"
    assert transport.bodies == []


@pytest.mark.asyncio
async def test_emotional_provider_failure_does_not_force_a_repair_script() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    base = _request(revision=3, call="call:emotion-failsafe")
    request = base.model_copy(
        update={
            "trigger_message": base.trigger_message.model_copy(
                update={"text": "你刚才回得有点敷衍，我有点失望。"}
            )
        }
    )

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(request, "main_invalid_output")


@pytest.mark.asyncio
async def test_colloquial_current_activity_probe_does_not_get_local_prose() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    base = _request(revision=3, call="call:colloquial-world-probe")
    request = base.model_copy(
        update={
            "trigger_message": base.trigger_message.model_copy(
                update={"text": "所以你现在在干啥呀"}
            )
        }
    )

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(request, "main_invalid_output")


@pytest.mark.asyncio
async def test_colloquial_world_probe_provider_failure_records_no_fake_reply(tmp_path) -> None:
    provider = _AlwaysFailProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-generic-silent.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:generic-silent",
                text="所以你现在在干啥呀",
                observed_at=NOW,
                trace_id="trace:generic-silent",
            )
        )
        await app.drain_actions_once()
    finally:
        app.close()

    assert outcome.status == "observed_only"
    assert transport.bodies == []


@pytest.mark.asyncio
async def test_double_provider_failure_records_recovery_failure_without_fake_ack(
    tmp_path,
) -> None:
    backup = _AlwaysFailProvider()
    primary = _FailingProviderWithFallback(backup)
    cognition = SingleCallInboundCognition(flash_model=primary)
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-double-provider-failure.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        main_model=cognition.expression,
        quick_recovery=cognition.expression,
        appraisal_model=cognition.appraisal,
        transport=transport,
        now=NOW,
    )
    try:
        outcome = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:double-provider-failure",
                text="我只是分享一下今天遇到的一件小事。",
                observed_at=NOW,
                trace_id="trace:double-provider-failure",
            )
        )
        await app.drain_actions_once()
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "observed_only"
    assert transport.bodies == []
    audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    top_level_audits = [
        audit
        for audit in audits
        if audit["route"]["router_version"]
        not in {"provider-subcall-audit.1", "authored-candidate-audit.1"}
    ]
    assert top_level_audits[-1]["status"] == "recovery_failed"
    assert top_level_audits[-1]["failure_code"]
    assert top_level_audits[-1]["model_version"] != "local-expression-failsafe.1"
    assert len(primary.calls) >= 1
    assert len(backup.calls) >= 1
    assert len(primary.calls) + len(backup.calls) <= 3


@pytest.mark.asyncio
async def test_inbound_appraisal_recovery_falls_closed_for_a_settled_world_trigger() -> None:
    """A background settlement has no message cache key to recover through."""

    cognition = SingleCallInboundCognition(flash_model=_OrdinaryCombinedProvider())
    settlement_request = _request(revision=3, call="call:settled-world-appraisal").model_copy(
        update={
            "trigger_ref": "event:world-occurrence:settled:1",
            "trigger_message": None,
        }
    )

    recovered = await cognition.appraisal.recover(
        settlement_request,
        "main_invalid_output",
    )

    assert recovered.model_version == "appraisal-draft-adapter.4"
    proposal = validate_proposal_envelope(recovered.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert proposal.trigger_ref == "event:world-occurrence:settled:1"
    assert proposal.proposed_changes == ()


@pytest.mark.asyncio
async def test_deferred_paired_shape_repair_cannot_cross_a_new_pinned_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale provider conversation cannot be rebound after Appraisal commits."""

    class _DeferredShapeOriginProvider(_CombinedProvider):
        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            if len(self.calls) == 1:
                return json.dumps(
                    {
                        "appraisal_draft": {
                            "appraise": False,
                            "brief_rationale": "No durable appraisal is needed.",
                            "behavior_tendency": "choose_own_response",
                            "stance": "present",
                            "display_strategy": "model_owned",
                            "confidence": 6_000,
                        },
                        "expression_draft": {},
                    },
                    ensure_ascii=False,
                )
            if "COMBINED OUTPUT ENVELOPE" in messages[0]["content"]:
                return json.dumps(
                    {
                        "appraisal_draft": {
                            "appraise": False,
                            "brief_rationale": "Keep the original appraisal.",
                            "behavior_tendency": "choose_own_response",
                            "stance": "present",
                            "display_strategy": "model_owned",
                            "confidence": 6_000,
                        },
                        "expression_draft": {
                            "timing_choice": "now",
                            "beats": [{"modality": "text", "text": "这是旧游标上的延迟修正。"}],
                            "stance": "stale_repair",
                            "brief_rationale": "Repair the old provider conversation.",
                            "world_claims": [],
                        },
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我按现在这轮重新想过了。"}],
                    "stance": "fresh_pinned_turn",
                    "brief_rationale": "Choose from the newly pinned Context.",
                    "world_claims": [],
                },
                ensure_ascii=False,
            )

    provider = _DeferredShapeOriginProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    origin = _request(revision=3, call="call:deferred-shape-origin")

    with monkeypatch.context() as patch:
        patch.setattr(
            "companion_daemon.world_v2.single_call_inbound_cognition.fit_secondary_call_timeout",
            lambda *_args, **_kwargs: None,
        )
        await cognition.appraisal.propose(origin)

    current = origin.model_copy(
        update={
            "call_id": "call:deferred-shape-after-appraisal",
            "capsule_id": "d" * 64,
            "evaluated_world_revision": 4,
            "evaluated_deliberation_revision": 1,
            "evaluated_ledger_sequence": 9,
            "model_content_json": json.dumps(
                {"world_revision": 4, "fresh_context": "accepted appraisal"}
            ),
        }
    )
    expression = await cognition.expression.propose(current)

    rendered = json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert "我按现在这轮重新想过了" in rendered
    assert "旧游标上的延迟修正" not in rendered
    assert len(provider.calls) == 2
    assert "COMBINED OUTPUT ENVELOPE" not in provider.calls[1][0]["content"]
    assert "fresh_context" in provider.calls[1][1]["content"]


@pytest.mark.asyncio
async def test_same_cursor_deferred_shape_repair_gets_candidate_wide_final_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structurally repaired candidate cannot smuggle in an unsupported life event."""

    unsupported_text = "刚才我在宿舍看书。"

    class _DeferredUnsupportedLifeProvider(_CombinedProvider):
        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            expression: dict[str, object] = (
                {}
                if len(self.calls) == 1
                else {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": unsupported_text}],
                    "stance": "invented_self_life",
                    "brief_rationale": "A structural replacement with a new visible claim.",
                    "world_claims": [],
                }
            )
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "No durable appraisal is needed.",
                        "behavior_tendency": "choose_own_response",
                        "stance": "present",
                        "display_strategy": "model_owned",
                        "confidence": 6_000,
                    },
                    "expression_draft": expression,
                },
                ensure_ascii=False,
            )

    class _UnsupportedLifeInventory:
        model = "candidate-inventory"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            return json.dumps(
                {
                    "contract": "candidate-external-proposition-inventory.2",
                    "locators": [
                        {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": 9,
                            "text": unsupported_text,
                        }
                    ],
                },
                ensure_ascii=False,
            )

    class _CandidateWideAuthority:
        model = "candidate-source-authority"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del temperature
            self.calls.append(messages)
            request = json.loads(messages[1]["content"])
            contract = request["output_contract"]["contract"]
            if contract == "source-closure-review.7":
                return json.dumps({"ci": [], "v": [], "p": []})
            assert contract == "candidate-external-proposition-coverage.1"
            locator = request["locators"][0]
            return json.dumps(
                {
                    "contract": contract,
                    "findings": [
                        {
                            "locator": locator,
                            "decision": "unclosed",
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                    ],
                }
            )

    provider = _DeferredUnsupportedLifeProvider()
    inventory = _UnsupportedLifeInventory()
    authority = _CandidateWideAuthority()
    cognition = SingleCallInboundCognition(
        flash_model=provider,
        source_closure_model=authority,
        candidate_external_proposition_inventory_model=inventory,
    )
    request = _request(revision=3, call="call:deferred-shape-final-review")

    with monkeypatch.context() as patch:
        patch.setattr(
            "companion_daemon.world_v2.single_call_inbound_cognition.fit_secondary_call_timeout",
            lambda *_args, **_kwargs: None,
        )
        await cognition.appraisal.propose(request)

    with pytest.raises(ValueError, match="semantic source closure rejected"):
        await cognition.expression.propose(request)

    assert len(provider.calls) == 2
    assert len(inventory.calls) == 1
    assert len(authority.calls) == 2


class _RoleIdenticalSourceCorrectionProvider:
    model = "strong-role-source-correction"
    semantic_authority_id = "semantic-authority:test:strong-role-source-correction"

    def __init__(
        self,
        *,
        remains_unsupported: bool = False,
        episode_disposition: str | None = None,
        strict_wire: bool = True,
    ) -> None:
        self.remains_unsupported = remains_unsupported
        self.episode_disposition = episode_disposition
        self.strict_wire = strict_wire
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        self.calls.append((messages, temperature))
        expression = dict(
            _UnsupportedLifeCombinedProvider._invalid_expression()
            if self.remains_unsupported
            else _UnsupportedLifeCombinedProvider._corrected_expression()
        )
        if not self.strict_wire:
            if self.episode_disposition is not None:
                expression["episode_disposition"] = self.episode_disposition
            return json.dumps(expression, ensure_ascii=False)
        private_state = dict(expression["private_turn_state"])
        private_state["contract"] = "private-turn-state.1"
        beats = [
            {
                "modality": beat["modality"],
                "text": beat.get("text"),
                "reaction_id": beat.get("reaction_id"),
                "sticker_id": beat.get("sticker_id"),
            }
            for beat in expression["beats"]
        ]
        return json.dumps(
            {
                "expression_draft": {
                    "private_turn_state": private_state,
                    "timing_choice": expression["timing_choice"],
                    "cadence": "conversational",
                    "beats": beats,
                    "delay_position_bp": None,
                    "expires_after_seconds": None,
                    "stance": expression["stance"],
                    "brief_rationale": expression["brief_rationale"],
                    "impulse_summary": None,
                    "confidence": expression["confidence"],
                    "variation_profile": None,
                    "response_expectation": None,
                    "response_expectation_assessment": None,
                    "world_claims": expression["world_claims"],
                },
                "episode_disposition": self.episode_disposition,
            },
            ensure_ascii=False,
        )


class _FixedSourceClosureReviewer:
    def __init__(self, *, model: str, unsupported: bool) -> None:
        self.model = model
        self.semantic_authority_id = f"semantic-authority:test:{model.casefold()}"
        self.unsupported = unsupported
        self.calls: list[list[dict[str, str]]] = []

    async def complete(
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
                "v": ["undeclared_external_assertion"] if self.unsupported else [],
                "p": [],
                "visible_findings": (
                    [
                        {
                            "category": "undeclared_external_assertion",
                            "visible_span": "刚才在宿舍翻书",
                            "claim_index": None,
                            "source_relation": "unclosed",
                            "source_refs": [],
                        }
                    ]
                    if self.unsupported
                    else []
                ),
                "r": (
                    "The candidate invents an unpinned companion-life occurrence."
                    if self.unsupported
                    else "The corrected candidate contains no external occurrence."
                ),
            },
            ensure_ascii=False,
        )


@pytest.mark.asyncio
async def test_paired_source_reselection_uses_recovery_role_and_independent_reviewer() -> None:
    primary = _UnsupportedLifeCombinedProvider()
    correction = _RoleIdenticalSourceCorrectionProvider(
        episode_disposition="complete_without_more"
    )
    primary_reviewer = _FixedSourceClosureReviewer(
        model="primary-source-reviewer",
        unsupported=True,
    )
    correction_reviewer = _FixedSourceClosureReviewer(
        model="independent-correction-reviewer",
        unsupported=False,
    )
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=correction,
        source_closure_model=primary_reviewer,
        recovery_source_closure_model=correction_reviewer,
        source_closure_reselection_lane=SourceClosureReselectionLane(
            author=correction,
            reviewer=correction_reviewer,
        ),
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-strong-source-reselection.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:strong-source-reselection")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(request)

    assert len(primary.calls) == 1
    assert len(correction.calls) == 1
    assert correction.calls[0][1] == 0.0
    assert len(primary_reviewer.calls) == 1
    assert len(correction_reviewer.calls) == 1
    assert expression.model_id == correction.model
    assert expression.episode_disposition == "complete_without_more"
    assert expression.winning_request_hash == _provider_request_hash(
        correction.calls[0][0],
        temperature=0.0,
    )
    rendered = json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert "我看到你这句了" in rendered
    assert "宿舍" not in rendered


@pytest.mark.asyncio
async def test_paired_source_reselection_without_strict_envelope_is_typed_failure() -> None:
    primary = _UnsupportedLifeCombinedProvider()
    correction = _RoleIdenticalSourceCorrectionProvider(strict_wire=False)
    primary_reviewer = _FixedSourceClosureReviewer(
        model="primary-source-reviewer",
        unsupported=True,
    )
    correction_reviewer = _FixedSourceClosureReviewer(
        model="independent-correction-reviewer",
        unsupported=False,
    )
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=correction,
        source_closure_model=primary_reviewer,
        recovery_source_closure_model=correction_reviewer,
        source_closure_reselection_lane=SourceClosureReselectionLane(
            author=correction,
            reviewer=correction_reviewer,
        ),
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-legacy-source-reselection.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:legacy-source-reselection")

    await cognition.appraisal.propose(request)
    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.expression.propose(request)

    assert caught.value.failure_code == "authored_expression_reselection_invalid"
    assert caught.value.attempted_model_id == correction.model
    assert len(primary.calls) == 1
    assert len(correction.calls) == 1
    assert len(primary_reviewer.calls) == 1
    assert len(correction_reviewer.calls) == 0


@pytest.mark.asyncio
async def test_paired_fresh_source_reselection_clears_prior_episode_disposition() -> None:
    class _EpisodeUnsupportedLifeCombinedProvider(_UnsupportedLifeCombinedProvider):
        @staticmethod
        def _invalid_expression() -> dict[str, object]:
            return {
                **_UnsupportedLifeCombinedProvider._invalid_expression(),
                "episode_disposition": "append",
            }

    primary = _EpisodeUnsupportedLifeCombinedProvider()
    correction = _RoleIdenticalSourceCorrectionProvider()
    primary_reviewer = _FixedSourceClosureReviewer(
        model="primary-source-reviewer",
        unsupported=True,
    )
    correction_reviewer = _FixedSourceClosureReviewer(
        model="independent-correction-reviewer",
        unsupported=False,
    )
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=correction,
        source_closure_model=primary_reviewer,
        recovery_source_closure_model=correction_reviewer,
        source_closure_reselection_lane=SourceClosureReselectionLane(
            author=correction,
            reviewer=correction_reviewer,
        ),
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-strong-source-reselection-clears-episode.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:strong-source-reselection-clears-episode")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(request)

    assert expression.episode_disposition is None
    assert "episode_disposition" not in expression.raw_proposal
    assert len(primary.calls) == 1
    assert len(correction.calls) == 1
    assert len(primary_reviewer.calls) == 1
    assert len(correction_reviewer.calls) == 1


@pytest.mark.asyncio
async def test_paired_recovery_role_source_reselection_stops_after_one_invalid_correction() -> None:
    primary = _UnsupportedLifeCombinedProvider()
    correction = _RoleIdenticalSourceCorrectionProvider(remains_unsupported=True)
    primary_reviewer = _FixedSourceClosureReviewer(
        model="primary-source-reviewer",
        unsupported=True,
    )
    correction_reviewer = _FixedSourceClosureReviewer(
        model="independent-correction-reviewer",
        unsupported=True,
    )
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=correction,
        source_closure_model=primary_reviewer,
        recovery_source_closure_model=correction_reviewer,
        source_closure_reselection_lane=SourceClosureReselectionLane(
            author=correction,
            reviewer=correction_reviewer,
        ),
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-strong-source-reselection-terminal.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:strong-source-reselection-terminal")

    await cognition.appraisal.propose(request)
    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.expression.propose(request)

    assert caught.value.failure_code == "authored_expression_reselection_invalid"
    assert caught.value.attempted_model_id == correction.model
    assert len(primary.calls) == 1
    assert len(correction.calls) == 1
    assert len(primary_reviewer.calls) == 1
    assert len(correction_reviewer.calls) == 1


@pytest.mark.asyncio
async def test_source_reselection_failure_log_never_contains_private_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _PrivateExplodingCorrection:
        model = "private-exploding-correction"
        semantic_authority_id = (
            "semantic-authority:test:private-exploding-correction"
        )

        async def complete(
            self,
            messages: list[dict[str, str]],
            *,
            temperature: float = 0.8,
        ) -> str:
            del messages, temperature
            raise RuntimeError("PRIVATE_SENTINEL candidate=user-secret")

    primary = _UnsupportedLifeCombinedProvider()
    correction = _PrivateExplodingCorrection()
    primary_reviewer = _FixedSourceClosureReviewer(
        model="primary-source-reviewer",
        unsupported=True,
    )
    correction_reviewer = _FixedSourceClosureReviewer(
        model="independent-correction-reviewer",
        unsupported=False,
    )
    cognition = SingleCallInboundCognition(
        flash_model=primary,
        recovery_model=correction,
        source_closure_model=primary_reviewer,
        recovery_source_closure_model=correction_reviewer,
        source_closure_reselection_lane=SourceClosureReselectionLane(
            author=correction,
            reviewer=correction_reviewer,
        ),
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-source-reselection-log.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:private-source-reselection-log")

    await cognition.appraisal.propose(request)
    with pytest.raises(ValidationTechnicalFailure):
        await cognition.expression.propose(request)

    assert "PRIVATE_SENTINEL" not in caplog.text
    assert "user-secret" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text
