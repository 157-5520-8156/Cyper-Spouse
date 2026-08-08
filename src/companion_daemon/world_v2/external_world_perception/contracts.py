"""Stable Phase-1 contracts for external-world signal acquisition."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ..external_perception_events import (
    ExternalPerceptionLiveDelivery,
    FrozenExternalSignalSnapshot,
)
from ..proposal_audit_schemas import ModelResultRecordedPayload
from ..schema_core import FrozenModel
from ..schemas import ProjectionCursor


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
    # Some ranking feeds expose only the gateway observation time. Keeping
    # this nullable prevents the adapter from forging a publication time;
    # downstream evidence carries the independently recorded observation time.
    published_at: datetime | None = None
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
        if (
            self.updated_at is not None
            and self.published_at is not None
            and self.updated_at < self.published_at
        ):
            raise ValueError("source item update cannot predate publication")
        if (
            self.expires_at is not None
            and self.published_at is not None
            and self.expires_at <= self.published_at
        ):
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


class SourceBoundAttentionContextItem(FrozenModel):
    """One current character-context item with exact World-side provenance."""

    context_ref: str = Field(min_length=1, max_length=1_024)
    context_kind: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4_096)
    source_refs: tuple[BoundedSignalText, ...] = Field(min_length=1, max_length=32)


class PerceptionChannelProof(FrozenModel):
    """A current capability proving access, never attention or prior use."""

    channel_ref: str = Field(min_length=1, max_length=1_024)
    channel_kind: str = Field(min_length=1, max_length=128)
    evidence_refs: tuple[BoundedSignalText, ...] = Field(min_length=1, max_length=32)
    accessible_source_ids: tuple[BoundedSignalText, ...] = Field(min_length=1, max_length=64)
    valid_until: datetime


class CharacterAttentionContext(FrozenModel):
    """Source-bound current context frozen by a World-facing read-only port."""

    world_id: str = Field(min_length=1, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=512)
    pinned_world_cursor: str = Field(min_length=1, max_length=1_024)
    world_logical_time: datetime
    situation: tuple[SourceBoundAttentionContextItem, ...] = Field(max_length=32)
    relevant_context: tuple[SourceBoundAttentionContextItem, ...] = Field(max_length=64)
    available_channels: tuple[PerceptionChannelProof, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def refs_are_unambiguous(self) -> CharacterAttentionContext:
        context_refs = tuple(item.context_ref for item in (*self.situation, *self.relevant_context))
        channel_refs = tuple(item.channel_ref for item in self.available_channels)
        if len(context_refs) != len(set(context_refs)):
            raise ValueError("attention context refs must be unique")
        if len(channel_refs) != len(set(channel_refs)):
            raise ValueError("perception channel refs must be unique")
        if self.world_logical_time.tzinfo is None or self.world_logical_time.utcoffset() is None:
            raise ValueError("attention context logical time must be timezone-aware")
        return self


class LicensedEvidenceView(FrozenModel):
    """Exact licensed, untrusted source material shown as data to the model."""

    signal_revision_ref: str = Field(min_length=1, max_length=1_024)
    content_trust: Literal["untrusted_external_evidence"] = "untrusted_external_evidence"
    source_id: str = Field(min_length=1, max_length=512)
    upstream_publisher_ref: str = Field(min_length=1, max_length=1_024)
    signal_kind: str = Field(min_length=1, max_length=128)
    headline: str = Field(min_length=1, max_length=1_000)
    licensed_summary: str = Field(default="", max_length=8_000)
    canonical_url: str | None = Field(default=None, max_length=4_096)
    published_at: datetime | None = None
    observed_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime
    source_provided_certainty: str | None = Field(default=None, max_length=256)
    place_scope: ExternalSignalPlace | None = None

    @model_validator(mode="after")
    def evidence_time_is_explicit(self) -> LicensedEvidenceView:
        if self.published_at is None and self.observed_at is None:
            raise ValueError("external evidence needs publication or observation time")
        for value in (self.published_at, self.observed_at, self.updated_at, self.expires_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("external evidence times must be timezone-aware")
        return self


class CorrectionEdge(FrozenModel):
    correction_revision_ref: str = Field(min_length=1, max_length=1_024)
    corrected_revision_ref: str = Field(min_length=1, max_length=1_024)


class SourceDisagreement(FrozenModel):
    """Structural conflict evidence; it deliberately carries no system verdict."""

    signal_revision_refs: tuple[BoundedSignalText, ...] = Field(min_length=2, max_length=32)
    differing_fields: tuple[Literal["headline", "licensed_summary", "certainty"], ...] = Field(
        min_length=1,
        max_length=3,
    )


class PerceptionDossier(FrozenModel):
    candidate_ref: str = Field(min_length=1, max_length=1_024)
    exact_signal_revisions: tuple[BoundedSignalText, ...] = Field(min_length=1, max_length=32)
    corrections: tuple[CorrectionEdge, ...] = Field(default=(), max_length=32)
    source_disagreements: tuple[SourceDisagreement, ...] = Field(default=(), max_length=16)
    accessible_channels: tuple[PerceptionChannelProof, ...] = Field(min_length=1, max_length=32)
    model_visible_material: tuple[LicensedEvidenceView, ...] = Field(min_length=1, max_length=32)
    evidence_digest: str = Field(min_length=64, max_length=64)


class PerceptionWindow(FrozenModel):
    """Frozen source packet offered to one shadow character-attention attempt."""

    window_id: str = Field(min_length=1, max_length=256)
    attention_attempt_id: str = Field(min_length=1, max_length=256)
    opportunity_id: str = Field(min_length=1, max_length=256)
    world_id: str = Field(min_length=1, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=512)
    pinned_world_cursor: str = Field(min_length=1, max_length=1_024)
    attention_policy_revision: str = Field(min_length=1, max_length=256)
    deployment_mode: Literal["shadow"] = "shadow"
    deployment_mode_revision: str = Field(min_length=1, max_length=256)
    generated_at: datetime
    expires_at: datetime
    candidates: tuple[PerceptionDossier, ...] = Field(min_length=1, max_length=16)
    candidate_set_hash: str = Field(min_length=64, max_length=64)
    exposure_draw_ref: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def window_identity_is_coherent(self) -> PerceptionWindow:
        if self.expires_at <= self.generated_at:
            raise ValueError("perception window expiry must follow generation")
        candidate_refs = tuple(item.candidate_ref for item in self.candidates)
        if len(candidate_refs) != len(set(candidate_refs)):
            raise ValueError("perception window candidate refs must be unique")
        return self


class CharacterAttentionSelection(FrozenModel):
    candidate_ref: str = Field(min_length=1, max_length=1_024)
    exact_signal_revision_refs: tuple[BoundedSignalText, ...] = Field(
        min_length=1,
        max_length=32,
    )
    selected_channel_ref: str = Field(min_length=1, max_length=1_024)
    subjective_summary: str = Field(min_length=1, max_length=8_000)
    epistemic_notes: str = Field(default="", max_length=4_000)
    attended_context_refs: tuple[BoundedSignalText, ...] = Field(default=(), max_length=64)


class CharacterAttentionResult(FrozenModel):
    """Character-authored attention; an empty tuple is an explicit model choice."""

    selections: tuple[CharacterAttentionSelection, ...] = Field(max_length=16)


class CharacterAttentionRequest(FrozenModel):
    attention_attempt_id: str = Field(min_length=1, max_length=256)
    retry_ordinal: int = Field(ge=0)
    selection_ordinal: Literal[0, 1]
    window: PerceptionWindow
    current_context: CharacterAttentionContext
    validation_failure_codes: tuple[BoundedSignalText, ...] = Field(default=(), max_length=32)
    rejected_result_json: str | None = Field(default=None, max_length=65_536)

    @model_validator(mode="after")
    def reselection_fields_are_coherent(self) -> CharacterAttentionRequest:
        if self.attention_attempt_id != self.window.attention_attempt_id:
            raise ValueError("attention request and window attempt ids differ")
        if self.selection_ordinal == 0 and (
            self.validation_failure_codes or self.rejected_result_json is not None
        ):
            raise ValueError("primary attention request cannot carry reselection feedback")
        if self.selection_ordinal == 1 and not self.validation_failure_codes:
            raise ValueError("attention reselection requires exact validation failures")
        return self


class CharacterAttentionContextPort(Protocol):
    async def freeze_attention_context(
        self,
        *,
        world_id: str,
        actor_ref: str,
        observed_at: datetime,
    ) -> CharacterAttentionContext: ...


class CharacterAttentionModelPort(Protocol):
    model_id: str

    async def consider_attention(self, request: CharacterAttentionRequest) -> object: ...


class CharacterAttentionTechnicalFailure(RuntimeError):
    """Typed provider failure; never translated into a character choosing none."""

    def __init__(self, failure_code: str) -> None:
        if not failure_code or len(failure_code) > 128:
            raise ValueError("character attention failure code is invalid")
        super().__init__(failure_code)
        self.failure_code = failure_code


@dataclass(frozen=True, slots=True)
class ShadowAttentionRuntime:
    """Explicit shadow-only capability bundle; it has no World writer."""

    world_id: str
    actor_ref: str
    attention_policy_revision: str
    deployment_mode_revision: str
    worker_id: str
    context_port: CharacterAttentionContextPort
    model: CharacterAttentionModelPort
    merge_wait_seconds: int = 120
    window_ttl_seconds: int = 21_600
    lease_seconds: int = 300
    model_timeout_seconds: int = 120
    max_candidate_dossiers: int = 12
    attempt_retention_seconds: int = 604_800

    def __post_init__(self) -> None:
        for value, label in (
            (self.world_id, "world id"),
            (self.actor_ref, "actor ref"),
            (self.attention_policy_revision, "attention policy revision"),
            (self.deployment_mode_revision, "deployment mode revision"),
            (self.worker_id, "worker id"),
            (self.model.model_id, "model id"),
        ):
            if not value or len(value) > 512:
                raise ValueError(f"shadow attention {label} is invalid")
        if not self.deployment_mode_revision.startswith("shadow:"):
            raise ValueError("shadow attention cannot use a live deployment identity")
        for value, label in (
            (self.merge_wait_seconds, "merge wait"),
            (self.window_ttl_seconds, "window TTL"),
            (self.lease_seconds, "lease"),
            (self.model_timeout_seconds, "model timeout"),
            (self.max_candidate_dossiers, "candidate limit"),
            (self.attempt_retention_seconds, "attempt retention"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"shadow attention {label} must be positive")
        if self.max_candidate_dossiers > 16:
            raise ValueError("shadow attention candidate limit exceeds the contract")


class LiveCharacterAttentionContext(FrozenModel):
    """World context pinned to the complete CAS cursor for a fresh live attempt."""

    world_id: str = Field(min_length=1, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=512)
    pinned_world_cursor: ProjectionCursor
    world_logical_time: datetime
    situation: tuple[SourceBoundAttentionContextItem, ...] = Field(max_length=32)
    relevant_context: tuple[SourceBoundAttentionContextItem, ...] = Field(max_length=64)
    available_channels: tuple[PerceptionChannelProof, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def refs_and_time_are_closed(self) -> LiveCharacterAttentionContext:
        if self.world_logical_time.tzinfo is None or self.world_logical_time.utcoffset() is None:
            raise ValueError("live attention logical time must be timezone-aware")
        context_refs = tuple(item.context_ref for item in (*self.situation, *self.relevant_context))
        channel_refs = tuple(item.channel_ref for item in self.available_channels)
        if len(context_refs) != len(set(context_refs)):
            raise ValueError("attention context refs must be unique")
        if len(channel_refs) != len(set(channel_refs)):
            raise ValueError("perception channel refs must be unique")
        return self


class LivePerceptionWindow(FrozenModel):
    """Fresh live window; no shadow attempt can be promoted into this identity."""

    window_id: str = Field(min_length=1, max_length=256)
    attention_attempt_id: str = Field(min_length=1, max_length=256)
    opportunity_id: str = Field(min_length=1, max_length=256)
    world_id: str = Field(min_length=1, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=512)
    pinned_world_cursor: ProjectionCursor
    attention_policy_revision: str = Field(min_length=1, max_length=256)
    deployment_mode: Literal["live"] = "live"
    deployment_mode_revision: str = Field(min_length=1, max_length=256)
    generated_at: datetime
    expires_at: datetime
    candidates: tuple[PerceptionDossier, ...] = Field(min_length=1, max_length=16)
    durable_snapshots: tuple[FrozenExternalSignalSnapshot, ...] = Field(min_length=1, max_length=64)
    candidate_set_hash: str = Field(min_length=64, max_length=64)
    exposure_draw_ref: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def live_window_is_closed(self) -> LivePerceptionWindow:
        if not self.deployment_mode_revision.startswith("live:"):
            raise ValueError("live window requires a live deployment identity")
        if self.expires_at <= self.generated_at:
            raise ValueError("perception window expiry must follow generation")
        candidate_refs = tuple(item.candidate_ref for item in self.candidates)
        snapshot_refs = tuple(item.signal_revision_ref for item in self.durable_snapshots)
        offered_revisions = {
            revision
            for candidate in self.candidates
            for revision in candidate.exact_signal_revisions
        }
        if len(candidate_refs) != len(set(candidate_refs)):
            raise ValueError("perception window candidate refs must be unique")
        if len(snapshot_refs) != len(set(snapshot_refs)):
            raise ValueError("live perception snapshots must be unique")
        if set(snapshot_refs) != offered_revisions:
            raise ValueError("live perception snapshots do not close the offered revisions")
        if any(
            not item.may_expose_to_character_model or not item.may_freeze_durable_snapshot
            for item in self.durable_snapshots
        ):
            raise ValueError("live window contains evidence not licensed for durable exposure")
        return self


class LiveCharacterAttentionSelection(CharacterAttentionSelection):
    """Character-authored encounter plus the privacy reading used downstream."""

    privacy_class: Literal["public", "shareable", "personal", "private", "withhold"]


class LiveCharacterAttentionResult(FrozenModel):
    """A live model may notice zero or many offered candidates."""

    selections: tuple[LiveCharacterAttentionSelection, ...] = Field(max_length=12)


class AuditedLiveCharacterAttentionResult(FrozenModel):
    """Exact character decision paired with its immutable provider/model audit."""

    decision: LiveCharacterAttentionResult
    model_result: ModelResultRecordedPayload


class LiveCharacterAttentionRequest(FrozenModel):
    attention_attempt_id: str = Field(min_length=1, max_length=256)
    retry_ordinal: int = Field(ge=0)
    selection_ordinal: Literal[0, 1]
    window: LivePerceptionWindow
    current_context: LiveCharacterAttentionContext
    validation_failure_codes: tuple[BoundedSignalText, ...] = Field(default=(), max_length=32)
    rejected_result_json: str | None = Field(default=None, max_length=65_536)

    @model_validator(mode="after")
    def request_is_pinned_and_reselection_is_bounded(self) -> LiveCharacterAttentionRequest:
        if self.attention_attempt_id != self.window.attention_attempt_id:
            raise ValueError("attention request and window attempt ids differ")
        if self.current_context.pinned_world_cursor != self.window.pinned_world_cursor:
            raise ValueError("live attention request changed its pinned cursor")
        if self.selection_ordinal == 0 and (
            self.validation_failure_codes or self.rejected_result_json is not None
        ):
            raise ValueError("primary attention request cannot carry reselection feedback")
        if self.selection_ordinal == 1 and not self.validation_failure_codes:
            raise ValueError("attention reselection requires exact validation failures")
        return self


class LiveCharacterAttentionContextPort(Protocol):
    async def freeze_attention_context(
        self,
        *,
        world_id: str,
        actor_ref: str,
        observed_at: datetime,
    ) -> LiveCharacterAttentionContext: ...


class LiveCharacterAttentionModelPort(Protocol):
    model_id: str

    async def consider_attention(self, request: LiveCharacterAttentionRequest) -> object: ...


class ExternalPerceptionAcceptancePort(Protocol):
    async def accept_external_perception(
        self, delivery: ExternalPerceptionLiveDelivery
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class LiveAttentionRuntime:
    """Explicit live-only bundle; it cannot reuse a shadow attempt identity."""

    world_id: str
    actor_ref: str
    attention_policy_revision: str
    deployment_mode_revision: str
    worker_id: str
    context_port: LiveCharacterAttentionContextPort
    model: LiveCharacterAttentionModelPort
    acceptance_port: ExternalPerceptionAcceptancePort
    merge_wait_seconds: int = 120
    window_ttl_seconds: int = 21_600
    lease_seconds: int = 300
    model_timeout_seconds: int = 120
    max_candidate_dossiers: int = 12
    attempt_retention_seconds: int = 604_800

    def __post_init__(self) -> None:
        for value, label in (
            (self.world_id, "world id"),
            (self.actor_ref, "actor ref"),
            (self.attention_policy_revision, "attention policy revision"),
            (self.deployment_mode_revision, "deployment mode revision"),
            (self.worker_id, "worker id"),
        ):
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError(f"live attention {label} is invalid")
        if not self.deployment_mode_revision.startswith("live:"):
            raise ValueError("live attention cannot use a shadow deployment identity")
        model_id = getattr(self.model, "model_id", None)
        if not isinstance(model_id, str) or not model_id or len(model_id) > 512:
            raise ValueError("live attention model id is invalid")
        for value, label in (
            (self.merge_wait_seconds, "merge wait"),
            (self.window_ttl_seconds, "window TTL"),
            (self.lease_seconds, "lease"),
            (self.model_timeout_seconds, "model timeout"),
            (self.max_candidate_dossiers, "candidate limit"),
            (self.attempt_retention_seconds, "attempt retention"),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"live attention {label} must be positive")
        if self.max_candidate_dossiers > 16:
            raise ValueError("live attention candidate limit exceeds the contract")


class ShadowAttentionHealthSnapshot(FrozenModel):
    state: Literal[
        "disabled",
        "no_candidate",
        "window_wait",
        "ready",
        "considering",
        "retry_wait",
        "attention_no_selection",
        "shadow_selected",
        "delivery_pending",
        "perception_committed",
        "superseded",
        "degraded",
    ]
    deployment_mode_revision: str | None = None
    eligible_signal_count: int = Field(ge=0)
    pending_opportunity_count: int = Field(ge=0)
    waiting_window_count: int = Field(ge=0)
    claimed_attempt_count: int = Field(ge=0)
    retry_wait_count: int = Field(ge=0)
    model_no_selection_count: int = Field(ge=0)
    shadow_selected_count: int = Field(ge=0)
    exposed_signal_count: int = Field(ge=0)
    model_call_count_24h: int = Field(ge=0)
    invalid_result_count_24h: int = Field(ge=0)
    technical_failure_count_24h: int = Field(ge=0)
    live_delivery_pending_count: int = Field(default=0, ge=0)
    live_committed_count: int = Field(default=0, ge=0)
    live_superseded_count: int = Field(default=0, ge=0)
    acceptance_failure_count_24h: int = Field(default=0, ge=0)
    outbox_backlog_count: int = Field(default=0, ge=0)
    last_attempt_id: str | None = None
    last_result: str | None = None
    last_failure_code: str | None = None
    next_attention_at: datetime | None = None


DISABLED_SHADOW_ATTENTION_HEALTH = ShadowAttentionHealthSnapshot(
    state="disabled",
    eligible_signal_count=0,
    pending_opportunity_count=0,
    waiting_window_count=0,
    claimed_attempt_count=0,
    retry_wait_count=0,
    model_no_selection_count=0,
    shadow_selected_count=0,
    exposed_signal_count=0,
    model_call_count_24h=0,
    invalid_result_count_24h=0,
    technical_failure_count_24h=0,
)


class PerceptionAdvanceResult(FrozenModel):
    status: Literal[
        "idle",
        "progressed",
        "window_wait",
        "attention_no_selection",
        "shadow_selected",
        "perception_committed",
        "superseded",
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
    shadow_attention: ShadowAttentionHealthSnapshot = DISABLED_SHADOW_ATTENTION_HEALTH
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
    "AuditedLiveCharacterAttentionResult",
    "CharacterAttentionContext",
    "CharacterAttentionContextPort",
    "CharacterAttentionModelPort",
    "CharacterAttentionRequest",
    "CharacterAttentionResult",
    "CharacterAttentionSelection",
    "CharacterAttentionTechnicalFailure",
    "CorrectionEdge",
    "ExternalSignalSourceFailure",
    "ExternalSignalEmbedding",
    "ExternalSignalPlace",
    "ExternalSignalSourceItem",
    "ExternalSignalSourcePage",
    "ExternalSignalSourcePort",
    "ExternalPerceptionAcceptancePort",
    "LicensedEvidenceView",
    "LiveAttentionRuntime",
    "LiveCharacterAttentionContext",
    "LiveCharacterAttentionContextPort",
    "LiveCharacterAttentionModelPort",
    "LiveCharacterAttentionRequest",
    "LiveCharacterAttentionResult",
    "LiveCharacterAttentionSelection",
    "LivePerceptionWindow",
    "MAX_EVIDENCE_BYTES",
    "PerceptionAdvanceResult",
    "PerceptionChannelProof",
    "PerceptionDossier",
    "PerceptionHealthSnapshot",
    "PerceptionWindow",
    "RecordedSignalSourceAdapter",
    "ShadowAttentionHealthSnapshot",
    "ShadowAttentionRuntime",
    "SourceBoundAttentionContextItem",
    "SourceCursor",
    "SourceDisagreement",
    "SourceHealthSnapshot",
    "SourcePolicyRevision",
    "SourceProfile",
    "WallClock",
    "WorldPerceptionHub",
]
