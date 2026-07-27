"""Recovery-safe background acceptance for source-bound user Facts.

The visible reply lane records only an ``interaction_fact`` opportunity.  This
worker later rereads the exact committed message, asks a constrained model for
one candidate, records its immutable audit, and drives the existing Fact-v2
acceptance authority.  It never lets a model write a Fact or choose evidence.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import logging
from typing import Literal

from .errors import ConcurrencyConflict
from .event_identity import domain_idempotency_key
from .fact_accepted_contracts import rehydrate_fact_commit_intent_v2_json
from .fact_correction_lifecycle import FactCorrectionLifecycle
from .fact_draft_adapter import (
    FactDraftTechnicalFailure,
    FactObservationProposalAdapter,
    FactWithdrawalDraft,
)
from .fact_memory_candidate_lifecycle import FactMemoryCandidateLifecycle
from .fact_memory_decision import (
    FactMemoryDecisionRecordedPayload,
    canonical_fact_memory_decision_json,
    fact_memory_decision_hash,
)
from .fact_memory_draft import (
    FactMemoryDraftAdapter,
    FactMemoryDraftTechnicalFailure,
    FactMemoryRetentionDraft,
)
from .fact_trigger import (
    INTERACTION_FACT_RETRY_DELAYS_SECONDS,
    InteractionFactDecisionRecordedPayload,
    InteractionFactTechnicalFailurePayload,
    fact_memory_decision_event_id,
    fact_memory_decision_identity,
    interaction_fact_decision_event_id,
    interaction_fact_decision_identity,
    interaction_fact_failure_event_id,
)
from .interaction_fact_decision import (
    canonical_interaction_fact_decision_json,
    interaction_fact_decision_hash,
)
from .fact_proposal_audit_v2 import build_fact_commit_proposal_recorded_event_v2
from .proposal_envelope_v2 import (
    FactCommitProposalDraftV2,
    FactCommitProposalEnvelopeV2,
    FactCommitProposalNormalizationContextV2,
    canonical_fact_commit_proposal_v2_json,
    normalize_fact_commit_proposal_v2,
    validate_fact_commit_proposal_v2,
)
from .fact_reducers import INSTALLED_FACT_PREDICATE_CARDINALITY
from .fact_v2_acceptance_envelope_authority import FactV2AcceptanceEnvelopeRequestV2
from .fact_v2_acceptance_runtime import FactV2AcceptanceRuntime
from .ledger import ObservationEventLocator
from .schema_core import FrozenModel
from .schemas import ClaimLease, Observation, ProjectionCursor, TriggerProcess, WorldEvent
from .sealed_fact_commit_adapter_v2 import FactCommitPolicyResolutionV2
from .sqlite_ledger import SQLiteWorldLedger


logger = logging.getLogger(__name__)
_NO_FACT_DECISION = object()
_NO_MEMORY_DECISION = object()
_STALE_MEMORY_DECISION = object()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FactTriggerRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "completed_existing", "processed"]
    work_status: Literal["no_change", "accepted", "technical_failure"] | None = None


class InteractionFactTriggerRuntime:
    """Drain one claimed-or-open ``interaction_fact`` trigger."""

    def __init__(
        self,
        *,
        ledger: SQLiteWorldLedger,
        acceptance: FactV2AcceptanceRuntime,
        adapter: FactObservationProposalAdapter,
        memory_adapter: FactMemoryDraftAdapter | None = None,
        memory_lifecycle: FactMemoryCandidateLifecycle | None = None,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:interaction-fact-trigger-runtime",
    ) -> None:
        if type(ledger) is not SQLiteWorldLedger or acceptance.ledger is not ledger:
            raise ValueError("Fact trigger must use the acceptance runtime's exact SQLite ledger")
        if not owner_id or lease_seconds <= 0:
            raise ValueError("Fact trigger runtime needs an owner and positive lease")
        if (memory_adapter is None) != (memory_lifecycle is None):
            raise ValueError("Fact memory adapter and lifecycle must be configured together")
        self._ledger = ledger
        self._acceptance = acceptance
        self._adapter = adapter
        self._memory_adapter = memory_adapter
        self._memory_lifecycle = memory_lifecycle
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> FactTriggerRunResult:
        projection = await self._project()
        process = await self._next_process(projection)
        if process is None:
            return FactTriggerRunResult(trigger_id="", status="idle")
        source_event, observation = await self._source_observation(
            process, self._cursor(projection)
        )
        active = await self._claim_or_reclaim(
            process=process, source_event=source_event, projection=projection
        )
        if active is None:
            return FactTriggerRunResult(trigger_id=process.trigger_id, status="owned_elsewhere")

        before = await self._project()
        cursor = self._cursor(before)
        # The observation is historical evidence, but Fact materialization is
        # a lived-world mutation.  A scheduler may advance the durable clock
        # between opening the trigger and accepting its proposal; reducers
        # deliberately reject an accepted mutation stamped with that older
        # observation time.  Rebase the acceptance envelope to the current
        # authoritative world time while retaining the source observation's
        # exact timestamp in its evidence binding.
        acceptance_logical_time = before.logical_time or source_event.logical_time
        source_commit = await self._lookup_event_commit(source_event.event_id)
        if source_commit is None:
            raise ValueError("interaction fact source event is no longer available")
        source_world_revision = source_commit[1].world_revision
        current_single_fact_sources = await self._current_single_fact_sources(
            before,
            subject_ref=observation.actor,
        )
        fact_context_hash = _digest(
            self._single_fact_authority_context(
                before,
                subject_ref=observation.actor,
            )
        )
        recorded_decision = await self._existing_decision(
            trigger_id=active.trigger_id,
            source_event_ref=source_event.event_id,
            fact_context_hash=fact_context_hash,
            subject_ref=observation.actor,
        )
        decision_recorded = recorded_decision is not None
        proposal = (
            None if recorded_decision is _NO_FACT_DECISION else recorded_decision
        )
        if not decision_recorded:
            # Historical pre-decision-audit retain proposals remain
            # replayable only while the exact single-Fact authority visible at
            # their durable audit epoch is still current.  A changed epoch
            # requires a fresh role-model decision; rebasing old semantics
            # could otherwise overwrite a newer user fact.
            proposal = await self._existing_legacy_proposal(
                before,
                source_event_ref=source_event.event_id,
                subject_ref=observation.actor,
                fact_context_hash=fact_context_hash,
            )
        try:
            if not decision_recorded and proposal is None:
                proposal = await self._adapter.propose(
                    observation=observation,
                    observation_event=source_event,
                    source_world_revision=source_world_revision,
                    evaluated_world_revision=cursor.world_revision,
                    current_single_fact_sources=current_single_fact_sources,
                )
                recorded_decision = await self._record_decision(
                    process=active,
                    source_event=source_event,
                    observation=observation,
                    evaluated_cursor=cursor,
                    current_single_fact_sources=current_single_fact_sources,
                    fact_context_hash=fact_context_hash,
                    decision=proposal,
                )
                proposal = (
                    None
                    if recorded_decision is _NO_FACT_DECISION
                    else recorded_decision
                )
                before = await self._project()
                cursor = self._cursor(before)
                acceptance_logical_time = before.logical_time or source_event.logical_time
        except FactDraftTechnicalFailure as failure:
            await self._record_technical_failure(
                process=active,
                source_event=source_event,
                phase="fact_draft",
                failure_code=failure.failure_code,
            )
            return FactTriggerRunResult(
                trigger_id=active.trigger_id,
                status="processed",
                work_status="technical_failure",
            )
        if fact_context_hash != _digest(
            self._single_fact_authority_context(
                before,
                subject_ref=observation.actor,
            )
        ):
            # The immutable result remains auditable under its old Context
            # epoch. A later wake will ask the role model again against the
            # new exact Fact authority instead of rebinding old semantics.
            raise ConcurrencyConflict(
                "interaction Fact decision Context changed before effect"
            )
        if (
            isinstance(proposal, FactCommitProposalEnvelopeV2)
            and proposal.evaluated_world_revision != cursor.world_revision
        ):
            proposal = self._rebase_retained_proposal(
                proposal,
                evaluated_world_revision=cursor.world_revision,
            )
        if isinstance(proposal, FactWithdrawalDraft):
            withdrawn_fact = next(
                (
                    item
                    for item in before.facts
                    if item.values.status == "active"
                    and item.values.cardinality == "single"
                    and item.values.subject_ref == observation.actor
                    and item.values.predicate_code == proposal.predicate_code
                ),
                None,
            )
            if withdrawn_fact is None:
                await self._complete(
                    process=active,
                    source_event=source_event,
                    cursor=cursor,
                    outcome_ref=f"outcome:{active.trigger_id}:withdraw-no-current-slot",
                )
                return FactTriggerRunResult(
                    trigger_id=active.trigger_id,
                    status="processed",
                    work_status="no_change",
                )
            withdrawn = await asyncio.to_thread(
                FactCorrectionLifecycle(
                    ledger=self._ledger,
                    actor=self._owner_id,
                    source=self._source,
                ).withdraw,
                before=withdrawn_fact,
                observation=observation,
                observation_event=source_event,
                observation_world_revision=source_world_revision,
                logical_time=acceptance_logical_time,
                created_at=max(
                    source_event.created_at,
                    source_event.logical_time,
                    acceptance_logical_time,
                ),
            )
            cursor = self._cursor(await self._project())
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:withdrawn:{withdrawn.fact_id}",
            )
            return FactTriggerRunResult(
                trigger_id=active.trigger_id,
                status="processed",
                work_status="accepted",
            )
        if proposal is None:
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:no-change",
            )
            return FactTriggerRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )

        intent = rehydrate_fact_commit_intent_v2_json(
            proposal.proposed_changes[0].payload.canonical_json
        )
        # Repeated observations often restate the same user fact.  The fact
        # reducer correctly rejects a duplicate semantic identity, but a
        # scheduler wake must treat that durable state as an idempotent
        # no-change rather than surfacing a 500 and leaving the trigger open.
        duplicate_fact = next(
            (
                item
                for item in before.facts
                if item.values.status == "active"
                and item.values.subject_ref == intent.subject_ref
                and item.values.predicate_code == intent.predicate_code
                and item.values.value_hash == str(intent.value_hash).removeprefix("sha256:")
            ),
            None,
        )
        if duplicate_fact is not None:
            if (
                self._memory_adapter is not None
                and duplicate_fact.values.assertion_binding.source_ref
                == observation.observation_id
            ):
                try:
                    await self._materialize_memory(
                        process=active,
                        accepted_event_ids=(
                            duplicate_fact.origin.accepted_event_ref,
                        ),
                        observation=observation,
                        source_event=source_event,
                    )
                except FactMemoryDraftTechnicalFailure as failure:
                    await self._record_technical_failure(
                        process=active,
                        source_event=source_event,
                        phase="memory_draft",
                        failure_code=failure.failure_code,
                    )
                    return FactTriggerRunResult(
                        trigger_id=active.trigger_id,
                        status="processed",
                        work_status="technical_failure",
                    )
                cursor = self._cursor(await self._project())
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:duplicate-fact",
            )
            return FactTriggerRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )

        existing_audit = next(
            (
                item
                for item in before.fact_commit_proposal_audits_v2
                if item.proposal_id == proposal.proposal_id
            ),
            None,
        )
        if (
            existing_audit is not None
            and existing_audit.proposal_json
            != canonical_fact_commit_proposal_v2_json(
                proposal,
                world_id=self._ledger.world_id,
            )
        ):
            # A pre-context-epoch proposal ID intentionally omitted model
            # fields so old crash recovery could find it.  After the Fact
            # context changes, a fresh role decision may retain the same
            # source value with different privacy/confidence/rationale.
            # Preserve that new decision under a context-bound identity
            # instead of pinning incompatible bytes from the legacy audit.
            proposal = self._contextualize_retained_proposal(
                proposal,
                fact_context_hash=fact_context_hash,
            )
            existing_audit = next(
                (
                    item
                    for item in before.fact_commit_proposal_audits_v2
                    if item.proposal_id == proposal.proposal_id
                ),
                None,
            )
            if (
                existing_audit is not None
                and existing_audit.proposal_json
                != canonical_fact_commit_proposal_v2_json(
                    proposal,
                    world_id=self._ledger.world_id,
                )
            ):
                raise ValueError(
                    "context-bound interaction Fact proposal identity collision"
                )
        if existing_audit is None:
            audit_event = build_fact_commit_proposal_recorded_event_v2(
                proposal=proposal,
                world_id=self._ledger.world_id,
                logical_time=source_event.logical_time,
                created_at=max(source_event.created_at, source_event.logical_time),
                actor=self._owner_id,
                source=self._source,
                trace_id=source_event.trace_id,
                causation_id=source_event.event_id,
                correlation_id=source_event.correlation_id,
            )
            audit_commit = await self._commit_at_cursor(
                (audit_event,),
                cursor=cursor,
                commit_id="commit:interaction-fact:audit:" + _digest(proposal.proposal_id),
            )
            audit_cursor = self._cursor_from_commit(audit_commit)
        else:
            # Crash recovery joins the immutable audit already committed for
            # this exact source.
            audit_cursor = cursor

        conflicting_single_fact = next(
            (
                item
                for item in before.facts
                if item.values.status == "active"
                and item.values.subject_ref == intent.subject_ref
                and item.values.predicate_code == intent.predicate_code
                and item.values.value_hash != str(intent.value_hash).removeprefix("sha256:")
                and item.values.cardinality == "single"
            ),
            None,
        )
        if conflicting_single_fact is not None:
            corrected = await asyncio.to_thread(
                FactCorrectionLifecycle(
                    ledger=self._ledger,
                    actor=self._owner_id,
                    source=self._source,
                ).correct,
                before=conflicting_single_fact,
                intent=intent,
                observation=observation,
                observation_event=source_event,
                observation_world_revision=source_world_revision,
                logical_time=acceptance_logical_time,
                created_at=max(
                    source_event.created_at,
                    source_event.logical_time,
                    acceptance_logical_time,
                ),
            )
            if self._memory_adapter is not None:
                try:
                    await self._materialize_memory(
                        process=active,
                        accepted_event_ids=(corrected.origin.accepted_event_ref,),
                        observation=observation,
                        source_event=source_event,
                    )
                except FactMemoryDraftTechnicalFailure as failure:
                    await self._record_technical_failure(
                        process=active,
                        source_event=source_event,
                        phase="memory_draft",
                        failure_code=failure.failure_code,
                    )
                    return FactTriggerRunResult(
                        trigger_id=active.trigger_id,
                        status="processed",
                        work_status="technical_failure",
                    )
            cursor = self._cursor(await self._project())
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:corrected:{corrected.fact_id}",
            )
            return FactTriggerRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="accepted"
            )

        handle = self._acceptance.pin_proposal(
            cursor=audit_cursor, proposal_id=proposal.proposal_id
        )
        sources = self._acceptance.resolve_sources(
            cursor=audit_cursor,
            intent=intent,
            locators=(
                ObservationEventLocator.for_message(
                    world_id=self._ledger.world_id,
                    observation_id=observation.observation_id,
                    source=observation.source,
                    source_event_id=observation.source_event_id,
                ),
            ),
        )
        prepared = self._acceptance.prepare(
            proposal_handle=handle,
            change_id=proposal.proposed_changes[0].change_id,
            policy=FactCommitPolicyResolutionV2(
                cardinality=INSTALLED_FACT_PREDICATE_CARDINALITY[intent.predicate_code],
                policy_refs=("policy:fact-commit.2",),
            ),
        )
        identity = _digest({"proposal_id": proposal.proposal_id, "trigger_id": active.trigger_id})
        try:
            accepted = self._acceptance.accept(
                request=FactV2AcceptanceEnvelopeRequestV2(
                    acceptance_id=f"acceptance:interaction-fact:{identity}",
                    acceptance_event_id=f"event:interaction-fact:accepted:{identity}",
                    acceptance_causation_id=self._acceptance.proposal_audit_event_ref(
                        proposal_handle=handle
                    ),
                    cursor=audit_cursor,
                    world_id=self._ledger.world_id,
                    logical_time=acceptance_logical_time,
                    # A scheduler wake may have rebased ``acceptance_logical_time``
                    # beyond the historical observation.  The envelope invariant
                    # still requires its creation boundary to be at least that
                    # logical time; otherwise delayed fact materialization raises
                    # before it can complete the trigger and blocks all later
                    # scheduled work (including proactive contact).
                    created_at=max(
                        source_event.created_at,
                        source_event.logical_time,
                        acceptance_logical_time,
                    ),
                    actor=self._owner_id,
                    source=self._source,
                    trace_id=source_event.trace_id,
                    correlation_id=source_event.correlation_id,
                ),
                proposal_handle=handle,
                prepared=prepared,
                sources=sources,
            )
        except ConcurrencyConflict:
            # A cursor race is retryable; the durable audit is rejoined on the
            # next wake and acceptance is attempted again at a fresh cursor.
            raise
        if self._memory_adapter is not None:
            assert self._memory_lifecycle is not None
            try:
                await self._materialize_memory(
                    process=active,
                    accepted_event_ids=accepted.event_ids,
                    observation=observation,
                    source_event=source_event,
                )
            except FactMemoryDraftTechnicalFailure as failure:
                await self._record_technical_failure(
                    process=active,
                    source_event=source_event,
                    phase="memory_draft",
                    failure_code=failure.failure_code,
                )
                return FactTriggerRunResult(
                    trigger_id=active.trigger_id,
                    status="processed",
                    work_status="technical_failure",
                )
        completion_cursor = self._cursor(await self._project())
        await self._complete(
            process=active,
            source_event=source_event,
            cursor=completion_cursor,
            outcome_ref=f"outcome:{active.trigger_id}:accepted:{proposal.proposal_id}",
        )
        return FactTriggerRunResult(
            trigger_id=active.trigger_id, status="processed", work_status="accepted"
        )

    async def _next_process(self, projection) -> TriggerProcess | None:
        candidates = tuple(
            item
            for item in projection.trigger_processes
            if item.process_kind == "interaction_fact" and item.state != "terminal"
        )
        # New observations must never queue behind a provider failure that is
        # waiting for retry. Once the open work is drained, due retries resume.
        for process in candidates:
            if process.state == "open":
                return process
        at = projection.logical_time
        for process in candidates:
            if process.claim_lease is None:
                continue
            failure = await self._technical_failure(process)
            if failure is not None and (
                at is None or at < failure.next_retry_at
            ):
                continue
            if (
                process.claim_lease.owner_id != self._owner_id
                and at is not None
                and at < process.claim_lease.expires_at
            ):
                continue
            return process
        return None

    async def _technical_failure(
        self, process: TriggerProcess
    ) -> InteractionFactTechnicalFailurePayload | None:
        if process.claim_lease is None:
            return None
        stored = await self._lookup_event_commit(
            interaction_fact_failure_event_id(
                trigger_id=process.trigger_id,
                attempt_id=process.claim_lease.attempt_id,
            )
        )
        if stored is None:
            return None
        event, _commit = stored
        if event.event_type != "InteractionFactTechnicalFailureRecorded":
            raise ValueError("interaction fact retry identity resolved to another event")
        return InteractionFactTechnicalFailurePayload.model_validate_json(
            event.payload_json
        )

    async def _record_technical_failure(
        self,
        *,
        process: TriggerProcess,
        source_event: WorldEvent,
        phase: Literal["fact_draft", "memory_draft"],
        failure_code: str,
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("interaction fact failure requires a claimed process")
        existing = await self._technical_failure(process)
        if existing is not None:
            return
        projection = await self._project()
        failed_at = max(
            projection.logical_time or source_event.logical_time,
            process.claim_lease.acquired_at,
        )
        retry_ordinal = len(process.attempt_ids)
        delay_seconds = INTERACTION_FACT_RETRY_DELAYS_SECONDS[
            min(max(retry_ordinal, 1), len(INTERACTION_FACT_RETRY_DELAYS_SECONDS)) - 1
        ]
        payload = InteractionFactTechnicalFailurePayload(
            trigger_id=process.trigger_id,
            attempt_id=process.claim_lease.attempt_id,
            phase=phase,
            failure_code=(failure_code or "unknown_failure")[:128],
            failed_at=failed_at,
            retry_ordinal=retry_ordinal,
            next_retry_at=failed_at + timedelta(seconds=delay_seconds),
        )
        payload_json = payload.model_dump(mode="json")
        identity = domain_idempotency_key(
            event_type="InteractionFactTechnicalFailureRecorded",
            world_id=self._ledger.world_id,
            payload=payload_json,
        )
        if identity is None:
            raise ValueError("interaction fact technical failure has no domain identity")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=interaction_fact_failure_event_id(
                trigger_id=process.trigger_id,
                attempt_id=process.claim_lease.attempt_id,
            ),
            world_id=self._ledger.world_id,
            event_type="InteractionFactTechnicalFailureRecorded",
            logical_time=failed_at,
            created_at=max(source_event.created_at, failed_at),
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload_json,
        )
        await self._commit_at_cursor(
            (event,),
            cursor=self._cursor(projection),
            commit_id="commit:interaction-fact:technical-failure:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id]),
        )
        logger.warning(
            "interaction fact model failure scheduled for retry: trigger=%s "
            "phase=%s retry_ordinal=%s next_retry_at=%s failure=%s",
            process.trigger_id,
            phase,
            retry_ordinal,
            payload.next_retry_at.isoformat(),
            payload.failure_code,
        )

    async def _existing_legacy_proposal(
        self,
        projection,
        *,
        source_event_ref: str,
        subject_ref: str,
        fact_context_hash: str,
    ):
        matches = []
        for audit in projection.fact_commit_proposal_audits_v2:
            try:
                proposal = validate_fact_commit_proposal_v2(
                    json.loads(audit.proposal_json), world_id=self._ledger.world_id
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if proposal.trigger_ref == source_event_ref:
                matches.append((audit, proposal))
        if len(matches) > 1:
            raise ValueError("interaction fact source has multiple durable proposal audits")
        if not matches:
            return None
        audit, proposal = matches[0]
        stored = await self._lookup_event_commit(audit.event_ref)
        if stored is None:
            raise ValueError("legacy interaction Fact proposal audit is unavailable")
        if stored[1].world_revision != proposal.evaluated_world_revision:
            # The legacy envelope has no complete cursor.  If its durable
            # audit landed in another World epoch, no exact Fact context can
            # be reconstructed safely enough to reuse its semantic result.
            return None
        audited = await self._project_at(self._cursor_from_commit(stored[1]))
        if fact_context_hash != _digest(
            self._single_fact_authority_context(
                audited,
                subject_ref=subject_ref,
            )
        ):
            return None
        return proposal

    async def _existing_decision(
        self,
        *,
        trigger_id: str,
        source_event_ref: str,
        fact_context_hash: str,
        subject_ref: str,
    ):
        payload = await self._existing_decision_payload(
            trigger_id=trigger_id,
            source_event_ref=source_event_ref,
            fact_context_hash=fact_context_hash,
            subject_ref=subject_ref,
        )
        if payload is None:
            return None
        value = json.loads(payload.decision_json)
        if payload.decision_kind == "no_change":
            return _NO_FACT_DECISION
        if payload.decision_kind == "withdraw":
            return FactWithdrawalDraft.model_validate(value)
        proposal = validate_fact_commit_proposal_v2(
            value,
            world_id=self._ledger.world_id,
        )
        if proposal.trigger_ref != source_event_ref:
            raise ValueError("recorded interaction Fact proposal changed trigger")
        return proposal

    async def _existing_decision_payload(
        self,
        *,
        trigger_id: str,
        source_event_ref: str,
        fact_context_hash: str,
        subject_ref: str,
    ) -> InteractionFactDecisionRecordedPayload | None:
        stored = await self._lookup_event_commit(
            interaction_fact_decision_event_id(
                trigger_id=trigger_id,
                fact_context_hash=fact_context_hash,
            )
        )
        if stored is None:
            return None
        event, _commit = stored
        if event.event_type != "InteractionFactDecisionRecorded":
            raise ValueError("interaction Fact decision identity resolved to another event")
        payload = InteractionFactDecisionRecordedPayload.model_validate_json(
            event.payload_json
        )
        if (
            payload.trigger_id != trigger_id
            or payload.source_event_ref != source_event_ref
            or payload.fact_context_hash != fact_context_hash
        ):
            raise ValueError("interaction Fact decision does not bind its source")
        evaluated = await self._project_at(
            ProjectionCursor(
                world_revision=payload.evaluated_world_revision,
                deliberation_revision=payload.evaluated_deliberation_revision,
                ledger_sequence=payload.evaluated_ledger_sequence,
            )
        )
        if _digest(
            self._single_fact_authority_context(
                evaluated,
                subject_ref=subject_ref,
            )
        ) != payload.fact_context_hash:
            raise ValueError("interaction Fact decision Context hash is invalid")
        return payload

    @staticmethod
    def _single_fact_authority_context(
        projection,
        *,
        subject_ref: str,
    ) -> tuple[dict[str, object], ...]:
        """Canonical exact authority for every single-value Fact shown to the model."""

        return tuple(
            item.model_dump(mode="json")
            for item in sorted(
                (
                    fact
                    for fact in projection.facts
                    if fact.values.status == "active"
                    and fact.values.cardinality == "single"
                    and fact.values.subject_ref == subject_ref
                ),
                key=lambda fact: (fact.values.predicate_code, fact.fact_id),
            )
        )

    def _rebase_retained_proposal(
        self,
        proposal: FactCommitProposalEnvelopeV2,
        *,
        evaluated_world_revision: int,
    ) -> FactCommitProposalEnvelopeV2:
        """Rebind unchanged model semantics after an irrelevant cursor advance."""

        draft = FactCommitProposalDraftV2(
            fact_commit_intents=tuple(
                rehydrate_fact_commit_intent_v2_json(
                    change.payload.canonical_json
                )
                for change in proposal.proposed_changes
            ),
            confidence=proposal.confidence,
            brief_rationale=proposal.brief_rationale,
        )
        context = FactCommitProposalNormalizationContextV2(
            world_id=self._ledger.world_id,
            proposal_id=proposal.proposal_id,
            trigger_ref=proposal.trigger_ref,
            evaluated_world_revision=evaluated_world_revision,
            evidence_refs=proposal.evidence_refs,
            policy_refs=proposal.proposed_changes[0].policy_refs,
        )
        return normalize_fact_commit_proposal_v2(draft=draft, context=context)

    def _contextualize_retained_proposal(
        self,
        proposal: FactCommitProposalEnvelopeV2,
        *,
        fact_context_hash: str,
    ) -> FactCommitProposalEnvelopeV2:
        intents = tuple(
            rehydrate_fact_commit_intent_v2_json(
                change.payload.canonical_json
            )
            for change in proposal.proposed_changes
        )
        draft = FactCommitProposalDraftV2(
            fact_commit_intents=intents,
            confidence=proposal.confidence,
            brief_rationale=proposal.brief_rationale,
        )
        contextual_id = "proposal:fact-observation-context:" + _digest(
            {
                "legacy_proposal_id": proposal.proposal_id,
                "fact_context_hash": fact_context_hash,
                "fact_commit_intents": tuple(
                    item.model_dump(mode="json") for item in intents
                ),
                "confidence": proposal.confidence,
                "brief_rationale": proposal.brief_rationale,
                # The normalized envelope and its eventual acceptance are
                # revision-pinned.  A Clock/world advance after the audit must
                # therefore produce a distinct recoverable identity even when
                # the exact Fact context and role-owned semantics are unchanged.
                "evaluated_world_revision": proposal.evaluated_world_revision,
            }
        )
        context = FactCommitProposalNormalizationContextV2(
            world_id=self._ledger.world_id,
            proposal_id=contextual_id,
            trigger_ref=proposal.trigger_ref,
            evaluated_world_revision=proposal.evaluated_world_revision,
            evidence_refs=proposal.evidence_refs,
            policy_refs=proposal.proposed_changes[0].policy_refs,
        )
        return normalize_fact_commit_proposal_v2(draft=draft, context=context)

    async def _record_decision(
        self,
        *,
        process: TriggerProcess,
        source_event: WorldEvent,
        observation: Observation,
        evaluated_cursor: ProjectionCursor,
        current_single_fact_sources: tuple[dict[str, object], ...],
        fact_context_hash: str,
        decision,
    ):
        if process.claim_lease is None:
            raise ValueError("interaction Fact decision requires a claimed process")
        if decision is None:
            decision_kind = "no_change"
            decision_value: object = {"decision": "no_change"}
        elif isinstance(decision, FactWithdrawalDraft):
            decision_kind = "withdraw"
            decision_value = decision.model_dump(mode="json")
        else:
            decision_kind = "retain"
            decision_value = decision.model_dump(mode="json")
        decision_json = canonical_interaction_fact_decision_json(decision_value)
        request_hash = _digest(
            {
                "adapter_version": self._adapter.adapter_version,
                "source_event_ref": source_event.event_id,
                "source_payload_hash": source_event.payload_hash,
                "evaluated_cursor": evaluated_cursor.model_dump(mode="json"),
                "current_single_fact_sources": current_single_fact_sources,
                "fact_context_hash": fact_context_hash,
            }
        )
        commit_cursor = evaluated_cursor
        for _attempt in range(8):
            projection = await self._project_at(commit_cursor)
            recorded_at = projection.logical_time or source_event.logical_time
            payload = InteractionFactDecisionRecordedPayload(
                decision_id=interaction_fact_decision_identity(
                    trigger_id=process.trigger_id,
                    fact_context_hash=fact_context_hash,
                ),
                trigger_id=process.trigger_id,
                attempt_id=process.claim_lease.attempt_id,
                source_event_ref=source_event.event_id,
                source_observation_ref=observation.observation_id,
                evaluated_world_revision=evaluated_cursor.world_revision,
                evaluated_deliberation_revision=(
                    evaluated_cursor.deliberation_revision
                ),
                evaluated_ledger_sequence=evaluated_cursor.ledger_sequence,
                adapter_version=self._adapter.adapter_version,
                model_id=self._adapter.model_id,
                request_hash=request_hash,
                fact_context_hash=fact_context_hash,
                decision_kind=decision_kind,
                decision_json=decision_json,
                decision_hash=interaction_fact_decision_hash(decision_json),
                recorded_at=recorded_at,
            )
            payload_json = payload.model_dump(mode="json")
            identity = domain_idempotency_key(
                event_type="InteractionFactDecisionRecorded",
                world_id=self._ledger.world_id,
                payload=payload_json,
            )
            if identity is None:
                raise ValueError("interaction Fact decision has no domain identity")
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=interaction_fact_decision_event_id(
                    trigger_id=process.trigger_id,
                    fact_context_hash=fact_context_hash,
                ),
                world_id=self._ledger.world_id,
                event_type="InteractionFactDecisionRecorded",
                logical_time=recorded_at,
                created_at=max(source_event.created_at, recorded_at),
                actor=self._owner_id,
                source=self._source,
                trace_id=source_event.trace_id,
                causation_id=source_event.event_id,
                correlation_id=source_event.correlation_id,
                idempotency_key=identity,
                payload=payload_json,
            )
            try:
                await self._commit_at_cursor(
                    (event,),
                    cursor=commit_cursor,
                    commit_id="commit:interaction-fact:decision:"
                    + _digest([process.trigger_id, fact_context_hash]),
                )
            except ConcurrencyConflict:
                existing = await self._existing_decision(
                    trigger_id=process.trigger_id,
                    source_event_ref=source_event.event_id,
                    fact_context_hash=fact_context_hash,
                    subject_ref=observation.actor,
                )
                if existing is not None:
                    return existing
                latest = await self._project()
                active = next(
                    (
                        item
                        for item in latest.trigger_processes
                        if item.trigger_id == process.trigger_id
                    ),
                    None,
                )
                if (
                    active is None
                    or active.state != "claimed"
                    or active.claim_lease is None
                    or active.claim_lease.attempt_id
                    != process.claim_lease.attempt_id
                    or _digest(
                        self._single_fact_authority_context(
                            latest,
                            subject_ref=observation.actor,
                        )
                    )
                    != fact_context_hash
                ):
                    raise ConcurrencyConflict(
                        "interaction Fact decision context changed before recording"
                    )
                commit_cursor = self._cursor(latest)
                continue
            return _NO_FACT_DECISION if decision is None else decision
        raise ConcurrencyConflict(
            "interaction Fact decision could not join a stable ledger cursor"
        )

    async def _materialize_memory(
        self,
        *,
        process: TriggerProcess,
        accepted_event_ids: tuple[str, ...],
        observation: Observation,
        source_event: WorldEvent,
    ) -> None:
        if observation.text is None:
            return
        projection = await self._project()
        fact = next(
            (
                item
                for item in projection.facts
                if item.origin.accepted_event_ref in accepted_event_ids
            ),
            None,
        )
        if fact is None:
            # A newer correction may have replaced this exact authority while
            # the older trigger was crashed. It is no longer eligible to
            # create or revise retrieval memory.
            return
        transition = next(
            item
            for item in projection.fact_transitions
            if item.transition_id == fact.origin.transition_id
        )
        stored = await self._lookup_event_commit(fact.origin.accepted_event_ref)
        if stored is None:
            raise ValueError("accepted Fact event is unavailable")
        fact_event, fact_commit = stored
        memory_logical_time = projection.logical_time or source_event.logical_time
        assert self._memory_adapter is not None
        decision = await self._existing_memory_decision(
            trigger_id=process.trigger_id,
            fact_authority_event_ref=fact_event.event_id,
        )
        if decision is None:
            draft = await self._memory_adapter.classify(
                predicate_code=fact.values.predicate_code,
                source_text=observation.text,
            )
            decision = await self._record_memory_decision(
                process=process,
                fact=fact,
                fact_event=fact_event,
                fact_world_revision=fact_commit.world_revision,
                observation=observation,
                source_event=source_event,
                evaluated_cursor=self._cursor(projection),
                source_text=observation.text,
                decision=draft,
            )
            projection = await self._project()
            memory_logical_time = projection.logical_time or source_event.logical_time
        if decision is _STALE_MEMORY_DECISION:
            return
        current_fact = next(
            (
                item
                for item in projection.facts
                if item.fact_id == fact.fact_id
                and item.entity_revision == fact.entity_revision
                and item.origin.accepted_event_ref == fact_event.event_id
                and item.values.status == "active"
            ),
            None,
        )
        if current_fact is None:
            return
        assert self._memory_lifecycle is not None
        if decision is _NO_MEMORY_DECISION:
            # ``no_change`` is not permission for the system to forget.
            # It only declines retaining the new Fact revision.  We may still
            # remove the now-invalid old Fact binding from a multi-source
            # candidate so independently current evidence remains retrievable;
            # a sole-source candidate stays active and merely suppresses.
            await asyncio.to_thread(
                self._memory_lifecycle.retire_superseded_fact_source,
                fact=fact,
                logical_time=memory_logical_time,
                created_at=max(source_event.created_at, memory_logical_time),
                trace_id=source_event.trace_id,
                correlation_id=source_event.correlation_id,
            )
            return
        if not isinstance(decision, FactMemoryRetentionDraft):
            raise TypeError("Fact-memory decision has an unsupported runtime value")
        await asyncio.to_thread(
            self._memory_lifecycle.accept,
            fact=fact,
            transition=transition,
            fact_event=fact_event,
            fact_world_revision=fact_commit.world_revision,
            draft=decision,
            logical_time=memory_logical_time,
            created_at=max(source_event.created_at, memory_logical_time),
            trace_id=source_event.trace_id,
            correlation_id=source_event.correlation_id,
        )

    async def _existing_memory_decision(
        self,
        *,
        trigger_id: str,
        fact_authority_event_ref: str,
    ):
        stored = await self._lookup_event_commit(
            fact_memory_decision_event_id(
                trigger_id=trigger_id,
                fact_authority_event_ref=fact_authority_event_ref,
            )
        )
        if stored is None:
            return None
        event, _commit = stored
        if event.event_type != "FactMemoryDecisionRecorded":
            raise ValueError("Fact-memory decision identity resolved to another event")
        payload = FactMemoryDecisionRecordedPayload.model_validate_json(
            event.payload_json
        )
        if (
            payload.trigger_id != trigger_id
            or payload.fact_authority_event_ref != fact_authority_event_ref
        ):
            raise ValueError("Fact-memory decision does not bind its source")
        if payload.decision_kind == "no_change":
            return _NO_MEMORY_DECISION
        return FactMemoryRetentionDraft.model_validate_json(payload.decision_json)

    async def _record_memory_decision(
        self,
        *,
        process: TriggerProcess,
        fact,
        fact_event: WorldEvent,
        fact_world_revision: int,
        observation: Observation,
        source_event: WorldEvent,
        evaluated_cursor: ProjectionCursor,
        source_text: str,
        decision: FactMemoryRetentionDraft | None,
    ):
        if process.claim_lease is None:
            raise ValueError("Fact-memory decision requires a claimed process")
        if decision is None:
            decision_kind = "no_change"
            decision_value: object = {"decision": "no_change"}
        else:
            decision_kind = "retain"
            decision_value = decision.model_dump(mode="json")
        decision_json = canonical_fact_memory_decision_json(decision_value)
        request_hash = _digest(
            {
                "adapter_version": self._memory_adapter.adapter_version,
                "fact": fact.model_dump(mode="json"),
                "fact_authority_event_ref": fact_event.event_id,
                "fact_authority_payload_hash": fact_event.payload_hash,
                "source_observation_ref": observation.observation_id,
                "source_text_hash": hashlib.sha256(source_text.encode()).hexdigest(),
                "evaluated_cursor": evaluated_cursor.model_dump(mode="json"),
            }
        )
        commit_cursor = evaluated_cursor
        for _attempt in range(8):
            projection = await self._project_at(commit_cursor)
            recorded_at = projection.logical_time or source_event.logical_time
            payload = FactMemoryDecisionRecordedPayload(
                decision_id=fact_memory_decision_identity(
                    trigger_id=process.trigger_id,
                    fact_authority_event_ref=fact_event.event_id,
                ),
                trigger_id=process.trigger_id,
                attempt_id=process.claim_lease.attempt_id,
                source_observation_ref=observation.observation_id,
                fact_id=fact.fact_id,
                fact_entity_revision=fact.entity_revision,
                fact_authority_event_ref=fact_event.event_id,
                fact_authority_world_revision=fact_world_revision,
                fact_authority_payload_hash=fact_event.payload_hash,
                evaluated_world_revision=evaluated_cursor.world_revision,
                evaluated_deliberation_revision=(
                    evaluated_cursor.deliberation_revision
                ),
                evaluated_ledger_sequence=evaluated_cursor.ledger_sequence,
                adapter_version=self._memory_adapter.adapter_version,
                model_id=self._memory_adapter.model_id,
                request_hash=request_hash,
                decision_kind=decision_kind,
                decision_json=decision_json,
                decision_hash=fact_memory_decision_hash(decision_json),
                recorded_at=recorded_at,
            )
            payload_json = payload.model_dump(mode="json")
            identity = domain_idempotency_key(
                event_type="FactMemoryDecisionRecorded",
                world_id=self._ledger.world_id,
                payload=payload_json,
            )
            if identity is None:
                raise ValueError("Fact-memory decision has no domain identity")
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=fact_memory_decision_event_id(
                    trigger_id=process.trigger_id,
                    fact_authority_event_ref=fact_event.event_id,
                ),
                world_id=self._ledger.world_id,
                event_type="FactMemoryDecisionRecorded",
                logical_time=recorded_at,
                created_at=max(source_event.created_at, recorded_at),
                actor=self._owner_id,
                source=self._source,
                trace_id=source_event.trace_id,
                causation_id=fact_event.event_id,
                correlation_id=source_event.correlation_id,
                idempotency_key=identity,
                payload=payload_json,
            )
            try:
                await self._commit_at_cursor(
                    (event,),
                    cursor=commit_cursor,
                    commit_id="commit:fact-memory:decision:"
                    + _digest(
                        [process.trigger_id, fact_event.event_id]
                    ),
                )
            except ConcurrencyConflict:
                existing = await self._existing_memory_decision(
                    trigger_id=process.trigger_id,
                    fact_authority_event_ref=fact_event.event_id,
                )
                if existing is not None:
                    return existing
                latest = await self._project()
                active = next(
                    (
                        item
                        for item in latest.trigger_processes
                        if item.trigger_id == process.trigger_id
                    ),
                    None,
                )
                current_fact = next(
                    (
                        item
                        for item in latest.facts
                        if item.fact_id == fact.fact_id
                        and item.entity_revision == fact.entity_revision
                        and item.origin.accepted_event_ref == fact_event.event_id
                        and item.values.status == "active"
                    ),
                    None,
                )
                if (
                    active is None
                    or active.state != "claimed"
                    or active.claim_lease is None
                    or active.claim_lease.attempt_id
                    != process.claim_lease.attempt_id
                ):
                    raise ConcurrencyConflict(
                        "Fact-memory decision attempt changed before recording"
                    )
                if current_fact is None:
                    return _STALE_MEMORY_DECISION
                commit_cursor = self._cursor(latest)
                continue
            return _NO_MEMORY_DECISION if decision is None else decision
        raise ConcurrencyConflict(
            "Fact-memory decision could not join a stable ledger cursor"
        )

    async def _source_observation(
        self, process: TriggerProcess, cursor: ProjectionCursor
    ) -> tuple[WorldEvent, Observation]:
        observation_id = process.source_evidence_ref
        if observation_id is None:
            raise ValueError("interaction fact trigger has no source observation")
        projection = await self._project_at(cursor)
        reference = next(
            (
                item
                for item in projection.message_observations
                if item.observation_id == observation_id
            ),
            None,
        )
        if reference is None or not reference.source or not reference.source_event_id:
            raise ValueError("interaction fact source observation is unavailable")
        locator = ObservationEventLocator.for_message(
            world_id=self._ledger.world_id,
            observation_id=observation_id,
            source=reference.source,
            source_event_id=reference.source_event_id,
        )
        events = await self._observation_events_at((locator,), cursor=cursor)
        if len(events) != 1 or events[0].event.event_type != "ObservationRecorded":
            raise ValueError("interaction fact source proof is incomplete")
        event = events[0].event
        observation = Observation.model_validate_json(event.payload_json)
        if (
            observation.observation_id != observation_id
            or event.world_id != self._ledger.world_id
            or process.trigger_ref != f"fact:{observation_id}"
        ):
            raise ValueError("interaction fact source proof does not bind its trigger")
        return event, observation

    async def _claim_or_reclaim(
        self, *, process: TriggerProcess, source_event: WorldEvent, projection
    ) -> TriggerProcess | None:
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if (
                process.claim_lease.owner_id == self._owner_id
                and at <= process.claim_lease.expires_at
            ):
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:interaction-fact:" + _digest(
            {"trigger_id": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
        )
        claimed = process.model_copy(
            update={
                "state": "claimed",
                "claim_lease": ClaimLease(
                    owner_id=self._owner_id,
                    attempt_id=attempt_id,
                    acquired_at=at,
                    expires_at=at + timedelta(seconds=self._lease_seconds),
                ),
                "attempt_ids": (*process.attempt_ids, attempt_id),
            }
        )
        event_type = (
            "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        )
        payload = {"process": claimed.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("interaction fact claim has no domain identity")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:interaction-fact:{event_type.lower()}:{_digest([process.trigger_id, attempt_id])}",
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        # A ClockAdvanced event changes logical time on the WORLD lane without
        # necessarily changing the DELIBERATION revision used by a trigger
        # claim.  Revision-class CAS alone can therefore admit a claim built
        # from a stale logical time and let the reducer fail with an invariant
        # error.  Bind claims to the complete cursor so that any intervening
        # event becomes a normal retryable concurrency conflict.
        await self._commit_at_cursor(
            (event,),
            cursor=self._cursor(projection),
            commit_id=f"commit:interaction-fact:claim:{_digest([process.trigger_id, attempt_id])}",
        )
        return claimed

    async def _complete(
        self,
        *,
        process: TriggerProcess,
        source_event: WorldEvent,
        cursor: ProjectionCursor,
        outcome_ref: str,
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("interaction fact completion requires a claimed process")
        projection = await self._project_at(cursor)
        at = max(
            projection.logical_time or source_event.logical_time, process.claim_lease.acquired_at
        )
        if at > process.claim_lease.expires_at:
            raise ValueError("interaction fact lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:interaction-fact:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id]),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessCompleted",
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key="world-v2:interaction-fact:completion:"
            + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]),
            payload=payload,
        )
        await self._commit_at_cursor(
            (event,),
            cursor=cursor,
            commit_id="commit:interaction-fact:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref]),
        )

    async def _project(self):
        return await asyncio.to_thread(self._ledger.project)

    async def _project_at(self, cursor: ProjectionCursor):
        return await asyncio.to_thread(self._ledger.project_at, cursor)

    async def _observation_events_at(self, locators, *, cursor: ProjectionCursor):
        return await asyncio.to_thread(self._ledger.observation_events_at, locators, cursor=cursor)

    async def _lookup_event_commit(self, event_id: str):
        return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)

    async def _current_single_fact_sources(
        self,
        projection,
        *,
        subject_ref: str,
    ) -> tuple[dict[str, object], ...]:
        """Expose exact old source prose so the model, not slot code, judges updates."""

        def resolve() -> tuple[dict[str, object], ...]:
            output: list[dict[str, object]] = []
            for fact in projection.facts:
                if (
                    len(output) >= 16
                    or fact.values.status != "active"
                    or fact.values.cardinality != "single"
                    or fact.values.subject_ref != subject_ref
                ):
                    continue
                binding = fact.values.assertion_binding
                ref = next(
                    (
                        item
                        for item in projection.message_observations
                        if item.observation_id == binding.source_ref
                    ),
                    None,
                )
                if ref is None:
                    continue
                committed = next(
                    (
                        item
                        for item in projection.committed_world_event_refs
                        if item.event_type == "ObservationRecorded"
                        and item.world_revision == ref.world_revision
                        and item.payload_hash == ref.event_payload_hash
                    ),
                    None,
                )
                if committed is None:
                    continue
                located = self._ledger.lookup_event_commit(committed.event_id)
                if located is None:
                    continue
                try:
                    old_observation = Observation.model_validate_json(located[0].payload_json)
                except ValueError:
                    continue
                if old_observation.observation_id != binding.source_ref or not old_observation.text:
                    continue
                output.append(
                    {
                        "fact_id": fact.fact_id,
                        "predicate_code": fact.values.predicate_code,
                        "source_text": old_observation.text,
                        "fact_entity_revision": fact.entity_revision,
                        "fact_accepted_event_ref": fact.origin.accepted_event_ref,
                        "fact_accepted_world_revision": (
                            next(
                                item.world_revision
                                for item in projection.committed_world_event_refs
                                if item.event_id == fact.origin.accepted_event_ref
                            )
                        ),
                        "fact_committed_at": fact.committed_at.isoformat(),
                        "fact_updated_at": fact.updated_at.isoformat(),
                        "source_observation_id": old_observation.observation_id,
                        "source_observation_logical_time": (
                            old_observation.logical_time.isoformat()
                        ),
                        "source_observation_received_at": (
                            old_observation.received_at.isoformat()
                        ),
                    }
                )
            return tuple(output)

        return await asyncio.to_thread(resolve)

    async def _commit(
        self, events, *, world_revision: int, deliberation_revision: int, commit_id: str
    ):
        return await asyncio.to_thread(
            self._ledger.commit,
            events,
            expected_world_revision=world_revision,
            expected_deliberation_revision=deliberation_revision,
            commit_id=commit_id,
        )

    async def _commit_at_cursor(self, events, *, cursor: ProjectionCursor, commit_id: str):
        return await asyncio.to_thread(
            self._ledger.commit_at_cursor, events, expected_cursor=cursor, commit_id=commit_id
        )

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )

    @staticmethod
    def _cursor_from_commit(commit) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=commit.world_revision,
            deliberation_revision=commit.deliberation_revision,
            ledger_sequence=commit.ledger_sequence,
        )


__all__ = ["FactTriggerRunResult", "InteractionFactTriggerRuntime"]
