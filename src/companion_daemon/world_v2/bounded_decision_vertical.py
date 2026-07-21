"""BoundedDecisionVertical: the shared ceremony of autonomous decision lanes.

Every World v2 autonomous behaviour ("vertical") repeats the same ~500 lines
of ritual: durable trigger identity, ledger shims, claim/lease/CAS lifecycle,
recorded restraint draws, one bounded model call under a closed JSON contract,
single-call audit wrapping, proposal grammar validation and an existing
acceptance seam.  This module owns that ceremony once, parameterized by frozen
identity strings, so a vertical only declares its semantics (opportunity
predicate, weight compiler, prompt/contract, proposal compiler, acceptance
binding) through a :class:`VerticalSpec`.

Three lifecycle engines cover the shapes catalogued in
``docs/design/bounded-decision-vertical-framework.md`` §2.1:

- :class:`AnchoredTriggerLifecycle` — durable ``TriggerProcess`` anchored to a
  committed World Event: open → claim (lease) → work → complete, all CAS
  commits, event/commit identity shapes frozen per namespace.  Interpreter:
  :class:`AnchoredVerticalRuntime`.
- :class:`DailyCheckLifecycle` — clock-check verticals without a lease: wake
  exactness validation, local-date(+slot) identity, durable check events and
  crash recovery decoded from the check payload.  Engine primitives:
  :class:`DailyCheckEngine`.
- :class:`InlineOnceLifecycle` — one-shot in-turn verticals deduplicated by a
  proposal prefix on the audit projection, every failure silent by contract.
  Interpreter: :class:`InlineOnceVerticalWorker`.

Replay-compatibility contract: the engines are byte-compatible with the
hand-written pilots they absorb (``quick_reaction.py``,
``afterthought_author.py``).  Every identity string, projection re-read and
commit boundary is preserved exactly; the shadow-replay suite holds both
implementations to zero byte difference.  The framework version is therefore
deliberately *not* part of any event identity material (owner decision 3,
2026-07-20).

Escape hatch: a vertical may always stay hand-written.  Hand-rolled wells
register ``hand_rolled=True`` in ``vertical_registry`` and never import this
module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import logging
import time
from typing import Any, Literal, Protocol

from .action_pump import ActionExecutor, ActionPump
from .chat_model_deliberation_adapter import ChatCompletionModel
from .deliberation import (
    DeliberationResult,
    ModelResultAudit,
    ModelRoute,
    _digest as _deliberation_digest,
    _model_result_ref,
)
from .errors import ConcurrencyConflict, IdempotencyConflict
from .event_identity import domain_idempotency_key
from .expression_plan_acceptance import (
    ExpressionPlanAcceptanceError,
    ExpressionPlanBudgetPolicy,
    derive_expression_plan_material,
)
from .expression_plan_atomic_recorder import ExpressionPlanAtomicRecorder
from .ledger import LedgerPort
from .production_proposal_grammar import (
    ProductionProposalGrammarError,
    production_proposal_grammar,
)
from .proposal_audit import ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import DecisionProposal
from .random_authority import RandomAuthority, RandomDrawRecordedPayload
from .schema_core import FrozenModel
from .schemas import (
    ClaimLease,
    LedgerProjection,
    Observation,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)
from .settlement import SettlementPlanner


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical digests (formerly copied into every vertical; §2.2 item 1)
# ---------------------------------------------------------------------------


def canonical_json(value: object) -> str:
    """The quick/afterthought canonical form: sorted keys, no NaN, no spaces."""

    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Ledger shims (formerly ~40 lines per vertical; §2.2 items 2-3)
# ---------------------------------------------------------------------------


class LedgerOps:
    """Async projections and commits over a possibly blocking LedgerPort."""

    def __init__(self, ledger: LedgerPort) -> None:
        self._ledger = ledger

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    @property
    def world_id(self) -> str:
        return self._ledger.world_id

    async def project(self) -> LedgerProjection:
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def commit(
        self,
        events,
        *,
        world_revision: int,
        deliberation_revision: int,
        commit_id: str | None = None,
    ):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit,
                events,
                expected_world_revision=world_revision,
                expected_deliberation_revision=deliberation_revision,
                commit_id=commit_id,
            )
        return self._ledger.commit(
            events,
            expected_world_revision=world_revision,
            expected_deliberation_revision=deliberation_revision,
            commit_id=commit_id,
        )

    async def commit_at_cursor(self, events, *, cursor: ProjectionCursor, commit_id: str):
        kwargs = dict(events=events, expected_cursor=cursor, commit_id=commit_id)
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.commit_at_cursor, **kwargs)
        return self._ledger.commit_at_cursor(**kwargs)

    async def commit_accepted(self, batch, *, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit_accepted, batch, expected_cursor=cursor
            )
        return self._ledger.commit_accepted(batch, expected_cursor=cursor)

    async def call(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Bridge an arbitrary blocking ledger-adjacent call off the loop."""

        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(fn, *args, **kwargs)
        return fn(*args, **kwargs)

    @staticmethod
    def cursor(projection: LedgerProjection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


# ---------------------------------------------------------------------------
# Frozen identity templates (§3.1: string shapes frozen per namespace)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnchoredIdentityTemplates:
    """The exact ``event:<ns>:...`` string shapes of an anchored vertical.

    ``namespace`` is the frozen segment used by the pilot implementations
    (for example ``afterthought``); changing it changes event identity and is
    therefore forbidden for a migrated vertical.
    """

    namespace: str

    def opened_event_id(self, payload: dict[str, object]) -> str:
        return f"event:{self.namespace}:opened:" + digest(payload)

    def opened_commit_id(self, payload: dict[str, object]) -> str:
        return f"commit:{self.namespace}:opened:" + digest(payload)

    def claim_event_id(self, payload: dict[str, object]) -> str:
        return f"event:{self.namespace}:claim:" + digest(payload)

    def claim_commit_id(self, payload: dict[str, object]) -> str:
        return f"commit:{self.namespace}:claim:" + digest(payload)

    def claim_idempotency_fallback(self, payload: dict[str, object]) -> str:
        return f"world-v2:{self.namespace}-claim:" + digest(payload)

    def completed_event_id(self, payload: dict[str, object]) -> str:
        return f"event:{self.namespace}:completed:" + digest(payload)

    def completed_commit_id(self, payload: dict[str, object]) -> str:
        return f"commit:{self.namespace}:completed:" + digest(payload)

    def completed_idempotency(self, world_id: str, payload: dict[str, object]) -> str:
        return f"world-v2:{self.namespace}-completed:" + digest(
            {"world": world_id, "payload": payload}
        )

    def worker_attempt_id(self, trigger_id: str, attempt_number: int) -> str:
        return f"attempt:{self.namespace}-worker:" + digest(
            {"trigger": trigger_id, "attempt": attempt_number}
        )

    def outcome_ref(self, outcome: str) -> str:
        return f"{self.namespace}:{outcome}"

    def acceptance_id(self, proposal_id: str) -> str:
        return f"acceptance:{self.namespace}:" + digest(proposal_id)


@dataclass(frozen=True, slots=True)
class InlineIdentityTemplates:
    """String shapes of an inline-once vertical (pilot: ``quick-reaction``)."""

    namespace: str

    def acceptance_id(self, proposal_id: str) -> str:
        return f"acceptance:{self.namespace}:{proposal_id}"

    def pump_source(self) -> str:
        return f"world-v2:{self.namespace}-pump"


# ---------------------------------------------------------------------------
# Recorded restraint draws (§2.2 draw ritual; semantics stay in the spec)
# ---------------------------------------------------------------------------


class DecisionProfileLike(Protocol):
    candidate_weights: Mapping[str, int]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DrawPlan:
    """One fully compiled draw: identity, candidates and mass, no mechanics."""

    attempt_id: str
    candidate_refs: tuple[str, ...]
    candidate_weights: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class DrawContext:
    """What a draw planner may see: the anchor, projection and prior draws."""

    opportunity: object
    projection: LedgerProjection
    logical_time: datetime
    previous: Mapping[str, RandomDrawRecordedPayload]


@dataclass(frozen=True, slots=True)
class DrawStep:
    """One recorded draw of a vertical; the plan closure owns the semantics.

    ``halt_unless``: when set, any selected candidate other than this ref
    terminates the run with ``halt_outcome`` (recorded via the lifecycle's
    completion semantics) and ``halt_reason``.
    """

    step_id: str
    plan: Callable[[DrawContext], DrawPlan]
    catalog_version: str
    weight_policy_version: str
    halt_unless: str | None = None
    halt_outcome: str | None = None
    halt_reason: str | None = None
    interpret: Callable[[str], object] | None = None


# ---------------------------------------------------------------------------
# Bounded model step (§3.1: timeout, canonical messages, failure policies)
# ---------------------------------------------------------------------------

ModelFailurePolicy = Literal["decline_quietly", "raise_retryable", "correction_retry_once"]


class BoundedModelUnavailable(RuntimeError):
    """Raised by ``raise_retryable`` so the caller's durable claim retries."""


@dataclass(frozen=True, slots=True)
class ModelStepContext:
    """Inputs available to the messages/parse closures of one bounded call."""

    opportunity: object
    projection: LedgerProjection
    draws: Mapping[str, RandomDrawRecordedPayload]
    interpretations: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SingleCallAuditTemplate:
    """Identity material of the one-call ``ModelResultAudit`` wrapper.

    Shapes are frozen from the pilots: ``model-call:<ns>:<digest>``,
    ``attempt:<ns>:<digest>``; the digest inputs are the spec's prompt
    material and the raw response hash.
    """

    call_namespace: str
    route: ModelRoute
    model_version: str
    fallback_model_id: str


@dataclass(frozen=True, slots=True)
class BoundedModelStep:
    """One bounded model call under a closed JSON contract.

    ``messages`` may be async (a vertical may compile advisory context, for
    example a recent-dialogue frame, before prompting).  ``parse`` owns the
    strict contract including any semantic guards; returning ``None`` means
    the vertical declines quietly for this opportunity.
    """

    messages: Callable[
        [ModelStepContext], list[dict[str, str]] | Awaitable[list[dict[str, str]]]
    ]
    parse: Callable[[str, ModelStepContext], object | None]
    timeout_seconds: float
    audit: SingleCallAuditTemplate
    prompt_material: Callable[[ModelStepContext, DecisionProposal], dict[str, object]]
    temperature: float = 0.0
    failure_policy: ModelFailurePolicy = "decline_quietly"


async def run_bounded_model_step(
    *,
    step: BoundedModelStep,
    model: ChatCompletionModel,
    context: ModelStepContext,
    log_label: str,
) -> tuple[object | None, str | None]:
    """Return ``(verdict, raw_response)``; both ``None`` on quiet failure.

    Failure policies:

    - ``decline_quietly``: every transport/timeout/contract failure returns
      ``(None, raw-or-None)``; the lane treats it as a decline.
    - ``raise_retryable``: transport/timeout failures raise
      :class:`BoundedModelUnavailable` so a durable claim retries later;
      contract violations still decline (the model *did* answer).
    - ``correction_retry_once``: one corrective re-ask after a contract
      violation (the private-impression discipline); transport failures
      follow ``raise_retryable``.
    """

    messages = step.messages(context)
    if asyncio.iscoroutine(messages):
        messages = await messages
    attempts = 2 if step.failure_policy == "correction_retry_once" else 1
    raw: str | None = None
    for attempt in range(attempts):
        try:
            async with asyncio.timeout(step.timeout_seconds):
                raw_value = await model.complete(messages, temperature=step.temperature)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if step.failure_policy in {"raise_retryable", "correction_retry_once"}:
                raise BoundedModelUnavailable(
                    f"{log_label} model provider is unavailable"
                ) from exc
            _LOG.warning(
                "%s gate unavailable: %s: %s",
                log_label,
                type(exc).__name__,
                str(exc)[:240],
            )
            return None, None
        raw = raw_value if isinstance(raw_value, str) else None
        verdict = step.parse(raw_value, context) if raw is not None else None
        if verdict is not None:
            return verdict, raw
        if attempt + 1 < attempts:
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": (
                        "上一次输出不符合约定的 JSON 合同。请严格按合同重新输出，"
                        "不要添加任何其他内容。"
                    ),
                },
            ]
    return None, raw


