"""Durably fan out one verified life/clock wake into the media ecology.

This is the narrow Life Ecology scheduler seam.  It does not itself invent or
authorize a life fact.  Its job is to make the boundary between a committed
wake and the installed source-bound ecology reliable:
the wake is verified at a pinned projection, an injected durable trigger store
atomically owns or joins the run, and only the owner asks the media ecology to
scan the resulting committed world.

The trigger store is an explicit port despite the installed ``life_ecology``
``TriggerProcess`` kind.  Composition may not replace it with an in-memory
lock: implementations must persist the key
``(world_id, wake_event_ref, catalog_version)`` and make claim/join atomic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import inspect
import logging
import re
from typing import Literal, Protocol

from .life_ecology_contract import (
    LIFE_ECOLOGY_WAKE_EVENT_TYPES,
    LifeEcologyClaimState as LifeEcologyClaimState,  # noqa: F401
    LifeEcologyRunClaim,
    LifeEcologyRunKey,
)
from .fact_memory_draft import FactMemoryDraftTechnicalFailure
from .life_aftermath_runtime import LifeAftermathModelFailure
from .errors import ConcurrencyConflict
from .event_identity import domain_idempotency_key
from .life_events import WorldOccurrenceActivatedPayload
from .schema_core import FrozenModel
from .schemas import EvidenceRef, ProjectionCursor, WorldEvent

_LOG = logging.getLogger(__name__)


LifeEcologyAvailabilityState = Literal[
    "installed_and_active",
    "installed_but_scheduler_disabled",
    "authority_only",
    "adapter_only",
    "paused_by_budget",
    "blocked_by_missing_capability",
]
LifeEcologyRunStatus = Literal[
    "advanced",
    "idle",
    "joined_existing",
    "deferred",
    "unavailable",
    "rejected",
    "failed_safe",
]


# A wake remains intentionally narrow.  It is copied from the committed-life
# vocabulary consumed by EventEcologyMediaCandidateRuntime, not from inbound
# message or media paths.
class LifeEcologyAvailability(FrozenModel):
    """Installed state, distinct from a quiet/no-opening world outcome."""

    state: LifeEcologyAvailabilityState
    catalog_version: str = "life-ecology.1"


class LifeEcologyRunResult(FrozenModel):
    status: LifeEcologyRunStatus
    trigger_id: str | None = None
    reason_code: str | None = None
    media_followup_status: str | None = None
    activity_followup_status: str | None = None
    aftermath_followup_status: str | None = None
    npc_initiative_followup_status: str | None = None
    open_world_followup_status: str | None = None
    visual_evidence_followup_status: str | None = None
    biographical_followup_status: str | None = None
    life_development_followup_status: str | None = None
    technical_failure_code: str | None = None


class LifeEcologyTriggerStore(Protocol):
    """Durable, atomic claim/join storage owned by the composition root.

    ``claim_or_join`` must return ``owned`` to exactly one caller for a key.
    A retry/restart must return ``joined`` while the owner is live or
    ``completed`` after it records a terminal result.  This port grants no
    authority to write life facts.
    """

    async def claim_or_join(
        self,
        *,
        key: LifeEcologyRunKey,
        trace_id: str,
        correlation_id: str,
    ) -> LifeEcologyRunClaim: ...

    async def complete(
        self,
        *,
        key: LifeEcologyRunKey,
        trigger_id: str,
        outcome: str,
        character_interior_model_result: object | None = None,
    ) -> None: ...


class MediaEcologyFollowup(Protocol):
    """The existing source-bound candidate runtime's narrow public operation."""

    def drain_once(
        self,
        *,
        wake_event_ref: str,
        logical_time: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
    ) -> object: ...


class ActivityLifecycleFollowup(Protocol):
    """Optional model/acceptance lane installed by composition, never implicit."""

    async def advance_once(
        self,
        *,
        wake_event_ref: str,
        trigger_id: str,
        logical_time: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
    ) -> object: ...


class LifeAftermathFollowup(Protocol):
    def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object: ...


