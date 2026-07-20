from __future__ import annotations

from datetime import UTC, datetime, timedelta
import asyncio
import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
    CompanionIdentityFrame,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelOutput,
    ModelRoute,
    RouteRequest,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.expression_plan_acceptance import ExpressionPlanBudgetPolicy
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
)
from companion_daemon.world_v2.production_proposal_grammar import compose_production_deliberation
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.qq_c2c_transport import QQC2CPlatformTransport
from companion_daemon.world_v2.social_initiative import SocialInitiativePolicy
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    DueWindow,
    EvidenceRef,
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


def _event(event_id: str, event_type: str, payload: dict[str, object], *, at: datetime = NOW) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, world_id=WORLD,
        event_type=event_type, logical_time=at, created_at=at,
        actor="system:test", source="test", trace_id="trace:proactive",
        causation_id="cause:proactive", correlation_id="conversation:proactive",
        idempotency_key=(domain_idempotency_key(
            event_type=event_type, world_id=WORLD, payload=payload
        ) or "test:" + event_id), payload=payload,
    )


def _commit(ledger: WorldLedger, *events: WorldEvent) -> None:
    projection = ledger.project()
    ledger.commit(events, expected_world_revision=projection.world_revision,
                  expected_deliberation_revision=projection.deliberation_revision)


def _seed_due_thread(ledger: WorldLedger) -> None:
    source = EvidenceRef(
        ref_id="operator:unfinished-thought", evidence_type="operator_observation",
        claim_purpose="conversation_continuity", immutable_hash="a" * 64,
    )
    _commit(ledger, _event("event:operator:unfinished", "OperatorObservationRecorded", {
        "observation_id": source.ref_id, "observation_hash": source.immutable_hash,
    }))
    projection = ledger.project()
    origin = ThreadOrigin(
        change_id="change:thread:pulse:1", transition_id="transition:thread:pulse:1",
        policy_refs=("policy:thread-v1",), accepted_event_ref="event:thread:pulse:opened",
    )
    values = ThreadValues(
        kind="topic_open", subject_ref="subject:unfinished-thought",
        conversation_ref="conversation:proactive", anchor_evidence_refs=(source,),
        source_evidence_refs=(source,), importance_bp=7_000,
        due_window=DueWindow(opens_at=NOW + timedelta(minutes=1),
                             closes_at=NOW + timedelta(hours=1)),
        expires_at=NOW + timedelta(hours=2),
        resolution_contract_ref="resolution:unfinished-thought", privacy_class="private",
    )
    thread = ThreadProjection(
        thread_id="thread:pulse:1", entity_revision=1,
        semantic_fingerprint=thread_semantic_fingerprint(
            kind=values.kind, subject_ref=values.subject_ref,
            conversation_ref=values.conversation_ref,
            anchor_evidence_refs=values.anchor_evidence_refs,
            resolution_contract_ref=values.resolution_contract_ref,
            policy_refs=origin.policy_refs,
        ),
        values=values, origin=origin, opened_at=NOW, updated_at=NOW,
    )
    raw: dict[str, object] = {
        "change_id": origin.change_id, "transition_id": origin.transition_id,
        "expected_entity_revision": 0, "evidence_refs": (source,),
        "policy_refs": origin.policy_refs, "acceptance_id": "acceptance:thread:pulse:1",
        "proposal_id": "proposal:thread:pulse:1",
        "evaluated_world_revision": projection.world_revision,
        "accepted_change_hash": "0" * 64, "operation": "open",
        "thread_before": None, "thread_after": thread,
        "compensates_transition_id": None,
    }
    raw["accepted_change_hash"] = thread_mutation_hash(raw)
    changed = ThreadChangedPayload.model_validate(raw)
    proposed = ThreadProposalProjection(
        proposal_id=changed.proposal_id, proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:thread.1", transition_kind="open",
        change_id=changed.change_id, transition_id=changed.transition_id,
        evaluated_world_revision=changed.evaluated_world_revision,
        expected_entity_revision=0, proposed_change_hash=changed.accepted_change_hash,
        evidence_refs=changed.evidence_refs, policy_refs=changed.policy_refs,
        proposed_mutation=ThreadProposedMutation(
            event_type="ThreadOpened",
            payload_json=json.dumps(changed.model_dump(mode="json"), ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")),
        ),
    )
    _commit(ledger, _event("event:proposal:thread:pulse:1", "ProposalRecorded",
                           proposed.model_dump(mode="json")))
    _commit(
        ledger,
        _event("event:acceptance:thread:pulse:1", "AcceptanceRecorded", {
            "acceptance_id": changed.acceptance_id, "status": "accepted",
            "proposal_id": changed.proposal_id,
            "evaluated_world_revision": changed.evaluated_world_revision,
            "accepted_change_id": changed.change_id,
            "accepted_change_hash": changed.accepted_change_hash,
        }),
        _event(origin.accepted_event_ref, "ThreadOpened", changed.model_dump(mode="json")),
    )
    due = NOW + timedelta(minutes=2)
    _commit(ledger, _event("event:clock:thread-due", "ClockAdvanced", {
        "logical_time_from": NOW.isoformat(), "logical_time_to": due.isoformat(),
    }, at=due))


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
            "behavior_tendency": "remember_and_choose",
            "stance": "low_pressure",
            "display_strategy": "natural",
            "brief_rationale": "根据当前关系与未完事项自由决定",
            "confidence": 7_200,
        }
        if self.choice != "silent":
            value["response_text"] = "刚才那件事我又想了一下。"
        if self.choice == "later":
            value.update(delay_seconds=60, expires_after_seconds=600)
        return json.dumps(value, ensure_ascii=False)

    def captured_capsule(self) -> dict[str, object]:
        assert len(self.messages) == 1
        envelope = json.loads(self.messages[0][1]["content"])
        return json.loads(envelope["request"]["model_content_json"])


