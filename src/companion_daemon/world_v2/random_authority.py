"""Recorded deterministic draws for soft social variation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .event_identity import domain_idempotency_key
from .schema_core import FrozenModel
from .schemas import ProjectionCursor, WorldEvent


_WEIGHT_SCALE = 1_000_000


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class NormalizedCandidateWeight(FrozenModel):
    candidate_ref: str = Field(min_length=1, max_length=512)
    weight_ppm: int = Field(ge=0, le=_WEIGHT_SCALE)


def _weight_vector_hash(vector: tuple[NormalizedCandidateWeight, ...]) -> str:
    return _hash(tuple(item.model_dump(mode="json") for item in vector))


def _normalize_weights(
    refs: tuple[str, ...], weights: Mapping[str, int]
) -> tuple[NormalizedCandidateWeight, ...]:
    if set(weights) != set(refs):
        raise ValueError("random draw weights must cover the exact candidate set")
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in weights.values()):
        raise ValueError("random draw weights must be non-negative integers")
    total = sum(weights.values())
    if total <= 0:
        raise ValueError("random draw weights need positive total mass")
    floor = {ref: weights[ref] * _WEIGHT_SCALE // total for ref in refs}
    remaining = _WEIGHT_SCALE - sum(floor.values())
    order = sorted(
        refs,
        key=lambda ref: (-(weights[ref] * _WEIGHT_SCALE % total), ref),
    )
    for ref in order[:remaining]:
        floor[ref] += 1
    return tuple(
        NormalizedCandidateWeight(candidate_ref=ref, weight_ppm=floor[ref])
        for ref in refs
    )


class RandomDrawRecordedPayload(FrozenModel):
    draw_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    candidate_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    candidate_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_candidate_ref: str = Field(min_length=1, max_length=512)
    seed_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=128)
    sampler_version: Literal["random-authority.1", "random-authority.2"] = (
        "random-authority.1"
    )
    weight_policy_version: str | None = Field(default=None, min_length=1, max_length=128)
    weight_vector: tuple[NormalizedCandidateWeight, ...] = ()
    weight_vector_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def canonical(self) -> "RandomDrawRecordedPayload":
        if self.candidate_refs != tuple(sorted(set(self.candidate_refs))):
            raise ValueError("random draw candidates must be sorted and unique")
        if self.candidate_set_hash != _hash(self.candidate_refs):
            raise ValueError("random draw candidate set hash is invalid")
        if self.selected_candidate_ref not in self.candidate_refs:
            raise ValueError("random draw selected candidate is outside set")
        if self.sampler_version == "random-authority.1":
            if (
                self.weight_policy_version is not None
                or self.weight_vector
                or self.weight_vector_hash is not None
            ):
                raise ValueError("random-authority.1 cannot carry v2 weight authority")
            return self
        if not self.weight_policy_version:
            raise ValueError("weighted random draw requires a policy version")
        if tuple(item.candidate_ref for item in self.weight_vector) != self.candidate_refs:
            raise ValueError("random draw weight vector must bind the exact candidate order")
        if sum(item.weight_ppm for item in self.weight_vector) != _WEIGHT_SCALE:
            raise ValueError("random draw normalized weights must sum to one million")
        if self.weight_vector_hash != _weight_vector_hash(self.weight_vector):
            raise ValueError("random draw weight vector hash is invalid")
        return self


class RandomAuthority:
    def __init__(self, *, ledger, source: str = "world-v2:random-authority") -> None:
        self._ledger = ledger
        self._source = source

    def draw(
        self,
        *,
        attempt_id: str,
        candidate_refs: tuple[str, ...],
        catalog_version: str,
        logical_time: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
        seed_instant: datetime | None = None,
        candidate_weights: Mapping[str, int] | None = None,
        weight_policy_version: str | None = None,
    ) -> RandomDrawRecordedPayload:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            raise ValueError("random draw requires current logical time")
        refs = tuple(sorted(set(candidate_refs)))
        if not refs:
            raise ValueError("random draw needs candidates")
        deterministic_instant = seed_instant or logical_time
        if deterministic_instant.tzinfo is None or deterministic_instant.utcoffset() is None:
            raise ValueError("random draw seed instant must be timezone-aware")
        if (candidate_weights is None) != (weight_policy_version is None):
            raise ValueError("weighted random draw requires weights and policy version together")
        vector = (
            _normalize_weights(refs, candidate_weights)
            if candidate_weights is not None
            else ()
        )
        vector_hash = _weight_vector_hash(vector) if vector else None
        sampler_version = "random-authority.2" if vector else "random-authority.1"
        legacy_seed_material = {
            "world": self._ledger.world_id,
            "time": deterministic_instant.isoformat(),
            "attempt": attempt_id,
            "candidates": refs,
            "catalog": catalog_version,
        }
        if vector:
            legacy_seed = _hash(legacy_seed_material)
            legacy_draw_id = "draw:" + _hash({
                "attempt": attempt_id, "seed": legacy_seed
            })
            legacy_event = self._ledger.lookup_event_commit(
                "event:random-draw:" + legacy_draw_id
            )
            if legacy_event is not None:
                return RandomDrawRecordedPayload.model_validate_json(
                    legacy_event[0].payload_json
                )
        seed_material = (
            {
                "world": self._ledger.world_id,
                "time": deterministic_instant.isoformat(),
                "attempt": attempt_id,
                "candidates": refs,
                "catalog": catalog_version,
                "sampler_version": sampler_version,
                "weight_policy_version": weight_policy_version,
                "weight_vector_hash": vector_hash,
            }
            if vector
            else legacy_seed_material
        )
        seed = _hash(seed_material)
        if vector:
            ticket = int(seed, 16) % _WEIGHT_SCALE
            cumulative = 0
            selected = refs[-1]
            for item in vector:
                cumulative += item.weight_ppm
                if ticket < cumulative:
                    selected = item.candidate_ref
                    break
        else:
            selected = refs[int(seed, 16) % len(refs)]
        payload = RandomDrawRecordedPayload(
            draw_id="draw:" + _hash({"attempt": attempt_id, "seed": seed}),
            attempt_id=attempt_id,
            candidate_refs=refs,
            candidate_set_hash=_hash(refs),
            selected_candidate_ref=selected,
            seed_hash=seed,
            catalog_version=catalog_version,
            sampler_version=sampler_version,
            weight_policy_version=weight_policy_version,
            weight_vector=vector,
            weight_vector_hash=vector_hash,
        )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:random-draw:" + payload.draw_id,
            event_type="RandomDrawRecorded",
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=logical_time,
            actor=actor,
            source=self._source,
            trace_id=trace_id,
            causation_id="event:random-attempt:" + attempt_id,
            correlation_id=correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="RandomDrawRecorded",
                    world_id=self._ledger.world_id,
                    payload=payload.model_dump(mode="json"),
                )
                or "random:" + payload.draw_id
            ),
            payload=payload.model_dump(mode="json"),
        )
        existing = self._ledger.lookup_event_commit(event.event_id)
        if existing is not None:
            return RandomDrawRecordedPayload.model_validate_json(existing[0].payload_json)
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=cursor,
            commit_id="commit:random-draw:" + payload.draw_id,
        )
        return payload


__all__ = [
    "NormalizedCandidateWeight",
    "RandomAuthority",
    "RandomDrawRecordedPayload",
]
