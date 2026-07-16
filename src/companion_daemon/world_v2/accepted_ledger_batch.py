"""Opaque, recorder-issued batch capability for accepted-manifest v3 writes.

The data stored behind a handle is intentionally not a serializable plan.  A
ledger only receives it from the configured issuer, rechecks its exact cursor
and batch digest, and then applies its own transaction/CAS machinery.  This is
the capability boundary between a future ``AcceptedAtomicRecorder`` and every
ordinary ``commit`` caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
from weakref import WeakKeyDictionary

from .schemas import ProjectionCursor, WorldEvent
from .appraisal_acceptance_manifest import APPRAISAL_ACCEPTANCE_MANIFEST_VERSION
from .affect_acceptance_manifest import AFFECT_ACCEPTANCE_MANIFEST_VERSION
from .activity_lifecycle_acceptance_manifest import (
    ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
)
from .media_selection_acceptance_manifest import MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS
from .minimal_reply_manifest import MINIMAL_REPLY_MANIFEST_VERSION
from .outcome_acceptance_manifest import OUTCOME_ACCEPTANCE_MANIFEST_VERSION
from .expression_plan_manifest import EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION
from .interaction_bid_acceptance_manifest import INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION
from .media_thread_acceptance_manifest import MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION


class AcceptedLedgerBatchError(ValueError):
    """Stable failure at the accepted-ledger capability boundary."""


class AcceptedLedgerBatchHandle:
    """Issuer-owned, unforgeable-in-process reference to one accepted batch."""

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("accepted ledger batch handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("accepted ledger batch handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("accepted ledger batch handles cannot be copied")


@dataclass(frozen=True, slots=True)
class _AcceptedLedgerBatchMaterial:
    world_id: str
    expected_cursor: ProjectionCursor
    events: tuple[WorldEvent, ...]
    manifest_hash: str
    registry_digest: str
    commit_id: str
    batch_digest: str


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _event_material(event: WorldEvent) -> dict[str, object]:
    return event.model_dump(mode="json")


def _batch_digest(
    *,
    world_id: str,
    expected_cursor: ProjectionCursor,
    events: tuple[WorldEvent, ...],
    manifest_hash: str,
    registry_digest: str,
    commit_id: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "contract": "accepted-ledger-batch.1",
                "world_id": world_id,
                "expected_cursor": expected_cursor.model_dump(mode="json"),
                "events": tuple(_event_material(event) for event in events),
                "manifest_hash": manifest_hash,
                "registry_digest": registry_digest,
                "commit_id": commit_id,
            }
        ).encode("utf-8")
    ).hexdigest()


class AcceptedLedgerBatchIssuer:
    """One recorder-owned issuer for opaque v3 accepted batches.

    Construct this in the composition root and give the instance only to the
    matching recorder and ledger.  Possessing a DTO, or a handle issued by a
    different issuer, is never sufficient to write an accepted batch.
    """

    __slots__ = ("__handles",)

    def __init__(self) -> None:
        self.__handles: WeakKeyDictionary[
            AcceptedLedgerBatchHandle, _AcceptedLedgerBatchMaterial
        ] = WeakKeyDictionary()

    def issue(
        self,
        *,
        world_id: str,
        expected_cursor: ProjectionCursor,
        events: Sequence[WorldEvent],
        manifest_hash: str,
        registry_digest: str,
        commit_id: str,
    ) -> AcceptedLedgerBatchHandle:
        if type(world_id) is not str or not world_id:
            raise AcceptedLedgerBatchError("accepted batch world id is invalid")
        if type(expected_cursor) is not ProjectionCursor:
            raise AcceptedLedgerBatchError("accepted batch cursor must use its exact contract")
        if type(events) not in {tuple, list}:
            raise AcceptedLedgerBatchError("accepted batch events must be a concrete sequence")
        materialized = tuple(events)
        if len(materialized) < 2:
            raise AcceptedLedgerBatchError("accepted batch requires AcceptanceRecorded and effects")
        if any(type(event) is not WorldEvent for event in materialized):
            raise AcceptedLedgerBatchError("accepted batch events must use exact WorldEvent values")
        if any(event.world_id != world_id for event in materialized):
            raise AcceptedLedgerBatchError("accepted batch contains another world")
        acceptance = materialized[0]
        if acceptance.event_type != "AcceptanceRecorded" or acceptance.payload().get(
            "manifest_version"
        ) not in {
            "acceptance-manifest.3",
            MINIMAL_REPLY_MANIFEST_VERSION,
            APPRAISAL_ACCEPTANCE_MANIFEST_VERSION,
            AFFECT_ACCEPTANCE_MANIFEST_VERSION,
            ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
            *MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS,
            OUTCOME_ACCEPTANCE_MANIFEST_VERSION,
            EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION,
            INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION,
            MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION,
        }:
            raise AcceptedLedgerBatchError("accepted batch must begin with an accepted manifest")
        for name, value in {
            "manifest_hash": manifest_hash,
            "registry_digest": registry_digest,
        }.items():
            if (
                type(value) is not str
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise AcceptedLedgerBatchError(f"accepted batch {name} is invalid")
        if type(commit_id) is not str or not commit_id:
            raise AcceptedLedgerBatchError("accepted batch commit id is invalid")
        batch_digest = _batch_digest(
            world_id=world_id,
            expected_cursor=expected_cursor,
            events=materialized,
            manifest_hash=manifest_hash,
            registry_digest=registry_digest,
            commit_id=commit_id,
        )
        handle = AcceptedLedgerBatchHandle()
        self.__handles[handle] = _AcceptedLedgerBatchMaterial(
            world_id=world_id,
            expected_cursor=expected_cursor,
            events=materialized,
            manifest_hash=manifest_hash,
            registry_digest=registry_digest,
            commit_id=commit_id,
            batch_digest=batch_digest,
        )
        return handle

    def verify(
        self,
        *,
        handle: AcceptedLedgerBatchHandle,
        world_id: str,
        expected_cursor: ProjectionCursor,
    ) -> tuple[tuple[WorldEvent, ...], str]:
        if type(handle) is not AcceptedLedgerBatchHandle:
            raise AcceptedLedgerBatchError("accepted batch handle belongs to another issuer")
        if type(expected_cursor) is not ProjectionCursor:
            raise AcceptedLedgerBatchError("accepted batch cursor must use its exact contract")
        material = self.__handles.get(handle)
        if material is None:
            raise AcceptedLedgerBatchError("accepted batch handle belongs to another issuer")
        if material.world_id != world_id or material.expected_cursor != expected_cursor:
            raise AcceptedLedgerBatchError("accepted batch does not match its ledger authority")
        digest = _batch_digest(
            world_id=material.world_id,
            expected_cursor=material.expected_cursor,
            events=material.events,
            manifest_hash=material.manifest_hash,
            registry_digest=material.registry_digest,
            commit_id=material.commit_id,
        )
        if digest != material.batch_digest:
            raise AcceptedLedgerBatchError("accepted batch material no longer matches its digest")
        return material.events, material.commit_id


__all__ = [
    "AcceptedLedgerBatchError",
    "AcceptedLedgerBatchHandle",
    "AcceptedLedgerBatchIssuer",
]
