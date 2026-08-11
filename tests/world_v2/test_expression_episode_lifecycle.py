from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.expression_episode_lifecycle import (
    EXPRESSION_TECHNICAL_NOTICE_POLICY_ID,
    due_expression_retry_processes,
    expression_episode_attempt_id,
    expression_episode_technical_notice_candidates,
    expression_episode_claim_event,
    expression_episode_retry_reclaim_is_authorized,
    expression_episode_retry_due,
    expression_episode_technical_failure_count,
    next_expression_retry_due,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import ClaimLease, TriggerProcess, WorldEvent


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
WORLD_ID = "world-v2-expression-retry"
OBSERVATION_ID = "observation:expression-retry"
OBSERVATION_EVENT_ID = "event:observation:expression-retry"


def _open_process(*, suffix: str = "") -> TriggerProcess:
    return TriggerProcess(
        trigger_id=f"trigger:expression-episode:{suffix or 'one'}",
        trigger_ref=f"expression-episode:{OBSERVATION_ID}{suffix}",
        process_kind="expression_episode",
        source_evidence_ref=f"{OBSERVATION_ID}{suffix}",
        state="open",
    )


def _claimed_process(
    *,
    attempt: int = 1,
    acquired_at: datetime = NOW,
    suffix: str = "",
) -> TriggerProcess:
    attempt_ids = tuple(
        f"attempt:expression-episode:{suffix or 'one'}:{ordinal}"
        for ordinal in range(1, attempt + 1)
    )
    return TriggerProcess(
        trigger_id=f"trigger:expression-episode:{suffix or 'one'}",
        trigger_ref=f"expression-episode:{OBSERVATION_ID}{suffix}",
        process_kind="expression_episode",
        source_evidence_ref=f"{OBSERVATION_ID}{suffix}",
        state="claimed",
        claim_lease=ClaimLease(
            owner_id=f"worker:{attempt}",
            attempt_id=attempt_ids[-1],
            acquired_at=acquired_at,
            expires_at=acquired_at + timedelta(minutes=2),
        ),
        attempt_ids=attempt_ids,
    )


def _projection(
    *,
    processes: tuple[TriggerProcess, ...],
    failed: bool = True,
    final_attempt: bool = True,
    proposal_ids: tuple[str, ...] = (),
    bind_attempt: bool = True,
    proposal_bind_attempt: bool | None = None,
    acceptance_decisions: tuple[object, ...] = (),
    logical_time: datetime = NOW,
):
    message_observations = tuple(
        SimpleNamespace(
            observation_id=process.source_evidence_ref,
            # This is the provider/platform message identity, not the
            # committed ObservationRecorded event identity.
            source_event_id=f"qq-message:{process.source_evidence_ref}",
            event_payload_hash=f"{index + 1:064x}",
            world_revision=index + 1,
        )
        for index, process in enumerate(processes)
    )
    committed_refs = tuple(
        SimpleNamespace(
            event_id=f"event:observation:{message.observation_id}",
            event_type="ObservationRecorded",
            world_revision=message.world_revision,
            payload_hash=message.event_payload_hash,
        )
        for message in message_observations
    )
    audits = ()
    if failed:
        audits = tuple(
            SimpleNamespace(
                trigger_ref=authority.event_id,
                proposal_hash=None,
                attempt_index=1 if final_attempt else 0,
                attempt_count=2,
                attempt_id=(
                    processes[index].attempt_ids[-1]
                    if bind_attempt
                    else f"attempt:another-lane:{index}"
                ),
                deliberation_result_id=f"deliberation:failure:{index}",
            )
            for index, authority in enumerate(committed_refs)
        )
    proposal_audits = tuple(
        SimpleNamespace(
            proposal_id=proposal_id,
            proposal_event_ref=f"event:proposal:{index}",
            event_ref=f"event:proposal:{index}",
            trigger_ref=committed_refs[0].event_id,
            attempt_id=(
                processes[0].attempt_ids[-1]
                if (
                    bind_attempt
                    if proposal_bind_attempt is None
                    else proposal_bind_attempt
                )
                else f"attempt:legacy-reply:{index}"
            ),
        )
        for index, proposal_id in enumerate(proposal_ids)
    )
    return SimpleNamespace(
        logical_time=logical_time,
        trigger_processes=processes,
        message_observations=message_observations,
        committed_world_event_refs=committed_refs,
        model_result_audits=audits,
        proposal_audits=proposal_audits,
        minimal_reply_manifests=(),
        expression_plan_manifests=(),
        acceptance_decisions=acceptance_decisions,
        actions=(),
    )


def test_expression_episode_claim_and_reclaim_use_short_inflight_leases() -> None:
    opened = _open_process()

    first_event, first = expression_episode_claim_event(
        world_id=WORLD_ID,
        process=opened,
        owner_id="worker:one",
        at=NOW,
        trace_id="trace:one",
        correlation_id="correlation:one",
    )
    assert first_event.event_type == "TriggerProcessClaimed"
    assert first.claim_lease is not None
    assert first.claim_lease.expires_at == NOW + timedelta(minutes=2)
    assert len(first.attempt_ids) == 1
    repeated_event, repeated = expression_episode_claim_event(
        world_id=WORLD_ID,
        process=opened,
        owner_id="worker:one",
        at=NOW,
        trace_id="trace:one",
        correlation_id="correlation:one",
    )
    assert repeated_event.event_id == first_event.event_id
    assert repeated.attempt_ids == first.attempt_ids

    second_at = first.claim_lease.expires_at
    second_event, second = expression_episode_claim_event(
        world_id=WORLD_ID,
        process=first,
        owner_id="worker:two",
        at=second_at,
        trace_id="trace:one",
        correlation_id="correlation:one",
    )
    assert second_event.event_type == "TriggerProcessReclaimed"
    assert second.claim_lease is not None
    assert second.claim_lease.expires_at == second_at + timedelta(minutes=2)
    assert second.attempt_ids[:-1] == first.attempt_ids

    third_at = second.claim_lease.expires_at
    _, third = expression_episode_claim_event(
        world_id=WORLD_ID,
        process=second,
        owner_id="worker:three",
        at=third_at,
        trace_id="trace:one",
        correlation_id="correlation:one",
    )
    assert third.claim_lease is not None
    assert third.claim_lease.expires_at == third_at + timedelta(minutes=2)

    fourth_at = third.claim_lease.expires_at
    _, fourth = expression_episode_claim_event(
        world_id=WORLD_ID,
        process=third,
        owner_id="worker:four",
        at=fourth_at,
        trace_id="trace:one",
        correlation_id="correlation:one",
    )
    assert fourth.claim_lease is not None
    assert fourth.claim_lease.expires_at == fourth_at + timedelta(minutes=2)
    assert fourth.attempt_ids[:-1] == third.attempt_ids


def test_expression_episode_cannot_reclaim_an_active_lease() -> None:
    claimed = _claimed_process()

    with pytest.raises(ValueError, match="before its active lease expires"):
        expression_episode_claim_event(
            world_id=WORLD_ID,
            process=claimed,
            owner_id="worker:two",
            at=claimed.claim_lease.expires_at - timedelta(microseconds=1),
            trace_id="trace:one",
            correlation_id="correlation:one",
        )


def test_terminal_failure_rotates_attempt_at_installed_retry_due() -> None:
    claimed = _claimed_process()
    due_at = NOW + timedelta(seconds=30)
    projection = _projection(
        processes=(claimed,),
        logical_time=due_at,
    )

    assert expression_episode_retry_reclaim_is_authorized(
        projection,
        claimed,
        at=due_at,
    )
    event, retried = expression_episode_claim_event(
        world_id=WORLD_ID,
        process=claimed,
        owner_id="worker:retry",
        at=due_at,
        trace_id="trace:retry",
        correlation_id="correlation:retry",
        retry_projection=projection,
    )

    assert event.event_type == "TriggerProcessReclaimed"
    assert retried.claim_lease is not None
    assert retried.claim_lease.acquired_at == due_at
    assert retried.claim_lease.expires_at == due_at + timedelta(minutes=2)
    assert retried.attempt_ids[:-1] == claimed.attempt_ids


def test_spoofed_failure_count_cannot_rotate_an_active_attempt() -> None:
    claimed = _claimed_process()
    due_at = NOW + timedelta(seconds=30)
    projection = _projection(
        processes=(claimed,),
        failed=False,
        logical_time=due_at,
    )

    assert not expression_episode_retry_reclaim_is_authorized(
        projection,
        claimed,
        at=due_at,
    )
    with pytest.raises(ValueError, match="before its active lease expires"):
        expression_episode_claim_event(
            world_id=WORLD_ID,
            process=claimed,
            owner_id="worker:retry",
            at=due_at,
            trace_id="trace:retry",
            correlation_id="correlation:retry",
            technical_failure_count=99,
            retry_projection=projection,
        )


def test_reducer_reproves_exact_failure_before_early_retry_reclaim() -> None:
    claimed = _claimed_process()
    due_at = NOW + timedelta(seconds=30)
    projection = _projection(
        processes=(claimed,),
        failed=False,
        logical_time=due_at,
    )
    state = ReducerState.model_construct(
        logical_time=due_at,
        trigger_processes=(claimed,),
        message_observations=projection.message_observations,
        committed_world_event_refs=projection.committed_world_event_refs,
        model_result_audits=(),
        proposal_audits=(),
        acceptance_decisions=(),
        minimal_reply_manifests=(),
        expression_plan_manifests=(),
        actions=(),
    )
    attempt_id = expression_episode_attempt_id(
        trigger_id=claimed.trigger_id,
        attempt_ordinal=2,
    )
    replacement = claimed.model_copy(
        update={
            "claim_lease": ClaimLease(
                owner_id="worker:forged-retry",
                attempt_id=attempt_id,
                acquired_at=due_at,
                expires_at=due_at + timedelta(minutes=2),
            ),
            "attempt_ids": (*claimed.attempt_ids, attempt_id),
        }
    )
    forged = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:expression-episode:forged-early-reclaim",
        world_id=WORLD_ID,
        event_type="TriggerProcessReclaimed",
        logical_time=due_at,
        created_at=due_at,
        actor="worker:forged-retry",
        source="world-runtime:expression-episode",
        trace_id="trace:forged-retry",
        causation_id=claimed.trigger_id,
        correlation_id="correlation:forged-retry",
        idempotency_key="expression-episode:forged-early-reclaim",
        payload={"process": replacement.model_dump(mode="json")},
    )

    with pytest.raises(ValueError, match="exact due expression failure authority"):
        reduce_event(state, forged)


