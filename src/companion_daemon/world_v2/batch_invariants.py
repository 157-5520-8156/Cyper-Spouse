"""Cross-event invariants that must hold inside one atomic ledger commit."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json

from .experience_events import ExperienceCommittedPayload
from .life_events import WorldOccurrenceSettledPayload
from .proposal_audit_schemas import (
    ModelResultRecordedPayload,
    ProposalRecordedV2Payload,
)
from .acceptance_manifest import parse_acceptance_manifest_v2
from .appraisal_events import (
    AppraisalAcceptedPayload,
    AppraisalContradictedPayload,
    AppraisalSupersededPayload,
)
from .schemas import ExperienceOccurrenceSettlementBinding, WorldEvent
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

    if not accepted_manifest_v3_authorized:
        reject_accepted_manifest_v3_without_recorder(events)
    _validate_deliberation_audit_transaction(events)
    _validate_acceptance_manifest_v2_batch(events)

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
            and binding.evaluated_world_revision
            == acceptance.get("evaluated_world_revision")
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
            if isinstance(binding, ExperienceOccurrenceSettlementBinding) and (
                binding.occurrence_id,
                binding.result_id,
            ) not in settlement_pairs:
                raise ValueError("occurrence-backed experience must accompany its settlement")

    for mutation_index, binding in typed_mutations:
        matching = [
            acceptance
            for acceptance_index, acceptance in acceptances
            if acceptance_index == mutation_index - 1
            and acceptance.get("status") == "accepted"
            and acceptance.get("acceptance_id") == binding.acceptance_id
            and acceptance.get("proposal_id") == binding.proposal_id
            and acceptance.get("evaluated_world_revision")
            == binding.evaluated_world_revision
            and acceptance.get("accepted_change_id") == binding.change_id
            and acceptance.get("accepted_change_hash") == binding.accepted_change_hash
        ]
        if binding.evaluated_world_revision != expected_world_revision or len(matching) != 1:
            raise ValueError(
                "typed proposal mutation requires one adjacent revision-pinned "
                "AcceptanceRecorded"
            )


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
        and event.payload().get("manifest_version") != "acceptance-manifest.2"
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


def appraisal_trigger_identity(occurrence_id: str, result_id: str) -> str:
    return f"appraisal:{occurrence_id}:{result_id}"


def interaction_appraisal_trigger_identity(world_id: str, observation_ref: str) -> str:
    encoded = json.dumps(
        [world_id, observation_ref, "interaction_appraisal"],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"appraisal:interaction:{hashlib.sha256(encoded).hexdigest()}"
