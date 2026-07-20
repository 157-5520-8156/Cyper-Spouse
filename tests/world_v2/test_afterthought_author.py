"""Vertical tests for the afterthought lane (v1 "事后补充话" port).

Covered acceptance points: the recorded act/hold impulse draw bound to the
reply receipt, the joint mode+delay draw inside the v1 windows, the strict
gate JSON contract, terminal one-consideration-per-reply semantics, voiding
when the user answers inside the window, restraint against stacking on a
pending initiative, and the end-to-end authorized ``followup`` Action whose
due window the generic ActionPump owns.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.afterthought_author import (
    AFTERTHOUGHT_PROPOSAL_PREFIX,
    AfterthoughtAuthorRuntime,
    AfterthoughtContextPolicy,
    AfterthoughtPolicy,
    afterthought_attempt_id,
    parse_afterthought_verdict,
)
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
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.minimal_reply_acceptance import ReplyBudgetPolicy
from companion_daemon.world_v2.minimal_reply_atomic_recorder import (
    MinimalReplyAtomicRecorder,
)
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.random_authority import RandomAuthority
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    BudgetAccount,
    ClockObservation,
    Observation,
    ProviderReceipt,
    WorldEvent,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
WORLD = "world:afterthought"
TARGET = "user:primary"
COMPANION = "agent:companion"
CHRONOLOGY = LocalChronology("Asia/Shanghai")
REPLY_TEXT = "刚看完你说的那部片子，结尾比我想的温柔多了，我还坐着缓了一会儿。"


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="test", router_version="test.1")


class _ExpressionReplyModel:
    """Main chat model producing one ordinary text-reply DecisionProposal."""

    async def propose(self, request: ModelInput) -> ModelOutput:
        proposal = materialize_expression_draft(
            value={
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": REPLY_TEXT}],
                "stance": "share_reaction",
                "brief_rationale": "Answer the film question with her own reaction.",
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


class _EchoGateModel:
    """Afterthought gate fixture echoing the drawn mode from the user frame."""

    model = "fixture:afterthought-gate"

    def __init__(self, *, text: str = "对了，片尾曲也好听，你别跳。", decline: bool = False) -> None:
        self.text = text
        self.decline = decline
        self.calls = 0
        self.last_messages: list[dict[str, str]] | None = None

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        self.last_messages = messages
        if self.decline:
            return '{"afterthought":false}'
        mode = json.loads(messages[-1]["content"])["mode"]
        return json.dumps(
            {"afterthought": True, "mode": mode, "text": self.text}, ensure_ascii=False
        )


class _AcceptingExecutor:
    """Provider port that synchronously accepts every dispatched Action."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[str, str]] = []

    async def dispatch(self, action) -> ProviderReceipt:  # type: ignore[no-untyped-def]
        self.dispatched.append((action.kind, action.action_id))
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
        trace_id="trace:afterthought:setup",
        causation_id=event_id,
        correlation_id="correlation:afterthought",
        idempotency_key=event_id,
        payload=payload,
    )


def _observation(*, suffix: str = "1", text: str = "那部片子你看完了吗？") -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id=f"observation:afterthought:{suffix}",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id=f"trace:afterthought:{suffix}",
        causation_id=f"inbound:{suffix}",
        correlation_id="conversation:afterthought",
        source="test",
        source_event_id=f"message:{suffix}",
        actor=TARGET,
        channel="test",
        payload_ref=f"payload:source:{suffix}",
        payload_hash="sha256:" + suffix[0] * 64,
        text=text,
        received_at=NOW,
        reply_context={"target": TARGET, "platform_message_id": f"qq-msg-{suffix}"},
    )


def _neutral_view(**overrides) -> SimpleNamespace:
    view = SimpleNamespace(affect_episodes=(), plans=(), actions=())
    for key, value in overrides.items():
        setattr(view, key, value)
    return view


def _profile_for(policy: AfterthoughtPolicy, *, reply_text: str = REPLY_TEXT):
    return AfterthoughtContextPolicy(policy=policy, chronology=CHRONOLOGY).compile(
        projection=_neutral_view(), logical_time=NOW, reply_text=reply_text
    )


