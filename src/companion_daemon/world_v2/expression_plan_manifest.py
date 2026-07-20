"""Closed manifest for a fully materialized expression plan.

Unlike the compatibility ``minimal_reply`` lane this manifest represents the
normal expression acceptance boundary: one audited proposal can freeze an
ordered DAG of one or more message beats.  The manifest contains every byte
which will later be dispatched (and every reservation which pays for it), so
neither a scheduler nor a reconsideration worker can smuggle new prose into an
already accepted plan.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import Field, model_validator

from .minimal_reply_acceptance import ExpressionBeatMaterial
from .schema_core import FrozenModel
from .schemas import ResponseExpectationAuthority
from .schemas import Action, BudgetReservation

if TYPE_CHECKING:
    from .expression_plan_acceptance import ExpressionPlanAcceptanceMaterial


EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION = "expression-plan-acceptance.1"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def canonical_expression_plan_value_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(_compatibility_normalize(value)).encode("utf-8")).hexdigest()


def _compatibility_normalize(value: object) -> object:
    """Remove default sidecar metadata from pre-sidecar inline values.

    Existing .24 manifests and their component hashes were committed before
    these fields existed.  They must remain byte-identical after a replay;
    actual sidecar values retain every field and therefore remain authority
    bearing.
    """
    if isinstance(value, dict):
        normalized = {key: _compatibility_normalize(item) for key, item in value.items()}
        if normalized.get("storage_kind") == "inline_text":
            normalized.pop("storage_kind", None)
            normalized.pop("sidecar_kind", None)
            normalized.pop("privacy_class", None)
        return normalized
    if isinstance(value, tuple):
        return tuple(_compatibility_normalize(item) for item in value)
    if isinstance(value, list):
        return [_compatibility_normalize(item) for item in value]
    return value


def canonical_expression_plan_manifest_hash(value: dict[str, object]) -> str:
    material = dict(value)
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION)
    if isinstance(material.get("beats"), tuple):
        material["beats"] = tuple(
            item.model_dump(mode="json") if isinstance(item, ExpressionPlanBeatManifest) else item
            for item in material["beats"]
        )
    expectation = material.get("response_expectation")
    if isinstance(expectation, ResponseExpectationAuthority):
        material["response_expectation"] = expectation.model_dump(mode="json")
    elif expectation is None:
        # Preserve every pre-initiative manifest hash byte-for-byte.
        material.pop("response_expectation", None)
    return canonical_expression_plan_value_hash(material)


class ExpressionPlanBeatManifest(FrozenModel):
    """One immutable Beat/Action/Budget triple in an accepted expression plan."""

    beat: ExpressionBeatMaterial
    intent_id: str = Field(min_length=1, max_length=256)
    intent_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    message_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    beat_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reservation: BudgetReservation
    reservation_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Action
    action_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def triple_is_closed(self) -> "ExpressionPlanBeatManifest":
        if (
            self.message_hash != canonical_expression_plan_value_hash(self.beat.payload.model_dump(mode="json"))
            or self.beat_hash != canonical_expression_plan_value_hash(self.beat.model_dump(mode="json"))
            or self.reservation_hash
            != canonical_expression_plan_value_hash(self.reservation.model_dump(mode="json"))
            or self.action_hash != canonical_expression_plan_value_hash(self.action.model_dump(mode="json"))
            or self.reservation.action_id != self.action.action_id
            or self.action.budget_reservation_id != self.reservation.reservation_id
            or self.action.payload_ref != self.beat.payload.payload_ref
            or self.action.payload_hash != self.beat.payload.payload_hash
            or self.action.expression_plan_id != self.beat.plan_id
            or self.action.expression_beat_id != self.beat.beat_id
            or self.action.not_before != self.beat.not_before
            or self.action.expires_at != self.beat.expires_at
        ):
            raise ValueError("expression plan beat/action/reservation is not closed")
        return self


class ExpressionPlanAcceptanceManifest(FrozenModel):
    manifest_version: str = EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION
    acceptance_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_event_ref: str = Field(min_length=1, max_length=512)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expression_change_id: str = Field(min_length=1, max_length=256)
    expression_change_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1, max_length=512)
    ordering_policy: str = Field(min_length=1, max_length=128)
    terminal_policy: str = Field(min_length=1, max_length=128)
    beats: tuple[ExpressionPlanBeatManifest, ...] = Field(min_length=1, max_length=32)
    response_expectation: ResponseExpectationAuthority | None = None
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def manifest_is_self_bound_and_dag_is_closed(self) -> "ExpressionPlanAcceptanceManifest":
        if self.manifest_version != EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION:
            raise ValueError("expression plan manifest version is unsupported")
        if self.manifest_hash != canonical_expression_plan_manifest_hash(self.model_dump(mode="json")):
            raise ValueError("expression plan manifest hash is invalid")
        beat_ids = tuple(item.beat.beat_id for item in self.beats)
        if len(set(beat_ids)) != len(beat_ids):
            raise ValueError("expression plan manifest beat ids must be unique")
        if any(item.beat.plan_id != self.plan_id for item in self.beats):
            raise ValueError("expression plan manifest beat belongs to another plan")
        if any(set(item.beat.dependency_beat_ids) - set(beat_ids) for item in self.beats):
            raise ValueError("expression plan manifest dependency is unavailable")
        if any(item.beat.beat_id in item.beat.dependency_beat_ids for item in self.beats):
            raise ValueError("expression plan manifest beat depends on itself")
        unresolved = {item.beat.beat_id: set(item.beat.dependency_beat_ids) for item in self.beats}
        resolved: set[str] = set()
        while unresolved:
            ready = {beat_id for beat_id, deps in unresolved.items() if deps.issubset(resolved)}
            if not ready:
                raise ValueError("expression plan manifest dependencies must be acyclic")
            resolved.update(ready)
            for beat_id in ready:
                del unresolved[beat_id]
        action_ids = tuple(item.action.action_id for item in self.beats)
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("expression plan manifest action ids must be unique")
        if any(item.action.intent_ref != f"{self.proposal_id}:{item.intent_id}" for item in self.beats):
            raise ValueError("expression plan manifest action intent does not bind proposal")
        if self.response_expectation is not None and (
            self.response_expectation.source_plan_id != self.plan_id
            or self.response_expectation.source_beat_id not in beat_ids
        ):
            raise ValueError("response expectation does not bind this expression")
        return self


def build_expression_plan_manifest(
    *, acceptance_id: str, material: "ExpressionPlanAcceptanceMaterial"
) -> ExpressionPlanAcceptanceManifest:
    # Import lazily to keep manifest values acyclic with the compiler module.
    values: dict[str, object] = {
        "manifest_version": EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION,
        "acceptance_id": acceptance_id,
        "proposal_id": material.proposal_id,
        "proposal_event_ref": material.proposal_event_ref,
        "proposal_event_payload_hash": material.proposal_event_payload_hash,
        "proposal_hash": material.proposal_hash,
        "evaluated_world_revision": material.cursor.world_revision,
        "policy_digest": material.policy_digest,
        "expression_change_id": material.expression_change_id,
        "expression_change_hash": material.expression_change_hash,
        "plan_id": material.plan_id,
        "ordering_policy": material.ordering_policy,
        "terminal_policy": material.terminal_policy,
        "response_expectation": material.response_expectation,
        "beats": tuple(
            ExpressionPlanBeatManifest(
                beat=item.beat,
                intent_id=item.intent_id,
                intent_hash=item.intent_hash,
                message_hash=canonical_expression_plan_value_hash(item.beat.payload.model_dump(mode="json")),
                beat_hash=canonical_expression_plan_value_hash(item.beat.model_dump(mode="json")),
                reservation=item.reservation,
                reservation_hash=canonical_expression_plan_value_hash(item.reservation.model_dump(mode="json")),
                action=item.action,
                action_hash=canonical_expression_plan_value_hash(item.action.model_dump(mode="json")),
            )
            for item in material.beats
        ),
    }
    values["manifest_hash"] = canonical_expression_plan_manifest_hash(values)
    return ExpressionPlanAcceptanceManifest.model_validate(values, strict=True)


__all__ = [
    "EXPRESSION_PLAN_ACCEPTANCE_MANIFEST_VERSION",
    "ExpressionPlanAcceptanceManifest",
    "ExpressionPlanBeatManifest",
    "build_expression_plan_manifest",
    "canonical_expression_plan_manifest_hash",
    "canonical_expression_plan_value_hash",
]