def single_call_deliberation_result(
    *,
    template: SingleCallAuditTemplate,
    model: ChatCompletionModel,
    prompt_material: dict[str, object],
    proposal: DecisionProposal,
    raw_response: str,
) -> DeliberationResult:
    """Wrap one bounded gate call as an auditable single-attempt result.

    Byte-identical to the ``_deliberation_result`` helpers previously copied
    into ``quick_reaction.py:848`` and ``afterthought_author.py:1096``.
    """

    call_id = f"model-call:{template.call_namespace}:{_deliberation_digest(prompt_material)}"
    response_hash = _deliberation_digest(raw_response)
    audit = ModelResultAudit(
        model_call_id=call_id,
        model_result_ref=_model_result_ref(call_id, response_hash),
        attempt_id=(
            f"attempt:{template.call_namespace}:"
            + _deliberation_digest([call_id, response_hash])
        ),
        route=template.route,
        model_id=str(getattr(model, "model", template.fallback_model_id)),
        model_version=template.model_version,
        request_hash=_deliberation_digest(prompt_material),
        response_hash=response_hash,
        status="proposal_validated",
    )
    capsule_id = _deliberation_digest(prompt_material)
    identity = {
        "capsule_id": capsule_id,
        "proposal_hash": proposal.proposal_hash,
        "attempt_audits": (audit.model_dump(mode="json"),),
    }
    return DeliberationResult(
        result_id=f"deliberation:{_deliberation_digest(identity)}",
        capsule_id=capsule_id,
        proposal=proposal,
        audit=audit,
        attempt_audits=(audit,),
    )