def _base_act_bp_for(*, receipt_event_ref: str, want: str) -> int:
    """Deterministically pick a base act mass whose recorded draw lands as asked.

    The draw is a pure function of world id, seed instant, attempt identity
    and normalized weights, so a scratch ledger replays exactly what the lane
    will draw for this receipt in a fresh world.
    """

    for base in range(1_500, 4_001, 25):
        policy = AfterthoughtPolicy(base_act_bp=base)
        profile = _profile_for(policy)
        scratch = WorldLedger.in_memory(world_id=WORLD)
        scratch.commit(
            (_event("event:scratch:start", "WorldStarted", {}),),
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
        draw = RandomAuthority(ledger=scratch, source="world-v2:afterthought-random").draw(
            attempt_id=afterthought_attempt_id(
                receipt_event_ref=receipt_event_ref, profile=profile
            ),
            candidate_refs=("act", "hold"),
            candidate_weights=profile.candidate_weights,
            weight_policy_version=AfterthoughtContextPolicy.version,
            catalog_version="afterthought-act-hold.1",
            logical_time=NOW,
            seed_instant=NOW,
            actor="system:afterthought",
            trace_id="trace:scratch",
            correlation_id="correlation:scratch",
        )
        if draw.selected_candidate_ref == want:
            return base
    raise AssertionError(f"no base weight in range draws {want!r} for {receipt_event_ref}")


def _receipt_event_ref_for(observation: Observation) -> str:
    """The reply receipt settlement event id is deterministic per source message."""

    del observation  # identity flows through the executor fixture below
    return ""


def _build_runtime(
    *,
    gate_model: _EchoGateModel,
    base_act_bp: int,
    afterthought_policy: AfterthoughtPolicy | None = None,
) -> tuple[WorldRuntime, AfterthoughtAuthorRuntime, _AcceptingExecutor, WorldLedger]:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit(
        (_event("event:afterthought:start", "WorldStarted", {}),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    projection = ledger.project()
    ledger.commit(
        (
            _event(
                "event:afterthought:chat-budget",
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
            _event(
                "event:afterthought:proactive-budget",
                "BudgetAccountConfigured",
                {
                    "account": BudgetAccount(
                        account_id="account:proactive",
                        category="proactive",
                        window_id="window:1",
                        limit=100,
                    ).model_dump(mode="json")
                },
            ),
        ),
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    chat_policy = ExpressionPlanBudgetPolicy(
        account_id="account:chat",
        amount_limit_per_action=2,
        actor=COMPANION,
        allowed_targets=(TARGET,),
        recovery_policy="effect_once",
    )
    proactive_policy = ExpressionPlanBudgetPolicy(
        account_id="account:proactive",
        amount_limit_per_action=2,
        actor=COMPANION,
        allowed_targets=(TARGET,),
        recovery_policy="effect_once",
        category="proactive",
    )
    recorder = ExpressionPlanAtomicRecorder(batch_issuer=issuer)
    executor = _AcceptingExecutor()
    policy = afterthought_policy or AfterthoughtPolicy(base_act_bp=base_act_bp)
    author = AfterthoughtAuthorRuntime(
        ledger=ledger,
        model=gate_model,
        policy=proactive_policy,
        batch_issuer=issuer,
        owner_id="worker:afterthought-test",
        target=TARGET,
        companion_actor_ref=COMPANION,
        counterpart_actor_ref=TARGET,
        chronology=CHRONOLOGY,
        afterthought_policy=policy,
    )
    reply_model = _ExpressionReplyModel()
    capsules: ContextCapsuleCompiler = context_capsule_compiler_from_ledger(ledger=ledger)
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=capsules,
        deliberation=Deliberation(
            router=_Router(), main_model=reply_model, quick_recovery=_NoRecoveryModel()
        ),
        companion_actor_ref=COMPANION,
    )
    runtime = WorldRuntime(
        world_id=WORLD,
        ledger=ledger,
        pinned_turn=turn,
        reply_policy=ReplyBudgetPolicy(
            account_id="account:chat",
            amount_limit=2,
            actor=COMPANION,
            target=TARGET,
            recovery_policy="effect_once",
        ),
        reply_recorder=MinimalReplyAtomicRecorder(batch_issuer=issuer),
        expression_policy=chat_policy,
        expression_recorder=recorder,
        action_executor=executor,
        action_pump_owner="pump:afterthought-test",
        afterthought_author=author,
    )
    return runtime, author, executor, ledger


async def _settled_reply(runtime: WorldRuntime, ledger: WorldLedger, *, suffix: str = "1") -> str:
    """Ingest one user message, dispatch her reply, and return the receipt event id."""

    outcome = await runtime.ingest(_observation(suffix=suffix))
    assert outcome.status == "action_authorized"
    pumped = await runtime.drain_actions_once()
    assert pumped is not None and pumped.status == "settled"
    projection = ledger.project()
    reply = next(
        action
        for action in projection.actions
        if action.kind == "reply" and action.state == "provider_accepted"
    )
    receipt = next(
        item for item in projection.execution_receipts if item.action_id == reply.action_id
    )
    return (
        f"event:trigger:settlement:{receipt.provider}:{receipt.source_event_id}"
        ":execution-receipt"
    )


async def _enter_quick_window(runtime: WorldRuntime, *, seconds: int = 20) -> None:
    """Advance the durable clock into the afterthought consideration window."""

    outcome = await runtime.advance(
        ClockObservation(
            schema_version="world-v2.1",
            tick_id=f"tick:afterthought:{seconds}",
            world_id=WORLD,
            logical_time=NOW + timedelta(seconds=seconds),
            created_at=NOW + timedelta(seconds=seconds),
            trace_id="trace:afterthought:tick",
            causation_id="scheduler:test",
            correlation_id="correlation:afterthought",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=seconds),
            reason="test_afterthought_window",
        )
    )
    assert outcome.status in {"advanced", "observed_only", "noop"}


# ---------------------------------------------------------------------------
# Strict gate contract
# ---------------------------------------------------------------------------


def test_verdict_parsing_is_closed_over_mode_length_and_shape() -> None:
    ok = parse_afterthought_verdict(
        '{"afterthought":true,"mode":"quick_continue","text":"对了，片尾曲也好听。"}',
        drawn_mode="quick_continue",
        max_chars=120,
    )
    assert ok is not None and ok.mode == "quick_continue"

    # Explicit decline, mode mismatch, oversize, newline, and junk all fail closed.
    assert (
        parse_afterthought_verdict(
            '{"afterthought":false}', drawn_mode="quick_continue", max_chars=120
        )
        is None
    )
    assert (
        parse_afterthought_verdict(
            '{"afterthought":true,"mode":"topic_drift","text":"换个话题"}',
            drawn_mode="quick_continue",
            max_chars=120,
        )
        is None
    )
    assert (
        parse_afterthought_verdict(
            json.dumps(
                {"afterthought": True, "mode": "topic_drift", "text": "字" * 121},
                ensure_ascii=False,
            ),
            drawn_mode="topic_drift",
            max_chars=120,
        )
        is None
    )
    assert (
        parse_afterthought_verdict(
            '{"afterthought":true,"mode":"quick_continue","text":"两\n行"}',
            drawn_mode="quick_continue",
            max_chars=120,
        )
        is None
    )
    assert parse_afterthought_verdict("好呀", drawn_mode="quick_continue", max_chars=120) is None
    assert (
        parse_afterthought_verdict(
            '{"afterthought":"yes"}', drawn_mode="quick_continue", max_chars=120
        )
        is None
    )
    # Markdown fences around the object are tolerated, content stays strict.
    fenced = "```json\n{\"afterthought\":true,\"mode\":\"topic_drift\",\"text\":\"想起一件小事\"}\n```"
    parsed = parse_afterthought_verdict(fenced, drawn_mode="topic_drift", max_chars=120)
    assert parsed is not None and parsed.text == "想起一件小事"


# ---------------------------------------------------------------------------
# Weighted impulse profile
# ---------------------------------------------------------------------------


def test_context_policy_weights_follow_mood_reply_texture_and_daypart() -> None:
    policy = AfterthoughtPolicy(base_act_bp=2_000)
    context = AfterthoughtContextPolicy(policy=policy, chronology=CHRONOLOGY)

    def compile_for(*, logical_time: datetime = NOW, reply_text: str = "嗯嗯，好呀，那就这样说定了。", **overrides):
        profile = context.compile(
            projection=_neutral_view(**overrides),
            logical_time=logical_time,
            reply_text=reply_text,
        )
        return profile.candidate_weights["act"], profile.reason_codes

    neutral_act, neutral_reasons = compile_for()
    assert neutral_act == 2_000
    assert {"mood:neutral", "reply:moderate", "daypart:normal"} <= set(neutral_reasons)

    episode = lambda dimension, intensity: SimpleNamespace(  # noqa: E731
        status="active",
        components=(SimpleNamespace(dimension=dimension, intensity_bp=intensity),),
    )
    joy_act, joy_reasons = compile_for(affect_episodes=(episode("joy", 6_000),))
    assert joy_act > neutral_act and "mood:approach" in joy_reasons

    warm_act, _ = compile_for(affect_episodes=(episode("warmth", 3_500),))
    assert neutral_act < warm_act < joy_act

    guarded_act, guarded_reasons = compile_for(affect_episodes=(episode("anger", 6_000),))
    assert guarded_act < neutral_act // 2 + 1 and "mood:guarded" in guarded_reasons

    substantial_act, substantial_reasons = compile_for(reply_text=REPLY_TEXT)
    assert substantial_act > neutral_act and "reply:substantial" in substantial_reasons

    terse_act, terse_reasons = compile_for(reply_text="好。")
    assert terse_act < neutral_act and "reply:terse" in terse_reasons

    # 18:30 UTC is 02:30 in Asia/Shanghai: inside the 1:00-7:00 quiet hours.
    night_act, night_reasons = compile_for(
        logical_time=NOW.replace(hour=18, minute=30)
    )
    assert night_act == neutral_act // 2 and "daypart:late_night" in night_reasons


def test_delay_grids_stay_inside_the_v1_windows() -> None:
    policy = AfterthoughtPolicy()
    quick = policy.delay_candidates("quick_continue")
    drift = policy.delay_candidates("topic_drift")
    assert quick[0] == 12 and quick[-1] == 30
    assert all(12 <= seconds <= 30 for seconds in quick)
    assert drift[0] == 75 and drift[-1] == 180
    assert all(75 <= seconds <= 180 for seconds in drift)


# ---------------------------------------------------------------------------
# End-to-end lane behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_act_draw_authorizes_one_followup_with_recorded_mode_and_window() -> None:
    gate_model = _EchoGateModel()
    probe_runtime, _probe_author, _probe_executor, probe_ledger = _build_runtime(
        gate_model=_EchoGateModel(), base_act_bp=2_000
    )
    receipt_event_ref = await _settled_reply(probe_runtime, probe_ledger)
    base = _base_act_bp_for(receipt_event_ref=receipt_event_ref, want="act")

    runtime, _author, executor, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=base
    )
    await _settled_reply(runtime, ledger)
    # Inside the first seconds the moment is still the reply's own; the lane
    # must stay quiet before the quick window opens.
    assert await runtime.drain_background_once() is None
    await _enter_quick_window(runtime)

    opened = await runtime.drain_background_once()
    assert opened is not None and opened.status == "opened"
    outcome = await runtime.drain_background_once()
    assert outcome is not None and outcome.status == "authorized"
    assert outcome.mode in {"quick_continue", "topic_drift"}
    assert gate_model.calls == 1

    projection = ledger.project()
    followup = next(
        action for action in projection.actions if action.kind == "followup"
    )
    policy = AfterthoughtPolicy(base_act_bp=base)
    grid = policy.delay_candidates(outcome.mode)
    assert outcome.delay_seconds in grid
    assert followup.not_before == NOW + timedelta(seconds=outcome.delay_seconds)
    assert followup.expires_at == followup.not_before + timedelta(
        seconds=policy.dispatch_slack_seconds
    )
    assert followup.state == "authorized"
    stored = next(
        item
        for item in projection.stored_message_payloads
        if item.payload_ref == followup.payload_ref
    )
    assert stored.text == gate_model.text
    # Both restraint draws are durable, replayable evidence.
    draws = [
        ref
        for ref in projection.committed_world_event_refs
        if ref.event_type == "RandomDrawRecorded"
    ]
    assert len(draws) >= 2
    audit = next(
        item
        for item in projection.proposal_audits
        if item.proposal_id.startswith(AFTERTHOUGHT_PROPOSAL_PREFIX)
    )
    assert audit.trigger_ref.endswith(":execution-receipt")
    process = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "afterthought_author"
    )
    assert process.state == "terminal"
    assert "authorized:" in (process.runtime_outcome_ref or "")
    # The tail is not dispatched early: only her settled reply reached the
    # provider so far, and the followup waits for its ``not_before``.
    assert [kind for kind, _ in executor.dispatched] == ["reply"]

    # One consideration per reply: the lane is quiet afterwards.
    assert await runtime.drain_background_once() is None


@pytest.mark.asyncio
async def test_hold_draw_is_terminal_and_never_consults_the_model() -> None:
    gate_model = _EchoGateModel()
    probe_runtime, _pa, _pe, probe_ledger = _build_runtime(
        gate_model=_EchoGateModel(), base_act_bp=2_000
    )
    receipt_event_ref = await _settled_reply(probe_runtime, probe_ledger)
    base = _base_act_bp_for(receipt_event_ref=receipt_event_ref, want="hold")

    runtime, _author, _executor, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=base
    )
    await _settled_reply(runtime, ledger)
    await _enter_quick_window(runtime)

    opened = await runtime.drain_background_once()
    assert opened is not None and opened.status == "opened"
    outcome = await runtime.drain_background_once()
    assert outcome is not None and outcome.status == "held"
    assert gate_model.calls == 0

    projection = ledger.project()
    assert all(action.kind != "followup" for action in projection.actions)
    process = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "afterthought_author"
    )
    assert process.state == "terminal"
    assert "held" in (process.runtime_outcome_ref or "")
    assert await runtime.drain_background_once() is None


