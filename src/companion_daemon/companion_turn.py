"""Deep turn seam for model-led expression and authoritative delivery settlement.

Phase 1 deliberately delegates generation to ``CompanionEngine`` while moving
the observable text Action lifecycle behind one interface.  Later phases can
replace the generation implementation without changing platform callers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Literal, Mapping, Protocol, runtime_checkable

from companion_daemon.engine import CompanionEngine
from companion_daemon.models import IncomingMessage
from companion_daemon.platform_adapter import DeliveryReceipt
from companion_daemon.time import utc_now
from companion_daemon.world import WorldError


VisibleStatus = Literal["delivered", "accepted", "failed", "unknown"]
TerminalState = Literal["delivered", "failed", "cancelled", "unknown"]


@dataclass(frozen=True)
class TurnEnvelope:
    """One normalized, immutable platform observation.

    ``message`` remains the compatibility payload consumed by the Engine, but
    the adapter-facing boundary also freezes the identifiers and cadence that
    must not be rediscovered from mutable state midway through a turn.
    """

    message: IncomingMessage
    idempotency_key: str
    world_id: str | None = None
    canonical_user_id: str | None = None
    platform: str = ""
    platform_message_ids: tuple[str, ...] = ()
    attachment_refs: tuple[str, ...] = ()
    frozen_cadence: Literal["hot", "warm", "cold", "unknown"] = "unknown"

    @classmethod
    def from_message(
        cls,
        message: IncomingMessage,
        *,
        idempotency_key: str,
        world_id: str | None = None,
        canonical_user_id: str | None = None,
        frozen_cadence: Literal["hot", "warm", "cold", "unknown"] = "unknown",
    ) -> TurnEnvelope:
        raw_message_id = str(message.message_id or "").strip()
        if not raw_message_id:
            raise ValueError("TurnEnvelope requires a platform message_id")
        canonical_key = f"{message.platform}:{message.platform_user_id}:{raw_message_id}"
        if idempotency_key != canonical_key:
            raise ValueError("idempotency_key must bind platform, account, and platform message_id")
        normalized = message.model_copy(deep=True)
        normalized.message_id = canonical_key
        raw_ids = [raw_message_id, *(str(item).strip() for item in message.source_message_ids)]
        platform_message_ids = tuple(dict.fromkeys(item for item in raw_ids if item))
        attachment_refs = tuple(
            dict.fromkeys(
                ref
                for attachment in message.attachments
                for ref in (str(attachment.url or "").strip(), str(attachment.filename or "").strip())
                if ref
            )
        )
        return cls(
            message=normalized,
            idempotency_key=canonical_key,
            world_id=world_id,
            canonical_user_id=canonical_user_id,
            platform=message.platform,
            platform_message_ids=platform_message_ids,
            attachment_refs=attachment_refs,
            frozen_cadence=frozen_cadence,
        )

    @property
    def observed_at(self) -> datetime:
        return self.message.sent_at


@dataclass(frozen=True)
class ResponseBudget:
    first_visible_by_ms: int
    complete_by_ms: int

    def __post_init__(self) -> None:
        if self.first_visible_by_ms <= 0 or self.complete_by_ms <= 0:
            raise ValueError("response budgets must be positive")
        if self.first_visible_by_ms > self.complete_by_ms:
            raise ValueError("first-visible budget cannot exceed complete budget")


@dataclass(frozen=True)
class TurnOptions:
    """Turn-scoped generation context owned by the caller's observation seam."""

    context_hint: str | None = None
    turn_context: object | None = None


@dataclass(frozen=True)
class TurnBeat:
    action_id: str
    delivery_id: int
    segment_id: str
    position: int
    text: str
    platform: str
    canonical_user_id: str
    delay_before_ms: int = 0


@dataclass(frozen=True)
class DispatchAcceptance:
    status: Literal["delivered", "accepted", "failed", "unknown"]
    external_receipt: str | None = None
    lookup_token: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status == "delivered" and not (self.external_receipt or "").strip():
            raise ValueError("delivered dispatch requires an external_receipt")
        if self.status == "accepted" and not (self.lookup_token or "").strip():
            raise ValueError("accepted dispatch requires a lookup_token")


class TurnTransport(Protocol):
    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance: ...


class _SettlementOnlyTransport:
    """Construction aid for background results that cannot dispatch text."""

    async def dispatch(self, _beat: TurnBeat) -> DispatchAcceptance:
        raise RuntimeError("settlement-only turn cannot dispatch an outgoing segment")


@runtime_checkable
class ReceiptLookupTransport(Protocol):
    """Optional durable-receipt recovery capability of a Turn transport.

    Dispatch and receipt lookup intentionally share the transport instance: a
    persisted lookup token is meaningful only to the platform/account that
    accepted the beat.  Immediate-receipt transports do not need to implement
    this protocol.
    """

    async def lookup_delivery(self, receipt_query_token: str) -> DeliveryReceipt: ...


@dataclass(frozen=True)
class TurnPresentation:
    """Non-text expression effects associated with one authoritative text Action."""

    action_id: str
    incoming: IncomingMessage
    canonical_user_id: str
    suggested_reaction: str | None
    sticker_path: str | None
    image_path: str | None
    media_action_id: str | None
    sticker_action_id: str | None


class TurnPresenter(Protocol):
    async def before_text(self, presentation: TurnPresentation) -> None: ...

    async def after_text(
        self, presentation: TurnPresentation, terminal_state: TerminalState
    ) -> None: ...


@dataclass(frozen=True)
class TurnOutcome:
    turn_id: str
    committed_revision: int
    action_ids: tuple[str, ...]
    visible_status: VisibleStatus
    degraded: bool = False
    degradation_reason: str | None = None


