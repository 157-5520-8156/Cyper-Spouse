"""Machine-enforced domain idempotency identities for typed event families."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .schemas import WorldEvent
from .typed_proposal_families import family_for_mutation, family_for_record
from .appraisal_acceptance_manifest import APPRAISAL_ACCEPTANCE_MANIFEST_VERSION
from .affect_acceptance_manifest import AFFECT_ACCEPTANCE_MANIFEST_VERSION
from .relationship_acceptance_manifest import RELATIONSHIP_ACCEPTANCE_MANIFEST_VERSION
from .relationship_adjustment_acceptance_manifest import (
    RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION,
)
from .minimal_reply_manifest import MINIMAL_REPLY_MANIFEST_VERSION
from .outcome_acceptance_manifest import OUTCOME_ACCEPTANCE_MANIFEST_VERSION
from .expression_plan_manifest import EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION
from .interaction_bid_acceptance_manifest import INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION
from .media_thread_acceptance_manifest import MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION
from .activity_lifecycle_acceptance_manifest import ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION
from .media_selection_acceptance_manifest import MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS
from .media_continuation_acceptance_manifest import (
    MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION,
)
from .social_action_acceptance import SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION


def domain_idempotency_key(
    *, event_type: str, world_id: str, payload: dict[str, Any]
) -> str | None:
    """Derive the installed event identity; return None for legacy families."""

    components = _life_identity_components(event_type, world_id, payload)
    if components is None:
        return None
    encoded = json.dumps(
        [event_type, *components],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"world-v2:{event_type}:{digest}"


def validate_event_identity(event: WorldEvent) -> None:
    if event.event_type in {
        "LegacyAcceptanceAuditRecorded",
        "LegacyExperienceCommitted",
    }:
        raise ValueError("legacy audit/experience events are migration-only")
    expected = domain_idempotency_key(
        event_type=event.event_type,
        world_id=event.world_id,
        payload=event.payload(),
    )
    if expected is not None and event.idempotency_key != expected:
        raise ValueError(f"{event.event_type} idempotency key does not match its domain identity")


def _life_identity_components(
    event_type: str, world_id: str, payload: dict[str, Any]
) -> tuple[object, ...] | None:
    if (
        event_type == "AcceptanceRecorded"
        and "manifest_version" in payload
        and payload.get("manifest_version")
        not in {
            "acceptance-manifest.2",
            "acceptance-manifest.3",
            MINIMAL_REPLY_MANIFEST_VERSION,
            APPRAISAL_ACCEPTANCE_MANIFEST_VERSION,
            AFFECT_ACCEPTANCE_MANIFEST_VERSION,
            RELATIONSHIP_ACCEPTANCE_MANIFEST_VERSION,
            RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION,
            OUTCOME_ACCEPTANCE_MANIFEST_VERSION,
            INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION,
            MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION,
            ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
            *MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSIONS,
            MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION,
            EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION,
            SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION,
        }
    ):
        raise ValueError("acceptance_manifest.unsupported_manifest_version")
    proposal_family = family_for_record(event_type, payload)
    if proposal_family is not None:
        return proposal_family.codec.record_identity(
            world_id=world_id,
            event_type=event_type,
            payload=payload,
        )
    mutation_family = family_for_mutation(event_type)
    if mutation_family is not None:
        return mutation_family.codec.mutation_identity(
            world_id=world_id,
            event_type=event_type,
            payload=payload,
        )
    if event_type == "NpcRegistered":
        return world_id, _nested(payload, "npc", "npc_id")
    if event_type == "AspirationPlanted":
        return (
            world_id,
            _nested(payload, "aspiration", "aspiration_id"),
            payload.get("transition_id"),
        )
    if event_type in {
        "AspirationReinforced",
        "AspirationFaded",
        "AspirationCrystallized",
    }:
        return (
            world_id,
            payload.get("aspiration_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )
    if event_type == "PhotoCandidateOpened":
        return world_id, _nested(payload, "candidate", "candidate_id")
    if event_type == "PhotoCandidateUnrenderable":
        return (
            world_id,
            payload.get("candidate_id"),
            payload.get("expected_entity_revision"),
            payload.get("reason_code"),
        )
    if event_type == "PhotoCandidateExpired":
        return (
            world_id,
            payload.get("candidate_id"),
            payload.get("expected_entity_revision"),
            payload.get("reason_code"),
        )
    if event_type == "ImageEvidenceDeclared":
        return world_id, payload.get("source_event_ref"), payload.get("source_event_payload_hash")
    if event_type == "VisualFactRecorded":
        return (
            world_id,
            payload.get("visual_fact_id"),
            payload.get("content_payload_hash"),
        )
    if event_type == "AppearanceStateRecorded":
        return (
            world_id,
            _nested(payload, "state", "appearance_state_id"),
            _nested(payload, "state", "entity_revision"),
        )
    if event_type == "VisiblePhysicalStateRecorded":
        return (
            world_id,
            _nested(payload, "state", "physical_state_id"),
            _nested(payload, "state", "entity_revision"),
        )
    if event_type == "RandomDrawRecorded":
        return world_id, payload.get("draw_id")
    if event_type == "AdvisoryAcceptanceRejected":
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("stage"),
            payload.get("failure_fingerprint"),
        )
    if event_type == "LifeAuthorDecisionRecorded":
        return world_id, payload.get("decision_id")
    if event_type == "MediaSelectionAttemptRecorded":
        return world_id, payload.get("attempt_id")
    if event_type == "MediaSelectionProposalRecorded":
        return world_id, payload.get("proposal_id")
    if event_type == "MediaOpportunityFrozen":
        return world_id, _nested(payload, "opportunity", "opportunity_id")
    if event_type == "MediaPlanRecorded":
        return (
            world_id,
            _nested(payload, "plan", "planning_request_id"),
            _nested(payload, "plan", "plan_id"),
        )
    if event_type == "MediaNotRenderableRecorded":
        return world_id, _nested(payload, "result", "planning_request_id"), "not_renderable"
    if event_type == "MediaRenderArtifactRecorded":
        return world_id, _nested(payload, "artifact", "artifact_id")
    if event_type == "MediaInspectionRecorded":
        return world_id, _nested(payload, "inspection", "inspection_id")
    if event_type == "MediaRepairAuthorized":
        return world_id, _nested(payload, "repair", "repair_attempt_id")
    if event_type == "MediaPreviewGenerated":
        return world_id, _nested(payload, "preview", "preview_id")
    if event_type == "MediaPreviewFailed":
        return world_id, payload.get("plan_id"), "preview_failed"
    if event_type == "MediaAutomaticDeliveryApproved":
        return (
            world_id,
            _nested(payload, "approval", "approval_id"),
            _nested(payload, "approval", "entity_revision"),
        )
    if event_type == "MediaDeliveryShared":
        return world_id, _nested(payload, "delivery", "delivery_id")
    if event_type == "ToolRequestAccepted":
        return world_id, _nested(payload, "request", "request_id")
    if event_type == "ToolResultAccepted":
        return world_id, _nested(payload, "result", "result_id")
    if event_type == "PerceptionRequestAccepted":
        return world_id, _nested(payload, "request", "request_id")
    if event_type == "PerceptionResultAccepted":
        return world_id, _nested(payload, "result", "result_id")
    if event_type == "MediaDeliveryThreadProposalRecorded":
        return world_id, payload.get("media_thread_proposal_id"), payload.get("change_id")
    if event_type in {"MediaDeliveryThreadOpened", "MediaDeliveryThreadUpdated"}:
        after = payload.get("thread_after")
        return world_id, _nested({"x": after}, "x", "thread_id"), payload.get("transition_id")
    if event_type == "ActorAuthorityBootstrapped":
        return world_id, payload.get("authority_id"), payload.get("transition_id")
    if event_type in {
        "ActorAuthorityRotated",
        "ActorAuthorityRevoked",
        "ActorAuthorityCompensated",
    }:
        return (
            world_id,
            payload.get("authority_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )
    if event_type in {
        "CapabilityGranted",
        "CapabilityRevised",
        "CapabilityRevoked",
        "CapabilityCompensated",
        "ConsentGranted",
        "ConsentRevised",
        "ConsentRevoked",
        "ConsentCompensated",
        "PrivacyPolicyRevised",
        "PrivacyPolicyRevoked",
        "PrivacyPolicyCompensated",
    }:
        return (
            world_id,
            payload.get("entity_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )
    if (
        event_type == "ObservationRecorded"
        and payload.get("observation_kind") == "message"
        and isinstance(payload.get("source"), str)
        and isinstance(payload.get("source_event_id"), str)
    ):
        return payload.get("source"), payload.get("source_event_id")
    if event_type == "OperatorObservationRecorded":
        return world_id, payload.get("observation_id")
    if event_type == "ActivityPlanned":
        return _nested(payload, "plan", "plan_id"), payload.get("transition_id")
    if event_type in {
        "ActivityStarted",
        "ActivityPaused",
        "ActivityResumed",
        "ActivityCompleted",
        "ActivityAbandoned",
    }:
        return payload.get("plan_id"), payload.get("transition_id")
    if event_type == "WorldOccurrenceCommitted":
        return (
            _nested(payload, "occurrence", "occurrence_id"),
            payload.get("transition_id"),
        )
    if event_type == "WorldOccurrenceActivated":
        return payload.get("occurrence_id"), payload.get("transition_id")
    if event_type == "OutcomeObservationRecorded":
        return world_id, _nested(payload, "observation", "observation_id")
    if event_type == "ActivityLifecycleProposalRecorded":
        return world_id, payload.get("proposal_id")
    if (
        event_type == "ModelResultRecorded"
        and payload.get("model_call_id") is not None
        and payload.get("model_result_ref") is not None
    ):
        return world_id, payload.get("model_call_id"), payload.get("model_result_ref")
    if (
        event_type == "ProposalRecorded"
        and payload.get("audit_contract") == "proposal-envelope-audit.1"
    ):
        return world_id, payload.get("trigger_ref"), payload.get("proposal_id")
    if (
        event_type == "ProposalRecorded"
        and payload.get("proposal_kind") == "continuation"
    ):
        return world_id, payload.get("trigger_ref"), payload.get("proposal_id")
    if event_type == "FactCommitProposalRecorded":
        return world_id, payload.get("proposal_id"), payload.get("proposal_hash")
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == "acceptance-manifest.2"
    ):
        return world_id, payload.get("manifest_version"), payload.get("acceptance_id")
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == MEDIA_CONTINUATION_ACCEPTANCE_MANIFEST_VERSION
    ):
        return world_id, payload.get("manifest_version"), payload.get("acceptance_id")
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == "acceptance-manifest.3"
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == MINIMAL_REPLY_MANIFEST_VERSION
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == APPRAISAL_ACCEPTANCE_MANIFEST_VERSION
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == AFFECT_ACCEPTANCE_MANIFEST_VERSION
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == RELATIONSHIP_ACCEPTANCE_MANIFEST_VERSION
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version")
        == RELATIONSHIP_ADJUSTMENT_ACCEPTANCE_MANIFEST_VERSION
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == OUTCOME_ACCEPTANCE_MANIFEST_VERSION
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("manifest_version") == ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION
    ):
        return (
            world_id,
            payload.get("manifest_version"),
            payload.get("acceptance_id"),
            payload.get("manifest_hash"),
        )
    if event_type == "MessagePayloadStored":
        message = payload.get("message")
        return (
            world_id,
            payload.get("acceptance_id"),
            _mapping_value(message, "payload_ref"),
            _mapping_value(message, "payload_hash"),
        )
    if event_type == "ExpressionPayloadDescriptorRecorded":
        return (
            world_id,
            payload.get("acceptance_id"),
            payload.get("payload_ref"),
            payload.get("payload_hash"),
        )
    if event_type == "ExpressionPlanAccepted":
        return (
            world_id,
            payload.get("acceptance_id"),
            payload.get("plan_id"),
            payload.get("expression_change_id"),
        )
    if event_type == "ExpressionBeatAuthorized":
        beat = payload.get("beat")
        return (
            world_id,
            payload.get("acceptance_id"),
            _mapping_value(beat, "plan_id"),
            _mapping_value(beat, "beat_id"),
            _mapping_value(_mapping_value(beat, "payload"), "payload_hash"),
        )
    if event_type == "ExpressionBeatSettled":
        return (
            world_id,
            payload.get("beat_id"),
            payload.get("receipt_id"),
            payload.get("terminal_action_state"),
        )
    if event_type == "ExpressionBeatTerminated":
        return (
            world_id,
            payload.get("beat_id"),
            payload.get("action_id"),
            payload.get("disposition"),
            payload.get("source_event_ref"),
        )
    if event_type == "ExpressionPlanCompleted":
        return (
            world_id,
            payload.get("plan_id"),
            payload.get("receipt_id"),
            payload.get("terminal_beat_id"),
        )
    if event_type == "ExpressionPlanTerminated":
        return (
            world_id,
            payload.get("plan_id"),
            payload.get("terminal_beat_id"),
            payload.get("disposition"),
            payload.get("source_event_ref"),
        )
    if event_type == "FactCommittedV2":
        return (
            world_id,
            payload.get("payload_contract"),
            payload.get("fact_id"),
            payload.get("transition_id"),
            payload.get("materialized_change_hash"),
        )
    if (
        event_type == "AcceptanceRecorded"
        and payload.get("proposal_id") is not None
        and payload.get("evaluated_world_revision") is not None
    ):
        return world_id, payload.get("proposal_id"), payload.get("evaluated_world_revision")
    if event_type == "ExperienceCommitted":
        return world_id, _nested(payload, "experience", "experience_id")
    if event_type in {"WorldOccurrenceCancelled", "WorldOccurrenceExpired"}:
        return payload.get("occurrence_id"), payload.get("transition_id")
    if event_type == "AppraisalExpired":
        return payload.get("appraisal_id"), payload.get("transition_id")
    if event_type == "AffectEpisodeDecayed":
        results = payload.get("component_results")
        config_digests = (
            tuple(item.get("config_digest") for item in results if isinstance(item, dict))
            if isinstance(results, list)
            else ()
        )
        return (
            payload.get("episode_id"),
            payload.get("expected_entity_revision"),
            payload.get("to_logical_time"),
            config_digests,
        )
    if event_type == "ThreadExpired":
        return (
            world_id,
            _nested(payload, "thread_after", "thread_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )
    if event_type in {"PrivateCommitmentDue", "PrivateCommitmentDeadlineBroken"}:
        return (
            world_id,
            _nested(payload, "commitment_after", "commitment_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )
    if event_type == "V2GoalExpired":
        return (
            world_id,
            payload.get("operation"),
            _nested(payload, "goal_after", "goal_id"),
            payload.get("expected_entity_revision"),
            _nested(payload, "cause_authority", "clock_event_ref"),
            payload.get("policy_digest"),
        )
    if event_type == "TriggerProcessOpened":
        return world_id, _nested(payload, "process", "trigger_id"), "opened"
    if event_type in {"TriggerProcessClaimed", "TriggerProcessReclaimed"}:
        process = payload.get("process")
        if isinstance(process, dict) and process.get("process_kind") in {
            "npc_world_appraisal",
            "interaction_appraisal",
            "silence_appraisal",
            "plan_disruption_appraisal",
            "interaction_fact",
            "private_impression_deliberation",
            "affect_deliberation",
            "relationship_deliberation",
            "relationship_adjustment",
            "outcome_deliberation",
            "media_delivery_interaction",
            "expression_reconsideration",
            "external_result_deliberation",
            "life_ecology",
            "social_action_deliberation",
            "memory_candidate_review",
        }:
            attempts = process.get("attempt_ids")
            attempt_id = attempts[-1] if isinstance(attempts, list) and attempts else None
            return world_id, process.get("trigger_id"), attempt_id, event_type
    return None


def _nested(payload: dict[str, Any], parent: str, child: str) -> object:
    value = payload.get(parent)
    if not isinstance(value, dict):
        return None
    return value.get(child)


def _mapping_value(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, dict) else None
