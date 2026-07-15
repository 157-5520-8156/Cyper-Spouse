"""Cross-event invariants that must hold inside one atomic ledger commit."""

from __future__ import annotations

from collections.abc import Sequence
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
from .experience_events import ExperienceCommittedPayload
from .fact_accepted_contracts import (
    fact_commit_event_payload_hash,
    rehydrate_fact_commit_materialized_v2_json,
)
from .life_events import WorldOccurrenceSettledPayload
from .proposal_audit_schemas import (
    ModelResultRecordedPayload,
    ProposalRecordedV2Payload,
)
from .acceptance_manifest import parse_acceptance_manifest_v2
from .minimal_reply_events import (
    ExpressionBeatAuthorizedPayload,
    ExpressionBeatSettledPayload,
    ExpressionPlanAcceptedPayload,
    ExpressionPlanCompletedPayload,
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
from .appraisal_events import (
    AppraisalAcceptedPayload,
    AppraisalContradictedPayload,
    AppraisalSupersededPayload,
)
from .affect_events import AFFECT_PAYLOAD_MODELS, AffectAuthorizedMutationPayload
from .media_v2 import (
    MediaPlanRecordedPayload,
    MediaRepairAuthorizedPayload,
    continuation_trigger_id,
)
from .schemas import (
    Action,
    BudgetReservation,
    ExperienceOccurrenceSettlementBinding,
    TriggerProcess,
    WorldEvent,
)
from .typed_proposal_families import (
    family_for_mutation,
    family_for_record,
)


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
        reject_outcome_acceptance_manifest_without_recorder(events)
        reject_interaction_bid_acceptance_manifest_without_recorder(events)
        reject_media_thread_acceptance_manifest_without_recorder(events)
        reject_expression_plan_manifest_without_recorder(events)
    _validate_deliberation_audit_transaction(events)
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
    _validate_expression_receipt_lifecycle_batch(events)
    _validate_media_planning_settlement_batch(events)
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
        if acceptance.get("manifest_version") == MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION:
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
    settlement_trigger_refs = [item.appraisal_trigger_ref for item in settlements]
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

    settlement_pairs = {(item.occurrence_id, item.result_id) for item in settlements}
    for experience in experiences:
        for binding in experience.experience.values.source_bindings:
            if (
                isinstance(binding, ExperienceOccurrenceSettlementBinding)
                and (
                    binding.occurrence_id,
                    binding.result_id,
                )
                not in settlement_pairs
            ):
                raise ValueError("occurrence-backed experience must accompany its settlement")

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


def _validate_deliberation_audit_transaction(events: Sequence[WorldEvent]) -> None:
    """Keep Phase-4A provider lineage and its optional Proposal indivisible."""

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
        if v2_proposal_indexes:
            raise ValueError("ProposalRecorded v2 requires its complete model audit transaction")
        return
    if model_indexes[0] != 0:
        raise ValueError("model audit transaction must start the commit")

    first = ModelResultRecordedPayload.model_validate_json(events[0].payload_json)
    expected_model_indexes = list(range(first.attempt_count))
    if model_indexes != expected_model_indexes:
        raise ValueError("model attempts must be complete and contiguous in one commit")
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

    if first.proposal_hash is None:
        if len(events) != first.attempt_count or v2_proposal_indexes:
            raise ValueError("failed recovery audit transaction cannot contain a Proposal")
        return

    proposal_index = first.attempt_count
    if len(events) != proposal_index + 1 or v2_proposal_indexes != [proposal_index]:
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
            OUTCOME_ACCEPTANCE_MANIFEST_VERSION,
            INTERACTION_BID_ACCEPTANCE_MANIFEST_VERSION,
            MEDIA_THREAD_ACCEPTANCE_MANIFEST_VERSION,
            EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION,
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
        process = TriggerProcess.model_validate(events[index + 1].payload().get("process"))
        expected = continuation_trigger_id(result.plan)
        if (
            process.trigger_id != expected
            or process.trigger_ref != expected
            or process.process_kind != "media_continuation"
            or process.state != "open"
            or process.source_evidence_ref != result_event.event_id
        ):
            raise ValueError("media planning continuation is not bound to frozen plan")


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
