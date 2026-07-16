"""Durably fan out one verified life/clock wake into the media ecology.

This is deliberately the first, narrow Life Ecology vertical.  It does not
propose, accept, or materialise a life fact.  Its job is to make the boundary
between a committed wake and the already source-bound media ecology reliable:
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
from typing import Literal, Protocol

from .life_ecology_contract import (
    LIFE_ECOLOGY_WAKE_EVENT_TYPES,
    LifeEcologyClaimState as LifeEcologyClaimState,  # noqa: F401
    LifeEcologyRunClaim,
    LifeEcologyRunKey,
)
from .schema_core import FrozenModel


LifeEcologyAvailabilityState = Literal[
    "installed_and_active",
    "installed_but_scheduler_disabled",
    "authority_only",
    "adapter_only",
    "paused_by_budget",
    "blocked_by_missing_capability",
]
LifeEcologyRunStatus = Literal[
    "advanced", "idle", "joined_existing", "deferred", "unavailable", "rejected", "failed_safe",
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
        self, *, key: LifeEcologyRunKey, trigger_id: str, outcome: str
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


class LifeEcologyRuntime:
    """Advance a single durable wake without becoming a second life writer."""

    def __init__(
        self,
        *,
        ledger,
        trigger_store: LifeEcologyTriggerStore,
        media_followup: MediaEcologyFollowup,
        activity_followup: ActivityLifecycleFollowup | None = None,
        availability: LifeEcologyAvailability,
        actor: str = "worker:life-ecology",
    ) -> None:
        if not actor:
            raise ValueError("life ecology runtime requires an actor")
        self._ledger = ledger
        self._trigger_store = trigger_store
        self._media_followup = media_followup
        self._activity_followup = activity_followup
        self._availability = availability
        self._actor = actor

    def availability(self) -> LifeEcologyAvailability:
        return self._availability

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> LifeEcologyRunResult:
        """Claim exactly one verified wake and execute its media follow-up once.

        Later verticals insert catalog, deliberation and acceptance *between*
        the ownership claim and follow-up.  Until those lanes exist the valid
        life result is deliberately ``idle``; a media candidate may still be
        frozen from prior committed authority.
        """

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

        activity_status: str | None = None
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
            except Exception:
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.activity_followup_failed",
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
        except Exception:
            await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
            return LifeEcologyRunResult(
                status="failed_safe",
                trigger_id=claim.trigger_id,
                reason_code="life_ecology.media_followup_failed",
                activity_followup_status=activity_status,
            )

        try:
            await self._trigger_store.complete(
                key=key, trigger_id=claim.trigger_id, outcome="idle"
            )
        except Exception:
            return LifeEcologyRunResult(
                status="failed_safe",
                trigger_id=claim.trigger_id,
                reason_code="life_ecology.trigger_completion_failed",
                media_followup_status=media_status,
                activity_followup_status=activity_status,
            )
        return LifeEcologyRunResult(
            status="advanced" if activity_status == "transitioned" else "idle",
            trigger_id=claim.trigger_id,
            media_followup_status=media_status,
            activity_followup_status=activity_status,
        )

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
            or getattr(commit, "world_revision", -1) != committed.world_revision
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

    async def _complete_failed_safe(self, *, key: LifeEcologyRunKey, trigger_id: str) -> None:
        try:
            await self._trigger_store.complete(
                key=key, trigger_id=trigger_id, outcome="failed_safe"
            )
        except Exception:
            # The result remains fail-safe.  A durable store that could not
            # record this state must surface recovery rather than manufacture
            # a life fact in this runtime.
            return


__all__ = [
    "ActivityLifecycleFollowup",
    "LifeEcologyAvailability",
    "LifeEcologyAvailabilityState",
    "LifeEcologyRunClaim",
    "LifeEcologyRunKey",
    "LifeEcologyRunResult",
    "LifeEcologyRuntime",
    "LifeEcologyTriggerStore",
    "MediaEcologyFollowup",
]