# ---------------------------------------------------------------------------
# Lifecycle declarations
# ---------------------------------------------------------------------------


class AnchoredOpportunity(Protocol):
    """The anchor a durable A-shape vertical binds its trigger process to."""

    @property
    def anchor_event_ref(self) -> str: ...

    @property
    def anchored_at(self) -> datetime: ...

    @property
    def trace_id(self) -> str: ...

    @property
    def correlation_id(self) -> str: ...


@dataclass(frozen=True, slots=True)
class AnchoredTriggerLifecycle:
    """Durable event-anchored lifecycle (silence/plan/impression/afterthought
    family): open → claim (lease) → work → complete, event shapes verbatim
    compatible with the existing reducers' ``TriggerProcess`` constraints."""

    process_kind: str
    identity: AnchoredIdentityTemplates
    trigger_id: Callable[[str, Any], str]
    trigger_ref: Callable[[Any], str]
    lease_seconds: int = 120


@dataclass(frozen=True, slots=True)
class DailyCheckLifecycle:
    """Clock-check lifecycle (npc-initiative/aspiration/invitation/future-life
    family): wake exactness, local-date(+slot) identity, durable check events
    with ``decision == "selected"`` crash recovery."""

    namespace: str
    proposal_kind: str
    wake_reason_prefix: str


@dataclass(frozen=True, slots=True)
class InlineOnceLifecycle:
    """Same-turn one-shot lifecycle (quick-reaction family): proposal-prefix
    dedupe on the audit projection; every failure silent by contract."""

    identity: InlineIdentityTemplates
    proposal_prefix: str
    abandon_when: Callable[[LedgerProjection, tuple[Any, ...]], bool]
    abandon_status: str
    abandon_reason: str
    failure_contract: Literal["silent"] = "silent"


# ---------------------------------------------------------------------------
# Acceptance binding (§3.1: names one existing seam, never a new authority)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpressionAcceptanceBinding:
    """Route the audited proposal through the existing ExpressionPlan seam."""

    policy: ExpressionPlanBudgetPolicy
    dispatch: Literal["inline_pump_with_private_settlement", "generic_action_pump"]
    # Who signs the acceptance batch: the worker actor (quick pilot) or the
    # budget policy's actor (afterthought pilot).  Frozen per vertical.
    batch_actor: Literal["worker_actor", "policy_actor"] = "worker_actor"
    include_source_observation: bool = False


@dataclass(frozen=True, slots=True)
class AuditContextTimes:
    logical_time: datetime
    created_at: datetime


# ---------------------------------------------------------------------------
# VerticalSpec: the one frozen declaration per vertical
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerticalSpec:
    """Everything a bounded decision vertical declares; nothing it repeats.

    The composable steps deliberately do not force one fixed pipeline: a
    vertical with several maintenance draws may use the lifecycle and draw
    primitives directly (registry marks it accordingly).  The two installed
    interpreters cover the pilot shapes.
    """

    lane_id: str
    lifecycle: AnchoredTriggerLifecycle | DailyCheckLifecycle | InlineOnceLifecycle
    grammar_lane: str
    opportunity: Callable[..., object | None]
    draws: tuple[DrawStep, ...]
    model: BoundedModelStep | None
    compile: Callable[..., DecisionProposal]
    acceptance: ExpressionAcceptanceBinding
    audit_times: Callable[[Any, LedgerProjection], AuditContextTimes]
    random_source: str
    draw_actor: str
    worker_source: str
    downstream: Callable[..., Awaitable[None]] | None = None
    notes: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.lane_id or not self.grammar_lane:
            raise ValueError("vertical spec requires lane and grammar identities")
        if not self.random_source or not self.draw_actor or not self.worker_source:
            raise ValueError("vertical spec requires frozen actor/source strings")
        seen: set[str] = set()
        for step in self.draws:
            if step.step_id in seen:
                raise ValueError("vertical draw step ids must be unique")
            seen.add(step.step_id)


# ---------------------------------------------------------------------------
# Generic run results
# ---------------------------------------------------------------------------


class AnchoredRunResult(FrozenModel):
    """Drain outcome of an anchored vertical; field names match the pilots'
    hand-written results so hosts and logs keep working across the switch."""

    status: Literal[
        "idle",
        "opened",
        "owned_elsewhere",
        "held",
        "declined",
        "authorized",
        "budget_exhausted",
        "stale",
        "completed_existing",
    ]
    trigger_id: str | None = None
    proposal_id: str | None = None
    action_id: str | None = None
    reason_code: str | None = None
    # Vertical-defined reading of the recorded draws (for example
    # ``quick_continue:18``); replaces the pilot's mode/delay fields.
    interpretation: str | None = None


class InlineOnceRunResult(FrozenModel):
    """Run outcome of an inline-once vertical (field-compatible with the
    quick-reaction pilot's result so ``WorldRuntime.ingest`` logging holds)."""

    status: Literal[
        "reacted",
        "dispatch_incomplete",
        "held",
        "declined",
        "skipped",
        "duplicate",
        "abandoned_reply_delivered",
        "failed",
    ]
    reaction_id: str | None = None
    proposal_id: str | None = None
    action_id: str | None = None
    reason_code: str | None = None
    dispatch_status: str | None = None
    ledger_advanced: bool = False
    gate_ms: float | None = None
    total_ms: float | None = None


# ---------------------------------------------------------------------------
# Engine: anchored trigger lifecycle (A shape)
# ---------------------------------------------------------------------------


