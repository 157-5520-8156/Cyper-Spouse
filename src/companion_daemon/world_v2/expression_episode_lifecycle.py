"""Durable lifecycle identity for one visible Expression Episode.

The lifecycle reuses TriggerProcess plus the existing ModelResult/Proposal/
Action lineage.  It never authorizes delivery: opening and claiming only make
crash recovery and the afterthought mutex projection-visible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
from typing import Protocol

from .event_identity import domain_idempotency_key
from .minimal_reply_events import (
    ExpressionBeatTerminatedPayload,
    ExpressionPlanTerminatedPayload,
)
from .proposal_audit_schemas import (
    ModelResultAuditProjection,
    ProposalAuditProjection,
)
from .proposal_envelope import DecisionProposal, validate_proposal_envelope
from .schemas import (
    AcceptanceDecisionRef,
    Action,
    ClaimLease,
    CommittedWorldEventRef,
    ExpressionPlanManifestRef,
    MessageObservationRef,
    MinimalReplyManifestRef,
    Observation,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)


PROCESS_KIND = "expression_episode"
EXPRESSION_INFLIGHT_LEASE_SECONDS = 120
# One bounded same-model retry must happen before the platform declares a
# visible liveness failure. Later failures retain the conservative outage
# backoff so a provider incident cannot turn into an unbounded retry loop.
EXPRESSION_RETRY_DELAYS_SECONDS = (30, 1_800, 7_200)
EXPRESSION_FRESH_CONTEXT_REPIN_LIMIT = 2
# Platform liveness is a hard boundary, not a character decision.  Version the
# boundary explicitly so replay and a restarted transport derive the same due
# instant from the durable in-flight lease instead of from process-local sleep
# state. ModelResult event logical time is pinned World time, not provider
# completion wall time, so it cannot truthfully authorize a post-failure grace.
EXPRESSION_TECHNICAL_NOTICE_POLICY_ID = (
    "expression-technical-notice-at-inflight-lease.1"
)
_QUICK_REACTION_PROPOSAL_PREFIX = "proposal:quick-reaction:"
_INBOUND_EXPRESSION_PROPOSAL_PREFIXES = (
    "proposal:expression:",
    "proposal:chat-reply:",
)


class _ExpressionRetryProjection(Protocol):
    logical_time: datetime | None
    trigger_processes: tuple[TriggerProcess, ...]
    message_observations: tuple[MessageObservationRef, ...]
    committed_world_event_refs: tuple[CommittedWorldEventRef, ...]
    model_result_audits: tuple[ModelResultAuditProjection, ...]
    proposal_audits: tuple[ProposalAuditProjection, ...]
    minimal_reply_manifests: tuple[MinimalReplyManifestRef, ...]
    expression_plan_manifests: tuple[ExpressionPlanManifestRef, ...]
    acceptance_decisions: tuple[AcceptanceDecisionRef, ...]
    actions: tuple[Action, ...]


@dataclass(frozen=True, slots=True)
class ExpressionTechnicalNoticeCandidate:
    """One projection-derived platform liveness deadline.

    This is deliberately not a World event or Character Expression.  The
    transport may use it only to emit its fixed technical System Notice after
    the same latest inbound remains inside a durable technical retry state.
    """

    trigger_id: str
    observation_id: str
    due_at: datetime
    policy_id: str = EXPRESSION_TECHNICAL_NOTICE_POLICY_ID

    @property
    def notice_key(self) -> str:
        return (
            "system-notice:expression-technical-pending:"
            f"{self.policy_id}:{self.trigger_id}"
        )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def expression_episode_trigger_id(world_id: str, observation_id: str) -> str:
    if not world_id or not observation_id:
        raise ValueError("expression episode identity requires world and observation")
    return "trigger:expression-episode:" + _digest(
        {"world_id": world_id, "observation_id": observation_id}
    )


def expression_episode_attempt_id(*, trigger_id: str, attempt_ordinal: int) -> str:
    if not trigger_id or attempt_ordinal < 1:
        raise ValueError("expression attempt identity requires trigger and ordinal")
    return "attempt:expression-episode:" + _digest(
        {"trigger_id": trigger_id, "attempt": attempt_ordinal}
    )


def expression_episode_open_event(
    *, observation: Observation, observation_event: WorldEvent
) -> WorldEvent:
    if (
        observation_event.event_type != "ObservationRecorded"
        or observation_event.world_id != observation.world_id
    ):
        raise ValueError("expression episode requires its ObservationRecorded authority")
    process = TriggerProcess(
        trigger_id=expression_episode_trigger_id(
            observation.world_id, observation.observation_id
        ),
        trigger_ref=f"expression-episode:{observation.observation_id}",
        process_kind=PROCESS_KIND,
        source_evidence_ref=observation.observation_id,
        state="open",
    )
    payload = {"process": process.model_dump(mode="json")}
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:expression-episode:opened:" + _digest(payload),
        world_id=observation.world_id,
        event_type="TriggerProcessOpened",
        logical_time=observation.logical_time,
        created_at=observation.created_at,
        actor=observation.actor,
        source="world-runtime:expression-episode",
        trace_id=observation.trace_id,
        causation_id=observation_event.event_id,
        correlation_id=observation.correlation_id,
        idempotency_key=domain_idempotency_key(
            event_type="TriggerProcessOpened",
            world_id=observation.world_id,
            payload=payload,
        )
        or "expression-episode:opened:" + process.trigger_id,
        payload=payload,
    )


def expression_episode_claim_event(
    *,
    world_id: str,
    process: TriggerProcess,
    owner_id: str,
    at: datetime,
    trace_id: str,
    correlation_id: str,
    technical_failure_count: int | None = None,
    retry_projection: _ExpressionRetryProjection | None = None,
) -> tuple[WorldEvent, TriggerProcess]:
    if process.process_kind != PROCESS_KIND or process.state == "terminal":
        raise ValueError("only an active expression episode can be claimed")
    if process.state == "claimed":
        if process.claim_lease is None:
            raise ValueError("claimed expression episode is missing its active lease")
        if at < process.claim_lease.expires_at and not (
            retry_projection is not None
            and expression_episode_retry_reclaim_is_authorized(
                retry_projection,
                process,
                at=at,
            )
        ):
            raise ValueError(
                "expression episode cannot be reclaimed before its active lease expires"
            )
    attempt_ordinal = len(process.attempt_ids) + 1
    attempt_id = expression_episode_attempt_id(
        trigger_id=process.trigger_id,
        attempt_ordinal=attempt_ordinal,
    )
    if technical_failure_count is not None and technical_failure_count < 0:
        raise ValueError("expression technical failure count cannot be negative")
    # Claim ownership answers only whether a provider invocation may still be
    # in flight. Technical retry backoff is projected independently from
    # immutable failures; coupling the two made a process crash look silent
    # for 10–120 minutes before another Runtime could safely recover it.
    lease_seconds = EXPRESSION_INFLIGHT_LEASE_SECONDS
    claimed = process.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id=owner_id,
                attempt_id=attempt_id,
                acquired_at=at,
                expires_at=at + timedelta(seconds=lease_seconds),
            ),
            "attempt_ids": (*process.attempt_ids, attempt_id),
            # A reclaimed provider attempt owns a new bounded fresh-context
            # allowance. Reservations from the preceding failed attempt remain
            # immutable in its events but cannot constrain or authorize this one.
            "expression_repin_reservation_ids": (),
        }
    )
    event_type = (
        "TriggerProcessClaimed"
        if process.state == "open"
        else "TriggerProcessReclaimed"
    )
    payload = {"process": claimed.model_dump(mode="json")}
    return (
        WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=(
                "event:expression-episode:"
                + ("claimed:" if process.state == "open" else "reclaimed:")
                + _digest(payload)
            ),
            world_id=world_id,
            event_type=event_type,
            logical_time=at,
            created_at=at,
            actor=owner_id,
            source="world-runtime:expression-episode",
            trace_id=trace_id,
            causation_id=process.trigger_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type=event_type, world_id=world_id, payload=payload
            )
            or "expression-episode:"
            + ("claimed:" if process.state == "open" else "reclaimed:")
            + attempt_id,
            payload=payload,
        ),
        claimed,
    )


def expression_episode_repin_reservation_id(
    *,
    trigger_id: str,
    attempt_id: str,
    repin_ordinal: int,
    cursor: ProjectionCursor,
) -> str:
    if repin_ordinal < 1 or repin_ordinal > EXPRESSION_FRESH_CONTEXT_REPIN_LIMIT:
        raise ValueError("expression repin ordinal exceeds its bounded allowance")
    return "reservation:expression-fresh-context-repin:" + _digest(
        {
            "trigger_id": trigger_id,
            "attempt_id": attempt_id,
            "repin_ordinal": repin_ordinal,
            "cursor": cursor.model_dump(mode="json"),
        }
    )


def expression_episode_repin_reservation_event(
    *,
    world_id: str,
    process: TriggerProcess,
    cursor: ProjectionCursor,
    at: datetime,
    trace_id: str,
    correlation_id: str,
) -> tuple[WorldEvent, TriggerProcess]:
    """Reserve one provider re-authoring slot before crossing the API boundary."""

    if (
        process.process_kind != PROCESS_KIND
        or process.state != "claimed"
        or process.claim_lease is None
    ):
        raise ValueError("expression repin reservation requires a claimed episode")
    repin_ordinal = len(process.expression_repin_reservation_ids) + 1
    reservation_id = expression_episode_repin_reservation_id(
        trigger_id=process.trigger_id,
        attempt_id=process.claim_lease.attempt_id,
        repin_ordinal=repin_ordinal,
        cursor=cursor,
    )
    replacement = process.model_copy(
        update={
            "expression_repin_reservation_ids": (
                *process.expression_repin_reservation_ids,
                reservation_id,
            )
        }
    )
    payload = {
        "process": replacement.model_dump(mode="json"),
        "reservation_id": reservation_id,
        "attempt_id": process.claim_lease.attempt_id,
        "repin_ordinal": repin_ordinal,
        "reserved_world_revision": cursor.world_revision,
        "reserved_deliberation_revision": cursor.deliberation_revision,
        "reserved_ledger_sequence": cursor.ledger_sequence,
    }
    return (
        WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:expression-fresh-context-repin:reserved:"
            + _digest(payload),
            world_id=world_id,
            event_type="ExpressionRepinReserved",
            logical_time=at,
            created_at=at,
            actor=process.claim_lease.owner_id,
            source="world-runtime:expression-episode",
            trace_id=trace_id,
            causation_id=process.trigger_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ExpressionRepinReserved",
                world_id=world_id,
                payload=payload,
            )
            or "expression-fresh-context-repin:reserved:" + reservation_id,
            payload=payload,
        ),
        replacement,
    )


def expression_episode_retry_due(process: TriggerProcess) -> datetime | None:
    """Return the active provider in-flight lease deadline."""

    if (
        process.process_kind != PROCESS_KIND
        or process.state != "claimed"
        or process.claim_lease is None
    ):
        return None
    return process.claim_lease.expires_at


def _observation_event_ref(
    projection: _ExpressionRetryProjection,
    process: TriggerProcess,
) -> str | None:
    if process.source_evidence_ref is None:
        return None
    observation = next(
        (
            item
            for item in projection.message_observations
            if item.observation_id == process.source_evidence_ref
        ),
        None,
    )
    if (
        observation is None
        or observation.world_revision < 1
        or observation.world_revision > len(projection.committed_world_event_refs)
    ):
        return None
    authority = projection.committed_world_event_refs[
        observation.world_revision - 1
    ]
    if (
        authority.world_revision != observation.world_revision
        or authority.event_type != "ObservationRecorded"
        or authority.payload_hash != observation.event_payload_hash
    ):
        return None
    return authority.event_id


def _reply_proposal_audits(
    projection: _ExpressionRetryProjection,
    *,
    process: TriggerProcess,
    observation_event_ref: str,
) -> tuple[ProposalAuditProjection, ...]:
    """Return only the reply lane's exact durable Proposals.

    New expression deliberations bind their model ``attempt_id`` to the
    active expression-process claim.  The proposal-family fallback keeps
    pre-upgrade ledgers recoverable without letting quick reactions or
    appraisal proposals masquerade as the reply.
    """

    decided_proposal_ids = {
        decision.proposal_id
        for decision in getattr(projection, "acceptance_decisions", ())
        if decision.status in {"rejected", "stale"}
    }
    candidates = tuple(
        audit
        for audit in projection.proposal_audits
        if audit.trigger_ref == observation_event_ref
        and not audit.proposal_id.startswith(_QUICK_REACTION_PROPOSAL_PREFIX)
        and audit.proposal_id not in decided_proposal_ids
    )
    bound = tuple(
        audit
        for audit in candidates
        if audit.attempt_id in process.attempt_ids
    )
    family = tuple(
        audit
        for audit in candidates
        if audit.proposal_id.startswith(_INBOUND_EXPRESSION_PROPOSAL_PREFIXES)
    )
    return bound or family


def _current_attempt_has_terminal_technical_failure(
    projection: _ExpressionRetryProjection,
    *,
    process: TriggerProcess,
    observation_event_ref: str,
) -> bool:
    if process.claim_lease is None:
        return False
    current_attempt_id = process.claim_lease.attempt_id
    return any(
        audit.trigger_ref == observation_event_ref
        and audit.attempt_id == current_attempt_id
        and audit.proposal_hash is None
        and audit.attempt_index == audit.attempt_count - 1
        for audit in projection.model_result_audits
    )


def _current_attempt_acceptance_decisions(
    projection: _ExpressionRetryProjection,
    *,
    process: TriggerProcess,
    observation_event_ref: str,
) -> tuple[AcceptanceDecisionRef, ...]:
    """Return failed Acceptance decisions for reply Proposals on this claim.

    A model result can be perfectly valid while its exact external effect is
    temporarily impossible to accept (for example, the chat budget is
    exhausted), or stale after a crash.  Those are technical continuation
    states, not character-authored silence.
    """

    if process.claim_lease is None:
        return ()
    current_attempt_id = process.claim_lease.attempt_id
    proposal_ids = {
        audit.proposal_id
        for audit in projection.proposal_audits
        if audit.trigger_ref == observation_event_ref
        and audit.attempt_id == current_attempt_id
        and not audit.proposal_id.startswith(_QUICK_REACTION_PROPOSAL_PREFIX)
    }
    return tuple(
        decision
        for decision in getattr(projection, "acceptance_decisions", ())
        if decision.proposal_id in proposal_ids
        and decision.status in {"rejected", "stale"}
    )


def _current_attempt_is_role_owned_non_immediate_choice(
    projection: _ExpressionRetryProjection,
    *,
    process: TriggerProcess,
    observation_event_ref: str,
) -> bool:
    """Keep a recorded ``later``/``silent`` choice out of technical notices.

    A crash may leave the lifecycle claimed after the Proposal audit landed
    but before its deterministic terminal continuation.  The fixed platform
    Notice must not reinterpret that already-authored timing choice as an
    infrastructure failure.
    """

    if process.claim_lease is None:
        return False
    current_attempt_id = process.claim_lease.attempt_id
    for audit in reversed(projection.proposal_audits):
        if (
            audit.trigger_ref != observation_event_ref
            or audit.attempt_id != current_attempt_id
            or getattr(audit, "proposal_kind", "decision") != "decision"
        ):
            continue
        proposal_json = getattr(audit, "proposal_json", None)
        if not isinstance(proposal_json, str):
            continue
        try:
            proposal = validate_proposal_envelope(json.loads(proposal_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            # Immutable proposal validation owns corruption handling.  This
            # read-only liveness projection fails closed instead of inferring a
            # role choice from malformed audit bytes.
            continue
        if isinstance(proposal, DecisionProposal):
            return proposal.timing_choice in {"later", "silent"}
    return False


def expression_episode_technical_notice_candidates(
    projection: _ExpressionRetryProjection,
) -> tuple[ExpressionTechnicalNoticeCandidate, ...]:
    """Project delayed technical Notices for the one current conversation head.

    Eligibility is intentionally narrow and semantic-free: the exact current
    second-or-later attempt must have immutable technical-failure/
    failed-Acceptance evidence, the process must still be active, no external
    reply Action may already be authorized, and its source Observation must
    remain the latest inbound. Success, supersession, an in-flight first retry,
    and role-owned ``later``/``silent`` choices therefore remove the candidate
    before the transport crosses its Notice seam.
    """

    observations = tuple(getattr(projection, "message_observations", ()) or ())
    processes = tuple(getattr(projection, "trigger_processes", ()) or ())
    if not observations or not processes:
        return ()
    latest = max(
        observations,
        key=lambda item: (
            int(getattr(item, "world_revision", 0) or 0),
            str(getattr(item, "observation_id", "")),
        ),
    )
    latest_observation_id = getattr(latest, "observation_id", None)
    if not isinstance(latest_observation_id, str) or not latest_observation_id:
        return ()

    candidates: list[ExpressionTechnicalNoticeCandidate] = []
    for process in processes:
        if (
            process.process_kind != PROCESS_KIND
            or process.state != "claimed"
            or process.claim_lease is None
            or len(process.attempt_ids) < 2
            or process.source_evidence_ref != latest_observation_id
        ):
            continue
        observation_event_ref = _observation_event_ref(projection, process)
        if observation_event_ref is None:
            continue
        failed_acceptances = _current_attempt_acceptance_decisions(
            projection,
            process=process,
            observation_event_ref=observation_event_ref,
        )
        has_technical_terminal = _current_attempt_has_terminal_technical_failure(
            projection,
            process=process,
            observation_event_ref=observation_event_ref,
        ) or any(
            decision.status in {"rejected", "stale"}
            for decision in failed_acceptances
        )
        if not has_technical_terminal:
            continue
        if expression_episode_has_authorized_action(projection, process):
            continue
        if _current_attempt_is_role_owned_non_immediate_choice(
            projection,
            process=process,
            observation_event_ref=observation_event_ref,
        ):
            continue
        candidates.append(
            ExpressionTechnicalNoticeCandidate(
                trigger_id=process.trigger_id,
                observation_id=latest_observation_id,
                due_at=process.claim_lease.expires_at,
            )
        )
    return tuple(sorted(candidates, key=lambda item: (item.due_at, item.trigger_id)))


def expression_episode_technical_failure_count(
    projection: _ExpressionRetryProjection,
    process: TriggerProcess,
) -> int:
    """Count completed reply failures once per lifecycle attempt.

    One role attempt may persist several content-free ``ModelResultRecorded``
    rows: the returned author candidate, nested source-review provider calls,
    and the terminal validation result.  They are audit evidence for one
    expression attempt, not separate retry failures.  Likewise, nested
    reviewer results beside a reply Proposal must not be added to that
    Proposal's rejected Acceptance as another failure.
    """

    observation_event_ref = _observation_event_ref(projection, process)
    if observation_event_ref is None:
        return 0
    reply_audits = tuple(
        audit
        for audit in projection.proposal_audits
        if audit.trigger_ref == observation_event_ref
        and audit.attempt_id in process.attempt_ids
        and not audit.proposal_id.startswith(_QUICK_REACTION_PROPOSAL_PREFIX)
    )
    reply_attempt_ids = {audit.attempt_id for audit in reply_audits}
    failed_model_attempt_ids = {
        audit.attempt_id
        for audit in projection.model_result_audits
        if audit.trigger_ref == observation_event_ref
        and audit.attempt_id in process.attempt_ids
        and audit.attempt_id not in reply_attempt_ids
        and audit.proposal_hash is None
        and audit.attempt_index == audit.attempt_count - 1
    }
    rejected_proposal_ids = {
        decision.proposal_id
        for decision in getattr(projection, "acceptance_decisions", ())
        if decision.status == "rejected"
    }
    failed_acceptance_attempt_ids = {
        audit.attempt_id
        for audit in reply_audits
        if audit.proposal_id in rejected_proposal_ids
    }
    return len(failed_model_attempt_ids | failed_acceptance_attempt_ids)


def _technical_retry_due(
    projection: _ExpressionRetryProjection,
    process: TriggerProcess,
) -> datetime:
    if process.claim_lease is None:
        raise ValueError("technical retry deadline requires an active claim")
    failure_count = max(
        1,
        expression_episode_technical_failure_count(projection, process),
    )
    delay_seconds = EXPRESSION_RETRY_DELAYS_SECONDS[
        min(failure_count - 1, len(EXPRESSION_RETRY_DELAYS_SECONDS) - 1)
    ]
    return process.claim_lease.acquired_at + timedelta(seconds=delay_seconds)


def expression_episode_work_due(
    projection: _ExpressionRetryProjection,
    process: TriggerProcess,
    *,
    owner_id: str | None = None,
) -> datetime | None:
    """Return when an active expression lifecycle next needs deterministic work.

    A claim is not itself a failed model attempt.  If the process was claimed
    and the bound provider audit has not landed, the lease is the only
    cross-runtime proof that its owner may still be inside the provider call;
    global scheduling therefore waits for expiry.  Same-runtime continuation
    is an owner-aware Runtime operation and does not use this projection-only
    deadline. A terminal technical result bound to the *current* claim waits
    for a separate 30-second/30-minute/120-minute deadline projected from its recorded
    failure ordinal; the short ownership lease never weakens that backoff.
    The first terminal technical result receives one 30-second same-model
    retry before the two-minute platform Notice boundary; later failures keep
    the 30/120-minute outage backoff. A durable reply Proposal is also immediate work: the scheduler must
    continue that exact ``now/later/silent`` result without regenerating it.
    """

    if process.process_kind != PROCESS_KIND or process.state == "terminal":
        return None
    observation_event_ref = _observation_event_ref(projection, process)
    if observation_event_ref is None:
        return None
    if process.state == "open":
        return projection.logical_time
    if process.claim_lease is None:
        return None

    def lease_bound(local_due: datetime) -> datetime:
        if owner_id is not None and process.claim_lease is not None:
            if process.claim_lease.owner_id == owner_id:
                return local_due
        return max(local_due, process.claim_lease.expires_at)

    if _reply_proposal_audits(
        projection,
        process=process,
        observation_event_ref=observation_event_ref,
    ):
        # The provider choice is already immutable. Any Runtime may continue
        # its exact Acceptance/Action effect under CAS/effect-once without
        # invoking or regenerating model output.
        return process.claim_lease.acquired_at
    failed_acceptances = _current_attempt_acceptance_decisions(
        projection,
        process=process,
        observation_event_ref=observation_event_ref,
    )
    if any(decision.status == "rejected" for decision in failed_acceptances):
        return _technical_retry_due(projection, process)
    if any(decision.status == "stale" for decision in failed_acceptances):
        # Staleness already proves that the old wording cannot be authorized.
        # Re-pin the model at the current World immediately; it is not a
        # provider outage and therefore does not earn an outage backoff.
        return process.claim_lease.acquired_at
    if _current_attempt_has_terminal_technical_failure(
        projection,
        process=process,
        observation_event_ref=observation_event_ref,
    ):
        return _technical_retry_due(projection, process)
    return lease_bound(process.claim_lease.acquired_at)


def _eligible_expression_retry_processes(
    projection: _ExpressionRetryProjection,
    *,
    owner_id: str | None = None,
) -> tuple[TriggerProcess, ...]:
    eligible: list[TriggerProcess] = []
    for process in projection.trigger_processes:
        if (
            expression_episode_work_due(
                projection,
                process,
                owner_id=owner_id,
            )
            is None
        ):
            continue
        eligible.append(process)
    return tuple(eligible)


def next_expression_retry_due(
    projection: _ExpressionRetryProjection,
    *,
    owner_id: str | None = None,
) -> datetime | None:
    """Return the earliest durable technical-retry deadline, if one exists."""

    deadlines = tuple(
        due
        for process in _eligible_expression_retry_processes(
            projection,
            owner_id=owner_id,
        )
        if (
            due := expression_episode_work_due(
                projection,
                process,
                owner_id=owner_id,
            )
        )
        is not None
    )
    return min(deadlines, default=None)


def due_expression_retry_processes(
    projection: _ExpressionRetryProjection,
    *,
    at: datetime,
    owner_id: str | None = None,
) -> tuple[TriggerProcess, ...]:
    """Return eligible expired claims in stable scheduling order."""

    due_processes = tuple(
        process
        for process in _eligible_expression_retry_processes(
            projection,
            owner_id=owner_id,
        )
        if (
            deadline := expression_episode_work_due(
                projection,
                process,
                owner_id=owner_id,
            )
        )
        is not None
        and deadline <= at
    )
    return tuple(
        sorted(
            due_processes,
            key=lambda process: (
                expression_episode_work_due(
                    projection,
                    process,
                    owner_id=owner_id,
                ),
                process.trigger_id,
            ),
        )
    )


def expression_episode_complete_event(
    *,
    world_id: str,
    process: TriggerProcess,
    at: datetime,
    trace_id: str,
    correlation_id: str,
    outcome_ref: str,
    superseding_observation_event_ref: str | None = None,
) -> WorldEvent:
    if (
        process.process_kind != PROCESS_KIND
        or process.state != "claimed"
        or process.claim_lease is None
    ):
        raise ValueError("expression episode completion requires its claimed lifecycle")
    if (
        superseding_observation_event_ref is not None
        and outcome_ref != "expression-episode:superseded-by-newer-inbound"
    ):
        raise ValueError(
            "expression episode inbound supersession requires its exact outcome"
        )
    payload = {
        "trigger_id": process.trigger_id,
        "owner_id": process.claim_lease.owner_id,
        "attempt_id": process.claim_lease.attempt_id,
        "completed_at": at.isoformat(),
        "runtime_outcome_ref": outcome_ref,
        **(
            {
                "superseding_observation_event_ref": (
                    superseding_observation_event_ref
                )
            }
            if superseding_observation_event_ref is not None
            else {}
        ),
    }
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:expression-episode:completed:" + _digest(payload),
        world_id=world_id,
        event_type="TriggerProcessCompleted",
        logical_time=at,
        created_at=at,
        actor=process.claim_lease.owner_id,
        source="world-runtime:expression-episode",
        trace_id=trace_id,
        causation_id=superseding_observation_event_ref or process.trigger_id,
        correlation_id=correlation_id,
        idempotency_key="expression-episode:completed:" + _digest(
            {"world_id": world_id, "payload": payload}
        ),
        payload=payload,
    )


def expression_episode_has_authorized_action(
    projection: _ExpressionRetryProjection,
    process: TriggerProcess,
) -> bool:
    """Whether this episode already owns an immutable external Action.

    A newer user message may supersede unmaterialized reply cognition, but it
    cannot revoke an Action that has already crossed the authorization
    boundary. Dispatch and settlement retain their existing effect-once path.
    """

    observation_event_ref = _observation_event_ref(projection, process)
    if observation_event_ref is None:
        return False
    proposal_ids = {
        audit.proposal_id
        for audit in _reply_proposal_audits(
            projection,
            process=process,
            observation_event_ref=observation_event_ref,
        )
    }
    if not proposal_ids:
        return False
    plan_ids = {
        manifest.plan_id
        for manifest in projection.minimal_reply_manifests
        if manifest.proposal_id in proposal_ids
    }
    plan_ids.update(
        manifest.plan_id
        for manifest in projection.expression_plan_manifests
        if manifest.proposal_id in proposal_ids
    )
    return any(
        action.expression_plan_id in plan_ids
        for action in projection.actions
        if action.expression_plan_id is not None
    )


def expression_episode_retry_reclaim_is_authorized(
    projection: _ExpressionRetryProjection,
    process: TriggerProcess,
    *,
    at: datetime,
) -> bool:
    """Prove that one failed attempt may rotate before its lease expires.

    The two-minute lease protects an invocation whose result is still unknown.
    Once the *current* attempt has immutable terminal technical evidence, or
    its exact reply Proposal has a rejected Acceptance, no provider work can
    still be hidden behind that lease.  Only then may the installed retry due
    rotate a fresh attempt before the platform Notice boundary.

    The proof is projection-derived on purpose.  A caller-supplied failure
    count is diagnostic input and can never authorize an early reclaim.
    """

    if (
        process.process_kind != PROCESS_KIND
        or process.state != "claimed"
        or process.claim_lease is None
        or at >= process.claim_lease.expires_at
    ):
        return False
    observation_event_ref = _observation_event_ref(projection, process)
    if observation_event_ref is None:
        return False
    # A still-live reply Proposal must be continued exactly, not regenerated.
    if _reply_proposal_audits(
        projection,
        process=process,
        observation_event_ref=observation_event_ref,
    ):
        return False
    failed_acceptances = _current_attempt_acceptance_decisions(
        projection,
        process=process,
        observation_event_ref=observation_event_ref,
    )
    current_attempt_is_terminal = _current_attempt_has_terminal_technical_failure(
        projection,
        process=process,
        observation_event_ref=observation_event_ref,
    ) or any(decision.status == "rejected" for decision in failed_acceptances)
    if not current_attempt_is_terminal:
        return False
    if expression_episode_has_authorized_action(projection, process):
        return False
    return at >= _technical_retry_due(projection, process)


def expression_episode_cancel_events(
    *,
    world_id: str,
    projection,
    process: TriggerProcess,
    observation: Observation,
    observation_event_ref: str,
    superseded: bool = False,
) -> tuple[WorldEvent, ...]:
    """Atomically cancel every undispatched beat and release its reservation."""

    if process.process_kind != PROCESS_KIND or process.state != "claimed":
        raise ValueError("episode cancellation requires a claimed lifecycle")
    actions = tuple(
        item
        for item in projection.actions
        if item.state in {"authorized", "scheduled", "claimed"}
        and item.expression_plan_id is not None
        and any(
            audit.trigger_ref == observation_event_ref
            and manifest.proposal_id == audit.proposal_id
            and manifest.plan_id == item.expression_plan_id
            for manifest in projection.expression_plan_manifests
            for audit in projection.proposal_audits
        )
    )
    if not actions:
        return ()
    plan_ids = {item.expression_plan_id for item in actions}
    if len(plan_ids) != 1:
        raise ValueError("episode cancellation must bind one accepted plan")
    plan_id = next(iter(plan_ids))
    plan = next(
        (item for item in projection.expression_plans if item.plan_id == plan_id),
        None,
    )
    if plan is None or plan.state != "authorized":
        raise ValueError("episode cancellation requires an active plan")
    at = projection.logical_time or observation.logical_time
    common = {
        "schema_version": "world-v2.1",
        "world_id": world_id,
        "logical_time": at,
        "created_at": observation.created_at,
        "actor": process.claim_lease.owner_id if process.claim_lease else observation.actor,
        "source": "world-runtime:expression-episode-cancel",
        "trace_id": observation.trace_id,
        "causation_id": process.trigger_id,
        "correlation_id": observation.correlation_id,
    }
    events: list[WorldEvent] = []
    disposition = "superseded" if superseded else "cancelled"
    terminal_beat_id = ""
    for action in actions:
        beat = next(
            (
                item
                for item in projection.expression_beats
                if item.beat_id == action.expression_beat_id
            ),
            None,
        )
        reservation = next(
            (
                item
                for item in projection.budget_reservations
                if item.reservation_id == action.budget_reservation_id
            ),
            None,
        )
        if (
            beat is None
            or beat.state != "authorized"
            or reservation is None
            or reservation.state != "reserved"
        ):
            raise ValueError("episode cancellation lacks beat or budget authority")
        cancellation_id = "cancellation:expression-episode:" + _digest(
            {"trigger_id": process.trigger_id, "action_id": action.action_id}
        )
        cancel_payload = {"action_id": action.action_id}
        cancelled = WorldEvent.from_payload(
            **common,
            event_id="event:expression-episode:action-cancelled:"
            + _digest(cancel_payload | {"cancellation_id": cancellation_id}),
            event_type="ActionCancelled",
            idempotency_key="expression-episode:cancel:" + cancellation_id,
            payload=cancel_payload,
        )
        beat_payload = ExpressionBeatTerminatedPayload(
            acceptance_id=beat.acceptance_id,
            proposal_id=beat.proposal_id,
            plan_id=beat.plan_id,
            beat_id=beat.beat_id,
            action_id=action.action_id,
            disposition=disposition,
            source_event_ref=cancelled.event_id,
            source_event_payload_hash=cancelled.payload_hash,
        ).model_dump(mode="json")
        beat_event = WorldEvent.from_payload(
            **{**common, "causation_id": cancelled.event_id},
            event_id="event:expression-episode:beat-terminated:"
            + _digest(beat_payload),
            event_type="ExpressionBeatTerminated",
            idempotency_key=domain_idempotency_key(
                event_type="ExpressionBeatTerminated",
                world_id=world_id,
                payload=beat_payload,
            )
            or "expression-episode:beat-terminated:" + beat.beat_id,
            payload=beat_payload,
        )
        result_id = "result:expression-episode:cancel:" + _digest(
            {"trigger_id": process.trigger_id, "action_id": action.action_id}
        )
        settlement = {
            "settlement_id": "settlement:expression-episode:" + _digest(result_id),
            "reservation_id": reservation.reservation_id,
            "action_id": action.action_id,
            "result_id": result_id,
            "state": "released",
            "settlement_kind": "terminal",
            "previous_cost": reservation.settled_cost,
            "cost_actual": 0,
            "cost_delta": -reservation.settled_cost,
        }
        released = WorldEvent.from_payload(
            **{**common, "causation_id": beat_event.event_id},
            event_id="event:expression-episode:budget-released:"
            + _digest(settlement),
            event_type="BudgetReleased",
            idempotency_key="expression-episode:release:"
            + _digest({"world_id": world_id, "settlement": settlement}),
            payload={"settlement": settlement},
        )
        events.extend((cancelled, beat_event, released))
        terminal_beat_id = beat.beat_id
    plan_payload = ExpressionPlanTerminatedPayload(
        acceptance_id=plan.acceptance_id,
        proposal_id=plan.proposal_id,
        plan_id=plan.plan_id,
        terminal_beat_id=terminal_beat_id,
        disposition=disposition,
        source_event_ref=events[0].event_id,
        source_event_payload_hash=events[0].payload_hash,
    ).model_dump(mode="json")
    events.append(
        WorldEvent.from_payload(
            **{**common, "causation_id": events[-1].event_id},
            event_id="event:expression-episode:plan-terminated:"
            + _digest(plan_payload),
            event_type="ExpressionPlanTerminated",
            idempotency_key=domain_idempotency_key(
                event_type="ExpressionPlanTerminated",
                world_id=world_id,
                payload=plan_payload,
            )
            or "expression-episode:plan-terminated:" + plan.plan_id,
            payload=plan_payload,
        )
    )
    return tuple(events)


__all__ = [
    "EXPRESSION_RETRY_DELAYS_SECONDS",
    "EXPRESSION_TECHNICAL_NOTICE_POLICY_ID",
    "ExpressionTechnicalNoticeCandidate",
    "PROCESS_KIND",
    "due_expression_retry_processes",
    "expression_episode_attempt_id",
    "expression_episode_claim_event",
    "expression_episode_cancel_events",
    "expression_episode_complete_event",
    "expression_episode_has_authorized_action",
    "expression_episode_open_event",
    "expression_episode_retry_reclaim_is_authorized",
    "expression_episode_retry_due",
    "expression_episode_trigger_id",
    "expression_episode_technical_notice_candidates",
    "next_expression_retry_due",
]
