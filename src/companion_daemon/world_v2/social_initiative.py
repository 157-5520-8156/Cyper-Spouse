"""Authority-only compiler for human-like conversation initiative opportunities.

Timers never write prose here.  They only make an immutable source eligible for
the existing proactive deliberation lane, where the model remains free to act
now, later, or stay silent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from .ledger import LedgerPort
from .random_authority import RandomAuthority
from .schema_core import FrozenModel


class SocialInitiativePolicy(FrozenModel):
    spontaneous_idle_seconds: int = Field(default=1_800, ge=60, le=172_800)
    spontaneous_expiry_seconds: int = Field(default=43_200, ge=120, le=604_800)
    contact_cooldown_seconds: int = Field(default=900, ge=60, le=86_400)
    local_timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=64)
    consideration_band_override_seconds: tuple[int, int] | None = None

    @model_validator(mode="after")
    def expiry_follows_opening(self) -> "SocialInitiativePolicy":
        if self.spontaneous_expiry_seconds <= self.spontaneous_idle_seconds:
            raise ValueError("spontaneous initiative expiry must follow idle opening")
        if self.consideration_band_override_seconds is not None:
            low, high = self.consideration_band_override_seconds
            if not 60 <= low <= high < self.spontaneous_expiry_seconds:
                raise ValueError(
                    "social initiative cadence override must fit the spontaneous window"
                )
        return self


class SocialInitiativeOpportunity(FrozenModel):
    source_kind: Literal["spontaneous_contact", "response_gap", "ambient_presence"]
    source_id: str
    source_event_ref: str
    source_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_world_revision: int = Field(ge=1)
    trace_id: str
    correlation_id: str
    created_at: datetime
    consideration_id: str
    consideration_epoch: int = Field(default=0, ge=0)
    scheduled_for: datetime
    cadence_reason_codes: tuple[str, ...] = ()


class SocialInitiativeDecisionProfile(FrozenModel):
    """Explainable timing range; it never decides whether the character speaks."""

    consideration_band_seconds: tuple[int, int]
    delay_candidates_seconds: tuple[int, ...] = Field(min_length=1, max_length=3)
    candidate_weights: dict[str, int]
    reason_codes: tuple[str, ...]


class SocialInitiativeContextPolicy:
    """Translate relationship, affect, activity, and daypart into soft preference mass."""

    version = "social-initiative-context.2"

    def __init__(self, *, policy: SocialInitiativePolicy) -> None:
        self._policy = policy

    def compile(self, *, projection, logical_time: datetime) -> SocialInitiativeDecisionProfile:
        relationship = projection.relationship_states[-1] if projection.relationship_states else None
        variables = getattr(relationship, "variables", None)
        stage = str(getattr(relationship, "stage", "stranger"))
        trust = int(getattr(variables, "trust_bp", 0)) if variables is not None else 0
        closeness = int(getattr(variables, "closeness_bp", 0)) if variables is not None else 0
        mutuality = int(getattr(variables, "mutuality_bp", 0)) if variables is not None else 0
        highly_connected = min(trust, closeness, mutuality) >= 6_000 and (
            closeness + mutuality
        ) // 2 >= 7_000
        if stage in {"close_friend", "lover"} or highly_connected:
            low, high = 3_600, 7_200
            relationship_reason = f"relationship:{stage if relationship is not None else 'close'}"
        elif stage in {"friend", "ambiguous"} or (closeness + mutuality) // 2 >= 4_000:
            low, high = 7_200, 14_400
            relationship_reason = f"relationship:{stage if relationship is not None else 'friend'}"
        elif stage == "acquaintance":
            low, high = 10_800, 21_600
            relationship_reason = "relationship:acquaintance"
        else:
            low, high = 21_600, 28_800
            relationship_reason = "relationship:stranger"

        override = self._policy.consideration_band_override_seconds
        if override is not None:
            low, high = override
            relationship_reason = "relationship:test_override"

        approach = guarded = 0
        for episode in projection.affect_episodes:
            if getattr(episode, "status", None) != "active":
                continue
            for component in getattr(episode, "components", ()):
                intensity = int(getattr(component, "intensity_bp", 0))
                if getattr(component, "dimension", None) in {"warmth", "joy"}:
                    approach = max(approach, intensity)
                else:
                    guarded = max(guarded, intensity)
        if approach > guarded and approach >= 5_000:
            affect_reason = "affect:approach"
        elif guarded >= 5_000:
            affect_reason = "affect:guarded"
        else:
            affect_reason = "affect:neutral"

        engaged = any(getattr(plan, "status", None) == "active" for plan in projection.plans)
        if engaged:
            activity_reason = "activity:engaged"
        else:
            activity_reason = "activity:available"

        local_hour = logical_time.astimezone(ZoneInfo(self._policy.local_timezone)).hour
        if local_hour < 6:
            daypart_reason = "daypart:overnight"
        else:
            daypart_reason = "daypart:day"

        cadence_floor = 60 if override is not None else 2_700
        cadence_ceiling = min(28_800, self._policy.spontaneous_expiry_seconds - 1)
        low = min(max(cadence_floor, low), cadence_ceiling)
        high = min(max(low, high), cadence_ceiling)
        raw_candidates = (low, (low + high) // 2, high)
        candidates = tuple(dict.fromkeys(raw_candidates))
        early_bias = 0
        if affect_reason == "affect:approach":
            early_bias += 1_500
        elif affect_reason == "affect:guarded":
            early_bias -= 1_500
        if activity_reason == "activity:engaged":
            early_bias -= 1_000
        if daypart_reason == "daypart:overnight":
            early_bias -= 1_500
        early_bias = min(2_000, max(-2_000, early_bias))
        weights = (
            2_500 + early_bias,
            5_000,
            2_500 - early_bias,
        )
        candidate_weights: dict[str, int] = {}
        for delay, weight in zip(raw_candidates, weights, strict=True):
            ref = f"delay:{delay}"
            candidate_weights[ref] = candidate_weights.get(ref, 0) + weight
        return SocialInitiativeDecisionProfile(
            consideration_band_seconds=(low, high),
            delay_candidates_seconds=candidates,
            candidate_weights=candidate_weights,
            reason_codes=(
                relationship_reason,
                affect_reason,
                activity_reason,
                daypart_reason,
            ),
        )


def social_initiative_attempt_id(
    *, source_event_ref: str, profile: SocialInitiativeDecisionProfile,
    policy_version: str = SocialInitiativeContextPolicy.version,
) -> str:
    """Return the stable source/profile identity shared by writers and read models."""

    material = {
        "source_event_ref": source_event_ref,
        "policy_version": policy_version,
        "consideration_band_seconds": profile.consideration_band_seconds,
        "delay_candidates_seconds": profile.delay_candidates_seconds,
        "candidate_weights": profile.candidate_weights,
        "reason_codes": profile.reason_codes,
    }
    return "social-initiative:" + hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def social_initiative_consideration_id(
    *,
    attempt_id: str,
    delay_seconds: int,
    epoch: int,
    source_kind: Literal["spontaneous_contact", "ambient_presence"],
) -> str:
    return "consideration:social-initiative:" + hashlib.sha256(
        json.dumps(
            {
                "attempt_id": attempt_id,
                "delay_seconds": delay_seconds,
                "epoch": epoch,
                "kind": source_kind,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class SocialInitiativeCompiler:
    """Find one eligible source without interpreting words or inventing facts."""

    def __init__(self, *, ledger: LedgerPort, policy: SocialInitiativePolicy) -> None:
        self._ledger = ledger
        self._policy = policy
        self._context = SocialInitiativeContextPolicy(policy=policy)
        self._random = RandomAuthority(
            ledger=ledger, source="world-v2:social-initiative-random"
        )

    async def next_opportunity(self, projection) -> SocialInitiativeOpportunity | None:
        logical_time = projection.logical_time
        if logical_time is None:
            return None
        recent_contact = max(
            (
                item.logical_time
                for item in projection.actions
                if item.kind in {"proactive_message", "followup"}
                and item.state not in {"failed", "cancelled", "expired"}
            ),
            default=None,
        )
        if recent_contact is not None and (
            logical_time - recent_contact
        ).total_seconds() < self._policy.contact_cooldown_seconds:
            return None
        response = await self._response_gap(projection, logical_time)
        if response is not None:
            return response
        retry = await self._failed_consideration_retry(projection)
        if retry is not None:
            return retry
        return await self._spontaneous_contact(projection, logical_time)

    async def _failed_consideration_retry(
        self, projection
    ) -> SocialInitiativeOpportunity | None:
        process = next(
            (
                item
                for item in reversed(getattr(projection, "trigger_processes", ()))
                if item.process_kind == "proactive_action_deliberation"
                and item.state == "terminal"
            ),
            None,
        )
        if (
            process is None
            or not str(process.runtime_outcome_ref).startswith(
                "proactive:deliberation-failed:"
            )
            or not process.trigger_ref.startswith("proactive-consideration:")
            or process.source_evidence_ref is None
        ):
            return None
        source_ref = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == process.source_evidence_ref
            ),
            None,
        )
        if source_ref is None:
            return None
        failure_revision, _failed_at = technical_failure_point(
            projection=projection, process=process
        )
        latest_message_revision = (
            projection.message_observations[-1].world_revision
            if projection.message_observations
            else 0
        )
        if failure_revision is None or latest_message_revision > failure_revision:
            return None
        located = await self._lookup(source_ref.event_id)
        if located is None:
            return None
        event = located[0]
        source_kind = (
            "ambient_presence"
            if event.event_type == "ClockAdvanced"
            else "spontaneous_contact"
            if event.event_type == "ObservationRecorded"
            else None
        )
        if source_kind is None:
            return None
        return await self._from_source(
            source_kind=source_kind,
            source_id=f"retry:{source_ref.event_id}",
            source_event_ref=source_ref.event_id,
            source_world_revision=source_ref.world_revision,
            consideration_id=process.trigger_ref.removeprefix(
                "proactive-consideration:"
            ),
            scheduled_for=source_ref.logical_time,
            cadence_reason_codes=("technical_failure:retry",),
        )

    async def _response_gap(self, projection, logical_time: datetime):
        candidates = []
        for manifest in projection.expression_plan_manifests:
            expectation = manifest.response_expectation
            if expectation is None or not (
                expectation.not_before <= logical_time < expectation.expires_at
            ):
                continue
            plan = next(
                (item for item in projection.expression_plans if item.plan_id == manifest.plan_id),
                None,
            )
            beat = next(
                (item for item in manifest.beats if item.beat_id == expectation.source_beat_id),
                None,
            )
            action = next(
                (
                    item
                    for item in projection.actions
                    if beat is not None and item.action_id == beat.action.action_id
                ),
                None,
            )
            accepted_receipt = next(
                (
                    item
                    for item in projection.execution_receipts
                    if action is not None
                    and item.action_id == action.action_id
                    and item.observed_state in {"provider_accepted", "delivered"}
                ),
                None,
            )
            delivery_ready = (
                action is not None
                and accepted_receipt is not None
                and (
                    action.state == "delivered"
                    if expectation.delivery_requirement == "confirmed_delivered"
                    else action.state in {"provider_accepted", "delivered"}
                )
            )
            if (
                plan is None
                or plan.state not in {"authorized", "completed"}
                or beat is None
                or not delivery_ready
            ):
                continue
            # A later inbound observation is not, by itself, proof that the
            # person answered or cancelled the expected continuation.  The
            # proactive deliberation lane must still be allowed to consider
            # the source-bound opportunity; it can choose to acknowledge the
            # new message, continue the earlier thought, defer, or stay
            # silent.  Treating every later message as an answer makes the
            # character lose the human case where the other person sends an
            # unrelated message while she still has something to say.
            candidates.append((expectation.not_before, manifest))
        if not candidates:
            return None
        _due, manifest = min(candidates, key=lambda item: (item[0], item[1].acceptance_event_ref))
        context_revision = (
            projection.message_observations[-1].world_revision
            if projection.message_observations
            else manifest.recorded_at_world_revision
        )
        return await self._from_source(
            source_kind="response_gap",
            source_id=manifest.plan_id,
            source_event_ref=manifest.acceptance_event_ref,
            source_world_revision=manifest.recorded_at_world_revision,
            consideration_id=(
                "consideration:social-initiative:"
                + hashlib.sha256(
                    f"response_gap:{manifest.acceptance_event_ref}:{context_revision}".encode()
                ).hexdigest()
            ),
        )

    async def _spontaneous_contact(self, projection, logical_time: datetime):
        if not projection.message_observations:
            return None
        latest = projection.message_observations[-1]
        source = await self._lookup(f"event:observation:{latest.observation_id}")
        if source is None:
            # Observation event ids are deployment-defined; resolve by the
            # exact committed revision retained in the projection instead.
            ref = next(
                (
                    item
                    for item in projection.committed_world_event_refs
                    if item.world_revision == latest.world_revision
                    and item.event_type == "ObservationRecorded"
                ),
                None,
            )
            source = await self._lookup(ref.event_id) if ref is not None else None
        if source is None or source[0].event_type != "ObservationRecorded":
            return None
        elapsed = (logical_time - source[0].logical_time).total_seconds()
        if elapsed < self._policy.spontaneous_idle_seconds:
            return None
        profile = self._context.compile(projection=projection, logical_time=logical_time)
        attempt_id = social_initiative_attempt_id(
            source_event_ref=source[0].event_id,
            profile=profile,
            policy_version=self._context.version,
        )
        draw_kwargs = dict(
            attempt_id=attempt_id,
            candidate_refs=tuple(
                f"delay:{seconds}" for seconds in profile.delay_candidates_seconds
            ),
            candidate_weights=profile.candidate_weights,
            weight_policy_version=self._context.version,
            catalog_version="social-initiative-delay.1",
            logical_time=logical_time,
            seed_instant=source[0].logical_time,
            actor="system:social-initiative",
            trace_id=source[0].trace_id,
            correlation_id=source[0].correlation_id,
        )
        draw = (
            await asyncio.to_thread(self._random.draw, **draw_kwargs)
            if self._ledger.blocks_event_loop
            else self._random.draw(**draw_kwargs)
        )
        try:
            delay_seconds = int(draw.selected_candidate_ref.removeprefix("delay:"))
        except (AttributeError, ValueError):
            raise ValueError("social initiative cadence draw did not select a delay")
        if delay_seconds not in profile.delay_candidates_seconds:
            raise ValueError("social initiative cadence draw selected an unknown delay")
        if elapsed < delay_seconds:
            return None
        consideration_epoch = max(0, int(elapsed // delay_seconds) - 1)
        ambient = elapsed >= self._policy.spontaneous_expiry_seconds
        consideration_id = social_initiative_consideration_id(
            attempt_id=attempt_id,
            delay_seconds=delay_seconds,
            epoch=consideration_epoch,
            source_kind="ambient_presence" if ambient else "spontaneous_contact",
        )
        # A response expectation is the stronger and more specific authority.
        # Do not also manufacture a generic idle opportunity for that expression.
        source_kind = "ambient_presence" if ambient else "spontaneous_contact"
        source_id = latest.observation_id
        source_event_ref = source[0].event_id
        source_world_revision = latest.world_revision
        scheduled_for = source[0].logical_time + timedelta(
            seconds=delay_seconds * (consideration_epoch + 1)
        )
        if ambient:
            clock_ref = min(
                (
                    item
                    for item in projection.committed_world_event_refs
                    if item.event_type == "ClockAdvanced"
                    and scheduled_for <= item.logical_time <= logical_time
                ),
                key=lambda item: (item.logical_time, item.event_id),
                default=None,
            )
            if clock_ref is None:
                return None
            source_id = f"ambient:{consideration_epoch}"
            source_event_ref = clock_ref.event_id
            source_world_revision = clock_ref.world_revision
        opportunity = await self._from_source(
            source_kind=source_kind,
            source_id=source_id,
            source_event_ref=source_event_ref,
            source_world_revision=source_world_revision,
            consideration_id=consideration_id,
            consideration_epoch=consideration_epoch,
            scheduled_for=scheduled_for,
            cadence_reason_codes=profile.reason_codes,
        )
        current = next(
            (
                item
                for item in reversed(getattr(projection, "trigger_processes", ()))
                if item.process_kind == "proactive_action_deliberation"
                and item.trigger_ref == "proactive-consideration:" + consideration_id
            ),
            None,
        )
        if current is not None and current.state == "terminal":
            # A completed epoch is effect-once. A failed epoch is returned only
            # by the retry path once its technical backoff is actually due.
            return None
        return opportunity

    async def _from_source(
        self,
        *,
        source_kind,
        source_id: str,
        source_event_ref: str,
        source_world_revision: int,
        consideration_id: str | None = None,
        consideration_epoch: int = 0,
        scheduled_for: datetime | None = None,
        cadence_reason_codes: tuple[str, ...] = (),
    ):
        located = await self._lookup(source_event_ref)
        if located is None:
            return None
        event, commit = located
        return SocialInitiativeOpportunity(
            source_kind=source_kind,
            source_id=source_id,
            source_event_ref=event.event_id,
            source_event_hash=event.payload_hash,
            source_world_revision=source_world_revision,
            trace_id=event.trace_id,
            correlation_id=event.correlation_id,
            created_at=event.created_at,
            consideration_id=(
                consideration_id
                or "consideration:social-initiative:"
                + hashlib.sha256(
                    f"{source_kind}:{event.event_id}".encode()
                ).hexdigest()
            ),
            consideration_epoch=consideration_epoch,
            scheduled_for=scheduled_for or event.logical_time,
            cadence_reason_codes=cadence_reason_codes,
        )

    async def _lookup(self, event_id: str):
        if self._ledger.blocks_event_loop:
            import asyncio

            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)


def technical_failure_point(*, projection, process) -> tuple[int | None, datetime | None]:
    """Locate the durable failure boundary shared by retry and health readers."""

    result_ref = str(process.runtime_outcome_ref).removeprefix(
        "proactive:deliberation-failed:"
    )
    audit = next(
        (
            item
            for item in reversed(projection.model_result_audits)
            if item.model_result_ref == result_ref
            and item.proposal_hash is None
        ),
        None,
    )
    if audit is None:
        return None, None
    committed = next(
        (
            item
            for item in projection.committed_world_event_refs
            if item.event_id == audit.event_ref
        ),
        None,
    )
    return (
        committed.world_revision
        if committed is not None
        else audit.evaluated_world_revision,
        committed.logical_time
        if committed is not None
        else (
            process.claim_lease.acquired_at
            if process.claim_lease is not None
            else None
        ),
    )


__all__ = [
    "SocialInitiativeCompiler",
    "SocialInitiativeContextPolicy",
    "SocialInitiativeDecisionProfile",
    "SocialInitiativeOpportunity",
    "SocialInitiativePolicy",
    "social_initiative_attempt_id",
    "social_initiative_consideration_id",
    "technical_failure_point",
]
