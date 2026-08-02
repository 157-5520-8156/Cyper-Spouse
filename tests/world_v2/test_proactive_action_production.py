from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
from hashlib import sha256
import json
import threading
from types import SimpleNamespace

import pytest

import companion_daemon.world_v2.proactive_action as proactive_action_module
from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.biographical_claim_authority import (
    biographical_coordinate_authorities,
)
from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
    CompanionIdentityFrame,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelOutput,
    ModelRoute,
    ModelUsageProvenance,
    RouteRequest,
    TriggerMessage,
    ValidationTechnicalFailure,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.expression_draft import TEXT_ONLY_EXPRESSION_CAPABILITIES
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.expression_plan_acceptance import ExpressionPlanBudgetPolicy
from companion_daemon.world_v2.interactive_turn_budget import InteractiveTurnBudgetPolicy
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.proactive_action import (
    ProactiveActionRuntime,
    ProactiveDeliberationTurn,
    ProactiveDraftAdapter,
    ProactiveOpportunity,
    ProactiveTechnicalRetryState,
    next_proactive_retry_due,
    proactive_technical_retry_states,
)
from companion_daemon.world_v2.proposal_envelope import (
    DecisionProposal,
    ProposalEvidenceRef,
    validate_proposal_envelope,
)
from companion_daemon.world_v2.production_proposal_grammar import compose_production_deliberation
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.qq_c2c_transport import QQC2CPlatformTransport
from companion_daemon.world_v2.recall_index import (
    FeatureHashRecallEmbedding,
    InMemoryRecallIndex,
    RecallCursor,
    RecallDocument,
    RecallEmbedding,
    RecallSourceBinding,
)
from companion_daemon.world_v2.recall_runtime import (
    RecallCoordinator,
    verify_trusted_recall_trace,
)
from companion_daemon.world_v2.social_initiative import SocialInitiativePolicy
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    DueWindow,
    EvidenceRef,
    Observation,
    ProviderReceipt,
    ThreadOrigin,
    ThreadProjection,
    ThreadProposalProjection,
    ThreadProposedMutation,
    ThreadValues,
    WorldEvent,
    thread_semantic_fingerprint,
)
from companion_daemon.world_v2.thread_events import ThreadChangedPayload, thread_mutation_hash
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
WORLD = "world:proactive-production"


def _proactive_model_request() -> ModelInput:
    source_ref = "event:ambient:1"
    return ModelInput(
        call_id="call:proactive-grounding:1",
        attempt_id="attempt:proactive-grounding:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref=source_ref,
        evaluated_world_revision=9,
        trigger_evidence=(
            ProposalEvidenceRef(
                ref_id=source_ref,
                evidence_kind="settled_world_event",
                source_world_revision=8,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
        model_content_json=json.dumps(
            {
                "logical_time": NOW.isoformat(),
                "slices": {
                    "advisories": {
                        "items": [
                            {
                                "value": {
                                    "kind": "proactive_opportunity",
                                    "candidate_refs": ["ambient_presence:epoch:1"],
                                    "source_refs": [source_ref],
                                    "candidates": [{"value": "ambient context"}],
                                }
                            }
                        ]
                    },
                    "recent_dialogue": {
                        "availability": "available",
                        "source_refs": ["event:user:shenzhen"],
                        "items": [
                            {
                                "item_ref": "event:user:shenzhen",
                                "value": {
                                    "speaker": "counterpart",
                                    "text": "深圳说实话不是很好玩哈哈哈哈",
                                },
                            }
                        ],
                    },
                    "user_facts": {"availability": "available", "items": []},
                },
            },
            ensure_ascii=False,
        ),
    )


class _ProactiveReplySequence:
    model = "test-proactive-grounding"

    def __init__(self, replies: list[dict[str, object] | str]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        self.messages.append(messages)
        reply = self.replies.pop(0)
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


def _usage(*, provider: str, ordinal: int) -> ModelUsageProvenance:
    material = {
        "usage_contract": "model-usage.1",
        "route_class": "chat",
        "input_tokens": 10,
        "output_tokens": 2,
        "thinking_tokens": 0,
        "token_provenance": "provider_reported",
        "transport": "provider_api",
        "provider": provider,
        "provider_usage_ref": f"usage:{provider}:{ordinal}",
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


class _MeteredProactiveReplySequence(_ProactiveReplySequence):
    async def complete_with_usage(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.messages.append(messages)
        reply = self.replies.pop(0)
        raw = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return raw, _usage(provider=self.model, ordinal=self.calls)

    async def complete_json_with_usage(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        return await self.complete_with_usage(messages, temperature=temperature)


def _source_closure_review(
    *,
    claim_indexes: tuple[int, ...] = (),
    visible_failures: tuple[str, ...] = (),
    visible_findings: tuple[dict[str, object], ...] = (),
) -> str:
    return json.dumps(
        {
            "ci": list(claim_indexes),
            "v": list(visible_failures),
            "p": [],
            "visible_findings": list(visible_findings),
            "r": "Review only exact factual source closure.",
        },
        ensure_ascii=False,
    )


def _supporting_source_reviewer() -> _ProactiveReplySequence:
    return _ProactiveReplySequence([_source_closure_review()])


@pytest.mark.asyncio
async def test_proactive_role_receives_lived_context_without_audit_request_duplication() -> None:
    model = _ProactiveReplySequence([_proactive_draft("刚看见个诗集名字，莫名有点喜欢。")])

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=_supporting_source_reviewer(),
    ).propose(_proactive_model_request())

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert proposal.proactive_opportunity_decision.disposition == "engage_now"

    system = model.messages[0][0]["content"]
    assert "not a message outline" in system
    assert "Always return private_turn_state" not in system
    assert "Always return" not in system
    assert "world_claims" not in system

    supplied = json.loads(model.messages[0][1]["content"])
    assert set(supplied) == {
        "current_self_state",
        "expression_capabilities",
        "failure_code",
        "lived_context",
        "proactive_opportunity",
    }
    assert (
        supplied["lived_context"]["slices"]["recent_dialogue"]["items"][0]["value"]["text"]
        == "深圳说实话不是很好玩哈哈哈哈"
    )
    assert supplied["current_self_state"]["contract"] == "current-self-state.1"
    assert "request" not in supplied
    assert "proactive_fact_authority" not in supplied


@pytest.mark.asyncio
async def test_proactive_claim_binding_is_post_authorship_and_uses_only_visible_lived_evidence() -> (
    None
):
    visible_binding_ref = "event:life-content:experience:visible"
    hidden_binding_ref = "event:life-content:experience:hidden"
    request = _proactive_model_request()
    context = json.loads(request.model_content_json)
    context["slices"]["recent_experiences"] = {
        "availability": "available",
        "source_refs": [visible_binding_ref, hidden_binding_ref],
        "items": [
            {
                "item_ref": "experience:visible",
                "source_bindings": [{"ref": visible_binding_ref}],
                "value": {"summary": "在旧书交换活动里翻到一本《小王子》。"},
            },
            *(
                {
                    "item_ref": f"experience:filler:{index}",
                    "source_bindings": [{"ref": f"event:life-content:filler:{index}"}],
                    "value": {"summary": f"较低排序的经历 {index}"},
                }
                for index in range(1, 3)
            ),
            {
                "item_ref": "experience:hidden",
                "source_bindings": [{"ref": hidden_binding_ref}],
                "value": {"summary": "不应进入角色工作记忆的隐藏经历。"},
            },
        ],
    }
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )
    role = _ProactiveReplySequence(
        [_proactive_draft("刚翻到一本《小王子》，突然有点想跟你说句话。")]
    )
    binder = _ProactiveReplySequence(
        [
            {
                "contract": "proactive-world-claim-binding.1",
                "world_claims": [
                    {
                        "claim_text": "沈知栀在旧书交换活动里翻到一本《小王子》",
                        "scope": "past_world",
                        "source_refs": [visible_binding_ref],
                    }
                ],
            }
        ]
    )
    reviewer = _supporting_source_reviewer()

    output = await ProactiveDraftAdapter(
        model=role,
        target="user:primary",
        proactive_claim_binder_model=binder,
        source_closure_reviewer=reviewer,
    ).propose(request)

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert proposal.action_intents
    assert role.calls == 1
    assert binder.calls == 1
    binder_audit = next(
        audit
        for audit in output.provider_subcall_audits
        if audit.purpose.startswith("proactive_claim_binding")
    )
    assert binder_audit.parent_model_call_id == output.winning_model_call_id
    assert binder_audit.request_hash
    assert binder_audit.response_hash
    binder_packet = json.loads(binder.messages[0][1]["content"])
    assert visible_binding_ref in json.dumps(binder_packet, ensure_ascii=False)
    assert hidden_binding_ref not in json.dumps(binder_packet, ensure_ascii=False)
    assert (
        binder_packet["exact_selected_evidence"]["pinned_time"]["authority_transport_availability"]
        == "semantic_only_no_world_claim_authority"
    )
    review_packet = json.loads(reviewer.messages[0][1]["content"])
    assert visible_binding_ref in json.dumps(review_packet, ensure_ascii=False)
    assert hidden_binding_ref not in json.dumps(review_packet, ensure_ascii=False)
    expression_change = proposal.proposed_changes[0].payload.value()
    assert expression_change["world_claims"] == [
        {
            "claim_text": "沈知栀在旧书交换活动里翻到一本《小王子》",
            "scope": "past_world",
            "source_refs": [visible_binding_ref],
        }
    ]


@pytest.mark.asyncio
async def test_proactive_claim_binding_retries_invalid_wire_once_without_reauthoring() -> None:
    role = _ProactiveReplySequence([_proactive_draft("刚刚忽然有点想和你说句话。")])
    binder = _ProactiveReplySequence(
        [
            {"not_the_contract": True},
            {
                "contract": "proactive-world-claim-binding.1",
                "world_claims": [],
            },
        ]
    )

    output = await ProactiveDraftAdapter(
        model=role,
        target="user:primary",
        proactive_claim_binder_model=binder,
        source_closure_reviewer=_supporting_source_reviewer(),
    ).propose(_proactive_model_request())

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert proposal.action_intents
    assert role.calls == 1
    assert binder.calls == 2
    correction = json.loads(binder.messages[1][-1]["content"])
    assert correction["contract"] == "proactive-world-claim-binding-correction.1"


@pytest.mark.asyncio
async def test_proactive_claim_binding_fails_technically_after_one_invalid_retry() -> None:
    role = _ProactiveReplySequence([_proactive_draft("刚刚忽然有点想和你说句话。")])
    binder = _ProactiveReplySequence(["{}", "{}"])
    reviewer = _supporting_source_reviewer()

    with pytest.raises(
        ValidationTechnicalFailure,
        match="proactive_claim_binding_invalid",
    ):
        await ProactiveDraftAdapter(
            model=role,
            target="user:primary",
            proactive_claim_binder_model=binder,
            source_closure_reviewer=reviewer,
        ).propose(_proactive_model_request())

    assert role.calls == 1
    assert binder.calls == 2
    assert reviewer.calls == 0


@pytest.mark.asyncio
async def test_proactive_claim_binding_bogus_ref_is_technical_not_role_reselection() -> None:
    role = _ProactiveReplySequence([_proactive_draft("刚刚忽然有点想和你说句话。")])
    invalid_binding = {
        "contract": "proactive-world-claim-binding.1",
        "world_claims": [
            {
                "claim_text": "沈知栀刚刚去了火星",
                "scope": "current_world",
                "source_refs": ["event:not-visible:anywhere"],
            }
        ],
    }
    binder = _ProactiveReplySequence([invalid_binding, invalid_binding])
    reviewer = _supporting_source_reviewer()

    with pytest.raises(
        ValidationTechnicalFailure,
        match="proactive_claim_binding_invalid",
    ):
        await ProactiveDraftAdapter(
            model=role,
            target="user:primary",
            proactive_claim_binder_model=binder,
            source_closure_reviewer=reviewer,
        ).propose(_proactive_model_request())

    assert role.calls == 1
    assert binder.calls == 2
    assert reviewer.calls == 0


@pytest.mark.asyncio
async def test_claim_only_semantic_rejection_retries_binder_not_character() -> None:
    source_ref = "event:life-content:experience:visible"
    request = _proactive_model_request()
    context = json.loads(request.model_content_json)
    context["slices"]["recent_experiences"] = {
        "availability": "available",
        "items": [
            {
                "item_ref": "experience:visible",
                "source_bindings": [{"ref": source_ref}],
                "value": {"summary": "在旧书交换活动里翻到一本《小王子》。"},
            }
        ],
    }
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )
    role = _ProactiveReplySequence([_proactive_draft("刚刚忽然有点想和你说句话。")])
    binder = _ProactiveReplySequence(
        [
            {
                "contract": "proactive-world-claim-binding.1",
                "world_claims": [
                    {
                        "claim_text": "沈知栀刚去了火星",
                        "scope": "past_world",
                        "source_refs": [source_ref],
                    }
                ],
            },
            {
                "contract": "proactive-world-claim-binding.1",
                "world_claims": [],
            },
        ]
    )
    reviewer = _ProactiveReplySequence(
        [
            _source_closure_review(claim_indexes=(0,)),
            _source_closure_review(),
        ]
    )

    output = await ProactiveDraftAdapter(
        model=role,
        target="user:primary",
        proactive_claim_binder_model=binder,
        source_closure_reviewer=reviewer,
    ).propose(request)

    assert validate_proposal_envelope(output.raw_proposal).action_intents
    assert role.calls == 1
    assert binder.calls == 2
    assert reviewer.calls == 2
    correction_packet = json.loads(binder.messages[1][1]["content"])
    assert correction_packet["previous_binding_failure"]


@pytest.mark.asyncio
async def test_undeclared_visible_fact_retries_binder_not_character() -> None:
    source_ref = "event:life-content:experience:visible"
    request = _proactive_model_request()
    context = json.loads(request.model_content_json)
    context["slices"]["recent_experiences"] = {
        "availability": "available",
        "items": [
            {
                "item_ref": "experience:visible",
                "source_bindings": [{"ref": source_ref}],
                "value": {"summary": "在旧书交换活动里翻到一本《小王子》。"},
            }
        ],
    }
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )
    role = _ProactiveReplySequence([_proactive_draft("刚翻到一本《小王子》，突然想跟你说句话。")])
    binder = _ProactiveReplySequence(
        [
            {
                "contract": "proactive-world-claim-binding.1",
                "world_claims": [],
                "unbound_claim_locators": [],
            },
            {
                "contract": "proactive-world-claim-binding.1",
                "world_claims": [
                    {
                        "claim_text": "沈知栀在旧书交换活动里翻到一本《小王子》",
                        "scope": "past_world",
                        "source_refs": [source_ref],
                    }
                ],
                "unbound_claim_locators": [],
            },
        ]
    )
    reviewer = _ProactiveReplySequence(
        [
            _source_closure_review(
                visible_failures=("undeclared_external_assertion",),
                visible_findings=(
                    {
                        "category": "undeclared_external_assertion",
                        "visible_span": "刚翻到一本《小王子》",
                        "claim_index": None,
                        "source_relation": "unclosed",
                        "source_refs": [],
                    },
                ),
            ),
            _source_closure_review(),
        ]
    )

    output = await ProactiveDraftAdapter(
        model=role,
        target="user:primary",
        proactive_claim_binder_model=binder,
        source_closure_reviewer=reviewer,
    ).propose(request)

    assert validate_proposal_envelope(output.raw_proposal).action_intents
    assert role.calls == 1
    assert binder.calls == 2
    assert reviewer.calls == 2


@pytest.mark.asyncio
async def test_cancelled_proactive_binder_preserves_provider_subcall_audit() -> None:
    class _BlockingBinder:
        model = "test-blocking-binder"

        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def complete(self, _messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del temperature
            self.started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    role = _ProactiveReplySequence([_proactive_draft("刚刚忽然有点想和你说句话。")])
    binder = _BlockingBinder()
    adapter = ProactiveDraftAdapter(
        model=role,
        target="user:primary",
        proactive_claim_binder_model=binder,
        source_closure_reviewer=_supporting_source_reviewer(),
    )
    task = asyncio.create_task(adapter.propose(_proactive_model_request()))
    await binder.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError) as captured:
        await task

    failure = captured.value.world_v2_validation_technical_failure
    assert failure.failure_code == "source_review_timeout"
    binder_audit = next(
        audit
        for audit in failure.provider_subcall_audits
        if audit.purpose.startswith("proactive_claim_binding")
    )
    assert binder_audit.outcome == "timeout"
    assert binder_audit.parent_model_call_id
    assert binder_audit.request_hash


@pytest.mark.asyncio
async def test_unbound_claim_locator_must_match_visible_expression_exactly() -> None:
    role = _ProactiveReplySequence([_proactive_draft("刚刚忽然有点想和你说句话。")])
    invalid = {
        "contract": "proactive-world-claim-binding.1",
        "world_claims": [],
        "unbound_claim_locators": [
            {
                "beat_index": 0,
                "char_start": 0,
                "char_end": 2,
                "text": "火星",
            }
        ],
    }
    binder = _ProactiveReplySequence([invalid, invalid])

    with pytest.raises(
        ValidationTechnicalFailure,
        match="proactive_claim_binding_invalid",
    ):
        await ProactiveDraftAdapter(
            model=role,
            target="user:primary",
            proactive_claim_binder_model=binder,
            source_closure_reviewer=_supporting_source_reviewer(),
        ).propose(_proactive_model_request())

    assert role.calls == 1
    assert binder.calls == 2


@pytest.mark.asyncio
async def test_malformed_role_reselection_is_not_misattributed_to_prior_reviewer() -> None:
    text = "我刚刚去了一个没有来源的地方。"
    role = _ProactiveReplySequence([_proactive_draft(text), "{}"])
    binder = _ProactiveReplySequence(
        [
            {
                "contract": "proactive-world-claim-binding.1",
                "world_claims": [],
                "unbound_claim_locators": [
                    {
                        "beat_index": 0,
                        "char_start": 0,
                        "char_end": len(text),
                        "text": text,
                    }
                ],
            }
        ]
    )
    reviewer = _ProactiveReplySequence(
        [
            _source_closure_review(
                visible_failures=("undeclared_external_assertion",),
                visible_findings=(
                    {
                        "category": "undeclared_external_assertion",
                        "visible_span": text,
                        "claim_index": None,
                        "source_relation": "unclosed",
                        "source_refs": [],
                    },
                ),
            )
        ]
    )

    with pytest.raises(ValidationTechnicalFailure) as captured:
        await ProactiveDraftAdapter(
            model=role,
            target="user:primary",
            proactive_claim_binder_model=binder,
            source_closure_reviewer=reviewer,
        ).propose(_proactive_model_request())

    assert captured.value.failure_code == "authored_expression_reselection_invalid"
    assert role.calls == 2
    assert binder.calls == 1
    assert reviewer.calls == 1


def _candidate_inventory(
    text: str,
    *,
    semantic_role: str = "standalone_external_proposition",
) -> str:
    return json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.3",
            "propositions": [
                {
                    "locator": {
                        "beat_index": 0,
                        "char_start": 0,
                        "char_end": len(text),
                        "text": text,
                    },
                    "semantic_role": semantic_role,
                    "parent_index": None,
                }
            ],
        },
        ensure_ascii=False,
    )


