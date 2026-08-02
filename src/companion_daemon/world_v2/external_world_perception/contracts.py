"""Stable Phase-1 contracts for external-world signal acquisition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ..schema_core import FrozenModel


MAX_EVIDENCE_BYTES = 2_000_000
BoundedSignalText = Annotated[str, Field(min_length=1, max_length=256)]


class SourceCursor(FrozenModel):
    """Opaque provider checkpoint; the Hub never interprets its value."""

    opaque_value: str = Field(min_length=1, max_length=4_096)


class ExternalSignalPlace(FrozenModel):
    """Source-reported place scope; never inferred from private user location."""

    geometry_kind: Literal["point", "region_ref"]
    source_place_ref: str = Field(min_length=1, max_length=1_024)
    label: str | None = Field(default=None, max_length=512)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_meters: int | None = Field(default=None, gt=0, le=1_000_000)
    source_provided_certainty: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def geometry_matches_kind(self) -> ExternalSignalPlace:
        has_coordinates = self.latitude is not None or self.longitude is not None
        if self.geometry_kind == "point":
            if self.latitude is None or self.longitude is None:
                raise ValueError("source point place requires both coordinates")
        elif has_coordinates or self.radius_meters is not None:
            raise ValueError("source region reference cannot smuggle point geometry")
        return self


class ExternalSignalSourceItem(FrozenModel):
    """One source-reported item before the Hub assigns an immutable revision."""

    upstream_item_id: str = Field(min_length=1, max_length=1_024)
    gateway_ref: str = Field(min_length=1, max_length=1_024)
    upstream_publisher_ref: str = Field(min_length=1, max_length=1_024)
    signal_kind: str = Field(min_length=1, max_length=128)
    headline: str = Field(min_length=1, max_length=1_000)
    licensed_summary: str = Field(default="", max_length=8_000)
    canonical_url: str | None = Field(default=None, max_length=4_096)
    occurred_at: datetime | None = None
    published_at: datetime
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    correction_of_upstream_item_id: str | None = Field(default=None, max_length=1_024)
    entities: tuple[BoundedSignalText, ...] = Field(default=(), max_length=64)
    source_provided_certainty: str | None = Field(default=None, max_length=256)
    place_scope: ExternalSignalPlace | None = None

    @model_validator(mode="after")
    def temporal_order_is_coherent(self) -> ExternalSignalSourceItem:
        for value in (
            self.occurred_at,
            self.published_at,
            self.updated_at,
            self.expires_at,
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("source item timestamps must be timezone-aware")
        if self.updated_at is not None and self.updated_at < self.published_at:
            raise ValueError("source item update cannot predate publication")
        if self.expires_at is not None and self.expires_at <= self.published_at:
            raise ValueError("source item expiry must follow publication")
        if self.correction_of_upstream_item_id == self.upstream_item_id:
            raise ValueError("source item cannot correct itself")
        if self.canonical_url is not None:
            parsed_url = urlsplit(self.canonical_url)
            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
            ):
                raise ValueError("source canonical URL is unsafe")
        return self


class ExternalSignalSourcePage(FrozenModel):
    """One atomically persisted source response and its normalized item reports."""

    evidence_media_type: str = Field(min_length=1, max_length=256)
    evidence_bytes: bytes
    next_cursor: SourceCursor | None = None
    items: tuple[ExternalSignalSourceItem, ...] = Field(default=(), max_length=500)
    parser_rejected_item_count: int = Field(default=0, ge=0, le=500)
    parser_failure_codes: tuple[BoundedSignalText, ...] = Field(default=(), max_length=32)
    not_modified: bool = False

    @model_validator(mode="after")
    def evidence_shape_is_bounded(self) -> ExternalSignalSourcePage:
        if len(self.evidence_bytes) > MAX_EVIDENCE_BYTES:
            raise ValueError("external signal evidence exceeds the byte limit")
        if self.not_modified and (
            self.evidence_bytes
            or self.items
            or self.parser_rejected_item_count
            or self.parser_failure_codes
        ):
            raise ValueError("not-modified source page cannot carry evidence or items")
        if not self.not_modified and not self.evidence_bytes:
            raise ValueError("source page requires exact evidence bytes")
        if bool(self.parser_rejected_item_count) != bool(self.parser_failure_codes):
            raise ValueError("parser rejection count and failure codes must agree")
        return self


class ExternalSignalSourcePort(Protocol):
    """True-external source seam used by pull and recorded adapters."""

    source_id: str

    async def fetch(
        self,
        *,
        after: SourceCursor | None,
        observed_at: datetime,
        deadline_at: datetime,
        limit: int,
    ) -> ExternalSignalSourcePage: ...


class ExternalSignalEmbedding(Protocol):
    """Independent background embedding seam; it has no chat-runtime lease."""

    version: str
    dimensions: int

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


class ExternalSignalSourceFailure(RuntimeError):
    """Typed technical source failure; never equivalent to no new evidence."""

    def __init__(
        self,
        failure_code: str,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        if not failure_code or len(failure_code) > 128:
            raise ValueError("source failure code is invalid")
        if retry_after_seconds is not None and retry_after_seconds <= 0:
            raise ValueError("source retry-after must be positive")
        super().__init__(failure_code)
        self.failure_code = failure_code
        self.retry_after_seconds = retry_after_seconds


class SourcePolicyRevision(FrozenModel):
    """Audited usage rights pinned to every acquired source revision."""

    policy_revision: str = Field(min_length=1, max_length=256)
    may_fetch: bool
    may_cache_raw: bool
    may_store_normalized_summary: bool
    may_embed: bool
    may_expose_to_character_model: bool
    may_quote: bool
    may_freeze_durable_snapshot: bool
    maximum_raw_retention_seconds: int = Field(gt=0)
    maximum_signal_retention_seconds: int = Field(gt=0)
    maximum_normalized_retention_seconds: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class SourceProfile:
    """Deployment-owned acquisition and retention limits for one source Adapter."""

    adapter: ExternalSignalSourcePort
    policy: SourcePolicyRevision
    poll_interval_seconds: int
    signal_ttl_seconds: int
    raw_retention_seconds: int
    normalized_retention_seconds: int | None = None
    fetch_deadline_seconds: int = 20
    page_limit: int = 200

    def __post_init__(self) -> None:
        if not self.adapter.source_id or len(self.adapter.source_id) > 512:
            raise ValueError("external signal source id is invalid")
        for value, label in (
            (self.poll_interval_seconds, "poll interval"),
            (self.signal_ttl_seconds, "signal TTL"),
            (self.raw_retention_seconds, "raw retention"),
            (self.fetch_deadline_seconds, "fetch deadline"),
            (self.page_limit, "page limit"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"external signal {label} must be positive")
        if self.normalized_retention_seconds is not None and (
            not isinstance(self.normalized_retention_seconds, int)
            or isinstance(self.normalized_retention_seconds, bool)
            or self.normalized_retention_seconds <= 0
        ):
            raise ValueError("external signal normalized retention must be positive")
        if self.page_limit > 500:
            raise ValueError("external signal page limit exceeds the contract")
        if not self.policy.may_cache_raw:
            raise ValueError("source policy does not allow raw evidence caching")
        if not self.policy.may_fetch:
            raise ValueError("source policy does not allow acquisition")
        if self.raw_retention_seconds > self.policy.maximum_raw_retention_seconds:
            raise ValueError("external signal raw retention exceeds source policy")
        if self.signal_ttl_seconds > self.policy.maximum_signal_retention_seconds:
            raise ValueError("external signal TTL exceeds source policy")
        if (
            self.effective_normalized_retention_seconds
            > self.policy.maximum_normalized_retention_seconds
        ):
            raise ValueError("external signal normalized retention exceeds source policy")

    @property
    def effective_normalized_retention_seconds(self) -> int:
        return self.normalized_retention_seconds or self.signal_ttl_seconds


class PerceptionAdvanceResult(FrozenModel):
    status: Literal[
        "idle",
        "progressed",
        "window_wait",
        "attention_no_selection",
        "shadow_selected",
        "perception_committed",
        "retry_wait",
        "joined_existing",
        "deferred_visible_turn",
    ]
    progressed_units: int = Field(ge=0, le=1)
    committed_perception_count: int = Field(default=0, ge=0)
    next_wake_at: datetime | None = None
    more_due: bool = False

    @model_validator(mode="after")
    def commit_count_matches_the_authority_result(self) -> PerceptionAdvanceResult:
        if self.status == "perception_committed":
            if self.committed_perception_count <= 0:
                raise ValueError("perception commit result requires a positive commit count")
        elif self.committed_perception_count != 0:
            raise ValueError("non-commit perception result cannot report a commit")
        return self


class SourceHealthSnapshot(FrozenModel):
    source_id: str = Field(min_length=1)
    policy_revision: str = Field(min_length=1)
    state: Literal["never_polled", "healthy", "retry_wait", "stale", "malformed"]
    last_result: Literal[
        "never_polled",
        "new_revisions",
        "new_revisions_with_rejections",
        "duplicates_only",
        "no_new_signal",
        "not_modified",
        "malformed",
        "technical_failure",
    ]
    last_cursor: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    next_refresh_at: datetime | None = None
    consecutive_failures: int = Field(ge=0)
    last_failure_code: str | None = None
    accepted_revision_count: int = Field(ge=0)
    duplicate_suppressed_count: int = Field(ge=0)
    rejected_item_count: int = Field(ge=0)
    last_page_rejected_item_count: int = Field(ge=0)


class PerceptionHealthSnapshot(FrozenModel):
    state: Literal["healthy", "degraded", "warning"]
    as_of: datetime
    source_states: tuple[SourceHealthSnapshot, ...]
    signal_revision_count: int = Field(ge=0)
    superseded_revision_count: int = Field(ge=0)
    correction_edge_count: int = Field(ge=0)
    cluster_count: int = Field(ge=0)
    rejected_item_count: int = Field(ge=0)
    search_indexed_revision_count: int = Field(ge=0)
    fts_state: Literal["healthy"]
    embedding_state: Literal["not_configured", "healthy", "degraded"]
    embedding_version: str | None = None
    embedding_indexed_revision_count: int = Field(ge=0)
    embedding_pending_count: int = Field(ge=0)
    last_embedding_failure_code: str | None = None
    signal_revisions_last_24h: int = Field(ge=0)
    sidecar_main_bytes: int = Field(ge=0)
    sidecar_wal_bytes: int = Field(ge=0)
    sidecar_growth_24h_bytes: int = Field(ge=0)
    active_signal_count: int = Field(ge=0)
    expired_signal_count: int = Field(ge=0)
    raw_evidence_count: int = Field(ge=0)
    raw_evidence_bytes: int = Field(ge=0)
    duplicate_suppressed_count: int = Field(ge=0)
    warning_reasons: tuple[str, ...] = ()


class RecordedSignalSourceAdapter:
    """Deterministic recorded source used by interface and replay acceptance tests."""

    def __init__(
        self,
        *,
        source_id: str,
        pages: tuple[ExternalSignalSourcePage | ExternalSignalSourceFailure, ...],
    ) -> None:
        if not source_id:
            raise ValueError("recorded source requires source_id")
        self.source_id = source_id
        self._pages = pages
        self._ordinal = 0
        self._observed_cursors: list[str | None] = []

    @property
    def observed_cursors(self) -> tuple[str | None, ...]:
        return tuple(self._observed_cursors)

    async def fetch(
        self,
        *,
        after: SourceCursor | None,
        observed_at: datetime,
        deadline_at: datetime,
        limit: int,
    ) -> ExternalSignalSourcePage:
        del limit
        self._observed_cursors.append(after.opaque_value if after is not None else None)
        if deadline_at <= observed_at:
            raise ExternalSignalSourceFailure("source_deadline_elapsed")
        if self._ordinal >= len(self._pages):
            return ExternalSignalSourcePage(
                evidence_media_type="application/octet-stream",
                evidence_bytes=b"",
                next_cursor=None,
                not_modified=True,
            )
        page = self._pages[self._ordinal]
        self._ordinal += 1
        if isinstance(page, ExternalSignalSourceFailure):
            raise page
        return page


WallClock = Callable[[], datetime]


class WorldPerceptionHub(Protocol):
    """Deep scheduler seam; ordinary callers never coordinate internal stages."""

    async def advance_once(self, *, observed_at: datetime) -> PerceptionAdvanceResult: ...

    def health_snapshot(self) -> PerceptionHealthSnapshot: ...

    async def aclose(self) -> None: ...


__all__ = [
    "ExternalSignalSourceFailure",
    "ExternalSignalEmbedding",
    "ExternalSignalPlace",
    "ExternalSignalSourceItem",
    "ExternalSignalSourcePage",
    "ExternalSignalSourcePort",
    "MAX_EVIDENCE_BYTES",
    "PerceptionAdvanceResult",
    "PerceptionHealthSnapshot",
    "RecordedSignalSourceAdapter",
    "SourceCursor",
    "SourceHealthSnapshot",
    "SourcePolicyRevision",
    "SourceProfile",
    "WallClock",
    "WorldPerceptionHub",
]
