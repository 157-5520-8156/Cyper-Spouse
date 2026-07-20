"""Hash-bound provider perception output for later internal Context."""

from __future__ import annotations

from datetime import datetime
import hashlib
from typing import Protocol

from pydantic import Field, model_validator

from .perception import perception_result_trigger_id
from .schema_core import FrozenModel, PrivacyClass
from .schemas import LedgerProjection, ProjectionCursor


class PerceptionResultContent(FrozenModel):
    result_ref: str = Field(min_length=1, max_length=512)
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=12_000)

    @model_validator(mode="after")
    def content_matches_hash(self) -> PerceptionResultContent:
        actual = "sha256:" + hashlib.sha256(self.text.encode()).hexdigest()
        if actual != self.result_hash:
            raise ValueError("perception result content does not match result_hash")
        return self


class PerceptionResultReader(Protocol):
    """Read exact durable provider output by an accepted opaque reference."""

    def read_exact(self, *, result_ref: str) -> PerceptionResultContent | None: ...


class PerceptionResultContextSource(FrozenModel):
    result_event_ref: str = Field(min_length=1)
    result_world_revision: int = Field(ge=1)
    result_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_event_ref: str = Field(min_length=1)
    receipt_world_revision: int = Field(ge=1)
    receipt_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class PerceptionResultContextItem(FrozenModel):
    """External perception evidence, explicitly not an accepted world fact."""

    kind: str = "external_perception_descriptor"
    epistemic_status: str = "provider_observation_not_world_fact"
    result_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    analysis_kind: str = Field(min_length=1)
    content_privacy_class: PrivacyClass
    accepted_at: datetime
    result_ref: str = Field(min_length=1)
    result_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=720)
    truncated: bool
    source: PerceptionResultContextSource


class PerceptionResultContextCompiler:
    """Expose only completed, related, hash-matching perception results."""

    def __init__(self, *, reader: PerceptionResultReader, max_characters: int = 720) -> None:
        if not 1 <= max_characters <= 720:
            raise ValueError("perception Context result budget is invalid")
        self._reader = reader
        self._max_characters = max_characters

    def compile(
        self,
        *,
        projection: LedgerProjection,
        cursor: ProjectionCursor,
        subject_refs: frozenset[str],
    ) -> tuple[PerceptionResultContextItem, ...]:
        if (
            projection.world_revision != cursor.world_revision
            or projection.deliberation_revision != cursor.deliberation_revision
            or projection.ledger_sequence != cursor.ledger_sequence
        ):
            raise ValueError("perception Context projection does not match pinned cursor")
        events = {item.event_id: item for item in projection.committed_world_event_refs}
        requests = {item.request_id: item for item in projection.perception_requests}
        terminal = {
            item.trigger_id
            for item in projection.trigger_processes
            if item.process_kind == "perception_result_deliberation"
            and item.state == "terminal"
            and item.runtime_outcome_ref == f"outcome:{item.trigger_id}:no-visible-action"
        }
        rows: list[PerceptionResultContextItem] = []
        for result in projection.perception_results:
            if (
                result.content_privacy_class == "withhold"
                or perception_result_trigger_id(
                    world_id=projection.world_id, result_id=result.result_id
                )
                not in terminal
            ):
                continue
            request = requests.get(result.request_id)
            result_event = events.get(result.accepted_event_ref)
            receipt_event = events.get(result.receipt_event_ref)
            source_event = events.get(request.source_event_ref) if request is not None else None
            source_observations = tuple(
                item
                for item in projection.message_observations
                if request is not None
                and item.world_revision == request.source_world_revision
                and item.event_payload_hash == request.source_payload_hash
            )
            if (
                request is None
                or result_event is None
                or receipt_event is None
                or source_event is None
                or len(source_observations) != 1
                or source_observations[0].actor not in subject_refs
                or result_event.event_type != "PerceptionResultAccepted"
                or receipt_event.event_type != "ExecutionReceiptRecorded"
                or receipt_event.payload_hash != result.receipt_event_payload_hash
                or source_event.event_type != "ObservationRecorded"
                or source_event.world_revision != request.source_world_revision
                or source_event.payload_hash != request.source_payload_hash
            ):
                continue
            content = self._reader.read_exact(result_ref=result.result_ref)
            if (
                content is None
                or content.result_hash != result.result_hash
                or "sha256:" + hashlib.sha256(content.text.encode()).hexdigest()
                != result.result_hash
            ):
                continue
            text = content.text[: self._max_characters]
            rows.append(
                PerceptionResultContextItem(
                    result_id=result.result_id,
                    request_id=result.request_id,
                    analysis_kind=result.analysis_kind,
                    content_privacy_class=result.content_privacy_class,
                    accepted_at=result.accepted_at,
                    result_ref=result.result_ref,
                    result_hash=result.result_hash,
                    text=text,
                    truncated=text != content.text,
                    source=PerceptionResultContextSource(
                        result_event_ref=result_event.event_id,
                        result_world_revision=result_event.world_revision,
                        result_payload_hash=result_event.payload_hash,
                        receipt_event_ref=receipt_event.event_id,
                        receipt_world_revision=receipt_event.world_revision,
                        receipt_payload_hash=receipt_event.payload_hash,
                    ),
                )
            )
        return tuple(sorted(rows, key=lambda item: (-item.accepted_at.timestamp(), item.result_id)))


__all__ = [
    "PerceptionResultContent",
    "PerceptionResultContextCompiler",
    "PerceptionResultContextItem",
    "PerceptionResultContextSource",
    "PerceptionResultReader",
]