class _ProactiveCoverageAuthority:
    model = "test-proactive-coverage-authority"

    def __init__(self, *, unclosed_texts: tuple[str, ...] = (), metered: bool = False) -> None:
        self._unclosed_texts = frozenset(unclosed_texts)
        self._metered = metered
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    async def complete_json(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
        raw, _usage_value = await self._complete(messages, temperature=temperature)
        return raw

    async def complete_json_with_usage(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
        return await self._complete(messages, temperature=temperature)

    async def _complete(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        self.messages.append(messages)
        payload = json.loads(messages[-1]["content"])
        contract = payload.get("output_contract", {}).get("contract")
        if contract == "source-closure-review.7":
            raw = _source_closure_review()
        else:
            assert contract == "candidate-external-proposition-coverage.1"
            findings = []
            for locator in payload["locators"]:
                unclosed = locator["text"] in self._unclosed_texts
                findings.append(
                    {
                        "locator": locator,
                        "decision": "unclosed" if unclosed else "closed",
                        "source_relation": (
                            "unclosed" if unclosed else "first_person_immediate_private_continuity"
                        ),
                        "source_refs": [],
                    }
                )
            raw = json.dumps(
                {
                    "contract": "candidate-external-proposition-coverage.1",
                    "findings": findings,
                },
                ensure_ascii=False,
            )
        return raw, _usage(provider=self.model, ordinal=self.calls)


class _EmptyV5ProactiveCoverageAuthority:
    model = "test-empty-v5-proactive-coverage-authority"

    def __init__(self) -> None:
        self.calls = 0

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-coverage.5"

    async def complete_json(self, _messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return json.dumps(
            {
                "contract": "candidate-external-proposition-coverage.5",
                "findings": [],
            },
            ensure_ascii=False,
        )


class _RainAssociationEmbedding:
    version = "rain-association-fixture.1"
    dimensions = 2

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            (1.0, 0.0) if ("听雨" in value or "雨夜" in value) else (0.0, 1.0) for value in texts
        )


class _BlockingProactivePrefetchEmbedding:
    version = "blocking-proactive-prefetch-fixture.1"
    dimensions = FeatureHashRecallEmbedding.dimensions

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self._delegate = FeatureHashRecallEmbedding()

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        if any("听雨" in value for value in texts):
            self.started.set()
            self.release.wait(timeout=2)
        return self._delegate.embed(texts)


def _proactive_draft(text: str, *, claims: list[dict[str, object]] | None = None):
    return {
        "timing_choice": "now",
        "cadence": "conversational",
        "beats": [{"modality": "text", "text": text}],
        "stance": "curious",
        "brief_rationale": "The present context brought the counterpart to mind.",
        "impulse_summary": "突然想到对方，想顺着这个念头问一句。",
        "world_claims": claims or [],
        "confidence": 7_000,
    }


def _private_proactive_draft(
    choice: str,
    *,
    attended_source_ref: str,
    inner_state_summary: str = "此刻想到对方，想按自己的感觉决定要不要开口。",
    claims: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "private_turn_state": {
            "contract": "private-turn-state.1",
            "inner_state_summary": inner_state_summary,
            "attended_source_refs": [attended_source_ref],
        },
        "timing_choice": choice,
        "cadence": "conversational",
        "stance": "self_directed",
        "brief_rationale": "根据此刻真实注意到的情境作出选择。",
        "impulse_summary": "此刻自然地想到了对方。",
        "world_claims": claims or [],
        "confidence": 7_000,
    }
    if choice != "silent":
        value["beats"] = [{"modality": "text", "text": "刚刚忽然想和你说句话。"}]
    if choice == "later":
        value.update(delay_seconds=60, expires_after_seconds=600)
    return value


@pytest.mark.asyncio
async def test_proactive_expression_uses_shared_multi_beat_draft_and_plan_binding() -> None:
    model = _ProactiveReplySequence(
        [
            {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "想到对方刚好在意的事，想自然地补两句。",
                    "attended_source_refs": ["event:ambient:1"],
                },
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [
                    {"modality": "text", "text": "刚刚突然想起你。"},
                    {"modality": "text", "text": "这边的雨声有点像下午那会儿。"},
                ],
                "stance": "warm",
                "brief_rationale": "此刻有自然的分享冲动。",
                "impulse_summary": "雨声把注意力带回了这段关系。",
                "confidence": 7000,
                "world_claims": [],
            }
        ]
    )
    capabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )
    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        expression_capabilities=capabilities,
    ).propose(_proactive_model_request())

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert len(proposal.action_intents) == 2
    assert proposal.action_intents[1].dependencies == (proposal.action_intents[0].intent_id,)
    payload = proposal.proposed_changes[0].payload.value()
    binding = payload["proactive_source_plan_binding_v2"]
    assert binding["beat_payload_hashes"] == [
        action.payload_hash for action in proposal.action_intents
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", (False, True))
async def test_required_private_turn_state_is_independent_of_json_field_order(
    recovery: bool,
) -> None:
    model = _ProactiveReplySequence(
        [
            {
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [{"modality": "text", "text": "刚刚忽然想和你说句话。"}],
                "stance": "warm",
                "brief_rationale": "此刻想说。",
                "impulse_summary": "一个当下的念头。",
                "confidence": 7000,
                "world_claims": [],
                # Deliberately last on the provider wire. JSON object member
                # order is transport serialization, not causal evidence.
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": "想到对方，想说句话。",
                    "attended_source_refs": ["event:ambient:1"],
                },
            }
        ]
    )
    capabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )
    adapter = ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        expression_capabilities=capabilities,
    )

    output = (
        await adapter.recover(_proactive_model_request(), "primary_timeout")
        if recovery
        else await adapter.propose(_proactive_model_request())
    )

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert model.calls == 1
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.inner_state_summary == "想到对方，想说句话。"


@pytest.mark.asyncio
async def test_missing_required_proactive_private_turn_state_reselects_complete_draft() -> None:
    model = _ProactiveReplySequence(
        [
            {
                "timing_choice": "now",
                "cadence": "conversational",
                "beats": [{"modality": "text", "text": "这份草稿缺少私人状态。"}],
                "stance": "invalid_without_private_state",
                "brief_rationale": "Fixture.",
                "impulse_summary": "一个当下的念头。",
                "confidence": 7000,
                "world_claims": [],
            },
            _private_proactive_draft(
                "now",
                attended_source_ref="event:ambient:1",
            ),
        ]
    )
    capabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        expression_capabilities=capabilities,
    ).propose(_proactive_model_request())

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert model.calls == 2
    assert "private_turn_state.missing" in model.messages[1][-1]["content"]
    assert "这份草稿缺少私人状态" not in json.dumps(model.messages[1], ensure_ascii=False)
    assert proposal.private_turn_state is not None


@pytest.mark.asyncio
async def test_malformed_proactive_expression_gets_one_exact_structural_reselection() -> None:
    model = _ProactiveReplySequence(
        [
            {"timing_choice": "now"},
            _proactive_draft("忽然想跟你说句话。"),
        ]
    )

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
    ).propose(_proactive_model_request())

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert proposal.timing_choice == "now"
    assert model.calls == 2
    repair = json.loads(model.messages[1][-1]["content"])
    assert repair["contract"] == "proactive-expression-structure-reselection.1"
    assert "Exact ExpressionDraft JSON field contract" in repair["wire_contract"]
    assert "impulse_summary is required" in repair["wire_contract"]
    assert "valid ExpressionDraft" in repair["validation_failure"]
    assert "brief_rationale" in repair["validation_failure"]
    assert "忽然想跟你说句话" not in json.dumps(model.messages[0], ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["proactive_opportunity", "expression_draft"])
async def test_proactive_parser_accepts_provider_mirrored_opportunity_wrapper(
    wrapper: str,
) -> None:
    draft = _proactive_draft("刚刚忽然想和你说句话。")
    model = _ProactiveReplySequence([{wrapper: draft}])

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
    ).propose(_proactive_model_request())

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert proposal.timing_choice == "now"
    assert model.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "expected_action_kind"),
    (("now", "proactive_message"), ("later", "followup"), ("silent", None)),
)
async def test_unpinned_proactive_private_state_is_reselected_before_source_review(
    choice: str,
    expected_action_kind: str | None,
) -> None:
    forged_ref = "memory:untrusted:not-in-pinned-context"
    corrected_ref = "event:user:shenzhen"
    model = _ProactiveReplySequence(
        [
            _private_proactive_draft(choice, attended_source_ref=forged_ref),
            _private_proactive_draft(choice, attended_source_ref=corrected_ref),
        ]
    )
    inventory = _ProactiveReplySequence(
        [
            {
                "contract": "candidate-external-proposition-inventory.5",
                "propositions": [],
            }
        ]
        if choice != "silent"
        else []
    )
    authority = _EmptyV5ProactiveCoverageAuthority()
    capabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        expression_capabilities=capabilities,
        source_closure_reviewer=authority,
        candidate_external_proposition_inventory_model=inventory,
    ).propose(_proactive_model_request())
    proposal = validate_proposal_envelope(output.raw_proposal)

    assert isinstance(proposal, DecisionProposal)
    assert model.calls == 2
    assert inventory.calls == (0 if choice == "silent" else 1)
    assert authority.calls == (0 if choice == "silent" else 1)
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.attended_source_refs == (corrected_ref,)
    assert proposal.proactive_grounding_outcome == "corrected"
    if expected_action_kind is None:
        assert proposal.action_intents == ()
    else:
        assert tuple(intent.kind for intent in proposal.action_intents) == (expected_action_kind,)
    correction_messages = model.messages[1]
    rendered_correction = json.dumps(correction_messages, ensure_ascii=False)
    assert forged_ref not in rendered_correction
    assert "private_turn_state.unpinned_source" in rendered_correction
    assert "private_turn_state.attended_source_refs" in rendered_correction


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ("now", "later", "silent"))
async def test_second_unpinned_proactive_private_state_fails_closed_without_source_review(
    choice: str,
) -> None:
    model = _ProactiveReplySequence(
        [
            _private_proactive_draft(
                choice,
                attended_source_ref="memory:untrusted:first",
            ),
            _private_proactive_draft(
                choice,
                attended_source_ref="memory:untrusted:second",
            ),
        ]
    )
    inventory = _ProactiveReplySequence([])
    capabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )

    with pytest.raises(ValidationTechnicalFailure) as failure:
        await ProactiveDraftAdapter(
            model=model,
            target="user:primary",
            expression_capabilities=capabilities,
            candidate_external_proposition_inventory_model=inventory,
        ).propose(_proactive_model_request())

    assert failure.value.failure_code == "authored_expression_reselection_invalid"
    assert model.calls == 2
    assert inventory.calls == 0


@pytest.mark.asyncio
async def test_grounding_rejected_proactive_proposal_retains_valid_private_state_audit() -> None:
    unsupported_claim = {
        "claim_text": "对方之前说去成都看熊猫",
        "scope": "counterpart_history",
        "source_refs": ["event:user:chengdu:not-in-context"],
    }
    corrected_summary = "仍然想起对方，但没有可引用的那段经历。"
    model = _ProactiveReplySequence(
        [
            _private_proactive_draft(
                "now",
                attended_source_ref="event:ambient:1",
                claims=[unsupported_claim],
            ),
            _private_proactive_draft(
                "now",
                attended_source_ref="event:ambient:1",
                inner_state_summary=corrected_summary,
                claims=[unsupported_claim],
            ),
        ]
    )
    capabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        expression_capabilities=capabilities,
    ).propose(_proactive_model_request())
    proposal = validate_proposal_envelope(output.raw_proposal)

    assert isinstance(proposal, DecisionProposal)
    assert proposal.proactive_grounding_outcome == "rejected"
    assert proposal.private_turn_state is not None
    assert proposal.private_turn_state.inner_state_summary == corrected_summary
    assert proposal.proposed_changes == ()
    assert proposal.action_intents == ()


@pytest.mark.asyncio
async def test_proactive_private_state_changes_audit_not_effect_identity() -> None:
    capabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )
    request = _proactive_model_request()
    context = json.loads(request.model_content_json)
    attention_refs = ("attention:context-only:first", "attention:context-only:second")
    context["slices"]["recent_dialogue"]["items"][0]["attention_source_refs"] = list(attention_refs)
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )
    first_output = await ProactiveDraftAdapter(
        model=_ProactiveReplySequence(
            [
                _private_proactive_draft(
                    "now",
                    attended_source_ref=attention_refs[0],
                    inner_state_summary="想起这段关系，愿意现在开口。",
                )
            ]
        ),
        target="user:primary",
        expression_capabilities=capabilities,
    ).propose(request)
    second_output = await ProactiveDraftAdapter(
        model=_ProactiveReplySequence(
            [
                _private_proactive_draft(
                    "now",
                    attended_source_ref=attention_refs[1],
                    inner_state_summary="注意到旧对话，仍然愿意现在开口。",
                )
            ]
        ),
        target="user:primary",
        expression_capabilities=capabilities,
    ).propose(request)
    first = validate_proposal_envelope(first_output.raw_proposal)
    second = validate_proposal_envelope(second_output.raw_proposal)

    assert isinstance(first, DecisionProposal)
    assert isinstance(second, DecisionProposal)
    assert first.private_turn_state != second.private_turn_state
    assert first.proposal_hash != second.proposal_hash
    assert first.proposal_id == second.proposal_id
    assert first.effect_hash == second.effect_hash
    assert first.proposed_changes == second.proposed_changes
    assert first.action_intents == second.action_intents
    effect_material = json.dumps(
        {
            "changes": [item.model_dump(mode="json") for item in first.proposed_changes],
            "actions": [item.model_dump(mode="json") for item in first.action_intents],
        },
        ensure_ascii=False,
    )
    assert "private_turn_state" not in effect_material
    assert all(source_ref not in effect_material for source_ref in attention_refs)


@pytest.mark.asyncio
async def test_later_proactive_multi_beat_plan_preserves_ordered_effect_once_actions() -> None:
    ledger, _unused_model, _unused_runtime, _turn = _runtime(choice="silent")
    model = _ProactiveReplySequence(
        [
            {
                "timing_choice": "later",
                "cadence": "hesitant",
                "delay_seconds": 60,
                "expires_after_seconds": 600,
                "beats": [
                    {"modality": "text", "text": "我先把刚才的念头放一下。"},
                    {"modality": "text", "text": "晚一点再慢慢跟你讲。"},
                ],
                "stance": "thoughtful",
                "brief_rationale": "更适合稍后自然接续。",
                "impulse_summary": "这件事还想留一点余地。",
                "confidence": 7000,
                "world_claims": [],
            }
        ]
    )
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - acceptance seam fixture
        model=model,
    )

    assert (await runtime.drain_one()).status == "opened"
    result = await runtime.drain_one()
    assert result.status == "authorized"
    actions = ledger.project().actions
    assert len(actions) == 2
    assert actions[1].dependencies == (actions[0].action_id,)
    assert actions[0].not_before == actions[1].not_before
    assert actions[0].expires_at == actions[1].expires_at


def _proactive_biography_request() -> ModelInput:
    request = _proactive_model_request()
    context = json.loads(request.model_content_json)
    context["slices"]["world_life"] = {
        "availability": "available",
        "items": [
            {
                "item_ref": "biography:summer-home",
                "value": {
                    "context_kind": "biographical_context",
                    "logical_at": NOW.isoformat(),
                    "age": 21,
                    "academic_phase": "summer_break",
                    "season": "summer",
                    "current_residence_context_tags": ["residence:family_home_jiaxing"],
                    "active_life_arcs": [],
                },
            }
        ],
    }
    return request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )


