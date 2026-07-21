"""P1 shadow-replay proof: hand-written pilots vs framework editions.

The acceptance bar from the approved framework proposal (§3.5): the same
input stream — scenario-corpus texts, injected deterministic model
applications, the same logical clock — drives both implementations, and the
resulting ledgers must show **zero byte difference**: every commit's
``commit_request_hash``, every event's ``canonical_event_json`` (event id,
idempotency key, payload bytes) and the final ``semantic_hash``.

The dual crash matrix additionally interrupts both implementations at every
lane commit boundary (pre- and post-write) and requires both to converge to
identical final bytes.

These tests stay in CI for the whole coexistence window: the hand-written
implementations are frozen, and any framework change that would fork ledger
history fails here first.
"""

from __future__ import annotations

import pytest

from companion_daemon.world_v2.afterthought_author import (
    AfterthoughtAuthorRuntime,
    AfterthoughtPolicy as HandAfterthoughtPolicy,
)
from companion_daemon.world_v2.afterthought_author_vertical import (
    AfterthoughtPolicy as FrameworkAfterthoughtPolicy,
    AfterthoughtVerticalRuntime,
)
from companion_daemon.world_v2.expression_draft import QQ_NAPCAT_EXPRESSION_CAPABILITIES
from companion_daemon.world_v2.expression_plan_atomic_recorder import (
    ExpressionPlanAtomicRecorder,
)
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.quick_reaction import (
    QuickReactionPolicy as HandQuickReactionPolicy,
    QuickReactionWorker,
)
from companion_daemon.world_v2.quick_reaction_vertical import (
    QuickReactionPolicy as FrameworkQuickReactionPolicy,
    QuickReactionVerticalWorker,
)
from companion_daemon.world_v2.replay_evaluator import ReplayEvaluator
from companion_daemon.world_v2.scenario_corpus import verify_frozen_scenario_corpus

from bdv_shadow_support import (
    COMPANION,
    TARGET,
    CrashInjected,
    CrashingLedger,
    ScriptedAfterthoughtGateModel,
    ScriptedQuickGateModel,
    advance_clock,
    assert_identical_tails,
    build_side,
    ledger_tail,
    observation_for,
    run_conversation_case,
)


def _corpus_scripts() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """One deterministic multi-turn script per scenario family (frozen corpus).

    The first (fully scripted) member of every family carries the family's
    real turn structure; the shadow proof replays that structure through both
    implementations.
    """

    cases = verify_frozen_scenario_corpus()
    scripts: dict[str, tuple[str, ...]] = {}
    for case in cases:
        family = case.entry.scenario_family
        if family in scripts:
            continue
        scripts[family] = tuple(step.text for step in case.turns)
    return tuple(sorted(scripts.items()))


CORPUS_SCRIPTS = _corpus_scripts()


