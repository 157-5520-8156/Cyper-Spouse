"""Cursor-pinned Context → Deliberation → Proposal-Audit composition.

This is intentionally the first, non-authorizing WorldRuntime turn vertical.
It turns an already recorded Observation into a trusted Capsule and an audited
model result/proposal at one complete cursor.  Acceptance and Action remain
separate modules; this module never materializes an accepted world effect.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
import logging
import time
from typing import Literal

from companion_daemon.llm import model_request_emission_scope

from .affect_target_bounds import lower_bounds_from_projection
from .context_capsule import (
    ContextCapsuleCompiler,
    InnerAdvisoryProjection,
)
from .context_resolver import query_from_projection
from .deliberation import (
    Deliberation,
    DeliberationResult,
    ModelResultAudit,
    ModelRoute,
    TriggerMessage,
    TurnAttentionAdvisory,
    _digest,
    _model_result_ref,
)
from .expression_cadence import CadenceDraw
from .expression_episode_lifecycle import expression_episode_trigger_id
from .interactive_turn_budget import InteractiveTurnBudget
from .errors import ConcurrencyConflict
from .ledger import LedgerPort
from .model_facing_context import mechanism_consumption_summary
from .proposal_audit import ProposalAuditCommit, ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import DecisionProposal, ProposalEvidenceRef
from .production_latency_trace import ProductionLatencyRecorder, TurnLatencyTrace
from .recall_index import RecallEmbeddingUnavailable
from .aspiration_view import active_aspiration_advisories
from .change_phase_view import change_phase_advisories
from .npc_relationship_view import npc_relationship_advisories
from .shared_private_invitation import pending_shared_private_invitation_advisories
from .response_expectation_view import (
    pending_response_expectation,
    response_expectation_advisory,
)
from .schemas import LedgerProjection, Observation, ProjectionCursor, WorldEvent


_LOG = logging.getLogger(__name__)
_RETRYABLE_CONTEXT_PREPARATION_ERRORS = (
    ConnectionError,
    OSError,
    TimeoutError,
    RecallEmbeddingUnavailable,
)


def _attempt_id(
    *, trigger_ref: str, cursor: ProjectionCursor, namespace: str | None = None
) -> str:
    material: dict[str, object] = {
        "contract": "pinned-turn.1",
        "trigger_ref": trigger_ref,
        "cursor": cursor.model_dump(mode="json"),
    }
    # Preserve pre-relationship attempt identities exactly.  Only a second
    # consumer of the same accepted appraisal needs a namespace.
    if namespace is not None:
        material["namespace"] = namespace
    material = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"attempt:pinned-turn:{hashlib.sha256(material).hexdigest()}"


class PinnedTurnCompiler:
    """Deep module for one cursor-consistent, audit-only Deliberation attempt."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        deliberation: Deliberation,
        companion_actor_ref: str,
        relationship_evaluation: bool = False,
        latency_recorder: ProductionLatencyRecorder | None = None,
        pending_expectation_advisory: bool = False,
        change_phase_advisory: bool = False,
        npc_relationship_advisory: bool = False,
        shared_private_invitation_advisory: bool = False,
        recorded_cadence_mode: str = "off",
        affect_target_bounds_enabled: bool = False,
    ) -> None:
        if not companion_actor_ref:
            raise ValueError("Pinned turn companion actor is required")
        self._ledger = ledger
        self._capsules = capsule_compiler
        self._deliberation = deliberation
        self._recorder = ProposalAuditRecorder(ledger=ledger)
        self._companion_actor_ref = companion_actor_ref
        self._relationship_evaluation = relationship_evaluation
        self._latency = latency_recorder
        # The unified inbound author opts in: when she was waiting for a
        # response she invited earlier, the same character turn should know
        # what she hoped this new message would be.  It remains advisory and
        # cannot prescribe an appraisal or reply.
        self._pending_expectation_advisory = pending_expectation_advisory
        # Active aspirations are ledger-backed subjective state.  Every
        # canonical turn sees the same source-bound view; callers cannot hide
        # that faculty behind a composition switch.

        # Change Phase (CONTEXT.md): a projection-level reading of whether
        # she is departing from or returning toward baseline.  Advisory only.
        self._change_phase_advisory = change_phase_advisory
        # Per-NPC relationship reading derived from committed shared history;
        # like the others it is read-only texture, never a rule.
        self._npc_relationship_advisory = npc_relationship_advisory
        # A pending shared_private invitation plan she may still need to
        # voice (or is waiting on); read-only texture, never an obligation.
        self._shared_private_invitation_advisory = shared_private_invitation_advisory
        self._affect_target_bounds_enabled = affect_target_bounds_enabled
        if recorded_cadence_mode not in {"off", "shadow", "on"}:
            raise ValueError("recorded cadence mode must be off, shadow, or on")
        self._recorded_cadence_mode = recorded_cadence_mode

    def expression_episode_diagnostics(self) -> dict[str, object]:
        """Expose aggregate process-local episode evidence without text."""

        return self._deliberation.expression_episode_diagnostics()

    @property
    def expression_episode_mode(self) -> str:
        return self._deliberation.expression_episode_mode

    def has_expression_episode_tail(self, trigger_ref: str) -> bool:
        return self._deliberation.has_expression_episode_tail(trigger_ref)

    async def cancel_superseded_expression_streams(
        self, current_trigger_ref: str
    ) -> None:
        terminals = await self._deliberation.cancel_superseded_expression_streams(
            current_trigger_ref
        )
        for trigger_ref, tail in terminals:
            if tail.deliberation is None or tail.deliberation.proposal is not None:
                continue
            stored = await self._lookup_event_commit(trigger_ref)
            if stored is None or stored[0].event_type != "ObservationRecorded":
                continue
            try:
                observation = Observation.model_validate_json(stored[0].payload_json)
            except ValueError:
                continue
            projection = await self._project()
            context = ProposalAuditContext(
                world_id=observation.world_id,
                trigger_ref=trigger_ref,
                logical_time=projection.logical_time or observation.logical_time,
                created_at=observation.created_at,
                actor=self._companion_actor_ref,
                source="world-runtime:expression-stream-cancelled",
                trace_id=observation.trace_id,
                causation_id=trigger_ref,
                correlation_id=observation.correlation_id,
                evaluated_world_revision=projection.world_revision,
                expected_commit_world_revision=projection.world_revision,
                expected_deliberation_revision=projection.deliberation_revision,
                expected_ledger_sequence=projection.ledger_sequence,
            )
            # Cancellation races the already-authorized head's settlement by
            # construction.  Rebase this content-free terminal audit at the
            # latest head; a stale CAS must never abort the newer user's
            # ingress.  If the sibling settlement already recorded the same
            # semantic tail, the audit is complete and no retry is needed.
            for retry_ordinal in range(3):
                try:
                    await self._record(tail.deliberation, context)
                    break
                except ConcurrencyConflict:
                    projection = await self._project()
                    terminal_call_ids = {
                        audit.model_call_id for audit in projection.model_result_audits
                    }
                    if tail.deliberation.audit.model_call_id in terminal_call_ids:
                        break
                    if retry_ordinal == 2:
                        _LOG.warning(
                            "stream cancellation audit lost repeated CAS race trigger=%s",
                            trigger_ref,
                        )
                        break
                    context = context.model_copy(
                        update={
                            "logical_time": (
                                projection.logical_time or observation.logical_time
                            ),
                            "expected_commit_world_revision": projection.world_revision,
                            "expected_deliberation_revision": (
                                projection.deliberation_revision
                            ),
                            "expected_ledger_sequence": projection.ledger_sequence,
                        }
                    )

    @property
    def recorded_cadence_mode(self) -> str:
        return self._recorded_cadence_mode

    async def await_expression_episode_tail(self, trigger_ref: str):
        return await self._deliberation.await_expression_episode_tail(trigger_ref)

    async def audit_expression_episode_tail(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
    ) -> tuple[ProposalAuditCommit | None, str]:
        """Persist a completed full tail without another provider call."""

        tail = await self._deliberation.await_expression_episode_tail(
            observation_event.event_id
        )
        if tail is None:
            return None, "complete_without_more"
        if tail.deliberation is None:
            return None, tail.disposition
        projection = await self._project()
        proposal = tail.deliberation.proposal
        if proposal is None:
            context = ProposalAuditContext(
                world_id=observation.world_id,
                trigger_ref=observation_event.event_id,
                logical_time=projection.logical_time or observation.logical_time,
                created_at=observation.created_at,
                actor=self._companion_actor_ref,
                source="world-runtime:expression-stream-terminal",
                trace_id=observation.trace_id,
                causation_id=observation_event.event_id,
                correlation_id=observation.correlation_id,
                evaluated_world_revision=projection.world_revision,
                expected_commit_world_revision=projection.world_revision,
                expected_deliberation_revision=projection.deliberation_revision,
                expected_ledger_sequence=projection.ledger_sequence,
            )
            return await self._record(tail.deliberation, context), tail.disposition
        if tail.disposition != "append":
            return None, tail.disposition
        if not isinstance(proposal, DecisionProposal):
            return None, "complete_without_more"
        if any(
            message.world_revision > proposal.evaluated_world_revision
            for message in projection.message_observations
        ):
            # The tail was authored against an older user turn. Unlike the
            # already-authorized head, it has no external effect yet and must
            # not be rebound across a newer Observation.
            return None, "cancel_pending"
        if self._deliberation.expression_episode_mode == "stream":
            # A streamed tail shares one physical authorship with its already
            # accepted head. Rebase it only across that head's own expected
            # settlement chain. Any Clock, Affect, relationship, life, or
            # unrelated Action change invalidates the still-unsent unit.
            #
            # The historical test-only ``on`` mode has two independently
            # authored proposals and its established receipt-gated semantics;
            # do not retroactively apply the one-SSE identity rule to it.
            allowed_head_settlement_types = {
                "AcceptanceRecorded",
                "MessagePayloadStored",
                "ExpressionPlanAccepted",
                "ExpressionBeatAuthorized",
                "BudgetReserved",
                "ActionAuthorized",
                "ActionScheduled",
                "ActionClaimed",
                "ActionDispatchStarted",
                "ActionProviderAccepted",
                "ExecutionReceiptRecorded",
                "ActionSettled",
            }
            intervening_refs = tuple(
                item
                for item in projection.committed_world_event_refs
                if item.world_revision > proposal.evaluated_world_revision
            )
            for event_ref in intervening_refs:
                if event_ref.event_type not in allowed_head_settlement_types:
                    return None, "cancel_pending"
                located = await self._lookup_event_commit(event_ref.event_id)
                if located is None:
                    return None, "cancel_pending"
                event = located[0]
                if (
                    event.trace_id != observation.trace_id
                    and event.correlation_id != observation.correlation_id
                ):
                    return None, "cancel_pending"
        rebound = proposal.model_copy(
            update={
                "proposal_id": (
                    proposal.proposal_id
                    + f":episode-append:{projection.world_revision}"
                ),
                "evaluated_world_revision": projection.world_revision,
            }
        )
        identity = {
            "capsule_id": tail.deliberation.capsule_id,
            "proposal_hash": rebound.proposal_hash,
            "attempt_audits": tuple(
                value.model_dump(mode="json")
                for value in tail.deliberation.attempt_audits
            ),
        }
        rebound_result = DeliberationResult(
            result_id=f"deliberation:{_digest(identity)}",
            capsule_id=tail.deliberation.capsule_id,
            proposal=rebound,
            audit=tail.deliberation.audit,
            attempt_audits=tail.deliberation.attempt_audits,
        )
        context = ProposalAuditContext(
            world_id=observation.world_id,
            trigger_ref=observation_event.event_id,
            logical_time=projection.logical_time or observation.logical_time,
            created_at=observation.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:expression-episode-tail",
            trace_id=observation.trace_id,
            causation_id=observation_event.event_id,
            correlation_id=observation.correlation_id,
            evaluated_world_revision=projection.world_revision,
            expected_commit_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
            expected_ledger_sequence=projection.ledger_sequence,
        )
        return await self._record(rebound_result, context), "append"

    async def audit_observation(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
        turn_budget: InteractiveTurnBudget | None = None,
        recorded_cadence_draws: tuple[CadenceDraw, ...] = (),
        expression_attempt_id: str | None = None,
    ) -> ProposalAuditCommit:
        """Compile an audit at a cursor that includes the committed Observation.

        The audit is a deliberation-only commit.  Any world revision change
        between the read and write makes the attempt stale; callers must build
        a fresh turn rather than reusing its Capsule or proposal.  A background
        appraisal may legitimately run after the observation's trigger process
        was durably opened, so its source event need only be at-or-before the
        pinned cursor; the evidence retains that event's original revision.
        """

        if observation.world_id != self._ledger.world_id or observation_event.world_id != observation.world_id:
            raise ValueError("Pinned turn observation belongs to another world")
        if observation_event.event_type != "ObservationRecorded":
            raise ValueError("Pinned turn requires an ObservationRecorded event")
        stored = await self._lookup_event_commit(observation_event.event_id)
        if (
            stored is None
            or stored[0] != observation_event
            or stored[1].world_revision > cursor.world_revision
            or stored[1].ledger_sequence > cursor.ledger_sequence
        ):
            raise ValueError("Pinned turn observation event is not the committed authority")
        try:
            committed_observation = Observation.model_validate_json(stored[0].payload_json)
        except ValueError as exc:
            raise ValueError("Pinned turn event has an invalid observation payload") from exc
        if committed_observation != observation:
            raise ValueError("Pinned turn observation does not match its committed authority")
        observation = committed_observation
        started = time.perf_counter()
        latency_trace = (
            self._latency.get_active(observation.trace_id)
            if self._latency is not None
            else None
        )
        attempt_id = (
            expression_attempt_id
            or _attempt_id(trigger_ref=observation_event.event_id, cursor=cursor)
        )
        try:
            if latency_trace is None:
                projection = await self._project_at(cursor)
            else:
                async with latency_trace.measure("snapshot"):
                    projection = await self._project_at(cursor)
        except _RETRYABLE_CONTEXT_PREPARATION_ERRORS as exc:
            # ``project_at`` is itself part of pre-provider Context
            # preparation.  We may record a content-free technical result only
            # when the current head still proves the exact supplied cursor and
            # attempt authority; otherwise this is a genuine stale turn.
            current = await self._project()
            if (
                current.world_revision != cursor.world_revision
                or current.deliberation_revision != cursor.deliberation_revision
                or current.ledger_sequence != cursor.ledger_sequence
            ):
                raise ConcurrencyConflict("Pinned turn cursor became stale") from exc
            if expression_attempt_id is not None:
                self._require_current_expression_attempt(
                    projection=current,
                    observation=observation,
                    expression_attempt_id=expression_attempt_id,
                )
            context = ProposalAuditContext(
                world_id=observation.world_id,
                trigger_ref=observation_event.event_id,
                logical_time=current.logical_time or observation.logical_time,
                created_at=observation.created_at,
                actor=self._companion_actor_ref,
                source="world-runtime:pinned-turn",
                trace_id=observation.trace_id,
                causation_id=observation_event.event_id,
                correlation_id=observation.correlation_id,
                evaluated_world_revision=cursor.world_revision,
                expected_commit_world_revision=cursor.world_revision,
                expected_deliberation_revision=cursor.deliberation_revision,
                expected_ledger_sequence=cursor.ledger_sequence,
            )
            _LOG.warning(
                "pinned turn pre-provider preparation failed trace=%s error=%s",
                observation.trace_id,
                type(exc).__name__,
            )
            return await self._record_pre_provider_failure(
                context=context,
                cursor=cursor,
                attempt_id=attempt_id,
                observation=observation,
                observation_event=observation_event,
                observation_world_revision=stored[1].world_revision,
                expression_attempt_id=expression_attempt_id,
            )
        if expression_attempt_id is not None:
            self._require_current_expression_attempt(
                projection=projection,
                observation=observation,
                expression_attempt_id=expression_attempt_id,
            )
        context = ProposalAuditContext(
            world_id=observation.world_id,
            trigger_ref=observation_event.event_id,
            logical_time=projection.logical_time or observation.logical_time,
            created_at=observation.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:pinned-turn",
            trace_id=observation.trace_id,
            causation_id=observation_event.event_id,
            correlation_id=observation.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            expected_ledger_sequence=cursor.ledger_sequence,
        )
        _LOG.warning(
            "pinned turn phases trace=%s phase=snapshot_ms value=%.1f",
            observation.trace_id,
            (time.perf_counter() - started) * 1000,
        )
        if turn_budget is not None and turn_budget.author_remaining() <= 0:
            return await self._record_pre_provider_failure(
                context=context,
                cursor=cursor,
                attempt_id=attempt_id,
                observation=observation,
                observation_event=observation_event,
                observation_world_revision=stored[1].world_revision,
                expression_attempt_id=expression_attempt_id,
                failure_code="interactive_budget_exhausted",
            )
        try:
            query = query_from_projection(
                projection,
                actor_ref=self._companion_actor_ref,
                trigger_ref=observation_event.event_id,
            )
            trigger_message = self._trigger_message(
                observation,
                observation_event,
                source_world_revision=stored[1].world_revision,
            )
            capsule_operation = self._compile_capsule_with_source_context(
                query=query,
                projection=projection,
                observation_event=observation_event,
                latency_trace=latency_trace,
                source_context_advisories=(
                    *self._expectation_advisories(
                        projection,
                        observation_event=observation_event,
                        source_world_revision=stored[1].world_revision,
                    ),
                    *self._aspiration_advisories(projection),
                    *self._change_phase_view_advisories(projection),
                    *self._npc_relationship_view_advisories(projection),
                    *self._shared_private_invitation_view_advisories(projection),
                ),
            )
            if latency_trace is None:
                capsule = await capsule_operation
            else:
                context_call_id = "model-call:foreground-context:" + _digest(
                    {
                        "trigger_ref": observation_event.event_id,
                        "world_revision": cursor.world_revision,
                        "deliberation_revision": cursor.deliberation_revision,
                        "ledger_sequence": cursor.ledger_sequence,
                    }
                )
                with model_request_emission_scope(
                    provider_call_id=context_call_id,
                    entry_marker=latency_trace.mark_auxiliary_provider_entry,
                    completion_marker=latency_trace.mark_auxiliary_provider_completion,
                ):
                    capsule = await capsule_operation
            # Keep an operator-readable answer to the most important
            # production question: did the character mechanisms reach this
            # turn at all? The summary contains only counts/statuses and never
            # logs model-facing prose or private memory values.
            mechanism_summary = mechanism_consumption_summary(
                capsule.capsule.model_content_json
            )
        except _RETRYABLE_CONTEXT_PREPARATION_ERRORS as exc:
            await self._raise_if_stale(cursor, exc)
            _LOG.warning(
                "pinned turn pre-provider preparation failed trace=%s error=%s",
                observation.trace_id,
                type(exc).__name__,
            )
            return await self._record_pre_provider_failure(
                context=context,
                cursor=cursor,
                attempt_id=attempt_id,
                observation=observation,
                observation_event=observation_event,
                observation_world_revision=stored[1].world_revision,
                expression_attempt_id=expression_attempt_id,
            )
        except ValueError as exc:
            # Trusted resolvers that expose only the current projection reject
            # a cursor when an independent background commit lands between
            # snapshot and Context resolution. Translate only that proven
            # head advance into the normal expression repin lifecycle; a
            # genuine same-cursor validation error still propagates unchanged.
            await self._raise_if_stale(cursor, exc)
            raise
        _LOG.warning(
            "pinned turn phases trace=%s phase=context_ms value=%.1f",
            observation.trace_id,
            (time.perf_counter() - started) * 1000,
        )
        # Keep an operator-readable answer to the most important production
        # question: did the character mechanisms reach this turn at all?  The
        # summary contains only counts/statuses and never logs model-facing
        # prose or private memory values.
        _LOG.info(
            "pinned turn mechanism consumption trace=%s summary=%s",
            observation.trace_id,
            json.dumps(
                mechanism_summary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        deliberate_kwargs = dict(
            attempt_id=attempt_id,
            trigger_evidence=(
                ProposalEvidenceRef(
                    ref_id=observation.observation_id,
                    evidence_kind="observed_message",
                    source_world_revision=stored[1].world_revision,
                    immutable_hash="sha256:" + observation_event.payload_hash,
                ),
            ),
            trigger_message=trigger_message,
            budget=turn_budget,
            recorded_draw_refs=tuple(
                dict.fromkeys(item.draw_ref for item in recorded_cadence_draws)
            ),
            recorded_cadence_draws=recorded_cadence_draws,
        )
        if latency_trace is not None and turn_budget is not None:
            prior_marker = turn_budget.marker

            def latency_milestone(event: str) -> None:
                # These are ingress-relative streaming markers; older budget
                # observers only understand the historical duration-segment
                # vocabulary.
                if prior_marker is not None and event not in {
                    "first_expression_frame",
                    "source_closure_completed",
                }:
                    prior_marker(event)
                latency_trace.mark_interactive_milestone(event)

            turn_budget = replace(turn_budget, marker=latency_milestone)
            deliberate_kwargs["budget"] = turn_budget
        if latency_trace is not None:
            deliberate_kwargs["first_role_provider_marker"] = (
                latency_trace.mark_role_provider_entry
            )
            deliberate_kwargs["first_role_provider_completion_marker"] = (
                latency_trace.mark_role_provider_completion
            )
            deliberate_kwargs["first_role_provider_token_marker"] = (
                latency_trace.mark_role_provider_first_token
            )
        if self._affect_target_bounds_enabled:
            deliberate_kwargs["affect_target_bounds"] = lower_bounds_from_projection(
                projection
            )
        result = await self._deliberation.deliberate(capsule, **deliberate_kwargs)
        _LOG.warning(
            "pinned turn phases trace=%s phase=model_ms value=%.1f",
            observation.trace_id,
            (time.perf_counter() - started) * 1000,
        )
        try:
            recorded = await self._record(result, context)
            _LOG.warning(
                "pinned turn phases trace=%s phase=record_ms value=%.1f",
                observation.trace_id,
                (time.perf_counter() - started) * 1000,
            )
            return recorded
        except ConcurrencyConflict as exc:
            if result.proposal is None and expression_attempt_id is not None:
                return await self._record_content_free_result_at_current_head(
                    result=result,
                    context=context,
                    observation=observation,
                    observation_event=observation_event,
                    observation_world_revision=stored[1].world_revision,
                    expression_attempt_id=expression_attempt_id,
                    cause=exc,
                )
            await self._raise_if_stale(cursor, exc)
            raise
        except ValueError:
            # The completed provider audit failed strict revalidation
            # (typically metering drift under heavy retry/correction). The
            # model call already spent; record a content-free technical
            # result so the turn ends through the durable lifecycle instead
            # of killing the whole turn with no audit at all.
            _LOG.warning(
                "pinned turn audit strict revalidation failed trace=%s attempt=%s",
                observation.trace_id,
                attempt_id,
                exc_info=True,
            )
            return await self._record_pre_provider_failure(
                context=context,
                cursor=cursor,
                attempt_id=attempt_id,
                observation=observation,
                observation_event=observation_event,
                observation_world_revision=stored[1].world_revision,
                expression_attempt_id=expression_attempt_id,
                failure_code="main_exception",
            )

    async def _record_content_free_result_at_current_head(
        self,
        *,
        result: DeliberationResult,
        context: ProposalAuditContext,
        observation: Observation,
        observation_event: WorldEvent,
        observation_world_revision: int,
        expression_attempt_id: str,
        cause: Exception,
    ) -> ProposalAuditCommit:
        """Durably close a technical attempt after unrelated head progress.

        A proposal-free result contains no wording, decision, or World effect
        that could become semantically stale.  Rebinding only its write CAS
        preserves the original Capsule, request, attempt, evaluated revision,
        and timestamps while making the durable retry lifecycle observable.
        A content-bearing Proposal never enters this path.
        """

        if result.proposal is not None or any(
            audit.attempt_id != expression_attempt_id
            for audit in result.attempt_audits
        ):
            raise ConcurrencyConflict(
                "content-free expression result lost its attempt authority"
            ) from cause
        expected_result_refs = {
            audit.model_result_ref for audit in result.attempt_audits
        }
        for _ in range(4):
            current = await self._project()
            try:
                self._require_current_expression_attempt(
                    projection=current,
                    observation=observation,
                    expression_attempt_id=expression_attempt_id,
                )
            except ValueError as exc:
                raise ConcurrencyConflict(
                    "content-free expression result is no longer current"
                ) from exc
            if any(
                item.world_revision > observation_world_revision
                for item in current.message_observations
            ):
                raise ConcurrencyConflict(
                    "content-free expression result was superseded by newer inbound"
                ) from cause

            trigger_audits = tuple(
                item
                for item in current.model_result_audits
                if item.trigger_ref == observation_event.event_id
                and item.attempt_id == expression_attempt_id
            )
            if trigger_audits and {
                item.model_result_ref for item in trigger_audits
            } != expected_result_refs:
                raise ConcurrencyConflict(
                    "expression attempt already owns a different model result"
                ) from cause

            process = next(
                item
                for item in current.trigger_processes
                if item.trigger_id
                == expression_episode_trigger_id(
                    observation.world_id,
                    observation.observation_id,
                )
            )
            bound_proposals = tuple(
                item
                for item in current.proposal_audits
                if item.trigger_ref == observation_event.event_id
                and item.attempt_id in process.attempt_ids
            )
            if any(
                item.attempt_id == expression_attempt_id
                for item in bound_proposals
            ):
                raise ConcurrencyConflict(
                    "expression attempt already owns a content-bearing proposal"
                ) from cause
            bound_proposal_ids = {
                item.proposal_id for item in bound_proposals
            }
            authorized_plan_ids = {
                item.plan_id
                for item in current.minimal_reply_manifests
                if item.proposal_id in bound_proposal_ids
            } | {
                item.plan_id
                for item in current.expression_plan_manifests
                if item.proposal_id in bound_proposal_ids
            }
            if authorized_plan_ids and any(
                action.expression_plan_id in authorized_plan_ids
                for action in current.actions
            ):
                raise ConcurrencyConflict(
                    "expression episode already owns an authorized Action"
                ) from cause

            rebased_context = context.model_copy(
                update={
                    "expected_commit_world_revision": current.world_revision,
                    "expected_deliberation_revision": current.deliberation_revision,
                    "expected_ledger_sequence": current.ledger_sequence,
                }
            )
            try:
                return await self._record(result, rebased_context)
            except ConcurrencyConflict:
                # A competing writer may have moved the head or committed the
                # same deterministic audit. Reproject and revalidate every
                # authority condition; the recorder joins an exact commit by
                # its stable commit id on the next iteration.
                continue
        raise ConcurrencyConflict(
            "content-free expression result rebase did not converge"
        ) from cause

    async def record_expression_repin_exhausted(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
        expression_attempt_id: str,
    ) -> ProposalAuditCommit:
        """Close one attempt whose durable fresh-context allowance is spent.

        This crosses no provider boundary and authors no character content. It
        records only the technical terminal needed by the ordinary durable
        retry schedule.
        """

        if (
            observation.world_id != self._ledger.world_id
            or observation_event.world_id != observation.world_id
            or observation_event.event_type != "ObservationRecorded"
        ):
            raise ValueError(
                "expression repin exhaustion requires its Observation authority"
            )
        stored = await self._lookup_event_commit(observation_event.event_id)
        if stored is None or stored[0] != observation_event:
            raise ValueError("expression repin exhaustion source is not committed")
        committed_observation = Observation.model_validate_json(
            stored[0].payload_json
        )
        if committed_observation != observation:
            raise ValueError(
                "expression repin exhaustion changed its committed observation"
            )
        current = await self._project()
        self._require_current_expression_attempt(
            projection=current,
            observation=observation,
            expression_attempt_id=expression_attempt_id,
        )
        context = ProposalAuditContext(
            world_id=observation.world_id,
            trigger_ref=observation_event.event_id,
            logical_time=current.logical_time or observation.logical_time,
            created_at=observation.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:pinned-turn",
            trace_id=observation.trace_id,
            causation_id=observation_event.event_id,
            correlation_id=observation.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            expected_ledger_sequence=cursor.ledger_sequence,
        )
        return await self._record_pre_provider_failure(
            context=context,
            cursor=cursor,
            attempt_id=expression_attempt_id,
            observation=observation,
            observation_event=observation_event,
            observation_world_revision=stored[1].world_revision,
            expression_attempt_id=expression_attempt_id,
            failure_code="expression_fresh_context_repin_exhausted",
        )

    async def _record_pre_provider_failure(
        self,
        *,
        context: ProposalAuditContext,
        cursor: ProjectionCursor,
        attempt_id: str,
        observation: Observation,
        observation_event: WorldEvent,
        observation_world_revision: int,
        expression_attempt_id: str | None,
        failure_code: str = "main_exception",
    ) -> ProposalAuditCommit:
        """Persist one content-free technical result before any model call.

        The identity binds only source authority, cursor and attempt. Neither
        the exception message nor partially compiled Context is retained.
        """

        authority = {
            "contract": "pinned-turn-pre-provider-failure.1",
            "world_id": context.world_id,
            "trigger_ref": context.trigger_ref,
            "cursor": cursor.model_dump(mode="json"),
            "attempt_id": attempt_id,
        }
        capsule_id = _digest({**authority, "artifact": "uncompiled-context"})
        budget_exhausted = failure_code in {
            "interactive_budget_exhausted",
            "expression_fresh_context_repin_exhausted",
        }
        operation = (
            "expression-fresh-context-repin"
            if failure_code == "expression_fresh_context_repin_exhausted"
            else "context-preparation"
        )
        model_call_id = (
            "model-call:skipped-pre-provider:"
            + _digest({**authority, "operation": operation})
        )
        audit = ModelResultAudit(
            model_call_id=model_call_id,
            model_result_ref=_model_result_ref(model_call_id, None),
            attempt_id=attempt_id,
            route=ModelRoute(
                tier="flash",
                reason_code=(
                    "pre_provider_context_exception"
                    if failure_code == "main_exception"
                    else failure_code
                ),
                router_version="pinned-turn.1",
            ),
            request_hash=_digest(
                {
                    **authority,
                    "request": (
                        "reserve-expression-fresh-context-repin"
                        if operation == "expression-fresh-context-repin"
                        else "compile-source-bound-context"
                    ),
                }
            ),
            status="main_timeout" if budget_exhausted else "main_exception",
            failure_code="primary_timeout" if budget_exhausted else failure_code,
            slot="primary",
            outcome=(
                "budget_exhausted" if budget_exhausted else "exception"
            ),
        )
        identity = {
            "capsule_id": capsule_id,
            "proposal_hash": None,
            "attempt_audits": (audit.model_dump(mode="json"),),
        }
        result = DeliberationResult(
            result_id=f"deliberation:{_digest(identity)}",
            capsule_id=capsule_id,
            proposal=None,
            audit=audit,
            attempt_audits=(audit,),
        )
        failure_context = context.model_copy(
            update={"source": "world-runtime:pinned-turn-pre-provider-failure"}
        )
        try:
            return await self._record(result, failure_context)
        except ConcurrencyConflict as exc:
            if expression_attempt_id is None:
                raise
            return await self._record_content_free_result_at_current_head(
                result=result,
                context=failure_context,
                observation=observation,
                observation_event=observation_event,
                observation_world_revision=observation_world_revision,
                expression_attempt_id=expression_attempt_id,
                cause=exc,
            )

    @staticmethod
    def _require_current_expression_attempt(
        *,
        projection: LedgerProjection,
        observation: Observation,
        expression_attempt_id: str,
    ) -> None:
        trigger_id = expression_episode_trigger_id(
            observation.world_id,
            observation.observation_id,
        )
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == trigger_id
            ),
            None,
        )
        if (
            process is None
            or process.process_kind != "expression_episode"
            or process.source_evidence_ref != observation.observation_id
            or process.state != "claimed"
            or process.claim_lease is None
            or process.claim_lease.attempt_id != expression_attempt_id
            or not process.attempt_ids
            or process.attempt_ids[-1] != expression_attempt_id
        ):
            raise ValueError(
                "Pinned reply attempt must be the current expression episode claim "
                "for its Observation"
            )

    async def audit_appraisal_accepted(
        self,
        *,
        appraisal_event: WorldEvent,
        cursor: ProjectionCursor,
        attempt_namespace: str | None = None,
    ) -> ProposalAuditCommit:
        """Audit one fresh affect deliberation after an accepted Appraisal.

        This deliberately has no classifier side path: Appraisal is already a
        source-bound interpretation.  The subsequent model is asked only
        whether that fresh state warrants an Affect proposal; it cannot reuse
        the stale user-message turn or fabricate a new appraisal source.
        """

        if attempt_namespace is not None and (not attempt_namespace or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in attempt_namespace
        )):
            raise ValueError("Pinned turn appraisal attempt namespace is invalid")
        if appraisal_event.world_id != self._ledger.world_id:
            raise ValueError("Pinned turn appraisal belongs to another world")
        if appraisal_event.event_type != "AppraisalAccepted":
            raise ValueError("Pinned turn affect trigger requires AppraisalAccepted")
        stored = await self._lookup_event_commit(appraisal_event.event_id)
        # The source Appraisal is immutable evidence, rather than the state
        # proposed by this turn. Opening/claiming its durable affect trigger
        # can legitimately advance the ledger before this worker runs, so the
        # source need only be present in the pinned projection. Ledger
        # sequence is the total order enforced by ``project_at``.
        if (
            stored is None
            or stored[0] != appraisal_event
            or stored[1].ledger_sequence > cursor.ledger_sequence
        ):
            raise ValueError("Pinned turn appraisal event is not the committed authority")
        projection = await self._project_at(cursor)
        query = query_from_projection(
            projection,
            actor_ref=self._companion_actor_ref,
            trigger_ref=appraisal_event.event_id,
        )
        try:
            capsule = await self._compile_capsule(query)
        except ValueError as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        deliberate_kwargs = dict(
            # Affect and relationship both deliberate after the same immutable
            # appraisal.  Their expensive calls must have distinct durable
            # attempt identities; otherwise the second lane aliases the first
            # lane's ModelResultRecorded audit on recovery.
            attempt_id=_attempt_id(
                trigger_ref=appraisal_event.event_id,
                cursor=cursor,
                namespace=attempt_namespace,
            ),
            trigger_evidence=(
                ProposalEvidenceRef(
                    ref_id=appraisal_event.event_id,
                    evidence_kind="committed_world_event",
                    source_world_revision=stored[1].world_revision,
                    immutable_hash="sha256:" + appraisal_event.payload_hash,
                ),
            ),
        )
        if self._affect_target_bounds_enabled:
            deliberate_kwargs["affect_target_bounds"] = lower_bounds_from_projection(
                projection
            )
        result = await self._deliberation.deliberate(capsule, **deliberate_kwargs)
        context = ProposalAuditContext(
            world_id=appraisal_event.world_id,
            trigger_ref=appraisal_event.event_id,
            logical_time=projection.logical_time or appraisal_event.logical_time,
            created_at=appraisal_event.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:pinned-affect-turn",
            trace_id=appraisal_event.trace_id,
            causation_id=appraisal_event.event_id,
            correlation_id=appraisal_event.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            expected_ledger_sequence=cursor.ledger_sequence,
        )
        try:
            return await self._record(result, context)
        except ConcurrencyConflict as exc:
            await self._raise_if_stale(cursor, exc)
            raise

    async def audit_relationship_source(
        self,
        *,
        source_event: WorldEvent,
        cursor: ProjectionCursor,
    ) -> ProposalAuditCommit:
        """Audit one relationship interpretation from exact committed source.

        Accepted Appraisals retain their historical path and identities.
        Ordinary ``ObservationRecorded`` messages use the same pinned Context
        Capsule and relationship model without first inventing an appraisal or
        assigning a deterministic social score.
        """

        if source_event.event_type == "AppraisalAccepted":
            return await self.audit_appraisal_accepted(
                appraisal_event=source_event,
                cursor=cursor,
                attempt_namespace="relationship",
            )
        if source_event.event_type != "ObservationRecorded":
            raise ValueError("Pinned relationship source kind is unsupported")
        if source_event.world_id != self._ledger.world_id:
            raise ValueError("Pinned relationship source belongs to another world")
        stored = await self._lookup_event_commit(source_event.event_id)
        if (
            stored is None
            or stored[0] != source_event
            or stored[1].ledger_sequence > cursor.ledger_sequence
        ):
            raise ValueError("Pinned relationship observation is not committed authority")
        observation = Observation.model_validate_json(source_event.payload_json)
        projection = await self._project_at(cursor)
        reference = next(
            (
                item
                for item in projection.message_observations
                if item.observation_id == observation.observation_id
                and item.source == observation.source
                and item.source_event_id == observation.source_event_id
            ),
            None,
        )
        if (
            reference is None
            or reference.event_payload_hash != source_event.payload_hash
            or reference.content_payload_hash != observation.payload_hash
            or reference.world_revision != stored[1].world_revision
            or reference.actor != observation.actor
        ):
            raise ValueError("Pinned relationship observation projection is not source-complete")
        query = query_from_projection(
            projection,
            actor_ref=self._companion_actor_ref,
            trigger_ref=source_event.event_id,
        )
        try:
            capsule = await self._compile_capsule(query)
        except ValueError as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        result = await self._deliberation.deliberate(
            capsule,
            attempt_id=_attempt_id(
                trigger_ref=source_event.event_id,
                cursor=cursor,
                namespace="relationship",
            ),
            trigger_evidence=(
                ProposalEvidenceRef(
                    ref_id=source_event.event_id,
                    evidence_kind="committed_world_event",
                    source_world_revision=stored[1].world_revision,
                    immutable_hash="sha256:" + source_event.payload_hash,
                ),
            ),
        )
        context = ProposalAuditContext(
            world_id=source_event.world_id,
            trigger_ref=source_event.event_id,
            logical_time=projection.logical_time or source_event.logical_time,
            created_at=source_event.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:pinned-relationship-turn",
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            expected_ledger_sequence=cursor.ledger_sequence,
        )
        try:
            return await self._record(result, context)
        except ConcurrencyConflict as exc:
            await self._raise_if_stale(cursor, exc)
            raise

    async def _raise_if_stale(self, cursor: ProjectionCursor, cause: Exception) -> None:
        current = await self._project()
        if (
            current.world_revision != cursor.world_revision
            or current.deliberation_revision != cursor.deliberation_revision
            or current.ledger_sequence != cursor.ledger_sequence
        ):
            raise ConcurrencyConflict("Pinned turn cursor became stale") from cause

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _lookup_event_commit(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _project_at(self, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    async def _compile_capsule(self, query):
        if self._relationship_evaluation:
            compile_relationship = getattr(
                self._capsules, "compile_for_relationship_deliberation", None
            )
            if not callable(compile_relationship):
                raise ValueError("Context Capsule compiler lacks relationship deliberation support")
            return await asyncio.to_thread(compile_relationship, query)
        return await asyncio.to_thread(self._capsules.compile_for_deliberation, query)

    def _expectation_advisories(
        self,
        projection: LedgerProjection,
        *,
        observation_event: WorldEvent,
        source_world_revision: int,
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the deterministic pending-expectation advisory, if opted in.

        The revision bound keeps causality honest: an inbound message may
        only be weighed against a hope she declared before it arrived.
        """

        if not self._pending_expectation_advisory:
            return ()
        try:
            view = pending_response_expectation(
                projection, before_world_revision=source_world_revision
            )
        except (TypeError, ValueError):
            # Expectation advice is best-effort context.  A projection defect
            # must not make a normal user turn fail.
            return ()
        if view is None:
            return ()
        return (
            response_expectation_advisory(
                view,
                source_ref=observation_event.event_id,
                logical_time=projection.logical_time or observation_event.logical_time,
            ),
        )

    def _aspiration_advisories(
        self, projection: LedgerProjection
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the deterministic active-wish advisory.

        Best-effort context like the expectation advisory: a defect here must
        never make an ordinary turn fail, it only omits the wish texture.
        """

        try:
            return active_aspiration_advisories(projection)
        except (TypeError, ValueError):
            return ()

    def _change_phase_view_advisories(
        self, projection: LedgerProjection
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the deterministic Change Phase advisory, if opted in.

        Best-effort context like the wish advisory: expression should feel
        the difference between "刚陷入低落" and "正在走出低落", but a defect
        here must never fail an ordinary turn.
        """

        if not self._change_phase_advisory:
            return ()
        try:
            return change_phase_advisories(projection)
        except (TypeError, ValueError):
            return ()

    def _npc_relationship_view_advisories(
        self, projection: LedgerProjection
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the deterministic per-NPC relationship advisory, if opted in."""

        if not self._npc_relationship_advisory:
            return ()
        try:
            return npc_relationship_advisories(
                projection,
                protagonist_actor_ref=self._companion_actor_ref,
            )
        except (TypeError, ValueError):
            return ()

    def _shared_private_invitation_view_advisories(
        self, projection: LedgerProjection
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the pending shared_private invitation advisory, if opted in."""

        if not self._shared_private_invitation_advisory:
            return ()
        try:
            return pending_shared_private_invitation_advisories(projection)
        except (TypeError, ValueError):
            return ()

    async def _compile_capsule_with_extra(
        self, query, extra: tuple[InnerAdvisoryProjection, ...]
    ):
        if not extra:
            return await self._compile_capsule(query)
        return await asyncio.to_thread(
            self._capsules.compile_for_deliberation_with_advisories, query, extra
        )

    async def _compile_capsule_with_source_context(
        self,
        *,
        query,
        projection: LedgerProjection,
        observation_event: WorldEvent,
        latency_trace: TurnLatencyTrace | None = None,
        source_context_advisories: tuple[InnerAdvisoryProjection, ...] = (),
    ):
        # These are source-bound projection views (for example an already
        # accepted aspiration or an unexpired response expectation), not a
        # second semantic interpretation of the current Observation.  The
        # canonical CharacterInterior author alone forms current Appraisal,
        # Affect, relationship stance and private self in its inner turn.
        del projection, observation_event
        operation = self._compile_capsule_with_extra(query, source_context_advisories)
        if latency_trace is None:
            return await operation
        async with latency_trace.measure("context"):
            return await operation

    @staticmethod
    def _reply_target(observation: Observation) -> str:
        """Read the platform target from the immutable observation, never a model choice."""

        context = observation.reply_context
        target = context.get("target") if isinstance(context, dict) else None
        return target if isinstance(target, str) and target else observation.actor

    @classmethod
    def _trigger_message(
        cls,
        observation: Observation,
        observation_event: WorldEvent,
        *,
        source_world_revision: int,
    ) -> TriggerMessage | None:
        """Expose text and bounded attachment tokens, never fabricated contents."""

        if observation.text is None and not observation.attachment_refs:
            return None
        reply_context = observation.reply_context or {}
        platform_message_id = reply_context.get("platform_message_id")
        attention_advisory = None
        raw_attention_advisory = observation.coalescing_metadata.get(
            "turn_attention_advisory"
        )
        if isinstance(raw_attention_advisory, dict):
            try:
                attention_advisory = TurnAttentionAdvisory.model_validate(
                    raw_attention_advisory
                )
            except (TypeError, ValueError):
                # Endpoint evidence is optional provider-local advice. A
                # malformed/recovered batch still reaches the role with the
                # verified message and no synthetic replacement.
                attention_advisory = None
        return TriggerMessage(
            event_ref=observation_event.event_id,
            event_payload_hash=f"sha256:{observation_event.payload_hash}",
            observation_ref=observation.observation_id,
            source_world_revision=source_world_revision,
            actor=observation.actor,
            channel=observation.channel,
            reply_target=cls._reply_target(observation),
            platform_message_id=(
                platform_message_id
                if isinstance(platform_message_id, str) and platform_message_id
                else None
            ),
            text=observation.text,
            attachment_refs=observation.attachment_refs,
            attachment_media_types=tuple(
                cls._attachment_media_type(item) for item in observation.attachment_refs
            ),
            turn_attention_advisory=attention_advisory,
        )

    @staticmethod
    def _attachment_media_type(
        attachment_ref: str,
    ) -> Literal["image", "audio", "video", "file", "unknown"]:
        """Read only the provider-normalized type prefix; never dereference content."""

        tokens = tuple(token.lower() for token in attachment_ref.split(":"))
        if "image" in tokens:
            return "image"
        if "record" in tokens or "audio" in tokens:
            return "audio"
        if "video" in tokens:
            return "video"
        if "file" in tokens:
            return "file"
        return "unknown"

    async def _record(
        self, result, context: ProposalAuditContext
    ) -> ProposalAuditCommit:
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._recorder.record, result, context)
        return self._recorder.record(result, context)


__all__ = ["PinnedTurnCompiler"]