@pytest.mark.asyncio
async def test_proactive_biography_parent_cannot_authorize_an_occurrence() -> None:
    unsupported = _proactive_draft(
        "早上翻旧书的时候突然想起以前在嘉兴老街逛书店的日子。",
        claims=[
            {
                "claim_text": "沈知栀今天早上翻旧书并想起以前逛老街书店",
                "scope": "current_world",
                "source_refs": ["biography:summer-home"],
            }
        ],
    )
    model = _ProactiveReplySequence([unsupported, unsupported])

    output = await ProactiveDraftAdapter(model=model, target="user:primary").propose(
        _proactive_biography_request()
    )
    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert model.calls == 2
    assert proposal.proactive_grounding_outcome == "rejected"
    assert not proposal.action_intents
    first_request = json.loads(model.messages[0][1]["content"])
    biography = first_request["lived_context"]["slices"]["world_life"]["items"][0]
    assert biography["source_ref"] == "biography:summer-home"
    assert biography["value"]["academic_phase"] == "summer_break"
    assert "proactive_fact_authority" not in first_request
    correction_messages = model.messages[1]
    assert unsupported["beats"][0]["text"] not in json.dumps(
        correction_messages, ensure_ascii=False
    )
    correction = json.loads(correction_messages[-1]["content"])
    assert correction["contract"] == "proactive-grounding-reselection.1"
    assert correction["validation_failure"] == {
        "code": "proactive_world_claim_source_lane_mismatch",
        "path": "world_claims[].source_refs",
    }
    assert "original_draft" not in correction


@pytest.mark.asyncio
async def test_proactive_can_cite_an_exact_current_biographical_coordinate() -> None:
    request = _proactive_biography_request()
    context = json.loads(request.model_content_json)
    residence_ref = next(
        item.source_ref
        for item in biographical_coordinate_authorities(context)
        if item.field_path == "/current_residence_context_tags"
    )
    model = _ProactiveReplySequence(
        [
            _proactive_draft(
                "我现在在嘉兴家里，刚才忽然想和你说句话。",
                claims=[
                    {
                        "claim_text": "沈知栀当前住处情境是嘉兴家庭住处",
                        "scope": "current_world",
                        "source_refs": [residence_ref],
                    }
                ],
            )
        ]
    )

    reviewer = _supporting_source_reviewer()
    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=reviewer,
    ).propose(request)
    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert model.calls == 1
    assert reviewer.calls == 1
    assert proposal.proactive_grounding_outcome == "not_required"
    assert proposal.action_intents


@pytest.mark.asyncio
async def test_proactive_source_review_resolves_the_same_full_pinned_context_seen_by_role() -> None:
    source_ref = "event:life-content:experience:production-visible"
    request = _proactive_model_request()
    context = json.loads(request.model_content_json)
    context["slices"]["recent_experiences"] = {
        "availability": "available",
        "items": [
            {
                "item_ref": "experience:production-visible",
                "source_bindings": [{"ref": source_ref}],
                "value": {"summary": "在旧书交换活动里翻到一本《小王子》。"},
            }
        ],
    }
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )
    model = _ProactiveReplySequence(
        [
            _proactive_draft(
                "刚才翻到那本《小王子》的时候，忽然有点想和你说句话。",
                claims=[
                    {
                        "claim_text": "沈知栀在旧书交换活动里翻到一本《小王子》",
                        "scope": "past_world",
                        "source_refs": [source_ref],
                    }
                ],
            )
        ]
    )
    reviewer = _supporting_source_reviewer()

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=reviewer,
    ).propose(request)

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert proposal.proactive_grounding_outcome == "not_required"
    assert reviewer.calls == 1
    review = json.loads(reviewer.messages[0][1]["content"])
    assert any(source_ref in entry["source_refs"] for entry in review["source_evidence"]["entries"])


@pytest.mark.asyncio
async def test_proactive_cannot_use_age_coordinate_to_prove_a_life_occurrence() -> None:
    request = _proactive_biography_request()
    context = json.loads(request.model_content_json)
    age_ref = next(
        item.source_ref
        for item in biographical_coordinate_authorities(context)
        if item.field_path == "/age"
    )
    unsupported = _proactive_draft(
        "今天早上翻书架时看到一本旧书，忽然想起你。",
        claims=[
            {
                "claim_text": "沈知栀今天早上翻书架时看到一本旧书",
                "scope": "current_world",
                "source_refs": [age_ref],
            }
        ],
    )
    model = _ProactiveReplySequence([unsupported, unsupported])
    reviewer = _ProactiveReplySequence(
        [
            _source_closure_review(
                claim_indexes=(0,),
                visible_failures=("occurrence_or_status_authority_mismatch",),
                visible_findings=(
                    {
                        "category": "occurrence_or_status_authority_mismatch",
                        "visible_span": "今天早上翻书架时看到一本旧书",
                        "claim_index": 0,
                        "source_relation": "declared_world_claim_source_mismatch",
                        "source_refs": [age_ref],
                    },
                ),
            ),
            _source_closure_review(
                claim_indexes=(0,),
                visible_failures=("occurrence_or_status_authority_mismatch",),
                visible_findings=(
                    {
                        "category": "occurrence_or_status_authority_mismatch",
                        "visible_span": "今天早上翻书架时看到一本旧书",
                        "claim_index": 0,
                        "source_relation": "declared_world_claim_source_mismatch",
                        "source_refs": [age_ref],
                    },
                ),
            ),
        ]
    )

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=reviewer,
    ).propose(request)
    proposal = validate_proposal_envelope(output.raw_proposal)

    assert isinstance(proposal, DecisionProposal)
    assert model.calls == 2
    assert reviewer.calls == 2
    assert proposal.proactive_grounding_outcome == "rejected"
    assert proposal.proactive_opportunity_decision.disposition == "grounding_rejected"
    assert not proposal.action_intents
    first_review = json.loads(reviewer.messages[0][1]["content"])
    coordinate_entry = next(
        entry
        for entry in first_review["source_evidence"]["entries"]
        if entry["kind"] == "biographical_coordinate"
    )
    assert coordinate_entry["material"]["field_path"] == "/age"
    assert coordinate_entry["material"]["value"] == 21


@pytest.mark.asyncio
async def test_proactive_history_claim_is_corrected_once_against_pinned_sources() -> None:
    model = _ProactiveReplySequence(
        [
            _proactive_draft(
                "你之前说去成都看熊猫，后来怎么样？",
                claims=[
                    {
                        "claim_text": "你之前说去成都看熊猫",
                        "scope": "counterpart_history",
                        "source_refs": ["event:user:chengdu:not-in-context"],
                    }
                ],
            ),
            _proactive_draft(
                "你之前说深圳不太好玩，后来回想起来还是这个感觉吗？",
                claims=[
                    {
                        "claim_text": "你之前说深圳不太好玩",
                        "scope": "counterpart_history",
                        "source_refs": ["event:user:shenzhen"],
                    }
                ],
            ),
        ]
    )

    reviewer = _supporting_source_reviewer()
    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=reviewer,
    ).propose(_proactive_model_request())
    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)

    assert model.calls == 2
    assert reviewer.calls == 1
    assert proposal.proactive_grounding_outcome == "corrected"
    assert proposal.impulse_summary == "突然想到对方，想顺着这个念头问一句。"
    assert (
        "成都" not in proposal.proposed_changes[0].payload.value()["beat_drafts"][0]["inline_text"]
    )


@pytest.mark.asyncio
async def test_proactive_corrected_candidate_gets_its_own_report_relative_review() -> None:
    """A correction is a distinct author candidate, even in the proactive lane."""

    current_report = "深圳说实话不是很好玩哈哈哈哈"
    request = _proactive_model_request().model_copy(
        update={
            "trigger_ref": "event:user:current-shenzhen",
            "trigger_evidence": (
                ProposalEvidenceRef(
                    ref_id="event:user:current-shenzhen",
                    evidence_kind="observed_message",
                    source_world_revision=8,
                    immutable_hash="sha256:" + "c" * 64,
                ),
            ),
            "trigger_message": TriggerMessage(
                event_ref="event:user:current-shenzhen",
                event_payload_hash="sha256:" + "c" * 64,
                observation_ref="observation:current-shenzhen",
                source_world_revision=8,
                actor="user:primary",
                channel="qq:c2c",
                reply_target="user:primary",
                text=current_report,
            ),
        }
    )
    model = _ProactiveReplySequence(
        [
            _proactive_draft(
                "你上次去成都看熊猫，后来怎么样？",
                claims=[
                    {
                        "claim_text": "你上次去成都看熊猫",
                        "scope": "counterpart_history",
                        "source_refs": ["event:user:chengdu:not-in-context"],
                    }
                ],
            ),
            _proactive_draft(
                "你刚才说深圳不好玩，是哪里让你觉得没劲？",
                claims=[
                    {
                        "claim_text": "你刚才说深圳不好玩",
                        "scope": "counterpart_history",
                        "source_refs": ["event:user:shenzhen"],
                    }
                ],
            ),
        ]
    )
    primary = _ProactiveReplySequence(
        [
            _source_closure_review(
                visible_failures=("undeclared_external_assertion",),
                visible_findings=(
                    {
                        "category": "undeclared_external_assertion",
                        "visible_span": "你刚才说深圳不好玩",
                        "claim_index": None,
                        "source_relation": "unclosed",
                        "source_refs": [],
                    },
                ),
            )
        ]
    )
    narrow = _ProactiveReplySequence(
        [
            {
                "contract": "report-relative-entailment-adjudication.2",
                "findings": [
                    {
                        "finding_index": 0,
                        "decision": "covered_by_exact_current_report",
                        "failure_dimensions": [],
                    }
                ],
                "r": "The question directly takes up the exact current report.",
            }
        ]
    )

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=primary,
        report_relative_reviewer=narrow,
    ).propose(request)
    proposal = validate_proposal_envelope(output.raw_proposal)

    assert isinstance(proposal, DecisionProposal)
    assert proposal.proactive_grounding_outcome == "corrected"
    assert model.calls == 2
    assert primary.calls == 1
    assert narrow.calls == 1


@pytest.mark.asyncio
async def test_proactive_history_claim_is_rejected_after_one_bad_correction() -> None:
    model = _ProactiveReplySequence(
        [
            _proactive_draft(
                "你之前说去成都看熊猫，后来怎么样？",
                claims=[
                    {
                        "claim_text": "你之前说去成都看熊猫",
                        "scope": "counterpart_history",
                        "source_refs": ["event:user:chengdu:not-in-context"],
                    }
                ],
            ),
            _proactive_draft(
                "你之前说成都熊猫很好玩，对吧？",
                claims=[
                    {
                        "claim_text": "你之前说成都熊猫很好玩",
                        "scope": "counterpart_history",
                        "source_refs": ["event:user:chengdu:not-in-context"],
                    }
                ],
            ),
        ]
    )

    output = await ProactiveDraftAdapter(model=model, target="user:primary").propose(
        _proactive_model_request()
    )
    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)

    assert model.calls == 2
    assert proposal.proactive_grounding_outcome == "rejected"
    assert proposal.proactive_opportunity_decision.disposition == "grounding_rejected"
    assert not proposal.action_intents


@pytest.mark.asyncio
async def test_proactive_legacy_subjective_claim_cannot_launder_a_life_event() -> None:
    unsupported_life_claim = {
        "claim_text": "我下午在家泡茶翻书",
        "scope": "subjective_or_hypothetical",
        "source_refs": [],
    }
    model = _ProactiveReplySequence(
        [
            _proactive_draft(
                "我下午在家泡茶翻书的时候突然想起你。",
                claims=[unsupported_life_claim],
            ),
            _proactive_draft(
                "我下午在家泡茶翻书的时候突然想起你。",
                claims=[unsupported_life_claim],
            ),
        ]
    )

    output = await ProactiveDraftAdapter(model=model, target="user:primary").propose(
        _proactive_model_request()
    )
    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert model.calls == 2
    assert proposal.proactive_grounding_outcome == "rejected"
    assert proposal.proactive_opportunity_decision.disposition == "grounding_rejected"
    assert not proposal.action_intents


@pytest.mark.asyncio
async def test_companion_old_reply_cannot_prove_a_counterpart_experience() -> None:
    request = _proactive_model_request()
    context = json.loads(request.model_content_json)
    context["slices"]["recent_dialogue"] = {
        "availability": "available",
        "source_refs": ["event:companion:chengdu"],
        "items": [
            {
                "item_ref": "event:companion:chengdu",
                "value": {
                    "speaker": "companion",
                    "text": "你之前说去成都看熊猫。",
                },
            }
        ],
    }
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )
    model = _ProactiveReplySequence(
        [
            _proactive_draft(
                "你之前说去成都看熊猫，后来怎么样？",
                claims=[
                    {
                        "claim_text": "你之前说去成都看熊猫",
                        "scope": "counterpart_history",
                        "source_refs": ["event:companion:chengdu"],
                    }
                ],
            ),
            _proactive_draft(
                "你之前说去成都看熊猫，后来怎么样？",
                claims=[
                    {
                        "claim_text": "你之前说去成都看熊猫",
                        "scope": "counterpart_history",
                        "source_refs": ["event:companion:chengdu"],
                    }
                ],
            ),
        ]
    )

    output = await ProactiveDraftAdapter(model=model, target="user:primary").propose(request)
    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert proposal.proactive_grounding_outcome == "rejected"


@pytest.mark.asyncio
async def test_claim_free_proactive_greeting_passes_candidate_wide_review() -> None:
    text = "刚刚忽然想和你说句话。"
    model = _ProactiveReplySequence([_proactive_draft(text)])
    inventory = _ProactiveReplySequence(
        [_candidate_inventory(text, semantic_role="outer_private_state")]
    )
    reviewer = _ProactiveCoverageAuthority()

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=reviewer,
        candidate_external_proposition_inventory_model=inventory,
    ).propose(_proactive_model_request())
    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)

    assert model.calls == 1
    assert inventory.calls == 1
    assert reviewer.calls == 1
    assert proposal.proactive_grounding_outcome == "not_required"
    assert proposal.action_intents


@pytest.mark.asyncio
async def test_no_external_candidate_inventory_needs_no_source_authority_for_subjective_greeting() -> (
    None
):
    text = "刚刚忽然想和你说句话。"
    model = _ProactiveReplySequence([_proactive_draft(text)])
    inventory = _ProactiveReplySequence(
        [_candidate_inventory(text, semantic_role="outer_private_state")]
    )

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        candidate_external_proposition_inventory_model=inventory,
    ).propose(_proactive_model_request())
    proposal = validate_proposal_envelope(output.raw_proposal)

    assert isinstance(proposal, DecisionProposal)
    assert proposal.action_intents
    assert inventory.calls == 1


@pytest.mark.asyncio
async def test_nonempty_candidate_inventory_fails_closed_without_source_authority() -> None:
    invented = "你下午说已经把延迟问题彻底解决了。"
    model = _ProactiveReplySequence([_proactive_draft(invented)])
    inventory = _ProactiveReplySequence([_candidate_inventory(invented)])

    with pytest.raises(
        ValidationTechnicalFailure,
        match="proactive_source_closure_reviewer_unavailable",
    ):
        await ProactiveDraftAdapter(
            model=model,
            target="user:primary",
            candidate_external_proposition_inventory_model=inventory,
        ).propose(_proactive_model_request())

    assert model.calls == 1
    assert inventory.calls == 1


@pytest.mark.asyncio
async def test_undeclared_counterpart_fact_is_reselected_with_usage_and_winner_identity() -> None:
    invented = "你下午说已经把延迟问题彻底解决了。"
    corrected = "刚刚忽然想和你说句话。"
    model = _MeteredProactiveReplySequence(
        [_proactive_draft(invented), _proactive_draft(corrected)]
    )
    inventory = _MeteredProactiveReplySequence(
        [
            _candidate_inventory(invented),
            _candidate_inventory(
                corrected,
                semantic_role="outer_private_state",
            ),
        ]
    )
    inventory.model = "test-proactive-inventory"
    reviewer = _ProactiveCoverageAuthority(
        unclosed_texts=(invented,),
        metered=True,
    )
    request = _proactive_model_request()

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=reviewer,
        candidate_external_proposition_inventory_model=inventory,
    ).propose(request)
    proposal = validate_proposal_envelope(output.raw_proposal)

    assert isinstance(proposal, DecisionProposal)
    assert proposal.proactive_grounding_outcome == "corrected"
    assert corrected in json.dumps(output.raw_proposal, ensure_ascii=False)
    assert invented not in json.dumps(output.raw_proposal, ensure_ascii=False)
    assert model.calls == 2
    assert inventory.calls == 2
    assert reviewer.calls == 3
    assert output.usage is not None
    assert output.usage.input_tokens == 70
    assert output.usage.output_tokens == 14
    assert output.winning_model_call_id is not None
    assert output.winning_model_call_id != request.call_id
    correction_messages = model.messages[1]
    expected_request_hash = sha256(
        json.dumps(
            {"messages": correction_messages, "temperature": 0.2},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert output.winning_request_hash == expected_request_hash
    rendered_correction = json.dumps(correction_messages, ensure_ascii=False)
    assert invented not in rendered_correction
    assert "original_draft" not in rendered_correction
    correction = json.loads(correction_messages[-1]["content"])
    assert correction["contract"] == "source-closure-reselection.2"
    assert correction["rejected_candidate_sha256"]
    assert correction["rejected_categories"] == {
        "ci": [],
        "v": ["undeclared_external_assertion"],
        "p": [],
    }


@pytest.mark.asyncio
async def test_undeclared_companion_life_fact_is_grounding_rejected_after_one_reselection() -> None:
    invented = "我下午去学校旁边的新书店逛了两小时。"
    model = _ProactiveReplySequence([_proactive_draft(invented), _proactive_draft(invented)])
    inventory = _ProactiveReplySequence(
        [_candidate_inventory(invented), _candidate_inventory(invented)]
    )
    reviewer = _ProactiveCoverageAuthority(unclosed_texts=(invented,))

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=reviewer,
        candidate_external_proposition_inventory_model=inventory,
    ).propose(_proactive_model_request())
    proposal = validate_proposal_envelope(output.raw_proposal)

    assert isinstance(proposal, DecisionProposal)
    assert proposal.proactive_grounding_outcome == "rejected"
    assert proposal.proactive_opportunity_decision.disposition == "grounding_rejected"
    assert not proposal.action_intents
    assert model.calls == 2
    assert inventory.calls == 2
    assert reviewer.calls == 4


def _proactive_recall_fixture(
    *,
    semantic_embedding: RecallEmbedding | None = None,
) -> tuple[ModelInput, RecallCoordinator]:
    request = _proactive_model_request()
    context = json.loads(request.model_content_json)
    context["slices"]["current_situation"] = {
        "availability": "available",
        "source_refs": ["event:ambient:1"],
        "items": [
            {
                "item_ref": "situation:rain",
                "value": {"activity": "在窗边听雨，手边放着一杯热茶"},
            }
        ],
    }
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )
    cursor = RecallCursor(
        world_revision=request.evaluated_world_revision,
        deliberation_revision=request.evaluated_deliberation_revision,
        ledger_sequence=request.evaluated_ledger_sequence,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:experience:rain",
                memory_kind="episodic",
                source_item_ref="experience:rain",
                source_slice="recent_experiences",
                source_refs=("event:experience:rain",),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="ExperienceCommitted",
                        ref="event:experience:rain",
                        source_world_revision=7,
                        immutable_hash="c" * 64,
                    ),
                ),
                source_world_revision=7,
                text="上次雨夜回宿舍时，她在便利店买了热乌龙。",
                actor_ref="agent:companion",
                subject_refs=("agent:companion",),
                occurred_from=NOW - timedelta(days=12),
                privacy_class="private",
            ),
        ),
    )
    recall = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        semantic_embedding=semantic_embedding or _RainAssociationEmbedding(),
        trigger_ref=request.trigger_ref,
    )
    return request, recall


