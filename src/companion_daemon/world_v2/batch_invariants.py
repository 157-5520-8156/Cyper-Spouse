"""Cross-event invariants that must hold inside one atomic ledger commit."""

from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json

from .life_events import ExperienceCommittedPayload, WorldOccurrenceSettledPayload
from .appraisal_events import (
    AppraisalAcceptedPayload,
    AppraisalContradictedPayload,
    AppraisalSupersededPayload,
)
from .schemas import AppraisalProposalProjection, WorldEvent


def validate_commit_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int
) -> None:
    """Require every settled lived-world occurrence to schedule its appraisal."""

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
    appraisal_proposals: list[AppraisalProposalProjection] = []
    for event in events:
        if (
            event.event_type == "ProposalRecorded"
            and event.payload().get("proposal_kind") == "appraisal_transition"
        ):
            proposal = AppraisalProposalProjection.model_validate_json(
                event.payload_json
            )
            appraisal_proposals.append(proposal)
            if proposal.evaluated_world_revision != expected_world_revision:
                raise ValueError(
                    "appraisal proposal must be pinned to the current world revision"
                )
    if appraisal_proposals and any(
        event.event_type != "ProposalRecorded" for event in events
    ):
        raise ValueError(
            "appraisal ProposalRecorded requires a separate deliberation commit"
        )
    authorized_appraisal_models = {
        "AppraisalAccepted": AppraisalAcceptedPayload,
        "AppraisalContradicted": AppraisalContradictedPayload,
        "AppraisalSuperseded": AppraisalSupersededPayload,
    }
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
            and acceptance.get("evaluated_world_revision")
            == appraisal.evaluated_world_revision
            and acceptance.get("accepted_change_id") == appraisal.change_id
            and acceptance.get("accepted_change_hash")
            == appraisal.accepted_change_hash
        ]
        if appraisal.evaluated_world_revision != expected_world_revision or len(matching) != 1:
            raise ValueError(
                "AppraisalAccepted requires one revision-pinned AcceptanceRecorded"
            )
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
            and item.payload().get("runtime_outcome_ref")
            == outcome_ref
        ]
        if len(completions) != 1:
            raise ValueError(
                "AppraisalAccepted must complete its trigger in the same commit"
            )
    for acceptance_index, acceptance in acceptances:
        if (
            acceptance.get("status") != "accepted"
            or not isinstance(acceptance.get("proposal_id"), str)
        ):
            continue
        matching_appraisal_mutations: list[int] = []
        for mutation_index, event in enumerate(events):
            model = authorized_appraisal_models.get(event.event_type)
            if model is None or mutation_index <= acceptance_index:
                continue
            mutation = model.model_validate_json(event.payload_json)
            if (
                mutation.proposal_id == acceptance.get("proposal_id")
                and mutation.acceptance_id == acceptance.get("acceptance_id")
                and mutation.change_id == acceptance.get("accepted_change_id")
                and mutation.accepted_change_hash
                == acceptance.get("accepted_change_hash")
            ):
                matching_appraisal_mutations.append(mutation_index)
        matching_settlements = [
            settlement_index
            for settlement_index, _, settlement in settlement_events
            if settlement_index > acceptance_index
            and settlement.outcome_proposal_id == acceptance.get("proposal_id")
            and settlement.acceptance_id == acceptance.get("acceptance_id")
            and settlement.change_id == acceptance.get("accepted_change_id")
            and settlement.accepted_change_hash
            == acceptance.get("accepted_change_hash")
        ]
        matching_domain_mutations = [
            *matching_appraisal_mutations,
            *matching_settlements,
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
            experiences.append(
                ExperienceCommittedPayload.model_validate_json(event.payload_json)
            )
        if event.event_type != "TriggerProcessOpened":
            continue
        process = event.payload().get("process")
        if not isinstance(process, dict):
            continue
        if (
            process.get("process_kind") == "npc_world_appraisal"
            and process.get("state") == "open"
        ):
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
            and acceptance.get("evaluated_world_revision")
            == settlement.evaluated_world_revision
            and acceptance.get("accepted_change_id") == settlement.change_id
            and acceptance.get("accepted_change_hash")
            == settlement.accepted_change_hash
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
            if settlement.occurrence_id in item.experience.occurrence_refs
            and settlement.result_id in item.experience.result_refs
        ]
        if len(matching_experiences) > 1:
            raise ValueError(
                "WorldOccurrenceSettled permits at most one matching committed "
                "experience"
            )

    settlement_pairs = {
        (item.occurrence_id, item.result_id) for item in settlements
    }
    for experience in experiences:
        for occurrence_id in experience.experience.occurrence_refs:
            if not any(
                occurrence_id == candidate_occurrence
                and result_id in experience.experience.result_refs
                for candidate_occurrence, result_id in settlement_pairs
            ):
                raise ValueError(
                    "occurrence-backed experience must accompany its settlement"
                )


def appraisal_trigger_identity(occurrence_id: str, result_id: str) -> str:
    return f"appraisal:{occurrence_id}:{result_id}"


def interaction_appraisal_trigger_identity(world_id: str, observation_ref: str) -> str:
    encoded = json.dumps(
        [world_id, observation_ref, "interaction_appraisal"],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"appraisal:interaction:{hashlib.sha256(encoded).hexdigest()}"
