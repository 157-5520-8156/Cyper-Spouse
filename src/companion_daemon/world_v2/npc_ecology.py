"""Model-owned NPC ecology joined to the ordinary lived-world ledger.

The host chooses only *when* a bounded consideration may run and validates
source/permission closure.  One NPC actor model freely decides its private
state and whether to propose an occurrence.  A separate World Author then
adjudicates material reality.  Accepted occurrences use the normal
WorldOccurrence -> aftermath -> Appraisal -> Affect -> Experience/Memory path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from typing import Literal, Protocol

import httpx
from pydantic import Field, model_validator

from .event_identity import domain_idempotency_key
from .life_content_store import (
    ImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from .life_events import (
    ActivityPlannedPayload,
    ActivityTransitionPayload,
    NpcStateChangedPayload,
    WorldOccurrenceActivatedPayload,
)
from .life_content_events import LifeContentRecordedPayload
from .life_author_seed import ReviewedLifeSeedCatalog
from .npc_identity_view import NpcIdentityView, npc_identity_views
from .npc_relationship_view import npc_relationship_readings
from .occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OccurrenceContentCoordinator,
    OutcomeCandidateContent,
)
from .schema_core import FrozenModel, PrivacyClass
from .structured_completion import complete_json_object
from .proposal_audit_schemas import (
    ModelResultRecordedPayload,
    ProposalRecordedV2Payload,
    RecordedModelDecisionContext,
    RecordedModelResultAudit,
    RecordedModelRoute,
    canonical_json,
    model_audit_json,
    sha256,
)
from .proposal_envelope import MinimalProposal
from .schemas import (
    DueWindow,
    EvidenceRef,
    NpcSocialVariables,
    NpcSubjectiveState,
    PlanStateProjection,
    ProjectionCursor,
    WorldEvent,
    WorldOccurrenceProjection,
)


_POLICY = "policy:npc-ecology.2"
@dataclass(frozen=True, slots=True)
class _ModelAttempt:
    request_json: str
    raw: str | None
    status: Literal[
        "proposal_validated",
        "main_invalid",
        "main_invalid_recovered",
        "recovery_failed",
        "main_timeout",
        "main_exception",
    ]
    failure_code: str | None = None


class _NpcModelRunFailed(RuntimeError):
    def __init__(self, attempts: tuple[_ModelAttempt, ...], reason: str) -> None:
        super().__init__(reason)
        self.attempts = attempts
        self.reason = reason


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _provider_failure_reason(*, invalid_reason: str, failure_code: str) -> str:
    """Keep provider availability failures distinct from invalid model output."""

    stem = invalid_reason.removesuffix("_invalid_after_repair")
    return f"{stem}_{failure_code}"


def _cursor(projection: object) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=getattr(projection, "world_revision"),
        deliberation_revision=getattr(projection, "deliberation_revision"),
        ledger_sequence=getattr(projection, "ledger_sequence"),
    )


class NpcEcologyModel(Protocol):
    model: str

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2
    ) -> str: ...

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2
    ) -> str: ...


class NpcEcologyStimulus(FrozenModel):
    cursor: ProjectionCursor
    wake_event_ref: str = Field(min_length=1)
    source_event_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    epoch_ref: str = Field(min_length=1, max_length=256)
    focus_npc_ref: str | None = Field(default=None, pattern=r"^npc:")
    focus_plan_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def refs_are_canonical(self) -> "NpcEcologyStimulus":
        if self.source_event_refs != tuple(sorted(set(self.source_event_refs))):
            raise ValueError("NPC ecology stimulus refs must be sorted and unique")
        if self.wake_event_ref not in self.source_event_refs:
            raise ValueError("NPC ecology stimulus must include its exact wake")
        if (self.focus_npc_ref is None) != (self.focus_plan_ref is None):
            raise ValueError("NPC ecology plan focus must be complete")
        return self


class NpcSocialWorldSnapshot(FrozenModel):
    cursor: ProjectionCursor
    logical_time: object
    identities: tuple[NpcIdentityView, ...]
    available_npc_refs: tuple[str, ...]
    available_location_refs: tuple[str, ...]
    recent_occurrence_refs: tuple[str, ...]


class NpcActorDecision(FrozenModel):
    decision: Literal["no_op", "propose"]
    npc_ref: str = Field(pattern=r"^npc:")
    impulse_summary: str = Field(min_length=1, max_length=1_000)
    inner_state_summary: str = Field(min_length=1, max_length=4_000)
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    relationship_to_protagonist: NpcSocialVariables
    current_goal_summaries: tuple[str, ...] = Field(default=(), max_length=4)
    proposal: NpcActorProposal | None = None

    @model_validator(mode="after")
    def refs_and_goals_are_canonical(self) -> "NpcActorDecision":
        if self.source_refs != tuple(sorted(set(self.source_refs))):
            raise ValueError("NPC decision refs must be sorted and unique")
        if any(not value.strip() or len(value) > 1_000 for value in self.current_goal_summaries):
            raise ValueError("NPC goal summary is empty or oversized")
        if (self.decision == "propose") != (self.proposal is not None):
            raise ValueError("NPC proposal binding must match the actor decision")
        return self


class NpcWorldOutcomeDraft(FrozenModel):
    text: str = Field(min_length=1, max_length=4_000)
    privacy: PrivacyClass


class NpcActorProposal(FrozenModel):
    """The NPC-owned concrete intent; the World Author cannot rewrite it."""

    timing: Literal["now", "later"]
    premise: str = Field(min_length=1, max_length=4_000)
    participant_refs: tuple[str, ...] = Field(min_length=1, max_length=4)
    location_ref: str = Field(min_length=1, max_length=512)
    duration_minutes: int = Field(ge=5, le=24 * 60)
    visibility: PrivacyClass
    activity_kind: str | None = Field(default=None, min_length=1, max_length=128)
    scheduled_start_after_minutes: int | None = Field(default=None, ge=5, le=7 * 24 * 60)
    importance_bp: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def timing_shape_is_complete(self) -> "NpcActorProposal":
        if self.participant_refs != tuple(sorted(set(self.participant_refs))):
            raise ValueError("NPC actor participants must be canonical")
        future = (
            self.activity_kind,
            self.scheduled_start_after_minutes,
            self.importance_bp,
        )
        if self.timing == "now" and any(value is not None for value in future):
            raise ValueError("immediate NPC intent cannot smuggle a future plan")
        if self.timing == "later" and any(value is None for value in future):
            raise ValueError("future NPC intent is incomplete")
        return self


class NpcWorldDecision(FrozenModel):
    decision: Literal["no_op", "accept"]
    outcomes: tuple[NpcWorldOutcomeDraft, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def adjudication_has_no_actor_choices(self) -> "NpcWorldDecision":
        if self.decision == "no_op" and self.outcomes:
            raise ValueError("NPC world no_op cannot smuggle outcomes")
        return self


class NpcEcologyResult(FrozenModel):
    status: Literal[
        "no_op",
        "state_advanced",
        "occurrence_committed",
        "plan_committed",
        "plan_completed",
        "already_considered",
        "not_due",
        "no_npcs",
        "stale_prefix",
        "technical_failure",
        "rejected",
        "recovered",
    ]
    reason_code: str
    npc_ref: str | None = None
    decision_event_ref: str | None = None
    occurrence_id: str | None = None


class NpcEcology:
    """One source-bound public interface for NPC private and material life."""

    def __init__(
        self,
        *,
        ledger,
        content_store: ImmutableLifeContentStore,
        occurrence_content: OccurrenceContentCoordinator,
        actor_model: NpcEcologyModel,
        world_author: NpcEcologyModel,
        protagonist_actor_ref: str,
        catalog: ReviewedLifeSeedCatalog | None = None,
        worker_actor: str = "worker:world-v2:npc-ecology",
    ) -> None:
        if occurrence_content.ledger is not ledger:
            raise ValueError("NPC ecology occurrence coordinator must own the exact ledger")
        if not protagonist_actor_ref or not worker_actor:
            raise ValueError("NPC ecology requires protagonist and worker actors")
        self._ledger = ledger
        self._store = content_store
        self._occurrence_content = occurrence_content
        self._actor_model = actor_model
        self._world_author = world_author
        self._protagonist = protagonist_actor_ref
        self._worker_actor = worker_actor
        self._catalog = catalog

    def snapshot(self, cursor: ProjectionCursor) -> NpcSocialWorldSnapshot:
        projection = self._ledger.project_at(cursor)
        identities = npc_identity_views(
            projection,
            content_store=self._store,
            relationships=npc_relationship_readings(
                projection,
                protagonist_actor_ref=self._protagonist,
            ),
            reviewed_identity_summaries=(
                {
                    item.stable_identity_ref: item.identity_summary
                    for item in self._catalog.reviewed_npcs
                    if item.identity_summary is not None
                }
                if self._catalog is not None
                else None
            ),
        )
        available_npcs = tuple(
            item.npc_ref for item in identities if item.lifecycle_state == "active"
        )
        locations = {
            item.values.location_ref
            for item in projection.locations
            if isinstance(getattr(item.values, "location_ref", None), str)
        }
        locations.update(
            item.location_ref
            for item in projection.world_places
            if isinstance(getattr(item, "location_ref", None), str)
        )
        locations.update(
            item.location_ref
            for item in projection.plans
            if isinstance(getattr(item, "location_ref", None), str)
        )
        locations.update(
            item.current_location_ref
            for item in projection.npcs
            if isinstance(item.current_location_ref, str)
        )
        if self._catalog is not None and projection.logical_time is not None:
            biography = self._catalog.biographical_context_at(
                instant=projection.logical_time,
                life_arcs=projection.life_arcs,
                biographical_coordinates=projection.biographical_coordinates,
            )
            local = self._catalog.localize(projection.logical_time)
            locations.update(
                item.location_ref
                for item in self._catalog.reviewed_locations
                if item.eligible_in_context(biography) and item.available_at(local)
            )
        recent_occurrences = tuple(
            item.occurrence_id
            for item in sorted(
                projection.world_occurrences,
                key=lambda value: value.settled_at or value.time_window.opens_at,
                reverse=True,
            )[:16]
        )
        return NpcSocialWorldSnapshot(
            cursor=cursor,
            logical_time=projection.logical_time,
            identities=identities,
            available_npc_refs=available_npcs,
            available_location_refs=tuple(sorted(locations)),
            recent_occurrence_refs=recent_occurrences,
        )

    def has_due_work(self, *, projection: object) -> bool:
        """Read whether an already-authored NPC effect is exactly due.

        Ordinary ambient consideration obeys Life Ecology's recorded cadence.
        This read-only exception covers only effect completion already present
        in authority: a pending actor decision awaiting World adjudication or
        an NPC-owned Plan whose committed opening time has arrived.
        """

        if self._pending_actor_event_ref(projection) is not None:
            return True
        logical_time = getattr(projection, "logical_time", None)
        if logical_time is None:
            return False
        active_npc_refs = {
            f"npc:{item.npc_id}"
            for item in getattr(projection, "npcs", ())
            if item.status == "active"
        }
        return any(
            item.status == "planned"
            and item.owner_actor_ref in active_npc_refs
            and item.scheduled_window is not None
            and item.scheduled_window.opens_at <= logical_time
            for item in getattr(projection, "plans", ())
        )

    async def advance(self, stimulus: NpcEcologyStimulus) -> NpcEcologyResult:
        projection = self._ledger.project()
        if _cursor(projection) != stimulus.cursor:
            return NpcEcologyResult(status="stale_prefix", reason_code="npc_ecology.stale_prefix")
        authority = {item.event_id: item for item in projection.committed_world_event_refs}
        wake = authority.get(stimulus.wake_event_ref)
        if wake is None or wake.event_type != "ClockAdvanced":
            return NpcEcologyResult(
                status="rejected", reason_code="npc_ecology.wake_not_exact_clock"
            )
        if any(ref not in authority for ref in stimulus.source_event_refs):
            return NpcEcologyResult(
                status="rejected", reason_code="npc_ecology.source_not_committed"
            )
        snapshot = self.snapshot(stimulus.cursor)
        if not snapshot.available_npc_refs:
            return NpcEcologyResult(status="no_npcs", reason_code="npc_ecology.no_registered_npc")
        pending_actor_event_id = self._pending_actor_event_ref(projection)
        if pending_actor_event_id is not None:
            actor_event_id = pending_actor_event_id
            identity = pending_actor_event_id.removeprefix("event:npc-ecology:actor:")
        else:
            identity = _digest(
                {
                    "world": self._ledger.world_id,
                    "epoch": stimulus.epoch_ref,
                    "sources": stimulus.source_event_refs,
                }
            )
            actor_event_id = f"event:npc-ecology:actor:{identity}"
        existing_actor = self._ledger.lookup_event_commit(actor_event_id)
        actor_decision: NpcActorDecision
        if existing_actor is not None:
            actor_decision = NpcActorDecision.model_validate_json(
                _canonical(existing_actor[0].payload()["decision_payload"])
            )
        else:
            try:
                actor_decision, actor_raw, actor_attempts = await self._actor_decide(
                    stimulus=stimulus, snapshot=snapshot
                )
            except _NpcModelRunFailed as failure:
                self._commit_model_audits(
                    role="actor",
                    attempts=failure.attempts,
                    stimulus=stimulus,
                    wake=wake,
                    npc_ref=self._selected_npc_ref(stimulus=stimulus, snapshot=snapshot),
                )
                return NpcEcologyResult(
                    status="technical_failure",
                    reason_code=failure.reason,
                )
            self._commit_model_audits(
                role="actor",
                attempts=actor_attempts,
                stimulus=stimulus,
                wake=wake,
                npc_ref=actor_decision.npc_ref,
            )
            projection = self._ledger.project()
            committed = self._record_actor_state(
                projection=projection,
                wake=wake,
                stimulus=stimulus,
                decision=actor_decision,
                raw=actor_raw,
                event_id=actor_event_id,
            )
            if not committed:
                return NpcEcologyResult(
                    status="stale_prefix", reason_code="npc_ecology.state_cas_conflict"
                )
        if actor_decision.decision == "no_op":
            return NpcEcologyResult(
                status=("already_considered" if existing_actor else "state_advanced"),
                reason_code="npc_ecology.npc_chose_no_occurrence",
                npc_ref=actor_decision.npc_ref,
                decision_event_ref=actor_event_id,
            )
        return await self._materialize(
            stimulus=stimulus,
            snapshot=snapshot,
            wake=wake,
            actor_event_id=actor_event_id,
            actor_decision=actor_decision,
            identity=identity,
        )

    def _pending_actor_event_ref(self, projection) -> str | None:
        candidates = sorted(
            (
                item.subjective_state
                for item in projection.npcs
                if item.subjective_state is not None
                and item.subjective_state.pending_actor_event_ref is not None
            ),
            key=lambda item: item.evolved_at,
        )
        for state in candidates:
            actor_ref = state.pending_actor_event_ref
            assert actor_ref is not None
            identity = actor_ref.removeprefix("event:npc-ecology:actor:")
            if identity == actor_ref:
                continue
            if self._ledger.lookup_event_commit(f"event:npc-ecology:world:{identity}") is None:
                return actor_ref
        return None

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> NpcEcologyResult:
        """LifeEcology adapter: merge recent events, otherwise consider ambiently."""

        del trace_id, correlation_id
        projection = self._ledger.project()
        wake = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == wake_event_ref
            ),
            None,
        )
        if wake is None or projection.logical_time is None:
            return NpcEcologyResult(status="rejected", reason_code="npc_ecology.wake_missing")
        completed_plan = self._complete_settled_npc_plan(projection=projection, wake=wake)
        if completed_plan is not None:
            return NpcEcologyResult(
                status="plan_completed",
                reason_code="npc_ecology.settled_plan_completed",
                npc_ref=completed_plan.owner_actor_ref,
            )
        if self._pending_actor_event_ref(projection) is None and any(
            item.subjective_state is not None
            and item.subjective_state.evolved_at == wake.logical_time
            and wake_event_ref in item.subjective_state.source_event_refs
            for item in projection.npcs
        ):
            return NpcEcologyResult(
                status="already_considered",
                reason_code="npc_ecology.wake_already_consumed",
            )
        active_npc_refs = {
            f"npc:{item.npc_id}" for item in projection.npcs if item.status == "active"
        }
        due_plan = next(
            iter(
                sorted(
                    (
                        item
                        for item in projection.plans
                        if item.status == "planned"
                        and isinstance(item.owner_actor_ref, str)
                        and item.owner_actor_ref in active_npc_refs
                        and item.scheduled_window is not None
                        and item.scheduled_window.opens_at <= projection.logical_time
                    ),
                    key=lambda item: (item.scheduled_window.opens_at, item.plan_id),
                )
            ),
            None,
        )
        last_considered_at = max(
            (
                item.subjective_state.evolved_at
                for item in projection.npcs
                if item.subjective_state is not None
            ),
            default=None,
        )
        recent = self._npc_observable_recent_event_refs(
            projection=projection,
            after=last_considered_at,
        )
        if due_plan is not None:
            if due_plan.authority_origin is None:
                return NpcEcologyResult(
                    status="technical_failure",
                    reason_code="npc_ecology.due_plan_authority_missing",
                )
            refs = tuple(
                sorted(
                    {
                        wake_event_ref,
                        due_plan.authority_origin.accepted_event_ref,
                    }
                )
            )
            epoch = f"due-plan:{due_plan.plan_id}:{due_plan.entity_revision}"
        elif recent:
            # Recent material change opens an opportunity, but the actor gets
            # only the exact clock plus its own source-closed identity capsule.
            # The digest preserves effect-once scheduling without disclosing
            # another NPC's or the protagonist's private event refs.
            refs = (wake_event_ref,)
            epoch = "stimulus:" + _digest(recent)
        else:
            # LifeEcology already owns the durable, recorded 45m-8h cadence.
            # The exact wake is therefore this ambient opportunity's identity;
            # a second fixed gate here would replace that scheduling decision.
            epoch = f"ambient:{wake_event_ref}"
            refs = (wake_event_ref,)
        return await self.advance(
            NpcEcologyStimulus(
                cursor=_cursor(projection),
                wake_event_ref=wake_event_ref,
                source_event_refs=refs,
                epoch_ref=epoch,
                focus_npc_ref=(due_plan.owner_actor_ref if due_plan is not None else None),
                focus_plan_ref=(due_plan.plan_id if due_plan is not None else None),
            )
        )

    @staticmethod
    def _npc_observable_recent_event_refs(*, projection, after) -> tuple[str, ...]:
        """Return only settled material evidence involving a registered NPC.

        Protagonist Appraisal/Affect, direct observations, perceptions and
        private life transitions are deliberately absent.  They may affect
        the protagonist only through CharacterInterior; they are not NPC
        knowledge or NPC scheduling stimuli.
        """

        authority = {
            item.event_id: item
            for item in projection.committed_world_event_refs
            if item.logical_time <= projection.logical_time
            and (after is None or item.logical_time > after)
        }
        registered_npc_refs = {f"npc:{item.npc_id}" for item in projection.npcs}
        refs = {
            item.settlement_event_ref
            for item in projection.world_occurrences
            if item.status == "settled"
            and item.settlement_event_ref in authority
            and registered_npc_refs.intersection(item.participant_refs)
        }
        refs.update(
            item.origin.accepted_event_ref
            for item in projection.experiences
            if item.origin.accepted_event_ref in authority
            and registered_npc_refs.intersection(item.values.participant_refs)
        )
        return tuple(
            sorted(
                refs,
                key=lambda ref: (authority[ref].logical_time, ref),
            )[-31:]
        )

    async def _actor_decide(
        self, *, stimulus: NpcEcologyStimulus, snapshot: NpcSocialWorldSnapshot
    ) -> tuple[NpcActorDecision, str, tuple[_ModelAttempt, ...]]:
        prompt, payload = self._actor_request(stimulus=stimulus, snapshot=snapshot)
        return await self._run_model_with_one_reselect(
            model=self._actor_model,
            prompt=prompt,
            payload=payload,
            parser=NpcActorDecision.model_validate_json,
            validator=lambda decision: self._validate_actor_decision(
                decision, stimulus=stimulus, snapshot=snapshot
            ),
            failure_reason="npc_ecology.actor_invalid_after_repair",
        )

    def _actor_request(self, *, stimulus, snapshot):
        selected_npc_ref = self._selected_npc_ref(stimulus=stimulus, snapshot=snapshot)
        selected_identity = next(
            item for item in snapshot.identities if item.npc_ref == selected_npc_ref
        )
        payload = {
            "stimulus": stimulus.model_dump(mode="json"),
            "npc_private_capsule": selected_identity.model_dump(mode="json"),
            "public_world": {
                "logical_time": (
                    snapshot.logical_time.isoformat()
                    if hasattr(snapshot.logical_time, "isoformat")
                    else snapshot.logical_time
                ),
                "available_npc_refs": snapshot.available_npc_refs,
                "available_location_refs": snapshot.available_location_refs,
                "recent_occurrence_refs": snapshot.recent_occurrence_refs,
            },
            "authority": {
                "role": "one_npc_actor",
                "selected_npc_ref": selected_npc_ref,
                "system_does_not_choose_motive": True,
                "clock_is_opportunity_not_fact": True,
                "input_event_refs": self._actor_context_event_refs(
                    stimulus=stimulus,
                    snapshot=snapshot,
                    npc_ref=selected_npc_ref,
                ),
            },
        }
        output_contract = {
            "json_schema": NpcActorDecision.model_json_schema(mode="validation"),
        }
        prompt = (
            "Act as the exact selected NPC in this source-bound social world. Freely decide "
            "what they currently feel and want, and whether they propose doing anything. "
            "No motive catalogue or preferred social behavior exists. A no_op is valid. "
            "Return JSON matching NpcActorDecision: decision, npc_ref, impulse_summary, "
            "inner_state_summary, "
            "source_refs, relationship_to_protagonist (eight 0..10000 fields), "
            "current_goal_summaries and proposal. If proposing, the proposal must contain "
            "the NPC's own concrete timing (now/later), premise, participants, location, "
            "duration, visibility and, for later, free activity/timing/importance. The World "
            "Author cannot invent these choices. Exact refs must come from the supplied world. "
            "The following contract controls only JSON shape and authority closure; it does not "
            "prefer no_op, propose, any motive, or any relationship value: "
            + _canonical(output_contract)
        )
        return prompt, payload

    async def _world_decide(
        self,
        *,
        stimulus: NpcEcologyStimulus,
        snapshot: NpcSocialWorldSnapshot,
        actor_decision: NpcActorDecision,
    ) -> tuple[NpcWorldDecision, str, tuple[_ModelAttempt, ...]]:
        prompt, payload = self._world_request(
            stimulus=stimulus,
            snapshot=snapshot,
            actor_decision=actor_decision,
        )
        return await self._run_model_with_one_reselect(
            model=self._world_author,
            prompt=prompt,
            payload=payload,
            parser=NpcWorldDecision.model_validate_json,
            validator=lambda decision: self._validate_world_decision(
                decision,
                stimulus=stimulus,
                snapshot=snapshot,
                actor_decision=actor_decision,
            ),
            failure_reason="npc_ecology.world_invalid_after_repair",
        )

    def _world_request(self, *, stimulus, snapshot, actor_decision):
        payload = {
            "stimulus": stimulus.model_dump(mode="json"),
            "npc_actor_decision": actor_decision.model_dump(mode="json"),
            "world_capabilities": {
                "participant_refs": (
                    self._protagonist,
                    *tuple(
                        sorted(
                            f"npc:{item.npc_id}"
                            for item in self._ledger.project().npcs
                            if item.status == "active"
                        )
                    ),
                ),
                "location_refs": snapshot.available_location_refs,
            },
        }
        output_contract = {
            "json_schema": NpcWorldDecision.model_json_schema(mode="validation"),
        }
        prompt = (
            "You are World Author, not the NPC. Adjudicate the exact NPC-owned proposal without "
            "rewriting its motive, timing, people, place, activity or importance. Return no_op when "
            "the world does not permit it, or accept. For an immediate proposal, accept must include "
            "2-4 genuinely uncertain possible external outcomes. For a future plan, accept has no "
            "outcomes because the eventual occurrence remains unsettled. Return only "
            "NpcWorldDecision JSON with decision and outcomes. The following contract controls "
            "only the JSON wire, not the adjudication: " + _canonical(output_contract)
        )
        return prompt, payload

    async def _run_model_with_one_reselect(
        self, *, model, prompt, payload, parser, validator, failure_reason: str
    ):
        base = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": _canonical(payload)},
        ]
        attempts: list[_ModelAttempt] = []
        failure = ""
        raw = ""
        for ordinal in range(2):
            messages = (
                base
                if ordinal == 0
                else [
                    *base,
                    {"role": "assistant", "content": raw[:16_384]},
                    {
                        "role": "user",
                        "content": (
                            "That result failed this exact boundary: "
                            + failure[:1_000]
                            + ". Re-select once from the same evidence and capabilities."
                        ),
                    },
                ]
            )
            request_json = _canonical(messages)
            try:
                raw = await complete_json_object(
                    model,
                    messages,
                    temperature=0.75 if ordinal == 0 else 0.55,
                )
            except (TimeoutError, httpx.TimeoutException):
                attempts.append(
                    _ModelAttempt(
                        request_json=request_json,
                        raw=None,
                        status="main_timeout",
                        failure_code="main_timeout",
                    )
                )
                raise _NpcModelRunFailed(
                    tuple(attempts),
                    _provider_failure_reason(
                        invalid_reason=failure_reason,
                        failure_code="provider_timeout",
                    ),
                ) from None
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                attempts.append(
                    _ModelAttempt(
                        request_json=request_json,
                        raw=None,
                        status="main_exception",
                        failure_code="main_exception",
                    )
                )
                raise _NpcModelRunFailed(
                    tuple(attempts),
                    _provider_failure_reason(
                        invalid_reason=failure_reason,
                        failure_code=f"provider_http_{status_code}",
                    ),
                ) from None
            except (ConnectionError, OSError, httpx.HTTPError):
                attempts.append(
                    _ModelAttempt(
                        request_json=request_json,
                        raw=None,
                        status="main_exception",
                        failure_code="main_exception",
                    )
                )
                raise _NpcModelRunFailed(
                    tuple(attempts),
                    _provider_failure_reason(
                        invalid_reason=failure_reason,
                        failure_code="provider_unavailable",
                    ),
                ) from None
            try:
                parsed = parser(raw)
                failure = validator(parsed) or ""
            except (ValueError, TypeError) as exc:
                parsed = None
                failure = str(exc)
            if parsed is not None and not failure:
                attempts.append(
                    _ModelAttempt(
                        request_json=request_json,
                        raw=raw,
                        status=("proposal_validated" if ordinal == 0 else "main_invalid_recovered"),
                        failure_code=(None if ordinal == 0 else "main_invalid_output"),
                    )
                )
                return parsed, raw, tuple(attempts)
            attempts.append(
                _ModelAttempt(
                    request_json=request_json,
                    raw=raw,
                    status="main_invalid",
                    failure_code="main_invalid_output",
                )
            )
        attempts[-1] = _ModelAttempt(
            request_json=attempts[-1].request_json,
            raw=attempts[-1].raw,
            status="recovery_failed",
            failure_code="corrective_invalid",
        )
        raise _NpcModelRunFailed(tuple(attempts), failure_reason)

    def _validate_actor_decision(self, decision, *, stimulus, snapshot) -> str | None:
        if decision.npc_ref != self._selected_npc_ref(stimulus=stimulus, snapshot=snapshot):
            return "npc_ecology.npc_actor_scope_changed"
        selected = next(item for item in snapshot.identities if item.npc_ref == decision.npc_ref)
        allowed_sources = {
            *stimulus.source_event_refs,
            *selected.source_refs,
            *selected.private_source_refs,
        }
        if not set(decision.source_refs).issubset(allowed_sources):
            return "npc_ecology.source_closure_failed"
        committed = {
            item.event_id
            for item in self._ledger.project_at(stimulus.cursor).committed_world_event_refs
        }
        if not set(decision.source_refs).issubset(committed):
            return "npc_ecology.source_is_not_committed_event"
        proposal = decision.proposal
        if proposal is None:
            return None
        if (
            proposal.participant_refs != (decision.npc_ref,)
            or self._protagonist in proposal.participant_refs
        ):
            return "npc_ecology.actor_participant_authority_failed"
        if proposal.location_ref not in snapshot.available_location_refs:
            return "npc_ecology.actor_location_closure_failed"
        if stimulus.focus_plan_ref is not None and proposal.timing != "now":
            return "npc_ecology.due_plan_cannot_schedule_another_plan"
        return None

    def _selected_npc_ref(self, *, stimulus, snapshot) -> str:
        """Choose whose opportunity this is without choosing what they do."""

        available = tuple(
            item.npc_ref
            for item in snapshot.identities
            if item.npc_ref in snapshot.available_npc_refs
        )
        if not available:
            raise ValueError("NPC ecology has no source-closed identity capsule")
        if stimulus.focus_npc_ref is not None:
            if stimulus.focus_npc_ref not in available:
                raise ValueError("NPC ecology focused plan owner is unavailable")
            return stimulus.focus_npc_ref
        offset = int(
            _digest(
                {
                    "world": self._ledger.world_id,
                    "epoch": stimulus.epoch_ref,
                    "sources": stimulus.source_event_refs,
                    "lane": "npc_attention_opportunity",
                }
            ),
            16,
        ) % len(available)
        return available[offset]

    def _actor_context_event_refs(self, *, stimulus, snapshot, npc_ref: str) -> tuple[str, ...]:
        selected = next(item for item in snapshot.identities if item.npc_ref == npc_ref)
        committed = {
            item.event_id
            for item in self._ledger.project_at(stimulus.cursor).committed_world_event_refs
        }
        refs = tuple(
            sorted(
                {
                    *stimulus.source_event_refs,
                    *selected.source_refs,
                    *selected.private_source_refs,
                }
                & committed
            )
        )
        return refs[-32:]

    def _model_audit_events(
        self, *, role: Literal["actor", "world"], attempts, stimulus, wake, npc_ref
    ) -> tuple[WorldEvent, ...]:
        if not attempts:
            return ()
        model = self._actor_model if role == "actor" else self._world_author
        model_id = self._model_id(model)
        capsule_id = _digest(
            {
                "role": role,
                "cursor": stimulus.cursor.model_dump(mode="json"),
                "epoch": stimulus.epoch_ref,
                "sources": stimulus.source_event_refs,
                "npc_ref": npc_ref,
            }
        )
        attempt_id = "attempt:npc-ecology:" + _digest(
            {
                "capsule_id": capsule_id,
                "attempts": [
                    {
                        "request": sha256(item.request_json),
                        "response": sha256(item.raw) if item.raw is not None else None,
                    }
                    for item in attempts
                ],
            }
        )
        audits: list[RecordedModelResultAudit] = []
        for ordinal, item in enumerate(attempts):
            response_hash = sha256(item.raw) if item.raw is not None else None
            model_call_id = f"model-call:npc-ecology:{role}:{attempt_id}:{ordinal}"
            model_result_ref = "model-result:" + sha256(
                canonical_json(
                    {
                        "model_call_id": model_call_id,
                        "response_hash": response_hash,
                    }
                )
            )
            has_output = item.raw is not None
            outcome = (
                "winner"
                if item.status in {"proposal_validated", "main_invalid_recovered"}
                else "invalid"
                if item.status in {"main_invalid", "recovery_failed"}
                else "timeout"
                if item.status == "main_timeout"
                else "exception"
            )
            audits.append(
                RecordedModelResultAudit(
                    model_call_id=model_call_id,
                    model_result_ref=model_result_ref,
                    attempt_id=attempt_id,
                    route=RecordedModelRoute(
                        tier="thinking",
                        reason_code=f"npc_ecology_{role}",
                        router_version=_POLICY,
                    ),
                    model_id=model_id if has_output else None,
                    model_version=_POLICY if has_output else None,
                    attempted_model_id=None if has_output else model_id,
                    attempted_model_version=None if has_output else _POLICY,
                    request_hash=sha256(item.request_json),
                    response_hash=response_hash,
                    decision_context=RecordedModelDecisionContext(
                        decision_subject_hash=capsule_id,
                        world_revision=stimulus.cursor.world_revision,
                        deliberation_revision=stimulus.cursor.deliberation_revision,
                        ledger_sequence=stimulus.cursor.ledger_sequence,
                    ),
                    status=item.status,
                    failure_code=item.failure_code,
                    slot="primary" if ordinal == 0 else "corrective",
                    outcome=outcome,
                )
            )
        succeeded = audits[-1].status in {
            "proposal_validated",
            "main_invalid_recovered",
        }
        proposal = (
            MinimalProposal(
                proposal_id="proposal:npc-ecology:model-audit:"
                + _digest({"attempt_id": attempt_id, "role": role}),
                trigger_ref=stimulus.wake_event_ref,
                evaluated_world_revision=stimulus.cursor.world_revision,
                confidence=10_000,
                brief_rationale="Persist the NPC ecology model decision audit.",
                source_model_result=audits[-1].model_result_ref,
                response_text=_canonical({"npc_ref": npc_ref, "role": role}),
                stance="answer_without_world_claims",
            )
            if succeeded
            else None
        )
        proposal_hash = proposal.proposal_hash if proposal is not None else None
        deliberation_result_id = "deliberation:" + sha256(
            canonical_json(
                {
                    "capsule_id": capsule_id,
                    "proposal_hash": proposal_hash,
                    "attempt_audits": [
                        json.loads(model_audit_json(item)) for item in audits
                    ],
                }
            )
        )
        result: list[WorldEvent] = []
        for ordinal, audit in enumerate(audits):
            audit_json = model_audit_json(audit)
            payload = ModelResultRecordedPayload(
                audit_contract="model-result-audit.3",
                model_result_ref=audit.model_result_ref,
                deliberation_result_id=deliberation_result_id,
                proposal_hash=proposal_hash,
                model_call_id=audit.model_call_id,
                attempt_id=attempt_id,
                capsule_id=capsule_id,
                trigger_ref=stimulus.wake_event_ref,
                evaluated_world_revision=stimulus.cursor.world_revision,
                attempt_index=ordinal,
                attempt_count=len(audits),
                audit_json=audit_json,
                audit_hash=sha256(audit_json),
            ).model_dump(mode="json")
            result.append(
                self._event(
                    event_id="event:npc-ecology:model-result:" + role + ":" + _digest(payload),
                    event_type="ModelResultRecorded",
                    payload=payload,
                    logical_time=wake.logical_time,
                    actor=npc_ref if role == "actor" else self._worker_actor,
                    causation_id=stimulus.wake_event_ref,
                )
            )
        if proposal is not None:
            proposal_json = canonical_json(proposal.model_dump(mode="json"))
            proposal_payload = ProposalRecordedV2Payload(
                proposal_id=proposal.proposal_id,
                proposal_kind=proposal.proposal_kind,
                model_result_ref=audits[-1].model_result_ref,
                deliberation_result_id=deliberation_result_id,
                model_call_id=audits[-1].model_call_id,
                attempt_id=attempt_id,
                capsule_id=capsule_id,
                trigger_ref=stimulus.wake_event_ref,
                evaluated_world_revision=stimulus.cursor.world_revision,
                proposal_json=proposal_json,
                proposal_hash=proposal.proposal_hash,
            ).model_dump(mode="json")
            result.append(
                self._event(
                    event_id="event:npc-ecology:model-proposal:" + _digest(proposal_payload),
                    event_type="ProposalRecorded",
                    payload=proposal_payload,
                    logical_time=wake.logical_time,
                    actor=npc_ref if role == "actor" else self._worker_actor,
                    causation_id=result[-1].event_id,
                )
            )
        return tuple(result)

    def _commit_model_audits(self, *, role, attempts, stimulus, wake, npc_ref) -> None:
        projection = self._ledger.project()
        events = self._model_audit_events(
            role=role,
            attempts=attempts,
            stimulus=stimulus,
            wake=wake,
            npc_ref=npc_ref,
        )
        if not events:
            return
        self._ledger.commit_at_cursor(
            events,
            expected_cursor=_cursor(projection),
            commit_id="commit:npc-ecology:model-failure:"
            + _digest([item.event_id for item in events]),
        )

    def _record_actor_state(
        self, *, projection, wake, stimulus, decision, raw: str, event_id: str
    ) -> bool:
        npc_id = decision.npc_ref.removeprefix("npc:")
        before = next(item for item in projection.npcs if item.npc_id == npc_id)
        snapshot = self.snapshot(_cursor(projection))
        context_event_refs = self._actor_context_event_refs(
            stimulus=stimulus,
            snapshot=snapshot,
            npc_ref=decision.npc_ref,
        )
        state_ref = "content:npc-ecology:state:" + _digest(
            {"event": event_id, "text": decision.inner_state_summary}
        )
        state_hash = life_content_payload_hash(decision.inner_state_summary)
        self._store.put_if_absent(
            StoredLifeContent(
                content_ref=state_ref,
                content_kind="npc_inner_state",
                content_payload_hash=state_hash,
                text=decision.inner_state_summary,
            )
        )
        goal_refs: list[str] = []
        goal_hashes: list[str] = []
        for index, text in enumerate(decision.current_goal_summaries):
            ref = "content:npc-ecology:goal:" + _digest(
                {"event": event_id, "index": index, "text": text}
            )
            self._store.put_if_absent(
                StoredLifeContent(
                    content_ref=ref,
                    content_kind="npc_goal",
                    content_payload_hash=life_content_payload_hash(text),
                    text=text,
                )
            )
            goal_refs.append(ref)
            goal_hashes.append(life_content_payload_hash(text))
        state = NpcSubjectiveState(
            subject_ref=self._protagonist,
            inner_state_content_ref=state_ref,
            inner_state_payload_hash=state_hash,
            relationship_to_subject=decision.relationship_to_protagonist,
            goal_content_refs=tuple(sorted(goal_refs)),
            goal_content_hashes=tuple(
                value for _, value in sorted(zip(goal_refs, goal_hashes, strict=True))
            ),
            organization_refs=(
                before.subjective_state.organization_refs
                if before.subjective_state is not None
                else ()
            ),
            life_arc_refs=(
                before.subjective_state.life_arc_refs if before.subjective_state is not None else ()
            ),
            source_event_refs=context_event_refs,
            pending_actor_event_ref=(event_id if decision.decision == "propose" else None),
            pending_impulse_summary=(
                decision.impulse_summary if decision.decision == "propose" else None
            ),
            pending_impulse_source_refs=(
                context_event_refs if decision.decision == "propose" else ()
            ),
            evolved_at=wake.logical_time,
        )
        after = before.model_copy(
            update={
                "entity_revision": before.entity_revision + 1,
                "subjective_state": state,
            }
        )
        evidence = tuple(self._evidence(projection, ref) for ref in context_event_refs)
        state_payload = NpcStateChangedPayload(
            change_id="change:npc-ecology:state:" + _digest(event_id),
            transition_id="transition:npc-ecology:state:" + _digest(event_id),
            expected_entity_revision=before.entity_revision,
            evidence_refs=evidence,
            policy_refs=(_POLICY,),
            npc_before=before,
            npc_after=after,
        ).model_dump(mode="json")
        proposal_id = "proposal:npc-ecology:" + _digest(
            {
                "world": self._ledger.world_id,
                "epoch": stimulus.epoch_ref,
                "sources": stimulus.source_event_refs,
            }
        )
        proposal_payload = {
            "proposal_id": proposal_id,
            "proposal_kind": "npc_ecology",
            "trigger_id": stimulus.wake_event_ref,
            "evaluated_world_revision": projection.world_revision,
            "epoch_ref": stimulus.epoch_ref,
            "source_event_refs": list(stimulus.source_event_refs),
            "context_event_refs": list(context_event_refs),
            "context_payload_hash": "sha256:"
            + _digest(self._actor_request(stimulus=stimulus, snapshot=snapshot)[1]),
            "decision_payload": decision.model_dump(mode="json"),
            "model": self._model_id(self._actor_model),
            "raw_output_hash": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
        }
        proposal_event = self._event(
            event_id=event_id,
            event_type="ProposalRecorded",
            payload=proposal_payload,
            logical_time=wake.logical_time,
            actor=decision.npc_ref,
            causation_id=stimulus.wake_event_ref,
        )
        state_event = self._event(
            event_id="event:npc-ecology:state:" + _digest(event_id),
            event_type="NpcStateChanged",
            payload=state_payload,
            logical_time=wake.logical_time,
            actor=decision.npc_ref,
            causation_id=proposal_event.event_id,
        )
        content_bindings = (
            ("npc_inner_state", state_ref, state_hash),
            *tuple(
                ("npc_goal", ref, content_hash)
                for ref, content_hash in zip(
                    state.goal_content_refs,
                    state.goal_content_hashes,
                    strict=True,
                )
            ),
        )
        descriptor_events = tuple(
            self._event(
                event_id="event:npc-ecology:content:"
                + _digest({"state_event": state_event.event_id, "content_ref": ref}),
                event_type="LifeContentRecorded",
                payload=LifeContentRecordedPayload(
                    content_id="npc-content:"
                    + _digest({"state_event": state_event.event_id, "content_ref": ref}),
                    content_kind=kind,
                    content_ref=ref,
                    content_payload_hash=content_hash,
                    privacy_class=before.privacy_class,
                    source_kind="npc_state",
                    source_event_ref=state_event.event_id,
                    source_world_revision=projection.world_revision + 1,
                    source_payload_hash=state_event.payload_hash,
                    source_entity_id=before.npc_id,
                    source_entity_revision=after.entity_revision,
                ).model_dump(mode="json"),
                logical_time=wake.logical_time,
                actor=self._worker_actor,
                causation_id=state_event.event_id,
            )
            for kind, ref, content_hash in content_bindings
        )
        try:
            self._ledger.commit_at_cursor(
                (proposal_event, state_event, *descriptor_events),
                expected_cursor=_cursor(projection),
                commit_id="commit:npc-ecology:actor:" + _digest(event_id),
            )
        except Exception as exc:
            if type(exc).__name__ == "ConcurrencyConflict":
                return False
            raise
        return True

    async def _materialize(
        self, *, stimulus, snapshot, wake, actor_event_id, actor_decision, identity
    ) -> NpcEcologyResult:
        world_event_id = f"event:npc-ecology:world:{identity}"
        existing_world = self._ledger.lookup_event_commit(world_event_id)
        if existing_world is not None:
            world_decision = NpcWorldDecision.model_validate_json(
                _canonical(existing_world[0].payload()["decision_payload"])
            )
        else:
            try:
                world_decision, raw, world_attempts = await self._world_decide(
                    stimulus=stimulus,
                    snapshot=snapshot,
                    actor_decision=actor_decision,
                )
            except _NpcModelRunFailed as failure:
                self._commit_model_audits(
                    role="world",
                    attempts=failure.attempts,
                    stimulus=stimulus,
                    wake=wake,
                    npc_ref=actor_decision.npc_ref,
                )
                return NpcEcologyResult(
                    status="technical_failure",
                    reason_code=failure.reason,
                    npc_ref=actor_decision.npc_ref,
                    decision_event_ref=actor_event_id,
                )
            self._commit_model_audits(
                role="world",
                attempts=world_attempts,
                stimulus=stimulus,
                wake=wake,
                npc_ref=actor_decision.npc_ref,
            )
            projection = self._ledger.project()
            payload = {
                "proposal_id": "proposal:npc-ecology-world:" + identity,
                "proposal_kind": "npc_ecology_world_adjudication",
                "trigger_id": actor_event_id,
                "evaluated_world_revision": projection.world_revision,
                "actor_decision_event_ref": actor_event_id,
                "decision_payload": world_decision.model_dump(mode="json"),
                "model": self._model_id(self._world_author),
                "raw_output_hash": "sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
            }
            event = self._event(
                event_id=world_event_id,
                event_type="ProposalRecorded",
                payload=payload,
                logical_time=wake.logical_time,
                actor=self._worker_actor,
                causation_id=actor_event_id,
            )
            self._ledger.commit_at_cursor(
                (event,),
                expected_cursor=_cursor(projection),
                commit_id="commit:npc-ecology:world:" + identity,
            )
        if world_decision.decision == "no_op":
            return NpcEcologyResult(
                status="state_advanced",
                reason_code="npc_ecology.world_did_not_materialize",
                npc_ref=actor_decision.npc_ref,
                decision_event_ref=actor_event_id,
            )
        proposal = actor_decision.proposal
        if proposal is None:
            return NpcEcologyResult(
                status="technical_failure",
                reason_code="npc_ecology.accepted_actor_proposal_missing",
                npc_ref=actor_decision.npc_ref,
                decision_event_ref=actor_event_id,
            )
        if proposal.timing == "later":
            plan_id = "plan:npc-ecology:" + identity
            existed = any(item.plan_id == plan_id for item in self._ledger.project().plans)
            if not existed:
                self._commit_plan(
                    wake=wake,
                    world_event_id=world_event_id,
                    actor_proposal=proposal,
                    actor_decision=actor_decision,
                    plan_id=plan_id,
                    identity=identity,
                )
            return NpcEcologyResult(
                status=("recovered" if existed else "plan_committed"),
                reason_code="npc_ecology.npc_plan_entered_event_machine",
                npc_ref=actor_decision.npc_ref,
                decision_event_ref=actor_event_id,
            )
        if stimulus.focus_plan_ref is not None:
            self._transition_npc_plan(
                wake=wake,
                plan_id=stimulus.focus_plan_ref,
                target="start",
                reason_event_ref=(
                    next(
                        item.authority_origin.accepted_event_ref
                        for item in self._ledger.project().plans
                        if item.plan_id == stimulus.focus_plan_ref
                        and item.authority_origin is not None
                    )
                ),
                identity=identity,
            )
        occurrence_id = "occurrence:npc-ecology:" + identity
        projection = self._ledger.project()
        existing_occurrence = next(
            (item for item in projection.world_occurrences if item.occurrence_id == occurrence_id),
            None,
        )
        if existing_occurrence is None:
            self._commit_occurrence(
                projection=projection,
                wake=wake,
                world_event_id=world_event_id,
                world_decision=world_decision,
                actor_proposal=proposal,
                occurrence_id=occurrence_id,
                identity=identity,
                focus_plan_ref=stimulus.focus_plan_ref,
            )
        self._activate(wake=wake, occurrence_id=occurrence_id, identity=identity)
        return NpcEcologyResult(
            status=("recovered" if existing_world else "occurrence_committed"),
            reason_code="npc_ecology.occurrence_entered_event_machine",
            npc_ref=actor_decision.npc_ref,
            decision_event_ref=actor_event_id,
            occurrence_id=occurrence_id,
        )

    def _validate_world_decision(self, decision, *, stimulus, snapshot, actor_decision):
        if decision.decision == "no_op":
            return None
        proposal = actor_decision.proposal
        if proposal is None:
            return "npc_ecology.world_actor_proposal_missing"
        if proposal.timing == "now" and len(decision.outcomes) < 2:
            return "npc_ecology.world_outcomes_missing"
        if proposal.timing == "later" and decision.outcomes:
            return "npc_ecology.world_plan_cannot_prejudge_outcomes"
        return None

    def _complete_settled_npc_plan(self, *, projection, wake):
        for plan in projection.plans:
            if (
                plan.status != "active"
                or not isinstance(plan.owner_actor_ref, str)
                or not plan.owner_actor_ref.startswith("npc:")
            ):
                continue
            occurrence = next(
                (
                    item
                    for item in projection.world_occurrences
                    if item.trigger_ref == plan.plan_id
                    and item.status == "settled"
                    and item.settlement_event_ref is not None
                ),
                None,
            )
            if occurrence is None:
                continue
            self._transition_npc_plan(
                wake=wake,
                plan_id=plan.plan_id,
                target="complete",
                reason_event_ref=occurrence.settlement_event_ref,
                identity=_digest(
                    {
                        "plan": plan.plan_id,
                        "revision": plan.entity_revision,
                        "settlement": occurrence.settlement_event_ref,
                    }
                ),
            )
            return plan
        return None

    def _transition_npc_plan(
        self, *, wake, plan_id, target: Literal["start", "complete"], reason_event_ref, identity
    ) -> None:
        projection = self._ledger.project()
        plan = next(item for item in projection.plans if item.plan_id == plan_id)
        expected_status = "planned" if target == "start" else "active"
        if plan.status != expected_status:
            return
        evidence = self._evidence(projection, reason_event_ref).model_copy(
            update={"claim_purpose": "life_transition"}
        )
        payload = ActivityTransitionPayload(
            change_id=f"change:npc-ecology:plan:{target}:" + identity,
            transition_id=f"transition:npc-ecology:plan:{target}:" + identity,
            expected_entity_revision=plan.entity_revision,
            evidence_refs=(evidence,),
            policy_refs=(_POLICY,),
            plan_id=plan.plan_id,
            transitioned_at=wake.logical_time,
            reason_ref=reason_event_ref,
        ).model_dump(mode="json")
        event_type = "ActivityStarted" if target == "start" else "ActivityCompleted"
        event = self._event(
            event_id=f"event:npc-ecology:plan:{target}:" + identity,
            event_type=event_type,
            payload=payload,
            logical_time=wake.logical_time,
            actor=plan.owner_actor_ref or self._worker_actor,
            causation_id=reason_event_ref,
        )
        self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=_cursor(projection),
            commit_id=f"commit:npc-ecology:plan:{target}:" + identity,
        )

    def _commit_plan(
        self,
        *,
        wake,
        world_event_id,
        actor_proposal,
        actor_decision,
        plan_id,
        identity,
    ) -> None:
        assert actor_proposal.activity_kind is not None
        assert actor_proposal.scheduled_start_after_minutes is not None
        assert actor_proposal.importance_bp is not None
        projection = self._ledger.project()
        opens_at = wake.logical_time + timedelta(
            minutes=actor_proposal.scheduled_start_after_minutes
        )
        evidence = self._evidence(projection, wake.event_id).model_copy(
            update={"claim_purpose": "future_plan"}
        )
        plan = PlanStateProjection(
            plan_id=plan_id,
            activity_id="activity:npc-ecology:" + identity,
            entity_revision=1,
            activity_kind=actor_proposal.activity_kind,
            evidence_refs=(evidence,),
            status="planned",
            importance_bp=actor_proposal.importance_bp,
            scheduled_window=DueWindow(
                opens_at=opens_at,
                closes_at=opens_at + timedelta(minutes=actor_proposal.duration_minutes),
            ),
            participant_refs=actor_proposal.participant_refs,
            location_ref=actor_proposal.location_ref,
            privacy_class=actor_proposal.visibility,
            owner_actor_ref=actor_decision.npc_ref,
        )
        payload = ActivityPlannedPayload(
            change_id="change:npc-ecology:plan:" + identity,
            transition_id="transition:npc-ecology:plan:" + identity,
            expected_entity_revision=0,
            evidence_refs=(evidence,),
            policy_refs=(_POLICY,),
            plan=plan,
        ).model_dump(mode="json")
        event = self._event(
            event_id="event:npc-ecology:plan:" + identity,
            event_type="ActivityPlanned",
            payload=payload,
            logical_time=wake.logical_time,
            actor=actor_decision.npc_ref,
            causation_id=world_event_id,
        )
        self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=_cursor(projection),
            commit_id="commit:npc-ecology:plan:" + identity,
        )

    def _commit_occurrence(
        self,
        *,
        projection,
        wake,
        world_event_id,
        world_decision,
        actor_proposal,
        occurrence_id,
        identity,
        focus_plan_ref=None,
    ) -> None:
        candidates = tuple(
            OutcomeCandidateContent(
                candidate_result_ref=f"candidate:npc-ecology:{identity}:{index}",
                result_id=f"result:npc-ecology:{identity}:{index}",
                result_payload_ref=f"content:npc-ecology:result:{identity}:{index}",
                result_payload_hash=life_content_payload_hash(item.text),
                privacy_class=item.privacy,
                content_ref=f"content:npc-ecology:candidate:{identity}:{index}",
                text=item.text,
                # These branches describe what the World Author says may
                # objectively happen to the NPC.  They are not choices owned
                # by the protagonist (who may not even be present).
                causal_authority="world_contingency",
            )
            for index, item in enumerate(world_decision.outcomes, start=1)
        )
        occurrence = WorldOccurrenceProjection(
            occurrence_id=occurrence_id,
            entity_revision=1,
            trigger_ref=focus_plan_ref or world_event_id,
            participant_refs=actor_proposal.participant_refs,
            location_ref=actor_proposal.location_ref,
            time_window=DueWindow(
                opens_at=wake.logical_time,
                closes_at=wake.logical_time + timedelta(minutes=actor_proposal.duration_minutes),
            ),
            precondition_refs=((f"plan:{focus_plan_ref}",) if focus_plan_ref is not None else ()),
            candidate_outcome_refs=tuple(item.candidate_result_ref for item in candidates),
            visibility=actor_proposal.visibility,
            status="committed",
        )
        self._occurrence_content.commit(
            OccurrenceContentCommitRequest(
                world_id=self._ledger.world_id,
                occurrence=occurrence,
                candidate_contents=candidates,
                change_id="change:npc-ecology:occurrence:" + identity,
                transition_id="transition:npc-ecology:occurrence:" + identity,
                evidence_refs=(self._evidence(projection, wake.event_id),),
                policy_refs=(_POLICY,),
                logical_time=wake.logical_time,
                created_at=wake.logical_time,
                actor=self._worker_actor,
                source="world-v2:npc-ecology",
                trace_id="trace:npc-ecology:" + identity,
                causation_id=world_event_id,
                correlation_id="correlation:npc-ecology:" + identity,
            )
        )

    def _activate(self, *, wake, occurrence_id: str, identity: str) -> None:
        projection = self._ledger.project()
        occurrence = next(
            item for item in projection.world_occurrences if item.occurrence_id == occurrence_id
        )
        if occurrence.status != "committed":
            return
        payload = WorldOccurrenceActivatedPayload(
            change_id="change:npc-ecology:activate:" + identity,
            transition_id="transition:npc-ecology:activate:" + identity,
            expected_entity_revision=occurrence.entity_revision,
            evidence_refs=(self._evidence(projection, wake.event_id),),
            policy_refs=(_POLICY,),
            occurrence_id=occurrence_id,
            activated_at=wake.logical_time,
            satisfied_precondition_refs=occurrence.precondition_refs,
        ).model_dump(mode="json")
        event_id = "event:npc-ecology:activated:" + identity
        if self._ledger.lookup_event_commit(event_id) is not None:
            return
        event = self._event(
            event_id=event_id,
            event_type="WorldOccurrenceActivated",
            payload=payload,
            logical_time=wake.logical_time,
            actor=self._worker_actor,
            causation_id=occurrence_id,
        )
        self._ledger.commit_at_cursor(
            (event,),
            expected_cursor=_cursor(projection),
            commit_id="commit:npc-ecology:activate:" + identity,
        )

    @staticmethod
    def _evidence(projection, ref: str) -> EvidenceRef:
        authority = next(
            item for item in projection.committed_world_event_refs if item.event_id == ref
        )
        evidence_type = (
            "settled_world_event"
            if authority.event_type == "WorldOccurrenceSettled"
            else "committed_world_event"
        )
        return EvidenceRef(
            ref_id=ref,
            evidence_type=evidence_type,
            claim_purpose="private_hypothesis",
            source_world_revision=authority.world_revision,
            immutable_hash=authority.payload_hash,
        )

    def _event(self, *, event_id, event_type, payload, logical_time, actor, causation_id):
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            event_type=event_type,
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=logical_time,
            actor=actor,
            source="world-v2:npc-ecology",
            trace_id="trace:" + event_id,
            causation_id=causation_id,
            correlation_id="correlation:" + event_id,
            idempotency_key=domain_idempotency_key(
                event_type=event_type,
                world_id=self._ledger.world_id,
                payload=payload,
            )
            or "npc-ecology:" + _digest(event_id),
            payload=payload,
        )

    @staticmethod
    def _model_id(model: object) -> str:
        return str(getattr(model, "model", "")).strip() or type(model).__name__


__all__ = [
    "NpcActorDecision",
    "NpcEcology",
    "NpcEcologyModel",
    "NpcEcologyResult",
    "NpcEcologyStimulus",
    "NpcSocialWorldSnapshot",
    "NpcWorldDecision",
    "NpcWorldOutcomeDraft",
]