@pytest.mark.asyncio
async def test_unselected_proactive_prefetch_is_not_model_result_provenance() -> None:
    request, recall = _proactive_recall_fixture()
    model = _ProactiveReplySequence([_proactive_draft("刚刚忽然想和你说句话。")])
    adapter = ProactiveDraftAdapter(model=model, target="user:primary")
    adapter.install_recall_coordinator(recall)

    output = await adapter.propose(request)

    health = recall.semantic_health()
    assert health["prefetch_first_pass_local_count"] == 0
    assert health["prefetch_first_pass_semantic_count"] == 0
    assert output.prefetch_trace is None
    assert output.recall_trace is None


@pytest.mark.asyncio
async def test_unselected_proactive_prefetch_never_delays_or_claims_first_pass_delivery() -> None:
    embedding = _BlockingProactivePrefetchEmbedding()
    request, recall = _proactive_recall_fixture(semantic_embedding=embedding)
    model = _ProactiveReplySequence([_proactive_draft("刚刚忽然想和你说句话。")])
    adapter = ProactiveDraftAdapter(model=model, target="user:primary")
    adapter.install_recall_coordinator(recall)

    started = asyncio.get_running_loop().time()
    try:
        output = await adapter.propose(request)
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        embedding.release.set()
        recall.close()

    assert elapsed < 0.1
    assert output.prefetch_trace is None
    health = recall.semantic_health()
    assert health["prefetch_first_pass_local_count"] == 0
    assert health["prefetch_first_pass_semantic_count"] == 0


@pytest.mark.asyncio
async def test_proactive_character_chosen_recall_reuses_prefetch_then_forms_final_draft() -> None:
    request, recall = _proactive_recall_fixture()
    model = _ProactiveReplySequence(
        [
            {"recall_request": {"query_text": "雨夜 听雨", "limit": 4}},
            _proactive_draft("雨声让我突然想起一件旧事。"),
        ]
    )
    adapter = ProactiveDraftAdapter(model=model, target="user:primary")
    adapter.install_recall_coordinator(recall)

    output = await adapter.propose(request)

    assert output.prefetch_trace is not None
    assert verify_trusted_recall_trace(output.prefetch_trace).hits
    assert output.recall_trace is not None
    assert verify_trusted_recall_trace(output.recall_trace).hits
    assert model.calls == 2
    assert "either JSON member order" in model.messages[0][0]["content"]
    assert "first followed by recall_request" not in model.messages[0][0]["content"]
    assert "上次雨夜回宿舍" in model.messages[-1][-1]["content"]
    request_hash = sha256(
        json.dumps(
            {"messages": model.messages[-1], "temperature": 0.8},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    expected_call_id = (
        "model-call:"
        + sha256(
            json.dumps(
                {
                    "parent_call_id": request.call_id,
                    "purpose": "recall_followup",
                    "request_hash": request_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert output.winning_request_hash == request_hash
    assert output.winning_model_call_id == expected_call_id
    assert tuple(item.phase for item in output.presented_prefetch_traces) == ("recall_followup",)
    assert output.presented_prefetch_traces[0].model_call_id == expected_call_id


@pytest.mark.asyncio
async def test_proactive_recall_is_in_the_post_authorship_binding_closure() -> None:
    request, recall = _proactive_recall_fixture()
    role = _ProactiveReplySequence(
        [
            {"recall_request": {"query_text": "雨夜 听雨", "limit": 4}},
            _proactive_draft("上次雨夜回宿舍，我在便利店买了热乌龙。"),
        ]
    )
    binder = _ProactiveReplySequence(
        [
            {
                "contract": "proactive-world-claim-binding.1",
                "world_claims": [
                    {
                        "claim_text": "沈知栀上次雨夜回宿舍时在便利店买了热乌龙",
                        "scope": "past_world",
                        "source_refs": ["event:experience:rain"],
                    }
                ],
            }
        ]
    )
    reviewer = _supporting_source_reviewer()
    adapter = ProactiveDraftAdapter(
        model=role,
        target="user:primary",
        proactive_claim_binder_model=binder,
        source_closure_reviewer=reviewer,
    )
    adapter.install_recall_coordinator(recall)

    output = await adapter.propose(request)

    assert validate_proposal_envelope(output.raw_proposal).action_intents
    assert role.calls == 2
    assert binder.calls == 1
    binder_packet = json.loads(binder.messages[0][1]["content"])
    rendered_binding = json.dumps(binder_packet, ensure_ascii=False)
    assert "上次雨夜回宿舍" in rendered_binding
    assert "event:experience:rain" in rendered_binding
    review_packet = json.loads(reviewer.messages[0][1]["content"])
    assert "event:experience:rain" in json.dumps(review_packet, ensure_ascii=False)


@pytest.mark.asyncio
async def test_valid_grounded_proactive_claim_runs_one_source_review() -> None:
    model = _ProactiveReplySequence(
        [
            _proactive_draft(
                "你之前说深圳不太好玩。",
                claims=[
                    {
                        "claim_text": "你之前说深圳不太好玩",
                        "scope": "counterpart_history",
                        "source_refs": ["event:user:shenzhen"],
                    }
                ],
            )
        ]
    )

    reviewer = _supporting_source_reviewer()
    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        source_closure_reviewer=reviewer,
    ).propose(_proactive_model_request())
    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert model.calls == 1
    assert reviewer.calls == 1
    assert proposal.proactive_grounding_outcome == "not_required"


@pytest.mark.asyncio
async def test_malformed_grounding_correction_remains_a_technical_failure() -> None:
    model = _ProactiveReplySequence(
        [
            _proactive_draft(
                "你之前说去成都看熊猫，后来怎么样？",
                claims=[
                    {
                        "claim_text": "你之前说去成都看熊猫",
                        "scope": "counterpart_history",
                        "source_refs": ["event:user:chengdu:not-in-context"],
                    }
                ],
            ),
            "{not-json",
        ]
    )

    with pytest.raises(ValidationTechnicalFailure) as captured:
        await ProactiveDraftAdapter(model=model, target="user:primary").propose(
            _proactive_model_request()
        )
    assert captured.value.failure_code == "authored_expression_reselection_invalid"
    assert model.calls == 2


def _event(
    event_id: str, event_type: str, payload: dict[str, object], *, at: datetime = NOW
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=at,
        created_at=at,
        actor="system:test",
        source="test",
        trace_id="trace:proactive",
        causation_id="cause:proactive",
        correlation_id="conversation:proactive",
        idempotency_key=(
            domain_idempotency_key(event_type=event_type, world_id=WORLD, payload=payload)
            or "test:" + event_id
        ),
        payload=payload,
    )


def _commit(ledger: WorldLedger, *events: WorldEvent) -> None:
    projection = ledger.project()
    ledger.commit(
        events,
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def _seed_due_thread(
    ledger: WorldLedger,
    *,
    consideration_horizon: timedelta = timedelta(hours=8),
    thread_key: str = "1",
    advance_clock: bool = True,
    event_at: datetime = NOW,
) -> None:
    suffix = "" if thread_key == "1" else f":{thread_key}"
    source = EvidenceRef(
        ref_id="operator:unfinished-thought" + suffix,
        evidence_type="operator_observation",
        claim_purpose="conversation_continuity",
        immutable_hash="a" * 64,
    )
    _commit(
        ledger,
        _event(
            "event:operator:unfinished" + suffix,
            "OperatorObservationRecorded",
            {
                "observation_id": source.ref_id,
                "observation_hash": source.immutable_hash,
            },
            at=event_at,
        ),
    )
    projection = ledger.project()
    origin = ThreadOrigin(
        change_id=f"change:thread:pulse:{thread_key}",
        transition_id=f"transition:thread:pulse:{thread_key}",
        policy_refs=("policy:thread-v1",),
        accepted_event_ref="event:thread:pulse:opened" + suffix,
    )
    values = ThreadValues(
        kind="topic_open",
        subject_ref="subject:unfinished-thought" + suffix,
        conversation_ref="conversation:proactive",
        anchor_evidence_refs=(source,),
        source_evidence_refs=(source,),
        importance_bp=7_000,
        due_window=DueWindow(
            opens_at=max(NOW + timedelta(minutes=1), event_at),
            closes_at=NOW + consideration_horizon,
        ),
        expires_at=NOW + consideration_horizon,
        resolution_contract_ref="resolution:unfinished-thought" + suffix,
        privacy_class="private",
    )
    thread = ThreadProjection(
        thread_id=f"thread:pulse:{thread_key}",
        entity_revision=1,
        semantic_fingerprint=thread_semantic_fingerprint(
            kind=values.kind,
            subject_ref=values.subject_ref,
            conversation_ref=values.conversation_ref,
            anchor_evidence_refs=values.anchor_evidence_refs,
            resolution_contract_ref=values.resolution_contract_ref,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        opened_at=event_at,
        updated_at=event_at,
    )
    raw: dict[str, object] = {
        "change_id": origin.change_id,
        "transition_id": origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": (source,),
        "policy_refs": origin.policy_refs,
        "acceptance_id": f"acceptance:thread:pulse:{thread_key}",
        "proposal_id": f"proposal:thread:pulse:{thread_key}",
        "evaluated_world_revision": projection.world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": "open",
        "thread_before": None,
        "thread_after": thread,
        "compensates_transition_id": None,
    }
    raw["accepted_change_hash"] = thread_mutation_hash(raw)
    changed = ThreadChangedPayload.model_validate(raw)
    proposed = ThreadProposalProjection(
        proposal_id=changed.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:thread.1",
        transition_kind="open",
        change_id=changed.change_id,
        transition_id=changed.transition_id,
        evaluated_world_revision=changed.evaluated_world_revision,
        expected_entity_revision=0,
        proposed_change_hash=changed.accepted_change_hash,
        evidence_refs=changed.evidence_refs,
        policy_refs=changed.policy_refs,
        proposed_mutation=ThreadProposedMutation(
            event_type="ThreadOpened",
            payload_json=json.dumps(
                changed.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    _commit(
        ledger,
        _event(
            "event:proposal:thread:pulse:" + thread_key,
            "ProposalRecorded",
            proposed.model_dump(mode="json"),
            at=event_at,
        ),
    )
    _commit(
        ledger,
        _event(
            "event:acceptance:thread:pulse:" + thread_key,
            "AcceptanceRecorded",
            {
                "acceptance_id": changed.acceptance_id,
                "status": "accepted",
                "proposal_id": changed.proposal_id,
                "evaluated_world_revision": changed.evaluated_world_revision,
                "accepted_change_id": changed.change_id,
                "accepted_change_hash": changed.accepted_change_hash,
            },
            at=event_at,
        ),
        _event(
            origin.accepted_event_ref,
            "ThreadOpened",
            changed.model_dump(mode="json"),
            at=event_at,
        ),
    )
    if advance_clock:
        due = NOW + timedelta(minutes=2)
        _commit(
            ledger,
            _event(
                "event:clock:thread-due",
                "ClockAdvanced",
                {
                    "logical_time_from": NOW.isoformat(),
                    "logical_time_to": due.isoformat(),
                },
                at=due,
            ),
        )


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="proactive-test", router_version="test.1")


class _InvalidMain:
    async def propose(self, _request: ModelInput) -> ModelOutput:
        return ModelOutput(model_id="invalid", model_version="test.1", raw_proposal={})


class _InvalidQuick:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        return ModelOutput(model_id="invalid-quick", model_version="test.1", raw_proposal={})


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert platform == "http" and platform_user_id == "user.1"
        return "user:primary", "user:primary"


class _NoDispatchTransport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("invalid ordinary turn must not dispatch")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _DeliveredTransport:
    provider = "platform:test"

    def __init__(self) -> None:
        self.bodies: list[str] = []

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.bodies.append(request.body)
        return PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:social-initiative:{len(self.bodies)}",
            provider_ref=f"message:social-initiative:{len(self.bodies)}",
            status="delivered",
            received_at=NOW + timedelta(minutes=2),
            raw_payload_hash="sha256:" + "b" * 64,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _FailedTransport(_DeliveredTransport):
    async def send(self, request):  # type: ignore[no-untyped-def]
        self.bodies.append(request.body)
        return PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:social-initiative:failed:{len(self.bodies)}",
            provider_ref=f"message:social-initiative:failed:{len(self.bodies)}",
            status="failed",
            error_class="provider_rejected",
            received_at=NOW + timedelta(minutes=2),
            raw_payload_hash="sha256:" + "c" * 64,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )


class _QQDelivery:
    def __init__(self, *, failed: bool = False) -> None:
        self.failed = failed
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        if self.failed:
            return {"status": "failed", "retcode": 100, "message": "rejected"}
        return {"status": "ok", "data": {"message_id": f"qq-{len(self.sent)}"}}


class _DraftModel:
    model = "test-proactive-flash"

    def __init__(self, choice: str) -> None:
        self.choice = choice
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        self.messages.append(messages)
        value: dict[str, object] = {
            "timing_choice": self.choice,
            "cadence": "conversational",
            "stance": "low_pressure",
            "brief_rationale": "根据当前关系与未完事项自由决定",
            "impulse_summary": "此刻有一点想把这件事接回来的冲动。",
            "confidence": 7_200,
        }
        if self.choice != "silent":
            value["beats"] = [{"modality": "text", "text": "刚才那件事我又想了一下。"}]
        if self.choice == "later":
            value.update(delay_seconds=60, expires_after_seconds=600)
        return json.dumps(value, ensure_ascii=False)

    def captured_capsule(self) -> dict[str, object]:
        assert len(self.messages) == 1
        envelope = json.loads(self.messages[0][1]["content"])
        return envelope["lived_context"]


class _SequenceDraftModel(_DraftModel):
    def __init__(self, choices: tuple[str, ...]) -> None:
        super().__init__(choices[0])
        self._choices = iter(choices)

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        self.choice = next(self._choices)
        return await super().complete(messages, temperature=temperature)


class _SlowPrimaryThenSilentDraftModel:
    model = "test-slow-primary-then-silent-proactive"

    def __init__(self, *, primary_delay_seconds: float) -> None:
        self.primary_delay_seconds = primary_delay_seconds
        self.calls = 0
        self.primary_cancelled = False
        self.second_call_started_after_primary_cancel = False

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        if self.calls == 1:
            try:
                await asyncio.sleep(self.primary_delay_seconds)
            except asyncio.CancelledError:
                self.primary_cancelled = True
                raise
            choice = "now"
        else:
            self.second_call_started_after_primary_cancel = self.primary_cancelled
            choice = "silent"
        value: dict[str, object] = {
            "timing_choice": choice,
            "cadence": "conversational",
            "stance": "self_directed",
            "brief_rationale": "按此刻情境决定是否联系。",
            "impulse_summary": "此刻自然地想到了对方。",
            "confidence": 7_000,
        }
        if choice == "now":
            value["beats"] = [{"modality": "text", "text": "刚刚想起你了。"}]
        return json.dumps(
            value,
            ensure_ascii=False,
        )


class _DelayedSupportingSourceReviewer(_ProactiveReplySequence):
    def __init__(self, *, delay_seconds: float) -> None:
        super().__init__([_source_closure_review()])
        self.delay_seconds = delay_seconds

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        await asyncio.sleep(self.delay_seconds)
        return await super().complete(messages, temperature=temperature)


class _JsonPreferredProactiveModel:
    model = "test-json-preferred-proactive"

    def __init__(self) -> None:
        self.general_calls = 0
        self.json_calls = 0

    async def complete_with_usage(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.general_calls += 1
        return "{}", _usage(provider=self.model, ordinal=self.general_calls)

    async def complete_json_with_usage(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.json_calls += 1
        return (
            json.dumps(
                {
                    "timing_choice": "silent",
                    "cadence": "conversational",
                    "stance": "quietly_content",
                    "brief_rationale": "此刻没有想说的话。",
                    "impulse_summary": "念头停在心里，没有形成表达冲动。",
                    "confidence": 7_000,
                },
                ensure_ascii=False,
            ),
            _usage(provider=self.model, ordinal=self.json_calls),
        )


class _MalformedProactiveModel:
    model = "test-malformed-proactive"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return "{}"


class _RetainedPreferenceFactModel:
    model = "test-retained-preference-fact"

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.1
        return json.dumps(
            {
                "retain": True,
                "predicate_code": "preference.likes",
                "value": "乌龙茶",
                "privacy_class": "personal",
                "confidence": 8_600,
                "rationale": "The user stated an enduring preference.",
            },
            ensure_ascii=False,
        )


class _JsonOnlyProactiveModel(_DraftModel):
    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        raise AssertionError("structured proactive lane must use provider JSON mode")

    async def complete_json(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        return await super().complete(messages, temperature=temperature)


class _LooseProactiveModel:
    model = "test-loose-proactive"

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        self.messages.append(messages)
        return json.dumps(self.payload, ensure_ascii=False)

    def captured_capsule(self) -> dict[str, object]:
        envelope = json.loads(self.messages[0][1]["content"])
        return json.loads(envelope["request"]["model_content_json"])


class _ResponseExpectingChat:
    model = "test-response-expecting-chat"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你忙完跟我说一声呀。"}],
                "stance": "answer_without_world_claims",
                "brief_rationale": "自然地邀请对方回来继续聊",
                "confidence": 8_000,
                "response_expectation": {
                    "hoped_response": "对方忙完后回来继续聊天",
                    "pressure_bp": 2_000,
                    "importance_bp": 6_000,
                    "wait_seconds": 60,
                    "expires_after_seconds": 600,
                },
            },
            ensure_ascii=False,
        )


class _NoExpectationChat:
    model = "test-no-response-expectation-chat"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "好，你先忙。"}],
                "stance": "acknowledge_briefly",
                "brief_rationale": "无需对方回应",
                "confidence": 8_000,
            },
            ensure_ascii=False,
        )


class _ExpectationAssessmentChat:
    model = "test-expectation-assessment-chat"

    def __init__(self) -> None:
        self.replies = [
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "深圳怎么样，好玩吗？"}],
                "stance": "curious",
                "brief_rationale": "Ask about the trip.",
                "world_claims": [],
                "response_expectation": {
                    "hoped_response": "对方说说深圳好不好玩",
                    "pressure_bp": 1_000,
                    "importance_bp": 4_000,
                    "wait_seconds": 60,
                    "expires_after_seconds": 3_600,
                },
            },
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "哈哈，听起来确实没戳中你。"}],
                "stance": "receive_the_answer",
                "brief_rationale": "The counterpart answered directly.",
                "world_claims": [],
                "response_expectation_assessment": {
                    "status": "fulfilled",
                    "reason": "The counterpart directly said Shenzhen was not enjoyable.",
                },
            },
        ]

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(self.replies.pop(0), ensure_ascii=False)


