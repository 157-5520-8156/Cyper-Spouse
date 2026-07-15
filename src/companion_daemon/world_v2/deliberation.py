"""Bounded model deliberation over a trusted Context Capsule.

Deliberation produces an inert ProposalEnvelope and audit material.  It has no
ledger, action, platform, or domain-mutation capability; ProposalAcceptance is
the only later authority seam.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import logging
import math
from typing import Any, Awaitable, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context_capsule import ContextCapsule, TrustedContextCapsuleHandle
from .proposal_envelope import (
    MinimalProposal,
    ProposalEvidenceRef,
    ProposalInput,
    validate_proposal_envelope,
)


MAX_MODEL_OUTPUT_BYTES = 512_000
MAX_MODEL_OUTPUT_NODES = 16_384
MAX_ROUTE_REASON_CHARACTERS = 128
MAX_REPORTED_TOKENS = 10_000_000
MAX_INFLIGHT_PROVIDER_TASKS = 8
MAX_INFLIGHT_QUICK_TASKS = 2
_T = TypeVar("_T")
_LOG = logging.getLogger(__name__)

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
        material: object = {
            "model_id": getattr(value, "model_id", None),
            "model_version": getattr(value, "model_version", None),
            "raw_proposal": raw,
            "input_tokens": getattr(value, "input_tokens", None),
            "output_tokens": getattr(value, "output_tokens", None),
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
    text: str = Field(min_length=1, max_length=12_000)


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


class ModelOutput(_FrozenModel):
    model_id: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=256)
    raw_proposal: dict[str, Any]
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)


class ModelRouterAdapter(Protocol):
    async def route(self, request: RouteRequest) -> ModelRoute: ...


class DeliberationModelAdapter(Protocol):
    async def propose(self, request: ModelInput) -> ModelOutput: ...


class QuickRecoveryAdapter(Protocol):
    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput: ...


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
    input_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)
    output_tokens: int | None = Field(default=None, ge=0, le=MAX_REPORTED_TOKENS)

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
        required_failure = {
            "main_timeout": "main_timeout",
            "main_invalid": "main_invalid_output",
            "main_exception": "main_exception",
            "main_timeout_recovered": "main_timeout",
            "main_invalid_recovered": "main_invalid_output",
            "main_exception_recovered": "main_exception",
        }.get(self.status)
        if self.status == "proposal_validated":
            if not has_output or self.failure_code is not None:
                raise ValueError("validated proposal audit requires output and no failure")
        elif self.status in {"main_timeout", "main_exception"}:
            if has_output or self.failure_code != required_failure:
                raise ValueError("terminal main audit has an invalid output or failure")
        elif self.status == "main_invalid":
            if self.failure_code != required_failure:
                raise ValueError("invalid main audit has the wrong failure code")
        elif self.status == "recovery_failed":
            if not (self.failure_code or "").startswith("quick_"):
                raise ValueError("failed recovery audit requires a quick failure code")
        elif not has_output or self.failure_code != required_failure:
            raise ValueError("recovered audit lacks output or matching main failure")
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
        if self.attempt_audits[-1] != self.audit:
            raise ValueError("final audit must be the last model-attempt audit")
        call_ids = tuple(value.model_call_id for value in self.attempt_audits)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("model attempts require distinct call identities")
        if (
            isinstance(self.proposal, MinimalProposal)
            and self.proposal.source_model_result != self.audit.model_result_ref
        ):
            raise ValueError("minimal proposal is not bound to its final model audit")
        if len(self.attempt_audits) == 1:
            if self.audit.status != "proposal_validated" or self.proposal is None:
                raise ValueError("single-attempt deliberation must be a validated main proposal")
        else:
            main, quick = self.attempt_audits
            expected = {
                "main_timeout": ("main_timeout", "main_timeout_recovered"),
                "main_invalid": ("main_invalid_output", "main_invalid_recovered"),
                "main_exception": ("main_exception", "main_exception_recovered"),
            }.get(main.status)
            if expected is None or main.failure_code != expected[0]:
                raise ValueError("recovery lineage has an invalid main terminal audit")
            if quick.status == "recovery_failed":
                if self.proposal is not None or not (quick.failure_code or "").startswith("quick_"):
                    raise ValueError("failed recovery has invalid proposal or failure code")
            elif quick.status != expected[1] or quick.failure_code != expected[0]:
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


class Deliberation:
    """Orchestrate routing and model calls without granting write authority."""

    def __init__(
        self,
        *,
        router: ModelRouterAdapter,
        main_model: DeliberationModelAdapter,
        quick_recovery: QuickRecoveryAdapter,
        main_timeout_seconds: float = 8.0,
        quick_timeout_seconds: float = 3.0,
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
        self._provider_tasks: set[asyncio.Task[object]] = set()
        self._quick_provider_tasks: set[asyncio.Task[object]] = set()

    async def deliberate(
        self,
        capsule_handle: TrustedContextCapsuleHandle,
        *,
        attempt_id: str,
        catalog_versions: tuple[str, ...] = (),
        recorded_draw_refs: tuple[str, ...] = (),
        trigger_evidence: tuple[ProposalEvidenceRef, ...] = (),
        trigger_message: TriggerMessage | None = None,
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
        route = await self._route(
            RouteRequest(
                capsule_id=trusted.capsule_id,
                trigger_ref=trusted.trigger_ref,
                model_content_hash=content_hash,
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
        )
        request_hash = _digest(model_input.model_dump(mode="json"))
        failure_code: str | None = None
        recovered_status: AuditStatus | None = None
        output: ModelOutput | None = None
        try:
            output = _checked_output(
                await self._with_deadline(
                    self._main.propose(model_input),
                    timeout=self._main_timeout,
                    label=call_id,
                    lane="main",
                )
            )
            proposal = self._validated_proposal(
                output, trusted, trigger_evidence=trigger_evidence
            )
            proposal = self._bind_minimal_model_result(proposal, call_id, output)
            status: AuditStatus = "proposal_validated"
        except TimeoutError:
            failure_code = "main_timeout"
            recovered_status = "main_timeout_recovered"
        except (ValueError, TypeError):
            failure_code = "main_invalid_output"
            recovered_status = "main_invalid_recovered"
        except Exception:
            failure_code = "main_exception"
            recovered_status = "main_exception_recovered"

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
                quick_output = _checked_output(
                    await self._with_deadline(
                        self._quick.recover(quick_input, failure_code or "main_failure"),
                        timeout=self._quick_timeout,
                        label=quick_call_id,
                        lane="quick",
                    )
                )
                proposal = self._validated_proposal(
                    quick_output,
                    trusted,
                    minimal_only=True,
                    trigger_evidence=trigger_evidence,
                )
                proposal = self._bind_minimal_model_result(proposal, quick_call_id, quick_output)
                status = recovered_status
            except TimeoutError:
                quick_failure = "quick_timeout"
            except (ValueError, TypeError):
                quick_failure = "quick_invalid_output"
            except Exception:
                quick_failure = "quick_exception"
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

    @staticmethod
    def _validated_proposal(
        output: ModelOutput,
        capsule: ContextCapsule,
        *,
        minimal_only: bool = False,
        trigger_evidence: tuple[ProposalEvidenceRef, ...] = (),
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
            input_tokens=output.input_tokens if output is not None else None,
            output_tokens=output.output_tokens if output is not None else None,
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
    "ModelResultAudit",
    "ModelRoute",
    "TriggerMessage",
    "ModelRouterAdapter",
    "ProviderHealth",
    "QuickRecoveryAdapter",
    "RouteRequest",
]
