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
    life_author_followup_status: str | None = None
    future_life_author_followup_status: str | None = None
    aftermath_followup_status: str | None = None
    npc_initiative_followup_status: str | None = None
    aspiration_followup_status: str | None = None
    shared_private_followup_status: str | None = None
    open_world_followup_status: str | None = None
    visual_evidence_followup_status: str | None = None


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


class LifeAuthorFollowup(Protocol):
    """Reviewed-seed authoring lane, called only after this wake is owned."""

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object: ...


class LifeAftermathFollowup(Protocol):
    def advance_once(
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


class AspirationFollowup(Protocol):
    """Reviewed aspiration (low-stakes wish) lane, installed only by composition."""

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> object: ...


class SharedPrivateInvitationFollowup(Protocol):
    """Consent-shaped shared_private invitation lane, installed only by composition."""

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
        life_author_followup: LifeAuthorFollowup | None = None,
        future_life_author_followup: LifeAuthorFollowup | None = None,
        activity_followup: ActivityLifecycleFollowup | None = None,
        aftermath_followup: LifeAftermathFollowup | None = None,
        npc_initiative_followup: NpcInitiativeFollowup | None = None,
        aspiration_followup: AspirationFollowup | None = None,
        shared_private_followup: SharedPrivateInvitationFollowup | None = None,
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
        self._life_author_followup = life_author_followup
        self._future_life_author_followup = future_life_author_followup
        self._activity_followup = activity_followup
        self._aftermath_followup = aftermath_followup
        self._npc_initiative_followup = npc_initiative_followup
        self._aspiration_followup = aspiration_followup
        self._shared_private_followup = shared_private_followup
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

        author_status: str | None = None
        if self._life_author_followup is not None:
            # LifeAuthor translates only explicit provider/model failures into
            # a structured result.  Contract violations and programming bugs
            # must cross this boundary so monitoring sees them and the durable
            # trigger remains recoverable instead of being falsely completed.
            author_result = await self._life_author_followup.advance_once(
                wake_event_ref=wake_event_ref,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            author_status = getattr(author_result, "status", None)
            if not isinstance(author_status, str) or not author_status:
                raise ValueError("life author result has no stable status")

        activity_status: str | None = None
        # One ecology wake accepts at most one main life family.  A newly
        # planned activity may only start on a later ClockAdvanced.
        if self._activity_followup is not None and author_status != "planned":
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
                    life_author_followup_status=author_status,
                )

        aftermath_status: str | None = None
        if self._aftermath_followup is not None and author_status != "planned":
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
            except Exception:
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe", trigger_id=claim.trigger_id,
                    reason_code="life_ecology.aftermath_followup_failed",
                    activity_followup_status=activity_status,
                    life_author_followup_status=author_status,
                )

        future_author_status: str | None = None
        # The future calendar lane runs only on wakes whose main life family
        # is quiet: nothing was just planned for now, no lifecycle transition
        # happened, and no occurrence work was done.  It shares the life
        # author's error discipline: only explicit provider failures become a
        # structured "blocked"; bugs must cross this boundary.
        if (
            self._future_life_author_followup is not None
            and author_status != "planned"
            and activity_status != "transitioned"
            and aftermath_status not in {"occurrence_opened", "settled", "recovered_experience"}
        ):
            future_result = await self._future_life_author_followup.advance_once(
                wake_event_ref=wake_event_ref,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            future_author_status = getattr(future_result, "status", None)
            if not isinstance(future_author_status, str) or not future_author_status:
                raise ValueError("future life author result has no stable status")

        npc_initiative_status: str | None = None
        # NPC initiative is a quiet-wake lane: it runs only when no main life
        # family claimed this wake, exactly like the future calendar lane.  A
        # committed NPC occurrence then becomes this wake's main family.
        if (
            self._npc_initiative_followup is not None
            and author_status != "planned"
            and activity_status != "transitioned"
            and future_author_status != "planned"
            and aftermath_status not in {"occurrence_opened", "settled", "recovered_experience"}
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
            except Exception:
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.npc_initiative_followup_failed",
                    activity_followup_status=activity_status,
                    life_author_followup_status=author_status,
                    future_life_author_followup_status=future_author_status,
                    aftermath_followup_status=aftermath_status,
                )

        aspiration_status: str | None = None
        # The aspiration lane shares the quiet-wake discipline but never
        # becomes a main life family: planting or fading a wish is an inner
        # event, not an occurrence, so it neither blocks nor claims the wake.
        if (
            self._aspiration_followup is not None
            and author_status != "planned"
            and activity_status != "transitioned"
            and future_author_status != "planned"
            and aftermath_status not in {"occurrence_opened", "settled", "recovered_experience"}
        ):
            try:
                aspiration_result = await self._aspiration_followup.advance_once(
                    wake_event_ref=wake_event_ref,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                aspiration_status = getattr(aspiration_result, "status", None)
                if not isinstance(aspiration_status, str) or not aspiration_status:
                    raise ValueError("aspiration result has no stable status")
            except Exception:
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.aspiration_followup_failed",
                    activity_followup_status=activity_status,
                    life_author_followup_status=author_status,
                    future_life_author_followup_status=future_author_status,
                    aftermath_followup_status=aftermath_status,
                    npc_initiative_followup_status=npc_initiative_status,
                )

        shared_private_status: str | None = None
        # The shared_private invitation lane shares the quiet-wake discipline
        # and, like the aspiration lane, never claims the wake as a main life
        # family: planning-to-ask is an inner event until the user answers.
        if (
            self._shared_private_followup is not None
            and author_status != "planned"
            and activity_status != "transitioned"
            and future_author_status != "planned"
            and aftermath_status not in {"occurrence_opened", "settled", "recovered_experience"}
        ):
            try:
                shared_private_result = await self._shared_private_followup.advance_once(
                    wake_event_ref=wake_event_ref,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                shared_private_status = getattr(shared_private_result, "status", None)
                if not isinstance(shared_private_status, str) or not shared_private_status:
                    raise ValueError("shared private invitation result has no stable status")
            except Exception:
                await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
                return LifeEcologyRunResult(
                    status="failed_safe",
                    trigger_id=claim.trigger_id,
                    reason_code="life_ecology.shared_private_followup_failed",
                    activity_followup_status=activity_status,
                    life_author_followup_status=author_status,
                    future_life_author_followup_status=future_author_status,
                    aftermath_followup_status=aftermath_status,
                    npc_initiative_followup_status=npc_initiative_status,
                    aspiration_followup_status=aspiration_status,
                )

        open_world_status: str | None = None
        # A reviewed aftermath occurrence, a freshly written future plan, or a
        # committed NPC-initiated occurrence is the main life family for this
        # wake.  Do not open a second occurrence for the same active plan;
        # the model-authored lane gets a later wake once that family is done.
        if (
            self._open_world_followup is not None
            and future_author_status != "planned"
            and npc_initiative_status not in {"committed", "recovered"}
            and aftermath_status not in {"occurrence_opened", "settled", "recovered_experience"}
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
                    life_author_followup_status=author_status,
                    future_life_author_followup_status=future_author_status,
                    aftermath_followup_status=aftermath_status,
                    npc_initiative_followup_status=npc_initiative_status,
                    aspiration_followup_status=aspiration_status,
                    shared_private_followup_status=shared_private_status,
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
                    life_author_followup_status=author_status,
                    future_life_author_followup_status=future_author_status,
                    aftermath_followup_status=aftermath_status,
                    npc_initiative_followup_status=npc_initiative_status,
                    aspiration_followup_status=aspiration_status,
                    shared_private_followup_status=shared_private_status,
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
                    life_author_followup_status=author_status,
                    future_life_author_followup_status=future_author_status,
                    aftermath_followup_status=aftermath_status,
                    npc_initiative_followup_status=npc_initiative_status,
                    aspiration_followup_status=aspiration_status,
                    shared_private_followup_status=shared_private_status,
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
        except Exception:
            await self._complete_failed_safe(key=key, trigger_id=claim.trigger_id)
            return LifeEcologyRunResult(
                status="failed_safe",
                trigger_id=claim.trigger_id,
                reason_code="life_ecology.media_followup_failed",
                activity_followup_status=activity_status,
                life_author_followup_status=author_status,
                future_life_author_followup_status=future_author_status,
                aftermath_followup_status=aftermath_status,
                npc_initiative_followup_status=npc_initiative_status,
                aspiration_followup_status=aspiration_status,
                shared_private_followup_status=shared_private_status,
                open_world_followup_status=open_world_status,
                visual_evidence_followup_status=visual_evidence_status,
            )

        try:
            await self._trigger_store.complete(
                key=key,
                trigger_id=claim.trigger_id,
                outcome=(
                    f"aftermath_{aftermath_status}"
                    if aftermath_status in {
                        "occurrence_opened", "settled", "recovered_experience"
                    }
                    else "author_planned"
                    if author_status == "planned"
                    else "future_author_planned"
                    if future_author_status == "planned"
                    else "npc_initiative_committed"
                    if npc_initiative_status in {"committed", "recovered"}
                    else (
                        f"author_{author_status}"
                        if author_status is not None else "idle"
                    )
                ),
            )
        except Exception:
            return LifeEcologyRunResult(
                status="failed_safe",
                trigger_id=claim.trigger_id,
                reason_code="life_ecology.trigger_completion_failed",
                media_followup_status=media_status,
                activity_followup_status=activity_status,
                life_author_followup_status=author_status,
                future_life_author_followup_status=future_author_status,
                aftermath_followup_status=aftermath_status,
                npc_initiative_followup_status=npc_initiative_status,
                aspiration_followup_status=aspiration_status,
                shared_private_followup_status=shared_private_status,
                open_world_followup_status=open_world_status,
                visual_evidence_followup_status=visual_evidence_status,
            )
        return LifeEcologyRunResult(
            status=(
                "advanced"
                if author_status == "planned" or future_author_status == "planned"
                or activity_status == "transitioned"
                or aftermath_status in {"occurrence_opened", "settled", "recovered_experience"}
                or npc_initiative_status in {"committed", "recovered"}
                or open_world_status in {"committed", "recovered"}
                else "idle"
            ),
            trigger_id=claim.trigger_id,
            media_followup_status=media_status,
            activity_followup_status=activity_status,
            life_author_followup_status=author_status,
            future_life_author_followup_status=future_author_status,
            aftermath_followup_status=aftermath_status,
            npc_initiative_followup_status=npc_initiative_status,
            aspiration_followup_status=aspiration_status,
            shared_private_followup_status=shared_private_status,
            open_world_followup_status=open_world_status,
            visual_evidence_followup_status=visual_evidence_status,
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
    "AspirationFollowup",
    "LifeAuthorFollowup",
    "LifeAftermathFollowup",
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
    "SharedPrivateInvitationFollowup",
    "VisualEvidenceFollowup",
]