def test_reclaim_lease_is_short_even_after_a_bound_failure() -> None:
    crashed_second_claim = _claimed_process(
        attempt=2,
        acquired_at=NOW - timedelta(minutes=30),
    )

    _, resumed = expression_episode_claim_event(
        world_id=WORLD_ID,
        process=crashed_second_claim,
        owner_id="worker:resume",
        at=NOW,
        trace_id="trace:resume",
        correlation_id="correlation:resume",
        technical_failure_count=1,
    )

    assert len(resumed.attempt_ids) == 3
    assert resumed.claim_lease is not None
    assert resumed.claim_lease.expires_at == NOW + timedelta(minutes=2)


def test_retry_projection_separates_inflight_lease_from_technical_backoff() -> None:
    process = _claimed_process()

    assert expression_episode_retry_due(process) == NOW + timedelta(minutes=2)
    assert next_expression_retry_due(
        _projection(processes=(process,))
    ) == NOW + timedelta(seconds=30)
    assert next_expression_retry_due(
        _projection(processes=(process,), failed=False)
    ) == NOW + timedelta(minutes=2)
    assert next_expression_retry_due(
        _projection(processes=(process,), final_attempt=False)
    ) == NOW + timedelta(minutes=2)


def test_nested_model_audits_count_as_one_failed_expression_attempt() -> None:
    process = _claimed_process()
    projection = _projection(processes=(process,))
    terminal = projection.model_result_audits[0]
    projection.model_result_audits = tuple(
        SimpleNamespace(
            **{
                **vars(terminal),
                "deliberation_result_id": f"deliberation:nested:{ordinal}",
            }
        )
        for ordinal in range(4)
    )

    assert expression_episode_technical_failure_count(projection, process) == 1
    assert next_expression_retry_due(projection) == NOW + timedelta(seconds=30)


