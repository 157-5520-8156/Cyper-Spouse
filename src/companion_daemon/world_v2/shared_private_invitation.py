"""shared_private invitations: she may plan to do something *with the user*.

The reviewed life catalog has always carried the ``shared_private`` social
shape but the life author only produced alone/NPC openings.  This lane closes
the gap with a deliberately narrow, consent-shaped contract:

* the opening comes only from the reviewed ``future_openings`` section with
  ``social_shape: shared_private`` and a mandatory relationship closeness
  floor — a stranger never gets invited to a private shared moment;
* whether she even *wants* to invite today is a recorded ``RandomAuthority``
  draw plus the life author's bounded select/no_op confirmation, one check
  per companion-local day, at most one pending invitation at a time;
* selecting commits only an ``ActivityPlanned`` whose sole participant is the
  user.  Starting it is owned by the existing activity lifecycle contract,
  whose ``shared_private`` opening kind demands a *real user message inside
  the participant scope* as its cause — the ledger cannot start this plan
  without the user having answered in QQ;
* if the reviewed window closes and the activity never started (no answer,
  or the lifecycle declined), this lane deterministically abandons the plan.
  The abandonment is an ordinary ``ActivityAbandoned``, so the existing
  plan-disruption appraisal lane lets her feel the unanswered invitation.

The invitation must still be *spoken*: pending invitations surface as a
ledger-backed Inner Advisory in the chat and proactive lanes (see
``pending_shared_private_invitation_advisories``), where the ordinary
expression machinery — with its receipts and response expectations — owns the
actual QQ message.  This module never dispatches an Action.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Literal

import httpx

from .event_identity import domain_idempotency_key
from .life_author_runtime import (
    LifeAuthorModel,
    LifeAuthorModelFailure,
    LifeAvailabilitySnapshotRecordedPayload,
)
from .life_author_seed import ReviewedLifeSeedCatalog, ReviewedLifeSeedFutureCandidate
from .life_events import ActivityPlannedPayload, ActivityTransitionPayload
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import DueWindow, EvidenceRef, PlanStateProjection, ProjectionCursor, WorldEvent


_POLICY = "policy:shared-private-invitation.1"

NOTHING_CANDIDATE_REF = "nothing:shared-private-invitation"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class SharedPrivateInvitationResult(FrozenModel):
    status: Literal[
        "invited", "recovered", "no_op", "gated", "no_opening",
        "already_pending", "slot_consumed", "blocked",
    ]
    reason_code: str
    plan_event_ref: str | None = None
    draw_event_ref: str | None = None
    abandoned_plan_ids: tuple[str, ...] = ()


def shared_private_pending_plans(
    projection, *, user_participant_ref: str
) -> tuple[PlanStateProjection, ...]:
    """The still-planned user-participating private plans, oldest first."""

    return tuple(
        sorted(
            (
                plan
                for plan in getattr(projection, "plans", ())
                if plan.status == "planned"
                and plan.participant_refs == (user_participant_ref,)
                and plan.privacy_class in {"private", "withhold"}
            ),
            key=lambda plan: (plan.scheduled_window.opens_at, plan.plan_id),
        )
    )


def pending_shared_private_invitation_advisories(projection) -> tuple:
    """Expose pending invitations as a read-only, ledger-backed advisory.

    Identification is structural (exactly one ``user:`` participant, private
    plan, still planned), so the chat/proactive lanes need no catalog handle.
    The advisory only reminds her that the invitation exists and whether the
    user has answered is still open; the expression model owns the words.
    """

    from .context_capsule import InnerAdvisoryCandidate, InnerAdvisoryProjection

    logical_time = getattr(projection, "logical_time", None)
    if not isinstance(logical_time, datetime):
        return ()
    pending = tuple(
        plan
        for plan in getattr(projection, "plans", ())
        if plan.status == "planned"
        and len(plan.participant_refs) == 1
        and plan.participant_refs[0].startswith("user:")
        and plan.privacy_class in {"private", "withhold"}
        and plan.authority_origin is not None
        and plan.scheduled_window.closes_at > logical_time
    )[:2]
    if not pending:
        return ()
    candidates = tuple(
        InnerAdvisoryCandidate(
            candidate_ref="shared-private-invitation:" + _digest(plan.plan_id),
            value=(
                f"她已经打算约你一起：{plan.activity_kind}"
                f"（{plan.scheduled_window.opens_at.isoformat()} 开始）。"
                "如果还没真正开口邀请，或者对方还没答应，这件事悬着——"
                "顺着当下的对话自然地提，或者别提；由她自己判断。"
            )[:256],
            weight_bp=10_000,
            confidence_bp=10_000,
        )
        for plan in pending
    )
    return (
        InnerAdvisoryProjection(
            advisory_id="advisory:shared-private-invitations:" + _digest(
                tuple(plan.plan_id for plan in pending)
            ),
            kind="pending_shared_private_invitation",
            source_refs=tuple(
                dict.fromkeys(
                    plan.authority_origin.accepted_event_ref for plan in pending
                )
            ),
            candidate_refs=tuple(item.candidate_ref for item in candidates),
            candidates=candidates,
            # Below the continuity floors' rank: under extreme budget
            # pressure this reminder yields before relationship/affect state.
            confidence_bp=6_000,
            expiry=logical_time + timedelta(days=1),
            producer_version="shared-private-invitation-view.1",
        ),
    )


class SharedPrivateInvitationRuntime:
    """Own the daily invitation check, recorded draw, bounded confirmation,
    the consent-shaped plan, and the deterministic expiry abandonment."""

    weight_policy_version = "shared-private-invitation-weight.1"

    def __init__(
        self, *, ledger, catalog: ReviewedLifeSeedCatalog, model: LifeAuthorModel,
        owner_actor_ref: str, user_participant_ref: str,
        actor: str = "worker:world-v2:shared-private-invitation",
        invite_chance_bp: int = 2_000,
    ) -> None:
        if not owner_actor_ref or not actor:
            raise ValueError("shared private invitation requires owner and worker actors")
        if not user_participant_ref.startswith("user:"):
            raise ValueError("shared private invitation participant must be a user actor ref")
        if not 0 <= invite_chance_bp <= 10_000:
            raise ValueError("shared private invitation chance must be basis points")
        self._ledger = ledger
        self._catalog = catalog
        self._model = model
        self._owner_actor_ref = owner_actor_ref
        self._user_participant_ref = user_participant_ref
        self._actor = actor
        self._invite_chance_bp = invite_chance_bp
        self._random = RandomAuthority(
            ledger=ledger, source="world-v2:shared-private-invitation-random"
        )

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> SharedPrivateInvitationResult:
        projection = self._ledger.project()
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
            logical_time is None or wake is None or wake.event_type != "ClockAdvanced"
            or transition is None or transition.payload_hash != wake.payload_hash
            or transition.computed_world_revision != wake.world_revision
        ):
            return SharedPrivateInvitationResult(
                status="blocked", reason_code="shared_private.wake_not_exact_clock"
            )
        # Expiry runs on every wake: an invitation whose reviewed window
        # closed without a consent-caused start is quietly withdrawn.  The
        # abandonment feeds the ordinary plan-disruption appraisal lane.
        abandoned = self._abandon_expired(
            wake=wake, trace_id=trace_id, correlation_id=correlation_id
        )
        local_date_iso = self._catalog.localize(wake.logical_time).date().isoformat()
        check_event_id = "event:shared-private:check:" + _digest({
            "world_id": self._ledger.world_id, "local_date": local_date_iso,
        })
        existing_check = self._check_event(check_event_id)
        if existing_check is not None:
            payload = existing_check.payload()
            if payload.get("decision") == "selected":
                recovered = self._commit_invitation_from_check(
                    check_event=existing_check, wake=wake,
                    trace_id=trace_id, correlation_id=correlation_id,
                )
                if recovered is not None:
                    return SharedPrivateInvitationResult(
                        status="recovered",
                        reason_code="shared_private.invitation_recovered",
                        plan_event_ref=recovered,
                        abandoned_plan_ids=abandoned,
                    )
            return SharedPrivateInvitationResult(
                status="slot_consumed",
                reason_code="shared_private.daily_check_consumed",
                abandoned_plan_ids=abandoned,
            )
        projection = self._ledger.project()
        if shared_private_pending_plans(
            projection, user_participant_ref=self._user_participant_ref
        ):
            # One open invitation at a time: asking again before the first is
            # answered would turn a gesture into pressure.
            return SharedPrivateInvitationResult(
                status="already_pending",
                reason_code="shared_private.invitation_already_pending",
                abandoned_plan_ids=abandoned,
            )
        candidates = self._eligible_candidates(projection, wake=wake)
        if not candidates:
            return SharedPrivateInvitationResult(
                status="no_opening",
                reason_code="shared_private.no_eligible_opening",
                abandoned_plan_ids=abandoned,
            )
        draw = self._random.draw(
            attempt_id="attempt:shared-private-invitation:" + _digest({
                "world_id": self._ledger.world_id,
                "local_date": local_date_iso,
                "catalog_version": self._catalog.version,
                "catalog_hash": self._catalog.catalog_hash,
            }),
            candidate_refs=(*(item.token for item in candidates), NOTHING_CANDIDATE_REF),
            catalog_version=self._catalog.version,
            logical_time=self._ledger.project().logical_time,
            seed_instant=wake.logical_time,
            actor=self._actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
            candidate_weights=self._weights(candidates),
            weight_policy_version=self.weight_policy_version,
        )
        draw_event_ref = "event:random-draw:" + draw.draw_id
        if draw.selected_candidate_ref == NOTHING_CANDIDATE_REF:
            self._record_check(
                check_event_id=check_event_id, local_date_iso=local_date_iso,
                decision="nothing", wake=wake, draw_event_ref=draw_event_ref,
                slot=None, raw_output=draw.selected_candidate_ref,
                model="random-authority",
                trace_id=trace_id, correlation_id=correlation_id,
            )
            return SharedPrivateInvitationResult(
                status="no_op", reason_code="shared_private.nothing_drawn",
                draw_event_ref=draw_event_ref, abandoned_plan_ids=abandoned,
            )
        selected = next(
            item for item in candidates if item.token == draw.selected_candidate_ref
        )
        try:
            decision, raw = await self._deliberate(selected, wake=wake)
        except LifeAuthorModelFailure:
            # Model outage never consumes the check slot; the durable draw
            # replays on a later wake of the same day.
            return SharedPrivateInvitationResult(
                status="blocked", reason_code="shared_private.model_unavailable",
                draw_event_ref=draw_event_ref, abandoned_plan_ids=abandoned,
            )
        slot = self._slot_payload(selected)
        check_event = self._record_check(
            check_event_id=check_event_id, local_date_iso=local_date_iso,
            decision="selected" if decision == "select" else "no_op",
            wake=wake, draw_event_ref=draw_event_ref, slot=slot,
            raw_output=raw, model=self._model_id(),
            trace_id=trace_id, correlation_id=correlation_id,
        )
        if decision != "select":
            return SharedPrivateInvitationResult(
                status="no_op", reason_code="shared_private.model_declined",
                draw_event_ref=draw_event_ref, abandoned_plan_ids=abandoned,
            )
        plan_event_ref = self._commit_invitation(
            slot=slot, wake=wake, check_event_ref=check_event.event_id,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        return SharedPrivateInvitationResult(
            status="invited", reason_code="shared_private.invitation_planned",
            plan_event_ref=plan_event_ref, draw_event_ref=draw_event_ref,
            abandoned_plan_ids=abandoned,
        )

    # -- eligibility ---------------------------------------------------------

    def _eligible_candidates(
        self, projection, *, wake
    ) -> tuple[ReviewedLifeSeedFutureCandidate, ...]:
        relationship = (
            projection.relationship_states[-1] if projection.relationship_states else None
        )
        closeness = int(
            getattr(getattr(relationship, "variables", None), "closeness_bp", 0)
        )
        owner_plans = tuple(
            plan for plan in projection.plans
            if plan.owner_actor_ref == self._owner_actor_ref
        )
        return tuple(
            item
            for item in self._catalog.future_candidates_at(
                instant=wake.logical_time,
                plans=owner_plans,
                npcs=projection.npcs,
                social_shapes=frozenset({"shared_private"}),
            )
            if item.opening.requires_relationship_closeness_bp is not None
            and closeness >= item.opening.requires_relationship_closeness_bp
        )

    def _weights(
        self, candidates: tuple[ReviewedLifeSeedFutureCandidate, ...]
    ) -> dict[str, int]:
        total_importance = sum(
            max(1, item.opening.importance_bp) for item in candidates
        )
        weights: dict[str, int] = {}
        total = 0
        for item in candidates:
            mass = max(
                1,
                self._invite_chance_bp
                * max(1, item.opening.importance_bp)
                // max(1, total_importance),
            )
            weights[item.token] = mass
            total += mass
        weights[NOTHING_CANDIDATE_REF] = max(10_000 - total, 0)
        return weights

    # -- deliberation ---------------------------------------------------------

    async def _deliberate(
        self, candidate: ReviewedLifeSeedFutureCandidate, *, wake
    ) -> tuple[Literal["select", "no_op"], str]:
        """The life author's exact bounded select/no_op JSON contract."""

        try:
            raw = await self._model.complete(
                [
                    {"role": "system", "content": (
                        "You are the final semantic confirmation for one reviewed private shared "
                        "invitation: the companion planning to do something together with the user. "
                        "The host has already verified the reviewed opening, the relationship "
                        "closeness floor, the free calendar slot, the daily frequency budget, and "
                        "the controlled-random draw. Selecting only creates her own tentative plan "
                        "and the intent to ask; the activity can never start unless the user "
                        "actually answers her invitation. Select when wanting to ask now rings "
                        "true; return no_op when it does not — not asking today is always a "
                        "legitimate outcome. Return exactly {\"decision\":\"no_op\"} or "
                        "{\"decision\":\"select\",\"candidate_token\":\"offered token\"}. "
                        "Do not invent an outcome, person, place, time, event id, or extra detail."
                    )},
                    {"role": "user", "content": json.dumps({
                        "authoritative_eligibility": {
                            "logical_time": wake.logical_time.isoformat(),
                            "availability_hash": candidate.availability_hash,
                            "participant_ref": self._user_participant_ref,
                        },
                        "shared_private_candidate": {
                            "token": candidate.token,
                            "activity_kind": candidate.opening.activity_kind,
                            "target_local_date": candidate.target_local_date.isoformat(),
                            "local_window": candidate.local_window,
                            "duration_minutes": candidate.opening.duration_minutes,
                            "privacy": candidate.opening.privacy,
                        },
                    }, ensure_ascii=False, separators=(",", ":"))},
                ],
                temperature=0.2,
            )
        except (TimeoutError, ConnectionError, OSError, httpx.HTTPError) as exc:
            raise LifeAuthorModelFailure("shared private invitation model is unavailable") from exc
        if not isinstance(raw, str) or len(raw.encode()) > 32_768:
            raise LifeAuthorModelFailure("invitation model response is not bounded text")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LifeAuthorModelFailure("invitation model response is not valid JSON") from exc
        if not isinstance(parsed, dict) or set(parsed) not in (
            {"decision"}, {"decision", "candidate_token"}
        ):
            raise LifeAuthorModelFailure("invitation model returned an invalid decision")
        decision = parsed.get("decision")
        if decision == "no_op":
            if "candidate_token" in parsed:
                raise LifeAuthorModelFailure("invitation no_op cannot select a candidate")
            return "no_op", raw
        if decision != "select" or parsed.get("candidate_token") != candidate.token:
            raise LifeAuthorModelFailure("invitation model selected an unoffered candidate")
        return "select", raw

    # -- durable records ------------------------------------------------------

    def _slot_payload(self, candidate: ReviewedLifeSeedFutureCandidate) -> dict:
        return {
            "opening_id": candidate.opening.id,
            "activity_kind": candidate.opening.activity_kind,
            "candidate_token": candidate.token,
            "target_local_date": candidate.target_local_date.isoformat(),
            "local_window": candidate.local_window,
            "opens_at": candidate.opens_at.isoformat(),
            "closes_at": candidate.closes_at.isoformat(),
            "location_ref": candidate.location_ref,
            "availability_hash": candidate.availability_hash,
            "importance_bp": candidate.opening.importance_bp,
            "duration_minutes": candidate.opening.duration_minutes,
            "privacy": candidate.opening.privacy,
            "policy_refs": list(
                candidate.opening.policy_refs(catalog_version=self._catalog.version)
            ),
        }

    def _record_check(
        self, *, check_event_id: str, local_date_iso: str,
        decision: Literal["nothing", "no_op", "selected"], wake, draw_event_ref: str,
        slot: dict | None, raw_output: str, model: str,
        trace_id: str, correlation_id: str,
    ) -> WorldEvent:
        projection = self._ledger.project()
        payload = {
            "proposal_id": "proposal:shared-private-invitation:" + _digest({
                "world_id": self._ledger.world_id, "local_date": local_date_iso,
            }),
            "proposal_kind": "shared_private_invitation",
            "decision": decision,
            "check_local_date": local_date_iso,
            "trigger_id": wake.event_id,
            "evaluated_world_revision": projection.world_revision,
            "wake_event_ref": wake.event_id,
            "wake_event_payload_hash": wake.payload_hash,
            "draw_event_ref": draw_event_ref,
            "participant_ref": self._user_participant_ref,
            "slot": slot,
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
            source="world-v2:shared-private-invitation",
            trace_id=trace_id,
            causation_id=draw_event_ref,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload
            ) or "shared-private-check:" + _digest(check_event_id),
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=_cursor(projection),
            commit_id="commit:shared-private:check:" + _digest(check_event_id),
        )
        return event

    def _check_event(self, check_event_id: str) -> WorldEvent | None:
        located = self._ledger.lookup_event_commit(check_event_id)
        if located is None or located[0].event_type != "ProposalRecorded":
            return None
        if located[0].payload().get("proposal_kind") != "shared_private_invitation":
            return None
        return located[0]

    def _commit_invitation_from_check(
        self, *, check_event: WorldEvent, wake, trace_id: str, correlation_id: str
    ) -> str | None:
        slot = check_event.payload().get("slot")
        if not isinstance(slot, dict):
            return None
        return self._commit_invitation(
            slot=slot, wake=wake, check_event_ref=check_event.event_id,
            trace_id=trace_id, correlation_id=correlation_id,
            recovery=True,
        )

    def _commit_invitation(
        self, *, slot: dict, wake, check_event_ref: str,
        trace_id: str, correlation_id: str, recovery: bool = False,
    ) -> str | None:
        projection = self._ledger.project()
        suffix = _digest({
            "world_id": self._ledger.world_id,
            "opening_id": slot["opening_id"],
            "opens_at": slot["opens_at"],
        })
        plan_event_id = "event:shared-private:plan:" + suffix
        if self._ledger.lookup_event_commit(plan_event_id) is not None:
            return None if recovery else plan_event_id
        opens_at = datetime.fromisoformat(slot["opens_at"])
        if projection.logical_time is not None and projection.logical_time >= opens_at:
            # A long outage between selection and recovery: never invite into
            # the past.
            return None
        clock_evidence = EvidenceRef(
            ref_id=wake.event_id,
            evidence_type="committed_world_event",
            claim_purpose="future_plan",
            source_world_revision=wake.world_revision,
            immutable_hash=wake.payload_hash,
        )
        snapshot_payload = LifeAvailabilitySnapshotRecordedPayload(
            snapshot_id="availability:shared-private:" + suffix,
            wake_event_ref=wake.event_id,
            wake_event_payload_hash=wake.payload_hash,
            wake_world_revision=wake.world_revision,
            candidate_token=slot["candidate_token"],
            catalog_version=self._catalog.version,
            catalog_hash=self._catalog.catalog_hash,
            owner_actor_ref=self._owner_actor_ref,
            location_ref=slot.get("location_ref"),
            participant_refs=(),
            availability_hash=slot["availability_hash"],
        )
        snapshot_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:shared-private:availability:" + suffix,
            world_id=self._ledger.world_id,
            event_type="LifeAvailabilitySnapshotRecorded",
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:shared-private-invitation",
            trace_id=trace_id,
            causation_id=check_event_ref,
            correlation_id=correlation_id,
            idempotency_key="shared-private-availability:" + suffix,
            payload=snapshot_payload.model_dump(mode="json"),
        )
        snapshot_evidence = EvidenceRef(
            ref_id=snapshot_event.event_id,
            evidence_type="committed_world_event",
            claim_purpose="future_plan",
            source_world_revision=projection.world_revision + 1,
            immutable_hash=snapshot_event.payload_hash,
        )
        evidence = (clock_evidence, snapshot_evidence)
        plan = PlanStateProjection(
            plan_id="shared-private:" + suffix,
            activity_id="activity:shared-private:" + suffix,
            entity_revision=1,
            activity_kind=slot["activity_kind"],
            evidence_refs=evidence,
            status="planned",
            importance_bp=int(slot["importance_bp"]),
            scheduled_window=DueWindow(
                opens_at=opens_at,
                closes_at=datetime.fromisoformat(slot["closes_at"]),
            ),
            participant_refs=(self._user_participant_ref,),
            location_ref=slot.get("location_ref"),
            privacy_class=slot["privacy"],
            owner_actor_ref=self._owner_actor_ref,
        )
        payload = ActivityPlannedPayload(
            change_id="change:shared-private:plan:" + suffix,
            transition_id="transition:shared-private:plan:" + suffix,
            expected_entity_revision=0,
            evidence_refs=evidence,
            policy_refs=tuple(sorted({*slot.get("policy_refs", ()), _POLICY})),
            plan=plan,
        ).model_dump(mode="json")
        plan_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=plan_event_id,
            world_id=self._ledger.world_id,
            event_type="ActivityPlanned",
            logical_time=wake.logical_time,
            created_at=wake.logical_time,
            actor=self._actor,
            source="world-v2:shared-private-invitation",
            trace_id=trace_id,
            causation_id=check_event_ref,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ActivityPlanned", world_id=self._ledger.world_id, payload=payload
            ) or "shared-private-plan:" + suffix,
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (snapshot_event, plan_event), expected_cursor=_cursor(projection),
            commit_id="commit:shared-private:plan:" + suffix,
        )
        return plan_event_id

    def _abandon_expired(
        self, *, wake, trace_id: str, correlation_id: str
    ) -> tuple[str, ...]:
        projection = self._ledger.project()
        logical_time = projection.logical_time
        shared_kinds = {
            item.activity_kind
            for item in self._catalog.reviewed_future_openings
            if item.social_shape == "shared_private"
        }
        abandoned: list[str] = []
        for plan in shared_private_pending_plans(
            projection, user_participant_ref=self._user_participant_ref
        ):
            if plan.activity_kind not in shared_kinds:
                continue
            if plan.scheduled_window.closes_at > logical_time:
                continue
            event_id = "event:shared-private:abandoned:" + _digest({
                "world_id": self._ledger.world_id, "plan_id": plan.plan_id,
            })
            if self._ledger.lookup_event_commit(event_id) is not None:
                continue
            suffix = _digest({"event": event_id})
            payload = ActivityTransitionPayload(
                change_id="change:shared-private:abandon:" + suffix,
                transition_id="transition:shared-private:abandon:" + suffix,
                expected_entity_revision=plan.entity_revision,
                evidence_refs=(
                    EvidenceRef(
                        ref_id=wake.event_id,
                        evidence_type="committed_world_event",
                        claim_purpose="life_transition",
                        source_world_revision=wake.world_revision,
                        immutable_hash=wake.payload_hash,
                    ),
                ),
                policy_refs=(_POLICY,),
                plan_id=plan.plan_id,
                transitioned_at=wake.logical_time,
                reason_ref="reason:shared-private-invitation-expired",
            ).model_dump(mode="json")
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=event_id,
                event_type="ActivityAbandoned",
                world_id=self._ledger.world_id,
                logical_time=wake.logical_time,
                created_at=wake.logical_time,
                actor=self._actor,
                source="world-v2:shared-private-invitation",
                trace_id=trace_id,
                causation_id=wake.event_id,
                correlation_id=correlation_id,
                idempotency_key=domain_idempotency_key(
                    event_type="ActivityAbandoned",
                    world_id=self._ledger.world_id,
                    payload=payload,
                ) or "shared-private-abandoned:" + suffix,
                payload=payload,
            )
            current = self._ledger.project()
            self._ledger.commit_at_cursor(
                (event,), expected_cursor=_cursor(current),
                commit_id="commit:shared-private:abandon:" + suffix,
            )
            abandoned.append(plan.plan_id)
        return tuple(abandoned)

    def _model_id(self) -> str:
        return str(getattr(self._model, "model", "")).strip() or type(self._model).__name__


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


__all__ = [
    "NOTHING_CANDIDATE_REF",
    "SharedPrivateInvitationResult",
    "SharedPrivateInvitationRuntime",
    "pending_shared_private_invitation_advisories",
    "shared_private_pending_plans",
]