class AnchoredVerticalRuntime:
    """Recovery-safe interpreter of an anchored-lifecycle :class:`VerticalSpec`.

    Byte-compatible with the afterthought pilot: identical event identities,
    projection re-reads and commit boundaries.  The spec owns every semantic
    choice; this class owns only the ceremony.
    """

    def __init__(
        self,
        *,
        spec: VerticalSpec,
        ledger: LedgerPort,
        model: ChatCompletionModel,
        batch_issuer,
        owner_id: str,
        worker_actor: str,
    ) -> None:
        lifecycle = spec.lifecycle
        if not isinstance(lifecycle, AnchoredTriggerLifecycle):
            raise ValueError("anchored runtime requires an anchored lifecycle spec")
        if not owner_id or lifecycle.lease_seconds <= 0:
            raise ValueError("anchored vertical needs an owner and a positive lease")
        if spec.model is None:
            raise ValueError("anchored interpreter requires a bounded model step")
        self.ledger = ledger
        self._spec = spec
        self._lifecycle = lifecycle
        self._identity = lifecycle.identity
        self._ops = LedgerOps(ledger)
        self._model = model
        self._owner = owner_id
        self._actor = worker_actor
        self._random = RandomAuthority(ledger=ledger, source=spec.random_source)
        self._audits = ProposalAuditRecorder(ledger=ledger)
        self._grammar = production_proposal_grammar(spec.grammar_lane)
        self._recorder = ExpressionPlanAtomicRecorder(batch_issuer=batch_issuer)
        self._acceptance = spec.acceptance

    async def drain_one(self) -> AnchoredRunResult:
        projection = await self._ops.project()
        if projection.logical_time is None:
            return AnchoredRunResult(status="idle")
        opportunity = self._spec.opportunity(projection)
        if opportunity is None:
            return AnchoredRunResult(status="idle")
        trigger_id = self._lifecycle.trigger_id(self._ops.world_id, opportunity)
        process = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id),
            None,
        )
        if process is None:
            await self._open(
                opportunity=opportunity,
                trigger_id=trigger_id,
                cursor=self._ops.cursor(projection),
            )
            return AnchoredRunResult(status="opened", trigger_id=trigger_id)
        if process.state == "terminal":
            return AnchoredRunResult(status="completed_existing", trigger_id=trigger_id)
        active = await self._claim(
            process=process, opportunity=opportunity, projection=projection
        )
        if active is None:
            return AnchoredRunResult(status="owned_elsewhere", trigger_id=trigger_id)

        current = await self._ops.project()
        existing_audit = next(
            (
                item
                for item in current.proposal_audits
                if item.trigger_ref == opportunity.anchor_event_ref
                and item.proposal_id.startswith(
                    f"proposal:{self._identity.namespace}:"
                )
            ),
            None,
        )
        if existing_audit is not None:
            # Crash recovery: the model already spoke durably; only the
            # acceptance may still be missing.
            return await self._accept(
                process=active, opportunity=opportunity, audit=existing_audit
            )

        draws: dict[str, RandomDrawRecordedPayload] = {}
        interpretations: dict[str, object] = {}
        for step in self._spec.draws:
            plan = step.plan(
                DrawContext(
                    opportunity=opportunity,
                    projection=current,
                    logical_time=current.logical_time or opportunity.anchored_at,
                    previous=dict(draws),
                )
            )
            payload = await self._draw(step=step, plan=plan, opportunity=opportunity)
            draws[step.step_id] = payload
            if step.interpret is not None:
                interpretations[step.step_id] = step.interpret(
                    payload.selected_candidate_ref
                )
            if (
                step.halt_unless is not None
                and payload.selected_candidate_ref != step.halt_unless
            ):
                assert step.halt_outcome is not None
                await self._complete(
                    process=active, opportunity=opportunity, outcome=step.halt_outcome
                )
                return AnchoredRunResult(
                    status="held",
                    trigger_id=trigger_id,
                    reason_code=step.halt_reason,
                )

        model_context = ModelStepContext(
            opportunity=opportunity,
            projection=current,
            draws=dict(draws),
            interpretations=dict(interpretations),
        )
        interpretation_label = self._interpretation_label(interpretations)
        verdict, raw_response = await run_bounded_model_step(
            step=self._spec.model,
            model=self._model,
            context=model_context,
            log_label=self._spec.lane_id,
        )
        if verdict is None:
            await self._complete(
                process=active, opportunity=opportunity, outcome="declined"
            )
            return AnchoredRunResult(
                status="declined",
                trigger_id=trigger_id,
                interpretation=interpretation_label,
                reason_code=(
                    f"{self._spec.lane_id}.gate_declined"
                    if raw_response is not None
                    else f"{self._spec.lane_id}.gate_unavailable"
                ),
            )
        assert raw_response is not None

        head = await self._ops.project()
        proposal = self._spec.compile(
            opportunity=opportunity,
            evaluated_world_revision=head.world_revision,
            verdict=verdict,
            draws=dict(draws),
            interpretations=dict(interpretations),
        )
        try:
            self._grammar.validate(proposal)
        except ProductionProposalGrammarError:
            await self._complete(
                process=active, opportunity=opportunity, outcome="grammar-rejected"
            )
            return AnchoredRunResult(
                status="declined",
                trigger_id=trigger_id,
                interpretation=interpretation_label,
                reason_code=f"{self._spec.lane_id}.grammar_rejected",
            )
        result = single_call_deliberation_result(
            template=self._spec.model.audit,
            model=self._model,
            prompt_material=self._spec.model.prompt_material(model_context, proposal),
            proposal=proposal,
            raw_response=raw_response,
        )
        times = self._spec.audit_times(opportunity, head)
        context = ProposalAuditContext(
            world_id=self._ops.world_id,
            trigger_ref=opportunity.anchor_event_ref,
            logical_time=times.logical_time,
            created_at=times.created_at,
            actor=self._actor,
            source=self._spec.worker_source,
            trace_id=opportunity.trace_id,
            causation_id=opportunity.anchor_event_ref,
            correlation_id=opportunity.correlation_id,
            evaluated_world_revision=head.world_revision,
            expected_commit_world_revision=head.world_revision,
            expected_deliberation_revision=head.deliberation_revision,
        )
        try:
            await self._ops.call(self._audits.record, result, context)
        except (ConcurrencyConflict, IdempotencyConflict):
            return AnchoredRunResult(
                status="stale",
                trigger_id=trigger_id,
                interpretation=interpretation_label,
                reason_code=f"{self._spec.lane_id}.audit_race",
            )
        after_audit = await self._ops.project()
        audit = next(
            (
                item
                for item in after_audit.proposal_audits
                if item.proposal_id == proposal.proposal_id
            ),
            None,
        )
        if audit is None:
            raise RuntimeError(
                f"{self._spec.lane_id} deliberation produced no durable audit"
            )
        outcome = await self._accept(
            process=active, opportunity=opportunity, audit=audit
        )
        return outcome.model_copy(update={"interpretation": interpretation_label})

    @staticmethod
    def _interpretation_label(interpretations: Mapping[str, object]) -> str | None:
        if not interpretations:
            return None
        return ";".join(
            f"{step_id}={value}" for step_id, value in sorted(interpretations.items())
        )

    async def _draw(
        self, *, step: DrawStep, plan: DrawPlan, opportunity: AnchoredOpportunity
    ) -> RandomDrawRecordedPayload:
        projection = await self._ops.project()
        draw_kwargs = dict(
            attempt_id=plan.attempt_id,
            candidate_refs=plan.candidate_refs,
            candidate_weights=plan.candidate_weights,
            weight_policy_version=step.weight_policy_version,
            catalog_version=step.catalog_version,
            logical_time=projection.logical_time,
            seed_instant=opportunity.anchored_at,
            actor=self._spec.draw_actor,
            trace_id=opportunity.trace_id,
            correlation_id=opportunity.correlation_id,
        )
        return await self._ops.call(self._random.draw, **draw_kwargs)

    async def _accept(
        self, *, process: TriggerProcess, opportunity: AnchoredOpportunity, audit
    ) -> AnchoredRunResult:
        current = await self._ops.project()
        existing = next(
            (
                item
                for item in current.actions
                if item.intent_ref.startswith(audit.proposal_id + ":")
            ),
            None,
        )
        if existing is not None:
            await self._complete(
                process=process,
                opportunity=opportunity,
                outcome=f"authorized:{existing.action_id}",
            )
            return AnchoredRunResult(
                status="completed_existing",
                trigger_id=process.trigger_id,
                proposal_id=audit.proposal_id,
                action_id=existing.action_id,
            )
        account = next(
            (
                item
                for item in current.budget_accounts
                if item.account_id == self._acceptance.policy.account_id
            ),
            None,
        )
        if account is None:
            await self._complete(
                process=process,
                opportunity=opportunity,
                outcome="budget-exhausted:account-unavailable",
            )
            return AnchoredRunResult(
                status="budget_exhausted",
                trigger_id=process.trigger_id,
                proposal_id=audit.proposal_id,
                reason_code=f"{self._spec.lane_id}.budget_account_unavailable",
            )
        cursor = self._ops.cursor(current)
        projection_time = current.logical_time or opportunity.anchored_at
        try:
            material = derive_expression_plan_material(
                audit=audit,
                cursor=cursor,
                world_id=self._ops.world_id,
                policy=self._acceptance.policy,
                account=account,
                logical_time=projection_time,
                created_at=projection_time,
                trace_id=opportunity.trace_id,
                correlation_id=opportunity.correlation_id,
            )
        except ExpressionPlanAcceptanceError as exc:
            if exc.code in {
                "expression_plan_acceptance.budget_unavailable",
                "expression_plan_acceptance.budget_account_unavailable",
            }:
                await self._complete(
                    process=process, opportunity=opportunity, outcome="budget-exhausted"
                )
                return AnchoredRunResult(
                    status="budget_exhausted",
                    trigger_id=process.trigger_id,
                    proposal_id=audit.proposal_id,
                    reason_code=exc.code,
                )
            if exc.code == "expression_plan_acceptance.stale_revision":
                return AnchoredRunResult(
                    status="stale",
                    trigger_id=process.trigger_id,
                    proposal_id=audit.proposal_id,
                    reason_code=exc.code,
                )
            raise
        handle = self._recorder.prepare_batch(
            acceptance_id=self._identity.acceptance_id(audit.proposal_id),
            material=material,
            actor=(
                self._acceptance.policy.actor
                if self._acceptance.batch_actor == "policy_actor"
                else self._actor
            ),
            source=self._spec.worker_source,
        )
        try:
            await self._ops.commit_accepted(handle, cursor=cursor)
        except (ConcurrencyConflict, IdempotencyConflict):
            raced = await self._ops.project()
            existing = next(
                (
                    item
                    for item in raced.actions
                    if item.intent_ref.startswith(audit.proposal_id + ":")
                ),
                None,
            )
            if existing is None:
                return AnchoredRunResult(
                    status="stale",
                    trigger_id=process.trigger_id,
                    proposal_id=audit.proposal_id,
                    reason_code=f"{self._spec.lane_id}.acceptance_race",
                )
        action_id = (
            existing.action_id if existing is not None else material.beats[0].action.action_id
        )
        await self._complete(
            process=process, opportunity=opportunity, outcome=f"authorized:{action_id}"
        )
        return AnchoredRunResult(
            status="authorized",
            trigger_id=process.trigger_id,
            proposal_id=audit.proposal_id,
            action_id=action_id,
        )

    async def _open(
        self,
        *,
        opportunity: AnchoredOpportunity,
        trigger_id: str,
        cursor: ProjectionCursor,
    ) -> None:
        process = TriggerProcess(
            trigger_id=trigger_id,
            trigger_ref=self._lifecycle.trigger_ref(opportunity),
            process_kind=self._lifecycle.process_kind,
            source_evidence_ref=opportunity.anchor_event_ref,
            state="open",
        )
        payload = {"process": process.model_dump(mode="json")}
        await self._commit_event(
            event_type="TriggerProcessOpened",
            payload=payload,
            event_id=self._identity.opened_event_id(payload),
            idempotency_key=domain_idempotency_key(
                event_type="TriggerProcessOpened",
                world_id=self._ops.world_id,
                payload=payload,
            ),
            opportunity=opportunity,
            cursor=cursor,
            commit_id=self._identity.opened_commit_id(payload),
        )

    async def _claim(
        self,
        *,
        process: TriggerProcess,
        opportunity: AnchoredOpportunity,
        projection: LedgerProjection,
    ) -> TriggerProcess | None:
        at = projection.logical_time or opportunity.anchored_at
        if process.state == "claimed" and process.claim_lease is not None:
            if (
                process.claim_lease.owner_id == self._owner
                and at <= process.claim_lease.expires_at
            ):
                return process
            if at < process.claim_lease.expires_at:
                return None
        attempt_id = self._identity.worker_attempt_id(
            process.trigger_id, len(process.attempt_ids) + 1
        )
        claimed = process.model_copy(
            update={
                "state": "claimed",
                "claim_lease": ClaimLease(
                    owner_id=self._owner,
                    attempt_id=attempt_id,
                    acquired_at=at,
                    expires_at=at + timedelta(seconds=self._lifecycle.lease_seconds),
                ),
                "attempt_ids": (*process.attempt_ids, attempt_id),
            }
        )
        event_type = (
            "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        )
        payload = {"process": claimed.model_dump(mode="json")}
        try:
            await self._commit_event(
                event_type=event_type,
                payload=payload,
                event_id=self._identity.claim_event_id(payload),
                idempotency_key=(
                    domain_idempotency_key(
                        event_type=event_type,
                        world_id=self._ops.world_id,
                        payload=payload,
                    )
                    or self._identity.claim_idempotency_fallback(payload)
                ),
                opportunity=opportunity,
                cursor=self._ops.cursor(projection),
                commit_id=self._identity.claim_commit_id(payload),
            )
        except ConcurrencyConflict:
            return None
        return claimed

    async def _complete(
        self,
        *,
        process: TriggerProcess,
        opportunity: AnchoredOpportunity,
        outcome: str,
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("anchored completion requires a claimed process")
        projection = await self._ops.project()
        current = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == process.trigger_id
            ),
            None,
        )
        if current is not None and current.state == "terminal":
            return
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": (projection.logical_time or opportunity.anchored_at).isoformat(),
            "runtime_outcome_ref": self._identity.outcome_ref(outcome),
        }
        await self._commit_event(
            event_type="TriggerProcessCompleted",
            payload=payload,
            event_id=self._identity.completed_event_id(payload),
            idempotency_key=self._identity.completed_idempotency(
                self._ops.world_id, payload
            ),
            opportunity=opportunity,
            cursor=self._ops.cursor(projection),
            commit_id=self._identity.completed_commit_id(payload),
        )

    async def _commit_event(
        self,
        *,
        event_type: str,
        payload: dict[str, object],
        event_id: str,
        idempotency_key: str | None,
        opportunity: AnchoredOpportunity,
        cursor: ProjectionCursor,
        commit_id: str,
    ) -> None:
        if idempotency_key is None:
            raise ValueError(f"{self._spec.lane_id} lifecycle event lacks identity")
        projection_time = (
            await self._ops.project()
        ).logical_time or opportunity.anchored_at
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ops.world_id,
            event_type=event_type,
            logical_time=projection_time,
            created_at=projection_time,
            actor=self._owner,
            source=self._spec.worker_source,
            trace_id=opportunity.trace_id,
            causation_id=opportunity.anchor_event_ref,
            correlation_id=opportunity.correlation_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )
        await self._ops.commit_at_cursor(
            (event,), cursor=cursor, commit_id=commit_id
        )


