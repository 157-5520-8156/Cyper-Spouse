"""P0 acceptance for the BoundedDecisionVertical framework module.

Covered: the three lifecycle engines converge on a synthetic ledger with an
interruption injected at every commit boundary (pre- and post-write), the
bounded model step's three installed failure policies, and the daily-check
primitives (wake exactness, check identity, recorded decision recovery).

Historical pilot byte-equality is deliberately excluded: the production
afterthought semantics are now model-owned and are not required to reproduce
the removed act/hold pilot.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.bounded_decision_vertical import (
    BoundedModelStep,
    BoundedModelUnavailable,
    DailyCheckEngine,
    DailyCheckLifecycle,
    LedgerOps,
    ModelStepContext,
    SingleCallAuditTemplate,
    run_bounded_model_step,
)
from companion_daemon.world_v2.deliberation import ModelRoute
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.random_authority import RandomAuthority
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import ClockObservation, WorldEvent

from bdv_shadow_support import (
    CrashInjected,
    CrashingLedger,
    assert_identical_tails,
    ledger_tail,
)


NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Bounded model step: the three installed failure policies
# ---------------------------------------------------------------------------


class _StubModel:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = outputs
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(list(messages))
        result = self.outputs[min(len(self.calls) - 1, len(self.outputs) - 1)]
        if isinstance(result, Exception):
            raise result
        return result


def _step(*, failure_policy: str, timeout: float = 1.0) -> BoundedModelStep:
    return BoundedModelStep(
        messages=lambda _context: [{"role": "user", "content": "判断"}],
        parse=lambda raw, _context: (
            json.loads(raw).get("ok") if raw.strip().startswith("{") else None
        ),
        timeout_seconds=timeout,
        audit=SingleCallAuditTemplate(
            call_namespace="synthetic",
            route=ModelRoute(tier="flash", reason_code="synthetic", router_version="s.1"),
            model_version="synthetic.1",
            fallback_model_id="synthetic",
        ),
        prompt_material=lambda _context, _proposal: {"contract": "synthetic.1"},
        failure_policy=failure_policy,  # type: ignore[arg-type]
    )


def _context() -> ModelStepContext:
    return ModelStepContext(opportunity=None, projection=None, draws={}, interpretations={})


@pytest.mark.asyncio
async def test_decline_quietly_swallows_transport_and_contract_failures() -> None:
    broken = _StubModel([RuntimeError("down")])
    verdict, raw = await run_bounded_model_step(
        step=_step(failure_policy="decline_quietly"),
        model=broken,
        context=_context(),
        log_label="synthetic",
    )
    assert verdict is None and raw is None

    garbage = _StubModel(["不是 JSON"])
    verdict, raw = await run_bounded_model_step(
        step=_step(failure_policy="decline_quietly"),
        model=garbage,
        context=_context(),
        log_label="synthetic",
    )
    assert verdict is None and raw == "不是 JSON"


@pytest.mark.asyncio
async def test_raise_retryable_surfaces_transport_failures_only() -> None:
    broken = _StubModel([TimeoutError("slow")])
    with pytest.raises(BoundedModelUnavailable):
        await run_bounded_model_step(
            step=_step(failure_policy="raise_retryable"),
            model=broken,
            context=_context(),
            log_label="synthetic",
        )
    # A contract breach is an answered decline, never a retryable outage.
    garbage = _StubModel(["nope"])
    verdict, raw = await run_bounded_model_step(
        step=_step(failure_policy="raise_retryable"),
        model=garbage,
        context=_context(),
        log_label="synthetic",
    )
    assert verdict is None and raw == "nope"


@pytest.mark.asyncio
async def test_correction_retry_once_reasks_exactly_once() -> None:
    model = _StubModel(["坏掉的输出", '{"ok": "fixed"}'])
    verdict, raw = await run_bounded_model_step(
        step=_step(failure_policy="correction_retry_once"),
        model=model,
        context=_context(),
        log_label="synthetic",
    )
    assert verdict == "fixed" and raw == '{"ok": "fixed"}'
    assert len(model.calls) == 2
    assert "合同" in model.calls[1][-1]["content"]

    stubborn = _StubModel(["坏", "还是坏"])
    with pytest.raises(BoundedModelUnavailable):
        await run_bounded_model_step(
            step=_step(failure_policy="correction_retry_once"),
            model=stubborn,
            context=_context(),
            log_label="synthetic",
        )
    assert len(stubborn.calls) == 2


@pytest.mark.asyncio
async def test_timeout_is_a_transport_failure() -> None:
    class _Hanging:
        async def complete(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            await asyncio.sleep(30)

    verdict, raw = await run_bounded_model_step(
        step=_step(failure_policy="decline_quietly", timeout=0.05),
        model=_Hanging(),
        context=_context(),
        log_label="synthetic",
    )
    assert verdict is None and raw is None


# ---------------------------------------------------------------------------
# Daily-check engine: wake exactness, identity, recovery, crash matrix
# ---------------------------------------------------------------------------


def _seed_world(world_id: str) -> tuple[WorldLedger, WorldRuntime]:
    ledger = WorldLedger.in_memory(world_id=world_id)
    ledger.commit(
        (
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:synthetic:start",
                world_id=world_id,
                event_type="WorldStarted",
                logical_time=NOW,
                created_at=NOW,
                actor="system:test",
                source="test",
                trace_id="trace:synthetic",
                causation_id="setup",
                correlation_id="correlation:synthetic",
                idempotency_key="event:synthetic:start",
                payload={},
            ),
        ),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return ledger, WorldRuntime(world_id=world_id, ledger=ledger)


async def _wake(runtime: WorldRuntime, world_id: str, *, hours: int = 1) -> str:
    tick_id = f"tick:synthetic:{hours}"
    outcome = await runtime.advance(
        ClockObservation(
            schema_version="world-v2.1",
            tick_id=tick_id,
            world_id=world_id,
            logical_time=NOW + timedelta(hours=hours),
            created_at=NOW + timedelta(hours=hours),
            trace_id="trace:synthetic:tick",
            causation_id="scheduler:test",
            correlation_id="correlation:synthetic",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=hours),
            reason="synthetic_check",
        )
    )
    assert outcome.status in {"advanced", "observed_only", "noop"}
    return f"event:trigger:clock:{tick_id}"


class SyntheticDailyVertical:
    """A minimal B-shape vertical exercising every DailyCheckEngine seam.

    Phases per check: one recorded draw, one durable check event, and one
    follow-on "result" commit recovered from ``decision == "selected"`` —
    the same three commit boundaries the production clock wells cross.
    """

    def __init__(self, *, ledger) -> None:
        self.ledger = ledger
        self.engine = DailyCheckEngine(
            ledger=ledger,
            lifecycle=DailyCheckLifecycle(
                namespace="synthetic-check",
                proposal_kind="synthetic_check",
                wake_reason_prefix="synthetic_check",
            ),
            actor="worker:synthetic",
            source="world-v2:synthetic-check",
        )
        self._random = RandomAuthority(ledger=ledger, source="world-v2:synthetic-random")

    def advance_once(self, *, wake_event_ref: str) -> str:
        projection = self.ledger.project()
        wake = self.engine.validate_wake(
            projection=projection, wake_event_ref=wake_event_ref
        )
        if wake is None:
            return "blocked"
        local_date = wake.logical_time.date().isoformat()
        identity = {"world_id": self.ledger.world_id, "local_date": local_date, "slot": 0}
        check_event_id = self.engine.check_event_id(identity)
        result_event_id = "event:synthetic-check:result:" + check_event_id.rpartition(":")[2]

        if self.ledger.lookup_event_commit(result_event_id) is not None:
            return "already_done"
        existing = self.engine.read_check(check_event_id)
        if existing is not None:
            decision, token = self.engine.check_decision(existing)
            if decision == "selected":
                self._commit_result(result_event_id, existing, token)
                return "recovered"
            return "slot_consumed"

        draw = self._random.draw(
            attempt_id="attempt:synthetic-check:" + local_date,
            candidate_refs=("candidate:a", "nothing"),
            candidate_weights={"candidate:a": 9_000, "nothing": 1_000},
            weight_policy_version="synthetic-weight.1",
            catalog_version="synthetic-catalog.1",
            logical_time=projection.logical_time,
            seed_instant=wake.logical_time,
            actor="system:synthetic",
            trace_id="trace:synthetic",
            correlation_id="correlation:synthetic",
        )
        if draw.selected_candidate_ref == "nothing":
            self.engine.record_check(
                check_event_id=check_event_id,
                proposal_id="proposal:synthetic-check:" + local_date,
                decision="nothing",
                identity_fields=identity,
                wake=wake,
                draw_event_ref="event:random-draw:" + draw.draw_id,
                candidate_token=None,
                raw_output=draw.selected_candidate_ref,
                model_id="random-authority",
                trace_id="trace:synthetic",
                correlation_id="correlation:synthetic",
            )
            return "no_op"
        parsed = self.engine.parse_select_no_op(
            '{"decision":"select","candidate_token":"candidate:a"}',
            offered_token="candidate:a",
        )
        assert parsed is not None and parsed[0] == "select"
        check = self.engine.record_check(
            check_event_id=check_event_id,
            proposal_id="proposal:synthetic-check:" + local_date,
            decision="selected",
            identity_fields=identity,
            wake=wake,
            draw_event_ref="event:random-draw:" + draw.draw_id,
            candidate_token="candidate:a",
            raw_output=parsed[1],
            model_id="scripted",
            trace_id="trace:synthetic",
            correlation_id="correlation:synthetic",
        )
        self._commit_result(result_event_id, check, "candidate:a")
        return "committed"

    def _commit_result(self, result_event_id: str, check_event, token: str | None) -> None:
        if self.ledger.lookup_event_commit(result_event_id) is not None:
            return
        projection = self.ledger.project()
        payload = {
            "proposal_id": "proposal:synthetic-result:" + (token or "none"),
            "proposal_kind": "synthetic_result",
            "decision": "materialized",
            "check_event_ref": check_event.event_id,
            "candidate_token": token,
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=result_event_id,
            world_id=self.ledger.world_id,
            event_type="ProposalRecorded",
            logical_time=projection.logical_time or NOW,
            created_at=projection.logical_time or NOW,
            actor="worker:synthetic",
            source="world-v2:synthetic-check",
            trace_id="trace:synthetic",
            causation_id=check_event.event_id,
            correlation_id="correlation:synthetic",
            idempotency_key="synthetic-result:" + result_event_id,
            payload=payload,
        )
        self.ledger.commit_at_cursor(
            (event,),
            expected_cursor=LedgerOps.cursor(projection),
            commit_id="commit:synthetic-check:result:" + result_event_id,
        )


@pytest.mark.asyncio
async def test_daily_check_wake_exactness_rejects_non_clock_anchors() -> None:
    ledger, runtime = _seed_world("world:synthetic:wake")
    vertical = SyntheticDailyVertical(ledger=ledger)
    assert vertical.advance_once(wake_event_ref="event:synthetic:start") == "blocked"
    wake_ref = await _wake(runtime, "world:synthetic:wake")
    assert vertical.advance_once(wake_event_ref=wake_ref) in {"committed", "no_op"}


@pytest.mark.asyncio
async def test_daily_check_converges_on_every_wake_of_the_same_day() -> None:
    ledger, runtime = _seed_world("world:synthetic:converge")
    vertical = SyntheticDailyVertical(ledger=ledger)
    wake_ref = await _wake(runtime, "world:synthetic:converge")
    first = vertical.advance_once(wake_event_ref=wake_ref)
    assert first in {"committed", "no_op"}
    tail = ledger_tail(ledger)
    # Every later pass of the same day converges without new writes.
    for _ in range(3):
        again = vertical.advance_once(wake_event_ref=wake_ref)
        assert again in {"already_done", "slot_consumed"}
    assert_identical_tails(tail, ledger_tail(ledger), label="same-day convergence")


@pytest.mark.asyncio
async def test_daily_check_crash_matrix_converges_to_baseline_bytes() -> None:
    world_id = "world:synthetic:crash"

    async def run(crash_at: int | None, mode: str) -> tuple[object, list[str]]:
        ledger, runtime = _seed_world(world_id)
        wake_ref = await _wake(runtime, world_id)
        wrapper = CrashingLedger(ledger)
        vertical = SyntheticDailyVertical(ledger=wrapper)
        statuses: list[str] = []
        if crash_at is not None:
            wrapper.arm(crash_at_commit=crash_at, mode=mode)
        for _ in range(6):
            try:
                status = vertical.advance_once(wake_event_ref=wake_ref)
            except CrashInjected:
                statuses.append("crash")
                continue
            statuses.append(status)
            if status in {"already_done", "slot_consumed"}:
                break
        return ledger, statuses

    baseline_ledger, baseline_statuses = await run(None, "pre")
    assert baseline_statuses[0] == "committed", baseline_statuses
    baseline = ledger_tail(baseline_ledger)

    # The selected path crosses three lane commits: draw, check, result.
    for boundary in (1, 2, 3):
        for mode in ("pre", "post"):
            crashed_ledger, statuses = await run(boundary, mode)
            assert "crash" in statuses, f"boundary {boundary}/{mode} never crashed"
            assert_identical_tails(
                baseline,
                ledger_tail(crashed_ledger),
                label=f"daily check crash boundary {boundary} ({mode})",
            )


# ---------------------------------------------------------------------------
