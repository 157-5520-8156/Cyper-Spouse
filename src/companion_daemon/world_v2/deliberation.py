"""Bounded model deliberation over a trusted Context Capsule.

Deliberation produces an inert ProposalEnvelope and audit material.  It has no
ledger, action, platform, or domain-mutation capability; ProposalAcceptance is
the only later authority seam.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime
import hashlib
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context_capsule import ContextCapsule, TrustedContextCapsuleHandle
from .interactive_turn_budget import InteractiveTurnBudget
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
from .route_hints import RouteHints, derive_route_hints


MAX_MODEL_OUTPUT_BYTES = 512_000
MAX_MODEL_OUTPUT_NODES = 16_384
MAX_ROUTE_REASON_CHARACTERS = 128
MAX_REPORTED_TOKENS = 10_000_000
MAX_INFLIGHT_PROVIDER_TASKS = 8
MAX_INFLIGHT_QUICK_TASKS = 2
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
_PROVIDER_SLOT_COORDINATOR: ContextVar["_ProviderSlotCoordinator | None"] = ContextVar(
    "world_v2_provider_slot_coordinator", default=None
)


class _ProviderSlotCoordinator:
    """Process-local two-slot lease shared with nested cognition adapters."""

    def __init__(self) -> None:
        self.second_kind: Literal["backup", "corrective"] | None = None
        self.episode_reserved = False

    def claim_second(self, kind: Literal["backup", "corrective"]) -> bool:
        if self.second_kind is not None:
            return False
        self.second_kind = kind
        return True


def claim_secondary_provider_slot(kind: Literal["backup", "corrective"]) -> bool:
    """Claim the turn's only secondary provider call.

    Direct adapter use has no coordinator and keeps its historical one-retry
    behavior.  Interactive Deliberation installs the coordinator.
    """

    coordinator = _PROVIDER_SLOT_COORDINATOR.get()
    return coordinator is None or coordinator.claim_second(kind)


def secondary_provider_slot_kind() -> Literal["backup", "corrective"] | None:
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
    return deadline - time.monotonic()


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
            "episode_disposition": getattr(value, "episode_disposition", None),
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
    attachment_media_types: tuple[
        Literal["image", "audio", "video", "file", "unknown"], ...
    ] = Field(default=(), max_length=16)

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
    model_content_json: str = Field(min_length=2, max_length=512_000)
    trigger_evidence: tuple[ProposalEvidenceRef, ...] = Field(default=(), max_length=8)
    trigger_message: TriggerMessage | None = None
    catalog_versions: tuple[str, ...] = ()
    recorded_draw_refs: tuple[str, ...] = ()
    # Values are reconstructable from RandomDrawRecorded; refs remain in the
    # hashed/audited request while this process-local convenience is excluded.
    recorded_cadence_draws: tuple[CadenceDraw, ...] = Field(default=(), exclude=True)


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


class ModelOutput(_FrozenModel):
    model_id: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=256)
    raw_proposal: dict[str, Any]
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    usage: ModelUsageProvenance | None = Field(default=None, exclude_if=lambda value: value is None)
    episode_disposition: Literal[
        "complete_without_more",
        "append",
        "cancel_pending",
        "supersede_pending",
    ] | None = None

    @model_validator(mode="after")
    def usage_matches_legacy_token_fields(self) -> "ModelOutput":
        if self.usage is not None and (self.input_tokens, self.output_tokens) != (
            self.usage.input_tokens,
            self.usage.output_tokens,
        ):
            raise ValueError("model output usage tokens do not match token fields")
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
    "main_timeout",
    "main_invalid",
    "main_exception",
    "main_timeout_recovered",
    "main_invalid_recovered",
    "main_exception_recovered",
    "recovery_failed",
]


class ModelResultAudit(_FrozenModel):
    model_call_id: str = Field(min_length=1)
    model_result_ref: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    route: ModelRoute
    model_id: str | None = None
    model_version: str | None = None
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: AuditStatus
    failure_code: str | None = Field(default=None, max_length=64)
    slot: Literal["primary", "backup", "corrective"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    outcome: Literal[
        "winner",
        "invalid",
        "timeout",
        "exception",
        "hedge_cancelled",
        "hedge_lost",
        "budget_exhausted",
    ] | None = Field(default=None, exclude_if=lambda value: value is None)
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    usage: ModelUsageProvenance | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def result_ref_is_orchestrator_derived(self) -> ModelResultAudit:
        if self.model_result_ref != _model_result_ref(self.model_call_id, self.response_hash):
            raise ValueError("model result ref is not bound to its call")
        identity = (self.model_id, self.model_version, self.response_hash)
        has_output = all(value is not None for value in identity)
        if not has_output and any(value is not None for value in identity):
            raise ValueError("model output audit identity is partial")
        if not has_output and (self.input_tokens is not None or self.output_tokens is not None):
            raise ValueError("model token counts require an output identity")
        if self.usage is not None:
            if not has_output:
                raise ValueError("model usage requires an output identity")
            if (self.input_tokens, self.output_tokens) != (
                self.usage.input_tokens,
                self.usage.output_tokens,
            ):
                raise ValueError("model usage tokens do not match audit tokens")
            if self.route.tier == "flash" and self.usage.thinking_tokens:
                raise ValueError("flash audit cannot report thinking tokens")
        required_failures = {
            "main_timeout": {"main_timeout", "primary_timeout", "corrective_timeout"},
            "main_invalid": {
                "main_invalid_output",
                "primary_invalid",
                "corrective_invalid",
            },
            "main_exception": {"main_exception", "primary_exception"},
            "main_timeout_recovered": {
                "main_timeout",
                "primary_timeout",
                "corrective_timeout",
            },
            "main_invalid_recovered": {
                "main_invalid_output",
                "primary_invalid",
                "corrective_invalid",
            },
            "main_exception_recovered": {"main_exception", "primary_exception"},
        }.get(self.status)
        if self.status == "proposal_validated":
            if not has_output or self.failure_code is not None:
                raise ValueError("validated proposal audit requires output and no failure")
        elif self.status in {"main_timeout", "main_exception"}:
            if has_output or self.failure_code not in (required_failures or set()):
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
        if (
            isinstance(self.proposal, MinimalProposal)
            and self.proposal.source_model_result != self.audit.model_result_ref
        ):
            raise ValueError("minimal proposal is not bound to its final model audit")
        if len(self.attempt_audits) == 1:
            if self.proposal is None:
                if self.audit.outcome != "budget_exhausted":
                    raise ValueError("single failed attempt must exhaust the shared budget")
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
                        {"main_timeout", "primary_timeout", "corrective_timeout"},
                        "main_timeout_recovered",
                    ),
                    "main_invalid": (
                        {"main_invalid_output", "primary_invalid", "corrective_invalid"},
                        "main_invalid_recovered",
                    ),
                    "main_exception": ({"main_exception", "primary_exception"}, "main_exception_recovered"),
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
        expression_episode_mode: Literal["off", "shadow", "on"] = "off",
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
        self._episode_tail_tasks: dict[
            str, asyncio.Task[EpisodeTailResult | None]
        ] = {}

    def expression_episode_diagnostics(self) -> dict[str, object]:
        if self._episode_diagnostics is None:
            return ExpressionEpisodeDiagnostics(mode="off").snapshot()
        return self._episode_diagnostics.snapshot()

    async def await_expression_episode_tail(
        self, trigger_ref: str
    ) -> EpisodeTailResult | None:
        task = self._episode_tail_tasks.get(trigger_ref)
        if task is None:
            return None
        return await asyncio.shield(task)

    @property
    def expression_episode_mode(self) -> Literal["off", "shadow", "on"]:
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
        budget: InteractiveTurnBudget | None = None,
    ) -> DeliberationResult:
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
        cadence_refs = tuple(
            dict.fromkeys(item.draw_ref for item in recorded_cadence_draws)
        )
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
            model_content_json=trusted.model_content_json,
            trigger_evidence=trigger_evidence,
            trigger_message=trigger_message,
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
            )
        failure_code: str | None = None
        recovered_status: AuditStatus | None = None
        output: ModelOutput | None = None
        try:
            deadline_token = _ATTEMPT_DEADLINE.set(time.monotonic() + self._main_timeout)
            try:
                output = _checked_output(
                    await self._with_deadline(
                        self._main.propose(model_input),
                        timeout=self._main_timeout,
                        label=call_id,
                        lane="main",
                    )
                )
            finally:
                _ATTEMPT_DEADLINE.reset(deadline_token)
            proposal = self._validated_proposal(output, trusted, trigger_evidence=trigger_evidence)
            proposal = self._bind_minimal_model_result(proposal, call_id, output)
            status: AuditStatus = "proposal_validated"
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
                quick_deadline_token = _ATTEMPT_DEADLINE.set(
                    time.monotonic() + self._quick_timeout
                )
                try:
                    quick_output = _checked_output(
                        await self._with_deadline(
                            self._quick.recover(quick_input, failure_code or "main_failure"),
                            timeout=self._quick_timeout,
                            label=quick_call_id,
                            lane="quick",
                        )
                    )
                finally:
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
    ) -> DeliberationResult:
        """Race at most two fully validated candidates under one absolute deadline."""

        async def candidate(
            operation: Callable[[], Awaitable[ModelOutput]],
            *,
            call_id: str,
            minimal_only: bool,
            lane: Literal["main", "quick"],
            include_reserve: bool = False,
            proposal_grammar_override: ProposalGrammar | None = None,
        ) -> tuple[ProposalInput | None, ModelOutput | None, str | None]:
            remaining = budget.remaining(include_reserve=include_reserve)
            if remaining <= 0:
                return None, None, "timeout"
            output: ModelOutput | None = None
            token = _ATTEMPT_DEADLINE.set(budget.candidate_deadline)
            try:
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
                raise
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
                _ATTEMPT_DEADLINE.reset(token)

        primary_call_id = model_input.call_id
        slot_coordinator = _ProviderSlotCoordinator()
        provisional_operation = getattr(self._main, "propose_provisional", None)
        already_evaluated = getattr(
            self._main, "episode_provisional_already_evaluated", None
        )
        episode_enabled = (
            self._expression_episode_mode != "off"
            and callable(provisional_operation)
            and not (
                callable(already_evaluated)
                and already_evaluated(model_input)
            )
        )
        episode_started_at = budget.clock()
        episode_recorded = False

        def record_episode(
            result: tuple[ProposalInput | None, ModelOutput | None, str | None],
            *,
            winner: Literal["full", "provisional"],
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
                    rejection_kind = (
                        "placeholder"
                        if "placeholder" in str(exc)
                        else "other"
                    )
                else:
                    rejection_kind = "grounding"
            self._episode_diagnostics.record(
                candidate_ms=max(0.0, (budget.clock() - episode_started_at) * 1_000),
                valid=valid,
                winner=winner,
                would_send=valid,
                would_append=bool(
                    output is not None
                    and output.episode_disposition == "append"
                ),
                slot_calls=2,
                rejection_kind=rejection_kind,
            )
            episode_recorded = True

        budget.mark("primary")
        slot_token = _PROVIDER_SLOT_COORDINATOR.set(slot_coordinator)
        try:
            primary_task = asyncio.create_task(
                candidate(
                    lambda: self._main.propose(model_input),
                    call_id=primary_call_id,
                    minimal_only=False,
                    lane="main",
                )
            )
            hedge_timer = asyncio.create_task(budget.wait_for_hedge())
            deadline_timer = asyncio.create_task(budget.sleep(budget.remaining()))
        finally:
            _PROVIDER_SLOT_COORDINATOR.reset(slot_token)
        backup_task: asyncio.Task[
            tuple[ProposalInput | None, ModelOutput | None, str | None]
        ] | None = None
        primary_result: tuple[ProposalInput | None, ModelOutput | None, str | None] | None = None
        primary_timing_recorded = False
        backup_result: tuple[ProposalInput | None, ModelOutput | None, str | None] | None = None
        backup_call_id: str | None = None
        backup_input: ModelInput | None = None
        backup_request_hash: str | None = None
        primary_failure_for_recovery = "main_timeout"

        def start_backup(failure_code: str) -> None:
            nonlocal backup_task, backup_call_id, backup_input, backup_request_hash
            if backup_task is not None or budget.remaining() <= 0:
                return
            hedge_available = getattr(self._quick, "has_hedge_provider", None)
            if (
                failure_code == "main_timeout"
                and callable(hedge_available)
                and not hedge_available(model_input)
            ):
                return
            if not slot_coordinator.claim_second("backup"):
                return
            budget.mark("hedge_started")
            backup_call_id = f"model-call:{_digest({**call_identity, 'lane': 'hedge', 'main_failure': failure_code})}"
            backup_input = model_input.model_copy(update={"call_id": backup_call_id})
            backup_request_hash = _digest(backup_input.model_dump(mode="json"))
            token = _PROVIDER_SLOT_COORDINATOR.set(slot_coordinator)
            try:
                backup_task = asyncio.create_task(
                    candidate(
                        lambda: self._quick.recover(backup_input, failure_code),
                        call_id=backup_call_id,
                        minimal_only=self._recovery_mode == "minimal_only",
                        lane="quick",
                    )
                )
            finally:
                _PROVIDER_SLOT_COORDINATOR.reset(token)

        if episode_enabled and slot_coordinator.claim_second("backup"):
            slot_coordinator.episode_reserved = True
            budget.mark("provisional")
            backup_call_id = (
                f"model-call:{_digest({**call_identity, 'lane': 'provisional'})}"
            )
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
                        proposal_grammar_override=self._expression_episode_grammar,
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
                if hedge_timer in done and primary_result is None:
                    start_backup("main_timeout")
                if primary_task in done and primary_result is None:
                    primary_result = primary_task.result()
                    if (
                        not primary_timing_recorded
                        and self._episode_diagnostics is not None
                    ):
                        self._episode_diagnostics.record_full(
                            (budget.clock() - budget.started_at) * 1_000
                        )
                        primary_timing_recorded = True
                    proposal, output, failure = primary_result
                    if proposal is not None and failure is None:
                        budget.mark("candidate_validated")
                        accept_candidate = getattr(self._main, "accept_candidate", None)
                        if callable(accept_candidate):
                            accept_candidate(model_input)
                        loser_audit: ModelResultAudit | None = None
                        if (
                            backup_task is not None
                            and self._expression_episode_mode == "shadow"
                        ):
                            if backup_task.done():
                                record_episode(
                                    backup_task.result(), winner="provisional"
                                )
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
                            discard_candidate = getattr(
                                self._quick, "discard_candidate", None
                            )
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
                            "corrective"
                            if slot_coordinator.second_kind == "corrective"
                            else "primary"
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
                                (loser_audit, final)
                                if loser_audit is not None
                                else (final,)
                            ),
                        )
                    primary_failure_for_recovery = {
                        "invalid": "main_invalid_output",
                        "exception": "main_exception",
                        "timeout": "main_timeout",
                    }.get(failure or "", "main_exception")
                    discard_candidate = getattr(self._main, "discard_candidate", None)
                    if callable(discard_candidate):
                        discard_candidate(model_input)
                    start_backup(primary_failure_for_recovery)
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
                                full_proposal, full_output, full_failure = (
                                    await continuing_primary
                                )
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
                                    full_output.episode_disposition
                                    or "complete_without_more"
                                )
                                if (
                                    disposition != "append"
                                    or full_proposal is None
                                ):
                                    return EpisodeTailResult(
                                        disposition=disposition
                                    )
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

                            self._episode_tail_tasks[trusted.trigger_ref] = (
                                asyncio.create_task(
                                    finish_full_tail(),
                                    name=f"expression-tail:{trusted.trigger_ref}",
                                )
                            )
                            primary_task = None
                            assert (
                                backup_call_id is not None
                                and backup_request_hash is not None
                            )
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
                        main_status: AuditStatus = {
                            "invalid": "main_invalid",
                            "exception": "main_exception",
                            "timeout": "main_timeout",
                        }.get(main_failure or "", "main_timeout")
                        main_failure_code = {
                            "invalid": "primary_invalid",
                            "exception": "primary_exception",
                            "timeout": "primary_timeout",
                        }.get(main_failure or "", "primary_timeout")
                        main_audit = self._audit(
                            model_call_id=primary_call_id,
                            attempt_id=attempt_id,
                            route=route,
                            request_hash=request_hash,
                            output=main_output,
                            status=main_status,
                            failure_code=main_failure_code,
                            slot="primary",
                            outcome=(
                                "hedge_cancelled"
                                if main_failure == "timeout"
                                else (main_failure or "exception")
                            ),
                        )
                        recovered_status: AuditStatus = {
                            "primary_invalid": "main_invalid_recovered",
                            "primary_exception": "main_exception_recovered",
                            "primary_timeout": "main_timeout_recovered",
                        }[main_failure_code]
                        assert backup_call_id is not None and backup_request_hash is not None
                        final = self._audit(
                            model_call_id=backup_call_id,
                            attempt_id=attempt_id,
                            route=route,
                            request_hash=backup_request_hash,
                            output=output,
                            status=recovered_status,
                            failure_code=main_failure_code,
                            slot="backup",
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
                    break

            if primary_result is None:
                primary_result = (None, None, "timeout")
            main_failure = primary_result[2]
            main_status = {
                "invalid": "main_invalid",
                "exception": "main_exception",
                "timeout": "main_timeout",
            }.get(main_failure or "", "main_timeout")
            main_failure_code = {
                "invalid": (
                    "corrective_invalid"
                    if slot_coordinator.second_kind == "corrective"
                    else "primary_invalid"
                ),
                "exception": "primary_exception",
                "timeout": (
                    "corrective_timeout"
                    if slot_coordinator.second_kind == "corrective"
                    else "primary_timeout"
                ),
            }.get(main_failure or "", "primary_timeout")
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
                slot="primary",
                outcome=(
                    "budget_exhausted"
                    if budget.remaining() <= 0
                    else (main_failure or "exception")
                ),
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
            backup_kind = slot_coordinator.second_kind or "backup"
            final = self._audit(
                model_call_id=backup_call_id,
                attempt_id=attempt_id,
                route=route,
                request_hash=backup_request_hash,
                output=backup_result[1] if backup_result is not None else None,
                status="recovery_failed",
                failure_code=f"{backup_kind}_{backup_failure or 'exception'}",
                slot=backup_kind,
                outcome=(
                    "budget_exhausted"
                    if budget.remaining() <= 0
                    else (backup_failure or "exception")
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

    async def _with_deadline(
        self,
        awaitable: Awaitable[_T],
        *,
        timeout: float,
        label: str,
        lane: Literal["main", "quick"],
    ) -> _T:
        """Enforce a caller deadline even if a provider suppresses cancellation.

        A provider task that ignores cancellation is detached, observed, and
        counted against a small in-flight ceiling.  Production adapters still
        must terminate their own transport work on cancellation.
        """

        tasks = self._quick_provider_tasks if lane == "quick" else self._provider_tasks
        ceiling = MAX_INFLIGHT_QUICK_TASKS if lane == "quick" else MAX_INFLIGHT_PROVIDER_TASKS
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
        raise TimeoutError

    @staticmethod
    def _bind_minimal_model_result(
        proposal: ProposalInput, model_call_id: str, output: ModelOutput
    ) -> ProposalInput:
        if not isinstance(proposal, MinimalProposal):
            return proposal
        response_hash = _digest(output.raw_proposal)
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
        slot: Literal["primary", "backup", "corrective"] | None = None,
        outcome: Literal[
            "winner",
            "invalid",
            "timeout",
            "exception",
            "hedge_cancelled",
            "hedge_lost",
            "budget_exhausted",
        ] | None = None,
    ) -> ModelResultAudit:
        response_hash = _digest(output.raw_proposal) if output is not None else None
        return ModelResultAudit(
            model_call_id=model_call_id,
            model_result_ref=_model_result_ref(model_call_id, response_hash),
            attempt_id=attempt_id,
            route=route,
            model_id=output.model_id if output is not None else None,
            model_version=output.model_version if output is not None else None,
            request_hash=request_hash,
            response_hash=response_hash,
            status=status,
            failure_code=failure_code,
            slot=slot,
            outcome=outcome,
            input_tokens=output.input_tokens if output is not None else None,
            output_tokens=output.output_tokens if output is not None else None,
            usage=output.usage if output is not None else None,
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
    "TriggerMessage",
    "ModelRouterAdapter",
    "ProviderHealth",
    "QuickRecoveryAdapter",
    "RouteRequest",
    "fit_secondary_call_timeout",
    "claim_secondary_provider_slot",
    "has_provider_slot_coordinator",
    "remaining_attempt_seconds",
    "secondary_provider_slot_kind",
]
