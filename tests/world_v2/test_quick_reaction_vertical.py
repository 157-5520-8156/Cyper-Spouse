"""Vertical tests for the same-turn quick reaction lane.

Covered acceptance points: the semantic gate's social-safety boundary, the
recorded act/hold restraint draw, per-observation dedup, abandoning when the
visible reply already reached the provider, silent local-model failure, and
the end-to-end ordering guarantee that the reaction reaches the provider
before the main reply model is even consulted.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

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
from companion_daemon.world_v2.expression_draft import (
    QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    materialize_expression_draft,
)
from companion_daemon.world_v2.expression_plan_acceptance import ExpressionPlanBudgetPolicy
from companion_daemon.world_v2.expression_plan_atomic_recorder import (
    ExpressionPlanAtomicRecorder,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import (
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.minimal_reply_acceptance import ReplyBudgetPolicy
from companion_daemon.world_v2.minimal_reply_atomic_recorder import (
    MinimalReplyAtomicRecorder,
)
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.production_proposal_grammar import (
    ProductionProposalGrammarError,
    production_proposal_grammar,
)
from companion_daemon.world_v2.quick_reaction import (
    QUICK_REACTION_PROPOSAL_PREFIX,
    QuickReactionContextPolicy,
    QuickReactionPolicy,
    QuickReactionSemanticGate,
    QuickReactionWorker,
    materialize_quick_reaction_proposal,
    parse_quick_reaction_verdict,
    quick_reaction_attempt_id,
)
from companion_daemon.world_v2.random_authority import RandomAuthority
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    BudgetAccount,
    Observation,
    ProviderReceipt,
    WorldEvent,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
WORLD = "world:quick-reaction"
TARGET = "user:primary"
REACTION_OPTION_IDS = frozenset(
    item.option_id for item in QQ_NAPCAT_EXPRESSION_CAPABILITIES.reaction_options
)


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _ExpressionReplyModel:
    """Main chat model producing one ordinary text-reply DecisionProposal."""

    def __init__(self) -> None:
        self.calls = 0
        self.first_called_at: float | None = None

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.calls += 1
        if self.first_called_at is None:
            self.first_called_at = time.monotonic()
        proposal = materialize_expression_draft(
            value={
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "恭喜！写完最后一章最难得。"}],
                "stance": "celebrate_briefly",
                "brief_rationale": "Share the small win with one warm sentence.",
            },
            request=request,
            capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        )
        return ModelOutput(
            model_id="test-expression-main",
            model_version="test.1",
            raw_proposal=proposal.model_dump(mode="json"),
            input_tokens=1,
            output_tokens=1,
        )


class _NoRecoveryModel:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        raise AssertionError("quick recovery must not be consulted in this test")


class _GateModel:
    """Local model fixture for the quick-reaction semantic gate."""

    model = "fixture:quick-reaction-gate"

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls = 0
        self.last_messages: list[dict[str, str]] | None = None

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        self.last_messages = messages
        return self.output


class _BrokenGateModel(_GateModel):
    def __init__(self) -> None:
        super().__init__("")

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del messages, temperature
        self.calls += 1
        raise RuntimeError("local endpoint is down")


class _HangingGateModel(_GateModel):
    def __init__(self) -> None:
        super().__init__("")

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del messages, temperature
        self.calls += 1
        await asyncio.sleep(30)
        return '{"react":true,"reaction_id":"haha"}'


class _RecordingExecutor:
    """Provider port recording dispatch order; sync-accept like NapCat."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, str, float]] = []

    async def dispatch(self, action) -> ProviderReceipt:  # type: ignore[no-untyped-def]
        self.dispatched.append((action.kind, action.action_id, time.monotonic()))
        return ProviderReceipt(
            provider_receipt_id=f"receipt:{action.action_id}",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider="fixture:qq",
            provider_ref=f"provider:{action.action_id}",
            status="provider_accepted",
            artifact_refs=(),
            cost_actual=1,
            received_at=NOW,
            raw_payload_hash="f" * 64,
        )

    async def lookup_result(self, action) -> None:  # type: ignore[no-untyped-def]
        del action
        return None


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:quick-reaction:setup",
        causation_id=event_id,
        correlation_id="correlation:quick-reaction",
        idempotency_key=event_id,
        payload=payload,
    )