class _MalformedProactiveModel:
    model = "test-malformed-proactive"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        return "{}"


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
    *, ledger, issuer, model, owner="worker:proactive", identity_frame=None
):  # type: ignore[no-untyped-def]
    adapter = ProactiveDraftAdapter(
        model=model, target="user:primary", identity_frame=identity_frame
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
    )
    return runtime, turn


def _runtime(*, choice: str, budget: int = 100):
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    _commit(ledger, _event("event:world:start", "WorldStarted", {}))
    if ledger.project().logical_time != NOW:
        _commit(ledger, _event("event:clock", "ClockAdvanced", {
            "logical_time_from": (ledger.project().logical_time or NOW - timedelta(minutes=2)).isoformat(),
            "logical_time_to": NOW.isoformat(),
        }))
    account = BudgetAccount(
        account_id="account:proactive", category="proactive", window_id="day:1", limit=budget
    )
    _commit(ledger, _event("event:budget:proactive", "BudgetAccountConfigured", {
        "account": account.model_dump(mode="json")
    }))
    _seed_due_thread(ledger)
    model = _DraftModel(choice)
    runtime, turn = _make_proactive_runtime(ledger=ledger, issuer=issuer, model=model)
    return ledger, model, runtime, turn


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "status", "action_kind"),
    [("now", "authorized", "proactive_message"),
     ("later", "authorized", "followup"),
     ("silent", "silent", None)],
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
    assert "semantic anchor" in system
    assert "generic greeting" in system
    assert "choose silent" in system
    user = json.loads(model.messages[0][1]["content"])
    assert user["proactive_opportunity"]["source_kind"] == "thread"
    assert "verified proactive opportunity" in user["proactive_opportunity"]["guidance"].lower()
    assert user["proactive_opportunity"]["source_refs"] == [proposal["trigger_ref"]]


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
async def test_loose_visible_proactive_output_salvages_only_explicit_choice_and_text() -> None:
    ledger, _model, _runtime_value, _turn = _runtime(choice="silent")
    model = _LooseProactiveModel({"choice": "now", "text": "刚才那件事，我还记着。"})
    runtime, _ = _make_proactive_runtime(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - provider salvage seam
        model=model,
    )

    assert (await runtime.drain_one()).status == "opened"
    result = await runtime.drain_one()

    assert result.status == "authorized"
    proposal = json.loads(ledger.project().proposal_audits[-1].proposal_json)
    assert proposal["brief_rationale"] == "Considered the verified proactive opportunity."
    assert proposal["confidence"] == 5000
    assert proposal["proactive_opportunity_decision"]["decision_origin"] == "model"