@dataclass(frozen=True)
class ExternalObservation:
    """One idempotent result from the platform, a tool, media, or a timeout.

    The legacy segment fields remain optional so deployed platform adapters can
    keep reporting receipts without a flag day.  New non-text results use the
    canonical ``kind`` + ``payload`` envelope and are settled by the same turn
    seam into the authoritative World ledger.
    """

    action_id: str
    observed_at: datetime
    idempotency_key: str
    delivery_id: int | None = None
    segment_id: str | None = None
    status: TerminalState | None = None
    kind: Literal["platform_receipt", "tool_result", "media_result", "timeout"] = (
        "platform_receipt"
    )
    payload: Mapping[str, object] = field(default_factory=dict)
    world_id: str | None = None
    external_receipt: str | None = None
    reason: str | None = None
    # A normal adapter receipt has no reconciliation evidence: its transport
    # identity is already the authority.  This field exists for the narrow
    # crash-recovery case where an authenticated operator has checked an
    # otherwise-unrecoverable unknown dispatch.  It deliberately travels
    # through the same settlement seam and becomes part of the World event.
    reconciliation_evidence: Mapping[str, object] | None = None
    cancel_remaining: bool = False
    settlement_origin: Literal["adapter", "operator_reconciliation"] = "adapter"
    expected_revision: int | None = None


@dataclass(frozen=True)
class SettlementOutcome:
    action_id: str
    terminal_state: TerminalState | None
    committed_revision: int
    follow_up_action_ids: tuple[str, ...] = ()