def _observation(*, suffix: str = "1", text: str = "终于把最后一章写完啦！") -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id=f"observation:quick:{suffix}",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id=f"trace:quick:{suffix}",
        causation_id=f"inbound:{suffix}",
        correlation_id="conversation:quick",
        source="test",
        source_event_id=f"message:{suffix}",
        actor="user:primary",
        channel="test",
        payload_ref=f"payload:source:{suffix}",
        payload_hash="sha256:" + suffix[0] * 64,
        text=text,
        received_at=NOW,
        reply_context={"target": TARGET, "platform_message_id": f"qq-msg-{suffix}"},
    )


def _observation_event_id(observation: Observation) -> str:
    return f"event:trigger:observation:{observation.source}:{observation.source_event_id}"


def _empty_projection_view() -> SimpleNamespace:
    return SimpleNamespace(affect_episodes=(), plans=(), actions=())


def _base_act_bp_for(*, source_event_ref: str, want: str) -> int:
    """Deterministically pick a base act mass whose recorded draw lands as asked.

    The draw is a pure function of world id, seed instant, attempt identity and
    normalized weights, so a scratch ledger replays exactly what the production
    worker will draw for a fresh world with no mood/activity/backoff signals.
    """

    for base in range(2_500, 4_001, 25):
        policy = QuickReactionPolicy(base_act_bp=base)
        profile = QuickReactionContextPolicy(policy=policy).compile(
            projection=_empty_projection_view(), logical_time=NOW
        )
        scratch = WorldLedger.in_memory(world_id=WORLD)
        scratch.commit(
            (_event("event:scratch:start", "WorldStarted", {}),),
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
        draw = RandomAuthority(ledger=scratch, source="world-v2:quick-reaction-random").draw(
            attempt_id=quick_reaction_attempt_id(
                source_event_ref=source_event_ref, profile=profile
            ),
            candidate_refs=("act", "hold"),
            candidate_weights=profile.candidate_weights,
            weight_policy_version=QuickReactionContextPolicy.version,
            catalog_version="quick-reaction-act-hold.1",
            logical_time=NOW,
            seed_instant=NOW,
            actor="system:quick-reaction",
            trace_id="trace:scratch",
            correlation_id="correlation:scratch",
        )
        if draw.selected_candidate_ref == want:
            return base
    raise AssertionError(f"no base weight in range draws {want!r} for {source_event_ref}")


def _build_runtime(
    *,
    gate_model: _GateModel,
    base_act_bp: int,
    gate_timeout_seconds: float = 1.0,
    with_quick_worker: bool = True,
) -> tuple[WorldRuntime, QuickReactionWorker, _RecordingExecutor, _ExpressionReplyModel, WorldLedger]:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit(
        (_event("event:quick:start", "WorldStarted", {}),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    projection = ledger.project()
    ledger.commit(
        (
            _event(
                "event:quick:budget",
                "BudgetAccountConfigured",
                {
                    "account": BudgetAccount(
                        account_id="account:chat",
                        category="chat",
                        window_id="window:1",
                        limit=100,
                    ).model_dump(mode="json")
                },
            ),
        ),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    expression_policy = ExpressionPlanBudgetPolicy(
        account_id="account:chat",
        amount_limit_per_action=2,
        actor="agent:companion",
        allowed_targets=(TARGET,),
        recovery_policy="effect_once",
    )
    expression_recorder = ExpressionPlanAtomicRecorder(batch_issuer=issuer)
    executor = _RecordingExecutor()
    worker = QuickReactionWorker(
        ledger=ledger,
        model=gate_model,
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        expression_policy=expression_policy,
        expression_recorder=expression_recorder,
        executor=executor,
        pump_owner="pump:quick-reaction-test",
        policy=QuickReactionPolicy(
            base_act_bp=base_act_bp, gate_timeout_seconds=gate_timeout_seconds
        ),
    )
    reply_model = _ExpressionReplyModel()
    capsules: ContextCapsuleCompiler = context_capsule_compiler_from_ledger(ledger=ledger)
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=capsules,
        deliberation=Deliberation(
            router=_Router(), main_model=reply_model, quick_recovery=_NoRecoveryModel()
        ),
        companion_actor_ref="agent:companion",
    )
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        pinned_turn=turn,
        # The ingest acceptance branch is nested under the minimal reply
        # policy exactly as in production composition; both lanes share the
        # chat budget account.
        reply_policy=ReplyBudgetPolicy(
            account_id="account:chat",
            amount_limit=2,
            actor="agent:companion",
            target=TARGET,
            recovery_policy="effect_once",
        ),
        reply_recorder=MinimalReplyAtomicRecorder(batch_issuer=issuer),
        expression_policy=expression_policy,
        expression_recorder=expression_recorder,
        action_executor=executor,
        action_pump_owner="pump:quick-reaction-test",
        quick_reaction_worker=worker if with_quick_worker else None,
    )
    return runtime, worker, executor, reply_model, ledger


# ---------------------------------------------------------------------------
# Semantic gate: social-safety boundary and strict verdict contract
# ---------------------------------------------------------------------------


def test_gate_prompt_spells_out_the_social_safety_boundary() -> None:
    gate = QuickReactionSemanticGate(
        model=_GateModel('{"react":false}'),
        options=(("haha", "laughing"), ("like", "approval")),
        timeout_seconds=0.8,
    )
    system = gate.messages(text="随便")[0]["content"]
    # The boundary the coordinator required, verbatim concerns: distress,
    # anger/conflict, serious matters, and fail-closed uncertainty.
    for marker in ("痛苦", "生气", "争执", "严肃", "拿不准", '{"react":false}'):
        assert marker in system
    # The closed catalog is enumerated for the model, token by token.
    assert "- haha（laughing）" in system and "- like（approval）" in system


def test_gate_verdict_parsing_is_closed_over_the_installed_catalog() -> None:
    options = frozenset({"haha", "like"})
    assert parse_quick_reaction_verdict('{"react":false}', option_ids=options) is None
    assert (
        parse_quick_reaction_verdict(
            '{"react":true,"reaction_id":"haha"}', option_ids=options
        )
        == "haha"
    )
    # Out-of-catalog tokens, malformed JSON, and non-boolean shapes all fail closed.
    assert (
        parse_quick_reaction_verdict(
            '{"react":true,"reaction_id":"thumbsup"}', option_ids=options
        )
        is None
    )
    assert parse_quick_reaction_verdict('{"react":"yes"}', option_ids=options) is None
    assert parse_quick_reaction_verdict("haha", option_ids=options) is None
    assert parse_quick_reaction_verdict(None, option_ids=options) is None
    assert parse_quick_reaction_verdict('{"react":true}', option_ids=options) is None


@pytest.mark.asyncio
async def test_gate_declines_serious_content_and_no_reaction_is_authorized() -> None:
    observation = _observation(suffix="2", text="我真的撑不住了，今天全都搞砸了。")
    base = _base_act_bp_for(
        source_event_ref=_observation_event_id(observation), want="act"
    )
    gate_model = _GateModel('{"react":false}')
    runtime, _worker, executor, reply_model, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=base
    )

    outcome = await runtime.ingest(observation)

    assert outcome.status == "action_authorized"
    assert gate_model.calls == 1
    projection = ledger.project()
    assert all(action.kind != "reaction" for action in projection.actions)
    assert not any(
        audit.proposal_id.startswith(QUICK_REACTION_PROPOSAL_PREFIX)
        for audit in projection.proposal_audits
    )
    # The act draw itself remains recorded, auditable restraint evidence.
    assert any(
        ref.event_type == "RandomDrawRecorded"
        for ref in projection.committed_world_event_refs
    )
    assert reply_model.calls == 1


# ---------------------------------------------------------------------------
# Weighted restraint draw
# ---------------------------------------------------------------------------


def test_context_policy_weights_follow_mood_activity_and_backoff() -> None:
    policy = QuickReactionContextPolicy(policy=QuickReactionPolicy(base_act_bp=3_000))

    def compile_for(**kwargs) -> tuple[int, tuple[str, ...]]:
        view = _empty_projection_view()
        for key, value in kwargs.items():
            setattr(view, key, value)
        profile = policy.compile(projection=view, logical_time=NOW)
        return profile.candidate_weights["act"], profile.reason_codes

    neutral_act, neutral_reasons = compile_for()
    assert neutral_act == 3_000
    assert {"mood:neutral", "activity:available", "backoff:none"} <= set(neutral_reasons)

    episode = lambda dimension, intensity: SimpleNamespace(  # noqa: E731
        status="active",
        components=(SimpleNamespace(dimension=dimension, intensity_bp=intensity),),
    )
    joyful_act, joyful_reasons = compile_for(affect_episodes=(episode("joy", 6_000),))
    assert joyful_act > neutral_act and "mood:approach" in joyful_reasons

    guarded_act, guarded_reasons = compile_for(affect_episodes=(episode("anger", 6_000),))
    assert guarded_act < neutral_act // 2 + 1 and "mood:guarded" in guarded_reasons

    plan = lambda kind: SimpleNamespace(status="active", activity_kind=kind)  # noqa: E731
    phone_act, phone_reasons = compile_for(plans=(plan("leisure.digital_browse"),))
    assert phone_act > neutral_act and "activity:on_phone" in phone_reasons

    study_act, study_reasons = compile_for(plans=(plan("study.essay_writing"),))
    assert study_act < neutral_act // 2 + 1 and "activity:focused" in study_reasons

    recent = SimpleNamespace(kind="reaction", state="delivered", logical_time=NOW)
    backoff_act, backoff_reasons = compile_for(actions=(recent,))
    assert backoff_act == neutral_act // 2
    assert "backoff:recent_reaction" in backoff_reasons

    old = SimpleNamespace(
        kind="reaction",
        state="delivered",
        logical_time=NOW.replace(hour=10),
    )
    old_act, _ = compile_for(actions=(old,))
    assert old_act == neutral_act


@pytest.mark.asyncio
async def test_hold_draw_never_consults_the_local_model_and_replays_itself() -> None:
    observation = _observation(suffix="3", text="今天路过的猫超可爱。")
    base = _base_act_bp_for(
        source_event_ref=_observation_event_id(observation), want="hold"
    )
    gate_model = _GateModel('{"react":true,"reaction_id":"haha"}')
    runtime, worker, _executor, _reply_model, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=base
    )

    outcome = await runtime.ingest(observation)
    assert outcome.status == "action_authorized"
    assert gate_model.calls == 0
    projection = ledger.project()
    assert all(action.kind != "reaction" for action in projection.actions)

    # A direct retry joins the recorded draw instead of re-rolling the impulse.
    located = ledger.lookup_event_commit(_observation_event_id(observation))
    assert located is not None
    replay = await worker.run_observation(
        observation=observation.model_copy(
            update={"logical_time": located[0].logical_time}
        ),
        observation_event=located[0],
        source_world_revision=located[1].world_revision,
    )
    assert replay.status == "held"
    assert gate_model.calls == 0