def test_nested_model_audits_preserve_the_retry_ordinal_between_attempts() -> None:
    process = _claimed_process(attempt=2)
    projection = _projection(processes=(process,))
    terminal = projection.model_result_audits[0]
    projection.model_result_audits = tuple(
        SimpleNamespace(
            **{
                **vars(terminal),
                "attempt_id": attempt_id,
                "deliberation_result_id": (
                    f"deliberation:nested:{attempt_ordinal}:{nested_ordinal}"
                ),
            }
        )
        for attempt_ordinal, attempt_id in enumerate(process.attempt_ids, start=1)
        for nested_ordinal in range(4)
    )

    assert expression_episode_technical_failure_count(projection, process) == 2
    assert next_expression_retry_due(projection) == NOW + timedelta(minutes=30)


def test_rejected_reply_counts_once_despite_nested_attempt_audits() -> None:
    process = _claimed_process()
    proposal_id = "proposal:expression:one"
    projection = _projection(
        processes=(process,),
        failed=False,
        proposal_ids=(proposal_id,),
        acceptance_decisions=(
            SimpleNamespace(proposal_id=proposal_id, status="rejected"),
        ),
    )
    projection.model_result_audits = tuple(
        SimpleNamespace(
            trigger_ref=projection.committed_world_event_refs[0].event_id,
            proposal_hash=None,
            attempt_index=ordinal,
            attempt_count=3,
            attempt_id=process.attempt_ids[-1],
            deliberation_result_id=f"deliberation:nested:{ordinal}",
        )
        for ordinal in range(3)
    )

    assert expression_episode_technical_failure_count(projection, process) == 1
    assert next_expression_retry_due(projection) == NOW + timedelta(seconds=30)