@pytest.mark.asyncio
async def test_gate_decline_is_terminal_without_a_followup() -> None:
    gate_model = _EchoGateModel(decline=True)
    probe_runtime, _pa, _pe, probe_ledger = _build_runtime(
        gate_model=_EchoGateModel(), base_act_bp=2_000
    )
    receipt_event_ref = await _settled_reply(probe_runtime, probe_ledger)
    base = _base_act_bp_for(receipt_event_ref=receipt_event_ref, want="act")

    runtime, _author, _executor, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=base
    )
    await _settled_reply(runtime, ledger)
    await _enter_quick_window(runtime)

    assert (await runtime.drain_background_once()).status == "opened"
    outcome = await runtime.drain_background_once()
    assert outcome is not None and outcome.status == "declined"
    assert gate_model.calls == 1

    projection = ledger.project()
    assert all(action.kind != "followup" for action in projection.actions)
    assert all(
        not item.proposal_id.startswith(AFTERTHOUGHT_PROPOSAL_PREFIX)
        for item in projection.proposal_audits
    )
    assert await runtime.drain_background_once() is None


@pytest.mark.asyncio
async def test_user_answering_inside_the_window_voids_the_opportunity() -> None:
    gate_model = _EchoGateModel()
    runtime, _author, _executor, ledger = _build_runtime(
        gate_model=gate_model, base_act_bp=4_000
    )
    await _settled_reply(runtime, ledger)
    # The user answers before any afterthought consideration ran.
    followup_turn = await runtime.ingest(
        _observation(suffix="2", text="我也觉得结尾很好！")
    )
    assert followup_turn.status == "action_authorized"
    await _enter_quick_window(runtime)

    outcome = await runtime.drain_background_once()

    assert outcome is None
    assert gate_model.calls == 0
    projection = ledger.project()
    assert all(
        item.process_kind != "afterthought_author" for item in projection.trigger_processes
    )


