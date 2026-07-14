"""Trusted resolver boundary for internal Context Capsule compilation.

The capability below is an in-process architecture marker, not a signature or a
cryptographic credential.  Only composition-root resolver implementations should
subclass :class:`TrustedInternalContextResolver`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .schemas import LedgerProjection, ProjectionCursor


class ContextCompileQuery(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    world_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_ref: str = Field(min_length=1, max_length=256)
    consumer_scope: Literal["deliberation_internal"] = "deliberation_internal"
    trigger_ref: str = Field(min_length=1, max_length=256)
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    logical_time: datetime | None = None

    @field_validator("logical_time")
    @classmethod
    def logical_time_is_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Context compile logical time must be timezone-aware")
        return value

    @property
    def cursor(self) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=self.world_revision,
            deliberation_revision=self.deliberation_revision,
            ledger_sequence=self.ledger_sequence,
        )


def projection_snapshot_id(projection: LedgerProjection) -> str:
    """Canonical identity for the exact ledger cursor used by Context resolution."""

    cursor_hash = hashlib.sha256(
        json.dumps(
            {
                "world_id": projection.world_id,
                "world_revision": projection.world_revision,
                "deliberation_revision": projection.deliberation_revision,
                "ledger_sequence": projection.ledger_sequence,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"projection:{cursor_hash}"


def query_from_projection(
    projection: LedgerProjection, *, actor_ref: str, trigger_ref: str
) -> ContextCompileQuery:
    """Create a compile query pinned to one already-materialized projection."""

    return ContextCompileQuery(
        world_id=projection.world_id,
        snapshot_id=projection_snapshot_id(projection),
        snapshot_hash=projection.semantic_hash,
        actor_ref=actor_ref,
        trigger_ref=trigger_ref,
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
        logical_time=projection.logical_time,
    )


def context_query_hash(query: ContextCompileQuery) -> str:
    material = json.dumps(
        query.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest()


_CAPABILITY_SENTINEL = object()


class InternalResolverCapability:
    """Opaque process-local marker issued only by the trusted resolver base."""

    __slots__ = ("_resolver", "_sentinel")

    def __init__(self, resolver: object, sentinel: object) -> None:
        if sentinel is not _CAPABILITY_SENTINEL:
            raise TypeError("Internal resolver capabilities are composition-root issued")
        self._resolver = resolver
        self._sentinel = sentinel


class ResolvedContextResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True, extra="forbid")

    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capability: InternalResolverCapability
    resolved_context: object


class TrustedInternalContextResolver(ABC):
    """Composition-root base for resolvers backed by verified projections/ledgers."""

    def __init__(self) -> None:
        self.__capability = InternalResolverCapability(self, _CAPABILITY_SENTINEL)

    @property
    def capability(self) -> InternalResolverCapability:
        return self.__capability

    @abstractmethod
    def resolve(self, query: ContextCompileQuery) -> ResolvedContextResult:
        raise NotImplementedError


def resolver_capability_is_valid(resolver: object, capability: InternalResolverCapability) -> bool:
    return (
        isinstance(resolver, TrustedInternalContextResolver)
        and capability is resolver.capability
        and capability._resolver is resolver
        and capability._sentinel is _CAPABILITY_SENTINEL
    )
