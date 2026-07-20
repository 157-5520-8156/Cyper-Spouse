"""NPC light autonomy: a reviewed NPC may enter her day uninvited.

This is the unplanned-event lane's NPC-bound sibling.  ``OpenWorldEventRuntime``
already proved the downstream shape — a plan-less WorldOccurrence committed and
activated from one quiet wake, then settled by the ordinary
``LifeAftermathRuntime`` path (settlement → mandatory ``npc_world_appraisal``
trigger → Committed Experience → life content).  This runtime reuses exactly
that downstream and replaces the upstream with the reviewed discipline the NPC
lane needs:

* candidates come only from the reviewed ``npc_initiated_events`` seed section
  (anti-fabrication), never from an active plan or model prose;
* whether anything happens is a recorded ``RandomAuthority`` draw over the
  eligible events *plus one always-legal "nothing" candidate*, whose weights
  are ``base_chance_bp`` tilted by relationship warmth / unresolved friction /
  loneliness (a bounded tendency, never a gate);
* a drawn event still needs the bounded model's semantic confirmation using
  the life author's exact select/no_op JSON contract — "范予安今天没来找她"
  is a permanently legitimate answer;
* frequency is host-owned: each companion-local day has at most two check
  slots (morning / afternoon) and at most one occurrence, with all identities
  encoding the local date so every wake of one day converges instead of
  re-rolling.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal

import httpx

from .event_identity import domain_idempotency_key
from .life_author_runtime import LifeAuthorModel, LifeAuthorModelFailure
from .life_author_seed import NpcInitiativeCandidate, ReviewedLifeSeedCatalog
from .life_content_store import life_content_payload_hash
from .life_events import WorldOccurrenceActivatedPayload
from .mood_view import active_mood_intensities
from .npc_relationship_view import (
    RESTING_CLOSENESS_BP,
    NpcRelationshipReading,
    npc_relationship_by_ref,
    npc_relationship_readings,
)
from .occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OccurrenceContentCoordinator,
    OutcomeCandidateContent,
)
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import DueWindow, EvidenceRef, ProjectionCursor, WorldEvent, WorldOccurrenceProjection

_POLICY = "policy:npc-initiative.1"

# One ledger-recorded "nothing happened" candidate shares the draw with every
# eligible event, so not-happening is a first-class replayable outcome rather
# than an absence of evidence.
NOTHING_CANDIDATE_REF = "nothing:npc-initiative"

# The second daily check slot opens at this companion-local hour.  Two slots
# per day give "一两次检查机会" without letting the first two scheduler wakes
# of a morning burn the whole budget within minutes of each other.
_AFTERNOON_SLOT_HOUR = 14


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class NpcInitiativeResult(FrozenModel):
    status: Literal[
        "committed", "recovered", "no_op", "no_candidates",
        "already_occurred", "slot_consumed", "blocked",
    ]
    reason_code: str
    check_event_ref: str | None = None
    draw_event_ref: str | None = None
    occurrence_id: str | None = None


class NpcInitiativeWeightPolicy:
    """Replayable per-check probability mass for NPC-initiated events.

    ``npc-initiative-weight.1`` started from the reviewed ``base_chance_bp``
    of each eligible event inside a 10_000 probability space shared with the
    nothing candidate, tilted — never gated — by accepted inner state, with
    accepted Affect standing in for the missing per-NPC relationship reading.

    ``npc-initiative-weight.2`` adds that reading: the derived per-NPC
    relationship projection (committed shared history and unresolved
    ``npc_conflict`` appraisals) now tilts the exact NPC whose event is being
    weighed — someone she has grown close to comes by a bit more readily,
    live friction with *that* person raises the disagreement events — while
    the mood reading keeps its original direction-preserving role.

    The combined multiplier is clamped to +/-40% and all arithmetic is
    integer, so the recorded draw replays exactly.
    """

    version = "npc-initiative-weight.2"

    def compile(
        self, *, candidates: tuple[NpcInitiativeCandidate, ...],
        affect_episodes: tuple[object, ...] = (),
        npc_relationships: tuple[NpcRelationshipReading, ...] = (),
    ) -> dict[str, int]:
        mood = active_mood_intensities(affect_episodes)
        relationships = npc_relationship_by_ref(npc_relationships)
        weights: dict[str, int] = {}
        total = 0
        for candidate in candidates:
            multiplier = self._multiplier_bp(
                mood=mood,
                initiative_kind=candidate.event.initiative_kind,
                relationship=relationships.get(candidate.npc_ref),
            )
            mass = max(1, candidate.event.base_chance_bp * multiplier // 10_000)
            weights[candidate.token] = mass
            total += mass
        # The nothing candidate absorbs the remaining reviewed probability
        # space.  A reviewed catalog whose bases sum past 10_000 (test seeds)
        # may drive it to zero mass; the model's no_op keeps "nothing
        # happened" legitimate even then.
        weights[NOTHING_CANDIDATE_REF] = max(10_000 - total, 0)
        return weights

    @staticmethod
    def _multiplier_bp(
        *, mood: dict[str, int], initiative_kind: str,
        relationship: NpcRelationshipReading | None = None,
    ) -> int:
        multiplier = 10_000
        if mood:
            warmth = mood.get("warmth", 0)
            loneliness = mood.get("loneliness", 0)
            unresolved = max(mood.get("resentment", 0), mood.get("anger", 0))
            # Loneliness reaches toward any of the NPC's appearances.
            multiplier += loneliness * 2_500 // 10_000
            if initiative_kind == "shared_time":
                multiplier += warmth * 2_000 // 10_000
            if initiative_kind == "small_favor":
                multiplier += warmth * 1_000 // 10_000
            if initiative_kind == "friction":
                # 微升: unresolved friction makes a disagreement slightly
                # easier to surface, but never guarantees one.
                multiplier += unresolved * 1_500 // 10_000
        if relationship is not None:
            # This NPC's own derived reading, always a tendency: warmth above
            # the resting point invites their company, live friction raises
            # the chance of exactly the disagreement events.
            closeness_delta = relationship.closeness_bp - RESTING_CLOSENESS_BP
            if initiative_kind in {"shared_time", "small_favor"}:
                multiplier += closeness_delta * 2_000 // 10_000
            if initiative_kind == "friction":
                multiplier += relationship.friction_bp * 2_000 // 10_000
                multiplier -= max(0, closeness_delta) * 500 // 10_000
        return max(6_000, min(14_000, multiplier))


class NpcInitiativeRuntime:
    """Own the daily check budget, recorded draw, bounded confirmation, and
    occurrence commit for NPC-initiated events.

    The sole public operation accepts a committed clock ref, exactly like the
    life author lanes.  The caller provides no event, probability, identity,
    or evidence.  Settlement is deliberately *not* here: the committed and
    activated occurrence is settled by the existing ``LifeAftermathRuntime``
    on a later wake, which owns the mood-aware outcome selection, the
    mandatory ``npc_world_appraisal`` trigger, the Committed Experience, and
    the life content record.
    """

    def __init__(
        self, *, ledger, catalog: ReviewedLifeSeedCatalog, model: LifeAuthorModel,
        occurrence_content: OccurrenceContentCoordinator, owner_actor_ref: str,
        actor: str = "worker:world-v2:npc-initiative",
    ) -> None:
        if not owner_actor_ref or not actor:
            raise ValueError("npc initiative requires owner and worker actors")
        if occurrence_content.ledger is not ledger:
            raise ValueError("npc initiative occurrence coordinator must own the exact ledger")
        self._ledger = ledger
        self._catalog = catalog
        self._model = model
        self._occurrence_content = occurrence_content
        self._owner_actor_ref = owner_actor_ref
        self._actor = actor
        self._random = RandomAuthority(ledger=ledger, source="world-v2:npc-initiative-random")
        self._weight_policy = NpcInitiativeWeightPolicy()

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> NpcInitiativeResult:
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
            return NpcInitiativeResult(
                status="blocked", reason_code="npc_initiative.wake_not_exact_clock"
            )
        local = self._catalog.localize(wake.logical_time)
        local_date_iso = local.date().isoformat()
        slot = 0 if local.hour < _AFTERNOON_SLOT_HOUR else 1

        occurrence_id = "occurrence:npc-initiative:" + _digest({
            "world_id": self._ledger.world_id, "local_date": local_date_iso,
        })
        existing_occurrence = next(
            (item for item in projection.world_occurrences if item.occurrence_id == occurrence_id),
            None,
        )
        check_event_id = self._check_event_id(local_date_iso, slot)
        existing_check = self._check_event(check_event_id)

        if existing_occurrence is not None:
            if existing_occurrence.status == "committed":
                # Crash between commit and activation: finish the activation
                # so the aftermath path can settle it on a later wake.
                self._activate(
                    occurrence_id=occurrence_id, wake=wake,
                    trace_id=trace_id, correlation_id=correlation_id,
                )
                return NpcInitiativeResult(
                    status="recovered", reason_code="npc_initiative.activation_recovered",
                    occurrence_id=occurrence_id,
                    check_event_ref=existing_check.event_id if existing_check else None,
                )
            return NpcInitiativeResult(
                status="already_occurred",
                reason_code="npc_initiative.local_day_already_occurred",
                occurrence_id=occurrence_id,
            )

        if existing_check is not None:
            payload = existing_check.payload()
            if payload.get("decision") == "selected":
                # Crash between the recorded selection and the occurrence
                # commit: recover the exact durable choice.
                candidates = self._catalog.npc_initiative_candidates_at(
                    instant=wake.logical_time, npcs=projection.npcs
                )
                selected = next(
                    (item for item in candidates if item.token == payload.get("candidate_token")),
                    None,
                )
                if selected is None:
                    return NpcInitiativeResult(
                        status="blocked",
                        reason_code="npc_initiative.selected_candidate_stale",
                        check_event_ref=existing_check.event_id,
                    )
                self._commit_occurrence(
                    occurrence_id=occurrence_id, candidate=selected, wake=wake,
                    check_event=existing_check, trace_id=trace_id, correlation_id=correlation_id,
                )
                self._activate(
                    occurrence_id=occurrence_id, wake=wake,
                    trace_id=trace_id, correlation_id=correlation_id,
                )
                return NpcInitiativeResult(
                    status="recovered", reason_code="npc_initiative.occurrence_recovered",
                    occurrence_id=occurrence_id, check_event_ref=existing_check.event_id,
                )
            return NpcInitiativeResult(
                status="slot_consumed", reason_code="npc_initiative.check_slot_consumed",
                check_event_ref=existing_check.event_id,
            )

        candidates = self._catalog.npc_initiative_candidates_at(
            instant=wake.logical_time, npcs=projection.npcs
        )
        if not candidates:
            # An empty world (NPC absent, no reviewed window) never consumes a
            # check slot: checks are chances against a possible world only.
            return NpcInitiativeResult(
                status="no_candidates", reason_code="npc_initiative.no_eligible_event"
            )

        attempt_id = "attempt:npc-initiative:" + _digest({
            "world_id": self._ledger.world_id,
            "local_date": local_date_iso,
            "check_slot": slot,
            "catalog_version": self._catalog.version,
            "catalog_hash": self._catalog.catalog_hash,
        })
        draw = self._random.draw(
            attempt_id=attempt_id,
            candidate_refs=(*(item.token for item in candidates), NOTHING_CANDIDATE_REF),
            catalog_version=self._catalog.version,
            logical_time=logical_time,
            seed_instant=wake.logical_time,
            actor=self._actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
            candidate_weights=self._weight_policy.compile(
                candidates=candidates,
                affect_episodes=projection.affect_episodes,
                npc_relationships=npc_relationship_readings(projection),
            ),
            weight_policy_version=self._weight_policy.version,
        )
        draw_event_ref = "event:random-draw:" + draw.draw_id
        if draw.selected_candidate_ref == NOTHING_CANDIDATE_REF:
            check_event = self._record_check(
                check_event_id=check_event_id, local_date_iso=local_date_iso, slot=slot,
                decision="nothing", wake=wake, draw_event_ref=draw_event_ref,
                candidate_token=None, reviewed_event_id=None,
                model="random-authority", raw_output=draw.selected_candidate_ref,
                trace_id=trace_id, correlation_id=correlation_id,
            )
            return NpcInitiativeResult(
                status="no_op", reason_code="npc_initiative.nothing_drawn",
                check_event_ref=check_event.event_id, draw_event_ref=draw_event_ref,
            )
        selected = next(item for item in candidates if item.token == draw.selected_candidate_ref)
        try:
            decision, raw = await self._deliberate(selected, wake=wake)
        except LifeAuthorModelFailure:
            # Model outage does not consume the check slot; a later wake of
            # the same slot replays the identical durable draw and retries.
            return NpcInitiativeResult(
                status="blocked", reason_code="npc_initiative.model_unavailable",
                draw_event_ref=draw_event_ref,
            )
        if decision == "no_op":
            check_event = self._record_check(
                check_event_id=check_event_id, local_date_iso=local_date_iso, slot=slot,
                decision="no_op", wake=wake, draw_event_ref=draw_event_ref,
                candidate_token=selected.token, reviewed_event_id=selected.event.id,
                model=self._model_id(), raw_output=raw,
                trace_id=trace_id, correlation_id=correlation_id,
            )
            return NpcInitiativeResult(
                status="no_op", reason_code="npc_initiative.model_declined",
                check_event_ref=check_event.event_id, draw_event_ref=draw_event_ref,
            )
        check_event = self._record_check(
            check_event_id=check_event_id, local_date_iso=local_date_iso, slot=slot,
            decision="selected", wake=wake, draw_event_ref=draw_event_ref,
            candidate_token=selected.token, reviewed_event_id=selected.event.id,
            model=self._model_id(), raw_output=raw,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        self._commit_occurrence(
            occurrence_id=occurrence_id, candidate=selected, wake=wake,
            check_event=check_event, trace_id=trace_id, correlation_id=correlation_id,
        )
        self._activate(
            occurrence_id=occurrence_id, wake=wake,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        return NpcInitiativeResult(
            status="committed", reason_code="npc_initiative.occurrence_committed",
            occurrence_id=occurrence_id, check_event_ref=check_event.event_id,
            draw_event_ref=draw_event_ref,
        )

    async def _deliberate(
        self, candidate: NpcInitiativeCandidate, *, wake
    ) -> tuple[Literal["select", "no_op"], str]:
        """The life author's exact bounded select/no_op JSON contract."""

        try:
            raw = await self._model.complete(
                [
                    {"role": "system", "content": (
                        "You are the final semantic confirmation for one reviewed NPC-initiated moment. "
                        "The host has already verified the NPC's reviewed presence window, the location, "
                        "privacy, the daily frequency budget, and the controlled-random occurrence draw. "
                        "Select the offered moment when its supplied coordinates are coherent; return no_op "
                        "when this moment does not ring true right now — the NPC simply not coming by today "
                        "is always a legitimate outcome. Return exactly {\"decision\":\"no_op\"} or "
                        "{\"decision\":\"select\",\"candidate_token\":\"offered token\"}. "
                        "Do not invent an outcome, person, place, time, event id, or additional activity."
                    )},
                    {"role": "user", "content": json.dumps({
                        "authoritative_eligibility": {
                            "logical_time": wake.logical_time.isoformat(),
                            "npc_ref": candidate.npc_ref,
                            "location_ref": candidate.location_ref,
                            "availability_hash": candidate.availability_hash,
                        },
                        "npc_initiative_candidate": {
                            "token": candidate.token,
                            "initiative_kind": candidate.event.initiative_kind,
                            "summary": candidate.event.summary,
                            "privacy": candidate.event.privacy,
                            "duration_minutes": candidate.event.duration_minutes,
                            "base_chance_bp": candidate.event.base_chance_bp,
                        },
                    }, ensure_ascii=False, separators=(",", ":"))},
                ],
                temperature=0.2,
            )
        except (TimeoutError, ConnectionError, OSError, httpx.HTTPError) as exc:
            raise LifeAuthorModelFailure("npc initiative model provider is unavailable") from exc
        if not isinstance(raw, str) or len(raw.encode()) > 32_768:
            raise LifeAuthorModelFailure("npc initiative model response is not bounded text")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LifeAuthorModelFailure("npc initiative model response is not valid JSON") from exc
        if not isinstance(parsed, dict) or set(parsed) not in (
            {"decision"}, {"decision", "candidate_token"}
        ):
            raise LifeAuthorModelFailure("npc initiative model returned an invalid decision")
        decision = parsed.get("decision")
        if decision == "no_op":
            if "candidate_token" in parsed:
                raise LifeAuthorModelFailure("npc initiative no_op cannot select a candidate")
            return "no_op", raw
        if decision != "select" or parsed.get("candidate_token") != candidate.token:
            raise LifeAuthorModelFailure("npc initiative model selected an unoffered candidate")
        return "select", raw

    def _record_check(
        self, *, check_event_id: str, local_date_iso: str, slot: int,
        decision: Literal["nothing", "no_op", "selected"], wake, draw_event_ref: str,
        candidate_token: str | None, reviewed_event_id: str | None,
        model: str, raw_output: str, trace_id: str, correlation_id: str,
    ) -> WorldEvent:
        projection = self._ledger.project()
        payload = {
            "proposal_id": "proposal:npc-initiative:" + _digest({
                "world_id": self._ledger.world_id,
                "local_date": local_date_iso,
                "check_slot": slot,
            }),
            "proposal_kind": "npc_initiative",
            "decision": decision,
            "check_local_date": local_date_iso,
            "check_slot": slot,
            "trigger_id": wake.event_id,
            "evaluated_world_revision": projection.world_revision,
            "wake_event_ref": wake.event_id,
            "wake_event_payload_hash": wake.payload_hash,
            "draw_event_ref": draw_event_ref,
            "candidate_token": candidate_token,
            "reviewed_event_id": reviewed_event_id,
            "catalog_version": self._catalog.version,
            "catalog_hash": self._catalog.catalog_hash,
            "model": model,
            "raw_output_hash": "sha256:" + hashlib.sha256(raw_output.encode()).hexdigest(),
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=check_event_id,
            event_type="ProposalRecorded",
            world_id=self._ledger.world_id,
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:npc-initiative",
            trace_id=trace_id,
            causation_id=draw_event_ref,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload
            ) or "npc-initiative-check:" + _digest(check_event_id),
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=_cursor(projection),
            commit_id="commit:npc-initiative:check:" + _digest(check_event_id),
        )
        return event

    def _commit_occurrence(
        self, *, occurrence_id: str, candidate: NpcInitiativeCandidate, wake,
        check_event: WorldEvent, trace_id: str, correlation_id: str,
    ) -> None:
        projection = self._ledger.project()
        if any(item.occurrence_id == occurrence_id for item in projection.world_occurrences):
            return
        suffix = occurrence_id.removeprefix("occurrence:npc-initiative:")
        event = candidate.event
        candidate_contents = tuple(
            OutcomeCandidateContent(
                candidate_result_ref=f"candidate:npc-initiative:{suffix}:{item.id}",
                result_id=f"result:npc-initiative:{suffix}:{item.id}",
                result_payload_ref=f"content:npc-initiative-result:{suffix}:{item.id}",
                result_payload_hash=life_content_payload_hash(item.text),
                privacy_class=item.privacy,
                content_ref=f"content:npc-initiative-candidate:{suffix}:{item.id}",
                text=item.text,
            )
            for item in event.outcomes
        )
        occurrence = WorldOccurrenceProjection(
            occurrence_id=occurrence_id,
            entity_revision=1,
            trigger_ref=check_event.event_id,
            participant_refs=(self._owner_actor_ref, candidate.npc_ref),
            location_ref=candidate.location_ref,
            time_window=DueWindow(
                opens_at=wake.logical_time,
                closes_at=wake.logical_time + timedelta(minutes=event.duration_minutes),
            ),
            candidate_outcome_refs=tuple(item.candidate_result_ref for item in candidate_contents),
            visibility=event.privacy,
            status="committed",
        )
        self._occurrence_content.commit(OccurrenceContentCommitRequest(
            world_id=self._ledger.world_id,
            occurrence=occurrence,
            candidate_contents=candidate_contents,
            change_id="change:npc-initiative:occurrence:" + suffix,
            transition_id="transition:npc-initiative:occurrence:" + suffix,
            evidence_refs=(self._wake_evidence(wake),),
            policy_refs=(
                _POLICY,
                f"npc-initiative:{event.id}",
                f"policy:life-author-catalog:{self._catalog.version}",
            ),
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:npc-initiative",
            trace_id=trace_id,
            causation_id=check_event.event_id,
            correlation_id=correlation_id,
        ))

    def _activate(
        self, *, occurrence_id: str, wake, trace_id: str, correlation_id: str
    ) -> None:
        projection = self._ledger.project()
        current = next(
            item for item in projection.world_occurrences if item.occurrence_id == occurrence_id
        )
        if current.status != "committed":
            return
        suffix = occurrence_id.removeprefix("occurrence:npc-initiative:")
        payload = WorldOccurrenceActivatedPayload(
            change_id="change:npc-initiative:activate:" + suffix,
            transition_id="transition:npc-initiative:activate:" + suffix,
            expected_entity_revision=current.entity_revision,
            evidence_refs=(self._wake_evidence(wake),),
            policy_refs=(_POLICY,),
            occurrence_id=occurrence_id,
            activated_at=wake.logical_time,
            satisfied_precondition_refs=(),
        ).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:npc-initiative:activated:" + suffix,
            event_type="WorldOccurrenceActivated",
            world_id=self._ledger.world_id,
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:npc-initiative",
            trace_id=trace_id,
            causation_id=occurrence_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="WorldOccurrenceActivated", world_id=self._ledger.world_id, payload=payload
            ) or "npc-initiative-activated:" + suffix,
            payload=payload,
        )
        if self._ledger.lookup_event_commit(event.event_id) is not None:
            return
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=_cursor(projection),
            commit_id="commit:npc-initiative:activated:" + suffix,
        )

    def _check_event_id(self, local_date_iso: str, slot: int) -> str:
        return "event:npc-initiative:check:" + _digest({
            "world_id": self._ledger.world_id,
            "local_date": local_date_iso,
            "check_slot": slot,
        })

    def _check_event(self, check_event_id: str) -> WorldEvent | None:
        located = self._ledger.lookup_event_commit(check_event_id)
        if located is None or located[0].event_type != "ProposalRecorded":
            return None
        if located[0].payload().get("proposal_kind") != "npc_initiative":
            return None
        return located[0]

    def _model_id(self) -> str:
        return str(getattr(self._model, "model", "")).strip() or type(self._model).__name__

    @staticmethod
    def _wake_evidence(wake) -> EvidenceRef:
        return EvidenceRef(
            ref_id=wake.event_id,
            evidence_type="committed_world_event",
            claim_purpose="current_fact",
            source_world_revision=wake.world_revision,
            immutable_hash=wake.payload_hash,
        )


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


__all__ = [
    "NOTHING_CANDIDATE_REF",
    "NpcInitiativeResult",
    "NpcInitiativeRuntime",
    "NpcInitiativeWeightPolicy",
]