@pytest.mark.asyncio
async def test_inbound_cognition_durably_fulfills_the_exact_prior_expectation(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    model = _ExpectationAssessmentChat()
    chat = ChatModelDeliberationAdapter(model=model)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "expectation-assessment.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:expectation-assessment",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=_DeliveredTransport(),
        proactive_model=_DraftModel("silent"),
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:shenzhen:1",
            text="从深圳回来啦",
            observed_at=NOW,
            trace_id="trace:shenzhen:1",
        )
        assert (await app.drain_actions_once()).status == "settled"
        original_commit = WorldRuntime._commit  # noqa: SLF001
        conflicts = 0

        async def conflict_twice(runtime, events, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal conflicts
            if events and events[0].event_type == "ResponseExpectationAssessed" and conflicts < 2:
                conflicts += 1
                raise ConcurrencyConflict("simulated assessment CAS race")
            return await original_commit(runtime, events, **kwargs)

        monkeypatch.setattr(WorldRuntime, "_commit", conflict_twice)
        second = await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:shenzhen:2",
            text="深圳说实话不是很好玩哈哈哈哈",
            observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:shenzhen:2",
        )
        assert second.status == "action_authorized"
        assert not app._ledger.project().response_expectation_assessments  # noqa: SLF001
        monkeypatch.setattr(WorldRuntime, "_commit", original_commit)
        await app.drain_background_once()

        assessments = app._ledger.project().response_expectation_assessments  # noqa: SLF001
        assert len(assessments) == 1
        assert assessments[0].status == "fulfilled"
        assert assessments[0].inbound_observation_id.endswith("message:shenzhen:2")
    finally:
        app.close()


class _DeliveredExecutor:
    def __init__(self) -> None:
        self.dispatch_calls = 0

    async def dispatch(self, action: Action) -> ProviderReceipt:
        self.dispatch_calls += 1
        return ProviderReceipt(
            provider_receipt_id="provider-event:proactive:1",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider="provider:test",
            provider_ref="provider-ref:proactive:1",
            status="delivered",
            cost_actual=1,
            received_at=action.logical_time,
            raw_payload_hash="sha256:proactive-delivered",
        )

    async def lookup_result(self, _action: Action) -> ProviderReceipt | None:
        return None


def _make_proactive_runtime(
    *,
    ledger,
    issuer,
    model,
    owner="worker:proactive",
    identity_frame=None,
    social_initiative=None,
    expression_capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES,
):  # type: ignore[no-untyped-def]
    adapter = ProactiveDraftAdapter(
        model=model,
        target="user:primary",
        identity_frame=identity_frame,
        expression_capabilities=expression_capabilities,
    )
    turn = ProactiveDeliberationTurn(
        ledger=ledger,
        capsule_compiler=context_capsule_compiler_from_ledger(
            ledger=ledger,
            relevance_scope=ContextRelevanceScope(
                actor_ref="actor:companion", related_subject_refs=("user:primary",)
            ),
        ),
        deliberation=compose_production_deliberation(
            lane_id="proactive", router=_Router(), main_model=adapter, quick_recovery=adapter
        ),
        companion_actor_ref="actor:companion",
    )
    runtime = ProactiveActionRuntime(
        ledger=ledger,
        turn=turn,
        batch_issuer=issuer,
        policy=ExpressionPlanBudgetPolicy(
            account_id="account:proactive",
            amount_limit_per_action=10,
            actor="actor:companion",
            allowed_targets=("user:primary",),
            recovery_policy="effect_once",
            category="proactive",
        ),
        owner_id=owner,
        social_initiative=social_initiative,
    )
    return runtime, turn


def _runtime(
    *,
    choice: str,
    budget: int = 100,
    consideration_horizon: timedelta = timedelta(hours=8),
):
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    _commit(ledger, _event("event:world:start", "WorldStarted", {}))
    if ledger.project().logical_time != NOW:
        _commit(
            ledger,
            _event(
                "event:clock",
                "ClockAdvanced",
                {
                    "logical_time_from": (
                        ledger.project().logical_time or NOW - timedelta(minutes=2)
                    ).isoformat(),
                    "logical_time_to": NOW.isoformat(),
                },
            ),
        )
    account = BudgetAccount(
        account_id="account:proactive", category="proactive", window_id="day:1", limit=budget
    )
    _commit(
        ledger,
        _event(
            "event:budget:proactive",
            "BudgetAccountConfigured",
            {"account": account.model_dump(mode="json")},
        ),
    )
    _seed_due_thread(ledger, consideration_horizon=consideration_horizon)
    model = _DraftModel(choice)
    runtime, turn = _make_proactive_runtime(ledger=ledger, issuer=issuer, model=model)
    return ledger, model, runtime, turn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "status", "action_kind"),
    [
        ("now", "authorized", "proactive_message"),
        ("later", "authorized", "followup"),
        ("silent", "silent", None),
    ],
)
async def test_due_thread_is_a_model_opportunity_not_a_timer_message(
    choice: str, status: str, action_kind: str | None
) -> None:
    ledger, model, runtime, _turn = _runtime(choice=choice)
    assert (await runtime.drain_one()).status == "opened"
    result = await runtime.drain_one()
    assert result.status == status
    assert model.calls == 1
    projection = ledger.project()
    assert projection.trigger_processes[-1].state == "terminal"
    if action_kind is None:
        assert projection.actions == ()
    else:
        assert projection.actions[-1].kind == action_kind
        assert projection.actions[-1].budget_reservation_id is not None
        reservation = projection.budget_reservations[-1]
        assert reservation.category == "proactive"
        assert reservation.action_id == projection.actions[-1].action_id


@pytest.mark.asyncio
async def test_grounding_rejection_completes_consideration_without_retry_or_visible_effect() -> (
    None
):
    """A semantic rejection is terminal for this choice, not a technical outage."""

    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    unsupported_claim = {
        "claim_text": "对方之前说去成都看熊猫",
        "scope": "counterpart_history",
        "source_refs": ["event:user:chengdu:not-in-context"],
    }
    rejected = _proactive_draft(
        "突然想起你之前说去成都看熊猫。",
        claims=[unsupported_claim],
    )
    model = _ProactiveReplySequence([rejected, rejected])
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - acceptance seam fixture
        model=model,
        owner="worker:proactive:grounding-rejected",
    )

    assert (await runtime.drain_one()).status == "opened"
    rejected_result = await runtime.drain_one()

    assert rejected_result.status == "grounding_rejected"
    assert rejected_result.reason_code == "proactive.grounding_rejected"
    assert model.calls == 2
    projection = ledger.project()
    assert projection.actions == ()
    process = projection.trigger_processes[-1]
    assert process.state == "terminal"
    assert process.runtime_outcome_ref == "proactive:grounding-rejected"
    assert (await runtime.drain_one()).status == "idle"
    assert model.calls == 2


@pytest.mark.asyncio
async def test_visible_proactive_expression_is_bound_to_its_semantic_opportunity() -> None:
    ledger, model, runtime, _turn = _runtime(choice="now")

    assert (await runtime.drain_one()).status == "opened"
    result = await runtime.drain_one()

    assert result.status == "authorized"
    audit = ledger.project().proposal_audits[-1]
    proposal = json.loads(audit.proposal_json)
    payload = json.loads(proposal["proposed_changes"][0]["payload"]["canonical_json"])
    binding = payload["proactive_source_binding"]
    source = ledger.lookup_event_commit(proposal["trigger_ref"])
    assert source is not None
    assert binding == {
        "response_payload_hash": proposal["action_intents"][0]["payload_hash"],
        "source_event_ref": proposal["trigger_ref"],
        "source_kind": "thread",
        "source_payload_hash": "sha256:" + source[0].payload_hash,
        "source_world_revision": source[1].world_revision,
        "target_ref": "user:primary",
    }
    system = model.messages[0][0]["content"]
    assert "Exact ExpressionDraft JSON field contract" in system
    assert 'A text beat uses exactly modality="text" and text=<non-empty string>' in system
    assert "impulse_summary is required" in system
    for behavioral_instruction in (
        "curiosity, sharing, missing someone, asking for help or comfort",
        "semantic anchor",
        "light relationship-appropriate check-in",
        "Do not select from a motive menu",
        "choose silent",
        "A silent brief_rationale must explain",
        "smallest valid choice",
    ):
        assert behavioral_instruction not in system
    user = json.loads(model.messages[0][1]["content"])
    assert user["proactive_opportunity"]["source_kind"] == "thread"
    assert "verified proactive opportunity" in user["proactive_opportunity"]["guidance"].lower()
    assert user["proactive_opportunity"]["source_refs"] == [proposal["trigger_ref"]]


@pytest.mark.asyncio
async def test_proactive_recovery_keeps_the_same_character_choice_prompt() -> None:
    model = _SequenceDraftModel(("silent", "silent"))
    adapter = ProactiveDraftAdapter(model=model, target="user:primary")
    request = _proactive_model_request()

    await adapter.propose(request)
    await adapter.recover(request, "primary_timeout")

    assert model.messages[0][0]["content"] == model.messages[1][0]["content"]
    assert "smallest valid choice" not in model.messages[1][0]["content"]


@pytest.mark.asyncio
async def test_proactive_voice_receives_the_same_companion_identity_boundary() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    model = _DraftModel("silent")
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - acceptance seam fixture
        model=model,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="Geoff",
            relationship_frame="刚认识",
            personality_frame="慢热，有自己的判断。",
        ),
    )

    assert (await runtime.drain_one()).status == "opened"
    assert (await runtime.drain_one()).status == "silent"

    system = model.messages[0][0]["content"]
    assert "沈知栀" in system
    assert "Geoff" in system
    assert "慢热" in system
    assert "not an assistant" in system


@pytest.mark.asyncio
async def test_proactive_draft_uses_provider_json_mode_when_available() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    model = _JsonOnlyProactiveModel("silent")
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - provider seam fixture
        model=model,
    )

    assert (await runtime.drain_one()).status == "opened"
    assert (await runtime.drain_one()).status == "silent"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_proactive_output_requires_the_shared_expression_draft_shape() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    model = _LooseProactiveModel({"choice": "now", "text": "刚才那件事，我还记着。"})
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - provider salvage seam
        model=model,
    )

    assert (await runtime.drain_one()).status == "opened"
    result = await runtime.drain_one()

    assert result.status == "failed_safe"


@pytest.mark.asyncio
async def test_proactive_silence_requires_the_shared_expression_draft_shape() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    model = _LooseProactiveModel(
        {"choice": "silent", "confidence": "certain", "brief_rationale": []}
    )
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - provider salvage seam
        model=model,
    )

    assert (await runtime.drain_one()).status == "opened"
    assert (await runtime.drain_one()).status == "failed_safe"


@pytest.mark.asyncio
async def test_silent_proactive_proposal_records_that_the_opportunity_was_considered() -> None:
    ledger, _model, runtime, _turn = _runtime(choice="silent")

    assert (await runtime.drain_one()).status == "opened"
    assert (await runtime.drain_one()).status == "silent"

    proposal = json.loads(ledger.project().proposal_audits[-1].proposal_json)
    basis = proposal["proactive_opportunity_decision"]
    source = ledger.lookup_event_commit(proposal["trigger_ref"])
    assert source is not None
    assert basis == {
        "decision_origin": "model",
        "disposition": "silent_after_consideration",
        "source_event_ref": proposal["trigger_ref"],
        "source_kind": "thread",
        "source_payload_hash": "sha256:" + source[0].payload_hash,
        "source_world_revision": source[1].world_revision,
    }


@pytest.mark.asyncio
async def test_proactive_material_uses_projection_time_not_old_opportunity_time() -> None:
    ledger, _model, runtime, _turn = _runtime(choice="now")
    source_projection_time = ledger.project().logical_time
    assert source_projection_time is not None
    projection_time = source_projection_time + timedelta(minutes=5)
    _commit(
        ledger,
        _event(
            "event:clock:before-proactive",
            "ClockAdvanced",
            {
                "logical_time_from": source_projection_time.isoformat(),
                "logical_time_to": projection_time.isoformat(),
            },
            at=projection_time,
        ),
    )

    assert (await runtime.drain_one()).status == "opened"
    assert (await runtime.drain_one()).status == "authorized"

    proactive_events = tuple(
        stored.event
        for stored in ledger._events  # noqa: SLF001 - verify emitted envelopes at the seam
        if stored.event.source
        in {
            "world-runtime:proactive-turn",
            "world-v2:proactive-action-runtime",
        }
    )
    assert proactive_events
    assert {event.created_at for event in proactive_events} == {projection_time}
    projection = ledger.project()
    assert projection.actions[-1].created_at == projection_time


@pytest.mark.asyncio
async def test_exhausted_proactive_budget_abandons_with_a_durable_terminal_outcome() -> None:
    ledger, model, runtime, _turn = _runtime(choice="now", budget=0)
    assert (await runtime.drain_one()).status == "opened"
    result = await runtime.drain_one()
    assert result.status == "budget_exhausted"
    assert model.calls == 1
    projection = ledger.project()
    assert projection.actions == ()
    terminal = projection.trigger_processes[-1]
    assert terminal.state == "terminal"
    assert terminal.runtime_outcome_ref == "proactive:budget-exhausted:abandoned"


@pytest.mark.asyncio
async def test_two_unparseable_choices_are_technical_failure_not_character_silence() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    malformed = _MalformedProactiveModel()
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - acceptance seam fixture
        model=malformed,
        owner="worker:proactive:malformed",
    )

    assert (await runtime.drain_one()).status == "opened"
    result = await runtime.drain_one()

    assert result.status == "failed_safe"
    assert malformed.calls == 2
    projection = ledger.project()
    # The invalid author wire and its one same-role correction remain one
    # validation attempt, but each physical provider invocation now has its
    # own child audit beside the terminal orchestration result. They are not a
    # fabricated main/backup character decision pair.
    assert len(projection.model_result_audits) == 3
    assert len(projection.proposal_audits) == 0
    assert projection.actions == ()
    process = projection.trigger_processes[-1]
    assert process.state == "terminal"
    assert process.runtime_outcome_ref.startswith("proactive:deliberation-failed:")
    waiting = await runtime.drain_one()
    assert waiting.status == "retry_wait"
    assert waiting.retry_ordinal == 1
    assert waiting.next_retry_at == projection.logical_time + timedelta(minutes=10)
    assert malformed.calls == 2


