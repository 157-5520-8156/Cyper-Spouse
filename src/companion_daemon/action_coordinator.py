from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum


class DeliveryStatus(StrEnum):
    PLANNED = "planned"
    SENDING = "sending"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class UserInterjectionKind(StrEnum):
    BACKCHANNEL = "backchannel"
    SUBSTANTIVE = "substantive"


class SegmentTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class OutgoingSegment:
    segment_id: str
    position: int
    text: str
    delay_before_ms: int = 0
    status: DeliveryStatus = DeliveryStatus.PLANNED
    external_receipt: str | None = None
    terminal_reason: str | None = None


@dataclass(frozen=True)
class SegmentedOutgoingAction:
    action_id: str
    segments: tuple[OutgoingSegment, ...]

    @property
    def status(self) -> DeliveryStatus:
        statuses = {segment.status for segment in self.segments}
        for status in (
            DeliveryStatus.UNKNOWN,
            DeliveryStatus.SENDING,
            DeliveryStatus.PLANNED,
            DeliveryStatus.CANCELLED,
        ):
            if status in statuses:
                return status
        return DeliveryStatus.DELIVERED


class SegmentedActionCoordinator:
    """Pure state machine for one ordered, interruptible outgoing Action."""

    def plan_action(
        self,
        *,
        action_id: str,
        texts: Sequence[str],
        delays_before_ms: Sequence[int] | None = None,
    ) -> SegmentedOutgoingAction:
        if not action_id.strip():
            raise ValueError("action_id must not be blank")
        if not texts:
            raise ValueError("an outgoing action needs at least one segment")
        if any(not text.strip() for text in texts):
            raise ValueError("outgoing segments must not be blank")
        delays = tuple(delays_before_ms or (0,) * len(texts))
        if len(delays) != len(texts) or any(
            type(delay) is not int or not 0 <= delay <= 20_000
            for delay in delays
        ):
            raise ValueError("outgoing segment delays must match bounded texts")
        segments = tuple(
            OutgoingSegment(
                segment_id=f"{action_id}:segment:{position}",
                position=position,
                text=text,
                delay_before_ms=delays[position],
            )
            for position, text in enumerate(texts)
        )
        return SegmentedOutgoingAction(action_id=action_id, segments=segments)

    def claim_next(
        self, action: SegmentedOutgoingAction
    ) -> tuple[SegmentedOutgoingAction, OutgoingSegment]:
        if any(segment.status is DeliveryStatus.UNKNOWN for segment in action.segments):
            raise SegmentTransitionError("unknown segment requires receipt reconciliation")
        if any(segment.status is DeliveryStatus.SENDING for segment in action.segments):
            raise SegmentTransitionError("a segment is already sending")
        next_segment = next(
            (
                segment
                for segment in action.segments
                if segment.status is DeliveryStatus.PLANNED
            ),
            None,
        )
        if next_segment is None:
            raise SegmentTransitionError("action has no planned segment")
        claimed = replace(next_segment, status=DeliveryStatus.SENDING)
        segments = tuple(
            claimed if segment.segment_id == claimed.segment_id else segment
            for segment in action.segments
        )
        return replace(action, segments=segments), claimed

    def confirm_delivered(
        self,
        action: SegmentedOutgoingAction,
        *,
        segment_id: str,
        external_receipt: str | None = None,
    ) -> SegmentedOutgoingAction:
        current = self._segment(action, segment_id)
        if current.status is DeliveryStatus.DELIVERED:
            return action
        if current.status not in {DeliveryStatus.SENDING, DeliveryStatus.UNKNOWN}:
            raise SegmentTransitionError(
                f"{current.status.value} segment cannot transition to delivered"
            )
        if current.status is DeliveryStatus.UNKNOWN and not external_receipt:
            raise SegmentTransitionError(
                "unknown segment needs an external receipt before reconciliation"
            )
        delivered = replace(
            current,
            status=DeliveryStatus.DELIVERED,
            external_receipt=external_receipt,
            terminal_reason=None,
        )
        return self._replace_segment(action, delivered)

    def mark_unknown(
        self,
        action: SegmentedOutgoingAction,
        *,
        segment_id: str,
        reason: str,
    ) -> SegmentedOutgoingAction:
        current = self._segment(action, segment_id)
        if current.status is DeliveryStatus.UNKNOWN:
            return action
        if current.status is not DeliveryStatus.SENDING:
            raise SegmentTransitionError(
                f"{current.status.value} segment cannot transition to unknown"
            )
        unknown = replace(
            current,
            status=DeliveryStatus.UNKNOWN,
            terminal_reason=reason,
        )
        return self._replace_segment(action, unknown)

    def observe_user_interjection(
        self,
        action: SegmentedOutgoingAction,
        *,
        kind: UserInterjectionKind,
        user_message_id: str,
    ) -> tuple[SegmentedOutgoingAction, tuple[str, ...]]:
        has_delivered = any(
            segment.status is DeliveryStatus.DELIVERED for segment in action.segments
        )
        if kind is not UserInterjectionKind.SUBSTANTIVE or not has_delivered:
            return action, ()
        cancelled_ids = tuple(
            segment.segment_id
            for segment in action.segments
            if segment.status is DeliveryStatus.PLANNED
        )
        segments = tuple(
            replace(
                segment,
                status=DeliveryStatus.CANCELLED,
                terminal_reason=f"interrupted_by:{user_message_id}",
            )
            if segment.segment_id in cancelled_ids
            else segment
            for segment in action.segments
        )
        return replace(action, segments=segments), cancelled_ids

    def chat_history_entries(
        self, action: SegmentedOutgoingAction
    ) -> tuple[OutgoingSegment, ...]:
        return tuple(
            segment
            for segment in action.segments
            if segment.status is DeliveryStatus.DELIVERED
        )

    @staticmethod
    def to_projection(action: SegmentedOutgoingAction) -> dict[str, object]:
        return {
            "action_id": action.action_id,
            "status": action.status.value,
            "segments": [
                {
                    "segment_id": segment.segment_id,
                    "position": segment.position,
                    "text": segment.text,
                    "delay_before_ms": segment.delay_before_ms,
                    "status": segment.status.value,
                    "external_receipt": segment.external_receipt,
                    "terminal_reason": segment.terminal_reason,
                }
                for segment in action.segments
            ],
        }

    @staticmethod
    def from_projection(projection: Mapping[str, object]) -> SegmentedOutgoingAction:
        raw_segments = projection.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise ValueError("action projection needs at least one segment")
        segments: list[OutgoingSegment] = []
        for raw_segment in raw_segments:
            if not isinstance(raw_segment, Mapping):
                raise ValueError("action projection segment must be an object")
            delay_before_ms = int(raw_segment.get("delay_before_ms") or 0)
            if not 0 <= delay_before_ms <= 20_000:
                raise ValueError("action projection segment delay is out of bounds")
            segments.append(
                OutgoingSegment(
                    segment_id=str(raw_segment["segment_id"]),
                    position=int(raw_segment["position"]),
                    text=str(raw_segment["text"]),
                    delay_before_ms=delay_before_ms,
                    status=DeliveryStatus(str(raw_segment["status"])),
                    external_receipt=(
                        str(raw_segment["external_receipt"])
                        if raw_segment.get("external_receipt") is not None
                        else None
                    ),
                    terminal_reason=(
                        str(raw_segment["terminal_reason"])
                        if raw_segment.get("terminal_reason") is not None
                        else None
                    ),
                )
            )
        action = SegmentedOutgoingAction(
            action_id=str(projection["action_id"]),
            segments=tuple(segments),
        )
        projected_status = projection.get("status")
        if projected_status is not None and str(projected_status) != action.status.value:
            raise ValueError("action projection status does not match its segments")
        return action

    def planned_world_event(
        self, action: SegmentedOutgoingAction
    ) -> tuple[str, dict[str, object]]:
        projection = self.to_projection(action)
        return (
            "ActionSegmentsPlanned",
            {
                "action_id": projection["action_id"],
                "segments": projection["segments"],
            },
        )

    def claimed_world_event(
        self,
        action: SegmentedOutgoingAction,
        segment: OutgoingSegment,
    ) -> tuple[str, dict[str, object]]:
        current = self._segment(action, segment.segment_id)
        if current != segment or current.status is not DeliveryStatus.SENDING:
            raise SegmentTransitionError("dispatch event requires the claimed sending segment")
        return (
            "ActionSegmentDispatchClaimed",
            {
                "action_id": action.action_id,
                "segment_id": segment.segment_id,
                "position": segment.position,
            },
        )

    def settled_world_event(
        self,
        action: SegmentedOutgoingAction,
        *,
        segment_id: str,
    ) -> tuple[str, dict[str, object]]:
        segment = self._segment(action, segment_id)
        if segment.status is not DeliveryStatus.DELIVERED:
            raise SegmentTransitionError("settlement event requires a delivered segment")
        return (
            "ActionSegmentSettled",
            {
                "action_id": action.action_id,
                "segment_id": segment.segment_id,
                "position": segment.position,
                "result": {
                    "kind": "delivery",
                    "status": segment.status.value,
                    "external_receipt": segment.external_receipt,
                },
            },
        )

    def unknown_world_event(
        self,
        action: SegmentedOutgoingAction,
        *,
        segment_id: str,
    ) -> tuple[str, dict[str, object]]:
        segment = self._segment(action, segment_id)
        if segment.status is not DeliveryStatus.UNKNOWN:
            raise SegmentTransitionError("uncertainty event requires an unknown segment")
        return (
            "ActionSegmentDeliveryUncertain",
            {
                "action_id": action.action_id,
                "segment_id": segment.segment_id,
                "position": segment.position,
                "reason": segment.terminal_reason,
            },
        )

    def cancelled_world_event(
        self,
        action: SegmentedOutgoingAction,
        *,
        segment_ids: tuple[str, ...],
        user_message_id: str,
    ) -> tuple[str, dict[str, object]]:
        if not segment_ids:
            raise SegmentTransitionError("cancellation event requires cancelled segments")
        expected_reason = f"interrupted_by:{user_message_id}"
        for segment_id in segment_ids:
            segment = self._segment(action, segment_id)
            if (
                segment.status is not DeliveryStatus.CANCELLED
                or segment.terminal_reason != expected_reason
            ):
                raise SegmentTransitionError(
                    "cancellation event does not match the interrupting user turn"
                )
        return (
            "ActionSegmentsCancelled",
            {
                "action_id": action.action_id,
                "segment_ids": list(segment_ids),
                "reason": "substantive_user_interjection",
                "user_message_id": user_message_id,
            },
        )

    @staticmethod
    def _segment(action: SegmentedOutgoingAction, segment_id: str) -> OutgoingSegment:
        return next(segment for segment in action.segments if segment.segment_id == segment_id)

    @staticmethod
    def _replace_segment(
        action: SegmentedOutgoingAction, replacement: OutgoingSegment
    ) -> SegmentedOutgoingAction:
        return replace(
            action,
            segments=tuple(
                replacement if segment.segment_id == replacement.segment_id else segment
                for segment in action.segments
            ),
        )


ActionCoordinator = SegmentedActionCoordinator