def test_pending_initiative_and_stale_receipts_suppress_the_opportunity() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    author = AfterthoughtAuthorRuntime(
        ledger=ledger,
        model=_EchoGateModel(),
        policy=ExpressionPlanBudgetPolicy(
            account_id="account:proactive",
            amount_limit_per_action=2,
            actor=COMPANION,
            allowed_targets=(TARGET,),
            recovery_policy="effect_once",
            category="proactive",
        ),
        batch_issuer=issuer,
        owner_id="worker:afterthought-test",
        target=TARGET,
        companion_actor_ref=COMPANION,
        counterpart_actor_ref=TARGET,
        chronology=CHRONOLOGY,
    )

    reply_action = SimpleNamespace(
        kind="reply",
        state="provider_accepted",
        actor=COMPANION,
        target=TARGET,
        action_id="action:reply:1",
        expression_plan_id="plan:reply:1",
        payload_ref="payload:reply:1",
        payload_hash="sha256:" + "a" * 64,
        trace_id="trace:1",
        correlation_id="correlation:1",
    )
    receipt = SimpleNamespace(
        action_id="action:reply:1",
        observed_state="provider_accepted",
        provider="fixture:qq",
        source_event_id="receipt:action:reply:1",
        received_at=NOW,
    )
    receipt_ref = SimpleNamespace(
        event_id=(
            "event:trigger:settlement:fixture:qq:receipt:action:reply:1:execution-receipt"
        ),
        event_type="ExecutionReceiptRecorded",
        world_revision=5,
        payload_hash="b" * 64,
        logical_time=NOW,
    )

    def view(**overrides) -> SimpleNamespace:
        base = dict(
            logical_time=NOW + timedelta(seconds=30),
            actions=(reply_action,),
            execution_receipts=(receipt,),
            committed_world_event_refs=(receipt_ref,),
            trigger_processes=(),
            message_observations=(),
            stored_message_payloads=(),
            expression_plan_manifests=(),
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    assert author._next_opportunity(view()) is not None  # noqa: SLF001

    # Before the quick window opens, the moment still belongs to the reply.
    early = view(logical_time=NOW + timedelta(seconds=5))
    assert author._next_opportunity(early) is None  # noqa: SLF001

    # A reply that asked its own question keeps the floor (response gap owns it).
    expecting = view(
        expression_plan_manifests=(
            SimpleNamespace(
                plan_id="plan:reply:1",
                response_expectation=SimpleNamespace(hoped_response="回来继续聊"),
            ),
        )
    )
    assert author._next_opportunity(expecting) is None  # noqa: SLF001

    # v1's short-turn rule: a terse acknowledgement never grows a tail.
    terse = view(
        stored_message_payloads=(
            SimpleNamespace(
                payload_ref="payload:reply:1",
                payload_hash="sha256:" + "a" * 64,
                text="好。",
            ),
        )
    )
    assert author._next_opportunity(terse) is None  # noqa: SLF001

    pending = SimpleNamespace(
        kind="followup",
        state="authorized",
        actor=COMPANION,
        target=TARGET,
        action_id="action:followup:9",
        expression_plan_id="plan:followup:9",
        payload_ref="payload:followup:9",
        payload_hash="sha256:" + "c" * 64,
        trace_id="trace:9",
        correlation_id="correlation:9",
    )
    assert author._next_opportunity(view(actions=(reply_action, pending))) is None  # noqa: SLF001

    stale = view(logical_time=NOW + timedelta(seconds=600))
    assert author._next_opportunity(stale) is None  # noqa: SLF001

    answered = view(
        message_observations=(
            SimpleNamespace(actor=TARGET, world_revision=6, observation_id="observation:x"),
        )
    )
    assert author._next_opportunity(answered) is None  # noqa: SLF001

    considered = view(
        trigger_processes=(
            SimpleNamespace(
                process_kind="afterthought_author",
                state="terminal",
                source_evidence_ref=receipt_ref.event_id,
            ),
        )
    )
    assert author._next_opportunity(considered) is None  # noqa: SLF001