async def _drive_pair(
    *,
    case_id: str,
    turns: tuple[str, ...],
    quick_base_act_bp: int = 3_200,
    afterthought_base_act_bp: int = 2_000,
    quick_gate_behaviour: str = "by_text",
    afterthought_gate_behaviour: str = "by_text",
) -> None:
    sides = {
        edition: build_side(
            edition=edition,
            world_id=f"world:shadow:{case_id}",
            quick_base_act_bp=quick_base_act_bp,
            afterthought_base_act_bp=afterthought_base_act_bp,
            quick_gate_behaviour=quick_gate_behaviour,
            afterthought_gate_behaviour=afterthought_gate_behaviour,
        )
        for edition in ("hand", "framework")
    }
    for side in sides.values():
        await run_conversation_case(side, case_id=case_id, turns=turns)
    assert sides["hand"].statuses == sides["framework"].statuses, (
        f"{case_id}: run statuses diverged\n hand:      {sides['hand'].statuses}\n"
        f" framework: {sides['framework'].statuses}"
    )
    assert_identical_tails(
        ledger_tail(sides["hand"].ledger),
        ledger_tail(sides["framework"].ledger),
        label=case_id,
    )
    # Same-cursor replay evidence (ReplayEvaluator) must hold on both sides:
    # the shadow world is not merely equal, it is replay-sound.
    evaluator = ReplayEvaluator()
    for side in sides.values():
        evaluation = evaluator.evaluate(evidence=side.ledger.export_replay_evidence())
        assert evaluation.replay_hash_matches, f"{case_id}: {side.edition} replay hash"
        fatal = [item for item in evaluation.findings if item.severity == "error"]
        assert not fatal, f"{case_id}: {side.edition} replay findings {fatal}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("family", "turns"), CORPUS_SCRIPTS, ids=[item[0] for item in CORPUS_SCRIPTS]
)
async def test_scenario_corpus_family_scripts_are_byte_identical(
    family: str, turns: tuple[str, ...]
) -> None:
    await _drive_pair(case_id=f"corpus.{family}", turns=turns)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "quick_behaviour", "afterthought_behaviour"),
    [
        ("gates_decline", "decline", "decline"),
        ("gates_garbage", "garbage", "author"),
        ("gates_broken_transport", "broken", "broken"),
        ("afterthought_overlap_guard", "always_react", "overlap"),
        ("gates_always_act", "always_react", "author"),
    ],
)
async def test_gate_failure_modes_are_byte_identical(
    label: str, quick_behaviour: str, afterthought_behaviour: str
) -> None:
    await _drive_pair(
        case_id=f"edge.{label}",
        turns=("今天路过一家小店，突然想起你。",),
        quick_gate_behaviour=quick_behaviour,
        afterthought_gate_behaviour=afterthought_behaviour,
        # High masses so both lanes usually reach their gates.
        quick_base_act_bp=8_000,
        afterthought_base_act_bp=8_000,
    )


@pytest.mark.asyncio
async def test_duplicate_ingest_is_byte_identical() -> None:
    """Replaying the same observation must dedupe identically on both sides."""

    sides = {
        edition: build_side(edition=edition, world_id="world:shadow:dup")
        for edition in ("hand", "framework")
    }
    text = "刚才的晚霞好漂亮，想给你看。"
    for side in sides.values():
        first = await side.runtime.ingest(observation_for(side, suffix="dup.1", text=text))
        side.statuses.append(f"first:{first.status}")
        again = await side.runtime.ingest(observation_for(side, suffix="dup.1", text=text))
        side.statuses.append(f"again:{again.status}")
    assert sides["hand"].statuses == sides["framework"].statuses
    assert_identical_tails(
        ledger_tail(sides["hand"].ledger),
        ledger_tail(sides["framework"].ledger),
        label="duplicate_ingest",
    )


# ---------------------------------------------------------------------------
# Dual crash matrix: interrupt both implementations at every commit boundary
# ---------------------------------------------------------------------------

# The crash worlds share one frozen world id per lane so the recorded draws
# (functions of world id, seed instant and attempt identity) replay the same
# path across every boundary/mode/edition run.
_QUICK_CRASH_WORLD = "world:shadow:crash:quick"
_AFTERTHOUGHT_CRASH_WORLD = "world:shadow:crash:afterthought"


def _quick_worker_for(edition: str, side, wrapper: CrashingLedger, *, base_act_bp: int):
    recorder = ExpressionPlanAtomicRecorder(batch_issuer=side.issuer)
    gate = ScriptedQuickGateModel(behaviour="always_react")
    common = dict(
        ledger=wrapper,
        model=gate,
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        expression_policy=side.expression_policy,
        expression_recorder=recorder,
        executor=side.executor,
        pump_owner="pump:shadow:quick-reaction",
        actor=COMPANION,
    )
    if edition == "hand":
        return QuickReactionWorker(
            policy=HandQuickReactionPolicy(base_act_bp=base_act_bp), **common
        )
    return QuickReactionVerticalWorker(
        policy=FrameworkQuickReactionPolicy(base_act_bp=base_act_bp), **common
    )


