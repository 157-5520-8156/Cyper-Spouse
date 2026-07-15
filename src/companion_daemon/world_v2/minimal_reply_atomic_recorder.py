"""Opaque accepted-batch materializer for one ordinary text reply."""

from __future__ import annotations

import hashlib
import json

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .event_identity import domain_idempotency_key
from .minimal_reply_acceptance import MinimalReplyAcceptanceMaterial
from .minimal_reply_events import (
    ExpressionBeatAuthorizedPayload,
    ExpressionPlanAcceptedPayload,
    MessagePayloadStoredPayload,
    minimal_reply_event_id,
    minimal_reply_idempotency_key,
)
from .minimal_reply_manifest import build_minimal_reply_manifest
from .schemas import WorldEvent


class MinimalReplyAtomicRecorderError(ValueError):
    """Stable failure at the ordinary-reply materialization boundary."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


class MinimalReplyAtomicRecorder:
    """The sole writer capability for the isolated reply acceptance lane."""

    __slots__ = ("__batch_issuer",)

    def __init__(self, *, batch_issuer: AcceptedLedgerBatchIssuer) -> None:
        if type(batch_issuer) is not AcceptedLedgerBatchIssuer:
            raise TypeError("minimal reply recorder requires the exact accepted-batch issuer")
        self.__batch_issuer = batch_issuer

    def prepare_batch(
        self,
        *,
        acceptance_id: str,
        material: MinimalReplyAcceptanceMaterial,
        actor: str,
        source: str,
    ) -> AcceptedLedgerBatchHandle:
        """Derive six closed envelopes; only the issuer can turn them into a write."""

        if type(acceptance_id) is not str or not acceptance_id:
            raise MinimalReplyAtomicRecorderError("minimal reply acceptance id is invalid")
        manifest = build_minimal_reply_manifest(acceptance_id=acceptance_id, material=material)
        common = {
            "schema_version": "world-v2.1",
            "world_id": material.action.world_id,
            "logical_time": material.action.logical_time,
            "created_at": material.action.created_at,
            "actor": actor,
            "source": source,
            "trace_id": material.action.trace_id,
            "correlation_id": material.action.correlation_id,
        }
        payloads = (
            manifest.model_dump(mode="json"),
            MessagePayloadStoredPayload(
                acceptance_id=acceptance_id,
                proposal_id=material.proposal_id,
                message=material.beat.payload,
            ).model_dump(mode="json"),
            ExpressionPlanAcceptedPayload(
                acceptance_id=acceptance_id,
                proposal_id=material.proposal_id,
                expression_change_id=material.expression_change_id,
                plan_id=material.beat.plan_id,
            ).model_dump(mode="json"),
            ExpressionBeatAuthorizedPayload(
                acceptance_id=acceptance_id,
                proposal_id=material.proposal_id,
                expression_change_id=material.expression_change_id,
                beat=material.beat,
            ).model_dump(mode="json"),
            {"reservation": material.reservation.model_dump(mode="json")},
            {"action": material.action.model_dump(mode="json")},
        )
        types = (
            "AcceptanceRecorded",
            "MessagePayloadStored",
            "ExpressionPlanAccepted",
            "ExpressionBeatAuthorized",
            "BudgetReserved",
            "ActionAuthorized",
        )
        roles = ("acceptance", "message", "plan", "beat", "reservation", "action")
        stable_ids = (
            acceptance_id,
            material.beat.payload.payload_ref,
            material.beat.plan_id,
            material.beat.beat_id,
            material.reservation.reservation_id,
            material.action.action_id,
        )
        events: list[WorldEvent] = []
        for index, (event_type, role, stable_id, payload) in enumerate(
            zip(types, roles, stable_ids, payloads, strict=True)
        ):
            identity = (
                domain_idempotency_key(
                    event_type=event_type, world_id=material.action.world_id, payload=payload
                )
                if index < 4
                else minimal_reply_idempotency_key(
                    world_id=material.action.world_id,
                    manifest_hash=manifest.manifest_hash,
                    role=role,
                    stable_id=stable_id,
                )
            )
            if identity is None:
                raise MinimalReplyAtomicRecorderError("minimal reply event lacks domain identity")
            events.append(
                WorldEvent.from_payload(
                    **common,
                    event_id=minimal_reply_event_id(
                        manifest_hash=manifest.manifest_hash, role=role, stable_id=stable_id
                    ),
                    event_type=event_type,
                    causation_id=(
                        material.proposal_event_ref if index == 0 else events[-1].event_id
                    ),
                    idempotency_key=identity,
                    payload=payload,
                )
            )
        materialized = tuple(events)
        commit_id = _commit_id(material=material, manifest_hash=manifest.manifest_hash, events=materialized)
        return self.__batch_issuer.issue(
            world_id=material.action.world_id,
            expected_cursor=material.cursor,
            events=materialized,
            manifest_hash=manifest.manifest_hash,
            registry_digest=material.policy_digest,
            commit_id=commit_id,
        )


def _commit_id(
    *, material: MinimalReplyAcceptanceMaterial, manifest_hash: str, events: tuple[WorldEvent, ...]
) -> str:
    digest = hashlib.sha256(
        _canonical_json(
            {
                "contract": "minimal-reply-accepted-commit.1",
                "world_id": material.action.world_id,
                "cursor": material.cursor.model_dump(mode="json"),
                "manifest_hash": manifest_hash,
                "events": tuple(event.model_dump(mode="json") for event in events),
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"commit:minimal-reply:{digest}"


__all__ = ["MinimalReplyAtomicRecorder", "MinimalReplyAtomicRecorderError"]