@pytest.mark.asyncio
async def test_loose_explicit_silent_ignores_invalid_peripheral_fields() -> None:
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
    assert (await runtime.drain_one()).status == "silent"

    proposal = json.loads(ledger.project().proposal_audits[-1].proposal_json)
    decision = proposal["proactive_opportunity_decision"]
    assert decision["disposition"] == "silent_after_consideration"
    assert decision["decision_origin"] == "model"


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
async def test_two_unparseable_choices_close_as_a_distinct_local_failsafe_silence() -> None:
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

    assert result.status == "silent"
    assert malformed.calls == 2
    projection = ledger.project()
    assert len(projection.model_result_audits) == 2
    assert len(projection.proposal_audits) == 1
    assert projection.actions == ()
    process = projection.trigger_processes[-1]
    assert process.state == "terminal"
    assert process.runtime_outcome_ref == "proactive:silent"
    proposal = json.loads(projection.proposal_audits[-1].proposal_json)
    assert proposal["proactive_opportunity_decision"] == {
        "decision_origin": "local_failsafe",
        "disposition": "silent_after_consideration",
        "source_event_ref": proposal["trigger_ref"],
        "source_kind": "thread",
        "source_payload_hash": proposal["evidence_refs"][0]["immutable_hash"],
        "source_world_revision": proposal["evidence_refs"][0]["source_world_revision"],
    }
    assert (await runtime.drain_one()).status == "idle"
    assert malformed.calls == 2


@pytest.mark.asyncio
async def test_restart_closes_an_already_audited_local_failsafe_without_retry() -> None:
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
    commit = await turn.audit(
        opportunity=opportunity,
        cursor=ProactiveActionRuntime._cursor(projection),
    )
    assert commit.proposal_id is not None
    assert malformed.calls == 2

    result = await runtime.drain_one()

    assert result.status == "silent"
    assert malformed.calls == 2
    assert ledger.project().trigger_processes[-1].state == "terminal"


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
    assert len([item for item in ledger.project().trigger_processes
                if item.process_kind == "proactive_action_deliberation"]) == 1


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
    reopened = SQLiteWorldLedger(
        path=path, world_id=WORLD, accepted_batch_issuer=reopened_issuer
    )
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
    terminal = SQLiteWorldLedger(
        path=path, world_id=WORLD, accepted_batch_issuer=terminal_issuer
    )
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
        account_id="account:proactive", amount_limit_per_action=10,
        actor="actor:companion", allowed_targets=("user:primary",),
        recovery_policy="effect_once", category="proactive",
    )
    second = ProactiveActionRuntime(
        ledger=ledger, turn=turn, batch_issuer=ledger._accepted_batch_issuer,
        policy=policy, owner_id="worker:proactive:second",
    )
    assert (await first.drain_one()).status == "opened"
    results = await asyncio.gather(first.drain_one(), second.drain_one())
    assert {item.status for item in results} <= {
        "authorized", "owned_elsewhere", "stale", "completed_existing"
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
        source_kind="thread", source_id=projection.threads[-1].thread_id,
        source_event_ref=source_ref, source_event_hash="f" * 64,
        source_world_revision=located[1].world_revision,
        trace_id=located[0].trace_id, correlation_id=located[0].correlation_id,
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
            world_id="world:proactive-composed", companion_actor_ref="actor:companion",
            reply_target="user:primary", action_pump_owner="worker:actions",
        ),
        identities=_Identities(), router=_Router(), main_model=_InvalidMain(),
        quick_recovery=_InvalidQuick(), transport=_NoDispatchTransport(),
        proactive_model=proactive, now=NOW,
    )
    try:
        outcome = await app.inbound(
            platform="http", platform_user_id="user.1", platform_message_id="message:1",
            text="今天有点累", observed_at=NOW, trace_id="trace:ordinary",
        )
        assert not outcome.authorized_action_ids
        assert proactive.calls == 0
        await app.drain_background_once()
        assert proactive.calls == 0
    finally:
        app.close()
    ledger = SQLiteWorldLedger(path=path, world_id="world:proactive-composed")
    try:
        account = next(item for item in ledger.project().budget_accounts
                       if item.account_id == "account:world-v2:proactive")
        assert account.category == "proactive"
        assert account.limit == 1_000
    finally:
        ledger.close()


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
        assert draw_payload["weight_policy_version"] == "social-initiative-context.1"
        assert draw_payload["candidate_refs"] == ["act", "hold"]
        assert (await app.drain_background_once()).status == "authorized"
        assert (await app.drain_actions_once()).status == "settled"
        for _ in range(3):
            await app.drain_background_once()
        assert proactive.calls == 1
        capsule = proactive.captured_capsule()
        assert {"relationship_slice", "affect_episodes", "world_life"} <= set(
            capsule["slices"]
        )
        assert "我先去忙一会儿" in json.dumps(capsule, ensure_ascii=False)
        assert "spontaneous_contact" in json.dumps(capsule, ensure_ascii=False)
        assert transport.bodies == ["好，你先忙。", "刚才那件事我又想了一下。"]
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_application_uses_explicit_delivered_response_expectation_for_gap(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    proactive = _LooseProactiveModel(
        {"choice": "now", "text": "刚才说晚点聊，我还记着。"}
    )
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
        assert (await app.drain_background_once()).status == "opened"
        assert (await app.drain_background_once()).status == "authorized"
        assert (await app.drain_actions_once()).status == "settled"
        capsule = proactive.captured_capsule()
        assert "对方忙完后回来继续聊天" in json.dumps(capsule, ensure_ascii=False)
        assert "response_gap" in json.dumps(capsule, ensure_ascii=False)
        model_input = json.loads(proactive.messages[0][1]["content"])
        opportunity = model_input["proactive_opportunity"]
        assert opportunity["source_kind"] == "response_gap"
        assert "对方忙完后回来继续聊天" in opportunity["guidance"]
        proactive_system = proactive.messages[0][0]["content"]
        assert "non-null verified proof" in proactive_system
        assert "rather than denying" in proactive_system
        proposal = json.loads(
            app._ledger.project().proposal_audits[-1].proposal_json  # noqa: SLF001
        )
        assert proposal["proactive_opportunity_decision"]["source_kind"] == "response_gap"
        assert transport.bodies == [
            "你忙完跟我说一声呀。",
            "刚才说晚点聊，我还记着。",
        ]
    finally:
        app.close()


@pytest.mark.asyncio
async def test_real_qq_provider_acceptance_can_open_a_truthful_response_gap(
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
            companion_actor_ref="actor:companion", reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=3_600, spontaneous_expiry_seconds=7_200,
            ),
        ),
        identities=_Identities(), router=_Router(), main_model=chat,
        quick_recovery=chat, transport=transport, proactive_model=proactive, now=NOW,
    )
    try:
        await app.inbound(
            platform="http", platform_user_id="user.1",
            platform_message_id="message:qq-expectation", text="我先忙一下",
            observed_at=NOW, trace_id="trace:qq-expectation",
        )
        assert (await app.drain_actions_once()).status == "settled"
        await app.tick(
            tick_id="tick:qq-response-gap", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61), trace_id="trace:qq-response-gap",
            causation_id="scheduler:test", correlation_id="conversation:qq-response-gap",
            reason="test_qq_response_gap",
        )
        opened = await app.drain_background_once()
        assert opened is not None and opened.status == "opened"
        assert proactive.calls == 0
    finally:
        app.close()