# ---------------------------------------------------------------------------
# End to end: reaction lands first, text reply follows unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quick_reaction_reaches_provider_before_the_reply_model_is_consulted() -> None:
    observation = _observation(suffix="4")
    base = _base_act_bp_for(
        source_event_ref=_observation_event_id(observation), want="act"
    )
    gate_model = _GateModel('{"react":true,"reaction_id":"haha"}')
    runtime, worker, executor, reply_model, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=base
    )

    outcome = await runtime.ingest(observation)

    assert outcome.status == "action_authorized"
    assert len(outcome.authorized_action_ids) == 1
    reply_action_id = outcome.authorized_action_ids[0]

    # The reaction was dispatched during the turn, before the reply model ran.
    assert [item[0] for item in executor.dispatched] == ["reaction"]
    reaction_dispatched_at = executor.dispatched[0][2]
    assert reply_model.first_called_at is not None
    assert reaction_dispatched_at < reply_model.first_called_at

    projection = ledger.project()
    reaction_action = next(item for item in projection.actions if item.kind == "reaction")
    assert reaction_action.state == "provider_accepted"
    reaction_payload = next(
        item
        for item in projection.stored_message_payloads
        if item.content_type == "application/vnd.world-v2.reaction+json"
    )
    assert json.loads(reaction_payload.text) == {
        "provider_message_id": "qq-msg-4",
        "reaction_id": "haha",
        "version": "expression-reaction.1",
    }
    audits = [
        audit
        for audit in projection.proposal_audits
        if audit.trigger_ref == _observation_event_id(observation)
    ]
    assert sorted(
        audit.proposal_id.startswith(QUICK_REACTION_PROPOSAL_PREFIX) for audit in audits
    ) == [False, True]

    # The ordinary reply is untouched: authorized in the same turn, dispatched
    # by the host's targeted drain afterwards.
    drained = await runtime.drain_action(reply_action_id)
    assert drained is not None and drained.status == "settled"
    assert [item[0] for item in executor.dispatched] == ["reaction", "reply"]
    reply_action = next(
        item for item in ledger.project().actions if item.action_id == reply_action_id
    )
    assert reply_action.state == "provider_accepted"

    # Idempotent ingress retry still reports the reply lane, not the reaction.
    replay = await runtime.ingest(observation)
    assert replay.status == "action_authorized"
    assert replay.authorized_action_ids == (reply_action_id,)

    # Per-observation dedup: a direct second attempt is refused durably.
    located = ledger.lookup_event_commit(_observation_event_id(observation))
    assert located is not None
    duplicate = await worker.run_observation(
        observation=observation.model_copy(
            update={"logical_time": located[0].logical_time}
        ),
        observation_event=located[0],
        source_world_revision=located[1].world_revision,
    )
    assert duplicate.status == "duplicate"
    assert gate_model.calls == 1


