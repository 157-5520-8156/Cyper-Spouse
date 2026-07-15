"""The only Fact-v2 component allowed to materialize accepted event envelopes.

This module stops at an opaque accepted-ledger batch.  The ledger integration
will provide the transaction/CAS commit seam; callers cannot obtain the event
sequence from a production bundle without this recorder and its batch issuer.
"""

from __future__ import annotations

import hashlib
import json

from .accepted_ledger_batch import (
    AcceptedLedgerBatchHandle,
    AcceptedLedgerBatchIssuer,
)
from .event_identity import domain_idempotency_key
from .fact_v2_accepted_manifest_builder import (
    FACT_V2_ACCEPTED_EVENT_TYPE,
    FactV2AcceptedManifestBuilder,
    FactV2AcceptedManifestBuilderError,
    FactV2ProductionAcceptedBundle,
    FactV2ProductionAcceptedBundleHandle,
)
from .schemas import WorldEvent


class FactV2AtomicRecorderError(ValueError):
    """Stable failure at the Fact-v2 event-materialization boundary."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


class FactV2AtomicRecorder:
    """Materialize exactly one accepted Fact-v2 batch from a builder handle."""

    __slots__ = ("__builder", "__batch_issuer")

    def __init__(
        self,
        *,
        manifest_builder: FactV2AcceptedManifestBuilder,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> None:
        if type(manifest_builder) is not FactV2AcceptedManifestBuilder:
            raise TypeError("Fact v2 recorder requires the exact ManifestBuilder")
        if type(batch_issuer) is not AcceptedLedgerBatchIssuer:
            raise TypeError("Fact v2 recorder requires the exact accepted-batch issuer")
        self.__builder = manifest_builder
        self.__batch_issuer = batch_issuer

    def prepare_batch(
        self, *, bundle_handle: FactV2ProductionAcceptedBundleHandle
    ) -> AcceptedLedgerBatchHandle:
        """Revalidate one bundle and issue a ledger-owned opaque batch handle."""

        try:
            bundle = self.__builder.revalidate(handle=bundle_handle)
        except FactV2AcceptedManifestBuilderError as exc:
            raise FactV2AtomicRecorderError(str(exc)) from exc
        events = _materialize_events(bundle)
        commit_id = _commit_id(bundle=bundle, events=events)
        return self.__batch_issuer.issue(
            world_id=bundle.plan.envelope.world_id,
            expected_cursor=bundle.plan.envelope.cursor,
            events=events,
            manifest_hash=bundle.manifest.manifest_hash,
            registry_digest=bundle.plan.durable_authority.registry_digest,
            commit_id=commit_id,
        )


def _materialize_events(bundle: FactV2ProductionAcceptedBundle) -> tuple[WorldEvent, WorldEvent]:
    envelope = bundle.plan.envelope
    manifest_payload = bundle.manifest.model_dump(mode="json")
    acceptance_identity = domain_idempotency_key(
        event_type="AcceptanceRecorded",
        world_id=envelope.world_id,
        payload=manifest_payload,
    )
    effect_payload = bundle.plan.payload.model_dump(mode="json")
    effect_identity = domain_idempotency_key(
        event_type=FACT_V2_ACCEPTED_EVENT_TYPE,
        world_id=envelope.world_id,
        payload=effect_payload,
    )
    if acceptance_identity is None or effect_identity is None:
        raise FactV2AtomicRecorderError("Fact v2 accepted events lack domain identities")
    if effect_identity != bundle.effect_idempotency_key:
        raise FactV2AtomicRecorderError("Fact v2 bundle idempotency does not match event contract")
    acceptance = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=envelope.acceptance_event_id,
        world_id=envelope.world_id,
        event_type="AcceptanceRecorded",
        logical_time=envelope.logical_time,
        created_at=envelope.created_at,
        actor=envelope.actor,
        source=envelope.source,
        trace_id=envelope.trace_id,
        causation_id=envelope.acceptance_causation_id,
        correlation_id=envelope.correlation_id,
        idempotency_key=acceptance_identity,
        payload=manifest_payload,
    )
    effect = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=bundle.effect_event_id,
        world_id=envelope.world_id,
        event_type=FACT_V2_ACCEPTED_EVENT_TYPE,
        logical_time=envelope.logical_time,
        created_at=envelope.created_at,
        actor=envelope.actor,
        source=envelope.source,
        trace_id=envelope.trace_id,
        causation_id=acceptance.event_id,
        correlation_id=envelope.correlation_id,
        idempotency_key=effect_identity,
        payload=effect_payload,
    )
    return acceptance, effect


def _commit_id(*, bundle: FactV2ProductionAcceptedBundle, events: tuple[WorldEvent, WorldEvent]) -> str:
    envelope = bundle.plan.envelope
    ordered_envelope_hash = hashlib.sha256(
        _canonical_json(tuple(event.model_dump(mode="json") for event in events)).encode("utf-8")
    ).hexdigest()
    digest = hashlib.sha256(
        _canonical_json(
            {
                "contract": "accepted-ledger-commit.1",
                "world_id": envelope.world_id,
                "cursor": envelope.cursor.model_dump(mode="json"),
                "manifest_hash": bundle.manifest.manifest_hash,
                "ordered_envelope_hash": ordered_envelope_hash,
                "registry_digest": bundle.plan.durable_authority.registry_digest,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"commit:accepted-v3:{digest}"


__all__ = ["FactV2AtomicRecorder", "FactV2AtomicRecorderError"]