@pytest.mark.asyncio
async def test_repeated_unpinned_private_state_commits_no_effect_and_waits_for_retry() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    model = _ProactiveReplySequence(
        [
            _private_proactive_draft(
                "now",
                attended_source_ref="memory:untrusted:first",
            ),
            _private_proactive_draft(
                "now",
                attended_source_ref="memory:untrusted:second",
            ),
        ]
    )
    capabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - acceptance seam fixture
        model=model,
        owner="worker:proactive:private-state-failure",
        expression_capabilities=capabilities,
    )

    assert (await runtime.drain_one()).status == "opened"
    result = await runtime.drain_one()

    assert result.status == "failed_safe"
    assert model.calls == 2
    projection = ledger.project()
    assert projection.proposal_audits == ()
    assert projection.actions == ()
    waiting = await runtime.drain_one()
    assert waiting.status == "retry_wait"
    assert waiting.retry_ordinal == 1
    assert waiting.next_retry_at == projection.logical_time + timedelta(minutes=10)
    assert model.calls == 2


@pytest.mark.asyncio
async def test_technical_backoff_does_not_starve_a_later_due_opportunity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _model, runtime, _turn = _runtime(choice="silent")
    projection = ledger.project()
    first = await runtime._next_opportunity(projection)  # noqa: SLF001
    assert first is not None
    second = first.model_copy(
        update={
            "source_id": "second-due-source",
            "consideration_id": "consideration:proactive:second-due-source",
        }
    )

    async def next_opportunity(  # type: ignore[no-untyped-def]
        _projection, *, excluded_consideration_ids=frozenset()
    ):
        if runtime._consideration_id(first) not in excluded_consideration_ids:  # noqa: SLF001
            return first
        if runtime._consideration_id(second) not in excluded_consideration_ids:  # noqa: SLF001
            return second
        return None

    def retry_state(*, opportunity, **_kwargs):  # type: ignore[no-untyped-def]
        if opportunity is first:
            return 1, projection.logical_time + timedelta(minutes=10)
        return 0, None

    opened = []

    async def open_process(**kwargs):  # type: ignore[no-untyped-def]
        opened.append(kwargs["opportunity"])

    monkeypatch.setattr(runtime, "_next_opportunity", next_opportunity)
    monkeypatch.setattr(runtime, "_retry_state", retry_state)
    monkeypatch.setattr(runtime, "_open", open_process)

    result = await runtime.drain_one()

    assert result.status == "opened"
    assert opened == [second]


@pytest.mark.asyncio
async def test_terminal_social_candidate_without_retry_authority_is_skipped() -> None:
    ledger, _model, runtime, _turn = _runtime(choice="silent")
    stale = await runtime._next_opportunity(ledger.project())  # noqa: SLF001
    assert stale is not None
    assert (await runtime.drain_one()).status == "opened"
    assert (await runtime.drain_one()).status == "silent"
    alternate = stale.model_copy(
        update={
            "source_id": "ambient-after-recovered-failure",
            "consideration_id": "consideration:ambient-after-recovered-failure",
        }
    )

    class _StaleRetryThenAmbient:
        async def next_opportunity(  # type: ignore[no-untyped-def]
            self, _projection, *, excluded_consideration_ids=frozenset()
        ):
            if runtime._consideration_id(stale) not in excluded_consideration_ids:  # noqa: SLF001
                return stale
            return alternate

    runtime._social_initiative = _StaleRetryThenAmbient()  # type: ignore[assignment]  # noqa: SLF001

    selected = await runtime._next_opportunity(ledger.project())  # noqa: SLF001

    assert selected == alternate


@pytest.mark.asyncio
async def test_settled_occurrence_cannot_bypass_situation_change_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, _model, runtime, _turn = _runtime(choice="silent")
    settlement = _event(
        "event:settlement:scheduled-only",
        "WorldOccurrenceSettled",
        {},
        at=NOW,
    )
    source_ref = SimpleNamespace(
        event_id=settlement.event_id,
        event_type=settlement.event_type,
        payload_hash=settlement.payload_hash,
        world_revision=7,
        logical_time=settlement.logical_time,
    )
    projection = SimpleNamespace(
        logical_time=NOW + timedelta(seconds=30),
        world_occurrences=(
            SimpleNamespace(
                status="settled",
                settlement_event_ref=settlement.event_id,
                visibility="shareable",
                settled_at=NOW,
                occurrence_id="occurrence:scheduled-only",
            ),
        ),
        threads=(),
        commitments=(),
        trigger_processes=(),
        message_observations=(),
        committed_world_event_refs=(source_ref,),
        model_result_audits=(),
    )

    class _NotDueSituationInitiative:
        async def next_opportunity(self, _projection):  # type: ignore[no-untyped-def]
            return None

    async def lookup(event_id: str):  # type: ignore[no-untyped-def]
        assert event_id == settlement.event_id
        return settlement, SimpleNamespace(world_revision=source_ref.world_revision)

    runtime._social_initiative = _NotDueSituationInitiative()  # noqa: SLF001
    monkeypatch.setattr(runtime, "_lookup", lookup)

    assert await runtime._next_opportunity(projection) is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_restart_closes_an_audited_technical_failure_then_waits_for_retry() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    malformed = _MalformedProactiveModel()
    runtime, turn = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - crash-window fixture
        model=malformed,
        owner="worker:proactive:restart-failure",
    )
    assert (await runtime.drain_one()).status == "opened"
    projection = ledger.project()
    opportunity = await runtime._next_opportunity(projection)  # noqa: SLF001
    assert opportunity is not None
    consideration_id = runtime._consideration_id(opportunity)  # noqa: SLF001
    commit = await turn.audit(
        opportunity=opportunity,
        cursor=ProactiveActionRuntime._cursor(projection),
        attempt_id=runtime._model_attempt_id(  # noqa: SLF001
            consideration_id=consideration_id,
            retry_ordinal=0,
        ),
    )
    assert commit.proposal_id is None
    assert malformed.calls == 2

    result = await runtime.drain_one()

    assert result.status == "failed_safe"
    assert malformed.calls == 2
    assert ledger.project().trigger_processes[-1].state == "terminal"
    assert (await runtime.drain_one()).status == "retry_wait"


@pytest.mark.asyncio
async def test_authorized_proactive_action_reaches_a_durable_delivery_receipt() -> None:
    ledger, model, proactive, _turn = _runtime(choice="now")
    await proactive.drain_one()
    accepted = await proactive.drain_one()
    assert accepted.status == "authorized"
    executor = _DeliveredExecutor()
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        action_executor=executor,
        action_pump_owner="worker:proactive-action-pump",
    )

    result = await runtime.drain_actions_once()

    assert result is not None and result.status == "settled"
    assert executor.dispatch_calls == 1
    assert model.calls == 1
    projection = ledger.project()
    action = next(item for item in projection.actions if item.action_id == accepted.action_id)
    assert action.state == "delivered"
    receipt = next(
        item for item in projection.execution_receipts if item.action_id == action.action_id
    )
    assert receipt.observed_state == "delivered"
    reservation = next(
        item
        for item in projection.budget_reservations
        if item.reservation_id == action.budget_reservation_id
    )
    assert reservation.state == "settled"


@pytest.mark.asyncio
async def test_restart_reuses_the_terminal_decision_without_a_second_model_call() -> None:
    ledger, model, runtime, _turn = _runtime(choice="silent")
    await runtime.drain_one()
    await runtime.drain_one()
    assert (await runtime.drain_one()).status == "idle"
    assert model.calls == 1
    assert (
        len(
            [
                item
                for item in ledger.project().trigger_processes
                if item.process_kind == "proactive_action_deliberation"
            ]
        )
        == 1
    )


@pytest.mark.asyncio
async def test_sqlite_restart_resumes_open_proactive_process_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "proactive-restart.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=issuer)
    _commit(ledger, _event("event:world:start", "WorldStarted", {}))
    _commit(
        ledger,
        _event(
            "event:budget:proactive",
            "BudgetAccountConfigured",
            {
                "account": BudgetAccount(
                    account_id="account:proactive",
                    category="proactive",
                    window_id="day:1",
                    limit=100,
                ).model_dump(mode="json")
            },
        ),
    )
    _seed_due_thread(ledger)
    unopened_model = _DraftModel("now")
    first, _ = _make_proactive_runtime(
        ledger=ledger, issuer=issuer, model=unopened_model, owner="worker:before-restart"
    )
    assert (await first.drain_one()).status == "opened"
    assert unopened_model.calls == 0
    ledger.close()

    reopened_issuer = AcceptedLedgerBatchIssuer()
    reopened = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=reopened_issuer)
    resumed_model = _DraftModel("now")
    resumed, _ = _make_proactive_runtime(
        ledger=reopened,
        issuer=reopened_issuer,
        model=resumed_model,
        owner="worker:after-restart",
    )
    assert (await resumed.drain_one()).status == "authorized"
    assert resumed_model.calls == 1
    assert len(reopened.project().actions) == 1
    reopened.close()

    terminal_issuer = AcceptedLedgerBatchIssuer()
    terminal = SQLiteWorldLedger(path=path, world_id=WORLD, accepted_batch_issuer=terminal_issuer)
    unused_model = _DraftModel("now")
    duplicate, _ = _make_proactive_runtime(
        ledger=terminal,
        issuer=terminal_issuer,
        model=unused_model,
        owner="worker:terminal-restart",
    )
    assert (await duplicate.drain_one()).status == "idle"
    assert unused_model.calls == 0
    assert len(terminal.project().actions) == 1
    terminal.close()


@pytest.mark.asyncio
async def test_concurrent_proactive_workers_authorize_one_chain_and_one_model_call() -> None:
    ledger, model, first, turn = _runtime(choice="now")
    policy = ExpressionPlanBudgetPolicy(
        account_id="account:proactive",
        amount_limit_per_action=10,
        actor="actor:companion",
        allowed_targets=("user:primary",),
        recovery_policy="effect_once",
        category="proactive",
    )
    second = ProactiveActionRuntime(
        ledger=ledger,
        turn=turn,
        batch_issuer=ledger._accepted_batch_issuer,
        policy=policy,
        owner_id="worker:proactive:second",
    )
    assert (await first.drain_one()).status == "opened"
    results = await asyncio.gather(first.drain_one(), second.drain_one())
    assert {item.status for item in results} <= {
        "authorized",
        "owned_elsewhere",
        "stale",
        "completed_existing",
    }
    assert sum(item.status == "authorized" for item in results) == 1
    assert model.calls == 1
    assert len(ledger.project().actions) == 1


@pytest.mark.asyncio
async def test_proactive_source_hash_cannot_be_rebound_to_a_committed_event() -> None:
    ledger, model, _runtime_value, turn = _runtime(choice="silent")
    projection = ledger.project()
    source_ref = projection.thread_transitions[-1].accepted_event_ref
    located = ledger.lookup_event_commit(source_ref)
    assert located is not None
    forged = ProactiveOpportunity(
        source_kind="thread",
        source_id=projection.threads[-1].thread_id,
        source_event_ref=source_ref,
        source_event_hash="f" * 64,
        source_world_revision=located[1].world_revision,
        trace_id=located[0].trace_id,
        correlation_id=located[0].correlation_id,
        created_at=located[0].created_at,
    )
    with pytest.raises(ValueError, match="exact committed authority"):
        await turn.audit(
            opportunity=forged,
            cursor=ProactiveActionRuntime._cursor(projection),
        )
    assert model.calls == 0


@pytest.mark.asyncio
async def test_sqlite_production_composition_installs_proactive_budget_without_an_extra_ordinary_turn_call(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "proactive-production.sqlite3"
    proactive = _DraftModel("silent")
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=WorldV2TurnApplicationConfig(
            world_id="world:proactive-composed",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=_InvalidMain(),
        quick_recovery=_InvalidQuick(),
        transport=_NoDispatchTransport(),
        proactive_model=proactive,
        now=NOW,
    )
    try:
        outcome = await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:1",
            text="今天有点累",
            observed_at=NOW,
            trace_id="trace:ordinary",
        )
        assert not outcome.authorized_action_ids
        assert proactive.calls == 0
        await app.drain_background_once()
        assert proactive.calls == 0
    finally:
        app.close()
    ledger = SQLiteWorldLedger(path=path, world_id="world:proactive-composed")
    try:
        account = next(
            item
            for item in ledger.project().budget_accounts
            if item.account_id == "account:world-v2:proactive"
        )
        assert account.category == "proactive"
        assert account.limit == 1_000
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_production_proactive_lane_uses_its_configured_recovery_window(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _SlowPrimaryThenSilentDraftModel(primary_delay_seconds=0.05)
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "proactive-timeout-policy.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:proactive-timeout-policy",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
                total_seconds=0.03,
                hedge_after_seconds=0.005,
                acceptance_dispatch_reserve_seconds=0.005,
                first_provider_entry_seconds=0.001,
                technical_recovery_seconds=0.2,
                validation_recovery_seconds=0.2,
                validation_reselection_seconds=0.2,
            ),
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=60,
                spontaneous_expiry_seconds=3_600,
                consideration_band_override_seconds=(60, 60),
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=_NoDispatchTransport(),
        proactive_model=proactive,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:proactive-timeout-policy",
            text="我先去忙一会儿",
            observed_at=NOW,
            trace_id="trace:proactive-timeout-policy",
        )
        await app.tick(
            tick_id="tick:proactive-timeout-policy",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61),
            trace_id="trace:proactive-timeout-policy",
            causation_id="scheduler:test",
            correlation_id="conversation:proactive-timeout-policy",
            reason="test_idle",
        )

        assert (await app.drain_background_once()).status == "opened"
        result = await app.drain_background_once()

        assert result.status == "silent"
        assert proactive.calls == 2
        assert proactive.primary_cancelled is True
        assert proactive.second_call_started_after_primary_cancel is True
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_proactive_source_review_uses_its_validation_window_without_reauthoring(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _DraftModel("now")
    reviewer = _DelayedSupportingSourceReviewer(delay_seconds=0.05)
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "proactive-validation-window.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:proactive-validation-window",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            interactive_turn_budget_policy=InteractiveTurnBudgetPolicy(
                total_seconds=0.03,
                hedge_after_seconds=0.005,
                acceptance_dispatch_reserve_seconds=0.005,
                first_provider_entry_seconds=0.001,
                technical_recovery_seconds=0.2,
                validation_recovery_seconds=0.2,
                validation_reselection_seconds=0.2,
            ),
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=60,
                spontaneous_expiry_seconds=3_600,
                consideration_band_override_seconds=(60, 60),
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=_NoDispatchTransport(),
        proactive_model=proactive,
        proactive_source_closure_model=reviewer,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:proactive-validation-window",
            text="我先去忙一会儿",
            observed_at=NOW,
            trace_id="trace:proactive-validation-window",
        )
        due = NOW + timedelta(seconds=61)
        await app.tick(
            tick_id="tick:proactive-validation-window",
            logical_time_from=NOW,
            logical_time_to=due,
            observed_at=due,
            trace_id="trace:proactive-validation-window",
            causation_id="scheduler:test",
            correlation_id="conversation:proactive-validation-window",
            reason="test_idle",
        )

        assert (await app.drain_background_once()).status == "opened"
        result = await app.drain_background_once()

        assert result.status == "authorized"
        assert proactive.calls == 1
        assert reviewer.calls == 1
    finally:
        app.close()


@pytest.mark.asyncio
async def test_proactive_author_prefers_provider_json_mode_when_usage_is_metered() -> None:
    model = _JsonPreferredProactiveModel()

    output = await ProactiveDraftAdapter(
        model=model,
        target="user:primary",
    ).propose(_proactive_model_request())

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert (
        proposal.proactive_opportunity_decision.disposition
        == "silent_after_consideration"
    )
    assert model.json_calls == 1
    assert model.general_calls == 0


@pytest.mark.asyncio
async def test_production_application_opens_one_grounded_spontaneous_contact_after_idle(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "social-initiative.sqlite3"
    proactive = _DraftModel("now")
    transport = _DeliveredTransport()
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=WorldV2TurnApplicationConfig(
            world_id="world:social-initiative",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=60,
                spontaneous_expiry_seconds=3_600,
                consideration_band_override_seconds=(60, 60),
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        proactive_model=proactive,
        now=NOW,
    )
    try:
        initial = await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:idle-source",
            text="我先去忙一会儿",
            observed_at=NOW,
            trace_id="trace:idle-source",
        )
        assert len(initial.authorized_action_ids) == 1
        assert (await app.drain_actions_once()).status == "settled"
        await app.tick(
            tick_id="tick:idle-contact",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=63),
            observed_at=NOW + timedelta(seconds=63),
            trace_id="trace:idle-contact",
            causation_id="scheduler:test",
            correlation_id="conversation:social-initiative",
            reason="test_idle",
        )
        assert (await app.drain_background_once()).status == "opened"
        draw_ref = next(
            item
            for item in app._ledger.project().committed_world_event_refs  # noqa: SLF001
            if item.event_type == "RandomDrawRecorded"
        )
        draw_event = app._ledger.lookup_event_commit(draw_ref.event_id)  # noqa: SLF001
        assert draw_event is not None
        draw_payload = json.loads(draw_event[0].payload_json)
        assert draw_payload["sampler_version"] == "random-authority.2"
        assert draw_payload["weight_policy_version"] == "social-initiative-context.2"
        assert draw_payload["candidate_refs"] == ["delay:60"]
        assert (await app.drain_background_once()).status == "authorized"
        assert (await app.drain_actions_once()).status == "settled"
        for _ in range(3):
            await app.drain_background_once()
        assert proactive.calls == 1
        capsule = proactive.captured_capsule()
        assert "我先去忙一会儿" in json.dumps(capsule, ensure_ascii=False)
        supplied = json.loads(proactive.messages[0][1]["content"])
        assert supplied["current_self_state"]["contract"] == "current-self-state.1"
        assert supplied["current_self_state"]["availability"] == "available"
        assert supplied["proactive_opportunity"]["source_kind"] == "spontaneous_contact"
        assert transport.bodies == ["好，你先忙。", "刚才那件事我又想了一下。"]
    finally:
        app.close()