# ---------------------------------------------------------------------------
# Failure modes stay silent and never block the visible turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_model_failure_gives_up_silently_and_reply_is_unaffected() -> None:
    observation = _observation(suffix="5")
    base = _base_act_bp_for(
        source_event_ref=_observation_event_id(observation), want="act"
    )
    gate_model = _BrokenGateModel()
    runtime, _worker, executor, reply_model, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=base
    )

    outcome = await runtime.ingest(observation)

    assert outcome.status == "action_authorized"
    assert gate_model.calls == 1
    assert executor.dispatched == []
    assert reply_model.calls == 1
    projection = ledger.project()
    assert all(action.kind != "reaction" for action in projection.actions)
    assert not any(
        audit.proposal_id.startswith(QUICK_REACTION_PROPOSAL_PREFIX)
        for audit in projection.proposal_audits
    )


@pytest.mark.asyncio
async def test_hanging_local_model_is_cut_off_by_the_gate_budget() -> None:
    observation = _observation(suffix="6")
    base = _base_act_bp_for(
        source_event_ref=_observation_event_id(observation), want="act"
    )
    gate_model = _HangingGateModel()
    runtime, _worker, executor, reply_model, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=base, gate_timeout_seconds=0.05
    )

    outcome = await runtime.ingest(observation)

    assert outcome.status == "action_authorized"
    assert executor.dispatched == []
    assert reply_model.calls == 1
    assert all(action.kind != "reaction" for action in ledger.project().actions)


