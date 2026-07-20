"""Future Life Author: reviewed opening -> a plan one to seven days ahead.

The present-moment ``LifeAuthorRuntime`` answers "what is she doing right
now"; this lane answers "what has she already decided about the next few
days".  It follows the same discipline: the host compiles reviewed candidates,
``RandomAuthority`` records one weighted draw (mood may tilt the weights,
never gate them), a bounded model gives the final semantic confirmation, and
acceptance lands an ordinary ``ActivityPlanned`` whose ``scheduled_window``
simply lies in the future.  The existing activity lifecycle honors that plan
when its day arrives; nothing here executes an activity.

Frequency is one *successful* plan per companion-local day.  The plan event
identity encodes the local date, so every wake of the same day converges on
the same durable outcome instead of re-planning.
"""

from __future__ import annotations

from datetime import datetime, time
import hashlib
import json
from typing import Literal

import httpx
from pydantic import Field, model_validator

from .event_identity import domain_idempotency_key
from .life_author_runtime import (
    LifeAuthorDecisionRecordedPayload,
    LifeAuthorModel,
    LifeAuthorModelFailure,
    LifeAvailabilitySnapshotRecordedPayload,
)
from .life_author_seed import ReviewedLifeSeedCatalog, ReviewedLifeSeedFutureCandidate
from .life_events import ActivityPlannedPayload
from .mood_view import active_mood_intensities
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import DueWindow, EvidenceRef, PlanStateProjection, ProjectionCursor, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class FutureLifeAuthorResult(FrozenModel):
    status: Literal[
        "planned", "already_planned", "no_opening", "no_op", "blocked"
    ]
    reason_code: str
    plan_event_ref: str | None = None
    draw_event_ref: str | None = None


