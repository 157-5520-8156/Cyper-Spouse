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
from contextlib import asynccontextmanager
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Literal

from companion_daemon.config import Settings
from companion_daemon.qq_delivery import QQDelivery

from .action_due_wake import ActionDueWake
from .affect_chat_model_adapter import AffectDraftDeliberationAdapter
from .errors import ConcurrencyConflict
from .relationship_draft_deliberation_adapter import RelationshipDraftDeliberationAdapter
from .chat_model_deliberation_adapter import ChatCompletionModel
from .deliberation import DeliberationModelAdapter
from .perception_executor import PerceptionTransport
from .perception_input_source import PerceptionInputSource
from .platform_host import PlatformClockTick, PlatformInbound, WorldV2PlatformHost
from .production_latency_trace import ProductionLatencySample
from .production_reliability_metrics import record_dispatch_ack, record_visible_reply
from .production_turn_application import (
    LifeEcologyComposition,
    MediaPreviewDeployment,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from .platform_action_executor import (
    MediaProviderTransport,
    USER_VISIBLE_PLATFORM_ACTION_KINDS,
)
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
from .expression_episode_lifecycle import next_expression_retry_due
from .interactive_turn_budget import InteractiveTurnBudgetPolicy
from .life_development_model_adapter import RoleBoundLifeDevelopmentModelAdapter
from .recall_embedding import configured_recall_embedding
from .recall_index import RecallEmbedding
from .replay_evidence import ReplayEvidence


_LOG = logging.getLogger(__name__)


_MAX_EXACT_DUE_ADVANCES = 64
_ACTION_PUMP_CONFLICT_BACKOFF_SECONDS = (0.0, 0.01)
_OWNED_ACTION_DRAIN_CANCEL_GRACE_SECONDS = 0.1


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _next_action_due_boundary(
    projection: object,
    *,
    after: datetime,
    through: datetime,
) -> datetime | None:
    """Return the next Action boundary strictly after the current world time.

    A due Action remains scheduled until the Action pump claims it, so the
    projection's absolute ``nearest_due`` can keep returning a boundary the
    clock has already crossed.  The scheduler needs a cursor-relative view in
    order to advance several exact boundaries in one pass.
    """

    candidates: list[datetime] = []
    for action in getattr(projection, "actions", ()):
        due = None
        if getattr(action, "state", None) in {"authorized", "scheduled"}:
            due = getattr(action, "not_before", None)
        elif getattr(action, "state", None) in {
            "claimed",
            "dispatch_started",
            "provider_accepted",
        }:
            lease = getattr(action, "claim_lease", None)
            due = getattr(lease, "expires_at", None)
        if isinstance(due, datetime) and after < due <= through:
            candidates.append(due)
    return min(candidates, default=None)


class _VisibleTurnReconciliationGate:
    """Let visible turns overlap while excluding old receipt commits.

    Verification may run concurrently, but its terminal ledger commit uses
    :meth:`try_acquire_reconciliation`.  A visible turn therefore never waits
    behind a provider lookup; at most it waits for a reconciliation commit
    that already won this short process-local gate.
    """

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._visible_turns = 0
        self._reconciliation_active = False

    @asynccontextmanager
    async def visible_turn(self):
        async with self._condition:
            while self._reconciliation_active:
                await self._condition.wait()
            self._visible_turns += 1
        try:
            yield
        finally:
            async with self._condition:
                self._visible_turns -= 1
                if self._visible_turns == 0:
                    self._condition.notify_all()

    async def try_acquire_reconciliation(self) -> bool:
        async with self._condition:
            if self._visible_turns or self._reconciliation_active:
                return False
            self._reconciliation_active = True
            return True

    async def acquire_reconciliation(self) -> None:
        """Wait without an ActionPump lock, then exclude new visible pins."""

        async with self._condition:
            while self._visible_turns or self._reconciliation_active:
                await self._condition.wait()
            self._reconciliation_active = True

    async def release_reconciliation(self) -> None:
        async with self._condition:
            if not self._reconciliation_active:
                raise RuntimeError("provider reconciliation gate is not held")
            self._reconciliation_active = False
            self._condition.notify_all()

    async def wait_for_visible_turns(self) -> None:
        async with self._condition:
            while self._visible_turns:
                await self._condition.wait()


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
        observation_clock_ns: Callable[[], int] = time.perf_counter_ns,
        action_due_now: Callable[[], datetime] | None = None,
        action_due_sleep: Callable[[float], Awaitable[None]] | None = None,
        interactive_turn_budget_policy: InteractiveTurnBudgetPolicy | None = None,
        recorded_cadence_mode: str = "off",
        idle_heartbeat_seconds: float = 0.0,
        owned_action_close_grace_seconds: float = 1.0,
    ) -> None:
        if not recipient_id or not canonical_user_id:
            raise ValueError("QQ C2C host requires recipient and canonical user ids")
        if not 0 <= owned_action_close_grace_seconds <= 30:
            raise ValueError("owned Action close grace must be between zero and 30 seconds")
        self._host = host
        self._recipient_id = recipient_id
        self._canonical_user_id = canonical_user_id
        self._semantic_chat = semantic_chat
        self._ingress_store = ingress_store
        self._ingress_now = ingress_now or _utc_now
        self._ingress_sleep = ingress_sleep
        # Observation latency is process evidence, not World/pacing time.
        # Keep it on an independent monotonic clock so an offline audit may
        # fast-forward coalescing and Action deadlines without manufacturing
        # a subsecond "user perceived" result around a real multi-second model
        # call. The durable ingress store remains the restart authority; these
        # timestamps intentionally disappear with the process.
        self._observation_clock_ns = observation_clock_ns
        self._arrival_ns_by_source_event_id: dict[str, int] = {}
        # Sender-rhythm/pacing clocks are deliberately injectable so an
        # offline conversation audit can skip sub-second UI waits.  A future
        # Action deadline is world scheduling authority, not presentation
        # pacing: sharing that virtual sleep fast-forwards ``later`` Actions
        # by hours in the same event-loop turn.
        # Direct facade construction preserves its historical injected-clock
        # behavior. The production builder supplies an explicit scheduler
        # clock, so presentation pacing cannot become deadline authority
        # accidentally in a composed host.
        self._action_due_now = action_due_now or self._ingress_now
        self._action_due_sleep = action_due_sleep or asyncio.sleep
        self._interactive_turn_budget_policy = interactive_turn_budget_policy
        if idle_heartbeat_seconds < 0:
            raise ValueError("idle heartbeat seconds must not be negative")
        self._idle_heartbeat_seconds = idle_heartbeat_seconds
        self._ingress_lock = asyncio.Lock()
        # Multiple fragments from one coalesced batch may all observe the same
        # durable claim. Join their process-local execution so only one caller
        # enters World cognition; SQLite/World CAS remains the cross-process
        # effect-once authority.
        self._ingress_batch_tasks: dict[
            str, asyncio.Task[QQC2CIngressResult]
        ] = {}
        self._lock = asyncio.Lock()
        # Serialize passive scheduler passes with one another.  A visible
        # turn's *targeted* Action deliberately does not take this mutex:
        # background cognition may be waiting on an unbounded model call, and
        # ActionPump's durable claim/CAS is already the authority when a
        # targeted pump races a passive recovery pump.
        self._scheduled_work_lock = asyncio.Lock()
        # Provider handoffs are serialized separately from model-backed
        # scheduler work.  This protects one process from running overlapping
        # ActionPump effects while still allowing a visible, exact-target
        # reply to overtake unrelated slow background cognition.
        self._action_pump_lock = asyncio.Lock()
        # Once ActionPump has durably crossed ActionDispatchStarted, cancelling
        # the caller's wait must not cancel the provider attempt. Keep those
        # handoffs process-owned until their normal receipt/recovery path ends.
        self._owned_action_drains: set[asyncio.Task[object]] = set()
        self._owned_action_close_grace_seconds = owned_action_close_grace_seconds
        # Generic recovery has no caller-owned Action id.  Every local source
        # of that work (due timer, explicit drain, scheduler) therefore joins
        # one process-owned task instead of queuing equivalent ActionPump
        # passes behind the provider mutex.
        self._scheduled_action_drain_task: asyncio.Task[object] | None = None
        # ``_lock`` also serializes short scheduler clock commits.  Keep a
        # separate signal for a genuinely visible ingress turn so scheduler
        # CAS cannot masquerade as evidence that the user is continuing a
        # volley and trigger the 650ms continuation hold.
        self._visible_turn_depth = 0
        self._provider_reconciliation_gate = _VisibleTurnReconciliationGate()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._deferred_semantic_close_task: asyncio.Task[None] | None = None
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
        # The durable matrix wait precedes the adaptive rhythm hold. A second
        # bubble observed inside that first window is already real cadence
        # evidence and must seed the rolling hold once the window closes.
        self._coalescing_waits = 0
        action_due_projection = getattr(self._host, "action_due_projection", None)
        self._action_due_wake = (
            ActionDueWake(
                project=action_due_projection,
                wake=self._wake_due_actions,
                now=self._action_due_now,
                sleep=self._action_due_sleep,
            )
            if callable(action_due_projection)
            else None
        )
        if self._action_due_wake is not None:
            try:
                asyncio.get_running_loop().create_task(
                    self._action_due_wake.refresh(),
                    name="qq-c2c-action-due-wake:restart-rebuild",
                )
            except RuntimeError:
                # Synchronous construction is supported by tests/CLI setup;
                # the first ingress or scheduler pass performs the rebuild.
                pass

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("QQ C2C host is closing")

    def _start_owned_action_drain(self, action_id: str) -> asyncio.Task[object]:
        self._require_open()
        task = asyncio.create_task(
            self._drain_owned_action(action_id),
            name=f"qq-c2c-owned-action-drain:{action_id}",
        )
        return self._track_owned_action_drain(task, action_ref=action_id)

    def _start_owned_scheduled_action_drain(self) -> asyncio.Task[object]:
        self._require_open()
        current = self._scheduled_action_drain_task
        if current is not None and not current.done():
            return current
        task = asyncio.create_task(
            self._drain_scheduled_action_once_unowned(),
            name="qq-c2c-owned-action-drain:scheduled",
        )
        self._scheduled_action_drain_task = task
        tracked = self._track_owned_action_drain(task, action_ref="<scheduled>")

        def clear_single_flight(completed: asyncio.Task[object]) -> None:
            if self._scheduled_action_drain_task is completed:
                self._scheduled_action_drain_task = None

        tracked.add_done_callback(clear_single_flight)
        return tracked

    def _track_owned_action_drain(
        self,
        task: asyncio.Task[object],
        *,
        action_ref: str,
    ) -> asyncio.Task[object]:
        self._owned_action_drains.add(task)

        def finished(completed: asyncio.Task[object]) -> None:
            self._owned_action_drains.discard(completed)
            if completed.cancelled():
                _LOG.error(
                    "world v2 owned Action drain was cancelled action_id=%s",
                    action_ref,
                )
                return
            error = completed.exception()
            if error is not None:
                _LOG.error(
                    "world v2 owned Action drain failed action_id=%s error=%s",
                    action_ref,
                    type(error).__name__,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finished)
        return task

    async def _drain_owned_action(self, action_id: str) -> object:
        # Never queue a user-owned provider handoff behind unrelated
        # model-backed scheduler work.  The exact Action id narrows selection;
        # ActionPump atomically records ``ActionDispatchStarted`` before the
        # provider call, so a cross-process generic pump can only lose the
        # ledger CAS or observe the finite in-flight lease.  The narrow local
        # mutex only serializes provider handoffs, not background cognition.
        async with self._action_pump_lock:
            return await self._host.drain_action(action_id)

    async def _drain_scheduled_action_once(self) -> object:
        """Run one passive ActionPump unit without locking background work."""

        task = self._start_owned_scheduled_action_drain()
        result = await asyncio.shield(task)
        settled_visible_dispatch = (
            getattr(result, "status", None) == "settled"
            and getattr(result, "action_kind", None)
            in USER_VISIBLE_PLATFORM_ACTION_KINDS
        )
        if (
            settled_visible_dispatch
            and getattr(result, "provider_status", None) == "provider_accepted"
        ):
            record_dispatch_ack()
        if (
            settled_visible_dispatch
            and getattr(result, "provider_status", None) == "delivered"
        ):
            # A later provider verification (for example NapCat get_msg)
            # supplies the strong evidence absent from the original dispatch
            # ACK.  It can count as visible, but cannot manufacture a
            # user-perceived latency timestamp after the ingress observation
            # clock was intentionally released.
            record_visible_reply()
        return result

    async def _drain_scheduled_action_once_unowned(self) -> object:
        async with self._action_pump_lock:
            for retry_ordinal in range(
                len(_ACTION_PUMP_CONFLICT_BACKOFF_SECONDS) + 1
            ):
                try:
                    gated = getattr(
                        self._host,
                        "drain_actions_once_gated",
                        None,
                    )
                    if callable(gated):
                        return await gated(
                            provider_accepted_reconciliation_gate=(
                                self._provider_reconciliation_gate
                            )
                        )
                    return await self._host.drain_actions_once()
                except ConcurrencyConflict:
                    if retry_ordinal == len(_ACTION_PUMP_CONFLICT_BACKOFF_SECONDS):
                        raise
                    await asyncio.sleep(
                        _ACTION_PUMP_CONFLICT_BACKOFF_SECONDS[retry_ordinal]
                    )
            raise AssertionError("scheduled ActionPump CAS retry did not terminate")

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

        if self._closed:
            raise RuntimeError("QQ C2C host is closing")
        if fragment.recipient_id != self._recipient_id:
            raise ValueError("QQ C2C recipient is not configured for this World v2 host")
        if self._ingress_store is None:
            raise RuntimeError("QQ C2C ingress store is not configured")
        arrival_ns = self._observation_clock_ns()
        received_at = self._ingress_now()
        # A message landing while her visible turn is still in context/model
        # work cannot be an answer to that turn's reply (she has not spoken yet) —
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
        self._arrival_ns_by_source_event_id.setdefault(
            fragment.source_event_id,
            arrival_ns,
        )
        self._register_content_gap(
            received_at=received_at,
            previous_received_at=previous_received_at,
            continuation_observed=(
                burst_continuation
                or self._rhythm_holds > 0
                or self._coalescing_waits > 0
            ),
        )
        self._last_content_received_at = received_at
        self._last_content_text = fragment.text
        delay = max(0.0, (submitted.due_at - self._ingress_now()).total_seconds())
        if delay:
            self._coalescing_waits += 1
            try:
                await self._ingress_sleep(delay)
            finally:
                self._coalescing_waits -= 1
        # A sibling may have landed at the edge of the short observation
        # window.  Yield once before testing quietness so that already-ready
        # ingress tasks persist their fragment before this task can claim.
        # This adds no timer and no production sleep.
        await asyncio.sleep(0)
        await self._hold_for_sender_rhythm(
            fragment=fragment,
            received_at=received_at,
            burst_continuation=burst_continuation,
        )
        for _ in range(8):
            # The store's short claim transaction is the same-burst join seam.
            # Do not wait for an older visible turn here: after a batch has
            # entered its provider phase, a newer due batch is an interjection
            # and must become a World Observation while that provider is still
            # in flight. Durable World CAS, not a process-wide conversation
            # mutex, decides which candidate may later authorize effects.
            batch = None
            already = self._ingress_store.submission(fragment.source_event_id)
            if already is None or already.state != "committed":
                async with self._ingress_lock:
                    batch = self._ingress_store.claim_due(
                        now=self._ingress_now(),
                        source_event_id=fragment.source_event_id,
                    )
                if batch is not None:
                    await self._run_visible_ingress_batch_once(batch)
            # Else a sibling's claim already answered this fragment. It must
            # not claim further pending work, which belongs to a newer volley.
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

    # Provider-local sender-rhythm pacing delays only the *claim*; it never
    # changes batch identity, ledger state, or replay.  Its adaptive hint is
    # deliberately bounded inside the durable 150–500ms coalescing budget:
    # message shape may choose when inside that opportunity to claim, but may
    # never add a second, multi-second wait before the model call.
    _TEMPO_WINDOW_SECONDS = 600.0
    _TEMPO_SAMPLE_CEILING_SECONDS = 8.0
    # The durable coalescing matrix already absorbs 150–500ms of adjacent
    # bubbles.  Sender-rhythm courtesy must fit inside that same user-visible
    # budget instead of adding several more seconds before any model call.
    _DEFAULT_QUIET_GAP_SECONDS = 0.15
    _MIN_QUIET_GAP_SECONDS = 0.10
    _MAX_QUIET_GAP_SECONDS = 0.42
    # Only observed continuation earns the wider rolling window.  This keeps
    # a single bubble fast while retaining multi-bubble turns at real typing
    # cadences; the wider value is never charged speculatively.
    _BURST_MAX_QUIET_GAP_SECONDS = 0.8
    _BURST_CONTINUATION_QUIET_GAP_SECONDS = 0.65
    # Absolute per-fragment bound on burst absorption.  A person being
    # flooded keeps reading as long as bubbles keep landing, but after about
    # half a minute they interject anyway — so the hold keeps rolling while
    # the volley continues and answers what has arrived once this cap hits.
    _BURST_HOLD_CAP_SECONDS = 30.0

    def _register_content_gap(
        self,
        *,
        received_at: datetime,
        previous_received_at: datetime | None,
        continuation_observed: bool,
    ) -> None:
        """Track the sender's live typing cadence for adaptive pacing."""

        if previous_received_at is None:
            return
        if not continuation_observed:
            # A message arriving after the previous visible turn settled is a
            # new exchange, however small the wall-clock gap happens to be.
            # Treating that reply-to-reply interval as typing cadence made
            # each successful fast turn enlarge the *next* speculative wait
            # from the catalog window to 420ms. Only a bubble witnessed while
            # another bubble is being held or answered is evidence of one volley.
            self._recent_gap_seconds.clear()
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
        """One bounded composure gap derived only from observed sender cadence.

        ``burst`` marks a message that provably continues an ongoing volley
        (it landed during her turn, or during another bubble's hold).  The
        latest observed subsecond gap can then move the claim toward the back
        of the same opportunity.  A slower gap is a lull, not live typing
        cadence, so it cannot add latency. Message wording and punctuation are
        deliberately opaque here: deciding whether a thought is complete is
        semantic character work, not a provider-host timing rule.
        """

        del text
        if self._recent_gap_seconds:
            ordered = sorted(self._recent_gap_seconds)
            median = ordered[len(ordered) // 2]
            base = min(max(median * 1.3, self._MIN_QUIET_GAP_SECONDS), 0.7)
        else:
            base = self._DEFAULT_QUIET_GAP_SECONDS
        gap = min(max(base, self._MIN_QUIET_GAP_SECONDS), self._MAX_QUIET_GAP_SECONDS)
        if burst and self._recent_gap_seconds:
            cadence = self._recent_gap_seconds[-1]
            if cadence <= self._BURST_MAX_QUIET_GAP_SECONDS:
                gap = max(
                    gap,
                    min(cadence * 1.2, self._BURST_MAX_QUIET_GAP_SECONDS),
                )
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

        While rapid bubbles keep landing the hold keeps rolling: each newer
        bubble re-sizes the remaining subsecond wait from *its* shape and live
        cadence.  The absolute ``_BURST_HOLD_CAP_SECONDS`` remains only as a
        safety bound for a genuinely continuous stream or endless provider
        "typing…" pulses; it is never a post-input delay.
        """

        quiet_gap = self._quiet_gap_seconds(fragment.text, burst=burst_continuation)
        if burst_continuation:
            quiet_gap = max(
                quiet_gap, self._BURST_CONTINUATION_QUIET_GAP_SECONDS
            )
        hard_cap = received_at + timedelta(seconds=self._BURST_HOLD_CAP_SECONDS)
        yielded_at_quiet_edge = False
        self._rhythm_holds += 1
        try:
            while True:
                now = self._ingress_now()
                latest = self._last_content_received_at or received_at
                if latest > received_at:
                    # A newer bubble landed during this hold, so the volley is
                    # still going: let the newest bubble's shape and the
                    # just-measured cadence decide how much longer to wait.
                    quiet_gap = self._quiet_gap_seconds(self._last_content_text, burst=True)
                # A provider "peer is typing" pulse counts as not-quiet: she
                # can see the person still composing, so she keeps waiting
                # (within the same absolute cap) instead of answering half a
                # thought.
                typing_at = self._last_typing_started_at
                if typing_at is not None and typing_at > latest:
                    latest = typing_at
                quiet_for = (now - latest).total_seconds()
                if quiet_for >= quiet_gap or now >= hard_cap:
                    if not yielded_at_quiet_edge and now < hard_cap:
                        # Let a fragment whose provider callback became ready
                        # on this exact boundary persist before freezing batch
                        # membership. This is a scheduler yield, not another
                        # pacing timer.
                        yielded_at_quiet_edge = True
                        await asyncio.sleep(0)
                        continue
                    return
                await self._ingress_sleep(
                    min(
                        # The old 50ms floor routinely charged one whole
                        # scheduler quantum when the durable observation
                        # window ended a fraction of a millisecond before the
                        # adaptive quiet edge.  Five milliseconds is enough
                        # to avoid a busy loop without turning rounding error
                        # into visible reply latency.
                        max(quiet_gap - quiet_for, 0.005),
                        max((hard_cap - now).total_seconds(), 0.005),
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
        """Report whether user-visible context/model/reply work is in flight.

        A dedicated depth marker covers context, model call, and the reply's
        ledger record. Scheduler clock commits are not evidence of user
        continuation, so lock state cannot supply this signal. Background work
        consults it between durable units so a waiting reply is never starved
        by a long chain of commits. Ledger CAS and durable claims remain the
        correctness authority.
        """

        return self._visible_turn_depth > 0

    async def drain_ingress_once(self) -> QQC2CIngressResult | None:
        """Resume one due or previously claimed batch after a restart."""

        self._require_open()
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
        async with self._ingress_lock:
            batch = self._ingress_store.claim_due(now=self._ingress_now())
        if batch is None:
            return None
        return await self._run_visible_ingress_batch_once(batch)

    async def _run_visible_ingress_batch_once(
        self,
        batch: QQIngressBatch,
    ) -> QQC2CIngressResult:
        """Join one process-local execution of an exact durable batch."""

        async with self._ingress_lock:
            task = self._ingress_batch_tasks.get(batch.batch_id)
            if task is None:
                task = asyncio.create_task(
                    self._run_visible_ingress_batch(batch),
                    name=f"qq-c2c-ingress-batch:{batch.batch_id}",
                )
                self._ingress_batch_tasks[batch.batch_id] = task
                task.add_done_callback(
                    lambda completed, batch_id=batch.batch_id: (
                        self._finish_ingress_batch_task(batch_id, completed)
                    )
                )
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                async with self._ingress_lock:
                    if self._ingress_batch_tasks.get(batch.batch_id) is task:
                        self._ingress_batch_tasks.pop(batch.batch_id, None)

    def _finish_ingress_batch_task(
        self,
        batch_id: str,
        task: asyncio.Task[QQC2CIngressResult],
    ) -> None:
        """Retire one owned batch even when its last shielded waiter left early."""

        # Task callbacks and registry mutation run on this host's event loop.
        # Comparing identity prevents a late callback from deleting a newer
        # recovery attempt for the same durable batch.
        if self._ingress_batch_tasks.get(batch_id) is task:
            self._ingress_batch_tasks.pop(batch_id, None)
        if not task.cancelled():
            # A cancelled facade waiter no longer observes the shielded
            # process-owned task. Retrieve any terminal exception here so it
            # cannot surface later as an unhandled Task warning.
            task.exception()

    async def _run_visible_ingress_batch(self, batch: QQIngressBatch) -> QQC2CIngressResult:
        """Mark only context/model/reply work as a user-visible turn."""

        try:
            async with self._provider_reconciliation_gate.visible_turn():
                self._visible_turn_depth += 1
                try:
                    return await self._process_ingress_batch(batch)
                finally:
                    self._visible_turn_depth -= 1
        finally:
            # A due-wake may have completed a read-only positive lookup and
            # deferred only its terminal commit.  Rebuild from the ledger
            # immediately after the last visible reader exits; do not wait
            # for the ten-minute heartbeat or another user message.
            if self._action_due_wake is not None and not self._closed:
                await self._action_due_wake.refresh()

    async def _process_ingress_batch(self, batch: QQIngressBatch) -> QQC2CIngressResult:
        """Run one claimed batch without serializing another provider phase."""

        observed_started_ns = min(
            (
                started_ns
                for source_event_id in batch.source_event_ids
                if (
                    started_ns := self._arrival_ns_by_source_event_id.get(
                        source_event_id
                    )
                )
                is not None
            ),
            default=None,
        )
        if self._idle_heartbeat_seconds > 0:
            logical_from = await self._host.current_logical_time()
            if logical_from is not None and batch.observed_at > logical_from:
                tick_id = "tick:qq-c2c-inbound:" + batch.batch_id
                tick_outcome = await self._host.tick(
                    PlatformClockTick(
                        tick_id=tick_id,
                        logical_time_from=logical_from,
                        logical_time_to=batch.observed_at,
                        observed_at=batch.observed_at,
                        trace_id=f"trace:qq-c2c-v2:{tick_id}",
                        causation_id=f"ingress:qq-c2c-v2:{batch.batch_id}",
                        correlation_id=f"clock:qq-c2c-v2:{self._recipient_id}",
                        reason="qq_c2c_inbound",
                        run_life_ecology=False,
                    )
                )
                if tick_outcome.status not in {"observed_only", "deferred"}:
                    raise RuntimeError("QQ C2C inbound clock was not accepted")
        metadata = dict(batch.metadata)
        # New stores freeze the first claim instant in the durable batch. Old
        # claimed rows fall back to their already-persisted window close, which
        # is conservative for latency but, critically, stable across recovery.
        metadata.setdefault("processing_started_at", metadata.get("window_closed_at"))
        processing_started_at = _parse_metadata_time(metadata.get("processing_started_at"))
        turn_budget = (
            self._interactive_turn_budget_policy.start(
                processing_started_at=processing_started_at,
                ingress_started_at=_parse_metadata_time(
                    metadata.get("window_opened_at")
                ),
            )
            if self._interactive_turn_budget_policy is not None
            else None
        )
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
        action_ids = tuple(
            dict.fromkeys(
                (*outcome.authorized_action_ids, *outcome.scheduled_action_ids)
            )
        )
        action_id = next(iter(action_ids), None)
        if action_id is not None:
            dispatch_ack_recorded = False
            visible_reply_recorded = False
            first_visible_reply_ns: int | None = None
            for candidate_action_id in action_ids:
                drain_task = self._start_owned_action_drain(candidate_action_id)
                dispatch_seconds = (
                    turn_budget.remaining(include_reserve=True)
                    if turn_budget is not None
                    else None
                )
                if (
                    dispatch_seconds is not None
                    and turn_budget is not None
                    and dispatch_seconds
                    < turn_budget.acceptance_dispatch_reserve_seconds
                ):
                    # Cognition may consume the absolute turn deadline, but an
                    # already-authorized visible reply must still get one
                    # bounded provider attempt in this user-owned lane.
                    dispatch_seconds = (
                        turn_budget.acceptance_dispatch_reserve_seconds
                    )
                    _LOG.warning(
                        "world v2 dispatch grace trace=%s "
                        "reason=turn_budget_exhausted grace_seconds=%.3f",
                        inbound.trace_id,
                        dispatch_seconds,
                    )
                if dispatch_seconds is None:
                    result = await asyncio.shield(drain_task)
                else:
                    try:
                        async with asyncio.timeout(dispatch_seconds):
                            result = await asyncio.shield(drain_task)
                    except TimeoutError:
                        # Stop waiting for the visible request, but do not
                        # cancel the host-owned provider handoff. If it already
                        # crossed ActionDispatchStarted, cancellation here
                        # would manufacture a two-minute ambiguity and make a
                        # later user message appear to resurrect old prose.
                        result = None
                        _LOG.warning(
                            "world v2 dispatch deferred trace=%s "
                            "reason=turn_budget_exhausted action_id=%s",
                            inbound.trace_id,
                            candidate_action_id,
                        )
                if (
                    result is not None
                    and result.action_id not in {None, candidate_action_id}
                ):
                    raise RuntimeError(
                        "targeted QQ C2C drain returned a different Action"
                    )
                settled_user_visible_dispatch = (
                    outcome.status == "action_authorized"
                    and result is not None
                    and result.status == "settled"
                    and result.action_kind in USER_VISIBLE_PLATFORM_ACTION_KINDS
                    and result.provider_status in {
                        "provider_accepted",
                        "delivered",
                    }
                )
                if (
                    not dispatch_ack_recorded
                    and settled_user_visible_dispatch
                    and result.provider_status == "provider_accepted"
                ):
                    # A provider ACK proves only that the dispatch crossed the
                    # provider boundary.  It is useful reliability evidence,
                    # but cannot support a user-perceived latency claim.
                    record_dispatch_ack()
                    dispatch_ack_recorded = True
                if (
                    not visible_reply_recorded
                    and settled_user_visible_dispatch
                    and result.provider_status == "delivered"
                ):
                    # Count at most one visible reply per inbound plan even
                    # when the model chose typing plus several text beats.
                    record_visible_reply()
                    visible_reply_recorded = True
                    first_visible_reply_ns = self._observation_clock_ns()
            # Process-observed first fragment arrival to the first positively
            # accepted visible reply. The scheduler/pacing clock is excluded:
            # it may be virtual in an isolated audit and wall-clock recovery
            # metadata cannot share a monotonic epoch.
            if first_visible_reply_ns is not None and observed_started_ns is not None:
                _LOG.warning(
                    "world v2 user_perceived trace=%s "
                    "user_perceived_reply_ms=%.1f status=%s "
                    "measurement_clock=monotonic",
                    inbound.trace_id,
                    max(
                        0.0,
                        (first_visible_reply_ns - observed_started_ns) / 1_000_000,
                    ),
                    outcome.status,
                )
        self._ingress_store.complete(
            batch_id=batch.batch_id,
            outcome_status=outcome.status,
            action_id=action_id,
        )
        for source_event_id in batch.source_event_ids:
            self._arrival_ns_by_source_event_id.pop(source_event_id, None)
        return QQC2CIngressResult(
            status=outcome.status,
            action_id=action_id,
            canonical_user_id=self._canonical_user_id,
        )

    async def _wake_due_actions(self) -> None:
        """Advance only clock + ActionPump; never background cognition."""

        if self._closed:
            return
        # Read the durable due target before taking the short clock mutex, then
        # release that mutex before entering the scheduler/ActionPump lane.
        # Visible ingress no longer holds ``_lock`` across provider work, and
        # the ledger projection, clock CAS, Action claim and provider
        # idempotency remain the authority across this lock-free phase boundary.
        projection = self._host.action_due_projection()
        if isinstance(projection, Awaitable):
            projection = await projection
        due_at = ActionDueWake.nearest_due(projection)
        wall_now = self._action_due_now()
        if due_at is None or due_at > wall_now:
            # The timer that entered this callback may have been armed for an
            # earlier Action which another scheduler/process already handled.
            # Never reinterpret that stale wake as authority to jump the World
            # Clock to a newly discovered future due; ActionDueWake refreshes
            # the projection after this callback and arms the replacement.
            return
        # A provider-accepted lease boundary exists only to permit terminal
        # verification/reconciliation of an old external effect.  Its
        # ClockAdvanced is therefore part of that same recovery tail: writing
        # it under a cursor-pinned visible turn would invalidate otherwise
        # valid prose before the terminal receipt gate below gets a chance to
        # defer.  Exclude visible pins around this clock write only when every
        # currently due target is provider_accepted.  Authorized/scheduled
        # exact deadlines (and other recovery states) retain their immediate
        # clock/dispatch path.
        provider_only_clock = self._provider_accepted_only_due(
            projection,
            through=wall_now,
        )
        if provider_only_clock:
            await self._provider_reconciliation_gate.acquire_reconciliation()
            if self._closed:
                await self._provider_reconciliation_gate.release_reconciliation()
                return
        observed_at = due_at
        tick_id = "tick:qq-c2c-action-due:" + observed_at.isoformat()
        # The clock commit needs only the short visible-world mutex.  Release
        # it before waiting for the ActionPump lane so there is no nested lock
        # order in either direction.
        try:
            async with self._lock:
                logical_from = await self._host.current_logical_time()
                if logical_from is not None and observed_at > logical_from:
                    await self._host.tick(
                        PlatformClockTick(
                            tick_id=tick_id,
                            logical_time_from=logical_from,
                            logical_time_to=observed_at,
                            observed_at=observed_at,
                            trace_id=f"trace:qq-c2c-v2:tick:{tick_id}",
                            causation_id=f"scheduler:qq-c2c-v2:{tick_id}",
                            correlation_id=f"clock:qq-c2c-v2:{self._recipient_id}",
                            reason="qq_c2c_action_due_wake",
                            run_life_ecology=False,
                        )
                    )
        finally:
            if provider_only_clock:
                await self._provider_reconciliation_gate.release_reconciliation()
        # Exact Action deadlines are independent of background/model work.
        # ``_action_pump_lock`` serializes provider effects with every other
        # local pump, while the durable Action claim/CAS remains authoritative
        # across processes.  Taking ``_scheduled_work_lock`` here would let one
        # unrelated unbounded model call delay an already-due external effect.
        for _ in range(8):
            result = await self._drain_scheduled_action_once()
            if getattr(result, "status", None) == "deferred_visible_turn":
                # Release the ActionPump/provider mutex before waiting.  The
                # visible turn's targeted dispatch must be free to overtake
                # this old terminal receipt.  Once the last visible reader
                # exits, retry in this same bounded wake so reconciliation is
                # immediate even when a deterministic test wall clock has not
                # advanced far enough to arm another process-local timer.
                await self._provider_reconciliation_gate.wait_for_visible_turns()
                continue
            if result is None or getattr(result, "status", None) in {
                "idle",
                "not_due",
            }:
                break

    @staticmethod
    def _provider_accepted_only_due(projection: object, *, through: datetime) -> bool:
        """Whether all due clock targets are old acknowledged deliveries."""

        saw_provider_accepted = False
        for action in getattr(projection, "actions", ()):
            state = getattr(action, "state", None)
            due = None
            if state in {"authorized", "scheduled"}:
                due = getattr(action, "not_before", None)
            elif state in {"claimed", "dispatch_started", "provider_accepted"}:
                lease = getattr(action, "claim_lease", None)
                due = getattr(lease, "expires_at", None)
            if not isinstance(due, datetime) or due > through:
                continue
            if state != "provider_accepted":
                return False
            saw_provider_accepted = True
        return saw_provider_accepted

    async def tick(
        self,
        *,
        tick_id: str,
        logical_time_from: datetime,
        logical_time_to: datetime,
        observed_at: datetime,
        reason: str,
        run_life_ecology: bool = True,
    ) -> str:
        """Advance a caller-owned durable scheduler interval through the v2 host."""

        self._require_open()
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
                    run_life_ecology=run_life_ecology,
                )
            )
            return outcome.status

    async def drain(
        self, *, max_action_units: int = 8, max_background_units: int = 8
    ) -> QQC2CDrainResult:
        """Run restart-safe Action recovery and bounded background work once."""

        self._require_open()
        if not 0 <= max_action_units <= 64 or not 0 <= max_background_units <= 64:
            raise ValueError("QQ C2C drain limits must be between 0 and 64")
        async with self._scheduled_work_lock:
            result = await self._drain_serialized(
                max_action_units=max_action_units,
                max_background_units=max_background_units,
            )
        # Background work may have authorized a future Action. Rebuild the
        # process-local timer only after releasing the scheduler mutex; timer
        # projection reads and due callbacks must never join the lock graph of
        # model-backed work.
        if self._action_due_wake is not None:
            await self._action_due_wake.refresh()
        return result

    async def _drain_serialized(
        self, *, max_action_units: int, max_background_units: int
    ) -> QQC2CDrainResult:
        """Run one maintenance drain under the shared scheduler mutex."""

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
            action_pump_once=self._drain_scheduled_action_once,
        )
        return QQC2CDrainResult(
            action_statuses=drained.action_statuses,
            background_statuses=drained.background_statuses,
        )

    def latency_samples(self) -> tuple[ProductionLatencySample, ...]:
        """Expose read-only process evidence for diagnostics and acceptance runs."""

        return self._host.latency_samples()

    def action_due_wake_diagnostics(self) -> dict[str, float | int | None]:
        if self._action_due_wake is None:
            return {
                "wake_count": 0,
                "wake_latency_ms_p50": None,
                "wake_latency_ms_p95": None,
                "failure_count": 0,
                "permanent_failure_count": 0,
            }
        return self._action_due_wake.diagnostics()

    async def scheduler_once(
        self,
        *,
        observed_at: datetime,
        max_action_units: int = 8,
        max_background_units: int = 8,
    ) -> QQC2CDrainResult:
        self._require_open()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("QQ C2C scheduler time must be timezone-aware")
        if not 0 <= max_action_units <= 64 or not 0 <= max_background_units <= 64:
            raise ValueError("QQ C2C scheduler drain limits must be between 0 and 64")
        scheduler_started_at = self._ingress_now()
        # A recovered ingress may authorize a targeted Action. Finish that
        # visible phase before entering the scheduler's separately locked work
        # phases below. No scheduler phase may hold ``_scheduled_work_lock``
        # while waiting for ``_lock`` (or vice versa).
        for _ in range(8):
            if await self.drain_ingress_once() is None:
                break
        result = await self._scheduler_once_serialized(
            observed_at=observed_at,
            max_action_units=max_action_units,
            max_background_units=max_background_units,
            scheduler_started_at=scheduler_started_at,
        )
        # Initiative, Life and media background work can all authorize future
        # Actions. Re-arm from the final projection only after every scheduler
        # mutex has been released, so exact deadlines never depend on the next
        # periodic pass and refresh cannot participate in a lock cycle.
        if self._action_due_wake is not None:
            await self._action_due_wake.refresh()
        return result

    async def _scheduler_once_serialized(
        self,
        *,
        observed_at: datetime,
        max_action_units: int = 8,
        max_background_units: int = 8,
        scheduler_started_at: datetime,
    ) -> QQC2CDrainResult:
        """Continue clock and recovery through non-overlapping mutex phases.

        The ``from`` timestamp comes from the v2 application rather than a
        process-local variable, so a restart cannot invent a stale interval.
        """

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
        priority_action_ids: list[str] = []

        def remember_priority_actions(result: object) -> bool:
            candidates: list[str] = []
            action_id = getattr(result, "action_id", None)
            if isinstance(action_id, str) and action_id:
                candidates.append(action_id)
            authorized = getattr(result, "authorized_action_ids", ())
            if isinstance(authorized, (tuple, list)):
                candidates.extend(
                    item for item in authorized if isinstance(item, str) and item
                )
            for candidate in candidates:
                if candidate not in priority_action_ids:
                    priority_action_ids.append(candidate)
            return bool(candidates)

        due_projection_reader = getattr(self._host, "action_due_projection", None)
        retry_due_before_tick = None
        retry_logical_from = None
        # Slow/model-backed work is serialized only with other scheduled work.
        # This mutex is released before the short clock-CAS phase takes
        # ``_lock``. Any state used after that boundary is re-read by the
        # clock projection or ActionPump; only immutable refs and local budget
        # counters cross it.
        async with self._scheduled_work_lock:
            if callable(due_projection_reader):
                due_projection = await due_projection_reader()
                retry_due_before_tick = next_expression_retry_due(due_projection)
                retry_logical_from = getattr(due_projection, "logical_time", None)
            # Preserve one unit whenever an eligible retry lies ahead.  Slow
            # pre-tick work can move the effective wall-clock boundary across a
            # retry that was still in the future when this pass began.
            post_tick_retry_reserve = int(
                retry_logical_from is not None
                and retry_due_before_tick is not None
                and retry_logical_from < retry_due_before_tick
                and background_remaining > 0
            )
            for _ in range(background_remaining - post_tick_retry_reserve):
                # Do not begin another multi-second background unit while a
                # visible turn is in flight. The visible turn does not share a
                # provider lock with this lane; this is latency preemption only,
                # and durable claims simply wait for the next pass.
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
                if remember_priority_actions(result):
                    break

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
            advanced_exact_due = False
            reached_expression_retry = False
            for _ in range(_MAX_EXACT_DUE_ADVANCES):
                if logical_from is None:
                    break
                due_projection = (
                    await due_projection_reader()
                    if callable(due_projection_reader)
                    else None
                )
                action_due_target = (
                    _next_action_due_boundary(
                        due_projection,
                        after=logical_from,
                        through=tick_boundary,
                    )
                    if due_projection is not None
                    else None
                )
                nearest_expression_retry_due = (
                    next_expression_retry_due(due_projection)
                    if due_projection is not None
                    else None
                )
                expression_retry_target = (
                    nearest_expression_retry_due
                    if nearest_expression_retry_due is not None
                    and logical_from < nearest_expression_retry_due <= tick_boundary
                    else None
                )
                exact_due_targets = tuple(
                    value
                    for value in (action_due_target, expression_retry_target)
                    if value is not None
                )
                if not exact_due_targets:
                    break
                tick_target = min(exact_due_targets)
                tick_reason = (
                    "qq_c2c_action_due_wake"
                    if tick_target == action_due_target
                    else "qq_c2c_expression_retry_wake"
                )
                tick_id = "tick:qq-c2c-v2:" + tick_target.isoformat()
                outcome = await self._host.tick(
                    PlatformClockTick(
                        tick_id=tick_id,
                        logical_time_from=logical_from,
                        logical_time_to=tick_target,
                        observed_at=tick_boundary,
                        trace_id=f"trace:qq-c2c-v2:{tick_id}",
                        causation_id=f"scheduler:qq-c2c-v2:{tick_id}",
                        correlation_id=f"clock:qq-c2c-v2:{self._recipient_id}",
                        reason=tick_reason,
                        run_life_ecology=False,
                    )
                )
                if outcome.status not in {"observed_only", "deferred"}:
                    raise RuntimeError("QQ C2C scheduler clock was not accepted")
                remember_priority_actions(outcome)
                logical_from = tick_target
                advanced_exact_due = True
                if tick_target == expression_retry_target:
                    reached_expression_retry = True
                    break

            heartbeat_due = (
                logical_from is not None
                and not advanced_exact_due
                and tick_boundary > logical_from
                and (
                    self._idle_heartbeat_seconds == 0
                    or (tick_boundary - logical_from).total_seconds()
                    >= self._idle_heartbeat_seconds
                )
            )
            if heartbeat_due:
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
                        run_life_ecology=True,
                    )
                )
                if outcome.status not in {"observed_only", "deferred"}:
                    raise RuntimeError("QQ C2C scheduler clock was not accepted")
                remember_priority_actions(outcome)
        # Re-enter the scheduled-work lane only after releasing ``_lock``.
        # ``priority_action_ids`` are immutable hints from committed outcomes,
        # never cached Action state: each targeted and generic drain re-projects
        # the ledger and wins through the normal claim/CAS/effect-once path.
        async with self._scheduled_work_lock:
            held_retry_reserve = int(
                post_tick_retry_reserve
                and retry_due_before_tick is not None
                and retry_due_before_tick <= tick_boundary
                and not reached_expression_retry
            )
            # Give a time-sensitive, source-bound social-initiative
            # consideration one chance against the new clock before generic
            # Action recovery. This bounded preflight may advance only the
            # already-due model-owned consideration; Clock never supplies its
            # motive, prose, or send decision. Normal budgets and Action
            # recovery still run in ``drain_scheduled_work`` below.
            post_tick_background: list[str] = []
            post_tick_background_budget = max(0, background_remaining - held_retry_reserve)
            if post_tick_background_budget > 0 and not priority_action_ids:
                # Opening, deciding, and authorizing an initiative process are
                # separate durable steps. Continue only until that lane has
                # produced its Action (or the bounded background budget is
                # spent), then dispatch through the ordinary effect-once path.
                for _ in range(post_tick_background_budget):
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
                    if remember_priority_actions(result):
                        break
            post_tick_actions: list[str] = []
            for priority_action_id in priority_action_ids[:max_action_units]:
                targeted = await asyncio.shield(
                    self._start_owned_action_drain(priority_action_id)
                )
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
                max_background_units=max(0, background_remaining - held_retry_reserve),
                media_preview_trace_id="trace:qq-c2c-v2:media-preview",
                media_preview_correlation_id=(
                    f"correlation:qq-c2c-v2:media-preview:{self._recipient_id}"
                ),
                # A visible reply's record commit must not wait behind this
                # pass's remaining background commits.  Preemption only stops
                # starting new units; anything already claimed stays durable and
                # resumes on the next scheduler pass.
                should_preempt=self._visible_turn_in_flight,
                action_pump_once=self._drain_scheduled_action_once,
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

    def local_provider_capacity_health(self) -> dict[str, object]:
        """Expose the shared local-inference lease without touching the model."""

        capacity = (
            self._semantic_chat.local_provider_capacity
            if self._semantic_chat is not None
            else None
        )
        if capacity is None:
            return {"enabled": False, "status": "disabled"}
        return {"enabled": True, **capacity.health_snapshot()}

    def proactive_source_authority_health(self) -> dict[str, object]:
        """Expose whether proactive factual effects have independent review."""

        if self._semantic_chat is None:
            return {
                "status": "unavailable",
                "warning": True,
                "warning_reasons": ["proactive_source_authority.composition_unavailable"],
                "independent_reviewer": False,
                "fact_effects_available": False,
                "subjective_expression_available": False,
                "candidate_inventory_model": None,
                "requested_candidate_inventory_model": None,
                "inventory_capability_evidence": None,
                "inventory_runtime": {
                    "status": "unavailable",
                    "successful_calls": 0,
                    "failed_calls": 0,
                    "last_checked_at": None,
                    "last_failure_code": None,
                },
                "inventory_call_timeout_seconds": None,
                "visible_review_strategy": "unavailable",
                "candidate_review_capabilities": {
                    lane: {
                        "inventory_v5": False,
                        "coverage_v5": False,
                        "roles_independent": False,
                    }
                    for lane in ("ordinary", "recovery", "reselection")
                },
                "inventory_transport": {
                    "route_count": 0,
                    "routes": (),
                    "single_transport": False,
                    "provider_count": 0,
                    "single_provider": False,
                    "capability_evidence": [],
                    "attempt_timeout_seconds": None,
                    "secondary_reserved_seconds": None,
                },
            }
        return self._semantic_chat.proactive_source_authority_health()

    def export_replay_evidence(self) -> ReplayEvidence:
        """Expose immutable audit evidence without granting ledger mutation access."""

        return self._host.export_replay_evidence()

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
        close_task = self._close_task
        if close_task is None:
            # Close the task-creation gate before taking the owned-task
            # snapshot. Since no await separates these assignments, another
            # coroutine cannot enqueue a handoff between them.
            self._closed = True
            close_task = asyncio.create_task(
                self._aclose_owned(),
                name="qq-c2c-host-close",
            )
            self._close_task = close_task
        # Lifecycle callers own only their wait. Cancellation of one ASGI or
        # launchd shutdown waiter must not propagate through gather into a
        # provider handoff that already crossed ActionDispatchStarted.
        await asyncio.shield(close_task)

    async def _aclose_owned(self) -> None:
        if self._action_due_wake is not None:
            await self._action_due_wake.aclose()
        ingress = tuple(self._ingress_batch_tasks.values())
        if ingress:
            _done, pending_ingress = await asyncio.wait(
                ingress,
                timeout=self._owned_action_close_grace_seconds,
            )
            if pending_ingress:
                _LOG.warning(
                    "world v2 close grace elapsed; cancelling %d owned "
                    "ingress batch(es) for durable recovery",
                    len(pending_ingress),
                )
                for task in pending_ingress:
                    task.cancel()
                _cancelled, still_pending_ingress = await asyncio.wait(
                    pending_ingress,
                    timeout=_OWNED_ACTION_DRAIN_CANCEL_GRACE_SECONDS,
                )
                if still_pending_ingress:
                    _LOG.error(
                        "world v2 owned ingress batches ignored cancellation "
                        "during close count=%d",
                        len(still_pending_ingress),
                    )
        owned = tuple(self._owned_action_drains)
        if owned:
            _done, pending = await asyncio.wait(
                owned,
                timeout=self._owned_action_close_grace_seconds,
            )
            if pending:
                _LOG.warning(
                    "world v2 close grace elapsed; cancelling %d owned "
                    "Action drain(s) for durable recovery",
                    len(pending),
                )
                for task in pending:
                    task.cancel()
                _cancelled, still_pending = await asyncio.wait(
                    pending,
                    timeout=_OWNED_ACTION_DRAIN_CANCEL_GRACE_SECONDS,
                )
                if still_pending:
                    _LOG.error(
                        "world v2 owned Action drains ignored cancellation "
                        "during close count=%d",
                        len(still_pending),
                    )
        close_world = getattr(self._host, "aclose", None)
        if callable(close_world):
            await close_world()
        else:
            self._host.close()
        if self._ingress_store is not None:
            self._ingress_store.close()
        if self._semantic_chat is not None:
            world_quiescence = getattr(
                self._host,
                "wait_for_shutdown_quiescence",
                None,
            )
            if (
                getattr(self._host, "shutdown_pending_task_count", 0) > 0
                and callable(world_quiescence)
            ):
                deferred = asyncio.create_task(
                    self._close_semantic_after_world_quiescence(world_quiescence),
                    name="qq-c2c-deferred-semantic-close",
                )
                self._deferred_semantic_close_task = deferred
                deferred.add_done_callback(self._observe_deferred_semantic_close)
            else:
                await self._semantic_chat.aclose()

    async def _close_semantic_after_world_quiescence(
        self,
        wait_for_world: Callable[[], Awaitable[None]],
    ) -> None:
        await wait_for_world()
        assert self._semantic_chat is not None
        await self._semantic_chat.aclose()

    @staticmethod
    def _observe_deferred_semantic_close(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @property
    def shutdown_pending_task_count(self) -> int:
        """Whether detached World work still leases model dependencies."""

        deferred = self._deferred_semantic_close_task
        if deferred is not None and not deferred.done():
            return 1
        return int(
            getattr(self._host, "shutdown_pending_task_count", 0) > 0
            or (
                self._semantic_chat is not None
                and getattr(
                    self._semantic_chat,
                    "shutdown_pending_task_count",
                    0,
                )
                > 0
            )
        )

    async def wait_for_shutdown_quiescence(self) -> None:
        """Wait for deferred dependency cleanup without cancelling its owner."""

        deferred = self._deferred_semantic_close_task
        if deferred is not None:
            await asyncio.shield(deferred)
        else:
            world_quiescence = getattr(
                self._host,
                "wait_for_shutdown_quiescence",
                None,
            )
            if (
                getattr(self._host, "shutdown_pending_task_count", 0) > 0
                and callable(world_quiescence)
            ):
                await world_quiescence()
        semantic = self._semantic_chat
        semantic_quiescence = getattr(
            semantic,
            "wait_for_shutdown_quiescence",
            None,
        )
        if (
            semantic is not None
            and getattr(semantic, "shutdown_pending_task_count", 0) > 0
            and callable(semantic_quiescence)
        ):
            await semantic_quiescence()


def build_qq_c2c_host(
    *,
    settings: Settings,
    recipient_id: str,
    bootstrap_at: datetime | None = None,
    model: ChatCompletionModel | None = None,
    thinking_model: ChatCompletionModel | None = None,
    advisory_model: ChatCompletionModel | None = None,
    source_closure_model: ChatCompletionModel | None = None,
    candidate_external_proposition_inventory_model: ChatCompletionModel | None = None,
    delivery: QQC2CDelivery | None = None,
    media_transport: MediaProviderTransport | None = None,
    media_preview: MediaPreviewDeployment | None = None,
    perception_model: DeliberationModelAdapter | None = None,
    perception_input_source: PerceptionInputSource | None = None,
    perception_transport: PerceptionTransport | None = None,
    perception_budget_limit: int = 0,
    ingress_now: Callable[[], datetime] | None = None,
    ingress_sleep: Callable[[float], Awaitable[None]] | None = None,
    observation_clock_ns: Callable[[], int] = time.perf_counter_ns,
    action_due_now: Callable[[], datetime] | None = None,
    action_due_sleep: Callable[[float], Awaitable[None]] | None = None,
    semantic_recall_embedding: RecallEmbedding | None = None,
    use_configured_recall_embedding: bool = True,
    interactive_turn_budget_policy: InteractiveTurnBudgetPolicy | None = None,
    _test_only_expression_episode_mode: Literal["on"] | None = None,
) -> QQC2CHost:
    """Compose the C2C lane without importing legacy chat/runtime code.

    Media remains opt-in: a caller may provide only a transport that durably
    binds result bytes to idempotency keys and supports recovery lookup.  QQ
    delivery itself is deliberately text-only and is never used as an image
    provider fallback.

    ``ingress_now`` and ``ingress_sleep`` are the presentation-pacing seam that
    :class:`QQC2CHost` has always accepted but that no builder forwarded.  They
    exist so an offline harness can replay a conversation without waiting out
    the real sender-rhythm hold.  They affect only wall-clock pacing: logical
    time, ledger content, Action deadlines, and every world decision are
    unchanged. ``action_due_now``/``action_due_sleep`` are a separate paired
    scheduler seam for deterministic timer tests. The scheduler clock also
    timestamps provider receipts, keeping Action evidence ordered against
    that same wall-time authority. Production leaves them ``None`` and
    therefore keeps the real clock and sleep.
    """

    if not recipient_id:
        raise ValueError("QQ C2C v2 requires one configured private recipient")
    configured_expression_episode_mode = settings.world_v2_expression_episode_mode
    if configured_expression_episode_mode not in {"off", "shadow"}:
        raise ValueError("production QQ expression episode mode must be off or shadow")
    # The provisional Expression Episode remains useful for exercising its
    # dormant recovery lifecycle, but ADR 0014 forbids exposing that one-beat
    # candidate through production Settings.  Tests must opt in at this
    # conspicuous composition seam instead of smuggling ``on`` through env.
    expression_episode_mode: Literal["off", "shadow", "on"] = (
        _test_only_expression_episode_mode or configured_expression_episode_mode
    )
    expression_capabilities = qq_expression_capabilities(
        settings.qq_adapter,
        recorded_cadence_mode=getattr(settings, "world_v2_recorded_cadence_mode", "off"),
    )
    interactive_turn_budget_policy = (
        interactive_turn_budget_policy or InteractiveTurnBudgetPolicy()
    )
    semantic_chat = build_semantic_chat_composition(
        settings=settings,
        flash_model=model,
        thinking_model=thinking_model,
        advisory_model=advisory_model,
        source_closure_model=source_closure_model,
        candidate_external_proposition_inventory_model=(
            candidate_external_proposition_inventory_model
        ),
        model_id_prefix="qq-c2c-v2",
        expression_capabilities=expression_capabilities,
    )
    model = semantic_chat.flash_model
    background_model = semantic_chat.background_model
    life_world_author = RoleBoundLifeDevelopmentModelAdapter(
        model=background_model,
        role="world_author",
    )
    life_world_author_source_rewriter = RoleBoundLifeDevelopmentModelAdapter(
        # Source correction remains the same World Author semantic authority.
        # A source reviewer may identify exact failure coordinates, but it may
        # never become the author of the replacement life draft.
        model=background_model,
        role="world_author",
    )
    life_character = RoleBoundLifeDevelopmentModelAdapter(
        model=background_model,
        role="character_model",
    )
    life_source_closure_reviewer = (
        RoleBoundLifeDevelopmentModelAdapter(
            model=semantic_chat.proactive_source_closure_model,
            role="world_author_source_reviewer",
        )
        if semantic_chat.proactive_source_closure_model is not None
        else None
    )
    delivery = delivery or QQDelivery(settings)
    scheduler_now = action_due_now or _utc_now
    transport = QQC2CPlatformTransport(
        delivery=delivery,
        recipients_by_target={qq_c2c_target(recipient_id): recipient_id},
        now=scheduler_now,
    )
    resolved_recall_embedding = semantic_recall_embedding
    if resolved_recall_embedding is None and use_configured_recall_embedding:
        resolved_recall_embedding = configured_recall_embedding(settings)
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
            expression_capabilities=expression_capabilities,
            life_ecology=LifeEcologyComposition.production_v1(),
            # Visible QQ reactions must be ordinary role-model Expression
            # beats.  The retired local quick-reaction worker is still
            # available to offline/replay composition, but is not an option in
            # this production C2C builder.
            quick_reaction_enabled=False,
            media_selection_acceptance=(
                media_preview.acceptance if media_preview is not None else None
            ),
            media_continuation=(media_preview.continuation if media_preview is not None else None),
            media_auto_delivery=(
                media_preview.auto_delivery if media_preview is not None else None
            ),
            perception_budget_limit=perception_budget_limit,
            interactive_turn_budget_policy=interactive_turn_budget_policy,
            expression_episode_mode=expression_episode_mode,
            recorded_cadence_mode=getattr(settings, "world_v2_recorded_cadence_mode", "off"),
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
        private_impression_identity_frame=semantic_chat.identity_frame,
        proactive_model=background_model,
        proactive_identity_frame=semantic_chat.identity_frame,
        proactive_source_closure_model=semantic_chat.proactive_source_closure_model,
        proactive_candidate_external_proposition_inventory_model=(
            semantic_chat.candidate_external_proposition_inventory_model
        ),
        memory_model=background_model,
        activity_lifecycle_model=background_model,
        life_world_author_model=life_world_author,
        life_world_author_source_rewriter=life_world_author_source_rewriter,
        life_character_model=life_character,
        life_source_closure_reviewer=life_source_closure_reviewer,
        media_selection_model=(
            media_preview.selection_model if media_preview is not None else None
        ),
        semantic_recall_embedding=resolved_recall_embedding,
        now=bootstrap_at or datetime.now(UTC),
    )
    return QQC2CHost(
        host=WorldV2PlatformHost(application=application),
        recipient_id=recipient_id,
        canonical_user_id=settings.primary_user_id,
        semantic_chat=semantic_chat,
        ingress_store=SQLiteQQIngressStore(Path(settings.database_path)),
        ingress_now=ingress_now,
        ingress_sleep=ingress_sleep if ingress_sleep is not None else asyncio.sleep,
        observation_clock_ns=observation_clock_ns,
        action_due_now=scheduler_now,
        action_due_sleep=action_due_sleep,
        interactive_turn_budget_policy=interactive_turn_budget_policy,
        recorded_cadence_mode=getattr(settings, "world_v2_recorded_cadence_mode", "off"),
        idle_heartbeat_seconds=settings.qq_c2c_idle_heartbeat_seconds,
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
