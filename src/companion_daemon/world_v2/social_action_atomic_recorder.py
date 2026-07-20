"""Opaque accepted-batch materializer for one deferred social followup."""

from __future__ import annotations

import hashlib
import json

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .event_identity import domain_idempotency_key
from .expression_plan_atomic_recorder import expression_plan_idempotency_key
from .minimal_reply_events import (
    ExpressionBeatAuthorizedPayload,
    ExpressionPlanAcceptedPayload,
    MessagePayloadStoredPayload,
)
from .schemas import WorldEvent
from .social_action_acceptance import (
    SocialDeferredAcceptanceMaterial,
    build_social_deferred_manifest,
    social_deferred_commitment_event_id,
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _event_id(*, manifest_hash: str, role: str, stable_id: str) -> str:
    return "event:social-deferred:" + role + ":" + hashlib.sha256(
        _canonical_json({"contract": "social-deferred-event.1", "manifest_hash": manifest_hash,
                         "role": role, "stable_id": stable_id}).encode("utf-8")
    ).hexdigest()


class SocialDeferredAtomicRecorder:
    """The sole capability that can atomically authorize a delayed reply."""

    __slots__ = ("__batch_issuer",)

    def __init__(self, *, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        if type(batch_issuer) is not AcceptedLedgerBatchIssuer:
            raise TypeError("social deferred recorder requires the exact accepted-batch issuer")
        self.__batch_issuer = batch_issuer

    def prepare_batch(
        self, *, material: SocialDeferredAcceptanceMaterial, actor: str, source: str
    ) -> AcceptedLedgerBatchHandle:
        manifest = build_social_deferred_manifest(material)
        expression = material.expression
        item = expression.beats[0]
        common = {
            "schema_version": "world-v2.1",
            "world_id": item.action.world_id,
            "logical_time": item.action.logical_time,
            "created_at": item.action.created_at,
            "actor": actor,
            "source": source,
            "trace_id": item.action.trace_id,
            "correlation_id": item.action.correlation_id,
        }
        raw = (
            ("AcceptanceRecorded", "acceptance", material.acceptance_id,
             manifest.model_dump(mode="json"), True),
            ("PrivateCommitmentOpened", "commitment", material.commitment_payload.commitment_after.commitment_id,
             material.commitment_payload.model_dump(mode="json"), True),
            ("MessagePayloadStored", "message", item.beat.payload.payload_ref,
             MessagePayloadStoredPayload(acceptance_id=material.acceptance_id,
                 proposal_id=expression.proposal_id, message=item.beat.payload).model_dump(mode="json"), True),
            ("ExpressionPlanAccepted", "plan", expression.plan_id,
             ExpressionPlanAcceptedPayload(acceptance_id=material.acceptance_id,
                 proposal_id=expression.proposal_id, expression_change_id=expression.expression_change_id,
                 plan_id=expression.plan_id).model_dump(mode="json"), True),
            ("ExpressionBeatAuthorized", "beat", item.beat.beat_id,
             ExpressionBeatAuthorizedPayload(acceptance_id=material.acceptance_id,
                 proposal_id=expression.proposal_id, expression_change_id=expression.expression_change_id,
                 beat=item.beat).model_dump(mode="json"), True),
            ("BudgetReserved", "reservation", item.reservation.reservation_id,
             {"reservation": item.reservation.model_dump(mode="json")}, False),
            ("ActionAuthorized", "action", item.action.action_id,
             {"action": item.action.model_dump(mode="json")}, False),
            ("AcceptanceRecorded", "thread-acceptance", material.thread_payload.acceptance_id,
             {
                 "acceptance_id": material.thread_payload.acceptance_id,
                 "status": "accepted",
                 "proposal_id": material.thread_payload.proposal_id,
                 "evaluated_world_revision": material.thread_payload.evaluated_world_revision,
                 "accepted_change_id": material.thread_payload.change_id,
                 "accepted_change_hash": material.thread_payload.accepted_change_hash,
             }, True),
            ("ThreadOpened", "thread", material.thread_payload.thread_after.thread_id,
             material.thread_payload.model_dump(mode="json"), True),
        )
        events: list[WorldEvent] = []
        for index, (event_type, role, stable_id, payload, domain_identity) in enumerate(raw):
            identity = (
                domain_idempotency_key(event_type=event_type, world_id=item.action.world_id, payload=payload)
                if domain_identity
                else expression_plan_idempotency_key(world_id=item.action.world_id,
                    manifest_hash=manifest.manifest_hash, role=role, stable_id=stable_id)
            )
            if identity is None:
                raise ValueError("social deferred event lacks domain identity")
            event_id = (
                social_deferred_commitment_event_id(world_id=item.action.world_id,
                    acceptance_id=material.acceptance_id)
                if role == "commitment"
                else material.thread_payload.thread_after.origin.accepted_event_ref
                if role == "thread"
                else _event_id(manifest_hash=manifest.manifest_hash, role=role, stable_id=stable_id)
            )
            events.append(WorldEvent.from_payload(
                **common,
                event_id=event_id,
                event_type=event_type,
                causation_id=expression.proposal_event_ref if index == 0 else events[-1].event_id,
                idempotency_key=identity,
                payload=payload,
            ))
        frozen = tuple(events)
        commit_id = "commit:social-deferred:" + hashlib.sha256(_canonical_json({
            "contract": "social-deferred-accepted-commit.1",
            "world_id": item.action.world_id,
            "cursor": expression.cursor.model_dump(mode="json"),
            "manifest_hash": manifest.manifest_hash,
            "events": tuple(event.model_dump(mode="json") for event in frozen),
        }).encode("utf-8")).hexdigest()
        return self.__batch_issuer.issue(
            world_id=item.action.world_id,
            expected_cursor=expression.cursor,
            events=frozen,
            manifest_hash=manifest.manifest_hash,
            registry_digest=material.policy_digest,
            commit_id=commit_id,
        )


__all__ = ["SocialDeferredAtomicRecorder"]
