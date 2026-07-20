"""Reviewed activity aftermath: occurrence, settlement, experience, content."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import logging
from typing import Literal

import httpx

from .batch_invariants import appraisal_trigger_identity
from .event_identity import domain_idempotency_key
from .experience_memory_candidate_lifecycle import ExperienceMemoryCandidateLifecycle
from .experience_events import ExperienceCommittedPayload, experience_mutation_hash
from .fact_memory_draft import FactMemoryDraftAdapter, FactMemoryRetentionDraft
from .life_author_seed import ReviewedLifeSeedCatalog
from .life_content_events import LifeContentRecordedPayload
from .life_content_store import ImmutableLifeContentStore, StoredLifeContent, life_content_payload_hash
from .life_events import (
    OutcomeObservationRecordedPayload,
    OutcomeProposalRecordedPayload,
    WorldOccurrenceActivatedPayload,
    WorldOccurrenceSettledPayload,
    outcome_mutation_hash,
)
from .occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OccurrenceContentCoordinator,
    OutcomeCandidateContent,
)
from .mood_view import mood_summary_prose
from .outcome_selection_draft import (
    OutcomeSelectionDraftAdapter,
    OutcomeSelectionModel,
    OutcomeSelectionOption,
)
from .plan_evidence import canonical_plan_evidence_hash
from .random_authority import RandomAuthority
from .schema_core import FrozenModel
from .schemas import (
    DueWindow,
    EvidenceRef,
    ExperienceOccurrenceSettlementBinding,
    ExperienceOrigin,
    ExperienceProjection,
    ExperienceProposalProjection,
    ExperienceProposedMutation,
    ExperienceValues,
    MEMORY_SALIENCE_MATRIX_DIGEST,
    MemorySalienceVector,
    OutcomeObservationProjection,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
    WorldOccurrenceProjection,
    experience_semantic_fingerprint,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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

    return source_privacy if _PRIVACY_RANK[source_privacy] >= _PRIVACY_RANK["personal"] else "personal"


_LOG = logging.getLogger(__name__)


class LifeAftermathResult(FrozenModel):
    status: Literal["occurrence_opened", "settled", "recovered_experience", "no_op"]
    reason_code: str
    occurrence_id: str | None = None
    experience_id: str | None = None


class LifeAftermathRuntime:
    """One bounded authority seam from accepted activity to lived history.

    Models never provide identities, locations, result refs, hashes, or prose.
    Candidate prose comes only from the reviewed seed. When an optional
    outcome-selection model is installed it chooses among those candidates;
    the durable random draw remains the deterministic fallback when the model
    is unavailable or returns an invalid response.
    """

    def __init__(
        self, *, ledger, catalog: ReviewedLifeSeedCatalog,
        occurrence_content: OccurrenceContentCoordinator,
        content_store: ImmutableLifeContentStore, owner_actor_ref: str,
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
        self._memory_adapter = memory_adapter
        self._owner_actor_ref = owner_actor_ref
        self._actor = actor
        self._random = RandomAuthority(ledger=ledger, source="world-v2:life-aftermath-random")

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> LifeAftermathResult:
        projection = self._ledger.project()
        wake = next(
            (
                item for item in projection.committed_world_event_refs
                if item.event_id == wake_event_ref
                and item.event_type in {"ClockAdvanced", "ActivityCompleted", "ActivityAbandoned"}
            ),
            None,
        )
        if wake is None or projection.logical_time is None or wake.logical_time > projection.logical_time:
            return LifeAftermathResult(status="no_op", reason_code="life_aftermath.wake_unavailable")

        recoverable = next(
            (
                item for item in projection.world_occurrences
                if item.status == "settled" and not self._has_experience(projection, item.occurrence_id)
            ),
            None,
        )
        if recoverable is not None:
            experience_id = await self._commit_experience(
                occurrence=recoverable, logical_time=projection.logical_time,
                trace_id=trace_id, correlation_id=correlation_id,
            )
            return LifeAftermathResult(
                status="recovered_experience", reason_code="life_aftermath.experience_recovered",
                occurrence_id=recoverable.occurrence_id, experience_id=experience_id,
            )

        active = next(
            (
                item for item in projection.world_occurrences
                if item.status == "active" and item.activated_at is not None
                and (
                    item.activated_at < wake.logical_time
                    or wake.event_type in {"ActivityCompleted", "ActivityAbandoned"}
                )
            ),
            None,
        )
        if active is not None:
            experience_id = await self._settle(
                occurrence=active, wake=wake, logical_time=projection.logical_time,
                trace_id=trace_id, correlation_id=correlation_id,
            )
            return LifeAftermathResult(
                status="settled", reason_code="life_aftermath.settled",
                occurrence_id=active.occurrence_id, experience_id=experience_id,
            )

        existing_plan_ids = {item.trigger_ref for item in projection.world_occurrences}
        plan = next(
            (
                item for item in projection.plans
                if item.owner_actor_ref == self._owner_actor_ref and item.status == "active"
                and item.plan_id not in existing_plan_ids and item.location_ref is not None
                and self._catalog.outcomes_for_activity(item.activity_kind)
            ),
            None,
        )
        if plan is None:
            return LifeAftermathResult(status="no_op", reason_code="life_aftermath.no_eligible_activity")
        occurrence_id = self._open_occurrence(
            plan=plan, wake=wake, logical_time=projection.logical_time,
            trace_id=trace_id, correlation_id=correlation_id,
        )
        return LifeAftermathResult(
            status="occurrence_opened", reason_code="life_aftermath.occurrence_opened",
            occurrence_id=occurrence_id,
        )

    def _open_occurrence(self, *, plan, wake, logical_time: datetime,
                         trace_id: str, correlation_id: str) -> str:
        outcomes = self._catalog.outcomes_for_activity(plan.activity_kind)
        suffix = _digest({"world": self._ledger.world_id, "plan": plan.plan_id})
        occurrence_id = "occurrence:life-aftermath:" + suffix
        existing = self._ledger.project()
        if any(item.occurrence_id == occurrence_id for item in existing.world_occurrences):
            return occurrence_id
        candidate_contents = tuple(
            OutcomeCandidateContent(
                candidate_result_ref=f"candidate:life-aftermath:{suffix}:{item.id}",
                result_id=f"result:life-aftermath:{suffix}:{item.id}",
                result_payload_ref=f"content:occurrence-result:{suffix}:{item.id}",
                result_payload_hash=life_content_payload_hash(item.text),
                privacy_class=item.privacy,
                content_ref=f"content:outcome-candidate:{suffix}:{item.id}",
                text=item.text,
            )
            for item in outcomes
        )
        wake_evidence = self._event_evidence(wake, purpose="life_transition")
        plan_evidence = EvidenceRef(
            ref_id=plan.plan_id, evidence_type="active_plan", claim_purpose="life_transition",
            immutable_hash=canonical_plan_evidence_hash(plan),
        )
        occurrence = WorldOccurrenceProjection(
            occurrence_id=occurrence_id, entity_revision=1, trigger_ref=plan.plan_id,
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
            visibility=plan.privacy_class, status="committed",
        )
        self._occurrence_content.commit(OccurrenceContentCommitRequest(
            world_id=self._ledger.world_id, occurrence=occurrence,
            candidate_contents=candidate_contents,
            change_id="change:life-aftermath:occurrence:" + suffix,
            transition_id="transition:life-aftermath:occurrence:" + suffix,
            evidence_refs=(plan_evidence, wake_evidence),
            policy_refs=("policy:life-aftermath.1",), logical_time=logical_time,
            created_at=logical_time, actor=self._actor, source="world-v2:life-aftermath",
            trace_id=trace_id, causation_id=wake.event_id, correlation_id=correlation_id,
        ))
        projected = self._ledger.project()
        committed = next(item for item in projected.world_occurrences if item.occurrence_id == occurrence_id)
        payload = WorldOccurrenceActivatedPayload(
            change_id="change:life-aftermath:activate:" + suffix,
            transition_id="transition:life-aftermath:activate:" + suffix,
            expected_entity_revision=1, evidence_refs=(wake_evidence,),
            policy_refs=("policy:life-aftermath.1",), occurrence_id=occurrence_id,
            activated_at=logical_time, satisfied_precondition_refs=(),
        )
        event = self._event(
            event_id="event:life-aftermath:activate:" + suffix,
            event_type="WorldOccurrenceActivated", payload=payload.model_dump(mode="json"),
            logical_time=logical_time, trace_id=trace_id, causation_id=wake.event_id,
            correlation_id=correlation_id,
        )
        self._commit((event,), commit_id="commit:life-aftermath:activate:" + suffix)
        return committed.occurrence_id

    async def _settle(self, *, occurrence, wake, logical_time: datetime,
                trace_id: str, correlation_id: str) -> str:
        wake_evidence = self._event_evidence(wake, purpose="life_transition")
        suffix = occurrence.occurrence_id.removeprefix("occurrence:life-aftermath:")
        observation_id = "observation:life-aftermath:" + _digest([occurrence.occurrence_id, wake.event_id])
        observation = OutcomeObservationProjection(
            observation_id=observation_id, occurrence_id=occurrence.occurrence_id,
            source_kind="committed_world_event", source_refs=(wake.event_id,),
            observed_payload_ref=wake.event_id, observed_payload_hash=wake.payload_hash,
            observed_at=logical_time, confidence_bp=10_000,
        )
        observation_payload = OutcomeObservationRecordedPayload(
            change_id="change:life-aftermath:observation:" + suffix,
            transition_id="transition:life-aftermath:observation:" + suffix,
            expected_entity_revision=occurrence.entity_revision,
            evidence_refs=(wake_evidence,), policy_refs=("policy:life-aftermath.1",),
            observation=observation,
        )
        observation_event = self._event(
            event_id=f"event:outcome-observation:{observation_id}",
            event_type="OutcomeObservationRecorded",
            payload=observation_payload.model_dump(mode="json"), logical_time=logical_time,
            trace_id=trace_id, causation_id=wake.event_id, correlation_id=correlation_id,
        )
        if self._ledger.lookup_event_commit(observation_event.event_id) is None:
            self._commit((observation_event,), commit_id="commit:life-aftermath:observation:" + suffix)

        projection = self._ledger.project()
        occurrence = next(item for item in projection.world_occurrences if item.occurrence_id == occurrence.occurrence_id)
        draw = self._random.draw(
            attempt_id="attempt:life-aftermath:" + _digest([occurrence.occurrence_id, wake.event_id]),
            candidate_refs=occurrence.candidate_outcome_refs,
            catalog_version="life-aftermath.1", logical_time=logical_time,
            seed_instant=wake.logical_time, actor=self._actor, trace_id=trace_id,
            correlation_id=correlation_id,
        )
        projection = self._ledger.project()
        chosen = next(
            item for item in occurrence.candidate_outcomes
            if item.candidate_result_ref == draw.selected_candidate_ref
        )
        proposal_id = "proposal:life-aftermath:outcome:" + _digest([occurrence.occurrence_id, wake.event_id])
        proposal_event_id = "event:life-aftermath:outcome-proposal:" + suffix
        existing_proposal = self._ledger.lookup_event_commit(proposal_event_id)
        if existing_proposal is not None:
            persisted = OutcomeProposalRecordedPayload.model_validate_json(
                existing_proposal[0].payload_json
            )
            chosen = next(
                item
                for item in occurrence.candidate_outcomes
                if item.candidate_result_ref == persisted.candidate_result_ref
            )
        elif self._outcome_selection is not None:
            options = tuple(
                OutcomeSelectionOption(
                    candidate_result_ref=item.candidate_result_ref,
                    summary=self._candidate_text(item.content_ref, item.content_payload_hash),
                )
                for item in occurrence.candidate_outcomes
            )
            try:
                selected = await self._outcome_selection.deliberate(
                    options=options,
                    mood_summary=mood_summary_prose(projection.affect_episodes) or None,
                )
            except (TimeoutError, ConnectionError, OSError, httpx.HTTPError, ValueError) as exc:
                _LOG.warning(
                    "life aftermath outcome model unavailable; using reviewed random fallback: %s",
                    exc,
                )
            else:
                chosen = next(
                    item
                    for item in occurrence.candidate_outcomes
                    if item.candidate_result_ref == selected.candidate_result_ref
                )
        change_id = "change:life-aftermath:settle:" + suffix
        change_hash = outcome_mutation_hash(
            change_id=change_id, occurrence_id=occurrence.occurrence_id,
            evaluated_entity_revision=occurrence.entity_revision,
            evaluated_world_revision=projection.world_revision,
            candidate_result_ref=chosen.candidate_result_ref, result_id=chosen.result_id,
            result_payload_ref=chosen.result_payload_ref,
            result_payload_hash=chosen.result_payload_hash,
            observation_refs=(observation_id,),
        )
        proposal_payload = OutcomeProposalRecordedPayload(
            outcome_proposal_id=proposal_id, decision_proposal_id=proposal_id,
            change_id=change_id, occurrence_id=occurrence.occurrence_id,
            evaluated_entity_revision=occurrence.entity_revision,
            evaluated_world_revision=projection.world_revision,
            trigger_ref=occurrence.trigger_ref,
            candidate_result_ref=chosen.candidate_result_ref,
            proposed_result_id=chosen.result_id,
            proposed_result_payload_ref=chosen.result_payload_ref,
            proposed_result_payload_hash=chosen.result_payload_hash,
            proposed_change_hash=change_hash, observation_refs=(observation_id,),
            precondition_refs=occurrence.satisfied_precondition_refs,
            evidence_refs=(wake_evidence,), confidence_bp=10_000,
            expires_at=logical_time + timedelta(minutes=5),
        )
        proposal_event = self._event(
            event_id=proposal_event_id,
            event_type="OutcomeProposalRecorded", payload=proposal_payload.model_dump(mode="json"),
            logical_time=logical_time, trace_id=trace_id, causation_id=observation_event.event_id,
            correlation_id=correlation_id,
        )
        if self._ledger.lookup_event_commit(proposal_event.event_id) is None:
            self._commit((proposal_event,), commit_id="commit:life-aftermath:proposal:" + suffix)

        projection = self._ledger.project()
        acceptance_id = "acceptance:life-aftermath:" + suffix
        acceptance_payload = {
            "status": "accepted", "acceptance_id": acceptance_id,
            "proposal_id": proposal_id, "evaluated_world_revision": projection.world_revision,
            "accepted_change_id": change_id, "accepted_change_hash": change_hash,
        }
        acceptance_event = self._event(
            event_id="event:life-aftermath:acceptance:" + suffix,
            event_type="AcceptanceRecorded", payload=acceptance_payload,
            logical_time=logical_time, trace_id=trace_id, causation_id=proposal_event.event_id,
            correlation_id=correlation_id,
        )
        trigger_id = appraisal_trigger_identity(occurrence.occurrence_id, chosen.result_id)
        settlement_payload = WorldOccurrenceSettledPayload(
            change_id=change_id, transition_id="transition:life-aftermath:settle:" + suffix,
            expected_entity_revision=occurrence.entity_revision,
            evidence_refs=(wake_evidence,), policy_refs=("policy:outcome-v1",),
            acceptance_id=acceptance_id, evaluated_world_revision=projection.world_revision,
            accepted_change_hash=change_hash, occurrence_id=occurrence.occurrence_id,
            outcome_proposal_id=proposal_id, candidate_result_ref=chosen.candidate_result_ref,
            result_id=chosen.result_id, observation_refs=(observation_id,),
            result_payload_ref=chosen.result_payload_ref,
            result_payload_hash=chosen.result_payload_hash, settled_at=logical_time,
            appraisal_trigger_ref=trigger_id,
        )
        settlement_event = self._event(
            event_id="event:life-aftermath:settlement:" + suffix,
            event_type="WorldOccurrenceSettled", payload=settlement_payload.model_dump(mode="json"),
            logical_time=logical_time, trace_id=trace_id, causation_id=acceptance_event.event_id,
            correlation_id=correlation_id,
        )
        trigger = TriggerProcess(
            trigger_id=trigger_id, trigger_ref=trigger_id, process_kind="npc_world_appraisal",
            source_evidence_ref=settlement_event.event_id, state="open",
        )
        trigger_event = self._event(
            event_id="event:life-aftermath:appraisal-trigger:" + suffix,
            event_type="TriggerProcessOpened", payload={"process": trigger.model_dump(mode="json")},
            logical_time=logical_time, trace_id=trace_id, causation_id=settlement_event.event_id,
            correlation_id=correlation_id,
        )
        if self._ledger.lookup_event_commit(settlement_event.event_id) is None:
            self._commit(
                (acceptance_event, settlement_event, trigger_event),
                commit_id="commit:life-aftermath:settlement:" + suffix,
            )

        result_record = StoredLifeContent(
            content_ref=chosen.result_payload_ref, content_kind="occurrence_result",
            content_payload_hash=chosen.result_payload_hash,
            text=self._candidate_text(chosen.content_ref, chosen.content_payload_hash),
        )
        self._content_store.put_if_absent(result_record)
        settled_projection = self._ledger.project()
        settlement_ref = next(
            item for item in settled_projection.committed_world_event_refs
            if item.event_id == settlement_event.event_id
        )
        descriptor = LifeContentRecordedPayload(
            content_id="life-content:occurrence:" + suffix,
            content_kind="occurrence_result", content_ref=result_record.content_ref,
            content_payload_hash=result_record.content_payload_hash,
            privacy_class=chosen.privacy_class, source_kind="occurrence_settlement",
            source_event_ref=settlement_ref.event_id,
            source_world_revision=settlement_ref.world_revision,
            source_payload_hash=settlement_ref.payload_hash,
            source_entity_id=occurrence.occurrence_id, source_entity_revision=4,
        )
        descriptor_event = self._event(
            event_id="event:life-content:occurrence:" + suffix,
            event_type="LifeContentRecorded", payload=descriptor.model_dump(mode="json"),
            logical_time=logical_time, trace_id=trace_id, causation_id=settlement_event.event_id,
            correlation_id=correlation_id,
        )
        if self._ledger.lookup_event_commit(descriptor_event.event_id) is None:
            self._commit((descriptor_event,), commit_id="commit:life-content:occurrence:" + suffix)
        settled = next(
            item for item in self._ledger.project().world_occurrences
            if item.occurrence_id == occurrence.occurrence_id
        )
        return await self._commit_experience(
            occurrence=settled, logical_time=logical_time,
            trace_id=trace_id, correlation_id=correlation_id,
        )

    async def _commit_experience(self, *, occurrence, logical_time: datetime,
                           trace_id: str, correlation_id: str) -> str:
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
        if result_content is None or result_content.content_payload_hash != occurrence.result_payload_hash:
            descriptor = next(
                item for item in occurrence.candidate_outcomes
                if item.result_id == occurrence.result_id
            )
            result_content = StoredLifeContent(
                content_ref=occurrence.result_payload_ref, content_kind="occurrence_result",
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
                content_kind="occurrence_result", content_ref=result_content.content_ref,
                content_payload_hash=result_content.content_payload_hash,
                privacy_class=occurrence.visibility, source_kind="occurrence_settlement",
                source_event_ref=settlement_event.event_id,
                source_world_revision=settlement_commit.world_revision,
                source_payload_hash=settlement_event.payload_hash,
                source_entity_id=occurrence.occurrence_id,
                source_entity_revision=occurrence.entity_revision,
            )
            descriptor_event = self._event(
                event_id="event:life-content:occurrence:" + suffix,
                event_type="LifeContentRecorded", payload=descriptor.model_dump(mode="json"),
                logical_time=logical_time, trace_id=trace_id,
                causation_id=settlement_event.event_id, correlation_id=correlation_id,
            )
            self._commit(
                (descriptor_event,), commit_id="commit:life-content:occurrence:" + suffix
            )
        summary_ref = "content:experience-summary:" + suffix
        summary = StoredLifeContent(
            content_ref=summary_ref, content_kind="experience_summary",
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
            result_id=occurrence.result_id, result_payload_ref=occurrence.result_payload_ref,
            result_payload_hash=occurrence.result_payload_hash,
        )
        experience_privacy = _experience_privacy(occurrence.visibility)
        values = ExperienceValues(
            summary_ref=summary_ref, summary_payload_hash=summary.content_payload_hash,
            occurred_from=occurrence.activated_at, occurred_to=occurrence.settled_at,
            participant_refs=occurrence.participant_refs, source_bindings=(binding,),
            privacy_class=experience_privacy,
        )
        origin = ExperienceOrigin(
            change_id=change_id, transition_id=transition_id,
            policy_refs=policy_refs, accepted_event_ref=experience_event_id,
        )
        experience = ExperienceProjection(
            experience_id=experience_id,
            semantic_fingerprint=experience_semantic_fingerprint(values=values, policy_refs=policy_refs),
            values=values, origin=origin,
        )
        proposal_id = "proposal:life-aftermath:experience:" + suffix
        evidence = EvidenceRef(
            ref_id=settlement_event.event_id, evidence_type="settled_world_event",
            claim_purpose="past_experience", source_world_revision=settlement_commit.world_revision,
            immutable_hash=settlement_event.payload_hash,
        )
        base = {
            "change_id": change_id, "transition_id": transition_id,
            "expected_entity_revision": 0, "evidence_refs": (evidence,),
            "policy_refs": policy_refs, "acceptance_id": "acceptance:life-aftermath:experience:" + suffix,
            "proposal_id": proposal_id, "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64, "experience": experience,
        }
        base["accepted_change_hash"] = experience_mutation_hash(base)
        mutation = ExperienceCommittedPayload.model_validate(base)
        proposal = ExperienceProposalProjection(
            proposal_id=proposal_id, proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:experience.1", change_id=change_id,
            transition_id=transition_id, evaluated_world_revision=projection.world_revision,
            proposed_change_hash=mutation.accepted_change_hash, evidence_refs=(evidence,),
            policy_refs=policy_refs,
            proposed_mutation=ExperienceProposedMutation(
                payload_json=json.dumps(mutation.model_dump(mode="json"), ensure_ascii=False,
                                        sort_keys=True, separators=(",", ":"))
            ),
        )
        proposal_event = self._event(
            event_id="event:life-aftermath:experience-proposal:" + suffix,
            event_type="ProposalRecorded", payload=proposal.model_dump(mode="json"),
            logical_time=logical_time, trace_id=trace_id,
            causation_id=settlement_event.event_id, correlation_id=correlation_id,
        )
        if self._ledger.lookup_event_commit(proposal_event.event_id) is None:
            self._commit((proposal_event,), commit_id="commit:life-aftermath:experience-proposal:" + suffix)
        acceptance_payload = {
            "status": "accepted", "acceptance_id": mutation.acceptance_id,
            "proposal_id": proposal_id, "evaluated_world_revision": mutation.evaluated_world_revision,
            "accepted_change_id": change_id, "accepted_change_hash": mutation.accepted_change_hash,
        }
        acceptance_event = self._event(
            event_id="event:life-aftermath:experience-acceptance:" + suffix,
            event_type="AcceptanceRecorded", payload=acceptance_payload,
            logical_time=logical_time, trace_id=trace_id, causation_id=proposal_event.event_id,
            correlation_id=correlation_id,
        )
        experience_event = self._event(
            event_id=experience_event_id, event_type="ExperienceCommitted",
            payload=mutation.model_dump(mode="json"), logical_time=logical_time,
            trace_id=trace_id, causation_id=acceptance_event.event_id,
            correlation_id=correlation_id,
        )
        projected = self._ledger.project()
        content_payload = LifeContentRecordedPayload(
            content_id="life-content:experience:" + suffix,
            content_kind="experience_summary", content_ref=summary_ref,
            content_payload_hash=summary.content_payload_hash,
            privacy_class=experience_privacy, source_kind="experience",
            source_event_ref=experience_event.event_id,
            source_world_revision=projected.world_revision + 2,
            source_payload_hash=experience_event.payload_hash,
            source_entity_id=experience_id, source_entity_revision=1,
        )
        content_event = self._event(
            event_id="event:life-content:experience:" + suffix,
            event_type="LifeContentRecorded", payload=content_payload.model_dump(mode="json"),
            logical_time=logical_time, trace_id=trace_id, causation_id=experience_event.event_id,
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

    async def _materialize_experience_memory(
        self,
        *,
        experience_id: str,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> None:
        """Retain settled lived history through the existing memory authority.

        The semantic text remains the immutable sidecar and the candidate is
        still source-bound, private, replayable, and subject to normal memory
        withdrawal/decay.  An optional model may refine retention and salience
        inside the installed matrix without changing this authority seam; the
        continuity draft below is the deterministic fallback.
        """

        lifecycle = self._experience_memory_lifecycle
        if lifecycle is None:
            return
        projection = self._ledger.project()
        experience = next(
            (
                item
                for item in projection.experiences
                if isinstance(item, ExperienceProjection)
                and item.experience_id == experience_id
            ),
            None,
        )
        if experience is None:
            return
        if any(
            any(binding.source_id == experience_id for binding in item.values.source_bindings)
            for item in projection.memory_candidates
        ):
            return
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
        event, commit = located
        draft = FactMemoryRetentionDraft(
            cue_kind="world_continuity",
            retention_rationales=("world_continuity",),
            salience=MemorySalienceVector(
                autobiographical_relevance_bp=7_000,
                relationship_relevance_bp=2_000,
                emotional_residue_bp=2_000,
                unfinished_business_bp=1_000,
                recurrence_bp=3_000,
                novelty_bp=6_000,
                future_utility_bp=5_000,
                world_continuity_bp=9_000,
                matrix_digest=MEMORY_SALIENCE_MATRIX_DIGEST,
            ),
        )
        if self._memory_adapter is not None:
            try:
                summary = self._content_store.read_exact(content_ref=experience.values.summary_ref)
                if summary is None or summary.content_payload_hash != experience.values.summary_payload_hash:
                    raise ValueError("experience summary sidecar is unavailable for memory classification")
                classified = await self._memory_adapter.classify(
                    predicate_code="world.experience",
                    source_text=summary.text,
                )
            except (TimeoutError, ConnectionError, OSError, httpx.HTTPError, ValueError) as exc:
                _LOG.warning(
                    "experience memory model unavailable; using continuity fallback: %s", exc
                )
            else:
                if classified is not None:
                    draft = classified
                else:
                    # A settled Experience is her lived history: continuity
                    # retention is the design default and decay/withdrawal own
                    # forgetting.  The model refines salience when it engages;
                    # its fact-shaped "retain=false" must not silently erase
                    # the lived day (in production it declined every single
                    # experience and she accumulated zero memories).
                    _LOG.warning(
                        "experience memory model declined; keeping continuity draft for %s",
                        experience_id,
                    )
        lifecycle.accept(
            experience=experience,
            transition=transition,
            experience_event=event,
            experience_world_revision=commit.world_revision,
            draft=draft,
            logical_time=logical_time,
            created_at=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
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
            ref_id=event_ref.event_id, evidence_type="committed_world_event",
            claim_purpose=purpose, source_world_revision=event_ref.world_revision,
            immutable_hash=event_ref.payload_hash,
        )

    def _event(self, *, event_id: str, event_type: str, payload: dict[str, object],
               logical_time: datetime, trace_id: str, causation_id: str,
               correlation_id: str) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1", event_id=event_id, world_id=self._ledger.world_id,
            event_type=event_type, logical_time=logical_time, created_at=logical_time,
            actor=self._actor, source="world-v2:life-aftermath", trace_id=trace_id,
            causation_id=causation_id, correlation_id=correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type=event_type, world_id=self._ledger.world_id, payload=payload
            ) or f"life-aftermath:{event_type}:{_digest([self._ledger.world_id, event_id, payload])}",
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
