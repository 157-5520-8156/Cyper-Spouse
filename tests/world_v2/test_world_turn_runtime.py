from datetime import UTC, datetime
from hashlib import sha256

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.context_capsule import ContextCapsuleCompiler
from companion_daemon.world_v2.deliberation import (
    Deliberation,
    ModelInput,
    ModelOutput,
    ModelRoute,
    RouteRequest,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import context_capsule_compiler_from_ledger
from companion_daemon.world_v2.ledger_payload_reader import LedgerAuthorizedPayloadReader
from companion_daemon.world_v2.minimal_reply_acceptance import ReplyBudgetPolicy
from companion_daemon.world_v2.minimal_reply_atomic_recorder import MinimalReplyAtomicRecorder
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    MinimalProposal,
    ProposalActionIntent,
    TypedChange,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import BudgetAccount, WorldEvent
from companion_daemon.world_v2.platform_action_executor import (
    PlatformActionExecutor,
    PlatformDispatchReceipt,
    PlatformDispatchRequest,
)
from companion_daemon.world_v2.world_turn_runtime import InboundTurn, WorldTurnRuntime


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert (platform, platform_user_id) == ("test", "user.1")
        return "user:user.1", "user:user.1"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _InvalidQuick:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        return ModelOutput(
            model_id="test-quick",
            model_version="test.1",
            raw_proposal={},
            input_tokens=1,
            output_tokens=1,
        )


class _MinimalReplyModel:
    async def propose(self, request: ModelInput) -> ModelOutput:
        text = "我听见你的失望了，刚刚确实没有接住。"
        payload_hash = "sha256:" + sha256(text.encode("utf-8")).hexdigest()
        proposal = MinimalProposal(
            proposal_id="proposal:turn-runtime:minimal:1",
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(),
            proposed_changes=(
                TypedChange(
                    change_id="change:turn-runtime:expression:1",
                    kind="expression_plan_transition",
                    target_id="plan:turn-runtime:reply:1",
                    transition="accept",
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="expression_plan_transition.v1",
                        value={
                            "plan_id": "plan:turn-runtime:reply:1",
                            "overall_intent": "reply",
                            "ordering_policy": "dependencies",
                            "terminal_policy": "settle",
                            "beat_drafts": [
                                {
                                    "beat_id": "beat:turn-runtime:reply:1",
                                    "inline_text": text,
                                    "materialized_payload_ref": "payload:turn-runtime:reply:1",
                                    "payload_hash": payload_hash,
                                    "content_type": "text/plain",
                                    "dependency_beat_ids": [],
                                    "delay_window": None,
                                    "cancel_policy": "cancel-before-dispatch",
                                    "reconsider_policy": "reconsider-on-new-observation",
                                    "merge_policy": "never",
                                }
                            ],
                        },
                    ),
                ),
            ),
            action_intents=(
                ProposalActionIntent(
                    intent_id="intent:turn-runtime:reply:1",
                    kind="reply",
                    layer="external_action",
                    target="user:user.1",
                    payload_ref="payload:turn-runtime:reply:1",
                    payload_hash=payload_hash,
                    causal_change_id="change:turn-runtime:expression:1",
                    beat_ref="beat:turn-runtime:reply:1",
                ),
            ),
            confidence=7_000,
            brief_rationale="Acknowledge the user's disappointment without making world claims.",
            source_model_result="model-result:placeholder",
            response_text=text,
            stance="acknowledge_briefly",
        )
        return ModelOutput(
            model_id="test-minimal-main",
            model_version="test.1",
            raw_proposal=proposal.model_dump(mode="json"),
            input_tokens=1,
            output_tokens=1,
        )


class _Transport:
    provider = "platform:test"

    def __init__(self) -> None:
        self.sent: list[PlatformDispatchRequest] = []

    async def send(self, request: PlatformDispatchRequest) -> PlatformDispatchReceipt:
        self.sent.append(request)
        return PlatformDispatchReceipt(
            provider_receipt_id="provider-receipt:turn-runtime:1",
            provider_ref="provider-message:turn-runtime:1",
            status="delivered",
            received_at=datetime(2026, 7, 15, tzinfo=UTC),
            raw_payload_hash="sha256:" + "a" * 64,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


def _event(event_type: str, payload: dict[str, object], suffix: str) -> WorldEvent:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:turn-runtime:{suffix}",
        world_id="world:turn-runtime",
        event_type=event_type,
        logical_time=now,
        created_at=now,
        actor="system:test",
        source="test",
        trace_id="trace:turn-runtime",
        causation_id="test",
        correlation_id="test",
        idempotency_key=f"turn-runtime:{suffix}",
        payload=payload,
    )