@pytest.mark.asyncio
async def test_quick_reaction_is_abandoned_once_the_reply_reached_the_provider() -> None:
    observation = _observation(suffix="7")
    base = _base_act_bp_for(
        source_event_ref=_observation_event_id(observation), want="act"
    )
    gate_model = _GateModel('{"react":true,"reaction_id":"like"}')
    # The worker exists but is not hooked into ingest, simulating recovery
    # paths that might attempt a reaction after the reply already went out.
    runtime, worker, executor, _reply_model, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=base, with_quick_worker=False
    )

    outcome = await runtime.ingest(observation)
    assert outcome.status == "action_authorized"
    drained = await runtime.drain_action(outcome.authorized_action_ids[0])
    assert drained is not None and drained.status == "settled"
    assert [item[0] for item in executor.dispatched] == ["reply"]

    located = ledger.lookup_event_commit(_observation_event_id(observation))
    assert located is not None
    result = await worker.run_observation(
        observation=observation.model_copy(
            update={"logical_time": located[0].logical_time}
        ),
        observation_event=located[0],
        source_world_revision=located[1].world_revision,
    )

    assert result.status == "abandoned_reply_delivered"
    assert gate_model.calls == 0
    assert [item[0] for item in executor.dispatched] == ["reply"]


# ---------------------------------------------------------------------------
# Grammar closure
# ---------------------------------------------------------------------------


