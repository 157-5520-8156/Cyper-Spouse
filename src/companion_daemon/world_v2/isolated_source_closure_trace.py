"""Process-local source-closure diagnostics for explicit isolated audits.

This module is deliberately not a World authority, logger, metric, or recovery
input.  With no active :class:`ContextVar` sink, emission is a single no-op
lookup.  An explicitly scoped audit may retain only the bounded text that the
character proposed to make visible plus cryptographic hashes and the
reviewer's hard-boundary coordinates.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256
import json
from threading import Lock
from typing import Iterator, Literal, Protocol


SourceClosureTraceStage = Literal[
    "initial_rejection",
    "corrected_rejection",
    # Historical diagnostics emitted before rejection became monotonic.
    "appeal_cleared_initial_rejection",
    "post_appeal_initial_rejection",
    "reselection_not_attempted",
    "reselection_provider_failed",
    "reselection_output_invalid_before_review",
    "appeal_cleared_corrected_rejection",
    "post_appeal_corrected_rejection",
]

_MAX_VISIBLE_BEATS = 8
_MAX_VISIBLE_TEXT_BYTES = 8_192
_MAX_COORDINATES = 32
_MAX_FINDINGS = 16
_MAX_FINDING_TEXT_BYTES = 8_192
_MAX_PROVIDER_WIRE_ATTEMPTS = 8
_MAX_PROVIDER_WIRE_TEXT_BYTES = 8_192
_MAX_MATERIALIZATION_FIELD_PATHS = 16

CandidateMaterializationFailureCategory = Literal[
    "authored_expression_shape",
    "private_turn_state",
    "expression_draft_schema",
    "capability_validation",
    "episode_disposition",
]
CandidateMaterializationFailureStage = Literal[
    "pre_final_source_review",
    "post_source_acceptance",
]
_MATERIALIZATION_FAILURE_CATEGORIES = frozenset(
    {
        "authored_expression_shape",
        "private_turn_state",
        "expression_draft_schema",
        "capability_validation",
        "episode_disposition",
    }
)


class SourceClosureVisibleFindingLike(Protocol):
    """Structural subset needed by the optional isolated audit."""

    category: str
    visible_span: str
    claim_index: int | None
    source_relation: str
    source_refs: tuple[str, ...]


class SourceClosureLocatorLike(Protocol):
    """Exact authored coordinate whose text is hashed before retention."""

    beat_index: int
    char_start: int
    char_end: int
    text: str


class SourceClosurePropositionLike(Protocol):
    """Structural subset of one model-owned epistemic decomposition item."""

    locator: SourceClosureLocatorLike
    semantic_role: str
    parent_index: int | None


class SourceClosureCoverageFindingLike(Protocol):
    """Structural subset of one source-authority coverage verdict."""

    locator: SourceClosureLocatorLike
    decision: str
    source_relation: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceClosureTraceFinding:
    """One bounded proposition coordinate with authority identities hashed."""

    category: str
    visible_span: str
    visible_span_sha256: str
    visible_span_truncated: bool
    claim_index: int | None
    source_relation: str
    authority_sha256: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "visible_span": self.visible_span,
            "visible_span_sha256": self.visible_span_sha256,
            "visible_span_truncated": self.visible_span_truncated,
            "claim_index": self.claim_index,
            "source_relation": self.source_relation,
            "authority_sha256": list(self.authority_sha256),
        }


@dataclass(frozen=True, slots=True)
class SourceClosureTraceEvent:
    """One non-authoritative, deliberately surface-only rejection observation."""

    stage: SourceClosureTraceStage
    candidate_sha256: str
    visible_beat_texts: tuple[str, ...]
    visible_beat_sha256: tuple[str, ...]
    visible_text_truncated: bool
    surface_extraction: Literal["available", "unavailable"]
    ci: tuple[int, ...]
    v: tuple[str, ...]
    p: tuple[str, ...]
    visible_findings: tuple[SourceClosureTraceFinding, ...]
    discourse_resolved_visible_finding_indexes: tuple[int, ...]
    prior_correction_kind: Literal["private_turn_state", "recall_choice"] | None = None
    sanitized_failure_code: str | None = None
    sanitized_failure_field_path: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "stage": self.stage,
            "candidate_sha256": self.candidate_sha256,
            "visible_beat_texts": list(self.visible_beat_texts),
            "visible_beat_sha256": list(self.visible_beat_sha256),
            "visible_text_truncated": self.visible_text_truncated,
            "surface_extraction": self.surface_extraction,
            "ci": list(self.ci),
            "v": list(self.v),
            "p": list(self.p),
            "visible_findings": [finding.as_dict() for finding in self.visible_findings],
            "discourse_resolved_visible_finding_indexes": list(
                self.discourse_resolved_visible_finding_indexes
            ),
        }
        if self.prior_correction_kind is not None:
            result["prior_correction_kind"] = self.prior_correction_kind
        if self.sanitized_failure_code is not None:
            result["sanitized_failure_code"] = self.sanitized_failure_code
        if self.sanitized_failure_field_path is not None:
            result["sanitized_failure_field_path"] = self.sanitized_failure_field_path
        return result


@dataclass(frozen=True, slots=True)
class SourceClosureVerdictTraceLocator:
    """One text-free proposition coordinate from a successful review path."""

    beat_index: int
    char_start: int
    char_end: int
    text_sha256: str
    semantic_role: str
    parent_index: int | None

    def identity(self) -> tuple[int, int, int, str]:
        return (
            self.beat_index,
            self.char_start,
            self.char_end,
            self.text_sha256,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "beat_index": self.beat_index,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "text_sha256": self.text_sha256,
            "semantic_role": self.semantic_role,
            "parent_index": self.parent_index,
        }


@dataclass(frozen=True, slots=True)
class SourceClosureVerdictTraceCoverage:
    """One verdict bound to a hashed inventory coordinate."""

    locator_index: int
    decision: str
    source_relation: str
    authority_sha256: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "locator_index": self.locator_index,
            "decision": self.decision,
            "source_relation": self.source_relation,
            "authority_sha256": list(self.authority_sha256),
        }


@dataclass(frozen=True, slots=True)
class SourceClosureVerdictTraceEvent:
    """Minimal accepted-path evidence with no raw visible or private prose."""

    candidate_sha256: str
    inventory_outcome: Literal[
        "no_external_propositions",
        "external_propositions",
    ]
    coverage_outcome: Literal["not_run", "completed", "incomplete"]
    proposition_role_counts: tuple[tuple[str, int], ...]
    locators: tuple[SourceClosureVerdictTraceLocator, ...]
    coverage: tuple[SourceClosureVerdictTraceCoverage, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "record_kind": "candidate_verdict",
            "candidate_sha256": self.candidate_sha256,
            "inventory_outcome": self.inventory_outcome,
            "coverage_outcome": self.coverage_outcome,
            "proposition_role_counts": dict(self.proposition_role_counts),
            "locators": [locator.as_dict() for locator in self.locators],
            "coverage": [finding.as_dict() for finding in self.coverage],
        }


@dataclass(frozen=True, slots=True)
class SourceClosureWireFailureTraceEvent:
    """Text-free final structural coordinate for one exhausted wire."""

    candidate_sha256: str
    stage: Literal["inventory", "coverage"]
    code: str
    field: str
    provider_attempts: tuple["SourceClosureWireAttemptTrace", ...] = ()

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "record_kind": "wire_failure",
            "candidate_sha256": self.candidate_sha256,
            "stage": self.stage,
            "code": self.code,
            "field": self.field,
        }
        if self.provider_attempts:
            result["provider_attempts"] = [attempt.as_dict() for attempt in self.provider_attempts]
        return result


@dataclass(frozen=True, slots=True)
class SourceClosureCandidateMaterializationFailureTraceEvent:
    """Text-free coordinate for a candidate that failed after source acceptance."""

    candidate_sha256: str
    stage: CandidateMaterializationFailureStage
    category: CandidateMaterializationFailureCategory
    code: str
    field_paths: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "record_kind": "candidate_materialization_failure",
            "candidate_sha256": self.candidate_sha256,
            "stage": self.stage,
            "category": self.category,
            "code": self.code,
            "field_paths": list(self.field_paths),
        }


@dataclass(frozen=True, slots=True)
class SourceClosureWireNormalizationTraceEvent:
    """One semantics-preserving transport discriminator repair."""

    candidate_sha256: str
    stage: Literal["coverage"]
    code: Literal["missing_negotiated_contract"]
    raw_wire_sha256: str
    normalized_contract: str

    def as_dict(self) -> dict[str, object]:
        return {
            "record_kind": "wire_normalization",
            "candidate_sha256": self.candidate_sha256,
            "stage": self.stage,
            "code": self.code,
            "raw_wire_sha256": self.raw_wire_sha256,
            "normalized_contract": self.normalized_contract,
        }


@dataclass(frozen=True, slots=True)
class SourceClosureWireAttemptTrace:
    """Allowlisted provider wire retained only by an explicit isolated audit."""

    stage: Literal["inventory", "coverage"]
    attempt_ordinal: int
    wire_sha256: str
    extraction: Literal["available", "unavailable"]
    wire_json: str | None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "stage": self.stage,
            "attempt_ordinal": self.attempt_ordinal,
            "wire_sha256": self.wire_sha256,
            "extraction": self.extraction,
        }
        if self.wire_json is not None:
            result["wire"] = json.loads(self.wire_json)
        return result


SourceClosureTraceRecord = (
    SourceClosureTraceEvent
    | SourceClosureVerdictTraceEvent
    | SourceClosureWireFailureTraceEvent
    | SourceClosureCandidateMaterializationFailureTraceEvent
    | SourceClosureWireNormalizationTraceEvent
)


class SourceClosureTraceSink(Protocol):
    """Small observation seam intentionally kept out of production builders."""

    def record(self, event: SourceClosureTraceRecord) -> None: ...


class BoundedSourceClosureTraceCollector:
    """Concurrency-safe process collector with a hard event-count ceiling."""

    def __init__(self, *, max_events: int = 512) -> None:
        if max_events < 1 or max_events > 4_096:
            raise ValueError("source-closure trace max_events must be between 1 and 4096")
        self._max_events = max_events
        self._events: list[SourceClosureTraceRecord] = []
        self._dropped_count = 0
        self._lock = Lock()

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    def record(self, event: SourceClosureTraceRecord) -> None:
        with self._lock:
            if len(self._events) >= self._max_events:
                self._dropped_count += 1
                return
            self._events.append(event)

    def snapshot(self) -> tuple[SourceClosureTraceRecord, ...]:
        with self._lock:
            return tuple(self._events)


_ACTIVE_SOURCE_CLOSURE_TRACE: ContextVar[SourceClosureTraceSink | None] = ContextVar(
    "world_v2_isolated_source_closure_trace",
    default=None,
)


@contextmanager
def capture_isolated_source_closure_trace(
    sink: SourceClosureTraceSink,
) -> Iterator[None]:
    """Install one task-local sink for the lexical lifetime of an isolated audit."""

    token = _ACTIVE_SOURCE_CLOSURE_TRACE.set(sink)
    try:
        yield
    finally:
        _ACTIVE_SOURCE_CLOSURE_TRACE.reset(token)


def _unique_bounded(values: tuple[object, ...]) -> tuple[object, ...]:
    return tuple(dict.fromkeys(values))[:_MAX_COORDINATES]


def _truncate_utf8(value: str, *, remaining_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= remaining_bytes:
        return value, False
    return encoded[:remaining_bytes].decode("utf-8", errors="ignore"), True


def _bounded_wire_string(value: object, *, maximum_bytes: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    return _truncate_utf8(value, remaining_bytes=maximum_bytes)[0]


def _stable_failure_coordinate(value: object, *, maximum_bytes: int) -> str | None:
    """Admit only schema-like ASCII coordinates, never model-authored prose."""

    if not isinstance(value, str) or not value:
        return None
    if any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-[]"
        for character in value
    ):
        return None
    return _truncate_utf8(value, remaining_bytes=maximum_bytes)[0]


def _bounded_inventory_wire(
    value: dict[str, object],
    *,
    remaining_text_bytes: int,
) -> tuple[dict[str, object], int]:
    """Retain only contract coordinates and visible locator text."""

    result: dict[str, object] = {}
    contract = _bounded_wire_string(value.get("contract"))
    if contract is not None:
        result["contract"] = contract
    raw_propositions = value.get("propositions")
    if not isinstance(raw_propositions, list):
        return result, remaining_text_bytes
    propositions: list[dict[str, object]] = []
    for raw_proposition in raw_propositions[:_MAX_COORDINATES]:
        if not isinstance(raw_proposition, dict):
            continue
        proposition: dict[str, object] = {}
        raw_locator = raw_proposition.get("locator")
        if isinstance(raw_locator, dict):
            locator: dict[str, object] = {}
            for field in ("beat_index", "char_start", "char_end"):
                coordinate = raw_locator.get(field)
                if isinstance(coordinate, int) and not isinstance(coordinate, bool):
                    locator[field] = coordinate
            text = raw_locator.get("text")
            if isinstance(text, str) and remaining_text_bytes > 0:
                bounded_text, _ = _truncate_utf8(
                    text,
                    remaining_bytes=remaining_text_bytes,
                )
                locator["text"] = bounded_text
                remaining_text_bytes -= len(bounded_text.encode("utf-8"))
            if locator:
                proposition["locator"] = locator
        semantic_role = _bounded_wire_string(raw_proposition.get("semantic_role"))
        if semantic_role is not None:
            proposition["semantic_role"] = semantic_role
        if proposition:
            propositions.append(proposition)
    result["propositions"] = propositions
    return result, remaining_text_bytes


def _bounded_coverage_wire(
    value: dict[str, object],
    *,
    remaining_text_bytes: int,
) -> tuple[dict[str, object], int]:
    """Retain only verdict coordinates and bounded authored missing spans."""

    result: dict[str, object] = {}
    contract = _bounded_wire_string(value.get("contract"))
    if contract is not None:
        result["contract"] = contract
    inventory_complete = value.get("inventory_complete")
    if isinstance(inventory_complete, bool):
        result["inventory_complete"] = inventory_complete
    raw_findings = value.get("findings")
    findings: list[dict[str, object]] = []
    if isinstance(raw_findings, list):
        for raw_finding in raw_findings[:_MAX_COORDINATES]:
            if not isinstance(raw_finding, dict):
                continue
            finding: dict[str, object] = {}
            locator_index = raw_finding.get("locator_index")
            if isinstance(locator_index, int) and not isinstance(locator_index, bool):
                finding["locator_index"] = locator_index
            for field in ("decision", "source_relation"):
                bounded = _bounded_wire_string(raw_finding.get(field))
                if bounded is not None:
                    finding[field] = bounded
            raw_indexes = raw_finding.get("source_ref_indexes")
            if isinstance(raw_indexes, list):
                finding["source_ref_indexes"] = [
                    index
                    for index in raw_indexes[:8]
                    if isinstance(index, int) and not isinstance(index, bool)
                ]
            if finding:
                findings.append(finding)
        result["findings"] = findings

    raw_missing_findings = value.get("missing_findings")
    if isinstance(raw_missing_findings, list):
        bounded_missing, remaining_text_bytes = _bounded_inventory_wire(
            {"propositions": raw_missing_findings},
            remaining_text_bytes=remaining_text_bytes,
        )
        result["missing_findings"] = bounded_missing.get("propositions", [])
    return result, remaining_text_bytes


def _bounded_wire_attempts(
    provider_attempts: tuple[tuple[Literal["inventory", "coverage"], str], ...],
) -> tuple[SourceClosureWireAttemptTrace, ...]:
    retained: list[SourceClosureWireAttemptTrace] = []
    remaining_text_bytes = _MAX_PROVIDER_WIRE_TEXT_BYTES
    for ordinal, (stage, raw) in enumerate(
        provider_attempts[:_MAX_PROVIDER_WIRE_ATTEMPTS],
        start=1,
    ):
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("provider wire is not an object")
            if stage == "inventory":
                wire, remaining_text_bytes = _bounded_inventory_wire(
                    value,
                    remaining_text_bytes=remaining_text_bytes,
                )
            else:
                wire, remaining_text_bytes = _bounded_coverage_wire(
                    value,
                    remaining_text_bytes=remaining_text_bytes,
                )
            wire_json = json.dumps(
                wire,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            extraction: Literal["available", "unavailable"] = "available"
        except (TypeError, ValueError, json.JSONDecodeError):
            wire_json = None
            extraction = "unavailable"
        retained.append(
            SourceClosureWireAttemptTrace(
                stage=stage,
                attempt_ordinal=ordinal,
                wire_sha256=sha256(raw.encode("utf-8")).hexdigest(),
                extraction=extraction,
                wire_json=wire_json,
            )
        )
    return tuple(retained)


def _visible_surface(
    raw_candidate: str,
) -> tuple[tuple[str, ...], tuple[str, ...], bool, Literal["available", "unavailable"]]:
    value = json.loads(raw_candidate)
    if not isinstance(value, dict):
        raise ValueError("candidate must be a JSON object")
    nested = value.get("expression_draft", value.get("ExpressionDraft"))
    if isinstance(nested, dict):
        value = nested
    beats = value.get("beats")
    if not isinstance(beats, list):
        raise ValueError("candidate has no expression beats")
    all_texts = tuple(
        text
        for beat in beats
        if isinstance(beat, dict) and isinstance((text := beat.get("text")), str)
    )
    truncated = len(all_texts) > _MAX_VISIBLE_BEATS
    retained: list[str] = []
    hashes: list[str] = []
    remaining = _MAX_VISIBLE_TEXT_BYTES
    for text in all_texts[:_MAX_VISIBLE_BEATS]:
        hashes.append(sha256(text.encode("utf-8")).hexdigest())
        bounded, shortened = _truncate_utf8(text, remaining_bytes=remaining)
        retained.append(bounded)
        used = len(bounded.encode("utf-8"))
        remaining -= used
        truncated = truncated or shortened
        if remaining == 0:
            truncated = truncated or len(retained) < len(all_texts)
            break
    return tuple(retained), tuple(hashes), truncated, "available"


def _bounded_findings(
    findings: tuple[SourceClosureVisibleFindingLike, ...],
) -> tuple[SourceClosureTraceFinding, ...]:
    retained: list[SourceClosureTraceFinding] = []
    remaining = _MAX_FINDING_TEXT_BYTES
    for finding in findings[:_MAX_FINDINGS]:
        if remaining <= 0:
            break
        span = finding.visible_span
        if not isinstance(span, str) or not span:
            continue
        bounded_span, truncated = _truncate_utf8(
            span,
            remaining_bytes=remaining,
        )
        remaining -= len(bounded_span.encode("utf-8"))
        claim_index = finding.claim_index
        if isinstance(claim_index, bool) or (
            claim_index is not None and not isinstance(claim_index, int)
        ):
            claim_index = None
        source_refs = tuple(
            value
            for value in _unique_bounded(tuple(finding.source_refs))
            if isinstance(value, str) and value
        )
        retained.append(
            SourceClosureTraceFinding(
                category=str(finding.category),
                visible_span=bounded_span,
                visible_span_sha256=sha256(span.encode("utf-8")).hexdigest(),
                visible_span_truncated=truncated,
                claim_index=claim_index,
                source_relation=str(finding.source_relation),
                authority_sha256=tuple(
                    sha256(value.encode("utf-8")).hexdigest() for value in source_refs
                ),
            )
        )
    return tuple(retained)


def emit_source_closure_trace(
    *,
    stage: SourceClosureTraceStage,
    raw_candidate: str,
    ci: tuple[int, ...],
    v: tuple[str, ...],
    p: tuple[str, ...],
    visible_findings: tuple[SourceClosureVisibleFindingLike, ...] = (),
    discourse_resolved_visible_finding_indexes: tuple[int, ...] = (),
    prior_correction_kind: Literal["private_turn_state", "recall_choice"] | None = None,
    sanitized_failure_code: str | None = None,
    sanitized_failure_field_path: str | None = None,
) -> None:
    """Best-effort emission that can never change model or World behavior."""

    sink = _ACTIVE_SOURCE_CLOSURE_TRACE.get()
    if sink is None:
        return
    try:
        candidate_sha256 = sha256(raw_candidate.encode("utf-8")).hexdigest()
        try:
            texts, text_hashes, truncated, extraction = _visible_surface(raw_candidate)
        except (TypeError, ValueError, json.JSONDecodeError):
            texts, text_hashes, truncated, extraction = (), (), False, "unavailable"
        event = SourceClosureTraceEvent(
            stage=stage,
            candidate_sha256=candidate_sha256,
            visible_beat_texts=texts,
            visible_beat_sha256=text_hashes,
            visible_text_truncated=truncated,
            surface_extraction=extraction,
            ci=tuple(
                int(value)
                for value in _unique_bounded(tuple(ci))
                if isinstance(value, int) and not isinstance(value, bool)
            ),
            v=tuple(str(value) for value in _unique_bounded(tuple(v))),
            p=tuple(str(value) for value in _unique_bounded(tuple(p))),
            visible_findings=_bounded_findings(tuple(visible_findings)),
            discourse_resolved_visible_finding_indexes=tuple(
                int(value)
                for value in _unique_bounded(tuple(discourse_resolved_visible_finding_indexes))
                if isinstance(value, int) and not isinstance(value, bool)
            ),
            prior_correction_kind=(
                prior_correction_kind
                if prior_correction_kind in {"private_turn_state", "recall_choice"}
                else None
            ),
            sanitized_failure_code=_stable_failure_coordinate(
                sanitized_failure_code,
                maximum_bytes=128,
            ),
            sanitized_failure_field_path=_stable_failure_coordinate(
                sanitized_failure_field_path,
                maximum_bytes=256,
            ),
        )
        sink.record(event)
    except Exception:
        # Diagnostics are never allowed to become a new liveness dependency.
        return


def emit_source_closure_verdict_trace(
    *,
    raw_candidate: str,
    propositions: tuple[SourceClosurePropositionLike, ...],
    coverage_findings: tuple[SourceClosureCoverageFindingLike, ...],
    coverage_outcome: Literal["not_run", "completed", "incomplete"] | None = None,
) -> None:
    """Retain only hashed coordinates that explain one candidate coverage verdict."""

    sink = _ACTIVE_SOURCE_CLOSURE_TRACE.get()
    if sink is None:
        return
    try:
        retained_locators: list[SourceClosureVerdictTraceLocator] = []
        role_counts: dict[str, int] = {}
        for proposition in propositions[:32]:
            locator = proposition.locator
            text = locator.text
            if (
                isinstance(locator.beat_index, bool)
                or not isinstance(locator.beat_index, int)
                or isinstance(locator.char_start, bool)
                or not isinstance(locator.char_start, int)
                or isinstance(locator.char_end, bool)
                or not isinstance(locator.char_end, int)
                or not isinstance(text, str)
                or not text
            ):
                continue
            role = str(proposition.semantic_role)
            parent_index = proposition.parent_index
            if isinstance(parent_index, bool) or (
                parent_index is not None and not isinstance(parent_index, int)
            ):
                parent_index = None
            retained_locators.append(
                SourceClosureVerdictTraceLocator(
                    beat_index=locator.beat_index,
                    char_start=locator.char_start,
                    char_end=locator.char_end,
                    text_sha256=sha256(text.encode("utf-8")).hexdigest(),
                    semantic_role=role,
                    parent_index=parent_index,
                )
            )
            role_counts[role] = role_counts.get(role, 0) + 1

        locator_indexes: dict[tuple[int, int, int, str], int] = {}
        for index, locator in enumerate(retained_locators):
            if locator.semantic_role not in {
                "outer_private_state",
                "immediate_private_state",
                "source_bearing_private_episode",
                "embedded_external_proposition",
                "standalone_external_proposition",
            }:
                continue
            # A private parent and its embedded external proposition may
            # intentionally share the same visible span. Coverage follows the
            # first inventory proposition at that coordinate, so diagnostics
            # must not silently retarget the finding to a later parent record.
            locator_indexes.setdefault(locator.identity(), index)
        reviewable_indexes = [
            index
            for index, locator in enumerate(retained_locators)
            if locator.semantic_role
            not in {
                "nonassertive_content",
                "world_unbound_generalization",
            }
        ]
        external_indexes = [
            index
            for index, locator in enumerate(retained_locators)
            if locator.semantic_role
            in {
                "embedded_external_proposition",
                "standalone_external_proposition",
            }
        ]

        def finding_identity(
            finding: SourceClosureCoverageFindingLike,
        ) -> tuple[int, int, int, str]:
            return (
                finding.locator.beat_index,
                finding.locator.char_start,
                finding.locator.char_end,
                sha256(finding.locator.text.encode("utf-8")).hexdigest(),
            )

        # Current exhaustive inventories review every source-bound semantic
        # coordinate, excluding nonassertive and World-unbound discourse.
        # Historical inventories reviewed external coordinates only. Select the exact ordinal lane
        # when its length and frozen identities match; identity-only lookup is
        # merely a bounded fallback for old/malformed diagnostics.
        ordinal_indexes = reviewable_indexes
        bounded_findings = coverage_findings[:16]
        if len(bounded_findings) == len(external_indexes) and all(
            finding_identity(finding) == retained_locators[index].identity()
            for finding, index in zip(bounded_findings, external_indexes, strict=True)
        ):
            ordinal_indexes = external_indexes
        retained_coverage: list[SourceClosureVerdictTraceCoverage] = []
        for ordinal, finding in enumerate(bounded_findings):
            locator = finding.locator
            text = locator.text
            if not isinstance(text, str) or not text:
                continue
            identity = finding_identity(finding)
            locator_index = None
            if ordinal < len(ordinal_indexes):
                ordinal_index = ordinal_indexes[ordinal]
                if retained_locators[ordinal_index].identity() == identity:
                    locator_index = ordinal_index
            if locator_index is None:
                locator_index = locator_indexes.get(identity)
            if locator_index is None:
                continue
            source_refs = tuple(
                value
                for value in _unique_bounded(tuple(finding.source_refs))
                if isinstance(value, str) and value
            )
            retained_coverage.append(
                SourceClosureVerdictTraceCoverage(
                    locator_index=locator_index,
                    decision=str(finding.decision),
                    source_relation=str(finding.source_relation),
                    authority_sha256=tuple(
                        sha256(value.encode("utf-8")).hexdigest() for value in source_refs
                    ),
                )
            )

        external_count = sum(
            role_counts.get(role, 0)
            for role in (
                "embedded_external_proposition",
                "standalone_external_proposition",
            )
        )
        sink.record(
            SourceClosureVerdictTraceEvent(
                candidate_sha256=sha256(raw_candidate.encode("utf-8")).hexdigest(),
                inventory_outcome=(
                    "external_propositions" if external_count else "no_external_propositions"
                ),
                coverage_outcome=(
                    coverage_outcome
                    if coverage_outcome is not None
                    else "completed"
                    if retained_coverage
                    else "not_run"
                ),
                proposition_role_counts=tuple(sorted(role_counts.items())),
                locators=tuple(retained_locators),
                coverage=tuple(retained_coverage),
            )
        )
    except Exception:
        # Accepted-path observability cannot become a new delivery dependency.
        return


def emit_source_closure_wire_normalization_trace(
    *,
    raw_candidate: str,
    raw_wire: str,
    normalized_contract: str,
) -> None:
    """Audit a fixed transport-label repair without retaining provider prose."""

    sink = _ACTIVE_SOURCE_CLOSURE_TRACE.get()
    if sink is None:
        return
    try:
        sink.record(
            SourceClosureWireNormalizationTraceEvent(
                candidate_sha256=sha256(raw_candidate.encode("utf-8")).hexdigest(),
                stage="coverage",
                code="missing_negotiated_contract",
                raw_wire_sha256=sha256(raw_wire.encode("utf-8")).hexdigest(),
                normalized_contract=normalized_contract[:128],
            )
        )
    except Exception:
        return


def emit_source_closure_candidate_materialization_failure_trace(
    *,
    raw_candidate: str,
    category: CandidateMaterializationFailureCategory,
    code: str,
    field_paths: tuple[str, ...] = (),
    stage: CandidateMaterializationFailureStage = "post_source_acceptance",
) -> None:
    """Emit only stable structural coordinates after source acceptance.

    The stage is restricted to the two mechanical sides of the final source
    review and the API accepts neither exception objects nor explanatory text.
    Invalid coordinates suppress the diagnostic instead of retaining arbitrary
    provider, reviewer, visible, or private prose.
    """

    sink = _ACTIVE_SOURCE_CLOSURE_TRACE.get()
    if sink is None:
        return
    try:
        if (
            not isinstance(raw_candidate, str)
            or category not in _MATERIALIZATION_FAILURE_CATEGORIES
            or stage not in {"pre_final_source_review", "post_source_acceptance"}
            or not isinstance(code, str)
            or not isinstance(field_paths, tuple)
        ):
            return
        stable_code = _stable_failure_coordinate(code, maximum_bytes=128)
        if stable_code != code:
            return
        bounded_paths = tuple(field_paths)[:_MAX_MATERIALIZATION_FIELD_PATHS]
        if len(field_paths) > _MAX_MATERIALIZATION_FIELD_PATHS:
            return
        stable_paths = tuple(
            _stable_failure_coordinate(path, maximum_bytes=256) for path in bounded_paths
        )
        if any(path is None for path in stable_paths):
            return
        normalized_paths = tuple(
            dict.fromkeys(path for path in stable_paths if path is not None)
        )
        sink.record(
            SourceClosureCandidateMaterializationFailureTraceEvent(
                candidate_sha256=sha256(raw_candidate.encode("utf-8")).hexdigest(),
                stage=stage,
                category=category,
                code=stable_code,
                field_paths=normalized_paths,
            )
        )
    except Exception:
        # Isolated observability must never become a delivery dependency.
        return


def emit_source_closure_wire_failure_trace(
    *,
    raw_candidate: str,
    stage: Literal["inventory", "coverage"],
    code: str,
    field: str,
    provider_attempts: tuple[tuple[Literal["inventory", "coverage"], str], ...] = (),
) -> None:
    """Emit a stable coordinate plus allowlisted wires in an explicit audit."""

    sink = _ACTIVE_SOURCE_CLOSURE_TRACE.get()
    if sink is None:
        return
    try:
        sink.record(
            SourceClosureWireFailureTraceEvent(
                candidate_sha256=sha256(raw_candidate.encode("utf-8")).hexdigest(),
                stage=stage,
                code=str(code)[:128],
                field=str(field)[:256],
                provider_attempts=_bounded_wire_attempts(provider_attempts),
            )
        )
    except Exception:
        # Failure observability cannot become a new delivery dependency.
        return


__all__ = [
    "BoundedSourceClosureTraceCollector",
    "CandidateMaterializationFailureCategory",
    "CandidateMaterializationFailureStage",
    "SourceClosureCandidateMaterializationFailureTraceEvent",
    "SourceClosureTraceEvent",
    "SourceClosureTraceFinding",
    "SourceClosureTraceRecord",
    "SourceClosureTraceSink",
    "SourceClosureTraceStage",
    "SourceClosureVerdictTraceCoverage",
    "SourceClosureVerdictTraceEvent",
    "SourceClosureVerdictTraceLocator",
    "SourceClosureWireAttemptTrace",
    "SourceClosureWireFailureTraceEvent",
    "capture_isolated_source_closure_trace",
    "emit_source_closure_candidate_materialization_failure_trace",
    "emit_source_closure_trace",
    "emit_source_closure_verdict_trace",
    "emit_source_closure_wire_failure_trace",
]