# ---------------------------------------------------------------------------
# Engine: inline-once lifecycle (C shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InlineSkip:
    """An eligibility predicate's refusal, carrying the frozen reason code."""

    reason: str


class InlineOnceVerticalWorker:
    """One inline, bounded, effect-once attempt per Observation.

    Byte-compatible interpreter of the quick-reaction pilot's ceremony: audit
    prefix dedupe, one recorded draw, one bounded gate call, audit,
    ExpressionPlan acceptance, inline pump dispatch and private settlement.
    Every failure is silent: an inline vertical is an opportunity, never a
    debt.
    """

    def __init__(
        self,
        *,
        spec: VerticalSpec,
        ledger: LedgerPort,
        model: ChatCompletionModel,
        expression_recorder: ExpressionPlanAtomicRecorder,
        executor: ActionExecutor | None,
        pump_owner: str,
        worker_actor: str,
    ) -> None:
        lifecycle = spec.lifecycle
        if not isinstance(lifecycle, InlineOnceLifecycle):
            raise ValueError("inline worker requires an inline-once lifecycle spec")
        if not pump_owner:
            raise ValueError("inline vertical requires a pump owner id")
        if spec.model is None:
            raise ValueError("inline interpreter requires a bounded model step")
        if len(spec.draws) != 1:
            raise ValueError("inline interpreter runs exactly one recorded draw")
        self._spec = spec
        self._lifecycle = lifecycle
        self._ops = LedgerOps(ledger)
        self._model = model
        self._expression_recorder = expression_recorder
        self._executor = executor
        self._pump_owner = pump_owner
        self._actor = worker_actor
        self._random = RandomAuthority(ledger=ledger, source=spec.random_source)
        self._audits = ProposalAuditRecorder(ledger=ledger)
        self._grammar = production_proposal_grammar(spec.grammar_lane)
        self._settlement = SettlementPlanner(world_id=ledger.world_id)

    @property
    def ledger(self) -> LedgerPort:
        return self._ops.ledger

    async def run_observation(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        source_world_revision: int,
    ) -> InlineOnceRunResult:
        """Attempt one inline decision; every failure is silent by contract."""

        started = time.perf_counter()
        try:
            return await self._run(
                observation=observation,
                observation_event=observation_event,
                source_world_revision=source_world_revision,
                started=started,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the lane must never fail a turn
            _LOG.warning(
                "%s lane gave up trace=%s error=%s: %s",
                self._spec.lane_id,
                observation.trace_id,
                type(exc).__name__,
                str(exc)[:240],
            )
            return InlineOnceRunResult(
                status="failed",
                reason_code=f"{self._spec.lane_id}.{type(exc).__name__}",
                # Conservative: a failure mid-pipeline may have committed.
                ledger_advanced=True,
                total_ms=(time.perf_counter() - started) * 1000,
            )

    async def _run(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        source_world_revision: int,
        started: float,
    ) -> InlineOnceRunResult:
        def done(
            status: str,
            *,
            reason: str | None = None,
            reaction_id: str | None = None,
            proposal_id: str | None = None,
            action_id: str | None = None,
            dispatch_status: str | None = None,
            ledger_advanced: bool = False,
            gate_ms: float | None = None,
        ) -> InlineOnceRunResult:
            return InlineOnceRunResult(
                status=status,  # type: ignore[arg-type]
                reason_code=reason,
                reaction_id=reaction_id,
                proposal_id=proposal_id,
                action_id=action_id,
                dispatch_status=dispatch_status,
                ledger_advanced=ledger_advanced,
                gate_ms=gate_ms,
                total_ms=(time.perf_counter() - started) * 1000,
            )

        # Eligibility order (including the installed-executor check) is owned
        # by the spec's opportunity closure so skip reasons stay stable.
        opportunity = self._spec.opportunity(
            observation=observation, observation_event=observation_event
        )
        if isinstance(opportunity, InlineSkip):
            return done("skipped", reason=opportunity.reason)
        assert opportunity is not None

        projection = await self._ops.project()
        logical_time = projection.logical_time
        if logical_time is None:
            return done("skipped", reason=f"{self._spec.lane_id}.no_logical_time")

        trigger_audits = tuple(
            audit
            for audit in projection.proposal_audits
            if audit.trigger_ref == observation_event.event_id
        )
        if any(
            audit.proposal_id.startswith(self._lifecycle.proposal_prefix)
            for audit in trigger_audits
        ):
            return done("duplicate", reason=f"{self._spec.lane_id}.already_attempted")
        if self._lifecycle.abandon_when(projection, trigger_audits):
            return done(
                self._lifecycle.abandon_status,  # type: ignore[arg-type]
                reason=self._lifecycle.abandon_reason,
            )

        step = self._spec.draws[0]
        plan = step.plan(
            DrawContext(
                opportunity=opportunity,
                projection=projection,
                logical_time=logical_time,
                previous={},
            )
        )
        draw_kwargs = dict(
            attempt_id=plan.attempt_id,
            candidate_refs=plan.candidate_refs,
            candidate_weights=plan.candidate_weights,
            weight_policy_version=step.weight_policy_version,
            catalog_version=step.catalog_version,
            logical_time=logical_time,
            seed_instant=observation_event.logical_time,
            actor=self._spec.draw_actor,
            trace_id=observation.trace_id,
            correlation_id=observation.correlation_id,
        )
        draw = await self._ops.call(self._random.draw, **draw_kwargs)
        if (
            step.halt_unless is not None
            and draw.selected_candidate_ref != step.halt_unless
        ):
            return done("held", reason=step.halt_reason, ledger_advanced=True)

        model_context = ModelStepContext(
            opportunity=opportunity,
            projection=projection,
            draws={step.step_id: draw},
            interpretations={},
        )
        gate_started = time.perf_counter()
        verdict, raw_response = await run_bounded_model_step(
            step=self._spec.model,
            model=self._model,
            context=model_context,
            log_label=self._spec.lane_id,
        )
        gate_ms = (time.perf_counter() - gate_started) * 1000
        if verdict is None:
            return done(
                "declined",
                reason=(
                    f"{self._spec.lane_id}.gate_declined"
                    if raw_response is not None
                    else f"{self._spec.lane_id}.gate_unavailable"
                ),
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        assert raw_response is not None

        head = await self._ops.project()
        cursor = self._ops.cursor(head)
        proposal = self._spec.compile(
            opportunity=opportunity,
            observation=observation,
            observation_event=observation_event,
            source_world_revision=source_world_revision,
            evaluated_world_revision=cursor.world_revision,
            verdict=verdict,
            recorded_draw_ref=draw.draw_id,
        )
        try:
            self._grammar.validate(proposal)
        except ProductionProposalGrammarError:
            return done(
                "failed",
                reason=f"{self._spec.lane_id}.grammar_rejected",
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        result = single_call_deliberation_result(
            template=self._spec.model.audit,
            model=self._model,
            prompt_material=self._spec.model.prompt_material(model_context, proposal),
            proposal=proposal,
            raw_response=raw_response,
        )
        times = self._spec.audit_times(
            _InlineAuditAnchor(observation=observation, observation_event=observation_event),
            head,
        )
        context = ProposalAuditContext(
            world_id=self._ops.world_id,
            trigger_ref=observation_event.event_id,
            logical_time=times.logical_time,
            created_at=times.created_at,
            actor=self._actor,
            source=self._spec.worker_source,
            trace_id=observation.trace_id,
            causation_id=observation_event.event_id,
            correlation_id=observation.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
        try:
            await self._ops.call(self._audits.record, result, context)
        except (ConcurrencyConflict, IdempotencyConflict):
            return done(
                "failed",
                reason=f"{self._spec.lane_id}.audit_race",
                ledger_advanced=True,
                gate_ms=gate_ms,
            )

        after_audit = await self._ops.project()
        audit = next(
            (
                item
                for item in after_audit.proposal_audits
                if item.proposal_id == proposal.proposal_id
            ),
            None,
        )
        account = next(
            (
                item
                for item in after_audit.budget_accounts
                if item.account_id == self._spec.acceptance.policy.account_id
            ),
            None,
        )
        if audit is None or account is None:
            return done(
                "failed",
                reason=f"{self._spec.lane_id}.acceptance_material_unavailable",
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        acceptance_cursor = self._ops.cursor(after_audit)
        try:
            material = derive_expression_plan_material(
                audit=audit,
                cursor=acceptance_cursor,
                world_id=self._ops.world_id,
                policy=self._spec.acceptance.policy,
                account=account,
                logical_time=after_audit.logical_time or observation.logical_time,
                created_at=observation.created_at,
                trace_id=observation.trace_id,
                correlation_id=observation.correlation_id,
                source_observation=(
                    observation
                    if self._spec.acceptance.include_source_observation
                    else None
                ),
            )
        except ExpressionPlanAcceptanceError as exc:
            return done("failed", reason=exc.code, ledger_advanced=True, gate_ms=gate_ms)
        batch = self._expression_recorder.prepare_batch(
            acceptance_id=self._lifecycle.identity.acceptance_id(proposal.proposal_id),
            material=material,
            actor=self._actor,
            source=self._spec.worker_source,
        )
        try:
            await self._ops.commit_accepted(batch, cursor=material.cursor)
        except (ConcurrencyConflict, IdempotencyConflict):
            return done(
                "failed",
                reason=f"{self._spec.lane_id}.acceptance_race",
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        action_id = material.beats[0].action.action_id

        pump = ActionPump(
            ledger=self._ops.ledger,
            executor=self._executor,
            settle=self._settle,
            owner_id=self._pump_owner,
            source=self._lifecycle.identity.pump_source(),
        )
        reaction_id = self._selected_option_id(verdict)
        try:
            dispatched = await pump.drain_action(action_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - dispatch failure stays silent
            _LOG.warning(
                "%s dispatch gave up trace=%s action=%s error=%s: %s",
                self._spec.lane_id,
                observation.trace_id,
                action_id,
                type(exc).__name__,
                str(exc)[:240],
            )
            return done(
                "dispatch_incomplete",
                reason=f"{self._spec.lane_id}.dispatch_failed",
                reaction_id=reaction_id,
                proposal_id=proposal.proposal_id,
                action_id=action_id,
                ledger_advanced=True,
                gate_ms=gate_ms,
            )
        settled = dispatched.status == "settled"
        return done(
            "reacted" if settled else "dispatch_incomplete",
            reason=None if settled else f"{self._spec.lane_id}.pump_{dispatched.status}",
            reaction_id=reaction_id,
            proposal_id=proposal.proposal_id,
            action_id=action_id,
            dispatch_status=dispatched.status,
            ledger_advanced=True,
            gate_ms=gate_ms,
        )

    @staticmethod
    def _selected_option_id(verdict: object) -> str | None:
        if isinstance(verdict, str):
            return verdict
        return getattr(verdict, "option_id", None)

    async def _settle(self, result) -> None:
        """Settle one dispatch receipt without re-entering the locked runtime.

        Commit identities match ``WorldRuntime.settle`` exactly, so if this
        worker crashes mid-settlement the generic recovery path converges on
        the same events instead of duplicating the receipt.
        """

        trigger_id = f"trigger:settlement:{result.source}:{result.source_event_id}"
        before = await self._ops.project()
        await self._ops.commit(
            list(self._settlement.recording_events(result, trigger_id=trigger_id)),
            world_revision=before.world_revision,
            deliberation_revision=before.deliberation_revision,
            commit_id=f"commit:{trigger_id}:inbox",
        )
        after_inbox = await self._ops.project()
        plan = self._settlement.plan(result, trigger_id=trigger_id, projection=after_inbox)
        await self._ops.commit(
            list(plan.events),
            world_revision=after_inbox.world_revision,
            deliberation_revision=after_inbox.deliberation_revision,
            commit_id=f"commit:{trigger_id}:settlement",
        )


@dataclass(frozen=True, slots=True)
class _InlineAuditAnchor:
    """Adapter handing inline audit-times closures the observation pair."""

    observation: Observation
    observation_event: WorldEvent


# ---------------------------------------------------------------------------
# Engine: daily-check lifecycle primitives (B shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WakeAuthority:
    """One exactness-validated ClockAdvanced wake (§2.2 item 6)."""

    event_id: str
    logical_time: datetime
    world_revision: int
    payload_hash: str


class DailyCheckEngine:
    """Primitives of the clock-check shape: wake validation, check identity,
    durable check recording and crash-recovery decoding.

    No production vertical is migrated onto this engine yet (P0 scope); the
    crash matrix drives it through a synthetic vertical.  The identities are
    parameterized the same way the hand-written B wells derive theirs, so a
    future migration only freezes its namespace strings here.
    """

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        lifecycle: DailyCheckLifecycle,
        actor: str,
        source: str,
    ) -> None:
        if not actor or not source:
            raise ValueError("daily check engine requires actor and source")
        self._ledger = ledger
        self._lifecycle = lifecycle
        self._actor = actor
        self._source = source

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    def validate_wake(
        self, *, projection: LedgerProjection, wake_event_ref: str
    ) -> WakeAuthority | None:
        """The four-way exactness check copied by every clock vertical."""

        logical_time = projection.logical_time
        wake = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == wake_event_ref
            ),
            None,
        )
        transition = next(
            (
                item
                for item in projection.clock_transition_history
                if item.clock_event_ref == wake_event_ref
            ),
            None,
        )
        if (
            logical_time is None
            or wake is None
            or wake.event_type != "ClockAdvanced"
            or transition is None
            or transition.payload_hash != wake.payload_hash
            or transition.computed_world_revision != wake.world_revision
        ):
            return None
        return WakeAuthority(
            event_id=wake.event_id,
            logical_time=wake.logical_time,
            world_revision=wake.world_revision,
            payload_hash=wake.payload_hash,
        )

    def check_event_id(self, identity_material: dict[str, object]) -> str:
        return f"event:{self._lifecycle.namespace}:check:" + digest(identity_material)

    def read_check(self, check_event_id: str) -> WorldEvent | None:
        located = self._ledger.lookup_event_commit(check_event_id)
        if located is None or located[0].event_type != "ProposalRecorded":
            return None
        if located[0].payload().get("proposal_kind") != self._lifecycle.proposal_kind:
            return None
        return located[0]

    @staticmethod
    def check_decision(check_event: WorldEvent) -> tuple[str, str | None]:
        """Decode ``(decision, candidate_token)`` for crash recovery."""

        payload = check_event.payload()
        decision = payload.get("decision")
        token = payload.get("candidate_token")
        return (
            decision if isinstance(decision, str) else "",
            token if isinstance(token, str) else None,
        )

    def record_check(
        self,
        *,
        check_event_id: str,
        proposal_id: str,
        decision: Literal["nothing", "no_op", "selected"],
        identity_fields: dict[str, object],
        wake: WakeAuthority,
        draw_event_ref: str,
        candidate_token: str | None,
        raw_output: str,
        model_id: str,
        trace_id: str,
        correlation_id: str,
        extra_fields: dict[str, object] | None = None,
    ) -> WorldEvent:
        """Record one durable check decision at the current cursor."""

        projection = self._ledger.project()
        payload: dict[str, object] = {
            "proposal_id": proposal_id,
            "proposal_kind": self._lifecycle.proposal_kind,
            "decision": decision,
            **identity_fields,
            "trigger_id": wake.event_id,
            "evaluated_world_revision": projection.world_revision,
            "wake_event_ref": wake.event_id,
            "wake_event_payload_hash": wake.payload_hash,
            "draw_event_ref": draw_event_ref,
            "candidate_token": candidate_token,
            "model": model_id,
            "raw_output_hash": "sha256:"
            + hashlib.sha256(raw_output.encode()).hexdigest(),
            **(extra_fields or {}),
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=check_event_id,
            event_type="ProposalRecorded",
            world_id=self._ledger.world_id,
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source=self._source,
            trace_id=trace_id,
            causation_id=draw_event_ref,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ProposalRecorded",
                world_id=self._ledger.world_id,
                payload=payload,
            )
            or f"{self._lifecycle.namespace}-check:" + digest(check_event_id),
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=LedgerOps.cursor(projection),
            commit_id=f"commit:{self._lifecycle.namespace}:check:" + digest(check_event_id),
        )
        return event

    @staticmethod
    def parse_select_no_op(raw: object, *, offered_token: str) -> tuple[str, str] | None:
        """The strict select/no_op JSON block copied five times (§2.2 item 8).

        Returns ``(decision, raw_text)`` or ``None`` for any contract breach;
        the caller decides whether a breach consumes the slot (it must not).
        """

        if not isinstance(raw, str) or len(raw.encode()) > 32_768:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict) or set(parsed) not in (
            {"decision"},
            {"decision", "candidate_token"},
        ):
            return None
        decision = parsed.get("decision")
        if decision == "no_op":
            if "candidate_token" in parsed:
                return None
            return "no_op", raw
        if decision != "select" or parsed.get("candidate_token") != offered_token:
            return None
        return "select", raw


__all__ = [
    "AnchoredIdentityTemplates",
    "AnchoredOpportunity",
    "AnchoredRunResult",
    "AnchoredTriggerLifecycle",
    "AnchoredVerticalRuntime",
    "AuditContextTimes",
    "BoundedModelStep",
    "BoundedModelUnavailable",
    "DailyCheckEngine",
    "DailyCheckLifecycle",
    "DecisionProfileLike",
    "DrawContext",
    "DrawPlan",
    "DrawStep",
    "ExpressionAcceptanceBinding",
    "InlineIdentityTemplates",
    "InlineOnceLifecycle",
    "InlineOnceRunResult",
    "InlineOnceVerticalWorker",
    "InlineSkip",
    "LedgerOps",
    "ModelStepContext",
    "SingleCallAuditTemplate",
    "VerticalSpec",
    "WakeAuthority",
    "canonical_json",
    "digest",
    "run_bounded_model_step",
    "single_call_deliberation_result",
]
