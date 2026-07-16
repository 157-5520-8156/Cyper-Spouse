"""Compile an opaque activity choice into a source-bound lifecycle proposal.

This is deliberately a pure authority module.  The model chooses an opaque
catalog token, while this compiler re-resolves that token at one pinned
projection and derives every authority-bearing field itself.  It has no
ledger-write capability and cannot turn a no-op into an activity transition.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .activity_lifecycle_draft import ActivityLifecycleModelDraft
from .life_ecology_activity import (
    ActivityOpeningCatalog,
    ActivityOpeningOperation,
)
from .life_ecology_contract import life_ecology_trigger_id
from .plan_evidence import canonical_plan_evidence_hash
from .schema_core import EvidenceRef, FrozenModel
from .schemas import LedgerProjection


ACTIVITY_LIFECYCLE_PROPOSAL_POLICY_VERSION = "activity-lifecycle-proposal.1"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


ACTIVITY_LIFECYCLE_PROPOSAL_POLICY_DIGEST = _digest(
    {
        "contract": ACTIVITY_LIFECYCLE_PROPOSAL_POLICY_VERSION,
        "model_can_select": "one_preoffered_opaque_token_or_no_op",
        "source_trigger": "claimed_life_ecology",
        "evidence": ("active_plan", "committed_world_event:clock_wake"),
        "forbidden_evidence": "observed_message",
    }
)

ActivityLifecycleEffectEventType = Literal[
    "ActivityStarted", "ActivityPaused", "ActivityResumed", "ActivityCompleted", "ActivityAbandoned"
]

_EFFECT_BY_OPERATION: dict[ActivityOpeningOperation, ActivityLifecycleEffectEventType] = {
    "start": "ActivityStarted",
    "pause": "ActivityPaused",
    "resume": "ActivityResumed",
    "complete": "ActivityCompleted",
    "abandon": "ActivityAbandoned",
}


class ActivityLifecycleProposalError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"activity_lifecycle_proposal.{code}"
        super().__init__(self.code)


def activity_lifecycle_mutation_hash(
    *,
    change_id: str,
    plan_id: str,
    expected_plan_revision: int,
    operation: ActivityOpeningOperation,
    evaluated_world_revision: int,
    wake_event_ref: str,
    wake_event_payload_hash: str,
    catalog_version: str,
    catalog_hash: str,
    opening_token: str,
) -> str:
    """Hash the only mutation Acceptance may later materialize."""

    return _digest(
        {
            "catalog_hash": catalog_hash,
            "catalog_version": catalog_version,
            "change_id": change_id,
            "evaluated_world_revision": evaluated_world_revision,
            "expected_plan_revision": expected_plan_revision,
            "opening_token": opening_token,
            "operation": operation,
            "plan_id": plan_id,
            "wake_event_payload_hash": wake_event_payload_hash,
            "wake_event_ref": wake_event_ref,
        }
    )


class ActivityLifecycleProposal(FrozenModel):
    """All authority facts derived from one audited opaque model choice."""

    proposal_id: str = Field(min_length=1, max_length=256)
    change_id: str = Field(min_length=1, max_length=256)
    transition_id: str = Field(min_length=1, max_length=256)
    evaluated_world_revision: int = Field(ge=0)
    ecology_trigger_id: str = Field(min_length=1, max_length=256)
    wake_event_ref: str = Field(min_length=1, max_length=512)
    wake_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=128)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    opening_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1, max_length=512)
    expected_plan_revision: int = Field(ge=1)
    operation: ActivityOpeningOperation
    effect_event_type: ActivityLifecycleEffectEventType
    proposed_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=2, max_length=2)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=256)
    raw_output_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    normalized_output_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def fields_are_closed(self) -> "ActivityLifecycleProposal":
        if self.effect_event_type != _EFFECT_BY_OPERATION[self.operation]:
            raise ValueError("activity lifecycle effect type does not match operation")
        expected_hash = activity_lifecycle_mutation_hash(
            change_id=self.change_id,
            plan_id=self.plan_id,
            expected_plan_revision=self.expected_plan_revision,
            operation=self.operation,
            evaluated_world_revision=self.evaluated_world_revision,
            wake_event_ref=self.wake_event_ref,
            wake_event_payload_hash=self.wake_event_payload_hash,
            catalog_version=self.catalog_version,
            catalog_hash=self.catalog_hash,
            opening_token=self.opening_token,
        )
        if self.proposed_change_hash != expected_hash:
            raise ValueError("activity lifecycle proposed change hash is invalid")
        evidence_types = tuple(item.evidence_type for item in self.evidence_refs)
        if evidence_types != ("active_plan", "committed_world_event"):
            raise ValueError("activity lifecycle evidence must be canonical plan then clock wake")
        if any(item.evidence_type == "observed_message" for item in self.evidence_refs):
            raise ValueError("activity lifecycle proposal cannot use observed messages")
        return self


class ActivityLifecycleProposalCompiler:
    """Re-resolve a model draft without granting it plan or ledger authority."""

    def __init__(self, *, catalog: ActivityOpeningCatalog, ecology_catalog_version: str) -> None:
        if not ecology_catalog_version:
            raise ValueError("activity lifecycle proposal needs ecology catalog version")
        self._catalog = catalog
        self._ecology_catalog_version = ecology_catalog_version

    def compile(
        self,
        *,
        projection: LedgerProjection,
        wake_event_ref: str,
        ecology_trigger_id: str,
        draft: ActivityLifecycleModelDraft,
    ) -> ActivityLifecycleProposal | None:
        """Return a closed proposal, or ``None`` only for a genuine no-op."""

        if draft.decision == "no_op":
            if draft.model is not None:
                # A model-declared no-op is a valid quiet outcome.  It is not
                # a proposal and therefore cannot be accepted as an effect.
                return None
            return None
        if draft.opening_token is None or draft.model is None:
            raise ActivityLifecycleProposalError("selected_draft_not_audited")
        if draft.raw_output_hash is None or draft.normalized_output_hash is None:
            raise ActivityLifecycleProposalError("selected_draft_not_audited")
        resolved = self._catalog.resolve_opening(
            projection=projection,
            wake_event_ref=wake_event_ref,
            opening_token=draft.opening_token,
        )
        if resolved is None:
            raise ActivityLifecycleProposalError("opening_token_not_current")
        wake = self._wake(projection=projection, wake_event_ref=wake_event_ref)
        self._claimed_ecology_trigger(
            projection=projection,
            ecology_trigger_id=ecology_trigger_id,
            wake_event_ref=wake_event_ref,
        )
        plan = next((item for item in projection.plans if item.plan_id == resolved.plan_id), None)
        if plan is None or plan.entity_revision != resolved.plan_revision:
            raise ActivityLifecycleProposalError("plan_not_current")
        identity = _digest(
            {
                "catalog_hash": resolved.catalog_hash,
                "ecology_trigger_id": ecology_trigger_id,
                "opening_token": resolved.opening_token,
                "wake_event_ref": wake_event_ref,
                "world_id": projection.world_id,
            }
        )
        change_id = f"change:activity-lifecycle:{identity}"
        evidence = (
            EvidenceRef(
                ref_id=plan.plan_id,
                evidence_type="active_plan",
                claim_purpose="life_transition",
                immutable_hash=canonical_plan_evidence_hash(plan),
            ),
            EvidenceRef(
                ref_id=wake.event_id,
                evidence_type="committed_world_event",
                claim_purpose="life_transition",
                source_world_revision=wake.world_revision,
                immutable_hash=wake.payload_hash,
            ),
        )
        return ActivityLifecycleProposal(
            proposal_id=f"proposal:activity-lifecycle:{identity}",
            change_id=change_id,
            transition_id=f"transition:activity-lifecycle:{identity}",
            evaluated_world_revision=projection.world_revision,
            ecology_trigger_id=ecology_trigger_id,
            wake_event_ref=wake.event_id,
            wake_event_payload_hash=wake.payload_hash,
            catalog_version=resolved.catalog_version,
            catalog_hash=resolved.catalog_hash,
            opening_token=resolved.opening_token,
            plan_id=resolved.plan_id,
            expected_plan_revision=resolved.plan_revision,
            operation=resolved.operation,
            effect_event_type=_EFFECT_BY_OPERATION[resolved.operation],
            proposed_change_hash=activity_lifecycle_mutation_hash(
                change_id=change_id,
                plan_id=resolved.plan_id,
                expected_plan_revision=resolved.plan_revision,
                operation=resolved.operation,
                evaluated_world_revision=projection.world_revision,
                wake_event_ref=wake.event_id,
                wake_event_payload_hash=wake.payload_hash,
                catalog_version=resolved.catalog_version,
                catalog_hash=resolved.catalog_hash,
                opening_token=resolved.opening_token,
            ),
            evidence_refs=evidence,
            policy_digest=ACTIVITY_LIFECYCLE_PROPOSAL_POLICY_DIGEST,
            model=draft.model,
            raw_output_hash=draft.raw_output_hash,
            normalized_output_hash=draft.normalized_output_hash,
        )

    def _wake(self, *, projection: LedgerProjection, wake_event_ref: str):
        wake = next(
            (item for item in projection.committed_world_event_refs if item.event_id == wake_event_ref),
            None,
        )
        if (
            wake is None
            or projection.logical_time is None
            or wake.event_type != "ClockAdvanced"
            or wake.logical_time > projection.logical_time
        ):
            raise ActivityLifecycleProposalError("wake_not_current_clock_authority")
        return wake

    def _claimed_ecology_trigger(
        self, *, projection: LedgerProjection, ecology_trigger_id: str, wake_event_ref: str
    ) -> None:
        expected_id = life_ecology_trigger_id(
            world_id=projection.world_id,
            wake_event_ref=wake_event_ref,
            catalog_version=self._ecology_catalog_version,
        )
        trigger = next(
            (item for item in projection.trigger_processes if item.trigger_id == ecology_trigger_id),
            None,
        )
        if (
            ecology_trigger_id != expected_id
            or trigger is None
            or trigger.process_kind != "life_ecology"
            or trigger.state != "claimed"
            or trigger.claim_lease is None
            or trigger.source_evidence_ref != wake_event_ref
        ):
            raise ActivityLifecycleProposalError("ecology_trigger_not_claimed")


__all__ = [
    "ACTIVITY_LIFECYCLE_PROPOSAL_POLICY_DIGEST",
    "ACTIVITY_LIFECYCLE_PROPOSAL_POLICY_VERSION",
    "ActivityLifecycleEffectEventType",
    "ActivityLifecycleProposal",
    "ActivityLifecycleProposalCompiler",
    "ActivityLifecycleProposalError",
    "activity_lifecycle_mutation_hash",
]
