from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from functools import wraps
from hashlib import sha256
from typing import Any

import httpx
import pytest
from test_production_turn_application import (
    NOW,
    _config,
    _DeliveredTransport,
    _Identities,
    _Router,
)
from world_v2_application import (
    build_sqlite_world_v2_test_application,
    compose_fixture_character_interior,
)

from companion_daemon.llm import (
    ModelCapacityBusyError,
    ProviderCircuitBreaker,
    mark_model_request_completed,
    mark_model_request_emitted,
)
from companion_daemon.world_v2.affect_target_bounds import (
    AFFECT_DIMENSIONS,
    AffectTargetDimensionLowerBound,
    AffectTargetLowerBounds,
)
from companion_daemon.world_v2.companion_identity import (
    CompanionIdentityFrame,
    companion_identity_source_ref,
)
from companion_daemon.world_v2.character_interior import CharacterInterior
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    ModelUsageProvenance,
    TriggerMessage,
    ValidationTechnicalFailure,
)
from companion_daemon.world_v2.expression_draft import (
    ExpressionDraftCapabilities,
    QQ_NAPCAT_EXPRESSION_CAPABILITIES,
)
from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
)
from companion_daemon.world_v2.production_latency_trace import ProductionLatencyRecorder
from companion_daemon.world_v2.proposal_envelope import (
    DecisionProposal,
    ProposalEvidenceRef,
)
from companion_daemon.world_v2.character_interior.run_result import (
    CausalOpportunityIdentity,
)
from companion_daemon.world_v2.recall_embedding import OpenAICompatibleRecallEmbedding
from companion_daemon.world_v2.recall_index import FeatureHashRecallEmbedding
from companion_daemon.world_v2.character_interior.inbound_author import (
    _InboundCharacterAuthor as InboundCharacterAuthor,
    _retired_stream_candidate_audits,
)
from companion_daemon.world_v2.deliberation import PhysicalProviderInvocationAudit
from companion_daemon.world_v2.world_turn_runtime import InboundTurn

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
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": (
                            "这句贬低让我受伤；我想保持克制，但也要明确守住边界。"
                        ),
                        "attended_source_refs": [],
                    },
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
                    "affect": "open",
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
    """Let the configured recovery author follow an exhausted primary correction."""

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
                    "inner_state_summary": "前一次没组织好，但我仍然想回应。",
                    "attended_source_refs": [],
                },
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "嗯，我在听。"}],
                "stance": "present",
                "brief_rationale": "The configured role recovery owns its response.",
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
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": "这句话让我受伤，我想先用一句直接的话守住边界。",
                        "attended_source_refs": [],
                    },
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
        expression = {
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": ("他脑子里有很多事；我想先接住，再留一点空间认真听下去。"),
                "attended_source_refs": [],
            },
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
        }
        if provisional:
            return json.dumps(expression, ensure_ascii=False)
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "listen",
                    "stance": "attentive",
                    "display_strategy": "natural",
                    "confidence": 7000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )


class _InvalidAppraisalValidExpressionProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        appraisal = (
            {
                "appraise": True,
                # First result deliberately omits meanings/attribution/severity.
                "brief_rationale": "Maybe emotionally meaningful.",
                "behavior_tendency": "attend",
                "stance": "open",
                "display_strategy": "natural",
                "confidence": 5000,
            }
            if len(self.calls) == 1
            else {
                "appraise": False,
                "affect": "no_change",
                "brief_rationale": "On reconsideration I do not choose a durable appraisal.",
                "behavior_tendency": "attend",
                "stance": "open",
                "display_strategy": "natural",
                "confidence": 6500,
            }
        )
        return json.dumps(
            {
                "appraisal_draft": appraisal,
                "expression_draft": {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": (
                            "这是第一次见面的招呼；我想自然地介绍自己，不替这次相遇加戏。"
                        ),
                        "attended_source_refs": [],
                    },
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
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": ("这句轻慢仍让我不舒服；我想克制地说出当下的感受。"),
                        "attended_source_refs": [],
                    },
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
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": "我想先安静听完，再按当下真正注意到的内容回应。",
                        "attended_source_refs": [],
                    },
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
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": (
                            "他在担心家里，我想先问清发生了什么，不替他补充地点或经过。"
                        ),
                        "attended_source_refs": [],
                    },
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


class _InternalTypeErrorToolProvider(_CombinedProvider):
    supports_required_tool_choice = True

    def __init__(self) -> None:
        super().__init__()
        self.metered_calls = 0
        self.plain_calls = 0

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: dict[str, object] | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        del messages, temperature, tools, tool_choice
        self.metered_calls += 1
        raise TypeError("provider transport failed after request emission")

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del messages, temperature
        self.plain_calls += 1
        raise AssertionError("an internal TypeError must not trigger a second provider call")


@pytest.mark.asyncio
async def test_forced_tool_internal_type_error_never_retries_plain_request() -> None:
    provider = _InternalTypeErrorToolProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)

    with pytest.raises(TypeError, match="after request emission"):
        await cognition.propose(_request(revision=3, call="call:tool-internal-type-error"))

    assert provider.metered_calls == 1
    assert provider.plain_calls == 0


class _ToolIdentityCombinedProvider(_CombinedProvider):
    """Explicit provider-capable fixture for the public inbound author seam."""

    supports_required_tool_choice = True

    def __init__(self) -> None:
        super().__init__()
        self.tool_calls: list[tuple[list[dict[str, object]] | None, object | None]] = []
        self.tool_messages: list[list[dict[str, str]]] = []

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        self.tool_calls.append((tools, tool_choice))
        self.tool_messages.append(messages)
        raw = await self.complete(messages, temperature=temperature)
        parsed = json.loads(raw)
        if "AppraisalDraft" in parsed:
            parsed["appraisal_draft"] = parsed.pop("AppraisalDraft")
        if "ExpressionDraft" in parsed:
            parsed["expression_draft"] = parsed.pop("ExpressionDraft")
        return (
            json.dumps({"result_kind": "decision", **parsed}, ensure_ascii=False),
            _metered_usage(ref="usage:tool-identity", input_tokens=20, output_tokens=5),
        )


@pytest.mark.asyncio
async def test_inbound_forced_tool_request_identity_binds_versioned_schema() -> None:
    """The visible author call carries one versioned schema in both seams."""

    provider = _ToolIdentityCombinedProvider()
    author = InboundCharacterAuthor(flash_model=provider)

    await author.propose(_request(revision=3, call="call:tool-contract-identity"))

    assert len(provider.tool_calls) == 1
    tools, tool_choice = provider.tool_calls[0]
    assert tools is not None
    function = tools[0]["function"]
    assert function["name"] == "character_inbound_initial_v1"
    assert set(function) == {"name", "description", "parameters"}
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_inbound_initial_v1"},
    }
    assert "FORCED TOOL TRANSPORT" in provider.tool_messages[0][0]["content"]
    assert "result_kind=decision" in provider.tool_messages[0][0]["content"]