@pytest.mark.asyncio
async def test_model_silence_is_reconsidered_in_the_next_cadence_epoch(tmp_path) -> None:
    proactive = _SequenceDraftModel(("silent", "now"))
    transport = _DeliveredTransport()
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "social-initiative-reconsider.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:social-initiative-reconsider",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=60,
                spontaneous_expiry_seconds=3_600,
                consideration_band_override_seconds=(60, 60),
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        proactive_model=proactive,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:reconsider-source",
            text="我先忙会儿",
            observed_at=NOW,
            trace_id="trace:reconsider-source",
        )
        await app.drain_actions_once()
        await app.tick(
            tick_id="tick:reconsider:first",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61),
            trace_id="trace:reconsider:first",
            causation_id="scheduler:test",
            correlation_id="conversation:reconsider",
            reason="test_idle",
        )
        assert (await app.drain_background_once()).status == "opened"
        assert (await app.drain_background_once()).status == "silent"
        assert proactive.calls == 1
        await app.tick(
            tick_id="tick:reconsider:second",
            logical_time_from=NOW + timedelta(seconds=61),
            logical_time_to=NOW + timedelta(seconds=121),
            observed_at=NOW + timedelta(seconds=121),
            trace_id="trace:reconsider:second",
            causation_id="scheduler:test",
            correlation_id="conversation:reconsider",
            reason="test_idle",
        )
        assert (await app.drain_background_once()).status == "opened"
        assert (await app.drain_background_once()).status == "authorized"
        assert proactive.calls == 2
        assert (
            len(
                {
                    item.trigger_id
                    for item in app._ledger.project().trigger_processes  # noqa: SLF001
                    if item.process_kind == ProactiveActionRuntime.PROCESS_KIND
                }
            )
            == 2
        )
    finally:
        app.close()


@pytest.mark.asyncio
async def test_ambient_presence_clock_can_open_model_owned_contact_after_source_expiry(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _DraftModel("now")
    transport = _DeliveredTransport()
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "social-initiative-ambient.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:social-initiative-ambient",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=60,
                spontaneous_expiry_seconds=120,
                consideration_band_override_seconds=(60, 60),
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        proactive_model=proactive,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:ambient-source",
            text="我先去忙",
            observed_at=NOW,
            trace_id="trace:ambient-source",
        )
        await app.drain_actions_once()
        ambient_at = NOW + timedelta(seconds=121)
        await app.tick(
            tick_id="tick:ambient",
            logical_time_from=NOW,
            logical_time_to=ambient_at,
            observed_at=ambient_at,
            trace_id="trace:ambient",
            causation_id="scheduler:test",
            correlation_id="conversation:ambient",
            reason="test_ambient",
        )
        assert (await app.drain_background_once()).status == "opened"
        assert (await app.drain_background_once()).status == "authorized"
        proposal = json.loads(
            app._ledger.project().proposal_audits[-1].proposal_json  # noqa: SLF001
        )
        assert proposal["proactive_opportunity_decision"]["source_kind"] == "ambient_presence"
        assert proactive.calls == 1
        prompt = json.dumps(proactive.messages[0], ensure_ascii=False)
        assert "choose silen" not in prompt
        assert "genuine contact" not in prompt
        assert "timing authority only" in prompt
    finally:
        app.close()


@pytest.mark.asyncio
async def test_technical_failures_retry_at_ten_thirty_then_capped_one_twenty_minutes() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    malformed = _MalformedProactiveModel()
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001
        model=malformed,
        owner="worker:proactive:retry-backoff",
    )

    assert (await runtime.drain_one()).status == "opened"
    assert (await runtime.drain_one()).status == "failed_safe"
    expected_delays = (600, 1_800, 7_200, 7_200)
    for retry_ordinal, expected_delay in enumerate(expected_delays, start=1):
        waiting = await runtime.drain_one()
        assert waiting.status == "retry_wait"
        assert waiting.retry_ordinal == retry_ordinal
        assert waiting.next_retry_at is not None
        current = ledger.project().logical_time
        assert current is not None
        assert (waiting.next_retry_at - current).total_seconds() == expected_delay
        _commit(
            ledger,
            _event(
                f"event:clock:retry:{retry_ordinal}",
                "ClockAdvanced",
                {
                    "logical_time_from": current.isoformat(),
                    "logical_time_to": waiting.next_retry_at.isoformat(),
                },
                at=waiting.next_retry_at,
            ),
        )
        opened = await runtime.drain_one()
        assert opened.status == "opened", (
            ledger.project().logical_time,
            waiting.next_retry_at,
            opened,
        )
        assert (await runtime.drain_one()).status == "failed_safe"

    assert malformed.calls == 10


def test_proactive_retry_timer_tracks_newest_unresolved_consideration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = ProactiveTechnicalRetryState(
        consideration_id="consideration:old",
        trigger_ref="proactive-consideration:consideration:old",
        source_evidence_ref="event:situation:old",
        retry_ordinal=1,
        consecutive_technical_failures=1,
        last_failed_at=NOW,
        next_retry_at=NOW + timedelta(minutes=10),
        last_failure_code="quick_timeout",
        last_failure_world_revision=10,
    )
    newest = ProactiveTechnicalRetryState(
        consideration_id="consideration:newest",
        trigger_ref="proactive-consideration:consideration:newest",
        source_evidence_ref="event:situation:newest",
        retry_ordinal=2,
        consecutive_technical_failures=2,
        last_failed_at=NOW + timedelta(minutes=5),
        next_retry_at=NOW + timedelta(minutes=35),
        last_failure_code="quick_timeout",
        last_failure_world_revision=20,
    )
    monkeypatch.setattr(
        proactive_action_module,
        "proactive_technical_retry_states",
        lambda _projection: (old, newest),
    )

    assert next_proactive_retry_due(SimpleNamespace()) == newest.next_retry_at

    monkeypatch.setattr(
        proactive_action_module,
        "proactive_technical_retry_states",
        lambda _projection: (
            old,
            newest.model_copy(update={"retry_process_state": "open"}),
        ),
    )
    assert next_proactive_retry_due(SimpleNamespace()) is None


@pytest.mark.asyncio
async def test_technical_failure_retry_survives_sixty_four_attempts_and_runtime_restart() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(
        choice="silent",
        consideration_horizon=timedelta(days=14),
    )
    malformed = _MalformedProactiveModel()
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001
        model=malformed,
        owner="worker:proactive:retry-before-restart",
    )

    assert (await runtime.drain_one()).status == "opened"
    assert (await runtime.drain_one()).status == "failed_safe"
    for retry_ordinal in range(1, 65):
        waiting = await runtime.drain_one()
        assert waiting.status == "retry_wait"
        assert waiting.retry_ordinal == retry_ordinal
        assert waiting.next_retry_at is not None
        current = ledger.project().logical_time
        assert current is not None
        _commit(
            ledger,
            _event(
                f"event:clock:unbounded-retry:{retry_ordinal}",
                "ClockAdvanced",
                {
                    "logical_time_from": current.isoformat(),
                    "logical_time_to": waiting.next_retry_at.isoformat(),
                },
                at=waiting.next_retry_at,
            ),
        )
        assert (await runtime.drain_one()).status == "opened"
        assert (await runtime.drain_one()).status == "failed_safe"

    restarted, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001
        model=malformed,
        owner="worker:proactive:retry-after-restart",
    )
    waiting = await restarted.drain_one()

    assert waiting.status == "retry_wait"
    assert waiting.retry_ordinal == 65
    assert waiting.next_retry_at == ledger.project().logical_time + timedelta(minutes=120)

    current = ledger.project().logical_time
    assert current is not None
    assert waiting.next_retry_at is not None
    _commit(
        ledger,
        _event(
            "event:clock:unbounded-retry:65",
            "ClockAdvanced",
            {
                "logical_time_from": current.isoformat(),
                "logical_time_to": waiting.next_retry_at.isoformat(),
            },
            at=waiting.next_retry_at,
        ),
    )
    assert (await restarted.drain_one()).status == "opened"
    assert (await restarted.drain_one()).status == "failed_safe"
    calls_after_retry = malformed.calls

    restarted_again, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001
        model=malformed,
        owner="worker:proactive:retry-second-restart",
    )
    next_wait = await restarted_again.drain_one()
    assert next_wait.status == "retry_wait"
    assert next_wait.retry_ordinal == 66
    assert malformed.calls == calls_after_retry


@pytest.mark.asyncio
async def test_invalid_proactive_source_after_claim_is_terminal_not_a_scheduler_crash() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    source = _event("event:world:invalid-proactive-source", "WorldStarted", {})
    _commit(ledger, source)
    source_ref = next(
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_id == source.event_id
    )
    opportunity = ProactiveOpportunity(
        source_kind="spontaneous_contact",
        source_id="not-an-observation",
        source_event_ref=source.event_id,
        source_event_hash=source.payload_hash,
        source_world_revision=source_ref.world_revision,
        trace_id=source.trace_id,
        correlation_id=source.correlation_id,
        created_at=source.created_at,
        consideration_id="consideration:invalid-proactive-source",
    )

    class FixedInitiative:
        async def next_opportunity(self, _projection):  # type: ignore[no-untyped-def]
            return opportunity

    runtime, _turn = _make_proactive_runtime(
        ledger=ledger,
        issuer=issuer,
        model=_DraftModel("silent"),
        social_initiative=FixedInitiative(),
    )

    assert (await runtime.drain_one()).status == "opened"
    result = await runtime.drain_one()

    assert result.status == "failed_safe"
    assert result.reason_code == "proactive.source_binding_invalid"
    process = ledger.project().trigger_processes[-1]
    assert process.state == "terminal"
    assert process.runtime_outcome_ref == "proactive:source-binding-invalid"


@pytest.mark.asyncio
async def test_new_user_observation_supersedes_an_old_technical_retry() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    malformed = _MalformedProactiveModel()
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001
        model=malformed,
        owner="worker:proactive:retry-superseded",
    )
    assert (await runtime.drain_one()).status == "opened"
    assert (await runtime.drain_one()).status == "failed_safe"
    current = ledger.project().logical_time
    assert current is not None
    assert next_proactive_retry_due(ledger.project()) == current + timedelta(minutes=10)
    observed_at = current + timedelta(minutes=1)
    observation = Observation(
        schema_version="world-v2.1",
        observation_id="new-user-context",
        world_id=WORLD,
        logical_time=observed_at,
        created_at=observed_at,
        trace_id="trace:proactive",
        causation_id="cause:proactive",
        correlation_id="conversation:proactive",
        source="test",
        source_event_id="message:new-user-context",
        actor="system:test",
        channel="test",
        payload_ref="payload:new-user-context",
        payload_hash="sha256:" + "9" * 64,
        text="我回来了，刚才又发生了一件事。",
        received_at=observed_at,
        reply_context={
            "target": "actor:companion",
            "platform_message_id": "new-user-context",
        },
    )
    _commit(
        ledger,
        _event(
            "event:clock:new-user-context",
            "ClockAdvanced",
            {
                "logical_time_from": current.isoformat(),
                "logical_time_to": observed_at.isoformat(),
            },
            at=observed_at,
        ),
        _event(
            "event:observation:new-user-context",
            "ObservationRecorded",
            observation.model_dump(mode="json"),
            at=observed_at,
        ),
    )

    result = await runtime.drain_one()

    assert result.status == "idle"
    assert malformed.calls == 2
    assert proactive_technical_retry_states(ledger.project()) == ()
    assert next_proactive_retry_due(ledger.project()) is None


@pytest.mark.asyncio
async def test_proactive_retry_wait_does_not_starve_ready_background_cognition(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    malformed = _MalformedProactiveModel()
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "proactive-retry-background-fairness.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:proactive-retry-background-fairness",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=60,
                spontaneous_expiry_seconds=3_600,
                consideration_band_override_seconds=(60, 60),
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=_DeliveredTransport(),
        proactive_model=malformed,
        fact_model=_RetainedPreferenceFactModel(),
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:background-fairness",
            text="我最近很喜欢喝乌龙茶。",
            observed_at=NOW,
            trace_id="trace:background-fairness",
        )
        await app.drain_actions_once()
        first_due = NOW + timedelta(seconds=61)
        await app.tick(
            tick_id="tick:background-fairness",
            logical_time_from=NOW,
            logical_time_to=first_due,
            observed_at=first_due,
            trace_id="trace:background-fairness",
            causation_id="scheduler:test",
            correlation_id="conversation:background-fairness",
            reason="test_idle",
        )

        assert (await app.drain_background_once()).status == "opened"
        assert (await app.drain_background_once()).status == "failed_safe"
        retry_due = first_due + timedelta(minutes=10)

        background = await app.drain_background_once()

        assert background is not None
        assert background.status == "processed"
        assert background.work_status == "accepted"
        assert malformed.calls == 2
        health = await app.world_health_diagnostics()
        assert health["initiative_state"] == "retry_wait"
        assert health["initiative_next_consideration_at"] == retry_due.isoformat()
        projection = app._ledger.project()  # noqa: SLF001 - public result corroboration
        assert any(
            fact.values.predicate_code == "preference.likes"
            for fact in projection.facts
        )
    finally:
        app.close()


