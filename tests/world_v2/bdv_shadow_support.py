"""Shared harness for the BoundedDecisionVertical shadow-replay proof.

One :class:`ShadowSide` is a complete deterministic World v2 lane (ledger +
runtime + quick-reaction worker + afterthought author) built from either the
hand-written pilot classes or their framework editions.  The proof drives two
sides with the same input stream (same fixture model applications, same
logical clock) and requires the resulting ledger tails to be byte-identical:
every commit's ``commit_request_hash``, every event's
``canonical_event_json`` (event id, idempotency key, payload bytes) and the
final projection ``semantic_hash``.

The crash matrix reuses :class:`CrashingLedger` to interrupt each lane at
every commit boundary (both before and after the write lands) and then
re-drives the lane to convergence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.afterthought_author import AfterthoughtAuthorRuntime
from companion_daemon.world_v2.afterthought_author import (
    AfterthoughtPolicy as HandAfterthoughtPolicy,
)
from companion_daemon.world_v2.afterthought_author_vertical import (
    AfterthoughtPolicy as FrameworkAfterthoughtPolicy,
    AfterthoughtVerticalRuntime,
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
from companion_daemon.world_v2.ledger import WorldLedger, canonical_event_json
from companion_daemon.world_v2.ledger_context_resolver import (
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.minimal_reply_acceptance import ReplyBudgetPolicy
from companion_daemon.world_v2.minimal_reply_atomic_recorder import (
    MinimalReplyAtomicRecorder,
)
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.quick_reaction import (
    QuickReactionPolicy as HandQuickReactionPolicy,
    QuickReactionWorker,
)
from companion_daemon.world_v2.quick_reaction_vertical import (
    QuickReactionPolicy as FrameworkQuickReactionPolicy,
    QuickReactionVerticalWorker,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    BudgetAccount,
    ClockObservation,
    Observation,
    ProviderReceipt,
    WorldEvent,
)


BASE_TIME = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
TARGET = "user:primary"
COMPANION = "agent:companion"
CHRONOLOGY_ZONE = "Asia/Shanghai"
REACTION_OPTION_IDS = tuple(
    item.option_id for item in QQ_NAPCAT_EXPRESSION_CAPABILITIES.reaction_options
)

Edition = Literal["hand", "framework"]


def _stable_hash(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:4], "big")


# ---------------------------------------------------------------------------
# Deterministic fixture models (the "recorded model applications")
# ---------------------------------------------------------------------------


class ScriptedRouter:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="shadow", router_version="shadow.1")


class ScriptedReplyModel:
    """Main chat model producing one deterministic text reply per trigger."""

    async def propose(self, request: ModelInput) -> ModelOutput:
        reply = deterministic_reply_text(request.trigger_ref)
        proposal = materialize_expression_draft(
            value={
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": reply}],
                "stance": "steady_share",
                "brief_rationale": "Deterministic shadow reply.",
            },
            request=request,
            capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        )
        return ModelOutput(
            model_id="shadow-expression-main",
            model_version="shadow.1",
            raw_proposal=proposal.model_dump(mode="json"),
            input_tokens=1,
            output_tokens=1,
        )


def deterministic_reply_text(user_text: str) -> str:
    """A reply whose length crosses the afterthought substantial threshold."""

    marker = _stable_hash("reply:" + user_text) % 1000
    return f"我在想你说的这件事（{marker:03d}），一会儿再细聊，先记下这份心情。"


class NoRecoveryModel:
    async def recover(self, _request: ModelInput, _failure: str) -> ModelOutput:
        raise AssertionError("quick recovery must not run in the shadow corpus")


class ScriptedQuickGateModel:
    """Quick-reaction gate: a pure deterministic function of the user text."""

    model = "shadow:quick-gate"

    def __init__(self, *, behaviour: str = "by_text") -> None:
        self.behaviour = behaviour
        self.calls = 0

    async def complete(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        if self.behaviour == "broken":
            raise RuntimeError("shadow quick gate transport is down")
        text = messages[-1]["content"]
        if self.behaviour == "decline":
            return '{"react":false}'
        if self.behaviour == "garbage":
            return "就不给你 JSON。"
        option = REACTION_OPTION_IDS[
            _stable_hash("option:" + text) % len(REACTION_OPTION_IDS)
        ]
        if self.behaviour == "always_react":
            return '{"react":true,"reaction_id":"' + option + '"}'
        bucket = _stable_hash("quick:" + text) % 4
        if bucket == 0:
            return '{"react":false}'
        if bucket == 1:
            return "含糊其辞，不是合同输出。"
        return '{"react":true,"reaction_id":"' + option + '"}'


class ScriptedAfterthoughtGateModel:
    """Afterthought gate echoing the drawn mode with a deterministic line."""

    model = "shadow:afterthought-gate"

    def __init__(self, *, behaviour: str = "by_text") -> None:
        self.behaviour = behaviour
        self.calls = 0

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        if self.behaviour == "broken":
            raise RuntimeError("shadow afterthought gate transport is down")
        import json as _json

        frame = _json.loads(messages[-1]["content"])
        mode = frame["mode"]
        if self.behaviour == "decline":
            return '{"afterthought":false}'
        if self.behaviour == "overlap":
            # Paraphrases her own reply: the overlap guard must decline it.
            return _json.dumps(
                {"afterthought": True, "mode": mode, "text": frame["her_reply_just_sent"]},
                ensure_ascii=False,
            )
        seed = _stable_hash("tail:" + frame["her_reply_just_sent"]) % 3
        if self.behaviour == "by_text" and seed == 0:
            return '{"afterthought":false}'
        marker = _stable_hash("line:" + frame["her_reply_just_sent"]) % 1000
        # ``author`` and the remaining by_text buckets author one line.
        return _json.dumps(
            {
                "afterthought": True,
                "mode": mode,
                "text": f"对了，还有个小细节（{marker:03d}）想起来了。",
            },
            ensure_ascii=False,
        )


class AcceptingExecutor:
    """Provider port that synchronously accepts every dispatched Action.

    ``now`` is the deterministic receipt clock; the driver keeps it equal to
    the world's logical clock so settlement anchors (and therefore the
    afterthought window) behave exactly as with a live provider.
    """

    def __init__(self, *, now: datetime = BASE_TIME) -> None:
        self.dispatched: list[tuple[str, str]] = []
        self.now = now

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
            received_at=self.now,
            raw_payload_hash="f" * 64,
        )

    async def lookup_result(self, action) -> None:  # type: ignore[no-untyped-def]
        del action
        return None


# ---------------------------------------------------------------------------
# Crash injection
# ---------------------------------------------------------------------------


class CrashInjected(RuntimeError):
    """The injected interruption at one commit boundary."""


class CrashingLedger:
    """LedgerPort delegate that interrupts the Nth lane commit exactly once.

    ``mode="pre"`` raises before the write lands (crash on the way into the
    commit); ``mode="post"`` lets the write land and raises before the caller
    observes the result (crash between durability and acknowledgement).
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.commits_seen = 0
        self._crash_at: int | None = None
        self._mode: str = "pre"

    def arm(self, *, crash_at_commit: int, mode: str) -> None:
        if mode not in {"pre", "post"}:
            raise ValueError("crash mode must be pre or post")
        self._crash_at = crash_at_commit
        self._mode = mode
        self.commits_seen = 0

    def disarm(self) -> None:
        self._crash_at = None

    def _guard(self, apply):
        self.commits_seen += 1
        if self._crash_at is not None and self.commits_seen == self._crash_at:
            if self._mode == "pre":
                self.disarm()
                raise CrashInjected(f"pre-commit crash at lane commit {self.commits_seen}")
            apply()  # the write lands durably before the interruption
            self.disarm()
            raise CrashInjected(f"post-commit crash at lane commit {self.commits_seen}")
        return apply()

    # -- LedgerPort surface -------------------------------------------------
    @property
    def world_id(self) -> str:
        return self._inner.world_id

    @property
    def blocks_event_loop(self) -> bool:
        return self._inner.blocks_event_loop

    def commit(self, events, **kwargs):
        return self._guard(lambda: self._inner.commit(events, **kwargs))

    def commit_at_cursor(self, events, **kwargs):
        return self._guard(lambda: self._inner.commit_at_cursor(events, **kwargs))

    def commit_accepted(self, batch, **kwargs):
        return self._guard(lambda: self._inner.commit_accepted(batch, **kwargs))

    def project(self):
        return self._inner.project()

    def project_at(self, cursor):
        return self._inner.project_at(cursor)

    def observation_events_at(self, locators, *, cursor):
        return self._inner.observation_events_at(locators, cursor=cursor)

    def lookup_event_commit(self, event_id):
        return self._inner.lookup_event_commit(event_id)

    def resolve_committed_event_refs(self, event_ids, *, at_world_revision):
        return self._inner.resolve_committed_event_refs(
            event_ids, at_world_revision=at_world_revision
        )

    def resolve_initial_world_event_ref(self, *, at_world_revision):
        return self._inner.resolve_initial_world_event_ref(
            at_world_revision=at_world_revision
        )


