"""Cycle-free persisted contract for an activity-lifecycle proposal."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import EvidenceRef, FrozenModel


ActivityLifecycleOperation = Literal["start", "pause", "resume", "complete", "abandon"]
ActivityLifecycleEffectEventType = Literal[
    "ActivityStarted", "ActivityPaused", "ActivityResumed", "ActivityCompleted", "ActivityAbandoned"
]

EFFECT_BY_ACTIVITY_OPERATION: dict[ActivityLifecycleOperation, ActivityLifecycleEffectEventType] = {
    "start": "ActivityStarted",
    "pause": "ActivityPaused",
    "resume": "ActivityResumed",
    "complete": "ActivityCompleted",
    "abandon": "ActivityAbandoned",
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def activity_lifecycle_mutation_hash(
    *,
    change_id: str,
    plan_id: str,
    expected_plan_revision: int,
    operation: ActivityLifecycleOperation,
    evaluated_world_revision: int,
    evaluated_deliberation_revision: int,
    evaluated_ledger_sequence: int,
    wake_event_ref: str,
    wake_event_payload_hash: str,
    catalog_version: str,
    catalog_hash: str,
    opening_token: str,
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "catalog_hash": catalog_hash,
                "catalog_version": catalog_version,
                "change_id": change_id,
                "evaluated_deliberation_revision": evaluated_deliberation_revision,
                "evaluated_ledger_sequence": evaluated_ledger_sequence,
                "evaluated_world_revision": evaluated_world_revision,
                "expected_plan_revision": expected_plan_revision,
                "opening_token": opening_token,
                "operation": operation,
                "plan_id": plan_id,
                "wake_event_payload_hash": wake_event_payload_hash,
                "wake_event_ref": wake_event_ref,
            }
        ).encode("utf-8")
    ).hexdigest()


class ActivityLifecycleProposalRecordedPayload(FrozenModel):
    proposal_id: str = Field(min_length=1, max_length=256)
    change_id: str = Field(min_length=1, max_length=256)
    transition_id: str = Field(min_length=1, max_length=256)
    evaluated_world_revision: int = Field(ge=0)
    evaluated_deliberation_revision: int = Field(ge=0)
    evaluated_ledger_sequence: int = Field(ge=0)
    ecology_trigger_id: str = Field(min_length=1, max_length=256)
    wake_event_ref: str = Field(min_length=1, max_length=512)
    wake_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=128)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    opening_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1, max_length=512)
    expected_plan_revision: int = Field(ge=1)
    operation: ActivityLifecycleOperation
    effect_event_type: ActivityLifecycleEffectEventType
    proposed_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=2, max_length=2)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=256)
    raw_output_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    normalized_output_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def fields_are_closed(self) -> "ActivityLifecycleProposalRecordedPayload":
        if self.effect_event_type != EFFECT_BY_ACTIVITY_OPERATION[self.operation]:
            raise ValueError("activity lifecycle effect type does not match operation")
        if self.proposed_change_hash != activity_lifecycle_mutation_hash(
            change_id=self.change_id,
            plan_id=self.plan_id,
            expected_plan_revision=self.expected_plan_revision,
            operation=self.operation,
            evaluated_world_revision=self.evaluated_world_revision,
            evaluated_deliberation_revision=self.evaluated_deliberation_revision,
            evaluated_ledger_sequence=self.evaluated_ledger_sequence,
            wake_event_ref=self.wake_event_ref,
            wake_event_payload_hash=self.wake_event_payload_hash,
            catalog_version=self.catalog_version,
            catalog_hash=self.catalog_hash,
            opening_token=self.opening_token,
        ):
            raise ValueError("activity lifecycle proposed change hash is invalid")
        if tuple(item.evidence_type for item in self.evidence_refs) != (
            "active_plan",
            "committed_world_event",
        ):
            raise ValueError("activity lifecycle evidence must be canonical plan then clock wake")
        return self


__all__ = [
    "ActivityLifecycleEffectEventType",
    "ActivityLifecycleOperation",
    "ActivityLifecycleProposalRecordedPayload",
    "EFFECT_BY_ACTIVITY_OPERATION",
    "activity_lifecycle_mutation_hash",
]
