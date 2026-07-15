"""Compiler for the normal multi-beat ExpressionPlan acceptance lane."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from pydantic import Field, model_validator

from .minimal_reply_acceptance import ExpressionBeatMaterial, MessagePayloadMaterial
from .proposal_audit_schemas import ProposalAuditProjection
from .proposal_envelope import ProposalInput, validate_proposal_envelope
from .schema_core import FrozenModel
from .schemas import Action, BudgetAccount, BudgetReservation, ProjectionCursor


EXPRESSION_PLAN_ACCEPTANCE_POLICY_VERSION = "expression-plan-acceptance.1"


class ExpressionPlanAcceptanceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"expression_plan_acceptance.{code}"
        super().__init__(self.code)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ExpressionPlanBudgetPolicy(FrozenModel):
    """Composition-owned constraints, deliberately agnostic to the chosen prose."""

    account_id: str = Field(min_length=1, max_length=256)
    amount_limit_per_action: int = Field(ge=0, le=10_000_000)
    actor: str = Field(min_length=1, max_length=256)
    allowed_targets: tuple[str, ...] = Field(min_length=1, max_length=64)
    recovery_policy: str = Field(min_length=1, max_length=128)
    policy_version: str = EXPRESSION_PLAN_ACCEPTANCE_POLICY_VERSION

    @model_validator(mode="after")
    def target_set_is_canonical(self) -> "ExpressionPlanBudgetPolicy":
        if tuple(sorted(self.allowed_targets)) != self.allowed_targets or len(set(self.allowed_targets)) != len(self.allowed_targets):
            raise ValueError("expression plan target allow-list must be sorted and unique")
        return self

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))


class ExpressionPlanBeatMaterialized(FrozenModel):
    beat: ExpressionBeatMaterial
    intent_id: str = Field(min_length=1)
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reservation: BudgetReservation
    action: Action


class ExpressionPlanAcceptanceMaterial(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_event_ref: str = Field(min_length=1)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    cursor: ProjectionCursor
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expression_change_id: str = Field(min_length=1)
    expression_change_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1)
    ordering_policy: str = Field(min_length=1)
    terminal_policy: str = Field(min_length=1)
    beats: tuple[ExpressionPlanBeatMaterialized, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def material_is_closed(self) -> "ExpressionPlanAcceptanceMaterial":
        ids = {item.beat.beat_id for item in self.beats}
        if len(ids) != len(self.beats) or any(item.beat.plan_id != self.plan_id for item in self.beats):
            raise ValueError("expression plan material beat identity is invalid")
        if any(set(item.beat.dependency_beat_ids) - ids for item in self.beats):
            raise ValueError("expression plan material dependency is unknown")
        for item in self.beats:
            if (
                item.reservation.action_id != item.action.action_id
                or item.action.budget_reservation_id != item.reservation.reservation_id
                or item.action.payload_ref != item.beat.payload.payload_ref
                or item.action.payload_hash != item.beat.payload.payload_hash
                or item.action.expression_plan_id != self.plan_id
                or item.action.expression_beat_id != item.beat.beat_id
                or item.action.intent_ref != f"{self.proposal_id}:{item.intent_id}"
            ):
                raise ValueError("expression plan material action is not beat-bound")
        return self


def derive_expression_plan_material(
    *,
    audit: ProposalAuditProjection,
    cursor: ProjectionCursor,
    world_id: str,
    policy: ExpressionPlanBudgetPolicy,
    account: BudgetAccount,
    logical_time: datetime,
    created_at: datetime,
    trace_id: str,
    correlation_id: str,
) -> ExpressionPlanAcceptanceMaterial:
    """Fail closed unless all external expression work is one complete plan.

    Other domain changes intentionally remain for their own typed acceptance
    families.  This prevents a generic proposal from getting a partial
    acceptance where text actions are authorized but its claimed world changes
    quietly disappear.
    """

    if audit.evaluated_world_revision != cursor.world_revision:
        raise ExpressionPlanAcceptanceError("stale_revision")
    try:
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
    except Exception as exc:
        raise ExpressionPlanAcceptanceError("invalid_audit") from exc
    _validate_audit(audit=audit, proposal=proposal, cursor=cursor)
    if len(proposal.proposed_changes) != 1:
        raise ExpressionPlanAcceptanceError("proposal_has_other_changes")
    change = proposal.proposed_changes[0]
    if change.kind != "expression_plan_transition" or change.transition != "accept":
        raise ExpressionPlanAcceptanceError("expression_change_invalid")
    payload = change.payload.value()
    drafts = payload.get("beat_drafts")
    if not isinstance(drafts, list) or not drafts:
        raise ExpressionPlanAcceptanceError("beats_invalid")
    plan_id = payload.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise ExpressionPlanAcceptanceError("plan_invalid")
    if account.account_id != policy.account_id or account.category != "chat":
        raise ExpressionPlanAcceptanceError("budget_account_unavailable")
    external_intents = tuple(
        item for item in proposal.action_intents if item.kind in proposal.EXPRESSION_ACTION_KINDS
    )
    if len(external_intents) != len(proposal.action_intents) or len(external_intents) != len(drafts):
        raise ExpressionPlanAcceptanceError("expression_intents_not_exact")
    by_beat = {item.beat_ref: item for item in external_intents}
    if None in by_beat or len(by_beat) != len(external_intents):
        raise ExpressionPlanAcceptanceError("expression_intents_not_exact")
    if account.limit - account.reserved - account.spent < policy.amount_limit_per_action * len(drafts):
        raise ExpressionPlanAcceptanceError("budget_unavailable")

    identity_root = {
        "contract": "expression-plan-acceptance.1",
        "world_id": world_id,
        "proposal_id": proposal.proposal_id,
        "proposal_hash": proposal.proposal_hash,
        "policy_digest": policy.digest,
        "plan_id": plan_id,
    }
    action_id_by_beat: dict[str, str] = {}
    parsed: list[tuple[dict[str, object], object, MessagePayloadMaterial, ExpressionBeatMaterial, str]] = []
    for draft in drafts:
        if not isinstance(draft, dict):
            raise ExpressionPlanAcceptanceError("beats_invalid")
        beat_id = draft.get("beat_id")
        text = draft.get("inline_text")
        payload_ref = draft.get("materialized_payload_ref")
        payload_hash = draft.get("payload_hash")
        if not all(isinstance(value, str) and value for value in (beat_id, text, payload_ref, payload_hash)):
            raise ExpressionPlanAcceptanceError("beat_binding_invalid")
        if payload_hash != "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise ExpressionPlanAcceptanceError("beat_binding_invalid")
        intent = by_beat.get(beat_id)
        if intent is None or (
            intent.causal_change_id != change.change_id
            or intent.layer != "external_action"
            or intent.payload_ref != payload_ref
            or intent.payload_hash != payload_hash
            or intent.target not in policy.allowed_targets
        ):
            raise ExpressionPlanAcceptanceError("beat_binding_invalid")
        expected_intent_dependencies = tuple(
            by_beat[dependency].intent_id for dependency in draft.get("dependency_beat_ids", ())
        )
        if intent.dependencies != expected_intent_dependencies:
            raise ExpressionPlanAcceptanceError("beat_dependency_invalid")
        delay = draft.get("delay_window")
        not_before = expires_at = None
        if delay is not None:
            if not isinstance(delay, dict):
                raise ExpressionPlanAcceptanceError("delay_invalid")
            try:
                not_before = datetime.fromisoformat(str(delay["not_before"]))
                expires_at = datetime.fromisoformat(str(delay["expires_at"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ExpressionPlanAcceptanceError("delay_invalid") from exc
            if not_before.tzinfo is None or expires_at.tzinfo is None or expires_at <= not_before:
                raise ExpressionPlanAcceptanceError("delay_invalid")
            if intent.due_window != (not_before, expires_at):
                raise ExpressionPlanAcceptanceError("delay_binding_invalid")
        elif intent.due_window is not None:
            raise ExpressionPlanAcceptanceError("delay_binding_invalid")
        beat = ExpressionBeatMaterial(
            plan_id=plan_id,
            beat_id=beat_id,
            payload=MessagePayloadMaterial(
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                text=text,
                content_type=str(draft.get("content_type")),
            ),
            dependency_beat_ids=tuple(draft.get("dependency_beat_ids", ())),
            not_before=not_before,
            expires_at=expires_at,
            cancel_policy=str(draft.get("cancel_policy")),
            reconsider_policy=str(draft.get("reconsider_policy")),
            merge_policy=str(draft.get("merge_policy")),
        )
        action_id_by_beat[beat_id] = "action:expression-plan:" + _digest({**identity_root, "beat_id": beat_id, "role": "action"})
        parsed.append((draft, intent, beat.payload, beat, _digest(intent.model_dump(mode="json"))))

    materialized: list[ExpressionPlanBeatMaterialized] = []
    for draft, intent, _message, beat, intent_hash in parsed:
        beat_id = beat.beat_id
        action_id = action_id_by_beat[beat_id]
        reservation_id = "reservation:expression-plan:" + _digest({**identity_root, "beat_id": beat_id, "role": "budget"})
        delay = draft.get("delay_window")
        not_before = expires_at = None
        if isinstance(delay, dict):
            not_before = datetime.fromisoformat(str(delay["not_before"]))
            expires_at = datetime.fromisoformat(str(delay["expires_at"]))
        dependencies = tuple(action_id_by_beat[item] for item in beat.dependency_beat_ids)
        action = Action(
            schema_version="world-v2.1", action_id=action_id, world_id=world_id,
            logical_time=logical_time, created_at=created_at, trace_id=trace_id,
            causation_id=audit.event_ref, correlation_id=correlation_id, kind=intent.kind,
            layer="external_action", intent_ref=f"{proposal.proposal_id}:{intent.intent_id}",
            actor=policy.actor, target=intent.target, payload_ref=beat.payload.payload_ref,
            payload_hash=beat.payload.payload_hash, expression_plan_id=plan_id,
            expression_beat_id=beat_id,
            idempotency_key="expression-plan:" + _digest({**identity_root, "beat_id": beat_id, "role": "idempotency"}),
            not_before=not_before, expires_at=expires_at, dependencies=dependencies,
            budget_reservation_id=reservation_id, state="authorized", recovery_policy=policy.recovery_policy,
        )
        materialized.append(ExpressionPlanBeatMaterialized(
            beat=beat, intent_id=intent.intent_id, intent_hash=intent_hash,
            reservation=BudgetReservation(
                reservation_id=reservation_id, account_id=policy.account_id, action_id=action_id,
                category="chat", amount_limit=policy.amount_limit_per_action,
            ), action=action,
        ))
    return ExpressionPlanAcceptanceMaterial(
        proposal_id=proposal.proposal_id, proposal_event_ref=audit.event_ref,
        proposal_event_payload_hash=audit.event_payload_hash, proposal_hash=proposal.proposal_hash,
        cursor=cursor, policy_digest=policy.digest, expression_change_id=change.change_id,
        expression_change_hash=change.payload.payload_hash, plan_id=plan_id,
        ordering_policy=str(payload.get("ordering_policy")), terminal_policy=str(payload.get("terminal_policy")),
        beats=tuple(materialized),
    )


def _validate_audit(*, audit: ProposalAuditProjection, proposal: ProposalInput, cursor: ProjectionCursor) -> None:
    if (
        proposal.proposal_id != audit.proposal_id
        or proposal.proposal_hash != audit.proposal_hash
        or proposal.evaluated_world_revision != cursor.world_revision
        or proposal.schema_registry_version != "world-v2-proposals.1"
    ):
        raise ExpressionPlanAcceptanceError("authority_mismatch")


__all__ = [
    "EXPRESSION_PLAN_ACCEPTANCE_POLICY_VERSION", "ExpressionPlanAcceptanceError",
    "ExpressionPlanAcceptanceMaterial", "ExpressionPlanBeatMaterialized",
    "ExpressionPlanBudgetPolicy", "derive_expression_plan_material",
]
