"""Durably consume role-owned requests to consider an available media candidate.

The accepted expression is the only source of the request.  This runtime does
not infer intent from text.  It opens a restart-safe ``TriggerProcess`` after a
reply Action is provider accepted and delegates the bounded candidate choice
to the existing CharacterInterior media conductor.  When the accepted role
proposal also carries exact attended life evidence, an optional supplier may
compile one source-closed candidate before selection; it never guesses a scene
or treats the counterpart's request as visual proof.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import asyncio
import hashlib
import json
from typing import Literal, Protocol

from .errors import ConcurrencyConflict
from .event_identity import domain_idempotency_key
from .media_preview_conductor import MediaPreviewConductor, MediaPreviewConductorResult
from .minimal_reply_events import ExpressionPlanAcceptedPayload
from .proposal_envelope import DecisionProposal, validate_proposal_envelope
from .schema_core import FrozenModel
from .schemas import ClaimLease, ProjectionCursor, TriggerProcess, WorldEvent


_OWNER_ID = "worker:media-request"
_LEASE_SECONDS = 30
_TERMINAL_MEDIA_EVENTS = frozenset(
    {
        "MediaSelectionAttemptRecorded",
        "MediaPlanRecorded",
        "MediaNotRenderableRecorded",
        "PhotoCandidateUnrenderable",
    }
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def media_request_trigger_id(
    *, world_id: str, source_event_ref: str, source_event_payload_hash: str
) -> str:
    return "trigger:media-request:" + _digest(
        {
            "contract": "media-request-trigger.1",
            "world_id": world_id,
            "source_event_ref": source_event_ref,
            "source_event_payload_hash": source_event_payload_hash,
        }
    )


class _Ledger(Protocol):
    world_id: str
    blocks_event_loop: bool

    def project(self): ...  # type: ignore[no-untyped-def]
    def lookup_event_commit(self, event_id: str): ...  # type: ignore[no-untyped-def]
    def commit_at_cursor(
        self,
        events: tuple[WorldEvent, ...],
        *,
        expected_cursor: ProjectionCursor,
        commit_id: str,
    ): ...  # type: ignore[no-untyped-def]
    def recent_events_by_type(
        self, *, event_types: frozenset[str], since: datetime, limit: int
    ) -> tuple[WorldEvent, ...]: ...


class _CandidateSupplier(Protocol):
    def request_once(
        self,
        *,
        source_refs: tuple[str, ...],
        trace_id: str,
        correlation_id: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _AcceptedMediaRequest:
    event: WorldEvent
    source_refs: tuple[str, ...]


class MediaRequestAdvanceResult(FrozenModel):
    handled: bool
    status: Literal["idle", "owned", "completed", "blocked"]
    preview: MediaPreviewConductorResult | None = None
    reason_code: str | None = None


class MediaRequestRuntime:
    """Claim and settle each accepted media wake at most once across restart."""

    def __init__(
        self,
        *,
        ledger: _Ledger,
        conductor: MediaPreviewConductor,
        candidate_supplier: _CandidateSupplier | None = None,
        owner_id: str = _OWNER_ID,
    ) -> None:
        if not owner_id:
            raise ValueError("media request runtime requires an owner")
        self._ledger = ledger
        self._conductor = conductor
        self._candidate_supplier = candidate_supplier
        self._owner_id = owner_id
        self._lock = asyncio.Lock()
        self._owned_attempt_ids: set[str] = set()

    async def request_for_actions(self, action_ids: tuple[str, ...]) -> bool:
        """Resolve only the accepted request bound to these visible Actions."""

        if not action_ids:
            return False
        projection = await self._project()
        requested = set(action_ids)
        actions = tuple(item for item in projection.actions if item.action_id in requested)
        if {item.action_id for item in actions} != requested:
            return False
        plan_ids = {
            item.expression_plan_id
            for item in actions
            if item.expression_plan_id is not None
        }
        if not plan_ids:
            return False
        plans = tuple(item for item in projection.expression_plans if item.plan_id in plan_ids)
        if {item.plan_id for item in plans} != plan_ids:
            return False
        for plan in plans:
            if await self._accepted_request(plan, projection=projection) is not None:
                return True
        return False

    async def advance_once(
        self, *, logical_time: datetime, trace_id: str, correlation_id: str
    ) -> MediaRequestAdvanceResult:
        async with self._lock:
            projection = await self._project()
            process = self._next_process(projection)
            if process is None:
                source = await self._next_request_source(projection)
                if source is None:
                    return MediaRequestAdvanceResult(handled=False, status="idle")
                process = await self._open(source=source, projection=projection)
                if process is None:
                    return MediaRequestAdvanceResult(
                        handled=True,
                        status="blocked",
                        reason_code="media_request.open_cursor_stale",
                    )
                projection = await self._project()

            claimed = await self._claim_or_resume(
                process=process,
                projection=projection,
                logical_time=logical_time,
            )
            if claimed is None:
                return MediaRequestAdvanceResult(
                    handled=True,
                    status="owned",
                    reason_code="media_request.claim_owned_elsewhere",
                )

            request_correlation = "correlation:media-request:" + claimed.trigger_id
            recovered = await self._recovered_terminal(
                process=claimed,
                correlation_id=request_correlation,
            )
            if recovered is not None:
                completed = await self._complete(
                    process=claimed,
                    logical_time=logical_time,
                    outcome_ref=recovered,
                    trace_id=trace_id,
                    correlation_id=request_correlation,
                )
                return MediaRequestAdvanceResult(
                    handled=True,
                    status="completed" if completed else "blocked",
                    reason_code=(recovered if completed else "media_request.complete_cursor_stale"),
                )

            accepted_request = await self._accepted_request_for_process(
                process=claimed,
                projection=await self._project(),
            )
            if accepted_request is None:
                return MediaRequestAdvanceResult(
                    handled=True,
                    status="blocked",
                    reason_code="media_request.accepted_request_unavailable",
                )
            if self._candidate_supplier is not None and accepted_request.source_refs:
                supply = self._candidate_supplier.request_once
                if self._ledger.blocks_event_loop:
                    await asyncio.to_thread(
                        supply,
                        source_refs=accepted_request.source_refs,
                        trace_id=trace_id,
                        correlation_id=request_correlation,
                    )
                else:
                    supply(
                        source_refs=accepted_request.source_refs,
                        trace_id=trace_id,
                        correlation_id=request_correlation,
                    )

            preview = await self._conductor.advance_once(
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=request_correlation,
            )
            outcome = self._terminal_outcome(preview)
            if outcome is None:
                return MediaRequestAdvanceResult(
                    handled=True,
                    status=("owned" if preview.status == "in_progress" else "blocked"),
                    preview=preview,
                    reason_code=preview.reason_code,
                )
            if outcome != "media-request:no_candidate":
                recovered = await self._recovered_terminal(
                    process=claimed,
                    correlation_id=request_correlation,
                )
                if recovered != outcome:
                    return MediaRequestAdvanceResult(
                        handled=True,
                        status="blocked",
                        preview=preview,
                        reason_code="media_request.terminal_not_bound_to_request",
                    )
            completed = await self._complete(
                process=claimed,
                logical_time=logical_time,
                outcome_ref=outcome,
                trace_id=trace_id,
                correlation_id=request_correlation,
            )
            return MediaRequestAdvanceResult(
                handled=True,
                status="completed" if completed else "blocked",
                preview=preview,
                reason_code=(outcome if completed else "media_request.complete_cursor_stale"),
            )

    async def _project(self):  # type: ignore[no-untyped-def]
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    def _next_process(self, projection) -> TriggerProcess | None:  # type: ignore[no-untyped-def]
        active = tuple(
            item
            for item in projection.trigger_processes
            if item.process_kind == "media_request" and item.state != "terminal"
        )
        return sorted(active, key=lambda item: item.trigger_id)[0] if active else None

    async def _next_request_source(self, projection) -> WorldEvent | None:  # type: ignore[no-untyped-def]
        existing = {item.trigger_id for item in projection.trigger_processes}
        completed = set(projection.completed_trigger_ids)
        visible_plan_ids = {
            action.expression_plan_id
            for action in projection.actions
            if action.expression_plan_id is not None
            and action.kind != "typing"
            and action.state in {"provider_accepted", "delivered"}
        }
        for plan in sorted(projection.expression_plans, key=lambda item: item.plan_id):
            if plan.plan_id not in visible_plan_ids:
                continue
            request = await self._accepted_request(plan, projection=projection)
            if request is None:
                continue
            trigger_id = media_request_trigger_id(
                world_id=self._ledger.world_id, source_event_ref=request.event.event_id,
                source_event_payload_hash=request.event.payload_hash,
            )
            if trigger_id not in existing and trigger_id not in completed:
                return request.event
        return None

    async def _accepted_request(
        self, plan, *, projection
    ) -> _AcceptedMediaRequest | None:  # type: ignore[no-untyped-def]
        located = (
            await asyncio.to_thread(self._ledger.lookup_event_commit, plan.event_ref)
            if self._ledger.blocks_event_loop
            else self._ledger.lookup_event_commit(plan.event_ref)
        )
        if located is None:
            return None
        event, _commit = located
        if event.event_type != "ExpressionPlanAccepted" or event.payload_hash != plan.event_payload_hash:
            return None
        try:
            payload = ExpressionPlanAcceptedPayload.model_validate_json(event.payload_json)
        except ValueError:
            return None
        if (
            payload.plan_id != plan.plan_id
            or payload.media_request != "consider_available_candidate"
        ):
            return None
        audits = tuple(
            item
            for item in getattr(projection, "proposal_audits", ())
            if item.proposal_id == plan.proposal_id
        )
        if not audits:
            # Historical accepted plans predate the source-carrying request
            # bridge. They may still consume an already-open candidate, but
            # cannot authorize compilation of a new one.
            return _AcceptedMediaRequest(event=event, source_refs=())
        if len(audits) != 1:
            return None
        try:
            proposal = validate_proposal_envelope(json.loads(audits[0].proposal_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(proposal, DecisionProposal):
            return None
        changes = tuple(
            item
            for item in proposal.proposed_changes
            if item.kind == "expression_plan_transition" and item.target_id == plan.plan_id
        )
        if len(changes) != 1:
            return None
        payload_value = changes[0].payload.value()
        media_refs = payload_value.get("media_source_refs", [])
        if (
            payload_value.get("media_request") != "consider_available_candidate"
            or not isinstance(media_refs, list)
            or any(not isinstance(item, str) or not item for item in media_refs)
            or len(media_refs) != len(set(media_refs))
        ):
            return None
        evidence_refs = {item.ref_id for item in proposal.evidence_refs}
        if not set(media_refs).issubset(evidence_refs):
            return None
        return _AcceptedMediaRequest(
            event=event,
            source_refs=tuple(media_refs),
        )

    async def _accepted_request_for_process(
        self, *, process: TriggerProcess, projection
    ) -> _AcceptedMediaRequest | None:  # type: ignore[no-untyped-def]
        if process.source_evidence_ref is None:
            return None
        plan = next(
            (
                item
                for item in projection.expression_plans
                if item.event_ref == process.source_evidence_ref
            ),
            None,
        )
        if plan is None:
            return None
        return await self._accepted_request(plan, projection=projection)

    async def _open(self, *, source: WorldEvent, projection) -> TriggerProcess | None:  # type: ignore[no-untyped-def]
        trigger_id = media_request_trigger_id(
            world_id=self._ledger.world_id,
            source_event_ref=source.event_id,
            source_event_payload_hash=source.payload_hash,
        )
        process = TriggerProcess(
            trigger_id=trigger_id,
            trigger_ref="media-request:" + source.payload_hash,
            process_kind="media_request",
            source_evidence_ref=source.event_id,
            state="open",
        )
        payload = {"process": process.model_dump(mode="json")}
        event = self._event(
            event_type="TriggerProcessOpened",
            stable_id=trigger_id,
            logical_time=projection.logical_time,
            trace_id=source.trace_id,
            causation_id=source.event_id,
            correlation_id=source.correlation_id,
            payload=payload,
        )
        try:
            await self._commit((event,), projection=projection, label="open:" + trigger_id)
        except ConcurrencyConflict:
            return None
        return process

    async def _claim_or_resume(
        self, *, process: TriggerProcess, projection, logical_time: datetime
    ) -> TriggerProcess | None:  # type: ignore[no-untyped-def]
        if process.state == "claimed":
            assert process.claim_lease is not None
            if logical_time < process.claim_lease.expires_at:
                return (
                    process
                    if process.claim_lease.attempt_id in self._owned_attempt_ids
                    else None
                )
            self._owned_attempt_ids.discard(process.claim_lease.attempt_id)
            event_type = "TriggerProcessReclaimed"
        else:
            event_type = "TriggerProcessClaimed"
        attempt_id = "attempt:media-request:" + _digest(
            [process.trigger_id, len(process.attempt_ids) + 1]
        )
        lease = ClaimLease(
            owner_id=self._owner_id,
            attempt_id=attempt_id,
            acquired_at=logical_time,
            expires_at=logical_time + timedelta(seconds=_LEASE_SECONDS),
        )
        claimed = process.model_copy(
            update={
                "state": "claimed",
                "claim_lease": lease,
                "attempt_ids": (*process.attempt_ids, attempt_id),
                "runtime_outcome_ref": None,
            }
        )
        payload = {"process": claimed.model_dump(mode="json")}
        event = self._event(
            event_type=event_type,
            stable_id=attempt_id,
            logical_time=logical_time,
            trace_id="trace:" + process.trigger_id,
            causation_id=process.source_evidence_ref or process.trigger_id,
            correlation_id="correlation:media-request:" + process.trigger_id,
            payload=payload,
        )
        try:
            await self._commit((event,), projection=projection, label="claim:" + attempt_id)
        except ConcurrencyConflict:
            return None
        self._owned_attempt_ids.add(attempt_id)
        return claimed

    async def _recovered_terminal(
        self, *, process: TriggerProcess, correlation_id: str
    ) -> str | None:
        source = (
            await asyncio.to_thread(
                self._ledger.lookup_event_commit,
                process.source_evidence_ref or "",
            )
            if self._ledger.blocks_event_loop
            else self._ledger.lookup_event_commit(process.source_evidence_ref or "")
        )
        if source is None:
            return None
        since = source[0].logical_time
        events = (
            await asyncio.to_thread(
                self._ledger.recent_events_by_type,
                event_types=_TERMINAL_MEDIA_EVENTS,
                since=since,
                limit=65_536,
            )
            if self._ledger.blocks_event_loop
            else self._ledger.recent_events_by_type(
                event_types=_TERMINAL_MEDIA_EVENTS,
                since=since,
                limit=65_536,
            )
        )
        matching = tuple(item for item in events if item.correlation_id == correlation_id)
        if not matching:
            return None
        if any(item.event_type == "MediaPlanRecorded" for item in matching):
            return "media-request:planned"
        if any(
            item.event_type in {"MediaNotRenderableRecorded", "PhotoCandidateUnrenderable"}
            for item in matching
        ):
            return "media-request:not_renderable"
        attempts = tuple(
            item for item in matching if item.event_type == "MediaSelectionAttemptRecorded"
        )
        if attempts:
            payload = attempts[-1].payload()
            if payload.get("outcome") == "declined":
                return "media-request:role_no_op"
        return None

    @staticmethod
    def _terminal_outcome(preview: MediaPreviewConductorResult) -> str | None:
        if preview.status == "planned":
            return "media-request:planned"
        if preview.status == "not_renderable":
            return "media-request:not_renderable"
        if preview.status == "idle":
            reason = preview.reason_code or "idle"
            return "media-request:" + (
                "role_no_op" if "declin" in reason else "no_candidate"
            )
        return None

    async def _complete(
        self,
        *,
        process: TriggerProcess,
        logical_time: datetime,
        outcome_ref: str,
        trace_id: str,
        correlation_id: str,
    ) -> bool:
        projection = await self._project()
        current = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == process.trigger_id
            ),
            None,
        )
        if current is None or current.state != "claimed" or current.claim_lease is None:
            return current is not None and current.state == "terminal"
        payload = {
            "trigger_id": current.trigger_id,
            "owner_id": current.claim_lease.owner_id,
            "attempt_id": current.claim_lease.attempt_id,
            "completed_at": logical_time.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        event = self._event(
            event_type="TriggerProcessCompleted",
            stable_id=current.claim_lease.attempt_id,
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=current.source_evidence_ref or current.trigger_id,
            correlation_id=correlation_id,
            payload=payload,
        )
        try:
            await self._commit((event,), projection=projection, label="complete:" + current.trigger_id)
        except ConcurrencyConflict:
            return False
        self._owned_attempt_ids.discard(current.claim_lease.attempt_id)
        return True

    async def _commit(self, events: tuple[WorldEvent, ...], *, projection, label: str):  # type: ignore[no-untyped-def]
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit_at_cursor,
                events,
                expected_cursor=cursor,
                commit_id="commit:media-request:" + _digest(label),
            )
        return self._ledger.commit_at_cursor(
            events,
            expected_cursor=cursor,
            commit_id="commit:media-request:" + _digest(label),
        )

    def _event(
        self,
        *,
        event_type: str,
        stable_id: str,
        logical_time: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
        payload: dict[str, object],
    ) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:media-request:" + event_type.lower() + ":" + _digest(stable_id),
            event_type=event_type,
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=logical_time,
            actor=self._owner_id,
            source="world-v2:media-request",
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type=event_type,
                world_id=self._ledger.world_id,
                payload=payload,
            )
            or "media-request:" + _digest([event_type, stable_id]),
            payload=payload,
        )


__all__ = [
    "MediaRequestAdvanceResult",
    "MediaRequestRuntime",
    "media_request_trigger_id",
]