@pytest.mark.asyncio
async def test_new_cadence_epoch_cannot_bypass_a_social_technical_backoff(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    malformed = _MalformedProactiveModel()
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "social-initiative-backoff-epoch.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:social-initiative-backoff-epoch",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=60,
                spontaneous_expiry_seconds=3_600,
                consideration_band_override_seconds=(60, 60),
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=_DeliveredTransport(),
        proactive_model=malformed,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:backoff-epoch",
            text="我先忙一下",
            observed_at=NOW,
            trace_id="trace:backoff-epoch",
        )
        await app.drain_actions_once()
        first_due = NOW + timedelta(seconds=61)
        await app.tick(
            tick_id="tick:backoff:first",
            logical_time_from=NOW,
            logical_time_to=first_due,
            observed_at=first_due,
            trace_id="trace:backoff:first",
            causation_id="scheduler:test",
            correlation_id="conversation:backoff",
            reason="test_idle",
        )
        assert (await app.drain_background_once()).status == "opened"
        assert (await app.drain_background_once()).status == "failed_safe"
        retry_due = first_due + timedelta(minutes=10)
        second_epoch = NOW + timedelta(seconds=121)
        await app.tick(
            tick_id="tick:backoff:second-epoch",
            logical_time_from=first_due,
            logical_time_to=second_epoch,
            observed_at=second_epoch,
            trace_id="trace:backoff:second",
            causation_id="scheduler:test",
            correlation_id="conversation:backoff",
            reason="test_idle",
        )
        health = await app.world_health_diagnostics()
        assert health["initiative_state"] == "retry_wait"
        assert health["initiative_next_consideration_at"] == retry_due.isoformat()
        assert health["initiative_cadence_reason_codes"] == ["technical_failure:retry"]
        assert health["initiative_consecutive_technical_failures"] == 1
        assert health["initiative_retry_ordinal"] == 1
        assert health["initiative_last_failure_code"] == ("authored_expression_reselection_invalid")
        waiting = await app.drain_background_once()
        # A future retry remains visible in health/timer projections, but is
        # not reported as work performed by this background pass.
        assert waiting is None
        assert malformed.calls == 2
        await app.tick(
            tick_id="tick:backoff:retry-due",
            logical_time_from=second_epoch,
            logical_time_to=retry_due,
            observed_at=retry_due,
            trace_id="trace:backoff:retry-due",
            causation_id="scheduler:test",
            correlation_id="conversation:backoff",
            reason="test_idle",
        )
        assert (await app.drain_background_once()).status == "opened"
        considering = await app.world_health_diagnostics()
        assert considering["initiative_state"] == "considering"
        assert considering["initiative_retry_ordinal"] == 1
        assert considering["initiative_next_consideration_at"] == retry_due.isoformat()
        assert (await app.drain_background_once()).status == "failed_safe"
        assert malformed.calls == 4
        second_retry = await app.world_health_diagnostics()
        assert second_retry["initiative_state"] == "retry_wait"
        assert second_retry["initiative_retry_ordinal"] == 2
        third_due = retry_due + timedelta(minutes=30)
        assert second_retry["initiative_next_consideration_at"] == third_due.isoformat()
        await app.tick(
            tick_id="tick:backoff:third-due",
            logical_time_from=retry_due,
            logical_time_to=third_due,
            observed_at=third_due,
            trace_id="trace:backoff:third-due",
            causation_id="scheduler:test",
            correlation_id="conversation:backoff",
            reason="test_idle",
        )
        assert (await app.drain_background_once()).status == "opened"
        assert (await app.drain_background_once()).status == "failed_safe"

        repeated_failure = await app.world_health_diagnostics()

        assert repeated_failure["initiative_consecutive_technical_failures"] == 3
        assert repeated_failure["initiative_warning"] is True
        assert repeated_failure["initiative_warning_reasons"] == [
            "technical_failures_24h",
            "repeated_technical_failures",
        ]
    finally:
        app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("semantic_draft", "expected_status", "expected_outcome", "expected_decision"),
    [
        (
            {
                "timing_choice": "silent",
                "cadence": "conversational",
                "stance": "newer_context_does_not_call_for_contact",
                "brief_rationale": "The newer consideration is complete.",
                "impulse_summary": "Nothing to add from the newer context.",
                "confidence": 7_000,
                "world_claims": [],
            },
            "silent",
            "proactive:silent",
            "silent",
        ),
        (
            _proactive_draft("忽然想跟你说句话。"),
            "authorized",
            "proactive:authorized:",
            "now",
        ),
    ],
)
async def test_newer_semantic_consideration_resets_older_technical_retry(
    tmp_path,
    semantic_draft: dict[str, object],
    expected_status: str,
    expected_outcome: str,
    expected_decision: str,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _ProactiveReplySequence(
        [
            "{}",
            "{}",
            semantic_draft,
        ]
    )
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "health-multiple-proactive-considerations.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id=WORLD,
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=_DeliveredTransport(),
        proactive_model=proactive,
        now=NOW,
    )
    try:
        _seed_due_thread(app._ledger)  # noqa: SLF001 - real persisted authority fixture
        runtime = app._turns._runtime._proactive_action_runtime  # noqa: SLF001
        assert runtime is not None
        assert (await runtime.drain_one()).status == "opened"
        assert (await runtime.drain_one()).status == "failed_safe"
        failed_at = app._ledger.project().logical_time  # noqa: SLF001
        assert failed_at is not None
        retry_due = failed_at + timedelta(minutes=10)

        _seed_due_thread(
            app._ledger,  # noqa: SLF001
            thread_key="2",
            advance_clock=False,
            event_at=failed_at,
        )
        assert (await runtime.drain_one()).status == "opened"
        assert (await runtime.drain_one()).status == expected_status
        projection = app._ledger.project()  # noqa: SLF001
        proactive_processes = tuple(
            item
            for item in projection.trigger_processes
            if item.process_kind == ProactiveActionRuntime.PROCESS_KIND
        )
        assert len({item.trigger_ref for item in proactive_processes}) == 2
        assert str(proactive_processes[-1].runtime_outcome_ref).startswith(expected_outcome)
        # Simulate reverse completion without changing process-open order:
        # the later-opened consideration completes first, then the
        # earlier-opened attempt records its technical failure.
        failed_trigger_id = proactive_processes[0].trigger_id
        successful_trigger_id = proactive_processes[-1].trigger_id
        reverse_completion = projection.model_copy(
            update={
                "completed_trigger_ids": tuple(
                    successful_trigger_id
                    if item == failed_trigger_id
                    else failed_trigger_id
                    if item == successful_trigger_id
                    else item
                    for item in projection.completed_trigger_ids
                )
            }
        )
        assert len(proactive_technical_retry_states(reverse_completion)) == 1
        assert next_proactive_retry_due(reverse_completion) == retry_due
        assert next_proactive_retry_due(projection) is None
        assert proactive_technical_retry_states(projection) == ()
        replayed = app._ledger.rebuild()  # noqa: SLF001 - verify immutable completion order
        assert proactive_technical_retry_states(replayed) == ()
        assert next_proactive_retry_due(replayed) is None

        health = await app.world_health_diagnostics()

        assert health["initiative_next_consideration_at"] != retry_due.isoformat()
        assert health["initiative_consecutive_technical_failures"] == 0
        assert health["initiative_retry_ordinal"] == 0
        assert health["initiative_last_failure_code"] is None
        assert health["initiative_last_model_decision"] == expected_decision
        assert (await runtime.drain_one()).status != "completed_existing"
    finally:
        app.close()


@pytest.mark.asyncio
async def test_proactive_health_keeps_a_24_hour_failure_visible_after_a_newer_success(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _ProactiveReplySequence(
        [
            "{}",
            "{}",
            _proactive_draft("忽然想跟你说句话。"),
        ]
    )
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "proactive-health-window.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id=WORLD,
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=_DeliveredTransport(),
        proactive_model=proactive,
        now=NOW,
    )
    try:
        _seed_due_thread(app._ledger)  # noqa: SLF001 - real persisted authority fixture
        runtime = app._turns._runtime._proactive_action_runtime  # noqa: SLF001
        assert runtime is not None
        assert (await runtime.drain_one()).status == "opened"
        assert (await runtime.drain_one()).status == "failed_safe"
        current_time = app._ledger.project().logical_time  # noqa: SLF001
        assert current_time is not None

        _seed_due_thread(
            app._ledger,  # noqa: SLF001
            thread_key="newer",
            advance_clock=False,
            event_at=current_time,
        )
        assert (await runtime.drain_one()).status == "opened"
        assert (await runtime.drain_one()).status == "authorized"
        assert (await app.drain_actions_once()).status == "settled"

        health = await app.world_health_diagnostics()
        reliability = health["initiative_reliability_24h"]

        assert reliability == {
            "window_hours": 24,
            "as_of": current_time.isoformat(),
            "attempt_count": 2,
            "consideration_count": 2,
            "technical_failure_attempt_count": 1,
            "technical_failure_consideration_count": 1,
            "model_silent_count": 0,
            "grounding_rejected_count": 0,
            "authorized_count": 1,
            "delivered_count": 1,
            "delivery_pending_count": 0,
            "delivery_non_delivered_terminal_count": 0,
            "model_decision_success_rate": 0.5,
            "technical_failure_rate": 0.5,
            "technical_failure_attempt_rate": 0.5,
            "visible_authorization_rate": 0.5,
            "visible_delivery_rate": 0.5,
            "delivery_success_rate": 1.0,
            "technical_failure_codes": {
                "authored_expression_reselection_invalid": 1,
            },
            "warning": True,
            "warning_reasons": ["technical_failures_24h"],
        }
        assert health["initiative_last_model_decision"] == "now"
        assert health["initiative_consecutive_technical_failures"] == 0
        assert health["initiative_warning"] is True
        assert "technical_failures_24h" in health["initiative_warning_reasons"]
    finally:
        app.close()


@pytest.mark.asyncio
async def test_proactive_health_keeps_a_failed_attempt_visible_after_same_consideration_recovers(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _ProactiveReplySequence(
        [
            "{}",
            "{}",
            _proactive_draft("刚才想说的话现在说出来。"),
        ]
    )
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "proactive-health-recovered-retry.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id=WORLD,
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=_DeliveredTransport(),
        proactive_model=proactive,
        now=NOW,
    )
    try:
        _seed_due_thread(app._ledger)  # noqa: SLF001 - durable retry fixture
        runtime = app._turns._runtime._proactive_action_runtime  # noqa: SLF001
        assert runtime is not None
        assert (await runtime.drain_one()).status == "opened"
        assert (await runtime.drain_one()).status == "failed_safe"

        failed_projection = app._ledger.project()  # noqa: SLF001
        failed_at = failed_projection.logical_time
        assert failed_at is not None
        retry_health = await app.world_health_diagnostics()
        retry_due = datetime.fromisoformat(
            str(retry_health["initiative_next_consideration_at"])
        )
        await app.tick(
            tick_id="tick:proactive-health-recovered-retry",
            logical_time_from=failed_at,
            logical_time_to=retry_due,
            observed_at=retry_due,
            trace_id="trace:proactive-health-recovered-retry",
            causation_id="scheduler:test",
            correlation_id="conversation:proactive-health-recovered-retry",
            reason="technical_retry_due",
        )
        assert (await runtime.drain_one()).status == "opened"
        assert (await runtime.drain_one()).status == "authorized"
        assert (await app.drain_actions_once()).status == "settled"

        health = await app.world_health_diagnostics()
        reliability = health["initiative_reliability_24h"]

        assert reliability["attempt_count"] == 2
        assert reliability["consideration_count"] == 1
        assert reliability["technical_failure_attempt_count"] == 1
        assert reliability["technical_failure_consideration_count"] == 0
        assert reliability["technical_failure_rate"] == 0.0
        assert reliability["technical_failure_attempt_rate"] == 0.5
        assert reliability["authorized_count"] == 1
        assert reliability["delivered_count"] == 1
        assert reliability["warning"] is True
        assert reliability["warning_reasons"] == ["technical_failures_24h"]
        assert health["initiative_warning"] is True
    finally:
        app.close()


@pytest.mark.asyncio
async def test_delivered_response_expectation_does_not_open_a_proactive_lane(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _LooseProactiveModel({"choice": "now", "text": "刚才说晚点聊，我还记着。"})
    transport = _DeliveredTransport()
    chat = ChatModelDeliberationAdapter(model=_ResponseExpectingChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "response-gap.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:response-gap",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=3_600,
                spontaneous_expiry_seconds=7_200,
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        proactive_model=proactive,
        now=NOW,
    )
    try:
        initial = await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:expectation-source",
            text="我先忙一下",
            observed_at=NOW,
            trace_id="trace:expectation-source",
        )
        assert len(initial.authorized_action_ids) == 1
        assert (await app.drain_actions_once()).status == "settled"
        await app.tick(
            tick_id="tick:response-gap",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61),
            trace_id="trace:response-gap",
            causation_id="scheduler:test",
            correlation_id="conversation:response-gap",
            reason="test_response_gap",
        )
        assert await app.drain_background_once() is None
        assert proactive.calls == 0
        assert transport.bodies == ["你忙完跟我说一声呀。"]
    finally:
        app.close()


@pytest.mark.asyncio
async def test_real_qq_provider_expectation_remains_advisory_only(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _DraftModel("silent")
    delivery = _QQDelivery()
    transport = QQC2CPlatformTransport(
        delivery=delivery,  # type: ignore[arg-type]
        recipients_by_target={"user:primary": "qq-user-1"},
        now=lambda: NOW,
    )
    chat = ChatModelDeliberationAdapter(model=_ResponseExpectingChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "response-gap-qq-provider-accepted.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:response-gap-qq-provider-accepted",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=3_600,
                spontaneous_expiry_seconds=7_200,
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        proactive_model=proactive,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:qq-expectation",
            text="我先忙一下",
            observed_at=NOW,
            trace_id="trace:qq-expectation",
        )
        assert (await app.drain_actions_once()).status == "settled"
        await app.tick(
            tick_id="tick:qq-response-gap",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61),
            trace_id="trace:qq-response-gap",
            causation_id="scheduler:test",
            correlation_id="conversation:qq-response-gap",
            reason="test_qq_response_gap",
        )
        opened = await app.drain_background_once()
        assert opened is None
        assert proactive.calls == 0
    finally:
        app.close()


@pytest.mark.asyncio
async def test_failed_qq_send_never_opens_a_response_gap(tmp_path) -> None:  # type: ignore[no-untyped-def]
    proactive = _DraftModel("silent")
    delivery = _QQDelivery(failed=True)
    transport = QQC2CPlatformTransport(
        delivery=delivery,  # type: ignore[arg-type]
        recipients_by_target={"user:primary": "qq-user-1"},
        now=lambda: NOW,
    )
    chat = ChatModelDeliberationAdapter(model=_ResponseExpectingChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "response-gap-qq-failed.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:response-gap-qq-failed",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=3_600,
                spontaneous_expiry_seconds=7_200,
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        proactive_model=proactive,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:qq-failed",
            text="我先忙一下",
            observed_at=NOW,
            trace_id="trace:qq-failed",
        )
        assert (await app.drain_actions_once()).status == "settled"
        await app.tick(
            tick_id="tick:qq-failed",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61),
            trace_id="trace:qq-failed-tick",
            causation_id="scheduler:test",
            correlation_id="conversation:qq-failed",
            reason="test_qq_failed_gap",
        )
        assert await app.drain_background_once() is None
        assert proactive.calls == 0
    finally:
        app.close()


@pytest.mark.asyncio
async def test_unknown_qq_send_never_opens_a_response_gap(tmp_path) -> None:  # type: ignore[no-untyped-def]
    proactive = _DraftModel("silent")
    delivery = _QQDelivery()
    transport = QQC2CPlatformTransport(
        delivery=delivery,  # type: ignore[arg-type]
        recipients_by_target={"user:primary": "qq-user-1"},
        now=lambda: NOW,
    )
    chat = ChatModelDeliberationAdapter(model=_ResponseExpectingChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "response-gap-qq-unknown.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:response-gap-qq-unknown",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=3_600,
                spontaneous_expiry_seconds=7_200,
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        proactive_model=proactive,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:qq-unknown",
            text="我先忙一下",
            observed_at=NOW,
            trace_id="trace:qq-unknown",
        )
        assert (await app.drain_actions_once()).status == "settled"
        await app.tick(
            tick_id="tick:qq-unknown",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=121),
            observed_at=NOW + timedelta(seconds=121),
            trace_id="trace:qq-unknown-tick",
            causation_id="scheduler:test",
            correlation_id="conversation:qq-unknown",
            reason="test_qq_unknown_gap",
        )
        assert (await app.drain_actions_once()).status == "marked_unknown"
        assert await app.drain_background_once() is None
        assert proactive.calls == 0
    finally:
        app.close()


@pytest.mark.asyncio
async def test_persisted_qq_provider_expectation_does_not_reopen_after_restart(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "response-gap-qq-restart.sqlite3"
    config = WorldV2TurnApplicationConfig(
        world_id="world:response-gap-qq-restart",
        companion_actor_ref="actor:companion",
        reply_target="user:primary",
        action_pump_owner="worker:actions",
        social_initiative_policy=SocialInitiativePolicy(
            spontaneous_idle_seconds=3_600,
            spontaneous_expiry_seconds=7_200,
        ),
    )
    chat = ChatModelDeliberationAdapter(model=_ResponseExpectingChat())
    first = build_sqlite_world_v2_turn_application(
        path=path,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=QQC2CPlatformTransport(
            delivery=_QQDelivery(),  # type: ignore[arg-type]
            recipients_by_target={"user:primary": "qq-user-1"},
            now=lambda: NOW,
        ),
        proactive_model=_DraftModel("silent"),
        now=NOW,
    )
    try:
        await first.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:qq-restart",
            text="我先忙一下",
            observed_at=NOW,
            trace_id="trace:qq-restart",
        )
        assert (await first.drain_actions_once()).status == "settled"
    finally:
        first.close()

    restarted_proactive = _DraftModel("silent")
    restarted = build_sqlite_world_v2_turn_application(
        path=path,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=QQC2CPlatformTransport(
            delivery=_QQDelivery(),  # type: ignore[arg-type]
            recipients_by_target={"user:primary": "qq-user-1"},
            now=lambda: NOW,
        ),
        proactive_model=restarted_proactive,
        now=NOW,
    )
    try:
        await restarted.tick(
            tick_id="tick:qq-restart",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61),
            trace_id="trace:qq-restart-tick",
            causation_id="scheduler:test",
            correlation_id="conversation:qq-restart",
            reason="test_qq_restart_gap",
        )
        opened = await restarted.drain_background_once()
        assert opened is None
        assert restarted_proactive.calls == 0
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_production_application_does_not_infer_response_gap_from_message_text(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _DraftModel("now")
    transport = _DeliveredTransport()
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "no-response-gap.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:no-response-gap",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=3_600,
                spontaneous_expiry_seconds=7_200,
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        proactive_model=proactive,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:no-expectation",
            text="你在吗？",
            observed_at=NOW,
            trace_id="trace:no-expectation",
        )
        await app.drain_actions_once()
        await app.tick(
            tick_id="tick:no-response-gap",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61),
            trace_id="trace:no-response-gap",
            causation_id="scheduler:test",
            correlation_id="conversation:no-response-gap",
            reason="test_no_response_gap",
        )
        assert await app.drain_background_once() is None
        assert proactive.calls == 0
    finally:
        app.close()


@pytest.mark.asyncio
async def test_failed_spontaneous_delivery_is_settled_once_and_not_resent(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "social-initiative-failed.sqlite3"
    proactive = _DraftModel("now")
    transport = _FailedTransport()
    chat = ChatModelDeliberationAdapter(model=_NoExpectationChat())
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=WorldV2TurnApplicationConfig(
            world_id="world:social-initiative-failed",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=60,
                spontaneous_expiry_seconds=3_600,
                consideration_band_override_seconds=(60, 60),
            ),
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        proactive_model=proactive,
        now=NOW,
    )
    try:
        await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:failed-source",
            text="我去忙了",
            observed_at=NOW,
            trace_id="trace:failed-source",
        )
        await app.drain_actions_once()
        await app.tick(
            tick_id="tick:failed-contact",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61),
            trace_id="trace:failed-contact",
            causation_id="scheduler:test",
            correlation_id="conversation:failed-contact",
            reason="test_failed_contact",
        )
        assert (await app.drain_background_once()).status == "opened"
        assert (await app.drain_background_once()).status == "authorized"
        assert (await app.drain_actions_once()).status == "settled"
        assert (await app.drain_actions_once()).status == "idle"
        assert transport.bodies == ["好，你先忙。", "刚才那件事我又想了一下。"]
    finally:
        app.close()
    ledger = SQLiteWorldLedger(path=path, world_id="world:social-initiative-failed")
    try:
        proactive_actions = [
            item for item in ledger.project().actions if item.kind == "proactive_message"
        ]
        assert len(proactive_actions) == 1
        assert proactive_actions[0].state == "failed"
    finally:
        ledger.close()
