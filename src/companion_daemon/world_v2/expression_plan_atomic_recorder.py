"""Opaque materializer for a normal multi-beat expression-plan acceptance."""

from __future__ import annotations

import hashlib
import json

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .event_identity import domain_idempotency_key
from .expression_plan_acceptance import ExpressionPlanAcceptanceMaterial
from .expression_plan_manifest import build_expression_plan_manifest
from .minimal_reply_events import (
    ExpressionBeatAuthorizedPayload,
    ExpressionPlanAcceptedPayload,
    MessagePayloadStoredPayload,
)
from .schemas import WorldEvent


class ExpressionPlanAtomicRecorderError(ValueError):
    """Stable failure at the multi-beat materialization boundary."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def expression_plan_event_id(*, manifest_hash: str, role: str, stable_id: str) -> str:
    return "event:expression-plan:" + role + ":" + hashlib.sha256(
        _canonical_json({"contract": "expression-plan-event-id.1", "manifest_hash": manifest_hash, "role": role, "stable_id": stable_id}).encode("utf-8")
    ).hexdigest()


def expression_plan_idempotency_key(*, world_id: str, manifest_hash: str, role: str, stable_id: str) -> str:
    return "world-v2:expression-plan:" + role + ":" + hashlib.sha256(
        _canonical_json({"contract": "expression-plan-idempotency.1", "world_id": world_id, "manifest_hash": manifest_hash, "role": role, "stable_id": stable_id}).encode("utf-8")
    ).hexdigest()


class ExpressionPlanAtomicRecorder:
    """The only writer capability for accepted multi-beat expressions."""

    __slots__ = ("__batch_issuer",)

    def __init__(self, *, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        if type(batch_issuer) is not AcceptedLedgerBatchIssuer:
            raise TypeError("expression plan recorder requires the exact accepted-batch issuer")
        self.__batch_issuer = batch_issuer

    def prepare_batch(
        self, *, acceptance_id: str, material: ExpressionPlanAcceptanceMaterial, actor: str, source: str
    ) -> AcceptedLedgerBatchHandle:
        if type(acceptance_id) is not str or not acceptance_id:
            raise ExpressionPlanAtomicRecorderError("expression plan acceptance id is invalid")
        manifest = build_expression_plan_manifest(acceptance_id=acceptance_id, material=material)
        common = {
            "schema_version": "world-v2.1", "world_id": material.beats[0].action.world_id,
            "logical_time": material.beats[0].action.logical_time, "created_at": material.beats[0].action.created_at,
            "actor": actor, "source": source, "trace_id": material.beats[0].action.trace_id,
            "correlation_id": material.beats[0].action.correlation_id,
        }
        raw: list[tuple[str, str, str, dict[str, object], bool]] = [
            ("AcceptanceRecorded", "acceptance", acceptance_id, manifest.model_dump(mode="json"), True)
        ]
        for item in material.beats:
            raw.append((
                "MessagePayloadStored", "message", item.beat.payload.payload_ref,
                MessagePayloadStoredPayload(acceptance_id=acceptance_id, proposal_id=material.proposal_id, message=item.beat.payload).model_dump(mode="json"), True,
            ))
        raw.append((
            "ExpressionPlanAccepted", "plan", material.plan_id,
            ExpressionPlanAcceptedPayload(acceptance_id=acceptance_id, proposal_id=material.proposal_id, expression_change_id=material.expression_change_id, plan_id=material.plan_id).model_dump(mode="json"), True,
        ))
        for item in material.beats:
            raw.extend((
                ("ExpressionBeatAuthorized", "beat", item.beat.beat_id, ExpressionBeatAuthorizedPayload(acceptance_id=acceptance_id, proposal_id=material.proposal_id, expression_change_id=material.expression_change_id, beat=item.beat).model_dump(mode="json"), True),
                ("BudgetReserved", "reservation", item.reservation.reservation_id, {"reservation": item.reservation.model_dump(mode="json")}, False),
                ("ActionAuthorized", "action", item.action.action_id, {"action": item.action.model_dump(mode="json")}, False),
            ))
        events: list[WorldEvent] = []
        for index, (event_type, role, stable_id, payload, domain_identity) in enumerate(raw):
            identity = domain_idempotency_key(event_type=event_type, world_id=common["world_id"], payload=payload) if domain_identity else expression_plan_idempotency_key(world_id=common["world_id"], manifest_hash=manifest.manifest_hash, role=role, stable_id=stable_id)
            if identity is None:
                raise ExpressionPlanAtomicRecorderError("expression plan event lacks domain identity")
            events.append(WorldEvent.from_payload(
                **common, event_id=expression_plan_event_id(manifest_hash=manifest.manifest_hash, role=role, stable_id=stable_id),
                event_type=event_type, causation_id=material.proposal_event_ref if index == 0 else events[-1].event_id,
                idempotency_key=identity, payload=payload,
            ))
        frozen = tuple(events)
        commit_id = "commit:expression-plan:" + hashlib.sha256(_canonical_json({
            "contract": "expression-plan-accepted-commit.1", "world_id": common["world_id"],
            "cursor": material.cursor.model_dump(mode="json"), "manifest_hash": manifest.manifest_hash,
            "events": tuple(item.model_dump(mode="json") for item in frozen),
        }).encode("utf-8")).hexdigest()
        return self.__batch_issuer.issue(
            world_id=common["world_id"], expected_cursor=material.cursor, events=frozen,
            manifest_hash=manifest.manifest_hash, registry_digest=material.policy_digest, commit_id=commit_id,
        )


__all__ = [
    "ExpressionPlanAtomicRecorder", "ExpressionPlanAtomicRecorderError", "expression_plan_event_id", "expression_plan_idempotency_key",
]
