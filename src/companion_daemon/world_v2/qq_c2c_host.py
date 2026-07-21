"""QQ/OneBot C2C composition for the World v2 application lane.

This is deliberately not a compatibility layer around ``CompanionEngine`` or
``QQMessageCoalescer``.  A configured, single C2C recipient is mapped to one
World v2 reply target and all ingress, dispatch and restart recovery cross the
``WorldV2PlatformHost`` seam. Provider-local ingress normalization accepts
bounded text, attachment, quote, reaction, sticker and typing metadata while
outbound delivery remains an explicitly text-only transport.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable

from companion_daemon.config import Settings
from companion_daemon.qq_delivery import QQDelivery

from .affect_chat_model_adapter import AffectDraftDeliberationAdapter
from .relationship_draft_deliberation_adapter import RelationshipDraftDeliberationAdapter
from .chat_model_deliberation_adapter import ChatCompletionModel
from .deliberation import DeliberationModelAdapter
from .perception_executor import PerceptionTransport
from .perception_input_source import PerceptionInputSource
from .platform_host import PlatformClockTick, PlatformInbound, WorldV2PlatformHost
from .production_latency_trace import ProductionLatencySample
from .production_reliability_metrics import record_visible_reply
from .production_turn_application import (
    LifeEcologyComposition,
    MediaPreviewDeployment,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from .platform_action_executor import MediaProviderTransport
from .qq_c2c_transport import QQC2CDelivery, QQC2CPlatformTransport
from .qq_ingress_policy import (
    QQIngressBatch,
    QQIngressFragment,
    QQIngressStore,
    SQLiteQQIngressStore,
)
from .semantic_chat_composition import (
    SemanticChatComposition,
    build_semantic_chat_composition,
)
from .expression_draft import qq_expression_capabilities


_LOG = logging.getLogger(__name__)


# Shape hints for the adaptive composure gap.  A closed question or clearly
# terminated sentence usually stands alone; a clause that trails off with a
# comma, colon, or a dangling connective ("而且", "然后", "对了") almost always
# has another bubble behind it.  These are pacing hints only — they never gate
# what she may say.
_COMPLETE_TAIL = re.compile(r"[。！？!?~♪…]+[\"”』」)]*$|[吗嘛呢吧啦哟呀哈]$")
_CONTINUING_TAIL = re.compile(
    r"[，、：:,]$"
    r"|(?:而且|然后|不过|但是|所以|还有|就是|其实|对了|比如|因为|要是|如果|另外)$"
)


class QQC2CIdentityResolver:
    """Resolve exactly one configured QQ C2C recipient into one v2 world."""

    def __init__(self, *, recipient_id: str, canonical_user_id: str) -> None:
        if not recipient_id or not canonical_user_id:
            raise ValueError("QQ C2C identity requires recipient and canonical user ids")
        self._recipient_id = recipient_id
        self._canonical_user_id = canonical_user_id

    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        if platform != "qq" or platform_user_id != self._recipient_id:
            raise ValueError("QQ C2C ingress is not configured for this World v2 host")
        return (
            f"user:{self._canonical_user_id}",
            qq_c2c_target(self._recipient_id),
        )


def qq_c2c_target(recipient_id: str) -> str:
    if not recipient_id:
        raise ValueError("QQ C2C recipient id is required")
    return f"conversation:qq:c2c:{recipient_id}"


def qq_c2c_world_id(primary_user_id: str) -> str:
    """The one durable world identity of the QQ C2C composition."""

    if not primary_user_id:
        raise ValueError("QQ C2C world identity requires the primary user id")
    return f"world:companion-v2:qq-c2c:{primary_user_id}"


def _parse_metadata_time(value: object) -> datetime | None:
    """Best-effort observability parse; never an authority input."""

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class QQC2CIngressResult:
    status: str
    action_id: str | None
    canonical_user_id: str


@dataclass(frozen=True, slots=True)
class QQC2CDrainResult:
    action_statuses: tuple[str, ...]
    background_statuses: tuple[str, ...]


class QQC2CHost:
    """Small C2C-only facade over a durable :class:`WorldV2PlatformHost`.

    The process-local lock only serializes one adapter process.  The ledger
    remains the authority for duplicate ingress and restart recovery.
    """

    def __init__(
        self,
        *,
        host: WorldV2PlatformHost,
        recipient_id: str,
        canonical_user_id: str,
        semantic_chat: SemanticChatComposition | None = None,
        ingress_store: QQIngressStore | None = None,
        ingress_now: Callable[[], datetime] | None = None,
        ingress_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        typing_signal: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        if not recipient_id or not canonical_user_id:
            raise ValueError("QQ C2C host requires recipient and canonical user ids")
        self._host = host
        self._recipient_id = recipient_id
        self._canonical_user_id = canonical_user_id
        self._semantic_chat = semantic_chat
        self._ingress_store = ingress_store
        self._ingress_now = ingress_now or (lambda: datetime.now(UTC))
        self._ingress_sleep = ingress_sleep
        self._typing_signal = typing_signal
        self._ingress_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._closed = False
        self._last_content_received_at: datetime | None = None
        self._last_content_text: str | None = None
        self._last_typing_started_at: datetime | None = None
        self._recent_gap_seconds: deque[float] = deque(maxlen=8)
        # Number of content arrivals currently inside their sender-rhythm
        # hold.  While it is non-zero a volley is still being absorbed, so
        # the periodic scheduler claim path (``drain_ingress_once``) yields
        # instead of slicing the already-due half of the volley into its own
        # turn; the holding fragment claims the complete batch itself once
        # the sender goes quiet.  Pure claim-timing courtesy: batch identity
        # and ledger state never depend on it.
        self._rhythm_holds = 0

    async def inbound_text(
        self,
        *,
        message_id: str,
        recipient_id: str,
        text: str,
        observed_at: datetime,
    ) -> QQC2CIngressResult:
        """Ingest one authorized C2C text message and drain only its Action."""

        if not message_id or not text.strip():
            raise ValueError("QQ C2C v2 ingress requires a message id and non-empty text")
        return await self.inbound_fragment(
            QQIngressFragment(
                source_event_id=message_id,
                recipient_id=recipient_id,
                observed_at=observed_at,
                content_shape="text",
                text=text.strip(),
            )
        )

    async def inbound_fragment(self, fragment: QQIngressFragment) -> QQC2CIngressResult:
        """Persist one normalized fragment and join its deterministic batch."""

        if fragment.recipient_id != self._recipient_id:
            raise ValueError("QQ C2C recipient is not configured for this World v2 host")
        if self._ingress_store is None:
            raise RuntimeError("QQ C2C ingress store is not configured")
        received_at = self._ingress_now()
        # A message landing while her visible turn still owns the world lock
        # cannot be an answer to that turn's reply (she has not spoken yet) —
        # the sender is provably continuing their own volley, so the composure
        # gap must respect their just-shown cadence instead of trusting stale
        # median statistics from an earlier, faster exchange.
        burst_continuation = self._visible_turn_in_flight()
        previous_received_at = self._last_content_received_at
        submitted = self._ingress_store.submit(fragment, received_at=received_at)
        if submitted.state == "committed":
            return QQC2CIngressResult(
                status=submitted.outcome_status or "observed_only",
                action_id=submitted.action_id,
                canonical_user_id=self._canonical_user_id,
            )
        if fragment.content_shape == "control":
            if fragment.control_kind == "typing_started":
                # The peer is visibly composing: any in-flight rhythm hold
                # keeps waiting so the upcoming bubbles land in one turn.
                self._last_typing_started_at = received_at
            return QQC2CIngressResult(
                status="deferred",
                action_id=None,
                canonical_user_id=self._canonical_user_id,
            )
        self._register_content_gap(
            received_at=received_at, previous_received_at=previous_received_at
        )
        self._last_content_received_at = received_at
        self._last_content_text = fragment.text
        delay = max(0.0, (submitted.due_at - self._ingress_now()).total_seconds())
        if delay:
            await self._ingress_sleep(delay)
        await self._hold_for_sender_rhythm(
            fragment=fragment,
            received_at=received_at,
            burst_continuation=burst_continuation,
        )
        for _ in range(8):
            # Claim only once the world lock is available.  While an earlier
            # turn is still deliberating, later fragments of the same ongoing
            # exchange stay pending and the eventual claim joins them into one
            # batch — one continuous conversation is one turn, not a queue of
            # independent full turns each producing its own reply.
            async with self._lock:
                batch = None
                already = self._ingress_store.submission(fragment.source_event_id)
                if already is None or already.state != "committed":
                    async with self._ingress_lock:
                        batch = self._ingress_store.claim_due(now=self._ingress_now())
                    if batch is not None:
                        await self._process_ingress_batch_locked(batch)
                # else: a sibling's claim already answered this fragment
                # while it waited for the lock.  It must not claim anything
                # further: whatever is pending now belongs to a newer volley
                # whose own fragments are still pacing their claim, and
                # grabbing it early would slice that volley in half.
            current = self._ingress_store.submission(fragment.source_event_id)
            if current is not None and current.state == "committed":
                return QQC2CIngressResult(
                    status=current.outcome_status or "observed_only",
                    action_id=current.action_id,
                    canonical_user_id=self._canonical_user_id,
                )
            if batch is None:
                # A concurrent caller may be committing the claimed batch.
                await self._ingress_sleep(0)
        return QQC2CIngressResult(
            status="deferred",
            action_id=None,
            canonical_user_id=self._canonical_user_id,
        )

    # Provider-local sender-rhythm pacing, deliberately outside the frozen
    # 400-800ms coalescing matrix: it delays only the *claim*, never changes
    # batch identity, ledger state, or replay.  Nothing here is a fixed
    # session rule — the quiet gap adapts to the sender's own measured typing
    # cadence and to the shape of the message itself, because a person's
    # conversation can be continuous at any tempo.
    _TEMPO_WINDOW_SECONDS = 600.0
    _TEMPO_SAMPLE_CEILING_SECONDS = 45.0
    _DEFAULT_QUIET_GAP_SECONDS = 3.5
    _MIN_QUIET_GAP_SECONDS = 1.2
    _MAX_QUIET_GAP_SECONDS = 12.0
    # Absolute per-fragment bound on burst absorption.  A person being
    # flooded keeps reading as long as bubbles keep landing, but after about
    # half a minute they interject anyway — so the hold keeps rolling while
    # the volley continues and answers what has arrived once this cap hits.
    _BURST_HOLD_CAP_SECONDS = 30.0

    def _register_content_gap(
        self, *, received_at: datetime, previous_received_at: datetime | None
    ) -> None:
        """Track the sender's live typing cadence for adaptive pacing."""

        if previous_received_at is None:
            return
        gap = (received_at - previous_received_at).total_seconds()
        if gap > self._TEMPO_WINDOW_SECONDS:
            # A long silence starts a fresh exchange; yesterday's cadence
            # says nothing about how they are typing now.
            self._recent_gap_seconds.clear()
        elif 0 < gap <= self._TEMPO_SAMPLE_CEILING_SECONDS:
            # Only bubble-to-bubble gaps are cadence; a minutes-later reply
            # is a new thought, not typing rhythm.
            self._recent_gap_seconds.append(gap)

    def _quiet_gap_seconds(self, text: str | None, *, burst: bool = False) -> float:
        """One adaptive composure gap: cadence base times a content bias.

        The base follows how quickly this person has actually been sending
        bubbles right now; the bias reads the message's own shape — a clause
        that ends mid-thought ("而且…", trailing comma) earns more patience
        than a closed question.  Everything stays bounded and deterministic.

        ``burst`` marks a message that provably continues an ongoing volley
        (it landed during her turn, or during another bubble's hold).  Then
        the *latest* observed gap sets a floor on the wait: someone who just
        demonstrated an X-seconds-per-bubble rhythm has not finished talking
        after less than ~1.2X of silence, however short the median of earlier,
        faster gaps is and however closed the sentence looks.  The floor
        never exceeds the ordinary maximum, and gaps slower than that maximum
        are a lull rather than a rhythm, so they raise nothing.
        """

        if self._recent_gap_seconds:
            ordered = sorted(self._recent_gap_seconds)
            median = ordered[len(ordered) // 2]
            base = min(max(median * 1.3, 1.5), 8.0)
        else:
            base = self._DEFAULT_QUIET_GAP_SECONDS
        stripped = (text or "").rstrip()
        bias = 1.0
        if stripped:
            if _COMPLETE_TAIL.search(stripped):
                bias = 0.6
            elif _CONTINUING_TAIL.search(stripped):
                bias = 1.7
        gap = min(max(base * bias, self._MIN_QUIET_GAP_SECONDS), self._MAX_QUIET_GAP_SECONDS)
        if burst and self._recent_gap_seconds:
            cadence = self._recent_gap_seconds[-1]
            if cadence <= self._MAX_QUIET_GAP_SECONDS:
                gap = max(gap, min(cadence * 1.2, self._MAX_QUIET_GAP_SECONDS))
        return gap

    async def _hold_for_sender_rhythm(
        self,
        *,
        fragment: QQIngressFragment,
        received_at: datetime,
        burst_continuation: bool = False,
    ) -> None:
        """Wait for an adaptive quiet gap so one volley becomes one turn.

        A person composing consecutive bubbles is telling one continuous
        thought.  Starting a full turn on each bubble answers them one-by-one
        and queues the rest behind long model calls — the exact "机械一一对应"
        complaint.  Every content message therefore pays a composure pause
        sized by the sender's live cadence, the message's own shape, and any
        provider "peer is typing" pulse; whatever arrives during the pause
        joins the same batch and gets one reply.

        While bubbles keep landing the hold keeps rolling: each newer bubble
        re-sizes the remaining wait from *its* shape and the live cadence
        (a volley whose tail trails off earns more patience than one that
        just closed), and any bubble arriving during a hold is by definition
        a burst continuation, so the burst floor applies.  A person being
        flooded does not answer sentence three of six mid-stream — but they
        do interject after about half a minute, which is what the absolute
        ``_BURST_HOLD_CAP_SECONDS`` cap (anchored to this fragment's own
        arrival, and also bounding endless "typing…" pulses) reproduces.
        """

        quiet_gap = self._quiet_gap_seconds(fragment.text, burst=burst_continuation)
        hard_cap = received_at + timedelta(seconds=self._BURST_HOLD_CAP_SECONDS)
        self._rhythm_holds += 1
        try:
            while True:
                now = self._ingress_now()
                latest = self._last_content_received_at or received_at
                if latest > received_at:
                    # A newer bubble landed during this hold, so the volley is
                    # still going: let the newest bubble's shape and the
                    # just-measured cadence decide how much longer to wait.
                    quiet_gap = self._quiet_gap_seconds(
                        self._last_content_text, burst=True
                    )
                # A provider "peer is typing" pulse counts as not-quiet: she
                # can see the person still composing, so she keeps waiting
                # (within the same absolute cap) instead of answering half a
                # thought.
                typing_at = self._last_typing_started_at
                if typing_at is not None and typing_at > latest:
                    latest = typing_at
                quiet_for = (now - latest).total_seconds()
                if quiet_for >= quiet_gap or now >= hard_cap:
                    return
                await self._ingress_sleep(
                    min(
                        max(quiet_gap - quiet_for, 0.05),
                        max((hard_cap - now).total_seconds(), 0.05),
                    )
                )
        finally:
            self._rhythm_holds -= 1

    def submission_state(self, source_event_id: str) -> str | None:
        """Read-only durable dedupe check for restart-window compensation."""

        if self._ingress_store is None or not source_event_id:
            return None
        submitted = self._ingress_store.submission(source_event_id)
        return submitted.state if submitted is not None else None

    def _visible_turn_in_flight(self) -> bool:
        """Report whether a user-visible turn currently owns the world lock.

        ``self._lock`` is held for the whole visible ingress turn — context,
        model call, and the reply's ledger record.  Background scheduler work
        consults this signal between durable units so a waiting reply is never
        starved by a long chain of background commits.  This is scheduling
        courtesy only: ledger CAS and durable claims remain the correctness
        authority whether or not background work defers.
        """

        return self._lock.locked()

    async def drain_ingress_once(self) -> QQC2CIngressResult | None:
        """Resume one due or previously claimed batch after a restart."""

        if self._ingress_store is None:
            return None
        if self._rhythm_holds > 0:
            # A fragment is still absorbing an ongoing volley.  The oldest
            # bubbles of that volley are already claim-due, so a periodic
            # scheduler pass claiming here would slice the volley in half;
            # the holding fragment claims the complete batch itself once the
            # sender goes quiet.  After a restart no hold exists, so recovery
            # is never deferred by this courtesy.
            return None
        async with self._lock:
            async with self._ingress_lock:
                batch = self._ingress_store.claim_due(now=self._ingress_now())
            if batch is None:
                return None
            return await self._process_ingress_batch_locked(batch)

    def _pulse_typing(self) -> None:
        """Fire one best-effort provider typing pulse for a starting turn.

        Reading a message and starting to answer is exactly when a person's
        "typing…" appears, and the turn takes seconds of model work.  This is
        provider-local presence metadata: no ledger write, no reply authority,
        and any failure is swallowed so it can never affect the turn.
        """

        if self._typing_signal is None:
            return

        async def run() -> None:
            try:
                await self._typing_signal()
            except Exception:  # noqa: BLE001 - presence pulse must never fail a turn
                return

        task = asyncio.create_task(run())
        task.add_done_callback(lambda item: item.exception() if not item.cancelled() else None)

    async def _process_ingress_batch_locked(self, batch: QQIngressBatch) -> QQC2CIngressResult:
        """Run one claimed batch's turn.  The caller must hold ``self._lock``."""

        if batch.text is not None or batch.attachment_refs:
            self._pulse_typing()
        metadata = dict(batch.metadata)
        # New stores freeze the first claim instant in the durable batch. Old
        # claimed rows fall back to their already-persisted window close, which
        # is conservative for latency but, critically, stable across recovery.
        metadata.setdefault("processing_started_at", metadata.get("window_closed_at"))
        inbound = PlatformInbound(
            platform="qq",
            platform_user_id=batch.recipient_id,
            platform_message_id=batch.platform_message_id,
            text=batch.text,
            observed_at=batch.observed_at,
            trace_id=f"trace:qq-c2c-v2:{batch.recipient_id}:{batch.batch_id}",
            attachment_refs=batch.attachment_refs,
            coalescing_metadata=metadata,
        )
        outcome = await self._host.inbound(inbound)
        if outcome.status == "action_authorized":
            # Denominator for the /health failsafe rate: one inbound turn
            # produced an authorized visible reply (failsafe replies included).
            record_visible_reply()
        action_id = next(
            iter((*outcome.authorized_action_ids, *outcome.scheduled_action_ids)), None
        )
        if action_id is not None:
            result = await self._host.drain_action(action_id)
            if result is not None and result.action_id not in {None, action_id}:
                raise RuntimeError("targeted QQ C2C drain returned a different Action")
            # User-perceived audit line: first fragment arrival to the visible
            # reply's provider dispatch.  The quick-reaction counterpart is
            # logged by the runtime turn as user_perceived_quick_reaction_ms.
            opened = _parse_metadata_time(metadata.get("window_opened_at"))
            if opened is not None:
                _LOG.warning(
                    "world v2 user_perceived trace=%s user_perceived_reply_ms=%.1f status=%s",
                    inbound.trace_id,
                    (self._ingress_now() - opened).total_seconds() * 1000,
                    outcome.status,
                )
        self._ingress_store.complete(
            batch_id=batch.batch_id,
            outcome_status=outcome.status,
            action_id=action_id,
        )
        return QQC2CIngressResult(
            status=outcome.status,
            action_id=action_id,
            canonical_user_id=self._canonical_user_id,
        )

    async def tick(
        self,
        *,
        tick_id: str,
        logical_time_from: datetime,
        logical_time_to: datetime,
        observed_at: datetime,
        reason: str,
    ) -> str:
        """Advance a caller-owned durable scheduler interval through the v2 host."""

        async with self._lock:
            outcome = await self._host.tick(
                PlatformClockTick(
                    tick_id=tick_id,
                    logical_time_from=logical_time_from,
                    logical_time_to=logical_time_to,
                    observed_at=observed_at,
                    trace_id=f"trace:qq-c2c-v2:tick:{tick_id}",
                    causation_id=f"scheduler:qq-c2c-v2:{tick_id}",
                    correlation_id=f"clock:qq-c2c-v2:{self._recipient_id}",
                    reason=reason,
                )
            )
            return outcome.status

    async def drain(
        self, *, max_action_units: int = 8, max_background_units: int = 8
    ) -> QQC2CDrainResult:
        """Run restart-safe Action recovery and bounded background work once."""

        if not 0 <= max_action_units <= 64 or not 0 <= max_background_units <= 64:
            raise ValueError("QQ C2C drain limits must be between 0 and 64")
        # Do not hold the ingress serialization lock across model-backed
        # scheduler work.  Runtime-level durable claims/CAS serialize the
        # world mutation; this adapter lock is only for the short visible
        # ingress/tick critical sections.  A slow fact/appraisal/proactive
        # model must not make a new user message wait behind ``drain``.
        drained = await self._host.drain_scheduled_work(
            max_action_units=max_action_units,
            max_background_units=max_background_units,
            media_preview_trace_id="trace:qq-c2c-v2:media-preview",
            media_preview_correlation_id=(
                f"correlation:qq-c2c-v2:media-preview:{self._recipient_id}"
            ),
        )
        return QQC2CDrainResult(
            action_statuses=drained.action_statuses,
            background_statuses=drained.background_statuses,
        )

    def latency_samples(self) -> tuple[ProductionLatencySample, ...]:
        """Expose read-only process evidence for diagnostics and acceptance runs."""

        return self._host.latency_samples()

    async def scheduler_once(
        self,
        *,
        observed_at: datetime,
        max_action_units: int = 8,
        max_background_units: int = 8,
    ) -> QQC2CDrainResult:
        """Continue the durable clock and run recovery after a host restart.

        The ``from`` timestamp comes from the v2 application rather than a
        process-local variable, so a restart cannot invent a stale interval.
        """

        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("QQ C2C scheduler time must be timezone-aware")
        if not 0 <= max_action_units <= 64 or not 0 <= max_background_units <= 64:
            raise ValueError("QQ C2C scheduler drain limits must be between 0 and 64")
        scheduler_started_at = self._ingress_now()
        for _ in range(8):
            if await self.drain_ingress_once() is None:
                break
        # Model-backed cognition must not hold the ingress serialization lock:
        # a user message arriving during a slow advisory call should still be
        # accepted immediately. Ledger cursor CAS remains the cross-task
        # authority; a raced background proposal fails stale instead of
        # extending this process-local critical section.
        pre_background: list[str] = []
        # The caller's background budget is authoritative.  In particular,
        # ``max_background_units=0`` is used by ingress-only/recovery passes
        # and must not silently turn into sixteen model-backed cognition
        # attempts (which can create the observed 30s+ QQ tail).
        background_remaining = max_background_units
        for _ in range(background_remaining):
            # A visible turn owns ``self._lock`` for its whole reply
            # (context, model, record).  Its ledger commit must not queue
            # behind a chain of multi-second background commits, so the
            # scheduler stops consuming its budget while one is in flight;
            # the durable claims simply wait for the next pass.
            if self._visible_turn_in_flight():
                break
            result = await self._host.drain_background_once()
            # Yield the event loop between durable units so a just-arrived
            # visible turn can claim its batch before the next unit starts.
            await asyncio.sleep(0)
            if result is None:
                break
            work_status = getattr(result, "work_status", None)
            if getattr(result, "status", None) == "idle" and work_status is None:
                break
            pre_background.append(str(work_status or "processed"))
            background_remaining -= 1

        scheduler_finished_work_at = self._ingress_now()
        elapsed = scheduler_finished_work_at - scheduler_started_at
        if elapsed.total_seconds() < 0:
            elapsed = timedelta(0)
        tick_boundary = observed_at + elapsed
        # Only the short logical-clock CAS is serialized with inbound turns.
        # Re-read the head after waiting: inbound may have committed while the
        # background work above was in flight.
        async with self._lock:
            logical_from = await self._host.current_logical_time()
            if logical_from is not None and tick_boundary > logical_from:
                tick_id = "tick:qq-c2c-v2:" + tick_boundary.isoformat()
                outcome = await self._host.tick(
                    PlatformClockTick(
                        tick_id=tick_id,
                        logical_time_from=logical_from,
                        logical_time_to=tick_boundary,
                        observed_at=tick_boundary,
                        trace_id=f"trace:qq-c2c-v2:{tick_id}",
                        causation_id=f"scheduler:qq-c2c-v2:{tick_id}",
                        correlation_id=f"clock:qq-c2c-v2:{self._recipient_id}",
                        reason="qq_c2c_scheduler",
                    )
                )
                if outcome.status not in {"observed_only", "deferred"}:
                    raise RuntimeError("QQ C2C scheduler clock was not accepted")
        # Give time-sensitive social initiative one chance against the new
        # clock before the generic action-recovery queue runs.  A prior reply
        # can legitimately be ``provider_accepted`` without a provider-side
        # delivery lookup; the generic pump converts that state to ``unknown``
        # after its recovery lease.  If recovery runs first, a response-gap
        # opportunity is no longer eligible even though the provider accepted
        # the visible reply.  This bounded preflight only opens/advances one
        # background process; normal budgets and action recovery still run in
        # ``drain_scheduled_work`` below.
        post_tick_background: list[str] = []
        initiative_action_id: str | None = None
        if background_remaining > 0:
            # Opening and authorizing a response-gap process are separate
            # durable background steps.  Continue only until that lane has
            # produced its Action (or the bounded background budget is spent),
            # so the targeted dispatch below can happen before old reply
            # recovery changes its state to ``unknown``.
            for _ in range(background_remaining):
                if self._visible_turn_in_flight():
                    break
                result = await self._host.drain_background_once()
                await asyncio.sleep(0)
                work_status = getattr(result, "work_status", None)
                if result is None or (
                    getattr(result, "status", None) == "idle" and work_status is None
                ):
                    break
                post_tick_background.append(str(work_status or "processed"))
                background_remaining -= 1
                candidate_action_id = getattr(result, "action_id", None)
                if isinstance(candidate_action_id, str) and candidate_action_id:
                    initiative_action_id = candidate_action_id
                    break
        post_tick_actions: list[str] = []
        if initiative_action_id is not None and max_action_units > 0:
            targeted = await self._host.drain_action(initiative_action_id)
            if targeted is not None:
                post_tick_actions.append(str(getattr(targeted, "status", "processed")))
        # Post-tick background/model work is likewise outside the ingress
        # lock. Action/trigger claims and cursor CAS provide idempotency.
        drained = await self._host.drain_scheduled_work(
            # One action unit may have been consumed by the targeted initiative
            # dispatch above.  Keep the caller's action budget bounded while
            # retaining the generic recovery pass for unrelated actions.
            max_action_units=max(0, max_action_units - len(post_tick_actions)),
            # The pre-tick reserve protects message-owned cognition from a
            # stale clock cursor.  It is not the post-tick work budget.
            max_background_units=background_remaining,
            media_preview_trace_id="trace:qq-c2c-v2:media-preview",
            media_preview_correlation_id=(
                f"correlation:qq-c2c-v2:media-preview:{self._recipient_id}"
            ),
            # A visible reply's record commit must not wait behind this
            # pass's remaining background commits.  Preemption only stops
            # starting new units; anything already claimed stays durable and
            # resumes on the next scheduler pass.
            should_preempt=self._visible_turn_in_flight,
        )
        return QQC2CDrainResult(
            action_statuses=(*post_tick_actions, *drained.action_statuses),
            background_statuses=(
                *pre_background,
                *post_tick_background,
                *drained.background_statuses,
            ),
        )

    async def world_health_diagnostics(self) -> dict[str, object]:
        """Expose the platform-neutral projection-only health read."""

        return await self._host.world_health_diagnostics()

    def media_preview_operator(self):
        """Expose the read-only media observation service for this world."""

        return self._host.media_preview_operator()

    async def maintain_wal_once(self):
        """Run one bounded passive WAL checkpoint on the scheduler lane.

        The ledger keeps ``wal_autocheckpoint = 0`` so a visible reply never
        pays a synchronous multi-megabyte checkpoint.  Without this scheduler
        hook the QQ lane's WAL grows without bound and degrades every read
        and commit, so the same maintenance seam used by the HTTP capture
        host must run here as well.
        """

        return await self._host.maintain_wal_once()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._host.close()
        if self._ingress_store is not None:
            self._ingress_store.close()
        if self._semantic_chat is not None:
            await self._semantic_chat.aclose()


def build_qq_c2c_host(
    *,
    settings: Settings,
    recipient_id: str,
    bootstrap_at: datetime | None = None,
    model: ChatCompletionModel | None = None,
    thinking_model: ChatCompletionModel | None = None,
    advisory_model: ChatCompletionModel | None = None,
    delivery: QQC2CDelivery | None = None,
    media_transport: MediaProviderTransport | None = None,
    media_preview: MediaPreviewDeployment | None = None,
    perception_model: DeliberationModelAdapter | None = None,
    perception_input_source: PerceptionInputSource | None = None,
    perception_transport: PerceptionTransport | None = None,
    perception_budget_limit: int = 0,
    quick_reaction_model: ChatCompletionModel | None = None,
) -> QQC2CHost:
    """Compose the C2C lane without importing legacy chat/runtime code.

    Media remains opt-in: a caller may provide only a transport that durably
    binds result bytes to idempotency keys and supports recovery lookup.  QQ
    delivery itself is deliberately text-only and is never used as an image
    provider fallback.
    """

    if not recipient_id:
        raise ValueError("QQ C2C v2 requires one configured private recipient")
    expression_capabilities = qq_expression_capabilities(settings.qq_adapter)
    semantic_chat = build_semantic_chat_composition(
        settings=settings,
        flash_model=model,
        thinking_model=thinking_model,
        advisory_model=advisory_model,
        model_id_prefix="qq-c2c-v2",
        expression_capabilities=expression_capabilities,
    )
    model = semantic_chat.flash_model
    background_model = semantic_chat.background_model
    delivery = delivery or QQDelivery(settings)
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={qq_c2c_target(recipient_id): recipient_id},
        now=lambda: datetime.now(UTC),
    )
    application = build_sqlite_world_v2_turn_application(
        path=Path(settings.database_path),
        config=WorldV2TurnApplicationConfig(
            world_id=qq_c2c_world_id(settings.primary_user_id),
            companion_actor_ref="agent:companion",
            reply_target=qq_c2c_target(recipient_id),
            action_pump_owner="pump:qq-c2c-v2",
            counterpart_actor_ref=f"user:{settings.primary_user_id}",
            local_timezone=settings.local_timezone,
            trace_environment="real_transport",
            expression_action_kinds=expression_capabilities.action_kinds,
            life_ecology=LifeEcologyComposition.production_v1(),
            immediate_emotion_signal_gate=True,
            media_selection_acceptance=(
                media_preview.acceptance if media_preview is not None else None
            ),
            media_continuation=(media_preview.continuation if media_preview is not None else None),
            media_auto_delivery=(
                media_preview.auto_delivery if media_preview is not None else None
            ),
            perception_budget_limit=perception_budget_limit,
        ),
        identities=QQC2CIdentityResolver(
            recipient_id=recipient_id, canonical_user_id=settings.primary_user_id
        ),
        router=semantic_chat.router,
        main_model=semantic_chat.main_model,
        quick_recovery=semantic_chat.main_model,
        transport=transport,
        media_transport=media_transport,
        media_planner=(media_preview.planner if media_preview is not None else None),
        advisory_compiler=semantic_chat.advisory_compiler,
        appraisal_model=semantic_chat.appraisal_model,
        affect_model=AffectDraftDeliberationAdapter(model=background_model),
        perception_model=perception_model,
        perception_input_source=perception_input_source,
        perception_transport=perception_transport,
        relationship_model=RelationshipDraftDeliberationAdapter(model=background_model),
        outcome_draft_model=background_model,
        # This is background-only cognitive work; it never extends the QQ
        # interactive reply path, but makes accepted facts available next turn.
        fact_model=background_model,
        # Private impressions consolidate accepted appraisals on the same
        # background channel; they never touch the interactive reply path.
        private_impression_model=background_model,
        proactive_model=background_model,
        proactive_identity_frame=semantic_chat.identity_frame,
        memory_model=background_model,
        activity_lifecycle_model=background_model,
        # The open-world lane was designed and composed in
        # build_sqlite_world_v2_turn_application but no production host ever
        # injected its model, so the ledger never contained a single
        # model-authored temporary event.  It shares the background channel:
        # it runs only on quiet ecology wakes and never touches the
        # interactive reply path.
        open_world_event_model=background_model,
        media_selection_model=(
            media_preview.selection_model if media_preview is not None else None
        ),
        # Production leaves this None so the quick-reaction lane shares the
        # local appraisal client through the adapter seam; tests inject a
        # fixture model here to exercise the lane against a fake NapCat.
        quick_reaction_model=quick_reaction_model,
        now=bootstrap_at or datetime.now(UTC),
    )
    typing_signal = None
    send_typing = getattr(delivery, "send_typing", None)
    if settings.qq_adapter.lower() == "napcat" and callable(send_typing):

        async def send_typing_pulse() -> object:
            return await send_typing(recipient_id, state="composing")

        typing_signal = send_typing_pulse
    return QQC2CHost(
        host=WorldV2PlatformHost(application=application),
        recipient_id=recipient_id,
        canonical_user_id=settings.primary_user_id,
        semantic_chat=semantic_chat,
        ingress_store=SQLiteQQIngressStore(Path(settings.database_path)),
        typing_signal=typing_signal,
    )


__all__ = [
    "QQC2CDrainResult",
    "QQC2CDelivery",
    "QQC2CHost",
    "QQC2CIdentityResolver",
    "QQC2CIngressResult",
    "QQC2CPlatformTransport",
    "build_qq_c2c_host",
    "qq_c2c_target",
    "qq_c2c_world_id",
]