class CompanionTurn:
    """Own one inbound turn from generation through Action settlement."""

    def __init__(
        self,
        engine: CompanionEngine,
        transport: TurnTransport,
        *,
        cadence_delay_seconds: float = 0.3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        presenter: TurnPresenter | None = None,
    ) -> None:
        if engine.world_kernel is None or engine.world_id is None:
            raise ValueError("CompanionTurn v2 requires the authoritative World runtime")
        self.engine = engine
        self.transport = transport
        self.cadence_delay_seconds = max(0.0, cadence_delay_seconds)
        self.sleep = sleep
        self.presenter = presenter
        self._continuations: set[asyncio.Task[None]] = set()
        self._delivery_continuations: set[asyncio.Task[None]] = set()
        self._action_locks: dict[str, asyncio.Lock] = {}
        self._watchdog_keys: set[tuple[str, str]] = set()
        self._presentations: dict[str, TurnPresentation] = {}
        self._recover_receipt_watchdogs()

    async def start(self) -> None:
        """Restore durable receipt watchdogs after the application's loop starts."""
        self._recover_receipt_watchdogs()
        await asyncio.sleep(0)

    async def wait_for_delivery_continuations(self) -> None:
        """Wait for in-process follow-up beats, never for long receipt watchdogs."""
        while self._delivery_continuations:
            await asyncio.gather(*tuple(self._delivery_continuations), return_exceptions=True)

    async def observe_expired(self, turn: TurnEnvelope) -> TurnOutcome:
        """Commit an inbound message after its response budget is already exhausted."""
        existing = self._existing_outcome(turn)
        if existing is not None:
            return existing
        await self.engine.handle_message(
            turn.message,
            skip_reply=True,
            fast_observe=True,
        )
        return self._outcome(
            turn,
            action_ids=(),
            status="failed",
            degraded=True,
            reason="response_budget_exhausted",
        )

    async def respond(
        self,
        turn: TurnEnvelope,
        *,
        budget: ResponseBudget,
        options: TurnOptions | None = None,
    ) -> TurnOutcome:
        """Respond within the first-visible deadline and converge timed-out work."""
        self._recover_receipt_watchdogs()
        try:
            return await self._respond_with_timeout(
                turn, budget=budget, options=options or TurnOptions()
            )
        except TimeoutError:
            return await self._converge_timeout(turn)

    async def _respond_with_timeout(
        self,
        turn: TurnEnvelope,
        *,
        budget: ResponseBudget,
        options: TurnOptions,
    ) -> TurnOutcome:
        self._validate_turn_envelope(turn)
        existing = self._existing_outcome(turn)
        if existing is not None:
            return existing
        await self._cancel_interrupted_actions(turn)
        complete_by_at = utc_now() + timedelta(milliseconds=budget.complete_by_ms)
        timeout_seconds = self._generation_timeout_seconds(budget)
        async with asyncio.timeout(timeout_seconds):
            reply = await self.engine.handle_message(
                turn.message,
                defer_delivery=True,
                complete_by_observed_at=complete_by_at,
                context_hint=options.context_hint,
                turn_context=options.turn_context,
            )
            if reply is None:
                return self._outcome(
                    turn,
                    action_ids=(),
                    status="failed",
                    degraded=True,
                    reason="no_reply_selected",
                )
            if reply.delivery_id is None or not reply.world_action_id:
                raise WorldError("world reply did not stage an outgoing Action")
            presentation = TurnPresentation(
                action_id=reply.world_action_id,
                incoming=turn.message,
                canonical_user_id=reply.canonical_user_id,
                suggested_reaction=reply.suggested_reaction,
                sticker_path=reply.sticker_path,
                image_path=reply.image_path,
                media_action_id=reply.media_action_id,
                sticker_action_id=reply.sticker_action_id,
            )
            self._presentations[reply.world_action_id] = presentation
            await self._present_before_text(presentation)

            async with self._action_locks.setdefault(reply.world_action_id, asyncio.Lock()):
                settled = await self._dispatch_next(
                    action_id=reply.world_action_id,
                    delivery_id=reply.delivery_id,
                    observed_at=turn.observed_at,
                    idempotency_prefix=turn.idempotency_key,
                    advance=False,
                )
            terminal_status = settled.terminal_state
            status: VisibleStatus = (
                "failed"
                if terminal_status == "cancelled"
                else terminal_status or self._visible_action_status(reply.world_action_id)
            )
            if status == "delivered" and self._has_planned_beat(reply.world_action_id):
                self._start_continuation(
                    action_id=reply.world_action_id,
                    delivery_id=reply.delivery_id,
                    observed_at=turn.observed_at,
                    idempotency_prefix=turn.idempotency_key,
                )
            elif status in {"delivered", "failed", "unknown"}:
                await self._present_after_text(reply.world_action_id, status)
            return self._outcome(
                turn,
                action_ids=(reply.world_action_id,),
                status=status,
                degraded=status in {"failed", "unknown"},
                reason=(
                    "outgoing_delivery_failed"
                    if status == "failed"
                    else "outgoing_delivery_unknown"
                    if status == "unknown"
                    else None
                ),
            )

    def _validate_turn_envelope(self, turn: TurnEnvelope) -> None:
        if turn.world_id is not None and turn.world_id != self.engine.world_id:
            raise WorldError("TurnEnvelope belongs to a different World")
        if turn.platform and turn.platform != turn.message.platform:
            raise WorldError("TurnEnvelope platform does not match its message")
        expected_user = self.engine.store.resolve_user(
            turn.message.platform, turn.message.platform_user_id
        )
        if turn.canonical_user_id is not None and turn.canonical_user_id != expected_user:
            raise WorldError("TurnEnvelope user does not match its platform account")

    async def _converge_timeout(self, turn: TurnEnvelope) -> TurnOutcome:
        """Close any staged Action after cancellation instead of leaving it open."""
        action_match = self._matching_action(turn)
        if action_match is None:
            return await self._dispatch_timeout_fallback(turn)
        action_id, action = action_match
        delivery_id = int(action.get("delivery_id") or 0)
        segment_state = action.get("segment_state", {})
        segments = segment_state.get("segments", []) if isinstance(segment_state, dict) else []
        sending = next(
            (
                item
                for item in segments
                if isinstance(item, dict) and item.get("status") == "sending"
            ),
            None,
        )
        world = self.engine.world_kernel
        assert world is not None
        if sending is not None:
            world.mark_outgoing_segment_unknown(
                delivery_id,
                str(sending.get("segment_id") or ""),
                reason="first_visible_timeout_after_dispatch_started",
                expected_revision=world.revision(self.engine.world_id or ""),
            )
            status: VisibleStatus = "unknown"
        else:
            world.settle_outgoing_action(
                delivery_id,
                delivered=False,
                reason="first_visible_timeout_before_dispatch",
            )
            status = "failed"
        return self._outcome(
            turn,
            action_ids=(action_id,),
            status=status,
            degraded=True,
            reason="first_visible_timeout",
        )

    async def _dispatch_timeout_fallback(self, turn: TurnEnvelope) -> TurnOutcome:
        """Send one fact-free beat when generation timed out before staging.

        The original model task has already been cancelled by the first-visible
        deadline.  This is deliberately not a second model call or an adapter
        side channel: it creates and settles the same authoritative outgoing
        Action as a normal turn.
        """
        try:
            reply = self.engine.prepare_first_visible_timeout_reply(turn.message)
            if reply.delivery_id is None or not reply.world_action_id:
                raise WorldError("timeout fallback did not stage an outgoing Action")
            presentation = TurnPresentation(
                action_id=reply.world_action_id,
                incoming=turn.message,
                canonical_user_id=reply.canonical_user_id,
                suggested_reaction=None,
                sticker_path=None,
                image_path=None,
                media_action_id=None,
                sticker_action_id=None,
            )
            self._presentations[reply.world_action_id] = presentation
            await self._present_before_text(presentation)
            async with self._action_locks.setdefault(reply.world_action_id, asyncio.Lock()):
                settled = await self._dispatch_next(
                    action_id=reply.world_action_id,
                    delivery_id=reply.delivery_id,
                    observed_at=turn.observed_at,
                    idempotency_prefix=f"{turn.idempotency_key}:timeout-fallback",
                    advance=False,
                )
            status: VisibleStatus = settled.terminal_state or self._visible_action_status(
                reply.world_action_id
            )
            if status in {"delivered", "failed", "unknown"}:
                await self._present_after_text(reply.world_action_id, status)
            return self._outcome(
                turn,
                action_ids=(reply.world_action_id,),
                status=status,
                degraded=True,
                reason="first_visible_timeout",
            )

        except Exception:
            return self._outcome(
                turn,
                action_ids=(),
                status="failed",
                degraded=True,
                reason="first_visible_timeout",
            )

    @staticmethod
    def _generation_timeout_seconds(budget: ResponseBudget) -> float:
        """Reserve part of the visible budget for the no-model convergence.

        Letting generation consume the whole deadline made a successful local
        fallback visibly late.  The reserve is bounded so tiny test budgets
        still exercise timeout behavior while a five-second hot turn retains
        half a second for Action staging and transport dispatch.
        """
        reserve_ms = min(500, max(1, budget.first_visible_by_ms // 10))
        return max(0.001, (budget.first_visible_by_ms - reserve_ms) / 1000.0)

    async def settle(self, observation: ExternalObservation) -> SettlementOutcome:
        """Settle one external observation without inventing delivery state.

        A forensic ``operator_reconciliation`` proves one already-attempted
        segment but cannot cause the console to dispatch a later beat.  The
        origin and evidence are validated here rather than trusted from an
        adapter-local caller.
        """
        self._recover_receipt_watchdogs()
        self._validate_observation_world(observation)
        self._validate_reconciliation_observation(observation)
        async with self._action_locks.setdefault(observation.action_id, asyncio.Lock()):
            if observation.kind != "platform_receipt":
                return self._settle_external_observation(observation)
            settled = await self._settle(observation, advance=False)
        if (
            observation.settlement_origin != "operator_reconciliation"
            and observation.status == "delivered"
            and settled.terminal_state is None
        ):
            return await self._advance_after_cadence(
                action_id=observation.action_id,
                delivery_id=int(observation.delivery_id or 0),
                observed_at=observation.observed_at,
                idempotency_prefix=observation.idempotency_key,
                advance=True,
            )
        return settled

    async def dispatch_scheduled(
        self,
        *,
        action_id: str,
        delivery_id: int,
        observed_at: datetime,
        idempotency_key: str,
    ) -> SettlementOutcome:
        """Deliver a World-authorized continuation through the normal receipt path.

        An afterthought or recovered proactive action has already been
        generated and authorized, but it still has to claim a segment, obtain
        a platform receipt, and converge unknown delivery exactly as a normal
        response does.  Callers therefore never do adapter-local
        ``reply → confirm`` bookkeeping.
        """
        self._recover_receipt_watchdogs()
        async with self._action_locks.setdefault(action_id, asyncio.Lock()):
            settled = await self._dispatch_next(
                action_id=action_id,
                delivery_id=delivery_id,
                observed_at=observed_at,
                idempotency_prefix=idempotency_key,
                advance=False,
            )
        if settled.terminal_state == "delivered" and self._has_planned_beat(action_id):
            self._start_continuation(
                action_id=action_id,
                delivery_id=delivery_id,
                observed_at=observed_at,
                idempotency_prefix=idempotency_key,
            )
        return settled
    def _settle_external_observation(
        self, observation: ExternalObservation
    ) -> SettlementOutcome:
        """Settle a non-text External Result without inventing a receipt."""
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        action = self._action(observation.action_id)
        action_kind = str(action.get("kind") or "")

        if observation.kind == "timeout":
            reason = str(
                observation.payload.get("reason")
                or observation.reason
                or "external action timed out"
            ).strip()
            if not reason:
                raise ValueError("timeout observation requires a reason")
            world.submit(
                {
                    "type": "mark_external_action_unknown",
                    "world_id": world_id,
                    "action_id": observation.action_id,
                    "reason": reason[:300],
                    "idempotency_key": observation.idempotency_key,
                },
                expected_revision=world.revision(world_id),
            )
            return SettlementOutcome(
                action_id=observation.action_id,
                terminal_state="unknown",
                committed_revision=world.revision(world_id),
            )

        allowed_kinds = {
            "tool_result": {"tool_execution"},
            # Stickers and reactions are expression media.  Keeping them in
            # this canonical result family prevents adapters from inventing a
            # second, engine-private settlement path for non-text effects.
            "media_result": {
                "media_generation",
                "media_delivery",
                "sticker_delivery",
                "reaction_delivery",
            },
        }
        if action_kind not in allowed_kinds[observation.kind]:
            raise WorldError(
                f"{observation.kind} does not match action kind {action_kind!r}"
            )
        result = dict(observation.payload)
        if observation.status is not None:
            result.setdefault("status", observation.status)
        status = str(result.get("status") or "")
        if status not in {"delivered", "failed", "cancelled"}:
            raise ValueError("external result observation requires a terminal status")
        result.setdefault("kind", action_kind)
        decision = world.record_external_result(
            observation.action_id,
            result,
            world_id=world_id,
            expected_revision=world.revision(world_id),
            idempotency_key=observation.idempotency_key,
        )
        return SettlementOutcome(
            action_id=observation.action_id,
            terminal_state=status,  # type: ignore[arg-type]
            committed_revision=decision.revision,
        )

    async def interrupt(
        self, turn: TurnEnvelope, *, kind: Literal["backchannel", "substantive"]
    ) -> tuple[str, ...]:
        """Apply a live user interruption before the next coalesced turn flushes."""
        if kind == "backchannel":
            return ()
        before = self._cancelled_segment_ids()
        await self._cancel_interrupted_actions(turn, force=True)
        return tuple(sorted(self._cancelled_segment_ids() - before))

    async def _settle(
        self, observation: ExternalObservation, *, advance: bool
    ) -> SettlementOutcome:
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        action = self._validate_observation_reference(observation)
        segments = action.get("segment_state", {}).get("segments", [])
        segment = next(
            (
                item
                for item in segments
                if isinstance(item, dict) and item.get("segment_id") == observation.segment_id
            ),
            {},
        )
        # A process may die after claiming the whole delivery but before the
        # claim projection reaches the individual segment.  The delivery is
        # correctly ``unknown`` while its sole segment remains ``planned``.
        # A forensic receipt can settle that single exact segment, but must
        # use the action-level primitive to preserve its original state.
        unresolved_segments = [
            item
            for item in segments
            if isinstance(item, dict)
            and item.get("status") in {"planned", "sending", "unknown"}
        ]
        reconcile_unclaimed_unknown_action = (
            action.get("status") == "unknown"
            and segment.get("status") == "planned"
            and len(unresolved_segments) == 1
        )

        if observation.status == "delivered":
            if not (observation.external_receipt or "").strip():
                raise ValueError("delivered observation requires an external_receipt")
            reconciliation_evidence = (
                dict(observation.reconciliation_evidence)
                if observation.reconciliation_evidence
                else None
            )
            if reconcile_unclaimed_unknown_action:
                world.settle_outgoing_action(
                    observation.delivery_id,
                    delivered=True,
                    external_receipt=observation.external_receipt,
                    expected_revision=self._expected_revision(observation),
                    reconciliation_evidence=reconciliation_evidence,
                )
            else:
                world.settle_outgoing_segment(
                    observation.delivery_id,
                    observation.segment_id,
                    delivered=True,
                    external_receipt=observation.external_receipt,
                    expected_revision=self._expected_revision(observation),
                    reconciliation_evidence=reconciliation_evidence,
                    cancel_remaining=observation.cancel_remaining,
                )
            action = self._action(observation.action_id)
            segments = action.get("segment_state", {}).get("segments", [])
            if (
                isinstance(segments, list)
                and segments
                and all(
                    isinstance(item, dict) and item.get("status") == "delivered"
                    for item in segments
                )
            ):
                terminal: TerminalState | None = "delivered"
            else:
                terminal = None
        elif observation.status == "failed":
            reconciliation_evidence = (
                dict(observation.reconciliation_evidence)
                if observation.reconciliation_evidence
                else None
            )
            if reconcile_unclaimed_unknown_action:
                world.settle_outgoing_action(
                    observation.delivery_id,
                    delivered=False,
                    reason=observation.reason or "transport_failed",
                    external_receipt=observation.external_receipt,
                    expected_revision=self._expected_revision(observation),
                    reconciliation_evidence=reconciliation_evidence,
                )
            else:
                world.settle_outgoing_segment(
                    observation.delivery_id,
                    observation.segment_id,
                    delivered=False,
                    reason=observation.reason or "transport_failed",
                    expected_revision=self._expected_revision(observation),
                    external_receipt=observation.external_receipt,
                    reconciliation_evidence=reconciliation_evidence,
                )
                world.settle_outgoing_action(
                    observation.delivery_id,
                    delivered=False,
                    reason=observation.reason or "transport_failed",
                    external_receipt=observation.external_receipt,
                    reconciliation_evidence=reconciliation_evidence,
                )
            terminal = "failed"
        elif observation.status == "unknown":
            self._mark_unknown_observation(observation)
            terminal = "unknown"
        else:
            raise ValueError(f"settlement status {observation.status!r} is not yet supported")

        if terminal is not None:
            await self._present_after_text(observation.action_id, terminal)
        return SettlementOutcome(
            action_id=observation.action_id,
            terminal_state=terminal,
            committed_revision=world.revision(world_id),
        )

    async def _present_before_text(self, presentation: TurnPresentation) -> None:
        if self.presenter is None:
            return
        try:
            await self.presenter.before_text(presentation)
        except Exception:
            # A reaction/presence failure must not prevent the text Action from
            # being attempted.  Presenters are responsible for their own World
            # external-result settlement.
            return

    async def _present_after_text(self, action_id: str, terminal_state: TerminalState) -> None:
        presentation = self._presentations.pop(action_id, None)
        if presentation is None or self.presenter is None:
            return
        try:
            await self.presenter.after_text(presentation, terminal_state)
        except Exception:
            # The text receipt is already authoritative; a presenter failure
            # must be settled by that presenter without rewriting text truth.
            return

    async def _dispatch_next(
        self,
        *,
        action_id: str,
        delivery_id: int,
        observed_at: datetime,
        idempotency_prefix: str,
        advance: bool,
    ) -> SettlementOutcome:
        """Claim and dispatch one beat; confirmed delivery advances the sequence."""
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        action = self._action(action_id)
        deadline_raw = str(action.get("complete_by_observed_at") or "")
        remaining_seconds: float | None = None
        if deadline_raw:
            deadline = datetime.fromisoformat(deadline_raw)
            remaining_seconds = (deadline - utc_now()).total_seconds()
            if remaining_seconds <= 0:
                world.expire_outgoing_remainder(
                    delivery_id,
                    reason="outgoing completion deadline elapsed",
                    expected_revision=world.revision(world_id),
                )
                await self._present_after_text(action_id, "cancelled")
                return SettlementOutcome(
                    action_id=action_id,
                    terminal_state="cancelled",
                    committed_revision=world.revision(world_id),
                )
        raw_segments = action.get("segment_state", {}).get("segments", [])
        if not isinstance(raw_segments, list) or not raw_segments:
            raise WorldError("outgoing Action has no planned segments")
        claimed = world.claim_outgoing_segment(
            delivery_id, expected_revision=world.revision(world_id)
        )
        if claimed is None:
            raise WorldError("outgoing segment could not be claimed")
        raw_segment = next(
            (
                item
                for item in raw_segments
                if isinstance(item, dict)
                and str(item.get("segment_id") or "") == claimed.segment_id
            ),
            None,
        )
        if raw_segment is None:
            raise WorldError("claimed outgoing segment does not match projection")
        beat = TurnBeat(
            action_id=action_id,
            delivery_id=delivery_id,
            segment_id=claimed.segment_id,
            position=claimed.position,
            text=str(raw_segment.get("text") or ""),
            platform=str(action.get("platform") or ""),
            canonical_user_id=str(action.get("canonical_user_id") or ""),
            delay_before_ms=int(raw_segment.get("delay_before_ms") or 0),
        )
        try:
            if remaining_seconds is None:
                acceptance = await self.transport.dispatch(beat)
            else:
                async with asyncio.timeout(remaining_seconds):
                    acceptance = await self.transport.dispatch(beat)
        except Exception as exc:
            self._mark_unknown(
                beat,
                observed_at=observed_at,
                reason=f"transport_exception:{type(exc).__name__}",
            )
            return SettlementOutcome(
                action_id=action_id,
                terminal_state="unknown",
                committed_revision=world.revision(world_id),
            )
        if acceptance.status == "accepted":
            world.record_outgoing_segment_acceptance(
                delivery_id,
                claimed.segment_id,
                lookup_token=acceptance.lookup_token or "",
                expected_revision=world.revision(world_id),
            )
            self._start_receipt_watchdog(
                action_id=action_id,
                delivery_id=delivery_id,
                segment_id=claimed.segment_id,
            )
            return SettlementOutcome(
                action_id=action_id,
                terminal_state=None,
                committed_revision=world.revision(world_id),
            )
        return await self._settle(
            ExternalObservation(
                action_id=action_id,
                delivery_id=delivery_id,
                segment_id=claimed.segment_id,
                status=acceptance.status,
                observed_at=observed_at,
                external_receipt=acceptance.external_receipt,
                reason=acceptance.reason,
                idempotency_key=(
                    f"{idempotency_prefix}:segment:{claimed.position}:{acceptance.status}"
                ),
            ),
            advance=advance,
        )

    def _visible_action_status(self, action_id: str) -> VisibleStatus:
        action = self._action(action_id)
        action_status = str(action.get("status") or "")
        if action_status in {"failed", "unknown"}:
            return action_status  # type: ignore[return-value]
        segment_state = action.get("segment_state", {})
        segments = segment_state.get("segments", []) if isinstance(segment_state, dict) else []
        if any(isinstance(item, dict) and item.get("status") == "delivered" for item in segments):
            return "delivered"
        return "accepted"

    def _has_planned_beat(self, action_id: str) -> bool:
        action = self._action(action_id)
        segment_state = action.get("segment_state", {})
        segments = segment_state.get("segments", []) if isinstance(segment_state, dict) else []
        return any(isinstance(item, dict) and item.get("status") == "planned" for item in segments)

    def _start_continuation(
        self,
        *,
        action_id: str,
        delivery_id: int,
        observed_at: datetime,
        idempotency_prefix: str,
    ) -> None:
        async def run() -> None:
            try:
                await self._advance_after_cadence(
                    action_id=action_id,
                    delivery_id=delivery_id,
                    observed_at=observed_at,
                    idempotency_prefix=idempotency_prefix,
                    advance=True,
                )
            except (WorldError, ValueError):
                return

        task = asyncio.create_task(run())
        self._continuations.add(task)
        self._delivery_continuations.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._continuations.discard(done)
            self._delivery_continuations.discard(done)

        task.add_done_callback(finished)

    def _recover_receipt_watchdogs(self) -> None:
        """Re-arm accepted receipt deadlines when the seam is reconstructed."""
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        actions = world.snapshot(world_id).get("actions", {})
        if not isinstance(actions, dict):
            return
        for action_id, raw in actions.items():
            if not isinstance(raw, dict) or raw.get("kind") != "outgoing_message":
                continue
            segments = raw.get("segment_state", {}).get("segments", [])
            if not isinstance(segments, list):
                continue
            for segment in segments:
                if (
                    isinstance(segment, dict)
                    and segment.get("status") in {"sending", "unknown"}
                    and segment.get("receipt_lookup_token")
                ):
                    self._start_receipt_watchdog(
                        action_id=str(action_id),
                        delivery_id=int(raw.get("delivery_id") or 0),
                        segment_id=str(segment.get("segment_id") or ""),
                    )

    def _start_receipt_watchdog(self, *, action_id: str, delivery_id: int, segment_id: str) -> None:
        key = (action_id, segment_id)
        if key in self._watchdog_keys:
            return
        self._watchdog_keys.add(key)

        async def run() -> None:
            action = self._action(action_id)
            deadline_raw = str(action.get("complete_by_observed_at") or "")
            if not deadline_raw:
                return
            remaining = max(
                0.0,
                (datetime.fromisoformat(deadline_raw) - utc_now()).total_seconds(),
            )
            await self.sleep(remaining)
            lookup_reason = "platform receipt missing at completion deadline"
            action = self._action(action_id)
            segments = action.get("segment_state", {}).get("segments", [])
            segment = next(
                (
                    item
                    for item in segments
                    if isinstance(item, dict) and item.get("segment_id") == segment_id
                ),
                None,
            )
            if not isinstance(segment, dict) or segment.get("status") not in {
                "sending",
                "unknown",
            }:
                return
            lookup_token = str(segment.get("receipt_lookup_token") or "")
            if isinstance(self.transport, ReceiptLookupTransport) and lookup_token:
                try:
                    receipt = await self.transport.lookup_delivery(lookup_token)
                except Exception as exc:
                    lookup_reason = f"receipt_lookup_exception:{type(exc).__name__}"
                else:
                    if receipt.action_id != action_id:
                        lookup_reason = "receipt_lookup_action_mismatch"
                    elif receipt.status in {"delivered", "failed"}:
                        try:
                            await self.settle(
                                ExternalObservation(
                                    action_id=action_id,
                                    delivery_id=delivery_id,
                                    segment_id=segment_id,
                                    status=receipt.status,
                                    observed_at=utc_now(),
                                    external_receipt=(
                                        receipt.external_receipt
                                        or f"receipt_lookup:{lookup_token}"
                                    ),
                                    reason="receipt_lookup_confirmed",
                                    idempotency_key=(
                                        f"receipt-lookup:{action_id}:{segment_id}:"
                                        f"{receipt.status}:{receipt.external_receipt or lookup_token}"
                                    ),
                                )
                            )
                        except (ValueError, WorldError):
                            # A concurrent external observation or user
                            # interruption settled the segment first.  Its
                            # authoritative terminal state wins.
                            return
                        return
                    else:
                        lookup_reason = "receipt_lookup_unresolved"
            async with self._action_locks.setdefault(action_id, asyncio.Lock()):
                action = self._action(action_id)
                segments = action.get("segment_state", {}).get("segments", [])
                segment = next(
                    (
                        item
                        for item in segments
                        if isinstance(item, dict) and item.get("segment_id") == segment_id
                    ),
                    None,
                )
                if not isinstance(segment, dict) or segment.get("status") != "sending":
                    return
                world = self.engine.world_kernel
                world_id = self.engine.world_id
                assert world is not None and world_id is not None
                world.expire_outgoing_remainder(
                    delivery_id,
                    reason=f"platform receipt deadline elapsed: {lookup_reason}",
                    expected_revision=world.revision(world_id),
                )
                world.mark_outgoing_segment_unknown(
                    delivery_id,
                    segment_id,
                    reason=lookup_reason,
                    expected_revision=world.revision(world_id),
                )
                await self._present_after_text(action_id, "unknown")

        task = asyncio.create_task(run())
        self._continuations.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._continuations.discard(done)
            self._watchdog_keys.discard(key)

        task.add_done_callback(finished)

    async def _advance_after_cadence(
        self,
        *,
        action_id: str,
        delivery_id: int,
        observed_at: datetime,
        idempotency_prefix: str,
        advance: bool,
    ) -> SettlementOutcome:
        delay_seconds = self._next_beat_delay_seconds(action_id)
        action = self._action(action_id)
        deadline_raw = str(action.get("complete_by_observed_at") or "")
        if deadline_raw:
            remaining = (datetime.fromisoformat(deadline_raw) - utc_now()).total_seconds()
            if remaining <= 0 or delay_seconds >= remaining:
                world = self.engine.world_kernel
                world_id = self.engine.world_id
                assert world is not None and world_id is not None
                world.expire_outgoing_remainder(
                    delivery_id,
                    reason="next expression beat cannot fit completion deadline",
                    expected_revision=world.revision(world_id),
                )
                await self._present_after_text(action_id, "cancelled")
                return SettlementOutcome(
                    action_id=action_id,
                    terminal_state="cancelled",
                    committed_revision=world.revision(world_id),
                )
        # Do not hold the Action lock while a natural beat interval elapses:
        # a substantive user interjection must be able to cancel the remainder.
        await self.sleep(delay_seconds)
        async with self._action_locks.setdefault(action_id, asyncio.Lock()):
            if not self._has_planned_beat(action_id):
                action = self._action(action_id)
                state = str(action.get("status") or "")
                terminal: TerminalState | None = None
                if state in {"delivered", "failed", "cancelled", "unknown"}:
                    terminal = state  # type: ignore[assignment]
                if terminal is not None:
                    await self._present_after_text(action_id, terminal)
                world = self.engine.world_kernel
                assert world is not None
                return SettlementOutcome(
                    action_id=action_id,
                    terminal_state=terminal,
                    committed_revision=world.revision(self.engine.world_id or ""),
                )
            dispatched = await self._dispatch_next(
                action_id=action_id,
                delivery_id=delivery_id,
                observed_at=observed_at,
                idempotency_prefix=idempotency_prefix,
                advance=advance,
            )
            if dispatched.terminal_state is None and self._has_planned_beat(action_id):
                self._start_continuation(
                    action_id=action_id,
                    delivery_id=delivery_id,
                    observed_at=observed_at,
                    idempotency_prefix=idempotency_prefix,
                )
            return dispatched

    def _next_beat_delay_seconds(self, action_id: str) -> float:
        """Use a model-selected bounded pause, otherwise retain cadence."""
        action = self._action(action_id)
        segments = action.get("segment_state", {}).get("segments", [])
        if not isinstance(segments, list):
            return self.cadence_delay_seconds
        next_segment = next(
            (
                item
                for item in segments
                if isinstance(item, dict) and item.get("status") == "planned"
            ),
            None,
        )
        if not isinstance(next_segment, dict):
            return 0.0
        delay_ms = int(next_segment.get("delay_before_ms") or 0)
        return delay_ms / 1000 if delay_ms > 0 else self.cadence_delay_seconds

    async def _cancel_interrupted_actions(self, turn: TurnEnvelope, *, force: bool = False) -> None:
        """A substantive new user turn takes over any partially delivered reply."""
        if not force and not self._is_substantive_interjection(turn.message.text):
            return
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        canonical_user_id = self.engine.store.resolve_user(
            turn.message.platform, turn.message.platform_user_id
        )
        actions = world.snapshot(world_id).get("actions", {})
        if not isinstance(actions, dict):
            return
        candidates: list[str] = []
        for action_id, raw in actions.items():
            if not isinstance(raw, dict) or raw.get("kind") != "outgoing_message":
                continue
            if (
                str(raw.get("platform") or "") != turn.message.platform
                or str(raw.get("canonical_user_id") or "") != canonical_user_id
            ):
                continue
            candidates.append(str(action_id))
        for action_id in candidates:
            async with self._action_locks.setdefault(action_id, asyncio.Lock()):
                raw = self._action(action_id)
                await self._cancel_interrupted_action(turn, action_id, raw)

    def _cancelled_segment_ids(self) -> set[str]:
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        actions = world.snapshot(world_id).get("actions", {})
        if not isinstance(actions, dict):
            return set()
        return {
            str(segment.get("segment_id") or "")
            for raw in actions.values()
            if isinstance(raw, dict)
            for segment in (
                raw.get("segment_state", {}).get("segments", [])
                if isinstance(raw.get("segment_state"), dict)
                else []
            )
            if isinstance(segment, dict) and segment.get("status") == "cancelled"
        }

    async def _cancel_interrupted_action(
        self, turn: TurnEnvelope, action_id: str, raw: dict[str, object]
    ) -> None:
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        segment_state = raw.get("segment_state", {})
        segments = segment_state.get("segments", []) if isinstance(segment_state, dict) else []
        has_delivered = any(
            isinstance(item, dict) and item.get("status") == "delivered" for item in segments
        )
        has_planned = any(
            isinstance(item, dict) and item.get("status") == "planned" for item in segments
        )
        has_accepted = any(
            isinstance(item, dict)
            and item.get("status") == "sending"
            and bool(item.get("receipt_lookup_token"))
            for item in segments
        )
        if has_delivered and has_planned:
            world.observe_outgoing_interjection(
                int(raw.get("delivery_id") or 0),
                kind="substantive",
                user_message_id=turn.idempotency_key,
                expected_revision=world.revision(world_id),
            )
            await self._present_after_text(action_id, "cancelled")
        elif has_planned and not has_accepted:
            world.expire_outgoing_remainder(
                int(raw.get("delivery_id") or 0),
                reason="substantive user interjection before text dispatch",
                terminal_reason=f"interrupted_by:{turn.idempotency_key}",
                expected_revision=world.revision(world_id),
            )
            await self._present_after_text(action_id, "cancelled")
        elif has_accepted and has_planned:
            delivery_id = int(raw.get("delivery_id") or 0)
            world.expire_outgoing_remainder(
                delivery_id,
                reason="substantive user interjection before platform receipt",
                terminal_reason=f"interrupted_by:{turn.idempotency_key}",
                expected_revision=world.revision(world_id),
            )
            accepted_segment = next(
                item
                for item in segments
                if isinstance(item, dict)
                and item.get("status") == "sending"
                and item.get("receipt_lookup_token")
            )
            world.mark_outgoing_segment_unknown(
                delivery_id,
                str(accepted_segment.get("segment_id") or ""),
                reason="accepted delivery superseded before receipt",
                expected_revision=world.revision(world_id),
            )
            await self._present_after_text(action_id, "unknown")

    @staticmethod
    def _is_substantive_interjection(text: str) -> bool:
        compact = "".join(text.split())
        if not compact:
            return False
        backchannels = {
            "嗯",
            "嗯嗯",
            "哦",
            "噢",
            "啊",
            "对",
            "是",
            "确实",
            "真的",
            "好",
            "行",
            "可以",
            "对吧",
            "是吧",
            "懂了",
            "有道理",
        }
        return compact.rstrip("。！!～~") not in backchannels

    def _validate_observation_reference(
        self, observation: ExternalObservation
    ) -> dict[str, object]:
        try:
            action = self._action(observation.action_id)
        except WorldError as exc:
            raise WorldError("action does not belong to this CompanionTurn") from exc
        if observation.delivery_id is None or int(action.get("delivery_id") or -1) != observation.delivery_id:
            raise WorldError("delivery does not belong to the observed Action")
        segment_state = action.get("segment_state", {})
        segments = segment_state.get("segments", []) if isinstance(segment_state, dict) else []
        if not any(
            isinstance(item, dict) and str(item.get("segment_id") or "") == observation.segment_id
            for item in segments
        ):
            raise WorldError("segment does not belong to the observed Action")
        return action

    def _validate_observation_world(self, observation: ExternalObservation) -> None:
        world_id = self.engine.world_id
        if observation.world_id is not None and observation.world_id != world_id:
            raise WorldError("external observation belongs to a different World")

    def _validate_reconciliation_observation(
        self, observation: ExternalObservation
    ) -> None:
        evidence = observation.reconciliation_evidence
        is_operator_reconciliation = (
            observation.settlement_origin == "operator_reconciliation"
        )
        if not is_operator_reconciliation:
            if evidence or observation.cancel_remaining:
                raise WorldError(
                    "only operator reconciliation may attach evidence or cancel remaining beats"
                )
            return
        if observation.status not in {"delivered", "failed"}:
            raise WorldError("operator reconciliation requires a terminal platform receipt")
        if not evidence:
            raise WorldError("operator reconciliation requires auditable evidence")
        required_evidence = ("reference", "reviewer_id", "review_note")
        if any(not str(evidence.get(key) or "").strip() for key in required_evidence):
            raise WorldError("operator reconciliation evidence is incomplete")
        if observation.cancel_remaining and not str(
            evidence.get("cancel_remaining_reason") or ""
        ).strip():
            raise WorldError("cancelling remaining beats requires an audited reason")
        action = self._validate_observation_reference(observation)
        if action.get("status") != "unknown":
            raise WorldError("operator reconciliation may only settle an unknown Action")
        segments = action.get("segment_state", {}).get("segments", [])
        segment = next(
            (
                item
                for item in segments
                if isinstance(item, dict) and item.get("segment_id") == observation.segment_id
            ),
            None,
        )
        if not isinstance(segment, dict):
            raise WorldError("operator reconciliation segment is missing")
        if segment.get("status") == "unknown":
            return
        unresolved = [
            item
            for item in segments
            if isinstance(item, dict)
            and item.get("status") in {"planned", "sending", "unknown"}
        ]
        if not (segment.get("status") == "planned" and len(unresolved) == 1):
            raise WorldError(
                "operator reconciliation requires an unknown segment or one unclaimed unknown Action"
            )

    def _expected_revision(self, observation: ExternalObservation) -> int:
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        return (
            observation.expected_revision
            if observation.expected_revision is not None
            else world.revision(world_id)
        )

    def _action(self, action_id: str) -> dict[str, object]:
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        raw = world.snapshot(world_id).get("actions", {}).get(action_id)
        if not isinstance(raw, dict):
            raise WorldError(f"outgoing Action {action_id!r} is missing")
        return raw

    def _existing_outcome(self, turn: TurnEnvelope) -> TurnOutcome | None:
        """Return the durable result of an already accepted platform message."""
        matched = self._matching_action(turn)
        if matched is None:
            return None
        action_id, action = matched
        status = str(action.get("status") or "")
        visible_status: VisibleStatus = {
            "delivered": "delivered",
            "failed": "failed",
            "unknown": "unknown",
        }.get(status, "accepted")
        return self._outcome(
            turn,
            action_ids=(action_id,),
            status=visible_status,
            degraded=visible_status in {"failed", "unknown"},
            reason=(
                "replayed_idempotent_failure" if visible_status in {"failed", "unknown"} else None
            ),
        )

    def _matching_action(self, turn: TurnEnvelope) -> tuple[str, dict[str, object]] | None:
        message_id = str(turn.message.message_id or "")
        if not message_id:
            return None
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        canonical_user_id = self.engine.store.resolve_user(
            turn.message.platform, turn.message.platform_user_id
        )
        actions = world.snapshot(world_id).get("actions", {})
        if not isinstance(actions, dict):
            return None
        matched: list[tuple[str, dict[str, object]]] = []
        for action_id, raw in actions.items():
            if not isinstance(raw, dict) or raw.get("kind") != "outgoing_message":
                continue
            trace = raw.get("trace", {})
            if (
                isinstance(trace, dict)
                and str(trace.get("input_message_id") or "") == message_id
                and str(raw.get("platform") or "") == turn.message.platform
                and str(raw.get("canonical_user_id") or "") == canonical_user_id
            ):
                matched.append((str(action_id), raw))
        if not matched:
            return None
        return matched[-1]

    def _mark_unknown(self, beat: TurnBeat, *, observed_at: datetime, reason: str) -> None:
        self._mark_unknown_observation(
            ExternalObservation(
                action_id=beat.action_id,
                delivery_id=beat.delivery_id,
                segment_id=beat.segment_id,
                status="unknown",
                observed_at=observed_at,
                reason=reason,
                idempotency_key=f"unknown:{beat.segment_id}:{reason}",
            )
        )

    def _mark_unknown_observation(self, observation: ExternalObservation) -> None:
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        world.mark_outgoing_segment_unknown(
            observation.delivery_id,
            observation.segment_id,
            reason=observation.reason or "transport_delivery_unknown",
            expected_revision=world.revision(world_id),
        )

    def _outcome(
        self,
        turn: TurnEnvelope,
        *,
        action_ids: tuple[str, ...],
        status: VisibleStatus,
        degraded: bool = False,
        reason: str | None = None,
    ) -> TurnOutcome:
        world = self.engine.world_kernel
        world_id = self.engine.world_id
        assert world is not None and world_id is not None
        return TurnOutcome(
            turn_id=str(turn.message.message_id or turn.idempotency_key),
            committed_revision=world.revision(world_id),
            action_ids=action_ids,
            visible_status=status,
            degraded=degraded,
            degradation_reason=reason,
        )


async def settle_external_result(
    engine: CompanionEngine, observation: ExternalObservation
) -> SettlementOutcome:
    """Settle a background external result without creating an adapter bypass.

    Engine-owned workers use this narrow helper only when no live inbound turn
    owns the result (for example image render completion after a restart).
    The authoritative write remains ``CompanionTurn.settle``.
    """
    return await CompanionTurn(engine, _SettlementOnlyTransport()).settle(observation)
