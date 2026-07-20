"""Source-bound compiler and closed manifest for deferred social actions."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from pydantic import Field, model_validator

from .commitment_events import CommitmentChangedPayload, commitment_mutation_hash
from .expression_plan_acceptance import (
    ExpressionPlanAcceptanceMaterial,
    ExpressionPlanBudgetPolicy,
    derive_expression_plan_material,
)
from .expression_plan_manifest import (
    ExpressionPlanAcceptanceManifest,
    build_expression_plan_manifest,
    canonical_expression_plan_value_hash,
)
from .proposal_audit_schemas import ProposalAuditProjection
from .schema_core import FrozenModel
from .schemas import (
    BudgetAccount,
    CommitmentFulfillmentContract,
    CommitmentOrigin,
    CommitmentProjection,
    CommitmentValues,
    EvidenceRef,
    MessageObservationRef,
    ProjectionCursor,
    commitment_semantic_fingerprint,
)
from .thread_events import ThreadChangedPayload


SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION = "social-deferred-acceptance.1"
SOCIAL_DEFERRED_POLICY_VERSION = "social-deferred-policy.1"
_POLICY_REFS = ("policy:commitment-v1",)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def social_deferred_commitment_event_id(*, world_id: str, acceptance_id: str) -> str:
    return "event:social-deferred:commitment:" + _digest(
        {"contract": "social-deferred-commitment-event.1", "world_id": world_id, "acceptance_id": acceptance_id}
    )


class SocialDeferredPolicy(FrozenModel):
    expression: ExpressionPlanBudgetPolicy
    importance_bp: int = Field(default=5_000, ge=0, le=10_000)
    persistence_level: str = Field(default="session", min_length=1, max_length=128)
    policy_version: str = SOCIAL_DEFERRED_POLICY_VERSION

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class SocialDeferredAcceptanceMaterial(FrozenModel):
    acceptance_id: str = Field(min_length=1, max_length=256)
    expression: ExpressionPlanAcceptanceMaterial
    commitment_payload: CommitmentChangedPayload
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_observation_id: str = Field(min_length=1, max_length=512)
    source_observation_event_ref: str = Field(min_length=1, max_length=512)
    source_observation_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    thread_payload: ThreadChangedPayload

    @model_validator(mode="after")
    def effects_are_one_closed_defer(self) -> "SocialDeferredAcceptanceMaterial":
        if len(self.expression.beats) != 1:
            raise ValueError("social defer requires exactly one expression beat")
        action = self.expression.beats[0].action
        commitment = self.commitment_payload.commitment_after
        contract = commitment.values.fulfillment_contract
        thread = self.thread_payload.thread_after
        if (
            action.kind != "followup"
            or action.not_before is None
            or action.expires_at is None
            or action.not_before != commitment.values.due_window.opens_at
            or action.expires_at != commitment.values.due_window.closes_at
            or contract.expected_action_id != action.action_id
            or contract.expected_action_payload_hash != action.payload_hash
            or self.commitment_payload.acceptance_id != self.acceptance_id
            or self.commitment_payload.proposal_id != self.expression.proposal_id
            or commitment.values.subject_ref != self.source_observation_id
            or commitment.values.anchor_evidence_refs[0].ref_id != self.source_observation_id
            or commitment.values.anchor_evidence_refs[0].immutable_hash
            != self.source_observation_event_hash
            or self.thread_payload.operation != "open"
            or thread.values.kind != "reply_reconsideration"
            or thread.values.subject_ref != self.source_observation_id
            or thread.values.due_window != commitment.values.due_window
            or thread.values.anchor_evidence_refs != commitment.values.anchor_evidence_refs
        ):
            raise ValueError("social defer commitment does not bind its followup Action")
        return self


class SocialDeferredAcceptanceManifest(FrozenModel):
    manifest_version: str = SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    status: str = "accepted"
    accepted_change_id: str = Field(min_length=1, max_length=256)
    accepted_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    source_observation_id: str = Field(min_length=1, max_length=512)
    source_observation_event_ref: str = Field(min_length=1, max_length=512)
    source_observation_event_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expression_manifest: ExpressionPlanAcceptanceManifest
    commitment_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    commitment_id: str = Field(min_length=1, max_length=512)
    thread_proposal_id: str = Field(min_length=1, max_length=512)
    thread_id: str = Field(min_length=1, max_length=512)
    thread_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action_id: str = Field(min_length=1, max_length=512)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def manifest_is_self_bound(self) -> "SocialDeferredAcceptanceManifest":
        expression = self.expression_manifest
        if (
            self.manifest_version != SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION
            or expression.acceptance_id != self.acceptance_id
            or expression.proposal_id != self.proposal_id
            or expression.proposal_event_ref != self.proposal_event_ref
            or expression.proposal_event_payload_hash != self.proposal_event_payload_hash
            or expression.evaluated_world_revision != self.evaluated_world_revision
            or len(expression.beats) != 1
            or expression.beats[0].action.action_id != self.action_id
            or expression.beats[0].action.kind != "followup"
            or self.status != "accepted"
            or not self.thread_proposal_id.startswith("proposal:deferred-thread:")
            or not self.thread_id.startswith("thread:reply-reconsideration:")
            or self.manifest_hash != social_deferred_manifest_hash(self.model_dump(mode="json"))
        ):
            raise ValueError("social deferred manifest is not closed")
        return self


def social_deferred_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION)
    return _digest(material)


def derive_social_deferred_material(
    *,
    acceptance_id: str,
    audit: ProposalAuditProjection,
    cursor: ProjectionCursor,
    world_id: str,
    policy: SocialDeferredPolicy,
    account: BudgetAccount,
    source_observation: MessageObservationRef,
    source_observation_event_ref: str,
    logical_time: datetime,
    created_at: datetime,
    trace_id: str,
    correlation_id: str,
    thread_payload: ThreadChangedPayload,
) -> SocialDeferredAcceptanceMaterial:
    if audit.trigger_ref != source_observation_event_ref:
        raise ValueError("social deferred source event does not match proposal trigger")
    expression = derive_expression_plan_material(
        audit=audit,
        cursor=cursor,
        world_id=world_id,
        policy=policy.expression,
        account=account,
        logical_time=logical_time,
        created_at=created_at,
        trace_id=trace_id,
        correlation_id=correlation_id,
    )
    if len(expression.beats) != 1 or expression.beats[0].action.kind != "followup":
        raise ValueError("social deferred acceptance requires one followup proposal")
    item = expression.beats[0]
    action = item.action
    if action.not_before is None or action.expires_at is None or logical_time >= action.expires_at:
        raise ValueError("social deferred acceptance requires a live delayed window")
    evidence = EvidenceRef(
        ref_id=source_observation.observation_id,
        evidence_type="observed_message",
        claim_purpose="conversation_continuity",
        source_world_revision=source_observation.world_revision,
        immutable_hash=source_observation.event_payload_hash,
    )
    root = {"contract": "social-deferred-acceptance.1", "world_id": world_id, "proposal_id": audit.proposal_id,
            "proposal_hash": audit.proposal_hash, "action_id": action.action_id, "policy_digest": policy.digest}
    commitment_id = "commitment:social-deferred:" + _digest(root)
    change_id = "change:social-deferred:" + _digest({**root, "role": "change"})
    transition_id = "transition:social-deferred:" + _digest({**root, "role": "transition"})
    commitment_event_id = social_deferred_commitment_event_id(world_id=world_id, acceptance_id=acceptance_id)
    contract = CommitmentFulfillmentContract(
        contract_kind="execution_receipt",
        evidence_type="settled_external_result",
        expected_action_id=action.action_id,
        expected_action_payload_hash=action.payload_hash,
        expected_result_status="delivered",
        contract_version="commitment-fulfillment-contract.1",
    )
    values = CommitmentValues(
        subject_ref=source_observation.observation_id,
        content_ref=item.beat.payload.payload_ref,
        content_hash=item.beat.payload.payload_hash.removeprefix("sha256:"),
        anchor_evidence_refs=(evidence,),
        source_evidence_refs=(evidence,),
        importance_bp=policy.importance_bp,
        due_window={"opens_at": action.not_before, "closes_at": action.expires_at},
        persistence_level=policy.persistence_level,
        fulfillment_contract=contract,
        privacy_class="private",
        status="open",
    )
    after = CommitmentProjection(
        commitment_id=commitment_id,
        entity_revision=1,
        semantic_fingerprint=commitment_semantic_fingerprint(
            owner_ref=values.owner_ref,
            subject_ref=source_observation.observation_id,
            content_ref=item.beat.payload.payload_ref,
            content_hash=item.beat.payload.payload_hash.removeprefix("sha256:"),
            anchor_evidence_refs=(evidence,),
            fulfillment_contract=contract,
            policy_refs=_POLICY_REFS,
        ),
        values=values,
        origin=CommitmentOrigin(
            change_id=change_id,
            transition_id=transition_id,
            policy_refs=_POLICY_REFS,
            accepted_event_ref=commitment_event_id,
        ),
        opened_at=logical_time,
        updated_at=logical_time,
    )
    raw = {
        "change_id": change_id,
        "transition_id": transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": (evidence,),
        "policy_refs": _POLICY_REFS,
        "acceptance_id": acceptance_id,
        "proposal_id": expression.proposal_id,
        "evaluated_world_revision": cursor.world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": "open",
        "commitment_before": None,
        "commitment_after": after,
    }
    raw["accepted_change_hash"] = commitment_mutation_hash(raw)
    return SocialDeferredAcceptanceMaterial(
        acceptance_id=acceptance_id,
        expression=expression,
        commitment_payload=CommitmentChangedPayload.model_validate(raw),
        policy_digest=policy.digest,
        source_observation_id=source_observation.observation_id,
        source_observation_event_ref=source_observation_event_ref,
        source_observation_event_hash=source_observation.event_payload_hash,
        thread_payload=thread_payload,
    )


def build_social_deferred_manifest(material: SocialDeferredAcceptanceMaterial) -> SocialDeferredAcceptanceManifest:
    expression = build_expression_plan_manifest(
        acceptance_id=material.acceptance_id, material=material.expression
    )
    values: dict[str, object] = {
        "manifest_version": SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION,
        "acceptance_id": material.acceptance_id,
        "proposal_id": material.expression.proposal_id,
        "status": "accepted",
        "accepted_change_id": material.commitment_payload.change_id,
        "accepted_change_hash": material.commitment_payload.accepted_change_hash,
        "proposal_event_ref": material.expression.proposal_event_ref,
        "proposal_event_payload_hash": material.expression.proposal_event_payload_hash,
        "evaluated_world_revision": material.expression.cursor.world_revision,
        "source_observation_id": material.source_observation_id,
        "source_observation_event_ref": material.source_observation_event_ref,
        "source_observation_event_hash": material.source_observation_event_hash,
        "policy_digest": material.policy_digest,
        "expression_manifest": expression.model_dump(mode="json"),
        "commitment_payload_hash": canonical_expression_plan_value_hash(
            material.commitment_payload.model_dump(mode="json")
        ),
        "commitment_id": material.commitment_payload.commitment_after.commitment_id,
        "thread_proposal_id": material.thread_payload.proposal_id,
        "thread_id": material.thread_payload.thread_after.thread_id,
        "thread_payload_hash": canonical_expression_plan_value_hash(
            material.thread_payload.model_dump(mode="json")
        ),
        "action_id": material.expression.beats[0].action.action_id,
    }
    values["manifest_hash"] = social_deferred_manifest_hash(values)
    return SocialDeferredAcceptanceManifest.model_validate_json(_canonical_json(values), strict=True)


__all__ = [
    "SOCIAL_DEFERRED_ACCEPTANCE_MANIFEST_VERSION",
    "SocialDeferredAcceptanceManifest",
    "SocialDeferredAcceptanceMaterial",
    "SocialDeferredPolicy",
    "build_social_deferred_manifest",
    "derive_social_deferred_material",
    "social_deferred_commitment_event_id",
    "social_deferred_manifest_hash",
]