async def _quick_crash_run(
    *, edition: str, crash_at: int | None, mode: str, base_act_bp: int
) -> tuple[object, int, list[str]]:
    """Drive the quick lane directly with a crash injected at one boundary.

    Returns the raw ledger, the number of lane commits the wrapper saw on the
    first (possibly crashed) pass, and the observed statuses.
    """

    side = build_side(
        edition=edition,  # type: ignore[arg-type]
        world_id=_QUICK_CRASH_WORLD,
        wire_afterthought=False,
        wire_quick=False,
    )
    wrapper = CrashingLedger(side.ledger)
    worker = _quick_worker_for(edition, side, wrapper, base_act_bp=base_act_bp)

    text = "终于把最后一章写完啦！"
    outcome = await side.runtime.ingest(observation_for(side, suffix="crash.q", text=text))
    assert outcome.status == "action_authorized"
    located = side.ledger.lookup_event_commit(
        "event:trigger:observation:test:message:crash.q"
    )
    assert located is not None
    observation_event, observation_commit = located
    observation = observation_for(side, suffix="crash.q", text=text).model_copy(
        update={"logical_time": observation_event.logical_time}
    )

    statuses: list[str] = []
    if crash_at is not None:
        wrapper.arm(crash_at_commit=crash_at, mode=mode)
    first = await worker.run_observation(
        observation=observation,
        observation_event=observation_event,
        source_world_revision=observation_commit.world_revision,
    )
    first_pass_commits = wrapper.commits_seen
    statuses.append(first.status)
    if crash_at is not None:
        # The inline contract swallows the interruption silently.  A crash in
        # the pre-dispatch pipeline surfaces as the lane-level silent failure;
        # a crash inside the pump/settlement path surfaces as the equally
        # silent dispatch_incomplete (generic Action recovery owns it).
        crash_was_observed = (
            first.status == "failed"
            and str(first.reason_code).endswith("CrashInjected")
        ) or (
            first.status == "dispatch_incomplete"
            and first.reason_code == "quick_reaction.dispatch_failed"
        )
        assert crash_was_observed, (
            f"crash was not observed at boundary {crash_at}/{mode}: {first}"
        )
        # Recovery model: the platform redelivers the turn; the lane re-runs
        # and must converge without duplicating any effect.
        for _round in range(3):
            result = await worker.run_observation(
                observation=observation,
                observation_event=observation_event,
                source_world_revision=observation_commit.world_revision,
            )
            statuses.append(result.status)
            if result.status in {"duplicate", "held", "declined", "reacted"}:
                break
        assert statuses[-1] in {"duplicate", "held", "declined", "reacted"}
    return side.ledger, first_pass_commits, statuses


async def _find_quick_reacting_base() -> int:
    """Probe deterministic bases until the frozen crash world draws ``act``."""

    for base in range(2_500, 8_001, 137):
        _ledger, _commits, statuses = await _quick_crash_run(
            edition="hand", crash_at=None, mode="pre", base_act_bp=base
        )
        if statuses[-1] == "reacted":
            return base
    raise AssertionError("no probed base mass draws act in the quick crash world")


@pytest.mark.asyncio
async def test_quick_crash_matrix_dual_implementation_converges_identically() -> None:
    base = await _find_quick_reacting_base()
    baselines = {}
    boundary_counts = set()
    for edition in ("hand", "framework"):
        ledger, commits, statuses = await _quick_crash_run(
            edition=edition, crash_at=None, mode="pre", base_act_bp=base
        )
        assert statuses[-1] == "reacted", f"baseline must react, got {statuses}"
        baselines[edition] = ledger_tail(ledger)
        boundary_counts.add(commits)
    assert len(boundary_counts) == 1, f"lane commit counts diverged: {boundary_counts}"
    total_boundaries = boundary_counts.pop()
    assert total_boundaries >= 4, "the act path must cross several commit boundaries"
    assert_identical_tails(
        baselines["hand"], baselines["framework"], label="quick crash baseline"
    )
    for boundary in range(1, total_boundaries + 1):
        for mode in ("pre", "post"):
            tails = {}
            observed = {}
            for edition in ("hand", "framework"):
                ledger, _commits, statuses = await _quick_crash_run(
                    edition=edition, crash_at=boundary, mode=mode, base_act_bp=base
                )
                tails[edition] = ledger_tail(ledger)
                observed[edition] = statuses
            assert observed["hand"] == observed["framework"], (
                f"quick crash boundary {boundary} ({mode}): statuses diverged "
                f"{observed}"
            )
            assert_identical_tails(
                tails["hand"],
                tails["framework"],
                label=f"quick crash boundary {boundary} ({mode})",
            )


