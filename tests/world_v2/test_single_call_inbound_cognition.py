from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import threading

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import CompanionIdentityFrame
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.proposal_envelope import (
    DecisionProposal,
    MinimalProposal,
    ProposalEvidenceRef,
)
from companion_daemon.world_v2.immediate_emotion_gate import (
    SemanticImmediateEmotionGate,
    resolve_immediate_emotion_gate,
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
from companion_daemon.world_v2.recall_runtime import (
    RecallCoordinator,
    verify_trusted_recall_trace,
)
from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
)
from companion_daemon.world_v2.production_turn_application import (
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.production_latency_trace import ProductionLatencyRecorder
from companion_daemon.world_v2.world_turn_runtime import InboundTurn
from test_production_turn_application import (
    NOW,
    _DeliveredTransport,
    _Identities,
    _Router,
    _config,
)


class _CombinedProvider:
    model = "combined-flash"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return json.dumps(
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
                    "components": [{"dimension": "hurt", "intensity_bp": 6200}],
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


class _AdvancingClock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> int:
        return self.value

    def advance_ms(self, value: int) -> None:
        self.value += value * 1_000_000


class _TimedCombinedProvider(_OrdinaryCombinedProvider):
    def __init__(self, clock: _AdvancingClock) -> None:
        super().__init__()
        self.clock = clock

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.clock.advance_ms(5_000)
        return await super().complete(messages, temperature=temperature)


class _LooseTextCombinedProvider(_CombinedProvider):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
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
    def __init__(self, expression: dict[str, object]) -> None:
        super().__init__()
        self._expression = expression

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
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
            raise TimeoutError("claim correction timed out")
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
        return json.dumps(
            {
                "response_text": "我接到了，刚才只是慢了一拍。",
                "stance": "acknowledge_briefly",
                "brief_rationale": "Bounded backup response after the main model failed.",
                "confidence": 6000,
            },
            ensure_ascii=False,
        )


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
                "meaning": "disappointment",
                "attribution": "user",
                "severity": 4200,
                "confidence": 7200,
                "affect": "open",
                "affect_dimension": "hurt",
                "affect_intensity_bp": 2200,
            },
            ensure_ascii=False,
        )


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


def _request(*, revision: int, call: str) -> ModelInput:
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
    )


@pytest.mark.asyncio
async def test_invalid_world_claim_prefix_does_not_erase_a_valid_greeting() -> None:
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
    assert len(provider.calls) == 1


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
                                    "value": {"dimension": "hurt", "intensity_bp": 4200},
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
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:backup-expression"})
    )

    assert appraisal.model_id == "combined-flash"
    assert expression.model_id == "combined-flash"
    assert len(primary.calls) == 1
    assert len(backup.calls) == 1
    assert "整理桌面" in backup.calls[0][1]["content"]
    assert "new_acquaintance" in backup.calls[0][1]["content"]
    assert "recent-hurt" in backup.calls[0][1]["content"]
    assert "我在听" in json.dumps(expression.raw_proposal, ensure_ascii=False)


@pytest.mark.asyncio
async def test_backup_failure_enters_local_recovery_without_a_third_model_call() -> None:
    backup = _AlwaysFailProvider()
    primary = _FailingProviderWithFallback(backup)
    cognition = SingleCallInboundCognition(flash_model=primary)
    request = _request(revision=3, call="call:backup-also-fails")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await cognition.appraisal.propose(request)

    with pytest.raises(RuntimeError, match="model-owned expression unavailable"):
        await cognition.expression.recover(request, "main_exception")

    assert len(primary.calls) == 1
    assert len(backup.calls) == 1


