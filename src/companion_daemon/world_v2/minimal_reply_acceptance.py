"""Closed derivation for the first ordinary-reply Acceptance vertical.

This module is intentionally pure: it turns one already-audited
``MinimalProposal`` plus a composition-root policy into immutable expression,
budget and Action material.  It has no ledger write port.  A later recorder is
the only component allowed to materialize these values as events.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from pydantic import Field, model_validator

from .proposal_audit_schemas import ProposalAuditProjection
from .proposal_envelope import MinimalProposal, validate_proposal_envelope
from .schema_core import FrozenModel
from .schemas import Action, BudgetAccount, BudgetReservation, ProjectionCursor


REPLY_ACCEPTANCE_POLICY_VERSION = "minimal-reply-policy.1"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class MinimalReplyAcceptanceError(ValueError):
    """Stable failure codes at the reply-only Acceptance boundary."""

    def __init__(self, code: str) -> None:
        self.code = f"minimal_reply_acceptance.{code}"
        super().__init__(self.code)


class ReplyBudgetPolicy(FrozenModel):
    """Composition-owned facts that a model cannot select for a reply Action."""

    account_id: str = Field(min_length=1, max_length=256)
    amount_limit: int = Field(ge=0, le=10_000_000)
    actor: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=256)
    recovery_policy: str = Field(min_length=1, max_length=128)
    policy_version: str = REPLY_ACCEPTANCE_POLICY_VERSION

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class MessagePayloadMaterial(FrozenModel):
    payload_ref: str = Field(min_length=1, max_length=512)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    text: str = Field(min_length=1, max_length=4_096)
    content_type: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def text_hash_is_exact(self) -> MessagePayloadMaterial:
        expected = "sha256:" + hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        if self.payload_hash != expected:
            raise ValueError("message payload hash does not bind its text")
        return self


class ExpressionBeatMaterial(FrozenModel):
    plan_id: str = Field(min_length=1, max_length=512)
    beat_id: str = Field(min_length=1, max_length=512)
    payload: MessagePayloadMaterial
    dependency_beat_ids: tuple[str, ...] = ()
    cancel_policy: str = Field(min_length=1, max_length=128)
    reconsider_policy: str = Field(min_length=1, max_length=128)
    merge_policy: str = Field(min_length=1, max_length=128)


class MinimalReplyAcceptanceMaterial(FrozenModel):
    """All derived values needed by the future atomic recorder."""

    proposal_id: str = Field(min_length=1)
    proposal_event_ref: str = Field(min_length=1)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cursor: ProjectionCursor
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expression_change_id: str = Field(min_length=1)
    intent_id: str = Field(min_length=1)
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    beat: ExpressionBeatMaterial
    reservation: BudgetReservation
    action: Action

    @model_validator(mode="after")
    def action_and_reservation_are_closed(self) -> MinimalReplyAcceptanceMaterial:
        if (
            self.reservation.action_id != self.action.action_id
            or self.action.budget_reservation_id != self.reservation.reservation_id
            or self.action.payload_ref != self.beat.payload.payload_ref
            or self.action.payload_hash != self.beat.payload.payload_hash
            or self.action.intent_ref != f"{self.proposal_id}:{self.intent_id}"
        ):
            raise ValueError("reply Action does not exactly bind accepted material")
        return self


def derive_minimal_reply_material(
    *,
    audit: ProposalAuditProjection,
    cursor: ProjectionCursor,
    world_id: str,
    policy: ReplyBudgetPolicy,
    account: BudgetAccount,
    logical_time: datetime,
    created_at: datetime,
    trace_id: str,
    correlation_id: str,
) -> MinimalReplyAcceptanceMaterial:
    """Fail closed unless an audited proposal is exactly one normal reply."""

    if audit.evaluated_world_revision != cursor.world_revision:
        raise MinimalReplyAcceptanceError("stale_revision")
    try:
        proposal = validate_proposal_envelope(
            MinimalProposal.model_validate_json(audit.proposal_json, strict=True)
        )
    except Exception as exc:
        raise MinimalReplyAcceptanceError("invalid_audit") from exc
    if not isinstance(proposal, MinimalProposal):
        raise MinimalReplyAcceptanceError("unsupported_proposal")
    if (
        proposal.proposal_id != audit.proposal_id
        or proposal.proposal_hash != audit.proposal_hash
        or proposal.evaluated_world_revision != cursor.world_revision
        or proposal.schema_registry_version != "world-v2-proposals.1"
    ):
        raise MinimalReplyAcceptanceError("authority_mismatch")
    if len(proposal.proposed_changes) != 1 or len(proposal.action_intents) != 1:
        raise MinimalReplyAcceptanceError("reply_shape_invalid")
    change = proposal.proposed_changes[0]
    intent = proposal.action_intents[0]
    if (
        change.kind != "expression_plan_transition"
        or change.transition != "accept"
        or intent.kind != "reply"
        or intent.layer != "external_action"
        or intent.causal_change_id != change.change_id
    ):
        raise MinimalReplyAcceptanceError("reply_shape_invalid")
    payload = change.payload.value()
    drafts = payload.get("beat_drafts")
    if not isinstance(drafts, list) or len(drafts) != 1:
        raise MinimalReplyAcceptanceError("reply_shape_invalid")
    draft = drafts[0]
    if not isinstance(draft, dict):
        raise MinimalReplyAcceptanceError("reply_shape_invalid")
    plan_id = payload.get("plan_id")
    beat_id = draft.get("beat_id")
    text = draft.get("inline_text")
    payload_ref = draft.get("materialized_payload_ref")
    payload_hash = draft.get("payload_hash")
    if (
        not all(isinstance(value, str) and value for value in (plan_id, beat_id, text, payload_ref, payload_hash))
        or intent.beat_ref != beat_id
        or intent.payload_ref != payload_ref
        or intent.payload_hash != payload_hash
        or payload_hash != "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    ):
        raise MinimalReplyAcceptanceError("beat_binding_invalid")
    if account.account_id != policy.account_id or account.category != "chat":
        raise MinimalReplyAcceptanceError("budget_account_unavailable")
    if account.limit - account.reserved - account.spent < policy.amount_limit:
        raise MinimalReplyAcceptanceError("budget_unavailable")
    intent_hash = _digest(intent.model_dump(mode="json"))
    identity = {
        "contract": "minimal-reply-acceptance.1",
        "world_id": world_id,
        "proposal_id": proposal.proposal_id,
        "proposal_hash": proposal.proposal_hash,
        "intent_id": intent.intent_id,
        "intent_hash": intent_hash,
        "policy_digest": policy.digest,
    }
    action_id = f"action:minimal-reply:{_digest(identity)}"
    reservation_id = f"reservation:minimal-reply:{_digest({**identity, 'role': 'budget'})}"
    action = Action(
        schema_version="world-v2.1",
        action_id=action_id,
        world_id=world_id,
        logical_time=logical_time,
        created_at=created_at,
        trace_id=trace_id,
        causation_id=audit.event_ref,
        correlation_id=correlation_id,
        kind="reply",
        layer="external_action",
        intent_ref=f"{proposal.proposal_id}:{intent.intent_id}",
        actor=policy.actor,
        target=policy.target,
        payload_ref=payload_ref,
        payload_hash=payload_hash,
        idempotency_key=f"minimal-reply:{_digest({**identity, 'role': 'action'})}",
        not_before=None,
        expires_at=None,
        dependencies=(),
        budget_reservation_id=reservation_id,
        state="authorized",
        recovery_policy=policy.recovery_policy,
    )
    return MinimalReplyAcceptanceMaterial(
        proposal_id=proposal.proposal_id,
        proposal_event_ref=audit.event_ref,
        proposal_event_payload_hash=audit.event_payload_hash,
        proposal_hash=proposal.proposal_hash,
        cursor=cursor,
        policy_digest=policy.digest,
        expression_change_id=change.change_id,
        intent_id=intent.intent_id,
        intent_hash=intent_hash,
        beat=ExpressionBeatMaterial(
            plan_id=plan_id,
            beat_id=beat_id,
            payload=MessagePayloadMaterial(
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                text=text,
                content_type=str(draft.get("content_type")),
            ),
            dependency_beat_ids=tuple(draft.get("dependency_beat_ids", ())),
            cancel_policy=str(draft.get("cancel_policy")),
            reconsider_policy=str(draft.get("reconsider_policy")),
            merge_policy=str(draft.get("merge_policy")),
        ),
        reservation=BudgetReservation(
            reservation_id=reservation_id,
            account_id=policy.account_id,
            action_id=action_id,
            category="chat",
            amount_limit=policy.amount_limit,
        ),
        action=action,
    )


__all__ = [
    "ExpressionBeatMaterial",
    "MessagePayloadMaterial",
    "MinimalReplyAcceptanceError",
    "MinimalReplyAcceptanceMaterial",
    "REPLY_ACCEPTANCE_POLICY_VERSION",
    "ReplyBudgetPolicy",
    "derive_minimal_reply_material",
]