class FutureLifeAuthorWeightPolicy:
    """Replayable preference mass for future commitments.

    ``future-life-author-weight.1`` combines three priors, all recorded in
    the draw so replay never recomputes them:

    * reviewed importance is the base mass;
    * nearer days carry slightly more mass than the far end of the horizon (a
      person commits to "明天/后天" more often than to "下下周三"), without
      ever zeroing a legal day;
    * accepted, active Affect gently tilts *social* future commitments: felt
      loneliness reaches toward company days ahead, while heaviness that is
      not loneliness makes her less likely to promise company.  This mirrors
      ``LifeAuthorWeightPolicy`` and stays a tendency, never an if/else rule.
    """

    version = "future-life-author-weight.1"

    def compile(
        self, *, candidates: tuple[ReviewedLifeSeedFutureCandidate, ...],
        affect_episodes: tuple[object, ...] = (),
    ) -> dict[str, int]:
        mood = active_mood_intensities(affect_episodes)
        weights: dict[str, int] = {}
        for candidate in candidates:
            mass = max(1_000, candidate.opening.importance_bp)
            mass = max(1, mass * self._proximity_multiplier_bp(candidate.day_offset) // 10_000)
            mass = max(
                1,
                mass * self._mood_multiplier_bp(
                    mood=mood, candidate_domain=candidate.opening.domain
                ) // 10_000,
            )
            weights[candidate.token] = mass
        return weights

    @staticmethod
    def _proximity_multiplier_bp(day_offset: int) -> int:
        if day_offset <= 3:
            return 10_000
        if day_offset <= 5:
            return 8_500
        return 7_000

    @staticmethod
    def _mood_multiplier_bp(*, mood: dict[str, int], candidate_domain: str) -> int:
        """Bounded (+/-35%) mood-congruent prior over future commitment domains."""

        if not mood:
            return 10_000
        heaviness = max(
            mood.get("sadness", 0), mood.get("hurt", 0), mood.get("anxiety", 0),
            mood.get("anger", 0), mood.get("resentment", 0),
        )
        loneliness = mood.get("loneliness", 0)
        brightness = max(mood.get("joy", 0), mood.get("warmth", 0))
        multiplier = 10_000
        social = {"family_roommate_friend"}
        outgoing = {"commute_walk", "creative_photo_writing"}
        if candidate_domain in social:
            multiplier += loneliness * 3_000 // 10_000
            multiplier -= max(0, heaviness - loneliness) * 3_000 // 10_000
        if candidate_domain in outgoing:
            multiplier += brightness * 2_000 // 10_000
            multiplier -= heaviness * 1_500 // 10_000
        return max(6_500, min(13_500, multiplier))


class FutureLifeAuthorRuntime:
    """Own daily future-candidate compilation, recorded draw, and acceptance.

    The sole public operation accepts a committed clock ref, exactly like the
    present-moment life author.  The caller provides no activity, date, seed,
    plan identity, or evidence.
    """

    def __init__(
        self, *, ledger, catalog: ReviewedLifeSeedCatalog, model: LifeAuthorModel,
        owner_actor_ref: str, actor: str = "worker:world-v2:future-life-author",
        horizon_days: int = 7, max_candidates: int = 16,
    ) -> None:
        if not owner_actor_ref or not actor:
            raise ValueError("future life author requires owner and worker actors")
        if not 1 <= horizon_days <= 7:
            raise ValueError("future life author horizon must stay within one week")
        self._ledger = ledger
        self._catalog = catalog
        self._model = model
        self._owner_actor_ref = owner_actor_ref
        self._actor = actor
        self._horizon_days = horizon_days
        self._max_candidates = max_candidates
        self._random = RandomAuthority(
            ledger=ledger, source="world-v2:future-life-author-random"
        )
        self._weight_policy = FutureLifeAuthorWeightPolicy()

    def _daily_suffix(self, local_date_iso: str) -> str:
        return _digest({
            "world_id": self._ledger.world_id,
            "local_date": local_date_iso,
        })

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> FutureLifeAuthorResult:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        wake = next(
            (item for item in projection.committed_world_event_refs if item.event_id == wake_event_ref),
            None,
        )
        transition = next(
            (item for item in projection.clock_transition_history if item.clock_event_ref == wake_event_ref),
            None,
        )
        if (
            logical_time is None or wake is None or wake.event_type != "ClockAdvanced"
            or transition is None or transition.payload_hash != wake.payload_hash
            or transition.computed_world_revision != wake.world_revision
        ):
            return FutureLifeAuthorResult(
                status="blocked", reason_code="future_life_author.wake_not_exact_clock"
            )
        local = self._catalog.localize(wake.logical_time)
        local_date_iso = local.date().isoformat()
        suffix = self._daily_suffix(local_date_iso)
        # One successful plan per companion-local day: the durable plan event
        # is keyed by the date, so a later wake of the same day joins instead
        # of planning again.
        plan_event_id = "event:future-life-author-plan:" + suffix
        if self._ledger.lookup_event_commit(plan_event_id) is not None:
            return FutureLifeAuthorResult(
                status="already_planned",
                reason_code="future_life_author.local_day_already_planned",
                plan_event_ref=plan_event_id,
            )
        owner_plans = tuple(
            plan for plan in projection.plans
            if plan.owner_actor_ref == self._owner_actor_ref
        )
        candidates = self._catalog.future_candidates_at(
            instant=wake.logical_time,
            plans=owner_plans,
            npcs=projection.npcs,
            horizon_days=self._horizon_days,
            max_candidates=self._max_candidates,
        )
        if not candidates:
            return FutureLifeAuthorResult(
                status="no_opening", reason_code="future_life_author.no_eligible_opening"
            )
        attempt_id = "attempt:future-life-author:" + _digest({
            "world_id": self._ledger.world_id,
            "local_date": local_date_iso,
            "catalog_version": self._catalog.version,
            "catalog_hash": self._catalog.catalog_hash,
        })
        # The seed instant is the companion-local midnight of the planning
        # day, not the wake: together with wake-free candidate tokens this
        # makes every wake of one day replay the same draw and decision.
        seed_instant = datetime.combine(local.date(), time(0), tzinfo=local.tzinfo)
        draw = self._random.draw(
            attempt_id=attempt_id,
            candidate_refs=tuple(item.token for item in candidates),
            catalog_version=self._catalog.version,
            logical_time=logical_time,
            seed_instant=seed_instant,
            actor=self._actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
            candidate_weights=self._weight_policy.compile(
                candidates=candidates,
                affect_episodes=projection.affect_episodes,
            ),
            weight_policy_version=self._weight_policy.version,
        )
        draw_event_ref = "event:random-draw:" + draw.draw_id
        selected = next(
            item for item in candidates if item.token == draw.selected_candidate_ref
        )
        try:
            decision = await self._decision_once(
                candidate=selected,
                attempt_id=attempt_id,
                wake=wake,
                draw_event_ref=draw_event_ref,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        except LifeAuthorModelFailure:
            return FutureLifeAuthorResult(
                status="blocked", reason_code="future_life_author.model_unavailable",
                draw_event_ref=draw_event_ref,
            )
        if decision.decision == "no_op":
            return FutureLifeAuthorResult(
                status="no_op", reason_code="future_life_author.model_declined",
                draw_event_ref=draw_event_ref,
            )
        assert decision.candidate_token == selected.token
        projection = self._ledger.project()
        if projection.logical_time is not None and projection.logical_time >= selected.opens_at:
            return FutureLifeAuthorResult(
                status="blocked",
                reason_code="future_life_author.selected_slot_no_longer_future",
                draw_event_ref=draw_event_ref,
            )
        event_ref = self._accept_plan(
            candidate=selected, wake_event_ref=wake_event_ref,
            suffix=suffix, trace_id=trace_id, correlation_id=correlation_id,
        )
        return FutureLifeAuthorResult(
            status="planned", reason_code="future_life_author.plan_accepted",
            plan_event_ref=event_ref, draw_event_ref=draw_event_ref,
        )

    async def _decision_once(
        self, *, candidate: ReviewedLifeSeedFutureCandidate, attempt_id: str, wake,
        draw_event_ref: str, trace_id: str, correlation_id: str,
    ) -> "_FutureDecision":
        decision_id = "decision:future-life-author:" + _digest({
            "attempt_id": attempt_id, "candidate_token": candidate.token
        })
        event_id = "event:future-life-author-decision:" + _digest(decision_id)
        existing = self._ledger.lookup_event_commit(event_id)
        if existing is not None:
            payload = LifeAuthorDecisionRecordedPayload.model_validate_json(existing[0].payload_json)
            return _FutureDecision(
                decision=payload.decision,
                candidate_token=payload.selected_candidate_token,
            )
        decision, raw = await self._deliberate(candidate, logical_time=wake.logical_time)
        projection = self._ledger.project()
        draw = next(
            item for item in projection.committed_world_event_refs
            if item.event_id == draw_event_ref and item.event_type == "RandomDrawRecorded"
        )
        payload = LifeAuthorDecisionRecordedPayload(
            decision_id=decision_id,
            attempt_id=attempt_id,
            wake_event_ref=wake.event_id,
            wake_event_payload_hash=wake.payload_hash,
            wake_world_revision=wake.world_revision,
            draw_event_ref=draw.event_id,
            draw_event_payload_hash=draw.payload_hash,
            draw_world_revision=draw.world_revision,
            candidate_token=candidate.token,
            catalog_version=self._catalog.version,
            catalog_hash=self._catalog.catalog_hash,
            decision=decision.decision,
            selected_candidate_token=decision.candidate_token,
            model=(str(getattr(self._model, "model", "")).strip() or type(self._model).__name__),
            raw_output_hash=_digest(raw),
        )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type="LifeAuthorDecisionRecorded",
            logical_time=projection.logical_time,
            created_at=projection.logical_time,
            actor=self._actor,
            source="world-v2:future-life-author",
            trace_id=trace_id,
            causation_id=draw_event_ref,
            correlation_id=correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="LifeAuthorDecisionRecorded",
                    world_id=self._ledger.world_id,
                    payload=payload.model_dump(mode="json"),
                ) or "future-life-author-decision:" + _digest({
                    "world_id": self._ledger.world_id, "decision_id": decision_id
                })
            ),
            payload=payload.model_dump(mode="json"),
        )
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=cursor, commit_id="commit:" + event_id
        )
        return decision

    async def _deliberate(
        self, candidate: ReviewedLifeSeedFutureCandidate, *, logical_time: datetime
    ) -> tuple["_FutureDecision", str]:
        try:
            raw = await self._model.complete(
                [
                {"role": "system", "content": (
                    "You are the final semantic veto for one reviewed future life commitment. The host "
                    "has already verified the target local day, weekly location/NPC availability, plan "
                    "overlap, privacy, and controlled-random selection; the commitment only becomes a "
                    "plan she may mention, and the activity lifecycle lives the day itself later. Select "
                    "the offered future slot when its supplied coordinates are coherent; use no_op only "
                    "for a concrete semantic contradiction visible in those coordinates, not from "
                    "uncertainty or lack of extra narrative detail. Return exactly "
                    "{\"decision\":\"no_op\"} or "
                    "{\"decision\":\"select\",\"candidate_token\":\"offered token\"}. "
                    "Do not invent an outcome, location, NPC, event id, or additional activity."
                )},
                {"role": "user", "content": json.dumps({
                    "authoritative_eligibility": {
                        "logical_time": logical_time.isoformat(),
                        "target_local_date": candidate.target_local_date.isoformat(),
                        "day_offset": candidate.day_offset,
                        "local_window": candidate.local_window,
                        "location_ref": candidate.location_ref,
                        "participant_ref": candidate.participant_ref,
                        "availability_hash": candidate.availability_hash,
                    },
                    "future_candidate": {
                        "token": candidate.token,
                        "activity_kind": candidate.opening.activity_kind,
                        "source": candidate.opening.source,
                        "domain": candidate.opening.domain,
                        "social_shape": candidate.opening.social_shape,
                        "deviation": candidate.opening.deviation,
                        "visual_potential": candidate.opening.visual_potential,
                        "privacy": candidate.opening.privacy,
                        "duration_minutes": candidate.opening.duration_minutes,
                        "importance_bp": candidate.opening.importance_bp,
                    }
                }, ensure_ascii=False, separators=(",", ":"))},
                ],
                temperature=0.2,
            )
        except (TimeoutError, ConnectionError, httpx.HTTPError) as exc:
            raise LifeAuthorModelFailure("future life author model provider is unavailable") from exc
        if not isinstance(raw, str) or len(raw.encode()) > 32_768:
            raise LifeAuthorModelFailure("future life author model response is not bounded text")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LifeAuthorModelFailure("future life author model response is not valid JSON") from exc
        if not isinstance(parsed, dict) or set(parsed) not in (
            {"decision"}, {"decision", "candidate_token"}
        ):
            raise LifeAuthorModelFailure("future life author model returned an invalid decision")
        try:
            decision = _FutureDecision.model_validate(parsed)
        except ValueError as exc:
            raise LifeAuthorModelFailure("future life author model returned an invalid decision") from exc
        if decision.candidate_token not in {None, candidate.token}:
            raise LifeAuthorModelFailure("future life author model selected an unoffered candidate")
        return decision, raw

    def _accept_plan(
        self, *, candidate: ReviewedLifeSeedFutureCandidate, wake_event_ref: str,
        suffix: str, trace_id: str, correlation_id: str,
    ) -> str:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        wake = next(
            item for item in projection.committed_world_event_refs
            if item.event_id == wake_event_ref and item.event_type == "ClockAdvanced"
        )
        clock_evidence = EvidenceRef(
            ref_id=wake.event_id,
            evidence_type="committed_world_event",
            claim_purpose="future_plan",
            source_world_revision=wake.world_revision,
            immutable_hash=wake.payload_hash,
        )
        opening = candidate.opening
        participant_refs = (
            (candidate.participant_ref,) if candidate.participant_ref is not None else ()
        )
        plan_event_id = "event:future-life-author-plan:" + suffix
        existing = self._ledger.lookup_event_commit(plan_event_id)
        if existing is not None:
            persisted = ActivityPlannedPayload.model_validate_json(existing[0].payload_json)
            if (
                persisted.plan.plan_id != "plan:future-life-author:" + suffix
                or persisted.plan.activity_id != "activity:future-life-author:" + suffix
            ):
                raise ValueError("future life author plan identity conflicts with durable content")
            return plan_event_id
        snapshot_payload = LifeAvailabilitySnapshotRecordedPayload(
            snapshot_id="availability:future-life-author:" + suffix,
            wake_event_ref=wake.event_id,
            wake_event_payload_hash=wake.payload_hash,
            wake_world_revision=wake.world_revision,
            candidate_token=candidate.token,
            catalog_version=self._catalog.version,
            catalog_hash=self._catalog.catalog_hash,
            owner_actor_ref=self._owner_actor_ref,
            location_ref=candidate.location_ref,
            participant_refs=participant_refs,
            availability_hash=candidate.availability_hash,
        )
        snapshot_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:future-life-availability:" + suffix,
            world_id=self._ledger.world_id,
            event_type="LifeAvailabilitySnapshotRecorded",
            logical_time=logical_time,
            created_at=logical_time,
            actor=self._actor,
            source="world-v2:future-life-author",
            trace_id=trace_id,
            causation_id=wake_event_ref,
            correlation_id=correlation_id,
            idempotency_key="future-life-availability:" + suffix,
            payload=snapshot_payload.model_dump(mode="json"),
        )
        snapshot_evidence = EvidenceRef(
            ref_id=snapshot_event.event_id,
            evidence_type="committed_world_event",
            claim_purpose="future_plan",
            source_world_revision=projection.world_revision + 1,
            immutable_hash=snapshot_event.payload_hash,
        )
        plan = PlanStateProjection(
            plan_id="plan:future-life-author:" + suffix,
            activity_id="activity:future-life-author:" + suffix,
            entity_revision=1,
            activity_kind=opening.activity_kind,
            evidence_refs=(clock_evidence, snapshot_evidence),
            status="planned",
            importance_bp=opening.importance_bp,
            scheduled_window=DueWindow(
                opens_at=candidate.opens_at,
                closes_at=candidate.closes_at,
            ),
            participant_refs=participant_refs,
            location_ref=candidate.location_ref,
            privacy_class=opening.privacy,
            owner_actor_ref=self._owner_actor_ref,
        )
        payload = ActivityPlannedPayload(
            change_id="change:future-life-author:" + suffix,
            transition_id="transition:future-life-author:" + suffix,
            expected_entity_revision=0,
            evidence_refs=(clock_evidence, snapshot_evidence),
            policy_refs=opening.policy_refs(catalog_version=self._catalog.version),
            plan=plan,
        ).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=plan_event_id,
            world_id=self._ledger.world_id,
            event_type="ActivityPlanned",
            logical_time=logical_time,
            created_at=logical_time,
            actor=self._actor,
            source="world-v2:future-life-author",
            trace_id=trace_id,
            causation_id=wake_event_ref,
            correlation_id=correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="ActivityPlanned", world_id=self._ledger.world_id, payload=payload
                ) or "future-life-author-plan:" + suffix
            ),
            payload=payload,
        )
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        self._ledger.commit_at_cursor(
            (snapshot_event, event), expected_cursor=cursor,
            commit_id="commit:future-life-author-plan:" + suffix
        )
        return event.event_id


class _FutureDecision(FrozenModel):
    decision: Literal["no_op", "select"]
    candidate_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def decision_is_closed(self) -> "_FutureDecision":
        if (self.decision == "select") != (self.candidate_token is not None):
            raise ValueError("future life author selection must bind exactly one candidate")
        return self


__all__ = [
    "FutureLifeAuthorResult",
    "FutureLifeAuthorRuntime",
    "FutureLifeAuthorWeightPolicy",
]
