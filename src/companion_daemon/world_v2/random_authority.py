"""Recorded deterministic draws for soft social variation."""
from __future__ import annotations
import hashlib
import json
from datetime import datetime
from pydantic import Field, model_validator
from .event_identity import domain_idempotency_key
from .schema_core import FrozenModel
from .schemas import ProjectionCursor, WorldEvent

def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

class RandomDrawRecordedPayload(FrozenModel):
    draw_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    candidate_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    candidate_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_candidate_ref: str = Field(min_length=1, max_length=512)
    seed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=128)
    sampler_version: str = "random-authority.1"
    @model_validator(mode="after")
    def canonical(self) -> "RandomDrawRecordedPayload":
        if self.candidate_refs != tuple(sorted(set(self.candidate_refs))):
            raise ValueError("random draw candidates must be sorted and unique")
        if self.candidate_set_hash != _hash(self.candidate_refs):
            raise ValueError("random draw candidate set hash is invalid")
        if self.selected_candidate_ref not in self.candidate_refs:
            raise ValueError("random draw selected candidate is outside set")
        return self

class RandomAuthority:
    def __init__(self, *, ledger, source: str = "world-v2:random-authority") -> None: self._ledger, self._source = ledger, source
    def draw(self, *, attempt_id: str, candidate_refs: tuple[str, ...], catalog_version: str, logical_time: datetime, actor: str, trace_id: str, correlation_id: str) -> RandomDrawRecordedPayload:  # type: ignore[no-untyped-def]
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            raise ValueError("random draw requires current logical time")
        refs = tuple(sorted(set(candidate_refs)))
        if not refs:
            raise ValueError("random draw needs candidates")
        seed = _hash({"world": self._ledger.world_id, "time": logical_time.isoformat(), "attempt": attempt_id, "candidates": refs, "catalog": catalog_version})
        selected = refs[int(seed, 16) % len(refs)]
        payload = RandomDrawRecordedPayload(draw_id="draw:" + _hash({"attempt": attempt_id, "seed": seed}), attempt_id=attempt_id, candidate_refs=refs, candidate_set_hash=_hash(refs), selected_candidate_ref=selected, seed_hash=seed, catalog_version=catalog_version)
        event = WorldEvent.from_payload(schema_version="world-v2.1", event_id="event:random-draw:" + payload.draw_id, event_type="RandomDrawRecorded", world_id=self._ledger.world_id, logical_time=logical_time, created_at=logical_time, actor=actor, source=self._source, trace_id=trace_id, causation_id="event:random-attempt:" + attempt_id, correlation_id=correlation_id, idempotency_key=domain_idempotency_key(event_type="RandomDrawRecorded", world_id=self._ledger.world_id, payload=payload.model_dump(mode="json")) or "random:" + payload.draw_id, payload=payload.model_dump(mode="json"))
        existing = self._ledger.lookup_event_commit(event.event_id)
        if existing is not None:
            return RandomDrawRecordedPayload.model_validate_json(existing[0].payload_json)
        cursor = ProjectionCursor(world_revision=projection.world_revision, deliberation_revision=projection.deliberation_revision, ledger_sequence=projection.ledger_sequence)
        self._ledger.commit_at_cursor((event,), expected_cursor=cursor, commit_id="commit:random-draw:" + payload.draw_id)
        return payload

__all__ = ["RandomAuthority", "RandomDrawRecordedPayload"]
