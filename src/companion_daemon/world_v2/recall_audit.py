"""Cycle-free schemas for character recall requests and replay traces."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal, Self

from pydantic import Field, model_validator

from .recall_index import (
    MAX_RECALL_QUERY_CHARACTERS,
    RecallCursor,
    RecallDocument,
    RecallQuery,
    recall_query_hash,
    recall_result_hash,
)
from .schema_core import FrozenModel


MAX_RECALL_AUDIT_BYTES = 10_000


def paired_recall_transition_hash(
    *,
    trigger_ref: str,
    source_cursor: RecallCursor,
    target_cursor: RecallCursor,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "contract": "paired-recall-transition.1",
                "trigger_ref": trigger_ref,
                "source_cursor": source_cursor.model_dump(mode="json"),
                "target_cursor": target_cursor.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class CharacterRecallRequest(FrozenModel):
    query_text: str = Field(min_length=1, max_length=MAX_RECALL_QUERY_CHARACTERS)
    lexical_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_RECALL_QUERY_CHARACTERS,
        exclude_if=lambda value: value is None,
    )
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    link_refs: tuple[str, ...] = Field(default=(), max_length=16)
    memory_kinds: tuple[Literal["episodic", "semantic", "reflective"], ...] = ()
    include_historical: bool = False
    limit: int = Field(default=6, ge=1, le=6)

    @model_validator(mode="after")
    def filters_are_canonical(self) -> Self:
        if self.link_refs != tuple(sorted(set(self.link_refs))):
            raise ValueError("recall request links must be sorted and unique")
        if self.memory_kinds != tuple(sorted(set(self.memory_kinds))):
            raise ValueError("recall request kinds must be sorted and unique")
        if (
            self.occurred_from is not None
            and self.occurred_to is not None
            and self.occurred_to < self.occurred_from
        ):
            raise ValueError("recall request occurrence interval is reversed")
        return self


class RecallAuditHit(FrozenModel):
    document: RecallDocument
    match_channels: tuple[Literal["lexical", "dense", "temporal", "structured"], ...]
    score_bp: int = Field(ge=0, le=10_000)
    lexical_score_bp: int = Field(ge=0, le=10_000)
    dense_score_bp: int = Field(ge=0, le=10_000)
    temporal_score_bp: int = Field(ge=0, le=10_000)
    structured_score_bp: int = Field(ge=0, le=10_000)
    accessibility_offset_bp: int = Field(ge=-500, le=500)


class RecallAuditTrace(FrozenModel):
    trace_contract: Literal["character-recall-trace.1"] = "character-recall-trace.1"
    mode: Literal["prefetch", "character_pull"] = "character_pull"
    trigger_ref: str = Field(min_length=1, max_length=256)
    request: CharacterRecallRequest
    query: RecallQuery
    query_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    index_version: str = Field(min_length=1, max_length=256)
    embedding_version: str = Field(min_length=1, max_length=256)
    embedding_status: Literal["unknown", "used", "degraded"] = Field(
        default="unknown",
        exclude_if=lambda value: value == "unknown",
    )
    embedding_failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        exclude_if=lambda value: value is None,
    )
    index_cursor: RecallCursor
    evaluated_cursor: RecallCursor | None = None
    reuse_contract: Literal[
        "same_context",
        "paired_cognition_carry",
    ] = "same_context"
    paired_transition_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    hits: tuple[RecallAuditHit, ...] = Field(max_length=6)

    @model_validator(mode="after")
    def evaluation_cursor_is_explicit(self) -> Self:
        evaluated = self.evaluated_cursor or self.index_cursor
        if self.reuse_contract == "same_context" and evaluated != self.index_cursor:
            raise ValueError("ordinary recall must be evaluated at its search cursor")
        if self.reuse_contract == "same_context" and self.paired_transition_hash is not None:
            raise ValueError("ordinary recall cannot declare a paired transition")
        if self.reuse_contract == "paired_cognition_carry" and any(
            source > target
            for source, target in zip(
                (
                    self.index_cursor.world_revision,
                    self.index_cursor.deliberation_revision,
                    self.index_cursor.ledger_sequence,
                ),
                (
                    evaluated.world_revision,
                    evaluated.deliberation_revision,
                    evaluated.ledger_sequence,
                ),
                strict=True,
            )
        ):
            raise ValueError("paired recall cannot carry future search material")
        if self.reuse_contract == "paired_cognition_carry":
            expected_transition = paired_recall_transition_hash(
                trigger_ref=self.trigger_ref,
                source_cursor=self.index_cursor,
                target_cursor=evaluated,
            )
            if self.paired_transition_hash != expected_transition:
                raise ValueError("paired recall transition proof is absent or invalid")
        if self.query.cursor != self.index_cursor:
            raise ValueError("recall query and result cursors differ")
        if (self.embedding_status == "degraded") != (self.embedding_failure_code is not None):
            raise ValueError("recall embedding degradation metadata is incomplete")
        if (
            self.request.query_text != self.query.query_text
            or self.request.lexical_text != self.query.lexical_text
            or self.request.occurred_from != self.query.occurred_from
            or self.request.occurred_to != self.query.occurred_to
            or self.request.link_refs != self.query.link_refs
            or self.request.memory_kinds != self.query.memory_kinds
            or self.request.include_historical != self.query.include_historical
            or self.request.limit != self.query.limit
        ):
            raise ValueError("character recall request does not match executed query")
        if self.query_hash != recall_query_hash(
            index_version=self.index_version,
            query=self.query,
        ):
            raise ValueError("recall query hash does not match executed query")
        if self.result_hash != recall_result_hash(
            query_hash=self.query_hash,
            cursor=self.index_cursor,
            hit_values=[item.model_dump(mode="json") for item in self.hits],
        ):
            raise ValueError("recall result hash does not match recorded hits")
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_RECALL_AUDIT_BYTES:
            raise ValueError("recall audit exceeds its durable replay budget")
        return self


__all__ = [
    "CharacterRecallRequest",
    "MAX_RECALL_AUDIT_BYTES",
    "RecallAuditHit",
    "RecallAuditTrace",
    "paired_recall_transition_hash",
]