@pytest.mark.asyncio
async def test_existing_failover_does_not_call_its_fallback_twice() -> None:
    backup = _AlwaysFailProvider()
    primary = _FailoverAlreadyUsedProvider(backup)
    cognition = SingleCallInboundCognition(flash_model=primary)
    request = _request(revision=3, call="call:failover-already-used")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await cognition.appraisal.propose(request)

    recovered = await cognition.appraisal.recover(request, "main_exception")

    assert recovered.model_version == "single-call-inbound-cognition.1"
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
async def test_one_provider_round_trip_yields_two_independently_bound_proposals() -> None:
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
    # This represents the existing acceptance seam advancing World before the
    # expression proposal is audited.  Cached draft bytes must be rebound to it.
    expression_output = await cognition.expression.propose(
        _request(revision=5, call="call:expression")
    )

    appraisal = DecisionProposal.model_validate_json(json.dumps(appraisal_output.raw_proposal))
    expression = DecisionProposal.model_validate_json(json.dumps(expression_output.raw_proposal))
    assert len(provider.calls) == 1
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
                source_refs=("event:impression:paired:backup",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="PrivateImpressionAccepted",
                        ref="event:impression:paired:backup",
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
    assert output.prefetch_trace is not None
    trace = verify_trusted_recall_trace(output.prefetch_trace)
    assert trace.hits[0].document.source_item_ref == "impression:paired:backup"


@pytest.mark.asyncio
async def test_opt_in_separate_local_appraiser_keeps_expression_on_main_model() -> None:
    appraiser = _SeparateAppraisalProvider()
    expression_provider = _QuickExpressionProvider()
    cognition = SingleCallInboundCognition(
        flash_model=expression_provider,
        appraisal_model=appraiser,
    )

    appraisal_output = await cognition.appraisal.propose(
        _request(revision=3, call="call:local-appraisal")
    )
    expression_output = await cognition.expression.propose(
        _request(revision=4, call="call:local-expression")
    )

    appraisal = DecisionProposal.model_validate_json(json.dumps(appraisal_output.raw_proposal))
    expression = MinimalProposal.model_validate_json(json.dumps(expression_output.raw_proposal))
    assert appraisal_output.model_id == "qwen-local"
    assert expression_output.model_id == "backup-flash"
    assert len(appraiser.calls) == 1
    assert len(expression_provider.calls) == 1
    assert appraisal.proposed_changes[0].kind == "appraisal_transition"
    assert len(expression.action_intents) == 1


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
    assert len(provider.calls) == 1
    assert appraisal.proposed_changes == ()
    assert appraisal.affect_decision == "no_change"
    assert "invalid" in appraisal.brief_rationale.lower()
    assert len(expression.action_intents) == 1


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
async def test_one_call_vertical_accepts_emotion_before_authorizing_expression(tmp_path) -> None:
    provider = _CombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-vertical.sqlite",
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
    event_types = [item.event.event_type for item in evidence.events]
    assert event_types.index("AppraisalAccepted") < event_types.index("ExpressionPlanAccepted")
    assert event_types.index("AffectEpisodeOpened") < event_types.index("ActionAuthorized")
    assert len(evidence.projection.appraisals) == len(evidence.projection.affect_episodes) == 1


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
    recall_audits = tuple(
        item
        for item in evidence.projection.model_result_audits
        if item.audit_contract == "model-result-audit.4"
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
    cognition = SingleCallInboundCognition(flash_model=provider)
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
    assert len(provider.calls) <= 3
    # The full fixture intentionally contains two beats; shadow adds none.
    assert len(evidence.projection.actions) == 2
    episode = diagnostics["expression_episode"]
    assert episode["mode"] == "shadow"
    assert episode["turns"] == 1
    assert episode["candidate_valid"] == 1
    assert episode["slot_calls"] == 2


@pytest.mark.asyncio
async def test_episode_restart_after_audit_authorizes_without_model_recall(
    tmp_path, monkeypatch
) -> None:
    provider = _EpisodeCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-episode-crash.sqlite",
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
        calls_after_crash = len(provider.calls)
        projection_after_crash = app.export_replay_evidence().projection
        episode = next(
            item
            for item in projection_after_crash.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "claimed"
        assert projection_after_crash.proposal_audits
        assert projection_after_crash.actions == ()

        outcome = await app.respond(turn)
        projection = app.export_replay_evidence().projection
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert len(provider.calls) == calls_after_crash
    assert len(projection.expression_plan_manifests) == 1
    assert len(projection.actions) == 2
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

    assert outcome.status == "observed_only"
    assert len(provider.calls) <= 3
    episode = next(
        item for item in projection.trigger_processes if item.process_kind == "expression_episode"
    )
    assert episode.state == "terminal"


@pytest.mark.asyncio
async def test_on_episode_appends_only_after_provisional_receipt(tmp_path) -> None:
    provider = _AppendEpisodeProvider()
    expression = ChatModelDeliberationAdapter(model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "single-call-episode-append.sqlite",
        config=replace(_config(), expression_episode_mode="on"),
        identities=_Identities(),
        router=_Router(),
        main_model=expression,
        quick_recovery=expression,
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
        projection = app.export_replay_evidence().projection
    finally:
        app.close()

    assert outcome.status == "action_authorized"
    assert delivery is not None and delivery.status == "settled"
    assert len(provider.calls) == 2
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
    expression = ChatModelDeliberationAdapter(model=provider)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / f"single-call-episode-{disposition}.sqlite",
        config=replace(_config(), expression_episode_mode="on"),
        identities=_Identities(),
        router=_Router(),
        main_model=expression,
        quick_recovery=expression,
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
    assert len(provider.calls) == 2
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
    assert model_statuses == ["proposal_validated", "proposal_validated"]


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
async def test_ordinary_and_growing_context_use_one_generation_call_per_turn(tmp_path) -> None:
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
    single_call_audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
        and json.loads(item.event.payload()["audit_json"])["model_version"]
        == "single-call-inbound-cognition.1"
    ]
    # Two independent audits per turn are intentional; only the first crosses
    # the provider seam, while the second rebinds cached draft bytes.
    assert len(single_call_audits) == turns * 2
    assert all(item["status"] == "proposal_validated" for item in single_call_audits)


@pytest.mark.asyncio
async def test_latency_segment_covers_combined_provider_once_not_cached_audit(tmp_path) -> None:
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
    context_samples = [item for item in samples if item.segment == "context"]
    assert len(context_samples) == 1
    ledger_samples = [item for item in samples if item.segment == "ledger_commit"]
    assert len(ledger_samples) == 1
    assert ledger_samples[0].duration_ms == 0


@pytest.mark.asyncio
async def test_loose_combined_reply_text_is_salvaged_without_another_provider_call(
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
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_loose_combined_messages_preserve_two_visible_beats_without_retry() -> None:
    provider = _LooseMultiMessageCombinedProvider()
    cognition = SingleCallInboundCognition(flash_model=provider)

    await cognition.appraisal.propose(_request(revision=3, call="call:loose-messages-appraisal"))
    output = await cognition.expression.propose(
        _request(revision=5, call="call:loose-messages-expression")
    )

    proposal = DecisionProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert output.model_id == "combined-flash"
    assert len(provider.calls) == 1
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
    assert len(provider.calls) == 1
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
async def test_structural_salvage_rejects_roles_tools_and_nested_text(
    unsafe_shape: dict[str, object], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _LooseExpressionShapeProvider(unsafe_shape)
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
    assert "structural normalization rejected" in caplog.text
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

    assert outcome.status == "action_authorized"
    assert all("看电影" not in body for body in transport.bodies)
    audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    assert audits[-1]["model_version"] == "single-call-inbound-cognition.1"
    # The paired appraisal already failed closed, so the expression lane now
    # materializes its bounded local repair directly instead of opening a
    # second Deliberation recovery attempt.
    assert audits[-1]["status"] == "proposal_validated"


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

    assert outcome.status == "action_authorized"
    assert delivery is not None and delivery.status == "settled"
    assert len(transport.bodies) == 1
    reply = transport.bodies[0]
    assert reply == "我刚才一直在看电影。"
    audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    assert audits[-1]["model_version"] != "local-expression-failsafe.1"


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

    assert outcome.status == "action_authorized"
    assert delivery is not None and delivery.status == "settled"
    assert len(provider.calls) == 1
    assert len(transport.bodies) == 1
    reply = transport.bodies[0]
    assert reply == "我刚才一直在看电影。"
    audits = [
        json.loads(item.event.payload()["audit_json"])
        for item in evidence.events
        if item.event.event_type == "ModelResultRecorded"
    ]
    assert audits[-1]["model_version"] == "single-call-inbound-cognition.1"
    assert audits[-1]["status"] == "proposal_validated"


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
    output = await cognition.expression.propose(expression_request)
    proposal = MinimalProposal.model_validate_json(json.dumps(output.raw_proposal))
    assert proposal.response_text == "我接到了，刚才只是慢了一拍。"
    assert output.model_version != "local-expression-failsafe.1"
    assert len(provider.calls) == 2
    assert len(recovery.calls) == 1


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
    assert audits[-1]["status"] == "recovery_failed"
    assert audits[-1]["failure_code"]
    assert audits[-1]["model_version"] != "local-expression-failsafe.1"
    assert len(primary.calls) >= 1
    assert len(backup.calls) >= 1
    assert len(primary.calls) + len(backup.calls) <= 3


class _GateVerdictModel:
    model = "qwen-local"

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        return self.raw


class _HangingGateModel(_GateVerdictModel):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.calls.append(messages)
        await asyncio.sleep(5.0)
        return self.raw


@pytest.mark.asyncio
async def test_semantic_emotion_gate_detects_unlabeled_withdrawal() -> None:
    # Cold withdrawal carries no cue-table keyword; the semantic verdict must
    # still select the same-turn emotion lane.
    model = _GateVerdictModel('{"immediate": true}')
    gate = SemanticImmediateEmotionGate(model=model)

    selected = await resolve_immediate_emotion_gate(
        keyword_hit=False,
        text="哦。",
        gate=gate,
        recent_companion_texts=("今天路过那家店还想起你了", "晚上想一起看那部片吗"),
    )

    assert selected is True
    assert len(model.calls) == 1
    # The bounded contrast context reaches the model without the full capsule.
    assert "想起你了" in model.calls[0][1]["content"]
    assert "哦。" in model.calls[0][1]["content"]


@pytest.mark.asyncio
async def test_semantic_emotion_gate_lets_ordinary_sharing_stay_on_fast_lane() -> None:
    model = _GateVerdictModel('```json\n{"immediate": false}\n```')
    gate = SemanticImmediateEmotionGate(model=model)

    selected = await resolve_immediate_emotion_gate(
        keyword_hit=False, text="今天午饭吃了咖喱，好吃！", gate=gate
    )

    assert selected is False
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_semantic_emotion_gate_timeout_falls_back_to_keyword_verdict() -> None:
    model = _HangingGateModel('{"immediate": true}')
    gate = SemanticImmediateEmotionGate(model=model, timeout_seconds=0.05)

    selected = await resolve_immediate_emotion_gate(keyword_hit=False, text="哦。", gate=gate)

    # A slow local model must never block or flip the gate: the keyword miss
    # remains the decision and the reply path continues immediately.
    assert selected is False
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_semantic_emotion_gate_garbage_output_falls_back_to_keyword_verdict() -> None:
    model = _GateVerdictModel("嗯……这条消息看起来有点冷淡，也可能只是忙。")
    gate = SemanticImmediateEmotionGate(model=model)

    selected = await resolve_immediate_emotion_gate(keyword_hit=False, text="哦。", gate=gate)

    assert selected is False
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_semantic_emotion_gate_non_boolean_verdict_is_rejected() -> None:
    model = _GateVerdictModel('{"immediate": "true"}')
    gate = SemanticImmediateEmotionGate(model=model)

    assert await gate.assess(text="哦。") is None


@pytest.mark.asyncio
async def test_keyword_hit_selects_same_turn_emotion_without_a_model_call() -> None:
    model = _GateVerdictModel('{"immediate": false}')
    gate = SemanticImmediateEmotionGate(model=model)

    selected = await resolve_immediate_emotion_gate(
        keyword_hit=True, text="你让我很失望。", gate=gate
    )

    # Keyword hits are free and authoritative; spending the model call here
    # would only add latency to the highest-signal turns.
    assert selected is True
    assert not model.calls


@pytest.mark.asyncio
async def test_gate_without_semantic_model_keeps_pure_keyword_behavior() -> None:
    assert await resolve_immediate_emotion_gate(keyword_hit=False, text="哦。", gate=None) is False
    assert (
        await resolve_immediate_emotion_gate(keyword_hit=True, text="你让我很失望。", gate=None)
        is True
    )


def test_cognition_exposes_gate_only_when_a_local_appraiser_exists() -> None:
    with_local = SingleCallInboundCognition(
        flash_model=_OrdinaryCombinedProvider(),
        appraisal_model=_GateVerdictModel('{"immediate": false}'),
    )
    without_local = SingleCallInboundCognition(flash_model=_OrdinaryCombinedProvider())

    assert isinstance(with_local.appraisal.immediate_emotion_gate, SemanticImmediateEmotionGate)
    assert without_local.appraisal.immediate_emotion_gate is None
