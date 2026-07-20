"""Source-bound review of retrieval memories after an accepted Fact withdrawal.

Fact authority and retrieval-memory authority deliberately remain separate.
``FactWithdrawn`` therefore never edits a ``MemoryCandidate`` in its reducer.
This worker detects the durable invalidation, opens one deterministic review
process per affected active candidate, and lets a bounded semantic reviewer
choose retain, forget, or (when other current sources survive) revise.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
from typing import Literal, Protocol

from .errors import ConcurrencyConflict, IdempotencyConflict
from .event_identity import domain_idempotency_key
from .fact_events import FactChangedPayload
from .memory_events import (
    MemoryCandidateChangedPayload,
    MemorySourceIdentityRef,
    MemorySourceInvalidationForgetAuthority,
    memory_candidate_mutation_hash,
    memory_source_evidence,
)
from .memory_reducers import (
    MEMORY_POLICY_DIGEST,
    MEMORY_POLICY_REFS,
    MEMORY_POLICY_VERSION,
)
from .schema_core import FrozenModel
from .schemas import (
    ClaimLease,
    MemoryCandidateOrigin,
    MemoryCandidateProjection,
    MemoryCandidateProposedMutation,
    MemoryCandidateProposalProjection,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
    memory_candidate_semantic_fingerprint,
    memory_source_authority_id,
    memory_source_cluster_fingerprint,
)
from .sqlite_ledger import SQLiteWorldLedger


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class MemoryWithdrawalReviewChatModel(Protocol):
    model: str

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


class MemoryWithdrawalReviewDecision(FrozenModel):
    disposition: Literal["retain", "forget", "revise"]


def materialize_memory_withdrawal_review_draft(raw: str) -> MemoryWithdrawalReviewDecision:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("memory review model did not return one JSON object") from exc
    if not isinstance(value, dict) or set(value) != {"disposition"}:
        raise ValueError("memory review draft contains unsupported fields")
    try:
        return MemoryWithdrawalReviewDecision.model_validate(value, strict=True)
    except Exception as exc:
        raise ValueError("memory review disposition is invalid") from exc


class MemoryWithdrawalReviewAdapter:
    """Expose classification, never authority-bearing ids, hashes, or payloads."""

    VERSION = "memory-withdrawal-review.1"

    def __init__(self, *, model: MemoryWithdrawalReviewChatModel, temperature: float = 0.15) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("memory review temperature must be between 0 and 2")
        self._model = model
        self._temperature = temperature

    async def review(
        self,
        *,
        candidate: MemoryCandidateProjection,
        withdrawal: FactChangedPayload,
        withdrawal_payload_hash: str,
        can_revise: bool,
    ) -> MemoryWithdrawalReviewDecision:
        raw = await self._model.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Review one retrieval-memory cue after one of its verified Fact sources was "
                        "withdrawn. Return exactly one JSON object with disposition retain, forget, "
                        "or revise. retain means preserve the historical cue without active retrieval "
                        "changes; forget means deactivate retrieval while preserving history; revise "
                        "means remove only the invalidated source and is valid only when can_revise is "
                        "true. Decide by future continuity, relationship relevance, privacy, emotional "
                        "residue, and whether surviving sources independently support the cue. Do not "
                        "return prose, ids, hashes, summaries, actions, or replacement facts."
                    ),
                },
                {
                    "role": "user",
                    "content": _canonical(
                        {
                            "candidate": {
                                "cue_kind": candidate.values.cue_kind,
                                "retention_rationales": candidate.values.retention_rationales,
                                "privacy_ceiling": candidate.values.privacy_ceiling,
                                "salience": candidate.values.salience.model_dump(mode="json"),
                                "source_count": len(candidate.values.source_bindings),
                                "can_revise": can_revise,
                            },
                            "withdrawal": {
                                "predicate_code": withdrawal.fact_after.values.predicate_code,
                                "reason_code": withdrawal.fact_after.values.withdrawal_reason_code,
                                "source_hash": withdrawal_payload_hash,
                            },
                        }
                    ),
                },
            ],
            temperature=self._temperature,
        )
        return materialize_memory_withdrawal_review_draft(raw)


class MemoryWithdrawalReviewRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "processed", "joined"]
    work_status: Literal[
        "retain", "forget", "revise", "invalid_revise", "invalid_draft"
    ] | None = None


class MemoryWithdrawalReviewRuntime:
    """Recovery-safe FactWithdrawn -> review -> explicit memory transition."""

    PROCESS_KIND = "memory_candidate_review"

    def __init__(
        self,
        *,
        ledger: SQLiteWorldLedger,
        reviewer: MemoryWithdrawalReviewAdapter,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:memory-withdrawal-review",
    ) -> None:
        if type(ledger) is not SQLiteWorldLedger:
            raise ValueError("memory withdrawal review requires the production SQLite ledger")
        if not owner_id or not source or lease_seconds <= 0:
            raise ValueError("memory withdrawal review requires owner, source, and positive lease")
        self._ledger = ledger
        self._reviewer = reviewer
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    @property
    def ledger(self) -> SQLiteWorldLedger:
        return self._ledger

    async def drain_one(self) -> MemoryWithdrawalReviewRunResult:
        projection = await self._project()
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == self.PROCESS_KIND and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            process = await self._open_next(projection)
            if process is None:
                return MemoryWithdrawalReviewRunResult(trigger_id="", status="idle")
            projection = await self._project()
            process = next(item for item in projection.trigger_processes if item.trigger_id == process.trigger_id)

        source_event, withdrawal = await self._withdrawal(process.source_evidence_ref or "")
        proposal_id = self._proposal_id(process.trigger_id)
        existing = next(
            (item for item in projection.memory_candidate_proposals if item.proposal_id == proposal_id),
            None,
        )
        candidate = self._candidate_for_process(
            process=process, source_event=source_event, withdrawal=withdrawal,
            projection=projection, existing=existing,
        )
        active = await self._claim_or_reclaim(
            process=process, source_event=source_event, projection=projection
        )
        if active is None:
            return MemoryWithdrawalReviewRunResult(
                trigger_id=process.trigger_id, status="owned_elsewhere"
            )

        if existing is not None:
            mutation = MemoryCandidateChangedPayload.model_validate_json(
                existing.proposed_mutation.payload_json
            )
            current = next(
                (item for item in (await self._project()).memory_candidates if item.candidate_id == mutation.candidate_after.candidate_id),
                None,
            )
            if current == mutation.candidate_after:
                await self._complete(
                    process=active, source_event=source_event,
                    outcome_ref=f"outcome:{active.trigger_id}:{mutation.operation}:joined",
                )
                return MemoryWithdrawalReviewRunResult(
                    trigger_id=active.trigger_id, status="joined", work_status=mutation.operation
                )
            if current != mutation.candidate_before:
                raise ValueError("memory review proposal candidate CAS is no longer current")
            await self._accept_mutation_and_complete(
                process=active, source_event=source_event, proposal=existing, mutation=mutation
            )
            return MemoryWithdrawalReviewRunResult(
                trigger_id=active.trigger_id, status="joined", work_status=mutation.operation
            )

        invalidated = self._invalidated_bindings(candidate, withdrawal)
        surviving = tuple(item for item in candidate.values.source_bindings if item not in invalidated)
        try:
            decision = await self._reviewer.review(
                candidate=candidate,
                withdrawal=withdrawal,
                withdrawal_payload_hash=source_event.payload_hash,
                can_revise=bool(surviving),
            )
        except ValueError:
            # A structurally invalid classification has no authority.  Consume
            # the exact review opportunity as an explicit no-change so a
            # deterministic bad response cannot leave a permanent hot loop.
            await self._complete(
                process=active,
                source_event=source_event,
                outcome_ref=(
                    f"outcome:{active.trigger_id}:invalid-draft:"
                    + _digest(
                        {
                            "adapter": self._reviewer.VERSION,
                            "source_hash": source_event.payload_hash,
                            "candidate_revision": candidate.entity_revision,
                        }
                    )
                ),
            )
            return MemoryWithdrawalReviewRunResult(
                trigger_id=active.trigger_id,
                status="processed",
                work_status="invalid_draft",
            )
        if decision.disposition == "retain" or (
            decision.disposition == "revise" and not surviving
        ):
            work_status = "invalid_revise" if decision.disposition == "revise" else "retain"
            await self._complete(
                process=active,
                source_event=source_event,
                outcome_ref=(
                    f"outcome:{active.trigger_id}:{work_status}:"
                    + _digest(
                        {
                            "adapter": self._reviewer.VERSION,
                            "decision": decision.model_dump(mode="json"),
                            "source_hash": source_event.payload_hash,
                            "candidate_revision": candidate.entity_revision,
                        }
                    )
                ),
            )
            return MemoryWithdrawalReviewRunResult(
                trigger_id=active.trigger_id, status="processed", work_status=work_status
            )

        mutation = self._mutation(
            process=active,
            candidate=candidate,
            invalidated=invalidated,
            surviving=surviving,
            disposition=decision.disposition,
            evaluated_world_revision=(await self._project()).world_revision,
            at=max((await self._project()).logical_time or source_event.logical_time, source_event.logical_time),
        )
        proposal = self._proposal(mutation)
        await self._record_proposal(
            proposal=proposal, source_event=source_event, process=active
        )
        await self._accept_mutation_and_complete(
            process=active, source_event=source_event, proposal=proposal, mutation=mutation
        )
        return MemoryWithdrawalReviewRunResult(
            trigger_id=active.trigger_id,
            status="processed",
            work_status=decision.disposition,
        )

    async def _open_next(self, projection) -> TriggerProcess | None:
        terminal = {
            item.trigger_id
            for item in projection.trigger_processes
            if item.process_kind == self.PROCESS_KIND and item.state == "terminal"
        }
        for committed in projection.committed_world_event_refs:
            if committed.event_type != "FactWithdrawn":
                continue
            located = await self._lookup(committed.event_id)
            if located is None:
                raise ValueError("committed Fact withdrawal event is unavailable")
            source_event = located[0]
            withdrawal = FactChangedPayload.model_validate_json(source_event.payload_json)
            for candidate in projection.memory_candidates:
                if candidate.values.status != "active" or not self._invalidated_bindings(candidate, withdrawal):
                    continue
                trigger_id = self._trigger_id(source_event=source_event, candidate=candidate)
                if trigger_id in terminal:
                    continue
                process = TriggerProcess(
                    trigger_id=trigger_id,
                    trigger_ref=self._trigger_ref(source_event=source_event, candidate=candidate),
                    process_kind=self.PROCESS_KIND,
                    source_evidence_ref=source_event.event_id,
                    state="open",
                )
                opened = self._event(
                    event_id="event:memory-review:opened:" + _digest(trigger_id),
                    event_type="TriggerProcessOpened",
                    payload={"process": process.model_dump(mode="json")},
                    logical_time=projection.logical_time or source_event.logical_time,
                    created_at=source_event.created_at,
                    trace_id=source_event.trace_id,
                    causation_id=source_event.event_id,
                    correlation_id=source_event.correlation_id,
                )
                try:
                    await self._commit_at_cursor(
                        (opened,), cursor=self._cursor(projection),
                        commit_id="commit:memory-review:open:" + _digest(trigger_id),
                    )
                except (ConcurrencyConflict, IdempotencyConflict):
                    joined = await self._project()
                    return next(
                        (
                            item for item in joined.trigger_processes
                            if item.trigger_id == trigger_id and item.state != "terminal"
                        ),
                        None,
                    )
                return process
        return None

    def _candidate_for_process(
        self, *, process, source_event, withdrawal, projection, existing
    ) -> MemoryCandidateProjection:
        if existing is not None:
            mutation = MemoryCandidateChangedPayload.model_validate_json(
                existing.proposed_mutation.payload_json
            )
            candidate = mutation.candidate_before
            if candidate is None:
                raise ValueError("memory review proposal lacks before image")
        else:
            candidate = next(
                (
                    item for item in projection.memory_candidates
                    if item.values.status == "active"
                    and self._invalidated_bindings(item, withdrawal)
                    and self._trigger_id(source_event=source_event, candidate=item) == process.trigger_id
                ),
                None,
            )
            if candidate is None:
                raise ValueError("memory review trigger does not bind an affected candidate revision")
        if (
            self._trigger_id(source_event=source_event, candidate=candidate) != process.trigger_id
            or self._trigger_ref(source_event=source_event, candidate=candidate) != process.trigger_ref
        ):
            raise ValueError("memory review trigger source hash or candidate revision is forged")
        return candidate

    @staticmethod
    def _invalidated_bindings(candidate, withdrawal) -> tuple:
        before = withdrawal.fact_before
        if withdrawal.operation != "withdraw" or before is None:
            raise ValueError("memory review source is not one Fact withdrawal")
        return tuple(
            item
            for item in candidate.values.source_bindings
            if item.source_kind == "fact"
            and item.source_id == before.fact_id
            and item.source_entity_revision < withdrawal.fact_after.entity_revision
        )

    async def _claim_or_reclaim(self, *, process, source_event, projection):
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            if process.claim_lease.owner_id == self._owner_id and at <= process.claim_lease.expires_at:
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = "attempt:memory-review:" + _digest(
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
        event_type = "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        event = self._event(
            event_id=f"event:memory-review:{event_type.lower()}:" + _digest([process.trigger_id, attempt_id]),
            event_type=event_type,
            payload={"process": claimed.model_dump(mode="json")},
            logical_time=at,
            created_at=source_event.created_at,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
        )
        try:
            await self._commit_at_cursor(
                (event,), cursor=self._cursor(projection),
                commit_id=f"commit:memory-review:{event_type.lower()}:" + _digest([process.trigger_id, attempt_id]),
            )
        except (ConcurrencyConflict, IdempotencyConflict):
            joined = await self._project()
            current = next(item for item in joined.trigger_processes if item.trigger_id == process.trigger_id)
            if (
                current.state == "claimed"
                and current.claim_lease is not None
                and current.claim_lease.owner_id == self._owner_id
            ):
                return current
            return None
        return claimed

    def _mutation(
        self, *, process, candidate, invalidated, surviving, disposition, evaluated_world_revision, at
    ) -> MemoryCandidateChangedPayload:
        revision = candidate.entity_revision + 1
        transition_id = "transition:memory-review:" + _digest(process.trigger_id)
        event_id = "event:memory-review:mutation:" + _digest(process.trigger_id)
        origin = MemoryCandidateOrigin(
            change_id="change:memory-review:" + _digest(process.trigger_id),
            transition_id=transition_id,
            policy_refs=MEMORY_POLICY_REFS,
            accepted_event_ref=event_id,
        )
        if disposition == "forget":
            values = candidate.values.model_copy(
                update={
                    "status": "forgotten",
                    "retrieval_strength_bp": 0,
                    "reviewed_at": at,
                    "forgotten_at": at,
                }
            )
            cluster = candidate.source_cluster_fingerprint
            lineage = candidate.source_cluster_lineage
            forget_authority = MemorySourceInvalidationForgetAuthority(
                sources=tuple(
                    MemorySourceIdentityRef(
                        source_kind=item.source_kind,
                        source_id=item.source_id,
                        source_entity_revision=item.source_entity_revision,
                        source_authority_id=memory_source_authority_id(item),
                    )
                    for item in invalidated
                )
            )
            revise_kind = None
        else:
            values = candidate.values.model_copy(
                update={"source_bindings": surviving, "reviewed_at": at}
            )
            cluster = memory_source_cluster_fingerprint(values=values, policy_refs=MEMORY_POLICY_REFS)
            lineage = candidate.source_cluster_lineage
            if cluster != candidate.source_cluster_fingerprint:
                lineage = (*lineage, cluster)
            forget_authority = None
            revise_kind = "correct"
        after = MemoryCandidateProjection(
            candidate_id=candidate.candidate_id,
            entity_revision=revision,
            semantic_fingerprint=memory_candidate_semantic_fingerprint(
                values=values, policy_refs=MEMORY_POLICY_REFS
            ),
            source_cluster_fingerprint=cluster,
            source_cluster_lineage=lineage,
            values=values,
            origin=origin,
            opened_at=candidate.opened_at,
            updated_at=at,
        )
        raw = {
            "change_id": origin.change_id,
            "transition_id": origin.transition_id,
            "expected_entity_revision": candidate.entity_revision,
            "evidence_refs": tuple(memory_source_evidence(item) for item in after.values.source_bindings),
            "policy_refs": MEMORY_POLICY_REFS,
            "acceptance_id": "acceptance:memory-review:" + _digest(process.trigger_id),
            "proposal_id": self._proposal_id(process.trigger_id),
            "evaluated_world_revision": evaluated_world_revision,
            "accepted_change_hash": "0" * 64,
            "operation": disposition,
            "candidate_before": candidate,
            "candidate_after": after,
            "revise_kind": revise_kind,
            "reinforcement_reason": None,
            "rejection_reason": None,
            "forget_authority": forget_authority,
            "strength_before_bp": candidate.values.retrieval_strength_bp if disposition == "forget" else None,
            "strength_after_bp": after.values.retrieval_strength_bp if disposition == "forget" else None,
            "reinforcement_count_before": candidate.values.reinforcement_count if disposition == "forget" else None,
            "reinforcement_count_after": after.values.reinforcement_count if disposition == "forget" else None,
            "policy_version": MEMORY_POLICY_VERSION if disposition == "forget" else None,
            "policy_digest": MEMORY_POLICY_DIGEST if disposition == "forget" else None,
        }
        raw["accepted_change_hash"] = memory_candidate_mutation_hash(raw)
        return MemoryCandidateChangedPayload.model_validate(raw)

    @staticmethod
    def _proposal(mutation: MemoryCandidateChangedPayload) -> MemoryCandidateProposalProjection:
        event_type = "MemoryCandidateForgotten" if mutation.operation == "forget" else "MemoryCandidateRevised"
        return MemoryCandidateProposalProjection(
            proposal_id=mutation.proposal_id,
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:memory-candidate.1",
            transition_kind=mutation.operation,
            change_id=mutation.change_id,
            transition_id=mutation.transition_id,
            evaluated_world_revision=mutation.evaluated_world_revision,
            expected_entity_revision=mutation.expected_entity_revision,
            proposed_change_hash=mutation.accepted_change_hash,
            evidence_refs=mutation.evidence_refs,
            policy_refs=mutation.policy_refs,
            proposed_mutation=MemoryCandidateProposedMutation(
                event_type=event_type,
                payload_json=_canonical(mutation.model_dump(mode="json")),
            ),
        )

    async def _record_proposal(self, *, proposal, source_event, process) -> None:
        projection = await self._project()
        event = self._event(
            event_id="event:memory-review:proposal:" + _digest(process.trigger_id),
            event_type="ProposalRecorded",
            payload=proposal.model_dump(mode="json"),
            logical_time=projection.logical_time or source_event.logical_time,
            created_at=source_event.created_at,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
        )
        await self._commit_at_cursor(
            (event,), cursor=self._cursor(projection),
            commit_id="commit:memory-review:proposal:" + _digest(process.trigger_id),
        )

    async def _accept_mutation_and_complete(self, *, process, source_event, proposal, mutation) -> None:
        projection = await self._project()
        current = next(
            (item for item in projection.memory_candidates if item.candidate_id == mutation.candidate_after.candidate_id),
            None,
        )
        if current == mutation.candidate_after:
            await self._complete(
                process=process, source_event=source_event,
                outcome_ref=f"outcome:{process.trigger_id}:{mutation.operation}:joined",
            )
            return
        if current != mutation.candidate_before:
            raise ValueError("memory review acceptance candidate CAS failed")
        acceptance_payload = {
            "acceptance_id": mutation.acceptance_id,
            "status": "accepted",
            "proposal_id": mutation.proposal_id,
            "evaluated_world_revision": mutation.evaluated_world_revision,
            "accepted_change_id": mutation.change_id,
            "accepted_change_hash": mutation.accepted_change_hash,
        }
        acceptance = self._event(
            event_id="event:memory-review:acceptance:" + _digest(process.trigger_id),
            event_type="AcceptanceRecorded",
            payload=acceptance_payload,
            logical_time=projection.logical_time or source_event.logical_time,
            created_at=source_event.created_at,
            trace_id=source_event.trace_id,
            causation_id="event:memory-review:proposal:" + _digest(process.trigger_id),
            correlation_id=source_event.correlation_id,
        )
        mutation_event = self._event(
            event_id=mutation.candidate_after.origin.accepted_event_ref,
            event_type=proposal.proposed_mutation.event_type,
            payload=mutation.model_dump(mode="json"),
            logical_time=projection.logical_time or source_event.logical_time,
            created_at=source_event.created_at,
            trace_id=source_event.trace_id,
            causation_id=acceptance.event_id,
            correlation_id=source_event.correlation_id,
        )
        completion = self._completion_event(
            process=process,
            source_event=source_event,
            at=projection.logical_time or source_event.logical_time,
            outcome_ref=f"outcome:{process.trigger_id}:{mutation.operation}",
            causation_id=mutation_event.event_id,
        )
        await self._commit_at_cursor(
            (acceptance, mutation_event, completion),
            cursor=self._cursor(projection),
            commit_id="commit:memory-review:accepted:" + _digest(process.trigger_id),
        )

    async def _complete(self, *, process, source_event, outcome_ref) -> None:
        projection = await self._project()
        event = self._completion_event(
            process=process,
            source_event=source_event,
            at=projection.logical_time or source_event.logical_time,
            outcome_ref=outcome_ref,
            causation_id=source_event.event_id,
        )
        await self._commit_at_cursor(
            (event,), cursor=self._cursor(projection),
            commit_id="commit:memory-review:complete:" + _digest([process.trigger_id, outcome_ref]),
        )

    def _completion_event(self, *, process, source_event, at, outcome_ref, causation_id):
        if process.claim_lease is None or at > process.claim_lease.expires_at:
            raise ValueError("memory review completion requires a live claim")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": max(at, process.claim_lease.acquired_at).isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        return self._event(
            event_id="event:memory-review:completed:" + _digest([process.trigger_id, process.claim_lease.attempt_id]),
            event_type="TriggerProcessCompleted",
            payload=payload,
            logical_time=max(at, process.claim_lease.acquired_at),
            created_at=source_event.created_at,
            trace_id=source_event.trace_id,
            causation_id=causation_id,
            correlation_id=source_event.correlation_id,
            idempotency_key="world-v2:memory-review:completed:"
            + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]),
        )

    async def _withdrawal(self, event_id: str) -> tuple[WorldEvent, FactChangedPayload]:
        located = await self._lookup(event_id)
        if located is None or located[0].event_type != "FactWithdrawn":
            raise ValueError("memory review source withdrawal is unavailable")
        payload = FactChangedPayload.model_validate_json(located[0].payload_json)
        if payload.operation != "withdraw":
            raise ValueError("memory review source event does not contain a withdrawal")
        return located[0], payload

    @staticmethod
    def _proposal_id(trigger_id: str) -> str:
        return "proposal:memory-review:" + _digest(trigger_id)

    @staticmethod
    def _trigger_id(*, source_event, candidate) -> str:
        return "trigger:memory-review:" + _digest(
            {
                "world_id": source_event.world_id,
                "withdrawal_event_id": source_event.event_id,
                "withdrawal_payload_hash": source_event.payload_hash,
                "candidate_id": candidate.candidate_id,
                "candidate_revision": candidate.entity_revision,
                "candidate_fingerprint": candidate.semantic_fingerprint,
            }
        )

    @classmethod
    def _trigger_ref(cls, *, source_event, candidate) -> str:
        return "memory-review:" + _digest(
            [cls._trigger_id(source_event=source_event, candidate=candidate), source_event.payload_hash]
        )

    def _event(
        self, *, event_id, event_type, payload, logical_time, created_at,
        trace_id, causation_id, correlation_id, idempotency_key=None,
    ) -> WorldEvent:
        identity = idempotency_key or domain_idempotency_key(
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError(f"memory review has no identity for {event_type}")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=logical_time,
            created_at=created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=identity,
            payload=payload,
        )

    @staticmethod
    def _cursor(projection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )

    async def _project(self):
        return await asyncio.to_thread(self._ledger.project)

    async def _lookup(self, event_id):
        return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)

    async def _commit_at_cursor(self, events, *, cursor, commit_id):
        return await asyncio.to_thread(
            self._ledger.commit_at_cursor,
            events,
            expected_cursor=cursor,
            commit_id=commit_id,
        )


__all__ = [
    "MemoryWithdrawalReviewAdapter",
    "MemoryWithdrawalReviewChatModel",
    "MemoryWithdrawalReviewDecision",
    "MemoryWithdrawalReviewRunResult",
    "MemoryWithdrawalReviewRuntime",
    "materialize_memory_withdrawal_review_draft",
]