def test_retry_projection_rejects_a_mismatched_observation_event_authority() -> None:
    process = _claimed_process()
    projection = _projection(processes=(process,))
    authority = projection.committed_world_event_refs[0]
    projection.committed_world_event_refs = (
        SimpleNamespace(
            event_id=authority.event_id,
            event_type=authority.event_type,
            world_revision=authority.world_revision,
            payload_hash="f" * 64,
        ),
    )

    assert next_expression_retry_due(projection) is None


def test_valid_reply_proposal_requires_immediate_exact_effect_continuation() -> None:
    process = _claimed_process()
    projection = _projection(
        processes=(process,),
        failed=False,
        proposal_ids=("proposal:expression:one",),
    )

    assert next_expression_retry_due(projection) == NOW
    assert next_expression_retry_due(
        projection,
        owner_id="worker:1",
    ) == NOW
    assert next_expression_retry_due(
        _projection(
            processes=(process,),
            failed=False,
            proposal_ids=("proposal:quick-reaction:one",),
        )
    ) == NOW + timedelta(minutes=2)
    assert next_expression_retry_due(
        _projection(
            processes=(process,),
            failed=False,
            proposal_ids=("proposal:appraisal-draft:one",),
            proposal_bind_attempt=False,
        )
    ) == NOW + timedelta(minutes=2)


def test_stale_reply_proposal_reconsiders_now_but_rejected_acceptance_backs_off() -> None:
    process = _claimed_process()
    proposal_id = "proposal:expression:one"

    stale = SimpleNamespace(proposal_id=proposal_id, status="stale")
    rejected = SimpleNamespace(proposal_id=proposal_id, status="rejected")

    stale_projection = _projection(
        processes=(process,),
        failed=False,
        proposal_ids=(proposal_id,),
        acceptance_decisions=(stale,),
    )
    assert next_expression_retry_due(stale_projection) == NOW
    assert next_expression_retry_due(
        stale_projection,
        owner_id="worker:1",
    ) == NOW
    assert next_expression_retry_due(
        _projection(
                processes=(process,),
                failed=False,
                proposal_ids=(proposal_id,),
            acceptance_decisions=(rejected,),
        )
    ) == NOW + timedelta(seconds=30)