def test_quick_reaction_grammar_is_closed_to_one_reaction_action() -> None:
    grammar = production_proposal_grammar("quick_reaction")
    observation = _observation(suffix="8")
    event = _event(
        _observation_event_id(observation),
        "ObservationRecorded",
        {"observation": observation.model_dump(mode="json")},
    )
    proposal = materialize_quick_reaction_proposal(
        observation=observation,
        observation_event=event,
        source_world_revision=1,
        evaluated_world_revision=3,
        reply_target=TARGET,
        provider_message_id="qq-msg-8",
        reaction_id="haha",
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        recorded_draw_ref="draw:test",
    )
    grammar.validate(proposal)
    assert proposal.proposal_id.startswith(QUICK_REACTION_PROPOSAL_PREFIX)
    assert [intent.kind for intent in proposal.action_intents] == ["reaction"]

    from companion_daemon.world_v2.deliberation import TriggerMessage

    request = ModelInput(
        call_id="model-call:grammar:1",
        attempt_id="attempt:grammar:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref=event.event_id,
        evaluated_world_revision=3,
        model_content_json=json.dumps({"logical_time": NOW.isoformat()}),
        trigger_message=TriggerMessage(
            event_ref=event.event_id,
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref=observation.observation_id,
            source_world_revision=1,
            actor="user:primary",
            channel="test",
            reply_target=TARGET,
            platform_message_id="qq-msg-8",
            text=observation.text,
        ),
    )
    text_reply = materialize_expression_draft(
        value={
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "好呀。"}],
            "stance": "acknowledge",
            "brief_rationale": "A text reply is outside the quick reaction lane.",
        },
        request=request,
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )
    with pytest.raises(ProductionProposalGrammarError):
        grammar.validate(text_reply)
    sticker = materialize_expression_draft(
        value={
            "timing_choice": "now",
            "beats": [{"modality": "sticker", "sticker_id": "qq-face:14"}],
            "stance": "acknowledge",
            "brief_rationale": "A sticker is outside the quick reaction lane.",
        },
        request=request,
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )
    with pytest.raises(ProductionProposalGrammarError):
        grammar.validate(sticker)


# ---------------------------------------------------------------------------
# Host-level end to end: fake NapCat receives the reaction, reply still lands
# ---------------------------------------------------------------------------


class _NapCatDelivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {"status": "ok", "data": {"message_id": f"qq-{len(self.sent)}"}}

    async def send_reaction(
        self, recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        self.sent.append((recipient_id, f"reaction:{message_id}:{reaction_id}"))
        return {"status": "ok", "data": {"message_id": f"reaction-{len(self.sent)}"}}

    async def send_sticker(self, recipient_id: str, *, sticker_id: str) -> dict[str, object]:
        self.sent.append((recipient_id, f"sticker:{sticker_id}"))
        return {"status": "ok", "data": {"message_id": f"sticker-{len(self.sent)}"}}

    async def send_typing(self, recipient_id: str, *, state: str) -> dict[str, object]:
        self.sent.append((recipient_id, f"typing:{state}"))
        return {"status": "ok", "data": {"message_id": f"typing-{len(self.sent)}"}}


class _HostExpressionModel:
    """Raw ExpressionDraft model for the single-call cognition seam."""

    model = "fixture:host-expression"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del messages, temperature
        self.calls += 1
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "写完啦？恭喜恭喜，等你细讲。"}],
                "stance": "celebrate_briefly",
                "brief_rationale": "Celebrate the finished chapter with one line.",
            },
            ensure_ascii=False,
        )


