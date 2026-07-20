"""First production Life Author vertical: reviewed opening -> clock-bound plan."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Literal, Protocol

import httpx
from pydantic import Field, model_validator

from .change_phase_view import change_phase_by_dimension, change_phase_readings
from .event_identity import domain_idempotency_key
from .life_author_seed import ReviewedLifeSeedCandidate, ReviewedLifeSeedCatalog
from .life_events import ActivityPlannedPayload
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import DueWindow, EvidenceRef, PlanStateProjection, ProjectionCursor, WorldEvent


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class LifeAuthorModel(Protocol):
    model: str

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


class LifeAuthorModelFailure(RuntimeError):
    """An explicit provider outage or invalid bounded model response."""


class LifeAuthorResult(FrozenModel):
    status: Literal["planned", "no_opening", "no_op", "blocked"]
    reason_code: str
    plan_event_ref: str | None = None
    draw_event_ref: str | None = None


class LifeAuthorWeightPolicy:
    """Compile generic, replayable preference mass from committed projections.

    ``life-author-weight.3`` adds a bounded mood prior: accepted, active
    Affect episodes gently shift which opening is *likely*, never which one
    is eligible.  The behaviour therefore has an inner-state cause without
    becoming a deterministic mood rule; the recorded draw keeps the exact
    weights, so replay never recomputes them.

    ``life-author-weight.4`` adds a bounded Change Phase prior on top: the
    *stage* of a feeling matters, not only its intensity.  Freshly departing
    heaviness pulls a bit harder toward restorative openings, while a visible
    return toward baseline restores some appetite for demanding and outgoing
    ones.  Both remain tendencies; recorded draws replay under their original
    versioned weights.
    """

    version = "life-author-weight.4"

    def __init__(self, *, recent_window: timedelta = timedelta(days=7)) -> None:
        if recent_window <= timedelta(0):
            raise ValueError("life author recent window must be positive")
        self._recent_window = recent_window

    def compile(
        self, *, candidates: tuple[ReviewedLifeSeedCandidate, ...],
        plans: tuple[object, ...], logical_time: datetime,
        recent_domain_by_activity: dict[str, str] | None = None,
        affect_episodes: tuple[object, ...] = (),
    ) -> dict[str, int]:
        recent = tuple(
            plan for plan in plans
            if self._is_recent(plan=plan, logical_time=logical_time)
        )
        recent_social_count = sum(
            bool(getattr(plan, "participant_refs", ())) for plan in recent
        )
        previous_domain = self._latest_domain(
            recent=recent,
            domain_by_activity=recent_domain_by_activity or {},
        )
        mood = self._mood_intensities(affect_episodes)
        try:
            phases = change_phase_by_dimension(
                change_phase_readings(tuple(affect_episodes), logical_time=logical_time)
            )
        except (TypeError, ValueError):
            # Phase advice is a best-effort prior; a malformed episode must
            # not block life authoring, it only loses the stage nuance.
            phases = {}
        weights: dict[str, int] = {}
        for candidate in candidates:
            same_kind_count = sum(
                getattr(plan, "activity_kind", None)
                == candidate.opening.activity_kind
                for plan in recent
            )
            mass = max(1_000, candidate.opening.importance_bp)
            mass = max(1, mass * candidate.daypart_fit_bp // 10_000)
            mass = max(1, mass // (1 + same_kind_count))
            if candidate.participant_ref is not None and recent_social_count == 0:
                mass = max(1, mass * 3 // 2)
            mass = max(
                1,
                mass * self._rhythm_multiplier_bp(
                    previous_domain=previous_domain,
                    candidate_domain=candidate.opening.domain,
                ) // 10_000,
            )
            mass = max(
                1,
                mass * self._mood_multiplier_bp(
                    mood=mood, candidate_domain=candidate.opening.domain
                ) // 10_000,
            )
            mass = max(
                1,
                mass * self._change_phase_multiplier_bp(
                    phases=phases, candidate_domain=candidate.opening.domain
                ) // 10_000,
            )
            weights[candidate.token] = mass
        return weights

    @staticmethod
    def _mood_intensities(affect_episodes: tuple[object, ...]) -> dict[str, int]:
        """Aggregate active accepted Affect components to one bounded reading."""

        intensities: dict[str, int] = {}
        for episode in affect_episodes:
            if getattr(episode, "status", None) != "active":
                continue
            for component in getattr(episode, "components", ()):  # type: ignore[attr-defined]
                dimension = str(getattr(component, "dimension", ""))
                intensity = getattr(component, "intensity_bp", 0)
                if isinstance(intensity, int) and 0 <= intensity <= 10_000:
                    intensities[dimension] = max(intensities.get(dimension, 0), intensity)
        return intensities

    @staticmethod
    def _mood_multiplier_bp(*, mood: dict[str, int], candidate_domain: str) -> int:
        """Return a gentle mood-congruent prior in basis points.

        The shift is capped at roughly +/-35% at full affect intensity and is
        deliberately a *tendency*: a drained companion still sometimes goes to
        the library, she is just less likely to.  All arithmetic is integer so
        the recorded draw is exactly reproducible.
        """

        if not mood:
            return 10_000
        heaviness = max(
            mood.get("sadness", 0), mood.get("hurt", 0), mood.get("anxiety", 0),
            mood.get("anger", 0), mood.get("resentment", 0),
        )
        loneliness = mood.get("loneliness", 0)
        brightness = max(mood.get("joy", 0), mood.get("warmth", 0))
        multiplier = 10_000
        restorative = {"rest_recovery", "sleep_wake", "digital_leisure"}
        demanding = {"study_class", "creative_photo_writing", "errand_household"}
        outgoing = {"commute_walk", "creative_photo_writing", "family_roommate_friend"}
        social = {"family_roommate_friend"}
        if candidate_domain in restorative:
            multiplier += heaviness * 3_500 // 10_000
        if candidate_domain in demanding:
            multiplier -= heaviness * 3_000 // 10_000
        if candidate_domain in outgoing:
            multiplier += brightness * 2_500 // 10_000
        if candidate_domain in social:
            # Loneliness reaches toward company; heaviness that is not
            # loneliness (hurt, anger) pulls slightly away from it instead.
            multiplier += loneliness * 3_000 // 10_000
            multiplier -= max(0, heaviness - loneliness) * 1_500 // 10_000
        return max(4_000, min(16_000, multiplier))

    @staticmethod
    def _change_phase_multiplier_bp(*, phases: dict[str, str], candidate_domain: str) -> int:
        """Return a gentle stage-of-feeling prior in basis points.

        Capped at roughly +/-20%: "刚陷入低落" leans a bit further into rest
        than the same intensity mid-recovery, and "正在走出" quietly restores
        appetite for demanding or outgoing openings.  Integer arithmetic only,
        so the recorded draw replays exactly.
        """

        if not phases:
            return 10_000
        heavy = ("sadness", "hurt", "anxiety", "anger", "resentment", "loneliness")
        departing_heavy = any(phases.get(dimension) == "departing" for dimension in heavy)
        returning_heavy = any(
            phases.get(dimension) in {"returning", "recovering"} for dimension in heavy
        ) and not departing_heavy
        restorative = {"rest_recovery", "sleep_wake", "digital_leisure"}
        demanding = {"study_class", "creative_photo_writing", "errand_household"}
        outgoing = {"commute_walk", "creative_photo_writing", "family_roommate_friend"}
        multiplier = 10_000
        if departing_heavy:
            if candidate_domain in restorative:
                multiplier += 1_500
            if candidate_domain in demanding or candidate_domain in outgoing:
                multiplier -= 1_500
        elif returning_heavy:
            if candidate_domain in demanding or candidate_domain in outgoing:
                multiplier += 1_200
        return max(8_000, min(12_000, multiplier))

    @staticmethod
    def _latest_domain(
        *, recent: tuple[object, ...], domain_by_activity: dict[str, str],
    ) -> str | None:
        ordered = sorted(
            recent,
            key=lambda plan: getattr(
                getattr(plan, "authority_origin", None), "accepted_at"
            ),
            reverse=True,
        )
        for plan in ordered:
            domain = domain_by_activity.get(str(getattr(plan, "activity_kind", "")))
            if domain is not None:
                return domain
        return None

    @staticmethod
    def _rhythm_multiplier_bp(
        *, previous_domain: str | None, candidate_domain: str,
    ) -> int:
        """Return a soft phase-transition prior, never an eligibility rule.

        The matrix operates on broad life domains rather than named activities.
        It gently alternates sustained focus with movement or recovery while
        retaining meaningful mass for repetition and every other transition.
        """

        focus = {"study_class", "creative_photo_writing"}
        restorative = {"commute_walk", "rest_recovery", "sleep_wake"}
        if previous_domain in focus and candidate_domain in restorative:
            return 12_500
        if previous_domain in focus and candidate_domain in focus:
            return 8_500
        if previous_domain in restorative and candidate_domain in focus:
            return 11_000
        return 10_000

    def _is_recent(self, *, plan: object, logical_time: datetime) -> bool:
        accepted_at = getattr(
            getattr(plan, "authority_origin", None), "accepted_at", None
        )
        return (
            isinstance(accepted_at, datetime)
            and accepted_at.tzinfo is not None
            and accepted_at.utcoffset() is not None
            and logical_time - self._recent_window <= accepted_at <= logical_time
        )


class _Decision(FrozenModel):
    decision: Literal["no_op", "select"]
    candidate_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def decision_is_closed(self) -> "_Decision":
        if (self.decision == "select") != (self.candidate_token is not None):
            raise ValueError("life author selection must bind exactly one candidate")
        return self


class LifeAuthorDecisionRecordedPayload(FrozenModel):
    decision_id: str = Field(min_length=1, max_length=256)
    attempt_id: str = Field(min_length=1, max_length=256)
    wake_event_ref: str = Field(min_length=1)
    wake_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    wake_world_revision: int = Field(ge=1)
    draw_event_ref: str = Field(min_length=1)
    draw_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    draw_world_revision: int = Field(ge=1)
    candidate_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=128)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["no_op", "select"]
    selected_candidate_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=256)
    raw_output_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def selection_is_bound_to_the_drawn_candidate(self) -> "LifeAuthorDecisionRecordedPayload":
        if self.decision == "select":
            if self.selected_candidate_token != self.candidate_token:
                raise ValueError("life author decision selected a different candidate")
        elif self.selected_candidate_token is not None:
            raise ValueError("life author no-op cannot select a candidate")
        return self


class LifeAvailabilitySnapshotRecordedPayload(FrozenModel):
    """Exact reviewed location/social availability used by one plan decision."""

    snapshot_id: str = Field(min_length=1, max_length=256)
    wake_event_ref: str = Field(min_length=1)
    wake_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    wake_world_revision: int = Field(ge=1)
    candidate_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=128)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    owner_actor_ref: str = Field(min_length=1)
    location_ref: str | None = Field(default=None, min_length=1)
    participant_refs: tuple[str, ...] = ()
    availability_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def participants_are_registered_npc_refs(self) -> "LifeAvailabilitySnapshotRecordedPayload":
        if self.participant_refs != tuple(sorted(set(self.participant_refs))):
            raise ValueError("availability snapshot participants must be unique and canonical")
        if any(not item.startswith("npc:") for item in self.participant_refs):
            raise ValueError("life author availability snapshot accepts registered NPC refs only")
        return self


class LifeAuthorRuntime:
    """Own candidate compilation, recorded draw, bounded choice, and plan acceptance.

    The sole public operation accepts a committed clock ref.  The caller does
    not provide activity, time-of-day, random seed, plan identity, evidence,
    or matrix coordinates.
    """

    def __init__(
        self, *, ledger, catalog: ReviewedLifeSeedCatalog, model: LifeAuthorModel,
        owner_actor_ref: str, actor: str = "worker:world-v2:life-author",
    ) -> None:
        if not owner_actor_ref or not actor:
            raise ValueError("life author requires owner and worker actors")
        self._ledger = ledger
        self._catalog = catalog
        self._model = model
        self._owner_actor_ref = owner_actor_ref
        self._actor = actor
        self._random = RandomAuthority(ledger=ledger, source="world-v2:life-author-random")
        self._weight_policy = LifeAuthorWeightPolicy()

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> LifeAuthorResult:
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
            return LifeAuthorResult(
                status="blocked", reason_code="life_author.wake_not_exact_clock"
            )
        owner_plans = tuple(
            plan for plan in projection.plans
            if plan.owner_actor_ref == self._owner_actor_ref
        )
        candidates = self._catalog.candidates_at(
            instant=wake.logical_time,
            wake_event_ref=wake_event_ref,
            plans=owner_plans,
            npcs=projection.npcs,
        )
        if not candidates:
            return LifeAuthorResult(
                status="no_opening", reason_code="life_author.no_eligible_opening"
            )
        attempt_id = "attempt:life-author:" + _digest({
            "world_id": self._ledger.world_id,
            "wake_event_ref": wake_event_ref,
            "catalog_version": self._catalog.version,
            "catalog_hash": self._catalog.catalog_hash,
        })
        draw = self._random.draw(
            attempt_id=attempt_id,
            candidate_refs=tuple(item.token for item in candidates),
            catalog_version=self._catalog.version,
            logical_time=logical_time,
            seed_instant=wake.logical_time,
            actor=self._actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
            candidate_weights=self._weight_policy.compile(
                candidates=candidates, plans=owner_plans, logical_time=logical_time,
                recent_domain_by_activity=self._catalog.activity_domains,
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
            return LifeAuthorResult(
                status="blocked", reason_code="life_author.model_unavailable",
                draw_event_ref=draw_event_ref,
            )
        if decision.decision == "no_op":
            return LifeAuthorResult(
                status="no_op", reason_code="life_author.model_declined",
                draw_event_ref=draw_event_ref,
            )
        assert decision.candidate_token == selected.token
        if logical_time >= wake.logical_time + timedelta(
            minutes=selected.opening.duration_minutes
        ):
            return LifeAuthorResult(
                status="blocked",
                reason_code="life_author.selected_opening_expired_before_acceptance",
                draw_event_ref=draw_event_ref,
            )
        event_ref = self._accept_plan(
            candidate=selected, wake_event_ref=wake_event_ref, logical_time=logical_time,
            scheduled_from=wake.logical_time,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        return LifeAuthorResult(
            status="planned", reason_code="life_author.plan_accepted",
            plan_event_ref=event_ref, draw_event_ref=draw_event_ref,
        )

    async def _decision_once(
        self, *, candidate: ReviewedLifeSeedCandidate, attempt_id: str, wake,
        draw_event_ref: str, trace_id: str, correlation_id: str,
    ) -> _Decision:
        decision_id = "decision:life-author:" + _digest({
            "attempt_id": attempt_id, "candidate_token": candidate.token
        })
        event_id = "event:life-author-decision:" + _digest(decision_id)
        existing = self._ledger.lookup_event_commit(event_id)
        if existing is not None:
            payload = LifeAuthorDecisionRecordedPayload.model_validate_json(existing[0].payload_json)
            return _Decision(
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
            source="world-v2:life-author",
            trace_id=trace_id,
            causation_id=draw_event_ref,
            correlation_id=correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="LifeAuthorDecisionRecorded",
                    world_id=self._ledger.world_id,
                    payload=payload.model_dump(mode="json"),
                ) or "life-author-decision:" + _digest({
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
        self, candidate: ReviewedLifeSeedCandidate, *, logical_time: datetime
    ) -> tuple[_Decision, str]:
        try:
            raw = await self._model.complete(
                [
                {"role": "system", "content": (
                    "You are the final semantic veto for one reviewed abstract life opening. The host has "
                    "already verified its local-time window, daily frequency, plan overlap, location, NPC "
                    "availability, privacy, and controlled-random selection. This is not a choice between "
                    "having a life and staying empty. Select the offered opening when its supplied coordinates "
                    "are coherent; use no_op only for a concrete semantic contradiction visible in those "
                    "coordinates, not from uncertainty or lack of extra narrative detail. Return exactly "
                    "{\"decision\":\"no_op\"} or "
                    "{\"decision\":\"select\",\"candidate_token\":\"offered token\"}. "
                    "Do not invent an outcome, location, NPC, event id, or additional activity."
                )},
                {"role": "user", "content": json.dumps({
                    "authoritative_eligibility": {
                        "logical_time": logical_time.isoformat(),
                        "daypart_fit_bp": candidate.daypart_fit_bp,
                        "location_ref": candidate.location_ref,
                        "participant_ref": candidate.participant_ref,
                        "availability_hash": candidate.availability_hash,
                    },
                    "candidate": {
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
            raise LifeAuthorModelFailure("life author model provider is unavailable") from exc
        if not isinstance(raw, str) or len(raw.encode()) > 32_768:
            raise LifeAuthorModelFailure("life author model response is not bounded text")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LifeAuthorModelFailure("life author model response is not valid JSON") from exc
        if not isinstance(parsed, dict) or set(parsed) not in (
            {"decision"}, {"decision", "candidate_token"}
        ):
            raise LifeAuthorModelFailure("life author model returned an invalid decision")
        try:
            decision = _Decision.model_validate(parsed)
        except ValueError as exc:
            raise LifeAuthorModelFailure("life author model returned an invalid decision") from exc
        if decision.candidate_token not in {None, candidate.token}:
            raise LifeAuthorModelFailure("life author model selected an unoffered candidate")
        return decision, raw

    def _accept_plan(
        self, *, candidate: ReviewedLifeSeedCandidate, wake_event_ref: str,
        logical_time: datetime, scheduled_from: datetime,
        trace_id: str, correlation_id: str,
    ) -> str:
        projection = self._ledger.project()
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
        suffix = _digest({
            "world_id": self._ledger.world_id,
            "wake_event_ref": wake_event_ref,
            "candidate_token": candidate.token,
        })
        opening = candidate.opening
        participant_refs = (
            (candidate.participant_ref,) if candidate.participant_ref is not None else ()
        )
        plan_event_id = "event:life-author-plan:" + suffix
        existing = self._ledger.lookup_event_commit(plan_event_id)
        if existing is not None:
            persisted = ActivityPlannedPayload.model_validate_json(existing[0].payload_json)
            if (
                persisted.plan.plan_id != "plan:life-author:" + suffix
                or persisted.plan.activity_id != "activity:life-author:" + suffix
                or persisted.plan.activity_kind != opening.activity_kind
                or persisted.plan.location_ref != candidate.location_ref
                or persisted.plan.participant_refs != participant_refs
                or not persisted.evidence_refs
                or persisted.evidence_refs[0] != clock_evidence
            ):
                raise ValueError("life author plan identity conflicts with durable content")
            return plan_event_id
        snapshot_payload = LifeAvailabilitySnapshotRecordedPayload(
            snapshot_id="availability:life-author:" + suffix,
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
            event_id="event:life-availability:" + suffix,
            world_id=self._ledger.world_id,
            event_type="LifeAvailabilitySnapshotRecorded",
            logical_time=logical_time,
            created_at=logical_time,
            actor=self._actor,
            source="world-v2:life-author",
            trace_id=trace_id,
            causation_id=wake_event_ref,
            correlation_id=correlation_id,
            idempotency_key="life-availability:" + suffix,
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
            plan_id="plan:life-author:" + suffix,
            activity_id="activity:life-author:" + suffix,
            entity_revision=1,
            activity_kind=opening.activity_kind,
            evidence_refs=(clock_evidence, snapshot_evidence),
            status="planned",
            importance_bp=opening.importance_bp,
            scheduled_window=DueWindow(
                opens_at=scheduled_from,
                closes_at=scheduled_from + timedelta(minutes=opening.duration_minutes),
            ),
            participant_refs=participant_refs,
            location_ref=candidate.location_ref,
            privacy_class=opening.privacy,
            owner_actor_ref=self._owner_actor_ref,
        )
        payload = ActivityPlannedPayload(
            change_id="change:life-author:" + suffix,
            transition_id="transition:life-author:" + suffix,
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
            source="world-v2:life-author",
            trace_id=trace_id,
            causation_id=wake_event_ref,
            correlation_id=correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="ActivityPlanned", world_id=self._ledger.world_id, payload=payload
                ) or "life-author-plan:" + suffix
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
            commit_id="commit:life-author-plan:" + suffix
        )
        return event.event_id


__all__ = [
    "LifeAvailabilitySnapshotRecordedPayload",
    "LifeAuthorDecisionRecordedPayload",
    "LifeAuthorModel",
    "LifeAuthorModelFailure",
    "LifeAuthorResult",
    "LifeAuthorRuntime",
    "LifeAuthorWeightPolicy",
]
