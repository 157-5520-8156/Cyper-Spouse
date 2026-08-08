"""Deterministic reflection scheduling for significant life appraisals.

The role model already owns appraisal, affect, and aspiration formation in
``world_stimulus``.  This scheduler is the algorithm side of the reflection
root cause: it decides *which* committed appraisals are significant enough to
be revisited, and opens one bounded reflection trigger per appraisal so the
character can deepen, let go, or form a wish about it later.  It never chooses
the reflection's content — that stays with the character.
"""

from __future__ import annotations


from .errors import ConcurrencyConflict, IdempotencyConflict
from .event_identity import domain_idempotency_key
from .schema_core import FrozenModel
from .schemas import ProjectionCursor, TriggerProcess, WorldEvent


# An appraisal strong enough to be revisited.  The model's own confidence is
# the intensity signal; low-confidence appraisals are small enough to let go.
REFLECTION_CONFIDENCE_THRESHOLD_BP = 6_000
# Hard bound: no more than two reflections per source appraisal.
MAX_REFLECTIONS_PER_APPRAISAL = 2

_PROCESS_KIND = "life_reflection"
_SOURCE_EVENT_TYPE = "AppraisalAccepted"


class ReflectionSchedulerResult(FrozenModel):
    opened: int = 0
    skipped: int = 0
    reason: str | None = None


class ReflectionScheduler:
    """Open bounded reflection triggers for significant accepted appraisals."""

    def __init__(
        self,
        *,
        ledger,
        actor: str,
        threshold_bp: int = REFLECTION_CONFIDENCE_THRESHOLD_BP,
        max_reflections: int = MAX_REFLECTIONS_PER_APPRAISAL,
        source: str = "world-v2:reflection-scheduler",
    ) -> None:
        if not actor:
            raise ValueError("reflection scheduler requires an actor")
        if not 0 <= threshold_bp <= 10_000 or max_reflections < 1:
            raise ValueError("reflection scheduler bounds are invalid")
        self._ledger = ledger
        self._actor = actor
        self._threshold = threshold_bp
        self._max_reflections = max_reflections
        self._source = source

    def open_once(
        self,
        *,
        trace_id: str,
        correlation_id: str,
    ) -> ReflectionSchedulerResult:
        """Open at most one reflection trigger for the strongest eligible appraisal.

        Algorithmic gating only: significance threshold, per-appraisal bound,
        and idempotent trigger identity.  The character owns the reflection.
        """

        projection = self._ledger.project()
        logical_time = projection.logical_time
        if logical_time is None:
            return ReflectionSchedulerResult(skipped=0, reason="no_logical_time")
        existing_reflections = {
            process.source_evidence_ref
            for process in projection.trigger_processes
            if process.process_kind == _PROCESS_KIND
        }
        eligible = sorted(
            (
                item
                for item in projection.appraisals
                if item.status == "active"
                and item.confidence_bp >= self._threshold
                and item.origin.accepted_event_ref not in existing_reflections
            ),
            key=lambda item: item.confidence_bp,
            reverse=True,
        )
        for appraisal in eligible[:self._max_reflections]:
            trigger_id = f"reflection:{appraisal.origin.accepted_event_ref}"
            if trigger_id in existing_reflections:
                continue
            process = TriggerProcess(
                trigger_id=trigger_id,
                trigger_ref=trigger_id,
                process_kind=_PROCESS_KIND,
                source_evidence_ref=appraisal.origin.accepted_event_ref,
                state="open",
            )
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=f"event:life-reflection:opened:{appraisal.origin.accepted_event_ref}",
                world_id=self._ledger.world_id,
                event_type="TriggerProcessOpened",
                logical_time=logical_time,
                created_at=logical_time,
                actor=self._actor,
                source=self._source,
                trace_id=trace_id,
                causation_id=appraisal.origin.accepted_event_ref,
                correlation_id=correlation_id,
                idempotency_key=(
                    domain_idempotency_key(
                        event_type="TriggerProcessOpened",
                        world_id=self._ledger.world_id,
                        payload={"process": process.model_dump(mode="json")},
                    )
                    or "life-reflection-opened:" + appraisal.origin.accepted_event_ref
                ),
                payload={"process": process.model_dump(mode="json")},
            )
            try:
                self._ledger.commit_at_cursor(
                    (event,),
                    expected_cursor=ProjectionCursor(
                        world_revision=projection.world_revision,
                        deliberation_revision=projection.deliberation_revision,
                        ledger_sequence=projection.ledger_sequence,
                    ),
                    commit_id="commit:" + event.event_id,
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                continue
            return ReflectionSchedulerResult(opened=1)
        return ReflectionSchedulerResult(skipped=len(eligible))


__all__ = [
    "ReflectionScheduler",
    "ReflectionSchedulerResult",
]
