"""Activity aftermath: occurrence, settlement, experience, and content."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Literal

import httpx

from .batch_invariants import appraisal_trigger_identity
from .context_resolver import query_from_projection
from .contextual_life_retry import (
    record_technical_failure as record_contextual_life_technical_failure,
    retry_for as contextual_life_retry_for,
)
from .event_identity import domain_idempotency_key
from .errors import ConcurrencyConflict, IdempotencyConflict
from .experience_memory_candidate_lifecycle import ExperienceMemoryCandidateLifecycle
from .experience_memory_decision import (
    ExperienceMemoryDecisionRecordedPayload,
    canonical_experience_memory_decision_json,
    experience_memory_decision_event_id,
    experience_memory_decision_hash,
    experience_memory_decision_identity,
)
from .experience_events import ExperienceCommittedPayload, experience_mutation_hash
from .fact_memory_draft import (
    FactMemoryDraftAdapter,
    FactMemoryDraftTechnicalFailure,
    FactMemoryRetentionDraft,
)
from .life_author_seed import ReviewedLifeSeedCatalog
from .life_content_events import LifeContentRecordedPayload
from .life_content_store import (
    ImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from .life_development_runtime import LifeDevelopmentProposalReader
from .life_events import (
    OutcomeObservationRecordedPayload,
    OutcomeProposalRecordedPayload,
    WorldOccurrenceActivatedPayload,
    WorldOccurrenceSettledPayload,
    outcome_mutation_hash,
)
from .life_author_runtime import (
    LifeContextCapsuleCompiler,
    compile_life_decision_context,
)
from .occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OccurrenceContentCoordinator,
    OutcomeCandidateContent,
)
from .mood_view import mood_summary_prose
from .outcome_selection_draft import (
    OutcomeSelectionDraft,
    OutcomeSelectionDraftAdapter,
    OutcomeSelectionFailure,
    OutcomeSelectionModel,
    OutcomeSelectionOption,
    outcome_selection_audit_text,
)
from .plan_evidence import canonical_plan_evidence_hash
from .proposal_audit_schemas import (
    ModelResultRecordedPayload,
    ProposalRecordedV2Payload,
    RecordedModelResultAudit,
    RecordedModelRoute,
    canonical_json,
    model_audit_json,
    sha256,
)
from .proposal_envelope import MinimalProposal
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import (
    BiographicalCoordinateReplacement,
    DueWindow,
    EvidenceRef,
    ExperienceOccurrenceSettlementBinding,
    ExperienceOrigin,
    ExperienceProjection,
    ExperienceProposalProjection,
    ExperienceProposedMutation,
    ExperienceValues,
    OutcomeObservationProjection,
    ProjectionCursor,
    RecordedWorldDrawBinding,
    TriggerProcess,
    WorldEvent,
    WorldOccurrenceProjection,
    experience_semantic_fingerprint,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _candidate_matrix_hash(occurrence) -> str:
    return sha256(
        canonical_json(
            [
                item.model_dump(mode="json")
                for item in occurrence.candidate_outcomes
            ]
        )
    )


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


_PRIVACY_RANK = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def _experience_privacy(source_privacy: str) -> str:
    """Keep lived history internal even when its source may be shared.

    The experience authority requires ``past_experience`` evidence to be at
    least personal.  A shareable occurrence describes what may be disclosed;
    it must not broaden the companion's internal autobiographical record.
    """

    return (
        source_privacy if _PRIVACY_RANK[source_privacy] >= _PRIVACY_RANK["personal"] else "personal"
    )


_LOG = logging.getLogger(__name__)


class LifeAftermathModelFailure(RuntimeError):
    """A character-owned consequential outcome could not be decided."""

    def __init__(self, message: str, *, failure_code: str = "unknown") -> None:
        self.failure_code = failure_code
        super().__init__(message)


class _LifeAftermathRetryWait(RuntimeError):
    """The occurrence-owned model lane is waiting for its recorded retry due time."""


class LifeAftermathResult(FrozenModel):
    status: Literal[
        "occurrence_opened",
        "settled",
        "recovered_experience",
        "recovered_memory",
        "retry_wait",
        "no_op",
    ]
    reason_code: str
    occurrence_id: str | None = None
    experience_id: str | None = None


class LifeAftermathRuntime:
    """One bounded authority seam from accepted activity to lived history.

    Legacy activities read their frozen candidates from the reviewed replay
    catalog; open LifeDevelopment plans rehydrate model-authored candidates
    from the accepted Proposal and exact sidecars. World contingencies resolve
    through recorded weighted randomness, character-controlled consequences
    require the Character Model, and external results require a matching
    observation. No branch may invent identities or content during settlement.
    """

    def __init__(
        self,
        *,
        ledger,
        catalog: ReviewedLifeSeedCatalog,
        occurrence_content: OccurrenceContentCoordinator,
        content_store: ImmutableLifeContentStore,
        owner_actor_ref: str,
        capsule_compiler: LifeContextCapsuleCompiler,
        experience_memory_lifecycle: ExperienceMemoryCandidateLifecycle | None = None,
        outcome_selection_model: OutcomeSelectionModel | None = None,
        memory_adapter: FactMemoryDraftAdapter | None = None,
        actor: str = "worker:world-v2:life-aftermath",
    ) -> None:
        if occurrence_content.ledger is not ledger:
            raise ValueError("life aftermath occurrence coordinator must own the exact ledger")
        if not owner_actor_ref or not actor:
            raise ValueError("life aftermath requires owner and worker actors")
        self._ledger = ledger
        self._catalog = catalog
        self._occurrence_content = occurrence_content
        self._content_store = content_store
        self._life_development_proposals = LifeDevelopmentProposalReader(
            ledger=ledger,
            content_store=content_store,
        )
        if (
            experience_memory_lifecycle is not None
            and experience_memory_lifecycle._ledger is not ledger  # noqa: SLF001
        ):
            raise ValueError("life aftermath memory lifecycle must own the exact ledger")
        self._experience_memory_lifecycle = experience_memory_lifecycle
        self._outcome_selection = (
            OutcomeSelectionDraftAdapter(model=outcome_selection_model)
            if outcome_selection_model is not None
            else None
        )
        self._capsule_compiler = capsule_compiler
        self._memory_adapter = memory_adapter
        self._owner_actor_ref = owner_actor_ref
        self._actor = actor
        self._random = RandomAuthority(ledger=ledger, source="world-v2:life-aftermath-random")
        self._memory_decision_locks: dict[str, asyncio.Lock] = {}

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> LifeAftermathResult:
        projection = self._ledger.project()
        wake = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == wake_event_ref
                and item.event_type in {"ClockAdvanced", "ActivityCompleted", "ActivityAbandoned"}
            ),
            None,
        )
        if (
            wake is None
            or projection.logical_time is None
            or wake.logical_time > projection.logical_time
        ):
            return LifeAftermathResult(
                status="no_op", reason_code="life_aftermath.wake_unavailable"
            )

        if self._experience_memory_lifecycle is not None:
            settled_experience_ids = {
                binding.source_id
                for candidate in projection.memory_candidates
                if candidate.values.status != "pending"
                for binding in candidate.values.source_bindings
                if binding.source_kind == "experience"
            }
            settled_experience_ids.update(
                item.experience_id
                for item in projection.experiences
                if isinstance(item, ExperienceProjection)
                and self._has_no_change_memory_decision(item)
            )
            unremembered = next(
                (
                    item
                    for item in projection.experiences
                    if isinstance(item, ExperienceProjection)
                    and item.experience_id not in settled_experience_ids
                ),
                None,
            )
            if unremembered is not None:
                materialized = await self._materialize_experience_memory(
                    experience_id=unremembered.experience_id,
                    logical_time=projection.logical_time,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                if materialized == "retained":
                    return LifeAftermathResult(
                        status="recovered_memory",
                        reason_code="life_aftermath.experience_memory_recovered",
                        experience_id=unremembered.experience_id,
                    )
                projection = self._ledger.project()

        recoverable = next(
            (
                item
                for item in projection.world_occurrences
                if item.status == "settled"
                and not self._has_experience(projection, item.occurrence_id)
            ),
            None,
        )
        if recoverable is not None:
            experience_id = await self._commit_experience(
                occurrence=recoverable,
                logical_time=projection.logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            return LifeAftermathResult(
                status="recovered_experience",
                reason_code="life_aftermath.experience_recovered",
                occurrence_id=recoverable.occurrence_id,
                experience_id=experience_id,
            )

        active = next(
            (
                item
                for item in projection.world_occurrences
                if item.status == "active"
                and item.activated_at is not None
                and (
                    item.activated_at < wake.logical_time
                    or wake.event_type in {"ActivityCompleted", "ActivityAbandoned"}
                )
            ),
            None,
        )
        if active is not None:
            try:
                experience_id = await self._settle(
                    occurrence=active,
                    wake=wake,
                    logical_time=projection.logical_time,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
            except _LifeAftermathRetryWait:
                return LifeAftermathResult(
                    status="retry_wait",
                    reason_code="life_aftermath.outcome_retry_wait",
                    occurrence_id=active.occurrence_id,
                )
            return LifeAftermathResult(
                status="settled",
                reason_code="life_aftermath.settled",
                occurrence_id=active.occurrence_id,
                experience_id=experience_id,
            )

        existing_plan_ids = {item.trigger_ref for item in projection.world_occurrences}
        plan = next(
            (
                item
                for item in projection.plans
                if item.owner_actor_ref == self._owner_actor_ref
                and item.status == "active"
                and item.plan_id not in existing_plan_ids
                and (
                    self._catalog.outcomes_for_activity(item.activity_kind)
                    or self._life_development_proposals.read_for_plan(
                        plan_id=item.plan_id
                    )
                    is not None
                )
            ),
            None,
        )
        if plan is None:
            return LifeAftermathResult(
                status="no_op", reason_code="life_aftermath.no_eligible_activity"
            )
        occurrence_id = self._open_occurrence(
            plan=plan,
            wake=wake,
            logical_time=projection.logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return LifeAftermathResult(
            status="occurrence_opened",
            reason_code="life_aftermath.occurrence_opened",
            occurrence_id=occurrence_id,
        )

    def _open_occurrence(
        self, *, plan, wake, logical_time: datetime, trace_id: str, correlation_id: str
    ) -> str:
        outcomes = self._catalog.outcomes_for_activity(plan.activity_kind)
        dynamic = (
            None
            if outcomes
            else self._life_development_proposals.read_for_plan(
                plan_id=plan.plan_id
            )
        )
        outcome_authority = (
            "character_choice"
            if any(item.life_arc_effect is not None for item in outcomes)
            else "world_contingency"
        )
        suffix = _digest({"world": self._ledger.world_id, "plan": plan.plan_id})
        occurrence_id = "occurrence:life-aftermath:" + suffix
        existing = self._ledger.project()
        if any(item.occurrence_id == occurrence_id for item in existing.world_occurrences):
            return occurrence_id
        candidate_contents = (
            tuple(
                OutcomeCandidateContent(
                    candidate_result_ref=(
                        f"candidate:life-aftermath:{suffix}:{item.id}"
                    ),
                    result_id=f"result:life-aftermath:{suffix}:{item.id}",
                    result_payload_ref=(
                        f"content:occurrence-result:{suffix}:{item.id}"
                    ),
                    result_payload_hash=life_content_payload_hash(item.text),
                    privacy_class=item.privacy,
                    content_ref=(
                        f"content:outcome-candidate:{suffix}:{item.id}"
                    ),
                    text=item.text,
                    causal_authority=outcome_authority,
                    life_arc_effect=(
                        self._catalog.frozen_life_arc_effect_for_outcome(
                            activity_kind=plan.activity_kind,
                            outcome_id=item.id,
                        )
                    ),
                )
                for item in outcomes
            )
            if outcomes
            else tuple(
                OutcomeCandidateContent(
                    candidate_result_ref=item.descriptor.candidate_result_ref,
                    result_id=item.descriptor.result_id,
                    result_payload_ref=item.descriptor.result_payload_ref,
                    result_payload_hash=item.descriptor.result_payload_hash,
                    privacy_class=item.descriptor.privacy_class,
                    content_ref=item.descriptor.content_ref or "",
                    text=item.text,
                    life_arc_effect=item.descriptor.life_arc_effect,
                    causal_authority=item.descriptor.causal_authority,
                    relative_plausibility_weight=(
                        item.descriptor.relative_plausibility_weight
                    ),
                    provisional_npc_introductions=(
                        item.descriptor.provisional_npc_introductions
                    ),
                    dynamic_life_arc_context=(
                        item.descriptor.dynamic_life_arc_context
                    ),
                )
                for item in dynamic.outcomes
            )
        )
        wake_evidence = self._event_evidence(wake, purpose="life_transition")
        plan_evidence = EvidenceRef(
            ref_id=plan.plan_id,
            evidence_type="active_plan",
            claim_purpose="life_transition",
            immutable_hash=canonical_plan_evidence_hash(plan),
        )
        occurrence = WorldOccurrenceProjection(
            occurrence_id=occurrence_id,
            entity_revision=1,
            trigger_ref=plan.plan_id,
            participant_refs=tuple(dict.fromkeys((self._owner_actor_ref, *plan.participant_refs))),
            location_ref=plan.location_ref,
            time_window=DueWindow(
                opens_at=logical_time,
                closes_at=max(
                    logical_time + timedelta(minutes=5),
                    plan.scheduled_window.closes_at if plan.scheduled_window else logical_time,
                ),
            ),
            candidate_outcome_refs=tuple(item.candidate_result_ref for item in candidate_contents),
            visibility=plan.privacy_class,
            status="committed",
        )
        self._occurrence_content.commit(
            OccurrenceContentCommitRequest(
                world_id=self._ledger.world_id,
                occurrence=occurrence,
                candidate_contents=candidate_contents,
                change_id="change:life-aftermath:occurrence:" + suffix,
                transition_id="transition:life-aftermath:occurrence:" + suffix,
                evidence_refs=(plan_evidence, wake_evidence),
                policy_refs=("policy:life-aftermath.1",),
                logical_time=logical_time,
                created_at=logical_time,
                actor=self._actor,
                source="world-v2:life-aftermath",
                trace_id=trace_id,
                causation_id=wake.event_id,
                correlation_id=correlation_id,
            )
        )
        projected = self._ledger.project()
        committed = next(
            item for item in projected.world_occurrences if item.occurrence_id == occurrence_id
        )
        payload = WorldOccurrenceActivatedPayload(
            change_id="change:life-aftermath:activate:" + suffix,
            transition_id="transition:life-aftermath:activate:" + suffix,
            expected_entity_revision=1,
            evidence_refs=(wake_evidence,),
            policy_refs=("policy:life-aftermath.1",),
            occurrence_id=occurrence_id,
            activated_at=logical_time,
            satisfied_precondition_refs=(),
        )
        event = self._event(
            event_id="event:life-aftermath:activate:" + suffix,
            event_type="WorldOccurrenceActivated",
            payload=payload.model_dump(mode="json"),
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=wake.event_id,
            correlation_id=correlation_id,
        )
        self._commit((event,), commit_id="commit:life-aftermath:activate:" + suffix)
        return committed.occurrence_id

    async def _settle(
        self, *, occurrence, wake, logical_time: datetime, trace_id: str, correlation_id: str
    ) -> str:
        wake_evidence = self._event_evidence(wake, purpose="life_transition")
        suffix = occurrence.occurrence_id.removeprefix("occurrence:life-aftermath:")
        # Settlement observation is effect-once per occurrence.  A character
        # model failure may leave the observation committed while the outcome
        # remains undecided; a later clock wake must recover that same
        # observation instead of trying to write different content through the
        # occurrence-stable commit id.
        matrix_authority = occurrence.candidate_outcomes[0].causal_authority
        projected_before_observation = self._ledger.project()
        external_observation = (
            next(
                (
                    item
                    for item in projected_before_observation.outcome_observations
                    if item.occurrence_id == occurrence.occurrence_id
                    and item.source_kind == "settled_external_result"
                ),
                None,
            )
            if matrix_authority == "external_observation"
            else None
        )
        if matrix_authority == "external_observation":
            if external_observation is None:
                raise ValueError(
                    "external outcome is waiting for a settled external observation"
                )
            observation_id = external_observation.observation_id
            located_observation = self._ledger.lookup_event_commit(
                f"event:outcome-observation:{observation_id}"
            )
            if located_observation is None:
                raise ValueError("external outcome observation event is unavailable")
            observation_event = located_observation[0]
        else:
            observation_id = "observation:life-aftermath:" + _digest(
                [occurrence.occurrence_id]
            )
            observation = OutcomeObservationProjection(
                observation_id=observation_id,
                occurrence_id=occurrence.occurrence_id,
                source_kind="committed_world_event",
                source_refs=(wake.event_id,),
                observed_payload_ref=wake.event_id,
                observed_payload_hash=wake.payload_hash,
                observed_at=logical_time,
                confidence_bp=10_000,
            )
            observation_payload = OutcomeObservationRecordedPayload(
                change_id="change:life-aftermath:observation:" + suffix,
                transition_id="transition:life-aftermath:observation:" + suffix,
                expected_entity_revision=occurrence.entity_revision,
                evidence_refs=(wake_evidence,),
                policy_refs=("policy:life-aftermath.1",),
                observation=observation,
            )
            observation_event = self._event(
                event_id=f"event:outcome-observation:{observation_id}",
                event_type="OutcomeObservationRecorded",
                payload=observation_payload.model_dump(mode="json"),
                logical_time=logical_time,
                trace_id=trace_id,
                causation_id=wake.event_id,
                correlation_id=correlation_id,
            )
            if self._ledger.lookup_event_commit(observation_event.event_id) is None:
                self._commit(
                    (observation_event,),
                    commit_id="commit:life-aftermath:observation:" + suffix,
                )

        projection = self._ledger.project()
        occurrence = next(
            item
            for item in projection.world_occurrences
            if item.occurrence_id == occurrence.occurrence_id
        )
        proposal_id = "proposal:life-aftermath:outcome:" + _digest(
            [occurrence.occurrence_id]
        )
        proposal_event_id = "event:life-aftermath:outcome-proposal:" + suffix
        existing_proposal = self._ledger.lookup_event_commit(proposal_event_id)
        decision_identity: dict[str, object] = {}
        resolution_evidence = (wake_evidence,)
        if existing_proposal is not None:
            persisted = OutcomeProposalRecordedPayload.model_validate_json(
                existing_proposal[0].payload_json
            )
            if (
                persisted.decision_authority == "character_model"
                and persisted.context_identity_version
                == "life-aftermath-context.2"
            ):
                raise ValueError(
                    "incomplete Context v2 character outcome cannot cross a "
                    "later World prefix"
                )
            proposal_id = persisted.outcome_proposal_id
            chosen = next(
                item
                for item in occurrence.candidate_outcomes
                if item.candidate_result_ref == persisted.candidate_result_ref
            )
        elif matrix_authority == "character_choice":
            query = query_from_projection(
                projection,
                actor_ref=self._owner_actor_ref,
                trigger_ref=observation_event.event_id,
            )
            capsule = self._capsule_compiler.compile_for_deliberation(query).capsule
            capsule_cursor = ProjectionCursor(
                world_revision=capsule.world_revision,
                deliberation_revision=capsule.deliberation_revision,
                ledger_sequence=capsule.ledger_sequence,
            )
            retry_ordinal, retry_due_at = self._outcome_retry_state(
                occurrence=occurrence,
                observation_id=observation_id,
            )
            if retry_due_at is not None and logical_time < retry_due_at:
                raise _LifeAftermathRetryWait
            decision_context = compile_life_decision_context(capsule)
            try:
                chosen, selected = await self._select_long_lived_outcome(
                    occurrence=occurrence,
                    projection=projection,
                    decision_context=decision_context,
                )
            except OutcomeSelectionFailure as exc:
                self._record_outcome_model_failure(
                    occurrence=occurrence,
                    observation_event=observation_event,
                    observation_id=observation_id,
                    capsule=capsule,
                    capsule_cursor=capsule_cursor,
                    failure=exc,
                    retry_ordinal=retry_ordinal + 1,
                    logical_time=logical_time,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                raise LifeAftermathModelFailure(
                    "long-lived outcome model failed with a durable audit",
                    failure_code=exc.failure_code,
                ) from exc
            self._record_outcome_model_result(
                occurrence=occurrence,
                observation_event=observation_event,
                observation_id=observation_id,
                capsule=capsule,
                capsule_cursor=capsule_cursor,
                selected=selected,
                resolution_evidence=resolution_evidence,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            existing_proposal = self._ledger.lookup_event_commit(proposal_event_id)
            if existing_proposal is None:
                raise LifeAftermathModelFailure(
                    "character outcome proposal disappeared after atomic audit"
                )
            persisted = OutcomeProposalRecordedPayload.model_validate_json(
                existing_proposal[0].payload_json
            )
            proposal_id = persisted.outcome_proposal_id
            chosen = next(
                item
                for item in occurrence.candidate_outcomes
                if item.candidate_result_ref == persisted.candidate_result_ref
            )
        elif matrix_authority == "world_contingency":
            relative_weights = {
                item.candidate_result_ref: item.relative_plausibility_weight
                for item in occurrence.candidate_outcomes
            }
            draw = self._random.draw(
                attempt_id="attempt:life-aftermath:"
                + _digest([occurrence.occurrence_id, "world-contingency"]),
                candidate_refs=occurrence.candidate_outcome_refs,
                catalog_version="open-life-outcome.1",
                logical_time=logical_time,
                # Outcome identity belongs to the occurrence, not to whichever
                # scheduler wake happened to resume settlement.  A crash after
                # RandomDrawRecorded must therefore rejoin this exact draw.
                seed_instant=occurrence.time_window.opens_at,
                actor=self._actor,
                trace_id=trace_id,
                correlation_id=correlation_id,
                candidate_weights=relative_weights,
                weight_policy_version="open-life-outcome-weight.1",
            )
            projection = self._ledger.project()
            chosen = next(
                item
                for item in occurrence.candidate_outcomes
                if item.candidate_result_ref == draw.selected_candidate_ref
            )
            draw_event_ref = f"event:random-draw:{draw.draw_id}"
            draw_event = self._ledger.lookup_event_commit(draw_event_ref)
            if draw_event is None:
                raise ValueError("recorded outcome draw event is unavailable")
            decision_identity = {
                "decision_authority": "recorded_world_draw",
                "recorded_world_draw": RecordedWorldDrawBinding(
                    draw_event_ref=draw_event_ref,
                    draw_event_payload_hash=draw_event[0].payload_hash,
                    draw_payload_json=draw_event[0].payload_json,
                ),
            }
            resolution_evidence = (
                EvidenceRef(
                    ref_id=draw_event_ref,
                    evidence_type="committed_world_event",
                    claim_purpose="life_transition",
                    source_world_revision=draw_event[1].world_revision,
                    immutable_hash=draw_event[0].payload_hash,
                ),
            )
        else:
            source_observation = next(
                (
                    item
                    for item in projection.outcome_observations
                    if item.observation_id == observation_id
                    and item.source_kind == "settled_external_result"
                ),
                None,
            )
            chosen = (
                next(
                    (
                        item
                        for item in occurrence.candidate_outcomes
                        if source_observation is not None
                        and item.result_payload_ref
                        == source_observation.observed_payload_ref
                        and item.result_payload_hash
                        == source_observation.observed_payload_hash
                    ),
                    None,
                )
            )
            if chosen is None:
                raise ValueError(
                    "external outcome observation matches no frozen candidate"
                )
            decision_identity = {
                "decision_authority": "external_observation",
            }
            located = self._ledger.lookup_event_commit(observation_event.event_id)
            if located is None:
                raise ValueError("external outcome observation authority disappeared")
            resolution_evidence = (
                EvidenceRef(
                    ref_id=located[0].event_id,
                    evidence_type="committed_world_event",
                    claim_purpose="life_transition",
                    source_world_revision=located[1].world_revision,
                    immutable_hash=located[0].payload_hash,
                ),
            )
        if existing_proposal is not None:
            proposal_payload = persisted
            proposal_event = existing_proposal[0]
            change_id = persisted.change_id
            change_hash = persisted.proposed_change_hash
        else:
            change_id = "change:life-aftermath:settle:" + suffix
            change_hash = outcome_mutation_hash(
                change_id=change_id,
                occurrence_id=occurrence.occurrence_id,
                evaluated_entity_revision=occurrence.entity_revision,
                evaluated_world_revision=projection.world_revision,
                candidate_result_ref=chosen.candidate_result_ref,
                result_id=chosen.result_id,
                result_payload_ref=chosen.result_payload_ref,
                result_payload_hash=chosen.result_payload_hash,
                observation_refs=(observation_id,),
            )
            proposal_payload = OutcomeProposalRecordedPayload(
                outcome_proposal_id=proposal_id,
                decision_proposal_id=proposal_id,
                change_id=change_id,
                occurrence_id=occurrence.occurrence_id,
                evaluated_entity_revision=occurrence.entity_revision,
                evaluated_world_revision=projection.world_revision,
                trigger_ref=occurrence.trigger_ref,
                candidate_result_ref=chosen.candidate_result_ref,
                proposed_result_id=chosen.result_id,
                proposed_result_payload_ref=chosen.result_payload_ref,
                proposed_result_payload_hash=chosen.result_payload_hash,
                proposed_change_hash=change_hash,
                observation_refs=(observation_id,),
                precondition_refs=occurrence.satisfied_precondition_refs,
                evidence_refs=resolution_evidence,
                confidence_bp=10_000,
                expires_at=logical_time + timedelta(minutes=5),
                **decision_identity,
            )
            proposal_event = self._event(
                event_id=proposal_event_id,
                event_type="OutcomeProposalRecorded",
                payload=proposal_payload.model_dump(mode="json"),
                logical_time=logical_time,
                trace_id=trace_id,
                causation_id=observation_event.event_id,
                correlation_id=correlation_id,
            )
            self._commit(
                (proposal_event,),
                commit_id="commit:life-aftermath:proposal:" + suffix,
            )

        acceptance_id = "acceptance:life-aftermath:" + suffix
        acceptance_payload = {
            "status": "accepted",
            "acceptance_id": acceptance_id,
            "proposal_id": proposal_id,
            "evaluated_world_revision": proposal_payload.evaluated_world_revision,
            "accepted_change_id": change_id,
            "accepted_change_hash": change_hash,
        }
        acceptance_event = self._event(
            event_id="event:life-aftermath:acceptance:" + suffix,
            event_type="AcceptanceRecorded",
            payload=acceptance_payload,
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=proposal_event.event_id,
            correlation_id=correlation_id,
        )
        trigger_id = appraisal_trigger_identity(occurrence.occurrence_id, chosen.result_id)
        settlement_payload = WorldOccurrenceSettledPayload(
            change_id=change_id,
            transition_id="transition:life-aftermath:settle:" + suffix,
            expected_entity_revision=occurrence.entity_revision,
            evidence_refs=(wake_evidence,),
            policy_refs=("policy:outcome-v1",),
            acceptance_id=acceptance_id,
            evaluated_world_revision=proposal_payload.evaluated_world_revision,
            accepted_change_hash=change_hash,
            occurrence_id=occurrence.occurrence_id,
            outcome_proposal_id=proposal_id,
            candidate_result_ref=chosen.candidate_result_ref,
            result_id=chosen.result_id,
            observation_refs=(observation_id,),
            result_payload_ref=chosen.result_payload_ref,
            result_payload_hash=chosen.result_payload_hash,
            settled_at=logical_time,
            appraisal_trigger_ref=trigger_id,
            adopt_proposed_life_direction=(
                proposal_payload.adopt_proposed_life_direction
            ),
        )
        settlement_event = self._event(
            event_id="event:life-aftermath:settlement:" + suffix,
            event_type="WorldOccurrenceSettled",
            payload=settlement_payload.model_dump(mode="json"),
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=acceptance_event.event_id,
            correlation_id=correlation_id,
        )
        trigger = TriggerProcess(
            trigger_id=trigger_id,
            trigger_ref=trigger_id,
            process_kind="npc_world_appraisal",
            source_evidence_ref=settlement_event.event_id,
            state="open",
        )
        trigger_event = self._event(
            event_id="event:life-aftermath:appraisal-trigger:" + suffix,
            event_type="TriggerProcessOpened",
            payload={"process": trigger.model_dump(mode="json")},
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=settlement_event.event_id,
            correlation_id=correlation_id,
        )
        if self._ledger.lookup_event_commit(settlement_event.event_id) is None:
            self._commit(
                (acceptance_event, settlement_event, trigger_event),
                commit_id="commit:life-aftermath:settlement:" + suffix,
            )

        result_record = StoredLifeContent(
            content_ref=chosen.result_payload_ref,
            content_kind="occurrence_result",
            content_payload_hash=chosen.result_payload_hash,
            text=self._candidate_text(chosen.content_ref, chosen.content_payload_hash),
        )
        self._content_store.put_if_absent(result_record)
        settled_projection = self._ledger.project()
        settlement_ref = next(
            item
            for item in settled_projection.committed_world_event_refs
            if item.event_id == settlement_event.event_id
        )
        descriptor = LifeContentRecordedPayload(
            content_id="life-content:occurrence:" + suffix,
            content_kind="occurrence_result",
            content_ref=result_record.content_ref,
            content_payload_hash=result_record.content_payload_hash,
            privacy_class=chosen.privacy_class,
            source_kind="occurrence_settlement",
            source_event_ref=settlement_ref.event_id,
            source_world_revision=settlement_ref.world_revision,
            source_payload_hash=settlement_ref.payload_hash,
            source_entity_id=occurrence.occurrence_id,
            source_entity_revision=4,
        )
        descriptor_event = self._event(
            event_id="event:life-content:occurrence:" + suffix,
            event_type="LifeContentRecorded",
            payload=descriptor.model_dump(mode="json"),
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=settlement_event.event_id,
            correlation_id=correlation_id,
        )
        if self._ledger.lookup_event_commit(descriptor_event.event_id) is None:
            self._commit((descriptor_event,), commit_id="commit:life-content:occurrence:" + suffix)
        settled = next(
            item
            for item in self._ledger.project().world_occurrences
            if item.occurrence_id == occurrence.occurrence_id
        )
        return await self._commit_experience(
            occurrence=settled,
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    async def _commit_experience(
        self, *, occurrence, logical_time: datetime, trace_id: str, correlation_id: str
    ) -> str:
        suffix = occurrence.occurrence_id.removeprefix("occurrence:life-aftermath:")
        experience_id = "experience:life-aftermath:" + suffix
        if self._has_experience(self._ledger.project(), occurrence.occurrence_id):
            await self._materialize_experience_memory(
                experience_id=experience_id,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            return experience_id
        settlement = self._ledger.lookup_event_commit(occurrence.settlement_event_ref)
        if settlement is None:
            raise ValueError("settled aftermath has no durable settlement event")
        settlement_event, settlement_commit = settlement
        result_content = self._content_store.read_exact(content_ref=occurrence.result_payload_ref)
        if (
            result_content is None
            or result_content.content_payload_hash != occurrence.result_payload_hash
        ):
            descriptor = next(
                item
                for item in occurrence.candidate_outcomes
                if item.result_id == occurrence.result_id
            )
            result_content = StoredLifeContent(
                content_ref=occurrence.result_payload_ref,
                content_kind="occurrence_result",
                content_payload_hash=occurrence.result_payload_hash,
                text=self._candidate_text(descriptor.content_ref, descriptor.content_payload_hash),
            )
            self._content_store.put_if_absent(result_content)
        projection = self._ledger.project()
        if not any(
            item.source_kind == "occurrence_settlement"
            and item.source_entity_id == occurrence.occurrence_id
            for item in projection.life_content_descriptors
        ):
            descriptor = LifeContentRecordedPayload(
                content_id="life-content:occurrence:" + suffix,
                content_kind="occurrence_result",
                content_ref=result_content.content_ref,
                content_payload_hash=result_content.content_payload_hash,
                privacy_class=occurrence.visibility,
                source_kind="occurrence_settlement",
                source_event_ref=settlement_event.event_id,
                source_world_revision=settlement_commit.world_revision,
                source_payload_hash=settlement_event.payload_hash,
                source_entity_id=occurrence.occurrence_id,
                source_entity_revision=occurrence.entity_revision,
            )
            descriptor_event = self._event(
                event_id="event:life-content:occurrence:" + suffix,
                event_type="LifeContentRecorded",
                payload=descriptor.model_dump(mode="json"),
                logical_time=logical_time,
                trace_id=trace_id,
                causation_id=settlement_event.event_id,
                correlation_id=correlation_id,
            )
            self._commit((descriptor_event,), commit_id="commit:life-content:occurrence:" + suffix)
        summary_ref = "content:experience-summary:" + suffix
        summary = StoredLifeContent(
            content_ref=summary_ref,
            content_kind="experience_summary",
            content_payload_hash=life_content_payload_hash(result_content.text),
            text=result_content.text,
        )
        self._content_store.put_if_absent(summary)
        projection = self._ledger.project()
        policy_refs = ("policy:experience-v1",)
        change_id = "change:life-aftermath:experience:" + suffix
        transition_id = "transition:life-aftermath:experience:" + suffix
        experience_event_id = "event:life-aftermath:experience:" + suffix
        binding = ExperienceOccurrenceSettlementBinding(
            authority_event_ref=settlement_event.event_id,
            authority_world_revision=settlement_commit.world_revision,
            authority_payload_hash=settlement_event.payload_hash,
            occurrence_id=occurrence.occurrence_id,
            occurrence_entity_revision=occurrence.entity_revision,
            result_id=occurrence.result_id,
            result_payload_ref=occurrence.result_payload_ref,
            result_payload_hash=occurrence.result_payload_hash,
        )
        experience_privacy = _experience_privacy(occurrence.visibility)
        values = ExperienceValues(
            summary_ref=summary_ref,
            summary_payload_hash=summary.content_payload_hash,
            occurred_from=occurrence.activated_at,
            occurred_to=occurrence.settled_at,
            participant_refs=occurrence.participant_refs,
            source_bindings=(binding,),
            privacy_class=experience_privacy,
        )
        origin = ExperienceOrigin(
            change_id=change_id,
            transition_id=transition_id,
            policy_refs=policy_refs,
            accepted_event_ref=experience_event_id,
        )
        experience = ExperienceProjection(
            experience_id=experience_id,
            semantic_fingerprint=experience_semantic_fingerprint(
                values=values, policy_refs=policy_refs
            ),
            values=values,
            origin=origin,
        )
        proposal_id = "proposal:life-aftermath:experience:" + suffix
        evidence = EvidenceRef(
            ref_id=settlement_event.event_id,
            evidence_type="settled_world_event",
            claim_purpose="past_experience",
            source_world_revision=settlement_commit.world_revision,
            immutable_hash=settlement_event.payload_hash,
        )
        base = {
            "change_id": change_id,
            "transition_id": transition_id,
            "expected_entity_revision": 0,
            "evidence_refs": (evidence,),
            "policy_refs": policy_refs,
            "acceptance_id": "acceptance:life-aftermath:experience:" + suffix,
            "proposal_id": proposal_id,
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "experience": experience,
        }
        base["accepted_change_hash"] = experience_mutation_hash(base)
        mutation = ExperienceCommittedPayload.model_validate(base)
        proposal = ExperienceProposalProjection(
            proposal_id=proposal_id,
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:experience.1",
            change_id=change_id,
            transition_id=transition_id,
            evaluated_world_revision=projection.world_revision,
            proposed_change_hash=mutation.accepted_change_hash,
            evidence_refs=(evidence,),
            policy_refs=policy_refs,
            proposed_mutation=ExperienceProposedMutation(
                payload_json=json.dumps(
                    mutation.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        )
        proposal_event = self._event(
            event_id="event:life-aftermath:experience-proposal:" + suffix,
            event_type="ProposalRecorded",
            payload=proposal.model_dump(mode="json"),
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=settlement_event.event_id,
            correlation_id=correlation_id,
        )
        if self._ledger.lookup_event_commit(proposal_event.event_id) is None:
            self._commit(
                (proposal_event,), commit_id="commit:life-aftermath:experience-proposal:" + suffix
            )
        acceptance_payload = {
            "status": "accepted",
            "acceptance_id": mutation.acceptance_id,
            "proposal_id": proposal_id,
            "evaluated_world_revision": mutation.evaluated_world_revision,
            "accepted_change_id": change_id,
            "accepted_change_hash": mutation.accepted_change_hash,
        }
        acceptance_event = self._event(
            event_id="event:life-aftermath:experience-acceptance:" + suffix,
            event_type="AcceptanceRecorded",
            payload=acceptance_payload,
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=proposal_event.event_id,
            correlation_id=correlation_id,
        )
        experience_event = self._event(
            event_id=experience_event_id,
            event_type="ExperienceCommitted",
            payload=mutation.model_dump(mode="json"),
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=acceptance_event.event_id,
            correlation_id=correlation_id,
        )
        projected = self._ledger.project()
        content_payload = LifeContentRecordedPayload(
            content_id="life-content:experience:" + suffix,
            content_kind="experience_summary",
            content_ref=summary_ref,
            content_payload_hash=summary.content_payload_hash,
            privacy_class=experience_privacy,
            source_kind="experience",
            source_event_ref=experience_event.event_id,
            source_world_revision=projected.world_revision + 2,
            source_payload_hash=experience_event.payload_hash,
            source_entity_id=experience_id,
            source_entity_revision=1,
        )
        content_event = self._event(
            event_id="event:life-content:experience:" + suffix,
            event_type="LifeContentRecorded",
            payload=content_payload.model_dump(mode="json"),
            logical_time=logical_time,
            trace_id=trace_id,
            causation_id=experience_event.event_id,
            correlation_id=correlation_id,
        )
        self._commit(
            (acceptance_event, experience_event, content_event),
            commit_id="commit:life-aftermath:experience:" + suffix,
        )
        await self._materialize_experience_memory(
            experience_id=experience_id,
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return experience_id

    def _outcome_decision_key(
        self,
        *,
        occurrence,
        observation_id: str,
    ) -> tuple[str, str]:
        matrix_hash = _candidate_matrix_hash(occurrence)
        return (
            _digest(
                {
                    "candidate_matrix_hash": matrix_hash,
                    "observation_id": observation_id,
                    "occurrence_entity_revision": occurrence.entity_revision,
                    "occurrence_id": occurrence.occurrence_id,
                    "world_id": self._ledger.world_id,
                }
            ),
            matrix_hash,
        )

    def _record_outcome_model_result(
        self,
        *,
        occurrence,
        observation_event: WorldEvent,
        observation_id: str,
        capsule,
        capsule_cursor: ProjectionCursor,
        selected: OutcomeSelectionDraft,
        resolution_evidence: tuple[EvidenceRef, ...],
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> None:
        decision_key, matrix_hash = self._outcome_decision_key(
            occurrence=occurrence,
            observation_id=observation_id,
        )
        if len(selected.attempt_request_hashes) != len(
            selected.attempt_raw_outputs
        ):
            raise LifeAftermathModelFailure(
                "outcome selection did not retain exact per-call request hashes"
            )
        context_material = {
            "candidate_matrix_hash": matrix_hash,
            "capsule_id": capsule.capsule_id,
            "context_cursor": capsule_cursor.model_dump(mode="json"),
            "context_model_content_hash": hashlib.sha256(
                capsule.model_content_json.encode("utf-8")
            ).hexdigest(),
            "context_snapshot_hash": capsule.snapshot_hash,
            "observation_id": observation_id,
            "occurrence_entity_revision": occurrence.entity_revision,
            "occurrence_id": occurrence.occurrence_id,
        }
        context_text = canonical_json(context_material)
        context_ref = (
            "content:life-aftermath:outcome-model-context:"
            + decision_key
            + ":"
            + capsule.capsule_id
        )
        self._content_store.put_if_absent(
            StoredLifeContent(
                content_ref=context_ref,
                content_kind="outcome_candidate",
                content_payload_hash=life_content_payload_hash(context_text),
                text=context_text,
            )
        )

        attempt_id = "attempt:life-aftermath:outcome:" + decision_key
        route = RecordedModelRoute(
            tier="flash",
            reason_code="life_aftermath.character_outcome_selection",
            router_version="life-aftermath-outcome-router.1",
        )
        audits: list[RecordedModelResultAudit] = []
        for index, raw in enumerate(selected.attempt_raw_outputs):
            response_hash = life_content_payload_hash(raw)
            content_ref = (
                "content:life-aftermath:outcome-model:"
                + decision_key
                + ":"
                + response_hash
            )
            self._content_store.put_if_absent(
                StoredLifeContent(
                    content_ref=content_ref,
                    content_kind="outcome_candidate",
                    content_payload_hash=response_hash,
                    text=raw,
                )
            )
            model_call_id = (
                "model-call:life-aftermath:outcome:"
                + decision_key
                + ":correction:"
                + str(index)
            )
            model_result_ref = "model-result:" + sha256(
                canonical_json(
                    {
                        "model_call_id": model_call_id,
                        "response_hash": response_hash,
                    }
                )
            )
            repaired = len(selected.attempt_raw_outputs) == 2
            audits.append(
                RecordedModelResultAudit(
                    model_call_id=model_call_id,
                    model_result_ref=model_result_ref,
                    attempt_id=attempt_id,
                    route=route,
                    model_id=selected.model,
                    model_version=selected.model,
                    request_hash=selected.attempt_request_hashes[index],
                    response_hash=response_hash,
                    status=(
                        "main_invalid"
                        if repaired and index == 0
                        else "main_invalid_recovered"
                        if repaired
                        else "proposal_validated"
                    ),
                    failure_code=(
                        "main_invalid_output" if repaired else None
                    ),
                )
            )

        final = audits[-1]
        character_direction = (
            BiographicalCoordinateReplacement.create(
                coordinate_ref=selected.character_life_direction.coordinate_ref,
                summary=selected.character_life_direction.summary,
                context_tags=selected.character_life_direction.context_tags,
                replaces_context_tag_prefixes=(
                    selected.character_life_direction.replaces_context_tag_prefixes
                ),
                privacy_class=selected.character_life_direction.privacy_class,
            )
            if selected.character_life_direction is not None
            else None
        )
        audit_response_text = outcome_selection_audit_text(
            candidate_result_ref=selected.candidate_result_ref,
            adopt_proposed_life_direction=False,
            character_life_direction=selected.character_life_direction,
            candidate_matrix_hash=matrix_hash,
            response_hash=str(final.response_hash),
        )
        audit_proposal = MinimalProposal(
            proposal_id="proposal:life-aftermath:outcome-model:" + decision_key,
            trigger_ref=observation_event.event_id,
            evaluated_world_revision=capsule_cursor.world_revision,
            evidence_refs=(),
            proposed_changes=(),
            action_intents=(),
            confidence=10_000,
            brief_rationale="Persist validated character-owned outcome selection.",
            source_model_result=final.model_result_ref,
            response_text=audit_response_text,
            stance="answer_without_world_claims",
        )
        proposal_json = canonical_json(
            audit_proposal.model_dump(mode="json")
        )
        proposal_hash = audit_proposal.proposal_hash
        deliberation_result_id = "deliberation:" + sha256(
            canonical_json(
                {
                    "capsule_id": capsule.capsule_id,
                    "proposal_hash": proposal_hash,
                    "attempt_audits": [
                        json.loads(model_audit_json(item)) for item in audits
                    ],
                }
            )
        )
        events: list[WorldEvent] = []
        for index, audit in enumerate(audits):
            audit_json = model_audit_json(audit)
            payload = ModelResultRecordedPayload(
                model_result_ref=audit.model_result_ref,
                deliberation_result_id=deliberation_result_id,
                proposal_hash=proposal_hash,
                model_call_id=audit.model_call_id,
                attempt_id=attempt_id,
                capsule_id=capsule.capsule_id,
                trigger_ref=observation_event.event_id,
                evaluated_world_revision=capsule_cursor.world_revision,
                attempt_index=index,
                attempt_count=len(audits),
                audit_json=audit_json,
                audit_hash=sha256(audit_json),
            ).model_dump(mode="json")
            events.append(
                self._event(
                    event_id=(
                        "event:life-aftermath:outcome-model-result:"
                        + decision_key
                        + ":"
                        + str(index)
                    ),
                    event_type="ModelResultRecorded",
                    payload=payload,
                    logical_time=observation_event.logical_time,
                    trace_id=trace_id,
                    causation_id=(
                        observation_event.event_id
                        if index == 0
                        else events[-1].event_id
                    ),
                    correlation_id=correlation_id,
                )
            )
        proposal_payload = ProposalRecordedV2Payload(
            proposal_id=audit_proposal.proposal_id,
            proposal_kind=audit_proposal.proposal_kind,
            model_result_ref=final.model_result_ref,
            deliberation_result_id=deliberation_result_id,
            model_call_id=final.model_call_id,
            attempt_id=attempt_id,
            capsule_id=capsule.capsule_id,
            trigger_ref=observation_event.event_id,
            evaluated_world_revision=capsule_cursor.world_revision,
            proposal_json=proposal_json,
            proposal_hash=proposal_hash,
        ).model_dump(mode="json")
        audit_proposal_event = self._event(
            event_id=(
                "event:life-aftermath:outcome-model-proposal:"
                + decision_key
            ),
            event_type="ProposalRecorded",
            payload=proposal_payload,
            logical_time=observation_event.logical_time,
            trace_id=trace_id,
            causation_id=events[-1].event_id,
            correlation_id=correlation_id,
        )
        events.append(audit_proposal_event)
        chosen = next(
            item
            for item in occurrence.candidate_outcomes
            if item.candidate_result_ref == selected.candidate_result_ref
        )
        suffix = occurrence.occurrence_id.removeprefix(
            "occurrence:life-aftermath:"
        )
        change_id = "change:life-aftermath:settle:" + suffix
        change_hash = outcome_mutation_hash(
            change_id=change_id,
            occurrence_id=occurrence.occurrence_id,
            evaluated_entity_revision=occurrence.entity_revision,
            evaluated_world_revision=capsule_cursor.world_revision,
            candidate_result_ref=chosen.candidate_result_ref,
            result_id=chosen.result_id,
            result_payload_ref=chosen.result_payload_ref,
            result_payload_hash=chosen.result_payload_hash,
            observation_refs=(observation_id,),
            character_life_direction=character_direction,
        )
        outcome_proposal = OutcomeProposalRecordedPayload(
            outcome_proposal_id=(
                "proposal:life-aftermath:outcome:"
                + _digest([occurrence.occurrence_id])
            ),
            decision_proposal_id=(
                "proposal:life-aftermath:outcome:"
                + _digest([occurrence.occurrence_id])
            ),
            change_id=change_id,
            occurrence_id=occurrence.occurrence_id,
            evaluated_entity_revision=occurrence.entity_revision,
            evaluated_world_revision=capsule_cursor.world_revision,
            trigger_ref=occurrence.trigger_ref,
            candidate_result_ref=chosen.candidate_result_ref,
            proposed_result_id=chosen.result_id,
            proposed_result_payload_ref=chosen.result_payload_ref,
            proposed_result_payload_hash=chosen.result_payload_hash,
            proposed_change_hash=change_hash,
            observation_refs=(observation_id,),
            precondition_refs=occurrence.satisfied_precondition_refs,
            evidence_refs=resolution_evidence,
            confidence_bp=10_000,
            expires_at=logical_time + timedelta(minutes=5),
            decision_authority="character_model",
            decision_model=selected.model,
            decision_raw_output_hash=life_content_payload_hash(
                selected.raw_output
            ),
            decision_model_result_ref=final.model_result_ref,
            decision_model_result_event_ref=events[-2].event_id,
            decision_audit_proposal_event_ref=audit_proposal_event.event_id,
            decision_audit_proposal_event_payload_hash=(
                audit_proposal_event.payload_hash
            ),
            decision_candidate_matrix_hash=matrix_hash,
            character_life_direction=character_direction,
            context_identity_version="life-aftermath-context.3",
            context_capsule_id=capsule.capsule_id,
            context_model_content_hash=context_material[
                "context_model_content_hash"
            ],
            context_snapshot_hash=capsule.snapshot_hash,
            context_cursor=capsule_cursor,
        )
        outcome_proposal_event = self._event(
            event_id="event:life-aftermath:outcome-proposal:" + suffix,
            event_type="OutcomeProposalRecorded",
            payload=outcome_proposal.model_dump(mode="json"),
            logical_time=observation_event.logical_time,
            trace_id=trace_id,
            causation_id=audit_proposal_event.event_id,
            correlation_id=correlation_id,
        )
        events.append(outcome_proposal_event)
        acceptance_id = "acceptance:life-aftermath:" + suffix
        acceptance_event = self._event(
            event_id="event:life-aftermath:acceptance:" + suffix,
            event_type="AcceptanceRecorded",
            payload={
                "status": "accepted",
                "acceptance_id": acceptance_id,
                "proposal_id": outcome_proposal.outcome_proposal_id,
                "evaluated_world_revision": (
                    outcome_proposal.evaluated_world_revision
                ),
                "accepted_change_id": change_id,
                "accepted_change_hash": change_hash,
            },
            logical_time=observation_event.logical_time,
            trace_id=trace_id,
            causation_id=outcome_proposal_event.event_id,
            correlation_id=correlation_id,
        )
        events.append(acceptance_event)
        appraisal_trigger_ref = appraisal_trigger_identity(
            occurrence.occurrence_id,
            chosen.result_id,
        )
        settlement = WorldOccurrenceSettledPayload(
            change_id=change_id,
            transition_id="transition:life-aftermath:settle:" + suffix,
            expected_entity_revision=occurrence.entity_revision,
            evidence_refs=resolution_evidence,
            policy_refs=("policy:outcome-v1",),
            acceptance_id=acceptance_id,
            evaluated_world_revision=outcome_proposal.evaluated_world_revision,
            accepted_change_hash=change_hash,
            occurrence_id=occurrence.occurrence_id,
            outcome_proposal_id=outcome_proposal.outcome_proposal_id,
            candidate_result_ref=chosen.candidate_result_ref,
            result_id=chosen.result_id,
            observation_refs=(observation_id,),
            result_payload_ref=chosen.result_payload_ref,
            result_payload_hash=chosen.result_payload_hash,
            settled_at=logical_time,
            appraisal_trigger_ref=appraisal_trigger_ref,
            character_life_direction=character_direction,
        )
        settlement_event = self._event(
            event_id="event:life-aftermath:settlement:" + suffix,
            event_type="WorldOccurrenceSettled",
            payload=settlement.model_dump(mode="json"),
            logical_time=observation_event.logical_time,
            trace_id=trace_id,
            causation_id=acceptance_event.event_id,
            correlation_id=correlation_id,
        )
        events.append(settlement_event)
        appraisal_trigger = TriggerProcess(
            trigger_id=appraisal_trigger_ref,
            trigger_ref=appraisal_trigger_ref,
            process_kind="npc_world_appraisal",
            source_evidence_ref=settlement_event.event_id,
            state="open",
        )
        events.append(
            self._event(
                event_id="event:life-aftermath:appraisal-trigger:" + suffix,
                event_type="TriggerProcessOpened",
                payload={"process": appraisal_trigger.model_dump(mode="json")},
                logical_time=observation_event.logical_time,
                trace_id=trace_id,
                causation_id=settlement_event.event_id,
                correlation_id=correlation_id,
            )
        )
        try:
            self._ledger.commit_at_cursor(
                tuple(events),
                expected_cursor=capsule_cursor,
                commit_id=(
                    "commit:life-aftermath:character-outcome:" + decision_key
                ),
            )
        except ConcurrencyConflict:
            if (
                self._ledger.lookup_event_commit(
                    "event:life-aftermath:outcome-model-result:"
                    + decision_key
                    + ":0"
                )
                is None
            ):
                raise

    def _outcome_retry_state(
        self,
        *,
        occurrence,
        observation_id: str,
    ) -> tuple[int, datetime | None]:
        """Derive occurrence-scoped retry state solely from terminal audit events."""

        decision_key, _matrix_hash = self._outcome_decision_key(
            occurrence=occurrence,
            observation_id=observation_id,
        )
        ordinal = 0
        failed_at: datetime | None = None
        while True:
            first = self._ledger.lookup_event_commit(
                "event:life-aftermath:outcome-model-failure:"
                + decision_key
                + ":retry:"
                + str(ordinal + 1)
                + ":0"
            )
            if first is None:
                break
            first_payload = ModelResultRecordedPayload.model_validate_json(
                first[0].payload_json
            )
            terminal = self._ledger.lookup_event_commit(
                "event:life-aftermath:outcome-model-failure:"
                + decision_key
                + ":retry:"
                + str(ordinal + 1)
                + ":"
                + str(first_payload.attempt_count - 1)
            )
            if terminal is None:
                raise ValueError("outcome failure audit retry is incomplete")
            terminal_payload = ModelResultRecordedPayload.model_validate_json(
                terminal[0].payload_json
            )
            terminal_audit = RecordedModelResultAudit.model_validate_json(
                terminal_payload.audit_json
            )
            if terminal_audit.status not in {
                "main_timeout",
                "main_exception",
                "recovery_failed",
            }:
                raise ValueError("outcome retry audit is not a terminal failure")
            ordinal += 1
            failed_at = terminal[0].logical_time
        if failed_at is None:
            return 0, None
        delay_seconds = (600, 1_800, 7_200)[min(ordinal - 1, 2)]
        return ordinal, failed_at + timedelta(seconds=delay_seconds)

    def _record_outcome_model_failure(
        self,
        *,
        occurrence,
        observation_event: WorldEvent,
        observation_id: str,
        capsule,
        capsule_cursor: ProjectionCursor,
        failure: OutcomeSelectionFailure,
        retry_ordinal: int,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> None:
        decision_key, _matrix_hash = self._outcome_decision_key(
            occurrence=occurrence,
            observation_id=observation_id,
        )
        attempt_id = (
            "attempt:life-aftermath:outcome:"
            + decision_key
            + ":retry:"
            + str(retry_ordinal)
        )
        route = RecordedModelRoute(
            tier="flash",
            reason_code="life_aftermath.character_outcome_selection",
            router_version="life-aftermath-outcome-router.1",
        )
        audits: list[RecordedModelResultAudit] = []
        for index, failed in enumerate(failure.attempts):
            response_hash = (
                life_content_payload_hash(failed.raw_output)
                if failed.raw_output is not None
                else None
            )
            if failed.raw_output is not None and response_hash is not None:
                self._content_store.put_if_absent(
                    StoredLifeContent(
                        content_ref=(
                            "content:life-aftermath:outcome-model-failure:"
                            + decision_key
                            + ":retry:"
                            + str(retry_ordinal)
                            + ":"
                            + response_hash
                        ),
                        content_kind="outcome_candidate",
                        content_payload_hash=response_hash,
                        text=failed.raw_output,
                    )
                )
            model_call_id = (
                "model-call:life-aftermath:outcome:"
                + decision_key
                + ":retry:"
                + str(retry_ordinal)
                + ":"
                + str(index)
            )
            model_result_ref = "model-result:" + sha256(
                canonical_json(
                    {
                        "model_call_id": model_call_id,
                        "response_hash": response_hash,
                    }
                )
            )
            has_output = response_hash is not None
            audits.append(
                RecordedModelResultAudit(
                    model_call_id=model_call_id,
                    model_result_ref=model_result_ref,
                    attempt_id=attempt_id,
                    route=route,
                    model_id=failure.model_id if has_output else None,
                    model_version=failure.model_id if has_output else None,
                    attempted_model_id=(
                        None if has_output else failure.model_id
                    ),
                    attempted_model_version=(
                        None if has_output else failure.model_id
                    ),
                    request_hash=failed.request_hash,
                    response_hash=response_hash,
                    status=failed.status,
                    failure_code=failed.failure_code,
                    slot=failed.slot,
                    outcome=failed.outcome,
                )
            )
        deliberation_result_id = "deliberation:" + sha256(
            canonical_json(
                {
                    "capsule_id": capsule.capsule_id,
                    "proposal_hash": None,
                    "attempt_audits": [
                        json.loads(model_audit_json(item)) for item in audits
                    ],
                }
            )
        )
        events: list[WorldEvent] = []
        for index, audit in enumerate(audits):
            audit_json = model_audit_json(audit)
            payload = ModelResultRecordedPayload(
                audit_contract="model-result-audit.3",
                model_result_ref=audit.model_result_ref,
                deliberation_result_id=deliberation_result_id,
                proposal_hash=None,
                model_call_id=audit.model_call_id,
                attempt_id=attempt_id,
                capsule_id=capsule.capsule_id,
                trigger_ref=observation_event.event_id,
                evaluated_world_revision=capsule_cursor.world_revision,
                attempt_index=index,
                attempt_count=len(audits),
                audit_json=audit_json,
                audit_hash=sha256(audit_json),
            )
            events.append(
                self._event(
                    event_id=(
                        "event:life-aftermath:outcome-model-failure:"
                        + decision_key
                        + ":retry:"
                        + str(retry_ordinal)
                        + ":"
                        + str(index)
                    ),
                    event_type="ModelResultRecorded",
                    payload=payload.model_dump(mode="json"),
                    logical_time=logical_time,
                    trace_id=trace_id,
                    causation_id=(
                        observation_event.event_id
                        if index == 0
                        else events[-1].event_id
                    ),
                    correlation_id=correlation_id,
                )
            )
        try:
            self._ledger.commit_at_cursor(
                tuple(events),
                expected_cursor=capsule_cursor,
                commit_id=(
                    "commit:life-aftermath:outcome-model-failure:"
                    + decision_key
                    + ":retry:"
                    + str(retry_ordinal)
                ),
            )
        except ConcurrencyConflict:
            if (
                self._ledger.lookup_event_commit(events[0].event_id)
                is None
            ):
                raise

    async def _select_long_lived_outcome(
        self,
        *,
        occurrence,
        projection,
        decision_context: dict[str, object],
    ):
        """Require character-model authority whenever an option changes biography."""

        if self._outcome_selection is None:
            raise LifeAftermathModelFailure(
                "long-lived outcome requires the installed character model"
            )
        options = self._outcome_selection_options(occurrence)
        try:
            selected = await self._outcome_selection.deliberate(
                options=options,
                mood_summary=mood_summary_prose(projection.affect_episodes) or None,
                decision_context=decision_context,
                current_coordinates=tuple(
                    getattr(projection, "biographical_coordinates", ())
                ),
            )
        except OutcomeSelectionFailure:
            raise
        except (
            TimeoutError,
            ConnectionError,
            OSError,
            httpx.HTTPError,
            ValueError,
        ) as exc:
            raise LifeAftermathModelFailure(
                "long-lived outcome model unavailable"
            ) from exc
        return (
            next(
                item
                for item in occurrence.candidate_outcomes
                if item.candidate_result_ref == selected.candidate_result_ref
            ),
            selected,
        )

    def _outcome_selection_options(
        self,
        occurrence,
    ) -> tuple[OutcomeSelectionOption, ...]:
        """Expose objective alternatives only; the character authors her own direction."""

        options: list[OutcomeSelectionOption] = []
        for item in occurrence.candidate_outcomes:
            options.append(
                OutcomeSelectionOption(
                    candidate_result_ref=item.candidate_result_ref,
                    summary=self._candidate_text(
                        item.content_ref,
                        item.content_payload_hash,
                    ),
                )
            )
        return tuple(options)

    async def _materialize_experience_memory(
        self,
        *,
        experience_id: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> Literal["retained", "no_change", "retry_wait"] | None:
        """Single-flight one Experience decision inside this worker process."""

        lock = self._memory_decision_locks.setdefault(
            experience_id,
            asyncio.Lock(),
        )
        async with lock:
            return await self._materialize_experience_memory_owned(
                experience_id=experience_id,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )

    async def _materialize_experience_memory_owned(
        self,
        *,
        experience_id: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> Literal["retained", "no_change", "retry_wait"] | None:
        """Retain settled lived history through the existing memory authority.

        The semantic text remains the immutable sidecar and the candidate is
        still source-bound, private, replayable, and subject to normal memory
        withdrawal/decay.  An optional model may refine retention and salience
        inside the installed matrix without changing this authority seam.
        """

        lifecycle = self._experience_memory_lifecycle
        if lifecycle is None:
            return None
        projection = self._ledger.project()
        experience = next(
            (
                item
                for item in projection.experiences
                if isinstance(item, ExperienceProjection) and item.experience_id == experience_id
            ),
            None,
        )
        if experience is None:
            return None
        if any(
            item.values.status != "pending"
            and any(
                binding.source_id == experience_id
                for binding in item.values.source_bindings
            )
            for item in projection.memory_candidates
        ):
            return None
        transition = next(
            item
            for item in projection.experience_transitions
            if item.experience_id == experience_id
            and item.entity_revision == experience.entity_revision
            and item.accepted_event_ref == experience.origin.accepted_event_ref
        )
        located = self._ledger.lookup_event_commit(experience.origin.accepted_event_ref)
        if located is None:
            raise ValueError("committed Experience has no durable event")
        event, _ = located
        committed_experience = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == experience.origin.accepted_event_ref
            ),
            None,
        )
        if committed_experience is None:
            raise ValueError("committed Experience has no event projection")
        pending = next(
            (
                item
                for item in projection.memory_candidates
                if item.values.status == "pending"
                and any(
                    binding.source_kind == "experience"
                    and binding.source_id == experience_id
                    for binding in item.values.source_bindings
                )
            ),
            None,
        )
        decision = self._experience_memory_decision(experience)
        if decision is not None and decision.decision_kind == "no_change":
            return "no_change"
        if decision is not None:
            draft = FactMemoryRetentionDraft.model_validate_json(
                decision.decision_json
            )
        elif pending is not None:
            # Legacy pending candidates predate the durable decision audit and
            # could only have been opened from retain=true.
            draft = FactMemoryRetentionDraft(
                cue_kind=pending.values.cue_kind,
                retention_rationales=pending.values.retention_rationales,
                salience=pending.values.salience,
            )
        else:
            if self._memory_adapter is None:
                return None
            retry = contextual_life_retry_for(
                projection,
                lane="experience_memory",
                source_event_ref=experience.origin.accepted_event_ref,
            )
            if (
                retry is not None
                and projection.logical_time is not None
                and projection.logical_time < retry.next_retry_at
            ):
                return "retry_wait"
            summary = self._content_store.read_exact(
                content_ref=experience.values.summary_ref
            )
            if (
                summary is None
                or summary.content_payload_hash
                != experience.values.summary_payload_hash
            ):
                raise ValueError(
                    "experience summary sidecar is unavailable for memory classification"
                )
            try:
                classified = await self._memory_adapter.classify(
                    predicate_code="world.experience",
                    source_text=summary.text,
                )
            except FactMemoryDraftTechnicalFailure as exc:
                try:
                    record_contextual_life_technical_failure(
                        ledger=self._ledger,
                        projection=projection,
                        lane="experience_memory",
                        source_event_ref=experience.origin.accepted_event_ref,
                        source_payload_hash=committed_experience.payload_hash,
                        context_cursor=_cursor(projection),
                        failure_code=exc.failure_code,
                        actor=self._actor,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
                except (ConcurrencyConflict, IdempotencyConflict):
                    # A competing worker may have recorded the exact
                    # source-scoped failure first. Join that durable retry;
                    # never turn one failed attempt into two ordinals.
                    joined = contextual_life_retry_for(
                        self._ledger.project(),
                        lane="experience_memory",
                        source_event_ref=experience.origin.accepted_event_ref,
                    )
                    if joined is None:
                        raise
                raise
            decision = self._record_experience_memory_decision(
                experience=experience,
                committed_experience=committed_experience,
                evaluated_projection=projection,
                source_text=summary.text,
                decision=classified,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            if decision.decision_kind == "no_change":
                return "no_change"
            draft = FactMemoryRetentionDraft.model_validate_json(
                decision.decision_json
            )
        accepted = lifecycle.accept(
            experience=experience,
            transition=transition,
            experience_event=event,
            # lookup_event_commit returns the cursor after the whole batch.
            # ExperienceCommitted may be followed by a LifeContentRecorded
            # event in that batch, so only its own committed-event projection
            # carries the exact source revision required by memory authority.
            experience_world_revision=committed_experience.world_revision,
            draft=draft,
            logical_time=logical_time,
            created_at=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        if accepted is None:
            _LOG.warning(
                "experience memory lifecycle found existing authority for %s",
                experience_id,
            )
            return None
        elif not any(
            item.candidate_id == accepted.candidate_id
            for item in self._ledger.project().memory_candidates
        ):
            raise RuntimeError("experience memory lifecycle returned without durable projection")
        return "retained"

    def _has_no_change_memory_decision(
        self, experience: ExperienceProjection
    ) -> bool:
        decision = self._experience_memory_decision(experience)
        return decision is not None and decision.decision_kind == "no_change"

    def _experience_memory_decision(
        self, experience: ExperienceProjection
    ) -> ExperienceMemoryDecisionRecordedPayload | None:
        located = self._ledger.lookup_event_commit(
            experience_memory_decision_event_id(
                experience_authority_event_ref=experience.origin.accepted_event_ref
            )
        )
        if located is None:
            return None
        event, _ = located
        if event.event_type != "ExperienceMemoryDecisionRecorded":
            raise ValueError(
                "Experience-memory decision identity resolved to another event"
            )
        payload = ExperienceMemoryDecisionRecordedPayload.model_validate_json(
            event.payload_json
        )
        if (
            payload.experience_id != experience.experience_id
            or payload.experience_entity_revision != experience.entity_revision
            or payload.experience_authority_event_ref
            != experience.origin.accepted_event_ref
        ):
            raise ValueError(
                "Experience-memory decision does not bind its Experience"
            )
        return payload

    def _record_experience_memory_decision(
        self,
        *,
        experience: ExperienceProjection,
        committed_experience,
        evaluated_projection,
        source_text: str,
        decision: FactMemoryRetentionDraft | None,
        trace_id: str,
        correlation_id: str,
    ) -> ExperienceMemoryDecisionRecordedPayload:
        existing = self._experience_memory_decision(experience)
        if existing is not None:
            return existing
        if self._memory_adapter is None:
            raise ValueError(
                "Experience-memory decision requires a configured character model"
            )
        if decision is None:
            decision_kind = "no_change"
            decision_value: object = {"decision": "no_change"}
        else:
            decision_kind = "retain"
            decision_value = decision.model_dump(mode="json")
        decision_json = canonical_experience_memory_decision_json(
            decision_value
        )
        request_hash = _digest(
            {
                "adapter_version": self._memory_adapter.adapter_version,
                "experience": experience.model_dump(mode="json"),
                "experience_authority_event_ref": (
                    experience.origin.accepted_event_ref
                ),
                "experience_authority_payload_hash": (
                    committed_experience.payload_hash
                ),
                "source_text_hash": hashlib.sha256(
                    source_text.encode()
                ).hexdigest(),
                "evaluated_cursor": {
                    "world_revision": evaluated_projection.world_revision,
                    "deliberation_revision": (
                        evaluated_projection.deliberation_revision
                    ),
                    "ledger_sequence": evaluated_projection.ledger_sequence,
                },
            }
        )
        for _attempt in range(8):
            current = self._ledger.project()
            recorded_at = current.logical_time
            if recorded_at is None:
                raise ValueError(
                    "Experience-memory decision requires World logical time"
                )
            payload = ExperienceMemoryDecisionRecordedPayload(
                decision_id=experience_memory_decision_identity(
                    experience_authority_event_ref=(
                        experience.origin.accepted_event_ref
                    )
                ),
                experience_id=experience.experience_id,
                experience_entity_revision=experience.entity_revision,
                experience_authority_event_ref=(
                    experience.origin.accepted_event_ref
                ),
                experience_authority_world_revision=(
                    committed_experience.world_revision
                ),
                experience_authority_payload_hash=(
                    committed_experience.payload_hash
                ),
                evaluated_world_revision=evaluated_projection.world_revision,
                adapter_version=self._memory_adapter.adapter_version,
                model_id=self._memory_adapter.model_id,
                request_hash=request_hash,
                decision_kind=decision_kind,
                decision_json=decision_json,
                decision_hash=experience_memory_decision_hash(decision_json),
                recorded_at=recorded_at,
            )
            event = self._event(
                event_id=experience_memory_decision_event_id(
                    experience_authority_event_ref=(
                        experience.origin.accepted_event_ref
                    )
                ),
                event_type="ExperienceMemoryDecisionRecorded",
                payload=payload.model_dump(mode="json"),
                logical_time=recorded_at,
                trace_id=trace_id,
                causation_id=experience.origin.accepted_event_ref,
                correlation_id=correlation_id,
            )
            try:
                self._ledger.commit(
                    (event,),
                    expected_world_revision=current.world_revision,
                    expected_deliberation_revision=(
                        current.deliberation_revision
                    ),
                )
            except (ConcurrencyConflict, IdempotencyConflict):
                joined = self._experience_memory_decision(experience)
                if joined is not None:
                    return joined
                continue
            return payload
        raise ConcurrencyConflict(
            "Experience-memory decision could not join a stable ledger cursor"
        )

    def _candidate_text(self, content_ref: str | None, content_hash: str | None) -> str:
        if content_ref is None or content_hash is None:
            raise ValueError("aftermath candidate has no immutable content binding")
        record = self._content_store.read_exact(content_ref=content_ref)
        if record is None or record.content_payload_hash != content_hash:
            raise ValueError("aftermath candidate content is unavailable")
        return record.text

    @staticmethod
    def _has_experience(projection, occurrence_id: str) -> bool:
        return any(
            any(
                isinstance(binding, ExperienceOccurrenceSettlementBinding)
                and binding.occurrence_id == occurrence_id
                for binding in item.values.source_bindings
            )
            for item in projection.experiences
            if isinstance(item, ExperienceProjection)
        )

    @staticmethod
    def _event_evidence(event_ref, *, purpose: str) -> EvidenceRef:
        return EvidenceRef(
            ref_id=event_ref.event_id,
            evidence_type="committed_world_event",
            claim_purpose=purpose,
            source_world_revision=event_ref.world_revision,
            immutable_hash=event_ref.payload_hash,
        )

    def _event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        logical_time: datetime,
        trace_id: str,
        causation_id: str,
        correlation_id: str,
    ) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=logical_time,
            created_at=logical_time,
            actor=self._actor,
            source="world-v2:life-aftermath",
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type=event_type, world_id=self._ledger.world_id, payload=payload
            )
            or f"life-aftermath:{event_type}:{_digest([self._ledger.world_id, event_id, payload])}",
            payload=payload,
        )

    def _commit(self, events: tuple[WorldEvent, ...], *, commit_id: str):
        projection = self._ledger.project()
        return self._ledger.commit_at_cursor(
            events,
            expected_cursor=ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            ),
            commit_id=commit_id,
        )


__all__ = ["LifeAftermathResult", "LifeAftermathRuntime"]