def test_unattempted_reclaim_waits_only_for_its_short_inflight_lease() -> None:
    process = _claimed_process(attempt=2, acquired_at=NOW)
    projection = _projection(
        processes=(process,),
        failed=True,
        bind_attempt=False,
    )

    assert expression_episode_retry_due(process) == NOW + timedelta(minutes=2)
    assert next_expression_retry_due(projection) == NOW + timedelta(minutes=2)
    assert due_expression_retry_processes(projection, at=NOW) == ()
    assert due_expression_retry_processes(
        projection, at=NOW + timedelta(minutes=2)
    ) == (process,)


def test_other_lane_failure_does_not_accelerate_an_unattempted_reply_claim() -> None:
    process = _claimed_process()

    assert next_expression_retry_due(
        _projection(processes=(process,), failed=True, bind_attempt=False)
    ) == NOW + timedelta(minutes=2)


def test_open_expression_episode_is_immediately_recoverable() -> None:
    process = _open_process()

    assert next_expression_retry_due(
        _projection(processes=(process,), failed=False)
    ) == NOW
    assert due_expression_retry_processes(
        _projection(processes=(process,), failed=False),
        at=NOW,
    ) == (process,)


def test_due_expression_retry_processes_are_eligible_due_and_stably_ordered() -> None:
    later = _claimed_process(acquired_at=NOW + timedelta(minutes=2), suffix=":1")
    first = _claimed_process(suffix="")
    projection = _projection(processes=(later, first))

    assert due_expression_retry_processes(
        projection, at=NOW + timedelta(seconds=29)
    ) == ()
    assert due_expression_retry_processes(
        projection, at=NOW + timedelta(minutes=2)
    ) == (first,)
    assert due_expression_retry_processes(
        projection, at=NOW + timedelta(minutes=2, seconds=30)
    ) == (first, later)


def test_technical_notice_candidate_requires_second_attempt_technical_terminal() -> None:
    first = _claimed_process()
    first_failed = _projection(
        processes=(first,),
        logical_time=NOW + timedelta(seconds=30),
    )
    assert expression_episode_technical_notice_candidates(first_failed) == ()

    process = _claimed_process(
        attempt=2,
        acquired_at=NOW + timedelta(seconds=30),
    )
    retry_inflight = _projection(
        processes=(process,),
        failed=False,
        logical_time=NOW + timedelta(seconds=45),
    )
    assert expression_episode_technical_notice_candidates(retry_inflight) == ()

    retry_failed = _projection(
        processes=(process,),
        logical_time=NOW + timedelta(seconds=45),
    )
    candidates = expression_episode_technical_notice_candidates(retry_failed)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.trigger_id == process.trigger_id
    assert candidate.observation_id == OBSERVATION_ID
    assert candidate.policy_id == EXPRESSION_TECHNICAL_NOTICE_POLICY_ID
    assert candidate.due_at == NOW + timedelta(minutes=2, seconds=30)
    assert candidate.due_at >= retry_failed.logical_time + timedelta(seconds=30)
    assert candidate.notice_key == (
        "system-notice:expression-technical-pending:"
        f"{EXPRESSION_TECHNICAL_NOTICE_POLICY_ID}:{process.trigger_id}"
    )


def test_technical_notice_candidate_requires_the_latest_still_pending_inbound() -> None:
    failed = _claimed_process(
        attempt=2,
        acquired_at=NOW + timedelta(seconds=30),
    )
    projection = _projection(processes=(failed,), failed=True)
    projection.message_observations = (
        *projection.message_observations,
        SimpleNamespace(
            observation_id=f"{OBSERVATION_ID}:new",
            source_event_id="qq-message:new",
            event_payload_hash="2" * 64,
            world_revision=2,
        ),
    )
    projection.committed_world_event_refs = (
        *projection.committed_world_event_refs,
        SimpleNamespace(
            event_id="event:observation:new",
            event_type="ObservationRecorded",
            world_revision=2,
            payload_hash="2" * 64,
        ),
    )

    assert expression_episode_technical_notice_candidates(projection) == ()

    projection.trigger_processes = (
        failed.model_copy(
            update={
                "state": "terminal",
                "runtime_outcome_ref": "expression-episode:silent",
            }
        ),
    )
    projection.message_observations = projection.message_observations[:1]
    projection.committed_world_event_refs = projection.committed_world_event_refs[:1]

    assert expression_episode_technical_notice_candidates(projection) == ()