# ---------------------------------------------------------------------------
# Shadow sides
# ---------------------------------------------------------------------------


@dataclass
class ShadowSide:
    edition: Edition
    ledger: WorldLedger
    issuer: AcceptedLedgerBatchIssuer
    runtime: WorldRuntime
    executor: AcceptingExecutor
    quick_gate: ScriptedQuickGateModel | None
    afterthought_gate: ScriptedAfterthoughtGateModel | None
    quick_worker: object | None
    afterthought: object | None
    expression_policy: ExpressionPlanBudgetPolicy = None  # type: ignore[assignment]
    proactive_policy: ExpressionPlanBudgetPolicy = None  # type: ignore[assignment]
    clock: datetime = BASE_TIME
    tick_serial: int = 0
    statuses: list[str] = field(default_factory=list)


def _seed_event(world_id: str, event_id: str, event_type: str, payload: dict) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=world_id,
        event_type=event_type,
        logical_time=BASE_TIME,
        created_at=BASE_TIME,
        actor="system:test",
        source="test",
        trace_id="trace:shadow:setup",
        causation_id=event_id,
        correlation_id="correlation:shadow",
        idempotency_key=event_id,
        payload=payload,
    )


def build_side(
    *,
    edition: Edition,
    world_id: str,
    quick_base_act_bp: int = 3_200,
    afterthought_base_act_bp: int = 2_000,
    quick_gate_behaviour: str = "by_text",
    afterthought_gate_behaviour: str = "by_text",
    wire_quick: bool = True,
    wire_afterthought: bool = True,
    quick_ledger_wrapper: CrashingLedger | None = None,
    afterthought_ledger_wrapper: CrashingLedger | None = None,
) -> ShadowSide:
    """Build one deterministic shadow lane of the requested edition.

    Both editions receive byte-identical seeds, budgets, policies, actors and
    fixture models; the only difference is which implementation class runs the
    two pilot lanes.
    """

    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer)
    ledger.commit(
        (_seed_event(world_id, "event:shadow:start", "WorldStarted", {}),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    projection = ledger.project()
    ledger.commit(
        (
            _seed_event(
                world_id,
                "event:shadow:chat-budget",
                "BudgetAccountConfigured",
                {
                    "account": BudgetAccount(
                        account_id="account:chat",
                        category="chat",
                        window_id="window:1",
                        limit=1_000,
                    ).model_dump(mode="json")
                },
            ),
            _seed_event(
                world_id,
                "event:shadow:proactive-budget",
                "BudgetAccountConfigured",
                {
                    "account": BudgetAccount(
                        account_id="account:proactive",
                        category="proactive",
                        window_id="window:1",
                        limit=1_000,
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
    executor = AcceptingExecutor()

    quick_gate = ScriptedQuickGateModel(behaviour=quick_gate_behaviour) if wire_quick else None
    quick_worker = None
    if wire_quick:
        quick_ledger = quick_ledger_wrapper or ledger
        if edition == "hand":
            quick_worker = QuickReactionWorker(
                ledger=quick_ledger,
                model=quick_gate,
                capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
                expression_policy=chat_policy,
                expression_recorder=recorder,
                executor=executor,
                pump_owner="pump:shadow:quick-reaction",
                policy=HandQuickReactionPolicy(base_act_bp=quick_base_act_bp),
                actor=COMPANION,
            )
        else:
            quick_worker = QuickReactionVerticalWorker(
                ledger=quick_ledger,
                model=quick_gate,
                capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
                expression_policy=chat_policy,
                expression_recorder=recorder,
                executor=executor,
                pump_owner="pump:shadow:quick-reaction",
                policy=FrameworkQuickReactionPolicy(base_act_bp=quick_base_act_bp),
                actor=COMPANION,
            )

    afterthought_gate = (
        ScriptedAfterthoughtGateModel(behaviour=afterthought_gate_behaviour)
        if wire_afterthought
        else None
    )
    afterthought = None
    if wire_afterthought:
        afterthought_ledger = afterthought_ledger_wrapper or ledger
        if edition == "hand":
            afterthought = AfterthoughtAuthorRuntime(
                ledger=afterthought_ledger,
                model=afterthought_gate,
                policy=proactive_policy,
                batch_issuer=issuer,
                owner_id="worker:shadow:afterthought",
                target=TARGET,
                companion_actor_ref=COMPANION,
                counterpart_actor_ref=TARGET,
                chronology=LocalChronology(CHRONOLOGY_ZONE),
                afterthought_policy=HandAfterthoughtPolicy(
                    base_act_bp=afterthought_base_act_bp
                ),
            )
        else:
            afterthought = AfterthoughtVerticalRuntime(
                ledger=afterthought_ledger,
                model=afterthought_gate,
                policy=proactive_policy,
                batch_issuer=issuer,
                owner_id="worker:shadow:afterthought",
                target=TARGET,
                companion_actor_ref=COMPANION,
                counterpart_actor_ref=TARGET,
                chronology=LocalChronology(CHRONOLOGY_ZONE),
                afterthought_policy=FrameworkAfterthoughtPolicy(
                    base_act_bp=afterthought_base_act_bp
                ),
            )

    capsules: ContextCapsuleCompiler = context_capsule_compiler_from_ledger(ledger=ledger)
    turn = PinnedTurnCompiler(
        ledger=ledger,
        capsule_compiler=capsules,
        deliberation=Deliberation(
            router=ScriptedRouter(),
            main_model=ScriptedReplyModel(),
            quick_recovery=NoRecoveryModel(),
        ),
        companion_actor_ref=COMPANION,
    )
    runtime = WorldRuntime(
        world_id=world_id,
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
        action_pump_owner="pump:shadow",
        quick_reaction_worker=quick_worker if wire_quick else None,
        afterthought_author=afterthought if wire_afterthought else None,
    )
    return ShadowSide(
        edition=edition,
        ledger=ledger,
        issuer=issuer,
        runtime=runtime,
        executor=executor,
        quick_gate=quick_gate,
        afterthought_gate=afterthought_gate,
        quick_worker=quick_worker,
        afterthought=afterthought,
        expression_policy=chat_policy,
        proactive_policy=proactive_policy,
    )


def observation_for(
    side: ShadowSide, *, suffix: str, text: str
) -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id=f"observation:shadow:{suffix}",
        world_id=side.ledger.world_id,
        logical_time=side.clock,
        created_at=side.clock,
        trace_id=f"trace:shadow:{suffix}",
        causation_id=f"inbound:{suffix}",
        correlation_id="conversation:shadow",
        source="test",
        source_event_id=f"message:{suffix}",
        actor=TARGET,
        channel="test",
        payload_ref=f"payload:source:{suffix}",
        payload_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
        received_at=side.clock,
        reply_context={"target": TARGET, "platform_message_id": f"qq-msg-{suffix}"},
    )


async def advance_clock(side: ShadowSide, *, seconds: int) -> None:
    side.tick_serial += 1
    new_time = side.clock + timedelta(seconds=seconds)
    outcome = await side.runtime.advance(
        ClockObservation(
            schema_version="world-v2.1",
            tick_id=f"tick:shadow:{side.tick_serial}",
            world_id=side.ledger.world_id,
            logical_time=new_time,
            created_at=new_time,
            trace_id=f"trace:shadow:tick:{side.tick_serial}",
            causation_id="scheduler:shadow",
            correlation_id="correlation:shadow",
            logical_time_from=side.clock,
            logical_time_to=new_time,
            reason="shadow_window",
        )
    )
    assert outcome.status in {"advanced", "observed_only", "noop"}
    side.clock = new_time
    side.executor.now = new_time
    side.statuses.append(f"clock:{outcome.status}")


async def run_conversation_case(side: ShadowSide, *, case_id: str, turns: tuple[str, ...]) -> None:
    """One scenario-case script: ingest turns, settle the reply, consider the tail."""

    for index, text in enumerate(turns, start=1):
        suffix = f"{case_id}.{index}"
        outcome = await side.runtime.ingest(observation_for(side, suffix=suffix, text=text))
        side.statuses.append(f"ingest[{suffix}]:{outcome.status}")
        pumped = await side.runtime.drain_actions_once()
        side.statuses.append(
            f"pump[{suffix}]:{pumped.status if pumped is not None else 'none'}"
        )
    await advance_clock(side, seconds=20)
    for round_index in range(3):
        result = await side.runtime.drain_background_once()
        side.statuses.append(
            f"background[{case_id}.{round_index}]:"
            f"{result.status if result is not None else 'none'}"
        )


# ---------------------------------------------------------------------------
# Byte-level comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerTail:
    commits: tuple[tuple[str, str], ...]  # (commit_id, request_hash) in order
    events: tuple[tuple[int, str, str, str, str], ...]
    semantic_hash: str
    world_revision: int
    deliberation_revision: int
    ledger_sequence: int


def ledger_tail(ledger, *, since_ledger_sequence: int = 0) -> LedgerTail:
    evidence = ledger.export_replay_evidence()
    events = tuple(
        (
            item.cursor.ledger_sequence,
            item.commit_id,
            item.event.event_id,
            item.event.idempotency_key,
            canonical_event_json(item.event),
        )
        for item in evidence.events
        if item.cursor.ledger_sequence > since_ledger_sequence
    )
    tail_commit_ids = {item[1] for item in events}
    commits = tuple(
        (item.commit_id, item.request_hash)
        for item in evidence.commits
        if item.commit_id in tail_commit_ids
    )
    return LedgerTail(
        commits=commits,
        events=events,
        semantic_hash=evidence.projection.semantic_hash,
        world_revision=evidence.projection.world_revision,
        deliberation_revision=evidence.projection.deliberation_revision,
        ledger_sequence=evidence.projection.ledger_sequence,
    )


def assert_identical_tails(
    hand: LedgerTail, framework: LedgerTail, *, label: str
) -> None:
    """Fail with a precise pointer at the first byte difference."""

    assert hand.ledger_sequence == framework.ledger_sequence, (
        f"{label}: ledger lengths diverged "
        f"(hand={hand.ledger_sequence}, framework={framework.ledger_sequence})"
    )
    for index, (left, right) in enumerate(zip(hand.events, framework.events, strict=True)):
        assert left == right, (
            f"{label}: event #{index} differs\n hand:      {left[:4]}\n framework: {right[:4]}\n"
            f" hand bytes:      {left[4][:400]}\n framework bytes: {right[4][:400]}"
        )
    assert hand.commits == framework.commits, (
        f"{label}: commit ids/request hashes diverged\n"
        f" hand:      {hand.commits}\n framework: {framework.commits}"
    )
    assert hand.semantic_hash == framework.semantic_hash, (
        f"{label}: final semantic hash diverged"
    )
    assert (
        hand.world_revision == framework.world_revision
        and hand.deliberation_revision == framework.deliberation_revision
    ), f"{label}: final revisions diverged"


__all__ = [
    "BASE_TIME",
    "COMPANION",
    "CrashInjected",
    "CrashingLedger",
    "Edition",
    "LedgerTail",
    "REACTION_OPTION_IDS",
    "ScriptedAfterthoughtGateModel",
    "ScriptedQuickGateModel",
    "ShadowSide",
    "TARGET",
    "advance_clock",
    "assert_identical_tails",
    "build_side",
    "deterministic_reply_text",
    "ledger_tail",
    "observation_for",
    "run_conversation_case",
]
