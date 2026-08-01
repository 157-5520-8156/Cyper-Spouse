"""Shared-InnerSeed coordination for one staged expression episode.

This module owns timing and disposition only.  It cannot write World state or
send a message: every candidate still crosses the injected Proposal audit /
typed ExpressionPlan acceptance capability, and cancellation crosses the
existing Action lifecycle capability.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
import logging
from threading import Lock
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .interactive_turn_budget import InteractiveTurnBudget
from .proposal_envelope import DecisionProposal, ProposalInput, validate_proposal_envelope
from .schema_core import FrozenModel
from .schemas import Observation, ProjectionCursor


EpisodePhase = Literal["provisional", "full"]
EpisodeDisposition = Literal[
    "complete_without_more",
    "append",
    "cancel_pending",
    "supersede_pending",
]
ExternalActionState = Literal[
    "authorized",
    "scheduled",
    "claimed",
    "dispatch_started",
    "provider_accepted",
    "delivered",
    "failed",
    "cancelled",
    "expired",
    "unknown",
    "user_interjected",
]

_NON_CANCELLABLE_STATES = frozenset(
    {"dispatch_started", "provider_accepted", "delivered"}
)
_LOG = logging.getLogger(__name__)


def validate_provisional_proposal(proposal: ProposalInput | dict[str, object]) -> str:
    """Return its sole semantic text or fail before audit/Acceptance."""

    if isinstance(proposal, dict):
        proposal = validate_proposal_envelope(proposal)
    if not isinstance(proposal, DecisionProposal):
        raise ValueError("provisional requires a typed DecisionProposal")
    if proposal.timing_choice != "now" or len(proposal.action_intents) != 1:
        raise ValueError("provisional requires one immediate Action intent")
    intent = proposal.action_intents[0]
    if intent.kind != "reply" or intent.layer != "external_action":
        raise ValueError("provisional requires one text reply intent")
    if len(proposal.proposed_changes) != 1:
        raise ValueError("provisional requires one expression transition")
    change = proposal.proposed_changes[0]
    if change.kind != "expression_plan_transition" or change.transition != "accept":
        raise ValueError("provisional requires one accepted expression transition")
    payload = change.payload.value()
    beats = payload.get("beat_drafts")
    if not isinstance(beats, list) or len(beats) != 1 or not isinstance(beats[0], dict):
        raise ValueError("provisional requires exactly one beat")
    beat = beats[0]
    text = beat.get("inline_text")
    if (
        not isinstance(text, str)
        or not text.strip()
        or beat.get("content_type") != "text/plain"
    ):
        raise ValueError("provisional text is empty or non-text")
    return text.strip()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class EpisodePolicy(FrozenModel):
    """Operational mode; production defaults to shadow, never implicit send."""

    mode: Literal["off", "shadow", "on", "stream"] = "shadow"
    provisional_target_seconds: float = Field(default=3.0, gt=0.0, le=5.5)
    max_provider_slots: Literal[2] = 2


class ExpressionEpisodeDiagnostics:
    """Process-local aggregate shadow evidence; never stores candidate text."""

    def __init__(
        self, *, mode: Literal["off", "shadow", "on", "stream"] = "shadow"
    ) -> None:
        self._mode = mode
        self._lock = Lock()
        self._counts: dict[str, int] = {
            "turns": 0,
            "candidate_valid": 0,
            "candidate_rejected": 0,
            "full_first": 0,
            "provisional_first": 0,
            "would_send": 0,
            "would_append": 0,
            "would_stop": 0,
            "slot_calls": 0,
            "grounding_rejected": 0,
            "placeholder_rejected": 0,
            "other_rejected": 0,
        }
        self._candidate_ms: list[float] = []
        self._full_ms: list[float] = []

    @property
    def mode(self) -> Literal["off", "shadow", "on", "stream"]:
        return self._mode

    def record(
        self,
        *,
        candidate_ms: float | None,
        valid: bool,
        winner: Literal["full", "provisional"],
        would_send: bool,
        would_append: bool = False,
        slot_calls: int,
        rejection_kind: Literal["grounding", "placeholder", "other"] | None = None,
    ) -> None:
        with self._lock:
            self._counts["turns"] += 1
            self._counts["candidate_valid" if valid else "candidate_rejected"] += 1
            self._counts[f"{winner}_first"] += 1
            self._counts["would_send" if would_send else "would_stop"] += 1
            self._counts["would_append"] += int(would_append)
            self._counts["slot_calls"] += slot_calls
            if rejection_kind is not None:
                self._counts[f"{rejection_kind}_rejected"] += 1
            if candidate_ms is not None:
                self._candidate_ms.append(max(0.0, candidate_ms))
                if len(self._candidate_ms) > 2_048:
                    del self._candidate_ms[: len(self._candidate_ms) - 2_048]

    def record_full(self, full_ms: float) -> None:
        with self._lock:
            self._full_ms.append(max(0.0, full_ms))
            if len(self._full_ms) > 2_048:
                del self._full_ms[: len(self._full_ms) - 2_048]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            counts = dict(self._counts)
            values = sorted(self._candidate_ms)
            full_values = sorted(self._full_ms)

        def percentile(source: list[float], fraction: float) -> float | None:
            if not source:
                return None
            index = min(len(source) - 1, max(0, int((len(source) - 1) * fraction)))
            return round(source[index], 3)

        return {
            "mode": self._mode,
            **counts,
            "candidate_ms_p50": percentile(values, 0.50),
            "candidate_ms_p95": percentile(values, 0.95),
            "candidate_ms_max": round(values[-1], 3) if values else None,
            "full_ms_p50": percentile(full_values, 0.50),
            "full_ms_p95": percentile(full_values, 0.95),
            "full_ms_max": round(full_values[-1], 3) if full_values else None,
        }


class InnerSeed(FrozenModel):
    """Pinned read-only context identity shared by both authors.

    The capsule remains the accepted-state authority.  This value carries only
    its identity and source bindings; it is not a second mutable state store.
    """

    seed_id: str = Field(min_length=1)
    capsule_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_ref: str = Field(min_length=1)
    observation_event_ref: str = Field(min_length=1)
    cursor: ProjectionCursor
    accepted_source_bindings: tuple[str, ...] = Field(min_length=1, max_length=256)
    advisory_source_bindings: tuple[str, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def bindings_are_unique(self) -> "InnerSeed":
        bindings = (*self.accepted_source_bindings, *self.advisory_source_bindings)
        if len(bindings) != len(set(bindings)):
            raise ValueError("InnerSeed source bindings must be unique")
        return self


class EpisodeObservation(FrozenModel):
    observation: Observation
    observation_event_ref: str = Field(min_length=1)
    observation_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cursor: ProjectionCursor
    inner_seed: InnerSeed

    @model_validator(mode="after")
    def seed_is_pinned_to_observation(self) -> "EpisodeObservation":
        if (
            self.inner_seed.observation_ref != self.observation.observation_id
            or self.inner_seed.observation_event_ref != self.observation_event_ref
            or self.inner_seed.cursor != self.cursor
        ):
            raise ValueError("Expression Episode InnerSeed lineage is not pinned")
        return self

    @property
    def episode_id(self) -> str:
        return "expression-episode:" + _digest(
            {
                "world_id": self.observation.world_id,
                "observation_ref": self.observation.observation_id,
                "observation_event_ref": self.observation_event_ref,
                "cursor": self.cursor.model_dump(mode="json"),
                "seed_id": self.inner_seed.seed_id,
            }
        )


class EpisodeCandidate(FrozenModel):
    """Audited expression material awaiting the normal acceptance capability."""

    phase: EpisodePhase
    seed_id: str = Field(min_length=1)
    observation_ref: str = Field(min_length=1)
    observation_event_ref: str = Field(min_length=1)
    cursor: ProjectionCursor
    text: str = Field(min_length=1, max_length=8_000)
    proposal_ref: str = Field(min_length=1)
    audit_ref: str = Field(min_length=1)
    source_bindings: tuple[str, ...] = Field(max_length=320)
    beat_count: int = Field(default=1, ge=1, le=32)
    modality: Literal["text", "reaction", "sticker", "typing"] = "text"
    current_turn_advisory_claimed_as_fact: bool = False


class FullCognitionResult(FrozenModel):
    disposition: EpisodeDisposition
    candidate: EpisodeCandidate | None = None
    replacement_plan_ref: str | None = None

    @model_validator(mode="after")
    def replacement_shape_is_closed(self) -> "FullCognitionResult":
        if self.disposition == "append" and self.candidate is None:
            raise ValueError("append requires a full expression candidate")
        return self


class AuthorizationResult(FrozenModel):
    plan_id: str = Field(min_length=1)
    action_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    delivered_refs: tuple[str, ...] = Field(default=(), max_length=32)


class EpisodeExternalResult(FrozenModel):
    action_id: str = Field(min_length=1)
    observed_state: ExternalActionState
    receipt_ref: str | None = None


class EpisodeOutcome(FrozenModel):
    episode_id: str = Field(min_length=1)
    status: Literal[
        "full_authorized",
        "provisional_authorized",
        "shadow_full_authorized",
        "observed_only",
        "failed_safe",
    ]
    winner: Literal["provisional", "full", "none"]
    authorized_action_ids: tuple[str, ...] = ()
    plan_id: str | None = None
    full_pending: bool = False
    shadow_provisional_ref: str | None = None
    rejections: tuple[str, ...] = ()
    first_semantic_action_ms: float | None = Field(default=None, ge=0.0)
    provider_slots_started: int = Field(ge=1, le=2)


class SettlementOutcome(FrozenModel):
    episode_id: str = Field(min_length=1)
    disposition: EpisodeDisposition
    authorized_action_ids: tuple[str, ...] = ()
    cancelled_action_ids: tuple[str, ...] = ()
    delivered_refs: tuple[str, ...] = ()
    pending_cancellation: bool = False
    fail_closed_reason: str | None = None


class EpisodeReplaySnapshot(FrozenModel):
    """Projection-derived replay input; never causes a live provider call."""

    outcome: EpisodeOutcome
    authorization: AuthorizationResult | None = None
    full_result: FullCognitionResult | None = None

    @model_validator(mode="after")
    def replay_is_closed(self) -> "EpisodeReplaySnapshot":
        if self.outcome.authorized_action_ids and self.authorization is None:
            raise ValueError("authorized replay requires acceptance lineage")
        if self.outcome.full_pending and self.full_result is None:
            raise ValueError("pending replay requires a recorded full model result")
        return self


class ProvisionalAuthor(Protocol):
    def __call__(
        self,
        seed: InnerSeed,
        observation: EpisodeObservation,
        budget: InteractiveTurnBudget,
    ) -> Awaitable[EpisodeCandidate]: ...


class FullCognition(Protocol):
    def __call__(
        self,
        seed: InnerSeed,
        observation: EpisodeObservation,
        budget: InteractiveTurnBudget,
    ) -> Awaitable[FullCognitionResult]: ...


class CandidateAuthorizer(Protocol):
    def authorize(
        self, candidate: EpisodeCandidate, *, budget: InteractiveTurnBudget
    ) -> Awaitable[AuthorizationResult]: ...


CancelPending = Callable[[str], Awaitable[bool]]
SupersedePending = Callable[
    [tuple[str, ...], str, InteractiveTurnBudget], Awaitable[AuthorizationResult]
]
ReplayLookup = Callable[[str], Awaitable[EpisodeReplaySnapshot | None]]


class ExpressionEpisode:
    """Coordinate one provisional/full race behind a two-method seam."""

    def __init__(
        self,
        *,
        provisional_author: ProvisionalAuthor,
        full_cognition: FullCognition,
        authorizer: CandidateAuthorizer,
        cancel_pending: Callable[..., Awaitable[bool]] | None = None,
        supersede_pending: SupersedePending | None = None,
        replay_lookup: ReplayLookup | None = None,
        policy: EpisodePolicy | None = None,
    ) -> None:
        self._provisional_author = provisional_author
        self._full_cognition = full_cognition
        self._authorizer = authorizer
        self._cancel_pending = cancel_pending
        self._supersede_pending = supersede_pending
        self._replay_lookup = replay_lookup
        self._policy = policy or EpisodePolicy()
        self._lock = asyncio.Lock()
        self._outcomes: dict[str, EpisodeOutcome] = {}
        self._full_tasks: dict[str, asyncio.Task[FullCognitionResult]] = {}
        self._provisional_tasks: dict[str, asyncio.Task[EpisodeCandidate]] = {}
        self._observations: dict[str, EpisodeObservation] = {}
        self._budgets: dict[str, InteractiveTurnBudget] = {}
        self._authorizations: dict[str, AuthorizationResult] = {}
        self._outcome_futures: dict[str, asyncio.Future[EpisodeOutcome]] = {}

    async def respond(
        self, observation: EpisodeObservation, budget: InteractiveTurnBudget
    ) -> EpisodeOutcome:
        """Authorize the first valid semantic expression under one deadline."""

        if type(observation) is not EpisodeObservation:
            raise TypeError("ExpressionEpisode requires an EpisodeObservation")
        if type(budget) is not InteractiveTurnBudget:
            raise TypeError("ExpressionEpisode requires the shared InteractiveTurnBudget")
        episode_id = observation.episode_id
        join: asyncio.Future[EpisodeOutcome] | None = None
        async with self._lock:
            existing = self._outcomes.get(episode_id)
            if existing is not None:
                return existing
            join = self._outcome_futures.get(episode_id)
            if join is not None:
                # Join outside the lock so the owner can publish its outcome.
                pass
            else:
                self._outcome_futures[episode_id] = (
                    asyncio.get_running_loop().create_future()
                )
        if join is not None:
            return await asyncio.shield(join)

        async with self._lock:
            # This invocation owns the effect-once episode from here onward.
            if self._replay_lookup is not None:
                replay = await self._replay_lookup(episode_id)
                if replay is not None:
                    self._observations[episode_id] = observation
                    self._budgets[episode_id] = budget
                    self._outcomes[episode_id] = replay.outcome
                    if replay.authorization is not None:
                        self._authorizations[episode_id] = replay.authorization
                    if replay.full_result is not None:
                        async def replayed_full() -> FullCognitionResult:
                            return replay.full_result  # type: ignore[return-value]

                        self._full_tasks[episode_id] = asyncio.create_task(
                            replayed_full(), name=f"{episode_id}:full-replay"
                        )
                    _LOG.info(
                        "expression episode replayed episode_id=%s winner=%s phase=%s",
                        episode_id,
                        replay.outcome.winner,
                        "pending" if replay.outcome.full_pending else "terminal",
                    )
                    future = self._outcome_futures[episode_id]
                    if not future.done():
                        future.set_result(replay.outcome)
                    return replay.outcome
            self._observations[episode_id] = observation
            self._budgets[episode_id] = budget
            budget.mark("full")
            full_task = asyncio.create_task(
                self._full_cognition(observation.inner_seed, observation, budget),
                name=f"{episode_id}:full",
            )
            self._full_tasks[episode_id] = full_task
            provisional_task: asyncio.Task[EpisodeCandidate] | None = None
            if self._policy.mode != "off":
                budget.mark("provisional")
                provisional_task = asyncio.create_task(
                    self._provisional_author(observation.inner_seed, observation, budget),
                    name=f"{episode_id}:provisional",
                )
                self._provisional_tasks[episode_id] = provisional_task

        # Let both authors receive the exact same object before choosing a
        # winner.  If both complete in one loop turn, full wins and no extra
        # visible beat is authorized.
        await asyncio.sleep(0)
        provider_slots_started = 2 if provisional_task is not None else 1
        rejections: list[str] = []
        shadow_ref: str | None = None
        while True:
            # Preserve validation/shadow evidence even when both provider
            # slots complete in the same event-loop turn.  Full still wins;
            # inspecting the losing candidate grants it no Action authority.
            if (
                full_task.done()
                and provisional_task is not None
                and provisional_task.done()
            ):
                provisional = await self._provisional_result(
                    provisional_task, rejections
                )
                if provisional is not None:
                    reason = (
                        "provisional.candidate_deadline"
                        if budget.remaining() <= 0
                        else self._candidate_rejection(
                            provisional,
                            observation=observation,
                            phase="provisional",
                        )
                    )
                    if reason is not None:
                        rejections.append(reason)
                    elif self._policy.mode == "shadow":
                        shadow_ref = provisional.proposal_ref
                provisional_task = None
            if full_task.done():
                full = await self._full_result(full_task, rejections)
                valid_full = (
                    full.candidate
                    if full is not None and full.candidate is not None
                    else None
                )
                if valid_full is not None:
                    reason = (
                        "full.candidate_deadline"
                        if budget.remaining() <= 0
                        else self._candidate_rejection(
                            valid_full, observation=observation, phase="full"
                        )
                    )
                    if reason is None:
                        await self._cancel_task(provisional_task)
                        return await self._authorize_outcome(
                            episode_id=episode_id,
                            candidate=valid_full,
                            budget=budget,
                            winner="full",
                            status=(
                                "shadow_full_authorized"
                                if self._policy.mode == "shadow"
                                else "full_authorized"
                            ),
                            provider_slots=provider_slots_started,
                            shadow_ref=shadow_ref,
                            rejections=rejections,
                        )
                    rejections.append(reason)
                if (
                    full is not None
                    and full.disposition == "complete_without_more"
                    and full.candidate is None
                ):
                    await self._cancel_task(provisional_task)
                    return self._remember(
                        EpisodeOutcome(
                            episode_id=episode_id,
                            status="observed_only",
                            winner="none",
                            shadow_provisional_ref=shadow_ref,
                            rejections=tuple(rejections),
                            provider_slots_started=provider_slots_started,
                        )
                    )
                if provisional_task is None or provisional_task.done():
                    return self._remember(
                        EpisodeOutcome(
                            episode_id=episode_id,
                            status="failed_safe",
                            winner="none",
                            shadow_provisional_ref=shadow_ref,
                            rejections=tuple(rejections),
                            provider_slots_started=provider_slots_started,
                        )
                    )

            if provisional_task is not None and provisional_task.done():
                provisional = await self._provisional_result(
                    provisional_task, rejections
                )
                if provisional is not None:
                    reason = (
                        "provisional.candidate_deadline"
                        if budget.remaining() <= 0
                        else self._candidate_rejection(
                            provisional,
                            observation=observation,
                            phase="provisional",
                        )
                    )
                    if reason is None:
                        if self._policy.mode == "shadow":
                            shadow_ref = provisional.proposal_ref
                            provisional_task = None
                            continue
                        # Full may have become valid while provisional was
                        # being parsed.  Re-check before crossing Acceptance.
                        if full_task.done():
                            continue
                        return await self._authorize_outcome(
                            episode_id=episode_id,
                            candidate=provisional,
                            budget=budget,
                            winner="provisional",
                            status="provisional_authorized",
                            provider_slots=2,
                            shadow_ref=None,
                            rejections=rejections,
                            full_pending=True,
                        )
                    rejections.append(reason)
                provisional_task = None

            pending = [
                task
                for task in (full_task, provisional_task)
                if task is not None and not task.done()
            ]
            if not pending:
                continue
            remaining = budget.remaining()
            if remaining <= 0:
                for task in pending:
                    await self._cancel_task(task)
                rejections.append("episode.budget_exhausted")
                return self._remember(
                    EpisodeOutcome(
                        episode_id=episode_id,
                        status="failed_safe",
                        winner="none",
                        shadow_provisional_ref=shadow_ref,
                        rejections=tuple(rejections),
                        provider_slots_started=provider_slots_started,
                    )
                )
            done, _ = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                continue

    async def settle(self, external_result: EpisodeExternalResult) -> SettlementOutcome:
        """Settle the full disposition without rewriting a delivered beat."""

        if type(external_result) is not EpisodeExternalResult:
            raise TypeError("ExpressionEpisode requires a typed external result")
        episode_id = next(
            (
                key
                for key, authorization in self._authorizations.items()
                if external_result.action_id in authorization.action_ids
            ),
            None,
        )
        if episode_id is None:
            raise ValueError("external result does not belong to this Expression Episode")
        authorization = self._authorizations[episode_id]
        budget = self._budgets[episode_id]
        full_task = self._full_tasks[episode_id]
        full = await full_task
        observation = self._observations[episode_id]
        delivered_refs = (
            (external_result.receipt_ref,)
            if external_result.receipt_ref is not None
            else authorization.delivered_refs
        )
        immutable = external_result.observed_state in _NON_CANCELLABLE_STATES

        if external_result.observed_state == "user_interjected":
            full = FullCognitionResult(disposition="cancel_pending")

        if full.disposition == "append":
            assert full.candidate is not None
            rejection = self._candidate_rejection(
                full.candidate, observation=observation, phase="full"
            )
            if rejection is not None:
                return SettlementOutcome(
                    episode_id=episode_id,
                    disposition="complete_without_more",
                    delivered_refs=delivered_refs,
                    fail_closed_reason=rejection,
                )
            appended = await self._authorizer.authorize(full.candidate, budget=budget)
            return SettlementOutcome(
                episode_id=episode_id,
                disposition="append",
                authorized_action_ids=appended.action_ids,
                delivered_refs=delivered_refs,
            )

        if full.disposition == "complete_without_more":
            return SettlementOutcome(
                episode_id=episode_id,
                disposition="complete_without_more",
                delivered_refs=delivered_refs,
            )

        if immutable:
            verb = "supersede" if full.disposition == "supersede_pending" else "cancel"
            return SettlementOutcome(
                episode_id=episode_id,
                disposition="complete_without_more",
                delivered_refs=delivered_refs,
                fail_closed_reason=f"{verb}.delivered_immutable"
                if external_result.observed_state in {"provider_accepted", "delivered"}
                else f"{verb}.dispatch_started",
            )

        if full.disposition == "supersede_pending":
            if (
                self._supersede_pending is not None
                and full.replacement_plan_ref is not None
            ):
                replacement = await self._supersede_pending(
                    authorization.action_ids,
                    full.replacement_plan_ref,
                    budget,
                )
                return SettlementOutcome(
                    episode_id=episode_id,
                    disposition="supersede_pending",
                    authorized_action_ids=replacement.action_ids,
                    cancelled_action_ids=authorization.action_ids,
                    pending_cancellation=False,
                    delivered_refs=delivered_refs,
                )
            cancelled = await self._cancel_actions(
                authorization.action_ids, reason="episode:supersede-fail-closed"
            )
            return SettlementOutcome(
                episode_id=episode_id,
                disposition="cancel_pending",
                cancelled_action_ids=cancelled,
                delivered_refs=delivered_refs,
                fail_closed_reason=(
                    "supersede.replacement_plan_ref_unavailable"
                    if full.replacement_plan_ref is None
                    else "supersede.replacement_acceptance_unavailable"
                ),
            )

        cancelled = await self._cancel_actions(
            authorization.action_ids, reason="episode:full-cancel"
        )
        if len(cancelled) != len(authorization.action_ids):
            return SettlementOutcome(
                episode_id=episode_id,
                disposition="cancel_pending",
                cancelled_action_ids=cancelled,
                pending_cancellation=True,
                delivered_refs=delivered_refs,
                fail_closed_reason="cancel.lifecycle_rejected",
            )
        return SettlementOutcome(
            episode_id=episode_id,
            disposition="cancel_pending",
            cancelled_action_ids=cancelled,
            delivered_refs=delivered_refs,
        )

    async def aclose(self) -> None:
        for task in (*self._provisional_tasks.values(), *self._full_tasks.values()):
            await self._cancel_task(task)

    async def _authorize_outcome(
        self,
        *,
        episode_id: str,
        candidate: EpisodeCandidate,
        budget: InteractiveTurnBudget,
        winner: Literal["provisional", "full"],
        status: Literal[
            "full_authorized", "provisional_authorized", "shadow_full_authorized"
        ],
        provider_slots: int,
        shadow_ref: str | None,
        rejections: list[str],
        full_pending: bool = False,
    ) -> EpisodeOutcome:
        if budget.remaining(include_reserve=True) <= 0:
            rejections.append("episode.acceptance_budget_exhausted")
            return self._remember(
                EpisodeOutcome(
                    episode_id=episode_id,
                    status="failed_safe",
                    winner="none",
                    shadow_provisional_ref=shadow_ref,
                    rejections=tuple(rejections),
                    provider_slots_started=provider_slots,
                )
            )
        authorization = await self._authorizer.authorize(candidate, budget=budget)
        self._authorizations[episode_id] = authorization
        elapsed_ms = max(0.0, (budget.clock() - budget.started_at) * 1_000)
        budget.mark("first_semantic_action")
        _LOG.info(
            "expression episode authorized episode_id=%s phase=%s winner=%s "
            "plan_id=%s action_count=%d first_semantic_action_ms=%.1f",
            episode_id,
            candidate.phase,
            winner,
            authorization.plan_id,
            len(authorization.action_ids),
            elapsed_ms,
        )
        return self._remember(
            EpisodeOutcome(
                episode_id=episode_id,
                status=status,
                winner=winner,
                authorized_action_ids=authorization.action_ids,
                plan_id=authorization.plan_id,
                full_pending=full_pending,
                shadow_provisional_ref=shadow_ref,
                rejections=tuple(rejections),
                first_semantic_action_ms=elapsed_ms,
                provider_slots_started=provider_slots,
            )
        )

    def _candidate_rejection(
        self,
        candidate: EpisodeCandidate,
        *,
        observation: EpisodeObservation,
        phase: EpisodePhase,
    ) -> str | None:
        seed = observation.inner_seed
        if candidate.phase != phase:
            return f"{phase}.phase_mismatch"
        if (
            candidate.seed_id != seed.seed_id
            or candidate.observation_ref != seed.observation_ref
            or candidate.observation_event_ref != seed.observation_event_ref
            or candidate.cursor != seed.cursor
        ):
            return f"{phase}.lineage_mismatch"
        required = {
            *seed.accepted_source_bindings,
            *seed.advisory_source_bindings,
            seed.observation_ref,
            seed.observation_event_ref,
        }
        if not required.issubset(candidate.source_bindings):
            return f"{phase}.source_bindings_incomplete"
        if candidate.current_turn_advisory_claimed_as_fact:
            return f"{phase}.advisory_claimed_as_fact"
        if phase == "provisional":
            if candidate.beat_count != 1 or candidate.modality != "text":
                return "provisional.single_text_beat_required"
        return None

    async def _cancel_actions(
        self, action_ids: tuple[str, ...], *, reason: str
    ) -> tuple[str, ...]:
        if self._cancel_pending is None:
            return ()
        cancelled: list[str] = []
        for action_id in action_ids:
            if await self._cancel_pending(action_id, reason=reason):
                cancelled.append(action_id)
        return tuple(cancelled)

    @staticmethod
    async def _cancel_task(task: asyncio.Task[object] | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _full_result(
        task: asyncio.Task[FullCognitionResult], rejections: list[str]
    ) -> FullCognitionResult | None:
        try:
            return await task
        except Exception as exc:  # noqa: BLE001 - fail closed at orchestration seam
            rejections.append(f"full.exception:{type(exc).__name__}")
            return None

    @staticmethod
    async def _provisional_result(
        task: asyncio.Task[EpisodeCandidate], rejections: list[str]
    ) -> EpisodeCandidate | None:
        try:
            return await task
        except Exception as exc:  # noqa: BLE001 - fail closed at orchestration seam
            rejections.append(f"provisional.exception:{type(exc).__name__}")
            return None

    def _remember(self, outcome: EpisodeOutcome) -> EpisodeOutcome:
        self._outcomes[outcome.episode_id] = outcome
        future = self._outcome_futures.get(outcome.episode_id)
        if future is not None and not future.done():
            future.set_result(outcome)
        return outcome


__all__ = [
    "AuthorizationResult",
    "EpisodeCandidate",
    "EpisodeDisposition",
    "EpisodeExternalResult",
    "EpisodeObservation",
    "EpisodeOutcome",
    "EpisodePhase",
    "EpisodePolicy",
    "EpisodeReplaySnapshot",
    "ExpressionEpisode",
    "ExpressionEpisodeDiagnostics",
    "FullCognitionResult",
    "InnerSeed",
    "SettlementOutcome",
    "validate_provisional_proposal",
]