def _afterthought_author_for(
    edition: str, side, wrapper: CrashingLedger, *, base_act_bp: int
):
    gate = ScriptedAfterthoughtGateModel(behaviour="author")
    common = dict(
        ledger=wrapper,
        model=gate,
        policy=side.proactive_policy,
        batch_issuer=side.issuer,
        owner_id="worker:shadow:afterthought",
        target=TARGET,
        companion_actor_ref=COMPANION,
        counterpart_actor_ref=TARGET,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    if edition == "hand":
        return AfterthoughtAuthorRuntime(
            afterthought_policy=HandAfterthoughtPolicy(base_act_bp=base_act_bp), **common
        )
    return AfterthoughtVerticalRuntime(
        afterthought_policy=FrameworkAfterthoughtPolicy(base_act_bp=base_act_bp), **common
    )


async def _afterthought_crash_run(
    *, edition: str, crash_at: int | None, mode: str, base_act_bp: int
) -> tuple[object, int, list[str]]:
    side = build_side(
        edition=edition,  # type: ignore[arg-type]
        world_id=_AFTERTHOUGHT_CRASH_WORLD,
        wire_afterthought=False,
        wire_quick=False,
    )
    outcome = await side.runtime.ingest(
        observation_for(side, suffix="crash.a", text="那部片子你看完了吗？")
    )
    assert outcome.status == "action_authorized"
    pumped = await side.runtime.drain_actions_once()
    assert pumped is not None and pumped.status == "settled"
    await advance_clock(side, seconds=20)

    wrapper = CrashingLedger(side.ledger)
    author = _afterthought_author_for(edition, side, wrapper, base_act_bp=base_act_bp)

    if crash_at is not None:
        wrapper.arm(crash_at_commit=crash_at, mode=mode)
    statuses: list[str] = []
    crashed = False
    for _round in range(12):
        try:
            result = await author.drain_one()
        except CrashInjected:
            crashed = True
            statuses.append("crash")
            continue
        statuses.append(result.status)
        if result.status == "idle":
            break
    else:
        raise AssertionError(f"afterthought lane did not converge: {statuses}")
    if crash_at is not None:
        assert crashed, f"boundary {crash_at}/{mode} never crashed; matrix is stale"
    return side.ledger, wrapper.commits_seen, statuses


async def _find_afterthought_authorizing_base() -> int:
    """Probe deterministic bases until the frozen crash world draws ``act``."""

    for base in range(2_000, 4_501, 89):
        _ledger, _commits, statuses = await _afterthought_crash_run(
            edition="hand", crash_at=None, mode="pre", base_act_bp=base
        )
        if "authorized" in statuses:
            return base
    raise AssertionError("no probed base mass draws act in the afterthought crash world")


@pytest.mark.asyncio
async def test_afterthought_crash_matrix_dual_implementation_converges_identically() -> None:
    base = await _find_afterthought_authorizing_base()
    baselines = {}
    boundary_counts = set()
    for edition in ("hand", "framework"):
        ledger, commits, statuses = await _afterthought_crash_run(
            edition=edition, crash_at=None, mode="pre", base_act_bp=base
        )
        assert "authorized" in statuses, f"baseline must authorize, got {statuses}"
        baselines[edition] = ledger_tail(ledger)
        boundary_counts.add(commits)
    assert len(boundary_counts) == 1, f"lane commit counts diverged: {boundary_counts}"
    total_boundaries = boundary_counts.pop()
    assert total_boundaries >= 6, "the authorized path must cross several boundaries"
    assert_identical_tails(
        baselines["hand"], baselines["framework"], label="afterthought crash baseline"
    )
    for boundary in range(1, total_boundaries + 1):
        for mode in ("pre", "post"):
            tails = {}
            observed = {}
            for edition in ("hand", "framework"):
                ledger, _commits, statuses = await _afterthought_crash_run(
                    edition=edition, crash_at=boundary, mode=mode, base_act_bp=base
                )
                tails[edition] = ledger_tail(ledger)
                observed[edition] = statuses
            assert observed["hand"] == observed["framework"], (
                f"afterthought crash boundary {boundary} ({mode}): statuses diverged "
                f"{observed}"
            )
            assert_identical_tails(
                tails["hand"],
                tails["framework"],
                label=f"afterthought crash boundary {boundary} ({mode})",
            )
