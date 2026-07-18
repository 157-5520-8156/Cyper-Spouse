"""Authority-only compiler for human-like conversation initiative opportunities.

Timers never write prose here.  They only make an immutable source eligible for
the existing proactive deliberation lane, where the model remains free to act
now, later, or stay silent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .ledger import LedgerPort
from .random_authority import RandomAuthority
from .schema_core import FrozenModel


class SocialInitiativePolicy(FrozenModel):
    spontaneous_idle_seconds: int = Field(default=1_800, ge=60, le=172_800)
    spontaneous_expiry_seconds: int = Field(default=43_200, ge=120, le=604_800)
    contact_cooldown_seconds: int = Field(default=900, ge=60, le=86_400)

    @model_validator(mode="after")
    def expiry_follows_opening(self) -> "SocialInitiativePolicy":
        if self.spontaneous_expiry_seconds <= self.spontaneous_idle_seconds:
            raise ValueError("spontaneous initiative expiry must follow idle opening")
        return self


class SocialInitiativeOpportunity(FrozenModel):
    source_kind: Literal["spontaneous_contact", "response_gap"]
    source_id: str
    source_event_ref: str
    source_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_world_revision: int = Field(ge=1)
    trace_id: str
    correlation_id: str
    created_at: datetime


class SocialInitiativeDecisionProfile(FrozenModel):
    """Explainable soft timing and act/hold mass from general world context."""

    not_before_seconds: int = Field(ge=60)
    candidate_weights: dict[Literal["act", "hold"], int]
    reason_codes: tuple[str, ...]


class SocialInitiativeContextPolicy:
    """Translate relationship, affect, activity, and daypart into soft preference mass."""

    version = "social-initiative-context.1"

    def __init__(self, *, policy: SocialInitiativePolicy) -> None:
        self._policy = policy

    def compile(self, *, projection, logical_time: datetime) -> SocialInitiativeDecisionProfile:
        relationship = projection.relationship_states[-1] if projection.relationship_states else None
        variables = getattr(relationship, "variables", None)
        affinity = (
            (
                int(getattr(variables, "closeness_bp", 0))
                + int(getattr(variables, "mutuality_bp", 0))
            )
            // 2
            if variables is not None
            else None
        )
        wait = self._policy.spontaneous_idle_seconds
        act_weight = 6_000
        hold_weight = 6_000
        if affinity is not None and affinity >= 7_000:
            wait -= self._policy.spontaneous_idle_seconds // 5
            act_weight += 1_000
            hold_weight -= 1_000
            relationship_reason = "relationship:close"
        elif affinity is not None and affinity <= 2_000:
            wait += self._policy.spontaneous_idle_seconds // 5
            act_weight -= 500
            hold_weight += 1_000
            relationship_reason = "relationship:distant"
        else:
            relationship_reason = "relationship:established"

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
            wait -= self._policy.spontaneous_idle_seconds // 10
            act_weight += 1_000
            hold_weight -= 1_000
            affect_reason = "affect:approach"
        elif guarded >= 5_000:
            wait += self._policy.spontaneous_idle_seconds * 7 // 20
            act_weight -= 1_000
            hold_weight += 3_000
            affect_reason = "affect:guarded"
        else:
            affect_reason = "affect:neutral"

        engaged = any(getattr(plan, "status", None) == "active" for plan in projection.plans)
        if engaged:
            wait += self._policy.spontaneous_idle_seconds // 2
            hold_weight += 2_000
            activity_reason = "activity:engaged"
        else:
            activity_reason = "activity:available"

        if logical_time.hour < 6:
            wait += self._policy.spontaneous_idle_seconds // 2
            hold_weight += 1_500
            daypart_reason = "daypart:overnight"
        else:
            daypart_reason = "daypart:day"

        return SocialInitiativeDecisionProfile(
            not_before_seconds=min(
                max(60, wait), self._policy.spontaneous_expiry_seconds - 1
            ),
            candidate_weights={
                "act": max(1, act_weight),
                "hold": max(1, hold_weight),
            },
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
        "not_before_seconds": profile.not_before_seconds,
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
        return await self._spontaneous_contact(projection, logical_time)

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
        return await self._from_source(
            source_kind="response_gap",
            source_id=manifest.plan_id,
            source_event_ref=manifest.acceptance_event_ref,
            source_world_revision=manifest.recorded_at_world_revision,
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
        if not (60 <= elapsed < self._policy.spontaneous_expiry_seconds):
            return None
        profile = self._context.compile(projection=projection, logical_time=logical_time)
        if elapsed < profile.not_before_seconds:
            return None
        attempt_id = social_initiative_attempt_id(
            source_event_ref=source[0].event_id,
            profile=profile,
            policy_version=self._context.version,
        )
        draw_kwargs = dict(
            attempt_id=attempt_id,
            candidate_refs=("act", "hold"),
            candidate_weights=profile.candidate_weights,
            weight_policy_version=self._context.version,
            catalog_version="social-initiative-act-hold.1",
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
        if draw.selected_candidate_ref == "hold":
            return None
        # A response expectation is the stronger and more specific authority.
        # Do not also manufacture a generic idle opportunity for that expression.
        return await self._from_source(
            source_kind="spontaneous_contact",
            source_id=latest.observation_id,
            source_event_ref=source[0].event_id,
            source_world_revision=latest.world_revision,
        )

    async def _from_source(
        self,
        *,
        source_kind,
        source_id: str,
        source_event_ref: str,
        source_world_revision: int,
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
        )

    async def _lookup(self, event_id: str):
        if self._ledger.blocks_event_loop:
            import asyncio

            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)


__all__ = [
    "SocialInitiativeCompiler",
    "SocialInitiativeContextPolicy",
    "SocialInitiativeDecisionProfile",
    "SocialInitiativeOpportunity",
    "SocialInitiativePolicy",
    "social_initiative_attempt_id",
]
