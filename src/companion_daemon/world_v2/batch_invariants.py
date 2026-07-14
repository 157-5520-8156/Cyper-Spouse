"""Cross-event invariants that must hold inside one atomic ledger commit."""

from __future__ import annotations

from collections.abc import Sequence

from .life_events import ExperienceCommittedPayload, WorldOccurrenceSettledPayload
from .schemas import WorldEvent


def validate_commit_batch(
    events: Sequence[WorldEvent], *, expected_world_revision: int
) -> None:
    """Require every settled lived-world occurrence to schedule its appraisal."""

    appraisal_triggers: dict[str, list[tuple[str, str]]] = {}
    experiences: list[ExperienceCommittedPayload] = []
    settlements = [
        WorldOccurrenceSettledPayload.model_validate_json(event.payload_json)
        for event in events
        if event.event_type == "WorldOccurrenceSettled"
    ]
    acceptances = [
        event.payload()
        for event in events
        if event.event_type == "AcceptanceRecorded"
    ]
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
                    (str(process.get("trigger_id")), trigger_ref)
                )

    for settlement in settlements:
        matching_acceptances = [
            acceptance
            for acceptance in acceptances
            if acceptance.get("status") == "accepted"
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
            (expected_trigger_id, expected_trigger_id)
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
