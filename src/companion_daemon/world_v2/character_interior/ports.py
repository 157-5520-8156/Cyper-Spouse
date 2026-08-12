"""Private ports used to compose Character Interior faculties and authorities."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Literal, Mapping, Protocol

from pydantic import Field, field_validator, model_validator

from ..schema_core import FrozenModel
from ..schemas import ProjectionCursor
from .contracts import (
    FACET_NAMES,
    InnerLifeSnapshot,
    _InteriorAuthorLineage,
    _InteriorCapabilityManifest,
)


class _RoleResultContractError(ValueError):
    """A provider result that violates a Character Interior wire contract.

    This is deliberately distinct from provider/runtime exceptions so the
    orchestrator can offer the same character author one bounded structural
    correction without treating an outage as an authored choice.
    """

    def __init__(
        self,
        code: str,
        *,
        detail: str,
        response_hash: str | None = None,
    ) -> None:
        self.code = code
        self.detail = detail
        self.response_hash = response_hash
        super().__init__(code)


class _ViewMaterial(FrozenModel):
    availability: Literal["available", "unavailable"]
    content: dict[str, Any] = Field(default_factory=dict)
    source_refs: tuple[str, ...] = ()


class _ProjectionMaterial(FrozenModel):
    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    cursor: ProjectionCursor
    logical_time: datetime
    situation: _ViewMaterial
    continuity: _ViewMaterial
    facets: dict[str, _ViewMaterial]

    @model_validator(mode="after")
    def all_facets_are_present_once(self) -> "_ProjectionMaterial":
        if tuple(self.facets) != FACET_NAMES:
            raise ValueError("projection must provide all eight ordered interior facets")
        return self


class _RecallRequest(FrozenModel):
    inner_turn_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    cursor: ProjectionCursor
    trigger_ref: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=1_024)
    subject_source_refs: tuple[str, ...] = Field(min_length=1)
    snapshot: InnerLifeSnapshot


class _PrefetchRequest(FrozenModel):
    """One cursor-bound chance to expose already-scheduled memory candidates."""

    inner_turn_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    cursor: ProjectionCursor
    trigger_ref: str = Field(min_length=1)
    subject_source_refs: tuple[str, ...] = Field(min_length=1)
    snapshot: InnerLifeSnapshot
    join_seconds: float = Field(ge=0, le=1)


class _PrefetchResult(FrozenModel):
    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    cursor: ProjectionCursor
    content: dict[str, Any]
    source_refs: tuple[str, ...] = Field(default=(), max_length=16)
    prefetch_trace_json: str | None = None

    @field_validator("source_refs")
    @classmethod
    def prefetch_sources_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("prefetch source refs must be unique")
        return value


class _RecallResult(FrozenModel):
    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    cursor: ProjectionCursor
    content: dict[str, Any]
    source_refs: tuple[str, ...] = Field(default=(), max_length=16)
    recall_trace_json: str | None = None
    prefetch: _PrefetchResult | None = None

    @field_validator("source_refs")
    @classmethod
    def recall_sources_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("recall source refs must be unique")
        return value


class _InteriorRoleRequest(FrozenModel):
    inner_turn_id: str = Field(min_length=1)
    phase: Literal["experience", "consider"]
    subject_ref: str = Field(min_length=1)
    trigger_ref: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=128)
    context_note: str | None = None
    subject_source_refs: tuple[str, ...] = Field(min_length=1)
    capability_manifest: _InteriorCapabilityManifest | None = None
    snapshot: InnerLifeSnapshot
    recall_completed: bool = False
    recall_parent_author_lineage: _InteriorAuthorLineage | None = None
    recall_parent_usage_json: str | None = Field(default=None, max_length=8_192)
    # One bounded, same-author structural correction.  These fields describe
    # a rejected wire result; they never prescribe the character's semantic
    # choice and never expose a second provider route.
    correction_ordinal: Literal[0, 1] = 0
    correction_failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    correction_failure_detail: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_096,
    )

    @model_validator(mode="after")
    def correction_lineage_is_explicit(self) -> "_InteriorRoleRequest":
        if (self.correction_ordinal == 1) != (self.correction_failure_code is not None):
            raise ValueError("role correction lineage is incomplete")
        if self.correction_ordinal == 0 and self.correction_failure_detail is not None:
            raise ValueError("role correction detail requires a correction attempt")
        if not self.recall_completed and (
            self.recall_parent_author_lineage is not None
            or self.recall_parent_usage_json is not None
        ):
            raise ValueError("Recall parent audit requires a completed Recall")
        if (
            self.recall_parent_usage_json is not None
            and self.recall_parent_author_lineage is None
        ):
            raise ValueError("Recall parent usage lacks its author lineage")
        return self


class _InteriorRoleResult(FrozenModel):
    status: Literal["transition", "no_change", "decision", "silent", "recall_request"]
    summary: str = Field(min_length=1, max_length=1_024)
    attended_source_refs: tuple[str, ...] = Field(default=(), max_length=32)
    decision: dict[str, Any] | None = None
    recall_query: str | None = Field(default=None, min_length=1, max_length=1_024)
    proposals: tuple[dict[str, Any], ...] = Field(default=(), max_length=32)
    author_lineage: _InteriorAuthorLineage | None = None
    author_usage_json: str | None = Field(default=None, max_length=8_192)

    @model_validator(mode="after")
    def decision_payload_matches_status(self) -> "_InteriorRoleResult":
        if self.status == "decision" and self.decision is None:
            raise ValueError("role decision requires a decision payload")
        if self.status != "decision" and self.decision is not None:
            raise ValueError("only a role decision may carry a decision payload")
        if self.status == "recall_request" and self.recall_query is None:
            raise ValueError("role recall request requires a query")
        if self.status != "recall_request" and self.recall_query is not None:
            raise ValueError("only a role recall request may carry a query")
        if self.status == "recall_request" and self.proposals:
            raise ValueError("role recall request cannot submit effects before recall")
        if self.author_usage_json is not None and self.author_lineage is None:
            raise ValueError("role author usage lacks its invocation lineage")
        if self.author_usage_json is not None:
            try:
                usage = json.loads(self.author_usage_json)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("role author usage is not JSON") from exc
            if not isinstance(usage, dict):
                raise ValueError("role author usage must be one JSON object")
        if len(self.attended_source_refs) != len(set(self.attended_source_refs)):
            raise ValueError("role attention refs must be unique")
        return self


class _AuthorityRequest(FrozenModel):
    inner_turn_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    purpose: str = Field(min_length=1, max_length=128)
    subject_ref: str = Field(min_length=1, max_length=512)
    trigger_ref: str = Field(min_length=1, max_length=512)
    subject_source_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    cursor: ProjectionCursor
    snapshot_id: str = Field(min_length=1)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    capability_manifest: _InteriorCapabilityManifest | None = None
    author_lineage: _InteriorAuthorLineage | None = None
    private_self_lineage_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    proposals: tuple[dict[str, Any], ...] = Field(min_length=1, max_length=32)


class _ProjectionPort(Protocol):
    def project(
        self,
        *,
        subject: object,
    ) -> Mapping[str, object] | InnerLifeSnapshot: ...


class _RecallPort(Protocol):
    def prefetch(self, request: _PrefetchRequest) -> Mapping[str, object] | None: ...

    def recall(self, request: _RecallRequest) -> Mapping[str, object] | None: ...


class _RoleFaculty(Protocol):
    name: str

    def experience(self, request: _InteriorRoleRequest) -> Mapping[str, object]: ...

    def consider(self, request: _InteriorRoleRequest) -> Mapping[str, object]: ...


class _AuthorityPort(Protocol):
    def submit(self, request: _AuthorityRequest) -> tuple[str, ...]: ...


__all__: list[str] = []