@pytest.mark.asyncio
async def test_failed_qq_send_never_opens_a_response_gap(tmp_path) -> None:  # type: ignore[no-untyped-def]
    proactive = _DraftModel("silent")
    delivery = _QQDelivery(failed=True)
    transport = QQC2CPlatformTransport(
        delivery=delivery,  # type: ignore[arg-type]
        recipients_by_target={"user:primary": "qq-user-1"}, now=lambda: NOW,
    )
    chat = ChatModelDeliberationAdapter(model=_ResponseExpectingChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "response-gap-qq-failed.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:response-gap-qq-failed", companion_actor_ref="actor:companion",
            reply_target="user:primary", action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=3_600, spontaneous_expiry_seconds=7_200,
            ),
        ),
        identities=_Identities(), router=_Router(), main_model=chat,
        quick_recovery=chat, transport=transport, proactive_model=proactive, now=NOW,
    )
    try:
        await app.inbound(
            platform="http", platform_user_id="user.1",
            platform_message_id="message:qq-failed", text="我先忙一下",
            observed_at=NOW, trace_id="trace:qq-failed",
        )
        assert (await app.drain_actions_once()).status == "settled"
        await app.tick(
            tick_id="tick:qq-failed", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61), trace_id="trace:qq-failed-tick",
            causation_id="scheduler:test", correlation_id="conversation:qq-failed",
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
        recipients_by_target={"user:primary": "qq-user-1"}, now=lambda: NOW,
    )
    chat = ChatModelDeliberationAdapter(model=_ResponseExpectingChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "response-gap-qq-unknown.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id="world:response-gap-qq-unknown", companion_actor_ref="actor:companion",
            reply_target="user:primary", action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=3_600, spontaneous_expiry_seconds=7_200,
            ),
        ),
        identities=_Identities(), router=_Router(), main_model=chat,
        quick_recovery=chat, transport=transport, proactive_model=proactive, now=NOW,
    )
    try:
        await app.inbound(
            platform="http", platform_user_id="user.1",
            platform_message_id="message:qq-unknown", text="我先忙一下",
            observed_at=NOW, trace_id="trace:qq-unknown",
        )
        assert (await app.drain_actions_once()).status == "settled"
        await app.tick(
            tick_id="tick:qq-unknown", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=121),
            observed_at=NOW + timedelta(seconds=121), trace_id="trace:qq-unknown-tick",
            causation_id="scheduler:test", correlation_id="conversation:qq-unknown",
            reason="test_qq_unknown_gap",
        )
        assert (await app.drain_actions_once()).status == "marked_unknown"
        assert await app.drain_background_once() is None
        assert proactive.calls == 0
    finally:
        app.close()


