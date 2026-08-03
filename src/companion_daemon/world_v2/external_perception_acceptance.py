"""Producer-first acceptance seam for live External Perception deliveries."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from weakref import WeakKeyDictionary

from .accepted_ledger_batch import AcceptedLedgerBatchHandle, AcceptedLedgerBatchIssuer
from .event_identity import domain_idempotency_key
from .external_perception_acceptance_manifest import (
    EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_DIGEST,
    EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_VERSION,
    ExternalPerceptionAcceptedEffect,
    build_external_perception_acceptance_manifest,
)
from .external_perception_events import (
    ExternalPerceptionLiveDelivery,
    ExternalPerceptionRecordedPayload,
    ExternalSignalSnapshotAdoptedPayload,
)
from .ledger import LedgerPort, WorldLedger
from .schema_core import FrozenModel
from .schemas import CommitResult, WorldEvent
from .sqlite_ledger import SQLiteWorldLedger


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


class ExternalPerceptionAcceptanceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"external_perception_acceptance.{code}"
        super().__init__(self.code)


class ExternalPerceptionAcceptedBinding(FrozenModel):
    perception_id: str
    perception_event_ref: str
    snapshot_ref: str
    snapshot_event_ref: str


class ExternalPerceptionAcceptanceReceipt(FrozenModel):
    attention_attempt_id: str
    acceptance_event_ref: str
    model_result_event_ref: str
    perceptions: tuple[ExternalPerceptionAcceptedBinding, ...]
    commit_result: CommitResult

    @property
    def world_revision(self) -> int:
        return self.commit_result.world_revision

    @property
    def deliberation_revision(self) -> int:
        return self.commit_result.deliberation_revision


class ExternalPerceptionDeliveryHandle:
    """Opaque producer-issued capability for one validated live delivery."""

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("external perception delivery handles cannot be serialized")


class ExternalPerceptionDeliveryProducer:
    """Validate live handoff material before it can approach ledger authority."""

    __slots__ = ("__deliveries",)

    def __init__(self) -> None:
        self.__deliveries: WeakKeyDictionary[
            ExternalPerceptionDeliveryHandle, ExternalPerceptionLiveDelivery
        ] = WeakKeyDictionary()

    def prepare(self, delivery: ExternalPerceptionLiveDelivery) -> ExternalPerceptionDeliveryHandle:
        if type(delivery) is not ExternalPerceptionLiveDelivery:
            raise ExternalPerceptionAcceptanceError("delivery_contract_invalid")
        rebuilt = ExternalPerceptionLiveDelivery.model_validate(
            delivery.model_dump(mode="python"), strict=True
        )
        if not rebuilt.deployment_mode_revision.startswith("live:"):
            raise ExternalPerceptionAcceptanceError("live_mode_required")
        if any(
            not item.snapshot.may_expose_to_character_model
            or not item.snapshot.may_freeze_durable_snapshot
            for item in rebuilt.selections
        ):
            raise ExternalPerceptionAcceptanceError("snapshot_not_licensed")
        handle = ExternalPerceptionDeliveryHandle()
        self.__deliveries[handle] = rebuilt
        return handle

    def materialize(
        self, handle: ExternalPerceptionDeliveryHandle
    ) -> ExternalPerceptionLiveDelivery:
        if type(handle) is not ExternalPerceptionDeliveryHandle:
            raise ExternalPerceptionAcceptanceError("delivery_handle_untrusted")
        delivery = self.__deliveries.get(handle)
        if delivery is None:
            raise ExternalPerceptionAcceptanceError("delivery_handle_untrusted")
        return delivery


def _event_id(*, world_id: str, role: str, identity: object) -> str:
    return f"event:external-perception:{role}:" + _digest(
        {"world_id": world_id, "identity": identity}
    )


@dataclass(frozen=True, slots=True)
class _MaterializedDelivery:
    delivery: ExternalPerceptionLiveDelivery
    events: tuple[WorldEvent, ...]
    manifest_hash: str
    commit_id: str


class ExternalPerceptionAtomicRecorder:
    """Sole materializer for the accepted audit/snapshot/perception batch."""

    __slots__ = ("__producer", "__batch_issuer")

    def __init__(
        self,
        *,
        delivery_producer: ExternalPerceptionDeliveryProducer,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> None:
        self.__producer = delivery_producer
        self.__batch_issuer = batch_issuer

    def prepare_batch(
        self, handle: ExternalPerceptionDeliveryHandle
    ) -> tuple[
        AcceptedLedgerBatchHandle,
        ExternalPerceptionLiveDelivery,
        _MaterializedDelivery,
    ]:
        delivery = self.__producer.materialize(handle)
        material = self._materialize(delivery)
        batch = self.__batch_issuer.issue(
            world_id=delivery.world_id,
            expected_cursor=delivery.pinned_cursor,
            events=material.events,
            manifest_hash=material.manifest_hash,
            registry_digest=EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_DIGEST,
            commit_id=material.commit_id,
        )
        return batch, delivery, material

    @staticmethod
    def _materialize(delivery: ExternalPerceptionLiveDelivery) -> _MaterializedDelivery:
        common = {
            "schema_version": "world-v2.1",
            "world_id": delivery.world_id,
            "logical_time": delivery.encountered_world_time,
            "created_at": delivery.observed_wall_time,
            "actor": delivery.actor_ref,
            "source": "external-perception-live-delivery",
            "trace_id": f"trace:{delivery.attention_attempt_id}",
            "correlation_id": delivery.window_id,
        }
        model_payload = delivery.attention_model_result.model_dump(mode="json")
        model_event_id = _event_id(
            world_id=delivery.world_id,
            role="model-result",
            identity=(
                delivery.attention_attempt_id,
                delivery.attention_model_result.model_result_ref,
            ),
        )
        model_event = WorldEvent.from_payload(
            **common,
            event_id=model_event_id,
            event_type="ModelResultRecorded",
            causation_id=delivery.attention_attempt_id,
            idempotency_key=domain_idempotency_key(
                event_type="ModelResultRecorded",
                world_id=delivery.world_id,
                payload=model_payload,
            )
            or model_event_id,
            payload=model_payload,
        )

        snapshot_events: list[WorldEvent] = []
        perception_events: list[WorldEvent] = []
        acceptance_id = "acceptance:external-perception:" + _digest(
            {
                "world_id": delivery.world_id,
                "attention_attempt_id": delivery.attention_attempt_id,
                "cursor": delivery.pinned_cursor.model_dump(mode="json"),
                "candidate_snapshot_hash": delivery.candidate_snapshot_hash,
            }
        )
        for selection in delivery.selections:
            snapshot_event_id = _event_id(
                world_id=delivery.world_id,
                role="snapshot",
                identity=(delivery.attention_attempt_id, selection.snapshot.signal_revision_ref),
            )
            snapshot_payload = ExternalSignalSnapshotAdoptedPayload(
                acceptance_id=acceptance_id,
                attention_attempt_id=delivery.attention_attempt_id,
                model_result_event_ref=model_event.event_id,
                model_result_event_payload_hash=model_event.payload_hash,
                snapshot=selection.snapshot,
            ).model_dump(mode="json")
            snapshot_event = WorldEvent.from_payload(
                **common,
                event_id=snapshot_event_id,
                event_type="ExternalSignalSnapshotAdopted",
                causation_id=model_event.event_id,
                idempotency_key=domain_idempotency_key(
                    event_type="ExternalSignalSnapshotAdopted",
                    world_id=delivery.world_id,
                    payload=snapshot_payload,
                )
                or snapshot_event_id,
                payload=snapshot_payload,
            )
            perception_event_id = _event_id(
                world_id=delivery.world_id,
                role="perception",
                identity=(
                    delivery.attention_attempt_id,
                    selection.snapshot.signal_revision_ref,
                    selection.channel.channel_ref,
                ),
            )
            perception_payload = ExternalPerceptionRecordedPayload(
                acceptance_id=acceptance_id,
                perception_id=selection.perception_id,
                actor_ref=delivery.actor_ref,
                attention_attempt_id=delivery.attention_attempt_id,
                window_id=delivery.window_id,
                pinned_cursor=delivery.pinned_cursor,
                encountered_world_time=delivery.encountered_world_time,
                observed_wall_time=delivery.observed_wall_time,
                candidate_ref=selection.candidate_ref,
                candidate_snapshot_hash=delivery.candidate_snapshot_hash,
                snapshot_ref=selection.snapshot.snapshot_ref,
                snapshot_event_ref=snapshot_event.event_id,
                snapshot_event_payload_hash=snapshot_event.payload_hash,
                channel=selection.channel,
                subjective_summary=selection.subjective_summary,
                epistemic_notes=selection.epistemic_notes,
                attended_context_refs=selection.attended_context_refs,
                attention_model_result_ref=delivery.attention_model_result.model_result_ref,
                attention_model_event_ref=model_event.event_id,
                attention_model_event_payload_hash=model_event.payload_hash,
                privacy_class=selection.privacy_class,
            ).model_dump(mode="json")
            perception_event = WorldEvent.from_payload(
                **common,
                event_id=perception_event_id,
                event_type="ExternalPerceptionRecorded",
                causation_id=snapshot_event.event_id,
                idempotency_key=domain_idempotency_key(
                    event_type="ExternalPerceptionRecorded",
                    world_id=delivery.world_id,
                    payload=perception_payload,
                )
                or perception_event_id,
                payload=perception_payload,
            )
            snapshot_events.append(snapshot_event)
            perception_events.append(perception_event)

        effects = (
            ExternalPerceptionAcceptedEffect(
                ordinal=0,
                role="model_result",
                event_id=model_event.event_id,
                event_type=model_event.event_type,
                payload_hash=model_event.payload_hash,
            ),
            *tuple(
                ExternalPerceptionAcceptedEffect(
                    ordinal=index + 1,
                    role="signal_snapshot",
                    event_id=event.event_id,
                    event_type=event.event_type,
                    payload_hash=event.payload_hash,
                )
                for index, event in enumerate(snapshot_events)
            ),
            *tuple(
                ExternalPerceptionAcceptedEffect(
                    ordinal=1 + len(snapshot_events) + index,
                    role="external_perception",
                    event_id=event.event_id,
                    event_type=event.event_type,
                    payload_hash=event.payload_hash,
                )
                for index, event in enumerate(perception_events)
            ),
        )
        manifest = build_external_perception_acceptance_manifest(
            acceptance_id=acceptance_id,
            attention_attempt_id=delivery.attention_attempt_id,
            window_id=delivery.window_id,
            candidate_snapshot_hash=delivery.candidate_snapshot_hash,
            evaluated_world_revision=delivery.pinned_cursor.world_revision,
            evaluated_deliberation_revision=delivery.pinned_cursor.deliberation_revision,
            evaluated_ledger_sequence=delivery.pinned_cursor.ledger_sequence,
            policy_digest=EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_DIGEST,
            effects=effects,
        )
        acceptance_payload = manifest.model_dump(mode="json")
        acceptance_event_id = _event_id(
            world_id=delivery.world_id,
            role="acceptance",
            identity=(delivery.attention_attempt_id, manifest.manifest_hash),
        )
        acceptance_event = WorldEvent.from_payload(
            **common,
            event_id=acceptance_event_id,
            event_type="AcceptanceRecorded",
            causation_id=delivery.attention_attempt_id,
            idempotency_key=domain_idempotency_key(
                event_type="AcceptanceRecorded",
                world_id=delivery.world_id,
                payload=acceptance_payload,
            )
            or acceptance_event_id,
            payload=acceptance_payload,
        )
        events = (acceptance_event, model_event, *snapshot_events, *perception_events)
        return _MaterializedDelivery(
            delivery=delivery,
            events=events,
            manifest_hash=manifest.manifest_hash,
            commit_id="commit:external-perception:"
            + _digest(
                {
                    "world_id": delivery.world_id,
                    "cursor": delivery.pinned_cursor.model_dump(mode="json"),
                    "manifest_hash": manifest.manifest_hash,
                }
            ),
        )


class ExternalPerceptionAcceptanceRuntime:
    """Accept only producer-issued deliveries at their complete pinned cursor."""

    __slots__ = ("ledger", "_recorder")

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        batch_issuer: AcceptedLedgerBatchIssuer,
        delivery_producer: ExternalPerceptionDeliveryProducer,
    ) -> None:
        self.ledger = ledger
        self._recorder = ExternalPerceptionAtomicRecorder(
            delivery_producer=delivery_producer,
            batch_issuer=batch_issuer,
        )

    @classmethod
    def in_memory(
        cls,
        *,
        world_id: str,
        delivery_producer: ExternalPerceptionDeliveryProducer,
    ) -> ExternalPerceptionAcceptanceRuntime:
        issuer = AcceptedLedgerBatchIssuer()
        return cls(
            ledger=WorldLedger.in_memory(
                world_id=world_id,
                accepted_batch_issuer=issuer,
            ),
            batch_issuer=issuer,
            delivery_producer=delivery_producer,
        )

    @classmethod
    def open(
        cls,
        *,
        path: Path,
        world_id: str,
        delivery_producer: ExternalPerceptionDeliveryProducer,
    ) -> ExternalPerceptionAcceptanceRuntime:
        issuer = AcceptedLedgerBatchIssuer()
        return cls(
            ledger=SQLiteWorldLedger(
                path=path,
                world_id=world_id,
                accepted_batch_issuer=issuer,
            ),
            batch_issuer=issuer,
            delivery_producer=delivery_producer,
        )

    def accept(
        self, handle: ExternalPerceptionDeliveryHandle
    ) -> ExternalPerceptionAcceptanceReceipt:
        batch, delivery, material = self._recorder.prepare_batch(handle)
        if delivery.world_id != self.ledger.world_id:
            raise ExternalPerceptionAcceptanceError("world_mismatch")
        committed = self.ledger.commit_accepted(batch, expected_cursor=delivery.pinned_cursor)
        acceptance_event, model_event = material.events[:2]
        snapshot_events = tuple(
            event
            for event in material.events
            if event.event_type == "ExternalSignalSnapshotAdopted"
        )
        perception_events = tuple(
            event for event in material.events if event.event_type == "ExternalPerceptionRecorded"
        )
        return ExternalPerceptionAcceptanceReceipt(
            attention_attempt_id=delivery.attention_attempt_id,
            acceptance_event_ref=acceptance_event.event_id,
            model_result_event_ref=model_event.event_id,
            perceptions=tuple(
                ExternalPerceptionAcceptedBinding(
                    perception_id=selection.perception_id,
                    perception_event_ref=perception_event.event_id,
                    snapshot_ref=selection.snapshot.snapshot_ref,
                    snapshot_event_ref=snapshot_event.event_id,
                )
                for selection, snapshot_event, perception_event in zip(
                    delivery.selections,
                    snapshot_events,
                    perception_events,
                    strict=True,
                )
            ),
            commit_result=committed,
        )

    def close(self) -> None:
        close = getattr(self.ledger, "close", None)
        if close is not None:
            close()


__all__ = [
    "EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_DIGEST",
    "EXTERNAL_PERCEPTION_ACCEPTANCE_POLICY_VERSION",
    "ExternalPerceptionAcceptanceError",
    "ExternalPerceptionAcceptanceReceipt",
    "ExternalPerceptionAcceptedBinding",
    "ExternalPerceptionAcceptanceRuntime",
    "ExternalPerceptionAtomicRecorder",
    "ExternalPerceptionDeliveryHandle",
    "ExternalPerceptionDeliveryProducer",
]