class _ForcedStreamingCombinedProvider:
    model = "forced-streaming-combined"
    supports_required_tool_choice = True

    def __init__(self) -> None:
        self.calls: list[tuple[list[dict[str, object]] | None, object | None]] = []
        self.release_tail = asyncio.Event()
        self.tail_started = asyncio.Event()

    @staticmethod
    def payload() -> dict[str, object]:
        return {
            "result_kind": "decision",
            "protocol": "character-interior-events.1",
            "appraisal_draft": {
                "appraise": False,
                "affect": "no_change",
                "brief_rationale": "这句不需要形成新的持久评价。",
                "behavior_tendency": "自由接话",
                "stance": "自然回应",
                "display_strategy": "直接说",
                "confidence": 7000,
            },
            "events": [
                {
                    "type": "head",
                    "timing_choice": "now",
                    "beat": {"modality": "text", "text": "第一条先到。"},
                    "stance": "自然接话",
                    "brief_rationale": "我想分两句说。",
                    "confidence": 7000,
                    "world_claims": [],
                },
                {
                    "type": "beat",
                    "beat": {"modality": "text", "text": "第二条随后到。"},
                    "world_claims": [],
                },
                {"type": "end"},
            ],
        }

    async def complete_json_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        del messages, temperature
        self.calls.append((tools, tool_choice))
        raw = json.dumps(
            self.payload(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        marker = ',{"type":"beat"'
        head_bytes, tail_bytes = raw.split(marker, 1)
        if on_text_delta is not None:
            on_text_delta(head_bytes)
        self.tail_started.set()
        await self.release_tail.wait()
        if on_text_delta is not None:
            on_text_delta(marker + tail_bytes)
        return raw, _metered_usage(
            ref="usage:forced-stream",
            input_tokens=20,
            output_tokens=10,
        )


@pytest.mark.asyncio
async def test_forced_stream_releases_head_before_later_tool_argument_frames() -> None:
    provider = _ForcedStreamingCombinedProvider()
    author = InboundCharacterAuthor(flash_model=provider)
    request = _request(revision=3, call="call:forced-stream-head")

    head_task = asyncio.create_task(author.propose_stream_head(request))
    await asyncio.wait_for(provider.tail_started.wait(), timeout=0.5)
    head = await asyncio.wait_for(head_task, timeout=0.5)

    assert len(provider.calls) == 1
    tools, tool_choice = provider.calls[0]
    assert tools is not None
    assert tools[0]["function"]["name"] == "character_inbound_initial_stream_v1"
    assert tool_choice == {
        "type": "function",
        "function": {"name": "character_inbound_initial_stream_v1"},
    }
    assert head.semantic_stream_part == "head"

    provider.release_tail.set()
    tail = await asyncio.wait_for(
        author.propose_stream_tail(request.model_copy(update={"call_id": "call:forced-tail"})),
        timeout=0.5,
    )
    assert tail.semantic_stream_part == "tail"


class _PermutedForcedStreamingProvider(_ForcedStreamingCombinedProvider):
    def __init__(self, order: tuple[str, ...]) -> None:
        super().__init__()
        self.order = order

    async def complete_json_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        del messages, temperature
        self.calls.append((tools, tool_choice))
        payload = self.payload()
        raw = json.dumps(
            {key: payload[key] for key in self.order},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if on_text_delta is not None:
            on_text_delta(raw)
        return raw, _metered_usage(
            ref="usage:permuted-forced-stream",
            input_tokens=20,
            output_tokens=10,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order",
    [
        ("result_kind", "appraisal_draft", "protocol", "events"),
        ("result_kind", "events", "protocol", "appraisal_draft"),
        ("appraisal_draft", "protocol", "events", "result_kind"),
    ],
)
async def test_forced_stream_accepts_legal_tool_argument_field_permutations(
    order: tuple[str, ...],
) -> None:
    provider = _PermutedForcedStreamingProvider(order)
    author = InboundCharacterAuthor(flash_model=provider)
    request = _request(revision=3, call="call:permuted-forced-stream")

    head = await asyncio.wait_for(author.propose_stream_head(request), timeout=0.5)
    tail = await asyncio.wait_for(
        author.propose_stream_tail(
            request.model_copy(update={"call_id": "call:permuted-forced-tail"})
        ),
        timeout=0.5,
    )

    assert head.semantic_stream_part == "head"
    assert tail.semantic_stream_part == "tail"


class _ResultKindLastStreamingProvider(_ForcedStreamingCombinedProvider):
    async def complete_json_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        del messages, temperature
        self.calls.append((tools, tool_choice))
        payload = self.payload()
        raw = json.dumps(
            {
                "protocol": payload["protocol"],
                "appraisal_draft": payload["appraisal_draft"],
                "events": payload["events"],
                "result_kind": payload["result_kind"],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        marker = ',"result_kind"'
        prefix, suffix = raw.split(marker, 1)
        assert on_text_delta is not None
        on_text_delta(prefix + ",")
        self.tail_started.set()
        await self.release_tail.wait()
        on_text_delta('"result_kind"' + suffix)
        return raw, _metered_usage(
            ref="usage:result-kind-last-stream",
            input_tokens=20,
            output_tokens=10,
        )


@pytest.mark.asyncio
async def test_forced_stream_waits_for_late_transport_discriminator() -> None:
    provider = _ResultKindLastStreamingProvider()
    author = InboundCharacterAuthor(flash_model=provider)
    request = _request(revision=3, call="call:result-kind-last-stream")

    head_task = asyncio.create_task(author.propose_stream_head(request))
    await asyncio.wait_for(provider.tail_started.wait(), timeout=0.5)
    await asyncio.sleep(0)
    assert not head_task.done()

    provider.release_tail.set()
    head = await asyncio.wait_for(head_task, timeout=0.5)
    assert head.semantic_stream_part == "head"


class _ExtraToolProtocolFailureProvider(_ForcedStreamingCombinedProvider):
    def __init__(self, *, after_head: bool) -> None:
        super().__init__()
        self.after_head = after_head
        self.release_failure = asyncio.Event()

    async def complete_json_stream_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        on_text_delta=None,  # type: ignore[no-untyped-def]
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        del messages, temperature
        self.calls.append((tools, tool_choice))
        assert on_text_delta is not None
        if not self.after_head:
            released = on_text_delta(
                '{"result_kind":"decision","protocol":"character-interior-events.1",'
            )
            assert released is False
            raise ValueError("model response must contain exactly one tool call")
        raw = json.dumps(
            self.payload(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        released = on_text_delta(raw)
        assert released is True
        await self.release_failure.wait()
        raise ValueError("model response must contain exactly one tool call")


@pytest.mark.asyncio
async def test_extra_tool_before_closed_head_never_authorizes_a_stream_unit() -> None:
    provider = _ExtraToolProtocolFailureProvider(after_head=False)
    author = InboundCharacterAuthor(flash_model=provider)

    with pytest.raises(ValueError, match="exactly one tool call"):
        await asyncio.wait_for(
            author.propose_stream_head(
                _request(revision=3, call="call:extra-tool-before-head")
            ),
            timeout=0.5,
        )


@pytest.mark.asyncio
async def test_extra_tool_after_closed_head_fails_only_the_unsettled_tail() -> None:
    provider = _ExtraToolProtocolFailureProvider(after_head=True)
    author = InboundCharacterAuthor(flash_model=provider)
    request = _request(revision=3, call="call:extra-tool-after-head")

    head = await asyncio.wait_for(author.propose_stream_head(request), timeout=0.5)
    assert head.semantic_stream_part == "head"

    provider.release_failure.set()
    with pytest.raises(ValueError, match="exactly one tool call"):
        await asyncio.wait_for(
            author.propose_stream_tail(
                request.model_copy(update={"call_id": "call:extra-tool-failed-tail"})
            ),
            timeout=0.5,
        )


class _LaterMultiBeatStreamingProvider(_PermutedForcedStreamingProvider):
    @staticmethod
    def payload() -> dict[str, object]:
        payload = _ForcedStreamingCombinedProvider.payload()
        payload["events"] = [
            {
                "type": "head",
                "timing_choice": "later",
                "delay_seconds": 60,
                "expires_after_seconds": 600,
                "beats": [
                    {"modality": "text", "text": "等我忙完先跟你说第一句。"},
                    {"modality": "text", "text": "还有第二句也想一起发。"},
                ],
                "stance": "自然接话",
                "brief_rationale": "我想稍后分两条说完。",
                "confidence": 7000,
                "world_claims": [],
            },
            {"type": "end"},
        ]
        return payload


@pytest.mark.asyncio
async def test_forced_stream_later_keeps_multiple_role_authored_beats() -> None:
    provider = _LaterMultiBeatStreamingProvider(
        ("result_kind", "protocol", "appraisal_draft", "events")
    )
    author = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
            update={"max_later_beats": 2}
        ),
    )

    request = _request(revision=3, call="call:later-multi-beat-stream")
    context = json.loads(request.model_content_json)
    context["logical_time"] = NOW.isoformat()
    head = await asyncio.wait_for(
        author.propose_stream_head(
            request.model_copy(
                update={
                    "model_content_json": json.dumps(
                        context,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                }
            )
        ),
        timeout=0.5,
    )
    proposal = json.dumps(head.raw_proposal, ensure_ascii=False)

    assert "等我忙完先跟你说第一句。" in proposal
    assert "还有第二句也想一起发。" in proposal


class _CorrectingForcedStreamingProvider(_ForcedStreamingCombinedProvider):
    def __init__(self) -> None:
        super().__init__()
        self.stream_task: asyncio.Task[object] | None = None
        self.correction_saw_stream_cancelling = False

    async def complete_json_stream_with_usage(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.stream_task = asyncio.current_task()
        return await super().complete_json_stream_with_usage(*args, **kwargs)

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> str:
        del messages, temperature, tool_choice
        self.correction_saw_stream_cancelling = bool(
            self.stream_task is not None and self.stream_task.cancelling()
        )
        assert tools is not None
        return json.dumps(
            {
                "result_kind": "decision",
                "appraisal_draft": {
                    "appraise": False,
                    "affect": "no_change",
                    "brief_rationale": "纠正后仍由我自己选择不形成持久评价。",
                    "behavior_tendency": "自由接话",
                    "stance": "自然回应",
                    "display_strategy": "直接说",
                    "confidence": 7000,
                },
                "expression_draft": {
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我重新接好这句。"}],
                    "stance": "自然回应",
                    "brief_rationale": "替换结构无效的同一选择。",
                    "confidence": 7000,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        raw = await self.complete_json(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        return raw, _metered_usage(
            ref="usage:metered-stream-correction",
            input_tokens=20,
            output_tokens=10,
        )


class _FailingCorrectionStreamingProvider(_CorrectingForcedStreamingProvider):
    @staticmethod
    def payload() -> dict[str, object]:
        payload = _ForcedStreamingCombinedProvider.payload()
        appraisal = dict(payload["appraisal_draft"])
        appraisal.pop("brief_rationale")
        payload["appraisal_draft"] = appraisal
        return payload

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        del messages, temperature, tools, tool_choice
        self.correction_saw_stream_cancelling = bool(
            self.stream_task is not None and self.stream_task.cancelling()
        )
        raise ConnectionError("correction provider unavailable")


@pytest.mark.asyncio
async def test_failed_stream_correction_preserves_physical_retirement_audit() -> None:
    provider = _FailingCorrectionStreamingProvider()
    author = InboundCharacterAuthor(flash_model=provider)

    with pytest.raises(ValidationTechnicalFailure) as raised:
        await asyncio.wait_for(
            author.propose_stream_head(
                _request(revision=3, call="call:failed-stream-correction")
            ),
            timeout=0.5,
        )

    assert provider.correction_saw_stream_cancelling is True
    assert len(raised.value.physical_provider_audits) == 1
    physical = raised.value.physical_provider_audits[0]
    assert physical.outcome == "unresolved"
    assert physical.usage_status == "unresolved"


@pytest.mark.asyncio
async def test_streamed_correction_retires_old_physical_session_before_reselection() -> None:
    provider = _CorrectingForcedStreamingProvider()
    author = InboundCharacterAuthor(flash_model=provider)
    request = _request(revision=3, call="call:stream-correction-retirement")

    # Make the streamed appraisal structurally invalid while keeping a whole
    # first frame available. The same role's final correction must replace it.
    original_stream = provider.complete_json_stream_with_usage

    async def invalid_stream(*args, **kwargs):  # type: ignore[no-untyped-def]
        on_delta = kwargs.get("on_text_delta")
        raw = json.dumps(
            {
                "result_kind": "decision",
                "protocol": "character-interior-events.1",
                "appraisal_draft": {
                    "appraise": False,
                    "affect": "no_change",
                    "behavior_tendency": "自由接话",
                    "stance": "自然回应",
                    "display_strategy": "直接说",
                    "confidence": 7000,
                },
                "events": [
                    {
                        "type": "head",
                        "timing_choice": "now",
                        "beat": {"modality": "text", "text": "这条不能发。"},
                        "stance": "自然回应",
                        "brief_rationale": "结构仍不完整。",
                        "confidence": 7000,
                        "world_claims": [],
                    },
                    {"type": "end"},
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        provider.stream_task = asyncio.current_task()
        if on_delta is not None:
            on_delta(raw)
        await provider.release_tail.wait()
        return raw, _metered_usage(
            ref="usage:invalid-stream",
            input_tokens=20,
            output_tokens=10,
        )

    provider.complete_json_stream_with_usage = invalid_stream  # type: ignore[method-assign]
    try:
        output = await asyncio.wait_for(author.propose_stream_head(request), timeout=0.5)
    finally:
        provider.release_tail.set()
        provider.complete_json_stream_with_usage = original_stream  # type: ignore[method-assign]

    assert provider.correction_saw_stream_cancelling is True
    assert output.semantic_stream_part is None
    # The corrected full decision is independent of the retired stream. An
    # incomplete predecessor cannot be attached to the successful result's
    # physical tail audit, nor can it be promoted to a returned candidate.
    assert output.physical_provider_audits == ()
    assert output.authored_candidate_audits == ()
    with pytest.raises(RuntimeError, match="continuation is unavailable"):
        await author.propose_stream_tail(
            request.model_copy(update={"call_id": "call:retired-tail"})
        )


def test_completed_retired_stream_is_recorded_as_rejected_candidate() -> None:
    retirement = PhysicalProviderInvocationAudit(
        model_call_id="model-call:retired-stream",
        request_hash="a" * 64,
        model_id="model:stream",
        model_version="stream.1",
        outcome="completed",
        response_hash="b" * 64,
        usage_status="provider_reported",
        usage=_metered_usage(
            ref="usage:retired-stream",
            input_tokens=12,
            output_tokens=8,
        ),
        semantic_model_call_ids=(
            "model-call:retired-stream:head",
            "model-call:retired-stream:tail",
        ),
    )

    candidates = _retired_stream_candidate_audits(retirement)

    assert len(candidates) == 1
    assert candidates[0].model_call_id == retirement.model_call_id
    assert candidates[0].response_hash == retirement.response_hash
    assert candidates[0].outcome == "validation_rejected"


class _ForcedMissingAffectProvider(_ToolIdentityCombinedProvider):
    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        raw, usage = await super().complete_json_with_usage(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        parsed = json.loads(raw)
        if len(self.tool_calls) == 1:
            parsed["appraisal_draft"].pop("affect")
            return json.dumps(parsed, ensure_ascii=False), usage
        return json.dumps(parsed, ensure_ascii=False), usage


@pytest.mark.asyncio
async def test_forced_missing_affect_uses_the_existing_same_role_correction() -> None:
    provider = _ForcedMissingAffectProvider()
    author = InboundCharacterAuthor(flash_model=provider)

    await author.propose(_request(revision=3, call="call:forced-missing-affect"))

    assert len(provider.tool_calls) == 2
    assert provider.tool_calls[0][0] is not None
    assert provider.tool_calls[1][0] is not None
    assert provider.tool_calls[1][0][0]["function"]["name"] == (
        "character_inbound_final_atomic_v1"
    )


class _ForcedRepeatedMissingAffectProvider(_ForcedMissingAffectProvider):
    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        raw, usage = await super().complete_json_with_usage(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        parsed = json.loads(raw)
        if isinstance(parsed.get("appraisal_draft"), dict):
            parsed["appraisal_draft"].pop("affect", None)
        return json.dumps(parsed, ensure_ascii=False), usage


@pytest.mark.asyncio
async def test_forced_repeated_missing_affect_is_a_typed_terminal_failure() -> None:
    provider = _ForcedRepeatedMissingAffectProvider()
    author = InboundCharacterAuthor(flash_model=provider)

    with pytest.raises(ValidationTechnicalFailure, match="appraisal_reselection_invalid"):
        await author.propose(_request(revision=3, call="call:forced-repeated-missing-affect"))

    assert len(provider.tool_calls) == 2


class _MalformedForcedEnvelopeProvider(_ToolIdentityCombinedProvider):
    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        raw, usage = await super().complete_json_with_usage(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        parsed = json.loads(raw)
        if len(self.tool_calls) == 1:
            parsed.pop("result_kind")
            return json.dumps(parsed, ensure_ascii=False), usage
        return json.dumps(parsed, ensure_ascii=False), usage


@pytest.mark.asyncio
async def test_forced_envelope_mismatch_uses_existing_same_role_correction() -> None:
    provider = _MalformedForcedEnvelopeProvider()
    author = InboundCharacterAuthor(flash_model=provider)

    await author.propose(_request(revision=3, call="call:forced-envelope-mismatch"))

    assert len(provider.tool_calls) == 2


class _MalformedEnvelopeThenInvalidAppraisalProvider(_MalformedForcedEnvelopeProvider):
    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        raw, usage = await super().complete_json_with_usage(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        parsed = json.loads(raw)
        if len(self.tool_calls) == 2:
            parsed["appraisal_draft"].pop("brief_rationale", None)
        return json.dumps(parsed, ensure_ascii=False), usage


@pytest.mark.asyncio
async def test_envelope_correction_consumes_the_turn_corrective_budget() -> None:
    provider = _MalformedEnvelopeThenInvalidAppraisalProvider()
    author = InboundCharacterAuthor(flash_model=provider)

    with pytest.raises(ValidationTechnicalFailure, match="appraisal_reselection_invalid"):
        await author.propose(_request(revision=3, call="call:envelope-budget-consumed"))

    assert len(provider.tool_calls) == 2


class _MalformedEnvelopeThenBelowAffectFloorProvider(_MalformedForcedEnvelopeProvider):
    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        raw, usage = await super().complete_json_with_usage(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        parsed = json.loads(raw)
        if len(self.tool_calls) == 2:
            parsed["appraisal_draft"] = {
                "appraise": True,
                "affect": "open",
                "brief_rationale": "The slight still matters.",
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
                        "target_intensity_bp": 100,
                    }
                ],
            }
        return json.dumps(parsed, ensure_ascii=False), usage


@pytest.mark.asyncio
async def test_envelope_correction_cannot_open_a_second_affect_floor_correction() -> None:
    provider = _MalformedEnvelopeThenBelowAffectFloorProvider()
    author = InboundCharacterAuthor(flash_model=provider)

    with pytest.raises(ValidationTechnicalFailure, match="affect_target_reselection_invalid"):
        await author.propose(
            _request(
                revision=3,
                call="call:envelope-budget-before-affect-floor",
                hurt_minimum_bp=4200,
            )
        )

    assert len(provider.tool_calls) == 2


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
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": (
                            "这句话让我想起之前关于机器人的谈话；我想先确认记忆再回应。"
                        ),
                        "attended_source_refs": [],
                    },
                    "recall_request": {
                        "query_text": "之前关于机器人的谈话",
                        "memory_kinds": ["episodic", "semantic"],
                        "limit": 4,
                    },
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "affect": "no_change",
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "observe",
                    "stance": "self_possessed",
                    "display_strategy": "natural",
                    "confidence": 4000,
                },
                "expression_draft": {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": (
                            "召回后我仍觉得这句话很刺；我想直接确认他具体在不满什么。"
                        ),
                        "attended_source_refs": [],
                    },
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


class _ToolRecallThenCombinedProvider(_RecallThenCombinedProvider):
    supports_required_tool_choice = True

    def __init__(self) -> None:
        super().__init__()
        self.tool_names: list[str] = []

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, ModelUsageProvenance]:
        del tool_choice
        assert tools is not None
        self.tool_names.append(str(tools[0]["function"]["name"]))
        raw = await self.complete(messages, temperature=temperature)
        value = json.loads(raw)
        if "recall_request" in value:
            value = {"result_kind": "recall", **value}
        else:
            value = {"result_kind": "decision", **value}
        return json.dumps(value, ensure_ascii=False), _metered_usage(
            ref=f"usage:tool-recall:{len(self.tool_names)}",
            input_tokens=20,
            output_tokens=10,
        )


class _StrictNonMeteredToolRecallProvider(_RecallThenCombinedProvider):
    supports_required_tool_choice = True

    def __init__(self) -> None:
        super().__init__()
        self.tool_names: list[str] = []

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> str:
        del tool_choice
        raw = await self.complete(messages, temperature=temperature)
        if tools is None:
            return raw
        self.tool_names.append(str(tools[0]["function"]["name"]))
        return json.dumps(
            {"result_kind": "decision", **json.loads(raw)},
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
                        "affect": "open",
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
                    "affect": "open",
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
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "这是第一次见面的招呼；我想自然回应并介绍自己。",
                    "attended_source_refs": [],
                },
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
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我有两件短事想分开说，让每一句都保持自己的节奏。",
                    "attended_source_refs": [],
                },
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
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "我重新选择了完整表达，想按当下的自然节奏说清楚。",
                    "attended_source_refs": [],
                },
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
                        "private_turn_state": {
                            "contract": "private-turn-state.1",
                            "inner_state_summary": (
                                "这是普通午间招呼；我想轻松回应，不编造自己刚做过的事情。"
                            ),
                            "attended_source_refs": [],
                        },
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
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": (
                            "我想热情接住这句招呼，但不该拿没有来源的近况作开场。"
                        ),
                        "attended_source_refs": [],
                    },
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
            "private_turn_state": {
                "contract": "private-turn-state.1",
                "inner_state_summary": "主调用失败后我仍想简短接住，不装作发生了别的事。",
                "attended_source_refs": [],
            },
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
async def test_private_unified_engine_returns_one_merged_cognition_in_one_call() -> None:
    provider = _PrivateTurnStateCombinedProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-unified-cognition.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )

    output = await cognition.propose(_request(revision=3, call="call:unified-cognition"))

    proposal = DecisionProposal.model_validate_json(
        json.dumps(output.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 1
    assert proposal.private_turn_state is not None
    assert {change.kind for change in proposal.proposed_changes} >= {
        "appraisal_transition",
        "affect_transition",
        "expression_plan_transition",
    }


@pytest.mark.asyncio
async def test_paired_cache_reselects_missing_authored_confidence_and_cadence_once() -> None:
    provider = _ExplicitAuthoredFieldsCombinedProvider()
    cognition = InboundCharacterAuthor(
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

    await cognition._appraisal_materializer.propose(request)
    expression = await cognition._expression_materializer.propose(request)

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 2
    assert proposal.confidence == 8_100
    assert proposal.action_intents
    plan = proposal.proposed_changes[0].payload.value()
    assert plan["cadence_profile"] == "conversational"
    assert plan["recorded_cadence_mode"] == "shadow"


@pytest.mark.asyncio
async def test_paired_structural_reselection_propagates_its_episode_disposition() -> None:
    provider = _ExplicitAuthoredFieldsCombinedProvider(correction_episode_disposition="append")
    cognition = InboundCharacterAuthor(
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

    await cognition._appraisal_materializer.propose(request)
    expression = await cognition._expression_materializer.propose(request)

    assert expression.episode_disposition == "append"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_paired_cache_repeated_authored_field_omission_is_typed_technical_failure() -> None:
    provider = _ExplicitAuthoredFieldsCombinedProvider(remains_invalid=True)
    cognition = InboundCharacterAuthor(
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

    await cognition._appraisal_materializer.propose(request)
    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition._expression_materializer.propose(request)

    assert caught.value.failure_code == "authored_expression_reselection_invalid"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_paired_invalid_correction_episode_disposition_is_typed_terminal() -> None:
    provider = _ExplicitAuthoredFieldsCombinedProvider(
        correction_episode_disposition="wait_forever"
    )
    cognition = InboundCharacterAuthor(
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

    await cognition._appraisal_materializer.propose(request)
    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition._expression_materializer.propose(request)
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
    cognition = InboundCharacterAuthor(
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

    await cognition._appraisal_materializer.propose(request)
    expression = await cognition._expression_materializer.propose(request)

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
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        identity_frame=identity,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-shared-history.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:private-shared-history")

    expression = await cognition.propose(request)

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.attended_source_refs == (source_ref,)
    payload = proposal.proposed_changes[0].payload.value()
    assert payload["world_claims"][0]["scope"] == "shared_history"
    assert payload["world_claims"][0]["source_refs"] == [source_ref]
    assert '"scope":"shared_history"' in provider.calls[0][0]["content"]
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_combined_private_state_failure_reselects_the_complete_expression() -> None:
    provider = _PrivateTurnStateRepairProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-turn-state-repair.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:private-turn-state-repair")

    expression = await cognition.propose(request)

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 2
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
    payload = next(
        change.payload.value()
        for change in proposal.proposed_changes
        if change.kind == "expression_plan_transition"
    )
    assert payload["beat_drafts"][0]["inline_text"] == "这话挺伤人的，我不想装作没事。"


@pytest.mark.asyncio
async def test_unified_correction_usage_includes_both_author_calls() -> None:
    provider = _MeteredPrivateTurnStateRepairProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-metered-private-turn-state-repair.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:metered-private-state-repair")

    expression = await cognition.propose(request)

    assert len(provider.calls) == 2
    assert expression.input_tokens == 40
    assert expression.output_tokens == 10
    assert expression.usage is not None
    assert expression.usage.provider_usage_ref.startswith("provider-usage:combined:")


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
@pytest.mark.asyncio
async def test_paired_private_state_shape_failures_reselect_the_full_expression(
    invalid_state: object,
) -> None:
    provider = _InvalidPrivateStateShapeProvider(invalid_state)
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-state-shape-repair.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:private-state-shape-repair")

    expression = await cognition.propose(request)

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 2
    assert "这句来自无效状态" not in json.dumps(provider.calls[1], ensure_ascii=False)
    preserved = [
        json.loads(message["content"])
        for message in provider.calls[1]
        if message["role"] == "assistant"
    ]
    assert len(preserved) == 1
    assert set(preserved[0]) == {"appraisal_draft"}
    assert "complete replacement" in provider.calls[1][-1]["content"]
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.inner_state_summary.startswith("这句话让我")


@pytest.mark.asyncio
async def test_combined_invalid_private_state_recall_choice_reselects_once() -> None:
    provider = _InvalidPrivateStateRecallChoiceProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-invalid-private-recall.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    request = _request(revision=3, call="call:invalid-private-recall")

    expression = await cognition.propose(request)

    proposal = DecisionProposal.model_validate_json(
        json.dumps(expression.raw_proposal, ensure_ascii=False)
    )
    assert len(provider.calls) == 2
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
@pytest.mark.asyncio
async def test_combined_invalid_recall_payload_gets_one_sanitized_final_reselection(
    invalid_recall: dict[str, object],
    expected_code: str,
    expected_path: str,
) -> None:
    invalid_marker = str(invalid_recall["query_text"])
    provider = _InvalidRecallPayloadCombinedProvider((invalid_recall,))
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-combined-invalid-recall.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )

    output = await cognition._appraisal_materializer.propose(
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
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-combined-invalid-recall-terminal.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition._appraisal_materializer.propose(
            _request(revision=3, call="call:combined-invalid-recall-terminal")
        )

    assert caught.value.failure_code == "recall_choice_reselection_invalid"
    assert len(provider.calls) == 2
    assert first_invalid_marker not in json.dumps(provider.calls[1], ensure_ascii=False)


@pytest.mark.asyncio
async def test_combined_invalid_recall_final_cannot_trigger_another_shape_repair() -> None:
    provider = _InvalidRecallThenInvalidFinalCombinedProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-combined-invalid-recall-final.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition._appraisal_materializer.propose(
            _request(revision=3, call="call:combined-invalid-recall-final")
        )

    assert caught.value.failure_code == "recall_choice_reselection_invalid"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_public_invalid_recall_final_does_not_open_a_second_author_lane(
    tmp_path,
) -> None:
    provider = _InvalidRecallThenInvalidFinalCombinedProvider()
    capabilities = ExpressionDraftCapabilities(
        profile_id="expression:test-public-invalid-recall-final.1",
        modalities=("text",),
        private_turn_state_mode="required",
    )
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=capabilities,
    )
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-public-invalid-recall-final.sqlite",
        config=replace(_config(), expression_capabilities=capabilities),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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

    assert outcome.status == "deferred"
    assert len(provider.calls) == 2
    assert evidence.projection.actions == ()
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
    assert top_level_audits[-1]["status"] == "main_exception"
    assert top_level_audits[-1]["failure_code"]



@pytest.mark.asyncio
async def test_combined_inbound_role_receives_exact_active_affect_head_capability() -> None:
    provider = _CombinedProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    request = _request(revision=3, call="call:active-affect-head").model_copy(
        update={
            "model_content_json": json.dumps(
                {
                    "world_revision": 3,
                    "slices": {
                        "affect_episodes": {
                            "availability": "available",
                            "items": [
                                {
                                    "source_ref": "affect:existing:1",
                                    "value": {
                                        "episode_id": "affect:existing:1",
                                        "entity_revision": 4,
                                        "status": "active",
                                        "origin": {"accepted_event_ref": "event:affect:existing:1"},
                                        "opened_at": "2026-07-16T20:00:00+00:00",
                                        "updated_at": "2026-07-17T00:00:00+00:00",
                                        "components": [
                                            {
                                                "component_id": "component:hurt:1",
                                                "dimension": "hurt",
                                                "intensity_bp": 3100,
                                                "source_cluster_ref": "cluster:earlier",
                                                "decay_profile": {"floor_bp": 300},
                                                "residue_bp": 500,
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

    await cognition._appraisal_materializer.propose(request)

    supplied = json.loads(provider.calls[0][1]["content"])
    head = supplied["appraisal_affect_hard_boundaries"]["active_affect_heads"][0]
    assert head["episode_id"] == "affect:existing:1"
    assert head["episode_source_ref"] == "affect:existing:1"
    assert head["origin_event_ref"] == "event:affect:existing:1"
    assert head["entity_revision"] == 4
    assert head["components"][0]["component_id"] == "component:hurt:1"


@pytest.mark.asyncio
async def test_invalid_world_claim_prefix_is_rewritten_by_character_model() -> None:
    provider = _UnsupportedGreetingClaimProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
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

    expression = await cognition.propose(request)
    proposal = DecisionProposal.model_validate_json(json.dumps(expression.raw_proposal))
    visible = [
        change.payload.value()["beat_drafts"][0]["inline_text"]
        for change in proposal.proposed_changes
        if change.kind == "expression_plan_transition"
    ]

    # The fabricated-ref claim is stripped deterministically (2026-08-07):
    # the reply survives with the model's own wording, no second model call
    # rewrites it, and the unpinned claim never reaches the proposal.
    assert visible == ["刚忙完社团的事，午安呀。你今天过得怎么样？"]
    assert expression.model_version != "local-expression-failsafe.1"
    assert len(provider.calls) == 1
    visible_claims = [
        change.payload.value()["world_claims"]
        for change in proposal.proposed_changes
        if change.kind == "expression_plan_transition"
    ]
    assert all(claims == [] for claims in visible_claims)


@pytest.mark.asyncio
async def test_provider_failure_never_discovers_or_invokes_a_fallback_character_author() -> None:
    backup = _OrdinaryCombinedProvider()
    primary = _FailingProviderWithFallback(backup)
    cognition = InboundCharacterAuthor(flash_model=primary)
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

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await cognition.propose(request)

    assert len(primary.calls) == 1
    assert backup.calls == []


@pytest.mark.asyncio
async def test_technical_recovery_is_typed_without_a_backup_role_attempt() -> None:
    backup = _AlwaysFailProvider()
    primary = _FailingProviderWithFallback(backup)
    cognition = InboundCharacterAuthor(flash_model=primary)
    request = _request(revision=3, call="call:backup-also-fails")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await cognition.propose(request)

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_exception")

    assert caught.value.failure_code == "inbound_character_author_unavailable"
    assert len(primary.calls) == 1
    assert backup.calls == []


def test_inbound_author_has_no_backup_author_state() -> None:
    backup = _AlwaysFailProvider()
    primary = _FailingProviderWithFallback(backup)

    cognition = InboundCharacterAuthor(flash_model=primary)

    assert not hasattr(cognition, "_recovery_model")
    assert not hasattr(cognition, "_recovery_expression")


def test_provisional_episode_cannot_open_a_backup_character_author() -> None:
    backup = _ProvisionalBackupProvider()
    primary = _FailingProviderWithFallback(backup)
    cognition = InboundCharacterAuthor(flash_model=primary)

    assert not hasattr(cognition._expression_materializer, "propose_provisional")
    assert not hasattr(cognition._appraisal_materializer, "propose_provisional")
    assert primary.calls == []
    assert backup.calls == []


@pytest.mark.asyncio
async def test_shadow_observer_is_never_inferred_from_formal_recovery_provider() -> None:
    formal_recovery = _ProvisionalBackupProvider()
    primary = _FailingProviderWithFallback(formal_recovery)
    cognition = InboundCharacterAuthor(flash_model=primary)
    request = _request(revision=3, call="call:shadow-observer-not-configured")

    assert cognition._expression_materializer.shadow_observer_provider_available(request) is False
    with pytest.raises(RuntimeError, match="shadow observer is not configured"):
        await cognition._expression_materializer.propose_shadow_observer(request)

    assert formal_recovery.calls == []


def test_shadow_observer_rejects_the_selected_author_client_alias() -> None:
    shared = _ProvisionalBackupProvider()

    with pytest.raises(ValueError, match="independent provider client"):
        InboundCharacterAuthor(
            flash_model=shared,
            expression_episode_observer_model=shared,
        )


def test_shadow_observer_rejects_shared_runtime_resources() -> None:
    shared_client = object()
    selected_author = _ProvisionalBackupProvider()
    observer = _ProvisionalBackupProvider()
    selected_author.client = shared_client
    observer.client = shared_client

    with pytest.raises(ValueError, match="must not share client"):
        InboundCharacterAuthor(
            flash_model=selected_author,
            expression_episode_observer_model=observer,
        )


def test_shadow_observer_holds_no_authoritative_recall_or_review_capability() -> None:
    observer = _ProvisionalBackupProvider()
    reviewer = _SourceClosureReviewer()
    cognition = InboundCharacterAuthor(
        flash_model=_AlwaysFailProvider(),
        expression_episode_observer_model=observer,
        source_closure_model=reviewer,
        candidate_external_proposition_inventory_model=reviewer,
    )

    cognition._expression_materializer.install_recall_coordinator(object())  # type: ignore[arg-type]
    observer_adapter = cognition._expression_episode_observer

    assert observer_adapter is not None
    assert observer_adapter._recall is None
    assert observer_adapter._source_closure_reviewer is None
    assert observer_adapter._report_relative_reviewer is None
    assert observer_adapter._candidate_external_proposition_inventory_model is None


@pytest.mark.asyncio
async def test_existing_failover_exhaustion_is_technical_and_not_retried() -> None:
    backup = _AlwaysFailProvider()
    primary = _FailoverAlreadyUsedProvider(backup)
    cognition = InboundCharacterAuthor(flash_model=primary)
    request = _request(revision=3, call="call:failover-already-used")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await cognition.propose(request)

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_exception")

    assert caught.value.failure_code == "inbound_character_author_unavailable"
    assert len(primary.calls) == 1
    assert not backup.calls


@pytest.mark.asyncio
async def test_contextual_failsafe_is_default_off_after_timeout() -> None:
    primary = _AlwaysFailProvider()
    backup = _QuickExpressionProvider()
    cognition = InboundCharacterAuthor(flash_model=primary)
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

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_timeout")

    assert caught.value.failure_code == "inbound_character_author_unavailable"
    assert backup.calls == []


@pytest.mark.asyncio
async def test_public_turn_does_not_use_detached_technical_recovery_provider(
    tmp_path,
) -> None:
    primary = _AlwaysFailProvider()
    backup = _SlowQuickExpressionProvider(delay_seconds=2.7)
    cognition = InboundCharacterAuthor(flash_model=primary)
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-slow-technical-recovery.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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

    assert outcome.status == "deferred"
    assert delivery.status == "idle"
    assert len(primary.calls) == 1
    assert backup.started == backup.completed == 0
    assert transport.bodies == []


@pytest.mark.asyncio
async def test_public_turn_never_enters_detached_backup_correction(
    tmp_path,
) -> None:
    """The unified author may self-correct once; no backup author is entered."""

    primary = _LooseExpressionShapeProvider({}, repair=False)
    backup = _LooseExpressionShapeProvider({}, repair=False)
    cognition = InboundCharacterAuthor(
        flash_model=primary,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-state-required.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-backup-correction-slot.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    assert outcome.status == "deferred"
    top_level_audits = [
        audit
        for audit in audits
        if audit["route"]["router_version"]
        not in {"provider-subcall-audit.1", "authored-candidate-audit.1"}
    ]
    assert top_level_audits[-1]["status"] == "main_exception"
    assert top_level_audits[-1]["failure_code"] == "primary_exception"
    assert len(primary.calls) == 2
    assert backup.calls == []


@pytest.mark.asyncio
async def test_one_inbound_author_call_binds_appraisal_and_expression_together() -> None:
    provider = _CombinedProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="Geoff",
        ),
    )

    output = await cognition.propose(_request(revision=3, call="call:unified-inbound"))

    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert len(provider.calls) == 1
    assert proposal.evidence_refs[0].ref_id == "observation:1"
    assert proposal.proposed_changes[0].kind == "appraisal_transition"
    assert proposal.proposed_changes[1].kind == "affect_transition"
    assert proposal.evaluated_world_revision == 3
    assert len(proposal.action_intents) == 2
    assert proposal.action_intents[0].kind == "reply"
    assert "appraisal_draft" in provider.calls[0][0]["content"]
    assert "expression_draft" in provider.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_invalid_appraisal_requires_one_complete_same_role_reselection() -> None:
    provider = _InvalidAppraisalValidExpressionProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)

    output = await cognition.propose(_request(revision=3, call="call:invalid-appraisal"))

    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert len(provider.calls) == 2
    assert proposal.affect_decision == "no_change"
    assert all(change.kind != "appraisal_transition" for change in proposal.proposed_changes)
    assert "appraisal" in provider.calls[1][-1]["content"].lower()
    assert len(proposal.action_intents) == 1


@pytest.mark.asyncio
async def test_paired_appraisal_reselects_a_target_below_its_pinned_lower_bound() -> None:
    provider = _BelowBoundThenValidCombinedProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)

    output = await cognition._appraisal_materializer.propose(
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
    cognition = InboundCharacterAuthor(flash_model=provider)

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition._appraisal_materializer.propose(
            _request(
                revision=3,
                call="call:paired-affect-target-bound-still-invalid",
                hurt_minimum_bp=4200,
            )
        )

    assert len(provider.calls) == 2
    assert caught.value.failure_code == "affect_target_reselection_invalid"


@pytest.mark.asyncio
async def test_technical_failure_does_not_infer_affect_from_keywords() -> None:
    cognition = InboundCharacterAuthor(flash_model=_OrdinaryCombinedProvider())
    request = _request(revision=3, call="call:local-appraisal-recovery")
    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_timeout")

    assert caught.value.failure_code == "inbound_character_author_unavailable"


@pytest.mark.asyncio
async def test_unified_inbound_state_is_accepted_before_respond_returns_with_one_lineage(
    tmp_path,
) -> None:
    provider_temperature = 0.7
    provider = _ContextShiftPrivateTurnStateProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        temperature=provider_temperature,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-paired-lineage.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    unified_cognition = _RecordingModelAdapter(cognition)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-vertical.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=unified_cognition,
        ),
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
        evidence = app.export_replay_evidence()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == 1
    assert len(evidence.projection.appraisals) == 1
    assert len(evidence.projection.affect_episodes) == 1
    event_types = [item.event.event_type for item in evidence.events]
    assert event_types.index("ExpressionPlanAccepted") < event_types.index("AppraisalAccepted")
    assert event_types.index("ActionAuthorized") < event_types.index("AffectEpisodeOpened")
    outer_requests = tuple(unified_cognition.requests)
    recorded = [
        item.event.payload()
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    actual_provider_request_hashes = {
        _provider_request_hash(messages, temperature=provider_temperature)
        for messages in provider.calls
    }
    assert len(recorded) == len(actual_provider_request_hashes) == 1
    assert len(outer_requests) == 1
    assert {
        json.loads(payload["audit_json"])["request_hash"] for payload in recorded
    } == actual_provider_request_hashes
    outer_call_ids = {request.call_id for request in outer_requests}
    for payload in recorded:
        assert payload["model_call_id"] not in outer_call_ids
        assert payload["evaluated_world_revision"] in {
            request.evaluated_world_revision for request in outer_requests
        }
    assert len({payload["model_call_id"] for payload in recorded}) == 1
    assert len({json.loads(payload["audit_json"])["request_hash"] for payload in recorded}) == 1


@pytest.mark.asyncio
async def test_private_turn_state_is_audited_but_never_becomes_expression_authority(
    tmp_path,
) -> None:
    provider = _PrivateTurnStateCombinedProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_capabilities=ExpressionDraftCapabilities(
            profile_id="expression:test-private-state-vertical.1",
            modalities=("text",),
            private_turn_state_mode="required",
        ),
    )
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-private-state.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    provider = _ToolRecallThenCombinedProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-recall.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    assert provider.tool_names == [
        "character_inbound_initial_v1",
        "character_inbound_after_recall_v1",
    ]
    second_call = json.loads(provider.calls[1][-1]["content"])
    assert second_call["inner_life_snapshot"]["contract"] == "inner-life-snapshot.1"
    second_context = json.loads(second_call["request"]["model_content_json"])
    assert second_context["recall_control"] == {"remaining_character_pulls": 0}
    assert "bounded read-only recall result" not in provider.calls[1][-1]["content"]
    assert "place it first" not in provider.calls[1][-1]["content"]
    recall_audits = tuple(
        item
        for item in evidence.projection.model_result_audits
        if item.audit_contract in {
            "model-result-audit.4",
            "model-result-audit.5",
            "model-result-audit.7",
        }
    )
    assert recall_audits
    assert any(item.audit_contract == "model-result-audit.7" for item in recall_audits)
    assert all('"query_text":"之前关于机器人的谈话"' in item.audit_json for item in recall_audits)
    assert all('"mode":"character_pull"' in item.audit_json for item in recall_audits)
    assert all(
        '"accessibility_seed":"character-inner-turn:sha256:' in item.audit_json
        for item in recall_audits
    )
    all_model_statuses = tuple(
        json.loads(item.event.payload()["audit_json"])["status"]
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    )
    assert "main_invalid" not in all_model_statuses


@pytest.mark.asyncio
async def test_nonmetered_recall_followup_keeps_local_contract_identity_off_provider_wire(
    tmp_path,
) -> None:
    provider = _StrictNonMeteredToolRecallProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "nonmetered-recall-contract.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    cognition._character_interior_recall_delegate = False  # noqa: SLF001
    try:
        outcome = await asyncio.wait_for(
            app.respond(
                InboundTurn(
                    platform="test",
                    platform_user_id="user.1",
                    platform_message_id="message:nonmetered-recall-contract",
                    text="你就是个没用的机器人。",
                    observed_at=NOW,
                    trace_id="trace:nonmetered-recall-contract",
                )
            ),
            timeout=2.0,
        )
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert provider.tool_names == ["character_inbound_final_atomic_v1"]


@pytest.mark.asyncio
async def test_scheduled_prefetch_enters_canonical_selective_memory_and_final_audit(
    tmp_path,
) -> None:
    provider = _PrivateTurnStateCombinedProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    interior = compose_fixture_character_interior(inbound_author=cognition)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "character-interior-prefetch.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=interior,
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        first = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:prefetch:first",
                text="你就是个没用的机器人。",
                observed_at=NOW,
                trace_id="trace:prefetch:first",
            )
        )
        second = await app.respond(
            InboundTurn(
                platform="test",
                platform_user_id="user.1",
                platform_message_id="message:prefetch:second",
                text="刚才说机器人的那句话，我想换个说法。",
                observed_at=NOW,
                trace_id="trace:prefetch:second",
            )
        )
        evidence = app.export_replay_evidence()
        interior_health = interior.runtime_health()
    finally:
        app.close()

    assert first.status == "action_authorized"
    assert second.status == "action_authorized"
    assert cognition._recall is None
    assert interior_health["automatic_prefetch_bound"] is True
    assert len(provider.calls) == 2
    second_call = json.loads(provider.calls[1][-1]["content"])
    snapshot = second_call["inner_life_snapshot"]
    assert "automatic_prefetch" in snapshot["faculties"]["selective_memory"]["material_keys"]
    candidates = snapshot["materials"]["automatic_prefetch"]["items"]
    assert candidates
    assert all(item["source_ref"] in snapshot["source_refs"] for item in candidates)
    audits = tuple(
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    )
    assert audits[-1]["status"] == "proposal_validated"
    assert audits[-1]["presented_prefetch_traces"][0]["phase"] == "initial"
    assert audits[-1]["presented_prefetch_traces"][0]["trace"]["mode"] == "prefetch"


@pytest.mark.asyncio
async def test_shadow_episode_config_cannot_reopen_a_parallel_author_lane(tmp_path) -> None:
    provider = _EpisodeCombinedProvider()
    observer = _EpisodeCombinedProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_episode_observer_model=observer,
    )
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-episode-shadow.sqlite",
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    assert len(provider.calls) == 1
    assert observer.calls == []
    # The unified role result intentionally contains two beats; no detached
    # observer is allowed to add or pre-author another one.
    assert len(evidence.projection.actions) == 2
    episode = diagnostics["expression_episode"]
    assert episode["mode"] == "shadow"
    assert episode["turns"] == 0
    interior_health = diagnostics["character_interior"]
    assert interior_health["contract"] == "character-interior-runtime-health.2"
    assert interior_health["legacy_interface_invocations"] == 0
    assert interior_health["parallel_character_author_conflicts"] == 0
    assert interior_health["dual_write_conflicts"] == 0


@pytest.mark.asyncio
async def test_shadow_mode_without_observer_never_opens_another_author(
    tmp_path,
) -> None:
    provider = _EpisodeCombinedProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-episode-shadow-unconfigured.sqlite",
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    assert len(provider.calls) == 1
    assert diagnostics["expression_episode"]["turns"] == 0


@pytest.mark.asyncio
async def test_shadow_config_cannot_consume_recall_or_add_an_action(
    tmp_path,
) -> None:
    provider = _ShadowRecallCombinedProvider()
    shadow = _ShadowPrivateEpisodeProvider()
    capabilities = ExpressionDraftCapabilities(
        profile_id="expression:test-shadow-recall.1",
        modalities=("text",),
        private_turn_state_mode="required",
    )
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_episode_observer_model=shadow,
        expression_capabilities=capabilities,
    )
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-shadow-recall.sqlite",
        config=replace(
            _config(),
            expression_episode_mode="shadow",
            expression_capabilities=capabilities,
        ),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
        evidence = app.export_replay_evidence()
        diagnostics = await app.world_health_diagnostics()
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == 2
    second_call = json.loads(provider.calls[1][-1]["content"])
    assert second_call["inner_life_snapshot"]["contract"] == "inner-life-snapshot.1"
    assert "bounded read-only recall result" not in provider.calls[1][-1]["content"]
    assert shadow.calls == []
    assert len(evidence.projection.actions) == 1
    payloads = tuple(
        item.text for item in evidence.projection.stored_message_payloads if item.text is not None
    )
    assert payloads == ("这句挺刺的，我不想装作没感觉。",)
    assert diagnostics["expression_episode"]["turns"] == 0
    assert not hasattr(cognition._appraisal_materializer, "propose_provisional")


@pytest.mark.asyncio
async def test_private_state_reselection_stays_on_the_unified_lane_when_shadow_configured(
    tmp_path,
) -> None:
    provider = _ShadowPrivateStateRepairProvider()
    shadow = _ShadowPrivateEpisodeProvider()
    capabilities = ExpressionDraftCapabilities(
        profile_id="expression:test-shadow-private-state-reselection.1",
        modalities=("text",),
        private_turn_state_mode="required",
    )
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_episode_observer_model=shadow,
        expression_capabilities=capabilities,
    )
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-shadow-private-state-reselection.sqlite",
        config=replace(
            _config(),
            expression_episode_mode="shadow",
            expression_capabilities=capabilities,
        ),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    assert shadow.calls == []
    assert len(evidence.projection.actions) == 1


@pytest.mark.asyncio
async def test_episode_restart_after_audit_authorizes_without_model_recall(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "single-call-episode-crash.sqlite"
    provider = _EpisodeCombinedProvider()
    observer = _EpisodeCombinedProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        expression_episode_observer_model=observer,
    )
    app = build_sqlite_world_v2_test_application(
        path=path,
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    restarted_observer = _EpisodeCombinedProvider()
    restarted_cognition = InboundCharacterAuthor(
        flash_model=restarted_provider,
        expression_episode_observer_model=restarted_observer,
    )
    restarted = build_sqlite_world_v2_test_application(
        path=path,
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=restarted_cognition,
        ),
        transport=_DeliveredTransport(),
        now=NOW,
    )
    try:
        outcome = await restarted.respond(turn)
        duplicate = await restarted.respond(turn)
        for _ in range(8):
            await restarted.drain_background_once()
            if restarted.export_replay_evidence().projection.appraisals:
                break
        projection = restarted.export_replay_evidence().projection
    finally:
        restarted.close()

    assert outcome.status == "action_authorized"
    assert duplicate.status == "action_authorized"
    assert restarted_provider.calls == []
    assert restarted_observer.calls == []
    assert observer.calls == []
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
    assert len(projection.appraisals) == 1
    assert len(projection.affect_episodes) == 1
    assert all(item.text != "这句话有点伤人。" for item in projection.stored_message_payloads)
    episode = next(
        item for item in projection.trigger_processes if item.process_kind == "expression_episode"
    )
    assert episode.state == "terminal"


@pytest.mark.asyncio
async def test_episode_restart_before_author_resumes_claimed_trigger(tmp_path, monkeypatch) -> None:
    provider = _EpisodeCombinedProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-episode-crash-before-author.sqlite",
        config=replace(_config(), expression_episode_mode="shadow"),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
async def test_invalid_combined_appraisal_is_reselected_before_expression_vertical(
    tmp_path,
) -> None:
    provider = _InvalidAppraisalValidExpressionProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-invalid-appraisal.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    assert len(provider.calls) == 2
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
    cognition = InboundCharacterAuthor(flash_model=provider)

    def reject_invalid_episode_extension(*_args, **_kwargs):
        raise ValueError("new affect component is not a valid episode extension")

    monkeypatch.setattr(
        "companion_daemon.world_v2.affect_acceptance_runtime."
        "AffectAcceptanceRuntime.accept_runtime_owned",
        reject_invalid_episode_extension,
    )
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-invalid-affect-acceptance.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
async def test_inbound_expression_uses_one_generation_call_per_turn(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _OrdinaryCombinedProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    consider_calls = 0
    original_consider = CharacterInterior.consider

    async def counted_consider(self, opportunity):
        nonlocal consider_calls
        if opportunity.purpose == "inbound_turn":
            consider_calls += 1
        return await original_consider(self, opportunity)

    monkeypatch.setattr(CharacterInterior, "consider", counted_consider)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-growing-context.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    assert consider_calls == turns
    model_audits = [
        (item.event.payload(), json.loads(item.event.payload()["audit_json"]))
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    # Each visible expression has one immutable provider result. Durable
    # appraisal acceptance may continue in the background.
    assert len(model_audits) == turns
    assert all(audit["status"] == "proposal_validated" for _payload, audit in model_audits)
    for payload, audit in model_audits:
        expected_opportunity_ref = CausalOpportunityIdentity(
            world_id=_config().world_id,
            actor_ref=_config().companion_actor_ref,
            purpose="inbound_turn",
            source_refs=(payload["trigger_ref"],),
            epoch=payload["trigger_ref"],
        ).opportunity_ref
        assert (
            audit["character_interior_lineage"]["opportunity_ref"]
            == expected_opportunity_ref
        )


@pytest.mark.asyncio
async def test_latency_segment_covers_both_real_provider_requests(tmp_path) -> None:
    clock = _AdvancingClock()
    provider = _TimedCombinedProvider(clock)
    cognition = InboundCharacterAuthor(flash_model=provider)
    latency = ProductionLatencyRecorder(clock_ns=clock)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-latency.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    entry_samples = [item for item in samples if item.segment == "ingress_to_first_role_provider"]
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
                    {"index": index, "embedding": [1.0, 0.0]} for index, _text in enumerate(inputs)
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
    cognition = InboundCharacterAuthor(flash_model=provider)
    latency = ProductionLatencyRecorder(clock_ns=clock)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-embedding-latency.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    assert calls[0]["provider_call_id"].startswith("model-call:foreground-context:")
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
    cognition = InboundCharacterAuthor(flash_model=provider)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-loose-text.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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
    cognition = InboundCharacterAuthor(flash_model=provider)

    output = await cognition.propose(_request(revision=3, call="call:loose-messages"))

    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert output.model_id == "combined-flash"
    assert len(provider.calls) == 2
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
    cognition = InboundCharacterAuthor(flash_model=provider)

    output = await cognition.propose(_request(revision=3, call="call:text-array"))

    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert len(provider.calls) == 2
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
    cognition = InboundCharacterAuthor(flash_model=provider)
    request = _request(revision=3, call="call:unsafe-expression")

    with pytest.raises(ValidationTechnicalFailure):
        await cognition.propose(request)

    # A structurally rejected unified inner-turn result cannot survive as a
    # publishable expression candidate. There is no second advisory cache or
    # legacy expression prepass to inspect or reuse.
    assert not cognition._pending
    assert not cognition._candidate_pending
    assert "failed its exact contract" in caplog.text
    assert "不应被" not in caplog.text


@pytest.mark.asyncio
async def test_loose_unsupported_autobiography_never_reaches_an_action(tmp_path) -> None:
    provider = _UnsupportedAutobiographyProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-unsupported-autobiography.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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

    assert outcome.status == "deferred"
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
    assert top_level_audits[-1]["status"] == "main_exception"


@pytest.mark.asyncio
async def test_model_expression_is_not_replaced_by_a_local_role_template(
    tmp_path,
) -> None:
    provider = _UnsupportedAutobiographyProvider()
    transport = _DeliveredTransport()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        identity_frame=CompanionIdentityFrame(
            companion_name="林乔",
            counterpart_name="Geoff",
            not_an_assistant=True,
        ),
    )
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-role-boundary-failsafe.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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

    assert outcome.status == "deferred"
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
    assert top_level_audits[-1]["status"] == "main_exception"


@pytest.mark.asyncio
async def test_model_owned_world_answer_is_not_rewritten_by_a_keyword_gate(
    tmp_path,
) -> None:
    provider = _TimeoutAfterCombinedProvider()
    transport = _DeliveredTransport()
    cognition = InboundCharacterAuthor(flash_model=provider)
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-world-probe-timeout.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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

    assert outcome.status == "deferred"
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
    assert top_level_audits[-1]["status"] == "main_exception"


@pytest.mark.asyncio
async def test_ordinary_fact_context_does_not_trigger_local_character_prose() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
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

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_timeout")
    assert caught.value.failure_code == "inbound_character_author_unavailable"
    assert provider.calls == []


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
    cognition = InboundCharacterAuthor(flash_model=provider)
    base = _request(revision=3, call="call:generic-silent")
    trigger = base.trigger_message.model_copy(
        update={
            "text": text,
        }
    )
    request = base.model_copy(update={"trigger_message": trigger})

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_timeout")
    assert caught.value.failure_code == "inbound_character_author_unavailable"


@pytest.mark.asyncio
async def test_first_greeting_provider_failure_does_not_invent_a_local_greeting() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="Geoff",
        ),
    )
    base = _request(revision=3, call="call:first-greeting-failsafe")
    request = base.model_copy(
        update={
            "trigger_message": base.trigger_message.model_copy(update={"text": "你好，第一次见。"})
        }
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_invalid_output")
    assert caught.value.failure_code == "inbound_character_author_unavailable"


@pytest.mark.asyncio
async def test_user_fact_provider_failure_does_not_invent_a_local_acknowledgement() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    base = _request(revision=3, call="call:user-fact-failsafe")
    request = base.model_copy(
        update={
            "trigger_message": base.trigger_message.model_copy(
                update={"text": "我叫丁奥轩，英文名 Geoff。"}
            )
        }
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_invalid_output")
    assert caught.value.failure_code == "inbound_character_author_unavailable"


@pytest.mark.asyncio
async def test_first_greeting_provider_failure_records_technical_silence(tmp_path) -> None:
    provider = _AlwaysFailProvider()
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="user:user.1",
        ),
    )
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "first-greeting-failsafe.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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

    assert outcome.status == "deferred"
    assert transport.bodies == []


@pytest.mark.asyncio
async def test_emotional_provider_failure_does_not_force_a_repair_script() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    base = _request(revision=3, call="call:emotion-failsafe")
    request = base.model_copy(
        update={
            "trigger_message": base.trigger_message.model_copy(
                update={"text": "你刚才回得有点敷衍，我有点失望。"}
            )
        }
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_invalid_output")
    assert caught.value.failure_code == "inbound_character_author_unavailable"


@pytest.mark.asyncio
async def test_colloquial_current_activity_probe_does_not_get_local_prose() -> None:
    provider = _GroundedQuickRecoveryProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    base = _request(revision=3, call="call:colloquial-world-probe")
    request = base.model_copy(
        update={
            "trigger_message": base.trigger_message.model_copy(
                update={"text": "所以你现在在干啥呀"}
            )
        }
    )

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.recover(request, "main_invalid_output")
    assert caught.value.failure_code == "inbound_character_author_unavailable"


@pytest.mark.asyncio
async def test_colloquial_world_probe_provider_failure_records_no_fake_reply(tmp_path) -> None:
    provider = _AlwaysFailProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-generic-silent.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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

    assert outcome.status == "deferred"
    assert transport.bodies == []


@pytest.mark.asyncio
async def test_provider_failure_does_not_enter_detached_fallback_or_fake_ack(
    tmp_path,
) -> None:
    backup = _AlwaysFailProvider()
    primary = _FailingProviderWithFallback(backup)
    cognition = InboundCharacterAuthor(flash_model=primary)
    transport = _DeliveredTransport()
    app = build_sqlite_world_v2_test_application(
        path=tmp_path / "single-call-double-provider-failure.sqlite",
        config=_config(),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=cognition,
        ),
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

    assert outcome.status == "deferred"
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
    # Production deliberately keeps technical recovery closed.  The provider's
    # own availability fallback may be attempted, but CharacterInterior must
    # record the unified cognition failure instead of entering a second author
    # path or fabricating a visible acknowledgement.
    assert top_level_audits[-1]["status"] == "main_exception"
    assert top_level_audits[-1]["failure_code"]
    assert top_level_audits[-1]["model_version"] != "local-expression-failsafe.1"
    assert len(primary.calls) == 1
    assert backup.calls == []


@pytest.mark.asyncio
async def test_inbound_author_rejects_settled_world_without_an_observation() -> None:
    """Settled World stimuli belong to the typed Interior role, not inbound chat."""

    cognition = InboundCharacterAuthor(flash_model=_OrdinaryCombinedProvider())
    settlement_request = _request(revision=3, call="call:settled-world-appraisal").model_copy(
        update={
            "trigger_ref": "event:world-occurrence:settled:1",
            "trigger_message": None,
        }
    )

    with pytest.raises(
        ValueError,
        match="single-call inbound cognition requires a verified current message",
    ):
        await cognition.recover(
            settlement_request,
            "main_invalid_output",
        )


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
            if "fresh_context" not in messages[1]["content"]:
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
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "No durable appraisal is needed.",
                        "behavior_tendency": "choose_own_response",
                        "stance": "present",
                        "display_strategy": "model_owned",
                        "confidence": 6_000,
                    },
                    "expression_draft": {
                        "timing_choice": "now",
                        "beats": [{"modality": "text", "text": "我按现在这轮重新想过了。"}],
                        "stance": "fresh_pinned_turn",
                        "brief_rationale": "Choose from the newly pinned Context.",
                        "world_claims": [],
                    },
                },
                ensure_ascii=False,
            )

    provider = _DeferredShapeOriginProvider()
    cognition = InboundCharacterAuthor(flash_model=provider)
    origin = _request(revision=3, call="call:deferred-shape-origin")

    with monkeypatch.context() as patch:
        patch.setattr(
            "companion_daemon.world_v2.character_interior.inbound_author.fit_secondary_call_timeout",
            lambda *_args, **_kwargs: None,
        )
        with pytest.raises(ValidationTechnicalFailure) as caught:
            await cognition.propose(origin)
    assert caught.value.failure_code == "paired_expression_reselection_invalid"

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
    expression = await cognition.propose(current)

    rendered = json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert "我按现在这轮重新想过了" in rendered
    assert "旧游标上的延迟修正" not in rendered
    assert len(provider.calls) == 2
    assert not any(message["role"] == "assistant" for message in provider.calls[1])
    assert "fresh_context" in provider.calls[1][1]["content"]


@pytest.mark.asyncio
async def test_same_cursor_deferred_shape_repair_gets_candidate_wide_final_review() -> None:
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
    cognition = InboundCharacterAuthor(
        flash_model=provider,
        source_closure_model=authority,
        candidate_external_proposition_inventory_model=inventory,
    )
    request = _request(revision=3, call="call:deferred-shape-final-review")

    with pytest.raises(ValidationTechnicalFailure) as caught:
        await cognition.propose(request)
    assert caught.value.failure_code == "authored_expression_reselection_invalid"

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
