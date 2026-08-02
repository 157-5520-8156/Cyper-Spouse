"""Bounded model deliberation over a trusted Context Capsule.

Deliberation produces an inert ProposalEnvelope and audit material.  It has no
ledger, action, platform, or domain-mutation capability; ProposalAcceptance is
the only later authority seam.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable, Iterable, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from companion_daemon.llm import model_request_emission_scope

from .affect_target_bounds import AffectTargetLowerBounds
from .context_capsule import ContextCapsule, TrustedContextCapsuleHandle
from .interactive_turn_budget import (
    FIRST_PROVIDER_ENTRY_RESERVE_SECONDS,
    InteractiveTurnBudget,
)
from .expression_episode import (
    ExpressionEpisodeDiagnostics,
    validate_provisional_proposal,
)
from .expression_cadence import CadenceDraw
from .proposal_envelope import (
    MinimalProposal,
    ProposalEvidenceRef,
    ProposalInput,
    validate_proposal_envelope,
)
from .recall_audit import PrefetchPresentationAudit, RecallAuditTrace
from .recall_index import RecallCursor
from .recall_runtime import (
    PresentedPrefetchTrace,
    TrustedRecallTrace,
    verify_trusted_recall_trace,
)
from .route_hints import RouteHints, derive_route_hints


MAX_MODEL_OUTPUT_BYTES = 512_000
MAX_MODEL_OUTPUT_NODES = 16_384
MAX_ROUTE_REASON_CHARACTERS = 128
MAX_REPORTED_TOKENS = 10_000_000
# Process-wide ceilings cover unrelated lanes and detached cancellation audit.
# The expression-unit adapter separately enforces one active visible stream;
# these broader ceilings must not turn background work into a global mutex.
MAX_INFLIGHT_PROVIDER_TASKS = 8
MAX_INFLIGHT_QUICK_TASKS = 2
MAX_INFLIGHT_SHADOW_OBSERVER_TASKS = 2
_PROVIDER_CANCELLATION_AUDIT_GRACE_SECONDS = 0.01
_PROVIDER_CLOSE_GRACE_SECONDS = 0.05
_T = TypeVar("_T")
_LOG = logging.getLogger(__name__)

# Absolute monotonic deadline of the model attempt currently being awaited.
# Deliberation owns the attempt budget; adapters that spend bounded secondary
# calls (semantic reviews, corrective structural retries) read the remaining
# time through :func:`remaining_attempt_seconds` so a repair that cannot fit
# is skipped instead of blowing the whole attempt into a timeout after the
# repair already succeeded.  The variable is advisory-only: the enforcing
# authority remains ``Deliberation._with_deadline``.
_ATTEMPT_DEADLINE: ContextVar[float | None] = ContextVar(
    "world_v2_model_attempt_deadline", default=None
)
_INTERACTIVE_TURN_BUDGET: ContextVar[InteractiveTurnBudget | None] = ContextVar(
    "world_v2_interactive_turn_budget", default=None
)
_FIRST_ROLE_PROVIDER_MARKER: ContextVar[Callable[[str], None] | None] = ContextVar(
    "world_v2_first_role_provider_marker", default=None
)
_FIRST_ROLE_PROVIDER_COMPLETION_MARKER: ContextVar[
    Callable[[str], None] | None
] = ContextVar("world_v2_first_role_provider_completion_marker", default=None)
_FIRST_ROLE_PROVIDER_TOKEN_MARKER: ContextVar[
    Callable[[str], None] | None
] = ContextVar("world_v2_first_role_provider_token_marker", default=None)
_PROVIDER_SLOT_COORDINATOR: ContextVar["_ProviderSlotCoordinator | None"] = ContextVar(
    "world_v2_provider_slot_coordinator", default=None
)
_VALIDATION_ATTEMPT: ContextVar["_ValidationAttemptState | None"] = ContextVar(
    "world_v2_validation_attempt", default=None
)


def mark_interactive_turn_milestone(event: str) -> None:
    """Expose a presentation-neutral milestone through the current turn budget."""

    budget = _INTERACTIVE_TURN_BUDGET.get()
    if budget is not None:
        budget.mark(event)


class _ProviderSlotCoordinator:
    """Process-local bounded provider-call lease shared with cognition adapters."""

    def __init__(self) -> None:
        self.second_kind: Literal["backup", "corrective", "recall"] | None = None
        self.validation_corrective_claimed = False
        self.backup_validation_corrective_claimed = False
        self.backup_claimed = False
        self.episode_reserved = False

    def claim_second(self, kind: Literal["backup", "corrective", "recall"]) -> bool:
        if self.second_kind is not None:
            return False
        self.second_kind = kind
        if kind == "backup":
            self.backup_claimed = True
        elif kind == "corrective":
            self.validation_corrective_claimed = True
        return True

    def claim_failure_recovery(self) -> bool:
        """Reserve one fallback only after the primary candidate has failed.

        A main adapter may already have spent its one corrective call.  That
        correction is part of the failed primary candidate, not the configured
        fallback role model. Permit the latter once; the fallback candidate
        may then claim its own single validation correction, but never another
        fallback or a second correction.
        """

        if self.episode_reserved or self.backup_claimed:
            return False
        self.backup_claimed = True
        if self.second_kind is None:
            self.second_kind = "backup"
        return True

    def claim_validation_corrective(self, *, allow_after_backup: bool) -> bool:
        """Allow one repair for each independently authored candidate.

        A configured recovery author is a new role candidate, not a retry of
        the rejected primary bytes.  It therefore receives its own single
        bounded source/shape re-selection.  This is the rare fourth role call
        only when the primary already spent its correction; a second recovery
        correction remains impossible.
        """

        if self.episode_reserved:
            return False
        if allow_after_backup:
            if not self.backup_claimed or self.backup_validation_corrective_claimed:
                return False
            self.backup_validation_corrective_claimed = True
            return True
        if self.validation_corrective_claimed:
            return False
        if self.second_kind is None:
            self.second_kind = "corrective"
            self.validation_corrective_claimed = True
            return True
        if self.second_kind == "recall":
            self.validation_corrective_claimed = True
            return True
        return False

    @property
    def used_corrective(self) -> bool:
        return (
            self.second_kind == "corrective"
            or self.validation_corrective_claimed
            or self.backup_validation_corrective_claimed
        )


def claim_secondary_provider_slot(
    kind: Literal["backup", "corrective", "recall"],
) -> bool:
    """Claim the turn's only secondary provider call.

    Direct adapter use has no coordinator and keeps its historical one-retry
    behavior.  Interactive Deliberation installs the coordinator.
    """

    coordinator = _PROVIDER_SLOT_COORDINATOR.get()
    return coordinator is None or coordinator.claim_second(kind)


def claim_validation_corrective_provider_slot(
    *,
    allow_after_backup: bool = False,
) -> bool:
    """Claim the one structural correction allowed for a model-owned result.

    Usually this is the second provider call.  A bounded Recall result and each
    configured recovery-author candidate may each receive one correction.
    When both the primary and recovery authors needed correction this is the
    rare fourth role call, still scoped to reselecting that recovery
    candidate. It cannot open another Recall, backup, hedge, provisional
    episode, or a second correction for either author.
    """

    coordinator = _PROVIDER_SLOT_COORDINATOR.get()
    return coordinator is None or coordinator.claim_validation_corrective(
        allow_after_backup=allow_after_backup
    )


def secondary_provider_slot_kind() -> Literal["backup", "corrective", "recall"] | None:
    coordinator = _PROVIDER_SLOT_COORDINATOR.get()
    return coordinator.second_kind if coordinator is not None else None


def has_provider_slot_coordinator() -> bool:
    return _PROVIDER_SLOT_COORDINATOR.get() is not None


def expression_episode_provider_slots_active() -> bool:
    coordinator = _PROVIDER_SLOT_COORDINATOR.get()
    return bool(coordinator is not None and coordinator.episode_reserved)


def remaining_attempt_seconds() -> float | None:
    """Seconds left in the current model attempt, or ``None`` outside one."""

    deadline = _ATTEMPT_DEADLINE.get()
    if deadline is None:
        return None
    validation_state = _VALIDATION_ATTEMPT.get()
    now = time.monotonic()
    if validation_state is not None:
        # Validation deadlines are produced by the turn budget's injected
        # monotonic clock. Keep timeout fitting in that same clock domain;
        # mixing it with the process clock makes deterministic/manual-clock
        # tests—and any alternate monotonic source—look instantly expired.
        now = validation_state.budget.clock()
    if (
        validation_state is not None
        and validation_state.truth_boundary_active
        and validation_state.reselection_deadline is not None
    ):
        deadline = min(
            validation_state.reselection_deadline,
            validation_state.hard_deadline,
        )
    return deadline - now


def fit_secondary_call_timeout(
    default_seconds: float,
    *,
    minimum_seconds: float = 2.0,
    margin_seconds: float = 0.6,
) -> float | None:
    """Bound one secondary in-attempt call to the time that actually remains.

    Returns ``default_seconds`` when no attempt deadline is installed (direct
    adapter use in tests and offline tools), a smaller budget when the attempt
    is close to its deadline, and ``None`` when no useful call fits any more —
    callers must then skip the secondary call instead of paying for a result
    the deadline will discard.
    """

    remaining = remaining_attempt_seconds()
    if remaining is None:
        return default_seconds
    budget = min(default_seconds, remaining - margin_seconds)
    if budget < minimum_seconds:
        return None
    return budget


def fit_pre_provider_wait_timeout(
    default_seconds: float,
    *,
    provider_entry_reserve_seconds: float = FIRST_PROVIDER_ENTRY_RESERVE_SECONDS,
) -> float:
    """Fit optional local preparation into one ingress-relative fast budget.

    The hard author deadline remains unchanged. This softer bound prevents QQ
    coalescing, Recall joining, and prompt preparation from each charging an
    independent serial delay before the first role-provider request.
    """

    for label, value in (
        ("default_seconds", default_seconds),
        ("provider_entry_reserve_seconds", provider_entry_reserve_seconds),
    ):
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{label} must be finite and non-negative")
    turn_budget = _INTERACTIVE_TURN_BUDGET.get()
    if turn_budget is None:
        return default_seconds
    available = (
        turn_budget.first_provider_entry_remaining()
        - provider_entry_reserve_seconds
    )
    return max(0.0, min(default_seconds, available))


def mark_first_role_provider_entry(provider_call_id: str) -> None:
    """Emit optional process timing evidence at the real provider boundary."""

    if not provider_call_id:
        raise ValueError("role-provider call id is required")
    marker = _FIRST_ROLE_PROVIDER_MARKER.get()
    if marker is None:
        return
    try:
        marker(provider_call_id)
    except Exception:
        _LOG.warning("first role-provider latency marker failed", exc_info=True)


def mark_first_role_provider_completion(provider_call_id: str) -> None:
    """Emit the complete-response boundary for a non-streaming role call."""

    if not provider_call_id:
        raise ValueError("role-provider call id is required")
    marker = _FIRST_ROLE_PROVIDER_COMPLETION_MARKER.get()
    if marker is None:
        return
    try:
        marker(provider_call_id)
    except Exception:
        _LOG.warning("role-provider completion latency marker failed", exc_info=True)


def mark_first_role_provider_token(provider_call_id: str) -> None:
    """Emit first streamed content evidence for the exact role request."""

    if not provider_call_id:
        raise ValueError("role-provider call id is required")
    marker = _FIRST_ROLE_PROVIDER_TOKEN_MARKER.get()
    if marker is None:
        return
    try:
        marker(provider_call_id)
    except Exception:
        _LOG.warning("role-provider first-token latency marker failed", exc_info=True)


ValidationTechnicalFailureCode = Literal[
    "source_review_timeout",
    "source_review_exception",
    "authored_subcall_timeout",
    "authored_subcall_exception",
    "recall_choice_reselection_invalid",
    "authored_expression_reselection_invalid",
    "proactive_claim_binding_invalid",
    "affect_target_reselection_invalid",
    "inventory_invalid",
    "coverage_invalid",
]
_NON_RECOVERABLE_VALIDATION_FAILURE_CODES = frozenset(
    {
        "source_review_timeout",
        "source_review_exception",
        "recall_choice_reselection_invalid",
        "authored_expression_reselection_invalid",
        "proactive_claim_binding_invalid",
        "affect_target_reselection_invalid",
        "inventory_invalid",
        "coverage_invalid",
    }
)
_SOURCE_VALIDATION_INVALID_FAILURE_CODES = frozenset(
    {
        "inventory_invalid",
        "coverage_invalid",
    }
)


def _terminal_failure_audit_outcome(
    failure_code: str | None,
) -> Literal["invalid", "timeout", "exception"]:
    """Collapse precise terminal failures into the durable audit taxonomy."""

    if failure_code == "invalid" or failure_code in _SOURCE_VALIDATION_INVALID_FAILURE_CODES:
        return "invalid"
    if failure_code in {
        "timeout",
        "source_review_timeout",
        "authored_subcall_timeout",
    }:
        return "timeout"
    return "exception"


class ValidationTechnicalFailure(RuntimeError):
    """A bounded validation lane reached a terminal technical failure.

    This exception is deliberately distinct from an initial author failure.
    A truth reviewer may have exhausted its retry, or the role may have spent
    its one schema-correction choice and returned another invalid candidate.
    Deliberation must never route either terminal condition into a new
    role-author pass.
    """

    def __init__(
        self,
        failure_code: ValidationTechnicalFailureCode,
        *,
        model_call_id: str | None = None,
        request_hash: str | None = None,
        attempted_model_id: str | None = None,
        attempted_model_version: str | None = None,
        usage: ModelUsageProvenance | None = None,
        provider_subcall_audits: tuple[ProviderSubcallAudit, ...] = (),
        authored_candidate_audits: tuple[AuthoredCandidateInvocationAudit, ...] = (),
    ):
        identity = (model_call_id, request_hash)
        if (identity[0] is None) != (identity[1] is None):
            raise ValueError("validation failure provider identity is partial")
        attempted = (attempted_model_id, attempted_model_version)
        if (attempted[0] is None) != (attempted[1] is None):
            raise ValueError("validation failure attempted-model identity is partial")
        if usage is not None and attempted_model_id is None:
            raise ValueError("validation failure usage requires an attempted model")
        super().__init__(failure_code)
        self.failure_code = failure_code
        self.model_call_id = model_call_id
        self.request_hash = request_hash
        self.attempted_model_id = attempted_model_id
        self.attempted_model_version = attempted_model_version
        self.usage = usage
        self.provider_subcall_audits = tuple(provider_subcall_audits)
        self.authored_candidate_audits = tuple(authored_candidate_audits)


def _validation_failure_with_preserved_attempt(
    failure_code: ValidationTechnicalFailureCode,
    exc: BaseException,
) -> ValidationTechnicalFailure:
    """Reclassify a terminal review failure without erasing its provider call.

    A review operation may already have bound the exact sub-call identity and
    usage before its wire/parser failed. The orchestration layer owns retry
    classification, but it must not replace that evidence with the parent
    character-author call.
    """

    model_call_id = getattr(exc, "model_call_id", None)
    request_hash = getattr(exc, "request_hash", None)
    attempted_model_id = getattr(exc, "attempted_model_id", None)
    attempted_model_version = getattr(exc, "attempted_model_version", None)
    usage = getattr(exc, "usage", None)
    provider_subcall_audits = tuple(
        getattr(exc, "provider_subcall_audits", ())
    )
    authored_candidate_audits = tuple(
        getattr(exc, "authored_candidate_audits", ())
    )
    if model_call_id is None:
        return ValidationTechnicalFailure(
            failure_code,
            provider_subcall_audits=provider_subcall_audits,
            authored_candidate_audits=authored_candidate_audits,
        )
    return ValidationTechnicalFailure(
        failure_code,
        model_call_id=model_call_id,
        request_hash=request_hash,
        attempted_model_id=attempted_model_id,
        attempted_model_version=attempted_model_version,
        usage=usage,
        provider_subcall_audits=provider_subcall_audits,
        authored_candidate_audits=authored_candidate_audits,
    )


class RecoveryCandidateFailure(RuntimeError):
    """Preserve the configured recovery author's terminal failure class.

    Recovery adapters may need to clean up attempt-local state or try an
    explicitly configured independent recovery model before returning to
    Deliberation.  They must not collapse a bounded timeout or invalid model
    result into a generic exception while doing so: the durable lifecycle uses
    this class to retain the real terminal category without persisting raw
    provider errors.
    """

    def __init__(self, failure_kind: Literal["timeout", "invalid", "exception"]):
        super().__init__(f"model-owned recovery failed ({failure_kind})")
        self.failure_kind = failure_kind


class _ValidationAttemptState:
    """Process-local truth-boundary budget attached to one authored candidate."""

    def __init__(
        self,
        *,
        budget: InteractiveTurnBudget,
        author_deadline: float,
        candidate_key: str,
    ) -> None:
        self.budget = budget
        self.author_deadline = author_deadline
        self.candidate_key = candidate_key
        self.recovery_deadline: float | None = None
        self.reselection_deadline: float | None = None
        self.review_started = False
        self._review_inflight_count = 0
        self.truth_boundary_active = False

    @property
    def hard_deadline(self) -> float:
        # The candidate may open one fixed two-attempt reviewer phase and one
        # distinct correction/final-review sequence. Neither phase can renew
        # itself, and their sum is the absolute candidate-local ceiling.
        return (
            self.author_deadline
            + self.budget.validation_recovery_seconds
            + self.budget.validation_reselection_seconds
        )

    @property
    def review_inflight(self) -> bool:
        """True while any concurrent fact-boundary task owns this candidate."""

        return self._review_inflight_count > 0

    def review_started_now(self) -> None:
        self.review_started = True
        self._review_inflight_count += 1

    def review_finished_now(self) -> None:
        self._review_inflight_count = max(0, self._review_inflight_count - 1)

    def fit(self, requested_seconds: float) -> float | None:
        deadline = self.reselection_deadline or self.recovery_deadline or self.author_deadline
        available = max(0.0, deadline - self.budget.clock())
        fitted = min(max(0.0, requested_seconds), available)
        return fitted if fitted > 0 else None

    def begin_recovery(self) -> float | None:
        if self.recovery_deadline is not None:
            return None
        self.recovery_deadline = self.budget.begin_validation_recovery(
            candidate_key=self.candidate_key
        )
        return self.recovery_deadline

    def begin_reselection(self) -> float | None:
        if self.reselection_deadline is not None:
            return None
        self.reselection_deadline = self.budget.begin_validation_reselection(
            candidate_key=self.candidate_key
        )
        return self.reselection_deadline


def begin_validation_reselection_recovery() -> bool:
    """Open one bounded truth window for role re-selection plus re-review.

    A semantic reviewer may reject an otherwise well-shaped role draft just
    before the ordinary author deadline. The doctrine requires one correction
    by that same role model and then another independent review; starting the
    correction without enough time to review it only converts valid provider
    work into deterministic silence. This candidate-local window authorizes
    exactly that already-triggered source-boundary sequence. It does not open
    Recall, a hedge, generic author recovery, or another correction.
    """

    state = _VALIDATION_ATTEMPT.get()
    if state is None:
        return True
    state.truth_boundary_active = True
    if state.reselection_deadline is None:
        state.begin_reselection()
    return (
        state.reselection_deadline is not None
        and min(state.reselection_deadline, state.hard_deadline) > state.budget.clock()
    )


async def run_validation_review(
    operation: Callable[[], Awaitable[_T]],
    *,
    timeout_seconds: float,
) -> _T:
    """Run one source-review operation, retrying only that reviewer once.

    The role-authored draft lives in the caller's stack and is never regenerated
    here. Under interactive Deliberation, the first review opens one fixed
    candidate-local validation window before provider dispatch. That prevents a
    near-deadline author result from launching a review with only the scraps of
    the author window. Only a real first transport/wire failure dispatches the
    second attempt; the fixed phase can contain two complete per-attempt
    ceilings, but neither call can renew it. Direct/offline adapter calls retain
    the same two bounded attempts without gaining author recovery.
    """

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("validation review timeout must be finite and positive")
    state = _VALIDATION_ATTEMPT.get()
    if state is not None:
        state.review_started_now()
        if state.reselection_deadline is None and state.recovery_deadline is None:
            state.begin_recovery()

    async def attempt(timeout: float) -> _T:
        return await asyncio.wait_for(operation(), timeout=timeout)

    try:
        first_error: Exception
        try:
            first_timeout = timeout_seconds if state is None else state.fit(timeout_seconds)
            if first_timeout is None:
                raise TimeoutError("source review author window exhausted")
            return await attempt(first_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            first_error = exc
            if bool(getattr(exc, "validation_attempts_exhausted", False)):
                failure_code = getattr(exc, "failure_code", "source_review_exception")
                if failure_code not in {
                    "source_review_timeout",
                    "source_review_exception",
                }:
                    failure_code = "source_review_exception"
                raise _validation_failure_with_preserved_attempt(
                    failure_code,
                    exc,
                ) from exc

        try:
            if state is not None:
                if state.reselection_deadline is not None or state.recovery_deadline is not None:
                    # Initial and final reviews are distinct reviewer results.
                    # Each may retry its own first
                    # transport/wire failure once inside whichever
                    # candidate-local phase is already open; no call renews
                    # that phase deadline.
                    retry_timeout = state.fit(timeout_seconds)
                else:
                    if state.begin_recovery() is None:
                        raise first_error
                    retry_timeout = state.fit(timeout_seconds)
            else:
                retry_timeout = timeout_seconds
            if retry_timeout is None:
                raise TimeoutError("source review recovery window exhausted")
            return await attempt(retry_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_code: Literal["source_review_timeout", "source_review_exception"] = (
                "source_review_timeout"
                if isinstance(exc, TimeoutError)
                else "source_review_exception"
            )
            raise _validation_failure_with_preserved_attempt(
                failure_code,
                exc,
            ) from exc
    finally:
        if state is not None:
            state.review_finished_now()


_EVENT_EVIDENCE_KIND: dict[str, str] = {
    "ObservationRecorded": "observed_message",
    "FactCommitted": "committed_fact",
    "FactCorrected": "committed_fact",
    "FactWithdrawn": "committed_fact",
    "ExperienceCommitted": "committed_experience",
    "WorldOccurrenceSettled": "settled_world_event",
    "ActivityPlanned": "active_plan",
    "ActivityStarted": "active_plan",
    "ActivityPaused": "active_plan",
    "ActivityResumed": "active_plan",
}


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _model_result_ref(model_call_id: str, response_hash: str | None) -> str:
    return (
        f"model-result:{_digest({'model_call_id': model_call_id, 'response_hash': response_hash})}"
    )


def _output_response_hash(output: ModelOutput) -> str:
    if (
        output.recall_trace is None
        and output.prefetch_trace is None
        and not output.presented_prefetch_traces
    ):
        return _digest(output.raw_proposal)
    return _digest(
        {
            "raw_proposal": output.raw_proposal,
            "recall_trace": (
                verify_trusted_recall_trace(output.recall_trace).model_dump(mode="json")
                if output.recall_trace is not None
                else None
            ),
            "prefetch_trace": (
                verify_trusted_recall_trace(output.prefetch_trace).model_dump(mode="json")
                if output.prefetch_trace is not None
                else None
            ),
            "presented_prefetch_traces": tuple(
                item.recorded().model_dump(mode="json") for item in output.presented_prefetch_traces
            ),
        }
    )


def _bounded_raw(value: object, *, label: str) -> None:
    pending = [value]
    seen = 0
    characters = 0
    while pending:
        item = pending.pop()
        seen += 1
        if seen > MAX_MODEL_OUTPUT_NODES:
            raise ValueError(f"{label} exceeds node limit")
        if isinstance(item, str):
            characters += len(item.encode("utf-8"))
            if characters > MAX_MODEL_OUTPUT_BYTES:
                raise ValueError(f"{label} exceeds byte limit")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, list)):
            pending.extend(item)
        elif isinstance(item, bool) or item is None or isinstance(item, datetime):
            continue
        elif isinstance(item, int):
            if item.bit_length() > 128:
                raise ValueError(f"{label} contains an oversized integer")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{label} contains a non-finite number")
        else:
            raise ValueError(f"{label} contains unsupported data")


def _checked_output(value: object) -> ModelOutput:
    """Validate an adapter result without serializing attacker-sized model_construct data."""

    if isinstance(value, ModelOutput):
        raw = getattr(value, "raw_proposal", None)
        usage = getattr(value, "usage", None)
        material: object = {
            "model_id": getattr(value, "model_id", None),
            "model_version": getattr(value, "model_version", None),
            "raw_proposal": raw,
            "input_tokens": getattr(value, "input_tokens", None),
            "output_tokens": getattr(value, "output_tokens", None),
            "winning_model_call_id": getattr(value, "winning_model_call_id", None),
            "winning_request_hash": getattr(value, "winning_request_hash", None),
            "provider_parent_model_call_id": getattr(
                value,
                "provider_parent_model_call_id",
                None,
            ),
            "semantic_stream_part": getattr(value, "semantic_stream_part", None),
            "physical_provider_audits": tuple(
                item.model_dump(mode="python")
                if isinstance(item, PhysicalProviderInvocationAudit)
                else item
                for item in getattr(value, "physical_provider_audits", ())
            ),
            "provider_subcall_audits": tuple(
                item.model_dump(mode="python")
                if isinstance(item, ProviderSubcallAudit)
                else item
                for item in getattr(value, "provider_subcall_audits", ())
            ),
            "authored_candidate_audits": tuple(
                item.model_dump(mode="python")
                if isinstance(item, AuthoredCandidateInvocationAudit)
                else item
                for item in getattr(value, "authored_candidate_audits", ())
            ),
            "episode_disposition": getattr(value, "episode_disposition", None),
            "recall_trace": (
                recall_trace.model_dump(mode="python")
                if isinstance(
                    (recall_trace := getattr(value, "recall_trace", None)),
                    TrustedRecallTrace,
                )
                else recall_trace
            ),
            "prefetch_trace": (
                prefetch_trace.model_dump(mode="python")
                if isinstance(
                    (prefetch_trace := getattr(value, "prefetch_trace", None)),
                    TrustedRecallTrace,
                )
                else prefetch_trace
            ),
            "presented_prefetch_traces": tuple(
                item.model_dump(mode="python") if isinstance(item, PresentedPrefetchTrace) else item
                for item in getattr(value, "presented_prefetch_traces", ())
            ),
            # A validated provenance model is still untrusted adapter output at
            # this boundary.  Convert it to bounded primitives before the
            # hostile-shape walk; otherwise every metered production response
            # is rejected merely because it contains a Pydantic object.
            "usage": usage.model_dump(mode="python")
            if isinstance(usage, ModelUsageProvenance)
            else usage,
        }
    else:
        material = value
        raw = value.get("raw_proposal") if isinstance(value, dict) else None
    _bounded_raw(material, label="model output")
    return ModelOutput.model_validate(material)


def _checked_route(value: object) -> ModelRoute:
    if isinstance(value, ModelRoute):
        material: object = {
            "tier": getattr(value, "tier", None),
            "reason_code": getattr(value, "reason_code", None),
            "router_version": getattr(value, "router_version", None),
        }
    else:
        material = value
    _bounded_raw(material, label="model route")
    return ModelRoute.model_validate(material)


class ModelRoute(_FrozenModel):
    tier: Literal["flash", "thinking"] = "flash"
    reason_code: str = Field(min_length=1, max_length=MAX_ROUTE_REASON_CHARACTERS)
    router_version: str = Field(min_length=1, max_length=128)


class RouteRequest(_FrozenModel):
    capsule_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger_ref: str = Field(min_length=1, max_length=256)
    model_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    route_hints: RouteHints = Field(default_factory=RouteHints)

    @model_validator(mode="after")
    def hints_belong_to_capsule(self) -> RouteRequest:
        if (
            self.route_hints.source_capsule_id is not None
            and self.route_hints.source_capsule_id != self.capsule_id
        ):
            raise ValueError("route hints do not belong to the requested capsule")
        return self


class TurnAttentionAdvisory(_FrozenModel):
    """Provider-local endpoint evidence shown to the role as non-authority.

    The endpoint model is deliberately reused only as an attention signal. It
    cannot select a reply, an interruption, or a silence; those remain fields
    authored by the character model below.
    """

    continuation_probability_bp: int = Field(ge=0, le=10_000)
    confidence_bp: int = Field(ge=0, le=10_000)
    typing_active: bool
    status: Literal["predicted", "fallback"]
    model_id: str | None = Field(default=None, min_length=1, max_length=256)
    evidence_summary: str = Field(min_length=1, max_length=512)
    reason_codes: tuple[str, ...] = Field(default=(), max_length=8)
    authority: Literal["advisory_only"] = "advisory_only"

    @model_validator(mode="after")
    def reason_codes_are_bounded(self) -> "TurnAttentionAdvisory":
        if any(not item or len(item) > 64 for item in self.reason_codes):
            raise ValueError("turn attention advisory reason codes are invalid")
        return self


class TriggerMessage(_FrozenModel):
    """Current user text with the exact event evidence that authorizes it.

    A world snapshot alone is insufficient for a conversational decision: the
    model must see the message it is answering.  This is intentionally not a
    free-form prompt extension.  ``Deliberation`` accepts it only when its
    event reference and immutable hash match the pinned observed-message
    evidence for the capsule's trigger.
    """

    event_ref: str = Field(min_length=1, max_length=256)
    event_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observation_ref: str = Field(min_length=1, max_length=256)
    source_world_revision: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=256)
    channel: str = Field(min_length=1, max_length=256)
    reply_target: str = Field(min_length=1, max_length=256)
    # Provider identity is derived from the committed Observation and is used
    # only to bind operations such as reacting to that exact inbound message.
    # A model can choose a reaction token but cannot choose or redirect this ID.
    platform_message_id: str | None = Field(default=None, min_length=1, max_length=256)
    text: str | None = Field(default=None, min_length=1, max_length=12_000)
    attachment_refs: tuple[str, ...] = Field(default=(), max_length=16)
    attachment_media_types: tuple[Literal["image", "audio", "video", "file", "unknown"], ...] = (
        Field(default=(), max_length=16)
    )
    # The endpoint estimate is current transport evidence, not a response
    # instruction. Keeping it on the verified trigger prevents it from being
    # mistaken for a durable World fact or a host-selected social rule.
    turn_attention_advisory: TurnAttentionAdvisory | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def bounded_content_shape(self) -> TriggerMessage:
        if self.text is None and not self.attachment_refs:
            raise ValueError("trigger message needs text or attachment evidence")
        if len(self.attachment_refs) != len(self.attachment_media_types):
            raise ValueError("attachment media metadata does not align with opaque refs")
        if any(not item or len(item) > 512 for item in self.attachment_refs):
            raise ValueError("attachment refs must be bounded opaque tokens")
        return self


class ModelInput(_FrozenModel):
    call_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    route: ModelRoute
    capsule_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger_ref: str = Field(min_length=1, max_length=256)
    evaluated_world_revision: int = Field(ge=0)
    evaluated_deliberation_revision: int = Field(default=0, ge=0)
    evaluated_ledger_sequence: int = Field(default=0, ge=0)
    model_content_json: str = Field(min_length=2, max_length=512_000)
    trigger_evidence: tuple[ProposalEvidenceRef, ...] = Field(default=(), max_length=8)
    trigger_message: TriggerMessage | None = None
    affect_target_bounds: AffectTargetLowerBounds | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    catalog_versions: tuple[str, ...] = ()
    recorded_draw_refs: tuple[str, ...] = ()
    # Values are reconstructable from RandomDrawRecorded; refs remain in the
    # hashed/audited request while this process-local convenience is excluded.
    recorded_cadence_draws: tuple[CadenceDraw, ...] = Field(default=(), exclude=True)

    @model_validator(mode="after")
    def affect_target_bounds_bind_the_exact_cursor(self) -> "ModelInput":
        bounds = self.affect_target_bounds
        if bounds is None:
            return self
        if (
            bounds.source_world_revision != self.evaluated_world_revision
            or bounds.source_deliberation_revision != self.evaluated_deliberation_revision
            or bounds.source_ledger_sequence != self.evaluated_ledger_sequence
        ):
            raise ValueError("affect target lower bounds do not bind the ModelInput cursor")
        return self


class ModelUsageProvenance(_FrozenModel):
    """Bounded provider usage material returned by one model adapter call."""

    usage_contract: Literal["model-usage.1"] = "model-usage.1"
    route_class: Literal[
        "chat", "expressive", "world_action", "deep_deliberation", "quick_recovery"
    ]
    input_tokens: int = Field(ge=0, le=MAX_REPORTED_TOKENS)
    output_tokens: int = Field(ge=0, le=MAX_REPORTED_TOKENS)
    thinking_tokens: int = Field(ge=0, le=MAX_REPORTED_TOKENS)
    token_provenance: Literal["provider_reported", "offline_estimated"]
    transport: Literal["provider_api", "offline_fixture"]
    provider: str = Field(min_length=1, max_length=128)
    provider_usage_ref: str = Field(min_length=1, max_length=256)
    provider_usage_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def provider_usage_hash_binds_metering_fields(self) -> "ModelUsageProvenance":
        material = self.model_dump(mode="json", exclude={"provider_usage_hash"})
        if self.provider_usage_hash != _digest(material):
            raise ValueError("provider usage hash is not bound to metering fields")
        return self


class AuthoredCandidateInvocationAudit(_FrozenModel):
    """One returned role-author invocation recorded outside the terminal audit.

    On success the final author remains the owning :class:`ModelResultAudit`.
    When later validation ends technically, even that returned candidate is
    recorded here while the owning audit describes the orchestration failure.
    This keeps response identity and provider usage truthful without claiming
    that an unresolved candidate was accepted.
    """

    purpose: str = Field(min_length=1, max_length=64)
    model_call_id: str = Field(min_length=1, max_length=256)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=256)
    outcome: Literal[
        "superseded",
        "validation_rejected",
        "validation_unresolved",
    ]
    usage: ModelUsageProvenance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class ProviderSubcallAudit(_FrozenModel):
    """Exact identity of one provider invocation nested inside an author call.

    These values are process-local until :class:`ProposalAuditRecorder`
    expands them into their own immutable ``ModelResultRecorded`` events.
    Prompt and response bytes are never retained here.
    """

    purpose: str = Field(min_length=1, max_length=64)
    parent_model_call_id: str = Field(min_length=1, max_length=256)
    model_call_id: str = Field(min_length=1, max_length=256)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=256)
    lane: Literal["primary", "secondary", "direct"]
    outcome: Literal["winner", "timeout", "exception"]
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        exclude_if=lambda value: value is None,
    )
    response_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    usage: ModelUsageProvenance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def response_matches_outcome(self) -> "ProviderSubcallAudit":
        if (self.outcome == "winner") != (self.response_hash is not None):
            raise ValueError("provider subcall winner must bind response bytes")
        if self.outcome == "winner" and self.failure_code is not None:
            raise ValueError("provider subcall winner cannot carry a failure code")
        return self


class PhysicalProviderInvocationAudit(_FrozenModel):
    """Terminal evidence for one physical streamed provider request.

    Head and tail are separate semantic results derived from this request.
    Missing provider usage remains explicit; zero tokens are never invented.
    """

    purpose: Literal["expression_unit_stream"] = "expression_unit_stream"
    model_call_id: str = Field(min_length=1, max_length=256)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=256)
    outcome: Literal["completed", "cancelled", "unresolved"]
    failure_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        exclude_if=lambda value: value is None,
    )
    response_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    usage_status: Literal["provider_reported", "unresolved", "cancelled"]
    usage: ModelUsageProvenance | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    semantic_model_call_ids: tuple[str, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def terminal_evidence_is_truthful(self) -> "PhysicalProviderInvocationAudit":
        if len(set(self.semantic_model_call_ids)) != len(self.semantic_model_call_ids):
            raise ValueError("stream semantic result identities must be distinct")
        if self.model_call_id in self.semantic_model_call_ids:
            raise ValueError("physical provider identity cannot also be a semantic unit")
        completed = self.outcome == "completed"
        if completed != (self.response_hash is not None):
            raise ValueError("completed physical provider call must bind full response bytes")
        if completed and self.failure_code is not None:
            raise ValueError("completed physical provider call cannot carry a failure code")
        if not completed and self.failure_code is None:
            raise ValueError("incomplete physical provider call requires a failure code")
        if (self.usage_status == "provider_reported") != (self.usage is not None):
            raise ValueError("reported physical usage must carry provider provenance")
        if self.outcome == "cancelled" and self.usage_status not in {
            "cancelled",
            "provider_reported",
        }:
            raise ValueError("cancelled physical call has an invalid usage state")
        if self.outcome == "unresolved" and self.usage_status not in {
            "unresolved",
            "provider_reported",
        }:
            raise ValueError("unresolved physical call has an invalid usage state")
        return self


class ModelOutput(_FrozenModel):
    model_id: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=256)
    raw_proposal: dict[str, Any]
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    usage: ModelUsageProvenance | None = Field(default=None, exclude_if=lambda value: value is None)
    winning_model_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        exclude_if=lambda value: value is None,
    )
    winning_request_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    provider_parent_model_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        exclude_if=lambda value: value is None,
    )
    semantic_stream_part: Literal["head", "tail"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    physical_provider_audits: tuple[PhysicalProviderInvocationAudit, ...] = Field(
        default=(),
        max_length=1,
        exclude=True,
    )
    provider_subcall_audits: tuple[ProviderSubcallAudit, ...] = Field(
        default=(),
        max_length=16,
        exclude=True,
    )
    authored_candidate_audits: tuple[AuthoredCandidateInvocationAudit, ...] = Field(
        default=(),
        max_length=8,
        exclude=True,
    )
    recall_trace: TrustedRecallTrace | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    prefetch_trace: TrustedRecallTrace | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    presented_prefetch_traces: tuple[PresentedPrefetchTrace, ...] = Field(
        default=(),
        max_length=4,
        exclude_if=lambda value: not value,
    )
    episode_disposition: (
        Literal[
            "complete_without_more",
            "append",
            "cancel_pending",
            "supersede_pending",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def usage_matches_legacy_token_fields(self) -> "ModelOutput":
        if self.usage is not None and (self.input_tokens, self.output_tokens) != (
            self.usage.input_tokens,
            self.usage.output_tokens,
        ):
            raise ValueError("model output usage tokens do not match token fields")
        if (self.winning_model_call_id is None) != (self.winning_request_hash is None):
            raise ValueError("winning provider invocation identity must be complete")
        if (
            self.provider_parent_model_call_id is not None
            and self.provider_parent_model_call_id == self.winning_model_call_id
        ):
            raise ValueError("stream unit identity must differ from its provider parent")
        if (self.provider_parent_model_call_id is None) != (
            self.semantic_stream_part is None
        ):
            raise ValueError("stream semantic part requires a physical provider parent")
        if self.physical_provider_audits:
            parent = self.physical_provider_audits[0]
            bound_stream_tail = (
                self.semantic_stream_part == "tail"
                and self.winning_model_call_id in parent.semantic_model_call_ids
                and self.provider_parent_model_call_id == parent.model_call_id
                and self.winning_request_hash == parent.request_hash
            )
            corrected_after_stream = (
                self.semantic_stream_part is None
                and self.provider_parent_model_call_id is None
                and self.winning_model_call_id not in parent.semantic_model_call_ids
            )
            if not (bound_stream_tail or corrected_after_stream):
                raise ValueError("physical provider audit is not bound to its stream tail")
        subcall_ids = tuple(item.model_call_id for item in self.provider_subcall_audits)
        if len(subcall_ids) != len(set(subcall_ids)):
            raise ValueError("provider subcalls require distinct invocation identities")
        candidate_ids = tuple(
            item.model_call_id for item in self.authored_candidate_audits
        )
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("authored candidates require distinct invocation identities")
        all_call_ids = (
            *((self.winning_model_call_id,) if self.winning_model_call_id is not None else ()),
            *candidate_ids,
            *subcall_ids,
        )
        if len(all_call_ids) != len(set(all_call_ids)):
            raise ValueError("all captured provider invocations require distinct identities")
        allowed_parents = set(candidate_ids)
        if self.winning_model_call_id is not None:
            allowed_parents.add(self.winning_model_call_id)
        if any(
            item.parent_model_call_id not in allowed_parents
            or item.parent_model_call_id == item.model_call_id
            for item in self.provider_subcall_audits
        ):
            raise ValueError(
                "provider subcall parent is not a captured authored candidate"
            )
        return self


class ModelRouterAdapter(Protocol):
    async def route(self, request: RouteRequest) -> ModelRoute: ...


class DeliberationModelAdapter(Protocol):
    async def propose(self, request: ModelInput) -> ModelOutput: ...


class QuickRecoveryAdapter(Protocol):
    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput: ...


class ProposalGrammar(Protocol):
    """Composition-owned allow-list for otherwise inert proposal envelopes."""

    def validate(self, proposal: ProposalInput) -> None: ...


AuditStatus = Literal[
    "proposal_validated",
    "candidate_returned",
    "main_timeout",
    "main_invalid",
    "main_exception",
    "main_timeout_recovered",
    "main_invalid_recovered",
    "main_exception_recovered",
    "recovery_failed",
    "provider_completed",
    "provider_cancelled",
    "provider_unresolved",
]


@dataclass(frozen=True, slots=True)
class _TerminalValidationAudit:
    status: AuditStatus
    failure_code: ValidationTechnicalFailureCode
    outcome: Literal["invalid", "timeout", "exception"]
    slot: Literal["primary", "corrective"]


def _map_terminal_validation_failure(
    failure_code: ValidationTechnicalFailureCode,
    *,
    corrective_claimed: bool,
) -> _TerminalValidationAudit:
    return _TerminalValidationAudit(
        status=(
            "main_timeout"
            if failure_code
            in {"source_review_timeout", "authored_subcall_timeout"}
            else "main_exception"
        ),
        failure_code=failure_code,
        outcome=_terminal_failure_audit_outcome(failure_code),
        slot="corrective" if corrective_claimed else "primary",
    )


class ModelResultAudit(_FrozenModel):
    model_call_id: str = Field(min_length=1)
    parent_model_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        exclude_if=lambda value: value is None,
    )
    semantic_stream_part: Literal["head", "tail"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    usage_status: Literal["provider_reported", "unresolved", "cancelled"] | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    semantic_model_call_ids: tuple[str, ...] = Field(
        default=(),
        max_length=2,
        exclude_if=lambda value: not value,
    )
    model_result_ref: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    route: ModelRoute
    model_id: str | None = None
    model_version: str | None = None
    attempted_model_id: str | None = Field(
        default=None,
        max_length=256,
        exclude_if=lambda value: value is None,
    )
    attempted_model_version: str | None = Field(
        default=None,
        max_length=256,
        exclude_if=lambda value: value is None,
    )
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: AuditStatus
    failure_code: str | None = Field(default=None, max_length=64)
    slot: Literal["primary", "backup", "corrective"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    outcome: (
        Literal[
            "winner",
            "returned",
            "invalid",
            "timeout",
            "exception",
            "hedge_cancelled",
            "hedge_lost",
            "budget_exhausted",
            "completed",
            "cancelled",
            "unresolved",
        ]
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    usage: ModelUsageProvenance | None = Field(default=None, exclude_if=lambda value: value is None)
    recall_trace: RecallAuditTrace | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    prefetch_trace: RecallAuditTrace | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    presented_prefetch_traces: tuple[PrefetchPresentationAudit, ...] = Field(
        default=(),
        max_length=4,
        exclude_if=lambda value: not value,
    )
    provider_subcall_audits: tuple[ProviderSubcallAudit, ...] = Field(
        default=(),
        max_length=16,
        exclude=True,
    )
    authored_candidate_audits: tuple[AuthoredCandidateInvocationAudit, ...] = Field(
        default=(),
        max_length=8,
        exclude=True,
    )
    physical_provider_audits: tuple[PhysicalProviderInvocationAudit, ...] = Field(
        default=(),
        max_length=1,
        exclude=True,
    )

    @model_validator(mode="after")
    def result_ref_is_orchestrator_derived(self) -> ModelResultAudit:
        if self.parent_model_call_id == self.model_call_id:
            raise ValueError("model result cannot be its own provider parent")
        # Nested reviewer/reselection audits already use parent_model_call_id
        # to bind a provider subcall to its authored candidate.  Only a
        # semantic stream part *requires* a physical parent; a parent by
        # itself does not make an ordinary nested audit a stream unit.
        if self.semantic_stream_part is not None and self.parent_model_call_id is None:
            raise ValueError("stream semantic audit requires its physical provider parent")
        physical_status = self.status.startswith("provider_")
        if physical_status != bool(self.semantic_model_call_ids):
            raise ValueError("physical provider terminal must bind its semantic lineage")
        if physical_status:
            if self.parent_model_call_id is not None or self.semantic_stream_part is not None:
                raise ValueError("physical provider terminal cannot have a provider parent")
            if self.model_call_id in self.semantic_model_call_ids or len(
                set(self.semantic_model_call_ids)
            ) != len(self.semantic_model_call_ids):
                raise ValueError("physical provider semantic lineage is invalid")
            expected_outcome = {
                "provider_completed": "completed",
                "provider_cancelled": "cancelled",
                "provider_unresolved": "unresolved",
            }[self.status]
            if self.outcome != expected_outcome or self.slot != "primary":
                raise ValueError("physical provider terminal has an invalid outcome")
            if (self.usage_status == "provider_reported") != (self.usage is not None):
                raise ValueError("physical provider usage status is not truthful")
        elif self.usage_status is not None:
            raise ValueError("usage status is reserved for physical provider terminals")
        if self.model_result_ref != _model_result_ref(self.model_call_id, self.response_hash):
            raise ValueError("model result ref is not bound to its call")
        identity = (self.model_id, self.model_version, self.response_hash)
        has_output = all(value is not None for value in identity)
        if not has_output and any(value is not None for value in identity):
            raise ValueError("model output audit identity is partial")
        attempted_identity = (self.attempted_model_id, self.attempted_model_version)
        has_attempted_identity = all(value is not None for value in attempted_identity)
        if not has_attempted_identity and any(value is not None for value in attempted_identity):
            raise ValueError("attempted model audit identity is partial")
        if (
            not has_output
            and not has_attempted_identity
            and (self.input_tokens is not None or self.output_tokens is not None)
        ):
            raise ValueError("model token counts require an output or attempted identity")
        if self.recall_trace is not None and not has_output:
            raise ValueError("recall trace requires a model output identity")
        if (self.prefetch_trace is not None or self.presented_prefetch_traces) and not has_output:
            raise ValueError("prefetch trace requires a model output identity")
        if self.prefetch_trace is not None and self.presented_prefetch_traces:
            raise ValueError("ordered prefetch presentations supersede the legacy singular trace")
        if self.usage is not None:
            if not has_output and not has_attempted_identity:
                raise ValueError("model usage requires an output or attempted identity")
            if (self.input_tokens, self.output_tokens) != (
                self.usage.input_tokens,
                self.usage.output_tokens,
            ):
                raise ValueError("model usage tokens do not match audit tokens")
            # ``route.tier`` records the role-author route requested by the
            # orchestrator. Provider-reported usage records what actually ran,
            # including hidden reasoning in a recovery or truth-review
            # subcall. Preserve that evidence even when it exceeds the route's
            # expectation; telemetry may warn, but must not erase a valid
            # character result.
        if physical_status:
            if self.status == "provider_completed":
                if not has_output or self.failure_code is not None:
                    raise ValueError("completed physical provider audit lacks response identity")
            elif has_output or not has_attempted_identity or self.failure_code is None:
                raise ValueError("incomplete physical provider audit has invalid identity")
            return self
        required_failures = {
            "main_timeout": {
                "main_timeout",
                "primary_timeout",
                "corrective_timeout",
                "source_review_timeout",
                "authored_subcall_timeout",
            },
            "main_invalid": {
                "main_invalid_output",
                "primary_invalid",
                "corrective_invalid",
            },
            "main_exception": {
                "main_exception",
                "primary_exception",
                "source_review_exception",
                "authored_subcall_exception",
                "recall_choice_reselection_invalid",
                "authored_expression_reselection_invalid",
                "proactive_claim_binding_invalid",
                "affect_target_reselection_invalid",
                "inventory_invalid",
                "coverage_invalid",
                "stream_superseded_by_newer_input",
                "stream_tail_cancelled",
                "stream_tail_unresolved",
            },
            "main_timeout_recovered": {
                "main_timeout",
                "primary_timeout",
                "corrective_timeout",
                "authored_subcall_timeout",
            },
            "main_invalid_recovered": {
                "main_invalid_output",
                "primary_invalid",
                "corrective_invalid",
            },
            "main_exception_recovered": {
                "main_exception",
                "primary_exception",
                "authored_subcall_exception",
            },
        }.get(self.status)
        provider_failure_type, separator, provider_failure_detail = (
            (self.failure_code or "").partition(":")
        )
        typed_provider_subcall_failure = (
            self.route.router_version == "provider-subcall-audit.1"
            and self.outcome in {"timeout", "exception"}
            and (
                (
                    self.outcome == "timeout"
                    and self.failure_code in {"provider_timeout", "caller_cancelled"}
                )
                or (
                    self.outcome == "exception"
                    and provider_failure_type.replace("_", "").isalnum()
                    and (
                        (separator == "" and provider_failure_detail == "")
                        or (
                            separator == ":"
                            and provider_failure_detail.startswith("http_")
                            and provider_failure_detail.removeprefix("http_").isdigit()
                            and 100
                            <= int(provider_failure_detail.removeprefix("http_"))
                            <= 599
                        )
                    )
                )
            )
        )
        if self.status == "proposal_validated":
            if not has_output or self.failure_code is not None:
                raise ValueError("validated proposal audit requires output and no failure")
        elif self.status == "candidate_returned":
            if (
                not has_output
                or self.failure_code is not None
                or self.outcome != "returned"
            ):
                raise ValueError(
                    "returned candidate audit requires output without semantic acceptance"
                )
        elif self.status in {"main_timeout", "main_exception"}:
            if has_output or (
                self.failure_code not in (required_failures or set())
                and not typed_provider_subcall_failure
            ):
                raise ValueError("terminal main audit has an invalid output or failure")
        elif self.status == "main_invalid":
            if self.failure_code not in (required_failures or set()):
                raise ValueError("invalid main audit has the wrong failure code")
        elif self.status == "recovery_failed":
            if not (
                (self.failure_code or "").startswith("quick_")
                or (self.failure_code or "").startswith("backup_")
                or (self.failure_code or "").startswith("corrective_")
            ):
                raise ValueError("failed recovery audit requires a quick failure code")
        elif not has_output or self.failure_code not in (required_failures or set()):
            raise ValueError("recovered audit lacks output or matching main failure")
        if (self.slot is None) != (self.outcome is None):
            raise ValueError("slot and outcome audit metadata must appear together")
        return self


class ProviderHealth(_FrozenModel):
    main_inflight: int = Field(ge=0)
    main_ceiling: int = Field(ge=1)
    quick_inflight: int = Field(ge=0)
    quick_ceiling: int = Field(ge=1)
    main_circuit_open: bool
    quick_circuit_open: bool


class DeliberationResult(_FrozenModel):
    result_id: str = Field(min_length=1)
    capsule_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal: ProposalInput | None
    audit: ModelResultAudit
    attempt_audits: tuple[ModelResultAudit, ...] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def failure_has_no_proposal(self) -> DeliberationResult:
        if self.audit not in self.attempt_audits:
            raise ValueError("final audit must belong to the model-attempt audits")
        call_ids = tuple(value.model_call_id for value in self.attempt_audits)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("model attempts require distinct call identities")
        all_provider_call_ids = (
            *call_ids,
            *(
                item.model_call_id
                for audit in self.attempt_audits
                for item in audit.provider_subcall_audits
            ),
            *(
                item.model_call_id
                for audit in self.attempt_audits
                for item in audit.authored_candidate_audits
            ),
            *(
                item.model_call_id
                for audit in self.attempt_audits
                for item in audit.physical_provider_audits
            ),
        )
        if len(all_provider_call_ids) != len(set(all_provider_call_ids)):
            raise ValueError("all provider invocations require distinct call identities")
        if (
            isinstance(self.proposal, MinimalProposal)
            and self.proposal.source_model_result != self.audit.model_result_ref
        ):
            raise ValueError("minimal proposal is not bound to its final model audit")
        if len(self.attempt_audits) == 1:
            if self.proposal is None:
                if (
                    self.audit.semantic_stream_part == "tail"
                    and self.audit.status == "candidate_returned"
                    and self.audit.outcome == "returned"
                ):
                    pass
                elif self.audit.outcome not in {
                    "invalid",
                    "timeout",
                    "exception",
                    "budget_exhausted",
                }:
                    raise ValueError("single failed attempt lacks a terminal outcome")
            elif self.audit.status != "proposal_validated":
                raise ValueError("single successful attempt must validate a proposal")
        else:
            main, quick = self.attempt_audits
            primary_won_race = (
                self.proposal is not None
                and self.audit == quick
                and quick.status == "proposal_validated"
                and main.status == "recovery_failed"
                and main.failure_code in {"backup_cancelled", "backup_lost"}
            )
            if not primary_won_race:
                expected = {
                    "main_timeout": (
                        {
                            "main_timeout",
                            "primary_timeout",
                            "corrective_timeout",
                            "authored_subcall_timeout",
                        },
                        "main_timeout_recovered",
                    ),
                    "main_invalid": (
                        {"main_invalid_output", "primary_invalid", "corrective_invalid"},
                        "main_invalid_recovered",
                    ),
                    "main_exception": (
                        {
                            "main_exception",
                            "primary_exception",
                            "authored_subcall_exception",
                        },
                        "main_exception_recovered",
                    ),
                }.get(main.status)
                if expected is None or main.failure_code not in expected[0]:
                    raise ValueError("recovery lineage has an invalid main terminal audit")
                if quick.status == "recovery_failed":
                    if self.proposal is not None or not (
                        (quick.failure_code or "").startswith("quick_")
                        or (quick.failure_code or "").startswith("backup_")
                        or (quick.failure_code or "").startswith("corrective_")
                    ):
                        raise ValueError("failed recovery has invalid proposal or failure code")
                elif quick.status != expected[1] or quick.failure_code != main.failure_code:
                    raise ValueError("successful recovery does not match its main failure")
                elif self.proposal is None:
                    raise ValueError("successful recovery requires a proposal")
                elif (
                    isinstance(self.proposal, MinimalProposal)
                    and self.proposal.source_model_result != quick.model_result_ref
                ):
                    raise ValueError("minimal proposal is not bound to its final model audit")
            if main.attempt_id != quick.attempt_id or main.route != quick.route:
                raise ValueError("model attempt lineage changed identity or route")
        identity = {
            "capsule_id": self.capsule_id,
            "proposal_hash": self.proposal.proposal_hash if self.proposal is not None else None,
            "attempt_audits": tuple(value.model_dump(mode="json") for value in self.attempt_audits),
        }
        if self.result_id != f"deliberation:{_digest(identity)}":
            raise ValueError("deliberation result identity is invalid")
        return self


class EpisodeTailResult(_FrozenModel):
    disposition: Literal[
        "complete_without_more",
        "append",
        "cancel_pending",
        "supersede_pending",
    ]
    deliberation: DeliberationResult | None = None
    failure_code: str | None = None


class Deliberation:
    """Orchestrate routing and model calls without granting write authority."""

    def __init__(
        self,
        *,
        router: ModelRouterAdapter,
        main_model: DeliberationModelAdapter,
        quick_recovery: QuickRecoveryAdapter,
        # Interactive expression is deliberately latency-bounded.  A slow
        # provider must not hold a human conversation open while the host
        # still retains the full audit/recovery path.  Six seconds leaves
        # room for normal JSON generation; recovery gets a separate compact
        # two-and-a-half-second budget.
        main_timeout_seconds: float = 6.0,
        quick_timeout_seconds: float = 2.5,
        proposal_grammar: ProposalGrammar | None = None,
        recovery_mode: Literal["minimal_only", "proposal_grammar"] = "minimal_only",
        expression_episode_mode: Literal["off", "shadow", "on", "stream"] = "off",
        expression_episode_diagnostics: ExpressionEpisodeDiagnostics | None = None,
        expression_episode_grammar: ProposalGrammar | None = None,
    ) -> None:
        if not 0 < main_timeout_seconds <= 120:
            raise ValueError("main model timeout is out of bounds")
        if not 0 < quick_timeout_seconds <= 30:
            raise ValueError("quick recovery timeout is out of bounds")
        self._router = router
        self._main = main_model
        self._quick = quick_recovery
        self._main_timeout = main_timeout_seconds
        self._quick_timeout = quick_timeout_seconds
        self._proposal_grammar = proposal_grammar
        self._recovery_mode = recovery_mode
        self._expression_episode_mode = expression_episode_mode
        self._episode_diagnostics = expression_episode_diagnostics or (
            ExpressionEpisodeDiagnostics(mode=expression_episode_mode)
            if expression_episode_mode != "off"
            else None
        )
        self._expression_episode_grammar = expression_episode_grammar
        self._provider_tasks: set[asyncio.Task[object]] = set()
        self._quick_provider_tasks: set[asyncio.Task[object]] = set()
        self._shadow_observer_tasks: set[asyncio.Task[object]] = set()
        self._shadow_observer_provider_tasks: set[asyncio.Task[object]] = set()
        self._episode_tail_tasks: dict[str, asyncio.Task[EpisodeTailResult | None]] = {}
        self._episode_tail_superseded: dict[str, asyncio.Event] = {}
        self._episode_tail_fallbacks: dict[
            str, Callable[[bool, str], EpisodeTailResult]
        ] = {}
        self._detached_episode_tail_tasks: set[asyncio.Task[object]] = set()
        # An inbound attention change can happen while an older author head is
        # still validating, before its continuation has entered the registry.
        # Every stream attempt captures this epoch before its first await; only
        # an attempt still bound to the current epoch may publish a tail.
        self._stream_attention_epoch = 0
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    def expression_episode_diagnostics(self) -> dict[str, object]:
        if self._episode_diagnostics is None:
            return ExpressionEpisodeDiagnostics(mode="off").snapshot()
        return self._episode_diagnostics.snapshot()

    async def await_expression_episode_tail(self, trigger_ref: str) -> EpisodeTailResult | None:
        task = self._episode_tail_tasks.get(trigger_ref)
        if task is None:
            return None
        superseded = self._episode_tail_superseded.setdefault(trigger_ref, asyncio.Event())
        superseded_waiter = asyncio.create_task(superseded.wait())
        try:
            try:
                shielded = asyncio.shield(task)
                done, _ = await asyncio.wait(
                    (shielded, superseded_waiter),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if superseded_waiter in done and superseded.is_set():
                    return EpisodeTailResult(
                        disposition="complete_without_more",
                        failure_code="stream_superseded_by_newer_input",
                    )
                return await shielded
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                return EpisodeTailResult(
                    disposition="complete_without_more",
                    failure_code="stream_superseded_by_newer_input",
                )
        finally:
            superseded_waiter.cancel()
            # Keep a completed result available until durable lifecycle
            # settlement succeeds. Proposal-audit CAS can lose after this
            # process-local task finishes; consuming it here would turn that
            # ordinary retry into a permanently missing tail.

    async def cancel_superseded_expression_streams(
        self, current_trigger_ref: str
    ) -> tuple[tuple[str, EpisodeTailResult], ...]:
        """Drop process-local continuation work made stale by newer input."""

        if self._expression_episode_mode != "stream":
            return ()
        advance_attention = getattr(self._main, "advance_expression_attention", None)
        if callable(advance_attention):
            advance_attention(current_trigger_ref)
        self._stream_attention_epoch += 1
        cancelled: list[asyncio.Task[EpisodeTailResult | None]] = []
        trigger_by_task: dict[asyncio.Task[object], str] = {}
        fallback_by_task: dict[
            asyncio.Task[object], Callable[[bool, str], EpisodeTailResult]
        ] = {}
        for trigger_ref, task in tuple(self._episode_tail_tasks.items()):
            if trigger_ref == current_trigger_ref:
                continue
            if self._episode_tail_tasks.get(trigger_ref) is task:
                self._episode_tail_tasks.pop(trigger_ref, None)
            fallback = self._episode_tail_fallbacks.pop(trigger_ref, None)
            if fallback is not None:
                fallback_by_task[task] = fallback
            superseded = self._episode_tail_superseded.pop(trigger_ref, None)
            if superseded is not None:
                superseded.set()
            if not task.done():
                cancelled.append(task)
                trigger_by_task[task] = trigger_ref
        terminal_results = await self._cancel_and_observe_expression_tail_tasks(
            cancelled,
            reason="newer inbound attention",
        )
        results = list(
            (trigger_by_task[task], result)
            for task, result in terminal_results
            if isinstance(result, EpisodeTailResult)
        )
        completed_tasks = {task for task, _ in terminal_results}
        for task in cancelled:
            fallback = fallback_by_task.get(task)
            if task not in completed_tasks and fallback is not None:
                results.append(
                    (
                        trigger_by_task[task],
                        fallback(
                            False,
                            "stream_tail_unresolved_after_bounded_cancellation",
                        ),
                    )
                )
        return tuple(results)

    async def _cancel_and_observe_expression_tail_tasks(
        self,
        tasks: Iterable[asyncio.Task[object]],
        *,
        reason: str,
    ) -> tuple[tuple[asyncio.Task[object], object], ...]:
        """Give cancelled tails a tiny drain, then observe them in background."""

        pending_candidates = tuple(dict.fromkeys(task for task in tasks if not task.done()))
        for task in pending_candidates:
            task.cancel()
        if not pending_candidates:
            return ()
        done, pending = await asyncio.wait(
            pending_candidates,
            timeout=_PROVIDER_CANCELLATION_AUDIT_GRACE_SECONDS,
        )
        results: list[tuple[asyncio.Task[object], object]] = []
        for task in done:
            if not task.cancelled():
                try:
                    results.append((task, task.result()))
                except Exception:
                    pass
            self._finish_owned_shutdown_task(task)
        for task in pending:
            self._detached_episode_tail_tasks.add(task)
            task.add_done_callback(self._finish_owned_shutdown_task)
        if pending:
            _LOG.warning(
                "expression stream cancellation detached %d task(s): %s",
                len(pending),
                reason,
            )
        return tuple(results)

    def has_expression_episode_tail(self, trigger_ref: str) -> bool:
        """Whether this process owns a tail for the exact ledger trigger."""

        return trigger_ref in self._episode_tail_tasks

    @property
    def expression_episode_mode(self) -> Literal["off", "shadow", "on", "stream"]:
        return self._expression_episode_mode

    async def deliberate(
        self,
        capsule_handle: TrustedContextCapsuleHandle,
        *,
        attempt_id: str,
        catalog_versions: tuple[str, ...] = (),
        recorded_draw_refs: tuple[str, ...] = (),
        recorded_cadence_draws: tuple[CadenceDraw, ...] = (),
        trigger_evidence: tuple[ProposalEvidenceRef, ...] = (),
        trigger_message: TriggerMessage | None = None,
        affect_target_bounds: AffectTargetLowerBounds | None = None,
        budget: InteractiveTurnBudget | None = None,
        first_role_provider_marker: Callable[[str], None] | None = None,
        first_role_provider_completion_marker: Callable[[str], None] | None = None,
        first_role_provider_token_marker: Callable[[str], None] | None = None,
    ) -> DeliberationResult:
        if self._closed:
            raise RuntimeError("Deliberation is closing")
        stream_attention_epoch = self._stream_attention_epoch
        if not isinstance(capsule_handle, TrustedContextCapsuleHandle):
            raise TypeError("Deliberation requires a compiler-issued Capsule handle")
        trusted = ContextCapsule.model_validate(
            capsule_handle.capsule.model_dump(mode="python", warnings="error")
        )
        if type(attempt_id) is not str or not attempt_id or len(attempt_id) > 256:
            raise ValueError("attempt_id is empty or oversized")
        for label, values in (
            ("catalog versions", catalog_versions),
            ("recorded draw refs", recorded_draw_refs),
        ):
            if not isinstance(values, tuple) or len(values) > 16:
                raise ValueError(f"{label} are oversized or not a tuple")
            if any(type(value) is not str or not 1 <= len(value) <= 256 for value in values):
                raise ValueError(f"{label} contain an invalid reference")
            if len(set(values)) != len(values):
                raise ValueError(f"{label} must be unique")
        cadence_refs = tuple(dict.fromkeys(item.draw_ref for item in recorded_cadence_draws))
        if cadence_refs != recorded_draw_refs:
            if recorded_cadence_draws:
                raise ValueError("recorded cadence draws must bind the exact draw refs")
        if (
            not isinstance(trigger_evidence, tuple)
            or len(trigger_evidence) > 8
            or any(type(item) is not ProposalEvidenceRef for item in trigger_evidence)
            or len(set(trigger_evidence)) != len(trigger_evidence)
        ):
            raise ValueError("trigger evidence must be a bounded unique tuple")
        if trigger_message is not None:
            if type(trigger_message) is not TriggerMessage:
                raise TypeError("trigger message must use the exact Deliberation contract")
            if trigger_message.event_ref != trusted.trigger_ref:
                raise ValueError("trigger message does not belong to the Capsule trigger")
            if not any(
                item.ref_id == trigger_message.observation_ref
                and item.evidence_kind == "observed_message"
                and item.immutable_hash == trigger_message.event_payload_hash
                for item in trigger_evidence
            ):
                raise ValueError("trigger message is not bound to observed-message evidence")
        if (
            affect_target_bounds is not None
            and type(affect_target_bounds) is not AffectTargetLowerBounds
        ):
            raise TypeError("affect target bounds must use the exact pinned contract")
        if first_role_provider_marker is not None and not callable(
            first_role_provider_marker
        ):
            raise TypeError("first role-provider marker must be callable")
        if first_role_provider_completion_marker is not None and not callable(
            first_role_provider_completion_marker
        ):
            raise TypeError("first role-provider completion marker must be callable")
        if first_role_provider_token_marker is not None and not callable(
            first_role_provider_token_marker
        ):
            raise TypeError("first role-provider token marker must be callable")
        content_hash = _digest(json.loads(trusted.model_content_json))
        route_hints = derive_route_hints(capsule_handle)
        route = await self._route(
            RouteRequest(
                capsule_id=trusted.capsule_id,
                trigger_ref=trusted.trigger_ref,
                model_content_hash=content_hash,
                route_hints=route_hints,
            )
        )
        call_identity = {
            "capsule_id": trusted.capsule_id,
            "attempt_id": attempt_id,
            "route": route.model_dump(mode="json"),
        }
        call_id = f"model-call:{_digest({**call_identity, 'lane': 'main'})}"
        model_input = ModelInput(
            call_id=call_id,
            attempt_id=attempt_id,
            route=route,
            capsule_id=trusted.capsule_id,
            trigger_ref=trusted.trigger_ref,
            evaluated_world_revision=trusted.world_revision,
            evaluated_deliberation_revision=trusted.deliberation_revision,
            evaluated_ledger_sequence=trusted.ledger_sequence,
            model_content_json=trusted.model_content_json,
            trigger_evidence=trigger_evidence,
            trigger_message=trigger_message,
            affect_target_bounds=affect_target_bounds,
            catalog_versions=catalog_versions,
            recorded_draw_refs=recorded_draw_refs,
            recorded_cadence_draws=recorded_cadence_draws,
        )
        request_hash = _digest(model_input.model_dump(mode="json"))
        if budget is not None:
            return await self._deliberate_first_valid(
                trusted=trusted,
                model_input=model_input,
                request_hash=request_hash,
                call_identity=call_identity,
                route=route,
                attempt_id=attempt_id,
                trigger_evidence=trigger_evidence,
                budget=budget,
                first_role_provider_marker=first_role_provider_marker,
                first_role_provider_completion_marker=(
                    first_role_provider_completion_marker
                ),
                first_role_provider_token_marker=first_role_provider_token_marker,
                stream_attention_epoch=stream_attention_epoch,
            )
        failure_code: str | None = None
        recovered_status: AuditStatus | None = None
        output: ModelOutput | None = None
        slot_coordinator = _ProviderSlotCoordinator()
        try:
            deadline_token = _ATTEMPT_DEADLINE.set(time.monotonic() + self._main_timeout)
            slot_token = _PROVIDER_SLOT_COORDINATOR.set(slot_coordinator)
            marker_token = _FIRST_ROLE_PROVIDER_MARKER.set(
                first_role_provider_marker
            )
            completion_marker_token = _FIRST_ROLE_PROVIDER_COMPLETION_MARKER.set(
                first_role_provider_completion_marker
            )
            try:
                with model_request_emission_scope(
                    provider_call_id=call_id,
                    entry_marker=first_role_provider_marker,
                    completion_marker=first_role_provider_completion_marker,
                ):
                    output = _checked_output(
                        await self._with_deadline(
                            self._main.propose(model_input),
                            timeout=self._main_timeout,
                            label=call_id,
                            lane="main",
                        )
                    )
            finally:
                _FIRST_ROLE_PROVIDER_COMPLETION_MARKER.reset(
                    completion_marker_token
                )
                _FIRST_ROLE_PROVIDER_MARKER.reset(marker_token)
                _PROVIDER_SLOT_COORDINATOR.reset(slot_token)
                _ATTEMPT_DEADLINE.reset(deadline_token)
            proposal = self._validated_proposal(output, trusted, trigger_evidence=trigger_evidence)
            proposal = self._bind_minimal_model_result(proposal, call_id, output)
            status: AuditStatus = "proposal_validated"
        except ValidationTechnicalFailure as exc:
            _LOG.warning(
                "deliberation validation lane terminated call=%s trigger=%s failure=%s",
                call_id,
                trusted.trigger_ref,
                exc.failure_code,
            )
            terminal = _map_terminal_validation_failure(
                exc.failure_code,
                corrective_claimed=slot_coordinator.used_corrective,
            )
            terminal_audit = self._audit(
                model_call_id=call_id,
                attempt_id=attempt_id,
                route=route,
                request_hash=request_hash,
                output=None,
                status=terminal.status,
                failure_code=terminal.failure_code,
                technical_failure=exc,
                slot=terminal.slot,
                outcome=terminal.outcome,
            )
            return self._result(
                trusted,
                proposal=None,
                audit=terminal_audit,
                attempt_audits=(terminal_audit,),
            )
        except TimeoutError:
            failure_code = "main_timeout"
            recovered_status = "main_timeout_recovered"
        except (ValueError, TypeError) as exc:
            failure_code = "main_invalid_output"
            recovered_status = "main_invalid_recovered"
            _LOG.warning(
                "deliberation main attempt invalid call=%s trigger=%s error=%s: %s",
                call_id,
                trusted.trigger_ref,
                type(exc).__name__,
                str(exc)[:500],
            )
        except Exception as exc:
            failure_code = "main_exception"
            recovered_status = "main_exception_recovered"
            _LOG.warning(
                "deliberation main attempt raised call=%s trigger=%s error=%s: %s",
                call_id,
                trusted.trigger_ref,
                type(exc).__name__,
                str(exc)[:500],
            )

        if recovered_status is not None:
            main_status: AuditStatus = {
                "main_timeout": "main_timeout",
                "main_invalid_output": "main_invalid",
                "main_exception": "main_exception",
            }[failure_code or "main_exception"]
            main_audit = self._audit(
                model_call_id=call_id,
                attempt_id=attempt_id,
                route=route,
                request_hash=request_hash,
                output=output,
                status=main_status,
                failure_code=failure_code,
            )
            quick_call_id = f"model-call:{_digest({**call_identity, 'lane': 'quick_recovery', 'main_failure': failure_code})}"
            quick_input = model_input.model_copy(update={"call_id": quick_call_id})
            quick_request_hash = _digest(quick_input.model_dump(mode="json"))
            quick_output: ModelOutput | None = None
            try:
                quick_deadline_token = _ATTEMPT_DEADLINE.set(time.monotonic() + self._quick_timeout)
                marker_token = _FIRST_ROLE_PROVIDER_MARKER.set(
                    first_role_provider_marker
                )
                completion_marker_token = _FIRST_ROLE_PROVIDER_COMPLETION_MARKER.set(
                    first_role_provider_completion_marker
                )
                try:
                    with model_request_emission_scope(
                        provider_call_id=quick_call_id,
                        entry_marker=first_role_provider_marker,
                        completion_marker=first_role_provider_completion_marker,
                    ):
                        quick_output = _checked_output(
                            await self._with_deadline(
                                self._quick.recover(
                                    quick_input,
                                    failure_code or "main_failure",
                                ),
                                timeout=self._quick_timeout,
                                label=quick_call_id,
                                lane="quick",
                            )
                        )
                finally:
                    _FIRST_ROLE_PROVIDER_COMPLETION_MARKER.reset(
                        completion_marker_token
                    )
                    _FIRST_ROLE_PROVIDER_MARKER.reset(marker_token)
                    _ATTEMPT_DEADLINE.reset(quick_deadline_token)
                proposal = self._validated_proposal(
                    quick_output,
                    trusted,
                    minimal_only=self._recovery_mode == "minimal_only",
                    trigger_evidence=trigger_evidence,
                )
                proposal = self._bind_minimal_model_result(proposal, quick_call_id, quick_output)
                status = recovered_status
            except TimeoutError:
                quick_failure = "quick_timeout"
            except (ValueError, TypeError) as exc:
                quick_failure = "quick_invalid_output"
                _LOG.warning(
                    "deliberation quick recovery invalid call=%s trigger=%s error=%s: %s",
                    quick_call_id,
                    trusted.trigger_ref,
                    type(exc).__name__,
                    str(exc)[:500],
                )
            except Exception as exc:
                quick_failure = "quick_exception"
                _LOG.warning(
                    "deliberation quick recovery raised call=%s trigger=%s error=%s: %s",
                    quick_call_id,
                    trusted.trigger_ref,
                    type(exc).__name__,
                    str(exc)[:500],
                )
            else:
                final_audit = self._audit(
                    model_call_id=quick_call_id,
                    attempt_id=attempt_id,
                    route=route,
                    request_hash=quick_request_hash,
                    output=quick_output,
                    status=status,
                    failure_code=failure_code,
                )
                return self._result(
                    trusted,
                    proposal=proposal,
                    audit=final_audit,
                    attempt_audits=(main_audit, final_audit),
                )
            final_audit = self._audit(
                model_call_id=quick_call_id,
                attempt_id=attempt_id,
                route=route,
                request_hash=quick_request_hash,
                output=quick_output,
                status="recovery_failed",
                failure_code=quick_failure,
            )
            return self._result(
                trusted,
                proposal=None,
                audit=final_audit,
                attempt_audits=(main_audit, final_audit),
            )

        final_audit = self._audit(
            model_call_id=call_id,
            attempt_id=attempt_id,
            route=route,
            request_hash=request_hash,
            output=output,
            status=status,
            failure_code=None,
        )
        return self._result(
            trusted,
            proposal=proposal,
            audit=final_audit,
            attempt_audits=(final_audit,),
        )

    async def _deliberate_first_valid(
        self,
        *,
        trusted: ContextCapsule,
        model_input: ModelInput,
        request_hash: str,
        call_identity: dict[str, object],
        route: ModelRoute,
        attempt_id: str,
        trigger_evidence: tuple[ProposalEvidenceRef, ...],
        budget: InteractiveTurnBudget,
        first_role_provider_marker: Callable[[str], None] | None,
        first_role_provider_completion_marker: Callable[[str], None] | None,
        first_role_provider_token_marker: Callable[[str], None] | None,
        stream_attention_epoch: int,
    ) -> DeliberationResult:
        """Race at most two fully validated candidates under one absolute deadline."""

        terminal_validation_failures: dict[str, ValidationTechnicalFailure] = {}

        async def candidate(
            operation: Callable[[], Awaitable[ModelOutput]],
            *,
            call_id: str,
            minimal_only: bool,
            lane: Literal["main", "quick", "observer"],
            candidate_deadline: float,
            validation_state: _ValidationAttemptState | None = None,
            proposal_grammar_override: ProposalGrammar | None = None,
        ) -> tuple[ProposalInput | None, ModelOutput | None, str | None]:
            operation_deadline = (
                validation_state.hard_deadline
                if validation_state is not None
                else candidate_deadline
            )
            remaining = max(0.0, operation_deadline - budget.clock())
            if remaining <= 0:
                return None, None, "timeout"
            output: ModelOutput | None = None
            token = _ATTEMPT_DEADLINE.set(candidate_deadline)
            turn_budget_token = _INTERACTIVE_TURN_BUDGET.set(budget)
            marker_token = _FIRST_ROLE_PROVIDER_MARKER.set(
                first_role_provider_marker
            )
            completion_marker_token = _FIRST_ROLE_PROVIDER_COMPLETION_MARKER.set(
                first_role_provider_completion_marker
            )
            first_token_marker_token = _FIRST_ROLE_PROVIDER_TOKEN_MARKER.set(
                first_role_provider_token_marker
            )
            validation_token = (
                _VALIDATION_ATTEMPT.set(validation_state) if validation_state is not None else None
            )
            try:
                request_scope = (
                    model_request_emission_scope(
                        provider_call_id=call_id,
                        entry_marker=first_role_provider_marker,
                        completion_marker=first_role_provider_completion_marker,
                        first_token_marker=first_role_provider_token_marker,
                    )
                    if lane != "observer"
                    else nullcontext()
                )
                with request_scope:
                    output = _checked_output(
                        await self._with_deadline(
                            operation(),
                            timeout=remaining,
                            label=call_id,
                            lane=lane,
                        )
                    )
                proposal = self._validated_proposal(
                    output,
                    trusted,
                    minimal_only=minimal_only,
                    trigger_evidence=trigger_evidence,
                    proposal_grammar_override=proposal_grammar_override,
                )
                proposal = self._bind_minimal_model_result(proposal, call_id, output)
                return proposal, output, None
            except RecoveryCandidateFailure as exc:
                return None, output, exc.failure_kind
            except ValidationTechnicalFailure as exc:
                terminal_validation_failures[call_id] = exc
                _LOG.warning(
                    "deliberation validation lane failed call=%s trigger=%s failure=%s",
                    call_id,
                    trusted.trigger_ref,
                    exc.failure_code,
                )
                return None, output, exc.failure_code
            except TimeoutError:
                return None, output, "timeout"
            except (TypeError, ValueError) as exc:
                _LOG.warning(
                    "deliberation candidate invalid call=%s trigger=%s error=%s: %s",
                    call_id,
                    trusted.trigger_ref,
                    type(exc).__name__,
                    str(exc)[:500],
                )
                return None, output, "invalid"
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    # The caller/deadline owns this cancellation and must be
                    # allowed to unwind the candidate task.
                    raise
                # A provider/session can invalidate one of its own awaited
                # Futures (for example when an expression stream generation
                # is superseded).  That is a technical candidate failure, not
                # cancellation of the inbound request itself.
                return None, output, "cancelled"
            except Exception as exc:
                _LOG.warning(
                    "deliberation candidate raised call=%s trigger=%s error=%s: %s",
                    call_id,
                    trusted.trigger_ref,
                    type(exc).__name__,
                    str(exc)[:500],
                )
                return None, output, "exception"
            finally:
                if validation_token is not None:
                    _VALIDATION_ATTEMPT.reset(validation_token)
                _FIRST_ROLE_PROVIDER_COMPLETION_MARKER.reset(
                    completion_marker_token
                )
                _FIRST_ROLE_PROVIDER_TOKEN_MARKER.reset(first_token_marker_token)
                _FIRST_ROLE_PROVIDER_MARKER.reset(marker_token)
                _INTERACTIVE_TURN_BUDGET.reset(turn_budget_token)
                _ATTEMPT_DEADLINE.reset(token)

        primary_call_id = model_input.call_id
        slot_coordinator = _ProviderSlotCoordinator()
        isolated_shadow_episode = self._expression_episode_mode == "shadow"
        unit_stream_episode = self._expression_episode_mode == "stream"
        provisional_operation = getattr(
            self._main,
            (
                "propose_shadow_observer"
                if isolated_shadow_episode
                else "propose_stream_tail"
                if unit_stream_episode
                else "propose_provisional"
            ),
            None,
        )
        already_evaluated = getattr(self._main, "episode_provisional_already_evaluated", None)
        source_closure_enabled = getattr(self._main, "source_closure_review_enabled", None)
        provisional_provider_available = getattr(
            self._main,
            (
                "shadow_observer_provider_available"
                if isolated_shadow_episode
                else "stream_provider_available"
                if unit_stream_episode
                else "provisional_provider_available"
            ),
            None,
        )
        source_closure_review_active = bool(
            callable(source_closure_enabled) and source_closure_enabled()
        )
        # A shadow candidate is observational in every composition, including
        # the paired appraisal/expression path where the full adapter does not
        # advertise source review yet may still need its one Recall or
        # structural-correction slot.  It must therefore never reserve or
        # suppress any provider capability of the authoritative candidate.
        primary_author_deadline = budget.begin_author_candidate()
        primary_validation = (
            _ValidationAttemptState(
                budget=budget,
                author_deadline=primary_author_deadline,
                candidate_key=primary_call_id,
            )
            if source_closure_review_active
            else None
        )
        episode_enabled = (
            self._expression_episode_mode != "off"
            and callable(provisional_operation)
            and (
                not callable(provisional_provider_available)
                or provisional_provider_available(model_input)
            )
            # An authoritative provisional cannot bypass source closure.  A
            # shadow candidate is observational only, however, so always run
            # it with an isolated slot coordinator that cannot consume or
            # suppress Recall, factual review, or correction on the full lane.
            and (
                not source_closure_review_active
                or isolated_shadow_episode
                or unit_stream_episode
            )
            and not (callable(already_evaluated) and already_evaluated(model_input))
        )
        episode_started_at = budget.clock()
        episode_recorded = False

        def record_episode(
            result: tuple[ProposalInput | None, ModelOutput | None, str | None],
            *,
            winner: Literal["full", "provisional"],
            candidate_started_at: float | None = None,
        ) -> None:
            nonlocal episode_recorded
            if episode_recorded or self._episode_diagnostics is None:
                return
            proposal, output, failure = result
            valid = proposal is not None and failure is None
            rejection_kind: Literal["grounding", "placeholder", "other"] | None = None
            if not valid and output is not None:
                try:
                    validate_provisional_proposal(output.raw_proposal)
                except (TypeError, ValueError) as exc:
                    rejection_kind = "placeholder" if "placeholder" in str(exc) else "other"
                else:
                    rejection_kind = "grounding"
            self._episode_diagnostics.record(
                candidate_ms=max(
                    0.0,
                    (
                        budget.clock()
                        - (
                            candidate_started_at
                            if candidate_started_at is not None
                            else episode_started_at
                        )
                    )
                    * 1_000,
                ),
                valid=valid,
                winner=winner,
                would_send=valid,
                would_append=bool(output is not None and output.episode_disposition == "append"),
                slot_calls=2,
                rejection_kind=rejection_kind,
            )
            episode_recorded = True

        budget.mark("primary")
        slot_token = _PROVIDER_SLOT_COORDINATOR.set(slot_coordinator)
        try:
            primary_task = asyncio.create_task(
                candidate(
                    lambda: (
                        self._main.propose_stream_head(model_input)
                        if unit_stream_episode and episode_enabled
                        else self._main.propose(model_input)
                    ),
                    call_id=primary_call_id,
                    minimal_only=False,
                    lane="main",
                    candidate_deadline=primary_author_deadline,
                    validation_state=primary_validation,
                )
            )
            hedge_timer = asyncio.create_task(budget.wait_for_hedge())
            deadline_timer = asyncio.create_task(
                budget.sleep(max(0.0, primary_author_deadline - budget.clock()))
            )
        finally:
            _PROVIDER_SLOT_COORDINATOR.reset(slot_token)
        backup_task: (
            asyncio.Task[tuple[ProposalInput | None, ModelOutput | None, str | None]] | None
        ) = None
        primary_result: tuple[ProposalInput | None, ModelOutput | None, str | None] | None = None
        primary_timing_recorded = False
        backup_result: tuple[ProposalInput | None, ModelOutput | None, str | None] | None = None
        backup_call_id: str | None = None
        backup_input: ModelInput | None = None
        backup_request_hash: str | None = None
        backup_validation: _ValidationAttemptState | None = None
        primary_failure_for_recovery = "main_timeout"
        corrective_claimed_before_backup = False
        isolated_shadow_task: (
            asyncio.Task[tuple[ProposalInput | None, ModelOutput | None, str | None]] | None
        ) = None

        def start_isolated_shadow() -> None:
            """Observe one candidate after the factual full path has settled.

            The observer is an explicit provider dependency with its own
            transport and circuit state. It still starts only after the
            authoritative result so diagnostic work cannot extend visible
            latency, and it never participates in recovery or Action selection.
            """

            nonlocal isolated_shadow_task
            if (
                isolated_shadow_task is not None
                or not isolated_shadow_episode
                or not episode_enabled
            ):
                return
            shadow_coordinator = _ProviderSlotCoordinator()
            assert shadow_coordinator.claim_second("backup")
            shadow_coordinator.episode_reserved = True
            budget.mark("provisional")
            shadow_started_at = budget.clock()
            shadow_call_id = (
                f"model-call:{_digest({**call_identity, 'lane': 'provisional-shadow'})}"
            )
            shadow_input = model_input.model_copy(update={"call_id": shadow_call_id})
            reserve_provisional = getattr(
                self._main,
                "reserve_episode_provisional",
                None,
            )
            if callable(reserve_provisional):
                reserve_provisional(model_input)
            shadow_token = _PROVIDER_SLOT_COORDINATOR.set(shadow_coordinator)
            try:
                isolated_shadow_task = asyncio.create_task(
                    candidate(
                        lambda: provisional_operation(shadow_input),
                        call_id=shadow_call_id,
                        minimal_only=False,
                        lane="observer",
                        # Diagnostic work is not allowed to extend visible
                        # latency, but still needs a real bounded window after
                        # the reviewed full result consumed the turn budget.
                        candidate_deadline=(budget.clock() + min(self._quick_timeout, 8.0)),
                        proposal_grammar_override=self._expression_episode_grammar,
                    )
                )
            finally:
                _PROVIDER_SLOT_COORDINATOR.reset(shadow_token)
            self._shadow_observer_tasks.add(isolated_shadow_task)

            def finish_isolated_shadow(
                task: asyncio.Task[tuple[ProposalInput | None, ModelOutput | None, str | None]],
            ) -> None:
                self._shadow_observer_tasks.discard(task)
                if task.cancelled():
                    return
                try:
                    value = task.result()
                except Exception:
                    return
                record_episode(
                    value,
                    winner="full",
                    candidate_started_at=shadow_started_at,
                )

            isolated_shadow_task.add_done_callback(finish_isolated_shadow)

        def start_backup(failure_code: str, *, after_actual_failure: bool) -> bool:
            nonlocal backup_task, backup_call_id, backup_input, backup_request_hash
            nonlocal backup_validation, corrective_claimed_before_backup, deadline_timer
            if backup_task is not None:
                return False
            hedge_available = getattr(self._quick, "has_hedge_provider", None)
            if (
                not after_actual_failure
                and callable(hedge_available)
                and not hedge_available(model_input)
            ):
                return False
            if after_actual_failure:
                if not slot_coordinator.claim_failure_recovery():
                    return False
                corrective_claimed_before_backup = slot_coordinator.used_corrective
                recovery_deadline = budget.begin_technical_recovery()
                if recovery_deadline is None:
                    # A single inbound turn can run a paired appraisal before
                    # its visible expression.  If that earlier phase already
                    # opened the one-shot technical window, reuse its still
                    # live deadline here; do not mistake "already open" for
                    # "no recovery capability", and do not extend the turn a
                    # second time.
                    recovery_deadline = budget.author_candidate_deadline
                    if recovery_deadline <= budget.clock():
                        return False
                if not deadline_timer.done():
                    deadline_timer.cancel()
                deadline_timer = asyncio.create_task(
                    budget.sleep(max(0.0, recovery_deadline - budget.clock()))
                )
            else:
                if primary_author_deadline <= budget.clock() or not slot_coordinator.claim_second(
                    "backup"
                ):
                    return False
                recovery_deadline = primary_author_deadline
            budget.mark("hedge_started")
            backup_call_id = f"model-call:{_digest({**call_identity, 'lane': 'technical_recovery' if after_actual_failure else 'hedge', 'main_failure': failure_code})}"
            backup_input = model_input.model_copy(update={"call_id": backup_call_id})
            backup_request_hash = _digest(backup_input.model_dump(mode="json"))
            quick_source_closure = getattr(
                self._quick,
                "source_closure_review_enabled",
                None,
            )
            backup_validation = (
                _ValidationAttemptState(
                    budget=budget,
                    author_deadline=recovery_deadline,
                    candidate_key=backup_call_id,
                )
                if callable(quick_source_closure) and quick_source_closure()
                else None
            )
            token = _PROVIDER_SLOT_COORDINATOR.set(slot_coordinator)
            try:
                backup_task = asyncio.create_task(
                    candidate(
                        lambda: self._quick.recover(backup_input, failure_code),
                        call_id=backup_call_id,
                        minimal_only=self._recovery_mode == "minimal_only",
                        lane="quick",
                        candidate_deadline=recovery_deadline,
                        validation_state=backup_validation,
                    )
                )
            finally:
                _PROVIDER_SLOT_COORDINATOR.reset(token)
            return True

        if episode_enabled and not isolated_shadow_episode and (
            unit_stream_episode or slot_coordinator.claim_second("backup")
        ):
            slot_coordinator.episode_reserved = not unit_stream_episode
            budget.mark("provisional")
            backup_call_id = f"model-call:{_digest({**call_identity, 'lane': 'provisional'})}"
            backup_input = model_input.model_copy(update={"call_id": backup_call_id})
            backup_request_hash = _digest(backup_input.model_dump(mode="json"))
            token = _PROVIDER_SLOT_COORDINATOR.set(slot_coordinator)
            try:
                backup_task = asyncio.create_task(
                    candidate(
                        lambda: provisional_operation(backup_input),
                        call_id=backup_call_id,
                        minimal_only=False,
                        lane="quick",
                        candidate_deadline=primary_author_deadline,
                        proposal_grammar_override=(
                            None
                            if unit_stream_episode
                            else self._expression_episode_grammar
                        ),
                    )
                )
            finally:
                _PROVIDER_SLOT_COORDINATOR.reset(token)

        try:
            while True:
                active: set[asyncio.Task[object]] = {
                    task
                    for task in (primary_task, hedge_timer, deadline_timer, backup_task)
                    if task is not None and not task.done()
                }
                if not active:
                    break
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                # A source-closure pass starts after the author returns and
                # needs the same remaining provider budget.  A time-based
                # speculative hedge cannot see that draft, so starting it
                # first only consumes the correction lane and turns a
                # repairable factual mismatch into silence.  Fast failure of
                # the author still starts normal recovery below.
                if (
                    hedge_timer in done
                    and primary_result is None
                    and not source_closure_review_active
                ):
                    start_backup("main_timeout", after_actual_failure=False)
                if primary_task in done and primary_result is None:
                    primary_result = primary_task.result()
                    if not primary_timing_recorded and self._episode_diagnostics is not None:
                        self._episode_diagnostics.record_full(
                            (budget.clock() - budget.started_at) * 1_000
                        )
                        primary_timing_recorded = True
                    proposal, output, failure = primary_result
                    if proposal is not None and failure is None:
                        if (
                            unit_stream_episode
                            and stream_attention_epoch != self._stream_attention_epoch
                        ):
                            # The provider may have exposed a complete first
                            # unit before source/shape validation finished.
                            # New physical ingress invalidates that old
                            # attention generation, not only its continuation;
                            # never promote the stale head into a Proposal.
                            if backup_task is not None:
                                await self._cancel_and_observe_expression_tail_tasks(
                                    (backup_task,),
                                    reason="attention changed before head acceptance",
                                )
                                backup_task = None
                            discard_candidate = getattr(
                                self._main, "discard_candidate", None
                            )
                            if callable(discard_candidate):
                                discard_candidate(model_input)
                            stale_audit = self._audit(
                                model_call_id=primary_call_id,
                                attempt_id=attempt_id,
                                route=route,
                                request_hash=request_hash,
                                output=None,
                                status="main_exception",
                                failure_code="stream_superseded_by_newer_input",
                                slot="primary",
                                outcome="exception",
                            )
                            return self._result(
                                trusted,
                                proposal=None,
                                audit=stale_audit,
                                attempt_audits=(stale_audit,),
                            )
                        budget.mark("candidate_validated")
                        accept_candidate = getattr(self._main, "accept_candidate", None)
                        if callable(accept_candidate):
                            accept_candidate(model_input)
                        start_isolated_shadow()
                        loser_audit: ModelResultAudit | None = None
                        if backup_task is not None and unit_stream_episode:
                            continuing_tail = backup_task
                            tail_call_id = backup_call_id
                            tail_request_hash = backup_request_hash
                            head_output = output
                            assert head_output is not None
                            parent_call_id = head_output.provider_parent_model_call_id
                            parent_request_hash = head_output.winning_request_hash
                            tail_semantic_call_id = (
                                "model-call:"
                                + _digest(
                                    {
                                        "provider_call_id": parent_call_id,
                                        "unit": "tail",
                                    }
                                )
                            )

                            def incomplete_stream_result(
                                cancelled: bool,
                                failure_code: str,
                            ) -> EpisodeTailResult:
                                assert parent_call_id is not None
                                assert parent_request_hash is not None
                                physical = PhysicalProviderInvocationAudit(
                                    model_call_id=parent_call_id,
                                    request_hash=parent_request_hash,
                                    model_id=head_output.model_id,
                                    model_version=head_output.model_version,
                                    outcome="cancelled" if cancelled else "unresolved",
                                    failure_code=failure_code,
                                    usage_status=(
                                        "cancelled" if cancelled else "unresolved"
                                    ),
                                    semantic_model_call_ids=(
                                        head_output.winning_model_call_id,
                                        tail_semantic_call_id,
                                    ),
                                )
                                terminal_audit = ModelResultAudit(
                                    model_call_id=tail_semantic_call_id,
                                    parent_model_call_id=parent_call_id,
                                    semantic_stream_part="tail",
                                    model_result_ref=_model_result_ref(
                                        tail_semantic_call_id, None
                                    ),
                                    attempt_id=attempt_id,
                                    route=route,
                                    attempted_model_id=head_output.model_id,
                                    attempted_model_version=head_output.model_version,
                                    request_hash=parent_request_hash,
                                    status="main_exception",
                                    failure_code=(
                                        "stream_tail_cancelled"
                                        if cancelled
                                        else "stream_tail_unresolved"
                                    ),
                                    slot="backup",
                                    outcome="exception",
                                    physical_provider_audits=(physical,),
                                )
                                return EpisodeTailResult(
                                    disposition="complete_without_more",
                                    deliberation=self._result(
                                        trusted,
                                        proposal=None,
                                        audit=terminal_audit,
                                        attempt_audits=(terminal_audit,),
                                    ),
                                    failure_code=failure_code,
                                )

                            def completed_invalid_stream_result(
                                tail_output: ModelOutput,
                                failure_code: str,
                            ) -> EpisodeTailResult:
                                """Preserve a completed physical call when its tail is invalid.

                                The provider can finish the one physical SSE successfully while
                                the semantic tail fails proposal validation.  That is an invalid
                                character result, not an unresolved network call.
                                """

                                normalized_failure = (
                                    "main_invalid_output"
                                    if failure_code == "invalid"
                                    else failure_code
                                )
                                invalid_status: AuditStatus = (
                                    "main_invalid"
                                    if normalized_failure
                                    in {
                                        "main_invalid_output",
                                        "primary_invalid",
                                        "corrective_invalid",
                                    }
                                    else "main_exception"
                                )
                                invalid_outcome: Literal["invalid", "exception"] = (
                                    "invalid"
                                    if invalid_status == "main_invalid"
                                    else "exception"
                                )
                                physical = tail_output.physical_provider_audits
                                if (
                                    tail_output.semantic_stream_part == "tail"
                                    and tail_output.provider_parent_model_call_id
                                    == parent_call_id
                                ):
                                    invalid_audit = self._audit(
                                        model_call_id=tail_call_id or tail_semantic_call_id,
                                        attempt_id=attempt_id,
                                        route=route,
                                        request_hash=tail_request_hash
                                        or parent_request_hash,
                                        output=tail_output,
                                        status=invalid_status,
                                        failure_code=normalized_failure,
                                        slot="backup",
                                        outcome=invalid_outcome,
                                    )
                                elif physical:
                                    terminal = physical[0]
                                    invalid_audit = ModelResultAudit(
                                        model_call_id=tail_semantic_call_id,
                                        parent_model_call_id=parent_call_id,
                                        semantic_stream_part="tail",
                                        model_result_ref=_model_result_ref(
                                            tail_semantic_call_id, None
                                        ),
                                        attempt_id=attempt_id,
                                        route=route,
                                        attempted_model_id=terminal.model_id,
                                        attempted_model_version=terminal.model_version,
                                        request_hash=parent_request_hash,
                                        status=invalid_status,
                                        failure_code=normalized_failure,
                                        slot="backup",
                                        outcome=invalid_outcome,
                                        physical_provider_audits=physical,
                                    )
                                else:
                                    return incomplete_stream_result(
                                        False,
                                        failure_code,
                                    )
                                return EpisodeTailResult(
                                    disposition="complete_without_more",
                                    deliberation=self._result(
                                        trusted,
                                        proposal=None,
                                        audit=invalid_audit,
                                        attempt_audits=(invalid_audit,),
                                    ),
                                    failure_code=failure_code,
                                )

                            async def finish_stream_tail() -> EpisodeTailResult | None:
                                if parent_call_id is None or parent_request_hash is None:
                                    return EpisodeTailResult(
                                        disposition="complete_without_more",
                                        failure_code="stream_parent_identity_missing",
                                    )

                                try:
                                    tail_proposal, tail_output, tail_failure = (
                                        await continuing_tail
                                    )
                                except asyncio.CancelledError:
                                    return incomplete_stream_result(
                                        True,
                                        "stream_superseded_by_newer_input",
                                    )
                                if tail_output is None:
                                    return incomplete_stream_result(
                                        False,
                                        tail_failure or "missing_output",
                                    )
                                if tail_failure is not None:
                                    return completed_invalid_stream_result(
                                        tail_output,
                                        tail_failure,
                                    )
                                if (
                                    head_output.provider_parent_model_call_id is None
                                    or tail_output.provider_parent_model_call_id
                                    != head_output.provider_parent_model_call_id
                                ):
                                    physical = tail_output.physical_provider_audits
                                    corrected_after_stream = bool(
                                        physical
                                        and tail_output.provider_parent_model_call_id is None
                                        and tail_output.semantic_stream_part is None
                                        and tail_output.winning_model_call_id
                                        not in physical[0].semantic_model_call_ids
                                    )
                                    if corrected_after_stream:
                                        original = completed_invalid_stream_result(
                                            tail_output,
                                            "main_invalid_output",
                                        )
                                        assert original.deliberation is not None
                                        original_audit = original.deliberation.audit
                                        disposition = (
                                            tail_output.episode_disposition
                                            or "complete_without_more"
                                        )
                                        corrected_output = tail_output.model_copy(
                                            update={"physical_provider_audits": ()}
                                        )
                                        corrected_audit = self._audit(
                                            model_call_id=tail_call_id
                                            or tail_semantic_call_id,
                                            attempt_id=attempt_id,
                                            route=route,
                                            request_hash=tail_request_hash
                                            or parent_request_hash,
                                            output=corrected_output,
                                            status="main_invalid_recovered",
                                            failure_code="main_invalid_output",
                                            slot="corrective",
                                            outcome=(
                                                "winner"
                                                if disposition == "append"
                                                and tail_proposal is not None
                                                else "returned"
                                            ),
                                        )
                                        return EpisodeTailResult(
                                            disposition=disposition,
                                            deliberation=self._result(
                                                trusted,
                                                proposal=(
                                                    tail_proposal
                                                    if disposition == "append"
                                                    else None
                                                ),
                                                audit=corrected_audit,
                                                attempt_audits=(
                                                    original_audit,
                                                    corrected_audit,
                                                ),
                                            ),
                                        )
                                    # A correction/reselection replaced either
                                    # semantic unit. Never splice continuation
                                    # from the rejected original stream into
                                    # that independently authored replacement.
                                    _LOG.warning(
                                        "expression stream tail discarded after author identity "
                                        "change trigger=%s head_parent=%s tail_parent=%s",
                                        trusted.trigger_ref,
                                        head_output.provider_parent_model_call_id,
                                        tail_output.provider_parent_model_call_id,
                                    )
                                    return incomplete_stream_result(
                                        False,
                                        "stream_author_identity_changed",
                                    )
                                disposition = (
                                    tail_output.episode_disposition
                                    or "complete_without_more"
                                )
                                assert tail_call_id is not None
                                assert tail_request_hash is not None
                                tail_audit = self._audit(
                                    model_call_id=tail_call_id,
                                    attempt_id=attempt_id,
                                    route=route,
                                    request_hash=tail_request_hash,
                                    output=tail_output,
                                    status=(
                                        "proposal_validated"
                                        if disposition == "append"
                                        and tail_proposal is not None
                                        else "candidate_returned"
                                    ),
                                    failure_code=None,
                                    slot="backup",
                                    outcome=(
                                        "winner"
                                        if disposition == "append"
                                        and tail_proposal is not None
                                        else "returned"
                                    ),
                                )
                                return EpisodeTailResult(
                                    disposition=disposition,
                                    deliberation=self._result(
                                        trusted,
                                        proposal=(
                                            tail_proposal
                                            if disposition == "append"
                                            else None
                                        ),
                                        audit=tail_audit,
                                        attempt_audits=(tail_audit,),
                                    ),
                                )

                            if stream_attention_epoch != self._stream_attention_epoch:
                                await self._cancel_and_observe_expression_tail_tasks(
                                    (continuing_tail,),
                                    reason="attention changed before tail registration",
                                )
                            else:
                                self._episode_tail_superseded[trusted.trigger_ref] = asyncio.Event()
                                if parent_call_id is not None and parent_request_hash is not None:
                                    self._episode_tail_fallbacks[trusted.trigger_ref] = (
                                        incomplete_stream_result
                                    )
                                self._episode_tail_tasks[trusted.trigger_ref] = (
                                    asyncio.create_task(
                                        finish_stream_tail(),
                                        name=f"expression-stream-tail:{trusted.trigger_ref}",
                                    )
                                )
                            backup_task = None
                        elif backup_task is not None and self._expression_episode_mode == "shadow":
                            if backup_task.done():
                                record_episode(backup_task.result(), winner="provisional")
                            else:
                                self._quick_provider_tasks.add(backup_task)

                                def finish_shadow(
                                    task: asyncio.Task[
                                        tuple[
                                            ProposalInput | None,
                                            ModelOutput | None,
                                            str | None,
                                        ]
                                    ],
                                ) -> None:
                                    self._quick_provider_tasks.discard(task)
                                    if task.cancelled():
                                        return
                                    try:
                                        value = task.result()
                                    except Exception:
                                        return
                                    record_episode(value, winner="full")

                                backup_task.add_done_callback(finish_shadow)
                            backup_task = None
                        elif backup_task is not None:
                            if not backup_task.done():
                                backup_task.cancel()
                                await asyncio.gather(backup_task, return_exceptions=True)
                            discard_candidate = getattr(self._quick, "discard_candidate", None)
                            if callable(discard_candidate) and backup_input is not None:
                                discard_candidate(backup_input)
                            assert backup_call_id is not None and backup_request_hash is not None
                            loser_audit = self._audit(
                                model_call_id=backup_call_id,
                                attempt_id=attempt_id,
                                route=route,
                                request_hash=backup_request_hash,
                                output=None,
                                status="recovery_failed",
                                failure_code="backup_cancelled",
                                slot="backup",
                                outcome="hedge_cancelled",
                            )
                            budget.mark("hedge_cancelled")
                        winner_slot = (
                            "corrective" if slot_coordinator.used_corrective else "primary"
                        )
                        final = self._audit(
                            model_call_id=primary_call_id,
                            attempt_id=attempt_id,
                            route=route,
                            request_hash=request_hash,
                            output=output,
                            status="proposal_validated",
                            failure_code=None,
                            slot=winner_slot,
                            outcome="winner",
                        )
                        budget.mark("winner")
                        return self._result(
                            trusted,
                            proposal=proposal,
                            audit=final,
                            attempt_audits=(
                                (loser_audit, final) if loser_audit is not None else (final,)
                            ),
                        )
                    if failure in _NON_RECOVERABLE_VALIDATION_FAILURE_CODES:
                        # The candidate has already consumed its bounded
                        # validation/reselection lane.  A new role author would
                        # be a forbidden third semantic choice, not technical
                        # recovery for an initial author failure.
                        primary_failure_for_recovery = failure
                        break
                    primary_failure_for_recovery = (
                        failure
                        if failure
                        in {
                            "authored_subcall_timeout",
                            "authored_subcall_exception",
                        }
                        else {
                            "invalid": (
                                "corrective_invalid"
                                if slot_coordinator.used_corrective
                                else "main_invalid_output"
                            ),
                            "exception": "main_exception",
                            "timeout": (
                                "corrective_timeout"
                                if slot_coordinator.used_corrective
                                else "main_timeout"
                            ),
                        }.get(failure or "", "main_exception")
                    )
                    discard_candidate = getattr(self._main, "discard_candidate", None)
                    if callable(discard_candidate):
                        discard_candidate(model_input)
                    start_backup(
                        primary_failure_for_recovery,
                        after_actual_failure=True,
                    )
                    if (
                        self._expression_episode_mode == "shadow"
                        and backup_result is not None
                        and backup_result[0] is not None
                        and backup_result[2] is None
                    ):
                        # In shadow the provisional slot replaces the old
                        # hedge. If full fails, it may serve as that one normal
                        # recovery response; it never creates an extra Action.
                        backup_result = None
                if backup_task is not None and backup_task in done and backup_result is None:
                    backup_result = backup_task.result()
                    proposal, output, failure = backup_result
                    if unit_stream_episode:
                        # Tail units are never an independent winner.  They can
                        # only append after the validated head from the same
                        # provider stream has become authoritative.
                        if primary_result is None:
                            continue
                        break
                    if self._expression_episode_mode == "shadow":
                        record_episode(backup_result, winner="provisional")
                        if primary_result is None:
                            continue
                    if proposal is not None and failure is None:
                        budget.mark("candidate_validated")
                        accept_candidate = getattr(self._quick, "accept_candidate", None)
                        if callable(accept_candidate) and backup_input is not None:
                            accept_candidate(backup_input)
                        if (
                            self._expression_episode_mode == "on"
                            and primary_result is None
                            and not primary_task.done()
                        ):
                            continuing_primary = primary_task

                            async def finish_full_tail() -> EpisodeTailResult | None:
                                full_proposal, full_output, full_failure = await continuing_primary
                                if self._episode_diagnostics is not None:
                                    self._episode_diagnostics.record_full(
                                        (budget.clock() - budget.started_at) * 1_000
                                    )
                                if full_failure is not None or full_output is None:
                                    return EpisodeTailResult(
                                        disposition="complete_without_more",
                                        failure_code=full_failure or "missing_output",
                                    )
                                disposition = (
                                    full_output.episode_disposition or "complete_without_more"
                                )
                                if disposition != "append" or full_proposal is None:
                                    return EpisodeTailResult(disposition=disposition)
                                full_audit = self._audit(
                                    model_call_id=primary_call_id,
                                    attempt_id=attempt_id,
                                    route=route,
                                    request_hash=request_hash,
                                    output=full_output,
                                    status="proposal_validated",
                                    failure_code=None,
                                    slot="primary",
                                    outcome="winner",
                                )
                                return EpisodeTailResult(
                                    disposition="append",
                                    deliberation=self._result(
                                        trusted,
                                        proposal=full_proposal,
                                        audit=full_audit,
                                        attempt_audits=(full_audit,),
                                    ),
                                )
                            self._episode_tail_superseded[trusted.trigger_ref] = asyncio.Event()
                            self._episode_tail_tasks[trusted.trigger_ref] = asyncio.create_task(
                                finish_full_tail(),
                                name=f"expression-tail:{trusted.trigger_ref}",
                            )
                            primary_task = None
                            assert backup_call_id is not None and backup_request_hash is not None
                            provisional_audit = self._audit(
                                model_call_id=backup_call_id,
                                attempt_id=attempt_id,
                                route=route,
                                request_hash=backup_request_hash,
                                output=output,
                                status="proposal_validated",
                                failure_code=None,
                                slot="backup",
                                outcome="winner",
                            )
                            budget.mark("winner")
                            return self._result(
                                trusted,
                                proposal=proposal,
                                audit=provisional_audit,
                                attempt_audits=(provisional_audit,),
                            )
                        discard_candidate = getattr(self._main, "discard_candidate", None)
                        if callable(discard_candidate):
                            discard_candidate(model_input)
                        if not primary_task.done():
                            primary_task.cancel()
                            await asyncio.gather(primary_task, return_exceptions=True)
                            await asyncio.sleep(0)
                            budget.mark("hedge_lost")
                        if primary_result is None:
                            primary_result = (None, None, "timeout")
                        main_output = primary_result[1]
                        main_failure = primary_result[2]
                        technical_failure = terminal_validation_failures.get(
                            primary_call_id
                        )
                        if technical_failure is None:
                            main_status: AuditStatus = {
                                "invalid": "main_invalid",
                                "exception": "main_exception",
                                "timeout": "main_timeout",
                            }.get(main_failure or "", "main_timeout")
                            main_failure_code = {
                                "invalid": (
                                    "corrective_invalid"
                                    if corrective_claimed_before_backup
                                    else "primary_invalid"
                                ),
                                "exception": "primary_exception",
                                "timeout": (
                                    "corrective_timeout"
                                    if corrective_claimed_before_backup
                                    else "primary_timeout"
                                ),
                            }.get(main_failure or "", "primary_timeout")
                            main_slot: Literal["primary", "corrective"] = (
                                "corrective"
                                if corrective_claimed_before_backup
                                else "primary"
                            )
                            main_outcome = (
                                "hedge_cancelled"
                                if main_failure == "timeout"
                                else (main_failure or "exception")
                            )
                        else:
                            terminal = _map_terminal_validation_failure(
                                technical_failure.failure_code,
                                corrective_claimed=slot_coordinator.used_corrective,
                            )
                            main_status = terminal.status
                            main_failure_code = terminal.failure_code
                            main_slot = terminal.slot
                            main_outcome = terminal.outcome
                        main_audit = self._audit(
                            model_call_id=primary_call_id,
                            attempt_id=attempt_id,
                            route=route,
                            request_hash=request_hash,
                            output=main_output,
                            status=main_status,
                            failure_code=main_failure_code,
                            technical_failure=technical_failure,
                            slot=main_slot,
                            outcome=main_outcome,
                        )
                        recovered_status: AuditStatus = {
                            "primary_invalid": "main_invalid_recovered",
                            "corrective_invalid": "main_invalid_recovered",
                            "primary_exception": "main_exception_recovered",
                            "primary_timeout": "main_timeout_recovered",
                            "corrective_timeout": "main_timeout_recovered",
                            "authored_subcall_timeout": "main_timeout_recovered",
                            "authored_subcall_exception": (
                                "main_exception_recovered"
                            ),
                        }[main_failure_code]
                        assert backup_call_id is not None and backup_request_hash is not None
                        winner_slot = (
                            "corrective"
                            if slot_coordinator.used_corrective
                            and not corrective_claimed_before_backup
                            else "backup"
                        )
                        final = self._audit(
                            model_call_id=backup_call_id,
                            attempt_id=attempt_id,
                            route=route,
                            request_hash=backup_request_hash,
                            output=output,
                            status=recovered_status,
                            failure_code=main_failure_code,
                            slot=winner_slot,
                            outcome="winner",
                        )
                        budget.mark("winner")
                        return self._result(
                            trusted,
                            proposal=proposal,
                            audit=final,
                            attempt_audits=(main_audit, final),
                        )
                    if primary_result is not None:
                        discard_candidate = getattr(self._quick, "discard_candidate", None)
                        if callable(discard_candidate) and backup_input is not None:
                            discard_candidate(backup_input)
                        break
                # A candidate and the deadline can become ready in the same
                # scheduler turn.  Validation wins that tie: discarding an
                # already-validated corrected draft is the production race
                # that previously produced a canned failsafe after provider
                # success.
                if deadline_timer in done:
                    active_validation = (
                        primary_validation
                        if primary_result is None
                        else backup_validation
                        if backup_task is not None and backup_result is None
                        else None
                    )
                    if (
                        active_validation is not None
                        and (
                            active_validation.review_inflight
                            or active_validation.truth_boundary_active
                        )
                        and active_validation.hard_deadline > budget.clock()
                    ):
                        # The role-author deadline expired while its already
                        # authored draft was inside the separately bounded
                        # truth review. Follow the candidate-local hard
                        # reviewer deadline even when its recovery window is
                        # opened on this same scheduler turn; never reinterpret
                        # that race as an author timeout.
                        deadline_timer = asyncio.create_task(
                            budget.sleep(
                                max(
                                    0.0,
                                    active_validation.hard_deadline - budget.clock(),
                                )
                            )
                        )
                        continue
                    if primary_result is None:
                        if not primary_task.done():
                            primary_task.cancel()
                            await asyncio.gather(primary_task, return_exceptions=True)
                            await asyncio.sleep(0)
                        primary_result = (None, None, "timeout")
                        primary_failure_for_recovery = "main_timeout"
                        discard_candidate = getattr(self._main, "discard_candidate", None)
                        if callable(discard_candidate):
                            discard_candidate(model_input)
                        if start_backup(
                            primary_failure_for_recovery,
                            after_actual_failure=True,
                        ):
                            continue
                    break

            if primary_result is None:
                primary_result = (None, None, "timeout")
            main_failure = primary_result[2]
            main_used_corrective = (
                corrective_claimed_before_backup
                if backup_call_id is not None
                else slot_coordinator.used_corrective
            )
            terminal_failure = terminal_validation_failures.get(primary_call_id)
            if terminal_failure is not None:
                terminal = _map_terminal_validation_failure(
                    terminal_failure.failure_code,
                    corrective_claimed=main_used_corrective,
                )
                main_status = terminal.status
                main_failure_code = terminal.failure_code
                main_slot = terminal.slot
                main_outcome = terminal.outcome
            else:
                main_status = {
                    "invalid": "main_invalid",
                    "exception": "main_exception",
                    "timeout": "main_timeout",
                }.get(main_failure or "", "main_timeout")
                main_failure_code = {
                    "invalid": (
                        "corrective_invalid" if main_used_corrective else "primary_invalid"
                    ),
                    "exception": "primary_exception",
                    "timeout": (
                        "corrective_timeout" if main_used_corrective else "primary_timeout"
                    ),
                }.get(main_failure or "", "primary_timeout")
                main_slot = "corrective" if main_used_corrective else "primary"
                main_outcome = _terminal_failure_audit_outcome(main_failure)
            for task in (primary_task, hedge_timer, deadline_timer, backup_task):
                if task is not None and not task.done():
                    task.cancel()
            main_audit = self._audit(
                model_call_id=primary_call_id,
                attempt_id=attempt_id,
                route=route,
                request_hash=request_hash,
                output=primary_result[1],
                status=main_status,
                failure_code=main_failure_code,
                technical_failure=terminal_failure,
                slot=main_slot,
                outcome=("budget_exhausted" if budget.remaining() <= 0 else main_outcome),
            )
            if budget.remaining() <= 0:
                budget.mark("budget_exhausted")
            if backup_call_id is None or backup_request_hash is None:
                return self._result(
                    trusted,
                    proposal=None,
                    audit=main_audit,
                    attempt_audits=(main_audit,),
                )
            backup_failure = backup_result[2] if backup_result is not None else "timeout"
            backup_kind: Literal["backup", "corrective"] = (
                "corrective"
                if slot_coordinator.used_corrective and not corrective_claimed_before_backup
                else "backup"
            )
            final = self._audit(
                model_call_id=backup_call_id,
                attempt_id=attempt_id,
                route=route,
                request_hash=backup_request_hash,
                output=backup_result[1] if backup_result is not None else None,
                status="recovery_failed",
                failure_code=f"{backup_kind}_{backup_failure or 'exception'}",
                technical_failure=(
                    terminal_validation_failures.get(backup_call_id)
                    if backup_call_id is not None
                    else None
                ),
                slot=backup_kind,
                outcome=(
                    "budget_exhausted"
                    if budget.remaining() <= 0
                    else _terminal_failure_audit_outcome(backup_failure)
                ),
            )
            return self._result(
                trusted,
                proposal=None,
                audit=final,
                attempt_audits=(main_audit, final),
            )
        finally:
            for task in (primary_task, hedge_timer, deadline_timer, backup_task):
                if task is not None and not task.done():
                    task.cancel()

    def main_has_precomputed_advisory(
        self,
        *,
        trigger_ref: str,
        observation_ref: str,
        event_payload_hash: str,
    ) -> bool:
        """Report whether main already incorporated this trigger's advice.

        This is a read-only performance hint, never proposal or acceptance
        authority.  Adapters without a paired prepass simply return False.
        """

        checker = getattr(self._main, "has_precomputed_semantic_advisory", None)
        if not callable(checker):
            checker = getattr(self._main, "has_precomputed_advisory", None)
        return bool(
            callable(checker)
            and checker(
                trigger_ref=trigger_ref,
                observation_ref=observation_ref,
                event_payload_hash=event_payload_hash,
            )
        )

    async def _route(self, request: RouteRequest) -> ModelRoute:
        try:
            route = await self._with_deadline(
                self._router.route(request),
                timeout=0.5,
                label="model-router",
                lane="main",
            )
            return _checked_route(route)
        except TimeoutError:
            reason = "router_timeout_default"
        except (ValueError, TypeError):
            reason = "router_invalid_default"
        except Exception:
            reason = "router_exception_default"
        return ModelRoute(tier="flash", reason_code=reason, router_version="fallback.1")

    @property
    def provider_health(self) -> ProviderHealth:
        """Expose lane-specific saturation so the composition root can replace the instance."""

        main = len(self._provider_tasks)
        quick = len(self._quick_provider_tasks)
        return ProviderHealth(
            main_inflight=main,
            main_ceiling=MAX_INFLIGHT_PROVIDER_TASKS,
            quick_inflight=quick,
            quick_ceiling=MAX_INFLIGHT_QUICK_TASKS,
            main_circuit_open=main >= MAX_INFLIGHT_PROVIDER_TASKS,
            quick_circuit_open=quick >= MAX_INFLIGHT_QUICK_TASKS,
        )

    async def aclose(self) -> None:
        """Cancel and join every process-owned provider or episode tail.

        Shadow observers intentionally outlive the authoritative visible
        result.  Their lifetime must not, however, outlive the provider
        clients and cursor-pinned stores owned by the host composition.
        """

        close_task = self._close_task
        if close_task is None:
            self._closed = True
            close_task = asyncio.create_task(
                self._aclose_owned(),
                name="world-v2-deliberation-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _aclose_owned(self) -> None:
        tasks = tuple(
            dict.fromkeys(
                (
                    *self._shadow_observer_tasks,
                    *self._shadow_observer_provider_tasks,
                    *self._episode_tail_tasks.values(),
                    *self._detached_episode_tail_tasks,
                    *self._provider_tasks,
                    *self._quick_provider_tasks,
                )
            )
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                timeout=_PROVIDER_CLOSE_GRACE_SECONDS,
            )
            for task in done:
                self._finish_owned_shutdown_task(task)
            for task in pending:
                task.add_done_callback(self._finish_owned_shutdown_task)
            if pending:
                _LOG.warning(
                    "deliberation close detached %d cancellation-suppressing task(s)",
                    len(pending),
                )

    def _finish_owned_shutdown_task(self, task: asyncio.Task[object]) -> None:
        """Observe and retire one task that crossed the bounded close grace."""

        self._shadow_observer_tasks.discard(task)
        self._shadow_observer_provider_tasks.discard(task)
        self._detached_episode_tail_tasks.discard(task)
        self._provider_tasks.discard(task)
        self._quick_provider_tasks.discard(task)
        for trigger_ref, tail in tuple(self._episode_tail_tasks.items()):
            if tail is task:
                self._episode_tail_tasks.pop(trigger_ref, None)
                self._episode_tail_superseded.pop(trigger_ref, None)
                self._episode_tail_fallbacks.pop(trigger_ref, None)
        if not task.cancelled():
            # Retrieve a late exception even when no lifecycle caller remains
            # to await the cancellation-suppressing provider or episode tail.
            task.exception()

    def _pending_shutdown_tasks(self) -> tuple[asyncio.Task[object], ...]:
        return tuple(
            task
            for task in dict.fromkeys(
                (
                    *self._shadow_observer_tasks,
                    *self._shadow_observer_provider_tasks,
                    *self._episode_tail_tasks.values(),
                    *self._detached_episode_tail_tasks,
                    *self._provider_tasks,
                    *self._quick_provider_tasks,
                )
            )
            if not task.done()
        )

    @property
    def shutdown_pending_task_count(self) -> int:
        """Number of cancelled tasks still retaining their provider dependencies."""

        return len(self._pending_shutdown_tasks())

    async def wait_for_shutdown_quiescence(self) -> None:
        """Wait for detached shutdown work without propagating caller cancellation."""

        while tasks := self._pending_shutdown_tasks():
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )
            await asyncio.sleep(0)

    async def _with_deadline(
        self,
        awaitable: Awaitable[_T],
        *,
        timeout: float,
        label: str,
        lane: Literal["main", "quick", "observer"],
    ) -> _T:
        """Enforce a caller deadline even if a provider suppresses cancellation.

        A provider task that ignores cancellation is detached, observed, and
        counted against a small in-flight ceiling.  Production adapters still
        must terminate their own transport work on cancellation.
        """

        if lane == "observer":
            tasks = self._shadow_observer_provider_tasks
            ceiling = MAX_INFLIGHT_SHADOW_OBSERVER_TASKS
        elif lane == "quick":
            tasks = self._quick_provider_tasks
            ceiling = MAX_INFLIGHT_QUICK_TASKS
        else:
            tasks = self._provider_tasks
            ceiling = MAX_INFLIGHT_PROVIDER_TASKS
        if len(tasks) >= ceiling:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise RuntimeError("provider task ceiling reached")
        task: asyncio.Task[_T] = asyncio.create_task(awaitable)
        tasks.add(task)  # type: ignore[arg-type]
        detached = False

        def observe(completed: asyncio.Task[object]) -> None:
            tasks.discard(completed)
            if not completed.cancelled():
                exception = completed.exception()
                if detached and exception is not None:
                    _LOG.warning(
                        "detached provider task failed",
                        extra={"provider_call_ref": label, "error_type": type(exception).__name__},
                    )

        task.add_done_callback(observe)  # type: ignore[arg-type]
        try:
            done, _ = await asyncio.wait((task,), timeout=timeout)
        except BaseException:
            task.cancel()
            # Deliver cancellation to the provider before reporting the slot
            # as lost.  This is one scheduler turn, not a grace-period wait;
            # cancellation-suppressing transports remain detached below.
            await asyncio.sleep(0)
            raise
        if task in done:
            return task.result()
        detached = True
        task.cancel()
        # A nested reviewer can attach immutable invocation evidence while
        # unwinding cancellation. Nested ``wait_for``/transport layers may
        # need more than one scheduler turn, so give them a tiny bounded
        # drain. Cancellation-suppressing providers remain detached after the
        # grace and still count against the in-flight ceiling.
        await asyncio.wait(
            (task,),
            timeout=_PROVIDER_CANCELLATION_AUDIT_GRACE_SECONDS,
        )
        if task.done() and task.cancelled():
            try:
                task.result()
            except asyncio.CancelledError as exc:
                technical_failure = getattr(
                    exc,
                    "world_v2_validation_technical_failure",
                    None,
                )
                if isinstance(technical_failure, ValidationTechnicalFailure):
                    raise technical_failure from exc
        raise TimeoutError

    @staticmethod
    def _bind_minimal_model_result(
        proposal: ProposalInput, model_call_id: str, output: ModelOutput
    ) -> ProposalInput:
        if not isinstance(proposal, MinimalProposal):
            return proposal
        if output.winning_model_call_id is not None:
            model_call_id = output.winning_model_call_id
        response_hash = _output_response_hash(output)
        return validate_proposal_envelope(
            proposal.model_copy(
                update={"source_model_result": _model_result_ref(model_call_id, response_hash)}
            ).model_dump(mode="python")
        )

    def _validated_proposal(
        self,
        output: ModelOutput,
        capsule: ContextCapsule,
        *,
        minimal_only: bool = False,
        trigger_evidence: tuple[ProposalEvidenceRef, ...] = (),
        proposal_grammar_override: ProposalGrammar | None = None,
    ) -> ProposalInput:
        checked = _checked_output(output)
        proposal = validate_proposal_envelope(checked.raw_proposal)
        if proposal.trigger_ref != capsule.trigger_ref:
            raise ValueError("proposal trigger does not match Capsule")
        if proposal.evaluated_world_revision != capsule.world_revision:
            raise ValueError("proposal revision does not match Capsule")
        if minimal_only and not isinstance(proposal, MinimalProposal):
            raise ValueError("quick recovery may only return MinimalProposal")
        bindings_by_ref: dict[str, set[tuple[str, str, int, str]]] = {}
        for binding in (
            binding
            for name in (
                "character_core",
                "current_situation",
                "relationship_slice",
                "affect_episodes",
                "open_threads",
                "relevant_facts",
                "recent_experiences",
                "active_memory_candidates",
                "available_capabilities",
                "action_budget",
                "private_impressions",
                "advisories",
            )
            for item in getattr(capsule, name).items
            for binding in item.source_bindings
        ):
            bindings_by_ref.setdefault(binding.ref, set()).add(
                (
                    binding.source_kind,
                    binding.authority_type,
                    binding.source_world_revision,
                    binding.immutable_hash,
                )
            )
        presented_prefetches = tuple(item.trace for item in checked.presented_prefetch_traces)
        if not presented_prefetches and checked.prefetch_trace is not None:
            # Legacy adapter outputs and historical model-result audits carry
            # one unphased trace. Keep that source authority readable without
            # pretending it had a phase or provider identity that was never
            # recorded.
            presented_prefetches = (checked.prefetch_trace,)
        for presented_prefetch in presented_prefetches:
            prefetch = verify_trusted_recall_trace(presented_prefetch)
            current_cursor = RecallCursor(
                world_revision=capsule.world_revision,
                deliberation_revision=capsule.deliberation_revision,
                ledger_sequence=capsule.ledger_sequence,
            )
            if (
                prefetch.mode != "prefetch"
                or prefetch.trigger_ref != capsule.trigger_ref
                or (prefetch.evaluated_cursor or prefetch.index_cursor) != current_cursor
            ):
                raise ValueError("prefetch trace does not match the exact Capsule")
            if (
                prefetch.reuse_contract == "same_context"
                and prefetch.index_cursor != current_cursor
            ):
                raise ValueError("prefetch trace does not match the exact Capsule")
            if prefetch.reuse_contract == "paired_cognition_carry" and any(
                recalled > current
                for recalled, current in zip(
                    (
                        prefetch.index_cursor.world_revision,
                        prefetch.index_cursor.deliberation_revision,
                        prefetch.index_cursor.ledger_sequence,
                    ),
                    (
                        current_cursor.world_revision,
                        current_cursor.deliberation_revision,
                        current_cursor.ledger_sequence,
                    ),
                    strict=True,
                )
            ):
                raise ValueError("paired prefetch carry contains future search material")
            for hit in prefetch.hits:
                if hit.document.authority != "world_fact":
                    continue
                for binding in hit.document.source_bindings:
                    bindings_by_ref.setdefault(binding.ref, set()).add(
                        (
                            binding.source_kind,
                            binding.authority_type,
                            binding.source_world_revision,
                            binding.immutable_hash,
                        )
                    )
        if checked.recall_trace is not None:
            recall = verify_trusted_recall_trace(checked.recall_trace)
            if recall.mode != "character_pull":
                raise ValueError("character recall trace has the wrong mode")
            current_cursor = (
                capsule.world_revision,
                capsule.deliberation_revision,
                capsule.ledger_sequence,
            )
            recall_cursor = (
                recall.index_cursor.world_revision,
                recall.index_cursor.deliberation_revision,
                recall.index_cursor.ledger_sequence,
            )
            evaluated = recall.evaluated_cursor or recall.index_cursor
            evaluated_cursor = (
                evaluated.world_revision,
                evaluated.deliberation_revision,
                evaluated.ledger_sequence,
            )
            if recall.trigger_ref != capsule.trigger_ref:
                raise ValueError("recall trace belongs to another trigger")
            if evaluated_cursor != current_cursor:
                raise ValueError("recall trace was not evaluated at the Capsule cursor")
            if recall.reuse_contract == "paired_cognition_carry" and any(
                recalled > current
                for recalled, current in zip(recall_cursor, current_cursor, strict=True)
            ):
                raise ValueError("paired recall carry contains future search material")
            for hit in recall.hits:
                if hit.document.authority != "world_fact":
                    continue
                for binding in hit.document.source_bindings:
                    bindings_by_ref.setdefault(binding.ref, set()).add(
                        (
                            binding.source_kind,
                            binding.authority_type,
                            binding.source_world_revision,
                            binding.immutable_hash,
                        )
                    )
        for evidence in proposal.evidence_refs:
            if evidence in trigger_evidence:
                continue
            matches = bindings_by_ref.get(evidence.ref_id, set())
            evidence_hash = evidence.immutable_hash.removeprefix("sha256:")
            exact = {
                (source_kind, authority_type)
                for source_kind, authority_type, revision, immutable_hash in matches
                if revision == evidence.source_world_revision and immutable_hash == evidence_hash
            }
            if not exact:
                raise ValueError("proposal evidence authority is absent from the frozen Capsule")
            allowed_kinds = {
                (
                    "settled_external_result"
                    if source_kind == "execution_receipt"
                    else _EVENT_EVIDENCE_KIND.get(authority_type, "committed_world_event")
                    if source_kind == "committed_event"
                    and not authority_type.startswith("situation_source:")
                    else None
                )
                for source_kind, authority_type in exact
            }
            if evidence.evidence_kind not in allowed_kinds:
                raise ValueError("proposal evidence kind does not match Capsule source authority")
        grammar = proposal_grammar_override or self._proposal_grammar
        if grammar is not None:
            grammar.validate(proposal)
        return proposal

    @staticmethod
    def _audit(
        *,
        model_call_id: str,
        attempt_id: str,
        route: ModelRoute,
        request_hash: str,
        output: ModelOutput | None,
        status: AuditStatus,
        failure_code: str | None,
        technical_failure: ValidationTechnicalFailure | None = None,
        slot: Literal["primary", "backup", "corrective"] | None = None,
        outcome: Literal[
            "winner",
            "returned",
            "invalid",
            "timeout",
            "exception",
            "hedge_cancelled",
            "hedge_lost",
            "budget_exhausted",
        ]
        | None = None,
    ) -> ModelResultAudit:
        attempted_model_id: str | None = None
        attempted_model_version: str | None = None
        terminal_usage: ModelUsageProvenance | None = None
        provider_subcall_audits: tuple[ProviderSubcallAudit, ...] = ()
        authored_candidate_audits: tuple[AuthoredCandidateInvocationAudit, ...] = ()
        if technical_failure is not None:
            provider_subcall_audits = technical_failure.provider_subcall_audits
            authored_candidate_audits = technical_failure.authored_candidate_audits
            nested_provider_call_ids = {
                item.model_call_id
                for item in (
                    *provider_subcall_audits,
                    *authored_candidate_audits,
                )
            }
            if (
                technical_failure.model_call_id is not None
                and technical_failure.model_call_id not in nested_provider_call_ids
            ):
                assert technical_failure.request_hash is not None
                model_call_id = technical_failure.model_call_id
                request_hash = technical_failure.request_hash
            attempted_model_id = technical_failure.attempted_model_id
            attempted_model_version = technical_failure.attempted_model_version
            terminal_usage = technical_failure.usage
        if output is not None and output.winning_model_call_id is not None:
            assert output.winning_request_hash is not None
            model_call_id = output.winning_model_call_id
            request_hash = output.winning_request_hash
        if output is not None:
            provider_subcall_audits = output.provider_subcall_audits
            authored_candidate_audits = output.authored_candidate_audits
        response_hash = _output_response_hash(output) if output is not None else None
        return ModelResultAudit(
            model_call_id=model_call_id,
            parent_model_call_id=(
                output.provider_parent_model_call_id
                if output is not None
                else None
            ),
            semantic_stream_part=(
                output.semantic_stream_part if output is not None else None
            ),
            model_result_ref=_model_result_ref(model_call_id, response_hash),
            attempt_id=attempt_id,
            route=route,
            model_id=output.model_id if output is not None else None,
            model_version=output.model_version if output is not None else None,
            attempted_model_id=attempted_model_id,
            attempted_model_version=attempted_model_version,
            request_hash=request_hash,
            response_hash=response_hash,
            status=status,
            failure_code=failure_code,
            slot=slot,
            outcome=outcome,
            input_tokens=(
                output.input_tokens
                if output is not None
                else terminal_usage.input_tokens
                if terminal_usage is not None
                else None
            ),
            output_tokens=(
                output.output_tokens
                if output is not None
                else terminal_usage.output_tokens
                if terminal_usage is not None
                else None
            ),
            usage=output.usage if output is not None else terminal_usage,
            recall_trace=(
                verify_trusted_recall_trace(output.recall_trace)
                if output is not None and output.recall_trace is not None
                else None
            ),
            prefetch_trace=(
                verify_trusted_recall_trace(output.prefetch_trace)
                if (
                    output is not None
                    and output.prefetch_trace is not None
                    and not output.presented_prefetch_traces
                )
                else None
            ),
            presented_prefetch_traces=(
                tuple(item.recorded() for item in output.presented_prefetch_traces)
                if output is not None
                else ()
            ),
            provider_subcall_audits=provider_subcall_audits,
            authored_candidate_audits=authored_candidate_audits,
            physical_provider_audits=(
                output.physical_provider_audits if output is not None else ()
            ),
        )

    @staticmethod
    def _result(
        capsule: ContextCapsule,
        *,
        proposal: ProposalInput | None,
        audit: ModelResultAudit,
        attempt_audits: tuple[ModelResultAudit, ...],
    ) -> DeliberationResult:
        identity = {
            "capsule_id": capsule.capsule_id,
            "proposal_hash": proposal.proposal_hash if proposal is not None else None,
            "attempt_audits": tuple(value.model_dump(mode="json") for value in attempt_audits),
        }
        return DeliberationResult(
            result_id=f"deliberation:{_digest(identity)}",
            capsule_id=capsule.capsule_id,
            proposal=proposal,
            audit=audit,
            attempt_audits=attempt_audits,
        )


__all__ = [
    "Deliberation",
    "DeliberationModelAdapter",
    "DeliberationResult",
    "ModelInput",
    "ModelOutput",
    "ModelUsageProvenance",
    "ModelResultAudit",
    "ModelRoute",
    "ProviderSubcallAudit",
    "TriggerMessage",
    "ModelRouterAdapter",
    "ProviderHealth",
    "QuickRecoveryAdapter",
    "RecoveryCandidateFailure",
    "RouteRequest",
    "begin_validation_reselection_recovery",
    "fit_pre_provider_wait_timeout",
    "fit_secondary_call_timeout",
    "mark_first_role_provider_completion",
    "mark_first_role_provider_entry",
    "claim_secondary_provider_slot",
    "claim_validation_corrective_provider_slot",
    "has_provider_slot_coordinator",
    "remaining_attempt_seconds",
    "secondary_provider_slot_kind",
]