def _napcat_message_id_that_draws_act(*, world_id: str, recipient_id: str) -> str:
    """Pick a provider message id whose recorded quick-reaction draw acts.

    The host coalesces one fragment into a deterministic batch identity, and
    the draw is a pure function of world id, seed instant, attempt identity
    and normalized weights — so the test replays the exact production draw on
    scratch state instead of hoping for a lucky roll.
    """

    from companion_daemon.world_v2.qq_ingress_policy import (
        QQIngressPolicyCatalog,
        _digest as _ingress_digest,
    )

    catalog = QQIngressPolicyCatalog()
    profile = QuickReactionContextPolicy(policy=QuickReactionPolicy()).compile(
        projection=_empty_projection_view(), logical_time=NOW
    )
    for index in range(64):
        message_id = f"onebot-quick-{index}"
        identity = _ingress_digest(
            {
                "recipient_id": recipient_id,
                "sources": (message_id,),
                "policy": catalog.digest,
            }
        )
        source_event_ref = (
            "event:trigger:observation:platform:qq:"
            f"qq:{recipient_id}:qq-coalesced:{identity}"
        )
        scratch = WorldLedger.in_memory(world_id=world_id)
        scratch.commit(
            (
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id="event:scratch:qq:start",
                    world_id=world_id,
                    event_type="WorldStarted",
                    logical_time=NOW,
                    created_at=NOW,
                    actor="system:test",
                    source="test",
                    trace_id="trace:scratch",
                    causation_id="cause:scratch",
                    correlation_id="correlation:scratch",
                    idempotency_key="scratch:start",
                    payload={},
                ),
            ),
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
        draw = RandomAuthority(ledger=scratch, source="world-v2:quick-reaction-random").draw(
            attempt_id=quick_reaction_attempt_id(
                source_event_ref=source_event_ref, profile=profile
            ),
            candidate_refs=("act", "hold"),
            candidate_weights=profile.candidate_weights,
            weight_policy_version=QuickReactionContextPolicy.version,
            catalog_version="quick-reaction-act-hold.1",
            logical_time=NOW,
            seed_instant=NOW,
            actor="system:quick-reaction",
            trace_id="trace:scratch",
            correlation_id="correlation:scratch",
        )
        if draw.selected_candidate_ref == "act":
            return message_id
    raise AssertionError("no candidate message id draws act")


@pytest.mark.asyncio
async def test_qq_host_delivers_the_quick_reaction_before_the_ordinary_reply(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    from companion_daemon.config import Settings
    from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host

    delivery = _NapCatDelivery()
    model = _HostExpressionModel()
    gate_model = _GateModel('{"react":true,"reaction_id":"heart"}')
    from companion_daemon.llm import FakeCompanionModel

    message_id = _napcat_message_id_that_draws_act(
        world_id="world:companion-v2:qq-c2c:geoff", recipient_id="10001"
    )
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "qq-quick-reaction.sqlite",
            QQ_ADAPTER="napcat",
            PRIMARY_USER_ID="geoff",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=delivery,
        quick_reaction_model=gate_model,
    )
    try:
        result = await host.inbound_text(
            message_id=message_id,
            recipient_id="10001",
            text="终于把最后一章写完啦！",
            observed_at=NOW,
        )
        projection = host._host._application._ledger.project()  # type: ignore[attr-defined]
    finally:
        await host.aclose()

    assert result.status == "action_authorized"
    assert gate_model.calls == 1
    assert model.calls == 1
    visible = [item for item in delivery.sent if not item[1].startswith("typing:")]
    # The reaction binds the exact inbound provider message and lands before
    # the ordinary text reply.
    assert visible[0] == ("10001", f"reaction:{message_id}:heart")
    assert visible[1][0] == "10001" and "恭喜" in visible[1][1]
    assert len(visible) == 2
    reaction_action = next(item for item in projection.actions if item.kind == "reaction")
    reply_action = next(item for item in projection.actions if item.kind == "reply")
    assert reaction_action.state == "provider_accepted"
    assert reply_action.state == "provider_accepted"