class BiographicalLifecycleFollowup(Protocol):
    def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object: ...


class LifeDevelopmentFollowup(Protocol):
    """One open, model-authored life-development decision for an owned wake."""

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object: ...


class OpenWorldEventFollowup(Protocol):
    """Bounded model-authored event lane installed by composition."""

    def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object: ...


class NpcInitiativeFollowup(Protocol):
    """Reviewed NPC-initiated event lane, installed only by composition."""

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object: ...


class VisualEvidenceFollowup(Protocol):
    """Reviewed visual-declaration lane bridging settled life to media candidates."""

    def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object: ...


class LifeEcologyRuntime:
    """Advance a single durable wake without becoming a second life writer."""

    def __init__(
        self,
        *,
        ledger,
        trigger_store: LifeEcologyTriggerStore,
        media_followup: MediaEcologyFollowup,
        activity_followup: ActivityLifecycleFollowup | None = None,
        aftermath_followup: LifeAftermathFollowup | None = None,
        biographical_followup: BiographicalLifecycleFollowup | None = None,
        life_development_followup: LifeDevelopmentFollowup | None = None,
        npc_initiative_followup: NpcInitiativeFollowup | None = None,
        open_world_followup: OpenWorldEventFollowup | None = None,
        visual_evidence_followup: VisualEvidenceFollowup | None = None,
        availability: LifeEcologyAvailability,
        actor: str = "worker:life-ecology",
    ) -> None:
        if not actor:
            raise ValueError("life ecology runtime requires an actor")
        self._ledger = ledger
        self._trigger_store = trigger_store
        self._media_followup = media_followup
        self._activity_followup = activity_followup
        self._aftermath_followup = aftermath_followup
        self._biographical_followup = biographical_followup
        self._life_development_followup = life_development_followup
        self._npc_initiative_followup = npc_initiative_followup
        self._open_world_followup = open_world_followup
        self._visual_evidence_followup = visual_evidence_followup
        self._availability = availability
        self._actor = actor

    def availability(self) -> LifeEcologyAvailability:
        return self._availability

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> LifeEcologyRunResult:
        """Claim one verified wake and execute each installed follow-up once."""

        availability = self._availability
        if availability.state != "installed_and_active":
            return LifeEcologyRunResult(
                status="unavailable",
                reason_code=f"life_ecology.{availability.state}",
            )

        validated = self._validated_wake(wake_event_ref=wake_event_ref)
        if validated is None:
            return LifeEcologyRunResult(
                status="rejected", reason_code="life_ecology.wake_not_exactly_committed"
            )
        logical_time = validated
        projection = self._ledger.project()
        schedule = getattr(projection, "life_ecology_schedule", None)
        development_due = schedule is None or logical_time >= schedule.next_consideration_at
        key = LifeEcologyRunKey(
            world_id=self._ledger.world_id,
            wake_event_ref=wake_event_ref,
            catalog_version=availability.catalog_version,
        )
        try:
            claim = await self._trigger_store.claim_or_join(
                key=key, trace_id=trace_id, correlation_id=correlation_id
            )
        except Exception:
            return LifeEcologyRunResult(
                status="failed_safe", reason_code="life_ecology.trigger_store_unavailable"
            )
        if claim.state == "completed":
            return LifeEcologyRunResult(
                status="joined_existing",
                trigger_id=claim.trigger_id,
                reason_code="life_ecology.run_completed",
            )
        if claim.state == "joined":
            return LifeEcologyRunResult(
                status="joined_existing",
                trigger_id=claim.trigger_id,
                reason_code="life_ecology.run_in_progress",
            )

        try:
            await self._activate_due_occurrences(
                wake_event_ref=wake_event_ref,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        except Exception:
            _LOG.exception(
                "life ecology due-occurrence activation failed wake=%s",
                wake_event_ref,
            )
            await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
            return LifeEcologyRunResult(
                status="failed_safe",
                trigger_id=claim.trigger_id,
                reason_code="life_ecology.occurrence_activation_failed",
            )

        biographical_status: str | None = None
        if self._biographical_followup is not None:
            try:
                biographical_result = self._biographical_followup.advance_once(
                    wake_event_ref=wake_event_ref,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                if inspect.isawaitable(biographical_result):
                    biographical_result = await biographical_result
                biographical_status = getattr(biographical_result, "status", None)
                if not isinstance(biographical_status, str) or not biographical_status:
                    raise ValueError("biographical lifecycle result has no stable status")
            except Exception:
                _LOG.exception(
                    "life ecology biographical followup failed wake=%s",
                    wake_event_ref,
                )
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.biographical_followup_failed",
                )

        activity_status: str | None = None
        activity_quiet_model_result = None
        if self._activity_followup is not None:
            try:
                activity_result = await self._advance_activity_once(
                    wake_event_ref=wake_event_ref,
                    trigger_id=claim.trigger_id,
                    logical_time=logical_time,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                activity_status = getattr(activity_result, "status", None)
                if not isinstance(activity_status, str) or not activity_status:
                    raise ValueError("activity lifecycle result has no stable status")
                if activity_status == "no_op":
                    activity_quiet_model_result = getattr(
                        activity_result,
                        "character_interior_model_result",
                        None,
                    )
                if activity_status == "technical_failure":
                    supplied = getattr(activity_result, "reason_code", None)
                    normalized = (
                        re.sub(r"[^a-z0-9._-]+", "_", supplied.lower()).strip("._-")
                        if isinstance(supplied, str)
                        else ""
                    )
                    failure_code = normalized[:96] or "activity_lifecycle.unknown"
                    persisted = await self._complete_technical_failure(
                        key=key,
                        trigger_id=claim.trigger_id,
                        failure_code=failure_code,
                    )
                    return LifeEcologyRunResult(
                        status="deferred" if persisted else "failed_safe",
                        trigger_id=claim.trigger_id,
                        reason_code=(
                            "life_ecology.activity_lifecycle_technical_failure"
                            if persisted
                            else "life_ecology.technical_failure_persistence_failed"
                        ),
                        activity_followup_status=activity_status,
                        technical_failure_code=failure_code if persisted else None,
                    )
            except Exception:
                _LOG.exception(
                    "life ecology activity followup failed wake=%s",
                    wake_event_ref,
                )
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.activity_followup_failed",
                )

        aftermath_status: str | None = None
        memory_postprocess_failure_code: str | None = None
        if self._aftermath_followup is not None:
            try:
                aftermath_result = self._aftermath_followup.advance_once(
                    wake_event_ref=wake_event_ref,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                if inspect.isawaitable(aftermath_result):
                    aftermath_result = await aftermath_result
                aftermath_status = getattr(aftermath_result, "status", None)
                if not isinstance(aftermath_status, str) or not aftermath_status:
                    raise ValueError("life aftermath result has no stable status")
                if aftermath_status == "retry_wait":
                    await self._complete_outcome(
                        key=key,
                        trigger_id=claim.trigger_id,
                        outcome="cooldown",
                    )
                    return LifeEcologyRunResult(
                        status="deferred",
                        trigger_id=claim.trigger_id,
                        reason_code="life_ecology.aftermath_retry_wait",
                        activity_followup_status=activity_status,
                        aftermath_followup_status=aftermath_status,
                    )
                if aftermath_status == "settled" and self._biographical_followup is not None:
                    followup = self._biographical_followup.advance_once(
                        wake_event_ref=wake_event_ref,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
                    if inspect.isawaitable(followup):
                        followup = await followup
                    followup_status = getattr(followup, "status", None)
                    if not isinstance(followup_status, str) or not followup_status:
                        raise ValueError("post-settlement biographical result has no stable status")
                    if followup_status == "transitioned":
                        biographical_status = followup_status
            except FactMemoryDraftTechnicalFailure as exc:
                # Experience-memory classification is post-processing of an
                # already durable lived event.  A provider/parse failure must
                # remain visible and retryable on a later wake, but it must not
                # discard this ecology opportunity or prevent unrelated life
                # and media lanes from advancing.
                normalized = re.sub(
                    r"[^a-z0-9._-]+",
                    "_",
                    exc.failure_code.lower(),
                ).strip("._-")
                memory_postprocess_failure_code = "memory." + (normalized[:80] or "unknown")
                aftermath_status = "memory_technical_failure"
                _LOG.warning(
                    "life ecology memory postprocess deferred wake=%s failure=%s",
                    wake_event_ref,
                    memory_postprocess_failure_code,
                )
            except LifeAftermathModelFailure as exc:
                normalized = re.sub(
                    r"[^a-z0-9._-]+",
                    "_",
                    exc.failure_code.lower(),
                ).strip("._-")
                failure_code = "aftermath." + (normalized[:80] or "unknown")
                persisted = await self._complete_technical_failure(
                    key=key,
                    trigger_id=claim.trigger_id,
                    failure_code=failure_code,
                )
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code=(
                        "life_ecology.aftermath_technical_failure"
                        if persisted
                        else "life_ecology.technical_failure_persistence_failed"
                    ),
                    activity_followup_status=activity_status,
                    aftermath_followup_status="technical_failure",
                    technical_failure_code=(failure_code if persisted else None),
                )
            except Exception:
                _LOG.exception(
                    "life ecology aftermath followup failed wake=%s",
                    wake_event_ref,
                )
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.aftermath_followup_failed",
                    activity_followup_status=activity_status,
                )

        life_development_status: str | None = None
        life_development_failure_code: str | None = None
        if (
            self._life_development_followup is not None
            and development_due
            and activity_status != "transitioned"
            and aftermath_status
            not in {"occurrence_opened", "settled", "recovered_experience", "recovered_memory"}
        ):
            development_result = await self._life_development_followup.advance_once(
                wake_event_ref=wake_event_ref,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            life_development_status = getattr(development_result, "status", None)
            if not isinstance(life_development_status, str) or not life_development_status:
                raise ValueError("life development result has no stable status")
            if life_development_status == "technical_failure":
                supplied = getattr(development_result, "reason_code", None)
                normalized = (
                    re.sub(r"[^a-z0-9._-]+", "_", supplied.lower()).strip("._-")
                    if isinstance(supplied, str)
                    else ""
                )
                life_development_failure_code = normalized[:96] or "life_development.unknown"
            if life_development_status == "deferred":
                return LifeEcologyRunResult(
                    status="deferred",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.life_development_deferred",
                    activity_followup_status=activity_status,
                    aftermath_followup_status=aftermath_status,
                    biographical_followup_status=biographical_status,
                    life_development_followup_status=life_development_status,
                )

        npc_initiative_status: str | None = None
        # NPC Ecology is a quiet-wake lane: it runs when no main life family
        # claimed this wake.  Unlike the retired reviewed-candidate lane it is
        # also valid beside open Life Development; both share the ordinary
        # occurrence/aftermath event machine, so only one may materialize.
        npc_has_due_work = False
        if self._npc_initiative_followup is not None:
            due_reader = getattr(self._npc_initiative_followup, "has_due_work", None)
            if callable(due_reader):
                npc_has_due_work = bool(due_reader(projection=projection))
        if (
            self._npc_initiative_followup is not None
            and (development_due or npc_has_due_work)
            and activity_status != "transitioned"
            and life_development_status
            not in {
                "occurrence_committed",
                "plan_committed",
                "plan_completed",
                "technical_failure",
            }
            and aftermath_status
            not in {"occurrence_opened", "settled", "recovered_experience", "recovered_memory"}
        ):
            try:
                npc_result = await self._npc_initiative_followup.advance_once(
                    wake_event_ref=wake_event_ref,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                npc_initiative_status = getattr(npc_result, "status", None)
                if not isinstance(npc_initiative_status, str) or not npc_initiative_status:
                    raise ValueError("npc initiative result has no stable status")
                if npc_initiative_status == "technical_failure":
                    supplied = getattr(npc_result, "reason_code", None)
                    normalized = (
                        re.sub(r"[^a-z0-9._-]+", "_", supplied.lower()).strip("._-")
                        if isinstance(supplied, str)
                        else ""
                    )
                    failure_code = "npc_ecology." + (normalized[:80] or "unknown")
                    persisted = await self._complete_technical_failure(
                        key=key,
                        trigger_id=claim.trigger_id,
                        failure_code=failure_code,
                    )
                    return LifeEcologyRunResult(
                        status="failed_safe",
                        trigger_id=claim.trigger_id,
                        reason_code=(
                            "life_ecology.npc_ecology_technical_failure"
                            if persisted
                            else "life_ecology.technical_failure_persistence_failed"
                        ),
                        activity_followup_status=activity_status,
                        aftermath_followup_status=aftermath_status,
                        biographical_followup_status=biographical_status,
                        life_development_followup_status=life_development_status,
                        npc_initiative_followup_status=npc_initiative_status,
                        technical_failure_code=(failure_code if persisted else None),
                    )
            except Exception:
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.npc_initiative_followup_failed",
                    activity_followup_status=activity_status,
                    aftermath_followup_status=aftermath_status,
                )

        open_world_status: str | None = None
        # A reviewed aftermath occurrence, a freshly written future plan, or a
        # committed NPC-initiated occurrence is the main life family for this
        # wake.  Do not open a second occurrence for the same active plan;
        # the model-authored lane gets a later wake once that family is done.
        if (
            self._life_development_followup is None
            and self._open_world_followup is not None
            and npc_initiative_status
            not in {
                "committed",
                "plan_committed",
                "plan_completed",
                "occurrence_committed",
                "recovered",
            }
            and aftermath_status
            not in {"occurrence_opened", "settled", "recovered_experience", "recovered_memory"}
        ):
            try:
                open_world_result = await self._advance_open_world_once(
                    wake_event_ref=wake_event_ref,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                open_world_status = getattr(open_world_result, "status", None)
                if not isinstance(open_world_status, str) or not open_world_status:
                    raise ValueError("open world result has no stable status")
            except Exception:
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.open_world_followup_failed",
                    activity_followup_status=activity_status,
                    aftermath_followup_status=aftermath_status,
                    npc_initiative_followup_status=npc_initiative_status,
                )
            if open_world_status == "deferred":
                # Keep the durable claim live so a later scheduler wake can
                # reclaim it after the lease expires.  Completing a model
                # outage as ``idle`` would permanently erase this wake.
                return LifeEcologyRunResult(
                    status="deferred",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.open_world_deferred",
                    activity_followup_status=activity_status,
                    aftermath_followup_status=aftermath_status,
                    npc_initiative_followup_status=npc_initiative_status,
                    open_world_followup_status=open_world_status,
                )

        visual_evidence_status: str | None = None
        # The visual-declaration lane runs on every owned wake, immediately
        # before the media ecology scan: a same-wake aftermath settlement is
        # therefore declarable in the same pass.  Like the aspiration lane it
        # is texture — it never claims the wake as a main life family.
        if self._visual_evidence_followup is not None:
            try:
                visual_result = await self._advance_visual_evidence_once(
                    wake_event_ref=wake_event_ref,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                visual_evidence_status = getattr(visual_result, "status", None)
                if not isinstance(visual_evidence_status, str) or not visual_evidence_status:
                    raise ValueError("visual evidence result has no stable status")
            except Exception:
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.visual_evidence_followup_failed",
                    activity_followup_status=activity_status,
                    aftermath_followup_status=aftermath_status,
                    npc_initiative_followup_status=npc_initiative_status,
                    open_world_followup_status=open_world_status,
                )

        try:
            media_result = await self._drain_media_once(
                wake_event_ref=wake_event_ref,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            media_status = getattr(media_result, "status", None)
            if not isinstance(media_status, str) or not media_status:
                raise ValueError("media ecology result has no stable status")
        except Exception as exc:
            failure_code = _technical_failure_code("media", exc)
            persisted = await self._complete_technical_failure(
                key=key,
                trigger_id=claim.trigger_id,
                failure_code=failure_code,
            )
            if not persisted:
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.technical_failure_persistence_failed",
                    activity_followup_status=activity_status,
                    aftermath_followup_status=aftermath_status,
                    npc_initiative_followup_status=npc_initiative_status,
                    open_world_followup_status=open_world_status,
                    visual_evidence_followup_status=visual_evidence_status,
                )
            return LifeEcologyRunResult(
                status="failed_safe",
                trigger_id=claim.trigger_id,
                reason_code=f"life_ecology.media_followup_failed.{failure_code}",
                activity_followup_status=activity_status,
                aftermath_followup_status=aftermath_status,
                npc_initiative_followup_status=npc_initiative_status,
                open_world_followup_status=open_world_status,
                visual_evidence_followup_status=visual_evidence_status,
                technical_failure_code=failure_code,
            )

        try:
            completion_kwargs = {
                "key": key,
                "trigger_id": claim.trigger_id,
                "outcome": (
                    f"aftermath_{aftermath_status}"
                    if aftermath_status
                    in {
                        "occurrence_opened",
                        "settled",
                        "recovered_experience",
                        "recovered_memory",
                    }
                    else "npc_initiative_committed"
                    if npc_initiative_status
                    in {
                        "committed",
                        "plan_committed",
                        "plan_completed",
                        "occurrence_committed",
                        "recovered",
                    }
                    else "activity_transitioned"
                    if activity_status == "transitioned"
                    else "biographical_transitioned"
                    if biographical_status == "transitioned"
                    else f"technical_failure.{life_development_failure_code}"
                    if life_development_failure_code is not None
                    else f"life_development_{life_development_status}"
                    if life_development_status is not None
                    else "cooldown"
                    if self._life_development_followup is not None and not development_due
                    else "open_world_committed"
                    if open_world_status in {"committed", "recovered"}
                    else "idle"
                ),
            }
            if activity_quiet_model_result is not None:
                completion_kwargs["character_interior_model_result"] = (
                    activity_quiet_model_result
                )
            await self._trigger_store.complete(**completion_kwargs)
        except Exception:
            return LifeEcologyRunResult(
                status="failed_safe",
                trigger_id=claim.trigger_id,
                reason_code="life_ecology.trigger_completion_failed",
                media_followup_status=media_status,
                activity_followup_status=activity_status,
                aftermath_followup_status=aftermath_status,
                npc_initiative_followup_status=npc_initiative_status,
                open_world_followup_status=open_world_status,
                visual_evidence_followup_status=visual_evidence_status,
            )
        return LifeEcologyRunResult(
            status=(
                "deferred"
                if life_development_failure_code is not None
                else "advanced"
                if activity_status == "transitioned"
                or biographical_status == "transitioned"
                or life_development_status
                in {
                    "plan_committed",
                    "plan_completed",
                    "occurrence_committed",
                    "recovered",
                }
                or aftermath_status
                in {
                    "occurrence_opened",
                    "settled",
                    "recovered_experience",
                    "recovered_memory",
                }
                or npc_initiative_status
                in {
                    "committed",
                    "plan_committed",
                    "plan_completed",
                    "occurrence_committed",
                    "recovered",
                }
                or open_world_status in {"committed", "recovered"}
                else "idle"
            ),
            trigger_id=claim.trigger_id,
            media_followup_status=media_status,
            activity_followup_status=activity_status,
            aftermath_followup_status=aftermath_status,
            npc_initiative_followup_status=npc_initiative_status,
            open_world_followup_status=open_world_status,
            visual_evidence_followup_status=visual_evidence_status,
            biographical_followup_status=biographical_status,
            life_development_followup_status=life_development_status,
            reason_code=(
                "life_ecology.life_development_technical_failure"
                if life_development_failure_code is not None
                else None
            ),
            technical_failure_code=(
                life_development_failure_code or memory_postprocess_failure_code
            ),
        )

    async def _activate_due_occurrences(
        self,
        *,
        wake_event_ref: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> int:
        """Activate planned occurrences whose window has opened.

        Life Development may commit a later-mode occurrence (a future plan)
        that has no other activator.  Without this step such plans would stay
        ``committed`` forever and never reach aftermath settlement.  Activation
        is effect-once per occurrence: the event id derives from the
        occurrence identity, and a CAS conflict joins the concurrent winner.
        """

        projection = self._ledger.project()
        committed_refs = {
            item.event_id: item for item in projection.committed_world_event_refs
        }
        wake = committed_refs.get(wake_event_ref)
        if wake is None:
            return 0
        evidence = EvidenceRef(
            ref_id=wake.event_id,
            evidence_type="committed_world_event",
            claim_purpose="life_transition",
            source_world_revision=wake.world_revision,
            immutable_hash=wake.payload_hash,
        )
        activated = 0
        for occurrence in getattr(projection, "world_occurrences", ()):
            if occurrence.status != "committed" or occurrence.time_window is None:
                continue
            if occurrence.time_window.opens_at > logical_time:
                continue
            suffix = occurrence.occurrence_id.removeprefix("occurrence:")
            activation_id = "event:life-ecology:activate:" + suffix
            if self._ledger.lookup_event_commit(activation_id) is not None:
                continue
            payload = WorldOccurrenceActivatedPayload(
                change_id="change:life-ecology:activate:" + suffix,
                transition_id="transition:life-ecology:activate:" + suffix,
                expected_entity_revision=occurrence.entity_revision,
                evidence_refs=(evidence,),
                policy_refs=("policy:life-ecology.1",),
                occurrence_id=occurrence.occurrence_id,
                activated_at=logical_time,
                satisfied_precondition_refs=(),
            )
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=activation_id,
                world_id=self._ledger.world_id,
                event_type="WorldOccurrenceActivated",
                logical_time=logical_time,
                created_at=logical_time,
                actor=self._actor,
                source="world-v2:life-ecology",
                trace_id=trace_id,
                causation_id=wake_event_ref,
                correlation_id=correlation_id,
                idempotency_key=(
                    domain_idempotency_key(
                        event_type="WorldOccurrenceActivated",
                        world_id=self._ledger.world_id,
                        payload=payload.model_dump(mode="json"),
                    )
                    or "life-ecology-activated:" + suffix
                ),
                payload=payload.model_dump(mode="json"),
            )
            try:
                self._ledger.commit_at_cursor(
                    (event,),
                    expected_cursor=ProjectionCursor(
                        world_revision=projection.world_revision,
                        deliberation_revision=projection.deliberation_revision,
                        ledger_sequence=projection.ledger_sequence,
                    ),
                    commit_id="commit:" + activation_id,
                )
            except ConcurrencyConflict:
                if self._ledger.lookup_event_commit(activation_id) is not None:
                    continue
                raise
            activated += 1
            projection = self._ledger.project()
        return activated

    def _validated_wake(self, *, wake_event_ref: str) -> datetime | None:
        """Prove the exact immutable wake at the current projection head."""

        projection = self._ledger.project()
        logical_time = getattr(projection, "logical_time", None)
        if not isinstance(logical_time, datetime):
            return None
        committed = next(
            (
                item
                for item in getattr(projection, "committed_world_event_refs", ())
                if item.event_id == wake_event_ref
            ),
            None,
        )
        if (
            committed is None
            or committed.event_type not in LIFE_ECOLOGY_WAKE_EVENT_TYPES
            # A worker may recover a durable wake after a later ClockAdvanced.
            # The wake's immutable bytes remain its proof; candidate frequency
            # and expiry are evaluated at the current authoritative time.
            or committed.logical_time > logical_time
        ):
            return None
        located = self._ledger.lookup_event_commit(wake_event_ref)
        if located is None:
            return None
        event, commit = located
        if (
            event.world_id != self._ledger.world_id
            or event.event_id != committed.event_id
            or event.event_type != committed.event_type
            or event.payload_hash != committed.payload_hash
            or event.logical_time != committed.logical_time
            # CommitResult exposes the revision after the *whole atomic
            # batch*.  ClockAdvanced may share that batch with affect decay,
            # so its event-level committed ref can legitimately be lower.
            # Bind the exact event to the returned batch and require only
            # monotonic containment instead of revision equality.
            or event.event_id not in getattr(commit, "event_ids", ())
            or getattr(commit, "world_revision", -1) < committed.world_revision
            or committed.world_revision > getattr(projection, "world_revision", -1)
        ):
            return None
        return logical_time

    async def _drain_media_once(
        self,
        *,
        wake_event_ref: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> object:
        kwargs = {
            "wake_event_ref": wake_event_ref,
            "logical_time": logical_time,
            "actor": self._actor,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
        }
        if getattr(self._ledger, "blocks_event_loop", False):
            return await asyncio.to_thread(self._media_followup.drain_once, **kwargs)
        return self._media_followup.drain_once(**kwargs)

    async def _advance_activity_once(
        self,
        *,
        wake_event_ref: str,
        trigger_id: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> object:
        assert self._activity_followup is not None
        return await self._activity_followup.advance_once(
            wake_event_ref=wake_event_ref,
            trigger_id=trigger_id,
            logical_time=logical_time,
            actor=self._actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    async def _advance_visual_evidence_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object:
        assert self._visual_evidence_followup is not None
        kwargs = {
            "wake_event_ref": wake_event_ref,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
        }
        method = self._visual_evidence_followup.advance_once
        if inspect.iscoroutinefunction(method):
            result = method(**kwargs)
        elif getattr(self._ledger, "blocks_event_loop", False):
            result = await asyncio.to_thread(method, **kwargs)
        else:
            result = method(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _advance_open_world_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object:
        assert self._open_world_followup is not None
        kwargs = {
            "wake_event_ref": wake_event_ref,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
        }
        method = self._open_world_followup.advance_once
        if inspect.iscoroutinefunction(method):
            result = method(**kwargs)
        elif getattr(self._ledger, "blocks_event_loop", False):
            result = await asyncio.to_thread(method, **kwargs)
        else:
            result = method(**kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _complete_failed_safe(self, *, key: LifeEcologyRunKey, trigger_id: str) -> bool:
        return await self._complete_outcome(key=key, trigger_id=trigger_id, outcome="failed_safe")

    async def _complete_technical_failure(
        self,
        *,
        key: LifeEcologyRunKey,
        trigger_id: str,
        failure_code: str,
    ) -> bool:
        return await self._complete_outcome(
            key=key,
            trigger_id=trigger_id,
            outcome=f"technical_failure.{failure_code}",
        )

    async def _complete_outcome(
        self,
        *,
        key: LifeEcologyRunKey,
        trigger_id: str,
        outcome: str,
    ) -> bool:
        try:
            await self._trigger_store.complete(key=key, trigger_id=trigger_id, outcome=outcome)
        except Exception:
            # The result remains fail-safe.  A durable store that could not
            # record this state must surface recovery rather than manufacture
            # a life fact in this runtime.
            return False
        return True


def _technical_failure_code(phase: str, exc: Exception) -> str:
    supplied = getattr(exc, "code", None)
    if isinstance(supplied, str):
        normalized = re.sub(r"[^a-z0-9._-]+", "_", supplied.lower()).strip("._-")
        if normalized:
            return f"{phase}.{normalized}"[:96]
    type_name = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__).lower()
    return f"{phase}.{type_name}"[:96]


__all__ = [
    "ActivityLifecycleFollowup",
    "LifeAftermathFollowup",
    "LifeDevelopmentFollowup",
    "NpcInitiativeFollowup",
    "OpenWorldEventFollowup",
    "LifeEcologyAvailability",
    "LifeEcologyAvailabilityState",
    "LifeEcologyRunClaim",
    "LifeEcologyRunKey",
    "LifeEcologyRunResult",
    "LifeEcologyRuntime",
    "LifeEcologyTriggerStore",
    "MediaEcologyFollowup",
    "VisualEvidenceFollowup",
]
