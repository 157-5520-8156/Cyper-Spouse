"""Cross-event invariants that must hold inside one atomic ledger commit."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
import hashlib
import json

from .accepted_effect_contracts import rehydrate_acceptance_manifest_v3
from .appraisal_acceptance_manifest import (
    APPRAISAL_ACCEPTANCE_MANIFEST_VERSION,
    AppraisalAcceptanceManifest,
    canonical_appraisal_acceptance_value_hash,
)
from .affect_acceptance_manifest import (
    AFFECT_ACCEPTANCE_MANIFEST_VERSION,
    AffectAcceptanceManifest,
    canonical_affect_acceptance_value_hash,
)
from .relationship_acceptance_manifest import (
    RELATIONSHIP_ACCEPTANCE_MANIFEST_VERSION,
    RelationshipAcceptanceManifest,
    canonical_relationship_acceptance_value_hash,
)
from .relationship_events import RelationshipSignalAcceptedPayload
from .relationship_adjustment_acceptance_manifest import (
    RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION,
    RelationshipAdjustmentAcceptanceManifest,
    canonical_relationship_adjustment_acceptance_value_hash,
)
from .relationship_events import RelationshipSlowVariableAdjustedPayload
from .activity_lifecycle_acceptance_manifest import (
    ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
    ActivityLifecycleAcceptanceManifest,
    canonical_activity_lifecycle_acceptance_value_hash,
)
from .media_selection_acceptance_manifest import (
    MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS,
    canonical_media_selection_value_hash,
    parse_media_selection_acceptance_manifest,
)
from .media_continuation_acceptance_manifest import (
    MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION,
    MediaContinuationAcceptanceManifest,
    canonical_media_continuation_hash,
    media_continuation_event_identity,
)
from .media_v2 import MediaOpportunityFrozenPayload
from .outcome_acceptance_manifest import (
    OUTCOME_ACCEPTANCE_MANIFEST_VERSION,
    OutcomeAcceptanceManifest,
    canonical_outcome_acceptance_value_hash,
)
from .interaction_bid_acceptance_manifest import (
    INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION,
    InteractionBidAcceptanceManifest,
    canonical_interaction_bid_value_hash,
)
from .interaction_bid_events import InteractionBidOpenedPayload
from .media_thread_acceptance_manifest import (
    MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION,
    MediaDeliveryThreadAcceptanceManifest,
    canonical_media_thread_value_hash,
)
from .media_thread_events import MediaDeliveryThreadChangedPayload
from .event_identity import domain_idempotency_key
from .commitment_events import CommitmentChangedPayload
from .thread_events import ThreadChangedPayload
from .experience_events import ExperienceCommittedPayload
from .fact_accepted_contracts import (
    fact_commit_event_payload_hash,
    rehydrate_fact_commit_materialized_v2_json,
)
from .life_events import (
    ActivityPlannedPayload,
    ActivityTransitionPayload,
    NpcStateChangedPayload,
    OutcomeProposalRecordedPayload,
    WorldOccurrenceCommittedPayload,
    WorldOccurrenceSettledPayload,
)
from .life_content_events import LifeContentRecordedPayload
from .aspiration_events import AspirationCrystallizedPayload
from .life_development_draft import (
    LifeDevelopmentCapabilityManifest,
    LifeDevelopmentLocationCapability,
)
from .life_review_identity import (
    current_novel_origin_review_subject_hash,
    current_source_review_subject_hash,
    legacy_novel_origin_review_subject_hashes,
    legacy_source_review_subject_hash,
)
from .proposal_audit_schemas import (
    ModelResultRecordedPayload,
    ProposalRecordedV2Payload,
    RecordedModelResultAudit,
)
from .acceptance_manifest import parse_acceptance_manifest_v2
from .minimal_reply_events import (
    ExpressionBeatAuthorizedPayload,
    ExpressionBeatSettledPayload,
    ExpressionBeatTerminatedPayload,
    ExpressionPlanAcceptedPayload,
    ExpressionPlanCompletedPayload,
    ExpressionPlanTerminatedPayload,
    MessagePayloadStoredPayload,
    minimal_reply_event_id,
    minimal_reply_idempotency_key,
)
from .expression_payload_events import ExpressionPayloadDescriptorRecordedPayload
from .minimal_reply_manifest import (
    MINIMAL_REPLY_MANIFEST_VERSION,
    MinimalReplyManifest,
    canonical_minimal_reply_value_hash,
)
from .expression_plan_manifest import (
    EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION,
    ExpressionPlanAcceptanceManifest,
    canonical_expression_plan_value_hash,
)
from .expression_plan_atomic_recorder import (
    expression_plan_event_id,
    expression_plan_idempotency_key,
)
from .social_action_acceptance import (
    SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSIONS,
    parse_social_deferred_acceptance_manifest,
    social_deferred_authority_event_types,
)
from .external_perception_acceptance_manifest import (
    EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION,
    EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_DIGEST,
    ExternalPerceptionAcceptanceManifest,
)
from .external_perception_events import (
    ExternalPerceptionRecordedPayload,
    ExternalSignalSnapshotAdoptedPayload,
)
from .appraisal_events import (
    AppraisalAcceptedPayload,
    AppraisalContradictedPayload,
    AppraisalSupersededPayload,
)
from .affect_events import AFFECT_PAYLOAD_MODELS, AffectAuthorizedMutationPayload
from .media_v2 import (
    MediaRenderArtifactRecordedPayload,
    artifact_continuation_trigger_id,
    MediaPlanRecordedPayload,
    MediaRepairAuthorizedPayload,
    continuation_trigger_id,
)
from .schemas import (
    Action,
    BudgetReservation,
    DueWindow,
    ExperienceOccurrenceSettlementBinding,
    OutcomeCandidateDescriptor,
    TriggerProcess,
    WorldEvent,
)
from .typed_proposal_families import (
    family_for_mutation,
    family_for_record,
)


def _reject_new_private_impression_without_role_reflection(
    events: Sequence[WorldEvent],
) -> None:
    """Keep legacy decode compatibility out of the current write authority."""

    for event in events:
        if event.event_type != "PrivateImpressionAccepted":
            continue
        payload = event.payload()
        impression = payload.get("impression")
        contract = payload.get("reflection_contract")
        transition_kind = payload.get("transition_kind", "open")
        expected_decision = "retain" if transition_kind == "open" else transition_kind
        if (
            contract
            not in {
                "private-impression-draft.4",
                "character-interior-private-impression-transition.1",
            }
            or payload.get("reflection_decision") != expected_decision
            or not payload.get("reflection_source_refs")
            or not payload.get("source_model_result")
            or not payload.get("source_capsule_id")
            or not isinstance(impression, dict)
            or not impression.get("reflection_summary")
        ):
            raise ValueError("private_impression.new_write_requires_role_reflection")


def _validate_npc_state_content_batch(events: Sequence[WorldEvent]) -> None:
    """Bind every new NPC private-state byte string in the same atomic commit."""

    descriptors_by_source: dict[str, list[LifeContentRecordedPayload]] = {}
    for event in events:
        if event.event_type != "LifeContentRecorded":
            continue
        payload = LifeContentRecordedPayload.model_validate_json(event.payload_json)
        if payload.source_kind == "npc_state":
            descriptors_by_source.setdefault(payload.source_event_ref, []).append(payload)

    for event in events:
        if event.event_type != "NpcStateChanged":
            continue
        payload = NpcStateChangedPayload.model_validate_json(event.payload_json)
        state = payload.npc_after.subjective_state
        assert state is not None
        expected = {
            (
                "npc_inner_state",
                state.inner_state_content_ref,
                state.inner_state_payload_hash,
            ),
            *{
                ("npc_goal", ref, content_hash)
                for ref, content_hash in zip(
                    state.goal_content_refs,
                    state.goal_content_hashes,
                    strict=True,
                )
            },
        }
        actual_rows = descriptors_by_source.get(event.event_id, [])
        actual = {
            (item.content_kind, item.content_ref, item.content_payload_hash)
            for item in actual_rows
        }
        if actual != expected or len(actual_rows) != len(expected):
            raise ValueError(
                "NpcStateChanged requires an exact same-batch private content closure"
            )
        if any(
            item.source_payload_hash != event.payload_hash
            or item.source_entity_id != payload.npc_after.npc_id
            or item.source_entity_revision != payload.npc_after.entity_revision
            or item.privacy_class != payload.npc_after.privacy_class
            for item in actual_rows
        ):
            raise ValueError("NPC state content descriptor disagrees with its authority")


def validate_commit_batch(
    events: Sequence[WorldEvent],
    *,
    expected_world_revision: int,
    accepted_manifest_v3_authorized: bool = False,
) -> None:
    """Require every settled lived-world occurrence to schedule its appraisal."""

    if type(accepted_manifest_v3_authorized) is not bool:
        raise ValueError("accepted manifest v3 authorization must be an exact boolean")
    if not accepted_manifest_v3_authorized:
        reject_accepted_manifest_v3_without_recorder(events)
        reject_minimal_reply_manifest_without_recorder(events)
        reject_appraisal_acceptance_manifest_without_recorder(events)
        reject_affect_acceptance_manifest_without_recorder(events)
        reject_relationship_acceptance_manifest_without_recorder(events)
        reject_relationship_adjustment_acceptance_manifest_without_recorder(events)
        reject_activity_lifecycle_acceptance_manifest_without_recorder(events)
        reject_media_selection_acceptance_manifest_without_recorder(events)
        reject_media_continuation_acceptance_manifest_without_recorder(events)
        reject_outcome_acceptance_manifest_without_recorder(events)
        reject_interaction_bid_acceptance_manifest_without_recorder(events)
        reject_media_thread_acceptance_manifest_without_recorder(events)
        reject_expression_plan_manifest_without_recorder(events)
        reject_social_deferred_manifest_without_recorder(events)
        reject_external_perception_manifest_without_recorder(events)
    _reject_new_private_impression_without_role_reflection(events)
    _validate_deliberation_audit_transaction(events)
    _validate_life_development_location_authority_batch(events)
    _validate_npc_state_content_batch(events)
    _validate_acceptance_manifest_v2_batch(events)
    _validate_authorized_fact_manifest_v3_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_minimal_reply_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_expression_plan_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_social_deferred_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_appraisal_acceptance_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_affect_acceptance_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_relationship_acceptance_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_relationship_adjustment_acceptance_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_activity_lifecycle_acceptance_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_media_selection_acceptance_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_media_continuation_acceptance_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_outcome_acceptance_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_interaction_bid_acceptance_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_media_thread_acceptance_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_authorized_external_perception_manifest_batch(
        events,
        expected_world_revision=expected_world_revision,
        authorized=accepted_manifest_v3_authorized,
    )
    _validate_expression_receipt_lifecycle_batch(events)
    _validate_media_planning_settlement_batch(events)
    _validate_media_render_continuation_batch(events)
    _validate_media_repair_acceptance_batch(events)

    appraisal_triggers: dict[str, list[tuple[str, str, str | None]]] = {}
    experiences: list[ExperienceCommittedPayload] = []
    settlement_events = [
        (
            index,
            event,
            WorldOccurrenceSettledPayload.model_validate_json(event.payload_json),
        )
        for index, event in enumerate(events)
        if event.event_type == "WorldOccurrenceSettled"
    ]
    settlements = [payload for _, _, payload in settlement_events]
    acceptances = [
        (index, event.payload())
        for index, event in enumerate(events)
        if event.event_type == "AcceptanceRecorded"
    ]
    typed_proposals = []
    for event in events:
        family = family_for_record(event.event_type, event.payload())
        if family is None:
            continue
        proposal = family.codec.decode_record(
            event_type=event.event_type,
            payload=event.payload(),
        )
        binding = family.codec.bind(proposal)
        if binding.evaluated_world_revision != expected_world_revision:
            raise ValueError("typed proposal must be pinned to the current world revision")
        typed_proposals.append((family, binding))
    if any(family.requires_separate_deliberation_commit for family, _ in typed_proposals) and any(
        event.event_type != "ProposalRecorded" for event in events
    ):
        raise ValueError("typed proposal requires a separate deliberation commit")
    authorized_appraisal_models = {
        "AppraisalAccepted": AppraisalAcceptedPayload,
        "AppraisalContradicted": AppraisalContradictedPayload,
        "AppraisalSuperseded": AppraisalSupersededPayload,
    }
    typed_mutations = []
    for mutation_index, event in enumerate(events):
        family = family_for_mutation(event.event_type)
        if family is None:
            continue
        mutation = family.codec.decode_mutation(
            event_type=event.event_type,
            payload=event.payload(),
        )
        binding = family.codec.bind_mutation(mutation)
        typed_mutations.append((mutation_index, binding))
    for mutation_index, event in enumerate(events):
        model = authorized_appraisal_models.get(event.event_type)
        if model is None:
            continue
        appraisal = model.model_validate_json(event.payload_json)
        matching = [
            acceptance
            for acceptance_index, acceptance in acceptances
            if acceptance_index < mutation_index
            and acceptance.get("status") == "accepted"
            and acceptance.get("acceptance_id") == appraisal.acceptance_id
            and acceptance.get("proposal_id") == appraisal.proposal_id
            and acceptance.get("evaluated_world_revision") == appraisal.evaluated_world_revision
            and acceptance.get("accepted_change_id") == appraisal.change_id
            and acceptance.get("accepted_change_hash") == appraisal.accepted_change_hash
        ]
        if appraisal.evaluated_world_revision != expected_world_revision or len(matching) != 1:
            raise ValueError("AppraisalAccepted requires one revision-pinned AcceptanceRecorded")
        if isinstance(appraisal, AppraisalAcceptedPayload):
            outcome_ref = f"appraisal:{appraisal.appraisal.appraisal_id}"
        elif isinstance(appraisal, AppraisalSupersededPayload):
            outcome_ref = f"appraisal:{appraisal.successor.appraisal_id}"
        else:
            outcome_ref = f"appraisal:{appraisal.appraisal_id}:contradicted"
        completions = [
            item.payload()
            for completion_index, item in enumerate(events)
            if item.event_type == "TriggerProcessCompleted"
            and completion_index > mutation_index
            and item.payload().get("trigger_id") == appraisal.trigger_id
            and item.payload().get("runtime_outcome_ref") == outcome_ref
        ]
        if len(completions) != 1:
            raise ValueError("AppraisalAccepted must complete its trigger in the same commit")
    for acceptance_index, acceptance in acceptances:
        if acceptance.get("manifest_version") in {
            MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION,
            ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
            *MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS,
            MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION,
        }:
            # Dedicated source-bound lane is validated above; it is not a
            # member of the generic typed Thread mutation family.
            continue
        if acceptance.get("status") != "accepted" or not isinstance(
            acceptance.get("proposal_id"), str
        ):
            continue
        matching_domain_mutations = [
            mutation_index
            for mutation_index, binding in typed_mutations
            if mutation_index > acceptance_index
            and binding.proposal_id == acceptance.get("proposal_id")
            and binding.acceptance_id == acceptance.get("acceptance_id")
            and binding.evaluated_world_revision == acceptance.get("evaluated_world_revision")
            and binding.change_id == acceptance.get("accepted_change_id")
            and binding.accepted_change_hash == acceptance.get("accepted_change_hash")
        ]
        if matching_domain_mutations != [acceptance_index + 1]:
            raise ValueError(
                "accepted decision requires its one domain mutation immediately after it"
            )
    settlement_trigger_refs = [
        item.appraisal_trigger_ref
        for item in settlements
        if item.appraisal_trigger_ref is not None
    ]
    if len(set(settlement_trigger_refs)) != len(settlement_trigger_refs):
        raise ValueError("settlements in one commit require unique appraisal triggers")
    for event in events:
        if event.event_type == "ExperienceCommitted":
            experiences.append(ExperienceCommittedPayload.model_validate_json(event.payload_json))
        if event.event_type != "TriggerProcessOpened":
            continue
        process = event.payload().get("process")
        if not isinstance(process, dict):
            continue
        if process.get("process_kind") == "npc_world_appraisal" and process.get("state") == "open":
            trigger_ref = process.get("trigger_ref")
            if isinstance(trigger_ref, str):
                appraisal_triggers.setdefault(trigger_ref, []).append(
                    (
                        str(process.get("trigger_id")),
                        trigger_ref,
                        process.get("source_evidence_ref"),
                    )
                )

    for settlement_index, settlement_event, settlement in settlement_events:
        matching_acceptances = [
            acceptance
            for acceptance_index, acceptance in acceptances
            if acceptance_index < settlement_index
            and acceptance.get("status") == "accepted"
            and acceptance.get("acceptance_id") == settlement.acceptance_id
            and acceptance.get("proposal_id") == settlement.outcome_proposal_id
            and acceptance.get("evaluated_world_revision") == settlement.evaluated_world_revision
            and acceptance.get("accepted_change_id") == settlement.change_id
            and acceptance.get("accepted_change_hash") == settlement.accepted_change_hash
        ]
        if (
            settlement.evaluated_world_revision != expected_world_revision
            or len(matching_acceptances) != 1
        ):
            raise ValueError(
                "WorldOccurrenceSettled requires one revision-pinned accepted "
                "AcceptanceRecorded event in the same commit"
            )
        expected_trigger_id = appraisal_trigger_identity(
            settlement.occurrence_id, settlement.result_id
        )
        if settlement.appraisal_trigger_ref is None:
            if any(
                source_ref == settlement_event.event_id
                for bindings in appraisal_triggers.values()
                for _trigger_id, _trigger_ref, source_ref in bindings
            ):
                raise ValueError(
                    "WorldOccurrenceSettled without appraisal authority cannot open "
                    "a protagonist appraisal trigger"
                )
        else:
            if settlement.appraisal_trigger_ref != expected_trigger_id:
                raise ValueError("settlement appraisal trigger identity is not deterministic")
            if appraisal_triggers.get(settlement.appraisal_trigger_ref) != [
                (expected_trigger_id, expected_trigger_id, settlement_event.event_id)
            ]:
                raise ValueError(
                    "WorldOccurrenceSettled requires exactly one matching "
                    "npc_world_appraisal trigger in the same commit"
                )
        matching_experiences = [
            item
            for item in experiences
            if any(
                isinstance(binding, ExperienceOccurrenceSettlementBinding)
                and binding.occurrence_id == settlement.occurrence_id
                and binding.result_id == settlement.result_id
                for binding in item.experience.values.source_bindings
            )
        ]
        if len(matching_experiences) > 1:
            raise ValueError(
                "WorldOccurrenceSettled permits at most one matching committed experience"
            )

    # A source-bound Experience may be materialized after the settlement
    # commit.  Its reducer independently resolves the exact settlement event,
    # world revision, payload hash, occurrence revision and result bytes.  The
    # older same-batch-only restriction made crash recovery impossible: a
    # settlement committed just before process death could never gain its
    # durable Experience on restart.

    for mutation_index, binding in typed_mutations:
        matching = [
            acceptance
            for acceptance_index, acceptance in acceptances
            if acceptance_index == mutation_index - 1
            and acceptance.get("status") == "accepted"
            and acceptance.get("acceptance_id") == binding.acceptance_id
            and acceptance.get("proposal_id") == binding.proposal_id
            and acceptance.get("evaluated_world_revision") == binding.evaluated_world_revision
            and acceptance.get("accepted_change_id") == binding.change_id
            and acceptance.get("accepted_change_hash") == binding.accepted_change_hash
        ]
        if binding.evaluated_world_revision != expected_world_revision or len(matching) != 1:
            raise ValueError(
                "typed proposal mutation requires one adjacent revision-pinned AcceptanceRecorded"
            )


def _validate_expression_receipt_lifecycle_batch(events: Sequence[WorldEvent]) -> None:
    """Receipt-derived expression heads are one atomic, deterministic suffix."""

    for index, event in enumerate(events):
        if event.event_type != "ExpressionBeatSettled":
            continue
        if index == 0 or events[index - 1].event_type != "ExecutionReceiptRecorded":
            raise ValueError("expression_lifecycle.beat_requires_adjacent_receipt")
        beat = ExpressionBeatSettledPayload.model_validate_json(event.payload_json)
        receipt_event = events[index - 1]
        receipt = receipt_event.payload().get("receipt")
        if not isinstance(receipt, dict) or (
            beat.receipt_event_ref != receipt_event.event_id
            or beat.receipt_event_payload_hash != receipt_event.payload_hash
            or beat.receipt_id != receipt.get("receipt_id")
            or beat.action_id != receipt.get("action_id")
            or beat.terminal_action_state != receipt.get("observed_state")
            or receipt.get("is_terminal") is not True
        ):
            raise ValueError("expression_lifecycle.beat_receipt_binding_invalid")
    for index, event in enumerate(events):
        if event.event_type != "ExpressionPlanCompleted":
            continue
        if index == 0 or events[index - 1].event_type != "ExpressionBeatSettled":
            raise ValueError("expression_lifecycle.plan_requires_adjacent_settled_beat")
        plan = ExpressionPlanCompletedPayload.model_validate_json(event.payload_json)
        beat = ExpressionBeatSettledPayload.model_validate_json(events[index - 1].payload_json)
        if (
            plan.acceptance_id != beat.acceptance_id
            or plan.proposal_id != beat.proposal_id
            or plan.plan_id != beat.plan_id
            or plan.terminal_beat_id != beat.beat_id
            or plan.receipt_id != beat.receipt_id
            or plan.receipt_event_ref != beat.receipt_event_ref
            or plan.receipt_event_payload_hash != beat.receipt_event_payload_hash
            or plan.terminal_action_state != beat.terminal_action_state
        ):
            raise ValueError("expression_lifecycle.plan_beat_binding_invalid")
    by_id = {event.event_id: event for event in events}
    for event in events:
        if event.event_type != "ExpressionBeatTerminated":
            continue
        terminated_beat = ExpressionBeatTerminatedPayload.model_validate_json(event.payload_json)
        source = by_id.get(terminated_beat.source_event_ref)
        if (
            source is None
            or source.event_type != "ActionCancelled"
            or source.payload_hash != terminated_beat.source_event_payload_hash
            or source.payload().get("action_id") != terminated_beat.action_id
        ):
            raise ValueError("expression_lifecycle.beat_termination_source_binding_invalid")
    for event in events:
        if event.event_type != "ExpressionPlanTerminated":
            continue
        terminated = ExpressionPlanTerminatedPayload.model_validate_json(event.payload_json)
        source = by_id.get(terminated.source_event_ref)
        if source is None or source.payload_hash != terminated.source_event_payload_hash:
            raise ValueError("expression_lifecycle.termination_source_binding_invalid")
        if terminated.receipt_id is not None:
            if source.event_type != "ExecutionReceiptRecorded":
                raise ValueError("expression_lifecycle.termination_receipt_source_invalid")
            receipt = source.payload().get("receipt")
            if not isinstance(receipt, dict) or (
                receipt.get("receipt_id") != terminated.receipt_id
                or receipt.get("observed_state") != terminated.disposition
                or receipt.get("is_terminal") is not True
            ):
                raise ValueError("expression_lifecycle.termination_receipt_binding_invalid")
        elif source.event_type != "ActionCancelled":
            raise ValueError("expression_lifecycle.termination_cancellation_source_invalid")


def _validate_deliberation_audit_transaction(events: Sequence[WorldEvent]) -> None:
    """Keep Phase-4A provider lineage and its optional Proposal indivisible."""

    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        # This lane has its own closed validator because accepted batches must
        # begin with AcceptanceRecorded while the ordinary proposal audit lane
        # intentionally begins with ModelResultRecorded.
        return

    context_v2_character_outcomes = [
        event
        for event in events
        if event.event_type == "OutcomeProposalRecorded"
        and event.payload().get("decision_authority") == "character_model"
        and event.payload().get("context_identity_version") == "life-aftermath-context.2"
    ]
    model_indexes = [
        index for index, event in enumerate(events) if event.event_type == "ModelResultRecorded"
    ]
    v2_proposal_indexes = [
        index
        for index, event in enumerate(events)
        if event.event_type == "ProposalRecorded"
        and event.payload().get("audit_contract") == "proposal-envelope-audit.1"
    ]
    if not model_indexes:
        if context_v2_character_outcomes:
            raise ValueError(
                "Context v2 character outcome requires its complete pinned "
                "model-to-settlement transaction"
            )
        if v2_proposal_indexes:
            raise ValueError("ProposalRecorded v2 requires its complete model audit transaction")
        return
    if model_indexes[0] != 0:
        raise ValueError("model audit transaction must start the commit")
    if len(context_v2_character_outcomes) > 1:
        raise ValueError("model audit transaction cannot contain multiple character outcomes")

    first = ModelResultRecordedPayload.model_validate_json(events[0].payload_json)
    if model_indexes != list(range(len(model_indexes))):
        raise ValueError("model attempts must be complete and contiguous in one commit")
    if first.attempt_count > len(model_indexes):
        raise ValueError("model attempt lineage is incomplete")
    expected_model_indexes = list(range(first.attempt_count))
    attempts = [
        ModelResultRecordedPayload.model_validate_json(events[index].payload_json)
        for index in expected_model_indexes
    ]
    for index, attempt in enumerate(attempts):
        if (
            attempt.attempt_index != index
            or attempt.attempt_count != first.attempt_count
            or attempt.deliberation_result_id != first.deliberation_result_id
            or attempt.attempt_id != first.attempt_id
            or attempt.capsule_id != first.capsule_id
            or attempt.trigger_ref != first.trigger_ref
            or attempt.evaluated_world_revision != first.evaluated_world_revision
            or attempt.proposal_hash != first.proposal_hash
        ):
            raise ValueError("model attempts have mixed or out-of-order lineage")

    nested_provider_records = [
        ModelResultRecordedPayload.model_validate_json(events[index].payload_json)
        for index in model_indexes[first.attempt_count :]
    ]
    all_call_ids = {attempt.model_call_id for attempt in attempts}
    authored_call_ids = set(all_call_ids)
    for nested in nested_provider_records:
        recorded = RecordedModelResultAudit.model_validate_json(nested.audit_json)
        if (
            nested.attempt_index != 0
            or nested.attempt_count != 1
            or nested.proposal_hash is not None
            or nested.capsule_id != first.capsule_id
            or nested.trigger_ref != first.trigger_ref
            or nested.evaluated_world_revision != first.evaluated_world_revision
            or nested.model_call_id in all_call_ids
        ):
            raise ValueError("nested provider record has mixed or invalid lineage")
        if recorded.route.router_version == "authored-candidate-audit.1":
            if (
                recorded.parent_model_call_id is not None
                or recorded.response_hash is None
                or (
                    (recorded.status, recorded.outcome)
                    not in {
                        ("main_invalid", "invalid"),
                        ("candidate_returned", "returned"),
                    }
                )
            ):
                raise ValueError("authored candidate has invalid lineage")
            authored_call_ids.add(nested.model_call_id)
        elif recorded.route.router_version == "provider-subcall-audit.1":
            if (
                recorded.parent_model_call_id not in authored_call_ids
                or recorded.parent_model_call_id == recorded.model_call_id
            ):
                raise ValueError("provider subcall has no persisted author parent")
        elif recorded.route.router_version == "physical-provider-audit.1":
            semantic_children = tuple(
                RecordedModelResultAudit.model_validate_json(candidate.audit_json)
                for candidate in attempts
                if candidate.parent_model_call_id == recorded.model_call_id
            )
            if (
                recorded.parent_model_call_id is not None
                or not semantic_children
                or any(
                    child.model_call_id not in recorded.semantic_model_call_ids
                    or child.request_hash != recorded.request_hash
                    for child in semantic_children
                )
            ):
                raise ValueError("physical provider terminal has invalid stream lineage")
        else:
            raise ValueError("nested provider record uses an unknown audit contract")
        all_call_ids.add(nested.model_call_id)

    if first.proposal_hash is None:
        if len(events) != len(model_indexes) or v2_proposal_indexes:
            raise ValueError("failed recovery audit transaction cannot contain a Proposal")
        return

    proposal_index = len(model_indexes)
    if len(events) not in {proposal_index + 1, proposal_index + 5} or v2_proposal_indexes != [
        proposal_index
    ]:
        raise ValueError("validated model audit transaction requires one adjacent Proposal")
    proposal = ProposalRecordedV2Payload.model_validate_json(events[proposal_index].payload_json)
    final = attempts[-1]
    if (
        proposal.model_result_ref != final.model_result_ref
        or proposal.model_call_id != final.model_call_id
        or proposal.deliberation_result_id != final.deliberation_result_id
        or proposal.attempt_id != final.attempt_id
        or proposal.capsule_id != final.capsule_id
        or proposal.trigger_ref != final.trigger_ref
        or proposal.evaluated_world_revision != final.evaluated_world_revision
        or proposal.proposal_hash != final.proposal_hash
    ):
        raise ValueError("ProposalRecorded v2 does not bind the final model attempt")
    if len(events) == proposal_index + 5:
        outcome_event, acceptance_event, settlement_event, trigger_event = events[
            proposal_index + 1 :
        ]
        if tuple(
            item.event_type
            for item in (
                outcome_event,
                acceptance_event,
                settlement_event,
                trigger_event,
            )
        ) != (
            "OutcomeProposalRecorded",
            "AcceptanceRecorded",
            "WorldOccurrenceSettled",
            "TriggerProcessOpened",
        ):
            raise ValueError("atomic character outcome transaction has invalid domain ordering")
        outcome = OutcomeProposalRecordedPayload.model_validate_json(outcome_event.payload_json)
        settlement = WorldOccurrenceSettledPayload.model_validate_json(
            settlement_event.payload_json
        )
        acceptance = acceptance_event.payload()
        if (
            outcome.context_identity_version
            not in {
                "life-aftermath-context.2",
                "life-aftermath-context.3",
                "life-aftermath-context.4",
            }
            or outcome.decision_authority != "character_model"
            or outcome.decision_model_result_ref != final.model_result_ref
            or outcome.decision_model_result_event_ref != events[first.attempt_count - 1].event_id
            or outcome.decision_audit_proposal_event_ref != events[proposal_index].event_id
            or outcome.decision_audit_proposal_event_payload_hash
            != events[proposal_index].payload_hash
            or outcome.context_capsule_id != final.capsule_id
            or outcome.context_cursor is None
            or outcome.context_cursor.world_revision != final.evaluated_world_revision
            or outcome.evaluated_world_revision != final.evaluated_world_revision
            or outcome_event.causation_id != events[proposal_index].event_id
            or acceptance_event.causation_id != outcome_event.event_id
            or acceptance.get("proposal_id") != outcome.outcome_proposal_id
            or acceptance.get("evaluated_world_revision") != outcome.evaluated_world_revision
            or settlement_event.causation_id != acceptance_event.event_id
            or settlement.outcome_proposal_id != outcome.outcome_proposal_id
            or settlement.accepted_change_hash != outcome.proposed_change_hash
            or settlement.adopt_proposed_life_direction != outcome.adopt_proposed_life_direction
            or settlement.character_life_direction != outcome.character_life_direction
            or trigger_event.causation_id != settlement_event.event_id
        ):
            raise ValueError("atomic character outcome transaction is not fully pinned")


_LIFE_DEVELOPMENT_PRIVACY_RANK = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def _possibility_carries_objective_transition(
    possibility: dict[str, object],
) -> bool:
    outcomes = possibility.get("outcomes")
    return isinstance(outcomes, list) and any(
        isinstance(outcome, dict)
        and (
            outcome.get("objective_biographical_transition") is not None
            or (
                isinstance(descriptor := outcome.get("descriptor"), dict)
                and descriptor.get("objective_biographical_transition") is not None
            )
        )
        for outcome in outcomes
    )


def _validate_life_development_location_authority_batch(
    events: Sequence[WorldEvent],
) -> None:
    """Close every new life-development location effect over frozen authority."""

    for proposal_event in events:
        if proposal_event.event_type != "ProposalRecorded":
            continue
        proposal = proposal_event.payload()
        if proposal.get("proposal_kind") != "life_development":
            continue
        possibility = proposal.get("possibility_authority")
        if possibility is None:
            continue
        if not isinstance(possibility, dict):
            raise ValueError("life-development possibility authority must be an object")
        possibility_version = proposal.get("possibility_authority_version")
        if possibility_version not in {
            "life-development-possibility.2",
            "life-development-possibility.3",
            "life-development-possibility.4",
            "life-development-possibility.5",
            "life-development-possibility.6",
            "life-development-possibility.7",
        }:
            raise ValueError("life-development possibility authority version is unknown")
        if (
            possibility_version != "life-development-possibility.7"
            and _possibility_carries_objective_transition(possibility)
        ):
            raise ValueError("objective transition requires possibility authority version .7")
        if possibility_version in {
            "life-development-possibility.3",
            "life-development-possibility.4",
            "life-development-possibility.5",
            "life-development-possibility.6",
            "life-development-possibility.7",
        }:
            expected_possibility_hash = hashlib.sha256(
                json.dumps(
                    possibility,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if proposal.get("possibility_authority_hash") != expected_possibility_hash:
                raise ValueError("life-development possibility subject authority hash is invalid")
            authored_subject_ref = possibility.get("authored_subject_ref")
            outcomes = possibility.get("outcomes")
            if (
                not isinstance(authored_subject_ref, str)
                or not isinstance(outcomes, list)
                or not outcomes
                or any(
                    not isinstance(outcome, dict)
                    or outcome.get("experienced_by_ref") != authored_subject_ref
                    for outcome in outcomes
                )
            ):
                raise ValueError(
                    "life-development outcomes do not close over their authored subject"
                )
            deliberation = proposal.get("world_author_deliberation")
            manifest_value = (
                deliberation.get("capability_manifest") if isinstance(deliberation, dict) else None
            )
            if not isinstance(manifest_value, dict):
                raise ValueError("life-development subject has no pinned capability manifest")
            try:
                subject_manifest = LifeDevelopmentCapabilityManifest.model_validate_json(
                    json.dumps(
                        manifest_value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            except ValueError as exc:
                raise ValueError("life-development subject capability manifest is invalid") from exc
            if (
                subject_manifest.owner_actor_ref != authored_subject_ref
                or proposal.get("capability_manifest_version") != subject_manifest.version
                or proposal.get("capability_manifest_hash") != subject_manifest.manifest_hash
            ):
                raise ValueError("life-development authored subject exceeds its pinned authority")
            possibility_entity_refs = possibility.get("entity_refs")
            if (
                not isinstance(possibility_entity_refs, list)
                or any(not isinstance(ref, str) for ref in possibility_entity_refs)
                or not set(possibility_entity_refs) <= set(subject_manifest.entity_refs)
            ):
                raise ValueError(
                    "life-development possibility entities exceed pinned manifest authority"
                )
            _validate_life_development_subject_effect(
                events=events,
                proposal_event=proposal_event,
                proposal=proposal,
                possibility=possibility,
                authored_subject_ref=authored_subject_ref,
                possibility_version=str(possibility_version),
            )
            if possibility_version in {
                "life-development-possibility.4",
                "life-development-possibility.5",
                "life-development-possibility.6",
                "life-development-possibility.7",
            }:
                review = proposal.get("world_author_source_closure_review")
                review_deliberation = proposal.get("world_author_source_closure_deliberation")
                if not isinstance(review, dict) or not isinstance(
                    review_deliberation,
                    dict,
                ):
                    raise ValueError(
                        "life-development v4 possibility lacks source-closure authority"
                    )
                if (
                    review.get("decision") != "supported"
                    or review.get("unsupported_claim_ids") != []
                    or review.get("undeclared_fact_fragments") != []
                    or review.get("undeclared_fact_paths", []) != []
                    or review.get("typed_location_conflicts") != []
                ):
                    raise ValueError(
                        "life-development v4 possibility has unsupported source closure"
                    )
                expected_review_hash = hashlib.sha256(
                    json.dumps(
                        review,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                expected_review_deliberation_hash = hashlib.sha256(
                    json.dumps(
                        review_deliberation,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if (
                    proposal.get("world_author_source_closure_review_hash") != expected_review_hash
                    or proposal.get("world_author_source_closure_deliberation_hash")
                    != expected_review_deliberation_hash
                    or review_deliberation.get("role") != "world_author_source_reviewer"
                    or review_deliberation.get("capsule_id") != deliberation.get("capsule_id")
                    or review_deliberation.get("context_cursor")
                    != deliberation.get("context_cursor")
                    or review_deliberation.get("capability_manifest")
                    != deliberation.get("capability_manifest")
                    or not isinstance(
                        proposal.get("world_author_source_closure_model"),
                        str,
                    )
                ):
                    raise ValueError("life-development source-closure authority binding is invalid")
                raw_output_hash = proposal.get("world_author_raw_output_hash")
                manifest_hash = proposal.get("capability_manifest_hash")
                if not isinstance(raw_output_hash, str) or not isinstance(
                    manifest_hash,
                    str,
                ):
                    raise ValueError("life-development review subject hashes are missing")
                request_hashes = review_deliberation.get("request_hashes")
                review_cursor = review_deliberation.get("context_cursor")
                trigger_id = proposal.get("trigger_id")
                if possibility_version in {
                    "life-development-possibility.6",
                    "life-development-possibility.7",
                }:
                    if not (
                        isinstance(request_hashes, list)
                        and request_hashes
                        and all(isinstance(item, str) for item in request_hashes)
                        and isinstance(review_cursor, dict)
                        and isinstance(trigger_id, str)
                    ):
                        raise ValueError(
                            "life-development current source-review identity is incomplete"
                        )
                    expected_source_subject = current_source_review_subject_hash(
                        review_request_hashes=tuple(request_hashes),
                        world_author_raw_output_hash=raw_output_hash,
                        capability_manifest_hash=manifest_hash,
                        context_cursor=review_cursor,
                        wake_event_ref=trigger_id,
                        wake_world_id=proposal_event.world_id,
                        wake_logical_time=proposal_event.logical_time.isoformat(),
                    )
                else:
                    expected_source_subject = legacy_source_review_subject_hash(
                        world_author_raw_output_hash=raw_output_hash,
                        capability_manifest_hash=manifest_hash,
                    )
                if review_deliberation.get("decision_subject_hash") != (expected_source_subject):
                    raise ValueError("life-development source-closure reviewed another subject")
            if possibility_version in {
                "life-development-possibility.5",
                "life-development-possibility.6",
                "life-development-possibility.7",
            }:
                novel_review = proposal.get("world_author_novel_origin_review")
                novel_deliberation = proposal.get("world_author_novel_origin_deliberation")
                if not isinstance(novel_review, dict) or not isinstance(
                    novel_deliberation,
                    dict,
                ):
                    raise ValueError("life-development v5 possibility lacks novel-origin authority")
                if (
                    novel_review.get("decision") != "supported"
                    or novel_review.get("unsupported_claims") != []
                    or novel_review.get("unsupported_provisional_npcs") != []
                    or novel_review.get("unsupported_provisional_places") != []
                    or novel_review.get("unsupported_objective_transitions") != []
                    or novel_review.get(
                        "unsupported_outcome_prerequisites",
                        [],
                    )
                    != []
                    or novel_review.get("undeclared_premise_fragments") != []
                ):
                    raise ValueError("life-development v5 possibility has unsupported novel origin")
                expected_novel_hash = hashlib.sha256(
                    json.dumps(
                        novel_review,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                expected_novel_deliberation_hash = hashlib.sha256(
                    json.dumps(
                        novel_deliberation,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                if (
                    proposal.get("world_author_novel_origin_review_hash") != expected_novel_hash
                    or proposal.get("world_author_novel_origin_deliberation_hash")
                    != expected_novel_deliberation_hash
                    or novel_deliberation.get("role") != "world_author_novel_origin_critic"
                    or novel_deliberation.get("capsule_id") != deliberation.get("capsule_id")
                    or novel_deliberation.get("context_cursor")
                    != deliberation.get("context_cursor")
                    or novel_deliberation.get("capability_manifest")
                    != deliberation.get("capability_manifest")
                    or not isinstance(
                        proposal.get("world_author_novel_origin_model"),
                        str,
                    )
                ):
                    raise ValueError("life-development novel-origin authority binding is invalid")
                novel_request_hashes = novel_deliberation.get("request_hashes")
                novel_cursor = novel_deliberation.get("context_cursor")
                if possibility_version in {
                    "life-development-possibility.6",
                    "life-development-possibility.7",
                }:
                    if not (
                        isinstance(novel_request_hashes, list)
                        and novel_request_hashes
                        and all(isinstance(item, str) for item in novel_request_hashes)
                        and isinstance(novel_cursor, dict)
                        and isinstance(trigger_id, str)
                    ):
                        raise ValueError(
                            "life-development current novel-origin identity is incomplete"
                        )
                    expected_novel_subjects = {
                        current_novel_origin_review_subject_hash(
                            review_request_hashes=tuple(novel_request_hashes),
                            world_author_raw_output_hash=raw_output_hash,
                            capability_manifest_hash=manifest_hash,
                            context_cursor=novel_cursor,
                            wake_event_ref=trigger_id,
                            wake_world_id=proposal_event.world_id,
                            wake_logical_time=(proposal_event.logical_time.isoformat()),
                        )
                    }
                else:
                    expected_novel_subjects = legacy_novel_origin_review_subject_hashes(
                        world_author_raw_output_hash=raw_output_hash,
                        capability_manifest_hash=manifest_hash,
                    )
                if novel_deliberation.get("decision_subject_hash") not in (expected_novel_subjects):
                    raise ValueError(
                        "life-development novel-origin critic reviewed another subject"
                    )
        location_ref = possibility.get("location_ref")
        capability_ref = possibility.get("location_capability_ref")
        capability_value = possibility.get("location_capability")
        if location_ref is None and capability_ref is None and capability_value is None:
            effect_locations = [
                ActivityPlannedPayload.model_validate_json(event.payload_json).plan.location_ref
                for event in events
                if event.event_type == "ActivityPlanned"
                and event.causation_id == proposal_event.event_id
            ]
            effect_locations.extend(
                WorldOccurrenceCommittedPayload.model_validate_json(
                    event.payload_json
                ).occurrence.location_ref
                for event in events
                if event.event_type == "WorldOccurrenceCommitted"
                and event.causation_id == proposal_event.event_id
            )
            if any(value is not None for value in effect_locations):
                raise ValueError("life-development bare location effect has no frozen capability")
            continue
        if (
            possibility_version
            not in {
                "life-development-possibility.2",
                "life-development-possibility.3",
                "life-development-possibility.4",
                "life-development-possibility.5",
                "life-development-possibility.6",
                "life-development-possibility.7",
            }
            or not isinstance(location_ref, str)
            or not isinstance(capability_ref, str)
            or not isinstance(capability_value, dict)
        ):
            raise ValueError("life-development location requires one frozen capability")
        expected_possibility_hash = hashlib.sha256(
            json.dumps(
                possibility,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if proposal.get("possibility_authority_hash") != expected_possibility_hash:
            raise ValueError("life-development location possibility authority hash is invalid")
        try:
            capability = LifeDevelopmentLocationCapability.model_validate_json(
                json.dumps(
                    capability_value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except ValueError as exc:
            raise ValueError("life-development location capability snapshot is invalid") from exc
        if (
            capability_value
            != capability.model_dump(
                mode="json",
                exclude={"capability_ref"},
            )
            or capability.location_ref != location_ref
            or capability.capability_ref != capability_ref
        ):
            raise ValueError("life-development location capability snapshot does not match its ref")
        deliberation = proposal.get("world_author_deliberation")
        manifest_value = (
            deliberation.get("capability_manifest") if isinstance(deliberation, dict) else None
        )
        if not isinstance(manifest_value, dict):
            raise ValueError("life-development location has no pinned capability manifest")
        try:
            manifest = LifeDevelopmentCapabilityManifest.model_validate_json(
                json.dumps(
                    manifest_value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        except ValueError as exc:
            raise ValueError("life-development pinned capability manifest is invalid") from exc
        if (
            proposal.get("capability_manifest_version") != manifest.version
            or proposal.get("capability_manifest_hash") != manifest.manifest_hash
        ):
            raise ValueError("life-development pinned capability manifest identity changed")
        if not any(
            item.capability_ref == capability_ref and item == capability
            for item in manifest.location_capabilities
        ):
            raise ValueError(
                "life-development location capability is absent from its pinned manifest"
            )
        timing_mode, offered_window = _life_development_offered_window(
            proposal_event=proposal_event,
            possibility=possibility,
        )
        proposal_privacy = possibility.get("privacy_class")
        if (
            not isinstance(proposal_privacy, str)
            or proposal_privacy not in _LIFE_DEVELOPMENT_PRIVACY_RANK
            or _LIFE_DEVELOPMENT_PRIVACY_RANK[proposal_privacy]
            < _LIFE_DEVELOPMENT_PRIVACY_RANK[capability.privacy_class]
        ):
            raise ValueError("life-development location capability privacy is weakened")
        if not capability.authorizes(
            timing_mode=timing_mode,
            window=offered_window,
        ):
            raise ValueError(
                "life-development location capability does not authorize the proposed window"
            )

        effect_kind = proposal.get("effect_kind")
        effect_ref = proposal.get("effect_ref")
        if effect_kind is None and effect_ref is None:
            continue
        if effect_kind == "character_plan":
            matching = [
                event
                for event in events
                if event.event_type == "ActivityPlanned"
                and event.causation_id == proposal_event.event_id
            ]
            if len(matching) != 1:
                raise ValueError("life-development location Proposal requires one adjacent Plan")
            effect = ActivityPlannedPayload.model_validate_json(matching[0].payload_json)
            if effect.plan.plan_id != effect_ref or effect.plan.scheduled_window is None:
                raise ValueError("life-development location Proposal and Plan are inconsistent")
            effect_window = effect.plan.scheduled_window
            effect_location_ref = effect.plan.location_ref
            effect_privacy = effect.plan.privacy_class
            if effect.plan.evidence_refs != effect.evidence_refs:
                raise ValueError("life-development location Plan evidence is not closed")
            if (
                effect_window.opens_at < offered_window.opens_at
                or effect_window.closes_at > offered_window.closes_at
            ):
                raise ValueError("life-development location Plan exceeds its offered window")
        elif effect_kind == "world_occurrence":
            matching = [
                event
                for event in events
                if event.event_type == "WorldOccurrenceCommitted"
                and event.causation_id == proposal_event.event_id
            ]
            if len(matching) != 1:
                raise ValueError(
                    "life-development location Proposal requires one adjacent occurrence"
                )
            effect = WorldOccurrenceCommittedPayload.model_validate_json(matching[0].payload_json)
            if effect.occurrence.occurrence_id != effect_ref:
                raise ValueError(
                    "life-development location Proposal and occurrence are inconsistent"
                )
            effect_window = effect.occurrence.time_window
            effect_location_ref = effect.occurrence.location_ref
            effect_privacy = effect.occurrence.visibility
            if effect_window != offered_window:
                raise ValueError("life-development location occurrence changed its proposed window")
        else:
            raise ValueError("life-development location effect kind is invalid")

        if effect_location_ref != location_ref:
            raise ValueError("life-development location effect changed its authorized place")
        if (
            _LIFE_DEVELOPMENT_PRIVACY_RANK[effect_privacy]
            < _LIFE_DEVELOPMENT_PRIVACY_RANK[capability.privacy_class]
            or _LIFE_DEVELOPMENT_PRIVACY_RANK[effect_privacy]
            < _LIFE_DEVELOPMENT_PRIVACY_RANK[proposal_privacy]
        ):
            raise ValueError("life-development location effect weakened its authorized privacy")
        if not capability.authorizes(
            timing_mode=timing_mode,
            window=effect_window,
        ):
            raise ValueError("life-development location effect exceeds its capability window")
        carried_authority_refs = {
            *(item.ref_id for item in effect.evidence_refs),
            *effect.policy_refs,
        }
        if not set(capability.authority_refs) <= carried_authority_refs:
            raise ValueError(
                "life-development location capability authority is absent "
                "from effect evidence or policy refs"
            )


def _validate_life_development_subject_effect(
    *,
    events: Sequence[WorldEvent],
    proposal_event: WorldEvent,
    proposal: dict[str, object],
    possibility: dict[str, object],
    authored_subject_ref: str,
    possibility_version: str,
) -> None:
    """Close `.3` subject authority through the adjacent executable effect."""

    effect_kind = proposal.get("effect_kind")
    effect_ref = proposal.get("effect_ref")
    if effect_kind is None and effect_ref is None:
        return
    if not isinstance(effect_ref, str):
        raise ValueError("life-development subject effect identity is incomplete")
    if effect_kind == "character_plan":
        matching = [
            event
            for event in events
            if event.event_type == "ActivityPlanned"
            and event.causation_id == proposal_event.event_id
        ]
        if len(matching) != 1:
            raise ValueError("life-development subject authority requires one adjacent Plan")
        effect = ActivityPlannedPayload.model_validate_json(matching[0].payload_json)
        if effect.plan.plan_id != effect_ref or effect.plan.owner_actor_ref != authored_subject_ref:
            raise ValueError("life-development Plan owner exceeds authored subject authority")
        character_choice = proposal.get("character_choice")
        chosen_participants = (
            character_choice.get("participant_refs") if isinstance(character_choice, dict) else None
        )
        entity_refs = possibility.get("entity_refs")
        if (
            not isinstance(chosen_participants, list)
            or any(not isinstance(ref, str) for ref in chosen_participants)
            or not isinstance(entity_refs, list)
            or any(not isinstance(ref, str) for ref in entity_refs)
            or not set(chosen_participants) <= set(entity_refs)
            or effect.plan.participant_refs != tuple(chosen_participants)
        ):
            raise ValueError("life-development Plan participants exceed character choice authority")
        aspiration_source_ref = (
            character_choice.get("crystallized_aspiration_source_ref")
            if isinstance(character_choice, dict)
            else None
        )
        deliberation = proposal.get("world_author_deliberation")
        capability_manifest = (
            deliberation.get("capability_manifest") if isinstance(deliberation, dict) else None
        )
        active_aspiration_refs = (
            capability_manifest.get("active_aspiration_source_refs", [])
            if isinstance(capability_manifest, dict)
            else []
        )
        if aspiration_source_ref is not None and (
            not isinstance(aspiration_source_ref, str)
            or not isinstance(active_aspiration_refs, list)
            or aspiration_source_ref not in active_aspiration_refs
        ):
            raise ValueError("life-development aspiration choice exceeds pinned authority")
        aspiration_events = [
            event
            for event in events
            if event.event_type == "AspirationCrystallized"
            and event.causation_id == proposal_event.event_id
        ]
        if aspiration_source_ref is None:
            if aspiration_events:
                raise ValueError("life-development Plan has an undeclared aspiration effect")
        elif len(aspiration_events) != 1:
            raise ValueError("life-development aspiration choice requires one adjacent effect")
        else:
            aspiration_effect = AspirationCrystallizedPayload.model_validate_json(
                aspiration_events[0].payload_json
            )
            if (
                aspiration_effect.plan_ref != "plan:" + effect.plan.plan_id
                or aspiration_source_ref
                not in {item.ref_id for item in aspiration_effect.evidence_refs}
            ):
                raise ValueError(
                    "life-development aspiration effect changed its chosen source or Plan"
                )
        return
    if effect_kind == "world_occurrence":
        matching = [
            event
            for event in events
            if event.event_type == "WorldOccurrenceCommitted"
            and event.causation_id == proposal_event.event_id
        ]
        if len(matching) != 1:
            raise ValueError("life-development subject authority requires one adjacent occurrence")
        effect = WorldOccurrenceCommittedPayload.model_validate_json(matching[0].payload_json)
        entity_refs = possibility.get("entity_refs")
        if not isinstance(entity_refs, list) or any(
            not isinstance(ref, str) for ref in entity_refs
        ):
            raise ValueError("life-development possibility entity refs are invalid")
        expected_participants = (authored_subject_ref, *entity_refs)
        if (
            effect.occurrence.occurrence_id != effect_ref
            or effect.occurrence.participant_refs != expected_participants
        ):
            raise ValueError(
                "life-development occurrence participants exceed authored subject authority"
            )
        candidates = effect.occurrence.candidate_outcomes
        if possibility_version != "life-development-possibility.7" and any(
            candidate.objective_biographical_transition is not None for candidate in candidates
        ):
            raise ValueError("objective transition requires possibility authority version .7")
        if possibility_version == "life-development-possibility.7":
            outcomes = possibility.get("outcomes")
            if not isinstance(outcomes, list) or len(outcomes) != len(candidates):
                raise ValueError("life-development objective transition matrix changed shape")
            for outcome, candidate in zip(outcomes, candidates, strict=True):
                if not isinstance(outcome, dict):
                    raise ValueError("life-development objective transition outcome is invalid")
                descriptor_value = outcome.get("descriptor")
                expected = OutcomeCandidateDescriptor.model_validate_json(
                    json.dumps(
                        descriptor_value,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                if candidate != expected:
                    raise ValueError("life-development outcome descriptor changed after review")
        return
    raise ValueError("life-development subject effect kind is invalid")


def _life_development_offered_window(
    *,
    proposal_event: WorldEvent,
    possibility: dict[str, object],
) -> tuple[str, DueWindow]:
    timing = possibility.get("timing")
    if not isinstance(timing, dict):
        raise ValueError("life-development location timing is invalid")
    mode = timing.get("mode")
    if mode == "now":
        duration_minutes = timing.get("duration_minutes")
        if type(duration_minutes) is not int or duration_minutes <= 0:
            raise ValueError("life-development location now timing is invalid")
        return (
            "now",
            DueWindow(
                opens_at=proposal_event.logical_time,
                closes_at=proposal_event.logical_time + timedelta(minutes=duration_minutes),
            ),
        )
    if mode != "later":
        raise ValueError("life-development location timing mode is invalid")
    opens_at = timing.get("opens_at")
    closes_at = timing.get("closes_at")
    if not isinstance(opens_at, str) or not isinstance(closes_at, str):
        raise ValueError("life-development location later timing is invalid")
    try:
        window = DueWindow(
            opens_at=datetime.fromisoformat(opens_at),
            closes_at=datetime.fromisoformat(closes_at),
        )
    except ValueError as exc:
        raise ValueError("life-development location later timing is invalid") from exc
    return "later", window


def _validate_acceptance_manifest_v2_batch(events: Sequence[WorldEvent]) -> None:
    unknown = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and "manifest_version" in event.payload()
        and event.payload().get("manifest_version")
        not in {
            "acceptance-manifest.2",
            "acceptance-manifest.3",
            MINIMAL_REPLY_MANIFEST_VERSION,
            APPRAISAL_ACCEPTANCE_MANIFEST_VERSION,
            AFFECT_ACCEPTANCE_MANIFEST_VERSION,
            RELATIONSHIP_ACCEPTANCE_MANIFEST_VERSION,
            RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION,
            ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
            *MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS,
            MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION,
            OUTCOME_ACCEPTANCE_MANIFEST_VERSION,
            INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION,
            MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION,
            EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION,
            *SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSIONS,
            EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION,
        }
    ]
    if unknown:
        raise ValueError("acceptance_manifest.unsupported_manifest_version")
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == "acceptance-manifest.2"
    ]
    if not manifests:
        return
    if len(events) != 1 or len(manifests) != 1:
        raise ValueError("AcceptanceManifest v2 must be the only event in its commit")
    manifest = parse_acceptance_manifest_v2(manifests[0].payload())
    if manifest.status != "accepted" and manifest.authorized_effects:
        raise ValueError("non-accepted manifest cannot carry effects")


def reject_external_perception_manifest_without_recorder(
    events: Sequence[WorldEvent],
) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("external_perception_acceptance.recorder_capability_required")


def _validate_authorized_external_perception_manifest_batch(
    events: Sequence[WorldEvent],
    *,
    expected_world_revision: int,
    authorized: bool,
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == EXTERNAL_PERCEPTION_ACCEPTANCE_MANIFEST_VERSION
    ]
    external_effects = [
        event
        for event in events
        if event.event_type in {"ExternalSignalSnapshotAdopted", "ExternalPerceptionRecorded"}
    ]
    if not manifests:
        if external_effects:
            raise ValueError("external perception effects require their accepted manifest")
        return
    if not authorized:
        raise ValueError("external_perception_acceptance.recorder_capability_required")
    if len(manifests) != 1 or events[0] is not manifests[0]:
        raise ValueError("external perception accepted batch must start with one manifest")
    try:
        manifest = ExternalPerceptionAcceptanceManifest.model_validate_json(
            manifests[0].payload_json
        )
    except ValueError as exc:
        raise ValueError("external perception manifest payload is invalid") from exc
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or manifest.policy_digest != EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_DIGEST
    ):
        raise ValueError("external perception manifest is not pinned to installed authority")
    effects = tuple(events[1:])
    if len(effects) != len(manifest.effects) or tuple(
        (event.event_id, event.event_type, event.payload_hash) for event in effects
    ) != tuple(
        (effect.event_id, effect.event_type, effect.payload_hash) for effect in manifest.effects
    ):
        raise ValueError("external perception manifest effects changed")
    model_events = tuple(event for event in effects if event.event_type == "ModelResultRecorded")
    snapshots = tuple(
        event for event in effects if event.event_type == "ExternalSignalSnapshotAdopted"
    )
    perceptions = tuple(
        event for event in effects if event.event_type == "ExternalPerceptionRecorded"
    )
    if (
        len(model_events) != 1
        or len(snapshots) != len(perceptions)
        or tuple(event.event_type for event in effects)
        != (
            "ModelResultRecorded",
            *("ExternalSignalSnapshotAdopted" for _ in snapshots),
            *("ExternalPerceptionRecorded" for _ in perceptions),
        )
    ):
        raise ValueError("external perception accepted batch ordering is invalid")
    model = ModelResultRecordedPayload.model_validate_json(model_events[0].payload_json)
    if (
        model.attempt_id != manifest.attention_attempt_id
        or model.trigger_ref != manifest.attention_attempt_id
        or model.evaluated_world_revision != expected_world_revision
        or model.capsule_id != manifest.candidate_snapshot_hash
    ):
        raise ValueError("external perception model result changed pinned attention identity")
    snapshots_by_ref: dict[str, tuple[WorldEvent, ExternalSignalSnapshotAdoptedPayload]] = {}
    for event in snapshots:
        payload = ExternalSignalSnapshotAdoptedPayload.model_validate_json(event.payload_json)
        snapshot = payload.snapshot
        if (
            payload.acceptance_id != manifest.acceptance_id
            or payload.attention_attempt_id != manifest.attention_attempt_id
            or payload.model_result_event_ref != model_events[0].event_id
            or payload.model_result_event_payload_hash != model_events[0].payload_hash
            or not snapshot.may_expose_to_character_model
            or not snapshot.may_freeze_durable_snapshot
            or snapshot.snapshot_ref in snapshots_by_ref
        ):
            raise ValueError("external perception snapshot is not licensed and model-bound")
        snapshots_by_ref[snapshot.snapshot_ref] = (event, payload)
    for event in perceptions:
        payload = ExternalPerceptionRecordedPayload.model_validate_json(event.payload_json)
        snapshot_pair = snapshots_by_ref.get(payload.snapshot_ref)
        if snapshot_pair is None:
            raise ValueError("external perception lacks an exact adopted snapshot")
        snapshot_event, _ = snapshot_pair
        if (
            payload.acceptance_id != manifest.acceptance_id
            or payload.attention_attempt_id != manifest.attention_attempt_id
            or payload.window_id != manifest.window_id
            or payload.candidate_snapshot_hash != manifest.candidate_snapshot_hash
            or payload.pinned_cursor.world_revision != expected_world_revision
            or payload.pinned_cursor.deliberation_revision
            != manifest.evaluated_deliberation_revision
            or payload.pinned_cursor.ledger_sequence != manifest.evaluated_ledger_sequence
            or payload.snapshot_event_ref != snapshot_event.event_id
            or payload.snapshot_event_payload_hash != snapshot_event.payload_hash
            or payload.attention_model_result_ref != model.model_result_ref
            or payload.attention_model_event_ref != model_events[0].event_id
            or payload.attention_model_event_payload_hash != model_events[0].payload_hash
        ):
            raise ValueError("external perception changed accepted delivery lineage")


def _validate_authorized_fact_manifest_v3_batch(
    events: Sequence[WorldEvent],
    *,
    expected_world_revision: int,
    authorized: bool,
) -> None:
    """Bind the first accepted-v3 vertical to its one exact Fact event.

    ``AcceptanceManifestV3`` is a broad, inert compiler contract.  This ledger
    seam intentionally installs only its first production vertical: exactly one
    accepted manifest followed immediately by exactly one ``FactCommittedV2``.
    The opaque batch capability selects this code path; a complete CAS cursor
    or a syntactically valid manifest is not authorization by itself.
    """

    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == "acceptance-manifest.3"
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("accepted_manifest.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 2:
        raise ValueError("accepted_manifest.v3_fact_batch_must_be_exact")
    acceptance, fact_event = events
    if acceptance is not manifests[0] or fact_event.event_type != "FactCommittedV2":
        raise ValueError("accepted_manifest.v3_fact_batch_must_be_ordered")
    try:
        manifest = rehydrate_acceptance_manifest_v3(acceptance.payload())
        payload = rehydrate_fact_commit_materialized_v2_json(fact_event.payload_json)
    except Exception as exc:
        raise ValueError("accepted_manifest.v3_fact_batch_payload_is_invalid") from exc
    if (
        manifest.status != "accepted"
        or manifest.evaluated_world_revision != expected_world_revision
        or payload.evaluated_world_revision != expected_world_revision
        or payload.acceptance_id != manifest.acceptance_id
        or fact_event.causation_id != acceptance.event_id
    ):
        raise ValueError("accepted_manifest.v3_fact_batch_authority_is_not_pinned")
    if fact_commit_event_payload_hash(payload) != fact_event.payload_hash:
        raise ValueError("accepted_manifest.v3_fact_payload_hash_is_not_exact")
    if len(manifest.authorized_effects) != 1:
        raise ValueError("accepted_manifest.v3_fact_requires_one_effect")
    effect = manifest.authorized_effects[0]
    if (
        effect.ordinal != 0
        or effect.role != "domain_mutation"
        or effect.event_type != "FactCommittedV2"
        or effect.event_id != fact_event.event_id
        or effect.payload_hash != fact_event.payload_hash
        or len(effect.authority_refs) != 1
    ):
        raise ValueError("accepted_manifest.v3_fact_effect_does_not_match_event")
    authority = effect.authority_refs[0]
    if (
        authority.proposal_id != payload.proposal_id
        or authority.authority_kind != "change"
        or authority.authority_id != payload.change_id
        or authority.authority_hash != payload.full_change_authority_hash
    ):
        raise ValueError("accepted_manifest.v3_fact_effect_does_not_match_payload")
    proposals = tuple(
        proposal for proposal in manifest.proposals if proposal.proposal_id == payload.proposal_id
    )
    if len(proposals) != 1:
        raise ValueError("accepted_manifest.v3_fact_proposal_is_not_exact")
    proposal = proposals[0]
    matching_changes = tuple(
        change
        for change in proposal.changes
        if change.change_id == payload.change_id
        and change.full_change_authority_hash == payload.full_change_authority_hash
    )
    if (
        proposal.evaluated_world_revision != expected_world_revision
        or len(matching_changes) != 1
        or matching_changes[0].kind != "fact_transition"
        or matching_changes[0].transition != "commit"
    ):
        raise ValueError("accepted_manifest.v3_fact_change_authority_is_not_exact")


def reject_accepted_manifest_v3_without_recorder(events: Sequence[WorldEvent]) -> None:
    """Keep v3 accepted effects off every ordinary ledger write seam.

    This small, explicit gate is intentionally callable before event identity
    validation by both ledger adapters.  The future opaque accepted-batch
    capability will use a distinct invariant context; it must not weaken the
    default path merely because it needs to admit version 3.
    """
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == "acceptance-manifest.3"
        for event in events
    ):
        # A v3 manifest is valid only through the opaque accepted-batch
        # capability.  In particular, a complete cursor CAS is not itself an
        # authorization to record one: callers must not be able to forge an
        # accepted effect by using ``commit_at_cursor`` directly.
        raise ValueError("accepted_manifest.recorder_capability_required")


def _validate_authorized_minimal_reply_manifest_batch(
    events: Sequence[WorldEvent],
    *,
    expected_world_revision: int,
    authorized: bool,
) -> None:
    """Close the ordinary-reply effect path without borrowing Fact-v3 authority."""

    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == MINIMAL_REPLY_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("minimal_reply.recorder_capability_required")
    expected_types = (
        "AcceptanceRecorded",
        "MessagePayloadStored",
        "ExpressionPlanAccepted",
        "ExpressionBeatAuthorized",
        "BudgetReserved",
        "ActionAuthorized",
    )
    if len(manifests) != 1 or tuple(event.event_type for event in events) != expected_types:
        raise ValueError("minimal_reply.accepted_batch_must_be_exact")
    acceptance, message_event, plan_event, beat_event, reservation_event, action_event = events
    try:
        manifest = MinimalReplyManifest.model_validate_json(acceptance.payload_json)
        message = MessagePayloadStoredPayload.model_validate_json(message_event.payload_json)
        plan = ExpressionPlanAcceptedPayload.model_validate_json(plan_event.payload_json)
        beat = ExpressionBeatAuthorizedPayload.model_validate_json(beat_event.payload_json)
        reservation = BudgetReservation.model_validate_json(
            json.dumps(reservation_event.payload()["reservation"], ensure_ascii=False)
        )
        action = Action.model_validate_json(
            json.dumps(action_event.payload()["action"], ensure_ascii=False)
        )
    except Exception as exc:
        raise ValueError("minimal_reply.accepted_batch_payload_is_invalid") from exc
    if manifest.evaluated_world_revision != expected_world_revision:
        raise ValueError("minimal_reply.accepted_batch_authority_is_not_pinned")
    chain = (acceptance, message_event, plan_event, beat_event, reservation_event, action_event)
    if acceptance.causation_id != manifest.proposal_event_ref or any(
        current.causation_id != previous.event_id for previous, current in zip(chain, chain[1:])
    ):
        raise ValueError("minimal_reply.accepted_batch_causation_is_not_exact")
    first = acceptance
    if any(
        (
            event.world_id != first.world_id
            or event.logical_time != first.logical_time
            or event.created_at != first.created_at
            or event.actor != first.actor
            or event.source != first.source
            or event.trace_id != first.trace_id
            or event.correlation_id != first.correlation_id
        )
        for event in chain[1:]
    ):
        raise ValueError("minimal_reply.accepted_batch_envelope_metadata_mismatch")
    _validate_minimal_reply_event_identity(
        acceptance,
        manifest=manifest,
        role="acceptance",
        stable_id=manifest.acceptance_id,
        domain_identity=True,
    )
    _validate_minimal_reply_event_identity(
        message_event,
        manifest=manifest,
        role="message",
        stable_id=manifest.message_payload_ref,
        domain_identity=True,
    )
    _validate_minimal_reply_event_identity(
        plan_event,
        manifest=manifest,
        role="plan",
        stable_id=manifest.plan_id,
        domain_identity=True,
    )
    _validate_minimal_reply_event_identity(
        beat_event,
        manifest=manifest,
        role="beat",
        stable_id=manifest.beat_id,
        domain_identity=True,
    )
    _validate_minimal_reply_event_identity(
        reservation_event,
        manifest=manifest,
        role="reservation",
        stable_id=manifest.reservation_id,
    )
    _validate_minimal_reply_event_identity(
        action_event,
        manifest=manifest,
        role="action",
        stable_id=manifest.action_id,
    )
    payload = message.message
    if (
        message.acceptance_id != manifest.acceptance_id
        or message.proposal_id != manifest.proposal_id
        or payload.payload_ref != manifest.message_payload_ref
        or payload.payload_hash != manifest.message_payload_hash
    ):
        raise ValueError("minimal_reply.message_does_not_match_manifest")
    if (
        plan.acceptance_id != manifest.acceptance_id
        or plan.proposal_id != manifest.proposal_id
        or plan.expression_change_id != manifest.expression_change_id
        or plan.plan_id != manifest.plan_id
    ):
        raise ValueError("minimal_reply.plan_does_not_match_manifest")
    if (
        beat.acceptance_id != manifest.acceptance_id
        or beat.proposal_id != manifest.proposal_id
        or beat.expression_change_id != manifest.expression_change_id
        or beat.beat.plan_id != manifest.plan_id
        or beat.beat.beat_id != manifest.beat_id
        or beat.beat.payload != payload
    ):
        raise ValueError("minimal_reply.beat_does_not_match_manifest")
    if (
        reservation.reservation_id != manifest.reservation_id
        or canonical_minimal_reply_value_hash(reservation.model_dump(mode="json"))
        != manifest.reservation_hash
        or reservation.action_id != manifest.action_id
        or reservation.category != "chat"
        or reservation.state != "reserved"
        or action.action_id != manifest.action_id
        or canonical_minimal_reply_value_hash(action.model_dump(mode="json"))
        != manifest.action_hash
        or action.kind != "reply"
        or action.layer != "external_action"
        or action.world_id != action_event.world_id
        or action.budget_reservation_id != manifest.reservation_id
        or action.intent_ref != f"{manifest.proposal_id}:{manifest.intent_id}"
        or action.payload_ref != manifest.message_payload_ref
        or action.payload_hash != manifest.message_payload_hash
        or action.causation_id != manifest.proposal_event_ref
        or action.state != "authorized"
    ):
        raise ValueError("minimal_reply.action_or_budget_does_not_match_manifest")
    if canonical_minimal_reply_value_hash(beat.beat.model_dump(mode="json")) != manifest.beat_hash:
        raise ValueError("minimal_reply.beat_does_not_match_manifest")


def _validate_minimal_reply_event_identity(
    event: WorldEvent,
    *,
    manifest: MinimalReplyManifest,
    role: str,
    stable_id: str,
    domain_identity: bool = False,
) -> None:
    if event.event_id != minimal_reply_event_id(
        manifest_hash=manifest.manifest_hash, role=role, stable_id=stable_id
    ):
        raise ValueError("minimal_reply.event_id_is_not_deterministic")
    expected_key = (
        domain_idempotency_key(
            event_type=event.event_type, world_id=event.world_id, payload=event.payload()
        )
        if domain_identity
        else minimal_reply_idempotency_key(
            world_id=event.world_id,
            manifest_hash=manifest.manifest_hash,
            role=role,
            stable_id=stable_id,
        )
    )
    if expected_key is None or event.idempotency_key != expected_key:
        raise ValueError("minimal_reply.idempotency_key_is_not_deterministic")


def reject_minimal_reply_manifest_without_recorder(events: Sequence[WorldEvent]) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == MINIMAL_REPLY_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("minimal_reply.recorder_capability_required")


def reject_expression_plan_manifest_without_recorder(events: Sequence[WorldEvent]) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("expression_plan.recorder_capability_required")


def reject_social_deferred_manifest_without_recorder(events: Sequence[WorldEvent]) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") in SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSIONS
        for event in events
    ):
        raise ValueError("social_deferred.recorder_capability_required")


def _validate_authorized_social_deferred_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") in SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSIONS
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("social_deferred.recorder_capability_required")
    if len(manifests) != 1:
        raise ValueError("social_deferred.accepted_batch_shape_is_not_exact")
    manifest = parse_social_deferred_acceptance_manifest(manifests[0].payload_json)
    expression = manifest.expression_manifest
    beat_count = len(expression.beats)
    expected_types = (
        *social_deferred_authority_event_types(beat_count),
        "AcceptanceRecorded",
        "ThreadOpened",
    )
    if tuple(item.event_type for item in events) != expected_types:
        raise ValueError("social_deferred.accepted_batch_shape_is_not_exact")
    if manifest.evaluated_world_revision != expected_world_revision:
        raise ValueError("social_deferred.accepted_batch_authority_is_not_pinned")
    if manifests[0].causation_id != manifest.proposal_event_ref or any(
        current.causation_id != previous.event_id for previous, current in zip(events, events[1:])
    ):
        raise ValueError("social_deferred.accepted_batch_causation_is_not_exact")
    commitment = CommitmentChangedPayload.model_validate_json(events[1].payload_json)
    message_start = 2
    message_end = message_start + beat_count
    messages = tuple(
        MessagePayloadStoredPayload.model_validate_json(item.payload_json)
        for item in events[message_start:message_end]
    )
    plan_index = message_end
    plan = ExpressionPlanAcceptedPayload.model_validate_json(events[plan_index].payload_json)
    effect_start = plan_index + 1
    effect_events = events[effect_start : effect_start + beat_count * 3]
    beat_payloads = tuple(
        ExpressionBeatAuthorizedPayload.model_validate_json(effect_events[index].payload_json)
        for index in range(0, len(effect_events), 3)
    )
    reservations = tuple(
        BudgetReservation.model_validate_json(
            json.dumps(
                effect_events[index + 1].payload()["reservation"],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        for index in range(0, len(effect_events), 3)
    )
    actions = tuple(
        Action.model_validate_json(
            json.dumps(
                effect_events[index + 2].payload()["action"],
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        for index in range(0, len(effect_events), 3)
    )
    thread_acceptance_index = effect_start + beat_count * 3
    thread_acceptance = events[thread_acceptance_index].payload()
    thread = ThreadChangedPayload.model_validate_json(
        events[thread_acceptance_index + 1].payload_json
    )
    commitment_after = commitment.commitment_after
    anchor = next(iter(commitment_after.values.anchor_evidence_refs), None)
    expression_action_ids = tuple(item.action.action_id for item in expression.beats)
    if (
        canonical_expression_plan_value_hash(commitment.model_dump(mode="json"))
        != manifest.commitment_payload_hash
        or commitment.change_id != manifest.accepted_change_id
        or commitment.accepted_change_hash != manifest.accepted_change_hash
        or commitment.acceptance_id != manifest.acceptance_id
        or commitment.proposal_id != manifest.proposal_id
        or commitment.commitment_after.commitment_id != manifest.commitment_id
        or thread.proposal_id != manifest.thread_proposal_id
        or thread.thread_after.thread_id != manifest.thread_id
        or canonical_expression_plan_value_hash(thread.model_dump(mode="json"))
        != manifest.thread_payload_hash
        or thread_acceptance.get("proposal_id") != manifest.thread_proposal_id
        or thread_acceptance.get("acceptance_id") != thread.acceptance_id
        or thread_acceptance.get("accepted_change_hash") != thread.accepted_change_hash
        or commitment_after.values.subject_ref != manifest.source_observation_id
        or anchor is None
        or anchor.ref_id != manifest.source_observation_id
        or anchor.evidence_type != "observed_message"
        or anchor.immutable_hash != manifest.source_observation_event_hash
        or any(
            message.acceptance_id != manifest.acceptance_id
            or message.proposal_id != manifest.proposal_id
            or message.message != beat.beat.payload
            for message, beat in zip(messages, expression.beats, strict=True)
        )
        or plan.acceptance_id != manifest.acceptance_id
        or plan.proposal_id != manifest.proposal_id
        or plan.expression_change_id != expression.expression_change_id
        or plan.plan_id != expression.plan_id
        or any(
            beat_payload.acceptance_id != manifest.acceptance_id
            or beat_payload.beat != beat.beat
            or reservation != beat.reservation
            or action != beat.action
            or action.kind != "followup"
            for beat_payload, reservation, action, beat in zip(
                beat_payloads,
                reservations,
                actions,
                expression.beats,
                strict=True,
            )
        )
        or tuple(action.action_id for action in actions) != expression_action_ids
        or expression_action_ids[-1] != manifest.action_id
    ):
        raise ValueError("social_deferred.accepted_batch_does_not_match_manifest")


def _validate_authorized_expression_plan_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("expression_plan.recorder_capability_required")
    if len(manifests) != 1:
        raise ValueError("expression_plan.accepted_batch_must_have_one_manifest")
    acceptance = manifests[0]
    try:
        manifest = ExpressionPlanAcceptanceManifest.model_validate_json(acceptance.payload_json)
    except Exception as exc:
        raise ValueError("expression_plan.accepted_batch_payload_is_invalid") from exc
    if manifest.evaluated_world_revision != expected_world_revision:
        raise ValueError("expression_plan.accepted_batch_authority_is_not_pinned")
    payload_types = tuple(
        "MessagePayloadStored"
        if item.beat.payload.storage_kind == "inline_text"
        else "ExpressionPayloadDescriptorRecorded"
        for item in manifest.beats
    )
    expected_types = (
        ("AcceptanceRecorded",)
        + payload_types
        + ("ExpressionPlanAccepted",)
        + sum(
            (
                ("ExpressionBeatAuthorized", "BudgetReserved", "ActionAuthorized")
                for _ in manifest.beats
            ),
            (),
        )
    )
    if tuple(event.event_type for event in events) != expected_types:
        raise ValueError("expression_plan.accepted_batch_shape_is_not_exact")
    if acceptance.causation_id != manifest.proposal_event_ref or any(
        current.causation_id != previous.event_id for previous, current in zip(events, events[1:])
    ):
        raise ValueError("expression_plan.accepted_batch_causation_is_not_exact")
    first = events[0]
    if any(
        item.world_id != first.world_id
        or item.logical_time != first.logical_time
        or item.created_at != first.created_at
        or item.actor != first.actor
        or item.source != first.source
        or item.trace_id != first.trace_id
        or item.correlation_id != first.correlation_id
        for item in events[1:]
    ):
        raise ValueError("expression_plan.accepted_batch_envelope_metadata_mismatch")
    payload_events = events[1 : 1 + len(manifest.beats)]
    plan_event = events[1 + len(manifest.beats)]
    tails = events[2 + len(manifest.beats) :]
    _validate_expression_plan_identity(
        acceptance,
        manifest=manifest,
        role="acceptance",
        stable_id=manifest.acceptance_id,
        domain_identity=True,
    )
    for payload_event, item in zip(payload_events, manifest.beats, strict=True):
        if item.beat.payload.storage_kind == "inline_text":
            _validate_expression_plan_identity(
                payload_event,
                manifest=manifest,
                role="message",
                stable_id=item.beat.payload.payload_ref,
                domain_identity=True,
            )
            message = MessagePayloadStoredPayload.model_validate_json(payload_event.payload_json)
            if (
                message.acceptance_id != manifest.acceptance_id
                or message.proposal_id != manifest.proposal_id
                or message.message != item.beat.payload
                or canonical_expression_plan_value_hash(message.message.model_dump(mode="json"))
                != item.message_hash
            ):
                raise ValueError("expression_plan.message_does_not_match_manifest")
        else:
            _validate_expression_plan_identity(
                payload_event,
                manifest=manifest,
                role="payload-descriptor",
                stable_id=item.beat.payload.payload_ref,
                domain_identity=True,
            )
            descriptor = ExpressionPayloadDescriptorRecordedPayload.model_validate_json(
                payload_event.payload_json
            )
            if (
                descriptor.acceptance_id != manifest.acceptance_id
                or descriptor.proposal_id != manifest.proposal_id
                or descriptor.payload_ref != item.beat.payload.payload_ref
                or descriptor.payload_hash != item.beat.payload.payload_hash
                or descriptor.content_type != item.beat.payload.content_type
                or descriptor.privacy_class != item.beat.payload.privacy_class
                or descriptor.payload_kind != item.beat.payload.sidecar_kind
            ):
                raise ValueError("expression_plan.payload_descriptor_does_not_match_manifest")
    _validate_expression_plan_identity(
        plan_event, manifest=manifest, role="plan", stable_id=manifest.plan_id, domain_identity=True
    )
    plan = ExpressionPlanAcceptedPayload.model_validate_json(plan_event.payload_json)
    if (
        plan.acceptance_id != manifest.acceptance_id
        or plan.proposal_id != manifest.proposal_id
        or plan.expression_change_id != manifest.expression_change_id
        or plan.plan_id != manifest.plan_id
        or plan.media_request != manifest.media_request
    ):
        raise ValueError("expression_plan.plan_does_not_match_manifest")
    for offset, item in enumerate(manifest.beats):
        beat_event, reservation_event, action_event = tails[offset * 3 : offset * 3 + 3]
        _validate_expression_plan_identity(
            beat_event,
            manifest=manifest,
            role="beat",
            stable_id=item.beat.beat_id,
            domain_identity=True,
        )
        _validate_expression_plan_identity(
            reservation_event,
            manifest=manifest,
            role="reservation",
            stable_id=item.reservation.reservation_id,
        )
        _validate_expression_plan_identity(
            action_event, manifest=manifest, role="action", stable_id=item.action.action_id
        )
        beat = ExpressionBeatAuthorizedPayload.model_validate_json(beat_event.payload_json)
        reservation = BudgetReservation.model_validate_json(
            json.dumps(reservation_event.payload()["reservation"], ensure_ascii=False)
        )
        action = Action.model_validate_json(
            json.dumps(action_event.payload()["action"], ensure_ascii=False)
        )
        if (
            beat.acceptance_id != manifest.acceptance_id
            or beat.proposal_id != manifest.proposal_id
            or beat.expression_change_id != manifest.expression_change_id
            or beat.beat != item.beat
            or canonical_expression_plan_value_hash(beat.beat.model_dump(mode="json"))
            != item.beat_hash
            or reservation != item.reservation
            or action != item.action
            or canonical_expression_plan_value_hash(reservation.model_dump(mode="json"))
            != item.reservation_hash
            or canonical_expression_plan_value_hash(action.model_dump(mode="json"))
            != item.action_hash
        ):
            raise ValueError("expression_plan.effect_does_not_match_manifest")


def _validate_expression_plan_identity(
    event: WorldEvent,
    *,
    manifest: ExpressionPlanAcceptanceManifest,
    role: str,
    stable_id: str,
    domain_identity: bool = False,
) -> None:
    if event.event_id != expression_plan_event_id(
        manifest_hash=manifest.manifest_hash, role=role, stable_id=stable_id
    ):
        raise ValueError("expression_plan.event_id_is_not_deterministic")
    expected = (
        domain_idempotency_key(
            event_type=event.event_type, world_id=event.world_id, payload=event.payload()
        )
        if domain_identity
        else expression_plan_idempotency_key(
            world_id=event.world_id,
            manifest_hash=manifest.manifest_hash,
            role=role,
            stable_id=stable_id,
        )
    )
    if expected is None or event.idempotency_key != expected:
        raise ValueError("expression_plan.idempotency_key_is_not_deterministic")


def _validate_authorized_appraisal_acceptance_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == APPRAISAL_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("appraisal_acceptance.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 3:
        raise ValueError("appraisal_acceptance.accepted_batch_must_be_exact")
    acceptance, mutation, completion = events
    try:
        manifest = AppraisalAcceptanceManifest.model_validate_json(acceptance.payload_json)
        mutation_model = {
            "AppraisalAccepted": AppraisalAcceptedPayload,
            "AppraisalContradicted": AppraisalContradictedPayload,
            "AppraisalSuperseded": AppraisalSupersededPayload,
        }[manifest.mutation_event_type]
        payload = mutation_model.model_validate_json(mutation.payload_json)
    except Exception as exc:
        raise ValueError("appraisal_acceptance.accepted_batch_payload_is_invalid") from exc
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or tuple(event.event_type for event in events)
        != ("AcceptanceRecorded", manifest.mutation_event_type, "TriggerProcessCompleted")
        or acceptance.causation_id != manifest.proposal_event_ref
        or mutation.causation_id != acceptance.event_id
        or completion.causation_id != mutation.event_id
        or mutation.event_id != manifest.mutation_event_id
        or completion.event_id != manifest.completion_event_id
        or mutation.payload_hash != manifest.mutation_payload_hash
        or completion.payload_hash != manifest.completion_payload_hash
    ):
        raise ValueError("appraisal_acceptance.batch_does_not_match_manifest")
    first = acceptance
    if any(
        (
            item.world_id != first.world_id
            or item.logical_time != first.logical_time
            or item.created_at != first.created_at
            or item.actor != first.actor
            or item.source != first.source
            or item.trace_id != first.trace_id
            or item.correlation_id != first.correlation_id
        )
        for item in (mutation, completion)
    ):
        raise ValueError("appraisal_acceptance.envelope_metadata_mismatch")
    if (
        payload.acceptance_id != manifest.acceptance_id
        or payload.proposal_id != manifest.proposal_id
        or payload.change_id != manifest.accepted_change_id
        or payload.accepted_change_hash != manifest.accepted_change_hash
        or payload.evaluated_world_revision != manifest.evaluated_world_revision
        or payload.trigger_id != manifest.trigger_id
        or canonical_appraisal_acceptance_value_hash(payload.model_dump(mode="json"))
        != manifest.mutation_payload_hash
    ):
        raise ValueError("appraisal_acceptance.mutation_does_not_match_manifest")
    completion_payload = completion.payload()
    if (
        completion_payload.get("trigger_id") != manifest.trigger_id
        or canonical_appraisal_acceptance_value_hash(completion_payload)
        != manifest.completion_payload_hash
    ):
        raise ValueError("appraisal_acceptance.trigger_completion_does_not_match_manifest")
    for event in (acceptance, mutation):
        expected = domain_idempotency_key(
            event_type=event.event_type, world_id=event.world_id, payload=event.payload()
        )
        if expected is None or event.idempotency_key != expected:
            raise ValueError("appraisal_acceptance.event_identity_is_not_deterministic")


def reject_appraisal_acceptance_manifest_without_recorder(events: Sequence[WorldEvent]) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == APPRAISAL_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("appraisal_acceptance.recorder_capability_required")


def _validate_authorized_affect_acceptance_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == AFFECT_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("affect_acceptance.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 2:
        raise ValueError("affect_acceptance.accepted_batch_must_be_exact")
    acceptance, mutation = events
    try:
        manifest = AffectAcceptanceManifest.model_validate_json(acceptance.payload_json)
        payload = AFFECT_PAYLOAD_MODELS[manifest.mutation_event_type].model_validate_json(
            mutation.payload_json
        )
    except Exception as exc:
        raise ValueError("affect_acceptance.accepted_batch_payload_is_invalid") from exc
    if not isinstance(payload, AffectAuthorizedMutationPayload):
        raise ValueError("affect_acceptance.mechanical_mutation_is_not_acceptable")
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or tuple(event.event_type for event in events)
        != ("AcceptanceRecorded", manifest.mutation_event_type)
        or acceptance.causation_id != manifest.proposal_event_ref
        or mutation.causation_id != acceptance.event_id
        or mutation.event_id != manifest.mutation_event_id
        or mutation.payload_hash != manifest.mutation_payload_hash
    ):
        raise ValueError("affect_acceptance.batch_does_not_match_manifest")
    if any(
        (
            mutation.world_id != acceptance.world_id,
            mutation.logical_time != acceptance.logical_time,
            mutation.created_at != acceptance.created_at,
            mutation.actor != acceptance.actor,
            mutation.source != acceptance.source,
            mutation.trace_id != acceptance.trace_id,
            mutation.correlation_id != acceptance.correlation_id,
        )
    ):
        raise ValueError("affect_acceptance.envelope_metadata_mismatch")
    if (
        payload.acceptance_id != manifest.acceptance_id
        or payload.proposal_id != manifest.proposal_id
        or payload.change_id != manifest.accepted_change_id
        or payload.accepted_change_hash != manifest.accepted_change_hash
        or payload.evaluated_world_revision != manifest.evaluated_world_revision
        or canonical_affect_acceptance_value_hash(payload.model_dump(mode="json"))
        != manifest.mutation_payload_hash
    ):
        raise ValueError("affect_acceptance.mutation_does_not_match_manifest")
    origin = getattr(getattr(payload, "episode", None), "origin", None)
    if origin is None:
        origin = getattr(getattr(payload, "successor", None), "origin", None)
    if origin is not None and origin.accepted_event_ref != mutation.event_id:
        raise ValueError("affect_acceptance.mutation_event_identity_not_bound")
    for event in (acceptance, mutation):
        expected = domain_idempotency_key(
            event_type=event.event_type, world_id=event.world_id, payload=event.payload()
        )
        if expected is None or event.idempotency_key != expected:
            raise ValueError("affect_acceptance.event_identity_is_not_deterministic")


def reject_affect_acceptance_manifest_without_recorder(events: Sequence[WorldEvent]) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == AFFECT_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("affect_acceptance.recorder_capability_required")


def _validate_authorized_relationship_acceptance_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == RELATIONSHIP_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("relationship_acceptance.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 2:
        raise ValueError("relationship_acceptance.accepted_batch_must_be_exact")
    acceptance, mutation = events
    try:
        manifest = RelationshipAcceptanceManifest.model_validate_json(acceptance.payload_json)
        payload = RelationshipSignalAcceptedPayload.model_validate_json(mutation.payload_json)
    except Exception as exc:
        raise ValueError("relationship_acceptance.accepted_batch_payload_is_invalid") from exc
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or tuple(event.event_type for event in events)
        != ("AcceptanceRecorded", "RelationshipSignalAccepted")
        or acceptance.causation_id != manifest.proposal_event_ref
        or mutation.causation_id != acceptance.event_id
        or mutation.event_id != manifest.mutation_event_id
        or mutation.payload_hash != manifest.mutation_payload_hash
    ):
        raise ValueError("relationship_acceptance.batch_does_not_match_manifest")
    if any(
        (
            mutation.world_id != acceptance.world_id,
            mutation.logical_time != acceptance.logical_time,
            mutation.created_at != acceptance.created_at,
            mutation.actor != acceptance.actor,
            mutation.source != acceptance.source,
            mutation.trace_id != acceptance.trace_id,
            mutation.correlation_id != acceptance.correlation_id,
        )
    ):
        raise ValueError("relationship_acceptance.envelope_metadata_mismatch")
    if (
        payload.acceptance_id != manifest.acceptance_id
        or payload.proposal_id != manifest.proposal_id
        or payload.change_id != manifest.accepted_change_id
        or payload.accepted_change_hash != manifest.accepted_change_hash
        or payload.evaluated_world_revision != manifest.evaluated_world_revision
        or payload.signal.origin.accepted_event_ref != mutation.event_id
        or canonical_relationship_acceptance_value_hash(payload.model_dump(mode="json"))
        != manifest.mutation_payload_hash
    ):
        raise ValueError("relationship_acceptance.mutation_does_not_match_manifest")
    for item in (acceptance, mutation):
        expected = domain_idempotency_key(
            event_type=item.event_type, world_id=item.world_id, payload=item.payload()
        )
        if expected is None or item.idempotency_key != expected:
            raise ValueError("relationship_acceptance.event_identity_is_not_deterministic")


def reject_relationship_acceptance_manifest_without_recorder(events: Sequence[WorldEvent]) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == RELATIONSHIP_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("relationship_acceptance.recorder_capability_required")


def _validate_authorized_relationship_adjustment_acceptance_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("relationship_adjustment_acceptance.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 2:
        raise ValueError("relationship_adjustment_acceptance.accepted_batch_must_be_exact")
    acceptance, mutation = events
    try:
        manifest = RelationshipAdjustmentAcceptanceManifest.model_validate_json(
            acceptance.payload_json
        )
        payload = RelationshipSlowVariableAdjustedPayload.model_validate_json(mutation.payload_json)
    except Exception as exc:
        raise ValueError(
            "relationship_adjustment_acceptance.accepted_batch_payload_is_invalid"
        ) from exc
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or tuple(event.event_type for event in events)
        != ("AcceptanceRecorded", "RelationshipSlowVariableAdjusted")
        or acceptance.causation_id != manifest.proposal_event_ref
        or mutation.causation_id != acceptance.event_id
        or mutation.event_id != manifest.mutation_event_id
        or mutation.payload_hash != manifest.mutation_payload_hash
    ):
        raise ValueError("relationship_adjustment_acceptance.batch_does_not_match_manifest")
    if any(
        (
            mutation.world_id != acceptance.world_id,
            mutation.logical_time != acceptance.logical_time,
            mutation.created_at != acceptance.created_at,
            mutation.actor != acceptance.actor,
            mutation.source != acceptance.source,
            mutation.trace_id != acceptance.trace_id,
            mutation.correlation_id != acceptance.correlation_id,
        )
    ):
        raise ValueError("relationship_adjustment_acceptance.envelope_metadata_mismatch")
    if (
        payload.operation != "adjust"
        or payload.acceptance_id != manifest.acceptance_id
        or payload.proposal_id != manifest.proposal_id
        or payload.change_id != manifest.accepted_change_id
        or payload.accepted_change_hash != manifest.accepted_change_hash
        or payload.evaluated_world_revision != manifest.evaluated_world_revision
        or canonical_relationship_adjustment_acceptance_value_hash(payload.model_dump(mode="json"))
        != manifest.mutation_payload_hash
    ):
        raise ValueError("relationship_adjustment_acceptance.mutation_does_not_match_manifest")
    for item in (acceptance, mutation):
        expected = domain_idempotency_key(
            event_type=item.event_type, world_id=item.world_id, payload=item.payload()
        )
        if expected is None or item.idempotency_key != expected:
            raise ValueError(
                "relationship_adjustment_acceptance.event_identity_is_not_deterministic"
            )


def reject_relationship_adjustment_acceptance_manifest_without_recorder(
    events: Sequence[WorldEvent],
) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("relationship_adjustment_acceptance.recorder_capability_required")


def reject_activity_lifecycle_acceptance_manifest_without_recorder(
    events: Sequence[WorldEvent],
) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("activity_lifecycle_acceptance.recorder_capability_required")


def _validate_authorized_activity_lifecycle_acceptance_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    """Bind the scheduler manifest to precisely one accepted activity effect.

    The proposal was already committed in its own deliberation transaction, so
    this batch cannot re-read it.  It instead proves the remaining atomic
    boundary: manifest bytes, envelope, event identity, and the immediately
    following lifecycle payload all agree.  Reducers then verify the persisted
    proposal and the plan transition at replay time.
    """

    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("activity_lifecycle_acceptance.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 2:
        raise ValueError("activity_lifecycle_acceptance.accepted_batch_must_be_exact")
    acceptance, effect = events
    try:
        manifest = ActivityLifecycleAcceptanceManifest.model_validate_json(acceptance.payload_json)
        payload = ActivityTransitionPayload.model_validate_json(effect.payload_json)
    except Exception as exc:
        raise ValueError("activity_lifecycle_acceptance.accepted_batch_payload_is_invalid") from exc
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or tuple(event.event_type for event in events)
        != ("AcceptanceRecorded", manifest.effect_event_type)
        or acceptance.causation_id != manifest.proposal_event_ref
        or effect.causation_id != acceptance.event_id
        or acceptance.event_id != manifest.acceptance_event_ref
        or effect.event_id != manifest.effect_event_id
        or effect.payload_hash != manifest.effect_event_payload_hash
        or payload.acceptance_id != manifest.acceptance_id
        or payload.activity_lifecycle_proposal_id != manifest.proposal_id
        or payload.change_id != manifest.accepted_change_id
        or payload.accepted_change_hash != manifest.accepted_change_hash
        or canonical_activity_lifecycle_acceptance_value_hash(effect.payload())
        != manifest.effect_event_payload_hash
    ):
        raise ValueError("activity_lifecycle_acceptance.batch_does_not_match_manifest")
    if any(
        getattr(effect, field) != getattr(acceptance, field)
        for field in (
            "world_id",
            "logical_time",
            "created_at",
            "actor",
            "source",
            "trace_id",
            "correlation_id",
        )
    ):
        raise ValueError("activity_lifecycle_acceptance.envelope_metadata_mismatch")
    for event in events:
        expected = domain_idempotency_key(
            event_type=event.event_type, world_id=event.world_id, payload=event.payload()
        )
        if expected is None or event.idempotency_key != expected:
            raise ValueError("activity_lifecycle_acceptance.event_identity_is_not_deterministic")


def reject_media_selection_acceptance_manifest_without_recorder(
    events: Sequence[WorldEvent],
) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") in MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS
        for event in events
    ):
        raise ValueError("media_selection_acceptance.recorder_capability_required")


def reject_media_continuation_acceptance_manifest_without_recorder(
    events: Sequence[WorldEvent],
) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("media_continuation_acceptance.recorder_capability_required")


def _validate_authorized_media_continuation_acceptance_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    matches = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version")
        == MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not matches:
        return
    if not authorized:
        raise ValueError("media_continuation_acceptance.recorder_capability_required")
    if (
        len(matches) != 1
        or len(events) != 5
        or tuple(event.event_type for event in events)
        != (
            "AcceptanceRecorded",
            "TriggerProcessClaimed",
            "BudgetReserved",
            "ActionAuthorized",
            "TriggerProcessCompleted",
        )
    ):
        raise ValueError("media_continuation_acceptance.accepted_batch_must_be_exact")
    acceptance, claim_event, reservation_event, action_event, completion_event = events
    try:
        manifest = MediaContinuationAcceptanceManifest.model_validate(
            acceptance.payload(), strict=True
        )
        claimed = TriggerProcess.model_validate_json(
            json.dumps(claim_event.payload().get("process"), ensure_ascii=False)
        )
        reservation = BudgetReservation.model_validate_json(
            json.dumps(reservation_event.payload().get("reservation"), ensure_ascii=False)
        )
        action = Action.model_validate_json(
            json.dumps(action_event.payload().get("action"), ensure_ascii=False)
        )
        completion = completion_event.payload()
    except Exception as exc:
        raise ValueError("media_continuation_acceptance.payload_is_invalid") from exc
    expected_kind = {
        "plan_to_render": "media_render",
        "render_to_inspect": "media_inspection",
    }[manifest.continuation_step]
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or acceptance.event_id != manifest.acceptance_event_ref
        or acceptance.causation_id != manifest.proposal_event_ref
        or claim_event.event_id != manifest.claim_event_ref
        or claim_event.causation_id != acceptance.event_id
        or canonical_media_continuation_hash(claim_event.payload()) != manifest.claim_payload_hash
        or reservation_event.event_id != manifest.reservation_event_ref
        or reservation_event.causation_id != claim_event.event_id
        or canonical_media_continuation_hash(reservation_event.payload())
        != manifest.reservation_payload_hash
        or action_event.event_id != manifest.action_event_ref
        or action_event.causation_id != reservation_event.event_id
        or canonical_media_continuation_hash(action_event.payload()) != manifest.action_payload_hash
        or completion_event.event_id != manifest.completion_event_ref
        or completion_event.causation_id != action_event.event_id
        or canonical_media_continuation_hash(completion) != manifest.completion_payload_hash
        or claimed.trigger_id != manifest.trigger_id
        or claimed.source_evidence_ref != manifest.source_evidence_ref
        or claimed.state != "claimed"
        or claimed.claim_lease is None
        or completion.get("trigger_id") != manifest.trigger_id
        or completion.get("owner_id") != claimed.claim_lease.owner_id
        or completion.get("attempt_id") != claimed.claim_lease.attempt_id
        or completion.get("runtime_outcome_ref") != action.action_id
        or reservation.action_id != action.action_id
        or action.budget_reservation_id != reservation.reservation_id
        or action.kind != expected_kind
        or action.action_id != manifest.authorized_action_id
        or action.intent_ref != manifest.authorized_intent_ref
        or action.payload_ref != manifest.authorized_payload_ref
        or action.payload_hash != manifest.authorized_payload_hash
        or action.causation_id != manifest.source_evidence_ref
        or action.layer != "media_action"
        or action.state != "authorized"
        or action.recovery_policy != "effect_once"
    ):
        raise ValueError("media_continuation_acceptance.batch_does_not_match_manifest")
    if any(
        getattr(event, field) != getattr(acceptance, field)
        for event in events[1:]
        for field in (
            "world_id",
            "logical_time",
            "created_at",
            "actor",
            "source",
            "trace_id",
            "correlation_id",
        )
    ):
        raise ValueError("media_continuation_acceptance.envelope_metadata_mismatch")
    for event in events:
        expected = domain_idempotency_key(
            event_type=event.event_type, world_id=event.world_id, payload=event.payload()
        )
        expected = expected or media_continuation_event_identity(
            event_type=event.event_type, world_id=event.world_id, payload=event.payload()
        )
        if event.idempotency_key != expected:
            raise ValueError("media_continuation_acceptance.identity_is_not_deterministic")


def _validate_authorized_media_selection_acceptance_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    """Require the complete accepted selection → frozen planning authorization.

    This is deliberately not a generic accepted effect: P1 has four effects
    whose identities must remain inseparable.  The reducer rechecks the
    candidate/proposal state; this guard rejects a partial or substituted wire
    batch before any reducer sees it.
    """

    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") in MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("media_selection_acceptance.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 4:
        raise ValueError("media_selection_acceptance.accepted_batch_must_be_exact")
    acceptance, opportunity_event, reservation_event, action_event = events
    try:
        manifest = parse_media_selection_acceptance_manifest(acceptance.payload())
        opportunity = MediaOpportunityFrozenPayload.model_validate_json(
            opportunity_event.payload_json
        ).opportunity
        reservation = BudgetReservation.model_validate(
            reservation_event.payload().get("reservation")
        )
        # Event payloads have crossed the JSON envelope by this point.  Parse
        # them in JSON mode so RFC 3339 timestamps and JSON arrays rehydrate
        # to the strict Action value rather than being rejected as Python
        # strings/lists.
        action = Action.model_validate_json(
            json.dumps(action_event.payload().get("action"), ensure_ascii=False)
        )
    except Exception as exc:
        raise ValueError("media_selection_acceptance.accepted_batch_payload_is_invalid") from exc
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or tuple(event.event_type for event in events)
        != ("AcceptanceRecorded", "MediaOpportunityFrozen", "BudgetReserved", "ActionAuthorized")
        or acceptance.event_id != manifest.acceptance_event_ref
        or acceptance.causation_id != manifest.proposal_event_ref
        or opportunity_event.event_id != manifest.opportunity_event_id
        or opportunity_event.causation_id != acceptance.event_id
        or opportunity_event.payload_hash != manifest.opportunity_payload_hash
        or reservation_event.event_id != manifest.reservation_event_id
        or reservation_event.causation_id != opportunity_event.event_id
        or reservation_event.payload_hash != manifest.reservation_payload_hash
        or action_event.event_id != manifest.action_event_id
        or action_event.causation_id != reservation_event.event_id
        or action_event.payload_hash != manifest.action_payload_hash
        or opportunity.opportunity_id != manifest.opportunity_id
        or opportunity.candidate_id != manifest.candidate_id
        or opportunity.selection_proposal_id != manifest.proposal_id
        or opportunity.selection_hash != manifest.selection_hash
        or opportunity.selected_candidate_revision != manifest.expected_candidate_revision
        or opportunity.event_snapshot_ref != manifest.snapshot_ref
        or opportunity.event_snapshot_hash != manifest.snapshot_hash
        or reservation.action_id != action.action_id
        or action.kind != "media_planning"
        or action.intent_ref != opportunity.opportunity_id
        or action.payload_ref != opportunity.event_snapshot_ref
        or action.payload_hash != opportunity.event_snapshot_hash
        or action.budget_reservation_id != reservation.reservation_id
        or canonical_media_selection_value_hash(opportunity_event.payload())
        != manifest.opportunity_payload_hash
    ):
        raise ValueError("media_selection_acceptance.batch_does_not_match_manifest")
    if getattr(manifest, "manifest_version", None) == "media-selection-acceptance.2" and (
        opportunity.p3_authorization_digest != getattr(manifest, "p3_authorization_digest", None)
        or opportunity.media_lane not in {"alluring_life", "exclusive_private"}
        or opportunity.media_privacy_ceiling != "intimate"
        or opportunity.recipient_ref is None
        or opportunity.private_expression_basis_ref is None
    ):
        raise ValueError("media_selection_acceptance.p3_authorization_does_not_match_opportunity")
    if any(
        getattr(effect, field) != getattr(acceptance, field)
        for effect in (opportunity_event, reservation_event, action_event)
        for field in (
            "world_id",
            "logical_time",
            "created_at",
            "actor",
            "source",
            "trace_id",
            "correlation_id",
        )
    ):
        raise ValueError("media_selection_acceptance.envelope_metadata_mismatch")
    for event in (acceptance, opportunity_event):
        expected = domain_idempotency_key(
            event_type=event.event_type, world_id=event.world_id, payload=event.payload()
        )
        if expected is None or event.idempotency_key != expected:
            raise ValueError("media_selection_acceptance.event_identity_is_not_deterministic")


def reject_outcome_acceptance_manifest_without_recorder(events: Sequence[WorldEvent]) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == OUTCOME_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("outcome_acceptance.recorder_capability_required")


def reject_interaction_bid_acceptance_manifest_without_recorder(
    events: Sequence[WorldEvent],
) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("interaction_bid_acceptance.recorder_capability_required")


def reject_media_thread_acceptance_manifest_without_recorder(events: Sequence[WorldEvent]) -> None:
    if any(
        event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION
        for event in events
    ):
        raise ValueError("media_thread_acceptance.recorder_capability_required")


def _validate_authorized_media_thread_acceptance_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("media_thread_acceptance.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 2:
        raise ValueError("media_thread_acceptance.accepted_batch_must_be_exact")
    acceptance, changed = events
    try:
        manifest = MediaDeliveryThreadAcceptanceManifest.model_validate_json(
            acceptance.payload_json
        )
        payload = MediaDeliveryThreadChangedPayload.model_validate_json(changed.payload_json)
    except Exception as exc:
        raise ValueError("media_thread_acceptance.accepted_batch_payload_is_invalid") from exc
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or tuple(event.event_type for event in events)
        != ("AcceptanceRecorded", manifest.thread_event_type)
        or acceptance.causation_id != manifest.proposal_event_ref
        or changed.causation_id != acceptance.event_id
        or changed.event_id != manifest.thread_event_id
        or changed.payload_hash != manifest.thread_payload_hash
        or payload.acceptance_id != manifest.acceptance_id
        or payload.proposal_id != manifest.proposal_id
        or payload.change_id != manifest.accepted_change_id
        or payload.accepted_change_hash != manifest.accepted_change_hash
        or payload.evaluated_world_revision != manifest.evaluated_world_revision
        or canonical_media_thread_value_hash(changed.payload()) != manifest.thread_payload_hash
    ):
        raise ValueError("media_thread_acceptance.batch_does_not_match_manifest")
    if any(
        getattr(changed, field) != getattr(acceptance, field)
        for field in (
            "world_id",
            "logical_time",
            "created_at",
            "actor",
            "source",
            "trace_id",
            "correlation_id",
        )
    ):
        raise ValueError("media_thread_acceptance.envelope_metadata_mismatch")


def _validate_authorized_interaction_bid_acceptance_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("interaction_bid_acceptance.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 2:
        raise ValueError("interaction_bid_acceptance.accepted_batch_must_be_exact")
    acceptance, opened = events
    try:
        manifest = InteractionBidAcceptanceManifest.model_validate_json(acceptance.payload_json)
        payload = InteractionBidOpenedPayload.model_validate_json(opened.payload_json)
    except Exception as exc:
        raise ValueError("interaction_bid_acceptance.accepted_batch_payload_is_invalid") from exc
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or tuple(event.event_type for event in events)
        != ("AcceptanceRecorded", "InteractionBidOpened")
        or acceptance.causation_id != manifest.proposal_event_ref
        or opened.causation_id != acceptance.event_id
        or opened.event_id != manifest.bid_event_id
        or opened.payload_hash != manifest.bid_payload_hash
        or payload.acceptance_id != manifest.acceptance_id
        or payload.proposal_id != manifest.proposal_id
        or payload.change_id != manifest.accepted_change_id
        or payload.accepted_change_hash != manifest.accepted_change_hash
        or payload.evaluated_world_revision != manifest.evaluated_world_revision
        or payload.bid.delivery_id != manifest.delivery_id
        or payload.bid.delivery_event_ref != manifest.delivery_event_ref
        or payload.bid.delivery_event_payload_hash != manifest.delivery_event_payload_hash
        or payload.bid.deliberation_trigger_id != manifest.deliberation_trigger_id
        or canonical_interaction_bid_value_hash(opened.payload()) != manifest.bid_payload_hash
    ):
        raise ValueError("interaction_bid_acceptance.batch_does_not_match_manifest")
    if any(
        getattr(event, field) != getattr(acceptance, field)
        for event in (opened,)
        for field in (
            "world_id",
            "logical_time",
            "created_at",
            "actor",
            "source",
            "trace_id",
            "correlation_id",
        )
    ):
        raise ValueError("interaction_bid_acceptance.envelope_metadata_mismatch")


def _validate_authorized_outcome_acceptance_manifest_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int, authorized: bool
) -> None:
    manifests = [
        event
        for event in events
        if event.event_type == "AcceptanceRecorded"
        and event.payload().get("manifest_version") == OUTCOME_ACCEPTANCE_MANIFEST_VERSION
    ]
    if not manifests:
        return
    if not authorized:
        raise ValueError("outcome_acceptance.recorder_capability_required")
    if len(manifests) != 1 or len(events) != 3:
        raise ValueError("outcome_acceptance.accepted_batch_must_be_exact")
    acceptance, settlement, trigger_event = events
    try:
        manifest = OutcomeAcceptanceManifest.model_validate_json(acceptance.payload_json)
        settlement_payload = WorldOccurrenceSettledPayload.model_validate_json(
            settlement.payload_json
        )
        process = trigger_event.payload().get("process")
        trigger = TriggerProcess.model_validate_json(
            json.dumps(process, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    except Exception as exc:
        raise ValueError("outcome_acceptance.accepted_batch_payload_is_invalid") from exc
    if (
        manifest.evaluated_world_revision != expected_world_revision
        or tuple(event.event_type for event in events)
        != ("AcceptanceRecorded", "WorldOccurrenceSettled", "TriggerProcessOpened")
        or acceptance.causation_id != manifest.proposal_event_ref
        or settlement.causation_id != acceptance.event_id
        or trigger_event.causation_id != settlement.event_id
        or settlement.event_id != manifest.settlement_event_id
        or settlement.payload_hash != manifest.settlement_payload_hash
        or trigger_event.event_id != manifest.npc_appraisal_trigger_event_id
        or trigger_event.payload_hash != manifest.npc_appraisal_trigger_payload_hash
    ):
        raise ValueError("outcome_acceptance.batch_does_not_match_manifest")
    first = acceptance
    if any(
        (
            item.world_id != first.world_id
            or item.logical_time != first.logical_time
            or item.created_at != first.created_at
            or item.actor != first.actor
            or item.source != first.source
            or item.trace_id != first.trace_id
            or item.correlation_id != first.correlation_id
        )
        for item in (settlement, trigger_event)
    ):
        raise ValueError("outcome_acceptance.envelope_metadata_mismatch")
    if (
        settlement_payload.acceptance_id != manifest.acceptance_id
        or settlement_payload.outcome_proposal_id != manifest.proposal_id
        or settlement_payload.change_id != manifest.accepted_change_id
        or settlement_payload.accepted_change_hash != manifest.accepted_change_hash
        or settlement_payload.evaluated_world_revision != manifest.evaluated_world_revision
        or settlement_payload.appraisal_trigger_ref != manifest.npc_appraisal_trigger_id
        # The manifest pins the recorded wire payload (including the original
        # RFC3339 spelling), while model serialization may normalize ``+00:00``
        # to ``Z``.  Validate fields through the model above, then hash bytes
        # as actually committed.
        or canonical_outcome_acceptance_value_hash(settlement.payload())
        != manifest.settlement_payload_hash
    ):
        raise ValueError("outcome_acceptance.settlement_does_not_match_manifest")
    expected_trigger_id = appraisal_trigger_identity(
        settlement_payload.occurrence_id, settlement_payload.result_id
    )
    if (
        trigger.trigger_id != manifest.npc_appraisal_trigger_id
        or trigger.trigger_id != expected_trigger_id
        or trigger.trigger_ref != trigger.trigger_id
        or trigger.process_kind != "npc_world_appraisal"
        or trigger.state != "open"
        or trigger.source_evidence_ref != settlement.event_id
        or canonical_outcome_acceptance_value_hash(trigger_event.payload())
        != manifest.npc_appraisal_trigger_payload_hash
    ):
        raise ValueError("outcome_acceptance.npc_trigger_does_not_match_manifest")
    for event in events:
        expected = domain_idempotency_key(
            event_type=event.event_type, world_id=event.world_id, payload=event.payload()
        )
        if expected is None or event.idempotency_key != expected:
            raise ValueError("outcome_acceptance.event_identity_is_not_deterministic")


def _validate_media_planning_settlement_batch(events: Sequence[WorldEvent]) -> None:
    """A planning result is one effect-once terminal transaction, never a loose DTO.

    The candidate/opportunity are already validated by their reducers.  This
    guard closes the externally observable half: a plan/not-renderable result
    cannot be appended without the terminal Action, exact receipt, and budget
    settlement that made the planner call accountable.  A plan additionally
    opens exactly one render continuation; preview never creates delivery here.
    """

    indices = [
        index
        for index, event in enumerate(events)
        if event.event_type in {"MediaPlanRecorded", "MediaNotRenderableRecorded"}
    ]
    for index in indices:
        if index < 3:
            raise ValueError("media planning result lacks terminal action/receipt/budget prefix")
        delivered, receipt_event, budget_event, result_event = events[index - 3 : index + 1]
        if tuple(item.event_type for item in (delivered, receipt_event, budget_event)) != (
            "ActionDelivered",
            "ExecutionReceiptRecorded",
            "BudgetSettled",
        ):
            raise ValueError("media planning result requires adjacent terminal settlement events")
        action_id = result_event.payload().get("action_id")
        receipt_id = result_event.payload().get("receipt_id")
        if not isinstance(action_id, str) or not isinstance(receipt_id, str):
            raise ValueError("media planning result identity is invalid")
        if delivered.payload().get("action_id") != action_id:
            raise ValueError("media planning delivered Action does not match result")
        receipt = receipt_event.payload().get("receipt")
        settlement = budget_event.payload().get("settlement")
        if not isinstance(receipt, dict) or not isinstance(settlement, dict):
            raise ValueError("media planning receipt or budget payload is invalid")
        if (
            receipt.get("receipt_id") != receipt_id
            or receipt.get("action_id") != action_id
            or receipt.get("observed_state") != "delivered"
            or receipt.get("is_terminal") is not True
            or settlement.get("action_id") != action_id
            or settlement.get("result_id") != receipt.get("result_id")
            or settlement.get("state") != "settled"
        ):
            raise ValueError("media planning receipt/budget do not bind terminal action")
        if result_event.event_type == "MediaNotRenderableRecorded":
            continue
        result = MediaPlanRecordedPayload.model_validate_json(result_event.payload_json)
        if index + 1 >= len(events) or events[index + 1].event_type != "TriggerProcessOpened":
            raise ValueError("frozen MediaPlan must open one render continuation")
        process = TriggerProcess.model_validate_json(
            json.dumps(events[index + 1].payload().get("process"), ensure_ascii=False)
        )
        expected = continuation_trigger_id(result.plan)
        if (
            process.trigger_id != expected
            or process.trigger_ref != expected
            or process.process_kind != "media_continuation"
            or process.state != "open"
            or process.source_evidence_ref != result_event.event_id
        ):
            raise ValueError("media planning continuation is not bound to frozen plan")


def _validate_media_render_continuation_batch(events: Sequence[WorldEvent]) -> None:
    """Every settled render artifact opens exactly its inspection continuation."""

    for index, event in enumerate(events):
        if event.event_type != "MediaRenderArtifactRecorded":
            continue
        if index + 1 >= len(events) or events[index + 1].event_type != "TriggerProcessOpened":
            raise ValueError("settled media render must open one inspection continuation")
        payload = MediaRenderArtifactRecordedPayload.model_validate_json(event.payload_json)
        process = TriggerProcess.model_validate_json(
            json.dumps(events[index + 1].payload().get("process"), ensure_ascii=False)
        )
        expected = artifact_continuation_trigger_id(payload.artifact)
        if (
            process.trigger_id != expected
            or process.trigger_ref != expected
            or process.process_kind != "media_continuation"
            or process.state != "open"
            or process.source_evidence_ref != event.event_id
        ):
            raise ValueError("inspection continuation is not bound to exact render artifact")


def _validate_media_repair_acceptance_batch(events: Sequence[WorldEvent]) -> None:
    """A repair decision has no half-accepted state or unbudgeted Action."""
    for index, event in enumerate(events):
        if event.event_type != "MediaRepairAuthorized":
            continue
        if index < 1 or index + 3 >= len(events):
            raise ValueError(
                "media repair acceptance must be one atomic trigger/budget/action batch"
            )
        claimed, authorized, reserved, action_event, completed = events[index - 1 : index + 4]
        if tuple(
            item.event_type for item in (claimed, authorized, reserved, action_event, completed)
        ) != (
            "TriggerProcessClaimed",
            "MediaRepairAuthorized",
            "BudgetReserved",
            "ActionAuthorized",
            "TriggerProcessCompleted",
        ):
            raise ValueError("media repair acceptance event order is invalid")
        repair = MediaRepairAuthorizedPayload.model_validate_json(authorized.payload_json).repair
        process = TriggerProcess.model_validate(claimed.payload().get("process"))
        reservation = BudgetReservation.model_validate(reserved.payload().get("reservation"))
        action = Action.model_validate(action_event.payload().get("action"))
        completed_payload = completed.payload()
        if (
            process.trigger_id != repair.trigger_id
            or process.state != "claimed"
            or action.action_id != repair.action_id
            or action.idempotency_key != repair.repair_attempt_id
            or reservation.reservation_id != repair.reservation_id
            or reservation.action_id != action.action_id
            or action.budget_reservation_id != repair.reservation_id
            or reservation.category != "repair"
            or completed_payload.get("trigger_id") != repair.trigger_id
            or completed_payload.get("attempt_id") != process.claim_lease.attempt_id
            or completed_payload.get("owner_id") != process.claim_lease.owner_id
            or completed_payload.get("runtime_outcome_ref") != repair.repair_attempt_id
        ):
            raise ValueError("media repair acceptance binding is invalid")


def appraisal_trigger_identity(occurrence_id: str, result_id: str) -> str:
    return f"appraisal:{occurrence_id}:{result_id}"


def interaction_appraisal_trigger_identity(world_id: str, observation_ref: str) -> str:
    encoded = json.dumps(
        [world_id, observation_ref, "interaction_appraisal"],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"appraisal:interaction:{hashlib.sha256(encoded).hexdigest()}"


def silence_appraisal_trigger_identity(world_id: str, source_evidence_ref: str) -> str:
    """One deterministic trigger per delivered-reply silence anchor.

    The anchor is the committed ``ExecutionReceiptRecorded`` event of the
    companion's last visible message; deriving the identity from it makes the
    per-silence trigger idempotent under replay and concurrent openers.
    """

    if not world_id or not source_evidence_ref:
        raise ValueError("silence appraisal identity requires world and receipt event")
    encoded = json.dumps(
        [world_id, source_evidence_ref, "silence_appraisal"],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"trigger:silence-appraisal:{hashlib.sha256(encoded).hexdigest()}"


def private_impression_trigger_identity(world_id: str, source_evidence_ref: str) -> str:
    """One deterministic trigger per accepted appraisal considered for an impression.

    The anchor is the committed ``AppraisalAccepted`` event; deriving the
    identity from it makes the per-appraisal trigger idempotent under replay
    and concurrent openers.
    """

    if not world_id or not source_evidence_ref:
        raise ValueError("private impression identity requires world and appraisal event")
    encoded = json.dumps(
        [world_id, source_evidence_ref, "private_impression"],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"trigger:private-impression:{hashlib.sha256(encoded).hexdigest()}"


def plan_disruption_appraisal_trigger_identity(world_id: str, source_evidence_ref: str) -> str:
    """One deterministic trigger per abandoned-plan anchor.

    The anchor is the committed ``ActivityAbandoned`` event of one plan's
    terminal transition; deriving the identity from it makes the per-abandonment
    trigger idempotent under replay and concurrent openers, regardless of which
    producer (lifecycle acceptance, replacement, direct command) committed it.
    """

    if not world_id or not source_evidence_ref:
        raise ValueError("plan disruption identity requires world and abandonment event")
    encoded = json.dumps(
        [world_id, source_evidence_ref, "plan_disruption_appraisal"],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"trigger:plan-disruption-appraisal:{hashlib.sha256(encoded).hexdigest()}"