@pytest.mark.asyncio
async def test_persisted_qq_provider_acceptance_opens_response_gap_after_restart(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "response-gap-qq-restart.sqlite3"
    config = WorldV2TurnApplicationConfig(
        world_id="world:response-gap-qq-restart", companion_actor_ref="actor:companion",
        reply_target="user:primary", action_pump_owner="worker:actions",
        social_initiative_policy=SocialInitiativePolicy(
            spontaneous_idle_seconds=3_600, spontaneous_expiry_seconds=7_200,
        ),
    )
    chat = ChatModelDeliberationAdapter(model=_ResponseExpectingChat())
    first = build_sqlite_world_v2_turn_application(
        path=path, config=config, identities=_Identities(), router=_Router(), main_model=chat,
        quick_recovery=chat,
        transport=QQC2CPlatformTransport(
            delivery=_QQDelivery(),  # type: ignore[arg-type]
            recipients_by_target={"user:primary": "qq-user-1"}, now=lambda: NOW,
        ),
        proactive_model=_DraftModel("silent"), now=NOW,
    )
    try:
        await first.inbound(
            platform="http", platform_user_id="user.1",
            platform_message_id="message:qq-restart", text="我先忙一下",
            observed_at=NOW, trace_id="trace:qq-restart",
        )
        assert (await first.drain_actions_once()).status == "settled"
    finally:
        first.close()

    restarted_proactive = _DraftModel("silent")
    restarted = build_sqlite_world_v2_turn_application(
        path=path, config=config, identities=_Identities(), router=_Router(), main_model=chat,
        quick_recovery=chat,
        transport=QQC2CPlatformTransport(
            delivery=_QQDelivery(),  # type: ignore[arg-type]
            recipients_by_target={"user:primary": "qq-user-1"}, now=lambda: NOW,
        ),
        proactive_model=restarted_proactive, now=NOW,
    )
    try:
        await restarted.tick(
            tick_id="tick:qq-restart", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61), trace_id="trace:qq-restart-tick",
            causation_id="scheduler:test", correlation_id="conversation:qq-restart",
            reason="test_qq_restart_gap",
        )
        opened = await restarted.drain_background_once()
        assert opened is not None and opened.status == "opened"
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
        identities=_Identities(), router=_Router(), main_model=chat,
        quick_recovery=chat, transport=transport, proactive_model=proactive, now=NOW,
    )
    try:
        await app.inbound(
            platform="http", platform_user_id="user.1",
            platform_message_id="message:no-expectation", text="你在吗？",
            observed_at=NOW, trace_id="trace:no-expectation",
        )
        await app.drain_actions_once()
        await app.tick(
            tick_id="tick:no-response-gap", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61), trace_id="trace:no-response-gap",
            causation_id="scheduler:test", correlation_id="conversation:no-response-gap",
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
            companion_actor_ref="actor:companion", reply_target="user:primary",
            action_pump_owner="worker:actions",
            social_initiative_policy=SocialInitiativePolicy(
                spontaneous_idle_seconds=60, spontaneous_expiry_seconds=3_600,
            ),
        ),
        identities=_Identities(), router=_Router(), main_model=chat,
        quick_recovery=chat, transport=transport, proactive_model=proactive, now=NOW,
    )
    try:
        await app.inbound(
            platform="http", platform_user_id="user.1",
            platform_message_id="message:failed-source", text="我去忙了",
            observed_at=NOW, trace_id="trace:failed-source",
        )
        await app.drain_actions_once()
        await app.tick(
            tick_id="tick:failed-contact", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=61),
            observed_at=NOW + timedelta(seconds=61), trace_id="trace:failed-contact",
            causation_id="scheduler:test", correlation_id="conversation:failed-contact",
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