def _configured_runtime() -> tuple[WorldRuntime, WorldLedger, _Transport]:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id="world:turn-runtime", accepted_batch_issuer=issuer)
    account = BudgetAccount(
        account_id="account:turn-runtime:chat", category="chat", window_id="test", limit=100
    )
    ledger.commit(
        (
            _event("WorldStarted", {}, "started"),
            _event("BudgetAccountConfigured", {"account": account.model_dump(mode="json")}, "budget"),
        ),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    capsules: ContextCapsuleCompiler = context_capsule_compiler_from_ledger(ledger=ledger)
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=capsules,
        deliberation=Deliberation(
            router=_Router(), main_model=_MinimalReplyModel(), quick_recovery=_InvalidQuick()
        ),
        companion_actor_ref="agent:companion",
    )
    transport = _Transport()
    return (
        WorldRuntime(
            world_id="world:turn-runtime",
            ledger=ledger,
            pinned_turn=turn,
            reply_policy=ReplyBudgetPolicy(
                account_id=account.account_id,
                amount_limit=10,
                actor="agent:companion",
                target="user:user.1",
                recovery_policy="effect_once",
            ),
            reply_recorder=MinimalReplyAtomicRecorder(batch_issuer=issuer),
            action_executor=PlatformActionExecutor(
                payloads=LedgerAuthorizedPayloadReader(ledger=ledger), transport=transport
            ),
            action_pump_owner="pump:turn-runtime",
        ),
        ledger,
        transport,
    )


@pytest.mark.asyncio
async def test_platform_neutral_turn_ingress_records_one_idempotent_v2_observation() -> None:
    runtime = WorldRuntime.in_memory(world_id="world:turn-runtime")
    turn = WorldTurnRuntime(runtime=runtime, identities=_Identities())
    inbound = InboundTurn(
        platform="test", platform_user_id="user.1", platform_message_id="message.1",
        text="今天有点累。", observed_at=datetime(2026, 7, 15, tzinfo=UTC), trace_id="trace.1",
    )

    first = await turn.respond(inbound)
    duplicate = await turn.respond(inbound)

    assert first == duplicate
    assert first.status == "observed_only"


@pytest.mark.asyncio
async def test_platform_neutral_turn_runs_authorize_dispatch_and_settle_without_legacy_engine() -> None:
    runtime, ledger, transport = _configured_runtime()
    turn = WorldTurnRuntime(runtime=runtime, identities=_Identities())
    inbound = InboundTurn(
        platform="test",
        platform_user_id="user.1",
        platform_message_id="message:authorized",
        text="我有点失望，感觉你没接住。",
        observed_at=datetime(2026, 7, 15, tzinfo=UTC),
        trace_id="trace:authorized",
    )

    first = await turn.respond(inbound)
    duplicate = await turn.respond(inbound)
    authorized_action = ledger.project().pending_actions[0]
    payloads = LedgerAuthorizedPayloadReader(ledger=ledger)
    resolved = await payloads.resolve(authorized_action)
    delivery = await turn.drain_actions_once()

    assert first.status == "action_authorized"
    assert duplicate == first
    assert resolved.body == "我听见你的失望了，刚刚确实没有接住。"
    assert delivery is not None and delivery.status == "settled"
    assert [request.body for request in transport.sent] == ["我听见你的失望了，刚刚确实没有接住。"]
    projection = ledger.project()
    assert len(projection.proposal_audits) == 1
    assert len(projection.stored_message_payloads) == 1
    assert projection.actions[0].state == "delivered"


@pytest.mark.asyncio
async def test_authorized_payload_reader_rejects_an_action_with_substituted_payload_identity() -> None:
    runtime, ledger, _transport = _configured_runtime()
    turn = WorldTurnRuntime(runtime=runtime, identities=_Identities())
    await turn.respond(
        InboundTurn(
            platform="test",
            platform_user_id="user.1",
            platform_message_id="message:forged-payload",
            text="我有点失望，感觉你没接住。",
            observed_at=datetime(2026, 7, 15, tzinfo=UTC),
            trace_id="trace:forged-payload",
        )
    )
    action = ledger.project().pending_actions[0].model_copy(
        update={"payload_ref": "payload:substituted"}
    )

    with pytest.raises(ValueError, match="authorization manifest"):
        await LedgerAuthorizedPayloadReader(ledger=ledger).resolve(action)
