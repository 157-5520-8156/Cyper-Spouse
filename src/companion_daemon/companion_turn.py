"""Deep turn seam for model-led expression and authoritative delivery settlement.

Phase 1 deliberately delegates generation to ``CompanionEngine`` while moving
the observable text Action lifecycle behind one interface.  Later phases can
replace the generation implementation without changing platform callers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Literal, Protocol

from companion_daemon.engine import CompanionEngine
from companion_daemon.models import IncomingMessage
from companion_daemon.time import utc_now
from companion_daemon.world import WorldError


VisibleStatus = Literal["delivered", "accepted", "failed", "unknown"]
TerminalState = Literal["delivered", "failed", "cancelled", "unknown"]


@dataclass(frozen=True)
class TurnEnvelope:
    """One normalized, immutable platform observation."""

    message: IncomingMessage
    idempotency_key: str

    @classmethod
    def from_message(cls, message: IncomingMessage, *, idempotency_key: str) -> TurnEnvelope:
        raw_message_id = str(message.message_id or "").strip()
        if not raw_message_id:
            raise ValueError("TurnEnvelope requires a platform message_id")
        canonical_key = f"{message.platform}:{message.platform_user_id}:{raw_message_id}"
        if idempotency_key != canonical_key:
            raise ValueError("idempotency_key must bind platform, account, and platform message_id")
        normalized = message.model_copy(deep=True)
        normalized.message_id = canonical_key
        return cls(message=normalized, idempotency_key=canonical_key)

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
    action_id: str
    delivery_id: int
    segment_id: str
    status: TerminalState
    observed_at: datetime
    idempotency_key: str
    external_receipt: str | None = None
    reason: str | None = None


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
    ) -> None:
        if engine.world_kernel is None or engine.world_id is None:
            raise ValueError("CompanionTurn v2 requires the authoritative World runtime")
        self.engine = engine
        self.transport = transport
        self.cadence_delay_seconds = max(0.0, cadence_delay_seconds)
        self.sleep = sleep
        self._continuations: set[asyncio.Task[None]] = set()
        self._action_locks: dict[str, asyncio.Lock] = {}
        self._watchdog_keys: set[tuple[str, str]] = set()
        self._recover_receipt_watchdogs()

    async def start(self) -> None:
        """Restore durable receipt watchdogs after the application's loop starts."""
        self._recover_receipt_watchdogs()
        await asyncio.sleep(0)

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
            return self._converge_timeout(turn)

    async def _respond_with_timeout(
        self,
        turn: TurnEnvelope,
        *,
        budget: ResponseBudget,
        options: TurnOptions,
    ) -> TurnOutcome:
        existing = self._existing_outcome(turn)
        if existing is not None:
            return existing
        await self._cancel_interrupted_actions(turn)
        complete_by_at = utc_now() + timedelta(milliseconds=budget.complete_by_ms)
        timeout_seconds = budget.first_visible_by_ms / 1000.0
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

    def _converge_timeout(self, turn: TurnEnvelope) -> TurnOutcome:
        """Close any staged Action after cancellation instead of leaving it open."""
        action_match = self._matching_action(turn)
        if action_match is None:
            return self._outcome(
                turn,
                action_ids=(),
                status="failed",
                degraded=True,
                reason="first_visible_timeout",
            )
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

    async def settle(self, observation: ExternalObservation) -> SettlementOutcome:
        self._recover_receipt_watchdogs()
        async with self._action_locks.setdefault(observation.action_id, asyncio.Lock()):
            return await self._settle(observation, advance=True)

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
        self._validate_observation_reference(observation)

        if observation.status == "delivered":
            if not (observation.external_receipt or "").strip():
                raise ValueError("delivered observation requires an external_receipt")
            world.settle_outgoing_segment(
                observation.delivery_id,
                observation.segment_id,
                delivered=True,
                external_receipt=observation.external_receipt,
                expected_revision=world.revision(world_id),
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
            elif advance:
                return await self._advance_after_cadence(
                    action_id=observation.action_id,
                    delivery_id=observation.delivery_id,
                    observed_at=observation.observed_at,
                    idempotency_prefix=observation.idempotency_key,
                    advance=True,
                )
            else:
                terminal = None
        elif observation.status == "failed":
            world.settle_outgoing_segment(
                observation.delivery_id,
                observation.segment_id,
                delivered=False,
                reason=observation.reason or "transport_failed",
                expected_revision=world.revision(world_id),
            )
            world.settle_outgoing_action(
                observation.delivery_id,
                delivered=False,
                reason=observation.reason or "transport_failed",
            )
            terminal = "failed"
        elif observation.status == "unknown":
            self._mark_unknown_observation(observation)
            terminal = "unknown"
        else:
            raise ValueError(f"settlement status {observation.status!r} is not yet supported")

        return SettlementOutcome(
            action_id=observation.action_id,
            terminal_state=terminal,
            committed_revision=world.revision(world_id),
        )

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
                async with self._action_locks.setdefault(action_id, asyncio.Lock()):
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
        task.add_done_callback(self._continuations.discard)

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
                    and segment.get("status") == "sending"
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
                    reason="platform receipt deadline elapsed",
                    expected_revision=world.revision(world_id),
                )
                world.mark_outgoing_segment_unknown(
                    delivery_id,
                    segment_id,
                    reason="platform receipt missing at completion deadline",
                    expected_revision=world.revision(world_id),
                )

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
        await self.sleep(self.cadence_delay_seconds)
        if not self._has_planned_beat(action_id):
            action = self._action(action_id)
            state = str(action.get("status") or "")
            terminal: TerminalState | None = None
            if state in {"delivered", "failed", "cancelled", "unknown"}:
                terminal = state  # type: ignore[assignment]
            world = self.engine.world_kernel
            assert world is not None
            return SettlementOutcome(
                action_id=action_id,
                terminal_state=terminal,
                committed_revision=world.revision(self.engine.world_id or ""),
            )
        return await self._dispatch_next(
            action_id=action_id,
            delivery_id=delivery_id,
            observed_at=observed_at,
            idempotency_prefix=idempotency_prefix,
            advance=advance,
        )

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
            await self._cancel_interrupted_action(turn, raw)

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

    async def _cancel_interrupted_action(self, turn: TurnEnvelope, raw: dict[str, object]) -> None:
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
        if int(action.get("delivery_id") or -1) != observation.delivery_id:
            raise WorldError("delivery does not belong to the observed Action")
        segment_state = action.get("segment_state", {})
        segments = segment_state.get("segments", []) if isinstance(segment_state, dict) else []
        if not any(
            isinstance(item, dict) and str(item.get("segment_id") or "") == observation.segment_id
            for item in segments
        ):
            raise WorldError("segment does not belong to the observed Action")
        return action

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
